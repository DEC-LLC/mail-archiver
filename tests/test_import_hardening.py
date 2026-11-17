"""1.3.0 pre-deployment review: the importer must not be steerable by the
mailbox owner into other people's files, must not fill the data disk, and
its retention freeze, lock probe, upload and index-database handling must
do what they say.

The import tree (<data>/<user>/_import) is writable by the mailbox owner
and is read, chowned and moved by root (cron) and by the service
(CAP_DAC_OVERRIDE). "victim" below is a directory that stands for another
user's mail: nothing in it may be read, moved, chowned or written.
"""

try:
    import fcntl
except ImportError:                 # Windows (module skipped there, see pytestmark)
    fcntl = None
import hashlib
import io
import os
import sqlite3
import threading
import time
import unittest
import warnings
from collections import namedtuple
from contextlib import redirect_stdout
from unittest.mock import patch

import pytest

# Linux-only. The Windows paths of the same code are covered by
# tests/test_windows_import_port.py.
pytestmark = pytest.mark.skipif(
    os.name == 'nt',
    reason='hardening of the Linux privilege model: symbolic links planted by the '
           'mailbox owner, st_uid ownership checks, chown hooks, flock probes and '
           'directory descriptors (/proc/self/fd) - none of which exist on Windows')

import test_mail_import as tm      # sets the test environment, fake tools

mi, pst, ma, search_index = tm.mi, tm.pst, tm.ma, tm.search_index
D = tm.D
Usage = namedtuple('Usage', 'total used free')


def tree(path):
    """{relative path: content or '<dir>' or '-> target'} of a directory."""
    out = {}
    for root, dirs, files in os.walk(path):
        for n in dirs + files:
            p = os.path.join(root, n)
            rel = os.path.relpath(p, path)
            if os.path.islink(p):
                out[rel] = '-> ' + os.readlink(p)
            elif os.path.isdir(p):
                out[rel] = '<dir>'
            else:
                with open(p, 'rb') as f:
                    out[rel] = f.read()
    return out


class Hardened(tm.Flow):
    """Flow, with every prepare() (the chown hook) and convert() recorded."""

    def make(self, owners=None, **cfg):
        self.prepared = getattr(self, 'prepared', [])
        self.converted = getattr(self, 'converted', [])
        self.logged = getattr(self, 'logged', [])
        self.cfg = pst.normalize_config(cfg, [self.A, self.B])

        def convert(path, *a):
            self.converted.append(path)
            return self.convert(path, *a)

        self.imp = mi.UserImporter(
            self.tmp, self.USER, [self.A, self.B], self.cfg,
            convert=convert, prepare=self.prepared.append, index=self.indexed.append,
            open_ledger=self.ledger, log=self.logged.append, now=lambda: self.now,
            sleep=lambda s: None, stable_wait=0, owners=owners)
        self.imp.ensure_dirs()
        return self.imp

    def victim(self):
        """Another user's INBOX/cur with two messages in it."""
        v = os.path.join(self.tmp, 'victim', 'acct', 'INBOX', 'cur')
        tm.write(os.path.join(v, '1700000001.M1P1.host:2,S'), tm.raw('v1@x'))
        tm.write(os.path.join(v, '1700000002.M1P1.host:2,S'), tm.raw('v2@x'))
        return v

    def replace_with_link(self, path, target):
        os.rename(path, path + '.real')
        os.symlink(target, path)

    def assert_untouched(self, victim, before):
        self.assertEqual(tree(victim), before)
        self.assertEqual([p for p in self.prepared
                          if os.path.realpath(p).startswith(os.path.realpath(victim))], [])


class SymlinkTests(Hardened):
    def test_inbox_symlinked_to_another_directory_is_refused(self):
        victim = self.victim()
        before = tree(victim)
        self.replace_with_link(self.ap(self.A)['inbox'], victim)
        self.drop(self.B, 'ok.eml', tm.raw('b1@x'))         # the other account still works
        del self.prepared[:]
        summary = self.imp.run()
        # nothing read, nothing moved, nothing chowned
        self.assert_untouched(victim, before)
        self.assertEqual([r['account'] for r in summary['results']], [self.B])
        self.assertEqual(summary['results'][0]['status'], 'done')
        self.assertEqual(os.listdir(self.ap(self.A)['done']), [])
        self.assertEqual(os.listdir(self.ap(self.A)['failed']), [])
        self.assertFalse(os.path.exists(self.maildir(self.A)))
        # ... and the refusal is visible: summary, log, ledger
        why = summary['refused'][self.A]
        self.assertIn('inbox is a symbolic link', why)
        self.assertIn(('alice_at_example_com/', why), summary['ignored'])
        self.assertTrue(any('REFUSED' in line and self.A in line for line in self.logged))
        row = [r for r in self.rows() if r['account'] == self.A]
        self.assertEqual([(r['status'], r['source'], r['error']) for r in row],
                         [('failed', 'check', why)])
        # the same refusal is not recorded again every hour
        self.imp.run()
        self.assertEqual(len([r for r in self.rows() if r['source'] == 'check']), 1)
        self.assert_untouched(victim, before)

    def test_scan_inbox_itself_does_not_read_through_a_link(self):
        victim = self.victim()
        self.replace_with_link(self.ap(self.A)['inbox'], victim)
        self.assertEqual(mi.scan_inbox(self.ap(self.A)['inbox']),
                         {'candidates': [], 'ignored': []})
        self.assertEqual(self.imp.scan_account(self.A)['candidates'], [])
        self.assertIn('symbolic link', self.imp.scan_account(self.A)['error'])

    def test_account_directory_symlink_is_refused(self):
        other = os.path.join(self.tmp, 'victim', 'tree')
        victim_inbox = os.path.join(other, 'inbox')
        tm.write(os.path.join(victim_inbox, 'theirs.eml'), tm.raw('v@x'))
        os.makedirs(os.path.join(other, 'done'))
        os.makedirs(os.path.join(other, 'failed'))
        before = tree(other)
        self.replace_with_link(self.ap(self.A)['base'], other)
        del self.prepared[:]
        summary = self.imp.run()
        self.assertEqual(summary['results'], [])
        self.assertIn('alice_at_example_com is a symbolic link', summary['refused'][self.A])
        self.assert_untouched(other, before)
        # nothing is created through the link either
        self.assertEqual(self.imp.ensure_dirs(), summary['refused'])
        self.assert_untouched(other, before)

    def test_incoming_symlink_is_refused(self):
        victim = self.victim()
        before = tree(victim)
        os.rmdir(self.ap(self.A)['incoming'])
        os.symlink(victim, self.ap(self.A)['incoming'])
        self.drop(self.A, 'mine.eml', tm.raw('a1@x'))
        del self.prepared[:]
        summary = self.imp.run()
        self.assertEqual(summary['results'], [])
        self.assertIn('.incoming is a symbolic link', summary['refused'][self.A])
        self.assertTrue(os.path.exists(os.path.join(self.ap(self.A)['inbox'], 'mine.eml')))
        self.assert_untouched(victim, before)

    def test_done_failed_work_and_state_links_are_refused(self):
        victim = self.victim()
        before = tree(victim)
        for key in ('done', 'failed'):
            with self.subTest(key):
                path = self.ap(self.A)[key]
                self.replace_with_link(path, victim)
                self.assertIn(f'{key} is a symbolic link', self.imp.ensure_dirs()[self.A])
                os.unlink(path)
                os.rename(path + '.real', path)
        self.assertEqual(self.imp.ensure_dirs(), {})
        for key in ('work', 'state', 'archive', 'root'):
            with self.subTest(key):
                path = self.imp.paths[key]
                if os.path.lexists(path):
                    os.rename(path, path + '.real')
                os.symlink(victim, path)
                with self.assertRaises(mi.UnsafeImportTree):
                    self.imp.ensure_dirs()
                summary = self.imp.run()
                self.assertIn('symbolic link', summary['error'])
                self.assertEqual(summary['results'], [])
                os.unlink(path)
                if os.path.lexists(path + '.real'):
                    os.rename(path + '.real', path)
        self.assert_untouched(victim, before)
        rows = [r for r in self.rows() if r['source'] == 'check' and r['account'] == '']
        self.assertEqual(len(rows), 4)                      # one per distinct refusal

    def test_a_file_where_a_directory_belongs_is_refused(self):
        inbox = self.ap(self.A)['inbox']
        os.rmdir(self.ap(self.A)['incoming'])
        os.rmdir(inbox)
        tm.write(inbox, b'not a directory')
        self.assertIn('inbox is not a directory', self.imp.ensure_dirs()[self.A])
        self.assertEqual(open(inbox, 'rb').read(), b'not a directory')

    def test_directory_of_another_owner_is_refused(self):
        me = os.getuid()
        self.assertEqual(self.make(owners={0, me}).ensure_dirs(), {})
        self.drop(self.A, 'a.eml', tm.raw('own@x'))
        with self.assertRaises(mi.UnsafeImportTree) as cm:
            self.make(owners={me + 1, me + 2})  # as if the tree were someone else's (also when the tests run as root)
        imp = self.imp
        self.assertIn(f'belongs to uid {me}', str(cm.exception))
        self.assertEqual(imp.run()['results'], [])
        self.assertTrue(os.path.exists(os.path.join(self.ap(self.A)['inbox'], 'a.eml')))

    def test_default_watch_folder_symlink_is_not_read(self):
        self.make(default_account=self.A)
        victim = os.path.join(self.tmp, 'victim', 'psts')
        tm.write(os.path.join(victim, 'theirs-20261109.pst'),
                 tm.pst_bytes({'Inbox': [tm.msg('v@x')]}))
        before = tree(victim)
        watch = self.imp.paths['watch']
        os.rmdir(self.imp.paths['incoming'])
        os.rmdir(watch)
        os.symlink(victim, watch)
        self.assertEqual(pst.scan_watch(watch), {'candidates': [], 'ignored': []})
        del self.prepared[:]
        with self.assertRaises(mi.UnsafeImportTree):
            self.imp.ensure_dirs()
        summary = self.imp.run()
        self.assertEqual(summary['results'], [])
        self.assertIn('watch is a symbolic link', summary['error'])
        self.assert_untouched(victim, before)

    def test_custom_watch_folder_must_be_real_directories_all_the_way(self):
        user_root = os.path.join(self.tmp, self.USER)
        os.makedirs(os.path.join(user_root, 'drop', 'pst'))
        self.make(watch_dir='drop/pst', default_account=self.A)
        self.assertEqual(self.imp.paths['watch'],
                         os.path.join(os.path.realpath(user_root), 'drop', 'pst'))
        tm.write(os.path.join(user_root, 'drop', 'pst', 'export-20261109.pst'),
                 tm.pst_bytes({'Inbox': [tm.msg('w@x')]}))
        self.assertEqual(self.imp.run()['results'][0]['status'], 'done')
        # the settings check: a path with a link in it, or a file, is refused
        mi.check_tree(user_root, [self.imp.paths['watch']])
        victim = self.victim()
        os.symlink(victim, os.path.join(user_root, 'drop', 'pst', '.incoming.new'))
        os.rmdir(os.path.join(user_root, 'drop', 'pst', '.incoming'))
        os.rename(os.path.join(user_root, 'drop', 'pst', '.incoming.new'),
                  os.path.join(user_root, 'drop', 'pst', '.incoming'))
        with self.assertRaises(mi.UnsafeImportTree):
            self.imp.check_user_tree()
        with self.assertRaises(mi.UnsafeImportTree):
            mi.check_tree(user_root, [os.path.join(self.tmp, 'elsewhere')])

    @unittest.skipUnless(os.path.isdir('/proc/self/fd'), 'needs /proc')
    def test_inbox_swapped_for_a_link_after_the_scan_cannot_redirect_the_pass(self):
        """The check is not a check-then-use race: the pass works through
        the directory it opened, not through the name."""
        victim = self.victim()
        before = tree(victim)
        inbox = self.ap(self.A)['inbox']
        self.drop(self.A, 'mine.eml', tm.raw('a1@x'))
        # a victim file under the same name, to be chowned / moved if the
        # pass followed the name
        tm.write(os.path.join(victim, 'mine.eml'), tm.raw('v3@x'))
        before = tree(victim)

        def swap(_seconds):
            self.replace_with_link(inbox, victim)

        self.imp.sleep, self.imp.stable_wait = swap, 1
        del self.prepared[:]
        summary = self.imp.run()
        self.assert_untouched(victim, before)
        res = summary['results'][0]
        # the unit that was scanned is the one that was handled: it left
        # the real inbox (to done/ or failed/), whatever the converter,
        # which runs as the owner on the name, made of the swapped path
        self.assertEqual(os.listdir(inbox + '.real'), ['.incoming'])
        moved = res['detail'].get('archived_as') or res['detail'].get('moved_to')
        self.assertTrue(os.path.exists(os.path.join(self.imp.paths['root'], moved)))
        self.assertEqual(open(os.path.join(self.imp.paths['root'], moved), 'rb').read(),
                         tm.raw('a1@x'))

    def test_symlink_entry_in_the_inbox_is_reported_and_never_followed(self):
        victim = self.victim()
        before = tree(victim)
        os.symlink(victim, os.path.join(self.ap(self.A)['inbox'], 'theirs'))
        os.symlink(os.path.join(victim, '1700000001.M1P1.host:2,S'),
                   os.path.join(self.ap(self.A)['inbox'], 'one.eml'))
        summary = self.imp.run()
        self.assertEqual(summary['results'], [])
        self.assertEqual(sorted(summary['ignored']), [
            ('alice_at_example_com/inbox/one.eml', 'symbolic link (not followed)'),
            ('alice_at_example_com/inbox/theirs', 'symbolic link (not followed)')])
        self.assert_untouched(victim, before)

    def test_symlink_entries_inside_an_eml_directory_are_skipped(self):
        victim = self.victim()
        before = tree(victim)
        unit = os.path.join(self.ap(self.A)['inbox'], 'Saved mail')
        tm.write(os.path.join(unit, 'one.eml'), tm.raw('d1@x'))
        tm.write(os.path.join(unit, 'sub', 'two.eml'), tm.raw('d2@x'))
        os.symlink(os.path.join(victim, '1700000001.M1P1.host:2,S'),
                   os.path.join(unit, 'linked.eml'))
        os.symlink(victim, os.path.join(unit, 'linked-dir'))
        os.symlink(victim, os.path.join(unit, 'sub', 'cur'))
        res = self.imp.run()['results'][0]
        self.assertEqual((res['status'], res['imported']), ('done', 2))
        self.assertEqual(res['detail']['skipped_links'], 3)
        got = {m for _f, m in tm.maildir_messages(self.maildir(self.A))}
        self.assertEqual(len(got), 2)
        self.assert_untouched(victim, before)

    def test_symlink_entries_inside_a_maildir_unit_are_skipped(self):
        victim = self.victim()
        before = tree(victim)
        unit = os.path.join(self.ap(self.A)['inbox'], 'OldMaildir')
        tm.make_maildir(unit, {'': [(tm.raw('m1@x'), 'cur', 'S')]})
        os.symlink(os.path.join(victim, '1700000001.M1P1.host:2,S'),
                   os.path.join(unit, 'cur', '1700000009.M1P1.host:2,S'))
        os.symlink(os.path.dirname(victim), os.path.join(unit, '.Linked'))
        res = self.imp.run()['results'][0]
        self.assertEqual((res['status'], res['imported']), ('done', 1))
        self.assert_untouched(victim, before)

    def test_retry_is_refused_when_the_inbox_is_a_link(self):
        victim = self.victim()
        before = tree(victim)
        ap = self.ap(self.A)
        tm.write(os.path.join(ap['failed'], 'again.eml'), tm.raw('r@x'))
        self.replace_with_link(ap['inbox'], victim)
        with self.assertRaises(mi.UnsafeImportTree):
            mi.retry_failed(ap, 'again.eml')
        self.assertEqual(os.listdir(ap['failed']), ['again.eml'])
        self.assert_untouched(victim, before)

    def test_retry_never_replaces_a_file_in_the_inbox(self):
        ap = self.ap(self.A)
        tm.write(os.path.join(ap['failed'], '2~again.eml'), tm.raw('old@x'))
        tm.write(os.path.join(ap['failed'], '2~again.eml.sha256'), b'x')
        tm.write(os.path.join(ap['inbox'], 'again.eml.sha256'), b'producer sidecar')
        with self.assertRaises(FileExistsError):
            mi.retry_failed(ap, '2~again.eml')
        self.assertEqual(open(os.path.join(ap['inbox'], 'again.eml.sha256'), 'rb').read(),
                         b'producer sidecar')
        self.assertEqual(sorted(os.listdir(ap['failed'])),
                         ['2~again.eml', '2~again.eml.sha256'])
        os.unlink(os.path.join(ap['inbox'], 'again.eml.sha256'))
        self.assertEqual(mi.retry_failed(ap, '2~again.eml'),
                         os.path.join(ap['inbox'], 'again.eml'))
        self.assertEqual(sorted(os.listdir(ap['inbox'])),
                         ['.incoming', 'again.eml', 'again.eml.sha256'])

    def test_is_maildir_closes_its_directory_scan(self):
        root = os.path.join(self.tmp, 'plain')
        os.makedirs(os.path.join(root, '.Sent', 'cur'))
        os.makedirs(os.path.join(root, '.Sent', 'new'))
        os.makedirs(os.path.join(root, '.Zzz'))
        with warnings.catch_warnings():
            warnings.simplefilter('error', ResourceWarning)
            self.assertTrue(mi.is_maildir(root))            # stops at the first match
            import gc
            gc.collect()


class UploadTests(Hardened):
    def upload(self, data, name='up.eml', acct=None, **kw):
        return mi.receive_upload(io.BytesIO(data), len(data), self.ap(acct or self.A),
                                 name, prepare=self.prepared.append, **kw)

    def test_upload_is_refused_when_the_inbox_is_a_link(self):
        victim = self.victim()
        before = tree(victim)
        self.replace_with_link(self.ap(self.A)['inbox'], victim)
        del self.prepared[:]
        with self.assertRaises(mi.UnsafeImportTree):
            self.upload(tm.raw('evil@x'), name='planted.eml')
        self.assert_untouched(victim, before)
        self.assertEqual(os.listdir(os.path.join(self.ap(self.A)['inbox'] + '.real',
                                                 '.incoming')), [])

    def test_upload_is_refused_when_incoming_or_the_account_dir_is_a_link(self):
        victim = self.victim()
        before = tree(victim)
        inc = self.ap(self.A)['incoming']
        os.rmdir(inc)
        os.symlink(victim, inc)
        with self.assertRaises(mi.UnsafeImportTree):
            self.upload(tm.raw('evil@x'))
        os.unlink(inc)
        self.replace_with_link(self.ap(self.A)['base'], os.path.dirname(victim))
        with self.assertRaises(mi.UnsafeImportTree):
            self.upload(tm.raw('evil@x'))
        self.assert_untouched(victim, before)

    def test_upload_needs_the_file_size_plus_the_reserve_free(self):
        data = tm.raw('big@x', body='x' * 5000)
        inbox = self.ap(self.A)['inbox']
        with patch.object(mi.shutil, 'disk_usage',
                          return_value=Usage(10 ** 9, 0, len(data) + 999)):
            with self.assertRaises(mi.InsufficientSpace) as cm:
                self.upload(data, reserve=1000)
            self.assertIn('not enough free space', str(cm.exception))
            self.assertEqual(sorted(os.listdir(inbox)), ['.incoming'])
            self.assertEqual(os.listdir(os.path.join(inbox, '.incoming')), [])
            # exactly enough: accepted
            with patch.object(mi.shutil, 'disk_usage',
                              return_value=Usage(10 ** 9, 0, len(data) + 1000)):
                path, digest = self.upload(data, reserve=1000)
        self.assertEqual(digest, hashlib.sha256(data).hexdigest())
        self.assertEqual(open(path, 'rb').read(), data)
        # the default reserve is the one the importer keeps before converting
        import inspect
        self.assertIs(inspect.signature(mi.receive_upload).parameters['reserve'].default,
                      mi.FREE_RESERVE)

    def test_upload_stops_when_the_disk_runs_low_mid_stream(self):
        data = os.urandom(64 * 1024)
        inbox = self.ap(self.A)['inbox']
        free = iter([10 ** 9, 10 ** 9, 500])        # accepted, then fine, then low

        def usage(_path):
            return Usage(10 ** 9, 0, next(free))

        stream = io.BytesIO(data)
        with patch.object(mi.shutil, 'disk_usage', usage):
            with self.assertRaises(mi.InsufficientSpace) as cm:
                mi.receive_upload(stream, len(data), self.ap(self.A), 'big.eml',
                                  chunk=4096, reserve=1000, space_check_bytes=8192)
        self.assertIn('ran low during the upload', str(cm.exception))
        self.assertLess(stream.tell(), len(data))           # it did not read on
        self.assertEqual(sorted(os.listdir(inbox)), ['.incoming'])
        self.assertEqual(os.listdir(os.path.join(inbox, '.incoming')), [])

    def test_upload_never_replaces_a_file_that_arrived_meanwhile(self):
        """A producer drops <name> (sidecar first, then the file) while a
        long upload of the same name is running."""
        inbox = self.ap(self.A)['inbox']
        data = tm.raw('upload@x', body='u' * 20000)
        theirs = tm.raw('producer@x')

        class Racing(io.BytesIO):
            def __init__(self, data, at, action):
                super().__init__(data)
                self.at, self.action = at, action

            def read(self, n=-1):
                if self.action and self.tell() >= self.at:
                    self.action()
                    self.action = None
                return super().read(n)

        for what in ('file', 'sidecar'):
            with self.subTest(what):
                def produce():
                    tm.write(os.path.join(inbox, 'same.eml.sha256'), b'their sidecar')
                    if what == 'file':
                        tm.write(os.path.join(inbox, 'same.eml'), theirs)

                with self.assertRaises(FileExistsError):
                    mi.receive_upload(Racing(data, 8192, produce), len(data),
                                      self.ap(self.A), 'same.eml', chunk=4096)
                self.assertEqual(open(os.path.join(inbox, 'same.eml.sha256'), 'rb').read(),
                                 b'their sidecar')
                if what == 'file':
                    self.assertEqual(open(os.path.join(inbox, 'same.eml'), 'rb').read(), theirs)
                else:
                    self.assertFalse(os.path.exists(os.path.join(inbox, 'same.eml')))
                self.assertEqual(os.listdir(os.path.join(inbox, '.incoming')), [])
                for n in ('same.eml', 'same.eml.sha256'):
                    if os.path.exists(os.path.join(inbox, n)):
                        os.unlink(os.path.join(inbox, n))

    def test_move_noreplace(self):
        a, b = os.path.join(self.tmp, 'a'), os.path.join(self.tmp, 'b')
        tm.write(a, b'A')
        tm.write(b, b'B')
        with self.assertRaises(FileExistsError):
            mi.move_noreplace(a, b)
        self.assertEqual((open(a, 'rb').read(), open(b, 'rb').read()), (b'A', b'B'))
        os.unlink(b)
        mi.move_noreplace(a, b)
        self.assertFalse(os.path.exists(a))
        self.assertEqual(open(b, 'rb').read(), b'A')
        # directories (no hard links) and filesystems without link()
        d1, d2 = os.path.join(self.tmp, 'd1'), os.path.join(self.tmp, 'd2')
        os.makedirs(os.path.join(d1, 'x'))
        os.makedirs(d2)
        with self.assertRaises(FileExistsError):
            mi.move_noreplace(d1, d2)
        os.rmdir(d2)
        mi.move_noreplace(d1, d2)
        self.assertEqual(os.listdir(d2), ['x'])
        tm.write(a, b'A2')
        with patch.object(mi.os, 'link', side_effect=PermissionError(1, 'no links here')):
            with self.assertRaises(FileExistsError):
                mi.move_noreplace(a, b)
            self.assertEqual(open(b, 'rb').read(), b'A')
            os.unlink(b)
            mi.move_noreplace(a, b)
        self.assertEqual(open(b, 'rb').read(), b'A2')


class LeftoverTests(Hardened):
    def old(self, path, hours):
        t = time.time() - hours * 3600
        os.utime(path, (t, t), follow_symlinks=False)

    def test_work_directories_of_a_killed_pass_are_removed(self):
        work = self.imp.paths['work']
        dead = os.path.join(work, 'run-1700000000-4242-7')
        tm.write(os.path.join(dead, 'unpack-1', 'huge.mbox'), b'x' * 4096)
        tm.write(os.path.join(dead, 'pst-2', 'out', 'Inbox', '1.eml'), tm.raw('w@x'))
        victim = self.victim()
        before = tree(victim)
        os.symlink(victim, os.path.join(dead, 'link-inside'))
        os.symlink(victim, os.path.join(work, 'run-link'))      # not a directory: left
        tm.write(os.path.join(work, 'notes.txt'), b'not ours')
        self.drop(self.A, 'a.eml', tm.raw('a1@x'))
        summary = self.imp.run()
        self.assertEqual(summary['results'][0]['status'], 'done')
        self.assertEqual(sorted(os.listdir(work)), ['notes.txt', 'run-link'])
        self.assertTrue(any('removed leftover work directory' in line for line in self.logged))
        self.assertEqual(tree(victim), before)                  # links were not followed

    def test_a_killed_pass_does_not_leave_its_unpacked_data(self):
        """End to end: the converter dies (SIGKILL: no `finally` runs); the
        next pass, under the lock, clears what it had unpacked."""
        def killed(path, typ, name, maildir, work, cache):
            tm.write(os.path.join(work, 'unpack-1', 'big'), b'x' * 100000)
            raise KeyboardInterrupt        # stands for the process ending here

        self.drop(self.A, 'a.eml', tm.raw('a1@x'))
        self.imp.convert = killed
        with self.assertRaises(KeyboardInterrupt):
            self.imp.run()
        self.assertEqual(len(os.listdir(self.imp.paths['work'])), 1)
        imp = self.make()
        res = imp.run()['results'][0]
        self.assertEqual((res['status'], res['imported']), ('done', 1))
        self.assertEqual(os.listdir(imp.paths['work']), [])
        self.assertEqual([r['status'] for r in self.rows()], ['done', 'failed'])

    def test_stale_upload_temp_files_are_removed_and_live_ones_kept(self):
        inc = self.ap(self.A)['incoming']
        stale = tm.write(os.path.join(inc, 'Old laptop.mbox.upload-4242.tmp'), b'x' * 999)
        stale_side = tm.write(stale + '.sha256.tmp', b'h')
        live = tm.write(os.path.join(inc, 'now.mbox.upload-4343.tmp'), b'being written')
        aging = tm.write(os.path.join(inc, 'slow.mbox.upload-4444.tmp'), b'slow link')
        producer = tm.write(os.path.join(inc, 'export-20261109.pst'), b'producer, arriving')
        producer_tmp = tm.write(os.path.join(inc, 'export-20261109.pst.tmp'), b'producer tmp')
        victim = self.victim()
        target = os.path.join(victim, '1700000001.M1P1.host:2,S')
        link = os.path.join(inc, 'x.upload-1.tmp')
        os.symlink(target, link)
        for p in (stale, stale_side, producer, producer_tmp, link):
            self.old(p, 30)
        self.old(aging, mi.UPLOAD_STALE_SECONDS / 3600 - 1)     # not yet old enough
        self.assertGreaterEqual(mi.UPLOAD_STALE_SECONDS, 3600)
        self.imp.run()
        self.assertEqual(sorted(os.listdir(inc)), sorted(os.path.basename(p) for p in (
            live, aging, producer, producer_tmp, link)))
        self.assertTrue(os.path.exists(target))
        self.assertEqual(len([l for l in self.logged if 'leftover upload file' in l]), 2)

    def test_cleanup_does_not_run_in_a_refused_account(self):
        victim = self.victim()
        stale = tm.write(os.path.join(victim, 'a.upload-1.tmp'), b'theirs')
        self.old(stale, 30)
        inc = self.ap(self.A)['incoming']
        os.rmdir(inc)
        os.symlink(victim, inc)
        self.imp.run()
        self.assertTrue(os.path.exists(stale))


class StaleFreezeTests(Hardened):
    """Item 1. R4 of the ADR: "When the last successful backup is older
    than the expected interval x factor (default 2x, >= 48h), pruning for
    that set is suspended and an alert/finding is raised." The run that
    prunes has, by R2, just imported a copy successfully, so the success
    that counts is the last one BEFORE this run."""

    def test_this_runs_own_success_is_not_the_last_success(self):
        self.seed_done(self.A, tm.STEADY, imported_days_ago=30)
        self.export(self.A, D(2026, 11, 9))
        summary = self.imp.run()
        self.assertEqual(summary['results'][0]['status'], 'done')
        first = summary['results'][0]['id']
        led = self.ledger()
        try:
            # what the code used to ask: "now", because this run's row is done
            self.assertEqual(led.last_done_epoch(self.A, 'export-*.pst'), self.now)
            # what R4 asks
            self.assertEqual(led.last_done_epoch(self.A, 'export-*.pst', first),
                             self.now - 30 * 86400)
        finally:
            led.close()

    def test_source_quiet_for_a_month_then_one_copy_freezes_pruning(self):
        self.seed_done(self.A, tm.STEADY, imported_days_ago=30)
        before = sorted(os.listdir(self.ap(self.A)['done']))
        self.export(self.A, D(2026, 11, 9))
        summary = self.imp.run()
        self.assertEqual(summary['results'][0]['status'], 'done')        # R2 is satisfied
        rot = summary['rotation'][0]
        self.assertEqual(rot['deleted'], [])
        self.assertIn('R4: source is stale', rot['summary'])
        self.assertIn('720 h ago', rot['summary'])
        ev = self.events()
        self.assertEqual({(e['action'], e['rule']) for e in ev}, {('refused', 'R4')})
        self.assertEqual({e['name'] for e in ev}, set(rot['refused']))
        self.assertTrue(set(before) <= set(os.listdir(self.ap(self.A)['done'])))
        self.assertEqual(len(self.done_dates(self.A)), 8)
        # the finding is raised where the cron log shows it
        self.assertTrue(any('deletion(s) refused' in l and 'R4' in l for l in self.logged))
        # the next copy arrives on schedule: the source is healthy again
        self.now += 7 * 86400
        self.export(self.A, D(2026, 11, 16), mids=('2@x',))
        rot = self.imp.run()['rotation'][0]
        self.assertTrue(rot['deleted'])
        self.assertLessEqual(len(rot['deleted']), 3)

    def test_no_success_on_record_before_this_run_freezes_too(self):
        self.seed_done(self.A, tm.STEADY, imported_days_ago=None)
        self.export(self.A, D(2026, 11, 9))
        rot = self.imp.run()['rotation'][0]
        self.assertEqual(rot['deleted'], [])
        self.assertIn('R4: no successful import', rot['summary'])

    def test_a_recent_success_before_this_run_lets_rotation_work(self):
        self.seed_done(self.A, tm.STEADY, imported_days_ago=7)
        self.export(self.A, D(2026, 11, 2))
        self.export(self.A, D(2026, 11, 9), mids=('1@x', '2@x'))
        rot = self.imp.run()['rotation'][0]
        self.assertEqual(len(rot['deleted']), 3)

    def test_expected_interval_is_the_series_cadence_when_that_is_longer(self):
        """A weekly export looked for daily is expected weekly: it is not
        "stale" every single week (which would freeze rotation for ever)."""
        daily = {'kind': 'daily'}
        weekly_copies = tm.entries(tm.weekly(D(2026, 9, 28), 7))
        self.assertEqual(mi.series_interval_seconds(weekly_copies), 7 * 86400)
        self.assertEqual(mi.stale_after_seconds(daily), 48 * 3600)
        self.assertEqual(mi.stale_after_seconds(daily, copies=weekly_copies), 14 * 86400)
        nightly = tm.entries([D(2026, 11, d) for d in range(1, 10)])
        self.assertEqual(mi.stale_after_seconds(daily, copies=nightly), 48 * 3600)
        self.assertEqual(mi.stale_after_seconds({'kind': 'weekly'}, copies=nightly), 14 * 86400)
        # old monthly keeps do not stretch it: the newest copies decide
        self.assertEqual(mi.series_interval_seconds(tm.entries(tm.STEADY)), 7 * 86400)
        self.assertEqual(mi.series_interval_seconds(tm.entries([D(2026, 11, 9)])), 0)
        self.assertEqual(mi.series_interval_seconds([]), 0)
        # a weekly series that skipped two weeks IS stale
        self.seed_done(self.A, tm.STEADY, imported_days_ago=21)
        self.export(self.A, D(2026, 11, 16))
        self.assertIn('R4', self.imp.run()['rotation'][0]['summary'])


class AppCase(unittest.TestCase):
    """The fixture of test_mail_import.AppTests, without its tests."""
    USER, A, B = tm.AppTests.USER, tm.AppTests.A, tm.AppTests.B
    setUp = tm.AppTests.setUp
    tearDown = tm.AppTests.tearDown
    run_import = tm.AppTests.run_import


class LockTests(AppCase):
    """Item 2: the status probe must not make a starting import give up."""

    def lock_fd(self):
        path = ma.get_user_config_dir(self.USER) / 'mail_import.lock'
        return os.open(str(path), os.O_RDONLY | os.O_CREAT, 0o644)

    def test_probe_is_shared_and_does_not_see_another_probe(self):
        fd = self.lock_fd()
        try:
            fcntl.flock(fd, fcntl.LOCK_SH)              # another poll, right now
            self.assertFalse(ma.mail_import_running(self.USER))
        finally:
            os.close(fd)
        fd = ma._import_lock(self.USER)                 # an import
        try:
            self.assertTrue(ma.mail_import_running(self.USER))
        finally:
            os.close(fd)
        self.assertFalse(ma.mail_import_running(self.USER))

    def test_import_start_survives_a_probe_at_the_wrong_moment(self):
        tm.write(os.path.join(self.apA['inbox'], 'a.eml'), tm.raw('lock@x'))
        fd = self.lock_fd()
        fcntl.flock(fd, fcntl.LOCK_SH)                  # a poll holds the lock...
        threading.Timer(0.3, os.close, [fd]).start()    # ...for a moment
        t0 = time.monotonic()
        summary = self.run_import()
        self.assertIsNotNone(summary, 'the import gave up: "another import is running"')
        self.assertEqual(summary['results'][0]['status'], 'done')
        self.assertGreaterEqual(time.monotonic() - t0, 0.25)

    def test_a_real_running_import_still_blocks_a_second_one(self):
        fd = ma._import_lock(self.USER)
        try:
            with patch.object(ma, 'IMPORT_LOCK_WAIT', 0.2):
                out = io.StringIO()
                with redirect_stdout(out):
                    self.assertIsNone(ma.mail_import_user(self.USER, stable_wait=0))
            self.assertIn('another import is running', out.getvalue())
        finally:
            os.close(fd)

    def test_polling_while_an_import_starts(self):
        """Many polls and many starts: no start is ever lost to a poll."""
        stop = threading.Event()

        def poll():
            while not stop.is_set():
                ma.mail_import_running(self.USER)

        threads = [threading.Thread(target=poll) for _ in range(4)]
        for t in threads:
            t.start()
        try:
            for i in range(12):
                self.assertIsNotNone(self.run_import(), i)
        finally:
            stop.set()
            for t in threads:
                t.join()

    def test_spawned_import_is_reaped(self):
        """Item 3: the detached child must be waited for, or it stays a
        zombie until the gunicorn worker is recycled."""
        waited = threading.Event()

        class FakePopen:
            def __init__(self, cmd, **kw):
                self.cmd, self.kw = cmd, kw

            def wait(self, timeout=None):
                waited.set()
                return 0

        with patch.object(ma.subprocess, 'Popen', FakePopen):
            ma._spawn_mail_import(self.USER, self.A, 'a.eml')
        self.assertTrue(waited.wait(5), 'nobody waits for the import child')

    def test_spawned_import_leaves_no_zombie(self):
        import subprocess
        import sys
        real = subprocess.Popen
        made = []

        def quick(cmd, **kw):
            made.append(real([sys.executable, '-c', 'pass'], **kw))
            return made[-1]

        with patch.object(ma.subprocess, 'Popen', quick):
            ma._spawn_mail_import(self.USER)
        deadline = time.monotonic() + 10
        while made[0].returncode is None and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertEqual(made[0].returncode, 0)         # set by the reaping wait()


class AppHardeningTests(AppCase):
    def victim(self):
        v = os.path.join(self.data, 'victim', 'acct', 'INBOX', 'cur')
        tm.write(os.path.join(v, '1700000001.M1P1.host:2,S'), tm.raw('v1@x'))
        return v

    def link_inbox(self, victim):
        self.client.get('/import')                          # creates the folders
        os.rename(self.apA['inbox'], self.apA['inbox'] + '.real')
        os.symlink(victim, self.apA['inbox'])

    def test_upload_route_refuses_a_linked_inbox(self):
        victim = self.victim()
        before = tree(victim)
        self.link_inbox(victim)
        with patch.object(ma, '_spawn_mail_import') as spawn:
            r = self.client.post('/import/upload', query_string={
                'import': '1', 'account': self.A, 'name': 'planted.eml'},
                data=tm.raw('evil@x'), content_type='application/octet-stream')
        self.assertEqual(r.status_code, 409, r.data)
        self.assertIn('symbolic link', r.get_json()['error'])
        spawn.assert_not_called()
        self.assertEqual(tree(victim), before)
        # the other account is not affected
        r = self.client.post('/import/upload', query_string={
            'account': self.B, 'name': 'fine.eml'},
            data=tm.raw('fine@x'), content_type='application/octet-stream')
        self.assertEqual(r.status_code, 200, r.data)

    def test_page_run_and_retry_show_the_refusal_and_touch_nothing(self):
        victim = self.victim()
        before = tree(victim)
        self.link_inbox(victim)
        r = self.client.get('/import')
        self.assertEqual(r.status_code, 200)
        html = r.get_data(as_text=True)
        self.assertIn('This import folder is refused', html)
        self.assertIn('inbox is a symbolic link', html)
        self.assertNotIn('1700000001.M1P1.host', html)      # the victim's files are not listed
        with patch.object(ma, '_spawn_mail_import') as spawn:
            r = self.client.post('/import/run', data={
                'account': self.A, 'file': '1700000001.M1P1.host:2,S'}, follow_redirects=True)
        spawn.assert_not_called()
        self.assertIn('Not started', r.get_data(as_text=True))
        tm.write(os.path.join(self.apA['failed'], 'again.eml'), tm.raw('r@x'))
        r = self.client.post('/import/retry', data={'account': self.A, 'file': 'again.eml'},
                             follow_redirects=True)
        self.assertIn('Could not move again.eml', r.get_data(as_text=True))
        self.assertEqual(os.listdir(self.apA['failed']), ['again.eml'])
        # a pass (cron or manual) records it in the ledger
        summary = self.run_import()
        self.assertIn(self.A, summary['refused'])
        html = self.client.get('/import').get_data(as_text=True)
        self.assertIn('(import folder)', html)
        self.assertEqual(tree(victim), before)

    def test_upload_route_says_507_when_the_disk_is_too_full(self):
        self.client.get('/import')
        data = tm.raw('big@x', body='x' * 3000)
        with patch.object(mi.shutil, 'disk_usage',
                          return_value=Usage(10 ** 12, 0, mi.FREE_RESERVE + len(data) - 1)):
            r = self.client.post('/import/upload', query_string={
                'account': self.A, 'name': 'big.eml'},
                data=data, content_type='application/octet-stream')
        self.assertEqual(r.status_code, 507, r.data)
        self.assertIn('not enough free space', r.get_json()['error'])
        self.assertEqual(os.listdir(self.apA['inbox']), ['.incoming'])
        self.assertEqual(os.listdir(self.apA['incoming']), [])

    def test_watch_folder_setting_with_a_file_in_its_path_is_not_saved(self):
        root = os.path.join(self.data, self.USER)
        tm.write(os.path.join(root, 'drop'), b'a file, not a folder')
        r = self.client.post('/import/config', data={'watch_dir': 'drop'},
                             follow_redirects=True)
        self.assertIn('Not saved', r.get_data(as_text=True))
        self.assertFalse((ma.get_user_config_dir(self.USER) / ma.IMPORT_CONFIG).exists())
        os.unlink(os.path.join(root, 'drop'))
        os.makedirs(os.path.join(root, 'drop'))
        r = self.client.post('/import/config', data={'watch_dir': 'drop'},
                             follow_redirects=True)
        self.assertIn('Import settings saved', r.get_data(as_text=True))

    def test_compress_hook_removes_nothing_with_the_service_s_own_hands(self):
        """The archive compressor runs every step as the mailbox owner; the
        service (or root) does not unlink a name in a folder the owner can
        rearrange."""
        import inspect
        src = inspect.getsource(ma._import_compress_as_user)
        self.assertNotIn('os.unlink', src)
        self.assertNotIn('os.remove', src)
        compress = ma._import_compress_as_user(self.USER)
        if compress is None:
            self.skipTest('zstd is not installed')
        p = tm.write(os.path.join(self.data, 'c', 'x.mbox'), b'From a\n' * 500)
        z = compress(p)
        self.assertEqual((z, os.listdir(os.path.dirname(p))), (p + '.zst', ['x.mbox.zst']))
        tm.write(p, b'again')
        with patch.object(ma.shutil, 'which', return_value='/bin/false'):
            failing = ma._import_compress_as_user(self.USER)
        os.unlink(z)
        with self.assertRaises(Exception):
            failing(p)
        self.assertEqual(os.listdir(os.path.dirname(p)), ['x.mbox'])    # original kept


class CorruptIndexTests(AppCase):
    """Item 5."""

    def db(self):
        return os.path.join(self.data, self.USER, '.search_index.db')

    def corrupt(self):
        self.client.get('/import')
        tm.write(os.path.join(self.apA['inbox'], 'a.eml'), tm.raw('c1@x', subject='okapi'))
        self.assertEqual(self.run_import()['results'][0]['status'], 'done')
        junk = b'this is not an sqlite database' * 200
        for suffix in ('-wal', '-shm'):
            if os.path.exists(self.db() + suffix):
                os.unlink(self.db() + suffix)
        with open(self.db(), 'wb') as f:
            f.write(junk)
        return junk

    def test_import_page_and_status_degrade_instead_of_500(self):
        self.corrupt()
        r = self.client.get('/import')
        self.assertEqual(r.status_code, 200)
        html = r.get_data(as_text=True)
        self.assertIn('The import history cannot be read', html)
        self.assertIn('Rebuild the search index', html)
        self.assertIn('action="/index/rebuild"', html)
        self.assertIn('Upload a file', html)                 # the rest of the page is there
        r = self.client.get('/api/import/status')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()['rows'], [])
        self.assertIn('rebuild', r.get_json()['error'])

    def test_rebuild_moves_the_damaged_file_aside_and_starts_fresh(self):
        junk = self.corrupt()
        r = self.client.post('/index/rebuild')
        self.assertEqual(r.status_code, 302)
        user_dir = os.path.join(self.data, self.USER)
        aside = [n for n in os.listdir(user_dir) if n.startswith('.search_index.db.corrupt-')]
        self.assertEqual(len(aside), 1)
        self.assertRegex(aside[0], r'^\.search_index\.db\.corrupt-\d{8}-\d{6}$')
        with open(os.path.join(user_dir, aside[0]), 'rb') as f:
            self.assertEqual(f.read(), junk)                 # kept, not deleted
        # the new database indexes the mail and takes ledger rows again
        hits = search_index.search_fts(self.USER, self.data, 'okapi')
        self.assertEqual(len(hits['results'] if isinstance(hits, dict) else hits), 1)
        r = self.client.get('/import')
        self.assertEqual(r.status_code, 200)
        self.assertNotIn('The import history cannot be read', r.get_data(as_text=True))
        tm.write(os.path.join(self.apA['inbox'], 'b.eml'), tm.raw('c2@x'))
        self.assertEqual(self.run_import()['results'][0]['status'], 'done')

    def test_rebuild_of_a_healthy_database_still_keeps_the_ledger(self):
        self.client.get('/import')
        tm.write(os.path.join(self.apA['inbox'], 'a.eml'), tm.raw('c1@x'))
        self.run_import()
        search_index.rebuild_index(self.USER, self.data)
        user_dir = os.path.join(self.data, self.USER)
        self.assertEqual([n for n in os.listdir(user_dir) if 'corrupt' in n], [])
        led = ma.open_import_ledger(self.USER)
        try:
            self.assertEqual([r['status'] for r in led.rows()], ['done'])
        finally:
            led.close()

    def test_a_damaged_page_inside_a_real_database_is_also_set_aside(self):
        self.client.get('/import')
        for i in range(40):
            tm.write(os.path.join(self.apA['inbox'], f'm{i}.eml'),
                     tm.raw(f'bulk{i}@x', body='lorem ipsum ' * 400))
        self.run_import()
        conn = sqlite3.connect(self.db())
        conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        conn.close()
        size = os.path.getsize(self.db())
        with open(self.db(), 'r+b') as f:                    # header intact, pages ruined
            f.seek(4096)
            f.write(b'\xff' * (size - 4096))
        search_index.rebuild_index(self.USER, self.data)
        user_dir = os.path.join(self.data, self.USER)
        self.assertEqual(len([n for n in os.listdir(user_dir)
                              if n.startswith('.search_index.db.corrupt-')]), 1)
        conn = sqlite3.connect(self.db())
        try:
            self.assertEqual(conn.execute('PRAGMA quick_check').fetchone()[0], 'ok')
            self.assertEqual(conn.execute('SELECT count(*) FROM seen_ids').fetchone()[0], 40)
        finally:
            conn.close()


if __name__ == '__main__':
    unittest.main()
