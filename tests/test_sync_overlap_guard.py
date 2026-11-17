"""Unit tests for the 1.1.7 no-overlapping-syncs guard.

Runs with plain `python3 -m pytest tests/` from repo root. Uses a temp
JOB_STATE_DIR and a temp data dir; no IMAP, no mbsync.

Background: a WebUI "Sync all", a per-account click,
the hourly cron run and two overlapping iCloud runs all hit one user's
maildirs at once ("near side box ... cannot be opened anymore").
"""

import multiprocessing
import os
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sync_lifecycle as slc  # noqa: E402


def _initial(owner_pid=None, heartbeat=True):
    return {'state': 'starting', 'owner_pid': owner_pid or os.getpid(),
            'boot_id': slc.current_boot_id(), 'heartbeat': heartbeat,
            'mbsync_pid': None}


def _claim_in_child(state_dir, q):
    slc.JOB_STATE_DIR = state_dir
    job_id, running = slc.claim_sync('alice', 'alice.example@icloud.com',
                                     _initial(owner_pid=os.getppid()))
    q.put(bool(job_id))


class GuardTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = slc.JOB_STATE_DIR
        slc.JOB_STATE_DIR = self._tmp.name

    def tearDown(self):
        slc.JOB_STATE_DIR = self._orig
        self._tmp.cleanup()


class OverlapRuleTests(unittest.TestCase):
    def test_same_account_overlaps(self):
        self.assertTrue(slc.targets_overlap('a@x', 'a@x'))

    def test_all_overlaps_everything(self):
        self.assertTrue(slc.targets_overlap('__all__', 'a@x'))
        self.assertTrue(slc.targets_overlap('a@x', '__all__'))
        self.assertTrue(slc.targets_overlap(None, 'a@x'))

    def test_different_accounts_do_not_overlap(self):
        self.assertFalse(slc.targets_overlap('a@x', 'b@y'))


class ClaimTests(GuardTestBase):
    def test_second_click_is_refused_and_not_queued(self):
        first, none = slc.claim_sync('alice', 'a@x', _initial())
        self.assertIsNotNone(first)
        self.assertIsNone(none)
        second, running = slc.claim_sync('alice', 'a@x', _initial())
        self.assertIsNone(second)
        self.assertEqual(running['job_id'], first)
        # Nothing queued: exactly one job file exists.
        jobs = [n for n in os.listdir(self._tmp.name) if n.endswith('.json')]
        self.assertEqual(len(jobs), 1)

    def test_sync_all_blocks_account_and_vice_versa(self):
        job, _ = slc.claim_sync('alice', '__all__', _initial())
        self.assertIsNotNone(job)
        self.assertIsNone(slc.claim_sync('alice', 'a@x', _initial())[0])
        slc.update_state(job, state='done')
        acct, _ = slc.claim_sync('alice', 'a@x', _initial())
        self.assertIsNotNone(acct)
        self.assertIsNone(slc.claim_sync('alice', '__all__', _initial())[0])

    def test_other_account_and_other_user_may_run(self):
        self.assertIsNotNone(slc.claim_sync('alice', 'a@x', _initial())[0])
        self.assertIsNotNone(slc.claim_sync('alice', 'b@y', _initial())[0])
        self.assertIsNotNone(slc.claim_sync('dave', 'a@x', _initial())[0])

    def test_finished_sync_does_not_block(self):
        job, _ = slc.claim_sync('alice', 'a@x', _initial())
        slc.update_state(job, state='error')
        self.assertIsNotNone(slc.claim_sync('alice', 'a@x', _initial())[0])

    def test_dead_sync_is_reaped_and_does_not_block(self):
        # Owner pid that cannot exist: the worker died mid-sync.
        job, _ = slc.claim_sync('alice', 'a@x', _initial(owner_pid=2**22 + 7))
        new, running = slc.claim_sync('alice', 'a@x', _initial())
        self.assertIsNone(running)
        self.assertIsNotNone(new)
        self.assertEqual(slc.read_state(job)['state'], 'error')

    @pytest.mark.skipif(os.name == 'nt', reason="needs the 'fork' start method "
                        '(twelve children racing for one claim); Windows has only spawn')
    def test_concurrent_clicks_exactly_one_wins(self):
        ctx = multiprocessing.get_context('fork')
        q = ctx.Queue()
        procs = [ctx.Process(target=_claim_in_child, args=(self._tmp.name, q))
                 for _ in range(12)]
        for p in procs:
            p.start()
        for p in procs:
            p.join(10)
        wins = [q.get(timeout=5) for _ in procs]
        self.assertEqual(sum(wins), 1)


class LockModeTests(GuardTestBase):
    def test_claim_works_when_lock_file_is_read_only(self):
        # cron (root, umask 022) left the claim lock
        # 0640 root:mail-archiver; an O_RDWR open by the service failed.
        lock = os.path.join(self._tmp.name, '.claim-alice.lock')
        open(lock, 'w').close()
        os.chmod(lock, 0o440)
        job, running = slc.claim_sync('alice', 'a@x', _initial())
        self.assertIsNotNone(job)


class StallTests(GuardTestBase):
    def test_silent_job_is_stalled_but_still_blocks(self):
        job, _ = slc.claim_sync('alice', 'a@x', _initial())
        st = slc.read_state(job)
        later = time.time() + slc.STALL_SECONDS + 60
        self.assertTrue(slc.is_state_stalled(st, now=later))
        with patch('time.time', return_value=later):
            self.assertIsNone(slc.claim_sync('alice', 'a@x', _initial())[0])

    def test_fresh_job_is_not_stalled(self):
        job, _ = slc.claim_sync('alice', 'a@x', _initial())
        self.assertFalse(slc.is_state_stalled(slc.read_state(job)))

    def test_cron_job_without_heartbeat_is_never_stalled(self):
        job, _ = slc.claim_sync('alice', 'a@x', _initial(heartbeat=False))
        st = slc.read_state(job)
        self.assertFalse(slc.is_state_stalled(
            st, now=time.time() + 10 * slc.STALL_SECONDS))


if __name__ == '__main__':
    unittest.main()
