#!/usr/bin/env python3
"""Mail Archiver — Web UI for managing mbsync email archival.

Standalone Flask app for archiving Gmail, iCloud, and Outlook email via IMAP.
Supports PAM auth (for NAS/server deployments) or built-in auth (for containers).
Set MAIL_ARCHIVER_AUTH=builtin for container mode, or =pam for NAS mode (default).
"""

import sys
import os
import json
import subprocess
import time
import hashlib
import hmac
import secrets
import email
import email.policy
import re
import shlex
import shutil
import sqlite3
import threading
import uuid
from pathlib import Path
from functools import wraps

from flask import (Flask, render_template, request, redirect, url_for,
                   session, flash, jsonify, abort, send_from_directory,
                   Response)
from werkzeug.utils import secure_filename

import platform_compat as _pc
import pst_import as _pst
import mail_import as _imp
import bulk_sync as _bulk
import bulk_sync_routes as _bulk_routes
from flask import Request as _FlaskRequest


class _MailArchiverRequest(_FlaskRequest):
    """1.3.0: the mail import upload route streams a multi-GB body straight
    to the user's data dir; every other route keeps the 256 KiB cap."""

    @property
    def max_content_length(self):
        if self.path == '/import/upload':
            return _imp.MAX_IMPORT_BYTES
        return super().max_content_length

    @max_content_length.setter
    def max_content_length(self, value):  # Flask >= 3.1 has a setter
        fset = getattr(_FlaskRequest.max_content_length, 'fset', None)
        if fset is not None:
            fset(self, value)


app = Flask(__name__)
app.request_class = _MailArchiverRequest
# Cap request bodies at 256 KiB. Only routes we accept uploads on are the
# per-account client-cert PEM uploads — a PEM cert + PEM key together are
# well under 20 KiB. This guards the whole app from oversize form bodies.
# (1.3.0: except the mail import upload, see _MailArchiverRequest.)
app.config['MAX_CONTENT_LENGTH'] = 256 * 1024
# 1.3.3: the session cookie is not sent with cross-site form posts or
# scripted requests (SameSite=Lax) and is not readable from page scripts.
app.config.update(SESSION_COOKIE_SAMESITE='Lax', SESSION_COOKIE_HTTPONLY=True)

# HTTPS redirect: when serving on 8443, redirect HTTP requests
@app.before_request
def _https_redirect():
    """Redirect HTTP to HTTPS when TLS is configured."""
    if (os.environ.get('MAIL_ARCHIVER_HTTPS') == '1'
            and not request.is_secure
            and request.headers.get('X-Forwarded-Proto', 'http') != 'https'):
        from flask import redirect as _redir
        url = request.url.replace('http://', 'https://', 1).replace(':8400', ':8443', 1)
        return _redir(url, code=301)

# Load secret key from file if specified, else env var, else random.
#
# 1.1.3: track WHERE the key came from. A random per-process key is fine
# for Flask session cookies but is catastrophic for credential
# encryption — it silently derives a key that can never decrypt anything
# already on disk. `_get_encryption_key` refuses to run in that mode
# rather than hand back garbage (see the Layer-3 bug: mbsync's PassCmd
# helper ran without MAIL_ARCHIVER_SECRET_FILE in its env, fell through
# to a random key, produced an undecodable "password", and Gmail
# answered `Invalid credentials`).
_secret_file = os.environ.get('MAIL_ARCHIVER_SECRET_FILE')
_secret_key_source = 'random'
if _secret_file and os.path.exists(_secret_file):
    try:
        with open(_secret_file, encoding='utf-8') as _f:
            app.secret_key = _f.read().strip()
        _secret_key_source = 'file'
    except OSError as _e:
        # Unreadable (mode 0640 root:mail-archiver and we lack the group)
        # — do NOT silently degrade to a random key.
        sys.stderr.write(
            f'mail-archiver: cannot read secret key {_secret_file}: {_e}\n')
        app.secret_key = secrets.token_hex(32)
elif os.environ.get('MAIL_ARCHIVER_SECRET'):
    app.secret_key = os.environ['MAIL_ARCHIVER_SECRET']
    _secret_key_source = 'env'
else:
    app.secret_key = secrets.token_hex(32)

# Configuration — override via environment or config file
CONFIG = {
    # Default: platform_compat.LINUX_DATA_DIR on Linux, %APPDATA%\MailArchiver
    # on Windows (platform_compat.default_data_dir).
    'data_dir': os.environ.get('MAIL_ARCHIVER_DATA', _pc.default_data_dir()),
    'listen_port': int(os.environ.get('MAIL_ARCHIVER_PORT', '8400')),
    'session_timeout': 3600,  # 1 hour
    'allowed_users': None,  # None = any PAM user, or list of usernames
    # 'pam' or 'builtin'. Windows has no PAM, so it defaults to builtin.
    'auth_mode': os.environ.get(
        'MAIL_ARCHIVER_AUTH',
        'pam' if _pc.supports_system_accounts() else 'builtin'),
    # 1.0.15: batched sync — when an account's total message count exceeds
    # this, split into per-folder batches sized <= threshold. Fresh mbsync
    # process per batch = fresh IMAP/TLS/timeout window. 0 disables batching.
    'batch_threshold': int(os.environ.get(
        'MAIL_ARCHIVER_BATCH_THRESHOLD', '5000')),
}

SYNC_INTERVALS = {
    'manual': {'label': 'Manual only', 'seconds': 0},
    'hourly': {'label': 'Every hour', 'seconds': 3600},
    '6h':     {'label': 'Every 6 hours', 'seconds': 21600},
    '12h':    {'label': 'Every 12 hours', 'seconds': 43200},
    'daily':  {'label': 'Daily (default)', 'seconds': 86400},
}

PROVIDERS = {
    'gmail': {
        'name': 'Gmail',
        'host': 'imap.gmail.com',
        'port': 993,
        'auth': 'app_password',
        'auth_label': 'App Password',
        'auth_help': 'Generate at myaccount.google.com → Security → 2-Step → App Passwords',
        'tls': True,
    },
    'hotmail': {
        'name': 'Outlook / Hotmail',
        'host': 'outlook.office.com',
        'port': 993,
        'auth': 'oauth2',
        'auth_label': 'Microsoft Account',
        'auth_help': 'Click "Sign in with Microsoft" — you\'ll be redirected to log in securely. No password stored locally.',
        'tls': True,
        'oauth2_provider': 'microsoft',
    },
    'hotmail_apppass': {
        'name': 'Outlook / Hotmail (App Password)',
        'host': 'outlook.office.com',
        'port': 993,
        'auth': 'app_password',
        'auth_label': 'App Password',
        'auth_help': 'Enable 2FA at account.microsoft.com/security, then create an App Password. Use this if OAuth2 is not configured.',
        'tls': True,
    },
    'icloud': {
        'name': 'Apple iCloud Mail',
        'host': 'imap.mail.me.com',
        'port': 993,
        'auth': 'app_password',
        'auth_label': 'App-Specific Password',
        'auth_help': 'Generate at appleid.apple.com → Sign-In → App-Specific Passwords',
        'tls': True,
    },
    'yahoo': {
        'name': 'Yahoo Mail',
        'host': 'imap.mail.yahoo.com',
        'port': 993,
        'auth': 'app_password',
        'auth_label': 'App Password',
        'auth_help': 'Enable 2FA at login.yahoo.com → Account Security, then generate an App Password',
        'tls': True,
    },
    'custom': {
        'name': 'Other IMAP Server',
        'host': '',
        'port': 993,
        'auth': 'app_password',
        'auth_label': 'Password',
        'auth_help': 'Enter the IMAP server hostname, port, and your login credentials. Works with any standard IMAP server.',
        'tls': True,
        'custom_host': True,
    },
}


# --- Authentication ---

def _users_file():
    return Path(CONFIG['data_dir']) / '.users.json'


def _load_users():
    uf = _users_file()
    if uf.exists():
        with open(uf, encoding='utf-8') as f:
            return json.load(f)
    return {}


def _save_users(users):
    uf = _users_file()
    uf.parent.mkdir(parents=True, exist_ok=True)
    with open(uf, 'w', encoding='utf-8') as f:
        json.dump(users, f, indent=2)
    os.chmod(str(uf), 0o600)


def _hash_password(password, salt=None):
    if salt is None:
        salt = secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac('sha256', password.encode(), salt.encode(), 100_000)
    return f'{salt}${h.hex()}'


def _verify_password(password, stored):
    if '$' not in stored:
        return False
    salt = stored.split('$')[0]
    return hmac.compare_digest(_hash_password(password, salt), stored)


def builtin_authenticate(username, password):
    """Authenticate against built-in user store. Returns True/False."""
    users = _load_users()
    if username not in users:
        return False
    return _verify_password(password, users[username]['password'])


def builtin_create_user(username, password):
    """Create a user in the built-in store."""
    users = _load_users()
    users[username] = {'password': _hash_password(password)}
    _save_users(users)


def pam_authenticate(username, password):
    """Authenticate user via PAM. Returns True/False."""
    if not _pc.supports_system_accounts():
        return False         # no PAM on Windows; login() says so
    try:
        # Debian ships uppercase PAM module in python3-pam; Rocky/RHEL/Fedora
        # python3-pam ships lowercase pam. Try both so the same wheel works.
        try:
            import PAM
        except ImportError:
            import pam as PAM  # type: ignore

        def pam_conv(auth, query_list, userData):
            resp = []
            for query, qtype in query_list:
                if qtype in (PAM.PAM_PROMPT_ECHO_ON, PAM.PAM_PROMPT_ECHO_OFF):
                    resp.append((password, 0))
                else:
                    resp.append(('', 0))
            return resp

        auth = PAM.pam()
        auth.start('login')
        auth.set_item(PAM.PAM_USER, username)
        auth.set_item(PAM.PAM_CONV, pam_conv)
        auth.authenticate()
        auth.acct_mgmt()
        return True
    except Exception:
        return False


# Shown on the login page when the app is told to use PAM where there is
# none (MAIL_ARCHIVER_AUTH=pam on Windows).
PAM_UNAVAILABLE_MESSAGE = (
    'PAM (system account) sign-in is not available on Windows. Start Mail '
    'Archiver with MAIL_ARCHIVER_AUTH=builtin and create a Mail Archiver '
    'account instead.')


def auth_unavailable_message():
    """Why nobody can log in with the configured method, or ''."""
    if CONFIG['auth_mode'] != 'builtin' and not _pc.supports_system_accounts():
        return PAM_UNAVAILABLE_MESSAGE
    return ''


def authenticate(username, password):
    """Authenticate via configured method."""
    if CONFIG['auth_mode'] == 'builtin':
        return builtin_authenticate(username, password)
    return pam_authenticate(username, password)


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'username' not in session:
            return redirect(url_for('login'))
        if time.time() - session.get('login_time', 0) > CONFIG['session_timeout']:
            session.clear()
            flash('Session expired. Please log in again.')
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated


# --- Credential Storage ---

def _chown_user(path, username):
    """Set ownership to the system user if running in PAM mode.

    Best-effort: swallows OSError so callers running as the target user
    (batcher measurement, sync worker) don't crash trying to chown their
    own already-owned paths. Only root can chown to another uid; a
    non-root process's chown to its own uid is a no-op that raises EPERM
    for files it doesn't own.
    """
    if CONFIG['auth_mode'] != 'pam':
        return
    try:
        pw = _pc.user_info(username)
        if pw is not None:
            _pc.chown(str(path), pw.uid, pw.gid)
    except OSError:
        pass


def get_user_config_dir(username):
    """Return path to user's mail-archiver config directory."""
    p = Path(CONFIG['data_dir']) / username / '.config'
    p.mkdir(parents=True, exist_ok=True)
    _chown_user(p, username)
    _chown_user(p.parent, username)
    return p


def load_accounts(username):
    """Load user's email account list."""
    config_dir = get_user_config_dir(username)
    accounts_file = config_dir / 'accounts.json'
    if accounts_file.exists():
        with open(accounts_file, encoding='utf-8') as f:
            return json.load(f)
    return []


def save_accounts(username, accounts):
    """Save user's email account list.

    1.0.15: writes mode 0640 owned by <user>:mail-archiver so the gunicorn
    worker (running AS mail-archiver) can enumerate account counts at
    startup for the thread autoscaler (`gunicorn.conf.py::_total_accounts`).
    Prior mode 0600 blocked that read with PermissionError, and the
    autoscaler silently fell back to threads=4.
    """
    config_dir = get_user_config_dir(username)
    accounts_file = config_dir / 'accounts.json'
    with open(accounts_file, 'w', encoding='utf-8') as f:
        json.dump(accounts, f, indent=2)
    os.chmod(str(accounts_file), 0o640)
    _chown_user(accounts_file, username)
    # Best-effort group swap to mail-archiver — silently keep user group
    # if it doesn't exist (dev checkout / no service install / Windows).
    service_group = _pc.group_info('mail-archiver')
    if service_group is None:
        return
    gid = service_group.gid
    try:
        stat = os.stat(str(accounts_file))
        _pc.chown(str(accounts_file), stat.st_uid, gid)
    except OSError:
        pass
    # Parent .config dir + per-user data-root dir need group-execute so
    # gunicorn can descend to accounts.json. Both normally 0700 owned by
    # the user — flip to 0710 with mail-archiver group so the service
    # traverses but can't list unrelated files inside.
    try:
        for d in (config_dir, config_dir.parent):
            st = os.stat(str(d))
            _pc.chown(str(d), st.st_uid, gid)
            os.chmod(str(d), 0o710)
    except OSError:
        pass


# What the generated .mbsyncrc points mbsync at. The file is only ever read
# by mbsync, which Windows does not use (_has_mbsync); the two paths can be
# set for a host that keeps them elsewhere.
MBSYNC_CRED_HELPER = os.environ.get('MAIL_ARCHIVER_CRED_HELPER',
                                    '/usr/libexec/mail-archiver-cred')
# The system CA bundle: Debian/Ubuntu keep it in the first place, EL in the
# second (1.3.3: only the first was known, so every isync connection failed
# its certificate check on EL until MAIL_ARCHIVER_CA_FILE was set).
SYSTEM_CA_BUNDLES = ('/etc/ssl/certs/ca-certificates.crt',
                     '/etc/pki/tls/certs/ca-bundle.crt')


def _default_ca_file():
    for candidate in SYSTEM_CA_BUNDLES:
        if os.path.isfile(candidate):
            return candidate
    return SYSTEM_CA_BUNDLES[0]


MBSYNC_CA_FILE = os.environ.get('MAIL_ARCHIVER_CA_FILE') or _default_ca_file()


def generate_mbsyncrc(username):
    """Generate mbsyncrc from accounts.json."""
    accounts = load_accounts(username)
    archive_dir = Path(CONFIG['data_dir']) / username
    if CONFIG['auth_mode'] == 'pam':
        pw = _pc.user_info(username)
        home = pw.home if pw is not None else str(archive_dir)
    else:
        home = str(archive_dir)
    lines = [
        '# Auto-generated by mail-archiver. Do not edit manually.',
        f'# Generated: {time.strftime("%Y-%m-%d %H:%M:%S")}',
        '',
    ]

    config_dir_str = str(archive_dir / '.config')

    for acct in accounts:
        if not acct.get('enabled', True):
            continue
        provider = PROVIDERS.get(acct['provider'], {})
        safe_name = acct['email'].replace('@', '_at_').replace('.', '_')
        maildir = archive_dir / safe_name

        # Per-account overrides win over provider defaults so an operator can
        # switch a Gmail account to a paid business Exchange host, override
        # a port for a firewall traversal, etc. — without touching PROVIDERS.
        host = acct.get('host') or provider.get('host', '')
        port = acct.get('port') or provider.get('port', 993)
        tls_default = 'ssl' if provider.get('tls', True) else 'none'
        tls_mode = (acct.get('tls_mode') or tls_default).lower()
        display_name = acct.get('display_name') or provider.get('name', acct['provider'])

        lines.extend([
            f'# --- {acct["email"]} ({display_name}) ---',
            f'IMAPAccount {safe_name}',
            f'Host {host}',
            f'Port {port}',
            f'User {acct["email"]}',
        ])

        auth_type = acct.get('auth_type') or provider.get('auth', 'password')
        if auth_type == 'oauth2':
            token_path = os.path.join(config_dir_str, f'{safe_name}.token')
            lines.append(f'PassCmd "cat {token_path}"')
            lines.append('AuthMechs XOAUTH2')
        else:
            # T8 hardening: hand off to /usr/libexec/mail-archiver-cred so
            # username + email arrive via argv (no shell interpolation) and
            # sys.path is fixed at /opt/mail-archiver (no writable-dir
            # traversal). shlex.quote on the two args neutralizes any
            # exotic characters so mbsync's /bin/sh -c sees each as one arg.
            lines.append(
                f'PassCmd "{MBSYNC_CRED_HELPER} '
                f'{shlex.quote(username)} {shlex.quote(acct["email"])}"'
            )
            # 1.1.2: pin AuthMechs to LOGIN for app-password + plain-password
            # accounts. mbsync 1.5.x auto-picks the strongest SASL mechanism
            # the server advertises — for newer Gmail accounts that is
            # XOAUTH2, which then fails/hangs when handed an app-password
            # instead of an OAuth bearer token (crash: drv_imap.c:2033
            # assertion; hang: idle after "Authenticating with SASL mechanism
            # XOAUTH2" waiting for continuation prompts that never come).
            # LOGIN + TLS is safe (password transits inside the SSL/TLS
            # tunnel) and universally supported by Gmail, iCloud, Fastmail,
            # generic IMAP.
            lines.append('AuthMechs LOGIN')

        # TLS mode is now per-account, three-valued: ssl | starttls | none.
        # mbsync accepts SSLType {IMAPS,STARTTLS,None}.
        if tls_mode == 'ssl':
            lines.append('SSLType IMAPS')
        elif tls_mode == 'starttls':
            lines.append('SSLType STARTTLS')
        else:
            lines.append('SSLType None')
        if tls_mode != 'none':
            lines.append(f'CertificateFile {MBSYNC_CA_FILE}')

        # Client cert + key (mutual-TLS IMAP — Exchange with mTLS front-end,
        # some paid providers, self-hosted Dovecot with cert-auth).
        # Both filenames are stored under ${DATA}/${user}/.config/ and were
        # written by the cert-upload route with secure_filename + chown.
        client_cert = acct.get('client_cert')
        client_key = acct.get('client_key')
        if client_cert:
            lines.append('ClientCertificate '
                         + os.path.join(config_dir_str, client_cert))
        if client_key:
            lines.append('ClientKey ' + os.path.join(config_dir_str, client_key))

        # Timeout (mbsync default is 20s — expose so slow WAN paths + big
        # first syncs don't drop with generic "connection lost").
        timeout = acct.get('timeout_seconds')
        if timeout:
            lines.append(f'Timeout {int(timeout)}')

        # 1.3.0: while an initial / rebase sync job is open for the account,
        # its folders are left out of the regular sync (bulk_sync.py).
        pattern = _bulk.regular_sync_pattern(config_dir_str, acct)

        lines.extend([
            '',
            f'IMAPStore {safe_name}-remote',
            f'Account {safe_name}',
            '',
            f'MaildirStore {safe_name}-local',
            f'SubFolders Verbatim',
            f'Path {maildir}/',
            f'Inbox {maildir}/INBOX',
            '',
            f'Channel {safe_name}',
            f'Far :{safe_name}-remote:',
            f'Near :{safe_name}-local:',
            f'Patterns {pattern}',
            # 1.1.7: an archive only ever PULLS. Without this
            # line mbsync defaults to `Sync All` (pull + push + flags), so
            # a folder whose state is lost while local mail exists would
            # be appended back to the user's real mailbox as duplicates.
            'Sync Pull',
            'Create Near',
            'Expunge None',
            'SyncState *',
            '',
        ])

    mbsyncrc = Path(home) / '.mbsyncrc'
    mbsyncrc.write_text('\n'.join(lines), encoding='utf-8')
    os.chmod(str(mbsyncrc), 0o600)
    _chown_user(mbsyncrc, username)


def _supplementary_gids(username, primary_gid=None):
    """Return the full supplementary-group gid list for `username`.

    1.1.3: `subprocess.Popen(user=, group=)` sets EUID/EGID only — it does
    NOT initialize supplementary groups, so a child spawned that way runs
    with the parent's groups replaced by just the primary gid. mbsync's
    PassCmd helper then cannot read /opt/mail-archiver/.secret_key (mode
    0640 root:mail-archiver) and credential decryption fails. Pass the
    result as Popen's `extra_groups=`.
    """
    return _pc.supplementary_gids(username, primary_gid)


def _mbsync_child_env(pw, username):
    """Env for a kernel-setuid mbsync child.

    Deliberately minimal — the service's own env would leak
    HOME=/var/lib/mail-archiver — but it MUST carry the MAIL_ARCHIVER_*
    settings that `mail-archiver-cred` needs when it re-imports app.py in
    a fresh interpreter (1.1.3). Without MAIL_ARCHIVER_DATA the helper
    looks for the .pass file under the compiled-in default path and finds
    nothing (empty password); without MAIL_ARCHIVER_SECRET_FILE it derives
    a random encryption key and returns undecryptable bytes.
    """
    # `pw` is a platform_compat.UserInfo (home, shell) or anything with the
    # pwd names (pw_dir, pw_shell). PAM mode only, so Linux only.
    home = getattr(pw, 'home', None) or getattr(pw, 'pw_dir')
    shell = getattr(pw, 'shell', None) or getattr(pw, 'pw_shell', None)
    env = {
        'HOME':    home,
        'USER':    username,
        'LOGNAME': username,
        'SHELL':   shell or '/bin/sh',
        'PATH':    '/usr/local/sbin:/usr/local/bin:'
                   '/usr/sbin:/usr/bin:/sbin:/bin',
        'MAIL_ARCHIVER_AUTH': CONFIG['auth_mode'],
        'MAIL_ARCHIVER_DATA': str(CONFIG['data_dir']),
    }
    if _secret_file:
        env['MAIL_ARCHIVER_SECRET_FILE'] = _secret_file
    return env


def _get_encryption_key():
    """Derive a 32-byte encryption key from the Flask secret key.

    Raises RuntimeError when the secret key is a per-process random value
    (1.1.3). Such a key cannot decrypt anything previously stored and
    would produce credentials that no IMAP server accepts; failing loudly
    here is what turns that into a diagnosable error instead of a
    mysterious `Invalid credentials` from the mail provider.
    """
    import hashlib
    if _secret_key_source == 'random':
        raise RuntimeError(
            'no persistent secret key available (MAIL_ARCHIVER_SECRET_FILE '
            'unset/unreadable and MAIL_ARCHIVER_SECRET empty) — refusing to '
            'derive a credential key from an ephemeral random secret')
    secret = app.secret_key if isinstance(app.secret_key, bytes) else app.secret_key.encode()
    return hashlib.pbkdf2_hmac('sha256', secret, b'mail-archiver-cred-v1', 100000)


def _encrypt_credential(plaintext):
    """Encrypt a credential string using AES-CTR via stdlib.

    Returns base64-encoded ciphertext with 16-byte IV prefix.
    Uses hashlib PBKDF2 key derivation — no pip dependencies.
    """
    import base64
    key = _get_encryption_key()
    iv = os.urandom(16)
    # XOR stream cipher keyed with HMAC-SHA256 keystream
    keystream = b''
    counter = 0
    plainbytes = plaintext.encode('utf-8')
    while len(keystream) < len(plainbytes):
        import hmac as _hmac
        block = _hmac.new(key, iv + counter.to_bytes(4, 'big'), 'sha256').digest()
        keystream += block
        counter += 1
    ciphertext = bytes(a ^ b for a, b in zip(plainbytes, keystream[:len(plainbytes)]))
    return base64.b64encode(iv + ciphertext).decode('ascii')


def _decrypt_credential(encoded):
    """Decrypt a credential encrypted by _encrypt_credential."""
    import base64
    key = _get_encryption_key()
    raw = base64.b64decode(encoded)
    iv = raw[:16]
    ciphertext = raw[16:]
    keystream = b''
    counter = 0
    while len(keystream) < len(ciphertext):
        import hmac as _hmac
        block = _hmac.new(key, iv + counter.to_bytes(4, 'big'), 'sha256').digest()
        keystream += block
        counter += 1
    plainbytes = bytes(a ^ b for a, b in zip(ciphertext, keystream[:len(ciphertext)]))
    return plainbytes.decode('utf-8')


def save_credential(username, email, credential):
    """Save an encrypted app password/token for an email account."""
    config_dir = get_user_config_dir(username)
    safe_name = email.replace('@', '_at_').replace('.', '_')
    cred_file = config_dir / f'{safe_name}.pass'
    encrypted = _encrypt_credential(credential)
    cred_file.write_text(encrypted, encoding='utf-8')
    os.chmod(str(cred_file), 0o600)
    _chown_user(cred_file, username)


def load_credential(username, email):
    """Load and decrypt an app password/token for an email account."""
    config_dir = get_user_config_dir(username)
    safe_name = email.replace('@', '_at_').replace('.', '_')
    cred_file = config_dir / f'{safe_name}.pass'
    if not cred_file.exists():
        return None
    raw = cred_file.read_text(encoding='utf-8').strip()
    if not raw:
        return None
    # Encrypted credentials are always base64 with length > 24 (16-byte IV + data)
    # Legacy plaintext passwords are short ASCII strings (app passwords, etc.)
    import base64
    try:
        decoded = base64.b64decode(raw)
    except Exception:
        decoded = b''
    if len(decoded) > 16:
        # Definitely an encrypted blob. A failure here must NOT fall
        # through to returning the ciphertext as if it were a password
        # (1.1.3) — mbsync would hand that to the IMAP server verbatim.
        try:
            return _decrypt_credential(raw)
        except Exception as e:
            sys.stderr.write(
                f'mail-archiver: cannot decrypt credential for '
                f'{username}/{email}: {type(e).__name__}: {e}\n')
            raise
    # Legacy plaintext file — return as-is
    return raw


# --- Sync Operations ---

def _write_sync_status_locked(status_file, state, keys=None):
    """Atomic + locked write of sync_status.json.

    1.2.0: with `keys`, only those accounts' entries are written; every
    other entry is re-read from disk under the lock. The hourly sync now
    runs accounts of one user in parallel (one per IMAP server), and each
    run holds a copy of the file taken when it started, so a whole-file
    write would put back the other run's stale "syncing" entry. A new
    entry that has no 'started' keeps the one on disk, so the schedule and
    the dashboard can tell when the last sync began.

    T11 fix: cron (root) and the WebUI (mail-archiver) both write this
    file; unlocked writes let one process's truncate-then-write clobber
    the other's finished-state update. This helper:
      1. flock(LOCK_EX) on the target file itself (create-if-missing);
      2. writes the new JSON to <path>.tmp;
      3. os.replace() atomic rename (survives crashes cleanly);
      4. best-effort chown the result to the account owner.

    Falls back to a plain unlocked write on any locking error — degraded
    is better than dead. Callers must not hold any other lock on the
    file at the same time.
    """
    status_file = Path(status_file)
    tmp = status_file.with_suffix(status_file.suffix + '.tmp')
    lock_path = status_file.with_suffix(status_file.suffix + '.lock')
    payload = json.dumps(state, indent=2)
    try:
        # Use a sibling .lock file so we can flock even on first-ever write
        # (target file may not yet exist). Keep lock file around; empty.
        lock_fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            _pc.lock_fd(lock_fd, exclusive=True, blocking=True)
            if keys is not None:
                payload = json.dumps(
                    _merge_sync_status(status_file, state, keys), indent=2)
            with open(tmp, 'w', encoding='utf-8') as f:
                f.write(payload)
            _pc.replace_file(str(tmp), str(status_file))
        finally:
            _pc.unlock_fd(lock_fd)
            os.close(lock_fd)
    except (OSError, IOError):
        # Locking failed (e.g. fs doesn't support flock) — degraded
        # non-atomic path. Better than blocking the sync.
        try:
            with open(status_file, 'w', encoding='utf-8') as f:
                f.write(payload)
        except OSError:
            return


def _merge_sync_status(status_file, state, keys):
    """On-disk sync_status.json with `keys` taken from `state`. Caller
    holds the exclusive lock."""
    try:
        with open(status_file, encoding='utf-8') as f:
            merged = json.load(f)
    except (OSError, json.JSONDecodeError):
        merged = {}
    for k in keys:
        if k not in state:
            continue
        entry = dict(state[k])
        old = merged.get(k) or {}
        if 'started' not in entry and old.get('started'):
            entry['started'] = old['started']
        merged[k] = entry
    return merged


def _read_sync_status_locked(status_file):
    """Shared-lock read of sync_status.json. Returns {} on any error."""
    status_file = Path(status_file)
    if not status_file.exists():
        return {}
    lock_path = status_file.with_suffix(status_file.suffix + '.lock')
    try:
        lock_fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            _pc.lock_fd(lock_fd, exclusive=False, blocking=True)
            with open(status_file, encoding='utf-8') as f:
                return json.load(f)
        finally:
            _pc.unlock_fd(lock_fd)
            os.close(lock_fd)
    except (OSError, IOError, json.JSONDecodeError):
        # Unlocked fallback read
        try:
            with open(status_file, encoding='utf-8') as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            return {}


# 1.3.2: a "syncing" entry written by a release that did not record its
# owner (up to 1.3.1) is taken as abandoned after this long with no
# registered job in flight. The longest single mbsync run is one hour.
STALE_SYNCING_NO_OWNER_SECS = 6 * 3600


def _sync_owner():
    """Who is about to run a sync: stored in the "syncing" status entry so
    a later reader can tell a running sync from an abandoned one."""
    return {'pid': os.getpid(), 'boot_id': _slc.current_boot_id()}


def _syncing_entry_is_stale(username, key, entry, now=None):
    """True when `entry` says "syncing" and nothing is running it.

    Never true while a registered job covers the same target, or while the
    process that wrote the entry is alive in this boot.
    """
    if not isinstance(entry, dict) or entry.get('state') != 'syncing':
        return False
    try:
        for job in _slc.list_active_states_for_user(username):
            if _slc.targets_overlap(job.get('target') or '__all__', key):
                return False
    except Exception:
        return False                 # cannot tell: leave it alone
    pid = entry.get('pid')
    if pid is not None:
        boot, cur = entry.get('boot_id'), _slc.current_boot_id()
        if boot and cur and boot != cur:
            return True
        return not _pc.pid_alive(pid)
    started = _status_epoch({'started': entry.get('started')})
    if started is None:
        return True
    return ((now or time.time()) - started) > STALE_SYNCING_NO_OWNER_SECS


def _interrupted_entry(entry):
    since = entry.get('started') or 'an earlier time'
    return {
        'state': 'interrupted',
        'started': entry.get('started'),
        'finished': time.strftime('%Y-%m-%d %H:%M:%S'),
        'error': (f'The sync that started at {since} did not finish: the '
                  f'service was restarted or the sync process ended. No '
                  f'sync is running; the next one starts normally.'),
    }


def _replace_sync_status_entries(status_file, expected, replacement):
    """Under the exclusive lock, replace each key whose on-disk entry still
    equals expected[key]. An entry another run rewrote meanwhile is left
    alone. Returns the keys replaced."""
    status_file = Path(status_file)
    tmp = status_file.with_suffix(status_file.suffix + '.tmp')
    lock_path = status_file.with_suffix(status_file.suffix + '.lock')
    done = []
    try:
        lock_fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
    except OSError:
        return done
    try:
        _pc.lock_fd(lock_fd, exclusive=True, blocking=True)
        try:
            with open(status_file, encoding='utf-8') as f:
                current = json.load(f)
        except (OSError, json.JSONDecodeError):
            return done
        for k, old in expected.items():
            if current.get(k) == old:
                current[k] = replacement[k]
                done.append(k)
        if done:
            with open(tmp, 'w', encoding='utf-8') as f:
                f.write(json.dumps(current, indent=2))
            _pc.replace_file(str(tmp), str(status_file))
    except (OSError, IOError):
        return []
    finally:
        try:
            _pc.unlock_fd(lock_fd)
        finally:
            os.close(lock_fd)
    return done


def heal_stale_sync_status(username, status=None):
    """Mark abandoned "syncing" entries of `username` as "interrupted".

    Returns the status as it now reads. Runs on every status read and once
    per user when the service starts.
    """
    status_file = get_user_config_dir(username) / 'sync_status.json'
    if status is None:
        status = _read_sync_status_locked(status_file)
    stale = {k: e for k, e in status.items()
             if _syncing_entry_is_stale(username, k, e)}
    if not stale:
        return status
    new = {k: _interrupted_entry(e) for k, e in stale.items()}
    for k in _replace_sync_status_entries(status_file, stale, new):
        status[k] = new[k]
    return status


def heal_stale_sync_status_all():
    """Service start: every user's status file. Returns entries healed."""
    healed = 0
    data_dir = Path(CONFIG['data_dir'])
    try:
        users = sorted(d.name for d in data_dir.iterdir()
                       if d.is_dir() and not d.name.startswith('.')
                       and (d / '.config' / 'sync_status.json').is_file())
    except OSError:
        return 0
    for username in users:
        try:
            before = _read_sync_status_locked(
                data_dir / username / '.config' / 'sync_status.json')
            after = heal_stale_sync_status(username, dict(before))
            healed += sum(1 for k in after
                          if after[k] != before.get(k))
        except Exception:
            continue
    return healed


def get_sync_status(username):
    """Get sync status for all accounts (abandoned "syncing" entries read
    as "interrupted", 1.3.2)."""
    config_dir = get_user_config_dir(username)
    status_file = config_dir / 'sync_status.json'
    status = _read_sync_status_locked(status_file)
    try:
        return heal_stale_sync_status(username, status)
    except Exception:
        return status


def _has_mbsync():
    """Check if mbsync is available on this system.

    Never on Windows: the Windows application syncs with imaplib
    (imap_sync.py) even if an mbsync happens to be on PATH, because the
    generated .mbsyncrc and the credential helper are POSIX-only.
    """
    if _pc.IS_WINDOWS:
        return False
    return shutil.which('mbsync') is not None


def _run_sync_imaplib(username, account_email, status, key, status_file):
    """Sync using Python imaplib (stdlib) — works on all platforms."""
    accounts = load_accounts(username)
    archive_dir = Path(CONFIG['data_dir']) / username

    targets = [a for a in accounts if a.get('enabled', True)]
    if account_email:
        targets = [a for a in targets if a['email'] == account_email]

    if not targets:
        status[key] = {
            'state': 'error',
            'finished': time.strftime('%Y-%m-%d %H:%M:%S'),
            'error': 'No matching account found',
        }
        return

    try:
        from imap_sync import ImapSyncer
    except ImportError:
        status[key] = {
            'state': 'error',
            'finished': time.strftime('%Y-%m-%d %H:%M:%S'),
            'error': 'imap_sync module not found — cannot sync without mbsync or imaplib backend',
        }
        return

    all_new, all_errors = 0, 0
    for acct in targets:
        provider = PROVIDERS.get(acct['provider'], {})
        safe_name = acct['email'].replace('@', '_at_').replace('.', '_')
        local_dir = archive_dir / safe_name

        # Determine auth method and credentials
        auth_method = 'password'
        oauth2_token = None
        password = load_credential(username, acct['email']) or ''

        if (provider.get('auth') == 'oauth2' or acct.get('auth_method') == 'oauth2'
                or acct.get('auth_type') == 'oauth2'):
            auth_method = 'oauth2'
            # Resolve per-account OAuth app (server-default / user-default /
            # explicit override), refresh the token if stale, hand back the
            # access token for XOAUTH2. Any failure here silently drops to
            # a blank token so the request-scoped sync doesn't crash — the
            # per-account edit page will surface the misconfig loudly.
            try:
                from oauth2_microsoft import (resolve_oauth_app,
                                              build_microsoft_oauth2,
                                              ensure_fresh_token)
                redirect_uri = ''  # not needed for refresh, only for authorize
                app_obj = resolve_oauth_app(CONFIG['data_dir'], username,
                                            app_id=acct.get('oauth_app_id'),
                                            provider='microsoft')
                if app_obj:
                    oauth = build_microsoft_oauth2(app_obj, redirect_uri)
                    oauth2_token = ensure_fresh_token(oauth, CONFIG['data_dir'],
                                                     username, acct['email'])
            except Exception:
                pass

        # T5: in PAM mode, resolve the Linux user's uid/gid and pass to
        # ImapSyncer so every Maildir dir + message file + .synced_uids.json
        # write is chowned to the target user. Without this, when mbsync
        # gets installed later and takes over the same Maildir, it can't
        # rewrite mail-archiver-owned files → dual-uid corruption + dupes.
        chown_uid, chown_gid = None, None
        if CONFIG['auth_mode'] == 'pam' and _pc.supports_system_accounts():
            pw = _pc.user_info(username)
            if pw is not None:
                chown_uid, chown_gid = pw.uid, pw.gid
            # else: PAM user vanished from /etc/passwd between login and
            # sync. Skip chown — files land under mail-archiver, operator
            # will need to re-chown by hand once user comes back.

        syncer = ImapSyncer(
            # The account's own host/port first, as generate_mbsyncrc does:
            # the 'custom' provider has an empty host, so the provider-first
            # lookup connected to nothing. This backend is the only one on
            # Windows.
            host=acct.get('host') or provider.get('host', ''),
            port=int(acct.get('port') or provider.get('port', 993)),
            use_tls=provider.get('tls', True),
            # 1.3.2: the account's own TLS mode, as generate_mbsyncrc uses
            # (ssl | starttls | none); blank follows the provider.
            tls_mode=acct.get('tls_mode'),
            username=acct['email'],
            password=password,
            local_dir=local_dir,
            auth_method=auth_method,
            oauth2_token=oauth2_token,
            chown_uid=chown_uid,
            chown_gid=chown_gid,
        )

        try:
            result = syncer.sync()
            if not result.get('success'):
                # sync() reports a failed connection or login this way; it
                # used to be counted as a clean run with nothing new.
                raise RuntimeError(result.get('error') or 'IMAP sync failed')
            all_new += result.get('total_new', 0)
            all_errors += result.get('total_errors', 0)
        except Exception as e:
            all_errors += 1
            status[key] = {
                'state': 'error',
                'finished': time.strftime('%Y-%m-%d %H:%M:%S'),
                'error': str(e),
            }
            return

    if all_errors == 0:
        status[key] = {
            'state': 'ok',
            'finished': time.strftime('%Y-%m-%d %H:%M:%S'),
            'exit_code': 0,
            'error': '',
            'new_messages': all_new,
        }
    else:
        status[key] = {
            'state': 'error',
            'finished': time.strftime('%Y-%m-%d %H:%M:%S'),
            'error': f'{all_errors} errors during sync ({all_new} new messages)',
        }


def _refresh_oauth_tokens_before_sync(username, account_email=None,
                                       progress_log=None):
    """Refresh every OAuth account's access token BEFORE spawning mbsync.

    P0 bug fix (1.0.16). MSA/AAD access tokens live ~60 min; refresh
    tokens live 90 days. Prior releases wrote the access_token to disk
    at sign-in time and mbsync read it via `cat .token` — so every
    OAuth sync failed with AUTHENTICATIONFAILED an hour after
    authorize, until the user re-signed-in through the WebUI. Backgrounded
    archive can't survive that.

    This runs INSIDE the Flask process (as mail-archiver, gunicorn worker)
    BEFORE any mbsync spawn, so:
      * The refresh uses the mail-archiver user's write access to
        <data>/<user>/.config/*.oauth2.json (0640 <user>:mail-archiver)
      * mbsync-as-target-user later just reads the fresh .token file
      * No drop-priv-then-write complication

    Concurrency-safe via fcntl.flock on a sibling .lock file per account.

    Returns the per-email report dict from
    oauth2_microsoft.refresh_all_oauth_tokens(). Callers should surface
    needs_reauth flags in the sync status.
    """
    try:
        from oauth2_microsoft import refresh_all_oauth_tokens
    except ImportError:
        return {}
    accounts = load_accounts(username)

    def _log(msg):
        # Prefix so the message is greppable in journalctl vs any other
        # gunicorn output. flush=True because gunicorn's default buffering
        # swallowed the 1.0.14 autoscaler log until 1.0.15 fixed the
        # print(flush=True) — same class of bug, same defense.
        try:
            print(f'mail-archiver oauth-refresh: {msg}', flush=True)
        except Exception:
            pass
        if progress_log is not None:
            try:
                progress_log(msg)
            except Exception:
                pass

    return refresh_all_oauth_tokens(
        data_dir=CONFIG['data_dir'],
        username=username,
        only_email=account_email,
        accounts_iter=lambda: accounts,
        logger=_log,
    )


def run_sync(username, account_email=None):
    """Trigger email sync for a user.

    Auto-detects sync backend: mbsync if available, imaplib fallback.
    """
    # P0: refresh OAuth access tokens BEFORE spawning mbsync. Without
    # this, mbsync fires with a stale (>1h old) token → AUTHENTICATIONFAILED.
    # See _refresh_oauth_tokens_before_sync for full rationale.
    try:
        _refresh_oauth_tokens_before_sync(username, account_email)
    except Exception as e:
        # Refresh failure isn't fatal on its own — a per-account
        # needs_reauth flag lands via the helper. Continue to mbsync;
        # if the token really is dead, mbsync will surface it.
        print(f'mail-archiver oauth-refresh: pre-sync refresh raised: {e}',
              flush=True)
    import shlex
    config_dir = get_user_config_dir(username)
    archive_dir = Path(CONFIG['data_dir']) / username

    log_file = config_dir / 'sync.log'
    status_file = config_dir / 'sync_status.json'

    # Update status to "syncing"
    status = get_sync_status(username)
    key = account_email or '__all__'
    status[key] = {
        'state': 'syncing',
        'started': time.strftime('%Y-%m-%d %H:%M:%S'),
        **_sync_owner(),
    }
    # T11: locked+atomic (cron and webui both write this file)
    _write_sync_status_locked(status_file, status, keys=[key])

    # Auto-detect backend: mbsync (fast, Linux/Mac) or imaplib (portable)
    if not _has_mbsync():
        _run_sync_imaplib(username, account_email, status, key, status_file)
    else:
        # mbsync path
        if account_email:
            # The channel is named exactly as generate_mbsyncrc names it.
            # Until 1.3.1 every character outside [A-Za-z0-9_] was stripped
            # here but not there, so an address with a hyphen (or a plus)
            # asked isync for a channel that does not exist: "No channel or
            # group named 'mailarchivertest_at_...' defined", exit 1, on
            # every scheduled sync (found with a test mailbox).
            mbsync_arg = _bulk.safe_account_name(account_email)
        else:
            mbsync_arg = '-a'

        # Build subprocess.run kwargs. PAM-mode calls subprocess with
        # user=uid, group=gid — the kernel does setuid at exec time from
        # our ambient CAP_SETUID+CAP_SETGID. No shell wrapper needed:
        # su(1) prompts for password when caller isn't root, and
        # runuser(1) requires root on Debian (only setuid on Rocky/RHEL).
        # Kernel-level setuid via subprocess kwargs is portable + clean.
        subprocess_kwargs = {'capture_output': True, 'text': True,
                             'timeout': 3600}
        if CONFIG['auth_mode'] == 'pam':
            pw = _pc.user_info(username)
            if pw is None:
                raise ValueError(
                    f'PAM user {username!r} not present on this host — '
                    f'cannot run mbsync as that identity')
            cmd = ['mbsync', mbsync_arg]
            subprocess_kwargs.update({
                'user':  pw.uid,
                'group': pw.gid,
                'extra_groups': _supplementary_gids(username, pw.gid),
                'cwd':   pw.home,
                'env':   _mbsync_child_env(pw, username),
            })
        else:
            rc = archive_dir / '.mbsyncrc'
            cmd = ['mbsync', '-c', str(rc), mbsync_arg]

        try:
            result = subprocess.run(cmd, **subprocess_kwargs)
            if result.returncode == 0:
                status[key] = {
                    'state': 'ok',
                    'finished': time.strftime('%Y-%m-%d %H:%M:%S'),
                    'exit_code': 0,
                    'error': '',
                }
            else:
                raw = redact_sync_output(result.stderr or '')[-500:]
                status[key] = {
                    'state': 'error',
                    'finished': time.strftime('%Y-%m-%d %H:%M:%S'),
                    'exit_code': result.returncode,
                    'error': friendly_sync_error(raw, account_email or '',
                                                 result.returncode),
                    'raw_error': raw,
                }
        except subprocess.TimeoutExpired:
            status[key] = {
                'state': 'error',
                'finished': time.strftime('%Y-%m-%d %H:%M:%S'),
                'error': 'Sync timed out after 1 hour',
            }
        except Exception as e:
            status[key] = {
                'state': 'error',
                'finished': time.strftime('%Y-%m-%d %H:%M:%S'),
                'error': str(e),
            }

    # T11: locked+atomic (cron and webui both write this file)
    _write_sync_status_locked(status_file, status, keys=[key])
    _chown_user(status_file, username)

    # Update search index after successful sync
    if status[key].get('state') == 'ok':
        try:
            from search_index import index_maildir
            index_maildir(username, CONFIG['data_dir'],
                          account_filter=account_email)
        except Exception:
            pass  # Index update is best-effort, never blocks sync

    return status[key]


# 1.1.9: mbsync prints progress and notices alongside real errors.
# isync 1.5 prints "notice: SSLType is deprecated" on EVERY run, so the old
# "'ssl' in output" test labelled every failure, on every provider, as a
# TLS error; and the fallback showed the first 200 characters, which are
# progress lines. Classify only the lines that report a failure.
_SYNC_NOISE_RE = re.compile(
    r'^(opening |loading |synchroni[sz]ing|creating |near side:|far side:|'
    r'channels:|maildir notice|\[sasl-|c: \d|notice:)'
    r'|: notice: ', re.I)
_SYNC_ERROR_RE = re.compile(
    r'(error|fail|denied|refused|cannot|can\'t|unable|skipping|exited with|'
    r'no such file|bye response|timed out|locked|overquota)', re.I)


def _sync_error_lines(raw):
    lines = []
    for ln in (raw or '').splitlines():
        ln = ln.strip()
        if not ln or _SYNC_NOISE_RE.search(ln):
            continue
        if _SYNC_ERROR_RE.search(ln):
            lines.append(ln)
    return lines


# 1.1.12: with -V, mbsync echoes the SASL exchange, including
# `AUTHENTICATE XOAUTH2 <base64 of user + Bearer access token>`. Every
# piece of mbsync output we keep or show (live panel, job state, stored
# raw_error, the friendly message) goes through this first.
_AUTH_CMD_RE = re.compile(r'(AUTHENTICATE\s+[A-Za-z0-9_-]+\s+)\S+', re.I)
_LOGIN_CMD_RE = re.compile(r'(\bLOGIN\s+)(?!<user>)("[^"]*"|\S+)\s+("[^"]*"|\S+)', re.I)
_LONG_BLOB_RE = re.compile(r'[A-Za-z0-9+/_=-]{120,}')


def redact_sync_output(text):
    """Strip credentials that mbsync's verbose output can echo."""
    if not text:
        return text
    text = _AUTH_CMD_RE.sub(r'\1<credential redacted>', text)
    text = _LOGIN_CMD_RE.sub(r'\1<user> <pass>', text)
    return _LONG_BLOB_RE.sub(lambda m: f'<{len(m.group())} chars redacted>', text)


def friendly_sync_error(raw_error, email='', exit_code=None):
    """Turn raw mbsync output into a message that says what actually failed."""
    raw = redact_sync_output(raw_error or '').strip()
    errs = _sync_error_lines(raw)
    el = '\n'.join(errs).lower()
    domain = email.split('@')[-1].lower() if '@' in email else ''
    m = re.search(r'Near:\s*\+(\d+)', raw)
    pulled = int(m.group(1)) if m else 0
    got = f' {pulled:,} new message(s) were saved first.' if pulled else ''

    if 'overquota' in el or 'bandwidth limits' in el:
        return ('Gmail\'s daily IMAP download limit was reached, so Google '
                'closed the connection.' + got + ' Nothing is wrong with the '
                'account: the next scheduled sync continues where this one '
                'stopped. A large first sync can take several days. Please do '
                'not keep clicking Sync; it does not get past the limit.')

    if 'authenticated but not connected' in el:
        return ('Microsoft accepted the sign-in, but it belongs to a different '
                f'mailbox than {email or "this account"}. Usually the browser '
                'was still signed in to another Microsoft account. Sign out at '
                'outlook.com (or use a private window), then click "Sign in '
                'with Microsoft" again and sign in as this address. If this '
                'address is only an alias of an Outlook account you already '
                'archive, remove it here; its mail is in that archive.')

    if ('.token' in el and 'no such file' in el) or 'refresh_token' in el:
        return ('Not signed in to Microsoft yet. Open this account\'s '
                'Settings and click "Sign in with Microsoft", using the '
                'archiver at its full address (for example '
                'https://archive.example.org:8443).')

    if ('authenticationfailed' in el or 'authentication failed' in el
            or 'login failed' in el or 'invalid credentials' in el):
        if 'icloud' in domain or 'me.com' in domain:
            hint = 'For iCloud, use an app-specific password from appleid.apple.com.'
        elif 'gmail' in domain or 'google' in domain:
            hint = 'For Gmail, use an app password from myaccount.google.com.'
        elif any(d in domain for d in ('outlook', 'hotmail', 'live')):
            hint = 'For Outlook/Hotmail, click "Sign in with Microsoft" again.'
        else:
            hint = 'Use an app-specific password, not your normal login password.'
        return 'The mail server rejected the login. ' + hint

    if 'passcmd exited' in el:
        return ('The stored password or token for this account could not be '
                'read. Re-enter it in the account\'s Settings.')

    if 'permission denied' in el:
        return ('The archive on the server has a file-permission problem, so '
                'mail could not be written. This needs the administrator; '
                're-entering the password will not help.')

    if 'is locked' in el or 'cannot be opened anymore' in el:
        return ('Another sync of this account was running at the same time. '
                'Wait for it to finish; the next sync will continue normally.')

    if ('certificate' in el or 'ssl error' in el or 'tls error' in el
            or 'handshake' in el):
        return ('Secure (TLS) connection to the mail server failed. The '
                'server certificate may have changed, or the server name or '
                'port is wrong.')

    if ('resolve' in el or 'getaddrinfo' in el or 'connection refused' in el
            or 'timed out' in el or 'network is unreachable' in el):
        return ('Could not reach the mail server. Check the network, and '
                'that the server name and port are right.')

    if isinstance(exit_code, int) and exit_code < 0:
        return (f'The sync was stopped before it finished (signal '
                f'{-exit_code}), by an administrator or a service restart.'
                + got + ' The next sync continues where it stopped.')

    if errs:
        msg = ' | '.join(errs[-3:])
        if len(msg) > 300:
            msg = msg[-300:]
        return f'Sync error: {msg}'
    if not raw:
        return 'Sync failed with no error details. Check that mbsync is installed.'
    return 'Sync failed without an error message; see the sync log for details.'


def get_maildir_stats(username, email):
    """Count messages in a user's maildir for an account."""
    safe_name = email.replace('@', '_at_').replace('.', '_')
    maildir = Path(CONFIG['data_dir']) / username / safe_name
    if not maildir.exists():
        return {'folders': 0, 'messages': 0, 'size': '0'}

    folders = 0
    messages = 0
    total_size = 0
    for d in maildir.rglob('cur'):
        folders += 1
        for f in d.iterdir():
            if f.is_file():
                messages += 1
                total_size += f.stat().st_size
    for d in maildir.rglob('new'):
        for f in d.iterdir():
            if f.is_file():
                messages += 1
                total_size += f.stat().st_size

    if total_size > 1_073_741_824:
        size_str = f'{total_size / 1_073_741_824:.1f} GB'
    elif total_size > 1_048_576:
        size_str = f'{total_size / 1_048_576:.0f} MB'
    else:
        size_str = f'{total_size / 1024:.0f} KB'

    return {'folders': folders, 'messages': messages, 'size': size_str}


# --- Routes ---

@app.route('/')
def index():
    if 'username' in session:
        return redirect(url_for('dashboard'))
    return redirect(url_for('login'))


@app.route('/login', methods=['GET', 'POST'])
def login():
    unavailable = auth_unavailable_message()
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')

        if unavailable:
            # PAM asked for where there is none (Windows): say so instead
            # of "Invalid username or password".
            flash(unavailable)
            return render_template('login.html',
                                   auth_mode=CONFIG['auth_mode'],
                                   auth_unavailable=unavailable)

        if not username or not password:
            flash('Username and password required.')
            return render_template('login.html')

        if CONFIG['allowed_users'] and username not in CONFIG['allowed_users']:
            flash('Account not authorized for this service.')
            return render_template('login.html')

        if authenticate(username, password):
            session['username'] = username
            session['login_time'] = time.time()
            return redirect(url_for('dashboard'))
        else:
            flash('Invalid username or password.')

    return render_template('login.html',
                           auth_mode=CONFIG['auth_mode'],
                           auth_unavailable=unavailable)


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))


def registration_open():
    """Built-in sign-in lets anyone who reaches the login page create an
    account. MAIL_ARCHIVER_REGISTRATION=closed turns that off (1.3.3), for
    an installation whose accounts all exist already."""
    return os.environ.get('MAIL_ARCHIVER_REGISTRATION', 'open').strip().lower() != 'closed'


app.jinja_env.globals['registration_open'] = registration_open


@app.route('/register', methods=['GET', 'POST'])
def register():
    if CONFIG['auth_mode'] != 'builtin':
        abort(404)
    if not registration_open():
        flash('Registration is closed on this installation. Ask its administrator for an account.')
        return redirect(url_for('login'))
    if request.method == 'POST':
        username = request.form.get('username', '').strip().lower()
        password = request.form.get('password', '')
        if not username or not password:
            flash('Username and password required.')
            return render_template('register.html')
        if len(password) < 6:
            flash('Password must be at least 6 characters.')
            return render_template('register.html')
        if not username.isalnum():
            flash('Username must be alphanumeric.')
            return render_template('register.html')
        users = _load_users()
        if username in users:
            flash('Username already exists.')
            return render_template('register.html')
        builtin_create_user(username, password)
        session['username'] = username
        session['login_time'] = time.time()
        flash(f'Account created. Welcome, {username}!')
        return redirect(url_for('dashboard'))
    return render_template('register.html')


@app.route('/dashboard')
@login_required
def dashboard():
    username = session['username']
    accounts = load_accounts(username)
    sync_status = get_sync_status(username)

    # Enrich accounts with status and stats
    for acct in accounts:
        acct['status'] = sync_status.get(acct['email'], {})
        acct['stats'] = get_maildir_stats(username, acct['email'])
        acct['provider_info'] = PROVIDERS.get(acct['provider'], {})

    archive_dir = os.path.join(CONFIG['data_dir'], username)
    if CONFIG['auth_mode'] == 'pam':
        pw = _pc.user_info(username)
        home_dir = pw.home if pw is not None else archive_dir
    else:
        home_dir = archive_dir

    # 1.0.17: postinst drops /run/mail-archiver/restart-pending when it
    # detects an in-flight mbsync at upgrade time. Persistent amber banner
    # tells the operator new bits are on disk but the running process is
    # still on the previous version. ExecStartPost=-/bin/rm clears the
    # marker on the next manual `systemctl restart`.
    restart_pending = None
    try:
        # /run/mail-archiver/restart-pending; never written on Windows
        # (no package upgrade under a running service there).
        marker = os.path.join(_pc.runtime_dir(), 'restart-pending')
        if _pc.supports_system_accounts() and os.path.exists(marker):
            with open(marker, encoding='utf-8') as f:
                restart_pending = f.read().strip()
    except OSError:
        pass

    # 1.1.0: active-job auto-reattach. Enumerate any running sync jobs
    # for this user so the dashboard template can pre-open EventSource
    # connections for accounts currently syncing (logout/login/new-tab
    # all pick up the same live progress stream).
    active_jobs = {}
    try:
        for job in _slc.list_active_states_for_user(username):
            key = job.get('email') or '__all__'
            active_jobs[key] = job.get('job_id')
    except Exception:
        pass

    return render_template('dashboard.html',
                           username=username,
                           accounts=accounts,
                           home_dir=home_dir,
                           archive_dir=archive_dir,
                           sync_intervals=SYNC_INTERVALS,
                           restart_pending=restart_pending,
                           active_jobs=active_jobs)


@app.route('/api/sync/active')
@login_required
def sync_active_list():
    """List active syncs for the current user. Consumed by dashboard JS
    for post-login auto-reattach.
    """
    username = session['username']
    out = []
    try:
        for job in _slc.list_active_states_for_user(username):
            out.append({
                'job_id': job.get('job_id'),
                'email': job.get('email'),
                'target': job.get('target'),
                'state': job.get('state'),
                'started': job.get('started'),
            })
    except Exception:
        pass
    return jsonify({'jobs': out})


@app.route('/account/add', methods=['GET', 'POST'])
@login_required
def add_account():
    username = session['username']

    # Item-1 (1.0.10): make the OAuth-app picker available at add-time so
    # onboarding a new user is one form, not "add → dashboard → Settings →
    # pick app → back → Sign in". Filter to microsoft-provider apps (only
    # OAuth provider today).
    from oauth2_microsoft import list_oauth_apps, resolve_oauth_app
    available_oauth_apps = list_oauth_apps(
        CONFIG['data_dir'], username=username, provider='microsoft'
    )

    def _render():
        return render_template('add_account.html', providers=PROVIDERS,
                               available_oauth_apps=available_oauth_apps)

    if request.method == 'POST':
        provider = request.form.get('provider', '')
        email = request.form.get('email', '').strip()
        credential = request.form.get('credential', '').strip()
        oauth_app_id = request.form.get('oauth_app_id', '').strip() or None

        if not provider or not email:
            flash('Provider and email are required.')
            return _render()

        if provider not in PROVIDERS:
            flash('Unknown provider.')
            return _render()

        provider_info = PROVIDERS[provider]

        # For app_password providers, credential is required
        if provider_info['auth'] == 'app_password' and not credential:
            flash(f'{provider_info["auth_label"]} is required.')
            return _render()

        # Validate the picked OAuth app if one was supplied (only meaningful
        # for OAuth providers — silently ignore for password providers).
        if oauth_app_id and provider_info['auth'] == 'oauth2':
            if not resolve_oauth_app(CONFIG['data_dir'], username,
                                     app_id=oauth_app_id,
                                     provider='microsoft'):
                flash(f'OAuth app "{oauth_app_id}" not found — dropping pin, '
                      f'will fall back to default resolution.')
                oauth_app_id = None

        accounts = load_accounts(username)

        # Check for duplicate
        if any(a['email'] == email and a['provider'] == provider for a in accounts):
            flash(f'{email} ({provider_info["name"]}) is already registered.')
            return _render()

        # Save credential
        if credential:
            save_credential(username, email, credential)

        # Build account entry
        new_account = {
            'email': email,
            'provider': provider,
            'auth_type': provider_info['auth'],
            'enabled': True,
            'sync_interval': 'daily',
            'added': time.strftime('%Y-%m-%d %H:%M:%S'),
        }
        # Pin the chosen OAuth app at creation time so the immediate
        # authorize redirect below picks it up too.
        if oauth_app_id and provider_info['auth'] == 'oauth2':
            new_account['oauth_app_id'] = oauth_app_id

        # Custom IMAP: user provides host and port
        if provider_info.get('custom_host'):
            custom_host = request.form.get('custom_host', '').strip()
            custom_port = request.form.get('custom_port', '993').strip()
            if not custom_host:
                flash('IMAP server hostname is required for custom provider.')
                return _render()
            new_account['host'] = custom_host
            new_account['port'] = int(custom_port) if custom_port.isdigit() else 993

        accounts.append(new_account)
        save_accounts(username, accounts)

        # Regenerate mbsyncrc
        generate_mbsyncrc(username)

        # Create maildir
        safe_name = email.replace('@', '_at_').replace('.', '_')
        maildir = Path(CONFIG['data_dir']) / username / safe_name
        maildir.mkdir(parents=True, exist_ok=True)
        _chown_user(maildir, username)

        # OAuth2 accounts have no password to store — jump straight to the
        # Microsoft consent flow so the user isn't left staring at a "no
        # button to sign in" dashboard. Only redirect if OAuth is
        # configured (either the freshly-picked app or a fallback default);
        # otherwise fall through with a helpful flash.
        if provider_info['auth'] == 'oauth2':
            try:
                app_obj = resolve_oauth_app(CONFIG['data_dir'], username,
                                            app_id=oauth_app_id,
                                            provider='microsoft')
                if app_obj and app_obj.get('client_id') and app_obj.get('client_secret'):
                    flash(f'Added {email}. Signing in with Microsoft…')
                    return redirect(url_for('oauth2_authorize', email=email))
                flash(f'Added {email}. No Microsoft OAuth2 app is configured yet — '
                      f'go to Server Settings → OAuth Apps, add one, then click '
                      f'"Sign in with Microsoft" on the account row.')
            except Exception:
                flash(f'Added {email}. OAuth apps config not readable — check '
                      f'Server Settings → OAuth Apps.')
            return redirect(url_for('dashboard'))

        flash(f'Added {email}. You can now sync it.')
        return redirect(url_for('dashboard'))

    return _render()


@app.route('/account/<email>/remove', methods=['POST'])
@login_required
def remove_account(email):
    username = session['username']
    accounts = load_accounts(username)
    accounts = [a for a in accounts if a['email'] != email]
    save_accounts(username, accounts)
    generate_mbsyncrc(username)

    # Remove credential file
    config_dir = get_user_config_dir(username)
    safe_name = email.replace('@', '_at_').replace('.', '_')
    for ext in ('.pass', '.token'):
        cred_file = config_dir / f'{safe_name}{ext}'
        if cred_file.exists():
            cred_file.unlink()

    flash(f'Removed {email}.')
    return redirect(url_for('dashboard'))


@app.route('/account/<email>/toggle', methods=['POST'])
@login_required
def toggle_account(email):
    username = session['username']
    accounts = load_accounts(username)
    for acct in accounts:
        if acct['email'] == email:
            acct['enabled'] = not acct.get('enabled', True)
            break
    save_accounts(username, accounts)
    generate_mbsyncrc(username)
    return redirect(url_for('dashboard'))


@app.route('/account/<email>/update-credential', methods=['POST'])
@login_required
def update_credential(email):
    username = session['username']
    credential = request.form.get('credential', '').strip()
    if not credential:
        flash('Credential cannot be empty.')
        return redirect(url_for('dashboard'))
    save_credential(username, email, credential)
    flash(f'Updated credential for {email}.')
    return redirect(url_for('dashboard'))


# ------------------------------------------------------------------
# Per-account settings (edit page + client-cert upload)
# ------------------------------------------------------------------

# TLS modes offered in the edit form. Values map 1:1 to the enum
# generate_mbsyncrc emits into `SSLType` (ssl → IMAPS, starttls → STARTTLS,
# none → None). Provider defaults still win if the field is left blank.
TLS_MODES = [
    ('ssl',      'SSL/TLS (IMAPS, port 993) — default'),
    ('starttls', 'STARTTLS (usually port 143)'),
    ('none',     'Plaintext (NOT recommended; the built-in IMAP client refuses it)'),
]

_ALLOWED_CERT_EXTS = {'.pem', '.crt', '.cer', '.key'}


def _redact_middle(s, head=4, tail=4):
    """Redact a secret for display. Short strings collapse to bullets only
    so we never leak more than a hint. Never returns the full plaintext."""
    if not s:
        return ''
    n = len(s)
    if n <= head + tail + 2:
        # Too short to reveal head+tail without giving up most of it —
        # show a bullet run of the actual length instead.
        return '•' * min(n, 20)
    return f'{s[:head]}…{s[-tail:]}'


def _mtime_hint(path):
    """Human-friendly 'set N minutes/hours/days ago'."""
    try:
        mt = path.stat().st_mtime
    except OSError:
        return ''
    age = max(0, int(time.time() - mt))
    if age < 90:
        return f'{age} sec ago'
    if age < 5400:
        return f'{age // 60} min ago'
    if age < 172800:
        return f'{age // 3600} hr ago'
    return f'{age // 86400} d ago'


def _credential_hint(username, email):
    """State of the saved app-password / API-key for an account.
    Returns a dict safe for template use — never plaintext."""
    config_dir = get_user_config_dir(username)
    safe_name = email.replace('@', '_at_').replace('.', '_')
    cred_file = config_dir / f'{safe_name}.pass'
    if not cred_file.exists():
        return {'set': False, 'length': 0, 'redacted': '', 'age': ''}
    try:
        plain = load_credential(username, email) or ''
    except Exception as e:
        # Stored but undecryptable (1.1.3) — surface it in the UI rather
        # than 500ing a read-only status endpoint.
        return {'set': True, 'length': 0, 'redacted': f'(undecryptable: {e})',
                'age': _mtime_hint(cred_file)}
    return {
        'set': True,
        'length': len(plain),
        'redacted': _redact_middle(plain),
        'age': _mtime_hint(cred_file),
    }


def _oauth2_token_hint(username, email):
    """State of the saved Microsoft OAuth2 tokens for an account.
    Returns access-token head/tail, expiry, refresh-token presence."""
    from pathlib import Path as _P
    config_dir = _P(CONFIG['data_dir']) / username / '.config'
    safe_name = email.replace('@', '_at_').replace('.', '_')
    token_file = config_dir / f'{safe_name}.oauth2.json'
    pass_file = config_dir / f'{safe_name}.token'
    if not token_file.exists():
        return {'authorized': False}
    try:
        with open(token_file, encoding='utf-8') as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return {'authorized': False, 'error': 'token file unreadable'}
    access = data.get('access_token', '')
    refresh = data.get('refresh_token', '')
    expires_at = int(data.get('expires_at', 0))
    now = int(time.time())
    if expires_at > now:
        exp_delta = expires_at - now
        if exp_delta < 60:
            exp_str = f'in {exp_delta}s'
        elif exp_delta < 3600:
            exp_str = f'in {exp_delta // 60} min'
        else:
            exp_str = f'in {exp_delta // 3600} hr'
    elif expires_at:
        exp_str = f'{(now - expires_at) // 60} min ago (expired — will auto-refresh)'
    else:
        exp_str = 'unknown'
    return {
        'authorized': bool(access),
        'access_redacted': _redact_middle(access),
        'access_length': len(access),
        'refresh_present': bool(refresh),
        'refresh_redacted': _redact_middle(refresh) if refresh else '',
        'expires_at': time.strftime('%Y-%m-%d %H:%M:%S',
                                    time.localtime(expires_at)) if expires_at else '',
        'expires_when': exp_str,
        'scope': data.get('scope', ''),
        'pass_file_present': pass_file.exists(),
        'age': _mtime_hint(token_file),
    }


def _cert_hint(username, filename):
    """State of an uploaded client cert or key PEM file."""
    if not filename:
        return {'present': False}
    config_dir = get_user_config_dir(username)
    path = config_dir / filename
    if not path.exists():
        return {'present': False, 'error': f'{filename} missing from disk'}
    try:
        size = path.stat().st_size
    except OSError:
        return {'present': False, 'error': f'{filename} not readable'}
    # First 12 hex chars of SHA-256 — enough to verify "same file as before"
    # without leaking the actual key material.
    import hashlib as _hashlib
    try:
        h = _hashlib.sha256(path.read_bytes()).hexdigest()[:12]
    except OSError:
        h = ''
    return {
        'present': True,
        'filename': filename,
        'size_bytes': size,
        'sha256_head': h,
        'age': _mtime_hint(path),
    }


def _find_account(accounts, email):
    """Return (index, account_dict) for email, or (None, None)."""
    for i, acct in enumerate(accounts):
        if acct['email'] == email:
            return i, acct
    return None, None


@app.route('/account/<email>/edit', methods=['GET', 'POST'])
@login_required
def edit_account(email):
    """Per-account settings — display name, host/port, TLS, folders,
    timeout, client cert paths. Password + OAuth are managed by their
    own routes; this page links to them so an operator has ONE place
    to reach every configuration knob for the account."""
    username = session['username']
    accounts = load_accounts(username)
    idx, acct = _find_account(accounts, email)
    if acct is None:
        flash(f'Account {email} not found.')
        return redirect(url_for('dashboard'))
    provider = PROVIDERS.get(acct['provider'], {})

    if request.method == 'POST':
        # Blank field = "inherit provider default" (stored as None so the
        # generator falls back on PROVIDERS[]).
        def _blank_or(val):
            v = (val or '').strip()
            return v if v else None

        acct['display_name']    = _blank_or(request.form.get('display_name'))
        acct['host']            = _blank_or(request.form.get('host'))
        port_raw = _blank_or(request.form.get('port'))
        if port_raw:
            try:
                p = int(port_raw)
                if not (1 <= p <= 65535):
                    raise ValueError
                acct['port'] = p
            except ValueError:
                flash('Port must be an integer 1..65535 — left unchanged.')
                return render_template('edit_account.html', account=acct,
                                       provider=provider, tls_modes=TLS_MODES,
                                       providers=PROVIDERS)
        else:
            acct['port'] = None

        tls = (request.form.get('tls_mode') or '').strip().lower()
        acct['tls_mode'] = tls if tls in {'ssl', 'starttls', 'none'} else None

        acct['folder_pattern']  = _blank_or(request.form.get('folder_pattern'))
        timeout_raw = _blank_or(request.form.get('timeout_seconds'))
        if timeout_raw:
            try:
                t = int(timeout_raw)
                if not (1 <= t <= 3600):
                    raise ValueError
                acct['timeout_seconds'] = t
            except ValueError:
                flash('Timeout must be 1..3600 seconds — left unchanged.')
                return render_template('edit_account.html', account=acct,
                                       provider=provider, tls_modes=TLS_MODES,
                                       providers=PROVIDERS)
        else:
            acct['timeout_seconds'] = None

        # oauth_app_id: only accept a value that actually resolves. Blank
        # clears the pin and reverts this account to provider-default
        # resolution (user default → server default).
        oauth_app_id = _blank_or(request.form.get('oauth_app_id'))
        if oauth_app_id:
            from oauth2_microsoft import resolve_oauth_app
            resolved = resolve_oauth_app(CONFIG['data_dir'], username,
                                         app_id=oauth_app_id, provider='microsoft')
            if not resolved or resolved.get('app_id') != oauth_app_id:
                flash(f'OAuth app {oauth_app_id!r} not found — reverted to default.')
                oauth_app_id = None
        acct['oauth_app_id'] = oauth_app_id

        accounts[idx] = acct
        save_accounts(username, accounts)
        generate_mbsyncrc(username)
        flash(f'Saved settings for {email}.')
        return redirect(url_for('edit_account', email=email))

    # Build the dropdown list of OAuth apps this account could use.
    from oauth2_microsoft import list_oauth_apps
    available_oauth_apps = list_oauth_apps(CONFIG['data_dir'],
                                           username=username,
                                           provider='microsoft')

    return render_template('edit_account.html', account=acct,
                           provider=provider, tls_modes=TLS_MODES,
                           providers=PROVIDERS,
                           cred_hint=_credential_hint(username, email),
                           oauth_hint=_oauth2_token_hint(username, email),
                           cert_hint=_cert_hint(username, acct.get('client_cert')),
                           key_hint=_cert_hint(username, acct.get('client_key')),
                           available_oauth_apps=available_oauth_apps,
                           current_oauth_app_id=acct.get('oauth_app_id') or '')


def _save_cert_upload(username, email, field, form_field, allowed_ext_hint):
    """Save an uploaded PEM under ${DATA}/${user}/.config/, chown to the
    PAM user, and return the stored filename (or None on skip). Rejects
    unrecognized extensions and empty uploads."""
    f = request.files.get(form_field)
    if not f or not f.filename:
        return None, None
    fname = secure_filename(f.filename)
    ext = os.path.splitext(fname)[1].lower()
    if ext not in _ALLOWED_CERT_EXTS:
        return None, f'{field}: extension {ext!r} not allowed (use .pem/.crt/.cer/.key)'

    safe_name = email.replace('@', '_at_').replace('.', '_')
    stored = f'{safe_name}.{field}{ext}'
    config_dir = get_user_config_dir(username)
    dest = config_dir / stored
    data = f.read()
    if not data:
        return None, f'{field}: uploaded file was empty'
    # Sanity-check for PEM envelope (openssl-compatible). We don't do full
    # cryptographic validation here — mbsync will reject a broken PEM at
    # first-connect. A wrong-format upload just wastes a sync attempt.
    if b'-----BEGIN' not in data:
        return None, f'{field}: not a PEM file (no BEGIN marker)'
    dest.write_bytes(data)
    dest.chmod(0o600)
    _chown_user(dest, username)
    return stored, None


@app.route('/account/<email>/cert', methods=['POST'])
@login_required
def upload_account_cert(email):
    """Upload a client certificate + private key PEM for mutual-TLS IMAP.
    Both fields are optional — uploading only a cert clears the previous
    key path (rare, but valid if the key is embedded)."""
    username = session['username']
    accounts = load_accounts(username)
    idx, acct = _find_account(accounts, email)
    if acct is None:
        flash(f'Account {email} not found.')
        return redirect(url_for('dashboard'))

    cert_name, err = _save_cert_upload(username, email, 'cert', 'cert_file', '.pem/.crt/.cer')
    if err:
        flash(err)
        return redirect(url_for('edit_account', email=email))
    key_name, err = _save_cert_upload(username, email, 'key', 'key_file', '.pem/.key')
    if err:
        flash(err)
        return redirect(url_for('edit_account', email=email))

    if cert_name:
        acct['client_cert'] = cert_name
    if key_name:
        acct['client_key'] = key_name
    if not (cert_name or key_name):
        flash('No cert or key uploaded.')
        return redirect(url_for('edit_account', email=email))

    accounts[idx] = acct
    save_accounts(username, accounts)
    generate_mbsyncrc(username)
    flash(f'Uploaded client cert/key for {email}.')
    return redirect(url_for('edit_account', email=email))


@app.route('/account/<email>/cert/remove', methods=['POST'])
@login_required
def remove_account_cert(email):
    """Remove uploaded cert + key files and clear the paths from the
    account. Idempotent — safe on an account that never had one."""
    username = session['username']
    accounts = load_accounts(username)
    idx, acct = _find_account(accounts, email)
    if acct is None:
        flash(f'Account {email} not found.')
        return redirect(url_for('dashboard'))
    config_dir = get_user_config_dir(username)
    for field in ('client_cert', 'client_key'):
        fname = acct.get(field)
        if fname:
            path = config_dir / fname
            if path.exists():
                try:
                    path.unlink()
                except OSError:
                    pass
        acct[field] = None
    accounts[idx] = acct
    save_accounts(username, accounts)
    generate_mbsyncrc(username)
    flash(f'Removed client cert/key for {email}.')
    return redirect(url_for('edit_account', email=email))


@app.errorhandler(413)
def _too_large(e):
    """MAX_CONTENT_LENGTH tripped — the most common trigger is a bogus
    huge cert upload. Give a useful hint instead of Flask's default."""
    flash('Upload rejected: file exceeds 256 KiB. Client certs + keys '
          'should be well under 20 KiB. Check you selected the right file.')
    ref = request.referrer or url_for('dashboard')
    return redirect(ref)


@app.route('/account/<email>/schedule', methods=['POST'])
@login_required
def update_schedule(email):
    username = session['username']
    interval = request.form.get('sync_interval', 'daily')
    if interval not in SYNC_INTERVALS:
        interval = 'daily'
    accounts = load_accounts(username)
    for acct in accounts:
        if acct['email'] == email:
            acct['sync_interval'] = interval
            break
    save_accounts(username, accounts)
    label = SYNC_INTERVALS[interval]['label']
    flash(f'Sync schedule for {email} set to: {label}')
    return redirect(url_for('dashboard'))


# ----------------------------------------------------------------------
# Sync lifecycle — systemd-scoped, file-backed (1.1.0)
# ----------------------------------------------------------------------
# WebUI sync must not block the request for up to 3600s. Post launches a
# background driver thread that spawns mbsync inside a transient systemd
# scope, writes progress state to /run/mail-archiver/sync-jobs/<jid>.json
# every ~2s, and returns a job_id immediately. The SSE stream endpoint
# reads that state file — so any gunicorn worker (or any subsequent
# login) can attach to the same live job.
#
# 1.1.0 changes vs 1.0.14:
#   - No more in-process _SYNC_JOBS dict. State lives on disk, visible
#     to any worker.
#   - mbsync runs in a systemd scope, so worker recycle / crash / restart
#     does NOT SIGTERM the sync (see docs/README.md#sync-lifecycle).
#   - Cancel Sync = `systemctl stop mail-archiver-sync-<jobid>.scope`.
#   - Cron path (`python3 app.py scheduled-sync`) stays synchronous via
#     run_sync() below — no lifecycle overhead for a headless run.

import sync_lifecycle as _slc

# mbsync progress line: `C: 0/1 B: 0/12 M: +0/47 *0/0 #0/0`
#   C = channels, B = boxes (folders), M = messages,
#   * = flag changes, # = expunges. `+N/T` = done/total.
_MBSYNC_LINE_RE = re.compile(
    r'C:\s*(\d+)/(\d+)\s+B:\s*(\d+)/(\d+)\s+M:\s*\+(\d+)/(\d+)'
    r'\s+\*(\d+)/(\d+)\s+#(\d+)/(\d+)'
)


def _parse_mbsync_line(line):
    m = _MBSYNC_LINE_RE.search(line)
    if not m:
        return None
    (ch_d, ch_t, bx_d, bx_t,
     m_new_d, m_new_t, m_flg_d, m_flg_t, m_exp_d, m_exp_t) = map(int, m.groups())
    return {
        'channels_done': ch_d, 'channels_total': ch_t,
        'folders_done': bx_d, 'folders_total': bx_t,
        'msg_new_done': m_new_d, 'msg_new_total': m_new_t,
        'msg_flag_done': m_flg_d, 'msg_flag_total': m_flg_t,
        'msg_expunge_done': m_exp_d, 'msg_expunge_total': m_exp_t,
    }


def _new_sync_job(username, target, source='webui'):
    """Claim a new job for `target` (an email or '__all__').

    1.1.7: returns (job_id, None) on success, or (None, blocking_job) when a
    sync for an overlapping target is already in flight. Never queues: a
    second click, a second tab or a cron run that lands while a sync is
    running gets the running job back instead of starting another mbsync
    on the same maildirs.
    """
    _slc.purge_old_states(3600)
    return _slc.claim_sync(username, target, {
        'source': source,
        'state': 'starting',
        'started_epoch': time.time(),
        'started': _slc.iso_utc_now(),
        'started_local': time.strftime('%Y-%m-%d %H:%M:%S'),
        # 1.1.4: the worker that owns this job, plus the boot it belongs to.
        # The orphan reaper uses these to tell "worker died" from "worker is
        # busy measuring folders and has not spawned mbsync yet".
        'owner_pid': os.getpid(),
        'boot_id': _slc.current_boot_id(),
        # The cron path writes no progress while mbsync runs.
        'heartbeat': source != 'cron',
        'mbsync_pid': None,
        'progress': None,
        'last_line': '',
        'exit_code': None,
        'error': None,
        'finished': None,
    })


def _already_syncing_message(job):
    """Plain-language refusal shown when a sync is already in flight."""
    what = ('all accounts' if (job.get('target') or '__all__') == '__all__'
            else job.get('target'))
    since = job.get('started_local') or job.get('started') or 'earlier'
    if _slc.is_state_stalled(job):
        return (f'A sync of {what} started at {since} is still registered '
                f'but has reported nothing for over '
                f'{_slc.STALL_SECONDS // 60} minutes. A new sync was NOT '
                f'started, because it would collide with that one. Ask the '
                f'administrator to check it.')
    return (f'A sync of {what} is already running (started {since}). '
            f'Your click was ignored and nothing was queued. Please wait '
            f'for it to finish; a first sync of a large mailbox can take '
            f'hours, and clicking again does not make it faster.')


def _already_syncing_response(job):
    msg = _already_syncing_message(job)
    if _wants_json():
        return jsonify({
            'error': 'already_syncing',
            'message': msg,
            'stalled': _slc.is_state_stalled(job),
            'job_id': job.get('job_id'),
            'target': job.get('target'),
            'started': job.get('started_local') or job.get('started'),
            'stream_url': url_for('sync_job_stream', job_id=job.get('job_id')),
        }), 409
    flash(msg)
    return redirect(url_for('dashboard'))


def _get_sync_job(job_id):
    return _slc.read_state(job_id)


def _reap_if_dead(job):
    """If `job` claims to be in flight but nothing runs it any more, mark
    it failed (same wording as the orphan reaper) and return the new state."""
    if job and _slc.is_state_orphaned(job):
        _slc.reap_orphaned_states()
        return _slc.read_state(job['job_id']) or job
    return job


def _update_sync_job(job_id, **updates):
    _slc.update_state(job_id, **updates)


def _plan_batches_for_account(username, account_email):
    """Measure IMAP folder sizes and plan per-folder batches (1.0.15).

    Returns {'total_msgs': int, 'batches': list[list[str]],
             'folder_count': int} or None if measurement failed.

    In PAM mode, this MUST run as the target PAM user because the IMAP
    credentials (`.pass` + `.token` under ~<user>/.config/) are private
    to that user — the mail-archiver service uid can't read them. So we
    spawn a subprocess that drops privileges (subprocess.run with
    user=uid/group=gid, kernel setuid via our ambient CAP_SETUID) and
    have the child do the measurement + return the plan as JSON.

    In builtin mode (single-user container), we run inline — no priv
    boundary to cross.
    """
    if CONFIG['auth_mode'] != 'pam':
        return _plan_batches_inline(username, account_email)

    # PAM mode only (so Linux only): the paths in the child are those of
    # the packaged server.
    pw = _pc.user_info(username)
    if pw is None:
        return None
    child = (
        "import json, sys, os\n"
        "sys.path.insert(0, '/opt/mail-archiver')\n"
        "os.environ.setdefault('MAIL_ARCHIVER_AUTH', 'pam')\n"
        "os.environ.setdefault('MAIL_ARCHIVER_DATA', %r)\n"
        "os.environ.setdefault('MAIL_ARCHIVER_SECRET_FILE',\n"
        "    '/opt/mail-archiver/.secret_key')\n"
        "os.environ.setdefault('MAIL_ARCHIVER_BATCH_THRESHOLD', %r)\n"
        "import app\n"
        "plan = app._plan_batches_inline(%r, %r)\n"
        "print(json.dumps(plan))\n"
    ) % (CONFIG['data_dir'],
         str(CONFIG.get('batch_threshold', 5000)),
         username, account_email)
    try:
        result = subprocess.run(
            ['python3', '-c', child],
            user=pw.uid, group=pw.gid,
            extra_groups=_supplementary_gids(username, pw.gid),
            cwd=pw.home,
            env=_mbsync_child_env(pw, username),
            capture_output=True, text=True, timeout=60,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0:
        return None
    try:
        return json.loads(result.stdout.strip() or 'null')
    except (ValueError, json.JSONDecodeError):
        return None


def _plan_batches_inline(username, account_email):
    """The actual measurement — runs AS the target PAM user (post-drop),
    or directly in builtin mode."""
    from mail_batcher import measure_folders, plan_batches, BatcherError

    accounts = load_accounts(username)
    acct = next((a for a in accounts if a.get('email') == account_email), None)
    if not acct or not acct.get('enabled', True):
        return None

    provider = PROVIDERS.get(acct['provider'], {})
    host = acct.get('host') or provider.get('host', '')
    port = int(acct.get('port') or provider.get('port', 993))
    tls_default = 'ssl' if provider.get('tls', True) else 'none'
    tls_mode = (acct.get('tls_mode') or tls_default).lower()
    auth_type = acct.get('auth_type') or provider.get('auth', 'password')
    pattern = acct.get('folder_pattern') or '*'
    # 1.3.0: folders of an open initial / rebase sync job stay out of the
    # batches too (the batcher names folders explicitly).
    bulk_excluded = _bulk.excluded_folders_for(
        str(get_user_config_dir(username)), account_email)

    if auth_type == 'oauth2':
        safe_name = account_email.replace('@', '_at_').replace('.', '_')
        token_file = get_user_config_dir(username) / f'{safe_name}.token'
        if not token_file.exists():
            return None
        try:
            credential = token_file.read_text(encoding='utf-8').strip()
        except OSError:
            return None
    else:
        try:
            credential = load_credential(username, account_email)
        except Exception:
            return None
    if not credential:
        return None

    try:
        folders = measure_folders(
            host=host, port=port, tls_mode=tls_mode,
            email=account_email, credential=credential,
            auth_type=auth_type, folder_pattern=pattern)
    except BatcherError:
        return None
    folders = [(n, c) for n, c in folders if n not in bulk_excluded]

    total = sum(c for _, c in folders)
    threshold = int(os.environ.get('MAIL_ARCHIVER_BATCH_THRESHOLD',
                                   CONFIG.get('batch_threshold', 5000)))
    if threshold <= 0 or total <= threshold:
        return {'total_msgs': total, 'batches': [], 'folder_count': len(folders)}
    batches = plan_batches(folders, threshold)
    return {'total_msgs': total, 'batches': batches,
            'folder_count': len(folders)}


def _run_sync_background(job_id, username, account_email=None):
    """Threaded mbsync runner. Streams stderr into the file-backed job
    state at /run/mail-archiver/sync-jobs/<job_id>.json every ~2s.
    mbsync itself runs inside a transient systemd scope (1.1.0) so it
    survives worker recycling.

    Falls back to synchronous run_sync() when mbsync isn't installed
    (imaplib path — no line stream, single done event).
    """
    job = _get_sync_job(job_id)
    if not job:
        return

    # Small helper to batch state writes at ~2Hz so we don't hammer the
    # tmpfs with every stderr line. Also flushes on state transitions.
    _state_mem = {
        'last_line': job.get('last_line', ''),
        'progress': job.get('progress') or {},
        'last_flush': 0.0,
    }
    _flush_lock = threading.Lock()

    def _flush(force=False, **overrides):
        with _flush_lock:
            now = time.time()
            if not force and now - _state_mem['last_flush'] < 2.0:
                return
            _state_mem['last_flush'] = now
            payload = dict(overrides)
            payload['last_line'] = _state_mem['last_line']
            payload['progress'] = dict(_state_mem['progress'])
            _update_sync_job(job_id, **payload)

    try:
        if not _has_mbsync():
            _update_sync_job(job_id, state='syncing')
            result = run_sync(username, account_email)
            _update_sync_job(
                job_id,
                state='done' if result.get('state') == 'ok' else 'error',
                exit_code=result.get('exit_code'),
                error=result.get('error'),
                finished=time.strftime('%Y-%m-%d %H:%M:%S'),
            )
            return

        config_dir = get_user_config_dir(username)
        status_file = config_dir / 'sync_status.json'
        status = get_sync_status(username)
        key = account_email or '__all__'
        status[key] = {'state': 'syncing',
                       'started': time.strftime('%Y-%m-%d %H:%M:%S'),
                       **_sync_owner()}
        _write_sync_status_locked(status_file, status, keys=[key])

        # P0 (1.0.16): refresh OAuth tokens BEFORE spawning mbsync AND
        # BEFORE the batcher measurement. In 1.1.0 the timer-driven
        # refresh usually beats us to it, but keep the sync-path refresh
        # as a safety net for accounts added since the last timer tick.
        try:
            oauth_report = _refresh_oauth_tokens_before_sync(
                username, account_email)
            for e_addr, rep in (oauth_report or {}).items():
                if rep.get('needs_reauth'):
                    status[e_addr] = {
                        'state': 'error',
                        'finished': time.strftime('%Y-%m-%d %H:%M:%S'),
                        'error': ('Microsoft sign-in expired. Click '
                                  '"Sign in with Microsoft" on the '
                                  'account settings page to renew.'),
                        'needs_reauth': True,
                    }
                    _write_sync_status_locked(status_file, status,
                                              keys=[e_addr])
        except Exception as e:
            _state_mem['last_line'] = f'oauth pre-refresh error: {e}'
            _flush(force=True)

        if account_email:
            # same name as generate_mbsyncrc (see run_sync: stripping the
            # hyphen here asked isync for a channel that does not exist)
            mbsync_arg = _bulk.safe_account_name(account_email)
        else:
            mbsync_arg = '-a'

        # Build the mbsync argv + drop-priv info. In 1.1.0 the actual
        # spawn goes through sync_lifecycle.spawn_scoped_mbsync — no more
        # subprocess(user=, group=) + Ambient CAP_SETUID dance. systemd
        # handles the uid/gid drop via `systemd-run --uid --gid`.
        target_uid = None
        target_gid = None
        target_cwd = None
        target_env = None
        target_groups = None
        if CONFIG['auth_mode'] == 'pam':
            pw = _pc.user_info(username)
            if pw is None:
                raise ValueError(
                    f'PAM user {username!r} not present on this host — '
                    f'cannot run mbsync as that identity')
            target_uid = pw.uid
            target_gid = pw.gid
            target_cwd = pw.home
            target_env = _mbsync_child_env(pw, username)
            target_groups = _supplementary_gids(username, pw.gid)
            mbsync_argv = ['mbsync', '-V', mbsync_arg]
        else:
            archive_dir = Path(CONFIG['data_dir']) / username
            rc_path = archive_dir / '.mbsyncrc'
            mbsync_argv = ['mbsync', '-V', '-c', str(rc_path), mbsync_arg]

        _update_sync_job(job_id, state='running')
        started = time.time()
        tail_lines = []

        # 1.0.15: try to plan batches when syncing a single named account
        # and threshold is set. Batching for `-a` is out of scope.
        batch_plan = None
        if account_email and CONFIG.get('batch_threshold', 0) > 0:
            try:
                batch_plan = _plan_batches_for_account(
                    username, account_email)
            except Exception as e:
                _state_mem['last_line'] = f'batcher: measure failed: {e}'
                _flush(force=True)

        def _line_cb(line):
            line = redact_sync_output(line)
            tail_lines.append(line)
            if len(tail_lines) > 200:
                tail_lines.pop(0)
            parsed = _parse_mbsync_line(line)
            _state_mem['last_line'] = line
            if parsed:
                p = dict(_state_mem['progress'])
                # 1.1.0 (via 1.0.17 chunk-only mode): if the chunk loop
                # has set chunk_mode=True on the progress dict, DON'T
                # overwrite folder_msg_done from mbsync's own C:/M: —
                # the chunk loop's before/after count is truth there.
                if p.get('chunk_mode'):
                    # Take everything EXCEPT folder_msg_done/total from
                    # the mbsync C:/B:/M: parsed line, since those reset
                    # every chunk to the same total.
                    keep = {k: v for k, v in parsed.items()
                            if k not in ('folders_done', 'folders_total',
                                         'msg_new_done', 'msg_new_total')}
                    p.update(keep)
                else:
                    p.update(parsed)
                _state_mem['progress'] = p
            _flush()

        if batch_plan and batch_plan['total_msgs'] > CONFIG['batch_threshold']:
            from mail_batcher import run_batched_sync

            def _prog_cb(update):
                p = dict(_state_mem['progress'])
                st = update.get('state')
                # Batch-level start clears within-batch counters so the
                # UI doesn't carry stale per-folder numbers across batches.
                if st == 'syncing':
                    for f in ('folders_done', 'folders_total',
                              'msg_new_done', 'msg_new_total',
                              'msg_flag_done', 'msg_flag_total',
                              'folder_name', 'folder_msg_done',
                              'folder_msg_total', 'folder_chunk_current',
                              'folder_chunk_wall_seconds', 'chunk_mode'):
                        p.pop(f, None)
                # Persist chunk_mode / batch_* / folder_* fields
                for k, v in update.items():
                    if k == 'chunk_mode' or k.startswith('batch_') \
                            or k.startswith('folder_'):
                        p[k] = v
                _state_mem['progress'] = p
                if st in ('batch_retry', 'batch_failed'):
                    _state_mem['last_line'] = (
                        f"{st} batch "
                        f"{update.get('batch_current')}/"
                        f"{update.get('batch_total')}")
                _flush(force=True)

            safe_ch = re.sub(r'[^a-zA-Z0-9_]', '',
                             account_email.replace('@', '_at_')
                             .replace('.', '_'))
            batch_rc = (target_env or {}).get('HOME')
            if batch_rc:
                batch_rc = str(Path(batch_rc) / '.mbsyncrc')
            else:
                batch_rc = str(Path(CONFIG['data_dir']) / username / '.mbsyncrc')
            # NOTE: The batcher still spawns mbsync via subprocess.Popen
            # directly (multiple invocations per batch), not through the
            # systemd scope. That's OK — those child mbsync processes
            # inherit the parent scope, so they still survive worker
            # recycling. The parent scope is what matters for lifetime.
            sp_kwargs = {}
            if target_uid is not None:
                sp_kwargs['user'] = target_uid
                sp_kwargs['group'] = target_gid
                if target_groups:
                    sp_kwargs['extra_groups'] = list(target_groups)
                sp_kwargs['cwd'] = target_cwd
                sp_kwargs['env'] = dict(target_env or {})
            acct_slug = re.sub(r'[^a-zA-Z0-9_]', '',
                               account_email.replace('@', '_at_')
                               .replace('.', '_'))
            maildir_base = str(
                Path(CONFIG['data_dir']) / username / acct_slug)
            # Launch a placeholder scope so cancel + list_active_scopes
            # still work for a batched run. We use systemd-run to drop a
            # long-lived sleep in the scope; the batcher's mbsync children
            # run in the same slice (as gunicorn worker children). This
            # gives cross-worker enumeration via list_active_scopes without
            # forcing the batcher to be systemd-aware.
            #
            # Trade-off: the mbsync children aren't literally in the scope
            # unit — they're in the mail-archiver.service unit. So a
            # `systemctl stop mail-archiver-sync-<jid>.scope` won't kill
            # active mbsync in the batched-mode path. The cancel button
            # instead relies on the state file's mbsync_pid + os.killpg.
            # Documented in the Cancel path.
            rc = run_batched_sync(
                channel=safe_ch,
                mbsyncrc_path=batch_rc,
                batches=batch_plan['batches'],
                subprocess_kwargs=sp_kwargs,
                progress_cb=_prog_cb,
                line_cb=_line_cb,
                per_batch_timeout=3600,
                maildir_base=maildir_base,
            )
        else:
            # Single-shot mbsync — this IS run through the systemd scope.
            # Cancel works cleanly via `systemctl stop <scope>`.
            proc = _slc.spawn_scoped_mbsync(
                job_id=job_id,
                argv=mbsync_argv,
                email=account_email,
                uid=target_uid,
                gid=target_gid,
                extra_groups=target_groups,
                cwd=target_cwd,
                env=target_env,
            )
            _update_sync_job(job_id, mbsync_pid=proc.pid)
            try:
                for raw_line in _slc.iter_mbsync_lines(proc.stdout):
                    _line_cb(raw_line.rstrip())
            finally:
                proc.wait()
            rc = proc.returncode

        duration = int(time.time() - started)
        raw_tail = '\n'.join(tail_lines[-20:])
        finished_local = time.strftime('%Y-%m-%d %H:%M:%S')

        if rc == 0:
            _update_sync_job(job_id, state='done', exit_code=rc,
                             finished=finished_local, duration_s=duration)
            status[key] = {
                'state': 'ok',
                'finished': finished_local,
                'exit_code': 0,
                'error': '',
            }
        elif rc == 2:
            # 1.0.15 batched sync: partial success (some batches failed
            # after retry). Report as 'done' with a warning surfaced.
            _update_sync_job(job_id, state='done', exit_code=rc,
                             finished=finished_local, duration_s=duration)
            status[key] = {
                'state': 'ok',
                'finished': finished_local,
                'exit_code': 2,
                'error': 'Some folder batches failed — see log for details',
            }
        else:
            friendly = friendly_sync_error(raw_tail, account_email or '', rc)
            _update_sync_job(job_id, state='error', exit_code=rc,
                             error=friendly, finished=finished_local,
                             duration_s=duration)
            status[key] = {
                'state': 'error',
                'finished': finished_local,
                'exit_code': rc,
                'error': friendly,
                'raw_error': raw_tail,
            }

        _flush(force=True)
        _write_sync_status_locked(status_file, status, keys=[key])
        _chown_user(status_file, username)

        if status[key].get('state') == 'ok':
            try:
                from search_index import index_maildir
                index_maildir(username, CONFIG['data_dir'],
                              account_filter=account_email)
            except Exception:
                pass
    except Exception as e:
        _update_sync_job(job_id, state='error', error=str(e),
                         finished=time.strftime('%Y-%m-%d %H:%M:%S'))


def _wants_json():
    return ('application/json' in request.headers.get('Accept', '')
            or request.headers.get('X-Requested-With', '') == 'XMLHttpRequest')


@app.route('/sync', methods=['POST'])
@login_required
def sync_all():
    username = session['username']
    job_id, running = _new_sync_job(username, '__all__')
    if running:
        return _already_syncing_response(running)
    t = threading.Thread(target=_run_sync_background,
                         args=(job_id, username, None), daemon=True)
    t.start()
    if _wants_json():
        return jsonify({
            'job_id': job_id,
            'stream_url': url_for('sync_job_stream', job_id=job_id),
            'status_url': url_for('sync_job_status', job_id=job_id),
        }), 202
    flash('Sync started for all accounts.')
    return redirect(url_for('dashboard'))


@app.route('/sync/<email>', methods=['POST'])
@login_required
def sync_account(email):
    username = session['username']
    job_id, running = _new_sync_job(username, email)
    if running:
        return _already_syncing_response(running)
    t = threading.Thread(target=_run_sync_background,
                         args=(job_id, username, email), daemon=True)
    t.start()
    if _wants_json():
        return jsonify({
            'job_id': job_id,
            'stream_url': url_for('sync_job_stream', job_id=job_id),
            'status_url': url_for('sync_job_status', job_id=job_id),
        }), 202
    flash(f'Sync started for {email}.')
    return redirect(url_for('dashboard'))


@app.route('/api/sync/<job_id>')
@login_required
def sync_job_status(job_id):
    job = _reap_if_dead(_get_sync_job(job_id))
    if not job or job.get('user') != session['username']:
        return jsonify({'error': 'not_found'}), 404
    return jsonify({
        'job_id': job_id,
        'state': job.get('state'),
        'target': job.get('target'),
        'source': job.get('source'),
        'heartbeat': job.get('heartbeat', True),
        'started': job.get('started_local') or job.get('started'),
        'finished': job.get('finished'),
        'exit_code': job.get('exit_code'),
        'error': job.get('error'),
        'progress': job.get('progress'),
        'last_line': job.get('last_line'),
    })


@app.route('/api/sync/<job_id>/stream')
@login_required
def sync_job_stream(job_id):
    # Authorize via file-backed state — any worker can serve this.
    job = _get_sync_job(job_id)
    if not job or job.get('user') != session['username']:
        return jsonify({'error': 'not_found'}), 404

    def event_stream():
        last_progress = None
        last_heartbeat = time.time()
        last_reap_check = 0.0
        stream_start = time.time()
        # 1.1.11: say what is running straight away. A scheduled (cron)
        # sync writes no progress, so without this the page sat on
        # "Reconnecting to in-flight sync..." for the whole run.
        yield ('event: state\ndata: '
               + json.dumps({'state': job.get('state'),
                             'target': job.get('target'),
                             'source': job.get('source'),
                             'heartbeat': job.get('heartbeat', True),
                             'started': job.get('started_local')
                                        or job.get('started')})
               + '\n\n')
        # Poll the state file every 500ms. Cheap (small file, tmpfs).
        while True:
            current = _get_sync_job(job_id)
            # 1.1.11: a job whose worker died (service restart, worker
            # recycle) stays 'running' on disk; only the dashboard render
            # used to reap it, so an open stream pinged forever. Check
            # every 10s and turn it into a final error event.
            if current and time.time() - last_reap_check >= 10:
                last_reap_check = time.time()
                current = _reap_if_dead(current)
            if not current:
                yield 'event: error\ndata: {"error":"job_gone"}\n\n'
                return
            state = current.get('state')
            progress = current.get('progress')
            error = current.get('error')
            exit_code = current.get('exit_code')
            last_line = current.get('last_line')
            finished = current.get('finished')

            if progress and progress != last_progress:
                last_progress = progress
                yield ('event: progress\ndata: '
                       + json.dumps({'progress': progress,
                                     'last_line': last_line})
                       + '\n\n')

            if state == 'done':
                yield ('event: done\ndata: '
                       + json.dumps({'exit_code': exit_code,
                                     'finished': finished,
                                     'last_line': last_line,
                                     'progress': progress})
                       + '\n\n')
                return
            if state == 'error':
                yield ('event: error\ndata: '
                       + json.dumps({'error': error,
                                     'exit_code': exit_code,
                                     'finished': finished,
                                     'last_line': last_line})
                       + '\n\n')
                return
            if state == 'cancelled':
                yield ('event: error\ndata: '
                       + json.dumps({'error': 'cancelled by operator',
                                     'exit_code': exit_code,
                                     'finished': finished,
                                     'last_line': last_line})
                       + '\n\n')
                return

            now = time.time()
            if now - last_heartbeat >= 5:
                last_heartbeat = now
                yield 'event: ping\ndata: {}\n\n'

            if now - stream_start > 3500:
                yield 'event: timeout\ndata: {"error":"stream_capped"}\n\n'
                return

            time.sleep(0.5)

    resp = Response(event_stream(), mimetype='text/event-stream')
    resp.headers['Cache-Control'] = 'no-cache'
    resp.headers['X-Accel-Buffering'] = 'no'
    return resp


@app.route('/api/status')
@login_required
def api_status():
    username = session['username']
    return jsonify(get_sync_status(username))


def _parse_email_file(filepath):
    """Parse a Maildir message file into a dict with decoded headers and body.

    Handles MIME multipart, base64, quoted-printable, RFC 2047 encoded headers,
    and various charsets gracefully — returns best-effort text for all providers.
    """
    try:
        raw = filepath.read_bytes()
        msg = email.message_from_bytes(raw, policy=email.policy.default)
    except Exception:
        return None

    result = {
        'subject': str(msg.get('Subject', '')) or '(no subject)',
        'from': str(msg.get('From', '')),
        'to': str(msg.get('To', '')),
        'date': str(msg.get('Date', '')),
        'message_id': str(msg.get('Message-ID', '')),
    }

    # Extract plain text body
    body_parts = []
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            if ct == 'text/plain':
                try:
                    body_parts.append(part.get_content())
                except Exception:
                    # Fallback: try raw payload with charset detection
                    payload = part.get_payload(decode=True)
                    if payload:
                        for enc in ('utf-8', 'latin-1', 'windows-1252'):
                            try:
                                body_parts.append(payload.decode(enc))
                                break
                            except (UnicodeDecodeError, LookupError):
                                continue
            elif ct == 'text/html' and not body_parts:
                # Use HTML only if no plain text found
                try:
                    html = part.get_content()
                    # Strip HTML tags for search
                    body_parts.append(re.sub(r'<[^>]+>', ' ', html))
                except Exception:
                    pass
    else:
        try:
            body_parts.append(msg.get_content())
        except Exception:
            payload = msg.get_payload(decode=True)
            if payload:
                for enc in ('utf-8', 'latin-1', 'windows-1252'):
                    try:
                        body_parts.append(payload.decode(enc))
                        break
                    except (UnicodeDecodeError, LookupError):
                        continue

    result['body'] = '\n'.join(body_parts)
    return result


def search_maildir(username, query, account_filter=None, max_results=100):
    """Search a user's archived mail using grep + email parsing.

    Two-pass approach for performance:
    1. Fast grep across raw Maildir files for candidate matches
    2. Parse only matching files with Python email module for display
    """
    archive_dir = Path(CONFIG['data_dir']) / username
    if not archive_dir.exists():
        return []

    # Build list of directories to search
    search_dirs = []
    if account_filter:
        safe_name = account_filter.replace('@', '_at_').replace('.', '_')
        acct_dir = archive_dir / safe_name
        if acct_dir.exists():
            search_dirs.append(str(acct_dir))
    else:
        for d in archive_dir.iterdir():
            # 1.3.0: _import holds files waiting to be imported, not Maildirs
            if (d.is_dir() and not d.name.startswith('.')
                    and d.name != _pst.IMPORT_DIRNAME):
                search_dirs.append(str(d))

    if not search_dirs:
        return []

    # Pass 1: Fast grep for candidate files
    # Use grep -rl for file list, case-insensitive, binary-safe
    candidate_files = []

    def _in_maildir(path):
        # .../cur/<file> or .../new/<file>, with either path separator
        parts = path.replace('\\', '/').split('/')
        return 'cur' in parts[:-1] or 'new' in parts[:-1]

    if _pc.have_tool('grep'):
        try:
            cmd = [
                'grep', '-rl', '-i', '--include=*', '-m', '1',
                '--', query
            ] + search_dirs
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=30
            )
            if result.returncode == 0:
                candidate_files = [
                    f for f in result.stdout.strip().split('\n')
                    if f and _in_maildir(f)
                ]
        except (subprocess.TimeoutExpired, Exception):
            pass
    else:
        # No grep (Windows): the same first pass in Python, stopped at
        # the same 30 seconds.
        needle = query.lower().encode('utf-8', 'replace')
        deadline = time.monotonic() + 30
        for top in search_dirs:
            for root, _dirs, files in os.walk(top):
                if os.path.basename(root) not in ('cur', 'new'):
                    continue
                for name in files:
                    fpath = os.path.join(root, name)
                    try:
                        with open(fpath, 'rb') as fh:
                            if needle in fh.read().lower():
                                candidate_files.append(fpath)
                    except OSError:
                        pass
                    if (len(candidate_files) >= max_results * 2
                            or time.monotonic() > deadline):
                        break
                else:
                    continue
                break
            else:
                continue
            break

    # Cap candidates before parsing
    candidate_files = candidate_files[:max_results * 2]

    # Pass 2: Parse matching files for display
    results = []
    query_lower = query.lower()
    for fpath in candidate_files:
        if len(results) >= max_results:
            break
        fp = Path(fpath)
        parsed = _parse_email_file(fp)
        if not parsed:
            continue

        # Verify match in decoded content (grep matched raw, but we want
        # to confirm against decoded headers/body for accuracy)
        searchable = '\n'.join([
            parsed['subject'], parsed['from'], parsed['to'], parsed['body']
        ]).lower()
        if query_lower not in searchable:
            continue

        # Determine which account and folder this belongs to
        rel = fp.relative_to(archive_dir)
        parts = rel.parts
        parsed['account'] = parts[0].replace('_at_', '@').replace('_', '.') if parts else ''
        # Folder: everything between account dir and cur/new
        folder_parts = []
        for p in parts[1:]:
            if p in ('cur', 'new', 'tmp'):
                break
            folder_parts.append(p)
        parsed['folder'] = '/'.join(folder_parts) if folder_parts else 'INBOX'

        # Generate snippet with context around match
        snippet = _make_snippet(parsed['body'], query, context_chars=120)
        parsed['snippet'] = snippet
        parsed['filepath'] = str(fp)

        results.append(parsed)

    # Sort by date (newest first), best-effort parsing
    from email.utils import parsedate_tz, mktime_tz
    def _sort_key(r):
        try:
            parsed_date = parsedate_tz(r['date'])
            if parsed_date:
                return mktime_tz(parsed_date)
        except Exception:
            pass
        return 0
    results.sort(key=_sort_key, reverse=True)

    return results


def _make_snippet(text, query, context_chars=120):
    """Extract a snippet from text around the first match of query."""
    if not text:
        return ''
    idx = text.lower().find(query.lower())
    if idx == -1:
        # Match was in headers, show start of body
        return text[:context_chars * 2].strip() + ('...' if len(text) > context_chars * 2 else '')

    start = max(0, idx - context_chars)
    end = min(len(text), idx + len(query) + context_chars)
    snippet = text[start:end].strip()

    # Clean up whitespace
    snippet = re.sub(r'\s+', ' ', snippet)

    prefix = '...' if start > 0 else ''
    suffix = '...' if end < len(text) else ''
    return f'{prefix}{snippet}{suffix}'


@app.route('/search', methods=['GET'])
@login_required
def search():
    username = session['username']
    query = request.args.get('q', '').strip()
    account_filter = request.args.get('account', '')
    date_from = request.args.get('date_from', '')
    date_to = request.args.get('date_to', '')
    sender_filter = request.args.get('sender', '')
    recipient_filter = request.args.get('recipient', '')
    has_attachment = request.args.get('has_attachment', '')
    search_in = request.args.get('search_in', '')
    export_format = request.args.get('format', '')

    accounts = load_accounts(username)
    results = []
    search_time = 0
    showing_recent = False
    total_in_index = 0

    has_filters = any([account_filter, date_from, date_to, sender_filter,
                       recipient_filter, has_attachment])

    # Treat bare "*" as a filter-only browse (FTS5 doesn't support * as match-all)
    is_wildcard = query in ('*', '**')

    if query and len(query) >= 2 and not is_wildcard:
        # Use FTS5 index if available, fall back to grep
        # Apply column filter if search_in is set
        fts_query = query
        if search_in in ('subject', 'body', 'sender'):
            fts_query = f'{search_in}:{query}'
        try:
            from search_index import search_fts
            fts_result = search_fts(
                username, CONFIG['data_dir'], fts_query,
                account_filter=account_filter or None,
                date_from=date_from or None,
                date_to=date_to or None,
                sender_filter=sender_filter or None,
                recipient_filter=recipient_filter or None,
                has_attachment=True if has_attachment == 'yes' else (False if has_attachment == 'no' else None),
                max_results=200,
            )
            results = fts_result.get('results', [])
            search_time = fts_result.get('query_time', 0)
        except Exception:
            # Fallback to grep-based search
            t0 = time.time()
            results = search_maildir(username, query,
                                     account_filter=account_filter or None)
            search_time = round(time.time() - t0, 2)
    elif not query or is_wildcard:
        # No query — show top 10 recent emails (or filtered by date/account)
        try:
            from search_index import get_recent_emails
            recent = get_recent_emails(
                username, CONFIG['data_dir'],
                account_filter=account_filter or None,
                date_from=date_from or None,
                date_to=date_to or None,
                max_results=10,
            )
            results = recent.get('results', [])
            total_in_index = recent.get('total', 0)
            search_time = recent.get('query_time', 0)
            showing_recent = True
        except Exception:
            pass

    # Export handler
    if export_format in ('mbox', 'eml') and results:
        return _export_results(results, export_format, query, username)

    return render_template('search.html',
                           username=username,
                           query=query,
                           account_filter=account_filter,
                           date_from=date_from,
                           date_to=date_to,
                           sender_filter=sender_filter,
                           recipient_filter=recipient_filter,
                           has_attachment=has_attachment,
                           search_in=search_in,
                           accounts=accounts,
                           results=results,
                           search_time=search_time,
                           showing_recent=showing_recent,
                           total_in_index=total_in_index)


def _export_results(results, fmt, query, username):
    """Export search results as MBOX or EML zip."""
    from io import BytesIO
    import zipfile

    # Only files under the user's OWN folder (1.3.2; until then the whole
    # data directory passed, as the viewer did before 1.3.1). The paths
    # come from the user's own search index, so this is a second fence.
    own_root = _readable_root(username)

    def _safe_path(filepath):
        """Return True only if filepath is under the user's own folder."""
        if not filepath:
            return False
        real = os.path.realpath(filepath)
        return _path_is_under(real, own_root) and os.path.isfile(real)

    if fmt == 'mbox':
        # MBOX format: concatenate raw email files with From_ separator
        output = BytesIO()
        for r in results:
            filepath = r.get('filepath', '')
            if _safe_path(filepath):
                with open(filepath, 'rb') as f:
                    raw = f.read()
                output.write(b'From mail-archiver@localhost ')
                output.write(time.strftime('%a %b %d %H:%M:%S %Y\n').encode())
                output.write(raw)
                output.write(b'\n')
        output.seek(0)
        from flask import send_file
        safe_query = re.sub(r'[^\w\-]', '_', query)[:30]
        return send_file(output, mimetype='application/mbox',
                         as_attachment=True,
                         download_name=f'search_{safe_query}.mbox')

    elif fmt == 'eml':
        # EML zip: each email as a .eml file in a zip
        output = BytesIO()
        with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as zf:
            for i, r in enumerate(results):
                filepath = r.get('filepath', '')
                if _safe_path(filepath):
                    safe_subj = re.sub(r'[^\w\-]', '_', r.get('subject', 'email'))[:40]
                    zf.write(filepath, f'{i+1:04d}_{safe_subj}.eml')
        output.seek(0)
        from flask import send_file
        safe_query = re.sub(r'[^\w\-]', '_', query)[:30]
        return send_file(output, mimetype='application/zip',
                         as_attachment=True,
                         download_name=f'search_{safe_query}.zip')


def _readable_root(username):
    """The directory a logged-in user's message paths must lie under.

    Always the user's OWN folder. Up to 1.3.0 this was the whole data
    directory, so a logged-in user could open another user's message by
    passing its path (and, on Windows, the application's own key files).
    """
    data_root = os.path.realpath(CONFIG['data_dir'])
    return os.path.join(data_root, username)


def _path_is_under(real_path, root):
    # normcase: Windows paths compare without case; a no-op on Linux.
    return os.path.normcase(real_path).startswith(os.path.normcase(root) + os.sep)


@app.route('/email/view')
@login_required
def view_email():
    """View a full email from the archive — headers, body, attachments."""
    username = session['username']
    filepath = request.args.get('path', '')

    # Security: path must be under user's data directory
    data_root = os.path.realpath(CONFIG['data_dir'])
    if not filepath:
        abort(400)
    real_path = os.path.realpath(filepath)
    if not _path_is_under(real_path, _readable_root(username)) or not os.path.isfile(real_path):
        abort(403)

    # Parse the raw email
    try:
        with open(real_path, 'rb') as _fh:
            raw_bytes = _fh.read()
        msg = email.message_from_bytes(raw_bytes, policy=email.policy.default)
    except Exception:
        flash('Could not read email file.')
        return redirect(url_for('search'))

    # Extract headers
    headers = {
        'subject': str(msg.get('Subject', '')) or '(no subject)',
        'from': str(msg.get('From', '')),
        'to': str(msg.get('To', '')),
        'cc': str(msg.get('Cc', '')),
        'date': str(msg.get('Date', '')),
        'message_id': str(msg.get('Message-ID', '')),
        'reply_to': str(msg.get('Reply-To', '')),
    }

    # Extract body — prefer HTML for rendering, keep plain text as fallback
    plain_body = ''
    html_body = ''
    attachments = []

    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            cd = str(part.get('Content-Disposition', ''))
            fname = part.get_filename()

            if fname or 'attachment' in cd:
                # It's an attachment
                attachments.append({
                    'filename': fname or 'unnamed',
                    'content_type': ct,
                    'size': len(part.get_payload(decode=True) or b''),
                    'index': len(attachments),
                })
            elif ct == 'text/plain' and not plain_body:
                try:
                    plain_body = part.get_content()
                except Exception:
                    payload = part.get_payload(decode=True)
                    if payload:
                        plain_body = payload.decode('utf-8', errors='replace')
            elif ct == 'text/html' and not html_body:
                try:
                    html_body = part.get_content()
                except Exception:
                    payload = part.get_payload(decode=True)
                    if payload:
                        html_body = payload.decode('utf-8', errors='replace')
    else:
        ct = msg.get_content_type()
        try:
            content = msg.get_content()
        except Exception:
            payload = msg.get_payload(decode=True)
            content = payload.decode('utf-8', errors='replace') if payload else ''
        if ct == 'text/html':
            html_body = content
        else:
            plain_body = content

    # Determine account from filepath
    account = ''
    try:
        rel = os.path.relpath(real_path, os.path.join(data_root, username))
        parts = rel.split(os.sep)
        if parts:
            account = parts[0].replace('_at_', '@').replace('_', '.')
    except Exception:
        pass

    return render_template('view_email.html',
                           username=username,
                           headers=headers,
                           plain_body=plain_body,
                           html_body=html_body,
                           attachments=attachments,
                           filepath=filepath,
                           account=account)


@app.route('/email/attachment')
@login_required
def download_attachment():
    """Download a single attachment from an archived email."""
    username = session['username']
    filepath = request.args.get('path', '')
    att_index = request.args.get('index', 0, type=int)

    # Security: path must be under user's data directory
    data_root = os.path.realpath(CONFIG['data_dir'])
    if not filepath:
        abort(400)
    real_path = os.path.realpath(filepath)
    if not _path_is_under(real_path, _readable_root(username)) or not os.path.isfile(real_path):
        abort(403)

    try:
        with open(real_path, 'rb') as _fh:
            raw_bytes = _fh.read()
        msg = email.message_from_bytes(raw_bytes, policy=email.policy.default)
    except Exception:
        abort(404)

    # Walk to find the Nth attachment
    idx = 0
    for part in msg.walk():
        cd = str(part.get('Content-Disposition', ''))
        fname = part.get_filename()
        if fname or 'attachment' in cd:
            if idx == att_index:
                payload = part.get_payload(decode=True) or b''
                ct = part.get_content_type()
                from io import BytesIO
                from flask import send_file
                return send_file(
                    BytesIO(payload),
                    mimetype=ct,
                    as_attachment=True,
                    download_name=fname or f'attachment_{idx}',
                )
            idx += 1

    abort(404)


@app.route('/index/rebuild', methods=['POST'])
@login_required
def rebuild_search_index():
    """Rebuild the FTS5 search index from Maildir."""
    from search_index import rebuild_index
    username = session['username']
    try:
        result = rebuild_index(username, CONFIG['data_dir'])
    except PermissionError:
        if not _pc.IS_WINDOWS:
            raise
        # Windows: a damaged index cannot be moved aside while a sync or an
        # import still has it open.
        flash('The search index is in use by a running sync or import. '
              'Try again when it has finished.')
        return redirect(url_for('search'))
    flash(f'Search index rebuilt: {result["indexed"]} emails indexed in {result["total_time"]:.1f}s.')
    return redirect(url_for('search'))


# 1.2.0: accounts on different IMAP servers sync in parallel, at most this
# many at once; accounts on the same server never overlap (see
# sync_lifecycle "daily schedule, one lane per IMAP server").
MAX_PARALLEL_SYNCS = max(1, int(os.environ.get('MAIL_ARCHIVER_MAX_PARALLEL_SYNCS', '4')))
# Hour of the first slot on every server; the rest follow every 24/n hours.
SCHEDULE_START_HOUR = int(os.environ.get('MAIL_ARCHIVER_SCHEDULE_START_HOUR', '0')) % 24


def _account_server(acct):
    """The IMAP server an account syncs from, as a schedule lane key."""
    provider = PROVIDERS.get(acct.get('provider', ''), {})
    return _slc.server_key(acct.get('host') or provider.get('host', ''),
                           acct.get('provider'))


def _status_epoch(entry):
    """When the account's last sync started (finished, for entries written
    before 1.2.0 recorded the start)."""
    for field in ('started', 'finished'):
        value = (entry or {}).get(field)
        if value:
            try:
                return time.mktime(time.strptime(value, '%Y-%m-%d %H:%M:%S'))
            except ValueError:
                pass
    return None


def sync_schedule_plan(now=None):
    """Every scheduled account with its lane, slot and due state.

    Returns a list of dicts sorted by server then slot: user, email,
    server, interval, interval_secs, slot, last_started, due, slot_epoch.
    """
    now = now or time.time()
    data_dir = Path(CONFIG['data_dir'])
    if not data_dir.exists():
        return []
    rows = []
    for userdir in sorted(data_dir.iterdir()):
        if not userdir.is_dir() or userdir.name.startswith('.'):
            continue
        if _pc.IS_WINDOWS and not (userdir / '.config').is_dir():
            # There the data directory also holds the application's own
            # folders (certs): not a user, and load_accounts() would
            # create a .config inside it.
            continue
        username = userdir.name
        accounts = load_accounts(username)
        if not accounts:
            continue
        sync_status = get_sync_status(username)
        for acct in accounts:
            if not acct.get('enabled', True):
                continue
            interval_key = acct.get('sync_interval', 'daily')
            interval_secs = SYNC_INTERVALS.get(interval_key, {}).get('seconds', 86400)
            if interval_secs == 0:
                continue
            rows.append({
                'user': username, 'email': acct['email'],
                'server': _account_server(acct),
                'interval': interval_key, 'interval_secs': interval_secs,
                'last_started': _status_epoch(sync_status.get(acct['email'])),
            })
    slots = _slc.assign_slots(((r['user'], r['email'], r['server']) for r in rows),
                              SCHEDULE_START_HOUR)
    for r in rows:
        r['slot'] = slots[(r['user'], r['email'])]
        r['slot_epoch'] = _slc.last_slot_epoch(now, r['slot'], r['interval_secs'])
        r['due'] = _slc.is_due(now, r['slot'], r['interval_secs'], r['last_started'])
    rows.sort(key=lambda r: (r['server'], r['slot'], r['user'], r['email']))
    return rows


def _busy_servers():
    """Servers with a sync in flight right now, from any source (an
    earlier hour's cron run, a WebUI click, a "Sync all")."""
    busy = {}
    _slc.reap_orphaned_states()
    for st in _slc.list_all_states():
        if st.get('state') not in _slc.ACTIVE_STATES:
            continue
        user, target = st.get('user'), st.get('target') or '__all__'
        for acct in load_accounts(user) if user else []:
            if target == '__all__' or acct['email'] == target:
                busy.setdefault(_account_server(acct), st)
    return busy


def _scheduled_sync_one(username, email, interval_key):
    # 1.1.7: the cron run claims the account like a WebUI click does, so
    # it never overlaps a sync already in flight (a WebUI sync, or the
    # previous hour's cron run still downloading a large mailbox).
    job_id, running = _new_sync_job(username, email, source='cron')
    if running:
        print(f'Skipping {email} for {username}: a sync is already in '
              f'flight (job {running.get("job_id")}, '
              f'{running.get("source", "webui")}, started '
              f'{running.get("started_local") or running.get("started")})',
              flush=True)
        return
    print(f'Syncing {email} for {username} (interval: {interval_key})', flush=True)
    _update_sync_job(job_id, state='syncing')
    try:
        result = run_sync(username, email)
    except Exception as e:
        result = {'state': 'error', 'error': str(e)}
    _update_sync_job(
        job_id,
        state='done' if result.get('state') == 'ok' else 'error',
        exit_code=result.get('exit_code'),
        error=result.get('error'),
        finished=time.strftime('%Y-%m-%d %H:%M:%S'))
    print(f'  {email} -> {result.get("state", "error")}', flush=True)


def _scheduled_lane(rows):
    """One server's accounts for this hour, one after another."""
    for i, r in enumerate(rows):
        if i > 0:
            time.sleep(10)
        _scheduled_sync_one(r['user'], r['email'], r['interval'])


def scheduled_sync():
    """Run the scheduled syncs that are due this hour. Called by cron hourly.

    1.2.0: each account has a fixed hour (sync_schedule_plan). Accounts on
    different IMAP servers run in parallel, up to MAX_PARALLEL_SYNCS at
    once, 10 s apart; on one server they never overlap, and a server whose
    previous sync is still running waits for the next hourly run. An
    account whose hour was missed (host down, server busy) runs at the
    next hourly run.
    """
    plan = sync_schedule_plan()
    due = [r for r in plan if r['due']]
    print(f'=== Scheduled sync: {len(due)} account(s) due at '
          f'{time.strftime("%Y-%m-%d %H:%M:%S")} ===', flush=True)
    if not due:
        print('=== Done ===')
        return

    per_server = {}
    for r in plan:
        per_server[r['server']] = per_server.get(r['server'], 0) + 1
    lanes = {}
    for r in sorted(due, key=lambda r: (r['slot_epoch'], r['user'], r['email'])):
        lanes.setdefault(r['server'], []).append(r)

    busy = _busy_servers()
    work = []
    for server, rows in sorted(lanes.items(), key=lambda kv: kv[1][0]['slot_epoch']):
        if server in busy:
            st = busy[server]
            for r in rows:
                print(f'Skipping {r["email"]} for {r["user"]}: {server} is busy '
                      f'with a sync of {st.get("target")} (job '
                      f'{st.get("job_id")}, started '
                      f'{st.get("started_local") or st.get("started")}); '
                      f'it runs at the next hourly run', flush=True)
            continue
        work.append(rows[:_slc.lane_capacity(per_server[server])])

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=min(MAX_PARALLEL_SYNCS, max(1, len(work)))) as pool:
        futures = []
        for i, rows in enumerate(work):
            if i > 0:
                time.sleep(10)  # 10 s between lane starts
            futures.append(pool.submit(_scheduled_lane, rows))
        for f in futures:
            f.result()

    print(f'=== Done ===')


# --- In-process hourly schedule (hosts without cron: Windows) ---
#
# On Linux /etc/cron.d/mail-archiver runs `app.py scheduled-sync`, `import`
# and `bulk-sync` once an hour. The Windows application has no cron, so its
# launcher calls start_background_scheduler() once after importing this
# module; the same three passes then run in a daemon thread at the top of
# every hour. Nothing starts it on Linux.

_SCHEDULER_LOCK = threading.Lock()
_SCHEDULER_THREAD = None


def run_hourly_passes():
    """What the hourly cron runs, in the same order. One pass failing does
    not stop the next."""
    for name, job in (('scheduled-sync', scheduled_sync),
                      ('import', scheduled_mail_import),
                      ('bulk-sync', lambda: _bulk_routes.cli([]))):
        try:
            job()
        except (Exception, SystemExit) as e:   # SystemExit: an argparse path
            print(f'hourly {name} pass failed: {type(e).__name__}: {e}',
                  flush=True)


def _seconds_to_next_hour(now=None):
    now = time.time() if now is None else now
    lt = time.localtime(now)
    return max(1.0, 3600 - (lt.tm_min * 60 + lt.tm_sec))


def _scheduler_loop(stop_event):
    while not stop_event.wait(_seconds_to_next_hour()):
        run_hourly_passes()


def start_background_scheduler():
    """Start the in-process hourly schedule (idempotent). Returns the
    threading.Event that stops it when set."""
    global _SCHEDULER_THREAD
    with _SCHEDULER_LOCK:
        if _SCHEDULER_THREAD is not None and _SCHEDULER_THREAD[0].is_alive():
            return _SCHEDULER_THREAD[1]
        try:
            # The Windows application's "service start" (gunicorn does the
            # same from post_worker_init).
            heal_stale_sync_status_all()
        except Exception:
            pass
        stop_event = threading.Event()
        t = threading.Thread(target=_scheduler_loop, args=(stop_event,),
                             name='mail-archiver-scheduler', daemon=True)
        t.start()
        _SCHEDULER_THREAD = (t, stop_event)
        return stop_event


def print_sync_schedule():
    """`app.py sync-schedule`: the daily plan, one line per account."""
    plan = sync_schedule_plan()
    fmt = lambda t: time.strftime('%m-%d %H:%M', time.localtime(t)) if t else 'never'
    print(f'{"server":24} {"slot":5} {"interval":8} {"last start":11} {"due":3}  account')
    for r in plan:
        print(f'{r["server"][:24]:24} {r["slot"]:02d}:00 {r["interval"]:8} '
              f'{fmt(r["last_started"]):11} {"yes" if r["due"] else "no":3}  '
              f'{r["user"]}/{r["email"]}')


# --- Mail import (1.3.0) ---
#
# Every mail account has an import folder,
# <data_dir>/<user>/_import/<account_slug>/{inbox,done,failed}/ : what lands
# in inbox/ (a file or a directory; PST, mbox, Maildir, eml, emlx, msg, zip,
# tar) is imported into that account. The hourly cron runs
# `mail-archiver-scheduled-sync import`, which imports for every user whose
# import schedule is due; the WebUI starts the same pass for one file. The
# conversion itself runs AS THE MAILBOX OWNER (like mbsync), so mail lands
# owned by the user and an untrusted file is never parsed with the
# service's capabilities. The engine, the ledger and the retention rules
# are in mail_import.py; this section is the privilege model and the routes.

IMPORT_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'mail_import.py')
APP_SCRIPT = os.path.abspath(__file__)
IMPORT_CONFIG = 'mail_import.json'
IMPORT_CONFIG_LEGACY = 'pst_import.json'        # 1.2.x name, read if present
IMPORT_STATE = 'mail_import_state.json'


def _save_user_json(username, filename, data):
    """Atomic write of a per-user .config JSON file, 0640 <user>:mail-archiver
    (same ownership model as accounts.json, so the service can read what
    the root cron wrote and vice versa)."""
    config_dir = get_user_config_dir(username)
    path = config_dir / filename
    tmp = config_dir / f'.{filename}.{os.getpid()}.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2)
    os.chmod(str(tmp), 0o640)
    _chown_user(tmp, username)
    service_group = _pc.group_info('mail-archiver')
    if service_group is not None:
        try:
            st = os.stat(str(tmp))
            _pc.chown(str(tmp), st.st_uid, service_group.gid)
        except OSError:
            pass
    _pc.replace_file(str(tmp), str(path))


def _load_user_json(username, filename, default):
    path = get_user_config_dir(username) / filename
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def load_import_config(username, accounts=None):
    if accounts is None:
        accounts = load_accounts(username)
    raw = _load_user_json(username, IMPORT_CONFIG, None)
    if raw is None:
        raw = _load_user_json(username, IMPORT_CONFIG_LEGACY, None)
    cfg = _pst.normalize_config(raw or {}, [a['email'] for a in accounts])
    cfg['configured'] = raw is not None
    return cfg


def load_import_state(username):
    s = _load_user_json(username, IMPORT_STATE, None)
    return s if isinstance(s, dict) else {'last_scheduled_run': None}


def _import_child_kwargs(username):
    """subprocess kwargs that run a child as the mailbox owner (PAM mode),
    mirroring run_sync's mbsync spawn. Builtin mode runs as ourselves."""
    if CONFIG['auth_mode'] != 'pam':
        return {}
    if not _pc.supports_system_accounts():
        return {}            # Windows: one user, nobody to switch to
    pw = _pc.user_info(username)
    if pw is None:
        raise KeyError(f'getpwnam(): name not found: {username!r}')
    if _pc.current_uid() == pw.uid:
        return {}
    return {
        'user': pw.uid,
        'group': pw.gid,
        'extra_groups': _supplementary_gids(username, pw.gid),
        'env': {'HOME': pw.home, 'USER': username, 'LOGNAME': username,
                'LANG': 'C.UTF-8',
                'PATH': '/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin'},
    }


def _import_convert_as_user(username):
    def convert(path, typ, name, maildir, work_dir, cache_path):
        cmd = [sys.executable, '-I', IMPORT_SCRIPT, 'convert',
               '--path', path, '--type', typ, '--name', name,
               '--maildir', maildir, '--work', work_dir, '--cache', cache_path,
               '--readpst', _pst.READPST, '--msgconvert', _imp.MSGCONVERT,
               '--timeout', str(_pst.READPST_TIMEOUT),
               '--max-message-bytes', str(_pst.MAX_MESSAGE_BYTES)]
        kw = _import_child_kwargs(username)
        # (Linux: PATH=/usr/sbin:/usr/bin:/sbin:/bin and LANG=C.UTF-8.)
        env = dict(kw.get('env') or _pc.minimal_child_env())
        # The importer's own limits and tool paths (no secrets among them).
        env.update({k: v for k, v in os.environ.items()
                    if k.startswith('MAIL_ARCHIVER_IMPORT_') or k == 'MAIL_ARCHIVER_ZSTD'})
        kw['env'] = env
        res = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True,
                             text=True, cwd=os.path.dirname(work_dir),
                             timeout=_pst.READPST_TIMEOUT + 6 * 3600, **kw)
        lines = [l for l in (res.stdout or '').splitlines() if l.strip()]
        try:
            return json.loads(lines[-1])
        except (IndexError, ValueError):
            return {'status': 'failed',
                    'error': f'converter exited {res.returncode}: '
                             f'{(res.stderr or "").strip()[-300:]}'}
    return convert


def _import_compress_as_user(username):
    """zstd for archived PST/mbox files. The compressed copy is tested
    before the original is removed, so done/ never holds an unverified
    copy (retention rule R2)."""
    zstd = shutil.which('zstd')
    if not zstd:
        return None

    def compress(path):
        # Every step, the removals included, runs as the mailbox owner:
        # `path` is a name in a folder the owner can rearrange, so nothing
        # is unlinked by name with the service's or root's privileges.
        dest = path + '.zst'
        kw = _import_child_kwargs(username)

        def run(cmd):
            subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True,
                           timeout=6 * 3600, check=True, **kw)

        def remove(target):
            if _pc.IS_WINDOWS:
                # No rm there; still a child, like every other step.
                run([sys.executable, '-I', '-c',
                     'import pathlib, sys; '
                     'pathlib.Path(sys.argv[1]).unlink(missing_ok=True)',
                     target])
            else:
                run(['rm', '-f', '--', target])

        try:
            run([zstd, '-q', '-f', '-T0', '-o', dest, path])
            run([zstd, '-q', '-t', dest])
        except BaseException:
            try:
                remove(dest)
            except Exception:
                pass
            raise
        remove(path)
        return dest
    return compress


def _import_prepare(username):
    """Give a file or a directory tree we received to the mailbox owner,
    so the converter (running as that user) can read it. Never through a
    link: lchown only, and a tree is walked by directory fd."""
    def prepare(path):
        if CONFIG['auth_mode'] != 'pam':
            return
        if not _pc.supports_system_accounts():
            return
        import stat as _stat
        pw = _pc.user_info(username)
        if pw is None:
            return
        try:
            st = os.lstat(path)
        except OSError:
            return
        try:
            if _stat.S_ISREG(st.st_mode):
                _pc.lchown(path, pw.uid, pw.gid)
                if not (st.st_mode & 0o400):
                    os.chmod(path, (st.st_mode & 0o7777) | 0o400)
            elif _stat.S_ISDIR(st.st_mode):
                # lchown on the top, then a walk by directory fd
                _pc.chown_tree_nofollow(path, pw.uid, pw.gid)
        except OSError:
            pass
    return prepare


def _import_index(username):
    def index(account):
        from search_index import index_maildir
        index_maildir(username, CONFIG['data_dir'], account_filter=account)
    return index


def open_import_ledger(username):
    """The import ledger: tables in the user's index database."""
    from search_index import get_index_db
    return _imp.Ledger(get_index_db(username, CONFIG['data_dir']))


# How long a starting import waits for the per-user lock. The status probe
# (mail_import_running, polled by the page) holds the lock shared for a
# moment; without the wait, a poll at the wrong instant made an import that
# had just been started give up as "another import is running".
IMPORT_LOCK_WAIT = 5.0


def _import_lock(username, wait=0.0, shared=False):
    """Per-user import lock. Returns an fd, or None if another import for
    this user is running (still, after `wait` seconds of retrying).
    `shared` is the probe: it conflicts with a running import but not with
    another probe. O_RDONLY, like claim_sync (1.1.8): the root cron and the
    service user must both be able to open it."""
    path = get_user_config_dir(username) / 'mail_import.lock'
    fd = os.open(str(path), os.O_RDONLY | os.O_CREAT, 0o644)
    deadline = time.monotonic() + wait
    while True:
        try:
            _pc.lock_fd(fd, exclusive=not shared, blocking=False)
            break
        except _pc.LockUnavailable:
            if time.monotonic() >= deadline:
                os.close(fd)
                return None
            time.sleep(0.05)
    if not shared:
        _chown_user(path, username)
    return fd


def mail_import_running(username):
    """True while an import pass of this user holds the lock. A shared,
    non-blocking probe: two probes never see each other as an import."""
    fd = _import_lock(username, shared=True)
    if fd is None:
        return True
    os.close(fd)
    return False


def _import_owners(username):
    """uids that may own a directory of the user's import tree: the
    mailbox owner, root, and the service (it creates them, then chowns).
    None in builtin mode, where every user's files are the service's."""
    if CONFIG['auth_mode'] != 'pam':
        return None
    if not _pc.supports_system_accounts():
        return None
    owners = {0, _pc.current_uid()}
    for name in (username, 'mail-archiver'):    # the owner; the service account
        pw = _pc.user_info(name)
        if pw is not None:
            owners.add(pw.uid)
    return owners


def make_importer(username, cfg=None, accounts=None, log=print, stable_wait=None):
    accounts = load_accounts(username) if accounts is None else accounts
    cfg = cfg or load_import_config(username, accounts)
    return _imp.UserImporter(
        CONFIG['data_dir'], username, accounts, cfg,
        convert=_import_convert_as_user(username),
        prepare=_import_prepare(username),
        index=_import_index(username),
        open_ledger=lambda: open_import_ledger(username),
        compress=_import_compress_as_user(username),
        log=log,
        stable_wait=_pst.STABLE_WAIT if stable_wait is None else stable_wait,
        owners=_import_owners(username))


def _exclude_imported_folders_from_sync(username):
    """After an import: if an account's channel in ~/.mbsyncrc does not yet
    leave out the importer's local-only folders (`PST/`, `Imported/`),
    regenerate the file. isync fails a whole-channel run on a near-side
    folder that has no far-side partner (bulk_sync.IMPORT_TOP_FOLDERS).
    The file is rewritten only when a channel's Patterns line would
    change. Never raises: an import must not fail over this."""
    try:
        config_dir = str(get_user_config_dir(username))
        rc_text = _bulk_routes.mbsyncrc_path(username).read_text(encoding='utf-8')
        for acct in load_accounts(username):
            if not acct.get('enabled', True):
                continue
            have = _bulk.rc_patterns(rc_text, _bulk.safe_account_name(acct['email']))
            if have is not None and have != _bulk.regular_sync_pattern(config_dir, acct):
                generate_mbsyncrc(username)
                print(f'Import for {username}: imported folders are now left out '
                      f'of the regular sync (.mbsyncrc regenerated)', flush=True)
                return True
    except Exception as e:
        print(f'Import for {username}: could not update .mbsyncrc for the '
              f'imported folders: {type(e).__name__}: {e}', flush=True)
    return False


def mail_import_user(username, only=None, trigger='scheduled', stable_wait=None):
    """One import pass for one user under the per-user lock. `only` is
    (account or None, file name). Returns the summary, or None when another
    import for the user is running."""
    fd = _import_lock(username, wait=IMPORT_LOCK_WAIT)
    if fd is None:
        print(f'Import for {username}: another import is running, skipped', flush=True)
        return None
    try:
        imp = make_importer(username, stable_wait=stable_wait,
                            log=lambda m: print(m, flush=True))
        try:
            return imp.run(only=only, trigger=trigger)
        finally:
            _exclude_imported_folders_from_sync(username)
    finally:
        _pc.unlock_fd(fd)
        os.close(fd)


def scheduled_mail_import(now=None):
    """Hourly cron entry: import for every user whose import schedule is
    due. A pass that left something to retry (a file still changing, low
    disk) does not count as the scheduled run, so it is retried next hour."""
    now = now or time.time()
    data_dir = Path(CONFIG['data_dir'])
    print(f'=== Mail import check at {time.strftime("%Y-%m-%d %H:%M:%S")} ===', flush=True)
    if not data_dir.exists():
        return
    for userdir in sorted(data_dir.iterdir()):
        if not userdir.is_dir() or userdir.name.startswith('.'):
            continue
        if not any((userdir / '.config' / n).exists()
                   for n in (IMPORT_CONFIG, IMPORT_CONFIG_LEGACY)):
            continue
        username = userdir.name
        cfg = load_import_config(username)
        if not cfg['enabled']:
            continue
        state = load_import_state(username)
        if not _pst.is_import_due(cfg['schedule'], state.get('last_scheduled_run'), now):
            continue
        print(f'Import for {username}', flush=True)
        try:
            summary = mail_import_user(username, trigger='scheduled')
        except Exception as e:
            print(f'Import for {username} failed: {type(e).__name__}: {e}', flush=True)
            continue
        if summary is None or summary.get('retry'):
            continue
        state['last_scheduled_run'] = now
        _save_user_json(username, IMPORT_STATE, state)
    print('=== Done ===', flush=True)


def _spawn_mail_import(username, account=None, file_name=None):
    """Start a manual import in its own process so it outlives the request
    and any gunicorn worker recycle; progress lands in the ledger."""
    cmd = [sys.executable, APP_SCRIPT, 'import', '--user', username, '--manual']
    if account:
        cmd += ['--account', account]
    if file_name:
        cmd += ['--file', file_name]
    proc = subprocess.Popen(cmd, cwd=os.path.dirname(APP_SCRIPT), env=dict(os.environ),
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            **_pc.detached_popen_kwargs())  # start_new_session
    # Reap it when it ends (as bulk_sync_routes.spawn_manual_run does), or
    # every manual import leaves a zombie until the worker is recycled.
    threading.Thread(target=proc.wait, daemon=True).start()


def print_import_ledger(username, events=False, limit=50, as_json=False):
    """`app.py import --user U --ledger [--events]`: the import ledger (or
    the retention audit trail), newest first."""
    led = open_import_ledger(username)
    try:
        rows = led.events(limit) if events else led.rows(limit)
    finally:
        led.close()
    if as_json:
        print(json.dumps(rows, indent=2))
        return
    if events:
        print(f'{"id":>5} {"at":19} {"action":8} {"rule":5} {"copy date":10} account / name / reason')
        for r in rows:
            print(f'{r["id"]:>5} {r["at"]:19} {r["action"]:8} {r["rule"]:5} '
                  f'{r["copy_date"]:10} {r["account"]} / {r["name"]} / {r["reason"]}')
        return
    print(f'{"id":>5} {"finished":19} {"status":8} {"type":9} {"imported":>8} {"dup":>7} '
          f'{"failed":>6} {"big":>4} {"sha256":16} account / file -> prefix')
    for r in rows:
        print(f'{r["id"]:>5} {r["finished_at"] or r["started_at"] or r["queued_at"]:19} '
              f'{r["status"]:8} {r["detected_type"]:9} {r["imported"]:>8} '
              f'{r["duplicates"]:>7} {r["failed"]:>6} {r["too_large"]:>4} '
              f'{r["sha256"][:16]:16} {r["account"]} / {r["original_name"]} -> '
              f'{r["target_prefix"]}' + (f'  [{r["error"]}]' if r['error'] else ''))


def _fmt_bytes(n):
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if n < 1024 or unit == 'TB':
            return f'{n:.0f} {unit}' if unit == 'B' else f'{n:.1f} {unit}'
        n /= 1024


@app.route('/import')
@login_required
def mail_import_page():
    import datetime as _dt
    username = session['username']
    accounts = load_accounts(username)
    emails = [a['email'] for a in accounts]
    cfg = load_import_config(username, accounts)
    try:
        imp = make_importer(username, cfg=cfg, accounts=accounts)
    except ValueError as e:
        flash(f'Watch folder setting ignored: {e}')
        cfg['watch_dir'] = ''
        imp = make_importer(username, cfg=cfg, accounts=accounts)
    tree_error = ''
    try:
        imp.ensure_dirs()
    except _imp.UnsafeImportTree as e:
        tree_error = str(e)
    except OSError as e:
        flash(f'Could not create the import folders: {e.strerror or e}')
    today = _dt.date.today()

    def waiting(c):
        typ, how = _imp.detect_unit(c.get('io') or c['path'])
        return {'name': c['name'], 'size': _fmt_bytes(c['size']),
                'is_dir': c.get('kind') == 'dir', 'sidecar': bool(c['sidecar']),
                'type': typ, 'type_label': _imp.TYPE_LABELS.get(typ, ''), 'how': how}

    acct_rows, watch_files, watch_ignored = [], [], []
    try:
        for email in emails:
            ap = imp.account_paths(email)
            row = {'email': email, 'slug': _pst.safe_account_name(email), 'paths': ap,
                   'waiting': [], 'ignored': [], 'failed': [], 'archive': [],
                   'error': tree_error}
            acct_rows.append(row)
            if tree_error:
                continue
            # A folder that is a symbolic link (or not the owner's) is
            # refused: nothing in it is listed, read or moved.
            scan = imp.scan_account(email)
            row['error'] = scan['error']
            if scan['error']:
                continue
            entries = imp.archive_entries(email)
            plan = _imp.retention_preview(entries, today, cfg)
            row.update(
                waiting=[waiting(c) for c in scan['candidates']],
                ignored=scan['ignored'],
                failed=[dict(f, size=_fmt_bytes(f['size']))
                        for f in imp.failed_entries(email)],
                archive=[{'name': e['name'], 'date': e['date'].isoformat(),
                          'size': _fmt_bytes(e['size']), 'series': e['series'] or '',
                          'plan': plan.get(e['name'], '')} for e in entries])
        if not tree_error:
            wscan = imp.scan_watch()
            watch_ignored = wscan['ignored']
            if wscan['error']:
                flash(f'The watch folder is not used: {wscan["error"]}')
            for c in wscan['candidates']:
                acct, how = _pst.map_file_to_account(c['name'], cfg, emails)
                watch_files.append(dict(waiting(c), account=acct, mapped_by=how))
    finally:
        imp.close()
    # The ledger lives in the index database. If that file is damaged the
    # page still works and says what to do, instead of answering 500.
    rows, events, ledger_error = [], [], ''
    try:
        led = open_import_ledger(username)
        try:
            rows, events = led.rows(50), led.events(25)
        finally:
            led.close()
    except sqlite3.DatabaseError as e:
        ledger_error = str(e)
    state = load_import_state(username)
    return render_template(
        'mail_import.html', username=username, accounts=accounts, cfg=cfg,
        tool_notes=_imp.tool_notes(),
        paths=imp.paths, acct_rows=acct_rows, watch_files=watch_files,
        watch_ignored=watch_ignored, rows=rows, events=events,
        tree_error=tree_error, ledger_error=ledger_error,
        running=mail_import_running(username), weekdays=_pst.WEEKDAYS,
        msgconvert=bool(shutil.which(_imp.MSGCONVERT)),
        readpst=bool(shutil.which(_pst.READPST)),
        last_scheduled_run=(_pst.now_str(state['last_scheduled_run'])
                            if state.get('last_scheduled_run') else None),
        max_upload=_fmt_bytes(_imp.MAX_IMPORT_BYTES))


@app.route('/pst-import')
@login_required
def pst_import_page():
    """The 1.3.0 development name of the page."""
    return redirect(url_for('mail_import_page'))


@app.route('/import/config', methods=['POST'])
@login_required
def mail_import_config():
    username = session['username']
    accounts = load_accounts(username)
    f = request.form
    raw = {
        'enabled': f.get('enabled') == '1',
        'watch_dir': f.get('watch_dir', '').strip(),
        'default_account': f.get('default_account', ''),
        'schedule': {'kind': f.get('schedule_kind', 'daily'),
                     'weekday': f.get('weekday', 0), 'hour': f.get('hour', 6)},
        'keep_recent': f.get('keep_recent', f.get('keep_weekly', 4)),
        'keep_monthly': f.get('keep_monthly', 3),
        'min_keep': f.get('min_keep', 3),
        'max_delete_per_run': f.get('max_delete_per_run', 3),
        'mappings': [{'pattern': p, 'account': a} for p, a in
                     zip(f.getlist('map_pattern'), f.getlist('map_account'))
                     if p.strip()],
    }
    if raw['watch_dir']:
        try:
            watch = _pst.resolve_watch_dir(CONFIG['data_dir'], username, raw['watch_dir'])
            # ... and every directory on the way to it is a real directory
            # of the owner's, not a link (the scheduled run is root).
            _imp.check_tree(os.path.join(CONFIG['data_dir'], username), [watch],
                            _import_owners(username))
        except (ValueError, OSError) as e:
            flash(f'Not saved: {e}')
            return redirect(url_for('mail_import_page'))
    cfg = _pst.normalize_config(raw, [a['email'] for a in accounts])
    dropped = len(raw['mappings']) - len(cfg['mappings'])
    _save_user_json(username, IMPORT_CONFIG, cfg)
    flash('Import settings saved.' +
          (f' {dropped} mapping(s) dropped (unknown account or bad pattern).' if dropped else ''))
    return redirect(url_for('mail_import_page'))


@app.route('/import/run', methods=['POST'])
@login_required
def mail_import_run():
    """Import one waiting file now (account + file), or everything that is
    waiting (no file)."""
    username = session['username']
    name = request.form.get('file', '')
    account = request.form.get('account', '')
    accounts = load_accounts(username)
    cfg = load_import_config(username, accounts)
    if name:
        if account:
            if account not in [a['email'] for a in accounts]:
                flash('That is not one of your accounts.')
                return redirect(url_for('mail_import_page'))
        try:
            imp = make_importer(username, cfg=cfg, accounts=accounts)
        except ValueError:
            imp = make_importer(username, cfg=dict(cfg, watch_dir=''), accounts=accounts)
        try:
            scan = imp.scan_account(account) if account else imp.scan_watch()
        finally:
            imp.close()
        if scan['error']:
            flash(f'Not started: {scan["error"]}')
            return redirect(url_for('mail_import_page'))
        names = {c['name'] for c in scan['candidates']}
        if name not in names:
            flash('That file is not waiting in the import folder.')
            return redirect(url_for('mail_import_page'))
    if mail_import_running(username):
        flash('An import is already running. It picks up everything that is waiting.')
        return redirect(url_for('mail_import_page'))
    _spawn_mail_import(username, account or None, name or None)
    flash(f'Import of {name or "everything waiting"} started. This page refreshes while it runs.')
    return redirect(url_for('mail_import_page'))


@app.route('/import/upload', methods=['POST'])
@login_required
def mail_import_upload():
    """Raw-body upload (the page sends the file with XHR, not a form, so
    nothing is buffered in /tmp). The file enters the chosen account's
    inbox/ through the same .incoming + sidecar + rename contract a
    producer uses."""
    username = session['username']
    name = request.args.get('name', '')
    account = request.args.get('account', '')
    accounts = load_accounts(username)
    if account not in [a['email'] for a in accounts]:
        return jsonify({'error': 'choose one of your accounts to import into'}), 400
    try:
        try:
            imp = make_importer(username, accounts=accounts)
        except ValueError:      # a bad watch-folder setting does not stop an upload
            imp = make_importer(username, accounts=accounts,
                                cfg=dict(load_import_config(username, accounts),
                                         watch_dir=''))
        try:
            imp.ensure_dirs()
        finally:
            imp.close()
        dest, digest = _imp.receive_upload(
            request.stream, request.content_length, imp.account_paths(account),
            name, prepare=_import_prepare(username), owners=_import_owners(username))
    except _imp.UnsafeImportTree as e:
        return jsonify({'error': f'upload refused: {e}'}), 409
    except _imp.InsufficientSpace as e:
        return jsonify({'error': str(e)}), 507
    except FileExistsError as e:
        return jsonify({'error': str(e)}), 409
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    started = False
    if request.args.get('import') == '1' and not mail_import_running(username):
        _spawn_mail_import(username, account, name)
        started = True
    return jsonify({'ok': True, 'name': name, 'account': account, 'sha256': digest,
                    'import_started': started})


@app.route('/import/retry', methods=['POST'])
@login_required
def mail_import_retry():
    username = session['username']
    account = request.form.get('account', '')
    name = request.form.get('file', '')
    if account not in [a['email'] for a in load_accounts(username)]:
        flash('That is not one of your accounts.')
        return redirect(url_for('mail_import_page'))
    try:
        _imp.retry_failed(_pst.account_paths(CONFIG['data_dir'], username, account), name,
                          owners=_import_owners(username))
        flash(f'{name} moved back to the inbox of {account}; it is imported at the next '
              f'run (or press Import now).')
    except (OSError, ValueError) as e:
        flash(f'Could not move {name}: {e}')
    return redirect(url_for('mail_import_page'))


@app.route('/api/import/status')
@login_required
def mail_import_status():
    username = session['username']
    rows, error = [], None
    try:
        led = open_import_ledger(username)
        try:
            rows = led.rows(25)
        finally:
            led.close()
    except sqlite3.DatabaseError as e:
        error = f'the index database cannot be read ({e}); rebuild the search index'
    return jsonify({'running': mail_import_running(username), 'rows': rows,
                    'error': error})


# --- Microsoft OAuth2 Routes ---

def _resolve_oauth_for_account(username, email):
    """Resolve the OAuth app tied to a specific account (by email). Used by
    authorize/callback/refresh so each account can be pinned to its own app.
    Returns (app_obj, account_dict) or (None, account_dict)."""
    from oauth2_microsoft import resolve_oauth_app
    accounts = load_accounts(username)
    acct = None
    for a in accounts:
        if a['email'] == email:
            acct = a
            break
    if acct is None:
        return None, None
    app_obj = resolve_oauth_app(CONFIG['data_dir'], username,
                                app_id=acct.get('oauth_app_id'),
                                provider='microsoft')
    return app_obj, acct


@app.route('/oauth2/settings', methods=['GET'])
@login_required
def oauth2_settings():
    """List OAuth Apps at both scopes (server + this user's private).
    App CRUD lives at /oauth2/apps/*. Kept as GET-only landing page — the old
    single-app editor form is gone; the multi-app model replaces it."""
    from oauth2_microsoft import list_oauth_apps, load_oauth2_config, load_user_oauth2_config

    server_cfg = load_oauth2_config(CONFIG['data_dir'])
    user_cfg = load_user_oauth2_config(CONFIG['data_dir'], session['username'])

    all_apps = list_oauth_apps(CONFIG['data_dir'], username=session['username'])
    server_apps = [a for a in all_apps if a['scope'] == 'server']
    user_apps = [a for a in all_apps if a['scope'] == 'user']

    # Redact secrets for display
    for a in server_apps + user_apps:
        sec = a.get('client_secret', '')
        a['secret_redacted'] = _redact_middle(sec) if sec else ''
        a['secret_length'] = len(sec)
        # Show CID head/tail too (not secret, but useful glance)
        a['client_id_display'] = a.get('client_id', '')

    return render_template('oauth2_settings.html',
                           username=session['username'],
                           server_apps=server_apps,
                           user_apps=user_apps,
                           server_defaults=(server_cfg.get('defaults') or {}),
                           user_defaults=(user_cfg.get('defaults') or {}))


def _load_scope_cfg(scope, username):
    """Return (cfg, save_fn) for the given scope."""
    from oauth2_microsoft import (load_oauth2_config, save_oauth2_config,
                                  load_user_oauth2_config, save_user_oauth2_config)
    if scope == 'server':
        return load_oauth2_config(CONFIG['data_dir']), \
               lambda c: save_oauth2_config(CONFIG['data_dir'], c)
    return load_user_oauth2_config(CONFIG['data_dir'], username), \
           lambda c: save_user_oauth2_config(CONFIG['data_dir'], username, c)


SERVER_APPS_ADMIN_ONLY = (
    'Server-wide OAuth apps can only be added, changed or removed by an '
    'administrator. You can add a private app for your own accounts.')


def _may_manage_oauth_scope(scope, username):
    """Private apps are the user's own. A server-wide app is used by every
    user's accounts (its client secret, and where sign-ins are sent), so
    only an administrator may write one (1.3.3; any signed-in user could)."""
    if scope != 'server':
        return True
    import admin as _admin_module
    return _admin_module._is_admin(username)


def _find_app(app_id, username):
    """Look up app by id in whichever scope its prefix indicates.
    Returns (scope, cfg, app_dict, save_fn) or (None, None, None, None)."""
    scope = 'user' if app_id.startswith('usr-') else 'server'
    cfg, save = _load_scope_cfg(scope, username)
    app = (cfg.get('apps') or {}).get(app_id)
    if not app:
        return None, None, None, None
    return scope, cfg, app, save


@app.route('/oauth2/apps/new', methods=['GET', 'POST'])
@login_required
def new_oauth_app():
    """Create a new OAuth app at server-scope or user-scope."""
    from oauth2_microsoft import (load_oauth2_config, load_user_oauth2_config,
                                  new_app_id, KNOWN_PROVIDERS)

    if request.method == 'POST':
        scope = request.form.get('scope', 'user').strip()
        if scope not in ('server', 'user'):
            flash('Invalid scope.')
            return redirect(url_for('oauth2_settings'))
        if not _may_manage_oauth_scope(scope, session['username']):
            flash(SERVER_APPS_ADMIN_ONLY)
            return redirect(url_for('oauth2_settings'))
        provider = request.form.get('provider', 'microsoft').strip()
        if provider not in KNOWN_PROVIDERS:
            flash(f'Unsupported provider {provider!r}.')
            return redirect(url_for('new_oauth_app'))
        name = (request.form.get('name') or '').strip()
        client_id = (request.form.get('client_id') or '').strip()
        client_secret = (request.form.get('client_secret') or '').strip()
        tenant = (request.form.get('tenant') or 'common').strip() or 'common'
        if not (name and client_id and client_secret):
            flash('Name, Client ID, and Client Secret are all required for new apps.')
            return render_template('edit_app.html', app=None,
                                   form={'scope': scope, 'provider': provider,
                                         'name': name, 'client_id': client_id,
                                         'tenant': tenant},
                                   secret_redacted='', secret_length=0,
                                   providers=KNOWN_PROVIDERS)

        cfg, save = _load_scope_cfg(scope, session['username'])
        taken = set((cfg.get('apps') or {}).keys())
        app_id = new_app_id(scope, provider, name, taken)
        cfg.setdefault('apps', {})[app_id] = {
            'scope': scope, 'provider': provider, 'name': name,
            'client_id': client_id, 'client_secret': client_secret,
            'tenant': tenant,
        }
        # If this is the FIRST app of this provider in this scope, make it default.
        defs = cfg.setdefault('defaults', {})
        if provider not in defs:
            defs[provider] = app_id
        save(cfg)
        flash(f'Added OAuth app "{name}" ({scope}-scope, id={app_id}).')
        return redirect(url_for('oauth2_settings'))

    return render_template('edit_app.html', app=None,
                           form={'scope': 'user', 'provider': 'microsoft',
                                 'name': '', 'client_id': '', 'tenant': 'common'},
                           secret_redacted='', secret_length=0,
                           providers=KNOWN_PROVIDERS)


@app.route('/oauth2/apps/<app_id>/edit', methods=['GET', 'POST'])
@login_required
def edit_oauth_app(app_id):
    """Edit an existing OAuth app. Blank secret preserves existing (1.0.7 pattern)."""
    from oauth2_microsoft import KNOWN_PROVIDERS
    scope, cfg, app, save = _find_app(app_id, session['username'])
    if not app:
        flash(f'OAuth app {app_id!r} not found.')
        return redirect(url_for('oauth2_settings'))
    if not _may_manage_oauth_scope(scope, session['username']):
        flash(SERVER_APPS_ADMIN_ONLY)
        return redirect(url_for('oauth2_settings'))

    if request.method == 'POST':
        name = (request.form.get('name') or '').strip()
        client_id = (request.form.get('client_id') or '').strip()
        client_secret = (request.form.get('client_secret') or '').strip()
        tenant = (request.form.get('tenant') or 'common').strip() or 'common'
        if not (name and client_id):
            flash('Name and Client ID are required.')
            return redirect(url_for('edit_oauth_app', app_id=app_id))
        new_secret = client_secret or app.get('client_secret', '')
        if not new_secret:
            flash('Client Secret is required — no existing secret to preserve.')
            return redirect(url_for('edit_oauth_app', app_id=app_id))
        app['name'] = name
        app['client_id'] = client_id
        app['client_secret'] = new_secret
        app['tenant'] = tenant
        save(cfg)
        if client_secret:
            flash(f'Updated OAuth app "{name}" (both fields).')
        else:
            flash(f'Updated OAuth app "{name}" — existing Client Secret preserved.')
        return redirect(url_for('oauth2_settings'))

    sec = app.get('client_secret', '')
    return render_template('edit_app.html', app_id=app_id, app=app,
                           form={'scope': scope,
                                 'provider': app.get('provider', 'microsoft'),
                                 'name': app.get('name', ''),
                                 'client_id': app.get('client_id', ''),
                                 'tenant': app.get('tenant', 'common')},
                           secret_redacted=_redact_middle(sec) if sec else '',
                           secret_length=len(sec),
                           providers=KNOWN_PROVIDERS)


def _accounts_referencing_app(app_id):
    """Walk every user's accounts.json under CONFIG['data_dir'], return list
    of (username, email) pairs whose account.oauth_app_id == app_id."""
    hits = []
    root = Path(CONFIG['data_dir'])
    if not root.is_dir():
        return hits
    for user_dir in root.iterdir():
        if not user_dir.is_dir() or user_dir.name.startswith('.'):
            continue
        af = user_dir / '.config' / 'accounts.json'
        if not af.exists():
            continue
        try:
            with open(af, encoding='utf-8') as f:
                accts = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue
        for a in accts or []:
            if a.get('oauth_app_id') == app_id:
                hits.append((user_dir.name, a.get('email', '?')))
    return hits


@app.route('/oauth2/apps/<app_id>/delete', methods=['POST'])
@login_required
def delete_oauth_app(app_id):
    """Delete an OAuth app. Refuses if any account (across all users) still
    pins itself to it via oauth_app_id."""
    from oauth2_microsoft import clear_default_if
    scope, cfg, app, save = _find_app(app_id, session['username'])
    if not app:
        flash(f'OAuth app {app_id!r} not found.')
        return redirect(url_for('oauth2_settings'))
    if not _may_manage_oauth_scope(scope, session['username']):
        flash(SERVER_APPS_ADMIN_ONLY)
        return redirect(url_for('oauth2_settings'))

    refs = _accounts_referencing_app(app_id)
    if refs:
        preview = ', '.join(f'{u}:{e}' for u, e in refs[:5])
        more = f' and {len(refs)-5} more' if len(refs) > 5 else ''
        flash(f'Refused: {len(refs)} account(s) still use this app ({preview}{more}). '
              f'Reassign those accounts first (Account → Settings → OAuth app).')
        return redirect(url_for('oauth2_settings'))

    del cfg['apps'][app_id]
    clear_default_if(cfg, app_id)
    save(cfg)
    flash(f'Deleted OAuth app "{app.get("name", app_id)}".')
    return redirect(url_for('oauth2_settings'))


@app.route('/oauth2/apps/<app_id>/set_default', methods=['POST'])
@login_required
def set_default_oauth_app(app_id):
    """Mark this app as its scope's default for its provider."""
    from oauth2_microsoft import set_default_app
    scope, cfg, app, save = _find_app(app_id, session['username'])
    if not app:
        flash(f'OAuth app {app_id!r} not found.')
        return redirect(url_for('oauth2_settings'))
    if not _may_manage_oauth_scope(scope, session['username']):
        flash(SERVER_APPS_ADMIN_ONLY)
        return redirect(url_for('oauth2_settings'))
    set_default_app(cfg, app_id)
    save(cfg)
    flash(f'"{app.get("name", app_id)}" is now the {scope} default for {app.get("provider")}.')
    return redirect(url_for('oauth2_settings'))


@app.route('/oauth2/authorize')
@login_required
def oauth2_authorize():
    """Start Microsoft OAuth2 flow — resolves the per-account OAuth app,
    redirects to that app's Microsoft login URL (respects per-app tenant)."""
    from oauth2_microsoft import build_microsoft_oauth2

    email = request.args.get('email', '')
    if not email:
        flash('Email address required for OAuth2.')
        return redirect(url_for('dashboard'))

    username = session['username']
    app_obj, acct = _resolve_oauth_for_account(username, email)
    if acct is None:
        flash(f'Account {email} not found.')
        return redirect(url_for('dashboard'))
    if not app_obj:
        flash('No Microsoft OAuth2 app is configured — go to Server Settings → OAuth Apps to add one.')
        return redirect(url_for('oauth2_settings'))

    try:
        redirect_uri = url_for('oauth2_callback', _external=True)
        oauth2 = build_microsoft_oauth2(app_obj, redirect_uri)
        auth_url, state = oauth2.get_authorization_url()
        session['oauth2_state'] = state
        session['oauth2_email'] = email
        # Pin the callback to THIS app (per-account can change between now
        # and the callback if operator races the settings page).
        session['oauth2_app_id'] = app_obj.get('app_id')
        return redirect(auth_url)
    except ValueError as e:
        flash(str(e))
        return redirect(url_for('dashboard'))


@app.route('/oauth2/callback')
@login_required
def oauth2_callback():
    """Handle Microsoft OAuth2 callback — exchange code for tokens against
    the same app that started the flow."""
    from oauth2_microsoft import (build_microsoft_oauth2, resolve_oauth_app,
                                  save_oauth2_tokens)

    error = request.args.get('error')
    if error:
        flash(f'Microsoft login failed: {request.args.get("error_description", error)}')
        return redirect(url_for('dashboard'))

    code = request.args.get('code', '')
    state = request.args.get('state', '')
    if not code or state != session.get('oauth2_state'):
        flash('Invalid OAuth2 callback. Please try again.')
        return redirect(url_for('dashboard'))

    email = session.pop('oauth2_email', '')
    app_id = session.pop('oauth2_app_id', None)
    session.pop('oauth2_state', None)

    if not email:
        flash('OAuth2 session expired. Please try again.')
        return redirect(url_for('dashboard'))

    username = session['username']
    app_obj = resolve_oauth_app(CONFIG['data_dir'], username,
                                app_id=app_id, provider='microsoft')
    if not app_obj:
        flash('OAuth app disappeared between authorize and callback. Try again.')
        return redirect(url_for('dashboard'))

    try:
        redirect_uri = url_for('oauth2_callback', _external=True)
        oauth2 = build_microsoft_oauth2(app_obj, redirect_uri)
        tokens = oauth2.exchange_code(code)
        save_oauth2_tokens(CONFIG['data_dir'], username, email, tokens)

        accounts = load_accounts(username)
        for acct in accounts:
            if acct['email'] == email:
                acct['auth_type'] = 'oauth2'
                # Pin the successful app so future syncs / refreshes use it
                if app_id:
                    acct['oauth_app_id'] = app_id
                break
        else:
            accounts.append({
                'email': email,
                'provider': 'hotmail',
                'auth_type': 'oauth2',
                'oauth_app_id': app_id or '',
                'enabled': True,
                'sync_interval': 'daily',
                'added': time.strftime('%Y-%m-%d %H:%M:%S'),
            })
        save_accounts(username, accounts)
        generate_mbsyncrc(username)

        safe_name = email.replace('@', '_at_').replace('.', '_')
        maildir = Path(CONFIG['data_dir']) / username / safe_name
        maildir.mkdir(parents=True, exist_ok=True)
        _chown_user(maildir, username)

        flash(f'Microsoft account {email} authenticated successfully. You can now sync.')
    except ValueError as e:
        flash(f'OAuth2 error: {e}')

    return redirect(url_for('dashboard'))


@app.route('/oauth2/refresh/<email>')
@login_required
def oauth2_refresh(email):
    """Manually refresh OAuth2 token for an account using its resolved app."""
    from oauth2_microsoft import build_microsoft_oauth2, ensure_fresh_token
    username = session['username']
    app_obj, acct = _resolve_oauth_for_account(username, email)
    if not app_obj:
        flash(f'No OAuth app resolved for {email}. Check Server Settings → OAuth Apps.')
        return redirect(url_for('dashboard'))
    try:
        redirect_uri = url_for('oauth2_callback', _external=True)
        oauth2 = build_microsoft_oauth2(app_obj, redirect_uri)
        token = ensure_fresh_token(oauth2, CONFIG['data_dir'], username, email)
        flash(f'Token refreshed for {email}.')
    except ValueError as e:
        flash(f'Token refresh failed: {e}')
    return redirect(url_for('dashboard'))


# 1.0.18: /admin observability + service control page.
# Registered here (after all top-level routes are defined) so the
# blueprint import doesn't create circular-import weirdness with the
# admin module reading CONFIG from us. 1.1.0: admin.py reads job state
# from /run/mail-archiver/sync-jobs/ via sync_lifecycle; no need to
# reach into a module-global dict.
try:
    import admin as _admin_module
    _admin_module.register(app)
except ImportError:
    pass
_bulk_routes.register(app)

# 1.3.0: shared page layout (sidebar navigation, footer version) + /about.
import layout as _layout_module
_layout_module.register(app)


if __name__ == '__main__':
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == 'scheduled-sync':
        scheduled_sync()
    elif len(sys.argv) > 1 and sys.argv[1] == 'sync-schedule':
        print_sync_schedule()
    elif len(sys.argv) > 1 and sys.argv[1] in ('import', 'pst-import'):
        # import                                      scheduled pass (hourly cron)
        # import --user U --manual [--account A] [--file F]   one user now
        # import --user U --ledger [--events] [--limit N] [--json]   show the ledger
        # (`pst-import` is the 1.3.0 development name, kept as an alias.)
        import argparse
        ap = argparse.ArgumentParser(prog='app.py import')
        ap.add_argument('--user')
        ap.add_argument('--account', help='with --file: the account whose inbox holds it')
        ap.add_argument('--file')
        ap.add_argument('--manual', action='store_true')
        ap.add_argument('--ledger', action='store_true', help='list ledger rows')
        ap.add_argument('--events', action='store_true',
                        help='with --ledger: retention deletions and refusals')
        ap.add_argument('--limit', type=int, default=50)
        ap.add_argument('--json', action='store_true')
        a = ap.parse_args(sys.argv[2:])
        if a.user and a.ledger:
            print_import_ledger(a.user, events=a.events, limit=a.limit, as_json=a.json)
        elif a.user and a.manual:
            mail_import_user(a.user, only=(a.account, a.file) if a.file else None,
                             trigger='manual', stable_wait=5)
        elif a.user:
            ap.error('--user needs --manual or --ledger (the scheduled pass covers every user)')
        else:
            scheduled_mail_import()
    elif len(sys.argv) > 1 and sys.argv[1] == 'bulk-sync':
        # bulk-sync            hourly pass over every initial / rebase sync job
        # bulk-sync --list | --manual | --reconcile   see bulk_sync_routes.cli
        sys.exit(_bulk_routes.cli(sys.argv[2:]))
    else:
        # Every interface on a server; this machine only on Windows, where
        # the app is a single-user desktop application. MAIL_ARCHIVER_HOST
        # overrides either.
        app.run(host=os.environ.get(
                    'MAIL_ARCHIVER_HOST',
                    '127.0.0.1' if _pc.IS_WINDOWS else '0.0.0.0'),
                port=CONFIG['listen_port'], debug=False)
