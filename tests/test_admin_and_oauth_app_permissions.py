"""1.3.3: two permission rules tightened before the public release.

1. With built-in sign-in, administrators are the names in
   MAIL_ARCHIVER_ADMIN_USERS on every platform. On Linux the check used to
   look up a *system* account of the same name in the admin group; built-in
   registration is open, so registering an administrator's login name gave
   the new built-in user the admin pages.
2. Server-wide OAuth apps (client id + secret used for every user's
   Microsoft sign-in) could be added, edited, deleted or made the default
   by any signed-in user. Now only an administrator can write them; private
   apps are unchanged.
"""

import os
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

_DATA = tempfile.mkdtemp(prefix='ma-test-data-')
os.environ.setdefault('MAIL_ARCHIVER_DATA', _DATA)
os.environ['MAIL_ARCHIVER_AUTH'] = 'builtin'
os.environ.setdefault('MAIL_ARCHIVER_SECRET', 'test-only-secret')
os.environ.pop('MAIL_ARCHIVER_SECRET_FILE', None)

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import admin                 # noqa: E402
import app as ma             # noqa: E402
import oauth2_microsoft as om  # noqa: E402


@pytest.fixture(autouse=True)
def _fresh(tmp_path, monkeypatch):
    monkeypatch.setitem(ma.CONFIG, 'data_dir', str(tmp_path))
    admin._ADMIN_CHECK_CACHE.clear()
    yield
    admin._ADMIN_CHECK_CACHE.clear()


# ---- 1. administrators under built-in sign-in ------------------------------

def _system_admin_named(monkeypatch, name):
    """A system account `name` exists and is in the admin group."""
    monkeypatch.setattr(admin._pc, 'supports_system_accounts', lambda: True)
    monkeypatch.setattr(admin._pc, 'group_info',
                        lambda g: SimpleNamespace(gid=27, members=[name]))
    monkeypatch.setattr(admin._pc, 'user_info',
                        lambda u: SimpleNamespace(uid=1000, gid=1000) if u == name else None)


def test_builtin_user_is_not_admin_because_a_system_admin_has_that_name(monkeypatch):
    _system_admin_named(monkeypatch, 'sysadmin')
    monkeypatch.setenv('MAIL_ARCHIVER_AUTH', 'builtin')
    monkeypatch.delenv('MAIL_ARCHIVER_ADMIN_USERS', raising=False)
    assert admin._is_admin('sysadmin') is False


def test_builtin_admins_are_the_configured_names(monkeypatch):
    _system_admin_named(monkeypatch, 'sysadmin')
    monkeypatch.setenv('MAIL_ARCHIVER_AUTH', 'builtin')
    monkeypatch.setenv('MAIL_ARCHIVER_ADMIN_USERS', 'carol, dave')
    assert admin._is_admin('carol') is True
    assert admin._is_admin('sysadmin') is False


def test_pam_sign_in_still_uses_the_admin_group(monkeypatch):
    _system_admin_named(monkeypatch, 'sysadmin')
    monkeypatch.setenv('MAIL_ARCHIVER_AUTH', 'pam')
    monkeypatch.setenv('MAIL_ARCHIVER_ADMIN_USERS', 'carol')
    assert admin._is_admin('sysadmin') is True
    assert admin._is_admin('carol') is False        # the setting is for built-in sign-in


# ---- 2. server-wide OAuth apps ---------------------------------------------

def _client(username):
    ma.app.config['TESTING'] = True
    client = ma.app.test_client()
    with client.session_transaction() as sess:
        sess['username'] = username
        sess['login_time'] = time.time()
    return client


def _server_apps():
    return (om.load_oauth2_config(ma.CONFIG['data_dir']).get('apps') or {})


def _user_apps(username):
    return (om.load_user_oauth2_config(ma.CONFIG['data_dir'], username).get('apps') or {})


FORM = {'provider': 'microsoft', 'name': 'Example app', 'client_id': 'cid-123',
        'client_secret': 'secret-456', 'tenant': 'common'}


@pytest.fixture
def admins(monkeypatch):
    monkeypatch.setattr(admin, '_is_admin', lambda u: u == 'adminuser')


def _make_server_app():
    resp = _client('adminuser').post('/oauth2/apps/new', data=dict(FORM, scope='server'))
    assert resp.status_code in (302, 303)
    apps = _server_apps()
    assert len(apps) == 1
    return next(iter(apps))


def test_ordinary_user_cannot_add_a_server_wide_app(admins):
    resp = _client('alice').post('/oauth2/apps/new', data=dict(FORM, scope='server'))
    assert resp.status_code in (302, 303)
    assert _server_apps() == {}


def test_ordinary_user_can_still_add_a_private_app(admins):
    resp = _client('alice').post('/oauth2/apps/new', data=dict(FORM, scope='user'))
    assert resp.status_code in (302, 303)
    assert len(_user_apps('alice')) == 1
    assert _server_apps() == {}


def test_administrator_can_add_a_server_wide_app(admins):
    assert _make_server_app().startswith('srv-')


def test_ordinary_user_cannot_edit_delete_or_promote_a_server_wide_app(admins):
    app_id = _make_server_app()
    before = dict(_server_apps()[app_id])
    alice = _client('alice')
    alice.post(f'/oauth2/apps/{app_id}/edit',
               data=dict(FORM, name='Taken over', client_id='evil', client_secret='evil'))
    assert _server_apps()[app_id] == before
    alice.post(f'/oauth2/apps/{app_id}/delete')
    assert app_id in _server_apps()
    defaults_before = dict(om.load_oauth2_config(ma.CONFIG['data_dir']).get('defaults') or {})
    alice.post(f'/oauth2/apps/{app_id}/set_default')
    assert (om.load_oauth2_config(ma.CONFIG['data_dir']).get('defaults') or {}) == defaults_before
    # the edit form with the app's details is not shown either
    page = alice.get(f'/oauth2/apps/{app_id}/edit')
    assert page.status_code in (302, 303)


def test_administrator_can_edit_a_server_wide_app(admins):
    app_id = _make_server_app()
    _client('adminuser').post(f'/oauth2/apps/{app_id}/edit',
                              data=dict(FORM, name='Renamed', client_secret=''))
    assert _server_apps()[app_id]['name'] == 'Renamed'
    assert _server_apps()[app_id]['client_secret'] == 'secret-456'    # blank keeps it


# ---- 3. registration can be closed; session cookie flags --------------------

def test_registration_is_open_by_default_and_can_be_closed(monkeypatch):
    monkeypatch.setitem(ma.CONFIG, 'auth_mode', 'builtin')
    ma.app.config['TESTING'] = True
    client = ma.app.test_client()
    monkeypatch.delenv('MAIL_ARCHIVER_REGISTRATION', raising=False)
    assert client.get('/register').status_code == 200
    assert b'Create one' in client.get('/login').data

    monkeypatch.setenv('MAIL_ARCHIVER_REGISTRATION', 'closed')
    assert client.get('/register').status_code in (302, 303)
    resp = client.post('/register', data={'username': 'mallory', 'password': 'longenough'})
    assert resp.status_code in (302, 303)
    assert 'mallory' not in ma._load_users()
    assert b'Create one' not in client.get('/login').data


def test_session_cookie_is_samesite_lax_and_httponly(monkeypatch):
    monkeypatch.setitem(ma.CONFIG, 'auth_mode', 'builtin')
    monkeypatch.delenv('MAIL_ARCHIVER_REGISTRATION', raising=False)
    client = ma.app.test_client()
    resp = client.post('/register', data={'username': 'cookieuser', 'password': 'longenough'})
    cookie = resp.headers.get('Set-Cookie', '')
    assert 'SameSite=Lax' in cookie and 'HttpOnly' in cookie


def test_password_check_is_constant_time():
    src = (ROOT / 'app.py').read_text(encoding='utf-8')
    assert 'hmac.compare_digest(_hash_password(password, salt), stored)' in src
    assert ma._verify_password('pw-123456', ma._hash_password('pw-123456')) is True
    assert ma._verify_password('wrong', ma._hash_password('pw-123456')) is False
