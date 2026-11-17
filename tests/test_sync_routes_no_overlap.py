"""Route-level tests for the 1.1.7 no-overlapping-syncs guard: repeated
"Sync Now" clicks get 202 then 409 (nothing queued), and the hourly cron
skips an account whose sync is still in flight.
"""

import io
import json
import os
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

_DATA = tempfile.mkdtemp(prefix='ma-test-data-')
os.environ['MAIL_ARCHIVER_DATA'] = _DATA
os.environ['MAIL_ARCHIVER_AUTH'] = 'builtin'
os.environ['MAIL_ARCHIVER_SECRET'] = 'test-only-secret'
os.environ.pop('MAIL_ARCHIVER_SECRET_FILE', None)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as ma          # noqa: E402
import sync_lifecycle as slc  # noqa: E402

USER = 'alice'
EMAIL = 'alice.example@icloud.com'
JSON_HDRS = {'Accept': 'application/json', 'X-Requested-With': 'XMLHttpRequest'}


class RouteGuardTests(unittest.TestCase):
    def setUp(self):
        self._jobs = tempfile.TemporaryDirectory()
        self._orig = slc.JOB_STATE_DIR
        slc.JOB_STATE_DIR = self._jobs.name
        cfg = ma.get_user_config_dir(USER)
        (cfg / 'accounts.json').write_text(json.dumps([
            {'email': EMAIL, 'provider': 'icloud', 'enabled': True,
             'sync_interval': 'hourly'}]))
        (cfg / 'sync_status.json').write_text('{}')
        ma.app.config['TESTING'] = True
        self.client = ma.app.test_client()
        with self.client.session_transaction() as s:
            s['username'] = USER
            s['login_time'] = time.time()
        # The background runner would spawn mbsync; keep the job in flight
        # (owner = this live test process) without running anything.
        self._bg = patch.object(ma, '_run_sync_background', lambda *a, **k: None)
        self._bg.start()

    def tearDown(self):
        self._bg.stop()
        slc.JOB_STATE_DIR = self._orig
        self._jobs.cleanup()

    def _jobs_on_disk(self):
        return [n for n in os.listdir(self._jobs.name) if n.endswith('.json')]

    def test_repeated_clicks_202_then_409_nothing_queued(self):
        r1 = self.client.post(f'/sync/{EMAIL}', headers=JSON_HDRS)
        self.assertEqual(r1.status_code, 202)
        first = r1.get_json()['job_id']
        for _ in range(5):
            r = self.client.post(f'/sync/{EMAIL}', headers=JSON_HDRS)
            self.assertEqual(r.status_code, 409)
            body = r.get_json()
            self.assertEqual(body['error'], 'already_syncing')
            self.assertEqual(body['job_id'], first)
            self.assertIn('nothing was queued', body['message'])
        self.assertEqual(len(self._jobs_on_disk()), 1)

    def test_sync_all_refused_while_account_syncs(self):
        self.assertEqual(
            self.client.post(f'/sync/{EMAIL}', headers=JSON_HDRS).status_code, 202)
        r = self.client.post('/sync', headers=JSON_HDRS)
        self.assertEqual(r.status_code, 409)

    def test_cron_skips_account_already_in_flight(self):
        r = self.client.post(f'/sync/{EMAIL}', headers=JSON_HDRS)
        self.assertEqual(r.status_code, 202)
        out = io.StringIO()
        with patch.object(ma, 'run_sync') as rs, redirect_stdout(out):
            ma.scheduled_sync()
        rs.assert_not_called()
        self.assertIn(f'Skipping {EMAIL}', out.getvalue())

    def test_cron_registers_its_run_and_finishes_it(self):
        seen = {}

        def fake_run_sync(user, email):
            # While cron runs, a WebUI click must be refused.
            seen['click'] = self.client.post(
                f'/sync/{EMAIL}', headers=JSON_HDRS).status_code
            return {'state': 'ok', 'exit_code': 0}

        with patch.object(ma, 'run_sync', side_effect=fake_run_sync), \
                redirect_stdout(io.StringIO()):
            ma.scheduled_sync()
        self.assertEqual(seen['click'], 409)
        states = [slc.read_state(n[:-5]) for n in self._jobs_on_disk()]
        self.assertEqual([s['state'] for s in states], ['done'])
        self.assertEqual(states[0]['source'], 'cron')

    def _stream_events(self, job_id, limit=3):
        r = self.client.get(f'/api/sync/{job_id}/stream')
        out = []
        for chunk in r.response:
            chunk = chunk.decode() if isinstance(chunk, bytes) else chunk
            out.append(chunk.split('\n', 1)[0])
            if len(out) >= limit or 'event: error' in chunk or 'event: done' in chunk:
                break
        r.close()
        return out

    def test_stream_says_what_is_running_immediately(self):
        # A scheduled sync writes no progress, so the
        # panel sat on "Reconnecting to in-flight sync..." for the whole run.
        job_id, _ = ma._new_sync_job(USER, EMAIL, source='cron')
        ev = self._stream_events(job_id, limit=1)
        self.assertEqual(ev, ['event: state'])

    def test_stream_ends_for_a_job_whose_worker_died(self):
        job_id, _ = ma._new_sync_job(USER, EMAIL)
        slc.update_state(job_id, owner_pid=2**22 + 11, state='running')
        ev = self._stream_events(job_id, limit=5)
        self.assertEqual(ev[0], 'event: state')
        self.assertIn('event: error', ev)

    def test_status_endpoint_reaps_a_dead_job(self):
        job_id, _ = ma._new_sync_job(USER, EMAIL)
        slc.update_state(job_id, owner_pid=2**22 + 13, state='running')
        j = self.client.get(f'/api/sync/{job_id}', headers=JSON_HDRS).get_json()
        self.assertEqual(j['state'], 'error')

    def test_dashboard_renders_buttons_disabled_while_syncing(self):
        self.client.post(f'/sync/{EMAIL}', headers=JSON_HDRS)
        html = self.client.get('/dashboard').get_data(as_text=True)
        self.assertIn('A sync is running.', html)
        self.assertIn('Syncing…', html)


class MbsyncrcPullOnlyTests(unittest.TestCase):
    """1.1.7: the generated .mbsyncrc must never push to the provider."""

    def test_every_channel_is_pull_only(self):
        user = 'pulltest'
        cfg = ma.get_user_config_dir(user)
        (cfg / 'accounts.json').write_text(json.dumps([
            {'email': 'a@gmail.com', 'provider': 'gmail', 'enabled': True},
            {'email': 'b@icloud.com', 'provider': 'icloud', 'enabled': True,
             'folder_pattern': '* "![Gmail]/All Mail"'}]))
        ma.generate_mbsyncrc(user)
        rc = (cfg.parent / '.mbsyncrc').read_text()
        blocks = [b for b in rc.split('\n\n') if b.startswith('Channel ')]
        self.assertEqual(len(blocks), 2)
        for b in blocks:
            lines = b.splitlines()
            self.assertIn('Sync Pull', lines, b)
            self.assertFalse([l for l in lines
                              if l.startswith('Sync ') and l != 'Sync Pull'], b)
        # A per-account folder exclusion survives regeneration.
        self.assertIn('Patterns * "![Gmail]/All Mail"', rc)


if __name__ == '__main__':
    unittest.main()
