"""Mail Archiver — sync lifecycle (1.1.0).

Every mbsync invocation is a transient systemd scope under a shared
`mail-archiver-syncs.slice`. Per-job state lives on disk at
`/run/mail-archiver/sync-jobs/<job_id>.json`, atomically written by the
gunicorn worker that drives the sync.

Why this replaces the 1.0.14 in-process `_SYNC_JOBS` dict:

  1. mbsync survives gunicorn worker recycling (systemd owns the scope,
     not gunicorn — worker restart no longer kills the sync)
  2. Any gunicorn worker can see any job (cross-worker visibility)
  3. Dashboard page-load can enumerate active jobs and auto-reattach
     SSE streams — logout / login / new browser tab all reconnect to
     live progress
  4. Cancel Sync = `systemctl stop mail-archiver-sync-<jobid>.scope`
     (clean, auditable, no signal-guessing)
  5. The deferred-restart guard from 1.0.17 can enumerate active syncs
     via `systemctl list-units 'mail-archiver-sync-*.scope' --state=active`
     instead of walking cgroups
"""

from __future__ import annotations

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


def list_active_states_for_user(user: str) -> List[dict]:
    """Only jobs currently running/starting (not done/error/cancelled)."""
    active = ('starting', 'running')
    return [s for s in list_states_for_user(user)
            if s.get('state') in active]


def is_authorized(state: Optional[dict], user: str) -> bool:
    return bool(state and state.get('user') == user)


# ---------------------------------------------------------------
# Purge (housekeeping)
# ---------------------------------------------------------------

def purge_old_states(max_age_seconds: int = 3600) -> int:
    """Delete DONE / ERROR job files older than max_age_seconds.
    Running jobs are never purged regardless of age.
    """
    cutoff = time.time() - max_age_seconds
    deleted = 0
    for st in list_all_states():
        if st.get('state') not in ('done', 'error', 'cancelled'):
            continue
        # last_updated is ISO8601 UTC; parse to epoch — best-effort
        lu = st.get('last_updated', '')
        try:
            ts = time.mktime(time.strptime(lu, '%Y-%m-%dT%H:%M:%SZ'))
        except (ValueError, TypeError):
            continue
        if ts < cutoff:
            if delete_state(st['job_id']):
                deleted += 1
    return deleted


# ---------------------------------------------------------------
# systemd-scope spawn
# ---------------------------------------------------------------

def _systemd_run_available() -> bool:
    try:
        r = subprocess.run(['systemd-run', '--version'],
                           capture_output=True, text=True, timeout=5)
        return r.returncode == 0
    except (subprocess.SubprocessError, OSError):
        return False


def spawn_scoped_mbsync(
    *,
    job_id: str,
    argv: List[str],
    email: Optional[str],
    uid: Optional[int] = None,
    gid: Optional[int] = None,
    cwd: Optional[str] = None,
    env: Optional[dict] = None,
) -> subprocess.Popen:
    """Launch `argv` (a mbsync invocation) inside a transient systemd
    scope. Returns the Popen for the systemd-run process itself; its
    stdout/stderr are line-buffered so callers can stream them into the
    progress state.

    The scope unit name is `mail-archiver-sync-<job_id>.scope`. The
    scope is created under `mail-archiver-syncs.slice` so the operator
    can `systemctl status mail-archiver-syncs.slice` to see all
    outstanding syncs at once.

    The `--uid`/`--gid` flags drop privileges before exec — this
    replaces the 1.0.14 subprocess(user=, group=, ...) + CAP_SETUID
    Ambient-caps dance. systemd-run needs to be run as root or with
    CAP_SYS_ADMIN to succeed with --uid; on the mail-archiver service
    (which runs as mail-archiver + Ambient CAP_SETUID/SETGID), the
    d-bus call to systemd is permitted.
    """
    unit = scope_unit_for(job_id)
    description = f'Mail Archiver sync {email or "all-accounts"}'
    cmd: List[str] = [
        'systemd-run',
        '--scope',
        f'--unit={unit}',
        f'--slice={SLICE_NAME}',
        f'--description={description}',
        '--quiet',
        '--collect',       # remove failed unit automatically
        '--send-sighup',   # graceful cleanup on scope stop
    ]
    if uid is not None:
        cmd.append(f'--uid={uid}')
    if gid is not None:
        cmd.append(f'--gid={gid}')
    if cwd:
        cmd.append(f'--working-directory={cwd}')
    if env:
        for k, v in env.items():
            cmd.append(f'--setenv={k}={v}')
    cmd.append(f'--setenv=MAIL_ARCHIVER_JOB_ID={job_id}')
    cmd.append('--')
    cmd.extend(argv)

    return subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=1,
        text=True,
    )


def stop_scope(job_id: str, timeout: int = 10) -> bool:
    """`systemctl stop mail-archiver-sync-<jobid>.scope`. Returns True
    on success; False if the scope was already gone or systemctl failed.
    """
    unit = scope_unit_for(job_id)
    try:
        r = subprocess.run(
            ['systemctl', 'stop', unit],
            capture_output=True, text=True, timeout=timeout,
        )
        return r.returncode == 0
    except (subprocess.SubprocessError, OSError):
        return False


def scope_is_active(job_id: str) -> bool:
    unit = scope_unit_for(job_id)
    try:
        r = subprocess.run(
            ['systemctl', 'is-active', unit],
            capture_output=True, text=True, timeout=5,
        )
        return (r.stdout or '').strip() == 'active'
    except (subprocess.SubprocessError, OSError):
        return False


def list_active_scopes() -> List[str]:
    """Return the unit names of active mail-archiver-sync-*.scope units.
    Used by the deferred-restart guard (postinst) as a cleaner
    replacement for cgroup-walking.
    """
    try:
        r = subprocess.run(
            ['systemctl', 'list-units',
             f'{SCOPE_UNIT_PREFIX}*.scope',
             '--state=active', '--no-legend', '--plain'],
            capture_output=True, text=True, timeout=10,
        )
        out: List[str] = []
        for line in (r.stdout or '').splitlines():
            parts = line.split()
            if parts and parts[0].startswith(SCOPE_UNIT_PREFIX):
                out.append(parts[0])
        return out
    except (subprocess.SubprocessError, OSError):
        return []


# ---------------------------------------------------------------
# Iso timestamp for state file consumers
# ---------------------------------------------------------------

def iso_utc_now() -> str:
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
