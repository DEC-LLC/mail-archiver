"""DEC-LLC Mail-Archiver - Windows single-user launcher.

This is the entry point of MailArchiver-<version>.exe (PyInstaller onefile,
windowed) and can also be run from a checkout: `python windows-app\\main.py`.

It starts the shared Flask application (the top-level `app` module, the same
code the Linux packages ship) in its single-user Windows mode:

* built-in accounts (MAIL_ARCHIVER_AUTH=builtin); PAM does not exist here;
* everything under %APPDATA%\\MailArchiver\\ (archive, .secret_key, certs\\,
  logs\\, launcher.json);
* served on 127.0.0.1 only, port 8400 (MAIL_ARCHIVER_PORT overrides), HTTPS
  with a self-signed certificate when one can be made, plain HTTP on the
  loopback interface otherwise;
* the browser is opened on the URL; a tray icon (pystray) offers Open / Quit
  when pystray is importable, and without it the process simply stays in
  the background;
* one instance per Windows session: a second launch opens the browser on
  the running instance and exits. `MailArchiver.exe --quit` stops it.

Interpreter emulation. The application starts helper processes as
`[sys.executable, <script.py>, args...]`, `[sys.executable, '-I', ...]` and
`[sys.executable, '-c', code]`. In the EXE sys.executable is the EXE itself,
so when it is started that way this module behaves like `python`: it runs
the named bundled module as __main__ (or the -c code) and exits, without a
tray icon, a browser or the single-instance guard. See _interpreter_request.

No POSIX-only module is imported here, and nothing needs Python on PATH.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import runpy
import secrets
import socket
import ssl
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

PRODUCT = 'DEC-LLC Mail-Archiver'
APP_DIR_NAME = 'MailArchiver'
DEFAULT_PORT = 8400
BIND_HOST = '127.0.0.1'
MUTEX_NAME = 'Local\\DEC-LLC-MailArchiver-instance'
QUIT_EVENT_NAME = 'Local\\DEC-LLC-MailArchiver-quit'
STATE_FILE = 'launcher.json'

IS_WINDOWS = os.name == 'nt'
FROZEN = bool(getattr(sys, 'frozen', False))

log = logging.getLogger('mail_archiver.launcher')


# ---------------------------------------------------------------
# Paths
# ---------------------------------------------------------------

def resource_dir() -> Path:
    """Where the application modules, templates/, static/, VERSION and
    BUILD_INFO live: the PyInstaller unpack directory in the EXE, the
    repository root when run from source."""
    meipass = getattr(sys, '_MEIPASS', None)
    if meipass:
        return Path(meipass)
    return Path(__file__).resolve().parent.parent


def home_dir() -> Path:
    r"""%APPDATA%\MailArchiver (per Windows user, roams with the profile)."""
    if IS_WINDOWS:
        appdata = os.environ.get('APPDATA')
        base = Path(appdata) if appdata else Path.home() / 'AppData' / 'Roaming'
        return base / APP_DIR_NAME
    # Only reached when the launcher is run from source on another OS.
    return Path.home() / '.mail-archiver'


def read_version() -> str:
    try:
        return (resource_dir() / 'VERSION').read_text(encoding='utf-8').strip() or 'unknown'
    except OSError:
        return 'unknown'


# ---------------------------------------------------------------
# Interpreter emulation for the application's own child processes
# ---------------------------------------------------------------

_INTERPRETER_FLAGS = frozenset(('-I', '-E', '-s', '-S', '-u', '-B', '-O', '-OO', '-q'))


def _interpreter_request(argv):
    """Decide whether this process was started as a stand-in for `python`.

    Returns ('code', source, rest), ('module', name, argv0, rest) or None.
    Only the frozen EXE does this; from source sys.executable is a real
    interpreter. A script is matched by its file name, not its path: the
    path the parent computed points into the parent's unpack directory and
    the bundled modules are inside the EXE's archive, not files on disk.
    """
    if not FROZEN:
        return None
    args = list(argv[1:])
    while args and args[0] in _INTERPRETER_FLAGS:
        args.pop(0)
    if not args:
        return None
    if args[0] == '-c' and len(args) >= 2:
        return ('code', args[1], args[2:])
    if args[0] == '-m' and len(args) >= 2:
        return ('module', args[1], args[1], args[2:])
    name = os.path.basename(args[0].replace('\\', '/'))
    stem, ext = os.path.splitext(name)
    if ext.lower() in ('.py', '.pyc') and stem.isidentifier():
        return ('module', stem, args[0], args[1:])
    return None


def _run_as_interpreter(request) -> None:
    """Run what the parent asked `python` to run. Does not return normally
    when the code calls sys.exit()."""
    # A windowed EXE has no console; without inherited pipes the standard
    # streams are None and the first print() would raise.
    for name, mode in (('stdin', 'r'), ('stdout', 'w'), ('stderr', 'w')):
        if getattr(sys, name) is None:
            setattr(sys, name, open(os.devnull, mode, encoding='utf-8'))
    os.environ.pop('MSYSTEM', None)
    apply_environment(create=False)
    if request[0] == 'code':
        _, source, rest = request
        sys.argv = ['-c'] + list(rest)
        scope = {'__name__': '__main__', '__builtins__': __builtins__}
        exec(compile(source, '<string>', 'exec'), scope)
        return
    _, module, argv0, rest = request
    sys.argv = [argv0] + list(rest)
    runpy.run_module(module, run_name='__main__', alter_sys=True)


# ---------------------------------------------------------------
# Configuration contract with app.py (environment variables)
# ---------------------------------------------------------------

def apply_environment(create: bool = True) -> dict:
    """Set the environment the shared application reads at import time.

    MAIL_ARCHIVER_DATA         kept when already set (archive on another
                               drive), else %APPDATA%\\MailArchiver
    MAIL_ARCHIVER_AUTH         always 'builtin'
    MAIL_ARCHIVER_PORT         kept when already set, else 8400
    MAIL_ARCHIVER_SECRET_FILE  kept when already set, else
                               %APPDATA%\\MailArchiver\\.secret_key
    MAIL_ARCHIVER_HTTPS        set by serve() once TLS is known
    """
    home = home_dir()
    data = Path(os.environ.get('MAIL_ARCHIVER_DATA') or home)
    secret_file = Path(os.environ.get('MAIL_ARCHIVER_SECRET_FILE') or home / '.secret_key')
    port_text = os.environ.get('MAIL_ARCHIVER_PORT') or str(DEFAULT_PORT)

    os.environ['MAIL_ARCHIVER_DATA'] = str(data)
    os.environ['MAIL_ARCHIVER_AUTH'] = 'builtin'
    os.environ['MAIL_ARCHIVER_PORT'] = port_text
    os.environ['MAIL_ARCHIVER_SECRET_FILE'] = str(secret_file)

    if create:
        for d in (home, data, home / 'logs'):
            d.mkdir(parents=True, exist_ok=True)
        _ensure_secret_key(secret_file)
    return {'home': home, 'data': data, 'secret_file': secret_file,
            'port_text': port_text}


def _ensure_secret_key(path: Path) -> None:
    """The key signs sessions and derives the key that encrypts stored mail
    passwords, so it is created once and never replaced. Never logged."""
    try:
        if path.is_file() and path.stat().st_size > 0:
            return
    except OSError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.new')
    with open(tmp, 'w', encoding='ascii') as f:
        f.write(secrets.token_hex(32))
    os.replace(tmp, path)


# ---------------------------------------------------------------
# Logging
# ---------------------------------------------------------------

class _LogStream:
    """File-like object that sends writes to a logger: sys.stdout and
    sys.stderr are None in a windowed EXE and the application prints."""

    encoding = 'utf-8'

    def __init__(self, logger: logging.Logger, level: int):
        self._logger = logger
        self._level = level
        self._buffer = ''

    def write(self, text):
        if not isinstance(text, str):
            text = bytes(text).decode('utf-8', 'replace')
        self._buffer += text
        while '\n' in self._buffer:
            line, self._buffer = self._buffer.split('\n', 1)
            if line.strip():
                self._logger.log(self._level, line.rstrip())
        return len(text)

    def flush(self):
        if self._buffer.strip():
            self._logger.log(self._level, self._buffer.rstrip())
        self._buffer = ''

    def isatty(self):
        return False

    def fileno(self):
        raise OSError('no file descriptor: output goes to the log file')


def setup_logging(home: Path) -> Path:
    log_dir = home / 'logs'
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / 'mail-archiver.log'
    fmt = logging.Formatter('%(asctime)s %(name)s %(levelname)s %(message)s')
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    file_handler = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=2 * 1024 * 1024, backupCount=5, encoding='utf-8')
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)
    if sys.stderr is not None:
        console = logging.StreamHandler()
        console.setFormatter(fmt)
        root.addHandler(console)
    # One line per HTTP request would bury everything else.
    logging.getLogger('werkzeug').setLevel(logging.WARNING)
    if sys.stdout is None:
        sys.stdout = _LogStream(logging.getLogger('mail_archiver.stdout'), logging.INFO)
    if sys.stderr is None:
        sys.stderr = _LogStream(logging.getLogger('mail_archiver.stderr'), logging.WARNING)
    return log_file


def alert(text: str) -> None:
    """Tell the person something went wrong. A windowed EXE has no console,
    so on Windows this is a message box; the log has the detail."""
    log.error('%s', text)
    if IS_WINDOWS:
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, text, PRODUCT, 0x10)
        except Exception:
            pass


# ---------------------------------------------------------------
# Single instance + quit signal
# ---------------------------------------------------------------

class SingleInstance:
    """Named mutex on Windows; a lock file elsewhere (source runs only)."""

    def __init__(self, home: Path):
        self._home = home
        self._handle = None
        self._lock_file = None

    def acquire(self) -> bool:
        if IS_WINDOWS:
            import ctypes
            from ctypes import wintypes
            k32 = ctypes.WinDLL('kernel32', use_last_error=True)
            k32.CreateMutexW.restype = wintypes.HANDLE
            k32.CreateMutexW.argtypes = (ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR)
            ctypes.set_last_error(0)
            handle = k32.CreateMutexW(None, False, MUTEX_NAME)
            already = ctypes.get_last_error() == 183  # ERROR_ALREADY_EXISTS
            if not handle:
                return True  # cannot tell; the port check still protects us
            if already:
                k32.CloseHandle.argtypes = (wintypes.HANDLE,)
                k32.CloseHandle(handle)
                return False
            self._handle = handle  # held until the process exits
            return True
        import fcntl  # not Windows: reached from a source run on Linux/macOS
        self._lock_file = open(self._home / 'launcher.lock', 'w')
        try:
            fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._lock_file.close()
            self._lock_file = None
            return False
        return True


def _quit_event_api():
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.WinDLL('kernel32', use_last_error=True)
    k32.CreateEventW.restype = wintypes.HANDLE
    k32.CreateEventW.argtypes = (ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR)
    k32.OpenEventW.restype = wintypes.HANDLE
    k32.OpenEventW.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR)
    k32.SetEvent.argtypes = (wintypes.HANDLE,)
    k32.WaitForSingleObject.restype = wintypes.DWORD
    k32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    k32.CloseHandle.argtypes = (wintypes.HANDLE,)
    return k32


def watch_quit_event(stop: threading.Event) -> None:
    """Set `stop` when another launch runs `--quit` (Windows only)."""
    if not IS_WINDOWS:
        return
    try:
        k32 = _quit_event_api()
        handle = k32.CreateEventW(None, True, False, QUIT_EVENT_NAME)
    except Exception as exc:
        log.warning('quit signal unavailable: %s', exc)
        return
    if not handle:
        return

    def wait():
        while not stop.is_set():
            if k32.WaitForSingleObject(handle, 500) == 0:  # WAIT_OBJECT_0
                log.info('quit requested by another launch (--quit)')
                stop.set()
                return

    threading.Thread(target=wait, name='quit-watch', daemon=True).start()


def signal_quit() -> bool:
    """`--quit`: ask the running instance to stop. True when one was told."""
    if not IS_WINDOWS:
        return False
    k32 = _quit_event_api()
    handle = k32.OpenEventW(0x0002, False, QUIT_EVENT_NAME)  # EVENT_MODIFY_STATE
    if not handle:
        return False
    ok = bool(k32.SetEvent(handle))
    k32.CloseHandle(handle)
    return ok


# ---------------------------------------------------------------
# Launcher state (what a second launch needs: the URL)
# ---------------------------------------------------------------

def write_state(home: Path, url: str, port: int) -> None:
    tmp = home / (STATE_FILE + '.new')
    tmp.write_text(json.dumps({'url': url, 'port': port, 'pid': os.getpid(),
                               'version': read_version()}), encoding='utf-8')
    os.replace(tmp, home / STATE_FILE)


def read_state_url(home: Path, wait_seconds: float = 10.0):
    """URL of the running instance; waits briefly because the first
    instance writes it only once the server answers."""
    deadline = time.monotonic() + wait_seconds
    while True:
        try:
            url = json.loads((home / STATE_FILE).read_text(encoding='utf-8')).get('url')
            if url:
                return url
        except (OSError, ValueError):
            pass
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.25)


def clear_state(home: Path) -> None:
    try:
        (home / STATE_FILE).unlink()
    except OSError:
        pass


# ---------------------------------------------------------------
# TLS
# ---------------------------------------------------------------

_OPENSSL_CONFIG = """\
[req]
distinguished_name = dn
x509_extensions = v3
prompt = no
[dn]
O = DEC-LLC Mail-Archiver
CN = localhost
[v3]
subjectAltName = DNS:localhost, IP:127.0.0.1
basicConstraints = critical, CA:FALSE
keyUsage = digitalSignature, keyEncipherment
extendedKeyUsage = serverAuth
"""

_OPENSSL_PLACES = (
    r'C:\Program Files\OpenSSL-Win64\bin\openssl.exe',
    r'C:\Program Files\Git\usr\bin\openssl.exe',
    r'C:\Program Files\Git\mingw64\bin\openssl.exe',
    r'C:\msys64\ucrt64\bin\openssl.exe',
    r'C:\msys64\usr\bin\openssl.exe',
)


def _find_openssl():
    import shutil
    found = shutil.which('openssl')
    if found:
        return found
    if IS_WINDOWS:
        for candidate in _OPENSSL_PLACES:
            if os.path.isfile(candidate):
                return candidate
    return None


def _make_cert_with_cryptography(cert_file: Path, key_file: Path) -> bool:
    try:
        import datetime
        import ipaddress
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
    except Exception:
        return False
    try:
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, PRODUCT),
            x509.NameAttribute(NameOID.COMMON_NAME, 'localhost'),
        ])
        now = datetime.datetime.now(datetime.timezone.utc)
        cert = (
            x509.CertificateBuilder()
            .subject_name(name).issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=3650))
            .add_extension(x509.SubjectAlternativeName([
                x509.DNSName('localhost'),
                x509.IPAddress(ipaddress.ip_address('127.0.0.1')),
            ]), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]),
                           critical=False)
            .sign(key, hashes.SHA256())
        )
        key_file.write_bytes(key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption()))
        cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        return True
    except Exception as exc:
        log.warning('certificate generation (cryptography) failed: %s', exc)
        return False


def _make_cert_with_openssl(cert_file: Path, key_file: Path) -> bool:
    openssl = _find_openssl()
    if not openssl:
        return False
    config = cert_file.with_name('openssl-selfsigned.cnf')
    try:
        config.write_text(_OPENSSL_CONFIG, encoding='ascii')
        kwargs = {}
        if IS_WINDOWS:
            kwargs['creationflags'] = 0x08000000  # CREATE_NO_WINDOW
        done = subprocess.run(
            [openssl, 'req', '-x509', '-newkey', 'rsa:2048', '-nodes',
             '-days', '3650', '-config', str(config),
             '-keyout', str(key_file), '-out', str(cert_file)],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, timeout=60, **kwargs)
        if done.returncode != 0:
            log.warning('openssl exited %s while making the certificate', done.returncode)
        return done.returncode == 0 and cert_file.is_file() and key_file.is_file()
    except Exception as exc:
        log.warning('certificate generation (openssl) failed: %s', exc)
        return False
    finally:
        try:
            config.unlink()
        except OSError:
            pass


def tls_context(home: Path):
    r"""An SSLContext for the server, or None to serve plain HTTP.

    certs\mail-archiver.crt + .key are used when present (also a
    certificate the person put there themselves). Otherwise a self-signed
    one for localhost / 127.0.0.1 is made, valid ten years: in-process with
    the `cryptography` package when the EXE was built with it, else with an
    openssl.exe found on the machine. With neither, or MAIL_ARCHIVER_TLS=off,
    the server speaks HTTP; it listens on the loopback interface only.
    """
    if os.environ.get('MAIL_ARCHIVER_TLS', '').strip().lower() in ('0', 'off', 'no', 'false'):
        log.info('TLS switched off by MAIL_ARCHIVER_TLS')
        return None
    cert_dir = home / 'certs'
    cert_file = cert_dir / 'mail-archiver.crt'
    key_file = cert_dir / 'mail-archiver.key'
    if not (cert_file.is_file() and key_file.is_file()):
        cert_dir.mkdir(parents=True, exist_ok=True)
        if _make_cert_with_cryptography(cert_file, key_file):
            log.info('self-signed certificate created in %s (cryptography)', cert_dir)
        elif _make_cert_with_openssl(cert_file, key_file):
            log.info('self-signed certificate created in %s (openssl)', cert_dir)
        else:
            log.warning('no way to create a certificate (no cryptography package in '
                        'this build, no openssl.exe found): serving HTTP on %s', BIND_HOST)
            return None
    try:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(cert_file), str(key_file))
        return context
    except (ssl.SSLError, OSError) as exc:
        log.warning('certificate in %s is unusable (%s): serving HTTP', cert_dir, exc)
        return None


# ---------------------------------------------------------------
# Server
# ---------------------------------------------------------------

def port_is_free(port: int) -> bool:
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind((BIND_HOST, port))
        return True
    except OSError:
        return False
    finally:
        probe.close()


def wait_until_listening(port: int, seconds: float = 20.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((BIND_HOST, port), timeout=1.0):
                return True
        except OSError:
            time.sleep(0.2)
    return False


def start_server(home: Path, port: int):
    """Import the shared application and serve it on a background thread.
    Returns (server, thread, url)."""
    context = tls_context(home)
    if context is not None:
        os.environ['MAIL_ARCHIVER_HTTPS'] = '1'
    else:
        os.environ.pop('MAIL_ARCHIVER_HTTPS', None)
    scheme = 'https' if context is not None else 'http'
    url = '%s://%s:%d' % (scheme, BIND_HOST, port)

    root = str(resource_dir())
    if root not in sys.path:
        sys.path.insert(0, root)
    # Imported only now: app.py reads its configuration from the
    # environment at import time.
    import app as app_module
    from werkzeug.serving import make_server
    flask_app = app_module.app
    # On Linux a systemd timer runs the hourly schedule; here the app's
    # own in-process scheduler does (scheduled syncs and imports).
    try:
        app_module.start_background_scheduler()
    except Exception:
        logging.getLogger('mail-archiver.launcher').exception('scheduler did not start')

    server = make_server(BIND_HOST, port, flask_app, threaded=True, ssl_context=context)
    thread = threading.Thread(target=server.serve_forever, name='http-server', daemon=True)
    thread.start()
    return server, thread, url


# ---------------------------------------------------------------
# Tray icon
# ---------------------------------------------------------------

def _open_folder(path: Path) -> None:
    try:
        if IS_WINDOWS:
            os.startfile(str(path))  # noqa: only exists on Windows
        else:
            webbrowser.open(path.as_uri())
    except Exception as exc:
        log.warning('cannot open %s: %s', path, exc)


def _tray_image():
    from PIL import Image, ImageDraw
    icon_file = resource_dir() / 'MailArchiver.ico'
    if icon_file.is_file():
        try:
            return Image.open(icon_file)
        except Exception:
            pass
    blue = (31, 95, 168, 255)
    image = Image.new('RGBA', (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((2, 2, 61, 61), radius=12, fill=blue)
    draw.rectangle((12, 20, 51, 44), fill=(255, 255, 255, 255))
    draw.line((12, 20, 32, 34, 51, 20), fill=blue, width=3)
    return image


def run_tray(url: str, cfg: dict, stop: threading.Event) -> bool:
    """Show the tray icon and block until Quit (or `stop`). False when
    there is no tray to show - pystray or Pillow missing, or no shell."""
    try:
        import pystray
        image = _tray_image()
    except Exception as exc:
        log.info('no tray icon (%s: %s); running in the background',
                 type(exc).__name__, exc)
        return False

    def on_open(icon, item):
        webbrowser.open(url)

    def on_quit(icon, item):
        stop.set()
        icon.stop()

    menu = pystray.Menu(
        pystray.MenuItem('Open Mail-Archiver', on_open, default=True),
        pystray.MenuItem('Open archive folder', lambda icon, item: _open_folder(cfg['data'])),
        pystray.MenuItem('Open log folder', lambda icon, item: _open_folder(cfg['home'] / 'logs')),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem('Quit', on_quit),
    )
    try:
        icon = pystray.Icon('mail-archiver', image,
                            '%s %s' % (PRODUCT, read_version()), menu)
    except Exception as exc:
        log.warning('tray icon could not be created: %s', exc)
        return False

    def stop_icon_when_asked():
        stop.wait()
        try:
            icon.stop()
        except Exception:
            pass

    threading.Thread(target=stop_icon_when_asked, name='tray-stop', daemon=True).start()
    try:
        icon.run()
    except Exception as exc:
        log.warning('tray icon stopped working: %s', exc)
        return False
    return True


# ---------------------------------------------------------------
# Main
# ---------------------------------------------------------------

def _relaunch_without_msystem() -> None:
    """MSYS2's Python (which the EXE is built from) switches os.sep to '/'
    when MSYSTEM is set, i.e. when started from an MSYS2 shell. The
    application is tested with Windows separators, so start again without
    the variable. It is removed from this process's environment either way,
    so child processes never see it."""
    if not IS_WINDOWS or 'MSYSTEM' not in os.environ:
        return
    os.environ.pop('MSYSTEM', None)
    if os.sep == '\\':
        return  # this interpreter does not react to MSYSTEM
    env = dict(os.environ)
    if FROZEN:
        # A onefile EXE started by itself would reuse this process's
        # unpack directory; ask for an independent instance.
        env['PYINSTALLER_RESET_ENVIRONMENT'] = '1'
        cmd = [sys.executable] + sys.argv[1:]
    else:
        cmd = [sys.executable, os.path.abspath(__file__)] + sys.argv[1:]
    try:
        subprocess.Popen(cmd, env=env, close_fds=True,
                         stdin=subprocess.DEVNULL,
                         creationflags=0x00000008 | 0x00000200)  # DETACHED | NEW_GROUP
    except Exception:
        return  # carry on in this process
    sys.exit(0)


def main(argv=None) -> int:
    argv = list(sys.argv if argv is None else argv)

    request = _interpreter_request(argv)
    if request is not None:
        _run_as_interpreter(request)
        return 0

    options = set(argv[1:])
    if '--quit' in options:
        return 0 if signal_quit() else 1

    _relaunch_without_msystem()

    cfg = apply_environment(create=True)
    home = cfg['home']
    log_file = setup_logging(home)
    version = read_version()

    instance = SingleInstance(home)
    if not instance.acquire():
        url = read_state_url(home)
        log.info('already running; opening %s', url or '(URL unknown)')
        if url:
            webbrowser.open(url)
        else:
            alert('%s is already running, but its address is not known yet. '
                  'Try again in a moment.' % PRODUCT)
        return 0

    log.info('%s %s starting (%s)', PRODUCT, version,
             'EXE' if FROZEN else 'from source')
    log.info('data: %s   log: %s', cfg['data'], log_file)

    try:
        port = int(cfg['port_text'])
        if not 1 <= port <= 65535:
            raise ValueError(port)
    except ValueError:
        alert('MAIL_ARCHIVER_PORT is not a port number: %r' % cfg['port_text'])
        return 1
    if not port_is_free(port):
        alert('Port %d on this computer is in use by another program, so %s '
              'cannot start.\n\nClose that program, or set the environment '
              'variable MAIL_ARCHIVER_PORT to another port.' % (port, PRODUCT))
        return 1

    clear_state(home)
    try:
        server, server_thread, url = start_server(home, port)
    except BaseException as exc:  # make_server exits via SystemExit on bind errors
        if isinstance(exc, KeyboardInterrupt):
            raise
        log.exception('startup failed')
        alert('%s could not start: %s: %s\n\nDetails are in\n%s'
              % (PRODUCT, type(exc).__name__, exc, log_file))
        return 1

    if not wait_until_listening(port):
        alert('%s started but does not answer on port %d.\n\nDetails are in\n%s'
              % (PRODUCT, port, log_file))
        return 1
    write_state(home, url, port)
    log.info('serving %s', url)
    print('%s %s is running at %s' % (PRODUCT, version, url))

    if '--no-browser' not in options and os.environ.get('MAIL_ARCHIVER_NO_BROWSER') != '1':
        webbrowser.open(url)

    stop = threading.Event()
    watch_quit_event(stop)
    try:
        if not run_tray(url, cfg, stop):
            # No tray: stay in the background until --quit (or Ctrl+C when
            # run from a console), or until the server thread dies.
            while not stop.wait(1.0):
                if not server_thread.is_alive():
                    log.error('the server thread ended unexpectedly')
                    break
    except KeyboardInterrupt:
        log.info('interrupted')

    log.info('stopping')
    clear_state(home)
    stopper = threading.Thread(target=server.shutdown, daemon=True)
    stopper.start()
    stopper.join(5.0)
    log.info('%s stopped', PRODUCT)
    logging.shutdown()
    # Sync and indexing threads may still be busy; Quit means quit.
    os._exit(0)


if __name__ == '__main__':
    sys.exit(main())
