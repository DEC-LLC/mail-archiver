"""Mail Archiver: large-mailbox / rebase sync engine (1.3.0).

A first sync of a very large folder (Gmail "All Mail" with 200,000
messages) cannot finish in one go: the provider closes the connection when
the account's daily IMAP download allowance is used up. Left inside the
regular hourly sync, such a folder also starves every other folder of the
account, because the allowance is gone before they are reached.

A bulk job takes chosen folders out of the regular sync and downloads them
separately, a bounded amount per day, until they are complete:

  * while the job is open the regular sync skips the folders (the
    exclusion is added to the pattern written into the generated .mbsyncrc;
    accounts.json is never changed, so the user's own folder pattern comes
    back exactly when the job closes);
  * one pull-only mbsync run of just those folders per eligible day, with
    the host's isync, as the mailbox owner, password through the normal
    PassCmd. The run is stopped when the day's byte budget is reached, when
    the time window closes, or by the provider (`[OVERQUOTA]` BYE, socket
    timeout). isync's own state and journal carry it over to the next day;
  * complete = mbsync exit 0 AND `.mbsyncstate` present AND no
    `.mbsyncstate.journal` for every chosen folder. Then the job closes and
    the folders go back into the regular sync.

This generalises a manual, script-driven "All Mail catch-up" done once
by hand.

This module is the engine only: no Flask, no import of app.py. Everything
that touches the outside world (mbsync, IMAP, the overlap guard, where the
state file is written) is passed in, so the tests drive it with fakes.
`bulk_sync_routes.py` wires it to the app.

It is also the mbsync supervisor (`bulk_sync.py supervise -- mbsync ...`),
see supervise().
"""

from __future__ import annotations

import base64
import collections
import json
import math
import os
import re
import signal
import subprocess
import sys
import threading
import time
import uuid
from typing import Callable, Dict, Iterable, List, Optional, Tuple

# The supervisor child is started with `python3 -I`, which leaves the
# script's directory out of sys.path; this file's own directory (the
# install dir) is the only place platform_compat may come from.
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import platform_compat as compat  # noqa: E402


STATE_VERSION = 1
MB = 1000 * 1000

# How often a running job measures the folder and re-reads its own state
# (budget, window, pause / cancel).
POLL_SECONDS = 15
# After asking mbsync to stop, how long to wait for it to exit.
STOP_GRACE_SECONDS = 120
# A run never lasts longer than this, whatever the window says (the old
# catch-up units had TimeoutStartSec=6h).
MAX_RUN_SECONDS = 6 * 3600
# IMAP socket timeout written into the bulk config (the catch-up scripts
# used 120 s; isync's default of 20 s is too short for a large folder).
BULK_TIMEOUT_SECONDS = 120
MAX_RUNS_KEPT = 400
MAX_HISTORY_KEPT = 20
MAX_FOLDERS = 20

OPEN_STATUSES = ('active', 'paused')
# After one of these the job waits for the next day.
RETRY_NEXT_DAY = ('provider-cap', 'timeout', 'error', 'incomplete')

STOP_REASONS = {
    'complete':     'Complete',
    'budget':       'Daily budget reached',
    'window':       'Time window closed',
    'provider-cap': 'Provider daily limit',
    'timeout':      'Connection timed out',
    'error':        'Error',
    'incomplete':   'Not complete yet',
    'paused':       'Paused',
    'cancelled':    'Cancelled',
    'interrupted':  'Interrupted',
}

# Daily IMAP download budgets. Only Gmail publishes a number (2500 MB per
# day over IMAP); its default sits just under that, so a run normally stops
# by itself before Google closes the connection. The others are cautious
# starting values, not provider figures. All of them can be changed per job;
# 0 means no budget.
PROVIDER_DEFAULTS = {
    'gmail': {
        'label': 'Gmail',
        'budget_mb': 2400,
        'window_start': '01:00', 'window_end': '06:00',
        'folders': ['[Gmail]/All Mail'],
        'note': 'Google allows about 2,500 MB of IMAP download per day and '
                'account. The default stays just under it.',
    },
    'microsoft': {
        'label': 'Outlook / Microsoft 365',
        'budget_mb': 10000,
        'window_start': '01:00', 'window_end': '06:00',
        'folders': ['Archive'],
        'note': 'Microsoft publishes no daily IMAP limit but slows down '
                'heavy downloads. 10,000 MB per day is a cautious start.',
    },
    'icloud': {
        'label': 'iCloud Mail',
        'budget_mb': 5000,
        'window_start': '01:00', 'window_end': '06:00',
        'folders': ['Archive'],
        'note': 'Apple publishes no daily IMAP limit. 5,000 MB per day is a '
                'cautious start.',
    },
    'yahoo': {
        'label': 'Yahoo Mail',
        'budget_mb': 5000,
        'window_start': '01:00', 'window_end': '06:00',
        'folders': ['Archive'],
        'note': 'Yahoo publishes no daily IMAP limit. 5,000 MB per day is a '
                'cautious start.',
    },
    'generic': {
        'label': 'Other IMAP server',
        'budget_mb': 10000,
        'window_start': '01:00', 'window_end': '06:00',
        'folders': [],
        'note': 'No provider limit is known. 10,000 MB per day keeps the '
                'download from filling the line all day; set 0 for no '
                'budget.',
    },
}


class BulkError(Exception):
    """A request the engine refuses; the message is safe to show."""


# The download itself is done by isync. On Linux it is a dependency of the
# package (a host without it says so when a run starts, as before). On
# Windows it is an optional program: without it no job can be set up or
# run, and the page says why. Reconcile needs no isync.
MBSYNC = 'mbsync'
NEEDS_ISYNC = ('The initial / rebase sync needs isync (the mbsync program), which '
               'was not found on PATH. Install isync and start Mail Archiver again. '
               'The regular sync and "Check the server now" work without it.')


def isync_missing() -> str:
    """'' when a bulk job can be set up and run on this host, else the
    message to show instead."""
    if not compat.IS_WINDOWS or compat.have_tool(MBSYNC):
        return ''
    return NEEDS_ISYNC


# ---------------------------------------------------------------------
# Small helpers: names, time, patterns
# ---------------------------------------------------------------------

def safe_account_name(email: str) -> str:
    """The account's channel / Maildir name, as generate_mbsyncrc builds it."""
    return email.replace('@', '_at_').replace('.', '_')


def state_filename(email: str) -> str:
    return 'bulk_sync_%s.json' % re.sub(r'[^A-Za-z0-9_+-]', '_',
                                        safe_account_name(email))


def provider_profile(provider: Optional[str], host: Optional[str]) -> str:
    """Which PROVIDER_DEFAULTS entry an account uses."""
    h = (host or '').lower()
    p = (provider or '').lower()
    if 'gmail' in h or 'googlemail' in h or (not h and p == 'gmail'):
        return 'gmail'
    if any(x in h for x in ('outlook', 'office365', 'hotmail', 'live.com')) \
            or (not h and p.startswith('hotmail')):
        return 'microsoft'
    if 'me.com' in h or 'icloud' in h or (not h and p == 'icloud'):
        return 'icloud'
    if 'yahoo' in h or (not h and p == 'yahoo'):
        return 'yahoo'
    return 'generic'


def fmt_time(epoch: float) -> str:
    return time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(epoch))


def day_of(epoch: float) -> str:
    """Local calendar day; budgets are counted per local day."""
    return time.strftime('%Y-%m-%d', time.localtime(epoch))


_HHMM_RE = re.compile(r'^([01]?\d|2[0-3]):([0-5]\d)$')


def parse_hhmm(value: str) -> int:
    m = _HHMM_RE.match((value or '').strip())
    if not m:
        raise BulkError(f'"{value}" is not a time of day (use HH:MM, 24-hour).')
    return int(m.group(1)) * 60 + int(m.group(2))


def normalize_hhmm(value: str) -> str:
    minutes = parse_hhmm(value)
    return '%02d:%02d' % divmod(minutes, 60)


def in_window(start: str, end: str, epoch: float) -> bool:
    """True when local time `epoch` is inside [start, end). Equal start and
    end mean "all day"; a window may run over midnight (22:00 to 06:00)."""
    s, e = parse_hhmm(start), parse_hhmm(end)
    if s == e:
        return True
    lt = time.localtime(epoch)
    m = lt.tm_hour * 60 + lt.tm_min
    return (s <= m < e) if s < e else (m >= s or m < e)


def window_close_epoch(start: str, end: str, epoch: float) -> Optional[float]:
    """When the window that contains `epoch` closes; None for "all day"."""
    s, e = parse_hhmm(start), parse_hhmm(end)
    if s == e:
        return None
    lt = time.localtime(epoch)
    midnight = epoch - (lt.tm_hour * 3600 + lt.tm_min * 60 + lt.tm_sec)
    close = midnight + e * 60
    if close <= epoch:
        close += 86400
    return close


def validate_folder(name: str) -> str:
    """A folder name as mbsync and the Maildir know it ("[Gmail]/All Mail").
    Refuses anything that could leave the account's Maildir."""
    name = (name or '').strip()
    if not name or len(name) > 255:
        raise BulkError('Folder name is empty or too long.')
    if any(ord(c) < 32 for c in name):
        raise BulkError('Folder name contains control characters.')
    parts = name.split('/')
    if name.startswith('/') or any(p in ('', '.', '..') for p in parts):
        raise BulkError(f'"{name}" is not a usable folder name.')
    return name


def validate_folders(names: Iterable[str]) -> List[str]:
    out: List[str] = []
    for n in names:
        if not (n or '').strip():
            continue
        v = validate_folder(n)
        if v not in out:
            out.append(v)
    if not out:
        raise BulkError('Choose at least one folder.')
    if len(out) > MAX_FOLDERS:
        raise BulkError(f'At most {MAX_FOLDERS} folders per job.')
    return out


def quote_box(name: str) -> str:
    """One quoted word of an mbsync `Patterns` line."""
    return '"' + name.replace('\\', '\\\\').replace('"', '\\"') + '"'


def exclusion_pattern(base: Optional[str], folders: Iterable[str]) -> str:
    """`base` with the folders excluded. mbsync applies patterns in order
    and the last match wins, so the negations go last: they win even over
    a base pattern that names the folder. The base itself is kept as it is.

        exclusion_pattern(None, ['[Gmail]/All Mail'])
            -> '* "![Gmail]/All Mail"'
    """
    base = (base or '').strip() or '*'
    words = [base]
    for f in folders:
        neg = quote_box('!' + f)
        if neg not in base:
            words.append(neg)
    return ' '.join(words)


# ---------------------------------------------------------------------
# State: one JSON file per account in the user's .config directory
# ---------------------------------------------------------------------

def new_doc(email: str) -> dict:
    return {'version': STATE_VERSION, 'email': email, 'job': None,
            'history': [], 'reconcile': None}


def _default_writer(directory: str):
    def write(filename: str, data: dict) -> None:
        tmp = os.path.join(directory, f'.{filename}.{os.getpid()}.tmp')
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2)
        os.chmod(tmp, 0o640)
        compat.replace_file(tmp, os.path.join(directory, filename))
    return write


class JobStore:
    """The state file of one account's bulk job.

    The WebUI (pause, cancel, settings) and the runner (cron or a detached
    "run now") change the same file from different processes, so every
    change is a read-modify-write under one per-user lock, and nobody keeps
    a copy across changes. `writer(filename, data)` does the atomic write;
    the app passes its own (same owner and mode as accounts.json).
    """

    LOCK_NAME = 'bulk_sync.lock'

    def __init__(self, directory: str, email: str,
                 writer: Optional[Callable[[str, dict], None]] = None):
        self.directory = str(directory)
        self.email = email
        self.filename = state_filename(email)
        self.path = os.path.join(self.directory, self.filename)
        self._writer = writer or _default_writer(self.directory)

    def load(self) -> dict:
        try:
            with open(self.path, encoding='utf-8') as f:
                doc = json.load(f)
        except (OSError, ValueError):
            return new_doc(self.email)
        if not isinstance(doc, dict):
            return new_doc(self.email)
        for k, v in new_doc(self.email).items():
            doc.setdefault(k, v)
        return doc

    def update(self, fn: Callable[[dict], None]) -> dict:
        """Apply `fn(doc)` to the current state and save it. If `fn`
        raises, nothing is written."""
        # Read-only is enough for flock, and both root (cron) and the
        # service can open a file the other one created (as claim_sync).
        fd = os.open(os.path.join(self.directory, self.LOCK_NAME),
                     os.O_RDONLY | os.O_CREAT, 0o644)
        try:
            _lock_wait(fd)
            doc = self.load()
            fn(doc)
            self._writer(self.filename, doc)
            return doc
        finally:
            try:
                compat.unlock_fd(fd)
            except OSError:
                pass
            os.close(fd)


LOCK_WAIT_SECONDS = 120


def _lock_wait(fd: int) -> None:
    """Exclusive lock, waiting for it. On Linux this is one blocking
    flock. A blocking lock on Windows gives up after ten seconds
    (LockUnavailable), so it is asked again until LOCK_WAIT_SECONDS."""
    deadline = time.monotonic() + LOCK_WAIT_SECONDS
    while True:
        try:
            compat.lock_fd(fd, exclusive=True, blocking=True)
            return
        except compat.LockUnavailable:
            if time.monotonic() >= deadline:
                raise


def job_is_open(job: Optional[dict]) -> bool:
    return bool(job) and job.get('status') in OPEN_STATUSES


def excluded_folders(doc: Optional[dict]) -> List[str]:
    """Folders the regular sync must skip: those of an open job. A paused
    job keeps them excluded (the folder is half-downloaded)."""
    job = (doc or {}).get('job') if isinstance(doc, dict) else None
    if not isinstance(job, dict) or not job_is_open(job):
        return []
    folders = job.get('folders')
    # The state file is JSON anyone with the account can damage. Only a
    # list of non-empty strings is a folder list; anything else (a string
    # would be excluded letter by letter, a number cannot be quoted) is no
    # exclusion at all.
    if not isinstance(folders, list) or not folders \
            or not all(isinstance(f, str) and f.strip() for f in folders):
        return []
    return list(folders)


def excluded_folders_for(config_dir: str, email: str) -> List[str]:
    try:
        return excluded_folders(JobStore(config_dir, email).load())
    except Exception:
        return []


# Top-level folders the mail importer creates inside an account's Maildir
# (mail_import.PREFIX: `PST/...`, `Imported/<type>/...`). They exist only
# locally. With `Patterns *`, `Sync Pull` and `Create Near`, isync 1.5.1
# answers a near-side box that has no far-side partner with
#   Error: channel X: far side box PST/Inbox cannot be opened.
# and exit status 1 on every whole-channel run, which the dashboard shows
# as a failed sync (measured against a local Dovecot). So the
# regular sync leaves them out.
IMPORT_TOP_FOLDERS = ('PST', 'Imported')
_SYNC_STATE_NAMES = ('.mbsyncstate', '.mbsyncstate.journal', '.mbsyncstate.new')
_IMPORT_WALK_LIMIT = 20000


def local_only_import_folders(maildir) -> List[str]:
    """Those of IMPORT_TOP_FOLDERS that exist in `maildir` and were never
    synced: no isync state file anywhere beneath them. A mailbox on the
    server that happens to be called `PST` or `Imported` has sync state in
    its folder and therefore stays in the regular sync. Links are never
    followed."""
    found = []
    for top in IMPORT_TOP_FOLDERS:
        root = os.path.join(str(maildir), top)
        try:
            if os.path.islink(root) or not os.path.isdir(root):
                continue
        except OSError:
            continue
        synced = False
        seen = 0
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            dirnames[:] = [d for d in dirnames if d not in ('cur', 'new', 'tmp')]
            if any(n in filenames for n in _SYNC_STATE_NAMES):
                synced = True
                break
            seen += 1
            if seen > _IMPORT_WALK_LIMIT:
                break
        if not synced:
            found.append(top)
    return found


def base_sync_pattern(config_dir: str, acct: dict) -> str:
    """The account's own folder pattern, with the importer's local-only
    folders left out. Without imported mail this is exactly the account's
    pattern (`*` by default), as before 1.3.0. Never raises."""
    base = acct.get('folder_pattern') or '*'
    try:
        maildir = os.path.join(os.path.dirname(str(config_dir).rstrip('/' + os.sep)),
                               safe_account_name(acct.get('email', '')))
        tops = local_only_import_folders(maildir)
        if not tops:
            return base
        names = []
        for top in tops:
            names += [top, top + '/*']
        return exclusion_pattern(base, names)
    except Exception:
        return base


def regular_sync_pattern(config_dir: str, acct: dict) -> str:
    """The `Patterns` value of an account's regular sync: the user's folder
    pattern, minus the importer's local-only folders, minus the folders of
    an open bulk job. Called by app.generate_mbsyncrc for every account of
    the user, so it never raises: whatever is wrong with one account's bulk
    state, the answer is that account's own pattern and the user's
    .mbsyncrc is still written."""
    base = base_sync_pattern(config_dir, acct)
    try:
        folders = excluded_folders_for(str(config_dir), acct.get('email', ''))
        return exclusion_pattern(base, folders) if folders else base
    except Exception:
        return base


def rc_patterns(rc_text: str, channel: str) -> Optional[str]:
    """The `Patterns` value of `channel` in a generated .mbsyncrc, or None
    when the channel (or its Patterns line) is not there."""
    in_channel = False
    for line in (rc_text or '').splitlines():
        s = line.strip()
        if s.startswith('Channel '):
            in_channel = s.split(None, 1)[1].strip() == channel
        elif in_channel and s.startswith('Patterns'):
            return s[len('Patterns'):].strip()
    return None


def exclusion_is_stuck(rc_text: str, doc: Optional[dict], acct: dict,
                       config_dir: Optional[str] = None) -> bool:
    """True when the account has no open job (closed, cancelled, or none)
    but its channel in the generated .mbsyncrc still carries an exclusion
    the account's own pattern does not ask for. That is what a runner
    leaves behind when it dies after it recorded the job as complete and
    before it regenerated the config: the regular sync would go on
    skipping a folder nobody is downloading any more."""
    try:
        if excluded_folders(doc):
            return False                    # an open job: the exclusion is right
        have = rc_patterns(rc_text, safe_account_name(acct.get('email', '')))
        # what the channel should say without a job; with `config_dir` that
        # includes the importer's local-only folders, which are not a
        # left-over job exclusion
        want = (base_sync_pattern(config_dir, acct) if config_dir
                else (acct.get('folder_pattern') or '*')).strip()
        return have is not None and have != want and '"!' in have \
            and have.startswith(want)
    except Exception:
        return False


def new_job(email: str, folders: Iterable[str], *, budget_bytes: int,
            window_start: str, window_end: str, profile: str = 'generic',
            original_pattern: Optional[str] = None, kind: str = 'initial',
            created_by: str = '', now: Optional[float] = None,
            local: Optional[dict] = None) -> dict:
    now = time.time() if now is None else now
    folders = validate_folders(folders)
    budget_bytes = int(budget_bytes)
    if budget_bytes < 0:
        raise BulkError('The daily budget cannot be negative.')
    return {
        'id': 'bulk-%s-%s' % (time.strftime('%Y%m%d-%H%M%S',
                                            time.localtime(now)),
                              uuid.uuid4().hex[:6]),
        'kind': kind,
        'status': 'active',
        'email': email,
        'folders': folders,
        'created': fmt_time(now),
        'created_by': created_by,
        'profile': profile,
        'budget_bytes': budget_bytes,
        'window_start': normalize_hhmm(window_start),
        'window_end': normalize_hhmm(window_end),
        # The account's own folder pattern when the job started (None: it
        # had none). accounts.json is not changed by a job; this is the
        # record of what the regular sync goes back to.
        'original_pattern': original_pattern or None,
        'excluded_pattern': exclusion_pattern(original_pattern, folders),
        'blocked_day': None,
        'running': None,
        'last_stop': None,
        'local': local,
        'server': {},
        'runs': [],
        'closed': None,
    }


def _summary_for_history(job: dict) -> dict:
    keep = ('id', 'kind', 'status', 'folders', 'created', 'closed',
            'budget_bytes', 'window_start', 'window_end', 'last_stop',
            'local', 'original_pattern')
    out = {k: job.get(k) for k in keep}
    runs = job.get('runs') or []
    out['runs'] = len(runs)
    out['days'] = len({r.get('day') for r in runs})
    out['bytes'] = sum(int(r.get('bytes') or 0) for r in runs)
    out['messages'] = sum(int(r.get('messages') or 0) for r in runs)
    return out


def start_job(store: JobStore, job: dict) -> dict:
    """Open `job` for the account. Refused while another job is open."""
    def fn(doc):
        cur = doc.get('job')
        if job_is_open(cur):
            raise BulkError('This account already has an initial / rebase '
                            'sync. Cancel it or let it finish first.')
        if cur:
            doc['history'] = ([_summary_for_history(cur)]
                              + list(doc.get('history') or []))[:MAX_HISTORY_KEPT]
        doc['job'] = job
    return store.update(fn)


def set_status(store: JobStore, action: str,
               now: Optional[float] = None) -> dict:
    """pause / resume / cancel. Cancel closes the job and so ends the
    exclusion; it never touches downloaded mail."""
    now = time.time() if now is None else now

    def fn(doc):
        job = doc.get('job')
        if not job_is_open(job):
            raise BulkError('There is no open initial / rebase sync for '
                            'this account.')
        if action == 'pause':
            if job['status'] != 'active':
                raise BulkError('The job is already paused.')
            job['status'] = 'paused'
        elif action == 'resume':
            if job['status'] != 'paused':
                raise BulkError('The job is not paused.')
            job['status'] = 'active'
            job['blocked_day'] = None
        elif action == 'cancel':
            job['status'] = 'cancelled'
            job['closed'] = fmt_time(now)
            if not job.get('running'):
                job['last_stop'] = {'reason': 'cancelled', 'at': fmt_time(now),
                                    'detail': 'Cancelled; downloaded mail was kept.'}
        else:
            raise BulkError(f'Unknown action "{action}".')
    return store.update(fn)


def update_settings(store: JobStore, *, budget_bytes: int,
                    window_start: str, window_end: str) -> dict:
    budget_bytes = int(budget_bytes)
    if budget_bytes < 0:
        raise BulkError('The daily budget cannot be negative.')
    ws, we = normalize_hhmm(window_start), normalize_hhmm(window_end)

    def fn(doc):
        job = doc.get('job')
        if not job_is_open(job):
            raise BulkError('There is no open initial / rebase sync for '
                            'this account.')
        job['budget_bytes'] = budget_bytes
        job['window_start'] = ws
        job['window_end'] = we
    return store.update(fn)


def bytes_on_day(job: dict, day: str, include_running: bool = True) -> int:
    total = sum(int(r.get('bytes') or 0) for r in (job.get('runs') or [])
                if r.get('day') == day)
    running = job.get('running')
    if include_running and running and running.get('day') == day:
        total += int(running.get('bytes') or 0)
    return total


def eligibility(job: Optional[dict], now: float,
                trigger: str = 'cron') -> Tuple[bool, str]:
    """May a run start now? The budget always applies. The window and the
    "wait until tomorrow" after a provider stop apply to scheduled runs; a
    manual "run now" is the user's way past both."""
    if not job:
        return False, 'no-job'
    if job.get('status') != 'active':
        return False, job.get('status') or 'no-job'
    day = day_of(now)
    budget = int(job.get('budget_bytes') or 0)
    if budget and bytes_on_day(job, day, include_running=False) >= budget:
        return False, 'budget'
    if trigger == 'cron':
        if not in_window(job['window_start'], job['window_end'], now):
            return False, 'window'
        if job.get('blocked_day') == day:
            return False, 'retry-tomorrow'
    return True, 'ok'


# ---------------------------------------------------------------------
# Maildir measurement and isync state
# ---------------------------------------------------------------------

def folder_maildir(account_maildir: str, folder: str) -> str:
    """`SubFolders Verbatim`: the folder name is the path below the
    account's Maildir."""
    return os.path.join(str(account_maildir), *validate_folder(folder).split('/'))


def count_messages(path: str) -> int:
    total = 0
    for sub in ('cur', 'new'):
        try:
            with os.scandir(os.path.join(path, sub)) as it:
                total += sum(1 for _ in it)
        except OSError:
            pass
    return total


class MaildirMeter:
    """Messages and bytes in one folder's cur/ and new/.

    A run measures every few seconds, and the folder can hold hundreds of
    thousands of files, so sizes are remembered by file name: each
    measurement lists the two directories and stats only the names it has
    not seen."""

    def __init__(self, path: str):
        self.path = path
        self._sizes: Dict[str, Dict[str, int]] = {'cur': {}, 'new': {}}

    def measure(self) -> Tuple[int, int]:
        count = total = 0
        for sub in ('cur', 'new'):
            known = self._sizes[sub]
            current: Dict[str, int] = {}
            try:
                it = os.scandir(os.path.join(self.path, sub))
            except OSError:
                self._sizes[sub] = current
                continue
            with it:
                for entry in it:
                    size = known.get(entry.name)
                    if size is None:
                        try:
                            size = entry.stat(follow_symlinks=False).st_size
                        except OSError:
                            continue
                    current[entry.name] = size
            self._sizes[sub] = current
            count += len(current)
            total += sum(current.values())
        return count, total


def folder_complete(path: str) -> bool:
    """isync finished the folder: its state file is written and no journal
    is left (the test both catch-up scripts used)."""
    return (os.path.isfile(os.path.join(path, '.mbsyncstate'))
            and not os.path.exists(os.path.join(path, '.mbsyncstate.journal')))


def read_sync_state(path: str) -> Optional[dict]:
    """Header of a folder's `.mbsyncstate`: the highest server UID pulled,
    the server's UIDVALIDITY, and when the file was written (the time of
    the last finished sync of that folder). None when there is no state."""
    p = os.path.join(path, '.mbsyncstate')
    try:
        mtime = os.stat(p).st_mtime
        header = {}
        with open(p, encoding='utf-8', errors='replace') as f:
            for i, line in enumerate(f):
                line = line.strip()
                if not line or i > 20:
                    break
                parts = line.split(None, 1)
                if len(parts) == 2:
                    header[parts[0]] = parts[1]
    except OSError:
        return None

    def num(*keys):
        for k in keys:
            try:
                return int(header[k])
            except (KeyError, ValueError):
                pass
        return None

    return {
        'max_pulled_uid': num('MaxPulledUid'),
        'uidvalidity': num('FarUidValidity', 'MasterUidValidity'),
        'mtime': mtime,
        'journal': os.path.exists(p + '.journal'),
    }


# ---------------------------------------------------------------------
# The bulk mbsync config
# ---------------------------------------------------------------------

def build_bulk_rc(source_rc: str, channel: str, folders: Iterable[str],
                  timeout: int = BULK_TIMEOUT_SECONDS) -> str:
    """The config of a bulk run, cut from the user's generated .mbsyncrc:
    only this account's block, `Patterns` set to exactly the job's folders,
    and an IMAP timeout of at least `timeout` seconds. Everything else
    (PassCmd, TLS, `Sync Pull`, `Expunge None`) is taken over unchanged, so
    the password still comes from the normal credential helper and nothing
    secret is written."""
    lines = source_rc.splitlines()
    try:
        start = next(i for i, l in enumerate(lines)
                     if l.strip() == f'IMAPAccount {channel}')
    except StopIteration:
        raise BulkError(f'The account {channel} is not in the generated '
                        f'sync configuration (is it enabled?).')
    end = len(lines)
    for i in range(start + 1, len(lines)):
        if lines[i].startswith('IMAPAccount '):
            end = i
            break
    block = lines[start:end]
    while block and (not block[-1].strip() or block[-1].startswith('#')):
        block.pop()

    existing = 0
    out: List[str] = []
    patterns = ' '.join(quote_box(f) for f in validate_folders(folders))
    replaced = False
    in_channel = False
    for line in block:
        s = line.strip()
        if s.startswith('Timeout '):
            try:
                existing = max(existing, int(s.split()[1]))
            except (IndexError, ValueError):
                pass
            continue
        if s.startswith('Channel '):
            in_channel = s.split(None, 1)[1].strip() == channel
        if in_channel and s.startswith('Patterns'):
            out.append(f'Patterns {patterns}')
            replaced = True
            continue
        out.append(line)
    if not replaced:
        raise BulkError('The generated sync configuration has no Patterns '
                        f'line for {channel}.')
    out.insert(1, f'Timeout {max(existing, int(timeout))}')
    head = ['# Mail Archiver initial / rebase sync. Generated for one run; '
            'do not edit.', '']
    return '\n'.join(head + out) + '\n'


def verify_pull_only(rc_text: str) -> Tuple[bool, str]:
    """A bulk run may only ever download. Refuse a config that could push
    to the provider or delete on either side."""
    channels = 0
    sync_lines: List[str] = []
    for raw in rc_text.splitlines():
        s = ' '.join(raw.split())
        if not s or s.startswith('#'):
            continue
        word = s.split(' ', 1)[0]
        if word == 'Channel':
            channels += 1
        elif word == 'Sync':
            sync_lines.append(s)
        elif word == 'Expunge' and s != 'Expunge None':
            return False, f'the configuration has "{s}" (must be "Expunge None")'
        elif word == 'Remove' and s != 'Remove None':
            return False, f'the configuration has "{s}" (must be "Remove None")'
        elif word in ('ExpungeSolo', 'ExpireUnread', 'MaxMessages'):
            return False, f'the configuration has "{s}"'
    if channels != 1:
        return False, f'the configuration has {channels} channels, expected 1'
    if sync_lines != ['Sync Pull']:
        got = ', '.join(f'"{s}"' for s in sync_lines) or 'no Sync line'
        return False, f'the configuration is not pull-only ({got}; must be "Sync Pull")'
    return True, ''


# ---------------------------------------------------------------------
# Running mbsync: the supervisor and its handle
# ---------------------------------------------------------------------

def supervise(argv: List[str], grace: int = 90) -> int:
    """Run `argv` (mbsync) and stop it when our stdin says so.

    mbsync runs as the mailbox owner. The service that started it runs as
    another user without CAP_KILL, so it cannot signal mbsync itself. This
    small supervisor is started as the mailbox owner too and is told to
    stop through its stdin: one line, or end-of-file. End-of-file is also
    what it sees when the runner dies, so an unsupervised mbsync never
    keeps downloading past the budget. On stop it sends SIGTERM (isync's
    journal makes the run resumable) and, only if mbsync is still there
    after `grace` seconds, SIGKILL.
    """
    # Windows: no console window for mbsync (nothing extra on Linux, where
    # mbsync stays in the supervisor's own session).
    extra = compat.detached_popen_kwargs() if compat.IS_WINDOWS else {}
    proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, **extra)
    asked: List[Optional[float]] = [None]

    def ask(*_):
        if asked[0] is None:
            asked[0] = time.time()
            try:
                proc.send_signal(signal.SIGTERM)
            except OSError:
                pass

    signal.signal(signal.SIGTERM, ask)
    signal.signal(signal.SIGINT, ask)
    import select
    stdin_said = None
    if compat.IS_WINDOWS:
        # select() takes sockets only on Windows: a thread waits for the
        # line (or end-of-file) on stdin instead.
        stdin_said = threading.Event()

        def wait_stdin():
            try:
                os.read(0, 64)
            except OSError:
                pass
            stdin_said.set()
        threading.Thread(target=wait_stdin, daemon=True).start()
    while True:
        rc = proc.poll()
        if rc is not None:
            return rc if rc >= 0 else 128 - rc
        if asked[0] is None and stdin_said is not None:
            if stdin_said.wait(1.0):
                ask()
        elif asked[0] is None:
            try:
                ready, _, _ = select.select([0], [], [], 1.0)
            except (OSError, ValueError):
                ready = [0]
            if ready:
                try:
                    os.read(0, 64)
                except OSError:
                    pass
                ask()
        else:
            time.sleep(0.5)
            if time.time() - asked[0] > grace:
                try:
                    proc.kill()
                except OSError:
                    pass


def supervisor_argv(mbsync_argv: List[str]) -> List[str]:
    # -I: no PYTHON* environment, no user site, no script directory on
    # sys.path. The supervisor needs the standard library and
    # platform_compat, which this module finds in its own directory.
    return [sys.executable or 'python3', '-I', os.path.abspath(__file__),
            'supervise', '--'] + list(mbsync_argv)


class ProcessHandle:
    """A supervised mbsync, as the runner sees it."""

    def __init__(self, popen: subprocess.Popen):
        self.popen = popen
        self.pid = popen.pid
        self._lines: collections.deque = collections.deque(maxlen=200)
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()

    def _read(self):
        try:
            for line in self.popen.stdout:
                for part in re.split(r'[\r\n]', line):
                    if part.strip():
                        self._lines.append(part)
        except (OSError, ValueError):
            pass

    def poll(self) -> Optional[int]:
        return self.popen.poll()

    def request_stop(self) -> None:
        try:
            if self.popen.stdin and not self.popen.stdin.closed:
                self.popen.stdin.write('stop\n')
                self.popen.stdin.close()
        except (OSError, ValueError):
            pass

    def kill(self) -> None:
        self.request_stop()
        compat.kill_process_tree(self.pid)

    def output(self) -> str:
        if self.popen.poll() is not None:
            self._reader.join(timeout=5)
        return '\n'.join(self._lines)


def spawn_supervised(mbsync_argv: List[str], **popen_kwargs) -> ProcessHandle:
    """Start mbsync under the supervisor. `popen_kwargs` carries the
    drop to the mailbox owner (user, group, extra_groups, cwd, env), the
    same ones a normal sync uses."""
    kw = {
        'stdin': subprocess.PIPE,
        'stdout': subprocess.PIPE,
        'stderr': subprocess.STDOUT,
        'text': True,
        'errors': 'replace',
        'bufsize': 1,
    }
    kw.update(compat.detached_popen_kwargs())   # Linux: start_new_session
    kw.update(popen_kwargs)
    return ProcessHandle(subprocess.Popen(supervisor_argv(mbsync_argv), **kw))


def classify_exit(rc: Optional[int], output: str,
                  complete: bool) -> Tuple[str, str]:
    """Why a run that nobody stopped ended."""
    low = (output or '').lower()
    if rc == 0:
        if complete:
            return 'complete', ''
        return 'incomplete', ('mbsync finished without an error, but the '
                              'folder state is not final yet (the journal '
                              'is still there or the state file is missing).')
    if 'overquota' in low or 'bandwidth limits' in low:
        return 'provider-cap', ('The provider closed the connection: the '
                                'account\'s daily download limit is used up.')
    if re.search(r'\btimeout\b|timed out', low):
        return 'timeout', 'The connection to the mail server timed out.'
    return 'error', f'mbsync ended with exit code {rc}.'


# ---------------------------------------------------------------------
# The runner
# ---------------------------------------------------------------------

class BulkJobRunner:
    """One run of one account's bulk job.

    Dependencies (all injected):
      store            JobStore
      account_maildir  the account's Maildir directory
      prepare_config   (folders) -> (rc_path, rc_text) for the bulk run
      start            (rc_path) -> handle with pid, poll(), request_stop(),
                       kill(), output()
      claim            () -> (token, blocker): the app's no-overlapping-
                       syncs guard for this account; token None = blocked
      release          (token, ok, message) -> None
      heartbeat        (token, pid, message) -> None, optional
      server_busy      (token) -> something truthy when the account's IMAP
                       server has another sync in flight (scheduled runs)
      server_counts    (folders) -> {folder: {'messages', 'uidnext'}}, optional
      on_closed        () -> None: regenerate the regular config (the
                       exclusion ends when the job completes)
      after_run        () -> None, optional (search index)
      redact           (text) -> text: the app's credential scrubber
      clock / sleep / pid_alive / log
    """

    def __init__(self, *, store: JobStore, account_maildir: str,
                 prepare_config, start, claim, release,
                 heartbeat=None, server_busy=None, server_counts=None,
                 on_closed=None, after_run=None,
                 redact=lambda t: t, clock=time.time, sleep=time.sleep,
                 pid_alive=None, log=None, meter_factory=MaildirMeter,
                 poll_seconds: float = POLL_SECONDS,
                 stop_grace: float = STOP_GRACE_SECONDS,
                 max_run_seconds: float = MAX_RUN_SECONDS):
        self.store = store
        self.account_maildir = str(account_maildir)
        self.prepare_config = prepare_config
        self.start = start
        self.claim = claim
        self.release = release
        self.heartbeat = heartbeat
        self.server_busy = server_busy
        self.server_counts = server_counts
        self.on_closed = on_closed
        self.after_run = after_run
        self.redact = redact
        self.clock = clock
        self.sleep = sleep
        self.pid_alive = pid_alive or _pid_alive
        self.log = log or (lambda msg: None)
        self.meter_factory = meter_factory
        self.poll_seconds = poll_seconds
        self.stop_grace = stop_grace
        self.max_run_seconds = max_run_seconds
        self._meters: Dict[str, MaildirMeter] = {}
        self._job_id = None

    # -- measuring ----------------------------------------------------

    def _path(self, folder: str) -> str:
        return folder_maildir(self.account_maildir, folder)

    def measure(self, folders: Iterable[str]) -> dict:
        per = {}
        for f in folders:
            meter = self._meters.get(f)
            if meter is None:
                meter = self._meters[f] = self.meter_factory(self._path(f))
            n, b = meter.measure()
            per[f] = {'messages': n, 'bytes': b}
        return {'messages': sum(v['messages'] for v in per.values()),
                'bytes': sum(v['bytes'] for v in per.values()),
                'folders': per}

    def all_complete(self, folders: Iterable[str]) -> bool:
        return all(folder_complete(self._path(f)) for f in folders)

    # -- one run ------------------------------------------------------

    def run(self, trigger: str = 'cron') -> dict:
        """Run the job once if it may run now. Returns
        {'ran': bool, 'reason': str, ...}; never raises for an expected
        refusal."""
        job = self.store.load().get('job')
        ok, why = eligibility(job, self.clock(), trigger)
        if not ok:
            return {'ran': False, 'reason': why}
        token, blocker = self.claim()
        if not token:
            return {'ran': False, 'reason': 'overlap', 'blocker': blocker}
        # One lane per IMAP server, as the hourly sync keeps it. Checked
        # after the claim, so two starts cannot both see a free server.
        if trigger == 'cron' and self.server_busy:
            busy = self.server_busy(token)
            if busy:
                self.release(token, True, 'Not started: the mail server '
                             'already has a sync in flight.')
                return {'ran': False, 'reason': 'server-busy', 'blocker': busy}
        ok_exit, message = False, ''
        try:
            result = self._run_claimed(trigger, token)
            ok_exit = result.get('reason') != 'error'
            message = result.get('detail') or STOP_REASONS.get(
                result.get('reason'), '')
            return result
        except Exception as e:
            message = f'{type(e).__name__}: {e}'
            raise
        finally:
            self.release(token, ok_exit, message)

    def _record(self, run: dict, *, block: bool, closed: bool = False) -> dict:
        def fn(doc):
            job = doc.get('job')
            if not job or job.get('id') != self._job_id:
                return      # cancelled and replaced by a new job meanwhile
            job['running'] = None
            job['runs'] = (list(job.get('runs') or []) + [run])[-MAX_RUNS_KEPT:]
            job['last_stop'] = {'reason': run['reason'], 'at': run['end'],
                                'detail': run.get('detail', '')}
            if run.get('messages_after') is not None:
                job['local'] = {'messages': run['messages_after'],
                                'bytes': run['bytes_after'], 'at': run['end']}
            if block:
                job['blocked_day'] = day_of(run['end_epoch'])
            if closed and job.get('status') in OPEN_STATUSES:
                job['status'] = 'complete'
                job['closed'] = run['end']
        return self.store.update(fn)

    def _recover_interrupted(self) -> None:
        """A previous runner died (reboot, kill) before it could write its
        run record. Count what it downloaded, so the day's budget stays
        honest."""
        job = self.store.load().get('job') or {}
        running = job.get('running')
        if not running:
            return
        now = self.clock()
        after = self.measure(job.get('folders') or [])
        run = {
            'day': running.get('day') or day_of(now),
            'start': running.get('start'), 'start_epoch': running.get('start_epoch'),
            'end': fmt_time(now), 'end_epoch': now,
            'trigger': running.get('trigger'), 'rc': None,
            'reason': 'interrupted',
            'detail': 'The runner stopped before the run ended (restart or '
                      'reboot). The download continues from where it was.',
            'messages_before': running.get('messages_before'),
            'messages_after': after['messages'],
            'messages': max(0, after['messages'] - int(running.get('messages_before') or 0)),
            'bytes_before': running.get('bytes_before'),
            'bytes_after': after['bytes'],
            'bytes': max(0, after['bytes'] - int(running.get('bytes_before') or 0)),
            'output_tail': '',
        }
        self._record(run, block=False)
        self.log(f'recovered an interrupted run: +{run["messages"]} messages')

    def _refused(self, trigger: str, detail: str, before: Optional[dict] = None) -> dict:
        now = self.clock()
        b = before or {'messages': None, 'bytes': None}
        run = {
            'day': day_of(now), 'start': fmt_time(now), 'start_epoch': now,
            'end': fmt_time(now), 'end_epoch': now, 'trigger': trigger,
            'rc': None, 'reason': 'error', 'detail': detail,
            'messages_before': b['messages'], 'messages_after': b['messages'],
            'messages': 0, 'bytes_before': b['bytes'], 'bytes_after': b['bytes'],
            'bytes': 0, 'output_tail': '',
        }
        self._record(run, block=True)
        self.log(f'refused: {detail}')
        return {'ran': False, 'reason': 'error', 'detail': detail}

    def _run_claimed(self, trigger: str, token) -> dict:
        self._job_id = (self.store.load().get('job') or {}).get('id')
        self._recover_interrupted()
        job = self.store.load().get('job')
        ok, why = eligibility(job, self.clock(), trigger)
        if not ok:
            return {'ran': False, 'reason': why}
        folders = list(job['folders'])

        try:
            rc_path, rc_text = self.prepare_config(folders)
        except BulkError as e:
            return self._refused(trigger, str(e))
        except Exception as e:
            return self._refused(trigger, self.redact(
                f'Could not prepare the sync configuration: '
                f'{type(e).__name__}: {e}'))
        pull_only, why = verify_pull_only(rc_text)
        if not pull_only:
            return self._refused(
                trigger, f'Refused to run: {why}. Nothing was downloaded.')

        if self.server_counts:
            try:
                counts = self.server_counts(folders) or {}
            except Exception:
                counts = {}
            if counts:
                def fn(doc):
                    j = doc.get('job')
                    if j and j.get('id') == self._job_id:
                        server = dict(j.get('server') or {})
                        stamp = fmt_time(self.clock())
                        for f, c in counts.items():
                            server[f] = {'messages': c.get('messages'),
                                         'uidnext': c.get('uidnext'),
                                         'checked': stamp}
                        j['server'] = server
                self.store.update(fn)

        before = self.measure(folders)
        start = self.clock()
        day = day_of(start)
        budget = int(job.get('budget_bytes') or 0)
        used = bytes_on_day(job, day, include_running=False)
        deadline = start + self.max_run_seconds
        deadline_reason = 'The longest allowed run time was reached.'
        if trigger == 'cron':
            close = window_close_epoch(job['window_start'], job['window_end'], start)
            if close is not None and close < deadline:
                deadline = close
                deadline_reason = (f'The time window ({job["window_start"]} to '
                                   f'{job["window_end"]}) closed.')

        self.log(f'start ({trigger}), {before["messages"]} messages local, '
                 f'{used} of {budget or "unlimited"} bytes used today')
        handle = self.start(rc_path)

        def set_running(doc, cur=before, now=start):
            j = doc.get('job')
            if not j or j.get('id') != self._job_id:
                return
            j['running'] = {
                'pid': handle.pid, 'trigger': trigger, 'day': day,
                'start': fmt_time(start), 'start_epoch': start,
                'messages_before': before['messages'],
                'bytes_before': before['bytes'],
                'messages': max(0, cur['messages'] - before['messages']),
                'bytes': max(0, cur['bytes'] - before['bytes']),
                'updated': fmt_time(now),
            }
            j['local'] = {'messages': cur['messages'], 'bytes': cur['bytes'],
                          'at': fmt_time(now)}
        self.store.update(set_running)

        stop_reason, stop_detail = None, ''
        rc = None
        while True:
            rc = handle.poll()
            if rc is not None:
                break
            self.sleep(self.poll_seconds)
            now = self.clock()
            cur = self.measure(folders)
            doc = self.store.update(lambda d: set_running(d, cur, now))
            status = (doc.get('job') or {}).get('status')
            got = max(0, cur['bytes'] - before['bytes'])
            if self.heartbeat:
                try:
                    self.heartbeat(token, handle.pid,
                                   f'Initial / rebase sync: '
                                   f'+{max(0, cur["messages"] - before["messages"]):,} '
                                   f'messages, {fmt_bytes(used + got)} today')
                except Exception:
                    pass
            # Budget and window come from the live state, so a change made
            # in the WebUI applies to the run in flight.
            live = doc.get('job') or {}
            budget = int(live.get('budget_bytes') or 0)
            if status == 'cancelled' or live.get('id') != self._job_id:
                stop_reason, stop_detail = 'cancelled', 'Cancelled; downloaded mail was kept.'
            elif status == 'paused':
                stop_reason, stop_detail = 'paused', 'Paused from the WebUI.'
            elif budget and used + got >= budget:
                stop_reason = 'budget'
                stop_detail = (f'{fmt_bytes(used + got)} downloaded today; the '
                               f'daily budget is {fmt_bytes(budget)}.')
            elif now >= deadline:
                stop_reason, stop_detail = 'window', deadline_reason
            if stop_reason:
                handle.request_stop()
                waited = 0.0
                rc = handle.poll()
                while rc is None and waited < self.stop_grace:
                    self.sleep(1)
                    waited += 1
                    rc = handle.poll()
                if rc is None:
                    handle.kill()
                    self.sleep(2)
                    rc = handle.poll()
                break

        end = self.clock()
        after = self.measure(folders)
        output = self.redact(handle.output() or '')
        tail = '\n'.join(output.splitlines()[-15:])[-2000:]
        complete = rc == 0 and self.all_complete(folders)

        if complete and stop_reason != 'cancelled':
            reason, detail = 'complete', ''
        elif stop_reason:
            reason, detail = stop_reason, stop_detail
        else:
            reason, detail = classify_exit(rc, output, complete)
        if reason == 'complete':
            detail = ('Every chosen folder is fully downloaded. The regular '
                      'sync includes ' + ('it' if len(folders) == 1 else 'them')
                      + ' again.')

        run = {
            'day': day, 'start': fmt_time(start), 'start_epoch': start,
            'end': fmt_time(end), 'end_epoch': end, 'trigger': trigger,
            'rc': rc, 'reason': reason, 'detail': detail,
            'messages_before': before['messages'],
            'messages_after': after['messages'],
            'messages': max(0, after['messages'] - before['messages']),
            'bytes_before': before['bytes'], 'bytes_after': after['bytes'],
            'bytes': max(0, after['bytes'] - before['bytes']),
            'output_tail': tail,
        }
        self._record(run, block=reason in RETRY_NEXT_DAY,
                     closed=reason == 'complete')
        self.log(f'end rc={rc}, {after["messages"]} messages local '
                 f'(+{run["messages"]}, {run["bytes"]} bytes): {reason}'
                 + (f' ({detail})' if detail else ''))
        for line in tail.splitlines():
            self.log(f'  mbsync: {line}')
        if reason == 'complete':
            self.log('COMPLETE: the folder exclusion is removed; job closed')
            if self.on_closed:
                self.on_closed()
        if self.after_run:
            try:
                self.after_run()
            except Exception:
                pass
        return {'ran': True, 'reason': reason, 'detail': detail, 'rc': rc,
                'messages': run['messages'], 'bytes': run['bytes']}


def _pid_alive(pid) -> bool:
    return compat.pid_alive(pid)


# ---------------------------------------------------------------------
# Progress, as the WebUI and the JSON status show it
# ---------------------------------------------------------------------

def fmt_bytes(n) -> str:
    n = float(n or 0)
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if n < 1000 or unit == 'TB':
            return f'{n:.0f} {unit}' if unit in ('B', 'KB') else f'{n:.1f} {unit}'
        n /= 1000
    return ''


def daily_totals(job: dict) -> List[dict]:
    """The run log folded per day, oldest first."""
    days: Dict[str, dict] = {}
    for r in job.get('runs') or []:
        d = days.setdefault(r.get('day'), {'day': r.get('day'), 'runs': 0,
                                           'messages': 0, 'bytes': 0})
        d['runs'] += 1
        d['messages'] += int(r.get('messages') or 0)
        d['bytes'] += int(r.get('bytes') or 0)
    return [days[k] for k in sorted(days, key=lambda x: x or '')]


def estimate_eta(remaining: Optional[int], days: List[dict]) -> Optional[int]:
    """Days still needed: what is left on the server, divided by the mean
    of the last three days that downloaded anything."""
    if remaining is None:
        return None
    if remaining <= 0:
        return 0
    recent = [d['messages'] for d in days if d['messages'] > 0][-3:]
    if not recent:
        return None
    return int(math.ceil(remaining / (sum(recent) / len(recent))))


def job_view(job: Optional[dict], now: Optional[float] = None,
             pid_alive=None) -> Optional[dict]:
    """Everything the page shows about a job, computed from its state."""
    if not job:
        return None
    now = time.time() if now is None else now
    pid_alive = pid_alive or _pid_alive
    day = day_of(now)
    running = job.get('running')
    is_running = bool(running and pid_alive(running.get('pid')))
    status = job.get('status')
    if status == 'active':
        state = 'running' if is_running else 'waiting'
    else:
        state = status

    days = daily_totals(job)
    local = job.get('local') or {}
    server = job.get('server') or {}
    folders = job.get('folders') or []
    known = [server.get(f, {}).get('messages') for f in folders]
    server_total = sum(known) if folders and all(k is not None for k in known) else None
    local_messages = local.get('messages')
    remaining = None
    if server_total is not None and local_messages is not None:
        remaining = max(0, server_total - local_messages)
    eta_days = estimate_eta(remaining, days) if status in OPEN_STATUSES else None

    budget = int(job.get('budget_bytes') or 0)
    today = bytes_on_day(job, day)
    ok, why = eligibility(job, now, 'cron')
    if state == 'running':
        next_run = 'Running now.'
    elif status == 'paused':
        next_run = 'Paused. Resume to continue.'
    elif status != 'active':
        next_run = ''
    elif ok:
        next_run = 'At the next hourly check.'
    elif why == 'budget':
        next_run = 'Today\'s budget is used up. Continues tomorrow.'
    elif why == 'retry-tomorrow':
        next_run = 'Continues tomorrow.'
    else:
        next_run = (f'When the time window opens ({job.get("window_start")} '
                    f'to {job.get("window_end")}).')
    all_day = job.get('window_start') == job.get('window_end')

    last = job.get('last_stop') or {}
    return {
        'id': job.get('id'), 'kind': job.get('kind'), 'status': status,
        'state': state, 'running': is_running, 'folders': folders,
        'created': job.get('created'), 'closed': job.get('closed'),
        'messages_local': local_messages, 'bytes_local': local.get('bytes'),
        'bytes_local_text': fmt_bytes(local.get('bytes')) if local.get('bytes') is not None else None,
        'measured_at': local.get('at'),
        'messages_server': server_total, 'messages_remaining': remaining,
        'percent': (min(100, int(100 * local_messages / server_total))
                    if server_total and local_messages is not None else None),
        'today_bytes': today, 'today_text': fmt_bytes(today),
        'budget_bytes': budget,
        'budget_text': fmt_bytes(budget) if budget else 'no budget',
        'budget_mb': budget // MB,
        'budget_percent': min(100, int(100 * today / budget)) if budget else None,
        'window_start': job.get('window_start'), 'window_end': job.get('window_end'),
        'window_text': 'all day' if all_day else
                       f'{job.get("window_start")} to {job.get("window_end")}',
        'days_run': len(days), 'runs': len(job.get('runs') or []),
        'eta_days': eta_days,
        'eta_text': ('' if eta_days is None else 'done' if eta_days == 0 else
                     f'about {eta_days} more day{"s" if eta_days != 1 else ""}'),
        'last_stop_reason': last.get('reason'),
        'last_stop_label': STOP_REASONS.get(last.get('reason'), last.get('reason') or ''),
        'last_stop_detail': last.get('detail') or '',
        'last_stop_at': last.get('at'),
        'next_run': next_run,
        'original_pattern': job.get('original_pattern'),
        'excluded_pattern': job.get('excluded_pattern'),
        'excluding': job_is_open(job),
    }


# ---------------------------------------------------------------------
# Reconcile: server count against local count, strictly read-only
# ---------------------------------------------------------------------

def _imap_quote(name: str) -> str:
    return '"' + name.replace('\\', '\\\\').replace('"', '\\"') + '"'


def _mutf7_decode(s: str) -> str:
    """IMAP modified UTF-7 mailbox name to text."""
    def dec(m):
        b = m.group(1)
        if not b:
            return '&'
        try:
            return base64.b64decode(b.replace(',', '/') + '=' * (-len(b) % 4)
                                    ).decode('utf-16-be')
        except Exception:
            return m.group(0)
    return re.sub(r'&([^-]*)-', dec, s)


_LIST_RE = re.compile(
    r'\((?P<attrs>[^)]*)\)\s+(?P<delim>"(?:\\.|[^"\\])*"|NIL)\s+(?P<name>.*)$',
    re.S)


def _text(x) -> str:
    return x.decode('utf-8', 'replace') if isinstance(x, (bytes, bytearray)) else str(x)


def _unquote(s: str) -> str:
    s = s.strip()
    if len(s) >= 2 and s[0] == '"' and s[-1] == '"':
        return re.sub(r'\\(.)', r'\1', s[1:-1])
    return s


class ImapReadOnly:
    """The only IMAP conversation the reconcile view has: LIST, STATUS,
    and, to count mail that arrived since the last sync, EXAMINE (the
    read-only form of SELECT) with UID SEARCH. It never selects a mailbox
    read-write and has no code path to STORE, EXPUNGE, APPEND, COPY or
    DELETE.

    `conn` is an authenticated imaplib connection (or a test double with
    list / status / select / uid / logout).
    """

    def __init__(self, conn):
        self.conn = conn

    def list_folders(self) -> List[dict]:
        typ, data = self.conn.list()
        if typ != 'OK':
            raise BulkError('The mail server refused to list the folders.')
        out = []
        for item in data or []:
            if item is None:
                continue
            if isinstance(item, tuple):          # name sent as a literal
                head, name = _text(item[0]), _text(item[1])
                m = _LIST_RE.match(head)
                if not m:
                    continue
            else:
                m = _LIST_RE.match(_text(item).strip())
                if not m:
                    continue
                name = _unquote(m.group('name'))
            delim = m.group('delim')
            delim = None if delim == 'NIL' else _unquote(delim)
            shown = _mutf7_decode(name)
            if delim and delim != '/':
                shown = shown.replace(delim, '/')
            out.append({'name': shown, 'server_name': name,
                        'noselect': '\\noselect' in m.group('attrs').lower()})
        return out

    def status(self, server_name: str) -> dict:
        typ, data = self.conn.status(_imap_quote(server_name),
                                     '(MESSAGES UIDNEXT UIDVALIDITY)')
        if typ != 'OK':
            raise BulkError('The mail server gave no status for the folder.')
        text = ' '.join(_text(p) for item in (data or [])
                        for p in (item if isinstance(item, tuple) else (item,))
                        if p is not None)
        out = {}
        for key in ('MESSAGES', 'UIDNEXT', 'UIDVALIDITY'):
            m = re.search(key + r'\s+(\d+)', text)
            out[key.lower()] = int(m.group(1)) if m else None
        if out['messages'] is None:
            raise BulkError('The mail server gave no message count for the folder.')
        return out

    def count_above(self, server_name: str, after_uid: int, uidnext: int) -> int:
        """How many messages have a UID above `after_uid`."""
        typ, _ = self.conn.select(_imap_quote(server_name), readonly=True)
        if typ != 'OK':
            raise BulkError('The mail server refused to open the folder read-only.')
        typ, data = self.conn.uid('SEARCH', None,
                                  f'UID {after_uid + 1}:{max(uidnext - 1, after_uid + 1)}')
        if typ != 'OK':
            raise BulkError('The mail server refused the search.')
        uids = _text((data or [b''])[0] or b'').split()
        return sum(1 for u in uids if u.isdigit() and int(u) > after_uid)

    def logout(self) -> None:
        try:
            self.conn.logout()
        except Exception:
            pass


VERDICTS = {
    'in-sync':          ('In sync', 'good'),
    'new-mail':         ('New since last sync', 'info'),
    'missing':          ('Missing locally', 'alert'),
    'local-extra':      ('Archive has more', 'info'),
    'bulk-in-progress': ('Download in progress', 'warn'),
    'not-archived':     ('Not in the archive', 'warn'),
    'unknown':          ('Could not check', 'alert'),
}


def _n(count: int, noun: str = 'message') -> str:
    return f'{count:,} {noun}{"" if count == 1 else "s"}'


def reconcile_row(folder: str, *, server_messages: int,
                  local_messages: Optional[int],
                  new_since: Optional[int] = None, new_exact: bool = False,
                  last_sync: Optional[str] = None, in_job: bool = False,
                  uidnext: Optional[int] = None,
                  uidvalidity_changed: bool = False) -> dict:
    """Compare one folder. `new_since` is the number of server messages
    with a UID above the highest UID the last finished sync pulled: they
    arrived after that sync, so they explain a positive difference without
    anything being missing. When it is only an upper bound (`new_exact`
    False) it is used no further than the difference it has to explain.
    None means there is no finished sync to compare against."""
    local = int(local_messages or 0)
    delta = server_messages - local
    if new_since is None:
        pending = 0
    elif new_exact:
        pending = max(0, min(new_since, server_messages))
    else:
        pending = max(0, min(new_since, delta))
    on_server_at_last_sync = server_messages - pending
    missing = max(0, on_server_at_last_sync - local)
    local_only = max(0, local - on_server_at_last_sync)
    when = f' ({last_sync})' if last_sync else ''
    about = '' if new_exact else 'up to '

    if in_job and delta > 0:
        verdict = 'bulk-in-progress'
        text = (f'The initial / rebase sync is still downloading this '
                f'folder: {_n(delta)} of {server_messages:,} to go.')
        missing, pending = 0, 0
    elif local_messages is None:
        verdict = 'not-archived' if server_messages else 'in-sync'
        missing = server_messages
        text = ('This folder is not in the archive: it was never synced, '
                'or the account\'s folder pattern leaves it out.'
                if server_messages else 'The folder is empty on the server.')
    elif uidvalidity_changed:
        verdict = 'missing' if delta > 0 else ('local-extra' if delta < 0 else 'in-sync')
        missing, pending, local_only = max(0, delta), 0, max(0, -delta)
        text = ('The server renumbered this folder since the last sync '
                '(UIDVALIDITY changed), so new mail cannot be told from '
                'missing mail. ' + (f'The server holds {_n(delta)} more than '
                                    f'the archive.' if delta > 0 else
                                    'The counts do not point to missing mail.'))
    elif new_since is None:
        missing, local_only = max(0, delta), max(0, -delta)
        if delta > 0:
            verdict = 'missing'
            text = (f'The server holds {_n(delta)} more than the archive, '
                    f'and this folder has no finished sync to compare '
                    f'against, so they count as missing.')
        elif delta < 0:
            verdict = 'local-extra'
            text = (f'The archive holds {_n(-delta)} more than the server. '
                    f'The archive never deletes mail, so mail removed on '
                    f'the server stays here.')
        else:
            verdict, text = 'in-sync', 'Server and archive hold the same number of messages.'
    elif missing:
        verdict = 'missing'
        text = (f'{_n(missing)} on the server '
                f'{"is" if missing == 1 else "are"} not in the archive')
        text += (f', besides {about}{pending:,} that arrived after the last '
                 f'sync{when}.' if pending else '.')
        text += ' Use "Fetch missing".'
    elif pending:
        verdict = 'new-mail'
        text = (f'{about.capitalize()}{_n(pending)} arrived on the server '
                f'after the last sync of this folder{when}. The next sync '
                f'pulls {"it" if pending == 1 else "them"}; nothing is missing.')
        if local_only:
            text += (f' The archive also keeps {_n(local_only)} that '
                     f'{"is" if local_only == 1 else "are"} no longer on the server.')
    elif local_only:
        verdict = 'local-extra'
        text = (f'The archive keeps {_n(local_only)} that '
                f'{"is" if local_only == 1 else "are"} no longer on the '
                f'server (deleted or moved there). The archive never '
                f'deletes mail, so this is expected.')
    else:
        verdict, text = 'in-sync', 'Server and archive hold the same number of messages.'

    return {
        'folder': folder, 'server_messages': server_messages,
        'uidnext': uidnext, 'local_messages': local, 'in_archive': local_messages is not None,
        'delta': delta, 'new_since_last_sync': pending,
        'new_since_exact': bool(new_exact) if new_since is not None else None,
        'missing': missing, 'local_only': local_only, 'last_sync': last_sync,
        'verdict': verdict, 'verdict_label': VERDICTS[verdict][0],
        'verdict_tone': VERDICTS[verdict][1], 'explanation': text,
        'can_fetch': verdict in ('missing', 'new-mail', 'not-archived'),
        'error': None,
    }


def reconcile(client, account_maildir: str,
              folders: Optional[Iterable[str]] = None,
              active_folders: Iterable[str] = (), exact: bool = True) -> dict:
    """Server against archive for `folders` (all the server's folders when
    None). `client` is an ImapReadOnly or a fake with the same methods."""
    listing = client.list_folders()
    by_name = {f['name']: f for f in listing if not f.get('noselect')}
    names = list(folders) if folders else sorted(by_name)
    active = set(active_folders)
    rows = []
    for name in names:
        try:
            path = folder_maildir(account_maildir, name)
        except BulkError:
            continue
        local = count_messages(path) if os.path.isdir(path) else None
        state = read_sync_state(path)
        last_sync = fmt_time(state['mtime']) if state and not state['journal'] else None
        info = by_name.get(name)
        try:
            s = client.status(info['server_name'] if info else name)
        except Exception as e:
            rows.append({'folder': name, 'server_messages': None,
                         'local_messages': local or 0, 'in_archive': local is not None,
                         'verdict': 'unknown', 'verdict_label': VERDICTS['unknown'][0],
                         'verdict_tone': VERDICTS['unknown'][1],
                         'explanation': f'The server count could not be read: {e}',
                         'can_fetch': False, 'error': str(e), 'delta': None,
                         'new_since_last_sync': None, 'missing': None,
                         'local_only': None, 'last_sync': last_sync, 'uidnext': None})
            continue
        new_since, new_exact, changed = None, False, False
        if state and not state['journal'] and state['max_pulled_uid'] is not None:
            if (state['uidvalidity'] is not None and s.get('uidvalidity') is not None
                    and state['uidvalidity'] != s['uidvalidity']):
                changed = True
            elif s.get('uidnext') is not None:
                bound = max(0, s['uidnext'] - 1 - state['max_pulled_uid'])
                new_since, new_exact = bound, bound == 0
                if bound and exact:
                    try:
                        new_since = client.count_above(
                            info['server_name'] if info else name,
                            state['max_pulled_uid'], s['uidnext'])
                        new_exact = True
                    except Exception:
                        pass
        rows.append(reconcile_row(
            name, server_messages=s['messages'], local_messages=local,
            new_since=new_since, new_exact=new_exact, last_sync=last_sync,
            in_job=name in active, uidnext=s.get('uidnext'),
            uidvalidity_changed=changed))
    return {'folders': rows,
            'server_folders': sorted(by_name)}


# ---------------------------------------------------------------------

if __name__ == '__main__':
    if len(sys.argv) > 3 and sys.argv[1] == 'supervise' and sys.argv[2] == '--':
        sys.exit(supervise(sys.argv[3:]))
    sys.stderr.write('usage: bulk_sync.py supervise -- <command> [args...]\n')
    sys.exit(2)
