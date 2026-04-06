"""IMAP sync backend using Python stdlib imaplib.

This replaces mbsync for platforms where mbsync isn't available (Windows, macOS
without Homebrew). Uses only Python stdlib — no external dependencies.

Syncs all folders from an IMAP account to local Maildir format.
Supports Gmail, iCloud, Outlook (app password and OAuth2 XOAUTH2).
"""

import imaplib
import email
import email.policy
import os
import time
import json
import hashlib
import logging
from pathlib import Path
from typing import Optional

log = logging.getLogger('mail_archiver.imap_sync')


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
                 max_messages: int = 0):
        self.host = host
        self.port = port
        self.use_tls = use_tls
        self.username = username
        self.password = password
        self.local_dir = Path(local_dir)
        self.auth_method = auth_method
        self.oauth2_token = oauth2_token
        self.folders = folders  # None = all folders
        self.max_messages = max_messages  # 0 = no limit

    def connect(self) -> imaplib.IMAP4_SSL:
        """Connect and authenticate to the IMAP server."""
        if self.use_tls:
            conn = imaplib.IMAP4_SSL(self.host, self.port)
        else:
            conn = imaplib.IMAP4(self.host, self.port)

        if self.auth_method == 'oauth2' and self.oauth2_token:
            # XOAUTH2 for Microsoft/Google
            auth_string = f'user={self.username}\x01auth=Bearer {self.oauth2_token}\x01\x01'
            conn.authenticate('XOAUTH2', lambda x: auth_string.encode())
        else:
            conn.login(self.username, self.password)

        return conn

    def list_folders(self, conn: imaplib.IMAP4_SSL) -> list:
        """List all IMAP folders on the server."""
        status, data = conn.list()
        if status != 'OK':
            return []

        folders = []
        for item in data:
            if isinstance(item, bytes):
                # Parse: (\\HasNoChildren) "/" "INBOX"
                decoded = item.decode('utf-8', errors='replace')
                # Extract folder name (last quoted string or last token)
                parts = decoded.rsplit('"', 2)
                if len(parts) >= 2:
                    folders.append(parts[-2])
                else:
                    # Unquoted folder name
                    folders.append(decoded.rsplit(' ', 1)[-1])
        return folders

    def _safe_folder_name(self, folder: str) -> str:
        """Convert IMAP folder name to safe filesystem path."""
        return folder.replace('/', '_').replace('\\', '_').replace(
            '[Gmail]', 'Gmail').replace(' ', '_').strip('.')

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
        for sub in ('cur', 'new', 'tmp'):
            (folder_dir / sub).mkdir(parents=True, exist_ok=True)

        # Select folder
        try:
            status, data = conn.select(f'"{folder}"', readonly=True)
            if status != 'OK':
                log.warning('Cannot select folder %s: %s', folder, data)
                return result
        except imaplib.IMAP4.error as e:
            log.warning('Error selecting %s: %s', folder, e)
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
                synced_uids = set(json.loads(uid_file.read_text()))
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

                synced_uids.add(uid)
                result['new'] += 1

            except Exception as e:
                log.warning('Error fetching UID %s in %s: %s', uid, folder, e)
                result['errors'] += 1

        # Save synced UIDs
        uid_file.write_text(json.dumps(sorted(synced_uids)))

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
        username=account_config['email'],
        password=account_config.get('password', ''),
        local_dir=local_dir,
        auth_method=account_config.get('auth_method', 'password'),
        oauth2_token=account_config.get('oauth2_token'),
    )

    return syncer.sync()
