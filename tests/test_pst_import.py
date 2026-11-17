"""1.3.0 PST import through the general importer: dedupe, the legacy watch
folder contract, mapping, schedule, the retention policy, and the
app/route wiring. (Every other format, the ledger and the retention guard
rules are in test_mail_import.py.)

readpst is replaced by tests/fake_readpst.py, which writes the same
OUT/<root>/<folder>/<n> layout `readpst -M` does.
"""

import datetime
import hashlib
import io
import json
import os
import stat
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

import pytest

# Linux-only. The Windows paths of the same code are covered by
# tests/test_windows_import_port.py.
pytestmark = pytest.mark.skipif(
    os.name == 'nt',
    reason='fixtures are POSIX: the readpst stand-in is a "#!" script made executable '
           'with chmod (every PST test runs it), the watch-folder tests need os.symlink '
           'without privilege, and the schedule tests need time.tzset')

os.environ['TZ'] = 'UTC'
if hasattr(time, 'tzset'):          # not on Windows (module skipped there)
    time.tzset()
_DATA = tempfile.mkdtemp(prefix='ma-test-data-')
os.environ['MAIL_ARCHIVER_DATA'] = _DATA
os.environ['MAIL_ARCHIVER_AUTH'] = 'builtin'
os.environ['MAIL_ARCHIVER_SECRET'] = 'test-only-secret'
os.environ.pop('MAIL_ARCHIVER_SECRET_FILE', None)

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
FAKE_READPST = os.path.join(HERE, 'fake_readpst.py')
os.chmod(FAKE_READPST, 0o755)
os.environ['MAIL_ARCHIVER_READPST'] = FAKE_READPST

import pst_import as pst  # noqa: E402
pst.READPST = FAKE_READPST
import mail_import as mi  # noqa: E402
import search_index       # noqa: E402
import app as ma          # noqa: E402


def msg(mid, subject='hello', body='body text', frm='a@example.com'):
    h = f'Message-ID: <{mid}>\r\n' if mid else ''
    return (f'{h}From: {frm}\r\nTo: b@example.com\r\nSubject: {subject}\r\n'
            f'Date: Mon, 02 Nov 2026 10:00:00 +0000\r\n\r\n{body}\r\n')


def multipart_no_id(subject='no id', body='plain part'):
    return ('From: c@example.com\r\nTo: d@example.com\r\n'
            f'Subject: {subject}\r\nDate: Tue, 03 Nov 2026 11:00:00 +0000\r\n'
            'MIME-Version: 1.0\r\n'
            'Content-Type: multipart/mixed; boundary="{BOUNDARY}"\r\n\r\n'
            '--{BOUNDARY}\r\nContent-Type: text/plain\r\n\r\n'
            f'{body}\r\n--{{BOUNDARY}}--\r\n').replace('{{BOUNDARY}}', '{BOUNDARY}')


def write_pst(path, folders, **extra):
    spec = {'root': 'Outlook Data File', 'folders': folders}
    spec.update(extra)
    with open(path, 'w') as f:
        json.dump(spec, f)
    return path


def maildir_messages(maildir):
    out = []
    for root, _, files in os.walk(maildir):
        if os.path.basename(root) in ('cur', 'new'):
            rel = os.path.relpath(os.path.dirname(root), maildir)
            out.extend((rel, f) for f in files)
    return out


class Tmp(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.tmp = self._td.name

    def tearDown(self):
        self._td.cleanup()


# ---------------------------------------------------------------------------
# Dedupe + conversion
# ---------------------------------------------------------------------------

class DedupeTests(Tmp):
    def _import(self, pst_path, **kw):
        return pst.import_pst_to_maildir(
            pst_path, os.path.join(self.tmp, 'acct'), os.path.join(self.tmp, 'work'),
            readpst=FAKE_READPST, cache_path=os.path.join(self.tmp, 'ids.json'), **kw)

    def test_reimport_of_full_export_adds_only_new_messages(self):
        p = write_pst(os.path.join(self.tmp, 'a.pst'),
                      {'Inbox': [msg('1@x'), msg('2@x')], 'Sent Items': [msg('3@x')]})
        s1 = self._import(p)
        self.assertEqual((s1['added'], s1['duplicates']), (3, 0))
        # next week's full export: same three plus one new
        write_pst(p, {'Inbox': [msg('1@x'), msg('2@x'), msg('4@x')],
                      'Sent Items': [msg('3@x')]})
        s2 = self._import(p)
        self.assertEqual((s2['added'], s2['duplicates']), (1, 3))
        self.assertEqual(len(maildir_messages(os.path.join(self.tmp, 'acct'))), 4)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, 'work')))

    def test_message_already_synced_by_imap_in_another_folder_is_skipped(self):
        acct = os.path.join(self.tmp, 'acct')
        pst.deliver(os.path.join(acct, '[Gmail]', 'All Mail'), msg('synced@x').encode())
        p = write_pst(os.path.join(self.tmp, 'a.pst'), {'Inbox': [msg('synced@x'), msg('new@x')]})
        s = self._import(p)
        self.assertEqual((s['added'], s['duplicates']), (1, 1))

    def test_message_id_normalisation(self):
        acct = os.path.join(self.tmp, 'acct')
        pst.deliver(os.path.join(acct, 'INBOX'),
                    b'Message-ID:   <abc@x>  \r\nSubject: s\r\n\r\nb\r\n')
        p = write_pst(os.path.join(self.tmp, 'a.pst'), {'Inbox': [msg('abc@x')]})
        self.assertEqual(self._import(p)['duplicates'], 1)

    def test_no_message_id_falls_back_to_content_hash_despite_new_boundary(self):
        p = write_pst(os.path.join(self.tmp, 'a.pst'),
                      {'Drafts': [multipart_no_id(), multipart_no_id(body='other')]})
        self.assertEqual(self._import(p)['added'], 2)
        # readpst makes up new boundaries each run; still the same messages
        s = self._import(p)
        self.assertEqual((s['added'], s['duplicates']), (0, 2))

    def test_duplicates_inside_one_pst_are_written_once(self):
        p = write_pst(os.path.join(self.tmp, 'a.pst'),
                      {'Inbox': [msg('d@x')], 'Copy': [msg('d@x')]})
        s = self._import(p)
        self.assertEqual((s['added'], s['duplicates']), (1, 1))

    def test_id_cache_is_used_and_kept_correct(self):
        p = write_pst(os.path.join(self.tmp, 'a.pst'), {'Inbox': [msg('c1@x')]})
        self._import(p)
        self._import(p)  # second scan writes the cache entry for c1
        with open(os.path.join(self.tmp, 'ids.json')) as f:
            cache = json.load(f)
        self.assertIn('m:c1@x', [v[1] for v in cache.values()])
        with patch.object(pst, 'message_key_from_file',
                          side_effect=AssertionError('cache not used')):
            self.assertEqual(self._import(p)['duplicates'], 1)

    def test_folder_tree_is_preserved_under_the_PST_prefix(self):
        # a PST Inbox is PST/Inbox, never the synced INBOX
        p = write_pst(os.path.join(self.tmp, 'a.pst'), {
            'Inbox': [msg('i@x')], 'Inbox/Receipts': [msg('r@x')],
            'Projects/2026': [msg('p@x')], 'Sent Items': [msg('s@x')]})
        self._import(p)
        folders = sorted({f for f, _ in maildir_messages(os.path.join(self.tmp, 'acct'))})
        self.assertEqual(folders, ['PST/Inbox', 'PST/Inbox/Receipts',
                                   'PST/Projects/2026', 'PST/Sent Items'])
        self.assertFalse(os.path.exists(os.path.join(self.tmp, 'acct', 'INBOX')))

    def test_files_are_private_like_mbsync(self):
        p = write_pst(os.path.join(self.tmp, 'a.pst'), {'Inbox': [msg('m@x')]})
        old = os.umask(0o022)
        try:
            self._import(p)
        finally:
            os.umask(old)
        for d in ('PST', 'PST/Inbox', 'PST/Inbox/cur'):
            self.assertEqual(stat.S_IMODE(os.stat(os.path.join(self.tmp, 'acct', d)).st_mode),
                             0o700, d)
        cur = os.path.join(self.tmp, 'acct', 'PST', 'Inbox', 'cur')
        f = os.path.join(cur, os.listdir(cur)[0])
        self.assertEqual(stat.S_IMODE(os.stat(f).st_mode), 0o600)
        self.assertTrue(f.endswith(':2,S'))               # imported mail is marked read
        self.assertEqual(int(os.stat(f).st_mtime), 1793613600)  # the Date header

    def test_untrusted_folder_names_and_links(self):
        self.assertEqual(pst.sanitize_folder_component('..'), '_')
        self.assertEqual(pst.sanitize_folder_component('.hidden'), '_hidden')
        self.assertEqual(pst.sanitize_folder_component('cur'), '_cur')
        self.assertEqual(pst.sanitize_folder_component('a\x00b\nc'), 'a_b_c')
        self.assertEqual(pst.map_folder(['inbox', 'x']), ['PST', 'inbox', 'x'])
        self.assertEqual(pst.map_folder([]), ['PST'])
        p = write_pst(os.path.join(self.tmp, 'a.pst'),
                      {'Inbox': [msg('l@x')], 'new': [msg('n@x')]}, symlink=True)
        s = self._import(p)
        self.assertEqual((s['added'], s['skipped_links']), (2, 1))
        folders = {f for f, _ in maildir_messages(os.path.join(self.tmp, 'acct'))}
        self.assertEqual(folders, {'PST/Inbox', 'PST/_new'})

    def test_oversized_message_is_skipped_not_failed(self):
        p = write_pst(os.path.join(self.tmp, 'a.pst'),
                      {'Inbox': [msg('big@x', body='x' * 5000), msg('ok@x')]})
        s = self._import(p, max_message_bytes=2000)
        self.assertEqual((s['added'], s['too_large'], s['failed']), (1, 1, 0))

    def test_readpst_failure_raises_and_cleans_up(self):
        p = write_pst(os.path.join(self.tmp, 'a.pst'), {}, exit=1)
        with self.assertRaises(pst.ImportFailed):
            self._import(p)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, 'work')))

    def test_converter_cli_prints_json(self):
        p = write_pst(os.path.join(self.tmp, 'a.pst'), {'Inbox': [msg('cli@x')]})
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = mi._cli(['convert', '--path', p, '--type', 'pst',
                          '--maildir', os.path.join(self.tmp, 'acct'),
                          '--work', os.path.join(self.tmp, 'w'), '--readpst', FAKE_READPST])
        out = json.loads(buf.getvalue().strip().splitlines()[-1])
        self.assertEqual((rc, out['status'], out['added']), (0, 'ok', 1))


# ---------------------------------------------------------------------------
# Watch folder contract
# ---------------------------------------------------------------------------

def sidecar_for(path, digest=None, name=None):
    digest = digest or hashlib.sha256(open(path, 'rb').read()).hexdigest()
    with open(path + '.sha256', 'w') as f:
        f.write(f'{digest}  {name or os.path.basename(path)}\n')


class WatchTests(Tmp):
    def setUp(self):
        super().setUp()
        self.w = os.path.join(self.tmp, 'watch')
        os.makedirs(os.path.join(self.w, '.incoming'))

    def touch(self, name, data=b'pst'):
        p = os.path.join(self.w, name)
        with open(p, 'wb') as f:
            f.write(data)
        return p

    def test_scan_ignores_dotfiles_incoming_tmp_and_non_pst(self):
        self.touch('.incoming/x-20261109.pst')
        self.touch('.fetch-state')
        self.touch('.hidden.pst')
        self.touch('a@b.edu-20261109.pst.sha256.tmp')
        self.touch('b-20261109.pst.tmp')
        self.touch('notes.txt')
        os.mkdir(os.path.join(self.w, 'subdir.pst'))
        os.symlink('/etc/passwd', os.path.join(self.w, 'link-20261109.pst'))
        good = self.touch('a@b.edu-20261109.pst')
        sidecar_for(good)
        self.touch('orphan-20261109.pst.sha256')  # sidecar renamed in before its .pst
        scan = pst.scan_watch(self.w)
        self.assertEqual([c['name'] for c in scan['candidates']], ['a@b.edu-20261109.pst'])
        self.assertEqual(scan['candidates'][0]['sidecar'], good + '.sha256')
        ignored = dict(scan['ignored'])
        self.assertEqual(set(ignored), {'notes.txt', 'subdir.pst', 'link-20261109.pst'})
        self.assertIn('symbolic link', ignored['link-20261109.pst'])

    def test_missing_watch_dir_is_empty(self):
        self.assertEqual(pst.scan_watch(os.path.join(self.tmp, 'nope'))['candidates'], [])

    def test_stability_needs_two_equal_checks(self):
        a = self.touch('a-20261109.pst')
        b = self.touch('b-20261109.pst')
        cands = pst.scan_watch(self.w)['candidates']
        waited = []

        def sleep(s):
            waited.append(s)
            with open(b, 'ab') as f:   # b is still growing
                f.write(b'more')
        stable, unstable = pst.split_stable(cands, wait=7, sleep=sleep)
        self.assertEqual(waited, [7])
        self.assertEqual([c['path'] for c in stable], [a])
        self.assertEqual([c['path'] for c in unstable], [b])

    def test_mtime_change_alone_is_unstable(self):
        a = self.touch('a-20261109.pst')
        cands = pst.scan_watch(self.w)['candidates']
        stable, unstable = pst.split_stable(
            cands, wait=1, sleep=lambda s: os.utime(a, (1, 1)))
        self.assertEqual((len(stable), len(unstable)), (0, 1))

    def test_sidecar_verification(self):
        p = self.touch('a-20261109.pst', b'payload')
        sidecar_for(p)
        self.assertTrue(pst.verify_sidecar(p, p + '.sha256')[0])
        sidecar_for(p, digest='0' * 64)
        ok, why = pst.verify_sidecar(p, p + '.sha256')
        self.assertFalse(ok)
        self.assertIn('mismatch', why)
        sidecar_for(p, name='other-20261109.pst')
        self.assertFalse(pst.verify_sidecar(p, p + '.sha256')[0])
        with open(p + '.sha256', 'w') as f:
            f.write('not a digest\n')
        self.assertFalse(pst.verify_sidecar(p, p + '.sha256')[0])

    def test_sidecar_formats(self):
        d = 'a' * 64
        self.assertEqual(pst.parse_sidecar(f'{d}  x.pst\n', 'x.pst'), d)
        self.assertEqual(pst.parse_sidecar(f'{d} *x.pst\n', 'x.pst'), d)
        self.assertEqual(pst.parse_sidecar(f'{d}  /some/dir/x.pst', 'x.pst'), d)
        self.assertEqual(pst.parse_sidecar(d.upper(), 'x.pst'), d)
        with self.assertRaises(ValueError):
            pst.parse_sidecar(f'{d}  x.pst\n{d}  y.pst\n', 'x.pst')


# ---------------------------------------------------------------------------
# Mapping + config + schedule
# ---------------------------------------------------------------------------

ACCTS = ['ab1234c@student.example.edu', 'Bob.Example@hotmail.com']


class MappingTests(unittest.TestCase):
    def test_pattern_maps_other_domain_to_account(self):
        cfg = pst.normalize_config({'mappings': [
            {'pattern': 'ab1234c@example.edu-*.pst',
             'account': 'ab1234c@student.example.edu'}]}, ACCTS)
        self.assertEqual(pst.map_file_to_account('ab1234c@example.edu-20261109.pst', cfg, ACCTS),
                         ('ab1234c@student.example.edu', 'pattern ab1234c@example.edu-*.pst'))
        self.assertEqual(pst.map_file_to_account('AB1234C@Example.edu-20261116.PST', cfg, ACCTS)[0],
                         'ab1234c@student.example.edu')

    def test_file_named_after_account(self):
        cfg = pst.normalize_config({}, ACCTS)
        self.assertEqual(pst.map_file_to_account('bob.example@hotmail.com-20261109.pst', cfg, ACCTS),
                         ('Bob.Example@hotmail.com', 'file name'))

    def test_first_match_wins_then_default_then_unmapped(self):
        cfg = pst.normalize_config({'mappings': [
            {'pattern': '*-2026*.pst', 'account': ACCTS[1]},
            {'pattern': '*.pst', 'account': ACCTS[0]}]}, ACCTS)
        self.assertEqual(pst.map_file_to_account('x-20261109.pst', cfg, ACCTS)[0], ACCTS[1])
        cfg = pst.normalize_config({'default_account': ACCTS[0].upper()}, ACCTS)
        self.assertEqual(pst.map_file_to_account('whatever.pst', cfg, ACCTS),
                         (ACCTS[0], 'default account'))
        acct, why = pst.map_file_to_account('whatever.pst', pst.normalize_config({}, ACCTS), ACCTS)
        self.assertIsNone(acct)
        self.assertIn('no mapping', why)

    def test_mapping_to_unknown_account_is_dropped(self):
        cfg = pst.normalize_config({'mappings': [
            {'pattern': '*.pst', 'account': 'gone@x.com'},
            {'pattern': 'a/b', 'account': ACCTS[0]}]}, ACCTS)
        self.assertEqual(cfg['mappings'], [])

    def test_config_clamps(self):
        cfg = pst.normalize_config({'schedule': {'kind': 'yearly', 'hour': 99, 'weekday': -3},
                                    'keep_weekly': 0, 'keep_monthly': 'x',
                                    'min_keep': 0, 'max_delete_per_run': 999}, ACCTS)
        self.assertEqual(cfg['schedule'], {'kind': 'daily', 'weekday': 0, 'hour': 23})
        # keep_weekly is the older name of keep_recent
        self.assertEqual((cfg['keep_recent'], cfg['keep_monthly']), (1, 3))
        self.assertEqual((cfg['min_keep'], cfg['max_delete_per_run']), (1, 50))
        d = pst.normalize_config({}, ACCTS)
        self.assertEqual((d['keep_recent'], d['keep_monthly'], d['min_keep'],
                          d['max_delete_per_run']), (4, 3, 3, 3))
        self.assertEqual(d['schedule'], {'kind': 'daily', 'weekday': 0, 'hour': 6})

    def test_watch_dir_must_stay_inside_user_dir(self):
        d = tempfile.mkdtemp()
        os.makedirs(os.path.join(d, 'u', 'mine'))
        self.assertTrue(pst.resolve_watch_dir(d, 'u', 'mine').endswith('/u/mine'))
        self.assertTrue(pst.resolve_watch_dir(d, 'u', '').endswith('/u/_import/watch'))
        for bad in ('../other', '/etc', '.'):
            with self.assertRaises(ValueError):
                pst.resolve_watch_dir(d, 'u', bad)
        os.symlink('/etc', os.path.join(d, 'u', 'escape'))
        with self.assertRaises(ValueError):
            pst.resolve_watch_dir(d, 'u', 'escape')

    def test_file_date(self):
        self.assertEqual(pst.file_date('a@b.edu-20261109.pst'), datetime.date(2026, 11, 9))
        self.assertEqual(pst.file_date('a@b.edu-20261109.pst.zst'), datetime.date(2026, 11, 9))
        self.assertEqual(pst.file_date('x.pst', 0), datetime.date(1970, 1, 1))
        self.assertEqual(pst.file_date('takeout-20261109.mbox'), datetime.date(2026, 11, 9))
        self.assertEqual(pst.file_date('mail-20261109.tar.gz'), datetime.date(2026, 11, 9))


def ts(y, mo, d, h=0, mi=0):
    return time.mktime((y, mo, d, h, mi, 0, 0, 0, -1))


class ScheduleTests(unittest.TestCase):
    def test_weekly(self):
        sch = {'kind': 'weekly', 'weekday': 0, 'hour': 6}   # Monday 06:00
        mon = ts(2026, 11, 9, 6, 30)                          # 2026-11-09 is a Monday
        self.assertEqual(pst.last_slot_epoch(sch, mon), ts(2026, 11, 9, 6))
        self.assertEqual(pst.last_slot_epoch(sch, ts(2026, 11, 9, 5)), ts(2026, 11, 2, 6))
        self.assertTrue(pst.is_import_due(sch, None, mon))
        self.assertTrue(pst.is_import_due(sch, ts(2026, 11, 2, 6, 30), mon))
        self.assertFalse(pst.is_import_due(sch, ts(2026, 11, 9, 6, 30), ts(2026, 11, 12)))

    def test_daily_hourly_manual(self):
        d = {'kind': 'daily', 'hour': 6}
        self.assertEqual(pst.last_slot_epoch(d, ts(2026, 11, 9, 5)), ts(2026, 11, 8, 6))
        self.assertTrue(pst.is_import_due(d, ts(2026, 11, 8, 7), ts(2026, 11, 9, 6, 1)))
        self.assertFalse(pst.is_import_due(d, ts(2026, 11, 9, 6, 2), ts(2026, 11, 9, 23)))
        h = {'kind': 'hourly'}
        self.assertTrue(pst.is_import_due(h, ts(2026, 11, 9, 5, 30), ts(2026, 11, 9, 6, 30)))
        self.assertFalse(pst.is_import_due({'kind': 'manual'}, None, ts(2026, 11, 9)))

    def test_missed_slot_runs_late(self):
        sch = {'kind': 'weekly', 'weekday': 0, 'hour': 6}
        # host was down Monday; Wednesday's hourly check still finds it due
        self.assertTrue(pst.is_import_due(sch, ts(2026, 11, 2, 6, 30), ts(2026, 11, 11, 14)))


# ---------------------------------------------------------------------------
# Retention policy (which copies the policy would keep; the guard rules that
# decide what may really be deleted are tested in test_mail_import.py)
# ---------------------------------------------------------------------------

def mk_archive(directory, dates):
    os.makedirs(directory, exist_ok=True)
    for d in dates:
        n = f'ab1234c@example.edu-{d:%Y%m%d}.pst.zst'
        open(os.path.join(directory, n), 'w').close()
        open(os.path.join(directory, n[:-4] + '.sha256'), 'w').close()


def weekly(start, n):
    return [start + datetime.timedelta(days=7 * i) for i in range(n)]


# What a rotation on 2026-10-26 leaves behind: 4 recent + Sep, Aug, Jul.
STEADY = [datetime.date(2026, 7, 27), datetime.date(2026, 8, 31),
          datetime.date(2026, 9, 28), datetime.date(2026, 10, 5),
          datetime.date(2026, 10, 12), datetime.date(2026, 10, 19),
          datetime.date(2026, 10, 26)]


class RotationTests(Tmp):
    def names(self, rows):
        return sorted(r['date'].isoformat() for r in rows)

    def test_keeps_four_recent_and_one_per_previous_three_months(self):
        dates = weekly(datetime.date(2026, 6, 1), 24)       # Mondays 06-01 .. 11-09
        entries = [{'name': f'f-{d:%Y%m%d}.pst', 'date': d} for d in dates]
        keep, delete = pst.plan_rotation(entries, datetime.date(2026, 11, 9))
        self.assertEqual(self.names(keep), [
            '2026-08-31', '2026-09-28', '2026-10-19', '2026-10-26',
            '2026-11-02', '2026-11-09'])
        # Oct's newest (10-26) is already a recent copy; Aug + Sep monthlies are
        # the last copy in those months; everything from June/July is surplus.
        self.assertEqual(len(delete), 24 - 6)
        self.assertTrue(all(r['date'] < datetime.date(2026, 10, 19) for r in delete))
        reasons = {r['date'].isoformat(): r['keep_reason'] for r in keep}
        self.assertEqual(reasons['2026-10-26'], 'recent+monthly')
        self.assertEqual(reasons['2026-08-31'], 'monthly')

    def test_month_boundary_and_year_wrap(self):
        dates = [datetime.date(2025, 9, 29), datetime.date(2025, 10, 27),
                 datetime.date(2025, 11, 24), datetime.date(2025, 12, 1),
                 datetime.date(2025, 12, 29), datetime.date(2026, 1, 5),
                 datetime.date(2026, 1, 12), datetime.date(2026, 1, 19)]
        entries = [{'name': f'f-{d:%Y%m%d}.pst', 'date': d} for d in dates]
        keep, delete = pst.plan_rotation(entries, datetime.date(2026, 1, 19))
        # recent: 01-19, 01-12, 01-05, 12-29; monthlies Dec(12-29), Nov(11-24), Oct(10-27)
        self.assertEqual(self.names(keep), ['2025-10-27', '2025-11-24', '2025-12-29',
                                            '2026-01-05', '2026-01-12', '2026-01-19'])
        self.assertEqual(self.names(delete), ['2025-09-29', '2025-12-01'])

    def test_monthly_beyond_three_existing_months_is_surplus(self):
        entries = [{'name': n, 'date': d} for n, d in (
            ('a', datetime.date(2026, 6, 30)), ('b', datetime.date(2026, 7, 31)),
            ('c', datetime.date(2026, 8, 31)), ('d', datetime.date(2026, 9, 30)),
            ('e', datetime.date(2026, 11, 2)), ('f', datetime.date(2026, 11, 9)))]
        keep, delete = pst.plan_rotation(entries, datetime.date(2026, 11, 9), 2, 3)
        self.assertEqual([r['name'] for r in delete], ['a'])

    def test_a_gap_in_the_source_never_ages_a_monthly_out(self):
        # R1: counts of EXISTING copies. The source was silent Sep-Oct; the
        # July and August monthlies are still the two most recent months
        # that have a copy, so neither is surplus (the 1.3.0 draft deleted
        # 'a' here purely because of its age).
        entries = [{'name': 'a', 'date': datetime.date(2026, 7, 31)},
                   {'name': 'b', 'date': datetime.date(2026, 8, 31)},
                   {'name': 'c', 'date': datetime.date(2026, 11, 2)},
                   {'name': 'd', 'date': datetime.date(2026, 11, 9)}]
        keep, delete = pst.plan_rotation(entries, datetime.date(2026, 11, 9), 2, 3)
        self.assertEqual(delete, [])
        # ... and the same set a year later still keeps all of them
        keep, delete = pst.plan_rotation(entries, datetime.date(2027, 11, 9), 2, 3)
        self.assertEqual(delete, [])

    def test_never_deletes_newest_copy(self):
        entries = [{'name': 'only', 'date': datetime.date(2020, 1, 1)}]
        keep, delete = pst.plan_rotation(entries, datetime.date(2026, 11, 9), 1, 0)
        self.assertEqual((len(keep), delete), (1, []))

    def test_apply_deletes_pst_and_sidecar(self):
        a = os.path.join(self.tmp, 'done')
        mk_archive(a, STEADY + [datetime.date(2026, 11, 2), datetime.date(2026, 11, 9)])
        now = ts(2026, 11, 9, 7)
        dec = mi.retention_decide(mi.list_archive(a), datetime.date(2026, 11, 9),
                                  new_verified_copy=True, last_success=now, now=now)
        # kept: 11-09, 11-02, 10-26, 10-19 (recent), 09-28 + 08-31 (monthly)
        self.assertEqual(self.names(dec['delete']), ['2026-07-27', '2026-10-05', '2026-10-12'])
        self.assertEqual(dec['refused'], [])
        for e in dec['delete']:
            mi._remove_copy(e)
        left = sorted(os.listdir(a))
        self.assertEqual(len(left), 12)           # 6 copies x (pst + sidecar)
        self.assertIn('ab1234c@example.edu-20260928.pst.zst', left)
        self.assertIn('ab1234c@example.edu-20260928.pst.sha256', left)
        self.assertNotIn('ab1234c@example.edu-20261005.pst.sha256', left)

    def test_stop_on_failure(self):
        a = os.path.join(self.tmp, 'done')
        mk_archive(a, STEADY + [datetime.date(2026, 11, 2), datetime.date(2026, 11, 9)])
        now = ts(2026, 11, 9, 7)
        for kw in ({'new_verified_copy': False},
                   {'new_verified_copy': True, 'blocked': 'an import in this run was partial'}):
            dec = mi.retention_decide(mi.list_archive(a), datetime.date(2026, 11, 9),
                                      last_success=now, now=now, **kw)
            self.assertEqual(dec['delete'], [], kw)
            self.assertEqual({rule for _e, rule, _w in dec['refused']}, {'R2'}, kw)
            self.assertEqual(len(dec['refused']), 3)


# ---------------------------------------------------------------------------
# Orchestrator (UserImporter) end to end through the legacy PST watch
# folder, with the converter in-process
# ---------------------------------------------------------------------------

class ImporterTests(Tmp):
    USER = 'Bob'
    ACCT = 'ab1234c@student.example.edu'
    SLUG = 'ab1234c_at_student_example_edu'

    def setUp(self):
        super().setUp()
        self.indexed = []
        cfg = pst.normalize_config({'mappings': [
            {'pattern': 'ab1234c@example.edu-*.pst', 'account': self.ACCT}]}, [self.ACCT])
        self.now = ts(2026, 11, 9, 7)
        self.imp = mi.UserImporter(
            self.tmp, self.USER, [self.ACCT], cfg,
            convert=self.convert, prepare=lambda p: None,
            index=self.indexed.append, open_ledger=self.ledger,
            log=lambda m: None, now=lambda: self.now, sleep=lambda s: None,
            stable_wait=0)
        self.imp.ensure_dirs()
        self.paths = self.imp.paths
        self.ap = self.imp.account_paths(self.ACCT)

    def ledger(self):
        return mi.Ledger(search_index.get_index_db(self.USER, self.tmp))

    def rows(self):
        led = self.ledger()
        try:
            return led.rows()
        finally:
            led.close()

    def seed_archive(self, dates, imported_days_ago=1):
        """Copies in done/ and the ledger row of the newest one: retention
        rule R4 wants a recent successful import BEFORE the run that
        prunes."""
        mk_archive(self.ap['done'], dates)
        when = self.now - imported_days_ago * 86400
        led = self.ledger()
        try:
            led.add(status='done', account=self.ACCT, source='watch',
                    original_name=f'ab1234c@example.edu-{max(dates):%Y%m%d}.pst',
                    finished_at=pst.now_str(when), finished_epoch=when)
        finally:
            led.close()

    def convert(self, path, typ, name, maildir, work, cache):
        try:
            s = mi.convert_unit(path, typ, maildir, work, name=name,
                                readpst=FAKE_READPST, cache_path=cache)
            s['status'] = mi.stats_status(s)
            return s
        except pst.ImportFailed as e:
            return {'status': 'failed', 'error': str(e)}

    def produce(self, date, folders, sidecar=True, bad_sum=False, **extra):
        """What the producer does: write in .incoming, sidecar first, rename."""
        name = f'ab1234c@example.edu-{date:%Y%m%d}.pst'
        inc = os.path.join(self.paths['incoming'], name)
        write_pst(inc, folders, **extra)
        if sidecar:
            digest = '0' * 64 if bad_sum else hashlib.sha256(open(inc, 'rb').read()).hexdigest()
            with open(inc + '.sha256.tmp', 'w') as f:
                f.write(f'{digest}  {name}\n')
            os.rename(inc + '.sha256.tmp', os.path.join(self.paths['watch'], name + '.sha256'))
        os.rename(inc, os.path.join(self.paths['watch'], name))
        return name

    def test_import_archives_indexes_and_records(self):
        name = self.produce(datetime.date(2026, 11, 9), {'Inbox': [msg('1@x'), msg('2@x')]})
        summary = self.imp.run()
        run = summary['results'][0]
        self.assertEqual((run['status'], run['imported'], run['account']), ('done', 2, self.ACCT))
        self.assertEqual((run['source'], run['detected_type'], run['target_prefix']),
                         ('watch', 'pst', 'PST/'))
        self.assertIn('sha256 verified', run['detail']['checksum'])
        self.assertEqual(self.indexed, [self.ACCT])
        self.assertEqual(os.listdir(self.paths['watch']), ['.incoming'])
        self.assertEqual(sorted(os.listdir(self.ap['done'])), [name, name + '.sha256'])
        maildir = os.path.join(self.tmp, self.USER, self.SLUG)
        self.assertEqual({f for f, _ in maildir_messages(maildir)}, {'PST/Inbox'})
        self.assertEqual(len(maildir_messages(maildir)), 2)
        self.assertEqual(self.rows()[0]['status'], 'done')

    def test_weekly_cycle_adds_only_new_and_rotates(self):
        self.seed_archive(STEADY)
        self.produce(datetime.date(2026, 11, 2), {'Inbox': [msg('1@x')]})
        self.produce(datetime.date(2026, 11, 9), {'Inbox': [msg('1@x'), msg('2@x')]})
        summary = self.imp.run()
        self.assertEqual([(r['imported'], r['duplicates']) for r in summary['results']],
                         [(1, 0), (1, 1)])
        dates = sorted(r['date'] for r in mi.list_archive(self.ap['done']))
        self.assertEqual([d.isoformat() for d in dates], [
            '2026-08-31', '2026-09-28', '2026-10-19', '2026-10-26', '2026-11-02', '2026-11-09'])
        self.assertEqual(len(summary['rotation'][0]['deleted']), 3)

    def test_checksum_mismatch_goes_to_failed_and_stops_rotation(self):
        mk_archive(self.ap['done'], STEADY + [datetime.date(2026, 11, 2)])
        before = sorted(os.listdir(self.ap['done']))
        name = self.produce(datetime.date(2026, 11, 9), {'Inbox': [msg('1@x')]}, bad_sum=True)
        summary = self.imp.run()
        run = summary['results'][0]
        self.assertEqual(run['status'], 'failed')
        self.assertIn('mismatch', run['error'])
        self.assertEqual(sorted(os.listdir(self.ap['failed'])),
                         [name, name + '.reason.txt', name + '.sha256'])
        self.assertIn('mismatch', open(os.path.join(self.ap['failed'], name + '.reason.txt')).read())
        self.assertEqual(sorted(os.listdir(self.ap['done'])), before)
        self.assertEqual(summary['rotation'][0]['deleted'], [])
        # nothing was written to the mailbox
        self.assertFalse(os.path.exists(os.path.join(self.tmp, self.USER, self.SLUG, 'PST')))
        # retry moves it back, into the account's own inbox
        mi.retry_failed(self.ap, name)
        self.assertEqual(sorted(os.listdir(self.ap['inbox'])),
                         ['.incoming', name, name + '.sha256'])
        self.assertEqual(os.listdir(self.ap['failed']), [])

    def test_readpst_failure_keeps_every_archive_copy(self):
        self.seed_archive(STEADY)
        before = sorted(os.listdir(self.ap['done']))
        self.produce(datetime.date(2026, 11, 9), {}, exit=1)
        run = self.imp.run()['results'][0]
        self.assertEqual(run['status'], 'failed')
        self.assertIn('readpst exited 1', run['error'])
        self.assertEqual(sorted(os.listdir(self.ap['done'])), before)
        self.assertEqual(len(os.listdir(self.ap['failed'])), 3)
        # a later good import rotates again
        self.now = ts(2026, 11, 10, 7)
        self.produce(datetime.date(2026, 11, 10), {'Inbox': [msg('1@x')]})
        summary = self.imp.run()
        self.assertEqual(summary['results'][0]['status'], 'done')
        self.assertEqual(sorted(summary['rotation'][0]['deleted']), [
            'ab1234c@example.edu-20260727.pst.zst', 'ab1234c@example.edu-20261005.pst.zst'])

    def test_no_sidecar_is_allowed(self):
        name = self.produce(datetime.date(2026, 11, 9), {'Inbox': [msg('1@x')]}, sidecar=False)
        run = self.imp.run()['results'][0]
        self.assertEqual((run['status'], run['detail']['checksum']), ('done', 'no sidecar'))
        # the archived copy gets a sidecar from the hash the importer computed
        side = os.path.join(self.ap['done'], name + '.sha256')
        self.assertEqual(open(side).read().split()[0], run['sha256'])

    def test_unmapped_file_stays_and_is_recorded_once(self):
        p = os.path.join(self.paths['watch'], 'someone-else-20261109.pst')
        write_pst(p, {'Inbox': [msg('1@x')]})
        for _ in range(3):
            self.assertEqual(self.imp.run()['results'][0]['status'], 'skipped')
        self.assertTrue(os.path.exists(p))
        self.assertEqual(len(self.rows()), 1)

    def test_unstable_file_waits(self):
        self.produce(datetime.date(2026, 11, 9), {'Inbox': [msg('1@x')]})
        p = os.path.join(self.paths['watch'], 'ab1234c@example.edu-20261109.pst')
        self.imp.sleep = lambda s: os.utime(p, (5, 5))
        self.imp.stable_wait = 1
        summary = self.imp.run()
        self.assertEqual((summary['results'], summary['retry']), ([], True))
        self.assertTrue(os.path.exists(p))
        self.assertEqual(self.rows(), [])

    def test_interrupted_run_is_marked_failed(self):
        led = self.ledger()
        rid = led.add(status='running', original_name='f.pst', account=self.ACCT)
        led.close()
        self.imp.run()
        row = self.rows()[0]
        self.assertEqual((row['id'], row['status']), (rid, 'failed'))
        self.assertIn('interrupted', row['error'])

    def test_upload_follows_producer_contract(self):
        body = json.dumps({'folders': {'Inbox': [msg('u@x')]}}).encode()
        name = 'ab1234c@example.edu-20261109.pst'
        dest, digest = mi.receive_upload(io.BytesIO(body), len(body), self.ap, name)
        self.assertEqual(dest, os.path.join(self.ap['inbox'], name))
        self.assertEqual(digest, hashlib.sha256(body).hexdigest())
        self.assertTrue(pst.verify_sidecar(dest, dest + '.sha256')[0])
        self.assertEqual(os.listdir(self.ap['incoming']), [])
        with self.assertRaises(FileExistsError):
            mi.receive_upload(io.BytesIO(body), len(body), self.ap, name)
        for bad in ('../x.pst', '.x.pst', 'a/b.pst', 'x.pst.tmp', 'x.pst.sha256', ' x.pst', ''):
            with self.assertRaises(ValueError):
                mi.receive_upload(io.BytesIO(body), len(body), self.ap, bad)
        with self.assertRaises(ValueError):   # short body
            mi.receive_upload(io.BytesIO(body), len(body) + 10, self.ap, 'y-20261109.pst')
        self.assertEqual(os.listdir(self.ap['incoming']), [])


# ---------------------------------------------------------------------------
# App wiring: routes, the cron entry, search
# ---------------------------------------------------------------------------

class AppTests(unittest.TestCase):
    USER = 'bob'
    ACCT = 'ab1234c@student.example.edu'

    def setUp(self):
        # app is imported once per test run with the first test module's
        # data dir; give each test its own and put the shared one back.
        self._orig_data = ma.CONFIG['data_dir']
        self._td = tempfile.TemporaryDirectory(prefix='ma-pst-app-')
        self.data = self._td.name
        ma.CONFIG['data_dir'] = self.data
        cfg = ma.get_user_config_dir(self.USER)
        (cfg / 'accounts.json').write_text(json.dumps([
            {'email': self.ACCT, 'provider': 'hotmail', 'enabled': True,
             'sync_interval': 'manual'}]))
        ma.app.config['TESTING'] = True
        self.client = ma.app.test_client()
        with self.client.session_transaction() as s:
            s['username'] = self.USER
            s['login_time'] = time.time()
        self.paths = pst.import_paths(self.data, self.USER)
        self.ap = pst.account_paths(self.data, self.USER, self.ACCT)

    def tearDown(self):
        ma.CONFIG['data_dir'] = self._orig_data
        self._td.cleanup()

    def save_config(self, **extra):
        form = {'enabled': '1', 'schedule_kind': 'weekly', 'weekday': '0', 'hour': '6',
                'map_pattern': ['ab1234c@example.edu-*.pst', ''],
                'map_account': [self.ACCT, self.ACCT], 'keep_recent': '4',
                'keep_monthly': '3'}
        form.update(extra)
        return self.client.post('/import/config', data=form)

    def test_config_roundtrip_and_page(self):
        r = self.save_config()
        self.assertEqual(r.status_code, 302)
        cfg = ma.load_import_config(self.USER)
        self.assertTrue(cfg['enabled'])
        self.assertEqual(cfg['schedule'], {'kind': 'weekly', 'weekday': 0, 'hour': 6})
        self.assertEqual(cfg['mappings'], [{'pattern': 'ab1234c@example.edu-*.pst',
                                            'account': self.ACCT}])
        r = self.save_config(watch_dir='/etc')
        self.assertEqual(ma.load_import_config(self.USER)['watch_dir'], '')
        page = self.client.get('/import')
        self.assertEqual(page.status_code, 200)
        self.assertIn(b'Scheduled import', page.data)
        self.assertIn(b'Import mail', self.client.get('/dashboard').data)

    def test_upload_route_streams_past_the_256k_cap(self):
        body = json.dumps({'folders': {'Inbox': [msg('up@x', body='x' * 300_000)]}}).encode()
        name = 'ab1234c@example.edu-20261116.pst'
        with patch.object(ma, '_spawn_mail_import') as spawn:
            r = self.client.post(f'/import/upload?import=1&account={self.ACCT}&name={name}',
                                 data=body, content_type='application/octet-stream')
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.get_json()['sha256'], hashlib.sha256(body).hexdigest())
        spawn.assert_called_once_with(self.USER, self.ACCT, name)
        self.assertTrue(os.path.exists(os.path.join(self.ap['inbox'], name + '.sha256')))
        # other routes keep the cap
        # (the app's 413 handler flashes and redirects to the dashboard)
        r = self.client.post('/import/config', data={'enabled': '1', 'x': 'y' * 300_000})
        self.assertTrue(r.location.endswith('/dashboard'), r.location)

    def test_scheduled_cron_entry_imports_and_search_finds_it(self):
        self.save_config()
        os.makedirs(self.paths['incoming'], exist_ok=True)
        name = 'ab1234c@example.edu-20261109.pst'
        p = os.path.join(self.paths['watch'], name)
        write_pst(p, {'Inbox': [msg('cron@x', subject='Quarterly zebra report')],
                      'Sent Items': [msg('sent@x', subject='re: zebra')]})
        sidecar_for(p)
        with patch.object(pst, 'STABLE_WAIT', 0), redirect_stdout(io.StringIO()):
            ma.scheduled_mail_import(now=ts(2026, 11, 9, 6, 30))
        led = ma.open_import_ledger(self.USER)
        rows = led.rows()
        led.close()
        self.assertEqual((rows[0]['status'], rows[0]['imported']), ('done', 2), rows[0])
        self.assertEqual(ma.load_import_state(self.USER)['last_scheduled_run'],
                         ts(2026, 11, 9, 6, 30))
        from search_index import search_fts
        hits = search_fts(self.USER, self.data, 'zebra')
        self.assertEqual(sorted(h['folder'] for h in hits['results']),
                         ['PST/Inbox', 'PST/Sent Items'], hits)
        page = self.client.get('/import').data.decode()
        self.assertIn('tag-done', page)
        self.assertIn('archived as ab1234c_at_student_example_edu/done/'
                      'ab1234c@example.edu-20261109.pst', page)
        self.assertIn('kept: recent', page)               # retention column
        self.assertEqual(self.client.get('/api/import/status').get_json()['rows'][0]['imported'], 2)
        # not due again until next Monday
        write_pst(os.path.join(self.paths['watch'], 'ab1234c@example.edu-20261110.pst'),
                  {'Inbox': [msg('later@x')]})
        with redirect_stdout(io.StringIO()):
            ma.scheduled_mail_import(now=ts(2026, 11, 10, 6, 30))
        led = ma.open_import_ledger(self.USER)
        self.assertEqual(len(led.rows()), 1)
        led.close()
        # files waiting in _import/ are never searched as mail
        self.assertEqual(ma.search_maildir(self.USER, 'later'), [])

    def test_run_route_spawns_only_for_files_in_watch(self):
        with patch.object(ma, '_spawn_mail_import') as spawn:
            self.client.post('/import/run', data={'file': '../../etc/passwd'})
            self.client.post('/import/run', data={'file': '../../etc/passwd',
                                                  'account': self.ACCT})
            self.client.post('/import/run', data={'file': 'x.pst', 'account': 'other@x.com'})
            spawn.assert_not_called()

    def test_lock_prevents_parallel_imports(self):
        fd = ma._import_lock(self.USER)
        try:
            self.assertTrue(ma.mail_import_running(self.USER))
            with redirect_stdout(io.StringIO()):
                self.assertIsNone(ma.mail_import_user(self.USER, trigger='manual'))
        finally:
            os.close(fd)
        self.assertFalse(ma.mail_import_running(self.USER))


if __name__ == '__main__':
    unittest.main()
