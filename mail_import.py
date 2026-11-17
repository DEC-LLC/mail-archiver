#!/usr/bin/env python3
"""General mail importer for Mail Archiver (1.3.0).

Imports mail files into an existing account's Maildir: Outlook PST/OST
(readpst), mbox, Maildir / Maildir++, single .eml files and directories of
them, Apple .emlx, Outlook .msg (msgconvert) and zip / tar containers of
any of those. Stdlib only; the external tools are Debian packages and each
is optional at run time (a file that needs a missing tool is refused with
a clear message).

Three parts:

* The converter (`convert_unit`, CLI `convert`). It runs AS THE MAILBOX
  OWNER (app.py spawns it with Popen(user=, group=), the same way mbsync is
  run), so everything it writes is owned by that user exactly like synced
  mail (dirs 0700, files 0600) and no untrusted file is ever parsed with
  the service's CAP_DAC_OVERRIDE. It works in a private directory under the
  user's data dir (never /tmp), opens nothing through a link, drops
  messages already present anywhere in the account (Message-ID, or a
  content hash when there is none) and delivers the rest tmp/ -> cur/,
  marked read, under a per-source prefix folder (`PST/`, `Imported/mbox/`).

* The ledger (`Ledger`): one row per imported file in the per-user index
  database, and one row per retention deletion or refused deletion.

* The per-user pass (`UserImporter`), run by app.py as root (cron) or as
  the service: scan each account's inbox/, detect the type from the
  content, hash, skip what the ledger already has, convert, index, archive
  to done/, and apply retention through `retention_decide` (R1-R6 of the
  ADR "retention can never rotate a backup set to empty").
"""

import bz2
import datetime
import errno
import gzip
import hashlib
import json
import lzma
import os
import re
import shutil
import sqlite3
import stat
import struct
import subprocess
import sys
import tarfile
import time
import zipfile
import zlib

# The converter child is started with `python3 -I`, which leaves the
# script's directory out of sys.path; this file's own directory (the
# root-owned install dir) is the only place pst_import may come from.
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import platform_compat as compat  # noqa: E402
import pst_import as pst  # noqa: E402
from pst_import import ImportFailed  # noqa: E402

# ---------------------------------------------------------------------------
# Limits and tools
# ---------------------------------------------------------------------------

MAX_IMPORT_BYTES = int(os.environ.get('MAIL_ARCHIVER_IMPORT_MAX_BYTES',
                                      str(pst.MAX_PST_BYTES)))
MAX_UNPACK_BYTES = int(os.environ.get('MAIL_ARCHIVER_IMPORT_MAX_UNPACK_BYTES',
                                      str(100 * 1024 ** 3)))
MAX_UNPACK_RATIO = int(os.environ.get('MAIL_ARCHIVER_IMPORT_MAX_RATIO', '200'))
# The ratio is only judged once this much has come out: a few kilobytes of
# text legitimately compress very well.
RATIO_FLOOR_BYTES = int(os.environ.get('MAIL_ARCHIVER_IMPORT_RATIO_FLOOR',
                                       str(64 * 1024 ** 2)))
MAX_MEMBERS = int(os.environ.get('MAIL_ARCHIVER_IMPORT_MAX_MEMBERS', '2000000'))
MAX_CONTAINER_DEPTH = 2          # a container inside a container, no deeper
MSGCONVERT = os.environ.get('MAIL_ARCHIVER_MSGCONVERT', 'msgconvert')
ZSTD = os.environ.get('MAIL_ARCHIVER_ZSTD', 'zstd')
MSGCONVERT_TIMEOUT = 300
FREE_RESERVE = 1024 ** 3
SIDECAR_SUFFIX = pst.SIDECAR_SUFFIX
REASON_SUFFIX = '.reason.txt'
# Temporary files of receive_upload, in <account>/inbox/.incoming. Only
# names of this shape are ever cleaned up, and only once nothing has
# written to them for this long (an upload in progress keeps writing, and
# uploads do not hold the import lock).
UPLOAD_TMP_RE = re.compile(r'\.upload-\d+\.tmp(?:\.sha256\.tmp)?$')
UPLOAD_STALE_SECONDS = 6 * 3600
SPACE_CHECK_BYTES = 64 * 1024 ** 2
UPLOAD_NAME_RE = re.compile(r'^[A-Za-z0-9@][A-Za-z0-9@._+ ()\[\]-]{0,200}$')

# Maildir folder prefix per source type (`PST/` exactly).
PREFIX = {
    'pst': ('PST',),
    'mbox': ('Imported', 'mbox'),
    'maildir': ('Imported', 'Maildir'),
    'eml': ('Imported', 'eml'),
    'emlx': ('Imported', 'emlx'),
    'msg': ('Imported', 'msg'),
}
CONTAINER_TYPES = ('zip', 'tar', 'gzip', 'bzip2', 'xz', 'zstd')
TYPE_LABELS = {
    'pst': 'Outlook PST/OST', 'mbox': 'mbox', 'maildir': 'Maildir',
    'eml': 'email (.eml)', 'emlx': 'Apple Mail (.emlx)',
    'msg': 'Outlook message (.msg)', 'zip': 'zip archive', 'tar': 'tar archive',
    'gzip': 'gzip (tar.gz or one file)', 'bzip2': 'bzip2 (tar.bz2 or one file)',
    'xz': 'xz (tar.xz or one file)', 'zstd': 'zstd (tar.zst or one file)',
    'directory': 'directory of mail files',
}
MSG_MISSING = ('Outlook .msg files need msgconvert (Debian package '
               'libemail-outlook-message-perl), which is not installed')
MSG_MISSING_WINDOWS = ('Outlook .msg files need the msgconvert program (Perl module '
                       'Email::Outlook::Message), which was not found on PATH; '
                       'install it and import the file again')
PST_MISSING, PST_MISSING_WINDOWS = pst.PST_MISSING, pst.PST_MISSING_WINDOWS


def msg_missing_message():
    return MSG_MISSING_WINDOWS if compat.IS_WINDOWS else MSG_MISSING


def tool_notes():
    """For the import page on Windows: {'readpst': why PST/OST files are
    refused, 'msgconvert': why .msg files are refused}, each '' when the
    program is on PATH. On Linux both are always '': the page has its own
    text there, naming the Debian packages."""
    if not compat.IS_WINDOWS:
        return {'readpst': '', 'msgconvert': ''}
    return {'readpst': '' if compat.have_tool(pst.READPST) else PST_MISSING_WINDOWS,
            'msgconvert': '' if compat.have_tool(MSGCONVERT) else MSG_MISSING_WINDOWS}


def prefix_str(kind):
    return '/'.join(PREFIX[kind]) + '/'


# ---------------------------------------------------------------------------
# Type detection: by content; the extension only breaks ties
# ---------------------------------------------------------------------------

HEAD_BYTES = 64 * 1024
_CFB_MAGIC = bytes.fromhex('D0CF11E0A1B11AE1')
_MSG_STREAMS = tuple(s.encode('utf-16-le') for s in (
    '__substg1.0_', '__properties_version1.0', '__nameid_version1.0',
    '__recip_version1.0', '__attach_version1.0'))
_MSG_SCAN_BYTES = 4 * 1024 ** 2
_HDR_NAME = re.compile(rb'^([\x21-\x39\x3b-\x7e]{1,76}):(?:[ \t]|$)')
_KNOWN_HEADERS = frozenset(h.encode() for h in (
    'from', 'to', 'cc', 'bcc', 'subject', 'date', 'message-id', 'received',
    'return-path', 'mime-version', 'content-type', 'delivered-to', 'sender',
    'reply-to', 'x-mozilla-status', 'x-gmail-labels', 'x-uidl', 'x-apple-uuid',
    'content-transfer-encoding', 'in-reply-to', 'references', 'x-mailer',
    'x-original-to', 'dkim-signature', 'authentication-results'))
_EMLX_COUNT = re.compile(rb'^[ \t]*(\d{1,12})[ \t]*\r?\n')
_EXT_TYPES = {'.pst': 'pst', '.ost': 'pst', '.mbox': 'mbox', '.mbx': 'mbox',
              '.eml': 'eml', '.emlx': 'emlx', '.msg': 'msg'}


def looks_like_headers(data, strict=True):
    """True when `data` starts with an RFC 822 header block: header lines
    and folded continuations up to a blank line (or the end of what was
    read). `strict` also wants at least one header name mail really uses,
    so a text file that merely starts "Note: ..." is not mail."""
    if data.startswith(b'\xef\xbb\xbf'):
        data = data[3:]
    lines = data.split(b'\n')
    if not data.endswith(b'\n'):
        lines = lines[:-1] or lines          # the last line may be cut short
    count, known = 0, False
    for line in lines[:2000]:
        line = line.rstrip(b'\r')
        if not line:
            break
        if line[:1] in (b' ', b'\t'):
            if not count:
                return False
            continue
        m = _HDR_NAME.match(line)
        if not m:
            return False
        count += 1
        known = known or m.group(1).lower() in _KNOWN_HEADERS
    return count >= 1 and (known or not strict)


def _is_msg(f, head):
    """An OLE compound file is an Outlook message when its directory names
    MSG streams. Look where the header says the directory starts, then in
    the first few MiB; never the whole file."""
    if any(s in head for s in _MSG_STREAMS):
        return True
    try:
        shift = struct.unpack_from('<H', head, 0x1E)[0]
        first_dir = struct.unpack_from('<I', head, 0x30)[0]
        if shift in (9, 12) and first_dir < 0xFFFFFFF0:
            f.seek((first_dir + 1) << shift)
            if any(s in f.read(HEAD_BYTES) for s in _MSG_STREAMS):
                return True
    except (struct.error, OSError, OverflowError):
        pass
    try:
        f.seek(0)
        tail, done = b'', 0
        while done < _MSG_SCAN_BYTES:
            block = f.read(1024 * 1024)
            if not block:
                break
            if any(s in tail + block for s in _MSG_STREAMS):
                return True
            tail, done = block[-64:], done + len(block)
    except OSError:
        pass
    return False


def detect_file(path):
    """(type, how) of a regular file, or (None, reason). Opens without
    following a link and reads a bounded amount."""
    name = os.path.basename(path)
    ext = os.path.splitext(name)[1].lower()
    try:
        fd = pst._open_nofollow(path)
    except OSError as e:
        return None, f'cannot open: {e.strerror}'
    with os.fdopen(fd, 'rb') as f:
        head = f.read(HEAD_BYTES)
        if head[:4] == b'!BDN':
            return 'pst', 'PST magic !BDN'
        if head[:8] == _CFB_MAGIC:
            if _is_msg(f, head):
                return 'msg', 'OLE compound file with MSG streams'
            if ext == '.msg':
                return 'msg', 'OLE compound file named .msg'
            return None, ('an OLE compound file (old Office format) without '
                          'Outlook message streams')
    if head[:4] in (b'PK\x03\x04', b'PK\x05\x06'):
        return 'zip', 'zip magic'
    if head[:2] == b'\x1f\x8b':
        return 'gzip', 'gzip magic'
    if head[:3] == b'BZh' and head[3:4].isdigit() and head[4:10] in (
            b'1AY&SY', b'\x17rE8P\x90'):
        return 'bzip2', 'bzip2 magic'
    if head[:6] == b'\xfd7zXZ\x00':
        return 'xz', 'xz magic'
    if head[:4] == b'\x28\xb5\x2f\xfd':
        return 'zstd', 'zstd magic'
    if head[257:262] == b'ustar':
        return 'tar', 'tar magic'
    m = _EMLX_COUNT.match(head)
    if m and looks_like_headers(head[m.end():]):
        return 'emlx', 'byte-count line followed by a mail header block'
    if head.startswith(b'From '):
        nl = head.find(b'\n')
        if nl >= 0 and looks_like_headers(head[nl + 1:]):
            return 'mbox', '"From " line followed by a mail header block'
    if looks_like_headers(head):
        return 'eml', 'mail header block'
    # Extension, only now that the content did not say what it is.
    kind = _EXT_TYPES.get(ext)
    if kind == 'pst':
        return 'pst', f'named {ext} (no PST magic)'
    if kind == 'mbox' and (not head or head.startswith(b'From ')):
        return 'mbox', f'named {ext}'
    if kind == 'eml' and looks_like_headers(head, strict=False):
        return 'eml', f'named {ext}'
    if kind == 'emlx' and m:
        return 'emlx', f'named {ext}'
    if not head:
        return None, 'the file is empty'
    return None, 'not a mail format this importer knows (PST, mbox, Maildir, ' \
                 'eml, emlx, msg, zip, tar)'


def _real_dir(path):
    try:
        return stat.S_ISDIR(os.lstat(path).st_mode)
    except OSError:
        return False


# ---------------------------------------------------------------------------
# The import tree is writable by the mailbox owner, and it is read, chowned
# and moved by root (cron) and by the service (CAP_DAC_OVERRIDE). Nothing
# privileged may therefore be done through a symbolic link the owner put
# there: every directory of the tree is opened component by component with
# O_DIRECTORY|O_NOFOLLOW, and the open descriptor (not the path) is what the
# pass works through afterwards.
# ---------------------------------------------------------------------------

class UnsafeImportTree(PermissionError):
    """A directory of the import tree is a symbolic link, is not a
    directory, or belongs to neither the mailbox owner nor root. The
    account (or the user's whole tree) is refused and nothing is touched."""


class InsufficientSpace(OSError):
    """Not enough free space on the data disk to take an upload."""


_DIR_FLAGS = (os.O_RDONLY | compat.O_DIRECTORY | compat.O_NOFOLLOW
              | compat.O_CLOEXEC)
USER_TREE = ('root', 'work', 'state', 'archive', 'watch', 'incoming')
ACCOUNT_TREE = ('base', 'inbox', 'incoming', 'done', 'failed')


def _fd_path(fd):
    """A path that means the directory open at `fd`, whatever happens to
    the names that led to it; None where /proc does not offer one."""
    if fd is None:                      # Windows: nothing is held open
        return None
    via = f'/proc/self/fd/{fd}'
    try:
        if os.path.samestat(os.stat(via), os.fstat(fd)):
            return via
    except OSError:
        pass
    return None


class PinnedDir:
    """A directory that open_dir checked and holds open. `path` is its
    name; `via` is the path privileged work must go through: it resolves
    to the directory that was checked even if `path` is swapped for a link
    afterwards. (Without /proc, `via` is `path`: checked, not pinned. On
    Windows a directory cannot be held open at all: `fd` is None.)"""

    def __init__(self, fd, path):
        self.fd, self.path = fd, path
        self.via = _fd_path(fd) or path

    def join(self, name):
        return os.path.join(self.via, name)

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def _tree_parts(root, path):
    root, path = os.path.normpath(str(root)), os.path.normpath(str(path))
    low = os.path.normcase(path)        # the same text on Linux
    for base in (root, os.path.realpath(root)):
        if low == os.path.normcase(base):
            return base, []
        if low.startswith(os.path.normcase(base + os.sep)):
            return base, path[len(base) + 1:].split(os.sep)
    raise UnsafeImportTree(f'{path} is not inside {root}')


def open_dir(root, path, owners=None, *, create=False, mode=0o750, prepare=None):
    """Open the directory `path`, which is below `root` (the user's data
    directory, which the user cannot replace), and return it as a
    PinnedDir. Every component below `root` must be a real directory - not
    a symbolic link, not a file - owned by one of `owners` (uids; None = do
    not check). Raises UnsafeImportTree otherwise, FileNotFoundError when
    a component is missing and `create` is false. With `create`, missing
    components are made (mode `mode`) relative to the open parent and
    handed to `prepare`."""
    base, parts = _tree_parts(root, path)
    if not compat.supports_dir_fd():
        return _open_dir_by_path(base, parts, create, mode, prepare)
    fd = os.open(base, os.O_RDONLY | compat.O_DIRECTORY | compat.O_CLOEXEC)
    here = base
    try:
        for part in parts:
            here = os.path.join(here, part)
            made = False
            while True:
                try:
                    nfd = os.open(part, _DIR_FLAGS, dir_fd=fd)
                    break
                except FileNotFoundError:
                    if not create or made:
                        raise
                    try:
                        os.mkdir(part, mode, dir_fd=fd)
                        if prepare:
                            prepare(os.path.join(_fd_path(fd) or os.path.dirname(here),
                                                 part))
                    except FileExistsError:
                        pass                # made meanwhile; opened (no-follow) above
                    made = True
                except OSError as e:
                    if e.errno not in (errno.ELOOP, errno.ENOTDIR):
                        raise
                    try:
                        link = stat.S_ISLNK(os.stat(part, dir_fd=fd,
                                                    follow_symlinks=False).st_mode)
                    except OSError:
                        link = False
                    raise UnsafeImportTree(
                        f'{here} is {"a symbolic link" if link else "not a directory"}; '
                        f'the import folder is not used until it is a real directory'
                    ) from None
            os.close(fd)
            fd = nfd
            if owners is not None and compat.supports_ownership():
                uid = os.fstat(fd).st_uid
                if uid not in owners:
                    raise UnsafeImportTree(
                        f'{here} belongs to uid {uid}, not to the mailbox owner; '
                        f'the import folder is not used')
        return PinnedDir(fd, here)
    except BaseException:
        os.close(fd)
        raise


def _open_dir_by_path(base, parts, create, mode, prepare):
    """open_dir where a directory cannot be opened (Windows): the same
    rule - every component below `base` is a real directory, not a link or
    a junction - checked by name. Nothing is held open, and there are no
    owners to compare (one user; st_uid means nothing)."""
    if not stat.S_ISDIR(os.stat(base).st_mode):
        raise NotADirectoryError(errno.ENOTDIR, os.strerror(errno.ENOTDIR), base)
    here = base
    for part in parts:
        here = os.path.join(here, part)
        try:
            st = os.lstat(here)
        except FileNotFoundError:
            if not create:
                raise
            try:
                os.mkdir(here, mode)
                if prepare:
                    prepare(here)
            except FileExistsError:
                pass
            st = os.lstat(here)
        link = compat.is_link(here)
        if link or not stat.S_ISDIR(st.st_mode):
            raise UnsafeImportTree(
                f'{here} is {"a symbolic link" if link else "not a directory"}; '
                f'the import folder is not used until it is a real directory')
    return PinnedDir(None, here)


def check_tree(root, paths, owners=None):
    """Refuse (UnsafeImportTree) unless every one of `paths` that exists is
    a real directory reached through real directories only, each owned by
    the mailbox owner or root. Touches nothing."""
    for p in paths:
        try:
            open_dir(root, p, owners).close()
        except FileNotFoundError:
            continue


def account_root(acct_paths):
    """<data_dir>/<user> of pst.account_paths(...)."""
    return os.path.dirname(os.path.dirname(acct_paths['base']))


def check_account_tree(acct_paths, owners=None):
    check_tree(account_root(acct_paths), [acct_paths[k] for k in ACCOUNT_TREE], owners)


def move_noreplace(src, dst):
    """Rename `src` to `dst` unless `dst` exists (FileExistsError): a file
    that arrived under the same name meanwhile is never replaced."""
    if compat.IS_WINDOWS:
        # os.rename never replaces on Windows (FileExistsError); the check
        # in front says the same in words, also for a directory.
        if os.path.lexists(dst):
            raise FileExistsError(errno.EEXIST, os.strerror(errno.EEXIST), dst)
        os.rename(src, dst)
        return
    try:
        os.link(src, dst, follow_symlinks=False)
    except FileExistsError:
        raise
    except OSError as e:
        # A directory, or a filesystem / kernel policy without hard links.
        if e.errno not in (errno.EPERM, errno.EACCES, errno.EXDEV, errno.ENOSYS,
                           errno.EOPNOTSUPP, errno.EMLINK):
            raise
        return _renameat2_noreplace(src, dst)
    os.unlink(src)


def _renameat2_noreplace(src, dst):
    fn = None
    try:
        import ctypes
        fn = ctypes.CDLL(None, use_errno=True).renameat2
        fn.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p,
                       ctypes.c_uint]
        fn.restype = ctypes.c_int
    except (ImportError, OSError, AttributeError):
        fn = None
    if fn is not None:
        at_fdcwd, rename_noreplace = -100, 1
        if fn(at_fdcwd, os.fsencode(src), at_fdcwd, os.fsencode(dst),
              rename_noreplace) == 0:
            return
        err = ctypes.get_errno()
        if err == errno.EEXIST:
            raise FileExistsError(err, os.strerror(err), dst)
        if err not in (errno.EINVAL, errno.ENOSYS):
            raise OSError(err, os.strerror(err), dst)
    if os.path.lexists(dst):              # last resort: check, then rename
        raise FileExistsError(errno.EEXIST, os.strerror(errno.EEXIST), dst)
    os.rename(src, dst)


def is_maildir(path):
    """A Maildir (cur/ and new/, or cur/ and tmp/), or a Maildir++ root
    whose only mail is in `.Folder` subdirectories."""
    def one(d):
        cur = os.path.join(d, 'cur')
        if not _real_dir(cur):
            return False
        if _real_dir(os.path.join(d, 'new')) or _real_dir(os.path.join(d, 'tmp')):
            return True
        # An archive made without its empty directories has lost new/ and
        # tmp/; cur/ full of Maildir file names is still a Maildir.
        try:
            names = os.listdir(cur)
        except OSError:
            return False
        return bool(names) and all(compat.maildir_flags(n) is not None
                                   for n in names[:50])
    if one(path):
        return True
    try:
        with os.scandir(path) as it:
            return any(e.name.startswith('.') and e.name not in ('.', '..')
                       and e.is_dir(follow_symlinks=False) and one(e.path)
                       for e in it)
    except OSError:
        return False


def detect_unit(path):
    """(type, how) of something dropped in an inbox: a file or a directory."""
    try:
        st = os.lstat(path)
    except OSError as e:
        return None, f'cannot stat: {e.strerror}'
    if stat.S_ISDIR(st.st_mode):
        if is_maildir(path):
            return 'maildir', 'directory with cur/ new/ tmp/'
        return 'directory', 'directory; every file in it is detected on its own'
    if stat.S_ISREG(st.st_mode):
        return detect_file(path)
    return None, 'not a regular file or directory'


# ---------------------------------------------------------------------------
# mbox, emlx
# ---------------------------------------------------------------------------

_QUOTED_FROM = re.compile(rb'^>+From ')
_AFTER_FROM = re.compile(rb'^(?:[\x21-\x39\x3b-\x7e]{1,76}:|>From )')


def _pieces(f, limit=1 << 20):
    """Lines of a binary file, never more than `limit` bytes at once (a
    damaged mbox can hold one "line" of gigabytes). Yields (bytes,
    starts_a_line)."""
    start = True
    while True:
        piece = f.readline(limit)
        if not piece:
            return
        yield piece, start
        start = piece.endswith(b'\n')


def iter_mbox(f, max_bytes):
    """Messages of an mbox, streamed: memory use is one message, whatever
    the size of the file. Yields (raw, size); raw is None for a message
    over `max_bytes` (it is skipped, the scan goes on).

    A message starts at a line beginning "From " that is followed by a
    header line, so an unquoted "From " at the start of a body line (mboxo
    writers, Apple) does not split a message. ">From " quoting is undone
    the mboxrd way: one ">" comes off every line matching ^>+From .
    """
    state = {'buf': [], 'size': 0, 'over': False, 'open': False}

    def add(piece, start):
        if start and _QUOTED_FROM.match(piece):
            piece = piece[1:]
        state['size'] += len(piece)
        if state['size'] > max_bytes:
            state['over'], state['buf'] = True, []
        elif not state['over']:
            state['buf'].append(piece)

    def emit():
        buf, size, over = state['buf'], state['size'], state['over']
        state.update(buf=[], size=0, over=False)
        if over:
            return None, size
        if buf and buf[-1] in (b'\n', b'\r\n'):
            buf.pop()                      # the blank line before the next From
        return b''.join(buf), size

    held = None                            # a "From " line not yet confirmed
    for piece, start in _pieces(f):
        if held is not None:
            if start and _AFTER_FROM.match(piece):
                if state['open']:
                    yield emit()
                state['open'] = True
            elif state['open']:
                add(held, True)            # body text that began with "From "
            held = None
        if start and piece.startswith(b'From '):
            held = piece
            continue
        if state['open']:
            add(piece, start)
    if held is not None and state['open']:
        add(held, True)
    if state['open']:
        yield emit()


def emlx_message(data):
    """The RFC 822 message inside an Apple .emlx: a decimal byte count on
    the first line, that many bytes of message, then a plist trailer."""
    m = _EMLX_COUNT.match(data)
    if not m:
        raise ValueError('no byte-count line')
    count = int(m.group(1))
    body = data[m.end():m.end() + count]
    if len(body) < count:
        raise ValueError(f'truncated: {len(body)} of {count} bytes')
    return body


def mbox_folder_parts(parts, name):
    """Folder for an mbox file. Thunderbird keeps subfolders in `X.sbd/`,
    Apple exports `X.mbox/mbox`; otherwise the file name without .mbox."""
    parts = [p[:-4] if p.lower().endswith('.sbd') else p for p in parts]
    if name.lower() == 'mbox' and parts and parts[-1].lower().endswith('.mbox'):
        return parts[:-1] + [parts[-1][:-5]]
    stem = name
    for ext in ('.mbox', '.mbx'):
        if stem.lower().endswith(ext) and len(stem) > len(ext):
            stem = stem[:-len(ext)]
    return parts + [stem]


def emlx_folder_parts(parts):
    """Apple Mail stores X.mbox/<uuid>/Data/Messages/N.emlx: keep the
    mailbox names, drop the storage directories below them."""
    last = max((i for i, p in enumerate(parts) if p.lower().endswith('.mbox')),
               default=None)
    if last is None:
        return parts
    return [p[:-5] if p.lower().endswith('.mbox') else p for p in parts[:last + 1]]


# ---------------------------------------------------------------------------
# The converter (runs as the mailbox owner)
# ---------------------------------------------------------------------------

def _safe_member(name):
    """Path components of an archive member, or None when the name is
    absolute or climbs out with `..`. Leading dots are kept (Maildir++
    folders); the workspace is private and walked by this code only."""
    name = name.replace('\\', '/')
    if name.startswith('/') or re.match(r'^[A-Za-z]:', name):
        return None
    out = []
    for p in name.split('/'):
        if p in ('', '.'):
            continue
        if p == '..':
            return None
        p = ''.join('_' if (ord(c) < 32 or c == '\x7f') else c for c in p)
        while len(p.encode('utf-8', 'replace')) > 200:
            p = p[:-1]
        # Windows: a Maildir `:2,` becomes `!2,`, other characters and
        # names NTFS refuses become safe ones (unchanged on Linux)
        out.append(compat.safe_filename_component(p or '_'))
    return out or None


class Converter:
    """Reads one import unit and delivers its messages. One instance per
    unit; all counters and limits are per unit."""

    def __init__(self, maildir, work_dir, *, cache_path=None, readpst=None,
                 msgconvert=None, timeout=pst.READPST_TIMEOUT,
                 max_message_bytes=pst.MAX_MESSAGE_BYTES,
                 max_unpack_bytes=MAX_UNPACK_BYTES, max_ratio=MAX_UNPACK_RATIO,
                 ratio_floor=RATIO_FLOOR_BYTES, max_members=MAX_MEMBERS):
        self.maildir, self.work, self.cache_path = maildir, work_dir, cache_path
        self.readpst, self.msgconvert = readpst or pst.READPST, msgconvert or MSGCONVERT
        self.timeout, self.max_msg = timeout, max_message_bytes
        self.max_unpack, self.max_ratio = max_unpack_bytes, max_ratio
        self.ratio_floor, self.max_members = ratio_floor, max_members
        self.unpacked = self.members = self._n = 0
        self.existing = set()
        self.stats = {'added': 0, 'duplicates': 0, 'too_large': 0, 'failed': 0,
                      'skipped_links': 0, 'ignored': 0, 'refused_members': 0,
                      'folders': {}, 'types': {}, 'prefixes': [], 'errors': []}

    # -- bookkeeping -------------------------------------------------------
    def note(self, msg):
        if len(self.stats['errors']) < 20:
            self.stats['errors'].append(msg)

    def fail(self, msg):
        self.stats['failed'] += 1
        self.note(msg)

    def scratch(self, tag):
        self._n += 1
        d = os.path.join(self.work, f'{tag}-{self._n}')
        os.mkdir(d, 0o700)
        return d

    def read_capped(self, path, label, extra=0):
        """Bytes of a regular file, or None (counted) when it is not one or
        is over the message limit."""
        try:
            st = os.lstat(path)
            if not stat.S_ISREG(st.st_mode):
                self.stats['skipped_links'] += 1
                return None
            if st.st_size > self.max_msg + extra:
                self.stats['too_large'] += 1
                self.note(f'{label}: {st.st_size} bytes, over the '
                          f'{self.max_msg} byte limit')
                return None
            with os.fdopen(pst._open_nofollow(path), 'rb') as f:
                return f.read(self.max_msg + extra + 1)
        except OSError as e:
            self.fail(f'{label}: {e.strerror or e}')
            return None

    def deliver(self, kind, parts, raw, label, flags='S'):
        if raw is None or not raw.strip():
            return
        if len(raw) > self.max_msg:
            self.stats['too_large'] += 1
            self.note(f'{label}: {len(raw)} bytes, over the {self.max_msg} byte limit')
            return
        try:
            key = pst.message_key_from_bytes(raw)
        except Exception as e:
            return self.fail(f'{label}: {type(e).__name__}: {e}')
        if key in self.existing:
            self.stats['duplicates'] += 1
            return
        folder = pst.map_folder(parts, PREFIX[kind])
        try:
            pst.deliver(os.path.join(self.maildir, *folder), raw,
                        pst._message_epoch(raw), flags)
        except OSError as e:
            return self.fail(f'write {"/".join(folder)}: {e}')
        self.existing.add(key)
        st = self.stats
        st['added'] += 1
        fname = '/'.join(folder)
        st['folders'][fname] = st['folders'].get(fname, 0) + 1
        st['types'][kind] = st['types'].get(kind, 0) + 1
        if prefix_str(kind) not in st['prefixes']:
            st['prefixes'].append(prefix_str(kind))

    # -- readers -----------------------------------------------------------
    def read_pst(self, path, parts):
        work = self.scratch('pst')
        try:
            out = os.path.join(work, 'out')
            pst.run_readpst(path, out, work, self.readpst, self.timeout)
            files, skipped = pst._iter_output(out)
            self.stats['skipped_links'] += skipped
            for fparts, p, _size in files:
                label = '/'.join(fparts + [os.path.basename(p)])
                self.deliver('pst', parts + fparts, self.read_capped(p, label), label)
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def read_mbox(self, path, parts, label):
        try:
            f = os.fdopen(pst._open_nofollow(path), 'rb')
        except OSError as e:
            raise ImportFailed(f'{label}: {e.strerror or e}')
        with f:
            for n, (raw, size) in enumerate(iter_mbox(f, self.max_msg), 1):
                if raw is None:
                    self.stats['too_large'] += 1
                    self.note(f'{label} message {n}: {size} bytes, over the '
                              f'{self.max_msg} byte limit')
                    continue
                self.deliver('mbox', parts, raw, f'{label} message {n}')

    def read_maildir(self, root, parts):
        """A Maildir and every folder below it: Maildir++ `.A.B` names and
        nested directories both become A/B. The root is the INBOX."""
        for dirpath, dirnames, _files in os.walk(root, followlinks=False):
            dirnames.sort()
            subs = [d for d in ('cur', 'new') if d in dirnames
                    and _real_dir(os.path.join(dirpath, d))]
            dirnames[:] = [d for d in dirnames if d not in ('cur', 'new', 'tmp')]
            if not subs:
                continue
            rel = os.path.relpath(dirpath, root)
            comps = []
            for c in ([] if rel == '.' else rel.split(os.sep)):
                comps.extend([x for x in c.split('.') if x] if c.startswith('.') else [c])
            folder = parts + (comps or ['INBOX'])
            for sub in subs:
                try:
                    names = sorted(os.listdir(os.path.join(dirpath, sub)))
                except OSError as e:
                    self.fail(f'{"/".join(folder)}: {e.strerror or e}')
                    continue
                for fn in names:
                    if fn.startswith('.'):
                        continue
                    info = compat.maildir_flags(fn) or ''
                    flags = ''.join(c for c in info if c in 'DFPR') + 'S'
                    label = '/'.join(folder + [fn])
                    self.deliver('maildir', folder,
                                 self.read_capped(os.path.join(dirpath, sub, fn), label),
                                 label, flags)

    def read_eml(self, path, parts, label):
        self.deliver('eml', parts, self.read_capped(path, label), label)

    def read_emlx(self, path, parts, label):
        data = self.read_capped(path, label, extra=1024 * 1024)
        if data is None:
            return
        try:
            raw = emlx_message(data)
        except ValueError as e:
            return self.fail(f'{label}: not a usable .emlx ({e})')
        self.deliver('emlx', emlx_folder_parts(parts), raw, label)

    def read_msg(self, path, parts, label):
        exe = compat.have_tool(self.msgconvert)
        if not exe:
            raise ImportFailed(msg_missing_message())
        work = self.scratch('msg')
        try:
            out = os.path.join(work, 'message.eml')
            cmd = pst.limit_cmd([exe, '--outfile', out, os.path.abspath(path)],
                                path, self.max_msg)
            try:
                res = subprocess.run(cmd, stdin=subprocess.DEVNULL,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     cwd=work, env=pst.child_env(HOME=work),
                                     timeout=MSGCONVERT_TIMEOUT)
            except subprocess.TimeoutExpired:
                raise ImportFailed(f'{label}: msgconvert timed out')
            except OSError as e:
                raise ImportFailed(f'{label}: msgconvert could not be run: {e.strerror}')
            if res.returncode != 0 or not os.path.isfile(out):
                tail = (res.stderr or res.stdout or b'').decode('utf-8', 'replace')[-300:]
                raise ImportFailed(f'{label}: msgconvert exited {res.returncode}: '
                                   f'{tail.strip()}')
            self.deliver('msg', parts, self.read_capped(out, label), label)
        finally:
            shutil.rmtree(work, ignore_errors=True)

    # -- containers --------------------------------------------------------
    def _count(self, n, source_size, what):
        """Account for `n` unpacked bytes; refuse absurd totals."""
        self.unpacked += n
        if self.unpacked > self.max_unpack:
            raise ImportFailed(f'{what} unpacks to more than the '
                               f'{self.max_unpack} byte limit - refused')
        if (self.unpacked > self.ratio_floor
                and self.unpacked > self.max_ratio * max(source_size, 1)):
            raise ImportFailed(f'{what} expands more than {self.max_ratio} times '
                               f'its size - refused as a decompression bomb')

    def _member(self):
        self.members += 1
        if self.members > self.max_members:
            raise ImportFailed(f'more than {self.max_members} files in the '
                               f'archive - refused')

    def _write_member(self, dest_root, comps, src, source_size, what, limit=None):
        d = os.path.join(dest_root, *comps[:-1])
        os.makedirs(d, mode=0o700, exist_ok=True)
        target, n = os.path.join(d, comps[-1]), 1
        while os.path.lexists(target):
            n += 1
            target = os.path.join(d, f'{comps[-1]}~{n}')
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | pst.O_SAFE, 0o600)
        written = 0
        with os.fdopen(fd, 'wb') as f:
            while True:
                block = src.read(1024 * 1024)
                if not block:
                    break
                written += len(block)
                if limit is not None and written > limit:
                    raise ImportFailed(f'{what}: a member is larger than it declares')
                self._count(len(block), source_size, what)
                f.write(block)
                if (self.unpacked >> 26) != ((self.unpacked - len(block)) >> 26):
                    if shutil.disk_usage(dest_root).free < FREE_RESERVE:
                        raise ImportFailed('not enough free space to unpack ' + what)
        return target

    def _refuse(self, what, name, why):
        self.stats['refused_members'] += 1
        self.note(f'{what}: member {name!r} refused ({why})')

    def unpack_zip(self, path, dest, what):
        size = os.path.getsize(path)
        try:
            zf = zipfile.ZipFile(path)
        except (zipfile.BadZipFile, OSError) as e:
            raise ImportFailed(f'{what}: not a readable zip ({e})')
        with zf:
            infos = zf.infolist()
            total = sum(i.file_size for i in infos)
            if len(infos) > self.max_members:
                raise ImportFailed(f'{what}: {len(infos)} members, over the '
                                   f'{self.max_members} limit - refused')
            if self.unpacked + total > self.max_unpack:
                raise ImportFailed(f'{what} unpacks to {total} bytes, over the '
                                   f'{self.max_unpack} byte limit - refused')
            if total > self.ratio_floor and total > self.max_ratio * max(size, 1):
                raise ImportFailed(f'{what} expands more than {self.max_ratio} times '
                                   f'its size - refused as a decompression bomb')
            if total + FREE_RESERVE > shutil.disk_usage(dest).free:
                raise ImportFailed(f'not enough free space to unpack {what}')
            for info in infos:
                self._member()
                mode = (info.external_attr >> 16) & 0xFFFF
                comps = _safe_member(info.filename)
                if info.is_dir():
                    if comps:               # keep empty dirs (a Maildir's new/, tmp/)
                        os.makedirs(os.path.join(dest, *comps), mode=0o700, exist_ok=True)
                elif comps is None:
                    self._refuse(what, info.filename, 'path leaves the archive')
                elif stat.S_IFMT(mode) not in (0, stat.S_IFREG):
                    self._refuse(what, info.filename, 'link or special file')
                elif info.flag_bits & 0x1:
                    self._refuse(what, info.filename, 'encrypted')
                else:
                    try:
                        with zf.open(info) as src:
                            self._write_member(dest, comps, src, size, what,
                                               limit=info.file_size)
                    except (zipfile.BadZipFile, EOFError, NotImplementedError,
                            RuntimeError, OSError) as e:
                        self.fail(f'{what}: member {info.filename!r}: {e}')

    def unpack_tar(self, path, dest, what, mode='r:*'):
        size = os.path.getsize(path)
        try:
            tf = tarfile.open(path, mode)
        except (tarfile.TarError, EOFError, OSError, lzma.LZMAError, zlib.error) as e:
            raise ImportFailed(f'{what}: not a readable tar archive ({e})')
        try:
            with tf:
                for m in tf:
                    self._member()
                    comps = _safe_member(m.name)
                    if m.isdir():
                        if comps:           # keep empty dirs (a Maildir's new/, tmp/)
                            os.makedirs(os.path.join(dest, *comps), mode=0o700,
                                        exist_ok=True)
                    elif comps is None:
                        self._refuse(what, m.name, 'path leaves the archive')
                    elif not m.isreg():
                        self._refuse(what, m.name, 'link or special file')
                    else:
                        src = tf.extractfile(m)
                        if src is None:
                            self._refuse(what, m.name, 'no data')
                            continue
                        with src:
                            self._write_member(dest, comps, src, size, what,
                                               limit=m.size)
        except (tarfile.TarError, EOFError, lzma.LZMAError, zlib.error) as e:
            raise ImportFailed(f'{what}: the archive is damaged ({e})')
        except OSError as e:
            if e.errno is not None:        # a real I/O error, not bad data
                raise
            raise ImportFailed(f'{what}: the archive is damaged ({e})')

    def expand(self, path, typ, name, what):
        """Unpack a container. Returns ('tree', directory) or, for a single
        compressed file such as x.mbox.gz, ('file', path, name)."""
        dest = self.scratch('unpack')
        if typ == 'zip':
            self.unpack_zip(path, dest, what)
            return ('tree', dest)
        if typ == 'tar':
            self.unpack_tar(path, dest, what)
            return ('tree', dest)
        inner = name
        for ext in ('.gz', '.bz2', '.xz', '.zst', '.tgz', '.tbz2', '.txz'):
            if inner.lower().endswith(ext) and len(inner) > len(ext):
                inner = inner[:-len(ext)] + ('.tar' if ext.startswith('.t') else '')
                break
        comps = _safe_member(inner) or ['file']
        size = os.path.getsize(path)
        if typ == 'zstd':
            exe = compat.have_tool(ZSTD)
            if not exe:
                raise ImportFailed(f'{what}: zstd-compressed files need the zstd '
                                   f'program (Debian package zstd), which is not installed')
            proc = subprocess.Popen([exe, '-dc', '--', os.path.abspath(path)],
                                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL, env=pst.child_env())
            try:
                target = self._write_member(dest, comps[-1:], proc.stdout, size, what)
            except BaseException:
                proc.kill()
                proc.wait()
                raise
            finally:
                proc.stdout.close()
            if proc.wait() != 0:
                raise ImportFailed(f'{what}: zstd could not decompress it')
        else:
            try:
                tarfile.open(path, 'r:*').close()
                is_tar = True
            except (tarfile.TarError, EOFError, OSError, lzma.LZMAError, zlib.error):
                is_tar = False
            if is_tar:
                self.unpack_tar(path, dest, what)
                return ('tree', dest)
            opener = {'gzip': gzip.open, 'bzip2': bz2.open, 'xz': lzma.open}[typ]
            try:
                with opener(path, 'rb') as src:
                    target = self._write_member(dest, comps[-1:], src, size, what)
            except (EOFError, lzma.LZMAError, zlib.error) as e:
                raise ImportFailed(f'{what}: could not decompress ({e})')
            except OSError as e:
                if e.errno is not None:
                    raise
                raise ImportFailed(f'{what}: could not decompress ({e})')
        kind, _how = detect_file(target)
        if kind == 'tar':
            inner_dest = self.scratch('unpack')
            self.unpack_tar(target, inner_dest, what, mode='r:')
            os.unlink(target)
            return ('tree', inner_dest)
        return ('file', target, comps[-1])

    # -- dispatch ----------------------------------------------------------
    def import_file(self, path, typ, parts, name, depth):
        label = '/'.join(parts + [name])
        if typ == 'pst':
            self.read_pst(path, parts)
        elif typ == 'mbox':
            self.read_mbox(path, mbox_folder_parts(parts, name), label)
        elif typ == 'eml':
            self.read_eml(path, parts, label)
        elif typ == 'emlx':
            self.read_emlx(path, parts, label)
        elif typ == 'msg':
            self.read_msg(path, parts, label)
        elif typ in CONTAINER_TYPES:
            if depth >= MAX_CONTAINER_DEPTH:
                raise ImportFailed(f'{label}: an archive inside an archive inside an '
                                   f'archive - only one level of nesting is unpacked')
            got = self.expand(path, typ, name, label)
            try:
                if got[0] == 'tree':
                    self.import_tree(got[1], parts, depth + 1)
                else:
                    kind, how = detect_file(got[1])
                    if kind is None:
                        raise ImportFailed(f'{label}: the compressed file inside is {how}')
                    if kind in CONTAINER_TYPES:
                        raise ImportFailed(f'{label}: compressed more than once - refused')
                    self.import_file(got[1], kind, parts, got[2], depth + 1)
            finally:
                shutil.rmtree(os.path.dirname(got[1]) if got[0] == 'file' else got[1],
                              ignore_errors=True)
        else:
            raise ImportFailed(f'{label}: unknown type {typ!r}')

    def import_tree(self, root, parts, depth):
        """Every file under a directory, each detected by its content.
        Maildirs are taken whole; links and special files are never opened."""
        if is_maildir(root):
            return self.read_maildir(root, parts)
        try:
            entries = sorted(os.scandir(root), key=lambda e: e.name)
        except OSError as e:
            return self.fail(f'{"/".join(parts)}: {e.strerror or e}')
        for e in entries:
            try:
                st = os.lstat(e.path)
            except OSError:
                continue
            if stat.S_ISDIR(st.st_mode):
                if not e.name.startswith('.'):
                    self.import_tree(e.path, parts + [e.name], depth)
                continue
            if not stat.S_ISREG(st.st_mode):
                self.stats['skipped_links'] += 1
                continue
            if e.name.startswith('.'):
                continue
            kind, how = detect_file(e.path)
            if kind is None:
                self.stats['ignored'] += 1
                if self.stats['ignored'] <= 10:
                    self.note(f'{"/".join(parts + [e.name])}: not imported ({how})')
                continue
            try:
                self.import_file(e.path, kind, parts, e.name, depth)
            except ImportFailed as err:
                self.fail(str(err))

    def run(self, path, typ, name):
        os.makedirs(self.work, mode=0o700)
        try:
            os.makedirs(self.maildir, mode=0o700, exist_ok=True)
            self.existing = pst.load_existing_keys(self.maildir, self.cache_path)
            if typ == 'maildir':
                self.read_maildir(path, [])
            elif typ == 'directory':
                self.import_tree(path, [name], 0)
            else:
                self.import_file(path, typ, [], name, 0)
            st = self.stats
            found = st['added'] + st['duplicates'] + st['too_large'] + st['failed']
            if not found and (typ == 'directory' or typ in CONTAINER_TYPES):
                raise ImportFailed(
                    f'no mail found in this {TYPE_LABELS.get(typ, typ)}'
                    + (f' ({st["ignored"]} file(s) not recognised)' if st['ignored'] else '')
                    + (f' ({st["refused_members"]} member(s) refused)'
                       if st['refused_members'] else ''))
        finally:
            shutil.rmtree(self.work, ignore_errors=True)
        return self.stats


def convert_unit(path, typ, maildir, work_dir, *, name=None, **kw):
    """Import one file or directory of type `typ` into `maildir`. Runs as
    the mailbox owner. Returns the stats dict; raises ImportFailed when the
    unit as a whole cannot be imported (nothing useful was delivered)."""
    return Converter(maildir, work_dir, **kw).run(
        path, typ, name or os.path.basename(os.path.normpath(path)))


def stats_status(stats):
    return 'ok' if not (stats['failed'] or stats.get('refused_members')) else 'partial'


def convert_in_process(path, typ, name, maildir, work_dir, cache_path=None, **kw):
    """What the `convert` command does, as a function: the stats dict with
    'status' ('ok' / 'partial' / 'failed' + 'error'); never raises. It has
    the signature of UserImporter's `convert` hook, for a host where the
    converter is not a child run as another user (Windows: one user)."""
    try:
        stats = convert_unit(path, typ, maildir, work_dir, name=name,
                             cache_path=cache_path, **kw)
        stats['status'] = stats_status(stats)
    except ImportFailed as e:
        stats = {'status': 'failed', 'error': str(e)}
    except Exception as e:  # report, never a traceback with message content
        stats = {'status': 'failed', 'error': f'{type(e).__name__}: {e}'}
    return stats


# ---------------------------------------------------------------------------
# Inbox scan, signatures, hashes
# ---------------------------------------------------------------------------

def tree_signature(path):
    """(total size, signature) of a directory tree: file count, bytes and
    the newest mtime. Links are not followed and not counted."""
    count = size = newest = 0
    for root, dirs, files in os.walk(path, followlinks=False):
        for fn in files:
            try:
                st = os.lstat(os.path.join(root, fn))
            except OSError:
                continue
            if stat.S_ISREG(st.st_mode):
                count, size = count + 1, size + st.st_size
                newest = max(newest, st.st_mtime_ns)
    return size, ('dir', count, size, newest)


def unit_signature(path):
    try:
        st = os.lstat(path)
    except OSError:
        return None
    if stat.S_ISREG(st.st_mode):
        return pst._sig(st)
    if stat.S_ISDIR(st.st_mode):
        return tree_signature(path)[1]
    return None


def tree_sha256(path):
    """One sha256 for a directory: over the sorted relative path, size and
    sha256 of every regular file in it."""
    rows = []
    for root, dirs, files in os.walk(path, followlinks=False):
        for fn in files:
            p = os.path.join(root, fn)
            try:
                if not stat.S_ISREG(os.lstat(p).st_mode):
                    continue
                # '/' on every platform: the same tree has the same hash
                rows.append((os.path.relpath(p, path).replace(os.sep, '/'),
                             os.path.getsize(p), pst.sha256_file(p)))
            except OSError:
                continue
    h = hashlib.sha256()
    for rel, size, digest in sorted(rows):
        h.update(f'{rel}\0{size}\0{digest}\n'.encode('utf-8', 'surrogateescape'))
    return h.hexdigest()


def scan_inbox(inbox, io_dir=None):
    """What is waiting in one account's inbox/: every top-level file and
    directory is one import unit. Dotfiles (.incoming), *.tmp and sha256
    sidecars are skipped silently (a sidecar arrives before its file);
    links are reported and never followed. An inbox that is itself a
    symbolic link is not read at all (pst.dir_entries).

    A candidate's 'path' is its real path (for messages and for the
    converter, which runs as the owner); 'io' / 'sidecar_io' are the paths
    privileged code must use: through `io_dir`, the pinned directory."""
    out = {'candidates': [], 'ignored': []}
    entries = pst.dir_entries(inbox, io_dir)
    if entries is None:
        return out
    io = io_dir or inbox
    names = {n for n, _st, _err in entries}
    for n, st, err in entries:
        low = n.lower()
        if n.startswith('.') or low.endswith('.tmp') or low.endswith(SIDECAR_SUFFIX):
            continue
        if st is None:
            out['ignored'].append((n, err))
            continue
        if stat.S_ISLNK(st.st_mode):
            out['ignored'].append((n, 'symbolic link (not followed)'))
            continue
        if stat.S_ISREG(st.st_mode):
            kind, size, sig = 'file', st.st_size, pst._sig(st)
        elif stat.S_ISDIR(st.st_mode):
            kind = 'dir'
            size, sig = tree_signature(os.path.join(io, n))
        else:
            out['ignored'].append((n, 'not a regular file or directory'))
            continue
        sidecar = n + SIDECAR_SUFFIX
        has_side = kind == 'file' and sidecar in names
        out['candidates'].append({
            'name': n, 'path': os.path.join(inbox, n), 'io': os.path.join(io, n),
            'kind': kind, 'size': size, 'mtime': st.st_mtime, 'sig': sig,
            'source': 'inbox',
            'sidecar': os.path.join(inbox, sidecar) if has_side else None,
            'sidecar_io': os.path.join(io, sidecar) if has_side else None})
    return out


def split_stable(candidates, wait=pst.STABLE_WAIT, sleep=time.sleep):
    """A unit is stable when its signature is unchanged after `wait`
    seconds (a file: dev, inode, size, mtime; a directory: file count,
    bytes, newest mtime). Returns (stable, unstable)."""
    if not candidates:
        return [], []
    if wait > 0:
        sleep(wait)
    stable, unstable = [], []
    for c in candidates:
        (stable if unit_signature(c.get('io') or c['path']) == c['sig']
         else unstable).append(c)
    return stable, unstable


# ---------------------------------------------------------------------------
# Ledger (per-user index database)
# ---------------------------------------------------------------------------

LEDGER_STATUSES = ('queued', 'running', 'done', 'partial', 'failed', 'skipped')
_LEDGER_FIELDS = ('sha256', 'size_bytes', 'original_name', 'detected_type',
                  'account', 'target_prefix', 'source', 'trigger', 'status',
                  'imported', 'duplicates', 'failed', 'too_large', 'queued_at',
                  'started_at', 'finished_at', 'finished_epoch', 'error', 'detail')


class Ledger:
    """The import ledger and the retention audit trail. `conn` is a
    connection to the per-user index database (search_index.get_index_db,
    which creates the tables)."""

    def __init__(self, conn):
        self.conn = conn
        self.conn.row_factory = sqlite3.Row

    def close(self):
        self.conn.close()

    @staticmethod
    def _row(r):
        d = dict(r)
        try:
            d['detail'] = json.loads(d.get('detail') or '{}')
        except ValueError:
            d['detail'] = {}
        return d

    def add(self, **fields):
        bad = set(fields) - set(_LEDGER_FIELDS)
        if bad:
            raise ValueError(f'unknown ledger field(s): {sorted(bad)}')
        if isinstance(fields.get('detail'), dict):
            fields['detail'] = json.dumps(fields['detail'])
        cols = ', '.join(fields)
        cur = self.conn.execute(
            f'INSERT INTO import_ledger ({cols}) VALUES ({", ".join("?" * len(fields))})',
            list(fields.values()))
        self.conn.commit()
        return cur.lastrowid

    def update(self, row_id, **fields):
        bad = set(fields) - set(_LEDGER_FIELDS)
        if bad:
            raise ValueError(f'unknown ledger field(s): {sorted(bad)}')
        if isinstance(fields.get('detail'), dict):
            fields['detail'] = json.dumps(fields['detail'])
        sets = ', '.join(f'{k} = ?' for k in fields)
        self.conn.execute(f'UPDATE import_ledger SET {sets} WHERE id = ?',
                          list(fields.values()) + [row_id])
        self.conn.commit()

    def get(self, row_id):
        r = self.conn.execute('SELECT * FROM import_ledger WHERE id = ?',
                              (row_id,)).fetchone()
        return self._row(r) if r else None

    def rows(self, limit=50, account=None):
        q, args = 'SELECT * FROM import_ledger', []
        if account:
            q, args = q + ' WHERE account = ?', [account]
        return [self._row(r) for r in self.conn.execute(
            q + ' ORDER BY id DESC LIMIT ?', args + [int(limit)])]

    def find_done(self, sha256, account):
        """The row that already imported this exact content into this
        account, if any."""
        if not sha256:
            return None
        r = self.conn.execute(
            "SELECT * FROM import_ledger WHERE sha256 = ? AND account = ? "
            "AND status = 'done' ORDER BY id DESC LIMIT 1", (sha256, account)).fetchone()
        return self._row(r) if r else None

    def last_row_for(self, name, source):
        r = self.conn.execute(
            'SELECT * FROM import_ledger WHERE original_name = ? AND source = ? '
            'ORDER BY id DESC LIMIT 1', (name, source)).fetchone()
        return self._row(r) if r else None

    def last_done_epoch(self, account, series=None, before_id=None):
        """When a file of this account (and series) was last imported with
        status done. None if never. `before_id` leaves out that ledger row
        and every later one: the retention freeze (R4) asks when the last
        success BEFORE the current run was, so it passes the run's first
        row."""
        q = ("SELECT original_name, finished_epoch FROM import_ledger "
             "WHERE account = ? AND status = 'done'")
        args = [account]
        if before_id is not None:
            q, args = q + ' AND id < ?', args + [before_id]
        for r in self.conn.execute(q + ' ORDER BY id DESC LIMIT 2000', args):
            if series is None or series_key(r['original_name']) == series:
                return r['finished_epoch']
        return None

    def last_row_of(self, account):
        r = self.conn.execute('SELECT * FROM import_ledger WHERE account = ? '
                              'ORDER BY id DESC LIMIT 1', (account,)).fetchone()
        return self._row(r) if r else None

    def mark_interrupted(self, now):
        """Rows left queued/running by a killed process become failed. The
        caller holds the per-user import lock, so nothing else is running."""
        cur = self.conn.execute(
            "UPDATE import_ledger SET status = 'failed', finished_at = ?, "
            "finished_epoch = ?, error = CASE WHEN error = '' THEN "
            "'interrupted: the import process ended before it finished' ELSE error END "
            "WHERE status IN ('queued', 'running')", (pst.now_str(now), now))
        self.conn.commit()
        return cur.rowcount

    def add_event(self, *, now, account, series, action, rule, name, size_bytes=0,
                  copy_date='', reason='', ledger_id=None):
        self.conn.execute(
            'INSERT INTO import_retention_events (at, at_epoch, account, series, '
            'action, rule, name, size_bytes, copy_date, reason, ledger_id) '
            'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
            (pst.now_str(now), now, account, series, action, rule, name,
             int(size_bytes or 0), copy_date, reason, ledger_id))
        self.conn.commit()

    def events(self, limit=50, account=None):
        q, args = 'SELECT * FROM import_retention_events', []
        if account:
            q, args = q + ' WHERE account = ?', [account]
        return [dict(r) for r in self.conn.execute(
            q + ' ORDER BY id DESC LIMIT ?', args + [int(limit)])]


# ---------------------------------------------------------------------------
# Retention of done/ (ADR retention-never-rotates-to-empty, R1-R6)
# ---------------------------------------------------------------------------

_DUP_PREFIX = re.compile(r'^\d+~')
DEFAULT_STALE_AFTER = 48 * 3600


def series_key(name):
    """The retention set a copy belongs to: its name with the YYYYMMDD
    replaced, e.g. `ab1234c@example.edu-*.pst`. None for a name without a
    date: such a file is a one-off, belongs to no series and is never
    rotated out."""
    base = _DUP_PREFIX.sub('', name)
    if base.lower().endswith('.zst'):
        base = base[:-4]
    m = pst._DATE_RE.search(base)
    if not m:
        return None
    return base[:m.start(1)] + '*' + base[m.end(1):]


def list_archive(directory):
    """Copies in a done/ (or the legacy archive/) folder, newest first:
    [{'name', 'path', 'size', 'date', 'sidecar', 'is_dir', 'series'}]."""
    out = []
    try:
        entries = list(os.scandir(directory))
    except (FileNotFoundError, NotADirectoryError):
        return out
    names = {e.name for e in entries}
    for e in entries:
        n = e.name
        if n.startswith('.') or n.lower().endswith(SIDECAR_SUFFIX):
            continue
        try:
            st = os.lstat(e.path)
        except OSError:
            continue
        if stat.S_ISREG(st.st_mode):
            is_dir, size = False, st.st_size
        elif stat.S_ISDIR(st.st_mode):
            is_dir, size = True, tree_signature(e.path)[0]
        else:
            continue
        plain = n[:-4] if n.lower().endswith('.zst') else n
        sc = plain + SIDECAR_SUFFIX
        out.append({'name': n, 'path': e.path, 'size': size, 'is_dir': is_dir,
                    'date': pst.file_date(_DUP_PREFIX.sub('', n), st.st_mtime),
                    'series': series_key(n),
                    'sidecar': os.path.join(directory, sc) if sc in names else None})
    out.sort(key=lambda r: (r['date'], r['name']), reverse=True)
    return out


def series_interval_seconds(copies, recent=5):
    """The cadence of a series, from its own copies: the median gap between
    the dates of its newest `recent` copies (a weekly export: 7 days). 0
    when there are fewer than two dated copies."""
    dates = sorted({c['date'] for c in copies if c.get('date')}, reverse=True)[:recent]
    gaps = sorted((a - b).days for a, b in zip(dates, dates[1:]))
    if not gaps:
        return 0
    return gaps[len(gaps) // 2] * 86400


def stale_after_seconds(schedule, factor=2, copies=()):
    """R4 threshold: the expected interval between successful imports of a
    series x factor, never under 48 hours. The expected interval is the
    larger of the import schedule's and the series' own cadence: a weekly
    export looked for daily is expected weekly, not daily (otherwise every
    weekly copy would arrive "stale" and nothing would ever rotate)."""
    interval = {'hourly': 3600, 'daily': 86400, 'weekly': 7 * 86400}.get(
        (schedule or {}).get('kind'), 7 * 86400)
    interval = max(interval, series_interval_seconds(copies))
    return max(factor * interval, DEFAULT_STALE_AFTER)


def retention_decide(entries, today, *, keep_recent=4, keep_monthly=3, min_keep=3,
                     max_delete=3, new_verified_copy=False, blocked='',
                     last_success=None, now=None, stale_after=DEFAULT_STALE_AFTER):
    """THE choke point: which copies of ONE series may be deleted now.

    Deletes nothing itself. Returns {'keep': {name: reason}, 'delete':
    [entry], 'refused': [(entry, rule, reason)], 'summary': str}.

    R1  count, never age: the candidates come from plan_rotation, which
        keeps the newest N existing copies and the newest copy of the N
        most recent earlier months that have one.
    R2  only after a successful, verified import of a new copy of this
        series in this same run, and only if nothing in the run failed or
        was partial (`blocked`).
    R3  never the newest copy; never fewer than `min_keep` copies left.
    R4  frozen while the last successful import BEFORE this run
        (`last_success`) is older than `stale_after`, or there never was
        one: a source that went quiet and then delivers one copy does not
        get its old copies pruned on the strength of that one copy. The
        refusal is recorded; pruning resumes with the next import that
        follows a recent success.
    R5  at most `max_delete` deletions per run and never more than half the
        set; a plan over budget deletes NOTHING (it is a config mistake or
        a backlog a person should look at).
    R6  the caller records every entry of 'delete' and 'refused'.
    """
    min_keep = max(1, int(min_keep))
    copies = [dict(e) for e in entries]
    keep, cand = pst.plan_rotation(copies, today, keep_recent, keep_monthly)
    res = {'keep': {r['name']: r['keep_reason'] for r in keep}, 'delete': [],
           'refused': [], 'summary': 'nothing to delete'}
    if not copies:
        return res
    newest = max(copies, key=lambda r: (r['date'], r['name']))
    if any(c['name'] == newest['name'] for c in cand):          # R3, belt and braces
        cand = [c for c in cand if c['name'] != newest['name']]
        res['keep'][newest['name']] = 'newest copy'
    if not cand:
        return res

    def refuse_all(rule, why):
        res['refused'] = [(c, rule, why) for c in cand]
        res['summary'] = f'{rule}: {why}'
        for c in cand:
            res['keep'][c['name']] = f'held ({rule})'
        return res

    if blocked:
        return refuse_all('R2', blocked)
    if not new_verified_copy:
        return refuse_all('R2', 'no successful, verified import of a new copy of '
                                'this series in this run')
    now = time.time() if now is None else now
    if last_success is None:
        return refuse_all('R4', 'no successful import of this series on record '
                                'before this run - pruning frozen until the next one')
    if now - last_success > stale_after:
        return refuse_all('R4', f'source is stale: the last successful import before '
                                f'this run was {(now - last_success) / 3600:.0f} h ago '
                                f'(limit {stale_after / 3600:.0f} h) - pruning frozen '
                                f'until the next import')
    cand.sort(key=lambda r: (r['date'], r['name']))             # oldest first
    allowed = max(0, len(copies) - min_keep)
    for c in cand[allowed:]:
        res['refused'].append((c, 'R3', f'would leave fewer than min_keep={min_keep} copies'))
        res['keep'][c['name']] = f'floor (min_keep={min_keep})'
    cand = cand[:allowed]
    budget = min(int(max_delete), len(copies) // 2)
    if len(cand) > budget:
        why = (f'{len(cand)} deletions planned but one run may delete at most '
               f'{budget} (max_delete_per_run={max_delete}, never more than half of '
               f'the {len(copies)} copies) - nothing deleted')
        for c in cand:
            res['refused'].append((c, 'R5', why))
            res['keep'][c['name']] = 'held (R5)'
        res['summary'] = f'R5: {why}'
        return res
    res['delete'] = cand
    if cand:
        res['summary'] = (f'{len(cand)} old cop{"y" if len(cand) == 1 else "ies"} '
                          f'beyond the newest {keep_recent} and {keep_monthly} monthly')
    elif res['refused']:
        res['summary'] = f'R3: {res["refused"][0][2]}'
    return res


def retention_preview(entries, today, cfg):
    """{name: reason} for the WebUI: what the next successful import of
    each series would keep or delete. Purely informative."""
    out, groups = {}, {}
    for e in entries:
        if e['series'] is None:
            out[e['name']] = 'kept: one-off file (no date in the name), never rotated'
        else:
            groups.setdefault(e['series'], []).append(e)
    for key, copies in groups.items():
        dec = retention_decide(
            copies, today, keep_recent=cfg['keep_recent'],
            keep_monthly=cfg['keep_monthly'], min_keep=cfg['min_keep'],
            max_delete=cfg['max_delete_per_run'], new_verified_copy=True,
            last_success=0, now=0)
        for name, why in dec['keep'].items():
            out[name] = f'kept: {why}'
        for e in dec['delete']:
            out[e['name']] = 'deleted after the next successful import of this series'
    return out


def _remove_copy(entry):
    if entry.get('is_dir'):
        shutil.rmtree(entry['path'])
    else:
        os.unlink(entry['path'])
    if entry.get('sidecar'):
        try:
            os.unlink(entry['sidecar'])
        except FileNotFoundError:
            pass


# ---------------------------------------------------------------------------
# The per-user pass (runs as root from cron, or as the service user)
# ---------------------------------------------------------------------------

def _unique_name(directory, name):
    """`name`, or `2~name`, `3~name`... when that is taken (also by its
    .zst form). Nothing in done/ or failed/ is ever overwritten."""
    cand, n = name, 1
    while any(os.path.lexists(os.path.join(directory, c)) for c in (cand, cand + '.zst')):
        n += 1
        cand = f'{n}~{name}'
    return cand


class UserImporter:
    """One user's import pass. The caller holds the per-user import lock.

    Hooks (all supplied by app.py, so the privilege model stays there):
      convert(path, typ, name, maildir, work_dir, cache_path)
                               -> stats dict with 'status' (runs as the owner)
      prepare(path)            give a file/dir we created or received to the
                               mailbox owner
      index(account)           update the search index for that account
      compress(path) -> path   archive compression, verified (None = keep as is)
      open_ledger() -> Ledger
    """

    COMPRESS_TYPES = ('pst', 'mbox')

    def __init__(self, data_dir, username, accounts, cfg, *, convert, prepare,
                 index, open_ledger, compress=None, log=print, now=time.time,
                 sleep=time.sleep, stable_wait=pst.STABLE_WAIT,
                 max_bytes=MAX_IMPORT_BYTES, owners=None):
        self.data_dir = str(data_dir)
        self.username = username
        self.accounts = [a['email'] if isinstance(a, dict) else a for a in accounts]
        self.cfg = cfg
        self.paths = pst.import_paths(data_dir, username, cfg.get('watch_dir', ''))
        self.convert, self.prepare, self.index = convert, prepare, index
        self.open_ledger = open_ledger
        self.compress, self.log, self.now, self.sleep = compress, log, now, sleep
        self.stable_wait, self.max_bytes = stable_wait, max_bytes
        self.ledger = None
        # uids that may own a directory of the import tree (the mailbox
        # owner and root); None = ownership is not checked (builtin auth).
        self.owners = set(owners) if owners is not None else None
        self.user_root = os.path.join(self.data_dir, username)
        self.refused = {}                  # account -> why its folder is not used
        self._pins = {}

    # -- the tree, never through a link ------------------------------------
    def pin(self, path):
        """The checked, open directory `path` (kept until close())."""
        pd = self._pins.get(path)
        if pd is None:
            pd = self._pins[path] = open_dir(self.user_root, path, self.owners)
        return pd

    def close(self):
        pins, self._pins = self._pins, {}
        for pd in pins.values():
            pd.close()

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass

    def check_user_tree(self):
        check_tree(self.user_root, [self.paths[k] for k in USER_TREE], self.owners)

    def check_account(self, account):
        """'' when the account's import folder may be used, else why not."""
        if account in self.refused:
            return self.refused[account]
        ap = self.account_paths(account)
        try:
            check_tree(self.user_root, [ap[k] for k in ACCOUNT_TREE], self.owners)
        except UnsafeImportTree as e:
            self.refused[account] = str(e)
        return self.refused.get(account, '')

    def account_paths(self, account):
        return pst.account_paths(self.data_dir, self.username, account)

    # -- directories -------------------------------------------------------
    def ensure_dirs(self):
        """Create what is missing. Every directory is checked first
        (check_tree): an unsafe user tree raises UnsafeImportTree, an unsafe
        account folder puts the account in self.refused; in either case
        nothing is created or chowned there."""
        p = self.paths
        os.makedirs(self.user_root, exist_ok=True)
        self.check_user_tree()
        dirs = [(p['root'], 0o750), (p['work'], 0o700), (p['state'], 0o700)]
        if _real_dir(p['watch']) or self.cfg.get('mappings') \
                or self.cfg.get('default_account'):
            dirs += [(p['watch'], 0o750), (p['incoming'], 0o750)]
        self._make(dirs)
        self.refused = {}
        for acct in self.accounts:
            if self.check_account(acct):
                continue
            ap = self.account_paths(acct)
            try:
                self._make([(ap[k], 0o750) for k in ACCOUNT_TREE])
            except UnsafeImportTree as e:
                self.refused[acct] = str(e)
        return self.refused

    def _make(self, dirs):
        for d, mode in dirs:
            open_dir(self.user_root, d, self.owners, create=True, mode=mode,
                     prepare=self.prepare).close()

    # -- scan --------------------------------------------------------------
    def scan_account(self, account):
        """What waits in one account's inbox/: scan_inbox's result plus
        'error' (why the folder is refused; then nothing was read)."""
        out = {'candidates': [], 'ignored': [], 'error': self.check_account(account)}
        if out['error']:
            return out
        inbox = self.account_paths(account)['inbox']
        try:
            out.update(scan_inbox(inbox, io_dir=self.pin(inbox).via))
        except FileNotFoundError:
            pass
        except UnsafeImportTree as e:
            self.refused[account] = out['error'] = str(e)
        return out

    def scan_watch(self):
        """The legacy watch folder; same result shape as scan_account."""
        out = {'candidates': [], 'ignored': [], 'error': ''}
        try:
            self.check_user_tree()
            out.update(pst.scan_watch(self.paths['watch'],
                                      io_dir=self.pin(self.paths['watch']).via))
        except FileNotFoundError:
            pass
        except UnsafeImportTree as e:
            out['error'] = str(e)
        return out

    def failed_entries(self, account):
        if self.check_account(account):
            return []
        try:
            return list_failed(self.pin(self.account_paths(account)['failed']).via)
        except OSError:
            return []

    def scan(self):
        """Everything waiting: each account's inbox/, then the legacy watch
        folder (PST only, account from the file name). A refused folder
        contributes nothing and is listed in `ignored`."""
        cands, ignored = [], []
        for acct in self.accounts:
            s = self.scan_account(acct)
            slug = pst.safe_account_name(acct)
            if s['error']:
                ignored.append((f'{slug}/', s['error']))
            for c in s['candidates']:
                c.update(account=acct, mapped_by='import folder')
            cands += s['candidates']
            ignored += [(f'{slug}/inbox/{n}', why) for n, why in s['ignored']]
        s = self.scan_watch()
        if s['error']:
            ignored.append(('watch/', s['error']))
        for c in s['candidates']:
            acct, how = pst.map_file_to_account(c['name'], self.cfg, self.accounts)
            if acct and self.check_account(acct):
                acct, how = None, f'the import folder of {acct} is refused'
            c.update(account=acct, mapped_by=how, kind='file', source='watch')
        cands += s['candidates']
        ignored += [(f'watch/{n}', why) for n, why in s['ignored']]
        return cands, ignored

    # -- leftovers of a killed pass or upload --------------------------------
    def clean_stale(self):
        """Remove what a pass or an upload that was killed (SIGKILL, reboot,
        service restart) left behind, so it cannot fill the data disk:

        * every .work/run-* directory. The caller holds the per-user import
          lock and only a pass makes them, so none is in use. They hold
          unpacked copies only; the source is still in inbox/ or failed/.
        * receive_upload's own temporary files in inbox/.incoming that
          nothing has written to for UPLOAD_STALE_SECONDS. Uploads do not
          take the import lock, so a younger file may be an upload in
          progress and is left alone; a producer's files are never touched.

        Works through the pinned directories; links are never followed."""
        removed = 0
        try:
            work = self.pin(self.paths['work'])
            with os.scandir(work.via) as it:
                names = [e.name for e in it if e.name.startswith('run-')]
        except OSError:
            work, names = None, []
        for n in names:
            p = work.join(n)
            try:
                if not stat.S_ISDIR(os.lstat(p).st_mode):
                    continue
                shutil.rmtree(p)
                removed += 1
                self.log(f'  removed leftover work directory .work/{n}')
            except OSError as e:
                self.log(f'  could not remove leftover .work/{n}: {e}')
        cutoff = time.time() - UPLOAD_STALE_SECONDS
        for acct in self.accounts:
            if self.check_account(acct):
                continue
            try:
                inc = self.pin(self.account_paths(acct)['incoming'])
                with os.scandir(inc.via) as it:
                    names = [e.name for e in it if UPLOAD_TMP_RE.search(e.name)]
            except OSError:
                continue
            for n in names:
                p = inc.join(n)
                try:
                    st = os.lstat(p)
                    if not stat.S_ISREG(st.st_mode) or st.st_mtime > cutoff:
                        continue
                    os.unlink(p)
                    removed += 1
                    self.log(f'  removed leftover upload file '
                             f'{pst.safe_account_name(acct)}/inbox/.incoming/{n}')
                except OSError as e:
                    self.log(f'  could not remove leftover upload file {n}: {e}')
        return removed

    def _refuse(self, account, why, trigger):
        """Record (once, until something else happens in the account) that
        an import folder was refused."""
        self.log(f'  REFUSED {account or "the import folder"}: {why}')
        last = self.ledger.last_row_of(account)
        if last and last['source'] == 'check' and last['error'] == why:
            return
        self.ledger.add(
            status='failed', original_name='(import folder)', account=account,
            source='check', trigger=trigger, error=why,
            queued_at=pst.now_str(self.now()), finished_at=pst.now_str(self.now()),
            finished_epoch=self.now())

    # -- the pass ----------------------------------------------------------
    def run(self, only=None, trigger='scheduled'):
        """Import every stable unit (or just `only` = (account or None,
        name)). Returns {'results': [...], 'unstable': [...], 'ignored':
        [...], 'rotation': [...], 'retry': bool} - retry is True when
        something should be tried again at the next scheduled run."""
        self.ledger = self.open_ledger()
        try:
            try:
                self.ensure_dirs()
            except UnsafeImportTree as e:
                self._refuse('', str(e), trigger)
                return {'results': [], 'unstable': [], 'rotation': [], 'retry': False,
                        'ignored': [('_import/', str(e))], 'refused': {'': str(e)},
                        'error': str(e)}
            n = self.ledger.mark_interrupted(self.now())
            if n:
                self.log(f'  {n} import(s) of an earlier run never finished; marked failed')
            self.clean_stale()
            cands, ignored = self.scan()
            for acct, why in self.refused.items():
                self._refuse(acct, why, trigger)
            if only is not None:
                acct, name = only
                cands = [c for c in cands if c['name'] == name
                         and (acct is None or c['account'] == acct)]
            stable, unstable = split_stable(cands, self.stable_wait, self.sleep)
            summary = {'results': [], 'unstable': [c['name'] for c in unstable],
                       'ignored': ignored, 'rotation': [], 'retry': bool(unstable),
                       'refused': dict(self.refused)}
            for c in unstable:
                self.log(f'  {c["name"]}: still changing, left for the next run')
            stable.sort(key=lambda c: (pst.file_date(c['name'], c['mtime']), c['name']))
            for c in stable:
                if c['account']:
                    c['row'] = self.ledger.add(
                        status='queued', original_name=c['name'], account=c['account'],
                        size_bytes=c['size'], source=c['source'], trigger=trigger,
                        queued_at=pst.now_str(self.now()))
            touched = {}
            for c in stable:
                res = self.process(c, trigger)
                summary['results'].append(res)
                if res.get('retry'):
                    summary['retry'] = True
                if c['account']:
                    touched.setdefault(c['account'], []).append((c, res))
            for acct, units in touched.items():
                summary['rotation'] += self.rotate(acct, units)
            return summary
        finally:
            self.close()
            self.ledger.close()
            self.ledger = None

    def _finish(self, c, **fields):
        fields.setdefault('finished_at', pst.now_str(self.now()))
        fields.setdefault('finished_epoch', self.now())
        self.ledger.update(c['row'], **fields)
        row = self.ledger.get(c['row'])
        row['retry'] = bool(c.get('retry'))
        return row

    def process(self, c, trigger):
        name, acct = c['name'], c['account']
        if not acct:                       # legacy watch file nobody claims
            last = self.ledger.last_row_for(name, 'watch')
            if not (last and last['status'] == 'skipped' and not last['account']):
                rid = self.ledger.add(
                    status='skipped', original_name=name, size_bytes=c['size'],
                    source='watch', trigger=trigger, error=c['mapped_by'],
                    finished_at=pst.now_str(self.now()), finished_epoch=self.now())
                last = self.ledger.get(rid)
            self.log(f'  {name}: skipped - {c["mapped_by"]}')
            last['retry'] = False
            return last
        detail = c['detail'] = {'mapped_by': c['mapped_by']}
        self.ledger.update(c['row'], status='running',
                           started_at=pst.now_str(self.now()), detail=detail)
        self.log(f'  {name} -> {acct} ({c["mapped_by"]})')
        ap = self.account_paths(acct)
        # Privileged work goes through the pinned inbox ('io'); the real
        # path is for the converter (the owner's own process) and messages.
        io = c.setdefault('io', c['path'])
        side_io = c.setdefault('sidecar_io', c['sidecar'])

        if c['size'] > self.max_bytes:
            return self._fail(c, f'{c["size"]} bytes, over the {self.max_bytes} '
                                 f'byte limit', move=True)
        if unit_signature(io) != c['sig']:
            return self._fail(c, 'changed while queued', move=False, retry=True)
        typ, how = detect_unit(io)
        if typ is None:
            return self._fail(c, f'unknown type: {how}', move=True)
        c['type'] = typ
        detail['detected_by'] = how
        prefix = prefix_str(typ) if typ in PREFIX else ''
        try:
            digest = tree_sha256(io) if c['kind'] == 'dir' else pst.sha256_file(io)
        except OSError as e:
            return self._fail(c, f'cannot read: {e.strerror or e}', move=True)
        c['sha256'] = digest
        self.ledger.update(c['row'], sha256=digest, detected_type=typ,
                           target_prefix=prefix, detail=detail)
        if c['sidecar']:
            try:
                with os.fdopen(pst._open_nofollow(side_io), 'rb') as f:
                    want = pst.parse_sidecar(f.read(4096).decode('utf-8', 'replace'), name)
            except (OSError, ValueError) as e:
                return self._fail(c, f'sha256 sidecar unusable: {e}', move=True)
            if want != digest:
                return self._fail(c, f'sha256 mismatch: sidecar {want[:16]}..., '
                                     f'file {digest[:16]}...', move=True)
            detail['checksum'] = f'sha256 verified ({digest[:16]}...)'
        else:
            detail['checksum'] = 'no sidecar'

        before = self.ledger.find_done(digest, acct)
        if before:
            why = (f'identical to {before["original_name"]!r}, already imported into '
                   f'this account on {before["finished_at"]} (ledger #{before["id"]}); '
                   f'not imported again')
            try:
                detail['moved_to'] = self._move_out(c, ap['failed'], why + '. This copy '
                                                    'can be removed.')
            except OSError as e:
                why += f' (and could not move it out of the inbox: {e})'
            self.log(f'  {name}: skipped - {why}')
            return self._finish(c, status='skipped', error=why, detail=detail)

        try:
            free = shutil.disk_usage(self.paths['root']).free
        except OSError:
            free = None
        need = c['size'] * (2 if typ == 'pst' or typ in CONTAINER_TYPES else 1) + FREE_RESERVE
        if free is not None and free < need:
            return self._fail(c, f'not enough free space ({free} bytes) to import '
                                 f'{c["size"]} bytes', move=False, retry=True)

        self.prepare(io)
        if c['sidecar']:
            self.prepare(side_io)
        work = os.path.join(self.paths['work'], f'run-{int(self.now())}-{os.getpid()}-{c["row"]}')
        maildir = os.path.join(self.data_dir, self.username, pst.safe_account_name(acct))
        cache = os.path.join(self.paths['state'], f'{pst.safe_account_name(acct)}.ids.json')
        try:
            stats = self.convert(c['path'], typ, name, maildir, work, cache) or {}
        except Exception as e:
            stats = {'status': 'failed', 'error': f'{type(e).__name__}: {e}'}
        if stats.get('status') not in ('ok', 'partial'):
            return self._fail(c, stats.get('error') or 'conversion failed', move=True)
        for k in ('folders', 'types', 'skipped_links', 'ignored', 'refused_members'):
            if stats.get(k):
                detail[k] = stats[k]
        detail['errors'] = (stats.get('errors') or [])[:20]
        if stats.get('prefixes'):
            prefix = ', '.join(sorted(stats['prefixes']))
        try:
            self.index(acct)
        except Exception as e:
            detail['index_error'] = f'{type(e).__name__}: {e}'
        try:
            detail['archived_as'], detail['archive_verified'] = self._archive(c, ap['done'])
        except OSError as e:
            detail['archive_error'] = f'could not archive: {e}'
        status = 'done' if stats['status'] == 'ok' else 'partial'
        row = self._finish(
            c, status=status, target_prefix=prefix, imported=stats.get('added', 0),
            duplicates=stats.get('duplicates', 0), failed=stats.get('failed', 0),
            too_large=stats.get('too_large', 0), detail=detail)
        self.log(f'  {name}: {status} ({typ}) - imported {row["imported"]}, '
                 f'duplicates {row["duplicates"]}, failed {row["failed"]}')
        return row

    def _fail(self, c, error, move, retry=False):
        detail = c.get('detail') or {}
        if move:
            try:
                detail['moved_to'] = self._move_out(
                    c, self.account_paths(c['account'])['failed'], error)
            except OSError as e:
                error += f' (and could not move it to failed/: {e})'
        else:
            detail['left_in'] = c['source']
        c['retry'] = retry
        self.log(f'  {c["name"]}: FAILED - {error}')
        return self._finish(c, status='failed', error=error, detail=detail)

    def _move_out(self, c, directory, reason):
        """Move a unit (and its sidecar) to failed/ and say why next to it."""
        pd = self.pin(directory)            # OSError unless a real directory
        io = c.get('io') or c['path']
        side_io = c.get('sidecar_io') or c['sidecar']
        final = _unique_name(pd.via, c['name'])
        dest = pd.join(final)
        os.rename(io, dest)
        if side_io and os.path.lexists(side_io):
            os.rename(side_io, dest + SIDECAR_SUFFIX)
        try:
            rpath = dest + REASON_SUFFIX
            fd = os.open(rpath, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | pst.O_SAFE, 0o640)
            with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as f:
                f.write(f'{pst.now_str(self.now())}  {c["name"]}\n'
                        f'not imported into {c["account"]}: {reason}\n'
                        + (f'detected type: {c["type"]}\n' if c.get('type') else '')
                        + (f'sha256: {c["sha256"]}\n' if c.get('sha256') else ''))
            self.prepare(rpath)
        except OSError:
            pass
        return os.path.relpath(os.path.join(directory, final), self.paths['root'])

    def _archive(self, c, done_dir):
        """Move an imported unit to done/. Returns (relative path, verified):
        verified means the copy is there and is what was imported."""
        pd = self.pin(done_dir)             # OSError unless a real directory
        io = c.get('io') or c['path']
        side_io = c.get('sidecar_io') or c['sidecar']
        final = _unique_name(pd.via, c['name'])
        dest = pd.join(final)
        os.rename(io, dest)
        side = dest + SIDECAR_SUFFIX
        if side_io and os.path.lexists(side_io):
            os.replace(side_io, side)
        elif c['kind'] == 'file':
            fd = os.open(side, os.O_WRONLY | os.O_CREAT | os.O_EXCL | pst.O_SAFE, 0o640)
            with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as f:
                f.write(f'{c["sha256"]}  {final}\n')
            self.prepare(side)

        def regular(path):
            try:
                st = os.lstat(path)
            except OSError:
                return None
            return st if stat.S_ISREG(st.st_mode) else None

        st = regular(dest)
        verified = unit_signature(dest) is not None and (
            c['kind'] == 'dir' or (st is not None and st.st_size == c['size']))
        if self.compress and c['kind'] == 'file' and c.get('type') in self.COMPRESS_TYPES:
            try:
                # The compressor runs as the owner on the real path; what it
                # left is judged through the pinned directory.
                z = self.compress(os.path.join(done_dir, final))
                zst = regular(pd.join(os.path.basename(z))) if z else None
                if zst is not None and zst.st_size > 0:
                    final = os.path.basename(z)
            except Exception as e:
                self.log(f'  compression failed, archived uncompressed: {e}')
                verified = verified and regular(dest) is not None
        return os.path.relpath(os.path.join(done_dir, final), self.paths['root']), bool(verified)

    # -- retention ---------------------------------------------------------
    def archive_entries(self, account):
        """The account's copies: done/, plus copies in the legacy archive/
        whose file name maps to this account."""
        entries = []
        if not self.check_account(account):
            entries = self._list_archive(self.account_paths(account)['done'])
        for e in self._list_archive(self.paths['archive']):
            plain = e['name'][:-4] if e['name'].lower().endswith('.zst') else e['name']
            if pst.map_file_to_account(plain, self.cfg, self.accounts)[0] == account:
                entries.append(e)
        entries.sort(key=lambda r: (r['date'], r['name']), reverse=True)
        return entries

    def _list_archive(self, directory):
        """list_archive through the pinned directory: the entries' paths
        (which rotation deletes) cannot be redirected by a link."""
        try:
            return list_archive(self.pin(directory).via)
        except OSError:                     # missing, or refused: no copies
            return []

    def rotate(self, account, units):
        """Retention for the series that got a file in this pass. Every
        deletion and every refusal goes into the ledger."""
        bad = [r for _c, r in units if r['status'] in ('failed', 'partial')]
        blocked = ''
        if bad:
            blocked = (f'{len(bad)} import(s) in this run failed or were partial '
                       f'({", ".join(r["original_name"] for r in bad[:3])}) - rotation stopped')
        series = {}
        for c, r in units:
            key = series_key(c['name'])
            if key:
                series.setdefault(key, []).append(r)
        if not series:
            return []
        entries = self.archive_entries(account)
        today = datetime.date.fromtimestamp(self.now())
        # R4 looks at the last success BEFORE this run: every row of this
        # run was queued before anything was imported, so they are all
        # at or after the first one.
        first_row = min(c['row'] for c, _r in units)
        out = []
        for key, rows in sorted(series.items()):
            copies = [e for e in entries if e['series'] == key]
            new = [r for r in rows if r['status'] == 'done'
                   and r['detail'].get('archive_verified')]
            dec = retention_decide(
                copies, today, keep_recent=self.cfg.get('keep_recent', 4),
                keep_monthly=self.cfg.get('keep_monthly', 3),
                min_keep=self.cfg.get('min_keep', 3),
                max_delete=self.cfg.get('max_delete_per_run', 3),
                new_verified_copy=bool(new), blocked=blocked,
                last_success=self.ledger.last_done_epoch(account, key, first_row),
                now=self.now(),
                stale_after=stale_after_seconds(self.cfg.get('schedule'), copies=copies))
            ledger_id = (new or rows)[-1]['id']
            deleted, refused = [], []
            for e in dec['delete']:
                try:
                    _remove_copy(e)
                except OSError as err:
                    refused.append(e['name'])
                    self._event(account, key, 'refused', 'error', e,
                                f'could not delete: {err}', ledger_id)
                    continue
                deleted.append(e['name'])
                self._event(account, key, 'deleted', 'R1', e, dec['summary'], ledger_id)
            for e, rule, why in dec['refused']:
                refused.append(e['name'])
                self._event(account, key, 'refused', rule, e, why, ledger_id)
            if deleted:
                self.log(f'  rotation {key}: deleted {", ".join(deleted)}')
            if refused:
                self.log(f'  rotation {key}: {len(refused)} deletion(s) refused - '
                         f'{dec["summary"]}')
            out.append({'account': account, 'series': key, 'deleted': deleted,
                        'refused': refused, 'summary': dec['summary'],
                        'kept': len(copies) - len(deleted)})
        return out

    def _event(self, account, series, action, rule, entry, reason, ledger_id):
        self.ledger.add_event(
            now=self.now(), account=account, series=series, action=action, rule=rule,
            name=entry['name'], size_bytes=entry.get('size', 0),
            copy_date=entry['date'].isoformat(), reason=reason, ledger_id=ledger_id)


def list_failed(failed_dir):
    """Files in failed/ with the reason written next to each."""
    out = []
    try:
        entries = sorted(os.scandir(failed_dir), key=lambda e: e.name)
    except (FileNotFoundError, NotADirectoryError):
        return out
    for e in entries:
        low = e.name.lower()
        if e.name.startswith('.') or low.endswith(SIDECAR_SUFFIX) or low.endswith(REASON_SUFFIX):
            continue
        if e.is_symlink():
            continue
        try:
            is_dir = e.is_dir(follow_symlinks=False)
            size = tree_signature(e.path)[0] if is_dir else os.lstat(e.path).st_size
        except OSError:
            continue
        reason = ''
        try:
            with os.fdopen(pst._open_nofollow(e.path + REASON_SUFFIX), 'rb') as f:
                reason = f.read(2000).decode('utf-8', 'replace').strip()
        except OSError:
            pass
        out.append({'name': e.name, 'size': size, 'is_dir': is_dir, 'reason': reason})
    return out


def retry_failed(acct_paths, name, owners=None):
    """Move a unit (and its sidecar) from failed/ back into the account's
    inbox/. Returns the new path. Refused (UnsafeImportTree) when a folder
    of the account is a link or not the owner's; nothing already in the
    inbox is replaced."""
    low = name.lower()
    if ('/' in name or name.startswith('.') or not name or low.endswith(SIDECAR_SUFFIX)
            or low.endswith(REASON_SUFFIX)
            or compat.safe_filename_component(name) != name):   # Windows: \ : NUL ...
        raise ValueError('not a failed import file name')
    check_account_tree(acct_paths, owners)
    root = account_root(acct_paths)
    with open_dir(root, acct_paths['failed'], owners) as failed, \
            open_dir(root, acct_paths['inbox'], owners) as inbox:
        src = failed.join(name)
        st = os.lstat(src)
        if not (stat.S_ISREG(st.st_mode) or stat.S_ISDIR(st.st_mode)):
            raise ValueError('not a regular file or directory')
        back = _DUP_PREFIX.sub('', name)
        dest = inbox.join(back)
        if os.path.lexists(dest):
            raise FileExistsError(f'{back} is already in the inbox')
        side = src + SIDECAR_SUFFIX
        moved_side = False
        try:
            if os.path.lexists(side):
                move_noreplace(side, dest + SIDECAR_SUFFIX)
                moved_side = True
            move_noreplace(src, dest)
        except BaseException as e:
            if moved_side:                  # put our sidecar back where it was
                os.rename(dest + SIDECAR_SUFFIX, side)
            if isinstance(e, FileExistsError):
                raise FileExistsError(f'{back} is already in the inbox') from None
            raise
        try:
            os.unlink(src + REASON_SUFFIX)
        except OSError:
            pass
    return os.path.join(acct_paths['inbox'], back)


def receive_upload(stream, length, acct_paths, name, prepare=lambda p: None,
                   max_bytes=MAX_IMPORT_BYTES, chunk=4 * 1024 * 1024, *,
                   owners=None, reserve=FREE_RESERVE,
                   space_check_bytes=SPACE_CHECK_BYTES):
    """Store an uploaded file following the producer contract: write into
    the account's inbox/.incoming, put the sha256 sidecar in place, then
    move the file into inbox/. Returns (path, sha256).

    Refused before anything is written: an import folder that is a link or
    not the owner's (UnsafeImportTree), and an upload the data disk cannot
    take with `reserve` bytes to spare (InsufficientSpace, also raised
    mid-stream when free space falls under the reserve). A file or sidecar
    that appeared under the same name during the upload is never replaced
    (FileExistsError)."""
    low = (name or '').lower()
    if (not UPLOAD_NAME_RE.match(name or '') or low.endswith('.tmp')
            or low.endswith(SIDECAR_SUFFIX) or low.endswith(REASON_SUFFIX)
            or name != name.strip()):
        raise ValueError('file name may hold letters, digits, spaces and '
                         '@ . _ + - ( ) [ ] only, and must not end in .tmp or .sha256')
    if compat.safe_filename_component(name) != name:
        raise ValueError('this file name cannot be used on this computer '
                         '(a reserved name, or it ends in a dot or a space)')
    if length is None:
        raise ValueError('upload needs a Content-Length')
    if length > max_bytes:
        raise ValueError(f'file is larger than the {max_bytes} byte limit')
    check_account_tree(acct_paths, owners)
    root = account_root(acct_paths)
    with open_dir(root, acct_paths['inbox'], owners) as inbox, \
            open_dir(root, acct_paths['incoming'], owners, create=True,
                     prepare=prepare) as incoming:
        dest = inbox.join(name)
        if os.path.lexists(dest):
            raise FileExistsError(f'{name} is already waiting in the inbox')
        free = shutil.disk_usage(inbox.via).free
        if free < length + reserve:
            raise InsufficientSpace(
                f'not enough free space on the mail disk for this upload: '
                f'{length} bytes to store, {free} bytes free, and {reserve} bytes '
                f'are kept in reserve')
        part = incoming.join(f'{name}.upload-{os.getpid()}.tmp')
        side_tmp = part + '.sha256.tmp'
        h = hashlib.sha256()
        got = 0
        fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_EXCL | pst.O_SAFE, 0o640)
        try:
            with os.fdopen(fd, 'wb') as f:
                while got < length:
                    b = stream.read(min(chunk, length - got))
                    if not b:
                        break
                    f.write(b)
                    h.update(b)
                    if (got + len(b)) // space_check_bytes != got // space_check_bytes:
                        free = shutil.disk_usage(inbox.via).free
                        if free < reserve:
                            raise InsufficientSpace(
                                f'the mail disk ran low during the upload ({free} '
                                f'bytes free, {reserve} are kept in reserve); the '
                                f'upload was stopped and removed')
                    got += len(b)
            if got != length:
                raise ValueError(f'upload ended after {got} of {length} bytes')
            digest = h.hexdigest()
            sfd = os.open(side_tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | pst.O_SAFE,
                          0o640)
            with os.fdopen(sfd, 'w', encoding='utf-8', newline='\n') as f:
                f.write(f'{digest}  {name}\n')
            prepare(part)
            prepare(side_tmp)
            # Never replace: a producer may have delivered the same name
            # while this upload was running.
            try:
                move_noreplace(side_tmp, dest + SIDECAR_SUFFIX)
            except FileExistsError:
                raise FileExistsError(f'{name} is already arriving in the inbox') from None
            try:
                move_noreplace(part, dest)
            except BaseException as e:
                os.unlink(dest + SIDECAR_SUFFIX)       # ours: just placed, no-replace
                if isinstance(e, FileExistsError):
                    raise FileExistsError(
                        f'{name} is already waiting in the inbox') from None
                raise
        except BaseException:
            for p in (part, side_tmp):
                try:
                    os.unlink(p)
                except OSError:
                    pass
            raise
    return os.path.join(acct_paths['inbox'], name), digest


# ---------------------------------------------------------------------------
# CLI (the converter child)
# ---------------------------------------------------------------------------

def _cli(argv):
    import argparse
    ap = argparse.ArgumentParser(prog='mail_import.py')
    sub = ap.add_subparsers(dest='cmd', required=True)
    c = sub.add_parser('convert', help='import one file or directory into a '
                                       'Maildir (run as the mailbox owner)')
    c.add_argument('--path', required=True)
    c.add_argument('--type', required=True)
    c.add_argument('--name')
    c.add_argument('--maildir', required=True)
    c.add_argument('--work', required=True)
    c.add_argument('--cache')
    c.add_argument('--readpst', default=pst.READPST)
    c.add_argument('--msgconvert', default=MSGCONVERT)
    c.add_argument('--timeout', type=int, default=pst.READPST_TIMEOUT)
    c.add_argument('--max-message-bytes', type=int, default=pst.MAX_MESSAGE_BYTES)
    d = sub.add_parser('detect', help='print the detected type of a file or directory')
    d.add_argument('path')
    a = ap.parse_args(argv)
    if a.cmd == 'detect':
        typ, how = detect_unit(a.path)
        sys.stdout.write(json.dumps({'type': typ, 'how': how}) + '\n')
        return 0 if typ else 1
    os.umask(0o077)
    stats = convert_in_process(a.path, a.type, a.name, a.maildir, a.work, a.cache,
                               readpst=a.readpst, msgconvert=a.msgconvert,
                               timeout=a.timeout,
                               max_message_bytes=a.max_message_bytes)
    sys.stdout.write(json.dumps(stats) + '\n')
    return 0 if stats['status'] != 'failed' else 2


if __name__ == '__main__':
    sys.exit(_cli(sys.argv[1:]))
