"""The server core in its Windows shape (1.3.1).

Every scenario runs in a child interpreter that is made to look like the
Windows application before anything of ours is imported:

* `fcntl`, `pwd`, `grp`, `resource`, `pam` and `PAM` cannot be imported
  (`sys.modules[name] = None`), as on Windows;
* `platform_compat` is imported while `os.name` reads "nt", so
  `platform_compat.IS_WINDOWS` is True for everything that asks it;
* the environment is what the Windows launcher sets (see `_launcher_env`).

On real Windows nothing is faked and the same scenarios run natively. The
child prints one JSON document of observations; the tests assert on it.
No network: imaplib is replaced by an in-memory mailbox. No real accounts.

Run the whole file with `python3 -m pytest tests/test_windows_compat_app.py`.
"""

import json
import os
import subprocess
import sys
import tempfile

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
BLOCKED = ('fcntl', 'pwd', 'grp', 'resource', 'pam', 'PAM')
# What must never be started on Windows.
FORBIDDEN_TOOLS = ('systemctl', 'journalctl', 'mbsync', 'sudo', 'getent',
                   'du', 'rm', 'runuser', 'su')

ALICE = 'alice'
BOB = 'bob'
PASSWORD = 'not-a-real-password'            # builtin login of the test users
ACCOUNT = 'alice.archive@mail.example'
BAD_ACCOUNT = 'alice.locked@mail.example'
IMAP_HOST = 'imap.mail.example'
COLON_FOLDER = 'Lists: dev'


# ---------------------------------------------------------------------
# Child side
# ---------------------------------------------------------------------

def _become_windows():
    """Block the POSIX-only modules and load platform_compat as Windows."""
    for name in BLOCKED:
        sys.modules[name] = None
    # Import what platform_compat (and the stdlib behind it) needs while
    # os.name is still the real one.
    import getpass, shutil, pathlib, tempfile as _t, subprocess as _s  # noqa: F401,E401
    sys.path.insert(0, ROOT)
    if os.name == 'nt':
        import platform_compat
    else:
        real = os.name
        os.name = 'nt'
        try:
            import platform_compat
        finally:
            os.name = real
    assert platform_compat.IS_WINDOWS
    return platform_compat


def _launcher_env(data_dir, auth='builtin'):
    """The environment contract of the Windows launcher."""
    os.makedirs(data_dir, exist_ok=True)
    # The launcher keeps its certificate folder beside the users' folders.
    os.makedirs(os.path.join(data_dir, 'certs'), exist_ok=True)
    secret_file = os.path.join(data_dir, '.secret_key')
    if not os.path.exists(secret_file):
        import secrets
        with open(secret_file, 'w', encoding='utf-8') as f:
            f.write(secrets.token_hex(32))
    for name in [n for n in os.environ if n.startswith('MAIL_ARCHIVER_')]:
        del os.environ[name]
    os.environ['MAIL_ARCHIVER_DATA'] = data_dir
    os.environ['MAIL_ARCHIVER_PORT'] = '8400'
    os.environ['MAIL_ARCHIVER_SECRET_FILE'] = secret_file
    if auth:
        os.environ['MAIL_ARCHIVER_AUTH'] = auth


def _message(n, folder):
    return (f'From: Sender {n} <sender{n}@mail.example>\r\n'
            f'To: {ACCOUNT}\r\n'
            f'Subject: Quarterly zebra report {n} ({folder})\r\n'
            f'Date: Sat, 14 Nov 2026 10:0{n}:00 +0000\r\n'
            f'Message-ID: <{n}.{abs(hash(folder)) % 9999}@mail.example>\r\n'
            f'\r\n'
            f'Body of message {n} about the zebra migration.\r\n').encode('utf-8')


MAILBOX = {
    'INBOX': [_message(1, 'INBOX'), _message(2, 'INBOX')],
    COLON_FOLDER: [_message(3, 'lists')],
}


def _install_fake_imap(connections):
    """Replace imaplib.IMAP4_SSL with an in-memory server."""
    import imaplib

    class FakeIMAP:
        def __init__(self, host, port=993, *a, **kw):
            connections.append((host, int(port)))
            self.box = None

        def login(self, user, password):
            if user == BAD_ACCOUNT:
                raise imaplib.IMAP4.error('[AUTHENTICATIONFAILED] rejected')
            return 'OK', [b'logged in']

        def list(self):
            return 'OK', [f'(\\HasNoChildren) "/" "{name}"'.encode('utf-8')
                          for name in MAILBOX]

        def select(self, name, readonly=True):
            self.box = name.strip('"')
            return 'OK', [str(len(MAILBOX[self.box])).encode('ascii')]

        def uid(self, command, *args):
            msgs = MAILBOX[self.box]
            if command == 'search':
                return 'OK', [' '.join(str(i + 1) for i in range(len(msgs))).encode('ascii')]
            raw = msgs[int(args[0]) - 1]
            return 'OK', [(b'1 (RFC822 {%d}' % len(raw), raw), b')']

        def logout(self):
            return 'BYE', [b'']

    imaplib.IMAP4_SSL = FakeIMAP
    imaplib.IMAP4 = type('FakeIMAPPlain', (FakeIMAP,), {'error': imaplib.IMAP4.error})


def _record_spawns(spawned):
    """Note the program of every child process the scenario starts."""
    import subprocess as sp
    real_init = sp.Popen.__init__

    def init(self, args, *a, **kw):
        argv = [args] if isinstance(args, (str, bytes)) else list(args)
        spawned.append(os.path.basename(str(argv[0])))
        return real_init(self, args, *a, **kw)

    sp.Popen.__init__ = init


def _page(client, method, url, **kw):
    kw.setdefault('follow_redirects', False)
    r = getattr(client, method)(url, **kw)
    return {'status': r.status_code, 'body': r.get_data(as_text=True),
            'location': r.headers.get('Location', '')}


def scenario_builtin(data_dir):
    import time
    pc = _become_windows()
    _launcher_env(data_dir)
    spawned, connections = [], []
    _record_spawns(spawned)
    _install_fake_imap(connections)
    out = {'spawned': spawned, 'connections': connections}

    import app as ma
    import admin
    import imap_sync
    import layout
    import mail_batcher
    import oauth2_microsoft as oauth
    import search_index
    import sync_lifecycle as slc
    out['imported'] = sorted(m.__name__ for m in (
        ma, admin, imap_sync, layout, mail_batcher, oauth, search_index, slc))
    out['blocked_still_blocked'] = all(sys.modules.get(n) is None for n in BLOCKED)
    out['config'] = {'auth_mode': ma.CONFIG['auth_mode'],
                     'data_dir': ma.CONFIG['data_dir'],
                     'listen_port': ma.CONFIG['listen_port']}
    out['job_state_dir'] = slc.JOB_STATE_DIR
    out['version'] = layout.read_version()
    out['messages'] = {'restart': admin.WINDOWS_RESTART_MESSAGE,
                       'service': admin.WINDOWS_SERVICE_MESSAGE,
                       'host': admin.WINDOWS_HOST_MESSAGE}

    # mbsync is never the backend on Windows, even when one is on PATH.
    real_which = ma.shutil.which
    ma.shutil.which = lambda name, *a, **kw: os.path.join(data_dir, name)
    out['has_mbsync_with_mbsync_on_path'] = ma._has_mbsync()
    ma.shutil.which = real_which

    client = ma.app.test_client()
    other = ma.app.test_client()

    # -- builtin auth: register, log out, log in --------------------------
    out['register'] = _page(client, 'post', '/register',
                            data={'username': ALICE, 'password': PASSWORD})
    out['logout'] = _page(client, 'get', '/logout')
    out['dashboard_logged_out'] = _page(client, 'get', '/dashboard')
    out['login_wrong'] = _page(client, 'post', '/login',
                               data={'username': ALICE, 'password': 'nope-nope'})
    out['login'] = _page(client, 'post', '/login',
                         data={'username': ALICE, 'password': PASSWORD})
    out['dashboard'] = _page(client, 'get', '/dashboard')
    out['about'] = _page(client, 'get', '/about')
    out['search_empty'] = _page(client, 'get', '/search')

    # -- admin routes: 403 for a non-admin --------------------------------
    out['admin_non_admin'] = {
        'index': _page(client, 'get', '/admin/'),
        'storage': _page(client, 'get', '/admin/storage'),
        'host': _page(client, 'get', '/admin/host'),
        'app': _page(client, 'get', '/admin/app'),
        'restart': _page(client, 'post', '/admin/restart'),
        'cancel': _page(client, 'post', '/admin/sync/sync-x-all-00000000/cancel'),
    }

    # -- accounts ----------------------------------------------------------
    out['add_account'] = _page(client, 'post', '/account/add', data={
        'provider': 'custom', 'email': ACCOUNT, 'credential': 'app-password-placeholder',
        'custom_host': IMAP_HOST, 'custom_port': '993'})
    out['add_bad_account'] = _page(client, 'post', '/account/add', data={
        'provider': 'custom', 'email': BAD_ACCOUNT, 'credential': 'app-password-placeholder',
        'custom_host': IMAP_HOST, 'custom_port': '993'})
    out['accounts'] = [a['email'] for a in ma.load_accounts(ALICE)]
    out['credential_roundtrip'] = (
        ma.load_credential(ALICE, ACCOUNT) == 'app-password-placeholder')
    out['schedule_post'] = _page(client, 'post', f'/account/{ACCOUNT}/schedule',
                                 data={'sync_interval': 'hourly'})
    out['schedule_plan'] = [
        {'email': r['email'], 'interval': r['interval'], 'server': r['server'],
         'slot_ok': isinstance(r['slot'], int) and 0 <= r['slot'] <= 23}
        for r in ma.sync_schedule_plan()]

    # -- sync through imap_sync (imaplib), by the WebUI route -------------
    out['sync_post'] = _page(client, 'post', f'/sync/{ACCOUNT}',
                             headers={'Accept': 'application/json'})
    job = {}
    try:
        job_id = json.loads(out['sync_post']['body'])['job_id']
        deadline = time.time() + 30
        while time.time() < deadline:
            job = json.loads(_page(client, 'get', f'/api/sync/{job_id}')['body'])
            if job.get('state') in ('done', 'error', 'cancelled'):
                break
            time.sleep(0.1)
    except (ValueError, KeyError) as e:
        job = {'state': f'no job: {e}'}
    out['sync_job'] = {'state': job.get('state'), 'error': job.get('error')}
    out['sync_status'] = ma.get_sync_status(ALICE).get(ACCOUNT, {})
    # A second run finds nothing new and keeps the names it wrote.
    out['second_sync'] = ma.run_sync(ALICE, ACCOUNT)
    out['bad_sync'] = ma.run_sync(ALICE, BAD_ACCOUNT)
    acct_dir = os.path.join(data_dir, ALICE, ma._bulk.safe_account_name(ACCOUNT))
    names = []
    for root, _dirs, files in os.walk(os.path.join(data_dir, ALICE)):
        rel = os.path.relpath(root, data_dir)
        names.extend(os.path.join(rel, f) for f in files)
        names.append(rel)
    out['account_folders'] = sorted(os.listdir(acct_dir)) if os.path.isdir(acct_dir) else []
    out['names_with_colon'] = sorted(n for n in names if ':' in n)
    out['messages_on_disk'] = sum(
        len(files) for root, _d, files in os.walk(acct_dir)
        if os.path.basename(root) == 'cur')
    out['scheduled_sync_runs'] = None
    try:
        ma.scheduled_sync()
        out['scheduled_sync_runs'] = True
    except Exception as e:                      # reported, asserted on
        out['scheduled_sync_runs'] = f'{type(e).__name__}: {e}'
    out['certs_dir_after_schedule'] = sorted(os.listdir(os.path.join(data_dir, 'certs')))
    out['seconds_to_next_hour'] = ma._seconds_to_next_hour()
    stop = ma.start_background_scheduler()
    out['scheduler_idempotent'] = ma.start_background_scheduler() is stop
    stop.set()

    # -- search, viewer, export -------------------------------------------
    out['search'] = _page(client, 'get', '/search?q=zebra')
    hits = search_index.search_fts(ALICE, data_dir, 'zebra').get('results', [])
    out['search_hits'] = len(hits)
    path = hits[0]['filepath'] if hits else ''
    out['view'] = _page(client, 'get', '/email/view', query_string={'path': path})
    out['view_outside'] = _page(client, 'get', '/email/view',
                                query_string={'path': os.path.join(data_dir, '.secret_key')})
    r = client.get('/search?q=zebra&format=mbox')
    out['export_mbox'] = {'status': r.status_code, 'starts': r.data[:5].decode('latin-1'),
                          'messages': r.data.count(b'\nSubject: Quarterly zebra')}
    r = client.get('/search?q=zebra&format=eml')
    out['export_eml'] = {'status': r.status_code, 'starts': r.data[:2].decode('latin-1')}
    out['grep_fallback_hits'] = len(ma.search_maildir(ALICE, 'zebra'))
    out['rebuild'] = _page(client, 'post', '/index/rebuild')

    # -- every sidebar page renders ----------------------------------------
    pages = {}
    with ma.app.test_request_context():
        from flask import url_for
        # the first registered endpoint of each entry, as the sidebar does
        endpoints = [next(e for e in eps if e in ma.app.view_functions)
                     for _label, eps in layout.NAV_BEFORE_ADMIN + layout.NAV_AFTER_ADMIN
                     if any(e in ma.app.view_functions for e in eps)]
        endpoints = [e for e in endpoints if e != 'logout']
        urls = {e: url_for(e) for e in endpoints}
    for endpoint, url in urls.items():
        pages[endpoint] = _page(client, 'get', url)['status']
    out['sidebar_pages'] = pages

    # -- admin: refused panels for an administrator ------------------------
    out['register_bob'] = _page(other, 'post', '/register',
                                data={'username': BOB, 'password': PASSWORD})
    os.environ[admin.ADMIN_USERS_ENV] = ALICE
    admin._ADMIN_CHECK_CACHE.clear()
    out['admin_as_admin'] = {
        'index': _page(client, 'get', '/admin/'),
        'storage': _page(client, 'get', '/admin/storage'),
        'host': _page(client, 'get', '/admin/host'),
        'app': _page(client, 'get', '/admin/app'),
        'restart': _page(client, 'post', '/admin/restart'),
        'cancel_unknown': _page(client, 'post', '/admin/sync/sync-x-all-00000000/cancel'),
    }
    out['admin_bob'] = _page(other, 'get', '/admin/')
    out['restart_marker_written'] = os.path.exists(admin.RESTART_REQUEST_MARKER)

    # -- sync lifecycle state ----------------------------------------------
    first, blocker = slc.claim_sync(ALICE, ACCOUNT, {'state': 'running',
                                                     'owner_pid': os.getpid()})
    second, blocker2 = slc.claim_sync(ALICE, ACCOUNT, {'state': 'running',
                                                       'owner_pid': os.getpid()})
    out['claim'] = {'first': bool(first), 'second': second,
                    'blocked_by_first': bool(blocker2) and blocker2.get('job_id') == first,
                    'stop_without_pid': slc.stop_scope(first),
                    'pid_alive_self': slc._pid_alive(os.getpid()),
                    'boot_id': slc.current_boot_id()}
    slc.update_state(first, state='done')
    out['claim']['state_after_update'] = (slc.read_state(first) or {}).get('state')

    # -- OAuth token storage -----------------------------------------------
    oauth.save_oauth2_tokens(data_dir, ALICE, ACCOUNT, {
        'access_token': 'access-placeholder', 'refresh_token': 'refresh-placeholder',
        'expires_in': 3600, 'scope': 'IMAP.AccessAsUser.All'})
    oauth.save_oauth2_tokens(data_dir, ALICE, ACCOUNT, {     # replaces existing files
        'access_token': 'access-placeholder-2', 'refresh_token': 'refresh-placeholder',
        'expires_in': 3600})
    out['oauth'] = {
        'fresh': oauth.ensure_fresh_token(None, data_dir, ALICE, ACCOUNT) == 'access-placeholder-2',
        'user_config_saved': oauth.save_user_oauth2_config(data_dir, ALICE, {'apps': {}}) is None,
    }

    # -- platform helpers as the Windows build sees them -------------------
    lock_path = os.path.join(data_dir, 'probe.lock')
    with open(lock_path, 'a+', encoding='utf-8') as lf:
        pc.lock_file(lf, exclusive=True, blocking=False)
        pc.unlock_file(lf)
        pc.lock_fd(lf.fileno(), exclusive=False, blocking=True)
        pc.unlock_fd(lf.fileno())
    out['helpers'] = {
        'locks_ok': True,
        'maildir_info': pc.maildir_info('S'),
        'split_windows': list(pc.split_maildir_name('1700000000.1.host!2,RS')),
        'split_linux': list(pc.split_maildir_name('1700000000.1.host:2,RS')),
        'safe_component': pc.safe_path_component('Lists: dev?'),
        'reserved': pc.safe_path_component('CON'),
        'user_groups': pc.user_groups(ALICE),
        'is_root': pc.is_root(),
        'current_uid': pc.current_uid(),
        'supports_system_accounts': pc.supports_system_accounts(),
        'runtime_dir': pc.runtime_dir(),
        'child_env_keys_ok': 'MAIL_ARCHIVER_SECRET_FILE' not in pc.minimal_child_env(),
    }
    return out


def scenario_pam(data_dir):
    _become_windows()
    _launcher_env(data_dir, auth='pam')
    import app as ma
    out = {'message': ma.PAM_UNAVAILABLE_MESSAGE,
           'auth_mode': ma.CONFIG['auth_mode'],
           'pam_authenticate': ma.pam_authenticate('someone', 'whatever')}
    client = ma.app.test_client()
    out['login_get'] = _page(client, 'get', '/login')
    out['login_post'] = _page(client, 'post', '/login',
                              data={'username': 'someone', 'password': 'whatever'})
    out['dashboard'] = _page(client, 'get', '/dashboard')
    out['register'] = _page(client, 'get', '/register')
    out['blocked_still_blocked'] = all(sys.modules.get(n) is None for n in BLOCKED)
    return out


def scenario_defaults(data_dir):
    """Nothing set but APPDATA: what a bare `python app.py` would use."""
    pc = _become_windows()
    for name in [n for n in os.environ if n.startswith('MAIL_ARCHIVER_')]:
        del os.environ[name]
    os.environ['APPDATA'] = data_dir
    import app as ma
    import admin
    import sync_lifecycle as slc
    expected = os.path.join(data_dir, 'MailArchiver')
    return {'auth_mode': ma.CONFIG['auth_mode'],
            'data_dir_ok': ma.CONFIG['data_dir'] == expected,
            'default_data_dir_ok': pc.default_data_dir() == expected,
            'job_state_dir_ok': slc.JOB_STATE_DIR == os.path.join(expected, '.run', 'sync-jobs'),
            'restart_marker_ok': admin.RESTART_REQUEST_MARKER == os.path.join(
                expected, '.run', 'restart-requested'),
            'encryption_refused': _raises(ma._get_encryption_key, RuntimeError)}


def _raises(fn, exc):
    try:
        fn()
    except exc:
        return True
    return False


SCENARIOS = {'builtin': scenario_builtin, 'pam': scenario_pam,
             'defaults': scenario_defaults}


def _child_main(argv):
    name, data_dir, result_file = argv
    result = SCENARIOS[name](data_dir)
    with open(result_file, 'w', encoding='utf-8') as f:
        json.dump(result, f)
    return 0


# ---------------------------------------------------------------------
# Test side
# ---------------------------------------------------------------------

def _run_scenario(name):
    with tempfile.TemporaryDirectory(prefix='ma-wincompat-') as tmp:
        data_dir = os.path.join(tmp, 'data')
        result_file = os.path.join(tmp, 'result.json')
        env = {k: v for k, v in os.environ.items() if not k.startswith('MAIL_ARCHIVER_')}
        env['PYTHONDONTWRITEBYTECODE'] = '1'
        env['PYTHONIOENCODING'] = 'utf-8'
        proc = subprocess.run(
            [sys.executable, os.path.abspath(__file__), '--child', name, data_dir, result_file],
            cwd=ROOT, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, timeout=300)
        output = proc.stdout.decode('utf-8', 'replace')
        assert proc.returncode == 0, f'{name} scenario failed:\n{output[-6000:]}'
        with open(result_file, encoding='utf-8') as f:
            return json.load(f)


@pytest.fixture(scope='module')
def win():
    return _run_scenario('builtin')


@pytest.fixture(scope='module')
def pam():
    return _run_scenario('pam')


@pytest.fixture(scope='module')
def defaults():
    return _run_scenario('defaults')


# -- import ----------------------------------------------------------------

def test_core_modules_import_without_the_posix_only_modules(win):
    assert win['imported'] == ['admin', 'app', 'imap_sync', 'layout', 'mail_batcher',
                               'oauth2_microsoft', 'search_index', 'sync_lifecycle']
    assert win['blocked_still_blocked'] is True


def test_launcher_environment_is_honoured(win):
    assert win['config']['auth_mode'] == 'builtin'
    assert win['config']['listen_port'] == 8400
    data_dir = win['config']['data_dir']
    assert win['job_state_dir'] == os.path.join(data_dir, '.run', 'sync-jobs')
    assert win['helpers']['runtime_dir'] == os.path.join(data_dir, '.run')


def test_defaults_without_any_setting(defaults):
    assert defaults == {'auth_mode': 'builtin', 'data_dir_ok': True,
                        'default_data_dir_ok': True, 'job_state_dir_ok': True,
                        'restart_marker_ok': True, 'encryption_refused': True}


# -- builtin auth, pages ----------------------------------------------------

def test_register_and_login_with_builtin_auth(win):
    assert win['register']['status'] == 302
    assert win['register']['location'].endswith('/dashboard')
    assert win['dashboard_logged_out']['status'] == 302
    assert win['dashboard_logged_out']['location'].endswith('/login')
    assert win['login_wrong']['status'] == 200
    assert 'Invalid username or password.' in win['login_wrong']['body']
    assert win['login']['status'] == 302
    assert win['login']['location'].endswith('/dashboard')


def test_dashboard_renders_with_sidebar_and_footer_version(win):
    page = win['dashboard']
    assert page['status'] == 200
    for label in ('Dashboard', 'Search archive', 'Add email account', 'About', 'Log out'):
        assert label in page['body']
    assert f'DEC-LLC Mail-Archiver {win["version"]}' in page['body']
    assert 'sudo systemctl restart' not in page['body']    # the upgrade banner


def test_about_page_shows_the_version_and_the_imaplib_backend(win):
    page = win['about']
    assert page['status'] == 200
    assert win['version'] != 'unknown'
    assert f'<td id="about-version">{win["version"]}</td>' in page['body']
    assert 'built-in IMAP client (imaplib)' in page['body']
    assert 'not used on Windows' in page['body']


def test_every_sidebar_page_renders(win):
    assert win['sidebar_pages'], 'no sidebar endpoints found'
    assert {e: s for e, s in win['sidebar_pages'].items() if s != 200} == {}


# -- accounts, sync, schedule -------------------------------------------------

def test_accounts_are_saved_with_their_credential(win):
    assert win['add_account']['status'] == 302
    assert win['accounts'] == [ACCOUNT, BAD_ACCOUNT]
    assert win['credential_roundtrip'] is True


def test_schedule_is_stored_and_planned(win):
    assert win['schedule_post']['status'] == 302
    row = [r for r in win['schedule_plan'] if r['email'] == ACCOUNT]
    assert row == [{'email': ACCOUNT, 'interval': 'hourly', 'server': IMAP_HOST,
                    'slot_ok': True}]
    assert win['scheduled_sync_runs'] is True
    # the launcher's certs folder is not taken for a user
    assert win['certs_dir_after_schedule'] == []
    assert 1 <= win['seconds_to_next_hour'] <= 3600
    assert win['scheduler_idempotent'] is True


def test_sync_uses_imaplib_never_mbsync(win):
    assert win['has_mbsync_with_mbsync_on_path'] is False
    assert win['sync_post']['status'] == 202
    assert win['sync_job'] == {'state': 'done', 'error': ''}
    assert win['sync_status']['state'] == 'ok'
    assert win['sync_status']['new_messages'] == 3
    # the account's own host, not the (empty) host of the 'custom' provider
    assert [IMAP_HOST, 993] in win['connections']
    assert win['second_sync']['state'] == 'ok'
    assert win['second_sync']['new_messages'] == 0
    assert win['messages_on_disk'] == 3


def test_a_refused_login_is_an_error_not_a_clean_sync(win):
    assert win['bad_sync']['state'] == 'error'
    assert 'AUTHENTICATIONFAILED' in win['bad_sync']['error']


def test_nothing_on_disk_has_a_colon_in_its_name(win):
    # NTFS refuses ':'; the IMAP folder "Lists: dev" must still be archived.
    assert win['names_with_colon'] == []
    assert win['account_folders'] == ['INBOX', 'Lists__dev']


# -- search, viewer, export ------------------------------------------------

def test_search_viewer_and_export(win):
    assert win['search_empty']['status'] == 200
    assert win['search']['status'] == 200
    assert 'Quarterly zebra report' in win['search']['body']
    assert win['search_hits'] == 3
    assert win['view']['status'] == 200
    assert 'Quarterly zebra report' in win['view']['body']
    assert win['view_outside']['status'] == 403
    assert win['export_mbox'] == {'status': 200, 'starts': 'From ', 'messages': 3}
    assert win['export_eml'] == {'status': 200, 'starts': 'PK'}
    assert win['grep_fallback_hits'] == 3
    assert win['rebuild']['status'] == 302


# -- admin ---------------------------------------------------------------------

def test_admin_routes_return_403_for_a_non_admin(win):
    for name, page in win['admin_non_admin'].items():
        assert page['status'] == 403, name
        assert 'Admin access required' in page['body'], name
        assert 'usermod' not in page['body'], name
        assert 'MAIL_ARCHIVER_ADMIN_USERS' in page['body'], name
    assert win['register_bob']['status'] == 302
    assert win['admin_bob']['status'] == 403


def test_restart_is_refused_with_its_message(win):
    restart = win['admin_as_admin']['restart']
    assert restart['status'] == 409
    assert json.loads(restart['body']) == {'status': 'unsupported',
                                           'detail': win['messages']['restart']}
    assert win['messages']['restart'] == (
        'Restart is not available on Windows. Close Mail Archiver and start '
        'it again to restart it.')
    assert win['restart_marker_written'] is False


def test_systemd_backed_panels_show_their_refusal(win):
    index = win['admin_as_admin']['index']
    assert index['status'] == 200
    assert win['messages']['service'] in index['body']
    assert win['messages']['restart'] in index['body']
    assert win['messages']['host'] in index['body']
    assert 'id="restart-btn"' not in index['body']
    assert 'usermod' not in index['body']
    host = win['admin_as_admin']['host']
    assert host['status'] == 200
    assert win['messages']['host'] in host['body']
    assert win['admin_as_admin']['storage']['status'] == 200
    assert '/opt/mail-archiver' not in win['admin_as_admin']['storage']['body']
    assert '/var/log' not in win['admin_as_admin']['storage']['body']
    assert win['admin_as_admin']['app']['status'] == 200
    assert win['admin_as_admin']['cancel_unknown']['status'] == 404


def test_no_page_is_a_500_and_nothing_shells_out(win):
    statuses = {}
    for key, value in win.items():
        if isinstance(value, dict) and 'status' in value:
            statuses[key] = value['status']
        elif isinstance(value, dict):
            for sub, page in value.items():
                if isinstance(page, dict) and 'status' in page:
                    statuses[f'{key}.{sub}'] = page['status']
    assert {k: s for k, s in statuses.items() if s >= 500} == {}
    assert len(statuses) >= 25
    assert [p for p in win['spawned'] if os.path.splitext(p)[0] in FORBIDDEN_TOOLS] == []


# -- PAM -----------------------------------------------------------------------

def test_pam_login_is_refused_with_a_clear_message(pam):
    assert pam['auth_mode'] == 'pam'
    assert pam['message'] == (
        'PAM (system account) sign-in is not available on Windows. Start Mail '
        'Archiver with MAIL_ARCHIVER_AUTH=builtin and create a Mail Archiver '
        'account instead.')
    assert pam['pam_authenticate'] is False
    assert pam['login_get']['status'] == 200
    assert pam['message'] in pam['login_get']['body']
    assert pam['login_post']['status'] == 200
    assert pam['message'] in pam['login_post']['body']
    assert 'Invalid username or password.' not in pam['login_post']['body']
    assert pam['dashboard']['status'] == 302          # nobody got a session
    assert pam['register']['status'] == 404           # builtin mode only
    assert pam['blocked_still_blocked'] is True       # pam / PAM never imported


# -- state, tokens, helpers ---------------------------------------------------

def test_sync_job_state_and_the_overlap_guard(win):
    assert win['claim'] == {'first': True, 'second': None, 'blocked_by_first': True,
                            'stop_without_pid': False, 'pid_alive_self': True,
                            'boot_id': '', 'state_after_update': 'done'}


def test_oauth_token_files_are_replaced_in_place(win):
    assert win['oauth'] == {'fresh': True, 'user_config_saved': True}


def test_platform_helpers_in_windows_mode(win):
    helpers = win['helpers']
    assert helpers['locks_ok'] is True
    assert helpers['maildir_info'] == '!2,S'
    assert helpers['split_windows'] == ['1700000000.1.host', 'RS']
    assert helpers['split_linux'] == ['1700000000.1.host', 'RS']
    assert helpers['safe_component'] == 'Lists_ dev_'
    assert helpers['reserved'] == 'CON_'
    assert helpers['user_groups'] == []
    assert helpers['is_root'] is False
    assert helpers['current_uid'] == -1
    assert helpers['supports_system_accounts'] is False
    assert helpers['child_env_keys_ok'] is True


if __name__ == '__main__':
    if len(sys.argv) == 5 and sys.argv[1] == '--child':
        sys.exit(_child_main(sys.argv[2:]))
    sys.exit(pytest.main([os.path.abspath(__file__)] + sys.argv[1:]))
