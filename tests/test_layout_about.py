"""1.3.0 shared layout: the fixed sidebar (navigation tree), the footer
version on every page, and the "About DEC-LLC Mail-Archiver" page.

Admin vs non-admin is decided by `admin._is_admin(username)` (membership
of the MAIL_ARCHIVER_ADMIN_GROUP unix group in production); the tests
patch that one function, which both `requires_admin` and the template
`is_admin` global go through.
"""

import json
import os
import re
import shutil
import sys
import tempfile
import time
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import patch

import pytest

_DATA = tempfile.mkdtemp(prefix='ma-test-data-')
os.environ.setdefault('MAIL_ARCHIVER_DATA', _DATA)
os.environ['MAIL_ARCHIVER_AUTH'] = 'builtin'
os.environ.setdefault('MAIL_ARCHIVER_SECRET', 'test-only-secret')
os.environ.pop('MAIL_ARCHIVER_SECRET_FILE', None)

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import app as ma              # noqa: E402
import admin                  # noqa: E402
import layout                 # noqa: E402
import sync_lifecycle as slc  # noqa: E402

ADMIN = 'layoutadmin'
USER = 'layoutuser'
OTHER = 'layoutother'
USER_EMAIL = 'layoutuser@example.com'
OTHER_EMAIL = 'someone.else@example.org'
VERSION = (ROOT / 'VERSION').read_text().strip()
FOOTER_TEXT = f'DEC-LLC Mail-Archiver {VERSION}'
ADMIN_ENDPOINTS = {'admin.index', 'admin.storage_detail', 'admin.host_detail',
                   'admin.app_detail'}
ABOUT_LABELS = ['Version', 'Build commit', 'Package version', 'Install date',
                'Host', 'Data path', 'Sync backend', 'isync version',
                'Schedule', 'Links']


# ---------------------------------------------------------------
# HTML parsing helpers
# ---------------------------------------------------------------

class _Page(HTMLParser):
    """Collects the sidebar's nav entries, the footer and table header
    labels from a rendered page."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.sidebars = 0
        self.nav = []          # {'tag','attrs','text'} per .nav-link element
        self.footer_tag = None  # tag carrying .footer-version
        self.footer_attrs = {}
        self.footer_text = ''
        self.th = []
        self.title = ''
        self._open = None      # element currently collecting text
        self._in_sidebar = 0

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        classes = (a.get('class') or '').split()
        if tag == 'nav' and 'sidebar' in classes:
            self.sidebars += 1
            self._in_sidebar = 1
        if self._in_sidebar and 'nav-link' in classes:
            self._open = {'tag': tag, 'attrs': a, 'text': ''}
            self.nav.append(self._open)
        elif 'footer-version' in classes:
            self.footer_tag, self.footer_attrs = tag, a
            self._open = 'footer'
        elif tag == 'th':
            self.th.append('')
            self._open = 'th'
        elif tag == 'title':
            self._open = 'title'

    def handle_endtag(self, tag):
        if tag == 'nav':
            self._in_sidebar = 0
        if tag in ('a', 'span', 'th', 'title'):
            self._open = None

    def handle_data(self, data):
        if isinstance(self._open, dict):
            self._open['text'] += data
        elif self._open == 'footer':
            self.footer_text += data
        elif self._open == 'th':
            self.th[-1] += data
        elif self._open == 'title':
            self.title += data


def parse(resp):
    page = _Page()
    page.feed(resp.get_data(as_text=True))
    return page


def nav_by_endpoint(page):
    return {e['attrs'].get('data-endpoint'): e for e in page.nav}


# ---------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------

def _write_accounts(username, emails):
    cfg = ma.get_user_config_dir(username)
    (cfg / 'accounts.json').write_text(json.dumps([
        {'email': e, 'provider': 'icloud', 'enabled': True,
         'sync_interval': 'hourly'} for e in emails]))
    (cfg / 'sync_status.json').write_text('{}')


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    ma.app.config['TESTING'] = True
    monkeypatch.setitem(ma.CONFIG, 'auth_mode', 'builtin')
    monkeypatch.setattr(admin, '_is_admin', lambda username: username == ADMIN)
    # Nothing in these tests may reach the real service.
    monkeypatch.setattr(admin, 'RESTART_REQUEST_MARKER', str(tmp_path / 'restart-requested'))
    monkeypatch.setattr(slc, 'JOB_STATE_DIR', str(tmp_path / 'jobs'))
    os.makedirs(slc.JOB_STATE_DIR, exist_ok=True)
    _write_accounts(USER, [USER_EMAIL])
    _write_accounts(OTHER, [OTHER_EMAIL])
    _write_accounts(ADMIN, [])
    yield
    # The data dir is shared with the other test modules (one `app` import
    # per pytest process): leave no scheduled accounts behind.
    for username in (USER, OTHER, ADMIN):
        shutil.rmtree(Path(ma.CONFIG['data_dir']) / username, ignore_errors=True)


def client_for(username=None):
    client = ma.app.test_client()
    if username:
        with client.session_transaction() as s:
            s['username'] = username
            s['login_time'] = time.time()
    return client


def _url(endpoint, **values):
    with ma.app.test_request_context():
        return ma.url_for(endpoint, **values)


def _registered(endpoint):
    return endpoint in ma.app.view_functions


# ---------------------------------------------------------------
# Footer version on every page
# ---------------------------------------------------------------

# (id, user, endpoint, url values). 'import' resolves to the 1.3.0 import
# page; the pre-1.3.0 PST page is a standalone template that the import
# rewrite replaces, so it is not part of the layout contract.
PAGES = [
    ('login', None, 'login', {}),
    ('register', None, 'register', {}),
    ('dashboard', USER, 'dashboard', {}),
    ('search', USER, 'search', {}),
    ('add-account', USER, 'add_account', {}),
    ('edit-account', USER, 'edit_account', {'email': USER_EMAIL}),
    ('server-settings', USER, 'oauth2_settings', {}),
    ('new-oauth-app', USER, 'new_oauth_app', {}),
    ('admin-overview', ADMIN, 'admin.index', {}),
    ('admin-storage', ADMIN, 'admin.storage_detail', {}),
    ('admin-host', ADMIN, 'admin.host_detail', {}),
    ('admin-app', ADMIN, 'admin.app_detail', {}),
    ('about', USER, 'about', {}),
    ('import', USER, 'mail_import_page', {}),
    ('bulk-sync', USER, 'bulk_sync_page', {}),
]
NOT_YET_MERGED = {'mail_import_page', 'bulk_sync_page'}


def _get_page(user, endpoint, values):
    if endpoint in NOT_YET_MERGED and not _registered(endpoint):
        pytest.skip(f'{endpoint} is not registered on this branch')
    resp = client_for(user).get(_url(endpoint, **values))
    assert resp.status_code == 200, (endpoint, resp.status_code)
    return resp


@pytest.mark.parametrize('user,endpoint,values', [p[1:] for p in PAGES],
                         ids=[p[0] for p in PAGES])
def test_footer_version_on_every_page(user, endpoint, values):
    page = parse(_get_page(user, endpoint, values))
    assert ' '.join(page.footer_text.split()) == FOOTER_TEXT
    if user:
        assert page.footer_tag == 'a'
        assert page.footer_attrs.get('href') == '/about'
    else:
        # Logged out: plain text, no link.
        assert page.footer_tag == 'span'
        assert 'href' not in page.footer_attrs


def test_every_template_extends_base():
    """A future template cannot skip the layout (and with it the sidebar
    and the footer version)."""
    allowed = {'base.html', 'pst_import.html'}
    extends = re.compile(r'^\s*{%-?\s*extends\s+["\']base\.html["\']\s*-?%}')
    templates = sorted((ROOT / 'templates').glob('*.html'))
    assert templates
    missing = [t.name for t in templates
               if t.name not in allowed and not t.name.startswith('_')
               and not extends.match(t.read_text())]
    assert missing == []


def test_base_keeps_the_four_blocks_and_no_dark_scheme():
    base = (ROOT / 'templates' / 'base.html').read_text()
    blocks = re.findall(r'{%-?\s*block\s+(\w+)', base)
    assert sorted(blocks) == ['content', 'head_extra', 'scripts', 'title']
    for path in list((ROOT / 'templates').glob('*.html')) + [ROOT / 'static' / 'style.css']:
        if path.name == 'pst_import.html':
            continue
        assert 'prefers-color-scheme' not in path.read_text(), path.name


# ---------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------

@pytest.mark.parametrize('user,endpoint,values',
                         [p[1:] for p in PAGES if p[1]],
                         ids=[p[0] for p in PAGES if p[1]])
def test_sidebar_on_every_logged_in_page(user, endpoint, values):
    page = parse(_get_page(user, endpoint, values))
    assert page.sidebars == 1
    entries = nav_by_endpoint(page)
    for required in ('dashboard', 'search', 'add_account', 'oauth2_settings',
                     'about', 'logout', *ADMIN_ENDPOINTS):
        assert required in entries, required
    current = [e for e in page.nav if e['attrs'].get('aria-current') == 'page']
    if endpoint in entries:
        assert [e['attrs']['data-endpoint'] for e in current] == [endpoint]
    else:
        assert current == []


def test_sidebar_absent_when_logged_out():
    for endpoint in ('login', 'register'):
        page = parse(client_for().get(_url(endpoint)))
        assert page.sidebars == 0 and page.nav == []


def test_sidebar_order_and_labels():
    page = parse(client_for(ADMIN).get('/dashboard'))
    labels = [' '.join(e['text'].split()) for e in page.nav]
    expected = ['Dashboard', 'Search archive', 'Import mail',
                'Initial / rebase sync', 'Add email account', 'Server settings',
                'Admin overview', 'Storage', 'Host', 'Application', 'About', 'Log out']
    if not _registered('bulk_sync_page'):
        expected.remove('Initial / rebase sync')
    assert labels == expected


def test_import_entry_prefers_new_endpoint_and_falls_back():
    entries = nav_by_endpoint(parse(client_for(USER).get('/dashboard')))
    if _registered('mail_import_page'):
        assert 'mail_import_page' in entries and 'pst_import_page' not in entries
    else:
        assert entries['pst_import_page']['attrs']['href'] == _url('pst_import_page')


def test_non_admin_admin_entries_disabled_without_href():
    for endpoint in ('dashboard', 'about', 'search'):
        entries = nav_by_endpoint(parse(client_for(USER).get(_url(endpoint))))
        for admin_endpoint in ADMIN_ENDPOINTS:
            entry = entries[admin_endpoint]
            assert entry['tag'] == 'span'
            assert entry['attrs'].get('aria-disabled') == 'true'
            assert 'href' not in entry['attrs']
            assert entry['attrs'].get('title') == 'Administrators only'
        # ... and nothing else in the sidebar links into /admin.
        for entry in parse(client_for(USER).get(_url(endpoint))).nav:
            assert not (entry['attrs'].get('href') or '').startswith('/admin')


def test_admin_admin_entries_are_links():
    entries = nav_by_endpoint(parse(client_for(ADMIN).get('/dashboard')))
    for admin_endpoint in ADMIN_ENDPOINTS:
        entry = entries[admin_endpoint]
        assert entry['tag'] == 'a'
        assert entry['attrs'].get('href') == _url(admin_endpoint)
        assert 'aria-disabled' not in entry['attrs']


def test_unregistered_endpoints_are_skipped(monkeypatch):
    monkeypatch.setattr(layout, 'NAV_BEFORE_ADMIN', (
        ('Dashboard', ('dashboard',)),
        ('Ghost', ('no_such_endpoint',)),
        ('Fallback', ('no_such_endpoint', 'search')),
        ('Needs arguments', ('edit_account',)),
    ))
    monkeypatch.setattr(layout, 'NAV_ADMIN', (
        ('Admin overview', ('admin.index',)),
        ('Admin ghost', ('admin.no_such_page',)),
    ))
    resp = client_for(USER).get('/dashboard')
    assert resp.status_code == 200
    page = parse(resp)
    assert [' '.join(e['text'].split()) for e in page.nav] == [
        'Dashboard', 'Fallback', 'Admin overview', 'About', 'Log out']
    assert nav_by_endpoint(page)['search']['attrs']['href'] == '/search'


# ---------------------------------------------------------------
# Admin routes are enforced server-side
# ---------------------------------------------------------------

def _admin_routes():
    routes = []
    for rule in ma.app.url_map.iter_rules():
        if not rule.endpoint.startswith('admin.'):
            continue
        values = {arg: ('nobody@example.com' if arg == 'email' else 'x')
                  for arg in rule.arguments}
        for method in sorted(rule.methods - {'HEAD', 'OPTIONS'}):
            routes.append((method, rule.endpoint, values))
    return routes


def test_admin_blueprint_has_the_expected_routes():
    routes = {(m, e) for m, e, _ in _admin_routes()}
    assert {('GET', e) for e in ADMIN_ENDPOINTS} <= routes
    assert ('POST', 'admin.restart') in routes
    assert len(routes) >= 7


def test_every_admin_route_403_for_non_admin():
    client = client_for(USER)
    for method, endpoint, values in _admin_routes():
        resp = client.open(_url(endpoint, **values), method=method)
        assert resp.status_code == 403, (method, endpoint, resp.status_code)
    assert not os.path.exists(admin.RESTART_REQUEST_MARKER)


def test_every_admin_route_not_403_for_admin():
    client = client_for(ADMIN)
    with patch.object(ma, '_refresh_oauth_tokens_before_sync', lambda *a, **k: {}), \
            patch.object(admin.os, 'makedirs', lambda *a, **k: None):
        for method, endpoint, values in _admin_routes():
            resp = client.open(_url(endpoint, **values), method=method)
            assert resp.status_code != 403, (method, endpoint)
            assert resp.status_code < 500, (method, endpoint, resp.status_code)


def test_admin_routes_redirect_to_login_when_logged_out():
    client = client_for()
    for method, endpoint, values in _admin_routes():
        resp = client.open(_url(endpoint, **values), method=method)
        assert resp.status_code == 302 and resp.headers['Location'].endswith('/login')


# ---------------------------------------------------------------
# About page
# ---------------------------------------------------------------

def test_about_requires_login():
    resp = client_for().get('/about')
    assert resp.status_code == 302
    assert resp.headers['Location'].endswith('/login')


def test_about_fields_and_version():
    resp = client_for(USER).get('/about')
    assert resp.status_code == 200
    page = parse(resp)
    assert page.title == 'About DEC-LLC Mail-Archiver'
    labels = [' '.join(t.split()) for t in page.th]
    for label in ABOUT_LABELS:
        assert label in labels, label
    body = resp.get_data(as_text=True)
    assert re.search(r'id="about-version">\s*%s\s*<' % re.escape(VERSION), body)
    assert ma.CONFIG['data_dir'] in body
    assert layout.ENV_STATUS_URL in body
    assert layout.RELEASE_NOTES_URL in body


def test_about_build_commit_from_build_info(tmp_path, monkeypatch):
    info = tmp_path / 'BUILD_INFO'
    info.write_text('# written by packaging/build.sh\n'
                    'commit=0123abcd4567ef890123abcd4567ef890123abcd\n'
                    'built=2026-11-14T18:30:00Z\n'
                    'builder=debian12-build\n'
                    'unrelated=ignored-value\n')
    monkeypatch.setattr(layout, 'BUILD_INFO_FILE', str(info))
    body = client_for(USER).get('/about').get_data(as_text=True)
    assert '0123abcd4567ef890123abcd4567ef890123abcd' in body
    assert '2026-11-14T18:30:00Z' in body and 'debian12-build' in body
    assert 'ignored-value' not in body


def test_about_build_commit_unknown_without_build_info(tmp_path, monkeypatch):
    monkeypatch.setattr(layout, 'BUILD_INFO_FILE', str(tmp_path / 'absent'))
    body = client_for(USER).get('/about').get_data(as_text=True)
    assert re.search(r'id="about-commit"><code>unknown</code>', body)
    assert layout.read_build_info() == {'commit': 'unknown', 'built': 'unknown',
                                        'builder': 'unknown'}


@pytest.mark.skipif(os.name == 'nt', reason='isync / dpkg reporting: Windows always '
                    'uses the built-in IMAP client')
def test_about_degrades_when_tools_are_missing(monkeypatch):
    monkeypatch.setattr(layout.shutil, 'which', lambda name: None)
    body = client_for(USER).get('/about').get_data(as_text=True)
    assert 'not installed from a package' in body
    assert re.search(r'id="about-isync">not installed<', body)


@pytest.mark.skipif(os.name == 'nt', reason='isync / dpkg reporting: Windows always '
                    'uses the built-in IMAP client')
def test_about_reads_package_and_isync_versions(monkeypatch):
    calls = []

    def fake_run(argv):
        calls.append(argv)
        if argv[0] == 'dpkg-query':
            return 'ii |9.8.7-6'
        if argv[0] == 'mbsync':
            return 'isync 1.4.4\nsecond line'
        return None

    monkeypatch.setattr(layout.shutil, 'which', lambda name: '/usr/bin/' + name)
    monkeypatch.setattr(layout, '_run', fake_run)
    body = client_for(USER).get('/about').get_data(as_text=True)
    assert re.search(r'id="about-package">9\.8\.7-6<', body)
    assert re.search(r'id="about-isync">isync 1\.4\.4<', body)
    assert 'second line' not in body
    assert all(isinstance(argv, list) for argv in calls)


def test_run_failures_degrade_to_none():
    assert layout._run(['/nonexistent/binary-for-test']) is None
    assert layout._run(['false']) is None


def test_about_contains_no_secrets(tmp_path, monkeypatch):
    secret_key = 'PLANTED-SECRET-KEY-7f3a9c1e5b'
    credential = 'PLANTED-APP-PASSWORD-zx81qq'
    token = 'PLANTED-OAUTH-TOKEN-55aa'
    key_file = tmp_path / 'secret_key'
    key_file.write_text(secret_key)
    monkeypatch.setenv('MAIL_ARCHIVER_SECRET', secret_key)
    monkeypatch.setenv('MAIL_ARCHIVER_SECRET_FILE', str(key_file))
    monkeypatch.setenv('MAIL_ARCHIVER_TEST_CREDENTIAL', credential)
    monkeypatch.setattr(ma.app, 'secret_key', secret_key)
    monkeypatch.setattr(ma, '_secret_key_source', 'env')
    ma.save_credential(USER, USER_EMAIL, credential)
    (ma.get_user_config_dir(USER) / 'oauth2_tokens.json').write_text(
        json.dumps({USER_EMAIL: {'access_token': token, 'refresh_token': token}}))
    for user in (USER, ADMIN):
        body = client_for(user).get('/about').get_data(as_text=True)
        for planted in (secret_key, credential, token, str(key_file)):
            assert planted not in body, planted
        assert 'MAIL_ARCHIVER_SECRET' not in body


def test_about_schedule_summary_hides_other_users():
    body = client_for(USER).get('/about').get_data(as_text=True)
    schedule = re.search(r'id="about-schedule">(.*?)</td></tr>', body, re.S).group(1)
    assert re.search(r'\b1 account scheduled', schedule)
    assert 'your accounts' in schedule
    # No address — not another user's, not even the viewer's own — and no
    # other username anywhere on the page.
    assert OTHER_EMAIL not in body and USER_EMAIL not in body
    assert OTHER not in body


def test_about_schedule_summary_for_admin_counts_everyone():
    body = client_for(ADMIN).get('/about').get_data(as_text=True)
    schedule = re.search(r'id="about-schedule">(.*?)</td></tr>', body, re.S).group(1)
    count = int(re.search(r'(\d+) accounts? scheduled', schedule).group(1))
    assert count >= 2
    assert 'all users' in schedule
    assert OTHER_EMAIL not in body and USER_EMAIL not in body


def test_schedule_summary_unit():
    plan = [
        {'user': 'a', 'email': 'a1@x', 'server': 'imap.one', 'slot': 2, 'due': True},
        {'user': 'a', 'email': 'a2@x', 'server': 'imap.two', 'slot': 3, 'due': False},
        {'user': 'b', 'email': 'b1@x', 'server': 'imap.one', 'slot': 4, 'due': False},
    ]
    mine = layout.schedule_summary(plan, 'a', False)
    assert (mine['accounts'], mine['users'], mine['slots'], mine['due']) == \
        (2, None, '02:00, 03:00', 1)
    everyone = layout.schedule_summary(plan, 'a', True)
    assert (everyone['accounts'], everyone['users']) == (3, 2)
    assert everyone['lanes'][0] == {'server': 'imap.one', 'accounts': 2,
                                    'slots': '02:00, 04:00'}
    assert '@' not in json.dumps(mine) + json.dumps(everyone)
    assert layout.schedule_summary(None, 'a', True) == {'available': False}
