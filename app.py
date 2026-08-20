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
import shlex
import shutil
import fcntl
from pathlib import Path
from functools import wraps

from flask import (Flask, render_template, request, redirect, url_for,
                   session, flash, jsonify, abort, send_from_directory)
from werkzeug.utils import secure_filename

app = Flask(__name__)
# Cap request bodies at 256 KiB. Only routes we accept uploads on are the
# per-account client-cert PEM uploads — a PEM cert + PEM key together are
# well under 20 KiB. This guards the whole app from oversize form bodies.
app.config['MAX_CONTENT_LENGTH'] = 256 * 1024

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
            lines.append(f'PassCmd "cat {config_dir_str}/{safe_name}.token"')
            lines.append('AuthMechs XOAUTH2')
        else:
            # T8 hardening: hand off to /usr/libexec/mail-archiver-cred so
            # username + email arrive via argv (no shell interpolation) and
            # sys.path is fixed at /opt/mail-archiver (no writable-dir
            # traversal). shlex.quote on the two args neutralizes any
            # exotic characters so mbsync's /bin/sh -c sees each as one arg.
            lines.append(
                f'PassCmd "/usr/libexec/mail-archiver-cred '
                f'{shlex.quote(username)} {shlex.quote(acct["email"])}"'
            )

        # TLS mode is now per-account, three-valued: ssl | starttls | none.
        # mbsync accepts SSLType {IMAPS,STARTTLS,None}.
        if tls_mode == 'ssl':
            lines.append('SSLType IMAPS')
        elif tls_mode == 'starttls':
            lines.append('SSLType STARTTLS')
        else:
            lines.append('SSLType None')
        if tls_mode != 'none':
            lines.append('CertificateFile /etc/ssl/certs/ca-certificates.crt')

        # Client cert + key (mutual-TLS IMAP — Exchange with mTLS front-end,
        # some paid providers, self-hosted Dovecot with cert-auth).
        # Both filenames are stored under ${DATA}/${user}/.config/ and were
        # written by the cert-upload route with secure_filename + chown.
        client_cert = acct.get('client_cert')
        client_key = acct.get('client_key')
        if client_cert:
            lines.append(f'ClientCertificate {config_dir_str}/{client_cert}')
        if client_key:
            lines.append(f'ClientKey {config_dir_str}/{client_key}')

        # Timeout (mbsync default is 20s — expose so slow WAN paths + big
        # first syncs don't drop with generic "connection lost").
        timeout = acct.get('timeout_seconds')
        if timeout:
            lines.append(f'Timeout {int(timeout)}')

        pattern = acct.get('folder_pattern') or '*'

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
            'Create Near',
            'Expunge None',
            'SyncState *',
            '',
        ])

    mbsyncrc = Path(home) / '.mbsyncrc'
    mbsyncrc.write_text('\n'.join(lines))
    os.chmod(str(mbsyncrc), 0o600)
    _chown_user(mbsyncrc, username)


def _get_encryption_key():
    """Derive a 32-byte encryption key from the Flask secret key."""
    import hashlib
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
    cred_file.write_text(encrypted)
    os.chmod(str(cred_file), 0o600)
    _chown_user(cred_file, username)


def load_credential(username, email):
    """Load and decrypt an app password/token for an email account."""
    config_dir = get_user_config_dir(username)
    safe_name = email.replace('@', '_at_').replace('.', '_')
    cred_file = config_dir / f'{safe_name}.pass'
    if not cred_file.exists():
        return None
    raw = cred_file.read_text().strip()
    if not raw:
        return None
    # Encrypted credentials are always base64 with length > 24 (16-byte IV + data)
    # Legacy plaintext passwords are short ASCII strings (app passwords, etc.)
    try:
        import base64
        decoded = base64.b64decode(raw)
        if len(decoded) > 16:
            return _decrypt_credential(raw)
    except Exception:
        pass
    # Legacy plaintext file — return as-is
    return raw


# --- Sync Operations ---

def _write_sync_status_locked(status_file, state):
    """Atomic + locked write of sync_status.json.

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
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            with open(tmp, 'w') as f:
                f.write(payload)
            os.replace(str(tmp), str(status_file))
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)
    except (OSError, IOError):
        # Locking failed (e.g. fs doesn't support flock) — degraded
        # non-atomic path. Better than blocking the sync.
        try:
            with open(status_file, 'w') as f:
                f.write(payload)
        except OSError:
            return


def _read_sync_status_locked(status_file):
    """Shared-lock read of sync_status.json. Returns {} on any error."""
    status_file = Path(status_file)
    if not status_file.exists():
        return {}
    lock_path = status_file.with_suffix(status_file.suffix + '.lock')
    try:
        lock_fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_SH)
            with open(status_file) as f:
                return json.load(f)
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)
    except (OSError, IOError, json.JSONDecodeError):
        # Unlocked fallback read
        try:
            with open(status_file) as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            return {}


def get_sync_status(username):
    """Get sync status for all accounts."""
    config_dir = get_user_config_dir(username)
    status_file = config_dir / 'sync_status.json'
    return _read_sync_status_locked(status_file)


def _has_mbsync():
    """Check if mbsync is available on this system."""
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

        if provider.get('auth') == 'oauth2' or acct.get('auth_method') == 'oauth2':
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
        if CONFIG['auth_mode'] == 'pam':
            try:
                import pwd as _pwd
                pw = _pwd.getpwnam(username)
                chown_uid, chown_gid = pw.pw_uid, pw.pw_gid
            except KeyError:
                # PAM user vanished from /etc/passwd between login and sync.
                # Skip chown — files land under mail-archiver, operator will
                # need to re-chown by hand once user comes back.
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
            chown_uid=chown_uid,
            chown_gid=chown_gid,
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
    # T11: locked+atomic (cron and webui both write this file)
    _write_sync_status_locked(status_file, status)

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

        # Build subprocess.run kwargs. PAM-mode calls subprocess with
        # user=uid, group=gid — the kernel does setuid at exec time from
        # our ambient CAP_SETUID+CAP_SETGID. No shell wrapper needed:
        # su(1) prompts for password when caller isn't root, and
        # runuser(1) requires root on Debian (only setuid on Rocky/RHEL).
        # Kernel-level setuid via subprocess kwargs is portable + clean.
        subprocess_kwargs = {'capture_output': True, 'text': True,
                             'timeout': 3600}
        if CONFIG['auth_mode'] == 'pam':
            import pwd as _pwd
            try:
                pw = _pwd.getpwnam(username)
            except KeyError:
                raise ValueError(
                    f'PAM user {username!r} not present on this host — '
                    f'cannot run mbsync as that identity')
            cmd = ['mbsync', mbsync_arg]
            subprocess_kwargs.update({
                'user':  pw.pw_uid,
                'group': pw.pw_gid,
                'cwd':   pw.pw_dir,
                # Fresh minimal env — inherited env from the mail-archiver
                # service context would leak HOME=/var/lib/mail-archiver
                # etc. mbsync + our PassCmd helper only need HOME (for
                # ~/.mbsyncrc lookup), USER (for identity), and PATH (for
                # the helper's /usr/libexec/mail-archiver-cred).
                'env': {
                    'HOME':    pw.pw_dir,
                    'USER':    username,
                    'LOGNAME': username,
                    'SHELL':   pw.pw_shell or '/bin/sh',
                    'PATH':    '/usr/local/sbin:/usr/local/bin:'
                               '/usr/sbin:/usr/bin:/sbin:/bin',
                },
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

    # T11: locked+atomic (cron and webui both write this file)
    _write_sync_status_locked(status_file, status)
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

    # Item-1 (1.0.10): make the OAuth-app picker available at add-time so
    # family onboarding is one form, not "add → dashboard → Settings →
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
    ('none',     'Plaintext (NOT recommended)'),
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
    plain = load_credential(username, email) or ''
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
        with open(token_file) as f:
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
            with open(af) as f:
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


if __name__ == '__main__':
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == 'scheduled-sync':
        scheduled_sync()
    else:
        app.run(host='0.0.0.0', port=CONFIG['listen_port'], debug=False)
