"""The mail importer on Windows (1.3.1), proved on Linux and on Windows.

On Linux every test runs the importer with `platform_compat.IS_WINDOWS`
set and with fcntl / pwd / grp / resource made unimportable, so the code
takes the paths it takes on Windows: no directory descriptors, no
O_NOFOLLOW, no owner checks, the "!" Maildir info separator, sanitised
file names, readpst / msgconvert looked up on PATH. On Windows the same
tests run against the real thing.

Nothing here needs /tmp, a symbolic link (the one test that wants one
skips where it cannot be made), a shell script or a mail account. Every
fixture is generated.
"""

import ast
import datetime
import gzip
import hashlib
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
import zipfile
from unittest.mock import patch

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import platform_compat as compat   # noqa: E402
import pst_import as pst           # noqa: E402
import mail_import as mi           # noqa: E402
import search_index                # noqa: E402

POSIX_ONLY = ('fcntl', 'pwd', 'grp', 'resource')
PORTED = ('mail_import.py', 'pst_import.py', 'bulk_sync.py', 'bulk_sync_routes.py')
D = datetime.date
CFB = bytes.fromhex('D0CF11E0A1B11AE1')
# What a rotation on 2026-10-26 leaves behind: 4 recent + Sep, Aug, Jul.
STEADY = [D(2026, 7, 27), D(2026, 8, 31), D(2026, 9, 28), D(2026, 10, 5),
          D(2026, 10, 12), D(2026, 10, 19), D(2026, 10, 26)]


def raw(mid, subject='hello', body='body text'):
    head = f'Message-ID: <{mid}>\r\n' if mid else ''
    return (f'{head}From: a@example.com\r\nTo: b@example.com\r\nSubject: {subject}\r\n'
            f'Date: Mon, 02 Nov 2026 10:00:00 +0000\r\n\r\n{body}\r\n').encode()


def mbox_bytes(messages):
    out = []
    for m in messages:
        out.append(b'From MAILER-DAEMON Mon Nov  2 10:00:00 2026\n'
                   + m.replace(b'\r\n', b'\n') + b'\n')
    return b''.join(out)


def emlx_bytes(data):
    plist = (b'<?xml version="1.0" encoding="UTF-8"?>\n<plist version="1.0"><dict>'
             b'<key>flags</key><integer>8590195713</integer></dict></plist>\n')
    return str(len(data)).encode() + b'\n' + data + plist


def msg_bytes(data):
    return (CFB + b'\0' * 504 + '__substg1.0_0037001F'.encode('utf-16-le') + b'\0' * 64
            + b'\n--EML--\n' + data)


def zip_bytes(members):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return buf.getvalue()


def tar_bytes(members, mode='w:gz'):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode=mode) as tf:
        for name, data in members.items():
            ti = tarfile.TarInfo(name)
            ti.size = len(data)
            tf.addfile(ti, io.BytesIO(data))
    return buf.getvalue()


def write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'wb') as f:
        f.write(data)
    return path


def read_text(path):
    with open(path, encoding='utf-8') as f:
        return f.read()


def delivered(maildir):
    """[(folder with '/', file name)] of every message in a Maildir tree."""
    out = []
    for root, _dirs, files in os.walk(maildir):
        if os.path.basename(root) in ('cur', 'new'):
            rel = os.path.relpath(os.path.dirname(root), maildir).replace(os.sep, '/')
            out.extend((rel, f) for f in files)
    return sorted(out)


def all_names(top):
    names = []
    for _root, dirs, files in os.walk(top):
        names += dirs + files
    return names


class WindowsCase(unittest.TestCase):
    """The Windows code paths: IS_WINDOWS set, the POSIX-only modules not
    importable, an empty PATH (no readpst, no msgconvert, no zstd)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='ma-win-import-')
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.bin = os.path.join(self.tmp, 'empty-path')
        os.mkdir(self.bin)
        for p in (patch.object(compat, 'IS_WINDOWS', True),
                  patch.dict(sys.modules, {name: None for name in POSIX_ONLY}),
                  patch.dict(os.environ, {'PATH': self.bin})):
            p.start()
            self.addCleanup(p.stop)


class Flow(WindowsCase):
    USER = 'me'
    A = 'alice@example.com'
    B = 'bob.smith@example.org'

    def setUp(self):
        super().setUp()
        self.data = os.path.join(self.tmp, 'MailArchiver')
        os.mkdir(self.data)
        self.now = time.mktime((2026, 11, 9, 7, 0, 0, 0, 0, -1))
        self.indexed = []
        self.imp = mi.UserImporter(
            self.data, self.USER, [self.A, self.B],
            pst.normalize_config({}, [self.A, self.B]),
            convert=self.convert, prepare=lambda p: None, index=self.indexed.append,
            open_ledger=self.ledger, log=lambda m: None, now=lambda: self.now,
            sleep=lambda s: None, stable_wait=0)
        self.imp.ensure_dirs()

    def convert(self, path, typ, name, maildir, work, cache):
        # the tools by their plain names: found on PATH, or not at all
        return mi.convert_in_process(path, typ, name, maildir, work, cache,
                                     readpst='readpst', msgconvert='msgconvert')

    def ledger(self):
        conn = sqlite3.connect(os.path.join(self.data, 'ledger.sqlite'))
        search_index.ensure_import_ledger(conn)
        return mi.Ledger(conn)

    def rows(self):
        led = self.ledger()
        try:
            return led.rows(500)
        finally:
            led.close()

    def events(self):
        led = self.ledger()
        try:
            return led.events(500)
        finally:
            led.close()

    def ap(self, acct):
        return self.imp.account_paths(acct)

    def drop(self, acct, name, data):
        return write(os.path.join(self.ap(acct)['inbox'], *name.split('/')), data)

    def maildir(self, acct):
        return os.path.join(self.data, self.USER, pst.safe_account_name(acct))

    def run_one(self):
        results = self.imp.run()['results']
        self.assertEqual(len(results), 1, results)
        return results[0]

    def assert_windows_names(self, acct, count):
        """Every delivered message is marked read, carries the Windows
        info separator, and nothing anywhere under the data directory has
        a colon in its name."""
        got = delivered(self.maildir(acct))
        self.assertEqual(len(got), count, got)
        for _folder, fn in got:
            self.assertIn('!2,', fn)
            self.assertIn('S', compat.maildir_flags(fn))
            self.assertNotIn(':', fn)
        self.assertEqual([n for n in all_names(self.data) if ':' in n], [])
        return got


# ---------------------------------------------------------------------------
# The modules themselves
# ---------------------------------------------------------------------------

class ImportabilityTests(unittest.TestCase):
    def test_no_posix_only_module_is_imported_anywhere_in_the_ported_files(self):
        for fname in PORTED:
            with open(os.path.join(ROOT, fname), encoding='utf-8') as f:
                tree = ast.parse(f.read())
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.Import):
                    names = [a.name.split('.')[0] for a in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [(node.module or '').split('.')[0]]
                bad = sorted(set(names) & set(POSIX_ONLY))
                self.assertEqual(bad, [], f'{fname}:{getattr(node, "lineno", 0)} imports {bad}')

    def test_the_four_modules_import_with_the_posix_modules_blocked(self):
        """A fresh interpreter: platform_compat is told it is on Windows,
        then fcntl / pwd / grp / resource are made unimportable, then the
        four modules are imported and touched."""
        code = (
            'import sys\n'
            f'sys.path.insert(0, {ROOT!r})\n'
            'import platform_compat\n'
            'platform_compat.IS_WINDOWS = True\n'
            f'for name in {POSIX_ONLY!r}:\n'
            '    sys.modules[name] = None\n'
            '    setattr(platform_compat, name, None)\n'
            'import pst_import, mail_import, bulk_sync, bulk_sync_routes\n'
            'assert pst_import.maildir_info("S") == "!2,S", pst_import.maildir_info("S")\n'
            'assert mail_import.detect_unit(sys.argv[1])[0] == "directory"\n'
            'assert bulk_sync.validate_folder("[Gmail]/All Mail")\n'
            'assert callable(bulk_sync_routes.register)\n'
            'print("IMPORTED-OK")\n')
        with tempfile.TemporaryDirectory() as d:
            res = subprocess.run([sys.executable, '-c', code, d], capture_output=True,
                                 text=True, timeout=120)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn('IMPORTED-OK', res.stdout)

    def test_linux_names_are_unchanged(self):
        if os.name == 'nt':
            self.skipTest('this asserts the Linux on-disk names')
        self.assertEqual(compat.maildir_info_separator(), ':')
        self.assertEqual(pst.maildir_info('S'), ':2,S')
        self.assertEqual(pst.maildir_info('SRx'), ':2,RS')
        self.assertEqual(compat.safe_filename_component('Re: a? <b>'), 'Re: a? <b>')
        self.assertEqual(pst.sanitize_folder_component('Re: a?'), 'Re: a?')
        self.assertEqual(pst.pst_missing_message(), pst.PST_MISSING)
        self.assertEqual(mi.msg_missing_message(), mi.MSG_MISSING)
        self.assertEqual(mi.tool_notes(), {'readpst': '', 'msgconvert': ''})


class NameTests(WindowsCase):
    def test_maildir_info_uses_the_windows_separator(self):
        self.assertEqual(compat.maildir_info_separator(), '!')
        self.assertEqual(pst.maildir_info('S'), '!2,S')
        self.assertEqual(pst.maildir_info('SFz'), '!2,FS')
        self.assertEqual(pst._maildir_uniq('1700000000.M1P2Q3.host!2,S'),
                         '1700000000.M1P2Q3.host')
        # a source is read with either separator
        self.assertEqual(compat.maildir_flags('1.M1.host!2,FS'), 'FS')
        self.assertEqual(compat.maildir_flags('1.M1.host:2,RS'), 'RS')
        self.assertIsNone(compat.maildir_flags('1.M1.host'))

    def test_names_ntfs_refuses_are_made_safe(self):
        safe = compat.safe_filename_component
        self.assertEqual(safe('1700000001.M1P1.host:2,FS'), '1700000001.M1P1.host!2,FS')
        self.assertEqual(safe('Re: plans?'), 'Re_ plans_')
        self.assertEqual(safe('a<b>c|d"e*f'), 'a_b_c_d_e_f')
        self.assertNotEqual(safe('NUL'), 'NUL')
        self.assertNotEqual(safe('con.eml'), 'con.eml')
        self.assertFalse(safe('trailing. ').endswith(('.', ' ')))
        self.assertEqual(safe('plain name (1).eml'), 'plain name (1).eml')
        for name in ('Re: plans?', 'aux', 'x\\y', 'a/b', 'tab\there'):
            got = pst.sanitize_folder_component(name)
            self.assertEqual(compat.safe_filename_component(got), got, name)
            self.assertFalse(set(got) & set('<>:"/\\|?*'), got)

    def test_archive_member_names_are_made_safe_and_cannot_leave(self):
        self.assertEqual(mi._safe_member('box/cur/1.M1.host:2,S'),
                         ['box', 'cur', '1.M1.host!2,S'])
        self.assertEqual(mi._safe_member('Re: x/what?.eml'), ['Re_ x', 'what_.eml'])
        for bad in ('../x.eml', 'a/../../x.eml', '/abs/x.eml', 'C:\\x.eml', '..\\x.eml'):
            self.assertIsNone(mi._safe_member(bad), bad)

    def test_tool_messages_are_the_windows_ones(self):
        self.assertEqual(pst.pst_missing_message(), pst.PST_MISSING_WINDOWS)
        self.assertEqual(mi.msg_missing_message(), mi.MSG_MISSING_WINDOWS)
        # (the plain program names: other test modules point these at stand-ins)
        with patch.object(pst, 'READPST', 'readpst'), \
                patch.object(mi, 'MSGCONVERT', 'msgconvert'):
            self.assertEqual(mi.tool_notes(), {'readpst': pst.PST_MISSING_WINDOWS,
                                               'msgconvert': mi.MSG_MISSING_WINDOWS})
        for text in (pst.PST_MISSING_WINDOWS, mi.MSG_MISSING_WINDOWS):
            self.assertIn('not found on PATH', text)
            self.assertNotIn('Debian', text)


# ---------------------------------------------------------------------------
# Import, end to end
# ---------------------------------------------------------------------------

class MboxTests(Flow):
    def test_mbox_end_to_end(self):
        self.drop(self.A, 'Archive 2019.mbox',
                  mbox_bytes([raw('1@x'), raw('2@x'), raw('3@x')]))
        row = self.run_one()
        self.assertEqual((row['status'], row['detected_type'], row['imported'],
                          row['duplicates'], row['failed'], row['account']),
                         ('done', 'mbox', 3, 0, 0, self.A))
        self.assertEqual(row['target_prefix'], 'Imported/mbox/')
        self.assertEqual(len(row['sha256']), 64)
        self.assertEqual(row['detail']['folders'], {'Imported/mbox/Archive 2019': 3})
        got = self.assert_windows_names(self.A, 3)
        self.assertEqual({f for f, _ in got}, {'Imported/mbox/Archive 2019'})
        self.assertEqual(delivered(self.maildir(self.B)), [])
        self.assertEqual(self.indexed, [self.A])
        # the ledger row is in the database, the file is archived with a sidecar
        self.assertEqual([(r['original_name'], r['status']) for r in self.rows()],
                         [('Archive 2019.mbox', 'done')])
        self.assertEqual(os.listdir(self.ap(self.A)['inbox']), ['.incoming'])
        self.assertEqual(sorted(os.listdir(self.ap(self.A)['done'])),
                         ['Archive 2019.mbox', 'Archive 2019.mbox.sha256'])
        self.assertEqual(row['detail']['archived_as'],
                         os.path.join('alice_at_example_com', 'done', 'Archive 2019.mbox'))
        self.assertTrue(row['detail']['archive_verified'])
        side = read_text(os.path.join(self.ap(self.A)['done'], 'Archive 2019.mbox.sha256'))
        self.assertEqual(side, f'{row["sha256"]}  Archive 2019.mbox\n')
        # nothing left behind in the work directory
        self.assertEqual(os.listdir(self.imp.paths['work']), [])

    def test_the_same_file_dropped_again_is_skipped(self):
        data = mbox_bytes([raw('1@x'), raw('2@x')])
        self.drop(self.A, 'box.mbox', data)
        self.assertEqual(self.run_one()['imported'], 2)
        self.drop(self.A, 'box.mbox', data)
        row = self.run_one()
        self.assertEqual(row['status'], 'skipped')
        self.assertIn('identical to', row['error'])
        failed = self.ap(self.A)['failed']
        self.assertEqual(sorted(os.listdir(failed)), ['box.mbox', 'box.mbox.reason.txt'])
        self.assertIn('already imported', read_text(os.path.join(failed, 'box.mbox.reason.txt')))
        self.assert_windows_names(self.A, 2)

    def test_messages_already_in_the_account_are_not_added_twice(self):
        self.drop(self.A, 'one.mbox', mbox_bytes([raw('1@x'), raw('2@x'), raw(None, 'no id')]))
        self.assertEqual(self.run_one()['imported'], 3)
        # other file, other format, same mail plus one new message
        self.drop(self.A, 'two.zip', zip_bytes({
            'a.eml': raw('1@x'), 'b.eml': raw('2@x'), 'c.eml': raw(None, 'no id'),
            'd.eml': raw('4@x')}))
        row = self.run_one()
        self.assertEqual((row['status'], row['imported'], row['duplicates']), ('done', 1, 3))
        self.assert_windows_names(self.A, 4)
        # the dedupe cache (written at the start of the second import, keyed
        # by the part of the file name left of the "!") is there and is JSON
        cache = os.path.join(self.imp.paths['state'], 'alice_at_example_com.ids.json')
        keys = json.loads(read_text(cache))
        self.assertEqual(len(keys), 3)
        self.assertFalse([k for k in keys if '!' in k or ':' in k])

    def test_compressed_mbox(self):
        self.drop(self.A, 'old.mbox.gz', gzip.compress(mbox_bytes([raw('1@x'), raw('2@x')])))
        row = self.run_one()
        self.assertEqual((row['status'], row['detected_type'], row['imported']),
                         ('done', 'gzip', 2))
        self.assertEqual({f for f, _ in self.assert_windows_names(self.A, 2)},
                         {'Imported/mbox/old'})


class EmlTests(Flow):
    def test_directory_of_eml_end_to_end(self):
        unit = os.path.join(self.ap(self.A)['inbox'], 'letters')
        write(os.path.join(unit, 'a.eml'), raw('1@x'))
        write(os.path.join(unit, 'b.eml'), raw('2@x'))
        write(os.path.join(unit, '2024', 'c.eml'), raw('3@x'))
        write(os.path.join(unit, 'notes.txt'), b'not mail at all\n')
        row = self.run_one()
        self.assertEqual((row['status'], row['detected_type'], row['imported']),
                         ('done', 'directory', 3))
        self.assertEqual(row['target_prefix'], 'Imported/eml/')
        self.assertEqual(row['detail']['folders'],
                         {'Imported/eml/letters': 2, 'Imported/eml/letters/2024': 1})
        self.assertEqual(row['detail']['ignored'], 1)
        self.assert_windows_names(self.A, 3)
        # the directory itself is archived (no sidecar for a directory)
        self.assertEqual(os.listdir(self.ap(self.A)['done']), ['letters'])
        self.assertEqual(len(row['sha256']), 64)
        # dropped again: the ledger knows the tree by its hash
        write(os.path.join(unit, 'a.eml'), raw('1@x'))
        write(os.path.join(unit, 'b.eml'), raw('2@x'))
        write(os.path.join(unit, '2024', 'c.eml'), raw('3@x'))
        write(os.path.join(unit, 'notes.txt'), b'not mail at all\n')
        self.assertEqual(self.run_one()['status'], 'skipped')
        self.assert_windows_names(self.A, 3)

    def test_single_eml_and_emlx(self):
        self.drop(self.A, 'one.eml', raw('1@x'))
        self.drop(self.B, '4711.emlx', emlx_bytes(raw('2@x')))
        results = {r['original_name']: r for r in self.imp.run()['results']}
        self.assertEqual((results['one.eml']['status'], results['one.eml']['detected_type']),
                         ('done', 'eml'))
        self.assertEqual((results['4711.emlx']['status'],
                          results['4711.emlx']['detected_type']), ('done', 'emlx'))
        self.assertEqual({f for f, _ in self.assert_windows_names(self.A, 1)},
                         {'Imported/eml'})
        self.assertEqual({f for f, _ in self.assert_windows_names(self.B, 1)},
                         {'Imported/emlx'})

    def test_tar_gz_and_plain_tar_of_eml(self):
        self.drop(self.A, 'home.tar.gz', tar_bytes({'mail/a.eml': raw('1@x'),
                                                    'mail/b.eml': raw('2@x')}))
        self.drop(self.B, 'home.tar', tar_bytes({'c.eml': raw('3@x')}, mode='w'))
        results = {r['original_name']: r for r in self.imp.run()['results']}
        self.assertEqual((results['home.tar.gz']['status'], results['home.tar.gz']['imported']),
                         ('done', 2))
        self.assertEqual((results['home.tar']['status'], results['home.tar']['imported'],
                          results['home.tar']['detected_type']), ('done', 1, 'tar'))
        self.assert_windows_names(self.A, 2)
        self.assert_windows_names(self.B, 1)


class MaildirTests(Flow):
    def test_maildir_directory_with_the_local_separator(self):
        """A Maildir on this computer's disk: its files carry the
        separator the platform allows. Maildir++ folders become folders."""
        unit = os.path.join(self.ap(self.A)['inbox'], 'OldMail')
        for sub in ('cur', 'new', 'tmp'):
            os.makedirs(os.path.join(unit, sub))
            os.makedirs(os.path.join(unit, '.Sent.2024', sub))
        write(os.path.join(unit, 'cur', '1700000001.M1P1.host' + compat.maildir_info('FS')),
              raw('1@x'))
        write(os.path.join(unit, 'new', '1700000002.M2P1.host'), raw('2@x'))
        write(os.path.join(unit, '.Sent.2024', 'cur',
                           '1700000003.M3P1.host' + compat.maildir_info('RS')), raw('3@x'))
        row = self.run_one()
        self.assertEqual((row['status'], row['detected_type'], row['imported']),
                         ('done', 'maildir', 3))
        got = self.assert_windows_names(self.A, 3)
        self.assertEqual(sorted({f for f, _ in got}),
                         ['Imported/Maildir/INBOX', 'Imported/Maildir/Sent/2024'])
        flags = sorted(compat.maildir_flags(fn) for _f, fn in got)
        self.assertEqual(flags, ['FS', 'RS', 'S'])          # flags kept, all read

    def test_zip_of_a_maildir_whose_names_hold_colons(self):
        """`:2,` cannot exist on NTFS, but it can be a name inside a zip:
        extraction renames, the flags survive."""
        self.drop(self.A, 'backup.zip', zip_bytes({
            'box/cur/1700000001.M1P1.host:2,FS': raw('1@x'),
            'box/cur/1700000002.M2P1.host:2,S': raw('2@x'),
            'box/cur/1700000003.M3P1.host:2,': raw('3@x'),
            'box/.Lists/cur/1700000004.M4P1.host:2,RS': raw('4@x'),
        }))
        row = self.run_one()
        self.assertEqual((row['status'], row['detected_type'], row['imported'], row['failed']),
                         ('done', 'zip', 4, 0))
        got = self.assert_windows_names(self.A, 4)
        self.assertEqual(sorted({f for f, _ in got}),
                         ['Imported/Maildir/box/INBOX', 'Imported/Maildir/box/Lists'])
        self.assertEqual(sorted(compat.maildir_flags(fn) for _f, fn in got),
                         ['FS', 'RS', 'S', 'S'])

    def test_archive_names_that_ntfs_refuses_and_that_differ_only_by_case(self):
        self.drop(self.A, 'odd.zip', zip_bytes({
            'Re: plans/what?.eml': raw('1@x'),
            'Mail/a.eml': raw('2@x'),
            'mail/a.eml': raw('3@x'),           # one folder on NTFS, two on ext4
            'aux.eml': raw('4@x'),
            'ends with dot./b.eml': raw('5@x'),
        }))
        row = self.run_one()
        self.assertEqual((row['status'], row['imported'], row['failed']), ('done', 5, 0))
        got = self.assert_windows_names(self.A, 5)
        folders = {f for f, _ in got}
        self.assertIn('Imported/eml/Re_ plans', folders)
        for folder in folders:
            self.assertFalse(set(folder) & set('<>:"\\|?*'), folder)
            self.assertFalse(any(part.endswith(('.', ' ')) for part in folder.split('/')),
                             folder)

    def test_archive_members_cannot_leave_the_work_directory(self):
        outside = os.path.join(self.data, 'evil.eml')
        self.drop(self.A, 'trap.zip', zip_bytes({
            '../../../../evil.eml': raw('9@x'),
            '..\\..\\evil.eml': raw('8@x'),
            'ok.eml': raw('1@x'),
        }))
        row = self.run_one()
        self.assertEqual((row['status'], row['imported']), ('partial', 1))
        self.assertEqual(row['detail']['refused_members'], 2)
        self.assertFalse(os.path.exists(outside))
        self.assertEqual([n for n in all_names(self.tmp) if n == 'evil.eml'], [])
        self.assert_windows_names(self.A, 1)

    def test_a_link_in_the_tar_is_refused_not_followed(self):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode='w:gz') as tf:
            data = raw('1@x')
            ti = tarfile.TarInfo('ok.eml')
            ti.size = len(data)
            tf.addfile(ti, io.BytesIO(data))
            link = tarfile.TarInfo('sym.eml')
            link.type, link.linkname = tarfile.SYMTYPE, os.path.join(self.data, 'x')
            tf.addfile(link)
        self.drop(self.A, 'home.tar.gz', buf.getvalue())
        row = self.run_one()
        self.assertEqual((row['status'], row['imported']), ('partial', 1))
        self.assertEqual(row['detail']['refused_members'], 1)


# ---------------------------------------------------------------------------
# PST / OST and .msg need programs that may not be there
# ---------------------------------------------------------------------------

class MissingToolTests(Flow):
    def test_pst_without_readpst_is_refused_with_the_message(self):
        self.drop(self.A, 'outlook.pst', b'!BDN' + b'\0' * 600)
        row = self.run_one()
        self.assertEqual((row['status'], row['detected_type'], row['imported']),
                         ('failed', 'pst', 0))
        self.assertEqual(row['error'], pst.PST_MISSING_WINDOWS)
        failed = self.ap(self.A)['failed']
        self.assertEqual(sorted(os.listdir(failed)),
                         ['outlook.pst', 'outlook.pst.reason.txt'])
        reason = read_text(os.path.join(failed, 'outlook.pst.reason.txt'))
        self.assertIn(pst.PST_MISSING_WINDOWS, reason)
        self.assertIn(self.A, reason)
        self.assertEqual(row['detail']['moved_to'],
                         os.path.join('alice_at_example_com', 'failed', 'outlook.pst'))
        self.assertEqual([(f['name'], pst.PST_MISSING_WINDOWS in f['reason'])
                          for f in mi.list_failed(failed)], [('outlook.pst', True)])
        self.assertEqual(delivered(self.maildir(self.A)), [])
        self.assertEqual(os.listdir(self.ap(self.A)['inbox']), ['.incoming'])
        self.assertEqual(os.listdir(self.imp.paths['work']), [])
        self.assertEqual([(r['status'], r['error']) for r in self.rows()],
                         [('failed', pst.PST_MISSING_WINDOWS)])

    def test_ost_named_file_is_refused_the_same_way(self):
        self.drop(self.A, 'cache.ost', b'!BDN' + b'\0' * 600)
        row = self.run_one()
        self.assertEqual((row['status'], row['error']), ('failed', pst.PST_MISSING_WINDOWS))

    def test_msg_without_msgconvert_is_refused_with_the_message(self):
        self.drop(self.A, 'letter.msg', msg_bytes(raw('1@x')))
        row = self.run_one()
        self.assertEqual((row['status'], row['detected_type']), ('failed', 'msg'))
        self.assertEqual(row['error'], mi.MSG_MISSING_WINDOWS)
        failed = self.ap(self.A)['failed']
        self.assertEqual(sorted(os.listdir(failed)),
                         ['letter.msg', 'letter.msg.reason.txt'])
        self.assertIn(mi.MSG_MISSING_WINDOWS,
                      read_text(os.path.join(failed, 'letter.msg.reason.txt')))
        self.assertEqual(delivered(self.maildir(self.A)), [])

    def test_a_refused_pst_does_not_stop_the_mail_next_to_it(self):
        self.drop(self.A, 'outlook.pst', b'!BDN' + b'\0' * 600)
        self.drop(self.A, 'box.mbox', mbox_bytes([raw('1@x')]))
        results = {r['original_name']: r for r in self.imp.run()['results']}
        self.assertEqual(results['outlook.pst']['status'], 'failed')
        self.assertEqual((results['box.mbox']['status'], results['box.mbox']['imported']),
                         ('done', 1))
        self.assert_windows_names(self.A, 1)

    def test_msg_inside_a_zip_is_counted_not_fatal(self):
        self.drop(self.A, 'mixed.zip', zip_bytes({'a.eml': raw('1@x'),
                                                  'b.msg': msg_bytes(raw('2@x'))}))
        row = self.run_one()
        self.assertEqual((row['status'], row['imported'], row['failed']), ('partial', 1, 1))
        self.assertTrue(any(mi.MSG_MISSING_WINDOWS in e for e in row['detail']['errors']))

    def test_moved_back_after_the_tool_is_installed_it_is_tried_again(self):
        self.drop(self.A, 'letter.msg', msg_bytes(raw('1@x')))
        self.assertEqual(self.run_one()['status'], 'failed')
        back = mi.retry_failed(self.ap(self.A), 'letter.msg')
        self.assertEqual(back, os.path.join(self.ap(self.A)['inbox'], 'letter.msg'))
        self.assertEqual(os.listdir(self.ap(self.A)['failed']), [])
        self.assertTrue(os.path.isfile(back))
        for bad in ('..\\inbox\\x', 'a\\b', 'x:y', 'letter.msg.reason.txt', '../x'):
            with self.assertRaises(ValueError):
                mi.retry_failed(self.ap(self.A), bad)

    @pytest.mark.skipif(os.name == 'nt', reason='the stand-in readpst is a "#!" script, '
                        'which Windows cannot start; a real readpst.exe on PATH takes '
                        'the same code path')
    def test_pst_is_imported_when_readpst_is_on_path(self):
        tool = os.path.join(self.bin, 'readpst')
        shutil.copy(os.path.join(HERE, 'fake_readpst.py'), tool)
        os.chmod(tool, 0o755)
        real_path = os.pathsep.join([self.bin, os.path.dirname(sys.executable),
                                     '/usr/bin', '/bin'])
        with patch.dict(os.environ, {'PATH': real_path}):
            self.assertEqual(mi.tool_notes()['readpst'], '')
            self.drop(self.A, 'outlook.pst', json.dumps({
                'root': 'Outlook Data File',
                'folders': {'Inbox': [raw('1@x').decode(), raw('2@x').decode()],
                            'Re: plans': [raw('3@x').decode()]}}).encode())
            row = self.run_one()
        self.assertEqual((row['status'], row['detected_type'], row['imported']),
                         ('done', 'pst', 3), row)
        got = self.assert_windows_names(self.A, 3)
        self.assertEqual(sorted({f for f, _ in got}), ['PST/Inbox', 'PST/Re_ plans'])


# ---------------------------------------------------------------------------
# Folders, upload, links
# ---------------------------------------------------------------------------

class TreeTests(Flow):
    def test_per_account_folders_exist_and_are_checked_by_path(self):
        for acct in (self.A, self.B):
            ap = self.ap(acct)
            for key in ('inbox', 'incoming', 'done', 'failed'):
                self.assertTrue(os.path.isdir(ap[key]), ap[key])
        pd = mi.open_dir(self.imp.user_root, self.ap(self.A)['inbox'], owners={12345})
        try:
            self.assertIsNone(pd.fd)                    # nothing is held open
            self.assertEqual(pd.via, self.ap(self.A)['inbox'])
        finally:
            pd.close()
        with self.assertRaises(mi.UnsafeImportTree):
            mi.open_dir(self.imp.user_root, os.path.join(self.tmp, 'elsewhere'))
        with self.assertRaises(FileNotFoundError):
            mi.open_dir(self.imp.user_root, os.path.join(self.imp.user_root, 'nope'))
        # owners are not compared (st_uid means nothing on Windows): no refusal
        self.assertEqual(self.imp.check_account(self.A), '')

    def test_a_file_where_a_folder_should_be_is_refused(self):
        inbox = self.ap(self.B)['inbox']
        shutil.rmtree(inbox)
        write(inbox, b'not a directory')
        self.imp.refused = {}
        self.assertIn('not a directory', self.imp.check_account(self.B))
        scan = self.imp.scan_account(self.B)
        self.assertEqual(scan['candidates'], [])
        self.assertIn('not a directory', scan['error'])

    def test_an_inbox_that_is_a_link_is_refused(self):
        victim = os.path.join(self.tmp, 'victim')
        write(os.path.join(victim, 'theirs.eml'), raw('9@x'))
        inbox = self.ap(self.B)['inbox']
        shutil.rmtree(inbox)
        try:
            os.symlink(victim, inbox, target_is_directory=True)
        except (OSError, NotImplementedError, AttributeError) as e:
            self.skipTest(f'cannot make a symbolic link here: {e}')
        self.imp.refused = {}
        scan = self.imp.scan_account(self.B)
        self.assertEqual(scan['candidates'], [])
        self.assertIn('symbolic link', scan['error'])
        summary = self.imp.run()
        self.assertEqual(summary['results'], [])
        self.assertIn(self.B, summary['refused'])
        self.assertEqual(os.listdir(victim), ['theirs.eml'])        # untouched
        self.assertEqual(delivered(self.maildir(self.B)), [])
        # a link dropped INTO an inbox is reported and never followed
        link = os.path.join(self.ap(self.A)['inbox'], 'link.eml')
        os.symlink(os.path.join(victim, 'theirs.eml'), link)
        scan = self.imp.scan_account(self.A)
        self.assertEqual(scan['candidates'], [])
        self.assertEqual(scan['ignored'], [('link.eml', 'symbolic link (not followed)')])
        with self.assertRaises(OSError):
            pst._open_nofollow(link)

    def test_legacy_watch_folder_is_listed_without_a_directory_descriptor(self):
        watch = self.imp.paths['watch']
        os.makedirs(watch)
        write(os.path.join(watch, f'{self.A}-20261109.pst'), b'!BDN' + b'\0' * 64)
        write(os.path.join(watch, 'notes.txt'), b'x')
        scan = pst.scan_watch(watch)
        self.assertEqual([c['name'] for c in scan['candidates']], [f'{self.A}-20261109.pst'])
        self.assertEqual(scan['ignored'], [('notes.txt', 'not a .pst file')])
        self.assertIsNone(pst.dir_entries(os.path.join(watch, 'missing')))
        self.assertIsNone(pst.dir_entries(os.path.join(watch, 'notes.txt')))


class UploadTests(Flow):
    def upload(self, name, data, **kw):
        return mi.receive_upload(io.BytesIO(data), len(data), self.ap(self.A), name, **kw)

    def test_upload_lands_in_the_inbox_with_its_sidecar_and_is_imported(self):
        data = mbox_bytes([raw('1@x'), raw('2@x')])
        path, digest = self.upload('Takeout (1).mbox', data)
        self.assertEqual(path, os.path.join(self.ap(self.A)['inbox'], 'Takeout (1).mbox'))
        self.assertEqual(digest, hashlib.sha256(data).hexdigest())
        self.assertEqual(sorted(os.listdir(self.ap(self.A)['inbox'])),
                         ['.incoming', 'Takeout (1).mbox', 'Takeout (1).mbox.sha256'])
        self.assertEqual(os.listdir(self.ap(self.A)['incoming']), [])
        row = self.run_one()
        self.assertEqual((row['status'], row['imported']), ('done', 2))
        self.assertIn('sha256 verified', row['detail']['checksum'])

    def test_upload_never_replaces_and_refuses_bad_names(self):
        self.upload('a.eml', raw('1@x'))
        with self.assertRaises(FileExistsError):
            self.upload('a.eml', raw('2@x'))
        for bad in ('nul.eml', 'COM1', 'a.eml.', 'x/y.eml', '..\\y.eml', 'a:b.eml', 'a.tmp'):
            with self.assertRaises(ValueError, msg=bad):
                self.upload(bad, raw('3@x'))
        self.assertEqual(sorted(os.listdir(self.ap(self.A)['inbox'])),
                         ['.incoming', 'a.eml', 'a.eml.sha256'])

    def test_upload_checks_free_space_with_disk_usage(self):
        with self.assertRaises(mi.InsufficientSpace):
            self.upload('big.eml', raw('1@x'), reserve=1 << 62)
        self.assertEqual(os.listdir(self.ap(self.A)['inbox']), ['.incoming'])
        self.assertEqual(os.listdir(self.ap(self.A)['incoming']), [])

    def test_move_noreplace_keeps_what_is_there(self):
        a = write(os.path.join(self.tmp, 'a'), b'A')
        b = write(os.path.join(self.tmp, 'b'), b'B')
        with self.assertRaises(FileExistsError):
            mi.move_noreplace(a, b)
        with open(b, 'rb') as f:
            self.assertEqual(f.read(), b'B')
        c = os.path.join(self.tmp, 'c')
        mi.move_noreplace(a, c)
        self.assertFalse(os.path.exists(a))
        with open(c, 'rb') as f:
            self.assertEqual(f.read(), b'A')


# ---------------------------------------------------------------------------
# Retention R1-R6
# ---------------------------------------------------------------------------

class RetentionTests(Flow):
    def seed_done(self, dates, imported_days_ago=1):
        done = self.ap(self.A)['done']
        for day in dates:
            n = f'export-{day:%Y%m%d}.mbox'
            write(os.path.join(done, n), b'old copy')
            write(os.path.join(done, n + '.sha256'), b'0' * 64 + b'  ' + n.encode() + b'\n')
        if imported_days_ago is not None:
            when = self.now - imported_days_ago * 86400
            led = self.ledger()
            try:
                led.add(status='done', original_name=f'export-{max(dates):%Y%m%d}.mbox',
                        account=self.A, source='inbox', trigger='scheduled',
                        sha256='0' * 64, finished_at=pst.now_str(when), finished_epoch=when)
            finally:
                led.close()

    def export(self, day, mids):
        return self.drop(self.A, f'export-{day:%Y%m%d}.mbox',
                         mbox_bytes([raw(m) for m in mids]))

    def done_dates(self):
        return sorted(e['date'].isoformat()
                      for e in mi.list_archive(self.ap(self.A)['done']))

    def test_r1_normal_rotation_and_r6_every_deletion_recorded(self):
        self.seed_done(STEADY)
        self.export(D(2026, 11, 2), ['1@x'])
        self.export(D(2026, 11, 9), ['1@x', '2@x'])
        summary = self.imp.run()
        self.assertEqual([r['status'] for r in summary['results']], ['done', 'done'])
        self.assertEqual(self.done_dates(), [
            '2026-08-31', '2026-09-28', '2026-10-19', '2026-10-26', '2026-11-02', '2026-11-09'])
        ev = self.events()
        self.assertEqual(sorted((e['action'], e['rule'], e['name']) for e in ev), [
            ('deleted', 'R1', 'export-20260727.mbox'),
            ('deleted', 'R1', 'export-20261005.mbox'),
            ('deleted', 'R1', 'export-20261012.mbox')])
        self.assertEqual({(e['account'], e['series']) for e in ev}, {(self.A, 'export-*.mbox')})
        left = os.listdir(self.ap(self.A)['done'])
        self.assertNotIn('export-20261005.mbox.sha256', left)   # sidecars go with them
        self.assertEqual(len(left), 12)

    def test_r2_a_failed_file_in_the_same_run_blocks_rotation(self):
        self.seed_done(STEADY)
        before = self.done_dates()
        self.export(D(2026, 11, 9), ['1@x'])
        self.drop(self.A, 'outlook-20261109.pst', b'!BDN' + b'\0' * 600)    # no readpst
        summary = self.imp.run()
        self.assertEqual(sorted(r['status'] for r in summary['results']), ['done', 'failed'])
        self.assertEqual(self.done_dates(), before + ['2026-11-09'])
        ev = self.events()
        self.assertTrue(ev)
        self.assertEqual({(e['action'], e['rule']) for e in ev}, {('refused', 'R2')})

    def test_r4_no_earlier_success_freezes_pruning(self):
        self.seed_done(STEADY, imported_days_ago=None)
        before = self.done_dates()
        self.export(D(2026, 11, 9), ['1@x'])
        self.assertEqual(self.run_one()['status'], 'done')
        self.assertEqual(self.done_dates(), before + ['2026-11-09'])
        self.assertEqual({(e['action'], e['rule']) for e in self.events()},
                         {('refused', 'R4')})

    def test_r4_a_stale_source_freezes_pruning(self):
        self.seed_done(STEADY, imported_days_ago=60)
        before = self.done_dates()
        self.export(D(2026, 11, 9), ['1@x'])
        self.run_one()
        self.assertEqual(self.done_dates(), before + ['2026-11-09'])
        ev = self.events()
        self.assertEqual({(e['action'], e['rule']) for e in ev}, {('refused', 'R4')})
        self.assertIn('stale', ev[0]['reason'])

    def test_r3_and_r5_floor_and_budget(self):
        copies = [{'name': f'e-{d:%Y%m%d}.mbox', 'date': d, 'series': 'e-*.mbox'}
                  for d in (D(2026, 10, 5), D(2026, 10, 12), D(2026, 10, 19),
                            D(2026, 10, 26), D(2026, 11, 2))]
        kw = dict(keep_recent=1, keep_monthly=0, new_verified_copy=True,
                  last_success=self.now - 3600, now=self.now)
        dec = mi.retention_decide(copies, D(2026, 11, 9), min_keep=3, max_delete=3, **kw)
        self.assertEqual([c['name'] for c in dec['delete']],
                         ['e-20261005.mbox', 'e-20261012.mbox'])
        self.assertEqual([(c['name'], rule) for c, rule, _why in dec['refused']],
                         [('e-20261019.mbox', 'R3'), ('e-20261026.mbox', 'R3')])
        self.assertNotIn('e-20261102.mbox', [c['name'] for c in dec['delete']])   # newest
        dec = mi.retention_decide(copies, D(2026, 11, 9), min_keep=1, max_delete=1, **kw)
        self.assertEqual(dec['delete'], [])
        self.assertEqual({rule for _c, rule, _why in dec['refused']}, {'R5'})

    def test_one_off_files_are_never_rotated(self):
        done = self.ap(self.A)['done']
        write(os.path.join(done, 'Archive 2019.mbox'), b'x')
        self.seed_done(STEADY)
        self.export(D(2026, 11, 9), ['1@x'])
        self.run_one()
        self.assertIn('Archive 2019.mbox', os.listdir(done))


if __name__ == '__main__':
    unittest.main()
