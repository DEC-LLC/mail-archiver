"""Tests for the 1.3.0 large-mailbox / rebase sync (bulk_sync.py and
bulk_sync_routes.py).

No mail server and no mbsync: a fake mbsync "delivers" files into a temp
Maildir step by step, and a fake IMAP connection records every command.

Model: a manual Gmail "All Mail" catch-up (budget of one provider day per
run, journal resume next day, completion = rc 0 + .mbsyncstate + no
journal, then un-exclude).
"""

import io
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

import pytest

_DATA = tempfile.mkdtemp(prefix='ma-test-data-')
os.environ.setdefault('MAIL_ARCHIVER_DATA', _DATA)
os.environ['MAIL_ARCHIVER_AUTH'] = 'builtin'
os.environ.setdefault('MAIL_ARCHIVER_SECRET', 'test-only-secret')
os.environ.pop('MAIL_ARCHIVER_SECRET_FILE', None)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as ma                    # noqa: E402
import bulk_sync as bulk            # noqa: E402
import bulk_sync_routes as routes   # noqa: E402
import sync_lifecycle as slc        # noqa: E402

USER = 'bulkuser'
OTHER = 'bulkother'
EMAIL = 'big.box@gmail.com'
EMAIL2 = 'second@gmail.com'
ALLMAIL = '[Gmail]/All Mail'
SECRET_PASS = 'hunter2-app-pass-ZZ'
SECRET_TOKEN = 'ya29.SECRETTOKENaaaaBBBBccccDDDD'
OVERQUOTA = ('IMAP error: unexpected BYE response: [OVERQUOTA] Account '
             'exceeded command or bandwidth limits.\n')
SOCKET_TIMEOUT = 'Socket error on imap.gmail.com (192.0.2.10:993): timeout.\n'
MSG = 100_000   # bytes per fake message


def local_epoch(day, hour, minute=0, second=0):
    return time.mktime((2026, 11, day, hour, minute, second, 0, 0, -1))


class FakeClock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class FakeHandle:
    def __init__(self, owner, steps):
        self.owner = owner
        self.steps = list(steps)
        self.pid = os.getpid()
        self.rc = None
        self.stop_requested = False
        self.killed = False
        self._out = ''

    def poll(self):
        if self.rc is not None:
            return self.rc
        if self.stop_requested:
            # SIGTERM: isync leaves its journal behind and exits.
            self.owner.touch_journal()
            self.rc = 143
            return self.rc
        step = self.steps.pop(0) if self.steps else ('exit', 0, '', 'complete')
        if step[0] == 'deliver':
            self.owner.deliver(step[1])
        elif step[0] == 'call':
            step[1]()
        elif step[0] == 'exit':
            _, rc, out, final = step
            self._out += out
            if final == 'complete':
                self.owner.finish_state()
            else:
                self.owner.touch_journal()
            self.rc = rc
        return self.rc

    def request_stop(self):
        self.stop_requested = True

    def kill(self):
        self.killed = True

    def output(self):
        return self._out


class FakeMbsync:
    """Stands in for mbsync: each start() takes the next plan (a list of
    steps) and plays one step per poll."""

    def __init__(self, folder_path, plans):
        self.folder_path = folder_path
        self.plans = list(plans)
        self.started = []
        self.handles = []
        self._n = 0

    def start(self, rc_path):
        self.started.append(rc_path)
        h = FakeHandle(self, self.plans.pop(0))
        self.handles.append(h)
        return h

    def deliver(self, count):
        new = os.path.join(self.folder_path, 'new')
        os.makedirs(new, exist_ok=True)
        os.makedirs(os.path.join(self.folder_path, 'cur'), exist_ok=True)
        self.touch_journal()
        for _ in range(count):
            self._n += 1
            with open(os.path.join(new, f'{self._n}.fake'), 'wb') as f:
                f.write(b'x' * MSG)

    def touch_journal(self):
        os.makedirs(self.folder_path, exist_ok=True)
        open(os.path.join(self.folder_path, '.mbsyncstate.journal'), 'a').close()

    def finish_state(self, max_uid=None):
        os.makedirs(self.folder_path, exist_ok=True)
        with open(os.path.join(self.folder_path, '.mbsyncstate'), 'w') as f:
            f.write(f'FarUidValidity 7\nNearUidValidity 1\n'
                    f'MaxPulledUid {self._n if max_uid is None else max_uid}\n\n')
        try:
            os.unlink(os.path.join(self.folder_path, '.mbsyncstate.journal'))
        except FileNotFoundError:
            pass


GOOD_RC = '\n'.join([
    'IMAPAccount acct', 'Host imap.example.org', 'User u@example.org',
    'PassCmd "/usr/libexec/mail-archiver-cred u u@example.org"', '',
    'IMAPStore acct-remote', 'Account acct', '',
    'MaildirStore acct-local', 'SubFolders Verbatim', 'Path /x/', 'Inbox /x/INBOX', '',
    'Channel acct', 'Far :acct-remote:', 'Near :acct-local:',
    'Patterns "[Gmail]/All Mail"', 'Sync Pull', 'Create Near', 'Expunge None',
    'SyncState *', ''])


class EngineBase(unittest.TestCase):
    """The engine alone: temp state dir, temp Maildir, everything faked."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='ma-bulk-')
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.cfg = os.path.join(self.tmp, 'config')
        self.maildir = os.path.join(self.tmp, 'maildir')
        os.makedirs(self.cfg)
        os.makedirs(self.maildir)
        self.folder_path = bulk.folder_maildir(self.maildir, ALLMAIL)
        self.store = bulk.JobStore(self.cfg, EMAIL)
        self.clock = FakeClock(local_epoch(2, 2))       # 02:00, inside the window
        self.claims = []
        self.closed = []
        self.rc_text = GOOD_RC
        self.log = []

    def open_job(self, budget=250_000, window=('01:00', '06:00'), pattern=None):
        job = bulk.new_job(EMAIL, [ALLMAIL], budget_bytes=budget,
                           window_start=window[0], window_end=window[1],
                           profile='gmail', original_pattern=pattern,
                           now=self.clock())
        bulk.start_job(self.store, job)
        return job

    def runner(self, mbsync, **kw):
        deps = dict(
            store=self.store, account_maildir=self.maildir,
            prepare_config=lambda folders: ('/nonexistent/rc', self.rc_text),
            start=mbsync.start,
            claim=lambda: ('token', None),
            release=lambda token, ok, msg: self.claims.append((token, ok, msg)),
            on_closed=lambda: self.closed.append(True),
            clock=self.clock, sleep=self.clock.sleep,
            pid_alive=lambda pid: False, log=self.log.append)
        deps.update(kw)
        return bulk.BulkJobRunner(**deps)

    def job(self):
        return self.store.load()['job']


class BudgetTests(EngineBase):
    def test_budget_stop_then_resume_next_day(self):
        self.open_job(budget=250_000)
        mb = FakeMbsync(self.folder_path, [
            [('deliver', 1), ('deliver', 1), ('deliver', 1), ('deliver', 1)],
            [('deliver', 1), ('exit', 0, '', 'complete')],
        ])
        r = self.runner(mb)

        res = r.run('cron')
        self.assertTrue(res['ran'])
        self.assertEqual(res['reason'], 'budget')
        self.assertTrue(mb.handles[0].stop_requested)       # SIGTERM path
        self.assertFalse(mb.handles[0].killed)
        job = self.job()
        self.assertEqual(job['status'], 'active')            # state carried
        self.assertEqual(len(job['runs']), 1)
        run = job['runs'][0]
        self.assertEqual((run['messages_before'], run['messages_after']), (0, 3))
        self.assertEqual(run['bytes'], 3 * MSG)
        self.assertEqual(run['day'], '2026-11-02')
        self.assertTrue(os.path.exists(self.folder_path + '/.mbsyncstate.journal'))
        self.assertEqual(bulk.excluded_folders(self.store.load()), [ALLMAIL])

        # Same day, an hour later: the budget is used up, nothing starts.
        self.clock.now = local_epoch(2, 3)
        self.assertEqual(r.run('cron'), {'ran': False, 'reason': 'budget'})
        self.assertEqual(len(mb.started), 1)

        # Next day: a new budget, and isync continues from its journal.
        self.clock.now = local_epoch(3, 2)
        res = r.run('cron')
        self.assertEqual(res['reason'], 'complete')
        job = self.job()
        self.assertEqual([x['day'] for x in job['runs']],
                         ['2026-11-02', '2026-11-03'])
        self.assertEqual(bulk.bytes_on_day(job, '2026-11-02'), 3 * MSG)
        self.assertEqual(bulk.bytes_on_day(job, '2026-11-03'), 1 * MSG)
        self.assertEqual(job['runs'][1]['messages_before'], 3)
        self.assertEqual(job['runs'][1]['messages_after'], 4)

    def test_budget_counts_all_runs_of_the_day(self):
        self.open_job(budget=350_000, window=('00:00', '00:00'))
        mb = FakeMbsync(self.folder_path, [
            [('deliver', 2), ('exit', 1, 'some other failure\n', 'journal')],
            [('deliver', 1), ('deliver', 1), ('deliver', 1)],
        ])
        r = self.runner(mb)
        self.assertEqual(r.run('manual')['reason'], 'error')
        # 200 kB already used today: the second run stops after 200 kB more.
        res = r.run('manual')
        self.assertEqual(res['reason'], 'budget')
        self.assertEqual(self.job()['runs'][1]['bytes'], 2 * MSG)

    def test_no_budget_means_no_budget_stop(self):
        self.open_job(budget=0)
        mb = FakeMbsync(self.folder_path, [
            [('deliver', 5), ('deliver', 5), ('exit', 0, '', 'complete')]])
        self.assertEqual(self.runner(mb).run('cron')['reason'], 'complete')

    def test_raised_budget_applies_to_the_run_in_flight(self):
        self.open_job(budget=150_000)
        raise_it = lambda: bulk.update_settings(
            self.store, budget_bytes=10_000_000, window_start='01:00', window_end='06:00')
        mb = FakeMbsync(self.folder_path, [
            [('call', raise_it), ('deliver', 3), ('exit', 0, '', 'complete')]])
        self.assertEqual(self.runner(mb).run('cron')['reason'], 'complete')

    def test_interrupted_run_is_counted_against_the_day(self):
        self.open_job(budget=250_000)
        mb = FakeMbsync(self.folder_path, [[('deliver', 1)]])
        mb.deliver(3)       # a runner that died had downloaded 3 messages

        def crashed(doc):
            doc['job']['running'] = {
                'pid': 2 ** 22 + 5, 'trigger': 'cron', 'day': '2026-11-02',
                'start': '2026-11-02 01:10:00', 'start_epoch': local_epoch(2, 1, 10),
                'messages_before': 0, 'bytes_before': 0}
        self.store.update(crashed)
        res = self.runner(mb).run('cron')
        self.assertEqual(res, {'ran': False, 'reason': 'budget'})
        run = self.job()['runs'][0]
        self.assertEqual((run['reason'], run['bytes']), ('interrupted', 3 * MSG))
        self.assertIsNone(self.job()['running'])


class WindowTests(EngineBase):
    def test_scheduled_run_only_inside_the_window(self):
        self.open_job()
        mb = FakeMbsync(self.folder_path, [[('exit', 0, '', 'complete')]])
        self.clock.now = local_epoch(2, 12)
        self.assertEqual(self.runner(mb).run('cron'),
                         {'ran': False, 'reason': 'window'})
        self.assertEqual(mb.started, [])

    def test_run_stops_when_the_window_closes(self):
        self.open_job(budget=0)
        self.clock.now = local_epoch(2, 5, 59, 50)
        mb = FakeMbsync(self.folder_path, [[('deliver', 1)] * 50])
        res = self.runner(mb).run('cron')
        self.assertEqual(res['reason'], 'window')
        self.assertTrue(mb.handles[0].stop_requested)
        self.assertLess(self.job()['runs'][0]['messages'], 5)
        # Still due later the same night? No: the window is closed.
        self.assertEqual(bulk.eligibility(self.job(), self.clock(), 'cron'),
                         (False, 'window'))
        # And due again the next night.
        self.assertEqual(bulk.eligibility(self.job(), local_epoch(3, 1, 30), 'cron'),
                         (True, 'ok'))

    def test_run_now_ignores_the_window_but_not_the_budget(self):
        self.open_job(budget=250_000)
        self.clock.now = local_epoch(2, 12)
        mb = FakeMbsync(self.folder_path, [[('deliver', 1)] * 9])
        res = self.runner(mb).run('manual')
        self.assertEqual(res['reason'], 'budget')
        self.assertEqual(self.runner(mb).run('manual'),
                         {'ran': False, 'reason': 'budget'})

    def test_window_helpers(self):
        self.assertTrue(bulk.in_window('01:00', '06:00', local_epoch(2, 1)))
        self.assertFalse(bulk.in_window('01:00', '06:00', local_epoch(2, 6)))
        self.assertTrue(bulk.in_window('22:00', '06:00', local_epoch(2, 23)))
        self.assertTrue(bulk.in_window('22:00', '06:00', local_epoch(2, 3)))
        self.assertFalse(bulk.in_window('22:00', '06:00', local_epoch(2, 12)))
        self.assertTrue(bulk.in_window('00:00', '00:00', local_epoch(2, 12)))
        self.assertEqual(bulk.window_close_epoch('22:00', '06:00', local_epoch(2, 23)),
                         local_epoch(3, 6))
        self.assertIsNone(bulk.window_close_epoch('04:00', '04:00', local_epoch(2, 23)))
        with self.assertRaises(bulk.BulkError):
            bulk.parse_hhmm('25:00')


class ProviderStopTests(EngineBase):
    def _stop_and_retry(self, output, expected):
        self.open_job(budget=0)
        mb = FakeMbsync(self.folder_path, [
            [('deliver', 2), ('exit', 1, output, 'journal')],
            [('deliver', 1), ('exit', 0, '', 'complete')],
        ])
        r = self.runner(mb)
        self.assertEqual(r.run('cron')['reason'], expected)
        job = self.job()
        self.assertEqual(job['runs'][0]['reason'], expected)
        self.assertEqual(job['runs'][0]['rc'], 1)
        self.assertEqual(job['last_stop']['reason'], expected)
        self.assertEqual(job['status'], 'active')
        # Not retried the same day (the provider limit is per day) ...
        self.clock.now = local_epoch(2, 4)
        self.assertEqual(r.run('cron'), {'ran': False, 'reason': 'retry-tomorrow'})
        self.assertEqual(len(mb.started), 1)
        # ... but the next day, from the journal.
        self.clock.now = local_epoch(3, 2)
        self.assertEqual(r.run('cron')['reason'], 'complete')
        self.assertEqual(self.job()['runs'][1]['messages_before'], 2)

    def test_overquota_recorded_and_retried_next_day(self):
        self._stop_and_retry(
            'Warning: lost track of 180225 pulled message(s)\n' + OVERQUOTA,
            'provider-cap')

    def test_timeout_recorded_and_retried_next_day(self):
        self._stop_and_retry(SOCKET_TIMEOUT, 'timeout')

    def test_classify_exit(self):
        self.assertEqual(bulk.classify_exit(1, OVERQUOTA, False)[0], 'provider-cap')
        self.assertEqual(bulk.classify_exit(1, SOCKET_TIMEOUT, False)[0], 'timeout')
        self.assertEqual(bulk.classify_exit(100, '', False)[0], 'error')
        self.assertEqual(bulk.classify_exit(0, '', True)[0], 'complete')
        self.assertEqual(bulk.classify_exit(0, '', False)[0], 'incomplete')


class CompletionTests(EngineBase):
    def test_completion_hand_off(self):
        self.open_job(budget=0)
        mb = FakeMbsync(self.folder_path, [
            [('deliver', 4), ('exit', 0, 'Channels: 1 Boxes: 1 Far: +0 *0 #0 -0 '
                                         'Near: +4 *0 #0 -0\n', 'complete')]])
        res = self.runner(mb).run('cron')
        self.assertEqual(res['reason'], 'complete')
        job = self.job()
        self.assertEqual(job['status'], 'complete')
        self.assertIsNotNone(job['closed'])
        self.assertEqual(bulk.excluded_folders(self.store.load()), [])   # re-included
        self.assertEqual(self.closed, [True])                 # regular config rewritten
        self.assertEqual(self.claims, [('token', True, job['last_stop']['detail'])])
        # A closed job never runs again.
        self.assertEqual(self.runner(mb).run('cron'),
                         {'ran': False, 'reason': 'complete'})

    def test_not_complete_while_the_journal_remains(self):
        self.open_job(budget=0)
        mb = FakeMbsync(self.folder_path, [
            [('deliver', 4), ('exit', 0, '', 'journal')]])
        res = self.runner(mb).run('cron')
        self.assertEqual(res['reason'], 'incomplete')
        self.assertEqual(self.job()['status'], 'active')
        self.assertEqual(bulk.excluded_folders(self.store.load()), [ALLMAIL])
        self.assertEqual(self.closed, [])

    def test_not_complete_without_a_state_file(self):
        self.open_job(budget=0)
        # rc 0, no journal, but no .mbsyncstate either
        mb = FakeMbsync(self.folder_path, [[('deliver', 1), ('exit', 0, '', 'journal')]])
        r = self.runner(mb)
        with patch.object(mb, 'touch_journal', lambda: None):
            res = r.run('cron')
        self.assertFalse(os.path.exists(self.folder_path + '/.mbsyncstate'))
        self.assertEqual(res['reason'], 'incomplete')

    def test_a_folder_already_synced_still_needs_one_clean_run(self):
        # Rebase / fetch missing: the folder has a final state from an
        # earlier sync. The job must run once, not close itself unrun.
        mb = FakeMbsync(self.folder_path, [[('deliver', 2), ('exit', 0, '', 'complete')]])
        mb.deliver(1)
        mb.finish_state()
        self.open_job(budget=0)
        self.assertEqual(self.job()['status'], 'active')
        self.assertEqual(self.runner(mb).run('cron')['reason'], 'complete')
        self.assertEqual(len(mb.started), 1)


class PullOnlyTests(EngineBase):
    def test_refuses_a_config_that_is_not_pull_only(self):
        self.open_job()
        for bad in ('Sync All', 'Sync Pull Push', 'Sync Push'):
            self.rc_text = GOOD_RC.replace('Sync Pull', bad)
            mb = FakeMbsync(self.folder_path, [[('deliver', 1)]])
            self.clock.now += 86400      # the refusal blocks the day
            res = self.runner(mb).run('cron')
            self.assertEqual(res['ran'], False, bad)
            self.assertEqual(res['reason'], 'error')
            self.assertIn('not pull-only', res['detail'])
            self.assertEqual(mb.started, [], bad)             # mbsync never started
        self.assertEqual(self.job()['runs'][-1]['reason'], 'error')
        self.assertEqual(bulk.count_messages(self.folder_path), 0)

    def test_verify_pull_only(self):
        self.assertEqual(bulk.verify_pull_only(GOOD_RC), (True, ''))
        self.assertFalse(bulk.verify_pull_only(GOOD_RC.replace('Sync Pull\n', ''))[0])
        self.assertFalse(bulk.verify_pull_only(
            GOOD_RC.replace('Expunge None', 'Expunge Both'))[0])
        self.assertFalse(bulk.verify_pull_only(GOOD_RC + 'Remove Near\n')[0])
        self.assertFalse(bulk.verify_pull_only(
            GOOD_RC + '\nChannel other\nSync Pull\n')[0])
        self.assertFalse(bulk.verify_pull_only(GOOD_RC + 'Sync Pull Push\n')[0])

    def test_exclusion_pattern(self):
        self.assertEqual(bulk.exclusion_pattern(None, [ALLMAIL]),
                         '* "![Gmail]/All Mail"')
        self.assertEqual(bulk.exclusion_pattern('*', [ALLMAIL, 'Big "Box"']),
                         '* "![Gmail]/All Mail" "!Big \\"Box\\""')
        self.assertEqual(bulk.exclusion_pattern('INBOX "[Gmail]/Sent Mail"', [ALLMAIL]),
                         'INBOX "[Gmail]/Sent Mail" "![Gmail]/All Mail"')
        # Already excluded by the user: not added twice.
        self.assertEqual(bulk.exclusion_pattern('* "![Gmail]/All Mail"', [ALLMAIL]),
                         '* "![Gmail]/All Mail"')

    @pytest.mark.skipif(os.name == 'nt', reason='compares against POSIX path strings')
    def test_folder_names_cannot_leave_the_maildir(self):
        for bad in ('', '../other', '/etc', 'a/../../b', 'x\ny', 'a//b'):
            with self.assertRaises(bulk.BulkError, msg=bad):
                bulk.validate_folder(bad)
        self.assertEqual(bulk.folder_maildir('/m', ALLMAIL), '/m/[Gmail]/All Mail')


class PauseResumeCancelTests(EngineBase):
    def test_pause_stops_the_run_and_resume_continues(self):
        self.open_job(budget=0)
        mb = FakeMbsync(self.folder_path, [
            [('deliver', 1), ('call', lambda: bulk.set_status(self.store, 'pause')),
             ('deliver', 1), ('deliver', 1), ('deliver', 1)],
            [('deliver', 1), ('exit', 0, '', 'complete')],
        ])
        r = self.runner(mb)
        res = r.run('cron')
        self.assertEqual(res['reason'], 'paused')
        self.assertTrue(mb.handles[0].stop_requested)
        job = self.job()
        self.assertEqual(job['status'], 'paused')
        self.assertEqual(job['runs'][0]['reason'], 'paused')
        # Paused: nothing runs, and the folder stays out of the regular sync.
        self.assertEqual(r.run('cron'), {'ran': False, 'reason': 'paused'})
        self.assertEqual(r.run('manual'), {'ran': False, 'reason': 'paused'})
        self.assertEqual(bulk.excluded_folders(self.store.load()), [ALLMAIL])
        with self.assertRaises(bulk.BulkError):
            bulk.set_status(self.store, 'pause')

        bulk.set_status(self.store, 'resume')
        self.assertEqual(self.job()['status'], 'active')
        self.assertEqual(r.run('cron')['reason'], 'complete')

    def test_cancel_stops_the_run_and_keeps_the_mail(self):
        self.open_job(budget=0, pattern='INBOX "[Gmail]/Sent Mail"')
        mb = FakeMbsync(self.folder_path, [
            [('deliver', 3), ('call', lambda: bulk.set_status(self.store, 'cancel')),
             ('deliver', 1), ('deliver', 1)]])
        res = self.runner(mb).run('cron')
        self.assertEqual(res['reason'], 'cancelled')
        job = self.job()
        self.assertEqual(job['status'], 'cancelled')
        self.assertEqual(job['original_pattern'], 'INBOX "[Gmail]/Sent Mail"')
        self.assertEqual(bulk.excluded_folders(self.store.load()), [])
        self.assertGreaterEqual(bulk.count_messages(self.folder_path), 3)   # mail kept
        with self.assertRaises(bulk.BulkError):
            bulk.set_status(self.store, 'resume')

    def test_cancel_when_idle(self):
        self.open_job()
        mb = FakeMbsync(self.folder_path, [])
        mb.deliver(2)
        bulk.set_status(self.store, 'cancel', now=self.clock())
        job = self.job()
        self.assertEqual((job['status'], job['last_stop']['reason']),
                         ('cancelled', 'cancelled'))
        self.assertEqual(bulk.count_messages(self.folder_path), 2)

    def test_one_open_job_per_account_and_history(self):
        first = self.open_job()
        with self.assertRaises(bulk.BulkError):
            self.open_job()
        bulk.set_status(self.store, 'cancel')
        self.open_job()
        doc = self.store.load()
        self.assertEqual(doc['job']['status'], 'active')
        self.assertEqual([h['id'] for h in doc['history']], [first['id']])

    def test_a_cancelled_run_does_not_write_into_the_next_job(self):
        self.open_job(budget=0)

        def cancel_and_restart():
            bulk.set_status(self.store, 'cancel')
            self.open_job(budget=0)
        mb = FakeMbsync(self.folder_path, [
            [('deliver', 1), ('call', cancel_and_restart), ('deliver', 1)]])
        self.assertEqual(self.runner(mb).run('cron')['reason'], 'cancelled')
        self.assertEqual(self.job()['runs'], [])
        self.assertIsNone(self.job()['running'])


class ProgressTests(EngineBase):
    def test_progress_and_eta(self):
        self.open_job(budget=1_000_000, window=('00:00', '00:00'))
        mb = FakeMbsync(self.folder_path, [
            [('deliver', 4), ('exit', 1, OVERQUOTA, 'journal')],
            [('deliver', 2), ('exit', 1, OVERQUOTA, 'journal')],
            [('deliver', 1)] * 9,
        ])
        r = self.runner(mb, server_counts=lambda folders: {
            ALLMAIL: {'messages': 30, 'uidnext': 31}})
        r.run('cron')
        self.clock.now = local_epoch(3, 2)
        r.run('cron')
        v = bulk.job_view(self.job(), now=self.clock(), pid_alive=lambda p: False)
        self.assertEqual(v['state'], 'waiting')
        self.assertEqual((v['messages_local'], v['messages_server']), (6, 30))
        self.assertEqual(v['messages_remaining'], 24)
        self.assertEqual(v['days_run'], 2)
        self.assertEqual(v['eta_days'], 8)              # 24 left at (4+2)/2 a day
        self.assertEqual(v['today_bytes'], 2 * MSG)
        self.assertEqual(v['budget_percent'], 20)
        self.assertEqual(v['last_stop_reason'], 'provider-cap')
        self.assertEqual(v['next_run'], 'Continues tomorrow.')
        self.assertEqual(v['bytes_local'], 6 * MSG)

    def test_view_while_running_counts_today(self):
        self.open_job(budget=1_000_000)
        seen = {}

        def look():
            seen['v'] = bulk.job_view(self.job(), now=self.clock(),
                                      pid_alive=lambda p: True)
        mb = FakeMbsync(self.folder_path, [
            [('deliver', 3), ('call', look), ('exit', 0, '', 'complete')]])
        self.runner(mb).run('cron')
        self.assertEqual(seen['v']['state'], 'running')
        self.assertEqual(seen['v']['today_bytes'], 3 * MSG)
        self.assertEqual(seen['v']['messages_local'], 3)
        self.assertEqual(bulk.job_view(self.job(), pid_alive=lambda p: False)['state'],
                         'complete')

    def test_eta(self):
        days = [{'messages': 10}, {'messages': 0}, {'messages': 20}, {'messages': 30}]
        self.assertEqual(bulk.estimate_eta(100, days), 5)
        self.assertEqual(bulk.estimate_eta(0, days), 0)
        self.assertIsNone(bulk.estimate_eta(100, []))
        self.assertIsNone(bulk.estimate_eta(None, days))

    @pytest.mark.skipif(os.name == 'nt', reason='renames a message to a ":2,S" file '
                        'name, which NTFS does not allow')
    def test_meter_follows_new_and_cur(self):
        mb = FakeMbsync(self.folder_path, [])
        meter = bulk.MaildirMeter(self.folder_path)
        self.assertEqual(meter.measure(), (0, 0))
        mb.deliver(3)
        self.assertEqual(meter.measure(), (3, 3 * MSG))
        os.rename(self.folder_path + '/new/1.fake', self.folder_path + '/cur/1.fake:2,S')
        mb.deliver(1)
        self.assertEqual(meter.measure(), (4, 4 * MSG))

    def test_provider_defaults(self):
        self.assertEqual(bulk.provider_profile('gmail', 'imap.gmail.com'), 'gmail')
        self.assertEqual(bulk.provider_profile('hotmail', 'outlook.office.com'), 'microsoft')
        self.assertEqual(bulk.provider_profile('icloud', 'imap.mail.me.com'), 'icloud')
        self.assertEqual(bulk.provider_profile('custom', 'mail.example.org'), 'generic')
        # Gmail: just under Google's 2500 MB per day.
        self.assertLessEqual(bulk.PROVIDER_DEFAULTS['gmail']['budget_mb'], 2500)
        self.assertGreaterEqual(bulk.PROVIDER_DEFAULTS['gmail']['budget_mb'], 2000)
        for p in bulk.PROVIDER_DEFAULTS.values():
            bulk.parse_hhmm(p['window_start'])
            bulk.parse_hhmm(p['window_end'])


# ---------------------------------------------------------------------
# Reconcile
# ---------------------------------------------------------------------

class FakeImapConn:
    """An imaplib-shaped connection that records the IMAP commands the
    client code would send."""

    def __init__(self, boxes, delim='/'):
        self.boxes = boxes      # server name -> {'uids': [...], 'uidnext', 'uidvalidity'}
        self.delim = delim
        self.commands = []

    def list(self, *a):
        self.commands.append('LIST')
        out = [f'(\\HasNoChildren) "{self.delim}" "{name}"'.encode()
               for name in self.boxes]
        out.append(f'(\\Noselect \\HasChildren) "{self.delim}" "[Gmail]"'.encode())
        return 'OK', out

    def status(self, mailbox, names):
        self.commands.append('STATUS')
        box = self.boxes[mailbox.strip('"')]
        return 'OK', [f'{mailbox} (MESSAGES {len(box["uids"])} UIDNEXT '
                      f'{box["uidnext"]} UIDVALIDITY {box.get("uidvalidity", 7)})'.encode()]

    def select(self, mailbox='INBOX', readonly=False):
        self.commands.append('EXAMINE' if readonly else 'SELECT')
        self.selected = self.boxes[mailbox.strip('"')]
        return 'OK', [b'1']

    def uid(self, command, *args):
        self.commands.append('UID ' + command)
        lo, hi = [int(x) for x in args[-1].split()[-1].split(':')]
        hits = [u for u in self.selected['uids'] if lo <= u <= hi]
        return 'OK', [' '.join(map(str, hits)).encode()]

    def logout(self):
        self.commands.append('LOGOUT')
        return 'BYE', []

    def __getattr__(self, name):
        # store / expunge / append / copy / delete / create / rename ...
        def record(*a, **k):
            self.commands.append(name.upper())
            return 'OK', [b'']
        return record


READ_ONLY_COMMANDS = {'LIST', 'STATUS', 'EXAMINE', 'UID SEARCH', 'LOGOUT'}


class ReconcileTests(EngineBase):
    def _local(self, folder, count, max_uid=None, journal=False):
        mb = FakeMbsync(bulk.folder_maildir(self.maildir, folder), [])
        mb.deliver(count)
        if max_uid is not None:
            mb.finish_state(max_uid)
        if journal:
            mb.touch_journal()
        return mb

    def test_row_math(self):
        row = bulk.reconcile_row
        r = row('F', server_messages=100, local_messages=100, new_since=0, new_exact=True)
        self.assertEqual((r['verdict'], r['delta'], r['missing']), ('in-sync', 0, 0))

        # 5 more on the server, all 5 arrived after the last sync: explained.
        r = row('F', server_messages=105, local_messages=100, new_since=5,
                new_exact=True, last_sync='2026-11-13 04:00:00')
        self.assertEqual((r['verdict'], r['delta'], r['new_since_last_sync'], r['missing']),
                         ('new-mail', 5, 5, 0))
        self.assertIn('arrived on the server after the last sync', r['explanation'])
        self.assertIn('nothing is missing', r['explanation'])
        self.assertTrue(r['can_fetch'])

        # 12 more on the server, only 5 are new: 7 are really missing.
        r = row('F', server_messages=112, local_messages=100, new_since=5, new_exact=True)
        self.assertEqual((r['verdict'], r['missing'], r['new_since_last_sync']),
                         ('missing', 7, 5))

        # The archive keeps what the server deleted.
        r = row('F', server_messages=90, local_messages=100, new_since=0, new_exact=True)
        self.assertEqual((r['verdict'], r['local_only'], r['missing']),
                         ('local-extra', 10, 0))
        self.assertFalse(r['can_fetch'])

        # Deleted on the server AND new mail: 95 = 92 old + 3 new; 8 only local.
        r = row('F', server_messages=95, local_messages=100, new_since=3, new_exact=True)
        self.assertEqual((r['verdict'], r['new_since_last_sync'], r['local_only'], r['missing']),
                         ('new-mail', 3, 8, 0))

        # Only an upper bound for "new": it explains no more than the difference.
        r = row('F', server_messages=103, local_messages=100, new_since=50, new_exact=False)
        self.assertEqual((r['verdict'], r['new_since_last_sync'], r['missing']),
                         ('new-mail', 3, 0))

        # No finished sync to compare against: a surplus counts as missing.
        r = row('F', server_messages=120, local_messages=100, new_since=None)
        self.assertEqual((r['verdict'], r['missing']), ('missing', 20))

        r = row('F', server_messages=120, local_messages=100, in_job=True)
        self.assertEqual((r['verdict'], r['missing']), ('bulk-in-progress', 0))
        r = row('F', server_messages=50, local_messages=None)
        self.assertEqual(r['verdict'], 'not-archived')

    def test_reconcile_against_a_fake_server_is_read_only(self):
        self._local('INBOX', 100, max_uid=200)
        self._local('[Gmail]/Sent Mail', 40, max_uid=90)
        self._local('Old', 30, max_uid=30)
        self._local(ALLMAIL, 60, journal=True)
        conn = FakeImapConn({
            # 100 synced + 3 new (uids above 200)
            'INBOX': {'uids': list(range(101, 201)) + [201, 205, 206], 'uidnext': 207},
            # 40 local, server has 44 old ones: 4 missing, nothing new
            '[Gmail]/Sent Mail': {'uids': list(range(1, 45)), 'uidnext': 91},
            # server deleted 10 of the 30
            'Old': {'uids': list(range(1, 21)), 'uidnext': 31},
            ALLMAIL: {'uids': list(range(1, 501)), 'uidnext': 501},
            'Never': {'uids': [1, 2], 'uidnext': 3},
        })
        client = bulk.ImapReadOnly(conn)
        out = bulk.reconcile(client, self.maildir, active_folders=[ALLMAIL])
        client.logout()
        rows = {r['folder']: r for r in out['folders']}

        self.assertEqual(sorted(rows), sorted(conn.boxes))        # \Noselect skipped
        self.assertEqual((rows['INBOX']['verdict'], rows['INBOX']['new_since_last_sync'],
                          rows['INBOX']['missing'], rows['INBOX']['delta']),
                         ('new-mail', 3, 0, 3))
        self.assertTrue(rows['INBOX']['new_since_exact'])
        self.assertEqual((rows['[Gmail]/Sent Mail']['verdict'],
                          rows['[Gmail]/Sent Mail']['missing']), ('missing', 4))
        self.assertEqual((rows['Old']['verdict'], rows['Old']['local_only']),
                         ('local-extra', 10))
        self.assertEqual(rows[ALLMAIL]['verdict'], 'bulk-in-progress')
        self.assertEqual(rows[ALLMAIL]['delta'], 440)
        self.assertEqual(rows['Never']['verdict'], 'not-archived')

        # The read-only guarantee.
        self.assertTrue(set(conn.commands) <= READ_ONLY_COMMANDS, conn.commands)
        for forbidden in ('SELECT', 'STORE', 'EXPUNGE', 'APPEND', 'COPY',
                          'DELETE', 'CREATE', 'RENAME', 'CLOSE', 'UID STORE',
                          'UID COPY', 'UID EXPUNGE'):
            self.assertNotIn(forbidden, conn.commands)
        self.assertIn('STATUS', conn.commands)
        self.assertIn('EXAMINE', conn.commands)

    def test_status_only_when_not_exact(self):
        self._local('INBOX', 100, max_uid=200)
        conn = FakeImapConn({'INBOX': {'uids': list(range(101, 204)), 'uidnext': 204}})
        out = bulk.reconcile(bulk.ImapReadOnly(conn), self.maildir, ['INBOX'], exact=False)
        self.assertEqual(set(conn.commands), {'LIST', 'STATUS'})
        row = out['folders'][0]
        self.assertEqual((row['verdict'], row['new_since_last_sync'], row['new_since_exact']),
                         ('new-mail', 3, False))

    def test_other_hierarchy_delimiter(self):
        self._local('Archive/2024', 5, max_uid=5)
        conn = FakeImapConn({'Archive.2024': {'uids': [1, 2, 3, 4, 5], 'uidnext': 6}},
                            delim='.')
        out = bulk.reconcile(bulk.ImapReadOnly(conn), self.maildir)
        self.assertEqual([(r['folder'], r['verdict']) for r in out['folders']],
                         [('Archive/2024', 'in-sync')])

    def test_uidvalidity_change_is_not_called_new_mail(self):
        self._local('INBOX', 10, max_uid=10)
        conn = FakeImapConn({'INBOX': {'uids': list(range(1, 16)), 'uidnext': 16,
                                       'uidvalidity': 99}})
        row = bulk.reconcile(bulk.ImapReadOnly(conn), self.maildir)['folders'][0]
        self.assertEqual((row['verdict'], row['missing']), ('missing', 5))
        self.assertIn('UIDVALIDITY', row['explanation'])

    def test_a_folder_the_server_refuses_does_not_fail_the_rest(self):
        self._local('INBOX', 2, max_uid=2)

        class Flaky(FakeImapConn):
            def status(self, mailbox, names):
                if 'Broken' in mailbox:
                    self.commands.append('STATUS')
                    return 'NO', [b'no such mailbox']
                return super().status(mailbox, names)
        conn = Flaky({'INBOX': {'uids': [1, 2], 'uidnext': 3},
                      'Broken': {'uids': [], 'uidnext': 1}})
        rows = {r['folder']: r for r in
                bulk.reconcile(bulk.ImapReadOnly(conn), self.maildir)['folders']}
        self.assertEqual(rows['INBOX']['verdict'], 'in-sync')
        self.assertEqual(rows['Broken']['verdict'], 'unknown')


# ---------------------------------------------------------------------
# With the app: generated .mbsyncrc, overlap guard, secrets, routes
# ---------------------------------------------------------------------

class AppBase(unittest.TestCase):
    user = USER

    def setUp(self):
        self._jobs = tempfile.TemporaryDirectory()
        self._orig = slc.JOB_STATE_DIR
        slc.JOB_STATE_DIR = self._jobs.name
        self.addCleanup(self._restore)
        for u in (USER, OTHER):
            shutil.rmtree(os.path.join(ma.CONFIG['data_dir'], u), ignore_errors=True)
        self.cfg = ma.get_user_config_dir(USER)
        self.write_accounts([
            {'email': EMAIL, 'provider': 'gmail', 'enabled': True,
             'sync_interval': 'daily'},
            {'email': EMAIL2, 'provider': 'gmail', 'enabled': True,
             'sync_interval': 'daily'},
            {'email': 'me@icloud.com', 'provider': 'icloud', 'enabled': True}])
        (ma.get_user_config_dir(OTHER) / 'accounts.json').write_text(json.dumps([
            {'email': 'other@gmail.com', 'provider': 'gmail', 'enabled': True}]))
        self.maildir = routes.account_maildir(USER, EMAIL)
        self.folder_path = bulk.folder_maildir(self.maildir, ALLMAIL)
        self.clock = FakeClock(local_epoch(2, 2))
        ma.app.config['TESTING'] = True
        self.spawned = []
        p = patch.object(routes, 'spawn_manual_run',
                         lambda u, e: self.spawned.append((u, e)))
        p.start()
        self.addCleanup(p.stop)

    def _restore(self):
        slc.JOB_STATE_DIR = self._orig
        self._jobs.cleanup()
        # The data dir is shared with the other test modules, whose
        # scheduler tests walk every user in it.
        for u in (USER, OTHER):
            shutil.rmtree(os.path.join(ma.CONFIG['data_dir'], u), ignore_errors=True)

    def write_accounts(self, accounts):
        (self.cfg / 'accounts.json').write_text(json.dumps(accounts, indent=2))

    def rc(self):
        return routes.mbsyncrc_path(USER).read_text()

    def patterns(self):
        return [l for l in self.rc().splitlines() if l.startswith('Patterns ')]

    def run_fake(self, mbsync, trigger='cron', **kw):
        deps = dict(start=mbsync.start, server_counts=None, after_run=None,
                    clock=self.clock, sleep=self.clock.sleep, log=lambda m: None)
        deps.update(kw)
        return routes.run_account(USER, EMAIL, trigger, **deps)

    def client(self, user=USER):
        c = ma.app.test_client()
        if user:
            with c.session_transaction() as s:
                s['username'] = user
                s['login_time'] = time.time()
        return c

    def job(self):
        return routes.job_store(USER, EMAIL).load()['job']


@pytest.mark.skipif(os.name == 'nt', reason='drives the initial / rebase sync job, which needs isync; '
                    'what Windows does instead is in test_windows_bulk_sync_port.py')
class ExclusionTests(AppBase):
    def test_exclusion_added_on_start_and_visible_in_mbsyncrc(self):
        ma.generate_mbsyncrc(USER)
        self.assertEqual(self.patterns(), ['Patterns *'] * 3)
        routes.create_job(USER, EMAIL, [ALLMAIL])
        self.assertEqual(self.patterns(),
                         ['Patterns * "![Gmail]/All Mail"', 'Patterns *', 'Patterns *'])
        job = self.job()
        self.assertEqual(job['excluded_pattern'], '* "![Gmail]/All Mail"')
        self.assertIsNone(job['original_pattern'])
        self.assertEqual(job['budget_bytes'], 2400 * bulk.MB)        # Gmail default
        self.assertEqual((job['window_start'], job['window_end']), ('01:00', '06:00'))
        # Any later regeneration (an account save) keeps the exclusion.
        ma.generate_mbsyncrc(USER)
        self.assertIn('Patterns * "![Gmail]/All Mail"', self.rc())
        # Every channel stays pull-only.
        self.assertEqual(self.rc().count('Sync Pull'), 3)

    def test_existing_pattern_restored_exactly_on_cancel(self):
        mine = 'INBOX "[Gmail]/Sent Mail" "Projects/*"'
        self.write_accounts([{'email': EMAIL, 'provider': 'gmail', 'enabled': True,
                              'folder_pattern': mine}])
        before = (self.cfg / 'accounts.json').read_bytes()
        routes.create_job(USER, EMAIL, [ALLMAIL])
        self.assertEqual(self.patterns(), [f'Patterns {mine} "![Gmail]/All Mail"'])
        self.assertEqual(self.job()['original_pattern'], mine)
        self.assertEqual((self.cfg / 'accounts.json').read_bytes(), before)

        routes.change_status(USER, EMAIL, 'cancel')
        self.assertEqual(self.patterns(), [f'Patterns {mine}'])
        self.assertEqual((self.cfg / 'accounts.json').read_bytes(), before)

    def test_existing_pattern_restored_exactly_on_completion(self):
        mine = '* "!Spam" "!Trash"'
        self.write_accounts([{'email': EMAIL, 'provider': 'gmail', 'enabled': True,
                              'folder_pattern': mine}])
        before = (self.cfg / 'accounts.json').read_bytes()
        routes.create_job(USER, EMAIL, [ALLMAIL], budget_mb=0)
        self.assertEqual(self.patterns(), [f'Patterns {mine} "![Gmail]/All Mail"'])
        mb = FakeMbsync(self.folder_path, [[('deliver', 3), ('exit', 0, '', 'complete')]])
        self.assertEqual(self.run_fake(mb)['reason'], 'complete')
        self.assertEqual(self.job()['status'], 'complete')
        self.assertEqual(self.patterns(), [f'Patterns {mine}'])
        self.assertEqual((self.cfg / 'accounts.json').read_bytes(), before)

    def test_pattern_edited_during_the_job_is_kept(self):
        routes.create_job(USER, EMAIL, [ALLMAIL])
        accounts = ma.load_accounts(USER)
        accounts[0]['folder_pattern'] = 'INBOX'
        ma.save_accounts(USER, accounts)
        ma.generate_mbsyncrc(USER)
        self.assertEqual(self.patterns()[0], 'Patterns INBOX "![Gmail]/All Mail"')
        routes.change_status(USER, EMAIL, 'cancel')
        self.assertEqual(self.patterns()[0], 'Patterns INBOX')

    def test_paused_job_keeps_the_exclusion(self):
        routes.create_job(USER, EMAIL, [ALLMAIL])
        routes.change_status(USER, EMAIL, 'pause')
        self.assertEqual(self.patterns()[0], 'Patterns * "![Gmail]/All Mail"')

    @pytest.mark.skipif(os.name == 'nt', reason='asserts POSIX permission bits '
                        '(0600) on the generated config; Windows has no mode bits')
    def test_bulk_config_is_one_pull_only_channel_of_just_the_folders(self):
        routes.create_job(USER, EMAIL, [ALLMAIL, 'Big Archive'])
        path, text = routes.prepare_config(USER, EMAIL, [ALLMAIL, 'Big Archive'])
        self.assertEqual(bulk.verify_pull_only(text), (True, ''))
        self.assertIn('Patterns "[Gmail]/All Mail" "Big Archive"', text)
        self.assertIn('Timeout 120', text)
        self.assertIn('PassCmd "/usr/libexec/mail-archiver-cred', text)   # normal credential path
        self.assertEqual(text.count('IMAPAccount '), 1)
        self.assertNotIn(bulk.safe_account_name(EMAIL2), text)
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        self.assertEqual(os.path.dirname(path), str(self.cfg))

    def test_batched_regular_sync_also_skips_the_folders(self):
        routes.create_job(USER, EMAIL, [ALLMAIL])
        ma.save_credential(USER, EMAIL, SECRET_PASS)
        measured = [('INBOX', 4000), (ALLMAIL, 200000), ('[Gmail]/Sent Mail', 3000)]
        with patch('mail_batcher.measure_folders', return_value=measured):
            plan = ma._plan_batches_inline(USER, EMAIL)
        self.assertEqual(plan['total_msgs'], 7000)
        self.assertNotIn(ALLMAIL, [f for batch in plan['batches'] for f in
                                   (x[0] if isinstance(x, (list, tuple)) else x for x in batch)])


@pytest.mark.skipif(os.name == 'nt', reason='drives the initial / rebase sync job, which needs isync; '
                    'what Windows does instead is in test_windows_bulk_sync_port.py')
class OverlapTests(AppBase):
    def setUp(self):
        super().setUp()
        routes.create_job(USER, EMAIL, [ALLMAIL], budget_mb=0)

    def test_bulk_run_does_not_start_while_a_normal_sync_runs(self):
        job_id, _ = ma._new_sync_job(USER, EMAIL)            # a WebUI sync in flight
        mb = FakeMbsync(self.folder_path, [[('deliver', 1)]])
        res = self.run_fake(mb)
        self.assertEqual((res['ran'], res['reason']), (False, 'overlap'))
        self.assertEqual(res['blocker']['job_id'], job_id)
        self.assertEqual(mb.started, [])
        self.assertEqual(self.job()['runs'], [])

    def test_bulk_run_does_not_start_during_sync_all(self):
        ma._new_sync_job(USER, '__all__')
        mb = FakeMbsync(self.folder_path, [[('deliver', 1)]])
        self.assertEqual(self.run_fake(mb)['reason'], 'overlap')

    def test_normal_sync_is_refused_while_a_bulk_run_is_in_flight(self):
        seen = {}

        def during():
            seen['claim'] = ma._new_sync_job(USER, EMAIL)
            seen['click'] = self.client().post(
                f'/sync/{EMAIL}', headers={'Accept': 'application/json'}).status_code
            seen['all'] = ma._new_sync_job(USER, '__all__')[0]
            seen['other'] = ma._new_sync_job(USER, 'me@icloud.com')[0]
        mb = FakeMbsync(self.folder_path, [
            [('deliver', 1), ('call', during), ('exit', 0, '', 'complete')]])
        self.assertEqual(self.run_fake(mb)['reason'], 'complete')
        job_id, blocker = seen['claim']
        self.assertIsNone(job_id)
        self.assertEqual(blocker['source'], 'bulk')
        self.assertEqual(seen['click'], 409)
        self.assertIsNone(seen['all'])
        self.assertIsNotNone(seen['other'])         # another account is not blocked
        # Afterwards the claim is released.
        self.assertIsNotNone(ma._new_sync_job(USER, EMAIL)[0])

    def test_scheduled_run_waits_for_its_imap_server_lane(self):
        ma._new_sync_job(USER, EMAIL2)              # another Gmail account is syncing
        mb = FakeMbsync(self.folder_path, [[('deliver', 1), ('exit', 0, '', 'complete')]])
        res = self.run_fake(mb, 'cron')
        self.assertEqual((res['ran'], res['reason']), (False, 'server-busy'))
        self.assertEqual(mb.started, [])
        # "Run now" is the user's own decision, like "Sync Now" on another account.
        self.assertEqual(self.run_fake(mb, 'manual')['reason'], 'complete')

    def test_a_bulk_run_makes_its_server_busy_for_the_hourly_sync(self):
        seen = {}
        mb = FakeMbsync(self.folder_path, [
            [('call', lambda: seen.update(busy=ma._busy_servers())),
             ('exit', 0, '', 'complete')]])
        self.run_fake(mb)
        self.assertIn('imap.gmail.com', seen['busy'])

    def test_scheduled_pass_runs_only_jobs_that_are_due(self):
        routes.create_job(USER, EMAIL2, ['Big'], budget_mb=0,
                          window_start='00:00', window_end='00:00')
        routes.change_status(USER, EMAIL2, 'pause')
        bulk.update_settings(routes.job_store(USER, EMAIL), budget_bytes=0,
                             window_start='00:00', window_end='00:00')
        calls = []
        with patch.object(routes, 'run_account',
                          lambda u, e, t, **k: calls.append((u, e, t)) or {'ran': True}), \
                redirect_stdout(io.StringIO()):
            routes.scheduled_bulk_sync()
        self.assertEqual(calls, [(USER, EMAIL, 'cron')])


@pytest.mark.skipif(os.name == 'nt', reason='drives the initial / rebase sync job, which needs isync; '
                    'what Windows does instead is in test_windows_bulk_sync_port.py')
class SecretsTests(AppBase):
    def _all_written_text(self):
        out = []
        for root, _dirs, files in os.walk(str(self.cfg)):
            for name in files:
                if name.endswith('.pass') or name.endswith('.token'):
                    continue        # the app's own encrypted credential store
                with open(os.path.join(root, name), errors='replace') as f:
                    out.append(f.read())
        for name in os.listdir(self._jobs.name):
            with open(os.path.join(self._jobs.name, name), errors='replace') as f:
                out.append(f.read())
        return '\n'.join(out)

    def test_no_secret_reaches_state_log_or_config(self):
        ma.save_credential(USER, EMAIL, SECRET_PASS)
        routes.create_job(USER, EMAIL, [ALLMAIL], budget_mb=0)
        import base64
        sasl = base64.b64encode(
            f'user={EMAIL}\x01auth=Bearer {SECRET_TOKEN}\x01\x01'.encode()).decode()
        chatter = (f'>>> 1 LOGIN "{EMAIL}" "{SECRET_PASS}"\n'
                   f'>>> 2 AUTHENTICATE XOAUTH2 {sasl}\n'
                   f'IMAP command \'LOGIN <user> <pass>\' returned an error\n'
                   + OVERQUOTA)
        mb = FakeMbsync(self.folder_path, [[('deliver', 1), ('exit', 1, chatter, 'journal')]])
        printed = io.StringIO()
        with redirect_stdout(printed):
            res = self.run_fake(mb, log=routes._log(USER, EMAIL))
        self.assertEqual(res['reason'], 'provider-cap')

        tail = self.job()['runs'][0]['output_tail']
        self.assertIn('OVERQUOTA', tail)                     # the useful part is kept
        self.assertIn('redacted', tail)
        text = self._all_written_text() + printed.getvalue()
        self.assertIn('bulk_sync.log', os.listdir(str(self.cfg)))
        for secret in (SECRET_PASS, SECRET_TOKEN, sasl):
            self.assertNotIn(secret, text)
        # The state document itself has no credential-shaped field.
        state = json.dumps(routes.job_store(USER, EMAIL).load()).lower()
        for word in ('password', 'passwd', 'token', 'secret', 'credential'):
            self.assertNotIn(f'"{word}"', state)

    @pytest.mark.skipif(os.name == 'nt', reason='asserts POSIX permission bits '
                        '(0640) on the state file; Windows has no mode bits')
    def test_state_file_is_written_the_app_way(self):
        routes.create_job(USER, EMAIL, [ALLMAIL])
        path = routes.job_store(USER, EMAIL).path
        self.assertEqual(os.path.basename(path), 'bulk_sync_big_box_at_gmail_com.json')
        self.assertEqual(os.path.dirname(path), str(self.cfg))
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o640)      # as accounts.json
        self.assertEqual([n for n in os.listdir(str(self.cfg)) if n.endswith('.tmp')], [])

    def test_sign_in_failure_does_not_echo_the_password(self):
        ma.save_credential(USER, EMAIL, SECRET_PASS)

        def boom(conn, email, credential, auth_type):
            raise RuntimeError(f'[AUTHENTICATIONFAILED] LOGIN {email} {credential} rejected')
        with patch('mail_batcher._connect_imap', lambda *a: object()), \
                patch('mail_batcher._authenticate', boom):
            result = routes.reconcile_account(USER, EMAIL)
        self.assertFalse(result['ok'])
        self.assertIn('Could not sign in', result['error'])
        self.assertNotIn(SECRET_PASS, json.dumps(result))
        self.assertNotIn(SECRET_PASS, self._all_written_text())

    def test_reconcile_uses_the_stored_credential_path(self):
        ma.save_credential(USER, EMAIL, SECRET_PASS)
        seen = {}
        conn = FakeImapConn({'INBOX': {'uids': [1], 'uidnext': 2}})

        def auth(c, email, credential, auth_type):
            seen.update(email=email, credential=credential, auth_type=auth_type)
        with patch('mail_batcher._connect_imap', lambda *a: conn), \
                patch('mail_batcher._authenticate', auth):
            result = routes.reconcile_account(USER, EMAIL)
        self.assertTrue(result['ok'])
        self.assertEqual(seen, {'email': EMAIL, 'credential': SECRET_PASS,
                                'auth_type': 'app_password'})
        self.assertTrue(set(conn.commands) <= READ_ONLY_COMMANDS)
        self.assertEqual(conn.commands[-1], 'LOGOUT')
        self.assertNotIn(SECRET_PASS, self._all_written_text())


@pytest.mark.skipif(os.name == 'nt', reason='the stand-in mbsync writes its marker '
                    'from a SIGTERM handler; Windows ends a child with TerminateProcess, '
                    'which no handler sees (the Windows stop path is in '
                    'tests/test_windows_bulk_sync_port.py)')
class SupervisorTests(unittest.TestCase):
    """The real stop path: mbsync is ended through the supervisor's stdin,
    with SIGTERM, so isync can leave its journal."""

    def _fake_mbsync(self, d):
        script = os.path.join(d, 'fake-mbsync.py')
        with open(script, 'w') as f:
            f.write(
                'import os, signal, sys, time\n'
                'marker = sys.argv[1]\n'
                'def term(*a):\n'
                '    open(marker, "w").write("journal written on SIGTERM")\n'
                '    sys.exit(1)\n'
                'signal.signal(signal.SIGTERM, term)\n'
                'print("Socket connected", flush=True)\n'
                'if len(sys.argv) > 2:\n'
                '    print("done", flush=True); sys.exit(int(sys.argv[2]))\n'
                'time.sleep(60)\n')
        return script

    def test_stop_request_terminates_mbsync_cleanly(self):
        with tempfile.TemporaryDirectory() as d:
            marker = os.path.join(d, 'marker')
            h = bulk.spawn_supervised([sys.executable, self._fake_mbsync(d), marker])
            deadline = time.time() + 10
            while 'Socket connected' not in h.output() and time.time() < deadline:
                time.sleep(0.05)
            self.assertIsNone(h.poll())
            h.request_stop()
            while h.poll() is None and time.time() < deadline:
                time.sleep(0.05)
            self.assertEqual(h.poll(), 1)
            self.assertEqual(open(marker).read(), 'journal written on SIGTERM')
            self.assertIn('Socket connected', h.output())

    def test_exit_code_and_output_pass_through(self):
        with tempfile.TemporaryDirectory() as d:
            h = bulk.spawn_supervised(
                [sys.executable, self._fake_mbsync(d), os.path.join(d, 'm'), '0'])
            deadline = time.time() + 10
            while h.poll() is None and time.time() < deadline:
                time.sleep(0.05)
            self.assertEqual(h.poll(), 0)
            self.assertIn('done', h.output())


# ---------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------

POSTS = ['start', 'settings', 'pause', 'resume', 'cancel', 'run', 'reconcile',
         'fetch-missing']


def in_progress_state(now, pid):
    """A Gmail All Mail job four days in, with a run in flight."""
    job = bulk.new_job(EMAIL, [ALLMAIL], budget_bytes=2400 * bulk.MB,
                       window_start='01:00', window_end='06:00', profile='gmail',
                       created_by=USER, now=now - 3 * 86400 - 3600)
    runs = []
    local = 18376
    plan = [(3, 'cron', 28882, 2391, 1, 'provider-cap',
             'The provider closed the connection: the account\'s daily download limit is used up.',
             'Warning: lost track of 180225 pulled message(s)\n' + OVERQUOTA.strip()),
            (2, 'cron', 28579, 2402, 143, 'budget',
             '2.4 GB downloaded today; the daily budget is 2.4 GB.', ''),
            (1, 'cron', 0, 0, 1, 'timeout',
             'The connection to the mail server timed out.', SOCKET_TIMEOUT.strip())]
    for days_ago, trigger, msgs, mb, rc, reason, detail, tail in plan:
        start = now - days_ago * 86400
        runs.append({
            'day': bulk.day_of(start), 'start': bulk.fmt_time(start),
            'start_epoch': start, 'end': bulk.fmt_time(start + 1020),
            'end_epoch': start + 1020, 'trigger': trigger, 'rc': rc,
            'reason': reason, 'detail': detail, 'messages_before': local,
            'messages_after': local + msgs, 'messages': msgs,
            'bytes_before': 0, 'bytes_after': 0, 'bytes': mb * bulk.MB,
            'output_tail': tail})
        local += msgs
    job['runs'] = runs
    job['last_stop'] = {'reason': 'timeout', 'at': runs[-1]['end'],
                        'detail': runs[-1]['detail']}
    job['server'] = {ALLMAIL: {'messages': 199434, 'uidnext': 250112,
                               'checked': bulk.fmt_time(now - 600)}}
    job['running'] = {'pid': pid, 'trigger': 'cron', 'day': bulk.day_of(now),
                      'start': bulk.fmt_time(now - 600), 'start_epoch': now - 600,
                      'messages_before': local, 'bytes_before': 4793 * bulk.MB,
                      'messages': 9120, 'bytes': 771 * bulk.MB,
                      'updated': bulk.fmt_time(now)}
    job['local'] = {'messages': local + 9120, 'bytes': 5564 * bulk.MB,
                    'at': bulk.fmt_time(now)}
    return job


@pytest.mark.skipif(os.name == 'nt', reason='drives the initial / rebase sync job, which needs isync; '
                    'what Windows does instead is in test_windows_bulk_sync_port.py')
class RouteTests(AppBase):
    def test_page_renders_for_a_test_account(self):
        c = self.client()
        r = c.get('/bulk-sync')
        self.assertEqual(r.status_code, 200)
        html = r.get_data(as_text=True)
        self.assertIn(EMAIL, html)
        self.assertIn('Set up', html)

        r = c.get(f'/bulk-sync/{EMAIL}')
        self.assertEqual(r.status_code, 200)
        html = r.get_data(as_text=True)
        self.assertIn('Initial / rebase sync: ' + EMAIL, html)
        self.assertIn('Start initial / rebase sync', html)
        self.assertIn('value="2400"', html)                      # Gmail default budget
        self.assertIn('[Gmail]/All Mail', html)
        self.assertIn('Reconcile: server against archive', html)

    def test_endpoint_names_other_pages_link_to(self):
        with ma.app.test_request_context():
            from flask import url_for
            self.assertEqual(url_for('bulk_sync_page'), '/bulk-sync')
            # older werkzeug (Debian 12) writes the @ as %40; both reach the route
            from urllib.parse import unquote
            self.assertEqual(unquote(url_for('bulk_sync_account', email=EMAIL)),
                             f'/bulk-sync/{EMAIL}')
            self.assertEqual(url_for('bulk_sync_status'), '/api/bulk-sync/status')

    def test_template_extends_the_shared_layout_and_has_no_dark_mode(self):
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        src = open(os.path.join(here, 'templates', 'bulk_sync.html')).read()
        self.assertTrue(src.startswith('{% extends "base.html" %}'))
        self.assertNotIn('prefers-color-scheme', src)
        self.assertNotIn('<header', src)
        import re
        self.assertEqual(sorted(set(re.findall(r'{% block (\w+)', src))),
                         ['content', 'head_extra', 'scripts', 'title'])

    def test_dashboard_has_the_account_action(self):
        html = self.client().get('/dashboard').get_data(as_text=True)
        from urllib.parse import unquote
        self.assertIn(f'href="/bulk-sync/{EMAIL}"', unquote(html))
        self.assertIn('Initial / rebase sync…', html)

    def test_start_pause_resume_cancel_from_the_webui(self):
        c = self.client()
        r = c.post(f'/bulk-sync/{EMAIL}/start', data={
            'folders': [ALLMAIL], 'folders_other': 'Big Archive\n',
            'budget_mb': '1500', 'window_start': '02:00', 'window_end': '05:30'})
        self.assertEqual(r.status_code, 302)
        job = self.job()
        self.assertEqual(job['folders'], [ALLMAIL, 'Big Archive'])
        self.assertEqual(job['budget_bytes'], 1500 * bulk.MB)
        self.assertEqual((job['window_start'], job['window_end']), ('02:00', '05:30'))
        self.assertIn('"![Gmail]/All Mail" "!Big Archive"', self.rc())

        html = c.get(f'/bulk-sync/{EMAIL}').get_data(as_text=True)
        self.assertIn('Waiting', html)
        self.assertIn('Cancel job', html)

        c.post(f'/bulk-sync/{EMAIL}/settings', data={
            'budget_mb': '900', 'window_start': '00:00', 'window_end': '00:00'})
        self.assertEqual(self.job()['budget_bytes'], 900 * bulk.MB)

        c.post(f'/bulk-sync/{EMAIL}/pause')
        self.assertEqual(self.job()['status'], 'paused')
        c.post(f'/bulk-sync/{EMAIL}/run')
        self.assertEqual(self.spawned, [])                 # paused: no run
        c.post(f'/bulk-sync/{EMAIL}/resume')
        self.assertEqual(self.job()['status'], 'active')
        c.post(f'/bulk-sync/{EMAIL}/run')
        self.assertEqual(self.spawned, [(USER, EMAIL)])    # detached run started

        c.post(f'/bulk-sync/{EMAIL}/cancel')
        self.assertEqual(self.job()['status'], 'cancelled')
        self.assertNotIn('![Gmail]/All Mail', self.rc())
        html = c.get(f'/bulk-sync/{EMAIL}').get_data(as_text=True)
        self.assertIn('Cancelled', html)
        self.assertIn('Start another job', html)

    def test_bad_input_is_refused_with_a_message(self):
        c = self.client()
        for data in ({'folders': ['../../etc'], 'budget_mb': '10'},
                     {'folders': [], 'budget_mb': '10'},
                     {'folders': [ALLMAIL], 'budget_mb': 'lots'},
                     {'folders': [ALLMAIL], 'budget_mb': '-5'},
                     {'folders': [ALLMAIL], 'budget_mb': '5', 'window_start': '25:99',
                      'window_end': '06:00'}):
            r = c.post(f'/bulk-sync/{EMAIL}/start', data=data)
            self.assertEqual(r.status_code, 302)
            self.assertIsNone(self.job(), data)
        self.assertEqual(self.spawned, [])

    def test_non_owner_cannot_see_or_touch_another_users_job(self):
        routes.create_job(USER, EMAIL, [ALLMAIL])
        other = self.client(OTHER)
        self.assertEqual(other.get(f'/bulk-sync/{EMAIL}').status_code, 404)
        for action in POSTS:
            r = other.post(f'/bulk-sync/{EMAIL}/{action}', data={'folder': ALLMAIL})
            self.assertEqual(r.status_code, 404, action)
        self.assertEqual(self.job()['status'], 'active')
        self.assertEqual(self.spawned, [])
        self.assertNotIn(EMAIL, other.get('/bulk-sync').get_data(as_text=True))
        status = other.get('/api/bulk-sync/status').get_json()
        self.assertEqual([a['email'] for a in status['accounts']], ['other@gmail.com'])
        self.assertEqual(other.get(f'/api/bulk-sync/status?email={EMAIL}').get_json(),
                         {'accounts': []})
        self.assertFalse(os.path.exists(routes.job_store(OTHER, EMAIL).path))

    def test_posts_and_pages_require_login(self):
        routes.create_job(USER, EMAIL, [ALLMAIL])
        anon = self.client(user=None)
        for action in POSTS:
            r = anon.post(f'/bulk-sync/{EMAIL}/{action}', data={'folder': ALLMAIL})
            self.assertEqual(r.status_code, 302, action)
            self.assertTrue(r.headers['Location'].endswith('/login'), action)
        for path in ('/bulk-sync', f'/bulk-sync/{EMAIL}', '/api/bulk-sync/status'):
            r = anon.get(path)
            self.assertEqual(r.status_code, 302, path)
            self.assertTrue(r.headers['Location'].endswith('/login'), path)
        self.assertEqual(self.job()['status'], 'active')
        self.assertEqual(self.spawned, [])

    def test_status_json(self):
        routes.create_job(USER, EMAIL, [ALLMAIL])
        data = self.client().get(f'/api/bulk-sync/status?email={EMAIL}').get_json()
        self.assertEqual(len(data['accounts']), 1)
        job = data['accounts'][0]['job']
        self.assertEqual((job['state'], job['folders'], job['budget_mb']),
                         ('waiting', [ALLMAIL], 2400))
        every = self.client().get('/api/bulk-sync/status').get_json()
        self.assertEqual(len(every['accounts']), 3)

    def test_reconcile_view_and_fetch_missing(self):
        mb = FakeMbsync(self.folder_path, [])
        mb.deliver(6)
        mb.finish_state(max_uid=6)
        conn = FakeImapConn({ALLMAIL: {'uids': list(range(1, 11)), 'uidnext': 11},
                             'INBOX': {'uids': [1, 2], 'uidnext': 3}})
        c = self.client()
        with patch.object(routes, 'IMAP_FACTORY', lambda u, a: bulk.ImapReadOnly(conn)):
            r = c.post(f'/bulk-sync/{EMAIL}/reconcile')
        self.assertEqual(r.status_code, 302)
        self.assertTrue(set(conn.commands) <= READ_ONLY_COMMANDS, conn.commands)
        html = c.get(f'/bulk-sync/{EMAIL}').get_data(as_text=True)
        self.assertIn('New since last sync', html)
        self.assertIn('4 messages arrived on the server after the last sync', html)
        self.assertIn('Fetch missing', html)

        r = c.post(f'/bulk-sync/{EMAIL}/fetch-missing', data={'folder': ALLMAIL})
        self.assertEqual(r.status_code, 302)
        job = self.job()
        self.assertEqual((job['kind'], job['folders'], job['status']),
                         ('fetch-missing', [ALLMAIL], 'active'))
        self.assertEqual(self.spawned, [(USER, EMAIL)])
        self.assertIn('"![Gmail]/All Mail"', self.rc())

        # The one-off pull, then hand-back.
        plan = [[('deliver', 4), ('exit', 0, '', 'complete')]]
        mb.plans = plan
        self.clock.now = local_epoch(2, 14)                 # outside the window: manual
        self.assertEqual(self.run_fake(mb, 'manual')['reason'], 'complete')
        self.assertNotIn('![Gmail]/All Mail', self.rc())
        self.assertEqual(bulk.count_messages(self.folder_path), 10)

    def test_cli_list_and_reconcile(self):
        routes.create_job(USER, EMAIL, [ALLMAIL])
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(routes.cli(['--list']), 0)
        self.assertIn(f'{USER}/{EMAIL}', out.getvalue())
        self.assertIn('waiting', out.getvalue())

        conn = FakeImapConn({ALLMAIL: {'uids': [1, 2, 3], 'uidnext': 4}})
        out = io.StringIO()
        with patch.object(routes, 'IMAP_FACTORY', lambda u, a: bulk.ImapReadOnly(conn)), \
                redirect_stdout(out):
            rc = routes.cli(['--reconcile', '--user', USER, '--account', EMAIL,
                             '--folder', ALLMAIL])
        self.assertEqual(rc, 0)
        self.assertIn('[Gmail]/All Mail', out.getvalue())
        self.assertIn('Download in progress', out.getvalue())
        self.assertTrue(set(conn.commands) <= READ_ONLY_COMMANDS)
        with redirect_stdout(io.StringIO()):
            self.assertEqual(routes.cli(['--reconcile', '--user', USER,
                                         '--account', 'nobody@example.org']), 1)

    def test_page_capture_of_a_job_in_progress(self):
        now = time.time()
        job = in_progress_state(now, os.getpid())
        store = routes.job_store(USER, EMAIL)
        bulk.start_job(store, job)
        ma.generate_mbsyncrc(USER)

        def with_reconcile(doc):
            doc['reconcile'] = {
                'ok': True, 'error': None, 'email': EMAIL,
                'checked': bulk.fmt_time(now - 600),
                'server_folders': ['INBOX', ALLMAIL, '[Gmail]/Sent Mail'],
                'folders': [
                    bulk.reconcile_row('INBOX', server_messages=4127, local_messages=4121,
                                       new_since=6, new_exact=True,
                                       last_sync=bulk.fmt_time(now - 7200)),
                    bulk.reconcile_row(ALLMAIL, server_messages=199434,
                                       local_messages=84957, in_job=True),
                    bulk.reconcile_row('[Gmail]/Sent Mail', server_messages=9210,
                                       local_messages=9210, new_since=0, new_exact=True,
                                       last_sync=bulk.fmt_time(now - 7200)),
                ]}
        store.update(with_reconcile)

        r = self.client().get(f'/bulk-sync/{EMAIL}')
        self.assertEqual(r.status_code, 200)
        html = r.get_data(as_text=True)
        for text in ('Running', '84,957', 'of 199,434 on the server', 'Provider daily limit',
                     'Daily budget reached', 'Connection timed out', 'Pause', 'Cancel job',
                     'Download in progress', '* &#34;![Gmail]/All Mail&#34;', 'about '):
            self.assertIn(text, html)
        self.assertIn('disabled>Run now', html)               # a run is in flight
        target = os.environ.get('MAIL_ARCHIVER_PAGE_CAPTURE')
        if target:
            with open(target, 'w') as f:
                f.write(html)


# ---------------------------------------------------------------------
# 1.3.0 pre-deployment review
# ---------------------------------------------------------------------

@pytest.mark.skipif(os.name == 'nt', reason='drives the initial / rebase sync job, which needs isync; '
                    'what Windows does instead is in test_windows_bulk_sync_port.py')
class MalformedStateTests(AppBase):
    """A state file that is valid JSON but not a job must never stop the
    user's .mbsyncrc from being written, and never invent exclusions."""

    def write_state(self, doc, email=EMAIL):
        (self.cfg / bulk.state_filename(email)).write_text(json.dumps(doc))

    def test_folders_that_are_not_strings_are_no_exclusion(self):
        self.write_state({'job': {'status': 'active', 'folders': [1, 2]}})
        self.assertEqual(bulk.excluded_folders_for(str(self.cfg), EMAIL), [])
        ma.generate_mbsyncrc(USER)                      # used to raise TypeError
        self.assertEqual(self.patterns(), ['Patterns *'] * 3)

    def test_folders_as_one_string_is_not_split_into_letters(self):
        self.write_state({'job': {'status': 'active', 'folders': 'abc'}})
        self.assertEqual(bulk.excluded_folders_for(str(self.cfg), EMAIL), [])
        ma.generate_mbsyncrc(USER)                      # used to write * "!a" "!b" "!c"
        self.assertEqual(self.patterns(), ['Patterns *'] * 3)

    def test_other_shapes(self):
        for job in ({'status': 'active', 'folders': ['ok', '']},
                    {'status': 'active', 'folders': ['ok', None]},
                    {'status': 'active', 'folders': {'a': 1}},
                    {'status': 'active', 'folders': [['nested']]},
                    {'status': 'active', 'folders': []},
                    {'status': 'active'},
                    {'status': ['active'], 'folders': ['x']},
                    'active', ['active'], 7, True):
            with self.subTest(job=job):
                self.write_state({'job': job})
                self.assertEqual(bulk.excluded_folders_for(str(self.cfg), EMAIL), [])
                acct = {'email': EMAIL, 'folder_pattern': 'INBOX "Projects/*"'}
                self.assertEqual(bulk.regular_sync_pattern(str(self.cfg), acct),
                                 'INBOX "Projects/*"')
                ma.generate_mbsyncrc(USER)
                self.assertEqual(self.patterns(), ['Patterns *'] * 3)
        for doc in ([1, 2], 'x', None, 3):
            with self.subTest(doc=doc):
                self.write_state(doc)
                ma.generate_mbsyncrc(USER)
                self.assertEqual(self.patterns(), ['Patterns *'] * 3)
        # a well-formed open job is still excluded
        self.write_state({'job': {'status': 'paused', 'folders': [ALLMAIL]}})
        ma.generate_mbsyncrc(USER)
        self.assertEqual(self.patterns()[0], 'Patterns * "![Gmail]/All Mail"')

    def test_regular_sync_pattern_falls_back_to_the_base_on_any_exception(self):
        acct = {'email': EMAIL, 'folder_pattern': 'INBOX'}
        for target in ('excluded_folders_for', 'exclusion_pattern'):
            with self.subTest(target):
                routes.create_job(USER, EMAIL, [ALLMAIL])
                with patch.object(bulk, target, side_effect=RuntimeError('boom')):
                    self.assertEqual(bulk.regular_sync_pattern(str(self.cfg), acct), 'INBOX')
                    self.assertEqual(bulk.regular_sync_pattern(
                        str(self.cfg), {'email': EMAIL}), '*')
                    ma.generate_mbsyncrc(USER)          # the whole user still gets a config
                    self.assertEqual(len(self.patterns()), 3)
                routes.change_status(USER, EMAIL, 'cancel')

    def test_one_bad_state_file_does_not_cost_the_other_accounts_their_exclusion(self):
        routes.create_job(USER, EMAIL2, ['Big Archive'])
        self.write_state({'job': {'status': 'active', 'folders': [1, 2]}})
        ma.generate_mbsyncrc(USER)
        self.assertEqual(self.patterns(),
                         ['Patterns *', 'Patterns * "!Big Archive"', 'Patterns *'])


@pytest.mark.skipif(os.name == 'nt', reason='drives the initial / rebase sync job, which needs isync; '
                    'what Windows does instead is in test_windows_bulk_sync_port.py')
class StuckExclusionTests(AppBase):
    """The runner records the job as complete, then regenerates the
    .mbsyncrc. If it dies in between, the folder stayed excluded from the
    regular sync until something else happened to regenerate the file."""

    def die_before_regenerating(self, pattern=None):
        if pattern:
            self.write_accounts([{'email': EMAIL, 'provider': 'gmail', 'enabled': True,
                                  'folder_pattern': pattern},
                                 {'email': EMAIL2, 'provider': 'gmail', 'enabled': True}])
        routes.create_job(USER, EMAIL, [ALLMAIL], budget_mb=0)
        mb = FakeMbsync(self.folder_path, [[('deliver', 3), ('exit', 0, '', 'complete')]])

        def killed():
            raise KeyboardInterrupt        # stands for SIGKILL / reboot right here

        with self.assertRaises(KeyboardInterrupt):
            self.run_fake(mb, on_closed=killed)
        self.assertEqual(self.job()['status'], 'complete')
        self.assertIn('"![Gmail]/All Mail"', self.patterns()[0])    # stuck

    def test_hourly_pass_removes_the_exclusion_of_a_closed_job(self):
        self.die_before_regenerating()
        with redirect_stdout(io.StringIO()):
            routes.scheduled_bulk_sync()
        self.assertEqual(self.patterns(), ['Patterns *'] * 3)
        log = (self.cfg / routes.LOG_NAME).read_text()
        self.assertIn('folder exclusion was still in the sync configuration', log)

    def test_page_load_removes_it_too(self):
        for url in ('/bulk-sync', f'/bulk-sync/{EMAIL}'):
            with self.subTest(url):
                self.die_before_regenerating()
                self.assertEqual(self.client().get(url).status_code, 200)
                self.assertEqual(self.patterns(), ['Patterns *'] * 3)

    def test_the_accounts_own_pattern_comes_back_exactly(self):
        mine = '* "!Spam" "!Trash"'
        self.die_before_regenerating(pattern=mine)
        self.assertEqual(routes.heal_stuck_exclusions(USER), [EMAIL])
        self.assertEqual(self.patterns(), [f'Patterns {mine}', 'Patterns *'])
        self.assertEqual(routes.heal_stuck_exclusions(USER), [])

    def test_open_jobs_and_user_negations_are_left_alone(self):
        mine = '* "!Spam"'
        self.write_accounts([{'email': EMAIL, 'provider': 'gmail', 'enabled': True,
                              'folder_pattern': mine},
                             {'email': EMAIL2, 'provider': 'gmail', 'enabled': True}])
        ma.generate_mbsyncrc(USER)
        calls = []
        with patch.object(routes, 'regenerate_config', calls.append):
            self.assertEqual(routes.heal_stuck_exclusions(USER), [])     # no job at all
        routes.create_job(USER, EMAIL2, [ALLMAIL])
        with patch.object(routes, 'regenerate_config', calls.append):
            self.assertEqual(routes.heal_stuck_exclusions(USER), [])     # active
            routes.change_status(USER, EMAIL2, 'pause')
            self.assertEqual(routes.heal_stuck_exclusions(USER), [])     # paused
            with redirect_stdout(io.StringIO()):
                routes.scheduled_bulk_sync()
            self.client().get('/bulk-sync')
        self.assertEqual(calls, [])
        self.assertEqual(self.patterns(),
                         [f'Patterns {mine}', 'Patterns * "![Gmail]/All Mail"'])

    def test_rc_patterns_reads_the_right_channel(self):
        routes.create_job(USER, EMAIL2, ['Big Archive'])
        rc = self.rc()
        self.assertEqual(bulk.rc_patterns(rc, bulk.safe_account_name(EMAIL)), '*')
        self.assertEqual(bulk.rc_patterns(rc, bulk.safe_account_name(EMAIL2)),
                         '* "!Big Archive"')
        self.assertIsNone(bulk.rc_patterns(rc, 'nobody_at_example_com'))
        self.assertIsNone(bulk.rc_patterns('', 'x'))

    def test_a_missing_config_file_is_not_an_error(self):
        self.assertEqual(routes.heal_stuck_exclusions(USER), [])
        self.assertEqual(routes.heal_stuck_exclusions('no-such-user-here'), [])
        shutil.rmtree(os.path.join(ma.CONFIG['data_dir'], 'no-such-user-here'),
                      ignore_errors=True)


@pytest.mark.skipif(os.name == 'nt', reason='drives the initial / rebase sync job, which needs isync; '
                    'what Windows does instead is in test_windows_bulk_sync_port.py')
class BudgetInputTests(AppBase):
    BAD = ('inf', '-inf', 'Infinity', 'nan', 'NaN', '1e999', '-1e999', '-5', 'lots', '1,5')

    def flashed(self, c):
        with c.session_transaction() as s:
            return [m for _cat, m in s.get('_flashes', [])]

    def test_start_with_a_bad_budget_is_a_form_error_not_a_500(self):
        for value in self.BAD:
            with self.subTest(value):
                c = self.client()
                r = c.post(f'/bulk-sync/{EMAIL}/start',
                           data={'folders': [ALLMAIL], 'budget_mb': value})
                self.assertEqual(r.status_code, 302)
                msgs = self.flashed(c)
                self.assertEqual(len(msgs), 1)
                self.assertIn('daily budget', msgs[0])
                self.assertIsNone(self.job())               # nothing was set up
                html = c.get(f'/bulk-sync/{EMAIL}').get_data(as_text=True)
                self.assertIn('Start initial / rebase sync', html)   # the form, again

    def test_settings_with_a_bad_budget_keep_the_old_value(self):
        routes.create_job(USER, EMAIL, [ALLMAIL], budget_mb=700)
        for value in self.BAD:
            with self.subTest(value):
                c = self.client()
                r = c.post(f'/bulk-sync/{EMAIL}/settings', data={
                    'budget_mb': value, 'window_start': '02:00', 'window_end': '03:00'})
                self.assertEqual(r.status_code, 302)
                self.assertIn('daily budget', self.flashed(c)[0])
                job = self.job()
                self.assertEqual(job['budget_bytes'], 700 * bulk.MB)
                self.assertEqual((job['window_start'], job['window_end']), ('01:00', '06:00'))

    def test_a_huge_budget_is_clamped(self):
        c = self.client()
        r = c.post(f'/bulk-sync/{EMAIL}/start',
                   data={'folders': [ALLMAIL], 'budget_mb': '1e300'})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.job()['budget_bytes'], routes.MAX_BUDGET_MB * bulk.MB)
        r = c.post(f'/bulk-sync/{EMAIL}/settings', data={
            'budget_mb': '9' * 400, 'window_start': '01:00', 'window_end': '06:00'})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.job()['budget_bytes'], routes.MAX_BUDGET_MB * bulk.MB)
        self.assertEqual(c.get(f'/bulk-sync/{EMAIL}').status_code, 200)

    def test_parse_budget_mb(self):
        self.assertEqual(routes.parse_budget_mb('1500'), 1500.0)
        self.assertEqual(routes.parse_budget_mb(' 0.5 '), 0.5)
        self.assertEqual(routes.parse_budget_mb(''), 0.0)       # 0 = no daily limit
        self.assertEqual(routes.parse_budget_mb(None), 0.0)
        for value in self.BAD:
            with self.assertRaises(bulk.BulkError):
                routes.parse_budget_mb(value)


if __name__ == '__main__':
    unittest.main()


# ---------------------------------------------------------------------
# 1.3.0: the importer's local-only folders stay out of the regular sync
# (isync 1.5.1 fails a whole-channel run on a near-side box without a
# far-side partner: "far side box PST/Inbox cannot be opened", rc 1)
# ---------------------------------------------------------------------

def _mkbox(path):
    for d in ('cur', 'new', 'tmp'):
        os.makedirs(os.path.join(path, d), exist_ok=True)


class ImportedFoldersStayOutOfTheRegularSync(AppBase):
    def test_no_imported_folders_means_the_pattern_is_unchanged(self):
        ma.generate_mbsyncrc(USER)
        self.assertEqual(set(self.patterns()), {'Patterns *'})

    def test_account_pattern_is_kept_without_imports(self):
        self.write_accounts([{'email': EMAIL, 'provider': 'gmail', 'enabled': True,
                              'folder_pattern': 'INBOX Work'}])
        ma.generate_mbsyncrc(USER)
        self.assertEqual(self.patterns(), ['Patterns INBOX Work'])

    def test_pst_and_imported_folders_are_excluded_for_that_account_only(self):
        _mkbox(os.path.join(self.maildir, 'PST', 'Inbox'))
        _mkbox(os.path.join(self.maildir, 'Imported', 'mbox', 'x'))
        ma.generate_mbsyncrc(USER)
        want = 'Patterns * "!PST" "!PST/*" "!Imported" "!Imported/*"'
        self.assertEqual(self.patterns().count(want), 1)
        self.assertEqual(self.patterns().count('Patterns *'), 2)

    def test_only_the_prefix_that_exists_is_excluded(self):
        _mkbox(os.path.join(self.maildir, 'PST', 'Sent Items'))
        self.assertEqual(bulk.local_only_import_folders(self.maildir), ['PST'])
        self.assertEqual(bulk.base_sync_pattern(str(self.cfg), {'email': EMAIL}),
                         '* "!PST" "!PST/*"')

    def test_a_server_folder_called_PST_keeps_syncing(self):
        # a folder that isync has synced carries its state file
        box = os.path.join(self.maildir, 'PST', 'Archive')
        _mkbox(box)
        open(os.path.join(box, '.mbsyncstate'), 'w').close()
        self.assertEqual(bulk.local_only_import_folders(self.maildir), [])
        ma.generate_mbsyncrc(USER)
        self.assertEqual(set(self.patterns()), {'Patterns *'})

    @pytest.mark.skipif(os.name == 'nt', reason='needs os.symlink, which wants a '
                        'privilege (or developer mode) on Windows')
    def test_a_link_named_PST_is_not_followed(self):
        target = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, target, True)
        os.makedirs(self.maildir, exist_ok=True)
        os.symlink(target, os.path.join(self.maildir, 'PST'))
        self.assertEqual(bulk.local_only_import_folders(self.maildir), [])

    def test_open_bulk_job_and_imported_folders_combine(self):
        _mkbox(os.path.join(self.maildir, 'PST', 'Inbox'))
        (self.cfg / bulk.state_filename(EMAIL)).write_text(json.dumps(
            {'job': {'status': 'active', 'folders': [ALLMAIL]}}))
        got = bulk.regular_sync_pattern(str(self.cfg), {'email': EMAIL})
        self.assertEqual(got, '* "!PST" "!PST/*" "![Gmail]/All Mail"')

    def test_imported_folder_exclusion_is_not_a_stuck_job_exclusion(self):
        _mkbox(os.path.join(self.maildir, 'PST', 'Inbox'))
        ma.generate_mbsyncrc(USER)
        before = self.rc()
        self.assertEqual(routes.heal_stuck_exclusions(USER), [])
        self.assertEqual(self.rc(), before)

    def test_a_left_over_job_exclusion_is_still_healed_with_imports_present(self):
        _mkbox(os.path.join(self.maildir, 'PST', 'Inbox'))
        ma.generate_mbsyncrc(USER)
        rc = routes.mbsyncrc_path(USER)
        rc.write_text(rc.read_text().replace(
            'Patterns * "!PST" "!PST/*"',
            'Patterns * "!PST" "!PST/*" "![Gmail]/All Mail"'))
        self.assertEqual(routes.heal_stuck_exclusions(USER), [EMAIL])
        self.assertIn('Patterns * "!PST" "!PST/*"', self.patterns())
        self.assertNotIn('All Mail', self.rc())

    def test_after_an_import_the_config_is_regenerated_once(self):
        ma.generate_mbsyncrc(USER)
        self.assertFalse(ma._exclude_imported_folders_from_sync(USER))   # nothing imported
        _mkbox(os.path.join(self.maildir, 'Imported', 'eml'))
        with redirect_stdout(io.StringIO()):
            self.assertTrue(ma._exclude_imported_folders_from_sync(USER))
        self.assertIn('Patterns * "!Imported" "!Imported/*"', self.patterns())
        stamp = os.stat(routes.mbsyncrc_path(USER)).st_mtime_ns
        self.assertFalse(ma._exclude_imported_folders_from_sync(USER))   # already right
        self.assertEqual(os.stat(routes.mbsyncrc_path(USER)).st_mtime_ns, stamp)

    def test_a_missing_mbsyncrc_never_fails_the_import(self):
        try:
            os.unlink(routes.mbsyncrc_path(USER))
        except OSError:
            pass
        with redirect_stdout(io.StringIO()):
            self.assertFalse(ma._exclude_imported_folders_from_sync(USER))
