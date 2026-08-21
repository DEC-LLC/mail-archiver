"""Unit tests for mail_batcher.plan_batches + _run_folder_chunked (1.0.17).

Runs with plain `python3 -m pytest tests/` from repo root, no IMAP or
network access. Mocks subprocess.Popen and os.scandir so we can drive
the chunk loop deterministically.
"""

import os
import sys
import types
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import mail_batcher  # noqa: E402


class PlanBatchesTests(unittest.TestCase):
    def test_small_folders_pack_smallest_first(self):
        folders = [('INBOX', 100), ('Sent', 50), ('Spam', 200)]
        batches = mail_batcher.plan_batches(folders, threshold=1000)
        # All fit in one batch, ascending order preserved
        self.assertEqual(len(batches), 1)
        names = [n for n, _ in batches[0]]
        self.assertEqual(names, ['Sent', 'INBOX', 'Spam'])

    def test_oversized_folder_gets_own_batch(self):
        folders = [('INBOX', 19971), ('Sent', 500), ('Drafts', 100)]
        batches = mail_batcher.plan_batches(folders, threshold=5000)
        # Small folders pack together; INBOX gets its own batch
        self.assertEqual(len(batches), 2)
        self.assertEqual([n for n, _ in batches[1]], ['INBOX'])
        self.assertEqual(sorted(n for n, _ in batches[0]),
                         ['Drafts', 'Sent'])

    def test_normalize_batch_accepts_lists(self):
        # Post-JSON round-trip: tuples become lists
        got = mail_batcher._normalize_batch([['INBOX', 19971], ['Sent', 50]])
        self.assertEqual(got, [('INBOX', 19971), ('Sent', 50)])

    def test_normalize_batch_accepts_legacy_strings(self):
        got = mail_batcher._normalize_batch(['INBOX', 'Sent'])
        self.assertEqual(got, [('INBOX', 0), ('Sent', 0)])


class ChunkLoopTests(unittest.TestCase):
    def _fake_popen(self, returncode=0, lines=None):
        """Return a MagicMock that behaves like subprocess.Popen enough
        for _run_folder_chunked (iterable stdout, wait(), returncode).
        """
        proc = MagicMock()
        proc.stdout = iter(lines or [])
        proc.returncode = returncode
        proc.wait = MagicMock(return_value=returncode)
        proc.terminate = MagicMock()
        proc.kill = MagicMock()
        return proc

    def test_clean_exit_breaks_after_one_chunk(self):
        scandir_calls = {'n': 0}

        def fake_scandir(path):
            # before=0 → after=100 → loop exits (rc=0)
            scandir_calls['n'] += 1
            n = 0 if scandir_calls['n'] <= 2 else 100
            return iter([MagicMock() for _ in range(n)])

        with patch('mail_batcher.subprocess.Popen',
                   return_value=self._fake_popen(returncode=0,
                                                 lines=['line1\n'])), \
             patch('mail_batcher.os.scandir', side_effect=fake_scandir):
            rc = mail_batcher._run_folder_chunked(
                rc_path='/tmp/fake.rc',
                channel='ch',
                folder_name='INBOX',
                folder_total=100,
                maildir_path='/tmp/nonexistent',
                subprocess_kwargs={},
                progress_cb=None,
                line_cb=None,
                chunk_wall=5,
            )
        self.assertEqual(rc, 0)

    def test_stuck_counter_aborts_after_two_no_progress_chunks(self):
        # scandir always returns empty → delta stays 0 forever
        with patch('mail_batcher.subprocess.Popen',
                   return_value=self._fake_popen(returncode=1,
                                                 lines=[])), \
             patch('mail_batcher.os.scandir',
                   side_effect=FileNotFoundError):
            rc = mail_batcher._run_folder_chunked(
                rc_path='/tmp/fake.rc',
                channel='ch',
                folder_name='INBOX',
                folder_total=10000,
                maildir_path='/tmp/nonexistent',
                subprocess_kwargs={},
                progress_cb=None,
                line_cb=None,
                chunk_wall=5,
                max_stuck=2,
            )
        # Non-zero: 2 stuck chunks in a row with rc != 0
        self.assertNotEqual(rc, 0)

    def test_progress_resets_stuck_counter(self):
        # Sequence: chunk1 makes progress (0→50), chunk2 no progress
        # (50→50), chunk3 clean exit — must NOT abort at chunk2 alone.
        counts = [0, 50, 50, 50, 50, 100]
        idx = {'i': 0}

        def fake_scandir(path):
            i = idx['i']
            idx['i'] = min(i + 1, len(counts) - 1)
            n = counts[i]
            return iter([MagicMock() for _ in range(n)])

        # Chunk 1: rc=1 (partial), chunk 2: rc=1 stuck, chunk 3: rc=0
        procs = [
            self._fake_popen(returncode=1, lines=['a\n']),
            self._fake_popen(returncode=1, lines=['b\n']),
            self._fake_popen(returncode=0, lines=['c\n']),
        ]
        popen_calls = {'n': 0}

        def fake_popen_factory(*a, **kw):
            p = procs[popen_calls['n']]
            popen_calls['n'] += 1
            return p

        with patch('mail_batcher.subprocess.Popen',
                   side_effect=fake_popen_factory), \
             patch('mail_batcher.os.scandir', side_effect=fake_scandir):
            rc = mail_batcher._run_folder_chunked(
                rc_path='/tmp/fake.rc',
                channel='ch',
                folder_name='INBOX',
                folder_total=100,
                maildir_path='/tmp/nonexistent',
                subprocess_kwargs={},
                progress_cb=None,
                line_cb=None,
                chunk_wall=5,
                max_stuck=2,
            )
        self.assertEqual(rc, 0)
        self.assertEqual(popen_calls['n'], 3)

    def test_negative_delta_treated_as_no_progress(self):
        # Concurrent cleanup shrinks the maildir mid-chunk — must not
        # look like "progress" and reset the stuck counter falsely.
        counts = [100, 50]  # before=100, after=50 → delta=-50 → max(0, -50)=0
        idx = {'i': 0}

        def fake_scandir(path):
            i = idx['i']
            idx['i'] = min(i + 1, len(counts) - 1)
            return iter([MagicMock() for _ in range(counts[i])])

        with patch('mail_batcher.subprocess.Popen',
                   return_value=self._fake_popen(returncode=1,
                                                 lines=[])), \
             patch('mail_batcher.os.scandir', side_effect=fake_scandir):
            rc = mail_batcher._run_folder_chunked(
                rc_path='/tmp/fake.rc',
                channel='ch',
                folder_name='INBOX',
                folder_total=1000,
                maildir_path='/tmp/nonexistent',
                subprocess_kwargs={},
                progress_cb=None,
                line_cb=None,
                chunk_wall=5,
                max_stuck=1,
            )
        # max_stuck=1 + immediate delta=0 → abort after 1 chunk, rc=1
        self.assertNotEqual(rc, 0)


if __name__ == '__main__':
    unittest.main()
