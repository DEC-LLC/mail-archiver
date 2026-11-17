"""1.3.1: reading the server's folder list (imap_sync.parse_list_line).

Found with the Windows EXE against a Dovecot server: folder names sent as
bare atoms were all read as the hierarchy delimiter ".", every SELECT was
refused, and the sync was still recorded as clean with nothing new.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import imap_sync                      # noqa: E402
from imap_sync import parse_list_line  # noqa: E402


def test_bare_names_as_dovecot_sends_them():
    assert parse_list_line(b'(\\HasNoChildren) "." INBOX') == 'INBOX'
    assert parse_list_line(b'(\\HasNoChildren \\Junk) "." Junk') == 'Junk'
    assert parse_list_line(b'(\\HasChildren) "." Lists.dev') == 'Lists.dev'


def test_quoted_names_as_gmail_sends_them():
    assert parse_list_line(b'(\\HasNoChildren) "/" "INBOX"') == 'INBOX'
    assert parse_list_line(b'(\\All \\HasNoChildren) "/" "[Gmail]/All Mail"') == '[Gmail]/All Mail'
    assert parse_list_line(b'(\\HasNoChildren) "/" "He said \\"hi\\""') == 'He said "hi"'


def test_nil_delimiter_and_literal_name():
    assert parse_list_line(b'(\\HasNoChildren) NIL INBOX') == 'INBOX'
    assert parse_list_line((b'(\\HasNoChildren) "/" {10}', b'Odd "name"')) == 'Odd "name"'


def test_containers_and_junk_lines_are_left_out():
    assert parse_list_line(b'(\\Noselect \\HasChildren) "/" "[Gmail]"') is None
    assert parse_list_line(b'garbage') is None
    assert parse_list_line(None) is None


class _Conn:
    """Stand-in connection: three folders, none of which can be opened."""

    def __init__(self, openable):
        self.openable = openable

    def list(self):
        return 'OK', [b'(\\HasNoChildren) "." INBOX', b'(\\HasNoChildren) "." Junk']

    def select(self, name, readonly=True):
        if self.openable:
            return 'OK', [b'0']
        return 'NO', [b'[CANNOT] Invalid mailbox name']

    def logout(self):
        return 'BYE', []


def _syncer(tmp_path, conn):
    s = imap_sync.ImapSyncer.__new__(imap_sync.ImapSyncer)
    s.host, s.port, s.use_tls = 'imap.example.org', 993, True
    s.username, s.password, s.auth_method, s.oauth2_token = 'u', 'p', 'password', None
    s.local_dir, s.folders, s.max_messages = tmp_path / 'acct', None, 0
    s.chown_uid = s.chown_gid = None
    s.connect = lambda: conn
    return s


def test_folders_are_asked_for_by_their_names(tmp_path):
    assert _syncer(tmp_path, _Conn(True)).list_folders(_Conn(True)) == ['INBOX', 'Junk']


def test_a_server_whose_folders_all_refuse_is_an_error_not_a_clean_sync(tmp_path):
    result = _syncer(tmp_path, _Conn(False)).sync()
    assert not result.get('success')
    assert 'no folder could be opened' in result.get('error', '')
    assert result['total_errors'] == 2


def test_empty_folders_are_a_clean_sync(tmp_path):
    result = _syncer(tmp_path, _Conn(True)).sync()
    assert result.get('success') and not result.get('error')
