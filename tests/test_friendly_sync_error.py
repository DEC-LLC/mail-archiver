"""friendly_sync_error (1.1.9): classify the line that failed, not
the progress chatter around it.

Background: before 1.1.9 every failure on every provider was
shown as "TLS/SSL error ... certificate may have changed", because isync 1.5
prints "notice: SSLType is deprecated" on every run; and a Gmail quota stop
was shown as "Opening far side box Junk... Maildir notice: no UIDVALIDITY".
"""

import base64
import os
import sys
import tempfile
import unittest

os.environ.setdefault('MAIL_ARCHIVER_DATA', tempfile.mkdtemp(prefix='ma-test-data-'))
os.environ.setdefault('MAIL_ARCHIVER_AUTH', 'builtin')
os.environ.setdefault('MAIL_ARCHIVER_SECRET', 'test-only-secret')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as ma  # noqa: E402

NOTICE = '/home/alice/.mbsyncrc: notice: SSLType is deprecated. Use TLSType instead.\n'

GMAIL_QUOTA = NOTICE + """Opening far side box Junk...
Opening near side box Junk...
Creating near side box Junk...
Maildir notice: no UIDVALIDITY in /srv/mail-archive/alice/alice_at_gmail_com/Junk, creating new.
Loading far side box...
near side: 0 messages, 0 recent
far side: 45467 messages, 0 recent
Synchronizing...
IMAP error: unexpected BYE response: [OVERQUOTA] Account exceeded command or bandwidth limits.
Channels: 1    Boxes: 4    Far: +0 *0 #0 -0    Near: +18458 *0 #0 -0"""

OUTLOOK_NO_TOKEN = NOTICE + """[SASL-XOAUTH2] - Requesting authID!
[SASL-XOAUTH2] - Requesting token!
[SASL-XOAUTH2] - filling prompts!
cat: /srv/mail-archive/alice/.config/alice_example_at_outlook_com.token: No such file or directory
Skipping account alice_example_at_outlook_com, PassCmd exited with status 1"""

ICLOUD_AUTH = NOTICE + """IMAP command 'LOGIN <user> <pass>' returned an error: NO [AUTHENTICATIONFAILED] Authentication failed.
Channels: 1    Boxes: 0    Far: +0 *0 #0 -0    Near: +0 *0 #0 -0"""

PERMISSION = NOTICE + """Opening near side box Outbox...
Maildir error: cannot open /srv/mail-archive/dave/dave_example_at_gmail_com/Outbox/cur: Permission denied
Error: channel dave_example_at_gmail_com: near side box Outbox cannot be opened."""

LOCKED = NOTICE + """Error: channel :x-remote:[Gmail]/Starred-:x-local:[Gmail]/Starred is locked"""

# What isync prints for XOAUTH2: base64 of "user=<address>^Aauth=Bearer <token>".
# Built here from a made-up address, so no real one sits in the file encoded.
FAKE_BLOB = base64.b64encode(b'user=alice.example@hotmail.com\x01auth=Bearer ').decode() * 30
MS_NOT_CONNECTED = NOTICE + """Logging in...
[SASL-XOAUTH2] - Requesting token!
Authenticating with SASL mechanism XOAUTH2...
IMAP command 'AUTHENTICATE XOAUTH2 """ + FAKE_BLOB + """' returned an error: User is authenticated but not connected."""

REAL_TLS = NOTICE + """SSL error connecting imap.example.com (192.0.2.1:993): error:0A000086:SSL routines::certificate verify failed"""


class FriendlySyncErrorTests(unittest.TestCase):
    def f(self, raw, email):
        return ma.friendly_sync_error(raw, email)

    def test_gmail_quota_says_quota_not_folder_noise(self):
        msg = self.f(GMAIL_QUOTA, 'alice@gmail.com')
        self.assertIn('daily IMAP download limit', msg)
        self.assertIn('18,458', msg)
        self.assertNotIn('TLS', msg)
        self.assertNotIn('UIDVALIDITY', msg)

    def test_outlook_without_signin_says_sign_in(self):
        msg = self.f(OUTLOOK_NO_TOKEN, 'alice.example@outlook.com')
        self.assertIn('Sign in with Microsoft', msg)
        self.assertNotIn('TLS', msg)

    def test_auth_failure(self):
        msg = self.f(ICLOUD_AUTH, 'alice.example@icloud.com')
        self.assertIn('rejected the login', msg)
        self.assertIn('appleid.apple.com', msg)

    def test_permission_problem_is_named(self):
        msg = self.f(PERMISSION, 'dave.example@gmail.com')
        self.assertIn('file-permission problem', msg)
        self.assertNotIn('TLS', msg)

    def test_overlapping_sync(self):
        self.assertIn('Another sync', self.f(LOCKED, 'x@gmail.com'))

    def test_real_tls_failure_still_reported_as_tls(self):
        self.assertIn('TLS', self.f(REAL_TLS, 'x@example.com'))

    def test_deprecation_notice_alone_is_not_an_error(self):
        msg = self.f(NOTICE + 'Channels: 1 Boxes: 3 Far: +0 *0 #0 -0 Near: +0 *0 #0 -0', 'x@y')
        self.assertNotIn('TLS', msg)

    def test_oauth_token_never_reaches_the_message(self):
        # 1.1.10 showed the echoed AUTHENTICATE XOAUTH2
        # blob (user + Bearer access token) on the dashboard.
        msg = self.f(MS_NOT_CONNECTED, 'alice.example@hotmail.com')
        self.assertNotIn(FAKE_BLOB[:40], msg)
        self.assertIn('belongs to a different mailbox', msg)
        self.assertIn('alice.example@hotmail.com', msg)

    def test_redact_sync_output(self):
        out = ma.redact_sync_output(MS_NOT_CONNECTED)
        self.assertNotIn(FAKE_BLOB[:40], out)
        self.assertIn('AUTHENTICATE XOAUTH2 <credential redacted>', out)
        self.assertEqual(ma.redact_sync_output('LOGIN "u@x" "secret"'), 'LOGIN <user> <pass>')
        self.assertEqual(ma.redact_sync_output("LOGIN <user> <pass>"), "LOGIN <user> <pass>")

    def test_killed_sync_says_stopped(self):
        # A run stopped with SIGTERM (exit -15); the
        # only output was the deprecation notice.
        msg = ma.friendly_sync_error(NOTICE, 'bob@gmail.com', -15)
        self.assertIn('stopped before it finished (signal 15)', msg)

    def test_unknown_error_shows_the_error_line_not_progress(self):
        raw = NOTICE + 'Opening far side box INBOX...\nError: something odd happened'
        msg = self.f(raw, 'x@y')
        self.assertIn('something odd happened', msg)
        self.assertNotIn('Opening', msg)


if __name__ == '__main__':
    unittest.main()
