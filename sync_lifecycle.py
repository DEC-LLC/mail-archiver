"""Mail Archiver — sync lifecycle (1.1.1).

Per-job state lives on disk at `/run/mail-archiver/sync-jobs/<job_id>.json`,
atomically written by the gunicorn worker that drives the sync. This
gives cross-worker visibility, dashboard SSE re-attach across
logout/login/new-tab, and observability across worker recycling.

The 1.0.14→1.0.15 kernel-level setuid mechanism is what actually spawns
mbsync (subprocess.Popen(argv, user=uid, group=gid, ...) — the batcher
uses this too). Ambient CAP_SETUID/CAP_SETGID on mail-archiver.service
is what makes it work.

1.1.0 briefly attempted to spawn via `systemd-run --scope --uid=` for
cancel-via-`systemctl stop` cleanliness, but the D-Bus manage-units
call requires polkit auth that headless NAS installs do not provide —
every WebUI "Sync now" failed with `Failed to start transient scope
unit: Interactive authentication required.` (or Access denied where
polkit is absent). 1.1.1 reverts the spawn mechanism to direct Popen
(matching the batcher) while keeping the file-backed state design.

Cancel is unified on `os.killpg(os.getpgid(mbsync_pid), SIGTERM)` —
already the batched-mode fallback in admin.py:sync_cancel. Single-shot
mbsync is spawned with `start_new_session=True` so it owns its own
process group and the killpg is clean.
"""

from __future__ import annotations

import calendar
import json
import os
import re
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from typing import Iterable, List, Optional


JOB_STATE_DIR = '/run/mail-archiver/sync-jobs'
SCOPE_UNIT_PREFIX = 'mail-archiver-sync-'
SLICE_NAME = 'mail-archiver-syncs.slice'


# ---------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------

def state_file_for(job_id: str) -> str:
    return os.path.join(JOB_STATE_DIR, f'{job_id}.json')


def scope_unit_for(job_id: str) -> str:
    return f'{SCOPE_UNIT_PREFIX}{job_id}.scope'


def ensure_state_dir() -> bool:
    """Ensure JOB_STATE_DIR exists + is writable. Returns True on success.
    The tmpfiles.d config packaged in 1.1.0 handles this at boot; this is
    a runtime fallback for dev/first-install where the dir may not exist
    yet.
    """
    try:
        os.makedirs(JOB_STATE_DIR, mode=0o770, exist_ok=True)
        return True
    except OSError:
        return False


# ---------------------------------------------------------------
# job_id generation — keeps enough info in the name for humans
# ---------------------------------------------------------------

_SAFE_RE = re.compile(r'[^a-zA-Z0-9_-]')


def _safe(s: str) -> str:
    return _SAFE_RE.sub('_', s)


def new_job_id(user: str, email: Optional[str] = None) -> str:
    """`sync-<user>-<email-slug>-<uuid8>` — the scope unit name derives
    from this, so it must be valid for systemd (letters, digits, _, -).
    """
    target = _safe(email) if email else 'all'
    return f'sync-{_safe(user)}-{target}-{uuid.uuid4().hex[:8]}'


# ---------------------------------------------------------------
# Atomic state read/write
# ---------------------------------------------------------------

def _atomic_write(path: str, payload: dict) -> bool:
    ensure_state_dir()
    d = os.path.dirname(path)
    try:
        fd, tmp = tempfile.mkstemp(dir=d, prefix='.'+os.path.basename(path)+'.',
                                   suffix='.tmp')
    except OSError:
        return False
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(payload, f, separators=(',', ':'))
        try:
            os.chmod(tmp, 0o640)
        except OSError:
            pass
        os.rename(tmp, path)
        return True
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return False


def write_state(job_id: str, state: dict) -> bool:
    """Write the full state dict for a job. Caller is responsible for
    merging (read → mutate → write) if partial updates are desired.
    """
    state = dict(state)
    state['job_id'] = job_id
    state['last_updated'] = time.strftime('%Y-%m-%dT%H:%M:%SZ',
                                          time.gmtime())
    return _atomic_write(state_file_for(job_id), state)


def read_state(job_id: str) -> Optional[dict]:
    try:
        with open(state_file_for(job_id)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def update_state(job_id: str, **updates) -> Optional[dict]:
    """Read-modify-write. Progress dict is deep-merged; other keys are
    replaced. Non-atomic against other writers on the same job — but
    within-worker only one thread writes a given job's state, so this
    is safe by construction.
    """
    cur = read_state(job_id) or {'job_id': job_id}
    if 'progress' in updates and isinstance(updates['progress'], dict):
        p = dict(cur.get('progress') or {})
        p.update(updates['progress'])
        cur['progress'] = p
        updates = {k: v for k, v in updates.items() if k != 'progress'}
    cur.update(updates)
    write_state(job_id, cur)
    return cur


def delete_state(job_id: str) -> bool:
    try:
        os.unlink(state_file_for(job_id))
        return True
    except OSError:
        return False


# ---------------------------------------------------------------
# Enumeration
# ---------------------------------------------------------------

def list_all_states() -> List[dict]:
    ensure_state_dir()
    out: List[dict] = []
    try:
        for name in sorted(os.listdir(JOB_STATE_DIR)):
            if not name.endswith('.json'):
                continue
            job_id = name[:-5]
            st = read_state(job_id)
            if st:
                out.append(st)
    except OSError:
        pass
    return out


def list_states_for_user(user: str) -> List[dict]:
    return [s for s in list_all_states() if s.get('user') == user]


ACTIVE_STATES = ('starting', 'running')

# A job may legitimately sit in 'starting' with no pid recorded yet while
# the worker is still forking mbsync. Only treat a pid-less job as dead
# once it has been that way longer than this.
STARTING_GRACE_SECONDS = 180


def _state_epoch(st: dict) -> Optional[float]:
    """Best-effort epoch for a state's last_updated (ISO8601 UTC)."""
    lu = st.get('last_updated') or ''
    try:
        return calendar.timegm(time.strptime(lu, '%Y-%m-%dT%H:%M:%SZ'))
    except (ValueError, TypeError):
        return None


def _pid_alive(pid) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError, ProcessLookupError):
        return False


def is_state_orphaned(st: dict) -> bool:
    """True when a job CLAIMS to be starting/running but nothing is
    actually running for it.

    1.1.3: without this, a job whose worker died (gunicorn worker
    recycle, `systemctl restart mail-archiver`, host reboot) keeps its
    'starting'/'running' state forever — purge_old_states deliberately
    never reaps non-terminal jobs — and the WebUI sits on
    "Reconnecting to in-flight sync…" indefinitely with no sync running.
    """
    if st.get('state') not in ACTIVE_STATES:
        return False
    pid = st.get('mbsync_pid')
    if pid:
        return not _pid_alive(pid)
    # No pid recorded yet — dead only if it has been stuck past the grace.
    ts = _state_epoch(st)
    if ts is None:
        return True
    return (time.time() - ts) > STARTING_GRACE_SECONDS


def reap_orphaned_states() -> int:
    """Move orphaned starting/running jobs to 'error' so the UI stops
    reconnecting to them and purge_old_states can eventually clean up.
    Returns the number reaped.
    """
    reaped = 0
    for st in list_all_states():
        if not is_state_orphaned(st):
            continue
        update_state(
            st['job_id'],
            state='error',
            finished=time.strftime('%Y-%m-%d %H:%M:%S'),
            error=('Sync did not finish — the mail-archiver service was '
                   'restarted or the worker exited while this sync was '
                   'in flight. No sync is running; start a new one.'),
        )
        reaped += 1
    return reaped


def list_active_states_for_user(user: str) -> List[dict]:
    """Jobs genuinely in flight for `user`.

    Reaps orphans first (1.1.3) so a dead job never shows as active.
    """
    reap_orphaned_states()
    return [s for s in list_states_for_user(user)
            if s.get('state') in ACTIVE_STATES]


def is_authorized(state: Optional[dict], user: str) -> bool:
    return bool(state and state.get('user') == user)


# ---------------------------------------------------------------
# Purge (housekeeping)
# ---------------------------------------------------------------

def purge_old_states(max_age_seconds: int = 3600) -> int:
    """Delete DONE / ERROR job files older than max_age_seconds.
    Running jobs are never purged regardless of age.
    """
    reap_orphaned_states()
    cutoff = time.time() - max_age_seconds
    deleted = 0
    for st in list_all_states():
        if st.get('state') not in ('done', 'error', 'cancelled'):
            continue
        # last_updated is ISO8601 UTC. 1.1.3: was time.mktime(), which
        # interprets the UTC struct as LOCAL time — states aged out early
        # or late by the host's UTC offset. calendar.timegm is the
        # correct inverse of time.gmtime().
        ts = _state_epoch(st)
        if ts is None:
            continue
        if ts < cutoff:
            if delete_state(st['job_id']):
                deleted += 1
    return deleted


# ---------------------------------------------------------------
# mbsync spawn (direct kernel-setuid — see module docstring)
# ---------------------------------------------------------------

def spawn_scoped_mbsync(
    *,
    job_id: str,
    argv: List[str],
    email: Optional[str],
    uid: Optional[int] = None,
    gid: Optional[int] = None,
    extra_groups: Optional[List[int]] = None,
    cwd: Optional[str] = None,
    env: Optional[dict] = None,
) -> subprocess.Popen:
    """Launch `argv` (mbsync) as `uid:gid`, line-buffered stdout/stderr.
    Returns the Popen for mbsync itself so callers can `.stdout`, `.wait`,
    `.returncode`. `.pid` is the mbsync pid — record it in state so
    cancel can `os.killpg(os.getpgid(pid), SIGTERM)`.

    Runs in a new session (`start_new_session=True`) so the process
    group is well-defined for the cancel path. Ambient CAP_SETUID/SETGID
    on mail-archiver.service is what permits the setuid drop.

    Historical note: 1.1.0 attempted `systemd-run --scope --uid=` here.
    That path fails polkit `manage-units` auth on headless installs
    (no auth agent), breaking every WebUI Sync-now. 1.1.1 reverts to
    the batcher-style direct Popen. See module docstring.
    """
    _env: Optional[dict] = None
    if env:
        _env = dict(env)
        _env['MAIL_ARCHIVER_JOB_ID'] = job_id
    # (When env is None we inherit the parent env; MAIL_ARCHIVER_JOB_ID
    # is not exposed then, matching the batcher's contract.)

    kw: dict = {
        'stdout': subprocess.PIPE,
        'stderr': subprocess.STDOUT,
        'bufsize': 1,
        'text': True,
        'start_new_session': True,
    }
    if uid is not None:
        kw['user'] = uid
    if gid is not None:
        kw['group'] = gid
    if extra_groups:
        # 1.1.3: user=/group= set EUID/EGID only. Without extra_groups the
        # child loses every supplementary group — including mail-archiver,
        # which the PassCmd helper needs to read .secret_key.
        kw['extra_groups'] = list(extra_groups)
    if cwd is not None:
        kw['cwd'] = cwd
    if _env is not None:
        kw['env'] = _env

    return subprocess.Popen(list(argv), **kw)


def stop_scope(job_id: str, timeout: int = 10) -> bool:
    """Cancel a running sync by SIGTERM'ing the recorded mbsync process
    group. Returns True if a live pid was signalled; False if no state,
    no pid, or the pid was already gone.

    The scope-based cancel `systemctl stop mail-archiver-sync-<jobid>.scope`
    was 1.1.0-only and never worked on headless polkit-less installs;
    admin.sync_cancel's pgid fallback (present since 1.0.15 for the
    batched path) is now the ONE cancel mechanism.
    """
    import signal

    st = read_state(job_id)
    if not st:
        return False
    pid = st.get('mbsync_pid')
    if not pid:
        return False
    try:
        os.killpg(os.getpgid(int(pid)), signal.SIGTERM)
        return True
    except (OSError, ValueError, ProcessLookupError):
        return False


def scope_is_active(job_id: str) -> bool:
    """True iff the recorded mbsync pid is still alive. State-file
    driven — no systemd scope query."""
    st = read_state(job_id)
    if not st:
        return False
    pid = st.get('mbsync_pid')
    if not pid:
        return False
    return _pid_alive(pid)


def list_active_scopes() -> List[str]:
    """Return synthetic scope-unit names for jobs whose recorded pid is
    still alive. State-file driven — no systemd query. Kept for API
    compat with any external caller; internal enumeration uses
    `list_active_states_for_user` instead.
    """
    out: List[str] = []
    for st in list_all_states():
        pid = st.get('mbsync_pid')
        if not pid or not _pid_alive(pid):
            continue
        out.append(scope_unit_for(st.get('job_id', '')))
    return out


# ---------------------------------------------------------------
# Iso timestamp for state file consumers
# ---------------------------------------------------------------

def iso_utc_now() -> str:
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
