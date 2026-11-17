"""
Platform layer for DEC-LLC Mail-Archiver.

One code base runs on Linux (the packaged server) and on Windows (the
single-user EXE).  Everything that only exists on POSIX - fcntl locks,
pwd/grp lookups, chown, running a child as another user, resource limits -
is reached through this module, so the rest of the code never imports
fcntl, pwd, grp or resource directly.

On Linux every function does exactly what the direct call did.  On Windows
each one has the nearest meaningful behaviour: file locks use msvcrt,
ownership changes are no-ops (the EXE runs as, and stores data for, the
logged-in Windows user), and account lookups describe that one user.

Rules for callers:
- import this module, never fcntl / pwd / grp / resource;
- do not branch on os.name elsewhere - ask this module (IS_WINDOWS, or
  better, a capability such as have_tool()).
"""

from __future__ import annotations

import getpass
import os
import shutil
import time
from collections import namedtuple
from typing import Optional

IS_WINDOWS = os.name == "nt"

# The lock poll must really wait even where a caller has replaced time.sleep.
_real_sleep = time.sleep

if IS_WINDOWS:  # pragma: no cover - exercised on Windows and by compat tests
    fcntl = None
    pwd = None
    grp = None
    resource = None
    try:
        import msvcrt
    except ImportError:  # compat tests fake os.name on Linux
        msvcrt = None
else:
    import fcntl
    import grp
    import pwd
    try:
        import resource
    except ImportError:  # pragma: no cover
        resource = None
    msvcrt = None


# --------------------------------------------------------------------------
# File locks
# --------------------------------------------------------------------------

class LockUnavailable(OSError):
    """A non-blocking lock could not be taken because someone else holds it."""


def lock_file(fileobj, exclusive: bool = True, blocking: bool = True) -> None:
    """Lock an open file (whole-file advisory lock).

    Raises LockUnavailable when blocking is False and the lock is held by
    another process.  Shared locks become exclusive on Windows.
    """
    if not IS_WINDOWS:
        flags = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        if not blocking:
            flags |= fcntl.LOCK_NB
        try:
            fcntl.flock(fileobj.fileno(), flags)
        except BlockingIOError as exc:
            raise LockUnavailable(str(exc)) from exc
        return
    if msvcrt is None:
        return
    # msvcrt's own blocking mode (LK_LOCK) retries once a second and gives
    # up after ten tries.  flock() waits for as long as it takes and wakes
    # at once, so a blocking lock here polls the non-blocking mode instead.
    fd = fileobj.fileno()
    while True:
        pos = os.lseek(fd, 0, os.SEEK_CUR)
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                return
            except OSError as exc:
                if not blocking:
                    raise LockUnavailable(str(exc)) from exc
        finally:
            os.lseek(fd, pos, os.SEEK_SET)
        _real_sleep(0.02)


def unlock_file(fileobj) -> None:
    """Release a lock taken with lock_file()."""
    if not IS_WINDOWS:
        fcntl.flock(fileobj.fileno(), fcntl.LOCK_UN)
        return
    if msvcrt is None:
        return
    fd = fileobj.fileno()
    pos = os.lseek(fd, 0, os.SEEK_CUR)
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
    finally:
        os.lseek(fd, pos, os.SEEK_SET)


# --------------------------------------------------------------------------
# Accounts
# --------------------------------------------------------------------------

UserInfo = namedtuple("UserInfo", "name uid gid home shell")
GroupInfo = namedtuple("GroupInfo", "name gid members")


def current_username() -> str:
    try:
        return getpass.getuser()
    except Exception:
        return os.environ.get("USERNAME") or os.environ.get("USER") or "user"


def user_info(name_or_uid) -> Optional[UserInfo]:
    """Look up an OS account by name or uid.  None when it does not exist.

    On Windows only the logged-in user exists as far as the application is
    concerned; it is reported with uid/gid -1 (meaning "do not chown").
    """
    if IS_WINDOWS:
        me = current_username()
        if isinstance(name_or_uid, int) or str(name_or_uid).lower() == me.lower():
            return UserInfo(me, -1, -1, os.path.expanduser("~"), "")
        return None
    try:
        if isinstance(name_or_uid, int):
            p = pwd.getpwuid(name_or_uid)
        else:
            p = pwd.getpwnam(str(name_or_uid))
    except (KeyError, ValueError, OverflowError):
        return None
    return UserInfo(p.pw_name, p.pw_uid, p.pw_gid, p.pw_dir, p.pw_shell)


def all_users() -> list:
    """Every OS account (Linux) or just the logged-in user (Windows)."""
    if IS_WINDOWS:
        info = user_info(current_username())
        return [info] if info else []
    return [UserInfo(p.pw_name, p.pw_uid, p.pw_gid, p.pw_dir, p.pw_shell)
            for p in pwd.getpwall()]


def group_info(name_or_gid) -> Optional[GroupInfo]:
    """Look up an OS group by name or gid.  Always None on Windows."""
    if IS_WINDOWS:
        return None
    try:
        if isinstance(name_or_gid, int):
            g = grp.getgrgid(name_or_gid)
        else:
            g = grp.getgrnam(str(name_or_gid))
    except (KeyError, ValueError, OverflowError):
        return None
    return GroupInfo(g.gr_name, g.gr_gid, list(g.gr_mem))


def user_groups(username: str) -> list:
    """Names of the groups an account belongs to (empty on Windows)."""
    if IS_WINDOWS:
        return []
    names = []
    info = user_info(username)
    for g in grp.getgrall():
        if username in g.gr_mem or (info and g.gr_gid == info.gid):
            names.append(g.gr_name)
    return names


def is_root() -> bool:
    """True when running with full privileges (always False on Windows)."""
    if IS_WINDOWS:
        return False
    return os.geteuid() == 0


# --------------------------------------------------------------------------
# Ownership and child processes
# --------------------------------------------------------------------------

def chown(path, uid: int, gid: int) -> None:
    """os.chown on Linux; nothing on Windows or when uid/gid are -1/None."""
    if IS_WINDOWS or uid is None or gid is None or (uid == -1 and gid == -1):
        return
    os.chown(path, uid, gid)


def lchown(path, uid: int, gid: int) -> None:
    if IS_WINDOWS or uid is None or gid is None or (uid == -1 and gid == -1):
        return
    os.lchown(path, uid, gid)


def popen_user_kwargs(username: Optional[str]) -> dict:
    """Keyword arguments that make subprocess run a child as `username`.

    Linux, running as root: user / group / extra_groups for that account.
    Otherwise (not root, or Windows): {} - the child runs as the caller.
    """
    if IS_WINDOWS or not username or not is_root():
        return {}
    info = user_info(username)
    if info is None:
        return {}
    return {"user": info.uid, "group": info.gid, "extra_groups": []}


def set_resource_limit(name: str, soft: int, hard: Optional[int] = None) -> bool:
    """Set an rlimit by name (e.g. "RLIMIT_AS").  False when unsupported."""
    if IS_WINDOWS or resource is None:
        return False
    limit = getattr(resource, name, None)
    if limit is None:
        return False
    try:
        resource.setrlimit(limit, (soft, soft if hard is None else hard))
    except (ValueError, OSError):
        return False
    return True


# --------------------------------------------------------------------------
# Capabilities
# --------------------------------------------------------------------------

def have_tool(name: str) -> Optional[str]:
    """Full path of an external tool on PATH, or None."""
    return shutil.which(name)


def supports_system_accounts() -> bool:
    """PAM logins, per-user OS accounts, systemd units: Linux only."""
    return not IS_WINDOWS


# ==========================================================================
# Importer / initial-sync additions (mail_import, pst_import, bulk_sync*)
# ==========================================================================

import signal as _signal            # noqa: E402
import stat as _stat                # noqa: E402
import subprocess as _subprocess    # noqa: E402

# os.open flags that do not exist everywhere.  Taken from the real os module
# (not from IS_WINDOWS), so each is simply 0 where the platform lacks it.
# O_BINARY matters on Windows: without it the C runtime translates CR LF and
# stops at Ctrl-Z, even under os.fdopen(fd, 'rb').
O_BINARY = getattr(os, "O_BINARY", 0)
O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
O_NONBLOCK = getattr(os, "O_NONBLOCK", 0)
O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
O_CLOEXEC = getattr(os, "O_CLOEXEC", getattr(os, "O_NOINHERIT", 0))


def supports_dir_fd() -> bool:
    """A directory can be opened and its entries reached through the open
    descriptor (O_DIRECTORY, dir_fd=, /proc/self/fd).  False on Windows,
    where callers check the path components instead."""
    return not IS_WINDOWS


def supports_ownership() -> bool:
    """st_uid and the permission bits of a file mean something (they do
    not on Windows: skip owner / mode checks rather than fail them)."""
    return not IS_WINDOWS


_REPARSE_MOUNT_POINT = 0xA0000003


def is_link(path) -> bool:
    """True for a symbolic link and, on Windows, a directory junction.
    False for anything else, including a path that does not exist."""
    try:
        st = os.lstat(path)
    except OSError:
        return False
    if _stat.S_ISLNK(st.st_mode):
        return True
    return getattr(st, "st_reparse_tag", 0) == _REPARSE_MOUNT_POINT


# -- Maildir file names and path components ----------------------------------
# maildir_info_separator(), maildir_info(), split_maildir_name() and
# safe_path_component() are defined once, further down ("Names on disk").

def maildir_flags(filename: str) -> Optional[str]:
    """The flag letters of a Maildir file name, or None when the name has
    no info part at all (split_maildir_name() cannot tell "no info" from
    "no flags")."""
    for sep in _MAILDIR_INFO_SEPARATORS:
        marker = sep + "2,"
        if marker in filename:
            return filename.split(marker, 1)[1]
    return None


def maildir_uniq(filename: str) -> str:
    """The unique part of a Maildir file name: what is left of this
    platform's info separator."""
    return filename.split(maildir_info_separator(), 1)[0]


def safe_filename_component(name: str) -> str:
    """safe_path_component() for a name that comes out of a mail source
    (an archive member, a PST folder): on Windows a Maildir ":2," first
    becomes "!2,", so a message file keeps its flags.  Linux: unchanged.
    `safe_filename_component(n) == n` asks "is this name usable as is"."""
    if not IS_WINDOWS:
        return name
    return safe_path_component(name.replace(":2,", maildir_info("")))


# -- Child processes ---------------------------------------------------------
# detached_popen_kwargs(), pid_alive() and terminate_process_group() are
# defined once, further down.

_WIN_ENV_KEEP = frozenset((
    "PATH", "PATHEXT", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC",
    "TEMP", "TMP", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "PROGRAMDATA",
    "HOMEDRIVE", "HOMEPATH", "USERNAME", "PROCESSOR_ARCHITECTURE",
    "NUMBER_OF_PROCESSORS"))


def child_env(base: dict, **extra) -> dict:
    """Environment for an external tool (readpst, msgconvert, zstd).

    Linux: exactly `base` plus `extra` (a fixed, minimal environment).
    Windows: `base` without its POSIX PATH, plus the handful of variables a
    Windows program cannot start without (PATH, SystemRoot, TEMP ...) from
    our own environment, plus `extra`."""
    env = dict(base)
    if IS_WINDOWS:
        env.pop("PATH", None)
        for key, value in os.environ.items():
            if key.upper() in _WIN_ENV_KEEP:
                env.setdefault(key, value)
    env.update(extra)
    return env


def kill_process_tree(pid: int) -> None:
    """Kill `pid` and what it started, at once (terminate_process_group()
    is the polite SIGTERM).  Linux: SIGKILL to its process group (the child
    was started with detached_popen_kwargs()).  Windows: taskkill /T /F.
    Never raises for a process that is already gone."""
    if not IS_WINDOWS:
        try:
            os.killpg(os.getpgid(pid), _signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass
        return
    try:
        taskkill = have_tool("taskkill")
        if taskkill:
            kw = {}
            no_window = getattr(_subprocess, "CREATE_NO_WINDOW", 0)
            if no_window:
                kw["creationflags"] = no_window
            _subprocess.run([taskkill, "/F", "/T", "/PID", str(int(pid))],
                            stdin=_subprocess.DEVNULL, stdout=_subprocess.DEVNULL,
                            stderr=_subprocess.DEVNULL, timeout=30, **kw)
        else:
            os.kill(int(pid), _signal.SIGTERM)  # compat tests fake Windows on Linux
    except (OSError, ValueError, _subprocess.SubprocessError):
        pass


# --------------------------------------------------------------------------
# Server core additions (app.py, admin.py, sync_lifecycle.py, imap_sync.py,
# oauth2_microsoft.py).  Nothing above this line was changed.
# --------------------------------------------------------------------------

class _FdFile:
    """Just enough of a file object for lock_file()/unlock_file()."""

    __slots__ = ("_fd",)

    def __init__(self, fd: int):
        self._fd = fd

    def fileno(self) -> int:
        return self._fd


def lock_fd(fd: int, exclusive: bool = True, blocking: bool = True) -> None:
    """lock_file() for a raw descriptor from os.open()."""
    lock_file(_FdFile(fd), exclusive=exclusive, blocking=blocking)


def unlock_fd(fd: int) -> None:
    """unlock_file() for a raw descriptor from os.open()."""
    unlock_file(_FdFile(fd))


def current_uid() -> int:
    """Effective uid of this process; -1 on Windows (no uids)."""
    if IS_WINDOWS:
        return -1
    return os.geteuid()


def supplementary_gids(username: str, primary_gid: Optional[int] = None) -> list:
    """gids for subprocess `extra_groups=`: the primary gid (when given)
    followed by every group that lists the account as a member.  [] on
    Windows."""
    if IS_WINDOWS:
        return []
    gids = []
    if primary_gid is not None:
        gids.append(primary_gid)
    try:
        for g in grp.getgrall():
            if username in g.gr_mem and g.gr_gid not in gids:
                gids.append(g.gr_gid)
    except Exception:
        pass
    return gids


def chown_tree_nofollow(path, uid: int, gid: int) -> None:
    """Give a directory and everything under it to uid:gid without ever
    following a link (lchown on the top, then a walk by directory fd).
    Nothing on Windows or when uid/gid are -1/None."""
    if IS_WINDOWS or uid is None or gid is None or (uid == -1 and gid == -1):
        return
    os.chown(path, uid, gid, follow_symlinks=False)
    for _root, dirs, files, dfd in os.fwalk(path, follow_symlinks=False):
        for name in dirs + files:
            try:
                os.chown(name, uid, gid, dir_fd=dfd, follow_symlinks=False)
            except OSError:
                pass


def _posix_pid_alive(pid: int) -> bool:
    # /proc is ownership-independent; os.kill(pid, 0) raises EPERM for a
    # process of another user, which still proves that it exists.
    if os.path.isdir("/proc"):
        return os.path.exists(f"/proc/{pid}")
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except (ProcessLookupError, OSError):
        return False


def pid_alive(pid) -> bool:
    """True iff `pid` is a live process.

    Never uses os.kill() on Windows: there os.kill(pid, 0) TERMINATES the
    process (TerminateProcess with exit code 0).
    """
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if not IS_WINDOWS:
        return _posix_pid_alive(pid)
    try:
        import ctypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    except (ImportError, AttributeError, OSError):
        return _posix_pid_alive(pid)    # compat tests fake Windows on Linux
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    ERROR_ACCESS_DENIED = 5
    STILL_ACTIVE = 259
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.OpenProcess.argtypes = (ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong)
    kernel32.GetExitCodeProcess.argtypes = (ctypes.c_void_p,
                                            ctypes.POINTER(ctypes.c_ulong))
    kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid)
    if not handle:
        # Access denied: it exists, it is just not ours to open.
        return ctypes.get_last_error() == ERROR_ACCESS_DENIED
    try:
        code = ctypes.c_ulong(0)
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return True
        return code.value == STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def terminate_process_group(pid) -> bool:
    """Ask a child started with detached_popen_kwargs() (and its children)
    to stop.  Linux: SIGTERM to the pid's process group.  Windows:
    `taskkill /T /F`.  True when something was signalled."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if IS_WINDOWS:
        import subprocess
        kw = {}
        no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        if no_window:
            kw["creationflags"] = no_window     # no console flash
        try:
            res = subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                                 stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, timeout=15, **kw)
        except (OSError, subprocess.SubprocessError):
            return False
        return res.returncode == 0
    import signal
    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
        return True
    except (OSError, ValueError):
        return False


def detached_popen_kwargs() -> dict:
    """subprocess keyword arguments that start a child in its own process
    group, so it outlives the request that started it and can be stopped
    with terminate_process_group().  Linux: start_new_session=True.
    Windows: a new process group, without a console window."""
    if not IS_WINDOWS:
        return {"start_new_session": True}
    import subprocess
    if not hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP"):
        return {}                       # compat tests fake Windows on Linux
    return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP
            | getattr(subprocess, "CREATE_NO_WINDOW", 0)}


# --------------------------------------------------------------------------
# Locations
# --------------------------------------------------------------------------

LINUX_DATA_DIR = "/datapool/email-archive"
LINUX_RUNTIME_DIR = "/run/mail-archiver"
WINDOWS_APP_DIRNAME = "MailArchiver"


def default_data_dir() -> str:
    """Where mail lives when MAIL_ARCHIVER_DATA is not set: the packaged
    server's data pool on Linux, %APPDATA%\\MailArchiver on Windows."""
    if not IS_WINDOWS:
        return LINUX_DATA_DIR
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    return os.path.join(base, WINDOWS_APP_DIRNAME)


def runtime_dir() -> str:
    """Directory for run-time state (sync job state files, restart
    markers).  MAIL_ARCHIVER_RUN_DIR overrides it.  Linux:
    /run/mail-archiver (tmpfiles.d).  Windows: `.run` under the data
    directory (MAIL_ARCHIVER_DATA, else default_data_dir())."""
    override = os.environ.get("MAIL_ARCHIVER_RUN_DIR")
    if override:
        return override
    if not IS_WINDOWS:
        return LINUX_RUNTIME_DIR
    data = os.environ.get("MAIL_ARCHIVER_DATA") or default_data_dir()
    return os.path.join(data, ".run")


def replace_file(src, dst, attempts: int = 40, delay: float = 0.05) -> None:
    """os.replace().  On Windows the destination cannot be replaced while
    another thread or process has it open, so a PermissionError is retried
    for up to attempts * delay seconds before it is raised."""
    if not IS_WINDOWS:
        os.replace(src, dst)
        return
    import time
    for attempt in range(max(1, attempts)):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt >= attempts - 1:
                raise
            time.sleep(delay)


def fs_usage(path) -> tuple:
    """(total_bytes, available_bytes) of the filesystem holding `path`.
    Linux: statvfs blocks/bavail, as before.  Windows: shutil.disk_usage.
    Raises OSError when the path cannot be examined."""
    if not IS_WINDOWS and hasattr(os, "statvfs"):
        st = os.statvfs(path)
        return st.f_blocks * st.f_frsize, st.f_bavail * st.f_frsize
    usage = shutil.disk_usage(path)
    return usage.total, usage.free


# --------------------------------------------------------------------------
# Names on disk
# --------------------------------------------------------------------------

_MAILDIR_INFO_SEPARATORS = (":", "!")


def maildir_info_separator() -> str:
    """The character between a Maildir message's unique name and its
    `2,<flags>` info.  ':' on Linux (the Maildir standard, what mbsync
    writes).  '!' on Windows, where ':' is illegal in an NTFS file name;
    it is the replacement Python's mailbox.Maildir.colon documents."""
    return "!" if IS_WINDOWS else ":"


def maildir_info(flags: str = "") -> str:
    """The info suffix for a message file name: ':2,S' on Linux, '!2,S'
    on Windows.  `flags` is used as given (callers sort and filter)."""
    return f"{maildir_info_separator()}2,{flags}"


def split_maildir_name(filename: str) -> tuple:
    """(unique, flags) of a Maildir message file name.  Reads both
    separators on every platform, so an archive copied between Linux and
    Windows is still understood.  flags is '' when there is no info."""
    for sep in _MAILDIR_INFO_SEPARATORS:
        marker = sep + "2,"
        if marker in filename:
            unique, flags = filename.split(marker, 1)
            return unique, flags
    return filename, ""


_WINDOWS_RESERVED_NAMES = frozenset(
    ["CON", "PRN", "AUX", "NUL"]
    + [f"COM{i}" for i in range(1, 10)] + [f"LPT{i}" for i in range(1, 10)])


def safe_path_component(name: str) -> str:
    """One directory or file name that the local filesystem accepts.
    Linux: returned unchanged.  Windows: the characters NTFS refuses
    (< > : " / \\ | ? * and control characters) become '_', trailing dots
    and spaces are dropped, and a reserved device name gets a '_' suffix."""
    if not IS_WINDOWS:
        return name
    cleaned = "".join("_" if (c in '<>:"/\\|?*' or ord(c) < 32) else c
                      for c in name).rstrip(". ")
    if not cleaned:
        return "_"
    if cleaned.split(".", 1)[0].upper() in _WINDOWS_RESERVED_NAMES:
        cleaned += "_"
    return cleaned


_WINDOWS_CHILD_ENV_KEEP = frozenset((
    "PATH", "PATHEXT", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC",
    "TEMP", "TMP", "USERPROFILE", "USERNAME", "HOMEDRIVE", "HOMEPATH",
    "APPDATA", "LOCALAPPDATA", "PROGRAMDATA", "MSYSTEM"))


def minimal_child_env() -> dict:
    """Environment for a helper child that runs as ourselves and must not
    inherit our settings or secrets.  Linux: a fixed PATH and LANG, as
    before.  Windows: only the variables a process cannot start or find
    its tools without, taken from our own environment, plus UTF-8 mode
    for a Python child."""
    if not IS_WINDOWS:
        return {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8"}
    env = {k: v for k, v in os.environ.items()
           if k.upper() in _WINDOWS_CHILD_ENV_KEEP}
    env["PYTHONUTF8"] = "1"
    return env


# --------------------------------------------------------------------------
# TLS for the built-in IMAP client
# --------------------------------------------------------------------------

def tls_client_context():
    """A TLS context that VERIFIES the server: certificate chain and host name.

    imaplib's default (no context given) checks neither, so up to 1.3.0 the
    built-in IMAP client would hand the account's password to any server
    that answered.  Trust comes from the operating system (the Windows
    certificate store, or the distribution's CA bundle), plus the PEM file
    named in MAIL_ARCHIVER_CA_FILE when that is set (a private CA).
    """
    import ssl
    ctx = ssl.create_default_context()
    extra = os.environ.get("MAIL_ARCHIVER_CA_FILE", "")
    if extra and os.path.isfile(extra):
        try:
            ctx.load_verify_locations(cafile=extra)
        except (ssl.SSLError, OSError):
            pass
    return ctx

