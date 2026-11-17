"""1.3.2: the built-in IMAP client speaks STARTTLS and never logs in over an
unencrypted connection.

Up to 1.3.1 an account whose provider or setting said "no TLS" was opened
with imaplib.IMAP4 and the password sent in the clear; STARTTLS did not
exist on this path (mail_batcher.py had it, imap_sync.py did not).
"""

import ssl
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import imap_sync            # noqa: E402


class FakeConn:
    """Records the order of what the client says."""
    log = []

    def __init__(self, host, port, **kw):
        type(self).log.append(('connect', type(self).__name__, host, port, kw))

    def starttls(self, ssl_context=None):
        type(self).log.append(('starttls', ssl_context))
        return 'OK', []

    def login(self, user, password):
        type(self).log.append(('login', user))
        return 'OK', []

    def authenticate(self, mech, cb):
        type(self).log.append(('authenticate', mech))
        return 'OK', []

    def shutdown(self):
        type(self).log.append(('shutdown',))


class FakePlain(FakeConn):
    pass


class FakeSSL(FakeConn):
    pass


def _syncer(tmp_path, **kw):
    args = dict(host='imap.example.org', port=143, use_tls=True,
                username='u@example.org', password='secret',
                local_dir=tmp_path / 'acct')
    args.update(kw)
    return imap_sync.ImapSyncer(**args)


@pytest.fixture(autouse=True)
def _fakes():
    FakeConn.log = []
    with patch.object(imap_sync.imaplib, 'IMAP4', FakePlain), \
            patch.object(imap_sync.imaplib, 'IMAP4_SSL', FakeSSL):
        yield


def _verifies(ctx):
    return (isinstance(ctx, ssl.SSLContext) and ctx.verify_mode == ssl.CERT_REQUIRED
            and ctx.check_hostname is True)


def test_starttls_sync_upgrades_before_login(tmp_path):
    _syncer(tmp_path, tls_mode='starttls').connect()
    kinds = [e[0] for e in FakeConn.log]
    assert kinds == ['connect', 'starttls', 'login']
    assert FakeConn.log[0][1] == 'FakePlain'          # port 143, not IMAPS
    assert _verifies(FakeConn.log[1][1])              # chain + host name checked


def test_starttls_also_precedes_an_oauth_login(tmp_path):
    _syncer(tmp_path, tls_mode='starttls', auth_method='oauth2',
            oauth2_token='tok').connect()
    assert [e[0] for e in FakeConn.log] == ['connect', 'starttls', 'authenticate']


def test_refused_login_over_an_unencrypted_connection(tmp_path):
    with pytest.raises(imap_sync.PlaintextLoginRefused) as e:
        _syncer(tmp_path, tls_mode='none').connect()
    assert 'unencrypted' in str(e.value)
    assert FakeConn.log == []                         # not even a connection


def test_refused_when_only_the_old_flag_says_no_tls(tmp_path):
    # A provider entry with tls False and no per-account setting.
    with pytest.raises(imap_sync.PlaintextLoginRefused):
        _syncer(tmp_path, use_tls=False).connect()
    assert not any(e[0] in ('login', 'authenticate') for e in FakeConn.log)


def test_no_login_when_the_server_has_no_starttls(tmp_path):
    def no_starttls(self, ssl_context=None):
        raise RuntimeError('STARTTLS extension not supported by server')
    with patch.object(FakePlain, 'starttls', no_starttls):
        with pytest.raises(RuntimeError):
            _syncer(tmp_path, tls_mode='starttls').connect()
    assert not any(e[0] in ('login', 'authenticate') for e in FakeConn.log)
    assert ('shutdown',) in FakeConn.log


def test_no_login_when_the_starttls_certificate_does_not_verify(tmp_path):
    def bad_cert(self, ssl_context=None):
        raise ssl.SSLCertVerificationError('certificate verify failed')
    with patch.object(FakePlain, 'starttls', bad_cert):
        with pytest.raises(ssl.SSLCertVerificationError):
            _syncer(tmp_path, tls_mode='starttls').connect()
    assert not any(e[0] in ('login', 'authenticate') for e in FakeConn.log)


def test_sync_reports_the_refusal_as_a_failed_sync(tmp_path):
    result = _syncer(tmp_path, tls_mode='none').sync()
    assert result.get('success') is False
    assert 'unencrypted' in (result.get('error') or '')


def test_ssl_accounts_are_unchanged(tmp_path):
    _syncer(tmp_path, port=993).connect()                       # no tls_mode: follows use_tls
    _syncer(tmp_path, port=993, tls_mode='ssl').connect()
    connects = [e for e in FakeConn.log if e[0] == 'connect']
    assert [c[1] for c in connects] == ['FakeSSL', 'FakeSSL']
    assert all(_verifies(c[4].get('ssl_context')) for c in connects)
    assert not any(e[0] == 'starttls' for e in FakeConn.log)


@pytest.mark.parametrize('given,flag,want', [
    ('ssl', True, 'ssl'), ('STARTTLS', True, 'starttls'), ('none', True, 'none'),
    (None, True, 'ssl'), ('', False, 'none'), ('bogus', True, 'ssl'),
])
def test_tls_mode_normalisation(given, flag, want):
    assert imap_sync.normalise_tls_mode(given, flag) == want


def test_sync_account_passes_the_accounts_tls_mode(tmp_path):
    seen = {}

    class Spy(imap_sync.ImapSyncer):
        def sync(self):
            seen['mode'] = self.tls_mode
            return {'success': True}

    with patch.object(imap_sync, 'ImapSyncer', Spy):
        imap_sync.sync_account({'email': 'u@example.org', 'host': 'h', 'port': 143,
                                'tls_mode': 'starttls', 'password': 'p'}, tmp_path)
    assert seen['mode'] == 'starttls'


def test_the_app_hands_the_accounts_tls_mode_to_the_client():
    src = (ROOT / 'app.py').read_text(encoding='utf-8')
    assert "tls_mode=acct.get('tls_mode')," in src
    assert "refuses it" in src                      # the account form says so


def test_build_script_has_the_signing_step():
    script = ROOT / 'windows-app' / 'build.ps1'
    if not script.is_file():
        pytest.skip('windows-app/ is not part of this tree (Linux package test run)')
    ps1 = script.read_text(encoding='utf-8')
    for needle in ('$SignThumbprint', '$SignTest', 'Set-AuthenticodeSignature',
                   "HashAlgorithm = 'SHA256'", 'TESTSIGNED', "-ne 'Valid'"):
        assert needle in ps1, needle


def test_reconcile_and_measurement_connections_refuse_plaintext_too():
    import mail_batcher
    made = []

    class Conn:
        def __init__(self, host, port, **kw):
            made.append((host, port))

        def starttls(self, **kw):
            made.append('starttls')

    with patch.object(mail_batcher.imaplib, 'IMAP4', Conn), \
            patch.object(mail_batcher.imaplib, 'IMAP4_SSL', Conn):
        with pytest.raises(mail_batcher.BatcherError) as e:
            mail_batcher._connect_imap('imap.example.org', 143, 'none')
        assert 'unencrypted' in str(e.value)
        assert made == []
        mail_batcher._connect_imap('imap.example.org', 143, 'starttls')
    assert made == [('imap.example.org', 143), 'starttls']
