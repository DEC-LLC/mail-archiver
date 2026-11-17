"""1.2.0 schedule: a fixed hour per account, one lane per IMAP server.

Background: up to 1.1.x every daily account started an
hour later each day (due 24 h after it FINISHED, checked on the hour), and
all accounts ran one after another. The rule since 1.2.0: accounts on one
server (all Gmail, all iCloud, all Microsoft, one custom host) must never
run together; one of each may; give each download an hour; use the whole
24 hours.
"""

import io
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

_REAL_SLEEP = time.sleep
os.environ['TZ'] = 'UTC'
if hasattr(time, 'tzset'):
    time.tzset()
_DATA = tempfile.mkdtemp(prefix='ma-test-data-')
os.environ['MAIL_ARCHIVER_DATA'] = _DATA
os.environ['MAIL_ARCHIVER_AUTH'] = 'builtin'
os.environ['MAIL_ARCHIVER_SECRET'] = 'test-only-secret'
os.environ.pop('MAIL_ARCHIVER_SECRET_FILE', None)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as ma              # noqa: E402
import sync_lifecycle as slc  # noqa: E402

DAY = 86400
T0 = 1793836800  # 2026-11-05 00:00:00 UTC


def at(day, hour, minute=0):
    return T0 + day * DAY + hour * 3600 + minute * 60


class SlotTests(unittest.TestCase):
    def test_same_server_spread_over_the_day(self):
        gmail = [('u', f'g{i}@gmail.com', 'imap.gmail.com') for i in range(4)]
        ms = [('u', f'm{i}@hotmail.com', 'outlook.office.com') for i in range(3)]
        slots = slc.assign_slots(gmail + ms + [('u', 'a@icloud.com', 'imap.mail.me.com')])
        self.assertEqual(sorted(slots[(u, e)] for u, e, _ in gmail), [0, 6, 12, 18])
        self.assertEqual(sorted(slots[(u, e)] for u, e, _ in ms), [0, 8, 16])
        self.assertEqual(slots[('u', 'a@icloud.com')], 0)

    def test_slots_do_not_depend_on_list_order(self):
        e = [('u', f'g{i}@gmail.com', 'imap.gmail.com') for i in range(5)]
        self.assertEqual(slc.assign_slots(e), slc.assign_slots(list(reversed(e))))

    def test_every_download_gets_at_least_an_hour(self):
        e = [('u', f'g{i}@gmail.com', 'imap.gmail.com') for i in range(24)]
        self.assertEqual(sorted(slc.assign_slots(e).values()), list(range(24)))
        self.assertEqual(slc.lane_capacity(24), 1)
        self.assertEqual(slc.lane_capacity(25), 2)

    def test_start_hour(self):
        e = [('u', f'g{i}@gmail.com', 'imap.gmail.com') for i in range(2)]
        self.assertEqual(sorted(slc.assign_slots(e, start_hour=1).values()), [1, 13])

    def test_server_key(self):
        self.assertEqual(slc.server_key('IMAP.Gmail.com '), 'imap.gmail.com')
        self.assertEqual(slc.server_key('', 'custom'), 'provider:custom')


class DueTests(unittest.TestCase):
    def test_due_once_per_slot(self):
        self.assertTrue(slc.is_due(at(1, 6), 6, DAY, at(0, 6, 1)))
        self.assertFalse(slc.is_due(at(1, 6, 30), 6, DAY, at(1, 6, 1)))
        self.assertFalse(slc.is_due(at(1, 5), 6, DAY, at(0, 6, 1)))

    def test_missed_slot_runs_at_next_hour(self):
        self.assertTrue(slc.is_due(at(1, 9), 6, DAY, at(0, 6, 1)))

    def test_never_synced_is_due(self):
        self.assertTrue(slc.is_due(at(0, 3), 6, DAY, None))

    def test_moved_slot_does_not_run_twice(self):
        # ran 04:00 on slot 4; a new account on the server moves it to 5
        self.assertFalse(slc.is_due(at(0, 5), 5, DAY, at(0, 4, 1)))

    def test_sub_daily_intervals_repeat_from_the_slot(self):
        self.assertTrue(slc.is_due(at(0, 9), 3, 6 * 3600, at(0, 3, 1)))
        self.assertFalse(slc.is_due(at(0, 8), 3, 6 * 3600, at(0, 3, 1)))
        self.assertTrue(slc.is_due(at(0, 8), 3, 3600, at(0, 7, 1)))

    def test_no_drift_when_a_sync_runs_long(self):
        # Hourly cron; each sync takes 70 minutes. Before 1.2.0 the start
        # hour moved one hour later every day.
        last, starts = at(-1, 6), []
        for h in range(5 * 24):
            now = T0 + h * 3600 + 2
            if slc.is_due(now, 6, DAY, last):
                last = now  # a 70-minute sync: due is measured from its start
                starts.append(time.strftime('%d %H:%M', time.gmtime(now)))
        self.assertEqual(starts, [f'{5 + d:02d} 06:00' for d in range(5)])


class ScheduledRunTests(unittest.TestCase):
    USER = 'dave'

    def setUp(self):
        self._jobs = tempfile.TemporaryDirectory()
        self._orig = slc.JOB_STATE_DIR
        slc.JOB_STATE_DIR = self._jobs.name
        # a data dir of its own: other test modules leave users behind
        self._data = tempfile.TemporaryDirectory()
        self._cfgp = patch.dict(ma.CONFIG, {'data_dir': self._data.name})
        self._cfgp.start()
        self.cfg = ma.get_user_config_dir(self.USER)
        (self.cfg / 'accounts.json').write_text(json.dumps([
            {'email': 'a@gmail.com', 'provider': 'gmail', 'enabled': True},
            {'email': 'b@gmail.com', 'provider': 'gmail', 'enabled': True},
            {'email': 'c@icloud.com', 'provider': 'icloud', 'enabled': True},
            {'email': 'd@hotmail.com', 'provider': 'hotmail', 'enabled': True},
            {'email': 'e@example.org', 'provider': 'custom', 'host': 'mail.example.org',
             'enabled': True},
            {'email': 'f@gmail.com', 'provider': 'gmail', 'enabled': True,
             'sync_interval': 'manual'},
        ]))
        (self.cfg / 'sync_status.json').write_text('{}')
        self._sleep = patch.object(ma.time, 'sleep', lambda s: None)
        self._sleep.start()

    def tearDown(self):
        self._sleep.stop()
        self._cfgp.stop()
        self._data.cleanup()
        slc.JOB_STATE_DIR = self._orig
        self._jobs.cleanup()

    def _run(self, fake):
        out = io.StringIO()
        with patch.object(ma, 'run_sync', side_effect=fake), redirect_stdout(out):
            ma.scheduled_sync()
        return out.getvalue()

    def test_one_per_server_and_servers_in_parallel(self):
        calls, live, peak, lock = [], set(), [0], threading.Lock()
        gate = threading.Barrier(4, timeout=5)

        def fake(user, email):
            with lock:
                calls.append(email)
                live.add(email)
                peak[0] = max(peak[0], len(live))
            gate.wait()  # all four lanes must be running at once
            with lock:
                live.discard(email)
            return {'state': 'ok', 'exit_code': 0}

        self._run(fake)
        gmail = [e for e in calls if e.endswith('@gmail.com')]
        self.assertEqual(len(gmail), 1)  # never both Gmail accounts
        self.assertEqual(set(calls) - set(gmail),
                         {'c@icloud.com', 'd@hotmail.com', 'e@example.org'})
        self.assertEqual(peak[0], 4)
        self.assertNotIn('f@gmail.com', calls)  # manual: never scheduled

    def test_parallel_cap(self):
        live, peak, lock = set(), [0], threading.Lock()

        def fake(user, email):
            with lock:
                live.add(email)
                peak[0] = max(peak[0], len(live))
            # The real sleep (setUp replaces time.sleep): it lets go of the
            # interpreter lock, so the other lanes get to start. A busy-wait
            # here starved them (1 run in 5 on Linux, always on Windows).
            _REAL_SLEEP(0.2)
            with lock:
                live.discard(email)
            return {'state': 'ok', 'exit_code': 0}

        with patch.object(ma, 'MAX_PARALLEL_SYNCS', 2):
            self._run(fake)
        self.assertEqual(peak[0], 2)

    def test_busy_server_waits_for_the_next_hour(self):
        job_id, _ = ma._new_sync_job(self.USER, 'a@gmail.com', source='webui')
        calls = []
        out = self._run(lambda u, e: calls.append(e) or {'state': 'ok', 'exit_code': 0})
        self.assertFalse([e for e in calls if e.endswith('@gmail.com')])
        self.assertIn('imap.gmail.com is busy', out)
        self.assertIn('c@icloud.com', calls)

    def test_parallel_runs_keep_each_others_status(self):
        status_file = self.cfg / 'sync_status.json'
        # two runs each took a copy before either finished
        a = ma.get_sync_status(self.USER)
        b = ma.get_sync_status(self.USER)
        a['c@icloud.com'] = {'state': 'syncing', 'started': '2026-11-05 00:00:05'}
        ma._write_sync_status_locked(status_file, a, keys=['c@icloud.com'])
        b['d@hotmail.com'] = {'state': 'syncing', 'started': '2026-11-05 00:00:15'}
        ma._write_sync_status_locked(status_file, b, keys=['d@hotmail.com'])
        a['c@icloud.com'] = {'state': 'ok', 'finished': '2026-11-05 00:01:00'}
        ma._write_sync_status_locked(status_file, a, keys=['c@icloud.com'])
        s = json.loads(status_file.read_text())
        self.assertEqual(s['d@hotmail.com']['state'], 'syncing')
        self.assertEqual(s['c@icloud.com'], {'state': 'ok', 'finished': '2026-11-05 00:01:00',
                                             'started': '2026-11-05 00:00:05'})

    def test_plan_lists_slots(self):
        plan = ma.sync_schedule_plan(now=at(0, 0, 5))
        by = {r['email']: r for r in plan}
        self.assertEqual(sorted(by), ['a@gmail.com', 'b@gmail.com', 'c@icloud.com',
                                      'd@hotmail.com', 'e@example.org'])
        self.assertEqual({by['a@gmail.com']['slot'], by['b@gmail.com']['slot']}, {0, 12})
        self.assertEqual(by['e@example.org']['server'], 'mail.example.org')
        self.assertTrue(all(r['due'] for r in plan))  # never synced


if __name__ == '__main__':
    unittest.main()
