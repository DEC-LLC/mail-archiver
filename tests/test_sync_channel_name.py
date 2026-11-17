"""The scheduled sync must ask isync for the channel generate_mbsyncrc wrote.

Until 1.3.1 run_sync stripped every character outside [A-Za-z0-9_] from the
channel name while generate_mbsyncrc kept them, so an address such as
mail-archiver-test@example.org could never be synced by the scheduled sync
("No channel or group named ... defined")."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault('MAIL_ARCHIVER_DATA', tempfile.mkdtemp(prefix='ma-test-data-'))
os.environ['MAIL_ARCHIVER_AUTH'] = 'builtin'
os.environ.setdefault('MAIL_ARCHIVER_SECRET', 'test-only-secret')
os.environ.pop('MAIL_ARCHIVER_SECRET_FILE', None)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as ma                    # noqa: E402
import bulk_sync_routes as routes   # noqa: E402

USER = 'channeluser'
ADDRESSES = ['mail-archiver-test@example.org', 'first.last+tag@example.org', 'plain@example.org']


class ChannelNameTests(unittest.TestCase):
    def setUp(self):
        shutil.rmtree(os.path.join(ma.CONFIG['data_dir'], USER), ignore_errors=True)
        self.addCleanup(shutil.rmtree, os.path.join(ma.CONFIG['data_dir'], USER), True)
        cfg = ma.get_user_config_dir(USER)
        (cfg / 'accounts.json').write_text(json.dumps(
            [{'email': e, 'provider': 'custom', 'host': 'imap.example.org', 'port': 993,
              'tls_mode': 'ssl', 'enabled': True, 'auth_type': 'app_password'} for e in ADDRESSES]))
        ma.generate_mbsyncrc(USER)
        self.channels = [l.split(None, 1)[1] for l in routes.mbsyncrc_path(USER).read_text().splitlines()
                         if l.startswith('Channel ')]

    def test_every_account_has_its_own_channel(self):
        self.assertEqual(len(self.channels), len(ADDRESSES))
        self.assertEqual(len(set(self.channels)), len(ADDRESSES))
        self.assertIn('mail-archiver-test_at_example_org', self.channels)

    def test_run_sync_asks_isync_for_the_generated_channel(self):
        for email in ADDRESSES:
            calls = []

            def fake_run(argv, *a, **k):
                calls.append(list(argv))
                return subprocess.CompletedProcess(argv, 0, stdout='', stderr='')

            with patch.object(ma, '_has_mbsync', lambda: True), \
                 patch.object(ma.subprocess, 'run', fake_run), \
                 patch.object(ma, 'generate_mbsyncrc', lambda u: None):
                ma.run_sync(USER, email)
            mbsync = [c for c in calls if c and os.path.basename(str(c[0])) == 'mbsync']
            self.assertTrue(mbsync, f'no mbsync call recorded for {email}: {calls}')
            self.assertIn(mbsync[-1][-1], self.channels,
                          f'{email}: isync was asked for {mbsync[-1][-1]!r}, the config has {self.channels}')


    def test_no_sync_path_strips_characters_from_the_channel_name(self):
        """Both sync paths (scheduled run_sync and the WebUI background
        job) must build the channel with the generator's name function."""
        src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'app.py')).read()
        self.assertNotIn("re.sub(r'[^a-zA-Z0-9_]', '', safe_name)", src)
        self.assertEqual(src.count('mbsync_arg = _bulk.safe_account_name(account_email)'), 2)


if __name__ == '__main__':
    unittest.main()
