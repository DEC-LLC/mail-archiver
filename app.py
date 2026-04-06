#!/usr/bin/env python3
"""Mail Archiver — Web UI for managing mbsync email archival.

Standalone Flask app for archiving Gmail, iCloud, and Outlook email via IMAP.
Supports PAM auth (for NAS/server deployments) or built-in auth (for containers).
Set MAIL_ARCHIVER_AUTH=builtin for container mode, or =pam for NAS mode (default).
"""

import os
import json
import subprocess
import time
import hashlib
import secrets
import email
import email.policy
import re
from pathlib import Path
from functools import wraps

from flask import (Flask, render_template, request, redirect, url_for,
                   session, flash, jsonify, abort)

app = Flask(__name__)

# Load secret key from file if specified, else env var, else random
_secret_file = os.environ.get('MAIL_ARCHIVER_SECRET_FILE')
if _secret_file and os.path.exists(_secret_file):
    with open(_secret_file) as _f:
        app.secret_key = _f.read().strip()
else:
    app.secret_key = os.environ.get('MAIL_ARCHIVER_SECRET',
                                     secrets.token_hex(32))

# Configuration — override via environment or config file
CONFIG = {
    'data_dir': os.environ.get('MAIL_ARCHIVER_DATA', '/datapool/email-archive'),
    'listen_port': int(os.environ.get('MAIL_ARCHIVER_PORT', '8400')),
    'session_timeout': 3600,  # 1 hour
    'allowed_users': None,  # None = any PAM user, or list of usernames
    'auth_mode': os.environ.get('MAIL_ARCHIVER_AUTH', 'pam'),  # 'pam' or 'builtin'
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
        'host': 'outlook.office365.com',
        'port': 993,
        'auth': 'oauth2',
        'auth_label': 'Microsoft Account',
        'auth_help': 'Click "Sign in with Microsoft" — you\'ll be redirected to log in securely. No password stored locally.',
        'tls': True,
        'oauth2_provider': 'microsoft',
    },
    'hotmail_apppass': {
        'name': 'Outlook / Hotmail (App Password)',
        'host': 'outlook.office365.com',
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
        with open(uf) as f:
            return json.load(f)
    return {}


def _save_users(users):
    uf = _users_file()
    uf.parent.mkdir(parents=True, exist_ok=True)
    with open(uf, 'w') as f:
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
    return _hash_password(password, salt) == stored


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
    try:
        import PAM

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
    """Set ownership to the system user if running in PAM mode."""
    if CONFIG['auth_mode'] != 'pam':
        return
    try:
        import pwd
        pw = pwd.getpwnam(username)
        os.chown(str(path), pw.pw_uid, pw.pw_gid)
    except (KeyError, ImportError):
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
        with open(accounts_file) as f:
            return json.load(f)
    return []


def save_accounts(username, accounts):
    """Save user's email account list."""
    config_dir = get_user_config_dir(username)
    accounts_file = config_dir / 'accounts.json'
    with open(accounts_file, 'w') as f:
        json.dump(accounts, f, indent=2)
    os.chmod(str(accounts_file), 0o600)
    _chown_user(accounts_file, username)


def generate_mbsyncrc(username):
    """Generate mbsyncrc from accounts.json."""
    accounts = load_accounts(username)
    archive_dir = Path(CONFIG['data_dir']) / username
    if CONFIG['auth_mode'] == 'pam':
        import pwd
        try:
            pw = pwd.getpwnam(username)
            home = pw.pw_dir
        except KeyError:
            home = str(archive_dir)
    else:
        home = str(archive_dir)
    lines = [
        '# Auto-generated by mail-archiver. Do not edit manually.',
        f'# Generated: {time.strftime("%Y-%m-%d %H:%M:%S")}',
        '',
    ]

    for acct in accounts:
        if not acct.get('enabled', True):
            continue
        provider = PROVIDERS.get(acct['provider'], {})
        safe_name = acct['email'].replace('@', '_at_').replace('.', '_')
        maildir = archive_dir / safe_name

        lines.extend([
            f'# --- {acct["email"]} ({provider.get("name", acct["provider"])}) ---',
            f'IMAPAccount {safe_name}',
            f'Host {provider.get("host", acct.get("host", ""))}',
            f'Port {provider.get("port", 993)}',
            f'User {acct["email"]}',
        ])

        if acct['provider'] == 'hotmail' and acct.get('auth_type') == 'oauth2':
            lines.append(f'PassCmd "cat {archive_dir}/.config/{safe_name}.token"')
            lines.append('AuthMechs XOAUTH2')
        else:
            lines.append(f'PassCmd "cat {archive_dir}/.config/{safe_name}.pass"')

        if provider.get('tls', True):
            lines.append('SSLType IMAPS')
            lines.append('CertificateFile /etc/ssl/certs/ca-certificates.crt')

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
            'Patterns *',
            'Create Near',
            'Expunge None',
            'SyncState *',
            '',
        ])

    mbsyncrc = Path(home) / '.mbsyncrc'
    mbsyncrc.write_text('\n'.join(lines))
    os.chmod(str(mbsyncrc), 0o600)
    _chown_user(mbsyncrc, username)


def save_credential(username, email, credential):
    """Save an app password/token for an email account."""
    config_dir = get_user_config_dir(username)
    safe_name = email.replace('@', '_at_').replace('.', '_')
    cred_file = config_dir / f'{safe_name}.pass'
    cred_file.write_text(credential)
    os.chmod(str(cred_file), 0o600)
    _chown_user(cred_file, username)


# --- Sync Operations ---

def get_sync_status(username):
    """Get sync status for all accounts."""
    config_dir = get_user_config_dir(username)
    log_file = config_dir / 'sync.log'
    status_file = config_dir / 'sync_status.json'

    status = {}
    if status_file.exists():
        with open(status_file) as f:
            status = json.load(f)
    return status


def _has_mbsync():
    """Check if mbsync is available on this system."""
    import shutil
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
        password = acct.get('password', '')

        if provider.get('auth') == 'oauth2' or acct.get('auth_method') == 'oauth2':
            auth_method = 'oauth2'
            # Load OAuth2 token
            try:
                from oauth2_microsoft import MicrosoftOAuth2
                config_dir = get_user_config_dir(username)
                oauth_config_file = Path(CONFIG['data_dir']) / '.oauth2_config.json'
                if oauth_config_file.exists():
                    with open(oauth_config_file) as f:
                        oauth_cfg = json.load(f)
                    oauth = MicrosoftOAuth2(
                        client_id=oauth_cfg.get('client_id', ''),
                        client_secret=oauth_cfg.get('client_secret', ''),
                        redirect_uri=oauth_cfg.get('redirect_uri', ''),
                        data_dir=CONFIG['data_dir'],
                    )
                    token_data = oauth.ensure_fresh_token(username, safe_name)
                    if token_data:
                        oauth2_token = token_data.get('access_token', '')
            except Exception:
                pass

        syncer = ImapSyncer(
            host=provider.get('host', acct.get('host', '')),
            port=provider.get('port', acct.get('port', 993)),
            use_tls=provider.get('tls', True),
            username=acct['email'],
            password=password,
            local_dir=local_dir,
            auth_method=auth_method,
            oauth2_token=oauth2_token,
        )

        try:
            result = syncer.sync()
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


def run_sync(username, account_email=None):
    """Trigger email sync for a user.

    Auto-detects sync backend: mbsync if available, imaplib fallback.
    """
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
    }
    with open(status_file, 'w') as f:
        json.dump(status, f, indent=2)

    # Auto-detect backend: mbsync (fast, Linux/Mac) or imaplib (portable)
    if not _has_mbsync():
        _run_sync_imaplib(username, account_email, status, key, status_file)
    else:
        # mbsync path
        if account_email:
            safe_name = account_email.replace('@', '_at_').replace('.', '_')
            safe_name = re.sub(r'[^a-zA-Z0-9_]', '', safe_name)
            mbsync_arg = safe_name
        else:
            mbsync_arg = '-a'

        if CONFIG['auth_mode'] == 'pam':
            cmd = ['su', '-', username, '-c',
                   f'mbsync {shlex.quote(mbsync_arg)}']
        else:
            rc = archive_dir / '.mbsyncrc'
            cmd = ['mbsync', '-c', str(rc), mbsync_arg]

        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=3600
            )
            if result.returncode == 0:
                status[key] = {
                    'state': 'ok',
                    'finished': time.strftime('%Y-%m-%d %H:%M:%S'),
                    'exit_code': 0,
                    'error': '',
                }
            else:
                raw = result.stderr[-500:] if result.stderr else ''
                status[key] = {
                    'state': 'error',
                    'finished': time.strftime('%Y-%m-%d %H:%M:%S'),
                    'exit_code': result.returncode,
                    'error': friendly_sync_error(raw, account_email or ''),
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

    with open(status_file, 'w') as f:
        json.dump(status, f, indent=2)
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


def friendly_sync_error(raw_error, email=''):
    """Turn raw mbsync stderr into a user-friendly message."""
    e = raw_error.strip()
    el = e.lower()

    if 'authenticationfailed' in el or 'authenticate' in el and 'error' in el:
        domain = email.split('@')[-1] if '@' in email else ''
        hints = ['Check that the email address is spelled correctly.']
        if 'icloud' in domain or 'me.com' in domain:
            hints.append('For iCloud: use your Apple ID email and an app-specific password from appleid.apple.com.')
        elif 'gmail' in domain or 'google' in domain:
            hints.append('For Gmail: use an app password from myaccount.google.com (not your regular password).')
        elif 'outlook' in domain or 'hotmail' in domain or 'live' in domain:
            hints.append('For Outlook/Hotmail: app passwords or OAuth2 may be required.')
        else:
            hints.append('Make sure you are using an app-specific password, not your regular login password.')
        return 'Authentication failed. ' + ' '.join(hints)

    if 'resolve' in el or 'getaddrinfo' in el or 'connection refused' in el:
        return f'Could not connect to mail server. Check your internet connection and that the email provider is correct.'

    if 'certificate' in el or 'ssl' in el or 'tls' in el:
        return f'TLS/SSL error connecting to mail server. The server certificate may have changed.'

    if 'strstrstrstrstr' in el or 'strftime' in el:
        return 'Sync configuration error. Try removing and re-adding the account.'

    if 'no strftime' in el or 'strftime' in el:
        return 'Configuration error in mbsyncrc. Try removing and re-adding the account.'

    if not e:
        return 'Sync failed with no error details. Check that mbsync is installed.'

    # Truncate but keep raw error if we can't parse it
    if len(e) > 200:
        e = e[:200] + '...'
    return f'Sync error: {e}'


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
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')

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
                           auth_mode=CONFIG['auth_mode'])


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))


@app.route('/register', methods=['GET', 'POST'])
def register():
    if CONFIG['auth_mode'] != 'builtin':
        abort(404)
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

    archive_dir = f"{CONFIG['data_dir']}/{username}"
    if CONFIG['auth_mode'] == 'pam':
        try:
            import pwd
            home_dir = pwd.getpwnam(username).pw_dir
        except (KeyError, ImportError):
            home_dir = archive_dir
    else:
        home_dir = archive_dir

    return render_template('dashboard.html',
                           username=username,
                           accounts=accounts,
                           home_dir=home_dir,
                           archive_dir=archive_dir,
                           sync_intervals=SYNC_INTERVALS)


@app.route('/account/add', methods=['GET', 'POST'])
@login_required
def add_account():
    username = session['username']

    if request.method == 'POST':
        provider = request.form.get('provider', '')
        email = request.form.get('email', '').strip()
        credential = request.form.get('credential', '').strip()

        if not provider or not email:
            flash('Provider and email are required.')
            return render_template('add_account.html', providers=PROVIDERS)

        if provider not in PROVIDERS:
            flash('Unknown provider.')
            return render_template('add_account.html', providers=PROVIDERS)

        provider_info = PROVIDERS[provider]

        # For app_password providers, credential is required
        if provider_info['auth'] == 'app_password' and not credential:
            flash(f'{provider_info["auth_label"]} is required.')
            return render_template('add_account.html', providers=PROVIDERS)

        accounts = load_accounts(username)

        # Check for duplicate
        if any(a['email'] == email and a['provider'] == provider for a in accounts):
            flash(f'{email} ({provider_info["name"]}) is already registered.')
            return render_template('add_account.html', providers=PROVIDERS)

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

        # Custom IMAP: user provides host and port
        if provider_info.get('custom_host'):
            custom_host = request.form.get('custom_host', '').strip()
            custom_port = request.form.get('custom_port', '993').strip()
            if not custom_host:
                flash('IMAP server hostname is required for custom provider.')
                return render_template('add_account.html', providers=PROVIDERS)
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

        flash(f'Added {email}. You can now sync it.')
        return redirect(url_for('dashboard'))

    return render_template('add_account.html', providers=PROVIDERS)


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


@app.route('/sync', methods=['POST'])
@login_required
def sync_all():
    username = session['username']
    result = run_sync(username)
    if result['state'] == 'ok':
        flash('Sync completed successfully.')
    else:
        flash(f'Sync failed: {result.get("error", "unknown error")}')
    return redirect(url_for('dashboard'))


@app.route('/sync/<email>', methods=['POST'])
@login_required
def sync_account(email):
    username = session['username']
    result = run_sync(username, email)
    if result['state'] == 'ok':
        flash(f'Sync for {email} completed successfully.')
    else:
        flash(f'Sync for {email} failed: {result.get("error", "unknown error")}')
    return redirect(url_for('dashboard'))


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
            if d.is_dir() and not d.name.startswith('.'):
                search_dirs.append(str(d))

    if not search_dirs:
        return []

    # Pass 1: Fast grep for candidate files
    # Use grep -rl for file list, case-insensitive, binary-safe
    candidate_files = []
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
                if f and ('/cur/' in f or '/new/' in f)
            ]
    except (subprocess.TimeoutExpired, Exception):
        pass

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
        return _export_results(results, export_format, query)

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


def _export_results(results, fmt, query):
    """Export search results as MBOX or EML zip."""
    from io import BytesIO
    import zipfile

    # Path traversal guard: only allow files under the user's data directory
    data_root = os.path.realpath(CONFIG['data_dir'])

    def _safe_path(filepath):
        """Return True only if filepath is under the data directory."""
        if not filepath:
            return False
        real = os.path.realpath(filepath)
        return real.startswith(data_root + os.sep) and os.path.isfile(real)

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
    if not real_path.startswith(data_root + os.sep) or not os.path.isfile(real_path):
        abort(403)

    # Parse the raw email
    try:
        raw_bytes = open(real_path, 'rb').read()
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
    if not real_path.startswith(data_root + os.sep) or not os.path.isfile(real_path):
        abort(403)

    try:
        raw_bytes = open(real_path, 'rb').read()
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
    result = rebuild_index(username, CONFIG['data_dir'])
    flash(f'Search index rebuilt: {result["indexed"]} emails indexed in {result["total_time"]:.1f}s.')
    return redirect(url_for('search'))


def _stagger_offset(email):
    """Derive a deterministic offset (0-3599 seconds) from email for staggering."""
    return int(hashlib.md5(email.encode()).hexdigest()[:8], 16) % 3600


def scheduled_sync():
    """Run scheduled syncs for all users. Called by cron hourly.

    Accounts are auto-staggered: sorted by a hash of their email
    address with brief pauses between syncs, so multiple accounts
    don't all hit their IMAP servers simultaneously.
    """
    data_dir = Path(CONFIG['data_dir'])
    if not data_dir.exists():
        return
    now = time.time()
    due = []

    for userdir in sorted(data_dir.iterdir()):
        if not userdir.is_dir() or userdir.name.startswith('.'):
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
            acct_status = sync_status.get(acct['email'], {})
            last_finished = acct_status.get('finished', '')
            if last_finished:
                try:
                    last_ts = time.mktime(time.strptime(last_finished, '%Y-%m-%d %H:%M:%S'))
                    if now - last_ts < interval_secs:
                        continue
                except ValueError:
                    pass
            due.append((username, acct['email'], interval_key,
                        _stagger_offset(acct['email'])))

    # Sort by stagger hash so accounts sync in a deterministic spread order
    due.sort(key=lambda x: x[3])
    print(f'=== Scheduled sync: {len(due)} account(s) due at {time.strftime("%Y-%m-%d %H:%M:%S")} ===')

    for i, (username, email, interval_key, _) in enumerate(due):
        if i > 0:
            time.sleep(10)  # 10s pause between accounts
        print(f'Syncing {email} for {username} (interval: {interval_key})')
        result = run_sync(username, email)
        print(f'  -> {result.get("state", "error")}')

    print(f'=== Done ===')


# --- Microsoft OAuth2 Routes ---

@app.route('/oauth2/settings', methods=['GET', 'POST'])
@login_required
def oauth2_settings():
    """Configure Microsoft OAuth2 client credentials (Azure AD app)."""
    from oauth2_microsoft import load_oauth2_config, save_oauth2_config

    if request.method == 'POST':
        client_id = request.form.get('client_id', '').strip()
        client_secret = request.form.get('client_secret', '').strip()
        if client_id and client_secret:
            config = load_oauth2_config(CONFIG['data_dir'])
            config['microsoft'] = {
                'client_id': client_id,
                'client_secret': client_secret,
            }
            save_oauth2_config(CONFIG['data_dir'], config)
            flash('Microsoft OAuth2 credentials saved.')
        else:
            flash('Both Client ID and Client Secret are required.')
        return redirect(url_for('oauth2_settings'))

    config = load_oauth2_config(CONFIG['data_dir'])
    ms_config = config.get('microsoft', {})
    return render_template('oauth2_settings.html',
                           username=session['username'],
                           client_id=ms_config.get('client_id', ''),
                           has_secret=bool(ms_config.get('client_secret')))


@app.route('/oauth2/authorize')
@login_required
def oauth2_authorize():
    """Start Microsoft OAuth2 flow — redirect to Microsoft login."""
    from oauth2_microsoft import get_microsoft_oauth2

    email = request.args.get('email', '')
    if not email:
        flash('Email address required for OAuth2.')
        return redirect(url_for('dashboard'))

    try:
        redirect_uri = url_for('oauth2_callback', _external=True)
        oauth2 = get_microsoft_oauth2(CONFIG['data_dir'], redirect_uri)
        auth_url, state = oauth2.get_authorization_url()
        # Store state + email in session for the callback
        session['oauth2_state'] = state
        session['oauth2_email'] = email
        return redirect(auth_url)
    except ValueError as e:
        flash(str(e))
        return redirect(url_for('dashboard'))


@app.route('/oauth2/callback')
@login_required
def oauth2_callback():
    """Handle Microsoft OAuth2 callback — exchange code for tokens."""
    from oauth2_microsoft import (get_microsoft_oauth2, save_oauth2_tokens)

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
    session.pop('oauth2_state', None)

    if not email:
        flash('OAuth2 session expired. Please try again.')
        return redirect(url_for('dashboard'))

    try:
        redirect_uri = url_for('oauth2_callback', _external=True)
        oauth2 = get_microsoft_oauth2(CONFIG['data_dir'], redirect_uri)
        tokens = oauth2.exchange_code(code)
        save_oauth2_tokens(CONFIG['data_dir'], session['username'], email, tokens)

        # Update account auth_type to oauth2
        username = session['username']
        accounts = load_accounts(username)
        for acct in accounts:
            if acct['email'] == email:
                acct['auth_type'] = 'oauth2'
                break
        else:
            # Account doesn't exist yet — create it
            accounts.append({
                'email': email,
                'provider': 'hotmail',
                'auth_type': 'oauth2',
                'enabled': True,
                'sync_interval': 'daily',
                'added': time.strftime('%Y-%m-%d %H:%M:%S'),
            })
        save_accounts(username, accounts)
        generate_mbsyncrc(username)

        # Create maildir
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
    """Manually refresh OAuth2 token for an account."""
    from oauth2_microsoft import get_microsoft_oauth2, ensure_fresh_token

    try:
        redirect_uri = url_for('oauth2_callback', _external=True)
        oauth2 = get_microsoft_oauth2(CONFIG['data_dir'], redirect_uri)
        token = ensure_fresh_token(oauth2, CONFIG['data_dir'],
                                   session['username'], email)
        flash(f'Token refreshed for {email}.')
    except ValueError as e:
        flash(f'Token refresh failed: {e}')

    return redirect(url_for('dashboard'))


if __name__ == '__main__':
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == 'scheduled-sync':
        scheduled_sync()
    else:
        app.run(host='0.0.0.0', port=CONFIG['listen_port'], debug=False)
