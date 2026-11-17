"""IMAP sync backend using Python stdlib imaplib.

This replaces mbsync for platforms where mbsync isn't available (Windows, macOS
without Homebrew). Uses only Python stdlib — no external dependencies.

Syncs all folders from an IMAP account to local Maildir format.
Supports Gmail, iCloud, Outlook (app password and OAuth2 XOAUTH2).
"""

import imaplib
import re
import email
import email.policy
import os
import time
import json
import hashlib
import logging
from pathlib import Path
from typing import Optional

import platform_compat as _pc

log = logging.getLogger('mail_archiver.imap_sync')


_LIST_RE = re.compile(
    r'^\((?P<flags>[^)]*)\)\s+(?P<delim>"(?:[^"\\]|\\.)*"|NIL)\s+(?P<name>.*)$', re.S)


def parse_list_line(item) -> Optional[str]:
    """The folder name in one LIST response line, or None for a line that
    names nothing to open (a \\Noselect container, or a line that cannot be
    read).

    A line is `(flags) "delimiter" name`. The name is quoted (Gmail:
    `"[Gmail]/All Mail"`), a bare atom (Dovecot: `INBOX`, `Junk`) or sent
    as a literal, which imaplib hands over as a (header, name) pair. Up to
    1.3.0 the last quoted string on the line was taken, which for a bare
    name is the delimiter: every folder of such a server was read as ".".
    """
    literal = None
    if isinstance(item, tuple):
        if len(item) < 2:
            return None
        item, literal = item[0], item[1]
    if not isinstance(item, bytes):
        return None
    m = _LIST_RE.match(item.decode('utf-8', errors='replace').strip())
    if not m:
        return None
    if '\\noselect' in m.group('flags').lower():
        return None
    name = m.group('name').strip()
    if literal is not None:
        name = literal.decode('utf-8', errors='replace') if isinstance(literal, bytes) else str(literal)
    elif len(name) >= 2 and name[0] == '"' and name[-1] == '"':
        name = re.sub(r'\\(.)', r'\1', name[1:-1])
    return name or None


def _imap_text(data) -> str:
    """Server response data as short text for a message."""
    try:
        return ' '.join(d.decode('utf-8', errors='replace') if isinstance(d, bytes) else str(d)
                        for d in (data or []))[:200]
    except Exception:
        return str(data)[:200]


PLAINTEXT_REFUSED = (
    'This account is set to "Plaintext" (no TLS). The built-in IMAP client '
    'does not send a password over an unencrypted connection. Edit the '
    'account and choose SSL/TLS (port 993) or STARTTLS (port 143).')


class PlaintextLoginRefused(RuntimeError):
    """The account asks for a login without TLS."""


def normalise_tls_mode(tls_mode, use_tls=True) -> str:
    """'ssl', 'starttls' or 'none' from an account's setting.

    An empty or unknown value follows the older true/false `use_tls`.
    """
    mode = (tls_mode or '').strip().lower()
    if mode in ('ssl', 'starttls', 'none'):
        return mode
    return 'ssl' if use_tls else 'none'


class ImapSyncer:
    """Sync a single IMAP account to local Maildir.

    Usage::

        syncer = ImapSyncer(
            host='imap.gmail.com', port=993, use_tls=True,
            username='user@gmail.com', password='app-password',
            local_dir=Path('C:/Users/me/MailArchive/user_at_gmail_com'),
        )
        result = syncer.sync()
        print(f"New: {result['new']}, Skipped: {result['skipped']}")
    """

    def __init__(self, host: str, port: int, use_tls: bool,
                 username: str, password: str,
                 local_dir: Path,
                 auth_method: str = 'password',
                 oauth2_token: Optional[str] = None,
                 folders: Optional[list] = None,
                 max_messages: int = 0,
                 chown_uid: Optional[int] = None,
                 chown_gid: Optional[int] = None,
                 tls_mode: Optional[str] = None):
        self.host = host
        self.port = port
        self.use_tls = use_tls
        # 1.3.2: 'ssl' (IMAPS), 'starttls' or 'none'. Left out, it follows
        # use_tls as before. 'none' never logs in - see connect().
        self.tls_mode = normalise_tls_mode(tls_mode, use_tls)
        self.username = username
        self.password = password
        self.local_dir = Path(local_dir)
        self.auth_method = auth_method
        self.oauth2_token = oauth2_token
        self.folders = folders  # None = all folders
        self.max_messages = max_messages  # 0 = no limit
        # T5 fix: when the Flask worker (mail-archiver uid) writes into a
        # per-user Maildir, chown after each write so the target PAM user
        # can rewrite / mbsync can later take over without dual-uid
        # corruption. None disables chowning (container / builtin-auth mode).
        self.chown_uid = chown_uid
        self.chown_gid = chown_gid

    def _chown(self, path):
        """Best-effort chown to (chown_uid, chown_gid). Silent on failure —
        wrong ownership is worse than dead sync, but not by much."""
        if self.chown_uid is None or self.chown_gid is None:
            return
        try:
            _pc.chown(str(path), self.chown_uid, self.chown_gid)
        except (PermissionError, OSError):
            pass

    def connect(self) -> imaplib.IMAP4:
        """Connect and authenticate to the IMAP server.

        The password or token is only ever sent inside TLS: either the
        connection starts encrypted ('ssl') or it is upgraded with STARTTLS
        before anything else is said ('starttls'). With 'none' there is no
        login at all (1.3.2; up to 1.3.1 it logged in in the clear).
        """
        mode = normalise_tls_mode(getattr(self, 'tls_mode', None),
                                  getattr(self, 'use_tls', True))
        if mode == 'ssl':
            conn = imaplib.IMAP4_SSL(self.host, self.port,
                                     ssl_context=_pc.tls_client_context())
        elif mode == 'starttls':
            conn = imaplib.IMAP4(self.host, self.port)
            try:
                conn.starttls(ssl_context=_pc.tls_client_context())
            except Exception:
                # No STARTTLS on offer, or the certificate did not verify:
                # leave without logging in.
                try:
                    conn.shutdown()
                except Exception:
                    pass
                raise
        else:
            raise PlaintextLoginRefused(PLAINTEXT_REFUSED)

        if self.auth_method == 'oauth2' and self.oauth2_token:
            # XOAUTH2 for Microsoft/Google
            auth_string = f'user={self.username}\x01auth=Bearer {self.oauth2_token}\x01\x01'
            conn.authenticate('XOAUTH2', lambda x: auth_string.encode())
        else:
            conn.login(self.username, self.password)

        return conn

    def list_folders(self, conn: imaplib.IMAP4_SSL) -> list:
        """List the IMAP folders on the server that can be opened."""
        status, data = conn.list()
        if status != 'OK':
            return []
        folders = []
        for item in data:
            name = parse_list_line(item)
            if name:
                folders.append(name)
        return folders

    def _safe_folder_name(self, folder: str) -> str:
        """Convert IMAP folder name to safe filesystem path.

        The last step changes nothing on Linux; on Windows it replaces the
        characters NTFS refuses in a name (a folder called `Lists: dev`).
        """
        return _pc.safe_path_component(
            folder.replace('/', '_').replace('\\', '_').replace(
                '[Gmail]', 'Gmail').replace(' ', '_').strip('.'))

    def _message_filename(self, uid: str, msg_bytes: bytes) -> str:
        """Generate a unique Maildir-compatible filename for a message."""
        h = hashlib.md5(msg_bytes).hexdigest()[:12]
        return f'{int(time.time())}.{uid}.{h}'

    def sync_folder(self, conn: imaplib.IMAP4_SSL, folder: str) -> dict:
        """Sync a single IMAP folder to local Maildir.

        Returns dict with counts: new, skipped, errors.
        """
        result = {'new': 0, 'skipped': 0, 'errors': 0}
        safe_name = self._safe_folder_name(folder)

        # Create Maildir structure: cur/ new/ tmp/
        folder_dir = self.local_dir / safe_name
        # Ensure the local_dir + folder_dir themselves are owned by the
        # target PAM user (T5) — they may have been created by prior runs
        # under mail-archiver uid and would then be un-writable by mbsync.
        if not self.local_dir.exists():
            self.local_dir.mkdir(parents=True, exist_ok=True)
        self._chown(self.local_dir)
        if not folder_dir.exists():
            folder_dir.mkdir(parents=True, exist_ok=True)
        self._chown(folder_dir)
        for sub in ('cur', 'new', 'tmp'):
            subdir = folder_dir / sub
            subdir.mkdir(parents=True, exist_ok=True)
            self._chown(subdir)

        # Select folder
        try:
            status, data = conn.select(f'"{folder}"', readonly=True)
            if status != 'OK':
                log.warning('Cannot select folder %s: %s', folder, data)
                result['errors'] += 1
                result['error'] = 'cannot open folder: %s' % _imap_text(data)
                return result
        except imaplib.IMAP4.error as e:
            log.warning('Error selecting %s: %s', folder, e)
            result['errors'] += 1
            result['error'] = 'cannot open folder: %s' % e
            return result

        # Get message count
        msg_count = int(data[0])
        if msg_count == 0:
            return result

        # Track already-downloaded messages by UID
        uid_file = folder_dir / '.synced_uids.json'
        synced_uids = set()
        if uid_file.exists():
            try:
                synced_uids = set(json.loads(uid_file.read_text(encoding='utf-8')))
            except (json.JSONDecodeError, OSError):
                pass

        # Fetch UIDs
        status, uid_data = conn.uid('search', None, 'ALL')
        if status != 'OK':
            return result

        all_uids = uid_data[0].split()
        if self.max_messages > 0:
            all_uids = all_uids[-self.max_messages:]  # Most recent N

        for uid_bytes in all_uids:
            uid = uid_bytes.decode()
            if uid in synced_uids:
                result['skipped'] += 1
                continue

            try:
                status, msg_data = conn.uid('fetch', uid, '(RFC822)')
                if status != 'OK' or not msg_data or not msg_data[0]:
                    result['errors'] += 1
                    continue

                raw = msg_data[0][1]
                if not isinstance(raw, bytes):
                    result['errors'] += 1
                    continue

                # Save to Maildir cur/ (already seen — we're archiving)
                filename = self._message_filename(uid, raw)
                dest = folder_dir / 'cur' / filename
                dest.write_bytes(raw)
                self._chown(dest)  # T5

                synced_uids.add(uid)
                result['new'] += 1

            except Exception as e:
                log.warning('Error fetching UID %s in %s: %s', uid, folder, e)
                result['errors'] += 1

        # Save synced UIDs
        uid_file.write_text(json.dumps(sorted(synced_uids)), encoding='utf-8')
        self._chown(uid_file)  # T5

        return result

    def sync(self) -> dict:
        """Sync all folders (or specified folders) from the IMAP account.

        Returns dict with folder-level results and totals.
        """
        result = {
            'folders': {},
            'total_new': 0,
            'total_skipped': 0,
            'total_errors': 0,
            'sync_time': 0,
            'success': False,
        }

        t0 = time.time()

        try:
            conn = self.connect()
        except Exception as e:
            log.error('Connection failed: %s', e)
            result['error'] = str(e)
            result['sync_time'] = round(time.time() - t0, 1)
            return result

        try:
            folders = self.folders or self.list_folders(conn)
            log.info('Syncing %d folders for %s', len(folders), self.username)

            for folder in folders:
                try:
                    folder_result = self.sync_folder(conn, folder)
                    result['folders'][folder] = folder_result
                    result['total_new'] += folder_result['new']
                    result['total_skipped'] += folder_result['skipped']
                    result['total_errors'] += folder_result['errors']
                except Exception as e:
                    log.warning('Error syncing folder %s: %s', folder, e)
                    result['folders'][folder] = {'error': str(e)}

            # A server whose every folder refused to open was not synced:
            # report it, do not record a clean sync with nothing new.
            failed = [f for f, r in result['folders'].items() if r.get('error')]
            if folders and len(failed) == len(folders):
                result['error'] = 'no folder could be opened (%s: %s)' % (
                    failed[0], result['folders'][failed[0]]['error'])
            else:
                result['success'] = True

        finally:
            try:
                conn.logout()
            except Exception:
                pass

        result['sync_time'] = round(time.time() - t0, 1)
        return result


def sync_account(account_config: dict, data_dir: Path) -> dict:
    """Convenience wrapper: sync one account from its config dict.

    Args:
        account_config: dict with keys: email, provider, host, port, tls,
                       password, auth_method, oauth2_token
        data_dir: base data directory (account dir created under it)

    Returns:
        Sync result dict.
    """
    safe_name = account_config['email'].replace('@', '_at_').replace('.', '_')
    local_dir = data_dir / safe_name

    syncer = ImapSyncer(
        host=account_config.get('host', 'imap.gmail.com'),
        port=account_config.get('port', 993),
        use_tls=account_config.get('tls', True),
        tls_mode=account_config.get('tls_mode'),
        username=account_config['email'],
        password=account_config.get('password', ''),
        local_dir=local_dir,
        auth_method=account_config.get('auth_method', 'password'),
        oauth2_token=account_config.get('oauth2_token'),
    )

    return syncer.sync()
