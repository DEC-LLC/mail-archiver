#!/usr/bin/env python3
"""Shared primitives of the mail importer (1.3.0), plus the PST reader.

mail_import.py is the importer (type detection, the readers for every
format, containers, the ledger, retention, the per-user pass). This module
holds what it builds on, stdlib only:

* paths, config, the legacy PST watch folder (scan, file name -> account),
  stability checks, sha256 sidecars, the schedule;
* the count-based retention policy (`plan_rotation`; the R1-R6 guard that
  decides what may actually be deleted is mail_import.retention_decide);
* message dedupe keys (Message-ID, else a content hash) and their cache;
* Maildir delivery (tmp/ -> cur/, files 0600, dirs 0700, marked read);
* running `readpst` on a PST.

Layout under <data_dir>/<user>/_import/ :

    <account_slug>/inbox/             scanned; the folder names the account
    <account_slug>/inbox/.incoming/   producer / upload staging, ignored
    <account_slug>/done/              imported files (retention-managed)
    <account_slug>/failed/            refused files + <name>.reason.txt
    watch/  watch/.incoming/          legacy PST drop (file name -> account)
    archive/                          legacy archive, still read
    .work/  .state/                   private work dirs, dedupe key cache

Producer contract (per-account inbox/ and the legacy watch/): write into
.incoming/, rename `<name>.sha256` (sha256sum format, "<hex>  <name>") into
place first, then rename `<name>` in. Everything starting with "." and
every "*.tmp" is ignored.
"""

import datetime
import email
import email.parser
import email.utils
import errno
import fnmatch
import hashlib
import json
import os
import re
import socket
import stat
import subprocess
import time

import platform_compat as compat

IMPORT_DIRNAME = '_import'
SIDECAR_SUFFIX = '.sha256'
_DATE_RE = re.compile(r'-(\d{8})(?:\.[A-Za-z0-9]{1,8}){0,2}$')
_EMAIL_PREFIX_RE = re.compile(r'^(.+?)-\d{8}\.pst$', re.I)
UPLOAD_NAME_RE = re.compile(r'^[A-Za-z0-9@][A-Za-z0-9@._+-]{0,200}\.pst$')
_HEX64_RE = re.compile(r'^[0-9a-fA-F]{64}$')

MAX_PST_BYTES = int(os.environ.get('MAIL_ARCHIVER_PST_MAX_BYTES',
                                   str(50 * 1024 ** 3)))
MAX_MESSAGE_BYTES = int(os.environ.get('MAIL_ARCHIVER_PST_MAX_MESSAGE_BYTES',
                                       str(256 * 1024 ** 2)))
READPST_TIMEOUT = int(os.environ.get('MAIL_ARCHIVER_PST_TIMEOUT', str(6 * 3600)))
READPST = os.environ.get('MAIL_ARCHIVER_READPST', 'readpst')
STABLE_WAIT = int(os.environ.get('MAIL_ARCHIVER_PST_STABLE_WAIT', '30'))

SCHEDULE_KINDS = ('manual', 'hourly', 'daily', 'weekly')
WEEKDAYS = ('Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday',
            'Saturday', 'Sunday')
DEFAULT_CONFIG = {
    'enabled': False,
    'watch_dir': '',          # '' = <data_dir>/<user>/_import/watch
    'mappings': [],           # [{'pattern': 'x@a.edu-*.pst', 'account': 'x@b.edu'}]
    'default_account': '',
    'schedule': {'kind': 'daily', 'weekday': 0, 'hour': 6},
    # Retention of imported files in done/ (ADR retention-never-rotates-
    # to-empty, R1-R6). All counts of EXISTING copies, never ages.
    'keep_recent': 4,         # newest N copies of a dated series
    'keep_monthly': 3,        # + newest copy of each of N earlier months
    'min_keep': 3,            # hard floor per series (R3)
    'max_delete_per_run': 3,  # deletion budget per run (R5)
}


class ImportFailed(Exception):
    """The file could not be imported (converter error, timeout, limits)."""


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def safe_account_name(account_email):
    """Maildir directory name of an account; same rule as app.py."""
    return account_email.replace('@', '_at_').replace('.', '_')


def import_paths(data_dir, username, watch_dir=''):
    root = os.path.join(str(data_dir), username, IMPORT_DIRNAME)
    watch = resolve_watch_dir(data_dir, username, watch_dir)
    return {
        'root': root,
        'watch': watch,
        'incoming': os.path.join(watch, '.incoming'),
        'archive': os.path.join(root, 'archive'),     # legacy, read only
        'work': os.path.join(root, '.work'),
        'state': os.path.join(root, '.state'),
    }


def account_paths(data_dir, username, account_email):
    """The import folders of one mail account. The folder IS the target:
    whatever lands in inbox/ is imported into that account."""
    base = os.path.join(str(data_dir), username, IMPORT_DIRNAME,
                        safe_account_name(account_email))
    inbox = os.path.join(base, 'inbox')
    return {
        'base': base,
        'inbox': inbox,
        'incoming': os.path.join(inbox, '.incoming'),
        'done': os.path.join(base, 'done'),
        'failed': os.path.join(base, 'failed'),
    }


def resolve_watch_dir(data_dir, username, watch_dir=''):
    """The watch folder: the default, or a configured folder that must stay
    inside the user's own data dir (the scheduled run is root; a watch
    folder elsewhere would let a user import, then archive, any file)."""
    user_root = os.path.realpath(os.path.join(str(data_dir), username))
    default = os.path.join(user_root, IMPORT_DIRNAME, 'watch')
    if not watch_dir:
        return default
    cand = watch_dir if os.path.isabs(watch_dir) else os.path.join(user_root, watch_dir)
    cand = os.path.realpath(cand)
    # normcase: nothing on Linux; on Windows C:\Data and c:\data are one place
    if (os.path.normcase(cand) == os.path.normcase(user_root)
            or not os.path.normcase(cand).startswith(os.path.normcase(user_root + os.sep))):
        raise ValueError(f'watch folder must be inside {user_root}')
    return cand


# ---------------------------------------------------------------------------
# Config + mapping
# ---------------------------------------------------------------------------

def normalize_config(raw, account_emails=()):
    """Validated copy of a stored/submitted config. Unknown accounts are
    dropped from mappings (an account may have been removed)."""
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    raw = raw if isinstance(raw, dict) else {}
    known = {a.lower(): a for a in account_emails}
    cfg['enabled'] = bool(raw.get('enabled', False))
    cfg['watch_dir'] = str(raw.get('watch_dir') or '').strip()
    maps = []
    for m in raw.get('mappings') or []:
        if not isinstance(m, dict):
            continue
        pat = str(m.get('pattern') or '').strip()
        acct = known.get(str(m.get('account') or '').strip().lower())
        if pat and acct and '/' not in pat and len(pat) <= 300:
            maps.append({'pattern': pat, 'account': acct})
    cfg['mappings'] = maps
    cfg['default_account'] = known.get(
        str(raw.get('default_account') or '').strip().lower(), '')
    sch = raw.get('schedule') if isinstance(raw.get('schedule'), dict) else {}
    kind = sch.get('kind', cfg['schedule']['kind'])
    cfg['schedule'] = {
        'kind': kind if kind in SCHEDULE_KINDS else 'daily',
        'weekday': _clamp_int(sch.get('weekday', 0), 0, 6, 0),
        'hour': _clamp_int(sch.get('hour', 6), 0, 23, 6),
    }
    cfg['keep_recent'] = _clamp_int(
        raw.get('keep_recent', raw.get('keep_weekly', 4)), 1, 52, 4)
    cfg['keep_monthly'] = _clamp_int(raw.get('keep_monthly', 3), 0, 24, 3)
    cfg['min_keep'] = _clamp_int(raw.get('min_keep', 3), 1, 50, 3)
    cfg['max_delete_per_run'] = _clamp_int(raw.get('max_delete_per_run', 3), 1, 50, 3)
    return cfg


def _clamp_int(v, lo, hi, default):
    try:
        v = int(v)
    except (TypeError, ValueError):
        return default
    return min(hi, max(lo, v))


def map_file_to_account(filename, cfg, account_emails):
    """Which account a watch-folder file imports into.

    1. the first mapping whose glob (case-insensitive) matches the file name;
    2. `<account email>-YYYYMMDD.pst` naming an existing account exactly;
    3. the default account.
    Returns (account, how) or (None, reason).
    """
    known = {a.lower(): a for a in account_emails}
    low = filename.lower()
    for m in cfg.get('mappings') or []:
        acct = known.get(str(m.get('account', '')).lower())
        if acct and fnmatch.fnmatchcase(low, str(m.get('pattern', '')).lower()):
            return acct, f'pattern {m["pattern"]}'
    mm = _EMAIL_PREFIX_RE.match(filename)
    if mm and mm.group(1).lower() in known:
        return known[mm.group(1).lower()], 'file name'
    default = known.get(str(cfg.get('default_account') or '').lower())
    if default:
        return default, 'default account'
    return None, 'no mapping matches this file name and no default account is set'


def file_date(name, fallback_epoch=None):
    """The YYYYMMDD in `<name>-YYYYMMDD[.ext][.zst]`, else the fallback time."""
    base = name[:-4] if name.lower().endswith('.zst') else name
    m = _DATE_RE.search(base)
    if m:
        try:
            return datetime.datetime.strptime(m.group(1), '%Y%m%d').date()
        except ValueError:
            pass
    if fallback_epoch is not None:
        return datetime.date.fromtimestamp(fallback_epoch)
    return None


# ---------------------------------------------------------------------------
# Watch folder
# ---------------------------------------------------------------------------

def dir_entries(directory, io_dir=None):
    """[(name, lstat result or None, error text)] of a directory, or None
    when there is nothing to list. Without `io_dir` the directory itself is
    opened O_DIRECTORY|O_NOFOLLOW and its entries are examined relative to
    that descriptor: a symbolic link (or a file) in the directory's place
    lists nothing. `io_dir` is a path that already pins the checked
    directory (mail_import.PinnedDir.via)."""
    fd = None
    try:
        if io_dir is None and not compat.supports_dir_fd():
            # Windows: a directory cannot be opened; check the name, then
            # list it by path.
            if compat.is_link(directory) or not stat.S_ISDIR(os.lstat(directory).st_mode):
                return None
            io_dir = directory
        if io_dir is None:
            fd = os.open(directory, os.O_RDONLY | compat.O_DIRECTORY | compat.O_NOFOLLOW
                         | compat.O_CLOEXEC)
            with os.scandir(fd) as it:
                names = sorted(e.name for e in it)
        else:
            with os.scandir(io_dir) as it:
                names = sorted(e.name for e in it)
        out = []
        for n in names:
            try:
                st = (os.lstat(os.path.join(io_dir, n)) if fd is None
                      else os.stat(n, dir_fd=fd, follow_symlinks=False))
                out.append((n, st, ''))
            except OSError as err:
                out.append((n, None, f'cannot stat: {err.strerror}'))
        return out
    except OSError:
        return None                    # missing, a link, not a directory
    finally:
        if fd is not None:
            os.close(fd)


def scan_watch(watch_dir, io_dir=None):
    """Candidate .pst files in the watch folder.

    Returns {'candidates': [...], 'ignored': [(name, reason)]}. Dotfiles and
    dot-dirs (.incoming, .fetch-state) and *.tmp are skipped silently;
    sidecars attach to their .pst; a sidecar without its .pst is normal (the
    producer renames the sidecar first) and is skipped silently too.
    Symlinks are never followed, and a watch folder that is itself a
    symbolic link is not read (see dir_entries). A candidate's 'path' is
    its real path; 'io' and 'sidecar_io' are the paths a privileged caller
    must use (through `io_dir`, the pinned directory).
    """
    out = {'candidates': [], 'ignored': []}
    entries = dir_entries(watch_dir, io_dir)
    if entries is None:
        return out
    io = io_dir or watch_dir
    names = {n for n, _st, _err in entries}
    for n, st, err in entries:
        if n.startswith('.') or n.lower().endswith('.tmp'):
            continue
        if n.lower().endswith('.pst' + SIDECAR_SUFFIX):
            continue
        if st is not None and stat.S_ISLNK(st.st_mode):
            out['ignored'].append((n, 'symbolic link (not followed)'))
            continue
        if not n.lower().endswith('.pst'):
            out['ignored'].append((n, 'not a .pst file'))
            continue
        if st is None:
            out['ignored'].append((n, err))
            continue
        if not stat.S_ISREG(st.st_mode):
            out['ignored'].append((n, 'not a regular file'))
            continue
        sidecar = n + SIDECAR_SUFFIX
        has_side = sidecar in names
        out['candidates'].append({
            'name': n,
            'path': os.path.join(watch_dir, n),
            'io': os.path.join(io, n),
            'size': st.st_size,
            'mtime': st.st_mtime,
            'sig': _sig(st),
            'sidecar': os.path.join(watch_dir, sidecar) if has_side else None,
            'sidecar_io': os.path.join(io, sidecar) if has_side else None,
        })
    return out


def _sig(st):
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)


def file_signature(path):
    try:
        st = os.lstat(path)
    except OSError:
        return None
    return _sig(st) if stat.S_ISREG(st.st_mode) else None


def split_stable(candidates, wait=STABLE_WAIT, sleep=time.sleep):
    """Second check: a candidate is stable when its (dev, inode, size,
    mtime) is unchanged after `wait` seconds. Returns (stable, unstable)."""
    if not candidates:
        return [], []
    if wait > 0:
        sleep(wait)
    stable, unstable = [], []
    for c in candidates:
        (stable if file_signature(c['path']) == c['sig'] else unstable).append(c)
    return stable, unstable


# Flags for every file the importer creates: never through a link, and no
# CR LF translation by the C runtime on Windows (0 where a flag is unknown).
O_SAFE = compat.O_NOFOLLOW | compat.O_BINARY


def _open_nofollow(path):
    # O_NONBLOCK: a FIFO swapped in for a file must not hang the reader
    if not compat.O_NOFOLLOW or compat.IS_WINDOWS:
        # No O_NOFOLLOW on Windows: refuse a link by looking first.
        if compat.is_link(path):
            raise OSError(errno.ELOOP, os.strerror(errno.ELOOP), path)
    return os.open(path, os.O_RDONLY | compat.O_NOFOLLOW | compat.O_NONBLOCK
                   | compat.O_BINARY | compat.O_CLOEXEC)


def sha256_file(path, bufsize=4 * 1024 * 1024):
    h = hashlib.sha256()
    fd = _open_nofollow(path)
    with os.fdopen(fd, 'rb') as f:
        while True:
            b = f.read(bufsize)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def parse_sidecar(text, pst_name):
    """Expected hex digest from a sha256sum line. Raises ValueError when the
    sidecar is malformed or names a different file."""
    lines = [l for l in text.splitlines() if l.strip()]
    if len(lines) != 1:
        raise ValueError('sidecar must hold exactly one sha256sum line')
    parts = lines[0].strip().split(None, 1)
    digest = parts[0]
    if digest.startswith('\\'):
        digest = digest[1:]
    if not _HEX64_RE.match(digest):
        raise ValueError('sidecar does not start with a sha256 hex digest')
    if len(parts) == 2:
        named = parts[1].strip()
        if named.startswith('*'):
            named = named[1:]
        if os.path.basename(named) != pst_name:
            raise ValueError(f'sidecar names {named!r}, not {pst_name!r}')
    return digest.lower()


def verify_sidecar(pst_path, sidecar_path):
    """(ok, detail). Never raises for bad content."""
    try:
        fd = _open_nofollow(sidecar_path)
        with os.fdopen(fd, 'rb') as f:
            text = f.read(4096).decode('utf-8', 'replace')
        want = parse_sidecar(text, os.path.basename(pst_path))
    except (OSError, ValueError) as e:
        return False, f'sha256 sidecar unusable: {e}'
    got = sha256_file(pst_path)
    if got != want:
        return False, f'sha256 mismatch: sidecar {want[:16]}..., file {got[:16]}...'
    return True, f'sha256 verified ({got[:16]}...)'


# ---------------------------------------------------------------------------
# Schedule
# ---------------------------------------------------------------------------

def last_slot_epoch(schedule, now):
    """Epoch of the most recent scheduled slot at or before `now` (local
    time), or None for manual."""
    kind = schedule.get('kind', 'daily')
    lt = time.localtime(now)
    if kind == 'manual':
        return None
    if kind == 'hourly':
        return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, lt.tm_hour,
                            0, 0, 0, 0, -1))
    hour = int(schedule.get('hour', 6))
    slot = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, hour, 0, 0, 0, 0, -1))
    if kind == 'daily':
        if slot > now:
            slot = _shift_days(slot, -1, hour)
        return slot
    # weekly
    back = (lt.tm_wday - int(schedule.get('weekday', 0))) % 7
    slot = _shift_days(slot, -back, hour)
    if slot > now:
        slot = _shift_days(slot, -7, hour)
    return slot


def _shift_days(epoch, days, hour):
    d = datetime.date.fromtimestamp(epoch) + datetime.timedelta(days=days)
    return time.mktime((d.year, d.month, d.day, hour, 0, 0, 0, 0, -1))


def is_import_due(schedule, last_run, now):
    slot = last_slot_epoch(schedule, now)
    if slot is None:
        return False
    return last_run is None or last_run < slot


# ---------------------------------------------------------------------------
# Archive retention
# ---------------------------------------------------------------------------

def plan_rotation(entries, today=None, keep_recent=4, keep_monthly=3):
    """The retention POLICY for one series of copies: split into
    (keep, delete candidates). Count-based only (R1):

    * the newest `keep_recent` existing copies, plus
    * the newest copy of each of the `keep_monthly` most recent calendar
      months, before the month of the newest copy, that HAVE a copy.

    The plan depends on the set of copies alone, never on the clock
    (`today` is accepted for callers that pass it and is not used): the
    same copies give the same plan next week and next year, a month
    without copies does not use up a monthly slot, and so the passing of
    time can never make a copy surplus - only a newer copy can. While the
    source delivers, the newest copy is in the current month and this is
    "the newest copy of each of the 3 prior months". What may really be
    deleted is decided by mail_import.retention_decide (R2-R5) - this
    function deletes nothing.
    """
    ordered = sorted(entries, key=lambda r: (r['date'], r['name']), reverse=True)
    keep, reasons = [], {}
    for r in ordered[:max(1, keep_recent)]:
        keep.append(r)
        reasons[r['name']] = 'recent'
    this_month = (ordered[0]['date'].year * 12 + ordered[0]['date'].month - 1
                  if ordered else 0)
    seen_months = set()
    for r in ordered:
        m = r['date'].year * 12 + r['date'].month - 1
        if m >= this_month or m in seen_months or len(seen_months) >= keep_monthly:
            continue
        seen_months.add(m)
        if r['name'] not in reasons:
            keep.append(r)
            reasons[r['name']] = 'monthly'
        else:
            reasons[r['name']] += '+monthly'
    delete = [r for r in ordered if r['name'] not in reasons]
    for r in keep:
        r['keep_reason'] = reasons[r['name']]
    return keep, delete


# ---------------------------------------------------------------------------
# Dedupe keys
# ---------------------------------------------------------------------------

_HEAD_LIMIT = 256 * 1024


def normalize_message_id(value):
    if not value:
        return ''
    v = ' '.join(str(value).split())
    m = re.search(r'<([^<>]+)>', v)
    v = (m.group(1) if m else v).strip()
    return v if v and ' ' not in v else ''


def _read_head(fd_file):
    data = fd_file.read(_HEAD_LIMIT)
    for sep in (b'\r\n\r\n', b'\n\n'):
        i = data.find(sep)
        if i >= 0:
            return data[:i + len(sep)]
    return data


def content_key(msg):
    """Dedupe key for a message without a Message-ID: normalised address,
    subject and date headers plus the DECODED payload of every leaf part.
    MIME boundaries and transfer encodings are left out, because readpst
    makes up a new random boundary on every run."""
    h = hashlib.sha256()
    for name in ('from', 'to', 'cc', 'subject', 'date'):
        vals = msg.get_all(name) or []
        h.update(name.encode() + b':' + ' '.join(
            ' '.join(str(v).split()) for v in vals).encode('utf-8', 'replace') + b'\n')
    for part in msg.walk():
        if part.is_multipart():
            continue
        try:
            payload = part.get_payload(decode=True) or b''
        except Exception:
            payload = b''
        ct = part.get_content_type()
        if ct.startswith('text/'):
            payload = b'\n'.join(l.rstrip() for l in
                                 payload.replace(b'\r\n', b'\n').split(b'\n')).strip()
        fn = part.get_filename() or ''
        h.update(f'{ct}|{fn}|'.encode('utf-8', 'replace') + hashlib.sha256(payload).digest())
    return 'h:' + h.hexdigest()


def message_key_from_bytes(raw):
    head = raw[:_HEAD_LIMIT]
    for sep in (b'\r\n\r\n', b'\n\n'):
        i = head.find(sep)
        if i >= 0:
            head = head[:i + len(sep)]
            break
    hdrs = email.parser.BytesHeaderParser().parsebytes(head)
    mid = normalize_message_id(hdrs.get('Message-ID'))
    if mid:
        return 'm:' + mid
    return content_key(email.message_from_bytes(raw))


def message_key_from_file(path, max_bytes=MAX_MESSAGE_BYTES):
    fd = _open_nofollow(path)
    with os.fdopen(fd, 'rb') as f:
        head = _read_head(f)
        hdrs = email.parser.BytesHeaderParser().parsebytes(head)
        mid = normalize_message_id(hdrs.get('Message-ID'))
        if mid:
            return 'm:' + mid
        f.seek(0)
        raw = f.read(max_bytes + 1)
    if len(raw) > max_bytes:
        return None
    return content_key(email.message_from_bytes(raw))


def _maildir_uniq(name):
    return compat.maildir_uniq(name)


def load_existing_keys(maildir, cache_path=None):
    """Dedupe keys of every message already in the account (all folders).

    Message keys are cached per Maildir unique name + size in `cache_path`,
    so a weekly re-import of a big mailbox does not re-read every file.
    """
    cache = {}
    if cache_path:
        try:
            with open(cache_path, encoding='utf-8') as f:
                cache = json.load(f)
            if not isinstance(cache, dict):
                cache = {}
        except (OSError, ValueError):
            cache = {}
    keys, fresh = set(), {}
    for root, dirs, files in os.walk(maildir, followlinks=False):
        if os.path.basename(root) not in ('cur', 'new'):
            continue
        for fn in files:
            p = os.path.join(root, fn)
            try:
                st = os.lstat(p)
            except OSError:
                continue
            if not stat.S_ISREG(st.st_mode):
                continue
            uniq = _maildir_uniq(fn)
            hit = cache.get(uniq)
            if isinstance(hit, list) and len(hit) == 2 and hit[0] == st.st_size:
                k = hit[1]
            else:
                try:
                    k = message_key_from_file(p)
                except (OSError, ValueError):
                    k = None
            if k:
                keys.add(k)
                fresh[uniq] = [st.st_size, k]
    if cache_path:
        _write_json_atomic(cache_path, fresh, mode=0o600)
    return keys


def _write_json_atomic(path, data, mode=0o600):
    tmp = f'{path}.{os.getpid()}.tmp'
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | O_SAFE, mode)
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        json.dump(data, f)
    compat.replace_file(tmp, path)


# ---------------------------------------------------------------------------
# Folder mapping + delivery
# ---------------------------------------------------------------------------

_RESERVED = {'cur', 'new', 'tmp'}


def sanitize_folder_component(name):
    """A PST folder name as one safe Maildir path component."""
    n = ''.join('_' if (ord(c) < 32 or c in '/\\\x7f') else c for c in name).strip()
    if not n or n in ('.', '..'):
        n = '_'
    if n.startswith('.'):
        n = '_' + n[1:]
    if n.lower() in _RESERVED:
        n = '_' + n
    while len(n.encode('utf-8')) > 200:
        n = n[:-1]
    # Windows: characters and names NTFS refuses (unchanged on Linux)
    return compat.safe_filename_component(n)


def map_folder(parts, prefix=('PST',)):
    """Source folder path (list of names) -> Maildir folder components
    under the per-source prefix folder: a PST `Inbox` is
    `PST/Inbox`, never the synced INBOX; an mbox lands under
    `Imported/mbox/`. Mail in the source root goes into the prefix folder."""
    return list(prefix) + [sanitize_folder_component(p) for p in parts]


def _mkdirs_private(path):
    """makedirs with EVERY new level 0700 (os.makedirs gives only the leaf
    the mode; a prefix folder such as PST/ must be private too)."""
    missing = []
    while not os.path.lexists(path):
        missing.append(path)
        parent = os.path.dirname(path)
        if parent == path:
            break
        path = parent
    for p in reversed(missing):
        try:
            os.mkdir(p, 0o700)
        except FileExistsError:
            pass


def _ensure_maildir(folder_path):
    for sub in ('', 'cur', 'new', 'tmp'):
        p = os.path.join(folder_path, sub) if sub else folder_path
        _mkdirs_private(p)
        st = os.lstat(p)
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
            raise OSError(f'{p} is not a directory')


_HOST = compat.safe_filename_component(
    socket.gethostname().replace('/', '\\057').replace(':', '\\072'))
_SEQ = [0]


_FLAG_CHARS = 'DFPRST'


def maildir_info(flags='S'):
    """The `:2,<flags>` suffix: known flags only, sorted, as Maildir wants.
    (`!2,<flags>` on Windows, where a file name cannot hold a colon:
    platform_compat.maildir_info_separator.)"""
    return compat.maildir_info(''.join(sorted({c for c in flags if c in _FLAG_CHARS})))


def deliver(folder_path, raw, mtime=None, flags='S'):
    """Write one message Maildir-style: tmp/ then rename into cur/.
    Imported mail is marked read (the S flag) unless the caller says
    otherwise."""
    _ensure_maildir(folder_path)
    _SEQ[0] += 1
    now = time.time()
    uniq = f'{int(now)}.M{int((now % 1) * 1e6)}P{os.getpid()}Q{_SEQ[0]}.{_HOST}'
    tmp = os.path.join(folder_path, 'tmp', uniq)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | O_SAFE, 0o600)
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(raw)
        final = os.path.join(folder_path, 'cur', uniq + maildir_info(flags))
        os.rename(tmp, final)           # the file is closed; `final` is new
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    if mtime:
        try:
            os.utime(final, (mtime, mtime))
        except OSError:
            pass
    return final


def _message_epoch(raw):
    try:
        hdrs = email.parser.BytesHeaderParser().parsebytes(raw[:_HEAD_LIMIT])
        d = email.utils.parsedate_tz(hdrs.get('Date', ''))
        return email.utils.mktime_tz(d) if d else None
    except Exception:
        return None


def _iter_output(out_dir):
    """(folder_parts, path) for every regular file readpst wrote. The PST
    root folder (a single top-level directory) is stripped. Links and
    special files are skipped and counted."""
    skipped = 0
    base = out_dir
    top = list(os.scandir(out_dir))
    if (len(top) == 1 and top[0].is_dir(follow_symlinks=False)):
        base = top[0].path
    results = []
    for root, dirs, files in os.walk(base, followlinks=False):
        dirs.sort()
        rel = os.path.relpath(root, base)
        parts = [] if rel == '.' else rel.split(os.sep)
        for fn in sorted(files, key=lambda s: (len(s), s)):
            p = os.path.join(root, fn)
            st = os.lstat(p)
            if not stat.S_ISREG(st.st_mode):
                skipped += 1
                continue
            results.append((parts, p, st.st_size))
    return results, skipped


def limit_cmd(cmd, source_path, max_bytes=None):
    """Wrap a converter command in prlimit when it is installed: no core
    dumps of a crashing parser, and no single output file larger than the
    source could honestly produce (base64 grows an attachment by a third).
    Not the per-message limit: hitting RLIMIT_FSIZE kills the converter,
    which would fail a whole PST over one big message; those are skipped
    after conversion instead."""
    prlimit = compat.have_tool('prlimit')      # util-linux; not on Windows
    if not prlimit:
        return cmd
    try:
        fsize = os.path.getsize(source_path) * 2 + 64 * 1024 ** 2
    except OSError:
        fsize = (max_bytes or MAX_PST_BYTES) * 2
    return [prlimit, f'--fsize={fsize}', '--core=0', '--'] + cmd


CHILD_ENV = {'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LANG': 'C.UTF-8'}

# Why a PST / OST is refused when the converter is missing. On Linux it is a
# package; on Windows it is an optional program the user puts on PATH.
PST_MISSING = 'readpst is not installed (Debian package pst-utils)'
PST_MISSING_WINDOWS = ('Outlook PST/OST files need the readpst program (libpst), '
                       'which was not found on PATH; install it and import the '
                       'file again')


def pst_missing_message():
    return PST_MISSING_WINDOWS if compat.IS_WINDOWS else PST_MISSING


def child_env(**extra):
    """Environment of a converter child: CHILD_ENV (+ what a Windows
    program needs to start at all, see platform_compat.child_env)."""
    return compat.child_env(CHILD_ENV, **extra)


def run_readpst(pst_path, out_dir, work_dir, readpst=None, timeout=READPST_TIMEOUT):
    """`readpst -M` of one PST into out_dir (one file per message, the PST
    folder tree as directories). Raises ImportFailed."""
    readpst = readpst or READPST
    if compat.IS_WINDOWS:
        # Refuse before anything is created; give CreateProcess the full
        # path (it does not search the PATH of the child's environment).
        readpst = compat.have_tool(readpst)
        if not readpst:
            raise ImportFailed(pst_missing_message())
    os.mkdir(out_dir, 0o700)
    cmd = limit_cmd([readpst, '-M', '-b', '-q', '-t', 'e', '-o', out_dir,
                     os.path.abspath(pst_path)], pst_path)
    try:
        res = subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, cwd=work_dir,
                             env=child_env(HOME=work_dir), timeout=timeout)
    except subprocess.TimeoutExpired:
        raise ImportFailed(f'readpst timed out after {timeout} s')
    except FileNotFoundError:
        raise ImportFailed(pst_missing_message())
    if res.returncode != 0:
        tail = (res.stderr or res.stdout or b'').decode('utf-8', 'replace')[-500:]
        raise ImportFailed(f'readpst exited {res.returncode}: {tail.strip()}')


def import_pst_to_maildir(pst_path, maildir, work_dir, *, readpst=None,
                          timeout=READPST_TIMEOUT,
                          max_message_bytes=MAX_MESSAGE_BYTES, cache_path=None):
    """Convert one PST and deliver the new messages under `PST/`. Runs as
    the mailbox owner. Returns a stats dict; raises ImportFailed if readpst
    fails. (The general entry point is mail_import.convert_unit.)"""
    import mail_import
    return mail_import.convert_unit(
        pst_path, 'pst', maildir, work_dir, readpst=readpst, timeout=timeout,
        max_message_bytes=max_message_bytes, cache_path=cache_path)


def now_str(epoch):
    return time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(epoch))
