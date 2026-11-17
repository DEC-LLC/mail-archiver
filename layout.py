#!/usr/bin/env python3
"""Mail Archiver — shared page layout, navigation tree and About page (1.3.0).

Registered on the main Flask app via `layout.register(app)`. It provides:

* a context processor that hands every template a `layout` dict — the
  navigation tree for the fixed left sidebar in templates/base.html, the
  running version for the page footer, and whether anyone is logged in;
* the `/about` route (endpoint `about`): "About DEC-LLC Mail-Archiver".

The navigation tree is built here rather than in the template so that an
entry whose endpoint is not registered is skipped instead of breaking
every page, and so that the admin entries can be handed to a non-admin as
disabled, href-less items. That is a display rule only: the admin routes
themselves are enforced by `admin.requires_admin` (403).

Nothing on the About page is a secret: no key material, no tokens, no
credentials and no environment dump. External commands run from argv
lists with a timeout and degrade to "unknown"/"not installed".
"""

import os
import shutil
import socket
import subprocess
import sys
import time

from flask import render_template, request, session, url_for
from werkzeug.routing import BuildError

import platform_compat as _pc

_HERE = os.path.dirname(os.path.abspath(__file__))
VERSION_FILE = os.path.join(_HERE, 'VERSION')
# Optional, written by the package build next to VERSION. key=value lines:
#   commit=<git sha>   built=<ISO-8601 timestamp>   builder=<host or container>
BUILD_INFO_FILE = os.path.join(_HERE, 'BUILD_INFO')
BUILD_INFO_KEYS = ('commit', 'built', 'builder')

PACKAGE_NAME = 'mail-archiver'
DPKG_LIST_FILE = '/var/lib/dpkg/info/mail-archiver.list'
COMMAND_TIMEOUT = 5  # seconds

ENV_STATUS_URL = 'https://github.com/DEC-LLC/mail-archiver'
RELEASE_NOTES_URL = ('https://github.com/DEC-LLC/mail-archiver/blob/'
                     'main/CHANGELOG.md')

ADMIN_ONLY_TITLE = 'Administrators only'

# (label, endpoints tried in order). The first endpoint that is registered
# wins; when none is, the entry is left out. `mail_import_page` and
# `bulk_sync_page` arrive with the import and large-mailbox-sync work;
# `pst_import_page` is the pre-1.3.0 import page.
NAV_BEFORE_ADMIN = (
    ('Dashboard', ('dashboard',)),
    ('Search archive', ('search',)),
    ('Import mail', ('mail_import_page', 'pst_import_page')),
    ('Initial / rebase sync', ('bulk_sync_page',)),
    ('Add email account', ('add_account',)),
    ('Server settings', ('oauth2_settings',)),
)
NAV_ADMIN = (
    ('Admin overview', ('admin.index',)),
    ('Storage', ('admin.storage_detail',)),
    ('Host', ('admin.host_detail',)),
    ('Application', ('admin.app_detail',)),
)
NAV_AFTER_ADMIN = (
    ('About', ('about',)),
    ('Log out', ('logout',)),
)

# Pages that have no entry of their own but belong under one: the entry is
# shown as the open section (not as the current page).
SECTION_OF = {
    'view_email': 'search',
    'new_oauth_app': 'oauth2_settings',
    'edit_oauth_app': 'oauth2_settings',
}


# ---------------------------------------------------------------
# Version / build metadata
# ---------------------------------------------------------------

def read_version():
    try:
        with open(VERSION_FILE, encoding='utf-8') as f:
            return f.read().strip() or 'unknown'
    except OSError:
        return 'unknown'


def read_build_info():
    """Parse BUILD_INFO into {'commit','built','builder'}; every key that
    is absent (or the whole file) reads as 'unknown'."""
    info = {k: 'unknown' for k in BUILD_INFO_KEYS}
    try:
        with open(BUILD_INFO_FILE, encoding='utf-8') as f:
            lines = f.read(4096).splitlines()
    except OSError:
        return info
    for line in lines:
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, value = line.split('=', 1)
        key, value = key.strip().lower(), value.strip()
        if key in info and value:
            info[key] = value[:200]
    return info


def _run(argv):
    """Output (stdout+stderr) of argv, or None on any failure: missing
    binary, non-zero exit, timeout."""
    try:
        p = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           timeout=COMMAND_TIMEOUT, text=True, errors='replace',
                           stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    if p.returncode != 0:
        return None
    return p.stdout.strip()


def _fmt_time(epoch):
    return time.strftime('%Y-%m-%d %H:%M %Z', time.localtime(epoch))


def package_info():
    """(package version, install date) from dpkg, else rpm.

    dpkg keeps no install timestamp in its database; the package's file
    list (/var/lib/dpkg/info/<pkg>.list) is rewritten on every install or
    upgrade, so its mtime is when the running version was put on the host.
    rpm records INSTALLTIME itself.
    """
    not_packaged = 'not installed from a package'
    if shutil.which('dpkg-query'):
        out = _run(['dpkg-query', '-W', '-f=${db:Status-Abbrev}|${Version}',
                    PACKAGE_NAME])
        if out and '|' in out:
            status, version = out.split('|', 1)
            if status.startswith('ii') and version.strip():
                try:
                    installed = _fmt_time(os.path.getmtime(DPKG_LIST_FILE))
                except OSError:
                    installed = 'unknown'
                return version.strip(), installed
    if shutil.which('rpm'):
        out = _run(['rpm', '-q', '--qf', '%{VERSION}-%{RELEASE}|%{INSTALLTIME}',
                    PACKAGE_NAME])
        if out and '|' in out:
            version, when = out.split('|', 1)
            try:
                installed = _fmt_time(int(when))
            except ValueError:
                installed = 'unknown'
            return version.strip(), installed
    return not_packaged, not_packaged


def sync_backend_info():
    """(backend description, isync version line)."""
    if _pc.IS_WINDOWS:
        # Same rule as app._has_mbsync(): Windows always syncs with imaplib.
        return 'built-in IMAP client (imaplib)', 'not used on Windows'
    if not shutil.which('mbsync'):
        return 'built-in IMAP client (imaplib) — mbsync not installed', 'not installed'
    out = _run(['mbsync', '--version'])
    first = out.splitlines()[0].strip()[:120] if out else ''
    return 'mbsync (isync)', first or 'unknown'


def schedule_summary(plan, username, is_admin):
    """Counts and slots only — never an address. A non-admin's summary is
    computed from their own accounts alone."""
    if plan is None:
        return {'available': False}
    rows = plan if is_admin else [r for r in plan if r.get('user') == username]
    lanes = {}
    for r in rows:
        lane = lanes.setdefault(r.get('server') or 'unknown',
                                {'accounts': 0, 'slots': set()})
        lane['accounts'] += 1
        lane['slots'].add(r.get('slot'))
    fmt = lambda slots: ', '.join(f'{s:02d}:00' for s in sorted(
        s for s in slots if isinstance(s, int)))
    return {
        'available': True,
        'scope': 'all users' if is_admin else 'your accounts',
        'accounts': len(rows),
        'users': len({r.get('user') for r in rows}) if is_admin else None,
        'due': sum(1 for r in rows if r.get('due')),
        'slots': fmt({r.get('slot') for r in rows}),
        'lanes': [{'server': name, 'accounts': lane['accounts'],
                   'slots': fmt(lane['slots'])}
                  for name, lane in sorted(lanes.items())],
    }


# ---------------------------------------------------------------
# Navigation
# ---------------------------------------------------------------

def _resolve(endpoints):
    """(endpoint, url) for the first registered endpoint, else None."""
    for endpoint in endpoints:
        try:
            return endpoint, url_for(endpoint)
        except BuildError:
            continue
    return None


def _entry(label, endpoints, current, disabled=False):
    resolved = _resolve(endpoints)
    if resolved is None:
        return None
    endpoint, href = resolved
    return {
        'label': label,
        'endpoint': endpoint,
        'current': endpoint == current,
        'section': SECTION_OF.get(current) == endpoint,
        'disabled': disabled,
        'href': None if disabled else href,
        'title': ADMIN_ONLY_TITLE if disabled else '',
    }


def build_nav(is_admin, current=None):
    """The sidebar tree: a list of link items and one 'Admin' group."""
    nav = []
    for label, endpoints in NAV_BEFORE_ADMIN:
        item = _entry(label, endpoints, current)
        if item:
            nav.append(item)
    children = [c for c in (_entry(label, endpoints, current, disabled=not is_admin)
                            for label, endpoints in NAV_ADMIN) if c]
    if children:
        nav.append({'label': 'Admin', 'group': True, 'disabled': not is_admin,
                    'title': '' if is_admin else ADMIN_ONLY_TITLE,
                    'children': children})
    for label, endpoints in NAV_AFTER_ADMIN:
        item = _entry(label, endpoints, current)
        if item:
            nav.append(item)
    return nav


# ---------------------------------------------------------------
# Registration
# ---------------------------------------------------------------

def register(app):
    """Attach the layout context processor and the /about route."""
    # The module that created the Flask app ('app' under gunicorn and in
    # the tests, '__main__' for `python app.py`); importing it by name
    # here would load a second copy in the latter case.
    main = sys.modules[app.import_name]

    def _current_is_admin():
        check = app.jinja_env.globals.get('is_admin')
        try:
            return bool(check and check())
        except Exception:
            return False

    def _logged_in():
        """Same test as app.login_required, without its side effects: a
        username in a session that has not timed out."""
        if not session.get('username'):
            return False
        age = time.time() - session.get('login_time', 0)
        return age <= main.CONFIG.get('session_timeout', 3600)

    @app.context_processor
    def _layout_context():
        logged_in = _logged_in()
        return {'layout': {
            'version': read_version(),
            'logged_in': logged_in,
            'username': session.get('username') if logged_in else None,
            'nav': build_nav(_current_is_admin(), request.endpoint) if logged_in else [],
        }}

    def about():
        username = session.get('username')
        is_admin = _current_is_admin()
        try:
            plan = main.sync_schedule_plan()
        except Exception:
            plan = None
        package_version, install_date = package_info()
        backend, isync_version = sync_backend_info()
        return render_template(
            'about.html',
            version=read_version(),
            build=read_build_info(),
            package_version=package_version,
            install_date=install_date,
            host=socket.getfqdn() or 'unknown',
            data_path=main.CONFIG.get('data_dir', 'unknown'),
            sync_backend=backend,
            isync_version=isync_version,
            schedule=schedule_summary(plan, username, is_admin),
            env_status_url=ENV_STATUS_URL,
            release_notes_url=RELEASE_NOTES_URL,
        )

    app.add_url_rule('/about', 'about', main.login_required(about))
