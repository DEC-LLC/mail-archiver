"""platform_compat on the platform the tests run on, and the rule that
nothing else touches the POSIX-only modules (1.3.1).

These run natively on Linux and on Windows; nothing is faked here (the
faked-Windows scenarios are in test_windows_compat_app.py). No path is
hard-coded: everything lives under tempfile.
"""

import ast
import glob
import os
import subprocess
import sys
import tempfile
import time

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import platform_compat as pc  # noqa: E402

POSIX_ONLY = {'fcntl', 'pwd', 'grp', 'resource'}
PAM_MODULES = {'pam', 'PAM'}
# The server core: these must be clean whatever state the rest is in.
CORE_FILES = ('app.py', 'admin.py', 'oauth2_microsoft.py', 'sync_lifecycle.py',
              'search_index.py', 'imap_sync.py', 'mail_batcher.py', 'layout.py')
linux_only = pytest.mark.skipif(os.name == 'nt', reason='checks the Linux behaviour')


# ---------------------------------------------------------------------
# Static: who imports what
# ---------------------------------------------------------------------

def _imports(path):
    """(module, line, at_module_level) for every import in a source file,
    at any depth (function bodies and try blocks included)."""
    with open(path, encoding='utf-8') as f:
        tree = ast.parse(f.read(), filename=path)
    top_level = set()
    for node in tree.body:
        for sub in ast.walk(node) if not isinstance(
                node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) else ():
            top_level.add(id(sub))
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [a.name.split('.')[0] for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names = [node.module.split('.')[0]]
        elif (isinstance(node, ast.Call) and getattr(node.func, 'id', '') == '__import__'
              and node.args and isinstance(node.args[0], ast.Constant)):
            names = [str(node.args[0].value).split('.')[0]]
        else:
            continue
        found.extend((name, node.lineno, id(node) in top_level) for name in names)
    return found


def _top_level_sources():
    files = sorted(glob.glob(os.path.join(ROOT, '*.py')))
    return [f for f in files if os.path.basename(f) != 'platform_compat.py']


def test_only_platform_compat_imports_the_posix_only_modules():
    files = _top_level_sources()
    assert {os.path.basename(f) for f in files} >= set(CORE_FILES)
    offenders = [f'{os.path.basename(path)}:{line} imports {name}'
                 for path in files
                 for name, line, _top in _imports(path) if name in POSIX_ONLY]
    assert offenders == []


@pytest.mark.parametrize('name', CORE_FILES)
def test_core_file_has_no_posix_only_import(name):
    hits = [(mod, line) for mod, line, _top in _imports(os.path.join(ROOT, name))
            if mod in POSIX_ONLY]
    assert hits == []


def test_pam_is_imported_lazily_and_only_by_app():
    seen = {}
    for path in _top_level_sources() + [os.path.join(ROOT, 'platform_compat.py')]:
        for name, line, top in _imports(path):
            if name in PAM_MODULES:
                seen.setdefault(os.path.basename(path), []).append(top)
    assert list(seen) == ['app.py']
    assert not any(seen['app.py']), 'PAM must not be imported at module level'
    with open(os.path.join(ROOT, 'app.py'), encoding='utf-8') as f:
        source = f.read()
    guard = source.index('if not _pc.supports_system_accounts():',
                         source.index('def pam_authenticate('))
    assert guard < source.index('import PAM')


def test_core_files_do_not_call_the_posix_only_os_functions():
    """os.chown, os.geteuid & co. go through platform_compat as well."""
    banned = {'chown', 'lchown', 'fchown', 'geteuid', 'getuid', 'getegid', 'getgid',
              'getpgid', 'killpg', 'setsid', 'fork', 'kill', 'statvfs', 'rename'}
    hits = []
    for name in CORE_FILES:
        with open(os.path.join(ROOT, name), encoding='utf-8') as f:
            tree = ast.parse(f.read())
        for node in ast.walk(tree):
            if (isinstance(node, ast.Attribute) and node.attr in banned
                    and isinstance(node.value, ast.Name) and node.value.id == 'os'):
                hits.append(f'{name}:{node.lineno} os.{node.attr}')
            if (isinstance(node, ast.keyword) and node.arg in ('preexec_fn',
                                                                'start_new_session')):
                hits.append(f'{name}: {node.arg}=')
    assert hits == []


def test_core_files_open_text_files_with_an_encoding():
    """open() in text mode without encoding= reads cp1252 on Windows."""
    hits = []
    for name in CORE_FILES:
        with open(os.path.join(ROOT, name), encoding='utf-8') as f:
            tree = ast.parse(f.read())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            callee = getattr(func, 'id', None) or getattr(func, 'attr', None)
            keywords = {k.arg for k in node.keywords}
            if callee == 'open' and getattr(func, 'id', None) == 'open':
                mode = node.args[1] if len(node.args) > 1 else next(
                    (k.value for k in node.keywords if k.arg == 'mode'), None)
                binary = isinstance(mode, ast.Constant) and 'b' in str(mode.value)
                if not binary and 'encoding' not in keywords:
                    hits.append(f'{name}:{node.lineno} open()')
            elif callee in ('read_text', 'write_text') and 'encoding' not in keywords:
                hits.append(f'{name}:{node.lineno} {callee}()')
            elif (callee == 'fdopen' and 'encoding' not in keywords
                  and not any(isinstance(a, ast.Constant) and 'b' in str(a.value)
                              for a in node.args[1:])):
                hits.append(f'{name}:{node.lineno} fdopen()')
    assert hits == []


# ---------------------------------------------------------------------
# Locks
# ---------------------------------------------------------------------

@pytest.fixture
def workdir():
    with tempfile.TemporaryDirectory(prefix='ma-wincompat-') as d:
        yield d


def test_lock_file_excludes_a_second_holder(workdir):
    path = os.path.join(workdir, 'a.lock')
    with open(path, 'a+', encoding='utf-8') as first, \
            open(path, 'a+', encoding='utf-8') as second:
        pc.lock_file(first, exclusive=True, blocking=False)
        with pytest.raises(pc.LockUnavailable):
            pc.lock_file(second, exclusive=True, blocking=False)
        pc.unlock_file(first)
        pc.lock_file(second, exclusive=True, blocking=False)   # free again
        pc.unlock_file(second)


def test_lock_fd_excludes_a_second_holder_and_closing_releases(workdir):
    path = os.path.join(workdir, 'b.lock')
    first = os.open(path, os.O_RDONLY | os.O_CREAT, 0o644)
    second = os.open(path, os.O_RDONLY | os.O_CREAT, 0o644)
    try:
        pc.lock_fd(first, exclusive=True, blocking=False)
        with pytest.raises(pc.LockUnavailable):
            pc.lock_fd(second, exclusive=True, blocking=False)
        assert issubclass(pc.LockUnavailable, OSError)
    finally:
        os.close(first)                    # closing releases, as claim_sync relies on
    try:
        pc.lock_fd(second, exclusive=True, blocking=False)
        pc.unlock_fd(second)
    finally:
        os.close(second)


@linux_only
def test_shared_locks_coexist_on_linux(workdir):
    path = os.path.join(workdir, 'c.lock')
    first = os.open(path, os.O_RDONLY | os.O_CREAT, 0o644)
    second = os.open(path, os.O_RDONLY | os.O_CREAT, 0o644)
    try:
        pc.lock_fd(first, exclusive=False, blocking=False)
        pc.lock_fd(second, exclusive=False, blocking=False)    # two probes
        third = os.open(path, os.O_RDONLY)
        try:
            with pytest.raises(pc.LockUnavailable):
                pc.lock_fd(third, exclusive=True, blocking=False)
        finally:
            os.close(third)
    finally:
        os.close(first)
        os.close(second)


# ---------------------------------------------------------------------
# Files and processes
# ---------------------------------------------------------------------

def test_replace_file_overwrites_an_existing_file(workdir):
    src, dst = os.path.join(workdir, 'new'), os.path.join(workdir, 'current')
    for path, text in ((src, 'new'), (dst, 'old')):
        with open(path, 'w', encoding='utf-8') as f:
            f.write(text)
    pc.replace_file(src, dst)
    with open(dst, encoding='utf-8') as f:
        assert f.read() == 'new'
    assert not os.path.exists(src)


def test_fs_usage_reports_the_volume(workdir):
    total, available = pc.fs_usage(workdir)
    assert total > 0 and 0 <= available <= total
    with pytest.raises(OSError):
        pc.fs_usage(os.path.join(workdir, 'missing', 'deeper'))


def test_pid_alive_tells_a_live_process_from_a_finished_one():
    assert pc.pid_alive(os.getpid()) is True
    assert pc.pid_alive(None) is False
    assert pc.pid_alive('not a pid') is False
    assert pc.pid_alive(0) is False
    child = subprocess.Popen([sys.executable, '-c', 'pass'])
    child.wait()
    assert pc.pid_alive(child.pid) is False
    assert pc.pid_alive(os.getpid()) is True      # probing never signals us


def test_detached_child_starts_and_can_be_stopped():
    child = subprocess.Popen(
        [sys.executable, '-c', 'import time; time.sleep(120)'],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        **pc.detached_popen_kwargs())
    try:
        deadline = time.time() + 10
        while not pc.pid_alive(child.pid) and time.time() < deadline:
            time.sleep(0.05)
        assert pc.pid_alive(child.pid) is True
        assert pc.terminate_process_group(child.pid) is True
        child.wait(timeout=30)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()
    assert pc.terminate_process_group(child.pid) is False     # already gone
    assert pc.terminate_process_group('x') is False


def test_minimal_child_env_starts_a_python_child():
    env = pc.minimal_child_env()
    assert not [k for k in env if k.startswith('MAIL_ARCHIVER_')]
    out = subprocess.run([sys.executable, '-c', 'print("ok")'], env=env,
                         stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)
    assert (out.returncode, out.stdout.strip()) == (0, 'ok')


# ---------------------------------------------------------------------
# Names and places
# ---------------------------------------------------------------------

def test_maildir_names_are_read_with_either_separator():
    assert pc.split_maildir_name('1700000000.5.host:2,RS') == ('1700000000.5.host', 'RS')
    assert pc.split_maildir_name('1700000000.5.host!2,RS') == ('1700000000.5.host', 'RS')
    assert pc.split_maildir_name('1700000000.5.host') == ('1700000000.5.host', '')
    sep = pc.maildir_info_separator()
    assert sep in (':', '!')
    assert pc.maildir_info('S') == f'{sep}2,S'
    name = '1700000000.5.host' + pc.maildir_info('S')
    assert pc.split_maildir_name(name) == ('1700000000.5.host', 'S')
    # what this platform writes is a name this platform accepts
    with tempfile.TemporaryDirectory(prefix='ma-wincompat-') as d:
        with open(os.path.join(d, name), 'wb') as f:
            f.write(b'x')
        assert os.listdir(d) == [name]


def test_safe_path_component_is_usable_as_a_directory_name(workdir):
    for raw in ('INBOX', 'Lists: dev', 'a<b>c|d?e*f', 'trailing. ', 'NUL', 'Sent_Items'):
        name = pc.safe_path_component(raw)
        if os.name == 'nt' or raw == 'INBOX' or raw == 'Sent_Items':
            os.makedirs(os.path.join(workdir, name))
            assert name in os.listdir(workdir)
    assert pc.safe_path_component('INBOX') == 'INBOX'


@linux_only
def test_linux_names_and_places_are_what_they_were():
    assert pc.IS_WINDOWS is False
    assert pc.maildir_info('S') == ':2,S'
    assert pc.safe_path_component('Lists: dev?') == 'Lists: dev?'
    assert pc.default_data_dir() == '/datapool/email-archive'
    assert pc.detached_popen_kwargs() == {'start_new_session': True}
    assert pc.minimal_child_env() == {'PATH': '/usr/sbin:/usr/bin:/sbin:/bin',
                                      'LANG': 'C.UTF-8'}
    assert pc.current_uid() == os.geteuid()
    assert pc.supports_system_accounts() is True
    if 'MAIL_ARCHIVER_RUN_DIR' not in os.environ:
        assert pc.runtime_dir() == '/run/mail-archiver'
        import admin
        import sync_lifecycle
        assert sync_lifecycle.JOB_STATE_DIR in ('/run/mail-archiver/sync-jobs',) or \
            not sync_lifecycle.JOB_STATE_DIR.startswith('/run/')   # a test may repoint it
        assert admin.VERSION_FILE == '/opt/mail-archiver/VERSION'
    me = pc.user_info(os.geteuid())
    if me is not None:
        assert pc.supplementary_gids(me.name, me.gid)[0] == me.gid
    assert pc.supplementary_gids('no-such-user-here') == []


def test_runtime_dir_can_be_set(workdir, monkeypatch):
    monkeypatch.setenv('MAIL_ARCHIVER_RUN_DIR', workdir)
    assert pc.runtime_dir() == workdir


@linux_only
def test_linux_mbsyncrc_keeps_its_paths(workdir, monkeypatch):
    """The generated .mbsyncrc is byte-for-byte what 1.3.0 wrote for the
    lines that carry a path."""
    monkeypatch.setenv('MAIL_ARCHIVER_SECRET', 'test-only-secret')
    os.environ.setdefault('MAIL_ARCHIVER_DATA', workdir)
    os.environ.setdefault('MAIL_ARCHIVER_AUTH', 'builtin')
    import app as ma
    monkeypatch.setitem(ma.CONFIG, 'data_dir', workdir)
    monkeypatch.setitem(ma.CONFIG, 'auth_mode', 'builtin')
    monkeypatch.setattr(ma, '_secret_key_source', 'env')
    user = 'wincompatuser'
    ma.save_accounts(user, [
        {'email': 'a@mail.example', 'provider': 'custom', 'host': 'imap.mail.example',
         'auth_type': 'app_password', 'enabled': True, 'client_cert': 'a.client_cert.pem'},
        {'email': 'b@mail.example', 'provider': 'hotmail', 'auth_type': 'oauth2',
         'enabled': True}])
    ma.generate_mbsyncrc(user)
    with open(os.path.join(workdir, user, '.mbsyncrc'), encoding='utf-8') as f:
        rc = f.read()
    config = f'{workdir}/{user}/.config'
    assert 'PassCmd "/usr/libexec/mail-archiver-cred wincompatuser a@mail.example"' in rc
    assert f'PassCmd "cat {config}/b_at_mail_example.token"' in rc
    # the system CA bundle of the platform (Debian or EL location, 1.3.3)
    assert ma.MBSYNC_CA_FILE in ma.SYSTEM_CA_BUNDLES
    assert f'CertificateFile {ma.MBSYNC_CA_FILE}' in rc
    assert f'ClientCertificate {config}/a.client_cert.pem' in rc
    assert f'Path {workdir}/{user}/a_at_mail_example/' in rc
    assert f'Inbox {workdir}/{user}/a_at_mail_example/INBOX' in rc
