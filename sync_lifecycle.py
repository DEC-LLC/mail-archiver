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
import codecs
import json
import os
import re
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from typing import Iterable, List, Optional

import platform_compat as _pc


# /run/mail-archiver/sync-jobs on Linux; on Windows `.run\sync-jobs` under
# the data directory (platform_compat.runtime_dir, MAIL_ARCHIVER_RUN_DIR).
JOB_STATE_DIR = os.path.join(_pc.runtime_dir(), 'sync-jobs')
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

def _chown_like_dir(path: str, d: str) -> None:
    """1.1.7: the cron path runs as root. A root:root 0640 state file is
    unreadable to the mail-archiver service, so the WebUI would not see a
    cron sync at all. Hand root-created files to the directory's owner."""
    if not _pc.is_root():
        return
    try:
        st = os.stat(d)
        _pc.chown(path, st.st_uid, st.st_gid)
    except OSError:
        pass


def _atomic_write(path: str, payload: dict) -> bool:
    ensure_state_dir()
    d = os.path.dirname(path)
    try:
        fd, tmp = tempfile.mkstemp(dir=d, prefix='.'+os.path.basename(path)+'.',
                                   suffix='.tmp')
    except OSError:
        return False
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(payload, f, separators=(',', ':'))
        try:
            os.chmod(tmp, 0o640)
        except OSError:
            pass
        _chown_like_dir(tmp, d)
        # Atomic on both platforms; on Windows a reader that has the old
        # file open makes the replace wait (os.rename would fail there).
        _pc.replace_file(tmp, path)
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
    # Windows only: the file cannot be opened for the instant in which the
    # writer replaces it; that is not "no such job", so look again.
    for _attempt in range(5 if _pc.IS_WINDOWS else 1):
        try:
            with open(state_file_for(job_id), encoding='utf-8') as f:
                return json.load(f)
        except PermissionError:
            if _pc.IS_WINDOWS:
                time.sleep(0.02)
            continue
        except (OSError, ValueError):
            return None
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


ACTIVE_STATES = ('starting', 'running', 'syncing')

# 1.1.7: an in-flight job whose state file has not been touched for this
# long is reported as stalled. mbsync -V emits progress continuously and
# the worker flushes it at ~2Hz, so half an hour of silence is not normal.
# A stalled job still BLOCKS a new sync: its mbsync may still hold the
# box locks, and the service cannot signal it (no CAP_KILL).
STALL_SECONDS = 1800

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
    """True iff `pid` is a live process.

    1.1.4: MUST NOT use a bare `os.kill(pid, 0)` + `except OSError`. mbsync
    runs as the target PAM user while we run as mail-archiver, so signalling
    it raises PermissionError (EPERM) — which PROVES the process exists.
    Treating that as "dead" made the orphan reaper kill off healthy,
    actively-downloading syncs. Check /proc first (ownership-independent).
    """
    # The check itself lives in platform_compat: on Windows os.kill(pid, 0)
    # would terminate the process instead of probing it.
    return _pc.pid_alive(pid)


def current_boot_id() -> str:
    if _pc.IS_WINDOWS:
        return ''            # no boot id; owner_pid liveness governs
    try:
        with open('/proc/sys/kernel/random/boot_id', encoding='utf-8') as f:
            return f.read().strip()
    except OSError:
        return ''


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

    # Wrong boot => nothing from that boot survives. Unambiguous.
    boot = st.get('boot_id')
    cur_boot = current_boot_id()
    if boot and cur_boot and boot != cur_boot:
        return True

    # mbsync itself alive => definitely in flight.
    if _pid_alive(st.get('mbsync_pid')):
        return False

    # 1.1.4: owner_pid is the worker that runs the job. It is authoritative
    # for the windows where no mbsync pid exists yet but real work IS
    # happening — notably folder measurement for batch planning, which sets
    # state='running' BEFORE spawning mbsync and can easily run for minutes
    # on a large mailbox. While the owner lives, the job is not orphaned.
    owner = st.get('owner_pid')
    if owner is not None:
        return not _pid_alive(owner)

    # Legacy state file with no owner_pid: fall back to the age heuristic.
    if st.get('mbsync_pid'):
        return True
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


def is_state_stalled(st: dict, now: Optional[float] = None) -> bool:
    """True when an in-flight job has written nothing for STALL_SECONDS."""
    if st.get('state') not in ACTIVE_STATES:
        return False
    # The cron path runs mbsync synchronously and writes no progress, so
    # its silence means nothing; liveness alone (owner_pid) governs it.
    if st.get('heartbeat') is False:
        return False
    ts = _state_epoch(st)
    if ts is None:
        return False
    return ((now or time.time()) - ts) > STALL_SECONDS


def targets_overlap(a: Optional[str], b: Optional[str]) -> bool:
    """Two sync targets touch the same maildirs: same account, or either
    one is '__all__' (mbsync -a covers every account)."""
    a = a or '__all__'
    b = b or '__all__'
    return a == '__all__' or b == '__all__' or a == b


def claim_sync(user: str, target: str, initial: dict):
    """1.1.7: start a sync only if none is in flight for an overlapping
    target. Never queues.

    Returns (job_id, None) when the claim succeeded and the state file is
    written, or (None, blocking_state) when a live job already covers the
    target. The check and the create happen under one per-user flock, so
    two clicks that land on two gunicorn workers in the same millisecond
    cannot both win. Dead jobs are reaped first, so a crashed sync never
    blocks the next one.
    """
    ensure_state_dir()
    lock_path = os.path.join(JOB_STATE_DIR, f'.claim-{_safe(user)}.lock')
    # Read-only is enough for flock. The cron path (root, umask 022) and
    # the service (mail-archiver) both create this file; a root-created
    # 0640 lock is unwritable to the service, so never ask for write.
    fd = os.open(lock_path, os.O_RDONLY | os.O_CREAT, 0o660)
    try:
        _chown_like_dir(lock_path, JOB_STATE_DIR)
        _pc.lock_fd(fd, exclusive=True, blocking=True)
        for st in list_active_states_for_user(user):
            if targets_overlap(st.get('target'), target):
                return None, st
        email = None if target == '__all__' else target
        job_id = new_job_id(user, email)
        state = dict(initial)
        state.update({'job_id': job_id, 'user': user, 'email': email,
                      'target': target})
        if not write_state(job_id, state):
            raise OSError(f'cannot write sync state for {job_id}')
        return job_id, None
    finally:
        os.close(fd)   # releases the lock


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


def iter_mbsync_lines(stream):
    """Yield logical lines from mbsync, splitting on BOTH \n and \r.

    1.1.4: mbsync -V renders its progress counter in place — the format is
    `\rC: %d/%d  B: %d/%d  M: +%d/%d *%d/%d #%d/%d`, a LEADING carriage
    return and no trailing newline (verified against the isync 1.4.4 and
    1.5.1 binaries). `for line in proc.stdout` splits on \n only, so for
    the whole synchronize phase — the entire bulk of a large sync — it
    yields nothing at all. The WebUI therefore froze on the last
    newline-terminated line ("far side: N messages, 0 recent") and
    progress stayed empty until the run finished.

    Reads via os.read on the raw fd so a partial write is delivered
    immediately; `stream.read(n)` would block for n chars. The caller must
    not otherwise read `stream`.
    """
    try:
        fd = stream.fileno()
    except (AttributeError, OSError, ValueError):
        # Not a real pipe (in-memory stream, test double). Fall back to
        # plain iteration, still splitting embedded carriage returns.
        for raw in stream:
            if isinstance(raw, bytes):
                raw = raw.decode('utf-8', 'replace')
            for part in re.split(r'[\r\n]', raw):
                if part.strip():
                    yield part
        return

    decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
    buf = ''
    while True:
        try:
            chunk = os.read(fd, 4096)
        except (OSError, ValueError):
            break
        if not chunk:
            break
        buf += decoder.decode(chunk)
        parts = re.split(r'[\r\n]', buf)
        buf = parts.pop()          # trailing fragment, not yet terminated
        for part in parts:
            if part.strip():
                yield part
    buf += decoder.decode(b'', True)
    if buf.strip():
        yield buf


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
    }
    kw.update(_pc.detached_popen_kwargs())   # start_new_session on Linux
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
    st = read_state(job_id)
    if not st:
        return False
    pid = st.get('mbsync_pid')
    if not pid:
        return False
    return _pc.terminate_process_group(pid)


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


# ---------------------------------------------------------------
# 1.2.0: daily schedule, one lane per IMAP server
# ---------------------------------------------------------------
#
# The rule: accounts on the same server (all Gmail, all iCloud,
# all Microsoft, or all on one custom IMAP host) never sync at the same
# time, while one Gmail, one iCloud and one Microsoft account may run
# together. Each account gets a fixed hour of the day; the accounts on one
# server are spread evenly over the 24 hours, so every download has at
# least an hour (run_sync's 1-hour mbsync timeout) before the next one on
# that server starts.
#
# Before 1.2.0 an account was due 24 h after its last sync FINISHED, but
# the cron only looks once an hour, so every daily account started an hour
# later each day (a 25-hour "day") and the accounts drifted round the clock.
# Slots are fixed hours, so they do not drift.

import hashlib  # noqa: E402

HOURS_PER_DAY = 24


def server_key(host: Optional[str], provider: Optional[str] = None) -> str:
    """The lane an account belongs to: its IMAP host, lower-cased. An
    account with no host (an unknown custom provider) gets a lane of its
    own provider name."""
    h = (host or '').strip().lower()
    return h or f'provider:{(provider or "unknown").strip().lower()}'


def _slot_order(user: str, email: str) -> str:
    # Stable across runs and hosts, independent of account-list order.
    return hashlib.md5(f'{user}/{email}'.encode('utf-8')).hexdigest()


def assign_slots(entries: Iterable[tuple], start_hour: int = 0) -> dict:
    """Give each account a fixed hour of the day (0-23).

    `entries` is (user, email, server) per scheduled account. Returns
    {(user, email): hour}. The n accounts on one server get hours
    start, start + 24/n, start + 2*24/n, ... More than 24 accounts on one
    server share hours (lane_capacity() lets that lane run more than one
    per hour).
    """
    lanes: dict = {}
    for user, email, server in entries:
        lanes.setdefault(server, []).append((user, email))
    slots = {}
    for members in lanes.values():
        members.sort(key=lambda m: _slot_order(*m))
        n = len(members)
        for i, m in enumerate(members):
            slots[m] = (start_hour + (i * HOURS_PER_DAY) // n) % HOURS_PER_DAY
    return slots


def lane_capacity(n_accounts: int) -> int:
    """Accounts one server lane may start per hourly run: 1 up to 24
    accounts on that server, so that every account still syncs daily."""
    return max(1, -(-n_accounts // HOURS_PER_DAY))


def period_hours(interval_secs: int) -> int:
    return max(1, min(HOURS_PER_DAY, int(interval_secs) // 3600))


def last_slot_epoch(now: float, slot_hour: int, interval_secs: int) -> float:
    """The most recent top of an hour at or before `now` (local time) whose
    hour is the account's slot, repeated every interval: a daily account
    with slot 6 runs at 06:00, a 6-hourly one at 00, 06, 12 and 18."""
    period = period_hours(interval_secs)
    lt = time.localtime(now)
    top = now - lt.tm_min * 60 - lt.tm_sec
    back = (lt.tm_hour - slot_hour) % period
    return top - back * 3600


def is_due(now: float, slot_hour: int, interval_secs: int,
           last_started: Optional[float]) -> bool:
    """Due once per slot: not yet started since the slot's most recent
    occurrence. A slot that moves (accounts added to or removed from the
    server) never runs an account twice within half its interval."""
    if last_started is None:
        return True
    if last_started >= last_slot_epoch(now, slot_hour, interval_secs):
        return False
    return now - last_started >= interval_secs / 2
