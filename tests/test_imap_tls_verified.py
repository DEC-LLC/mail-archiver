"""1.3.1: the built-in IMAP client verifies the server's certificate.

imaplib checks neither the chain nor the host name unless it is given a
context; up to 1.3.0 none was given (imap_sync.py, mail_batcher.py).
"""

import os
import ssl
import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import imap_sync            # noqa: E402
import mail_batcher         # noqa: E402
import platform_compat      # noqa: E402


def _verifies(ctx):
    return (isinstance(ctx, ssl.SSLContext) and ctx.verify_mode == ssl.CERT_REQUIRED
            and ctx.check_hostname is True)


def test_the_context_checks_chain_and_host_name():
    assert _verifies(platform_compat.tls_client_context())


def test_a_private_ca_file_is_added_when_named(tmp_path):
    missing = tmp_path / 'no-such.pem'
    with patch.dict(os.environ, {'MAIL_ARCHIVER_CA_FILE': str(missing)}):
        assert _verifies(platform_compat.tls_client_context())   # ignored, not fatal
    junk = tmp_path / 'junk.pem'
    junk.write_text('not a certificate', encoding='utf-8')
    with patch.dict(os.environ, {'MAIL_ARCHIVER_CA_FILE': str(junk)}):
        assert _verifies(platform_compat.tls_client_context())


def test_imap_sync_connects_with_a_verifying_context():
    seen = {}

    class Conn:
        def __init__(self, host, port, **kw):
            seen.update(kw, host=host)

        def login(self, user, password):
            return 'OK', []

    with patch.object(imap_sync.imaplib, 'IMAP4_SSL', Conn):
        syncer = imap_sync.ImapSyncer.__new__(imap_sync.ImapSyncer)
        syncer.host, syncer.port, syncer.use_tls = 'imap.example.org', 993, True
        syncer.auth_method, syncer.oauth2_token = 'password', None
        syncer.username, syncer.password = 'u', 'p'
        syncer.connect()
    assert seen['host'] == 'imap.example.org'
    assert _verifies(seen.get('ssl_context'))


def test_mail_batcher_connects_with_a_verifying_context():
    seen = {}

    class Conn:
        def __init__(self, host, port, **kw):
            seen.setdefault('ssl', kw)   # the first connection: IMAP4_SSL

        def starttls(self, **kw):
            seen['starttls'] = kw

    with patch.object(mail_batcher.imaplib, 'IMAP4_SSL', Conn), \
            patch.object(mail_batcher.imaplib, 'IMAP4', Conn):
        mail_batcher._connect_imap('imap.example.org', 993, 'ssl')
        mail_batcher._connect_imap('imap.example.org', 143, 'starttls')
    assert _verifies(seen['ssl'].get('ssl_context'))
    assert _verifies(seen['starttls'].get('ssl_context'))
