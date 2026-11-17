"""1.3.0 general mail importer: every format, content detection, the
per-account import folders, the ledger, message dedupe across formats,
prefix folders, the read flag, and the retention guard (R1-R6 of the ADR
"retention can never rotate a backup set to empty").

Every fixture is generated here; there is no real mail. readpst and
msgconvert are replaced by tests/fake_readpst.py and
tests/fake_msgconvert.py.
"""

import datetime
import gzip
import hashlib
import io
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
import zipfile
from contextlib import redirect_stdout
from unittest.mock import patch

import pytest

# Linux-only. The Windows paths of the same code are covered by
# tests/test_windows_import_port.py.
pytestmark = pytest.mark.skipif(
    os.name == 'nt',
    reason='fixtures are POSIX: readpst / msgconvert stand-ins are "#!" scripts made '
           'executable with chmod, Maildir fixtures are named with ":2,", and the '
           'link tests need os.symlink without privilege')

os.environ['TZ'] = 'UTC'
if hasattr(time, 'tzset'):          # not on Windows (module skipped there)
    time.tzset()
os.environ.setdefault('MAIL_ARCHIVER_DATA', tempfile.mkdtemp(prefix='ma-test-data-'))
os.environ['MAIL_ARCHIVER_AUTH'] = 'builtin'
os.environ['MAIL_ARCHIVER_SECRET'] = 'test-only-secret'
os.environ.pop('MAIL_ARCHIVER_SECRET_FILE', None)

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
FAKE_READPST = os.path.join(HERE, 'fake_readpst.py')
FAKE_MSGCONVERT = os.path.join(HERE, 'fake_msgconvert.py')
for _p in (FAKE_READPST, FAKE_MSGCONVERT):
    os.chmod(_p, 0o755)
os.environ['MAIL_ARCHIVER_READPST'] = FAKE_READPST
os.environ['MAIL_ARCHIVER_MSGCONVERT'] = FAKE_MSGCONVERT

import pst_import as pst  # noqa: E402
pst.READPST = FAKE_READPST
import mail_import as mi  # noqa: E402
mi.MSGCONVERT = FAKE_MSGCONVERT
import search_index       # noqa: E402
import app as ma          # noqa: E402

NO_MSGCONVERT = '/nonexistent/msgconvert'


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------

def msg(mid, subject='hello', body='body text', frm='a@example.com'):
    h = f'Message-ID: <{mid}>\r\n' if mid else ''
    return (f'{h}From: {frm}\r\nTo: b@example.com\r\nSubject: {subject}\r\n'
            f'Date: Mon, 02 Nov 2026 10:00:00 +0000\r\n\r\n{body}\r\n')


def raw(mid, **kw):
    return msg(mid, **kw).encode()


def write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'wb') as f:
        f.write(data)
    return path


def pst_bytes(folders, **extra):
    spec = {'root': 'Outlook Data File', 'folders': folders}
    spec.update(extra)
    return json.dumps(spec).encode()


def mbox_bytes(messages, quote=True):
    """mboxrd: LF line ends, a From_ line per message, body lines that
    start with From (or >From) get one more '>'."""
    out = []
    for m in messages:
        m = m.replace(b'\r\n', b'\n')
        lines = m.split(b'\n')
        if quote:
            lines = [(b'>' + l) if l.lstrip(b'>').startswith(b'From ') else l for l in lines]
        out.append(b'From MAILER-DAEMON Mon Nov  2 10:00:00 2026\n' + b'\n'.join(lines) + b'\n')
    return b''.join(out)


def make_maildir(root, folders):
    """folders: {'': [(raw, 'cur'|'new', flags)], '.Sent': [...]}"""
    for sub in ('cur', 'new', 'tmp'):
        os.makedirs(os.path.join(root, sub), exist_ok=True)
    n = 0
    for folder, items in folders.items():
        base = os.path.join(root, folder) if folder else root
        for sub in ('cur', 'new', 'tmp'):
            os.makedirs(os.path.join(base, sub), exist_ok=True)
        for data, sub, flags in items:
            n += 1
            name = f'17000000{n:02d}.M1P1.host' + (f':2,{flags}' if sub == 'cur' else '')
            write(os.path.join(base, sub, name), data)
    return root


def emlx_bytes(data):
    plist = (b'<?xml version="1.0" encoding="UTF-8"?>\n<plist version="1.0"><dict>'
             b'<key>flags</key><integer>8590195713</integer></dict></plist>\n')
    return str(len(data)).encode() + b'\n' + data + plist


CFB = bytes.fromhex('D0CF11E0A1B11AE1')


def msg_bytes(data, fail=False):
    return (CFB + b'\0' * 504 + '__substg1.0_0037001F'.encode('utf-16-le') + b'\0' * 64
            + (b'--FAIL--' if fail else b'') + b'\n--EML--\n' + data)


def zip_bytes(members, raw_infos=()):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        for name, data in members.items():
            zf.writestr(name, data)
        for info, data in raw_infos:
            zf.writestr(info, data)
    return buf.getvalue()


def targz_bytes(members, extra=()):
    """members: {name: bytes}; extra: [TarInfo] (links, devices, ...)."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode='w:gz') as tf:
        for name, data in members.items():
            ti = tarfile.TarInfo(name)
            ti.size = len(data)
            tf.addfile(ti, io.BytesIO(data))
        for ti in extra:
            tf.addfile(ti)
    return buf.getvalue()


def tarinfo(name, kind, linkname=''):
    ti = tarfile.TarInfo(name)
    ti.type, ti.linkname = kind, linkname
    return ti


def maildir_messages(maildir):
    out = []
    for root, _, files in os.walk(maildir):
        if os.path.basename(root) in ('cur', 'new'):
            rel = os.path.relpath(os.path.dirname(root), maildir)
            out.extend((rel, f) for f in files)
    return sorted(out)


def folders_of(maildir):
    return sorted({f for f, _ in maildir_messages(maildir)})


def ts(y, mo, d, h=0, mi_=0):
    return time.mktime((y, mo, d, h, mi_, 0, 0, 0, -1))


class Tmp(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.tmp = self._td.name

    def tearDown(self):
        self._td.cleanup()


# ---------------------------------------------------------------------------
# Type detection by content
# ---------------------------------------------------------------------------

class DetectionTests(Tmp):
    def kind(self, name, data):
        return mi.detect_unit(write(os.path.join(self.tmp, name), data))[0]

    def test_each_type_is_detected_from_its_content(self):
        self.assertEqual(self.kind('a', b'!BDN' + b'\0' * 600), 'pst')
        self.assertEqual(self.kind('b', mbox_bytes([raw('1@x')])), 'mbox')
        self.assertEqual(self.kind('c', raw('1@x')), 'eml')
        self.assertEqual(self.kind('d', emlx_bytes(raw('1@x'))), 'emlx')
        self.assertEqual(self.kind('e', msg_bytes(raw('1@x'))), 'msg')
        self.assertEqual(self.kind('f', zip_bytes({'x.eml': raw('1@x')})), 'zip')
        self.assertEqual(self.kind('g', targz_bytes({'x.eml': raw('1@x')})), 'gzip')
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode='w') as tf:
            ti = tarfile.TarInfo('x.eml')
            ti.size = 3
            tf.addfile(ti, io.BytesIO(b'abc'))
        self.assertEqual(self.kind('h', buf.getvalue()), 'tar')
        make_maildir(os.path.join(self.tmp, 'md'), {'': []})
        self.assertEqual(mi.detect_unit(os.path.join(self.tmp, 'md'))[0], 'maildir')
        os.makedirs(os.path.join(self.tmp, 'plain'))
        self.assertEqual(mi.detect_unit(os.path.join(self.tmp, 'plain'))[0], 'directory')

    def test_wrong_extension_is_still_detected_correctly(self):
        self.assertEqual(self.kind('mail.pst', mbox_bytes([raw('1@x')])), 'mbox')
        self.assertEqual(self.kind('photo.jpg', raw('1@x')), 'eml')
        self.assertEqual(self.kind('export.mbox', zip_bytes({'x.eml': raw('1@x')})), 'zip')
        self.assertEqual(self.kind('letter.eml', msg_bytes(raw('1@x'))), 'msg')
        self.assertEqual(self.kind('1.eml', emlx_bytes(raw('1@x'))), 'emlx')
        self.assertEqual(self.kind('backup.zip', targz_bytes({'x.eml': raw('1@x')})), 'gzip')
        self.assertEqual(self.kind('real.mbox', b'!BDN' + b'\0' * 600), 'pst')
        make_maildir(os.path.join(self.tmp, 'looks-like.pst'), {'': []})
        self.assertEqual(mi.detect_unit(os.path.join(self.tmp, 'looks-like.pst'))[0], 'maildir')

    def test_msg_needs_the_ole_magic_and_msg_streams(self):
        # an old Office document: OLE magic, no MSG streams
        typ, why = mi.detect_unit(write(os.path.join(self.tmp, 'report.doc'),
                                        CFB + b'\0' * 4096 + b'WordDocument'))
        self.assertIsNone(typ)
        self.assertIn('OLE', why)
        # stream names only where the header says the directory starts,
        # beyond the first 64 KiB that are always read
        head = bytearray(CFB + b'\0' * 504)
        head[0x1E:0x20] = (9).to_bytes(2, 'little')
        head[0x30:0x34] = (300).to_bytes(4, 'little')
        body = bytes(head) + b'\0' * (301 * 512 - 512) + \
            '__properties_version1.0'.encode('utf-16-le') + b'\0' * 512
        self.assertEqual(self.kind('far.bin', body), 'msg')

    def test_unknown_content_is_not_guessed(self):
        self.assertIsNone(self.kind('random.bin', bytes(range(256)) * 20))
        self.assertIsNone(self.kind('notes.txt', b'Note: buy milk\n\nand eggs\n'))
        self.assertIsNone(self.kind('empty.eml', b''))
        self.assertIsNone(self.kind('page.html', b'<html><body>From: me</body></html>'))
        os.symlink('/etc/passwd', os.path.join(self.tmp, 'link.eml'))
        self.assertIsNone(mi.detect_unit(os.path.join(self.tmp, 'link.eml'))[0])

    def test_extension_is_only_a_fallback(self):
        # nothing in the content says PST (the test fixtures are JSON)
        typ, how = mi.detect_unit(write(os.path.join(self.tmp, 'x.pst'), pst_bytes({})))
        self.assertEqual(typ, 'pst')
        self.assertIn('named .pst', how)
        # headers nobody uses: mail only because it is called .eml
        odd = b'X-Custom-One: 1\nX-Custom-Two: 2\n\nbody\n'
        self.assertEqual(self.kind('odd.eml', odd), 'eml')
        self.assertIsNone(self.kind('odd.txt', odd))

    def test_header_block_rules(self):
        self.assertTrue(mi.looks_like_headers(b'Subject: x\r\n\r\nbody'))
        self.assertTrue(mi.looks_like_headers(b'Received: a\n\tfolded\nFrom: b\n'))
        self.assertFalse(mi.looks_like_headers(b' folded first\nFrom: b\n\n'))
        self.assertFalse(mi.looks_like_headers(b'Hello there\nFrom: b\n\n'))
        self.assertFalse(mi.looks_like_headers(b''))


# ---------------------------------------------------------------------------
# The converter: one test (or more) per format
# ---------------------------------------------------------------------------

class FormatTests(Tmp):
    def setUp(self):
        super().setUp()
        self.acct = os.path.join(self.tmp, 'acct')
        self.n = 0

    def imp(self, path, typ=None, **kw):
        self.n += 1
        typ = typ or mi.detect_unit(path)[0]
        kw.setdefault('readpst', FAKE_READPST)
        kw.setdefault('msgconvert', FAKE_MSGCONVERT)
        return mi.convert_unit(path, typ, self.acct, os.path.join(self.tmp, f'work{self.n}'),
                               cache_path=os.path.join(self.tmp, 'ids.json'), **kw)

    def f(self, name, data):
        return write(os.path.join(self.tmp, 'src', name), data)

    # -- PST ---------------------------------------------------------------
    def test_pst(self):
        s = self.imp(self.f('a.pst', pst_bytes({'Inbox': [msg('1@x')], 'Sent Items': [msg('2@x')]})))
        self.assertEqual((s['added'], s['duplicates'], s['failed']), (2, 0, 0))
        self.assertEqual(folders_of(self.acct), ['PST/Inbox', 'PST/Sent Items'])
        self.assertEqual(s['prefixes'], ['PST/'])

    # -- mbox --------------------------------------------------------------
    def test_mbox(self):
        s = self.imp(self.f('Archive.mbox', mbox_bytes([raw('1@x'), raw('2@x'), raw('3@x')])))
        self.assertEqual((s['added'], s['failed']), (3, 0))
        self.assertEqual(folders_of(self.acct), ['Imported/mbox/Archive'])

    def test_mbox_from_quoting(self):
        body = 'line one\r\nFrom the desk of the dean\r\n>From an earlier quote\r\nlast\r\n'
        s = self.imp(self.f('q.mbox', mbox_bytes([raw('q1@x', body=body), raw('q2@x')])))
        self.assertEqual(s['added'], 2)
        cur = os.path.join(self.acct, 'Imported', 'mbox', 'q', 'cur')
        texts = [open(os.path.join(cur, n), 'rb').read() for n in sorted(os.listdir(cur))]
        first = next(t for t in texts if b'q1@x' in t)
        # the quoting added by the mbox writer is undone, exactly one level
        self.assertIn(b'\nFrom the desk of the dean\n', first)
        self.assertIn(b'\n>From an earlier quote\n', first)
        self.assertNotIn(b'MAILER-DAEMON', first)           # the From_ line is not mail
        # byte for byte the message that went in (mbox line ends are LF)
        self.assertEqual(first, raw('q1@x', body=body).replace(b'\r\n', b'\n'))

    def test_mbox_unquoted_from_in_a_body_does_not_split_the_message(self):
        # mboxo / Apple exports do not always quote; "From " + a non-header
        # line is body text, not a new message
        body = 'see below\r\nFrom here on it gets easier\r\nand that is all\r\n'
        s = self.imp(self.f('u.mbox', mbox_bytes([raw('u1@x', body=body), raw('u2@x')],
                                                 quote=False)))
        self.assertEqual((s['added'], s['failed']), (2, 0))

    def test_mbox_is_streamed_and_oversized_message_is_skipped(self):
        big = raw('big@x', body='x' * 50_000)
        p = self.f('big.mbox', mbox_bytes([raw('a@x'), big, raw('b@x')]))
        with open(p, 'rb') as fh:
            sizes = [(r is None, n) for r, n in mi.iter_mbox(fh, 10_000)]
        self.assertEqual([s[0] for s in sizes], [False, True, False])
        self.assertGreater(sizes[1][1], 50_000)
        s = self.imp(p, max_message_bytes=10_000)
        self.assertEqual((s['added'], s['too_large'], s['failed']), (2, 1, 0))

    def test_mbox_thunderbird_and_apple_folder_names(self):
        self.assertEqual(mi.mbox_folder_parts(['Mail', 'Inbox.sbd'], 'Receipts'),
                         ['Mail', 'Inbox', 'Receipts'])
        self.assertEqual(mi.mbox_folder_parts(['Old.mbox'], 'mbox'), ['Old'])
        self.assertEqual(mi.mbox_folder_parts([], 'All mail Including Spam and Trash.mbox'),
                         ['All mail Including Spam and Trash'])

    # -- Maildir -----------------------------------------------------------
    def test_maildir(self):
        md = make_maildir(os.path.join(self.tmp, 'src', 'Maildir'), {
            '': [(raw('1@x'), 'cur', 'RS'), (raw('2@x'), 'new', '')],
            'Lists/python': [(raw('3@x'), 'cur', 'F')]})
        write(os.path.join(md, 'dovecot.index'), b'\0\1\2')       # not mail, not read
        s = self.imp(md)
        self.assertEqual((s['added'], s['failed'], s['ignored']), (3, 0, 0))
        self.assertEqual(folders_of(self.acct),
                         ['Imported/Maildir/INBOX', 'Imported/Maildir/Lists/python'])
        names = {f for _, f in maildir_messages(self.acct)}
        self.assertEqual({n.split(':2,')[1] for n in names}, {'RS', 'S', 'FS'})

    def test_maildirpp(self):
        md = make_maildir(os.path.join(self.tmp, 'src', 'Maildir'), {
            '': [(raw('1@x'), 'cur', 'S')],
            '.Sent': [(raw('2@x'), 'cur', 'S')],
            '.Archive.2020': [(raw('3@x'), 'cur', '')]})
        s = self.imp(md)
        self.assertEqual(s['added'], 3)
        self.assertEqual(folders_of(self.acct), [
            'Imported/Maildir/Archive/2020', 'Imported/Maildir/INBOX', 'Imported/Maildir/Sent'])

    def test_maildirpp_root_without_its_own_mail_folders(self):
        root = os.path.join(self.tmp, 'src', 'pp')
        make_maildir(os.path.join(root, '.Drafts'), {'': [(raw('d@x'), 'cur', 'D')]})
        self.assertEqual(mi.detect_unit(root)[0], 'maildir')
        self.imp(root)
        self.assertEqual(folders_of(self.acct), ['Imported/Maildir/Drafts'])

    def test_maildir_links_are_not_followed(self):
        md = make_maildir(os.path.join(self.tmp, 'src', 'Maildir'), {'': [(raw('1@x'), 'cur', 'S')]})
        os.symlink('/etc/passwd', os.path.join(md, 'cur', 'evil:2,S'))
        s = self.imp(md)
        self.assertEqual((s['added'], s['skipped_links']), (1, 1))

    # -- eml ---------------------------------------------------------------
    def test_single_eml(self):
        s = self.imp(self.f('note.eml', raw('1@x')))
        self.assertEqual(s['added'], 1)
        self.assertEqual(folders_of(self.acct), ['Imported/eml'])

    def test_eml_directory(self):
        d = os.path.join(self.tmp, 'src', 'old mail')
        write(os.path.join(d, 'a.eml'), raw('1@x'))
        write(os.path.join(d, 'b.eml'), raw('2@x'))
        write(os.path.join(d, '2019', 'c.eml'), raw('3@x'))
        write(os.path.join(d, 'Thumbs.db'), b'\0\1\2\3')
        write(os.path.join(d, '.DS_Store'), b'\0\1')
        os.symlink('/etc/passwd', os.path.join(d, 'link.eml'))
        s = self.imp(d)
        self.assertEqual((s['added'], s['failed'], s['ignored'], s['skipped_links']), (3, 0, 1, 1))
        self.assertEqual(folders_of(self.acct),
                         ['Imported/eml/old mail', 'Imported/eml/old mail/2019'])

    def test_directory_without_mail_is_refused(self):
        d = os.path.join(self.tmp, 'src', 'photos')
        write(os.path.join(d, 'a.jpg'), b'\xff\xd8\xff\xe0' + b'\0' * 100)
        with self.assertRaises(mi.ImportFailed) as cm:
            self.imp(d)
        self.assertIn('no mail found', str(cm.exception))

    # -- emlx --------------------------------------------------------------
    def test_emlx(self):
        data = raw('1@x')
        s = self.imp(self.f('1234.emlx', emlx_bytes(data)))
        self.assertEqual(s['added'], 1)
        self.assertEqual(folders_of(self.acct), ['Imported/emlx'])
        cur = os.path.join(self.acct, 'Imported', 'emlx', 'cur')
        stored = open(os.path.join(cur, os.listdir(cur)[0]), 'rb').read()
        self.assertEqual(stored, data)                  # no count line, no plist

    def test_emlx_truncated_is_reported(self):
        s = self.imp(self.f('t.emlx', b'99999\n' + raw('1@x')), typ='emlx')
        self.assertEqual((s['added'], s['failed']), (0, 1))

    def test_emlx_apple_mail_tree_folder_names(self):
        d = os.path.join(self.tmp, 'src', 'V10')
        write(os.path.join(d, 'Work.mbox', 'ABCD-1234', 'Data', 'Messages', '1.emlx'),
              emlx_bytes(raw('1@x')))
        self.imp(d)
        self.assertEqual(folders_of(self.acct), ['Imported/emlx/V10/Work'])

    # -- msg ---------------------------------------------------------------
    def test_msg_with_msgconvert(self):
        s = self.imp(self.f('letter.msg', msg_bytes(raw('1@x', subject='from outlook'))))
        self.assertEqual((s['added'], s['failed']), (1, 0))
        self.assertEqual(folders_of(self.acct), ['Imported/msg'])

    def test_msg_refused_when_msgconvert_is_missing(self):
        p = self.f('letter.msg', msg_bytes(raw('1@x')))
        with self.assertRaises(mi.ImportFailed) as cm:
            self.imp(p, msgconvert=NO_MSGCONVERT)
        self.assertIn('libemail-outlook-message-perl', str(cm.exception))
        self.assertEqual(maildir_messages(self.acct), [])

    def test_msg_that_msgconvert_rejects_fails(self):
        with self.assertRaises(mi.ImportFailed) as cm:
            self.imp(self.f('bad.msg', msg_bytes(raw('1@x'), fail=True)))
        self.assertIn('msgconvert exited 1', str(cm.exception))

    # -- containers --------------------------------------------------------
    def test_zip(self):
        z = zip_bytes({
            'Takeout/Mail/All mail.mbox': mbox_bytes([raw('1@x'), raw('2@x')]),
            'Takeout/Mail/loose/a.eml': raw('3@x'),
            'Takeout/archive_browser.html': b'<html></html>',
            'outlook/old.pst': pst_bytes({'Inbox': [msg('4@x')]}),
        })
        s = self.imp(self.f('takeout.zip', z))
        self.assertEqual((s['added'], s['failed'], s['ignored'], s['refused_members']),
                         (4, 0, 1, 0))
        self.assertEqual(folders_of(self.acct), [
            'Imported/eml/Takeout/Mail/loose', 'Imported/mbox/Takeout/Mail/All mail',
            'PST/outlook/Inbox'])
        self.assertEqual(sorted(s['prefixes']), ['Imported/eml/', 'Imported/mbox/', 'PST/'])

    def test_zip_traversal_and_link_members_are_refused(self):
        link = zipfile.ZipInfo('link.eml')
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        z = zip_bytes({'ok.eml': raw('1@x'), '../../evil.eml': raw('2@x'),
                       '/abs/evil2.eml': raw('3@x'), 'C:\\evil3.eml': raw('4@x')},
                      raw_infos=[(link, b'/etc/passwd')])
        s = self.imp(self.f('bad.zip', z))
        self.assertEqual((s['added'], s['refused_members']), (1, 4))
        self.assertEqual(mi.stats_status(s), 'partial')
        self.assertFalse(os.path.exists(os.path.join(self.tmp, 'evil.eml')))
        self.assertEqual(folders_of(self.acct), ['Imported/eml'])

    def test_tar_gz_with_a_maildir_inside(self):
        buf = io.BytesIO()
        md = make_maildir(os.path.join(self.tmp, 'pack', 'Maildir'), {
            '': [(raw('1@x'), 'cur', 'S')], '.Sent': [(raw('2@x'), 'cur', 'S')]})
        with tarfile.open(fileobj=buf, mode='w:gz') as tf:
            tf.add(md, arcname='home/Maildir')
        s = self.imp(self.f('home.tar.gz', buf.getvalue()))
        self.assertEqual((s['added'], s['failed']), (2, 0))
        self.assertEqual(folders_of(self.acct), [
            'Imported/Maildir/home/Maildir/INBOX', 'Imported/Maildir/home/Maildir/Sent'])

    def test_zip_of_a_maildir_without_its_empty_directories(self):
        # zip tools often leave out empty dirs, so new/ and tmp/ are gone
        z = zip_bytes({'Maildir/cur/1700000001.M1P1.host:2,S': raw('1@x'),
                       'Maildir/.Sent/cur/1700000002.M1P1.host:2,S': raw('2@x')})
        s = self.imp(self.f('md.zip', z))
        self.assertEqual((s['added'], s['failed'], s['ignored']), (2, 0, 0))
        self.assertEqual(folders_of(self.acct), [
            'Imported/Maildir/Maildir/INBOX', 'Imported/Maildir/Maildir/Sent'])

    def test_tar_gz_traversal_link_and_device_members_are_refused(self):
        t = targz_bytes(
            {'mail/ok.eml': raw('1@x'), '../../escape.eml': raw('2@x'),
             '/etc/cron.d/evil': raw('3@x')},
            extra=[tarinfo('mail/sym.eml', tarfile.SYMTYPE, '/etc/passwd'),
                   tarinfo('mail/hard.eml', tarfile.LNKTYPE, 'mail/ok.eml'),
                   tarinfo('mail/dev', tarfile.CHRTYPE),
                   tarinfo('mail/fifo', tarfile.FIFOTYPE)])
        s = self.imp(self.f('bad.tar.gz', t))
        self.assertEqual((s['added'], s['refused_members']), (1, 6))
        self.assertEqual(mi.stats_status(s), 'partial')
        self.assertTrue(any('path leaves the archive' in e for e in s['errors']))
        self.assertTrue(any('link or special file' in e for e in s['errors']))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, 'escape.eml')))
        self.assertEqual(folders_of(self.acct), ['Imported/eml/mail'])

    def test_container_with_absurd_expansion_is_refused_cleanly(self):
        z = zip_bytes({'ok.eml': raw('1@x'), 'zeros.mbox': b'\0' * (4 * 1024 * 1024)})
        with self.assertRaises(mi.ImportFailed) as cm:
            self.imp(self.f('bomb.zip', z), ratio_floor=1024 * 1024, max_ratio=20)
        self.assertIn('decompression bomb', str(cm.exception))
        self.assertEqual(maildir_messages(self.acct), [])     # nothing delivered
        # a tar stream is judged while it is unpacked
        t = targz_bytes({'zeros.mbox': b'\0' * (4 * 1024 * 1024)})
        with self.assertRaises(mi.ImportFailed):
            self.imp(self.f('bomb.tar.gz', t), ratio_floor=1024 * 1024, max_ratio=20)
        with self.assertRaises(mi.ImportFailed) as cm:
            self.imp(self.f('big.zip', zip_bytes({'a.eml': raw('1@x') * 50})),
                     max_unpack_bytes=1000)
        self.assertIn('byte limit', str(cm.exception))
        with self.assertRaises(mi.ImportFailed):
            self.imp(self.f('many.zip', zip_bytes({f'{i}.eml': raw(f'{i}@x') for i in range(5)})),
                     max_members=3)
        self.assertEqual(os.listdir(self.tmp).count('work1'), 0)   # work dirs removed

    def test_nested_container_one_level_only(self):
        inner = zip_bytes({'in.eml': raw('in@x')})
        s = self.imp(self.f('outer.zip', zip_bytes({'top.eml': raw('top@x'), 'inner.zip': inner})))
        self.assertEqual((s['added'], s['failed']), (2, 0))
        deep = zip_bytes({'mid.zip': zip_bytes({'deep.zip': inner, 'mid.eml': raw('mid@x')})})
        s = self.imp(self.f('deep.zip', deep))
        self.assertEqual((s['added'], s['failed']), (1, 1))            # mid.eml in, deep.zip refused
        self.assertTrue(any('one level of nesting' in e for e in s['errors']))

    def test_single_compressed_file(self):
        s = self.imp(self.f('Sent.mbox.gz', gzip.compress(mbox_bytes([raw('1@x'), raw('2@x')]))))
        self.assertEqual(s['added'], 2)
        self.assertEqual(folders_of(self.acct), ['Imported/mbox/Sent'])

    @unittest.skipUnless(shutil.which('zstd'), 'zstd not installed')
    def test_zstd_compressed_file(self):
        src = self.f('Old.mbox', mbox_bytes([raw('z@x')]))
        subprocess.run(['zstd', '-q', src, '-o', src + '.zst'], check=True)
        s = self.imp(src + '.zst')
        self.assertEqual(s['added'], 1)
        self.assertEqual(folders_of(self.acct), ['Imported/mbox/Old'])

    def test_damaged_archive_fails_cleanly(self):
        good = targz_bytes({'a.eml': raw('1@x') * 200})
        with self.assertRaises(mi.ImportFailed):
            self.imp(self.f('cut.tar.gz', good[:len(good) // 2]))
        with self.assertRaises(mi.ImportFailed):
            self.imp(self.f('cut.zip', b'PK\x03\x04' + b'\0' * 100))

    # -- every format -------------------------------------------------------
    def test_every_format_is_marked_read_and_private(self):
        make_maildir(os.path.join(self.tmp, 'src', 'md'), {'': [(raw('md@x'), 'new', '')]})
        for name, data in (
                ('a.pst', pst_bytes({'Inbox': [msg('pst@x')]})),
                ('b.mbox', mbox_bytes([raw('mbox@x')])), ('c.eml', raw('eml@x')),
                ('d.emlx', emlx_bytes(raw('emlx@x'))), ('e.msg', msg_bytes(raw('msg@x'))),
                ('f.zip', zip_bytes({'z.eml': raw('zip@x')}))):
            self.imp(self.f(name, data))
        self.imp(os.path.join(self.tmp, 'src', 'md'))
        got = maildir_messages(self.acct)
        self.assertEqual(len(got), 7)
        for folder, fn in got:
            self.assertTrue(fn.endswith(':2,S'), (folder, fn))
            p = os.path.join(self.acct, folder, 'cur', fn)
            self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), 0o600)
        self.assertEqual([f for f in folders_of(self.acct) if not
                          (f.startswith('PST/') or f.startswith('Imported/'))], [])
        for root, dirs, _ in os.walk(self.acct):
            for d in dirs:
                self.assertEqual(stat.S_IMODE(os.stat(os.path.join(root, d)).st_mode), 0o700)

    def test_converter_child_runs_isolated_and_prints_json(self):
        p = self.f('c.mbox', mbox_bytes([raw('cli@x')]))
        res = subprocess.run(
            [sys.executable, '-I', os.path.join(ROOT, 'mail_import.py'), 'convert',
             '--path', p, '--type', 'mbox', '--maildir', self.acct,
             '--work', os.path.join(self.tmp, 'w')],
            capture_output=True, text=True, cwd=self.tmp, env={'PATH': '/usr/bin:/bin'})
        out = json.loads(res.stdout.strip().splitlines()[-1])
        self.assertEqual((res.returncode, out['status'], out['added']), (0, 'ok', 1), res.stderr)


# ---------------------------------------------------------------------------
# The per-user pass: account folders, ledger, dedupe, failed/, retention
# ---------------------------------------------------------------------------

class Flow(Tmp):
    USER = 'fam'
    A = 'alice@example.com'
    B = 'bob.smith@example.org'

    def setUp(self):
        super().setUp()
        self.now = ts(2026, 11, 9, 7)
        self.indexed = []
        self.msgconvert = FAKE_MSGCONVERT
        self.force = {}                     # file name -> stats override
        self.make()

    def make(self, **cfg):
        self.cfg = pst.normalize_config(cfg, [self.A, self.B])
        self.imp = mi.UserImporter(
            self.tmp, self.USER, [self.A, self.B], self.cfg,
            convert=self.convert, prepare=lambda p: None, index=self.indexed.append,
            open_ledger=self.ledger, log=lambda m: None, now=lambda: self.now,
            sleep=lambda s: None, stable_wait=0)
        self.imp.ensure_dirs()
        return self.imp

    def ledger(self):
        return mi.Ledger(search_index.get_index_db(self.USER, self.tmp))

    def rows(self):
        led = self.ledger()
        try:
            return led.rows(500)
        finally:
            led.close()

    def events(self):
        led = self.ledger()
        try:
            return led.events(5000)
        finally:
            led.close()

    def convert(self, path, typ, name, maildir, work, cache):
        try:
            s = mi.convert_unit(path, typ, maildir, work, name=name, readpst=FAKE_READPST,
                                msgconvert=self.msgconvert, cache_path=cache)
            s['status'] = mi.stats_status(s)
        except mi.ImportFailed as e:
            return {'status': 'failed', 'error': str(e)}
        s.update(self.force.get(name, {}))
        return s

    def ap(self, acct):
        return self.imp.account_paths(acct)

    def drop(self, acct, name, data):
        return write(os.path.join(self.ap(acct)['inbox'], name), data)

    def maildir(self, acct):
        return os.path.join(self.tmp, self.USER, pst.safe_account_name(acct))

    def seed_done(self, acct, dates, stem='export', imported_days_ago=1):
        """Copies in done/, and (unless imported_days_ago is None) the
        ledger row of the newest one: a successful import that long before
        `self.now`. Retention rule R4 asks for a recent success BEFORE the
        run that prunes."""
        d = self.ap(acct)['done']
        for day in dates:
            n = f'{stem}-{day:%Y%m%d}.pst'
            write(os.path.join(d, n + '.zst'), b'zst')
            write(os.path.join(d, n + '.sha256'), b'0' * 64 + b'  ' + n.encode() + b'\n')
        if imported_days_ago is not None and dates:
            self.seed_success(acct, f'{stem}-{max(dates):%Y%m%d}.pst', imported_days_ago)

    def seed_success(self, acct, name, days_ago):
        when = self.now - days_ago * 86400
        led = self.ledger()
        try:
            return led.add(status='done', original_name=name, account=acct,
                           source='inbox', trigger='scheduled',
                           sha256=hashlib.sha256(name.encode()).hexdigest(),
                           finished_at=pst.now_str(when), finished_epoch=when)
        finally:
            led.close()

    def done_dates(self, acct):
        return sorted(e['date'].isoformat() for e in mi.list_archive(self.ap(acct)['done']))

    def export(self, acct, day, mids=('1@x',), **extra):
        return self.drop(acct, f'export-{day:%Y%m%d}.pst',
                         pst_bytes({'Inbox': [msg(m) for m in mids]}, **extra))


def weekly(start, n):
    return [start + datetime.timedelta(days=7 * i) for i in range(n)]


D = datetime.date
# What a rotation on 2026-10-26 leaves behind: 4 recent + Sep, Aug, Jul.
STEADY = [D(2026, 7, 27), D(2026, 8, 31), D(2026, 9, 28), D(2026, 10, 5),
          D(2026, 10, 12), D(2026, 10, 19), D(2026, 10, 26)]


class AccountFolderTests(Flow):
    def test_layout_one_import_folder_per_account(self):
        root = os.path.join(self.tmp, self.USER, '_import')
        for slug in ('alice_at_example_com', 'bob_smith_at_example_org'):
            for sub in ('inbox', 'inbox/.incoming', 'done', 'failed'):
                self.assertTrue(os.path.isdir(os.path.join(root, slug, sub)), (slug, sub))
        self.assertEqual(self.ap(self.A)['inbox'],
                         os.path.join(root, 'alice_at_example_com', 'inbox'))
        # no legacy watch folder is made for a user who never had one
        self.assertFalse(os.path.exists(os.path.join(root, 'watch')))

    def test_the_folder_decides_the_target_account(self):
        self.drop(self.A, 'mail.mbox', mbox_bytes([raw('a1@x'), raw('a2@x')]))
        self.drop(self.B, 'mail.mbox', mbox_bytes([raw('b1@x')]))
        res = self.imp.run()['results']
        self.assertEqual(sorted((r['account'], r['imported']) for r in res),
                         [(self.A, 2), (self.B, 1)])
        self.assertEqual(len(maildir_messages(self.maildir(self.A))), 2)
        self.assertEqual(len(maildir_messages(self.maildir(self.B))), 1)
        self.assertEqual(sorted(self.indexed), [self.A, self.B])
        # each file is archived in its own account's done/
        self.assertEqual(sorted(os.listdir(self.ap(self.A)['done'])),
                         ['mail.mbox', 'mail.mbox.sha256'])
        self.assertEqual(os.listdir(self.ap(self.A)['inbox']), ['.incoming'])

    def test_a_directory_in_the_inbox_is_one_import_unit(self):
        make_maildir(os.path.join(self.ap(self.A)['inbox'], 'Maildir'),
                     {'': [(raw('1@x'), 'cur', 'S')], '.Sent': [(raw('2@x'), 'cur', 'S')]})
        d = os.path.join(self.ap(self.A)['inbox'], 'loose')
        write(os.path.join(d, 'a.eml'), raw('3@x'))
        res = {r['original_name']: r for r in self.imp.run()['results']}
        self.assertEqual((res['Maildir']['status'], res['Maildir']['detected_type'],
                          res['Maildir']['imported']), ('done', 'maildir', 2))
        self.assertEqual((res['loose']['status'], res['loose']['detected_type'],
                          res['loose']['target_prefix']), ('done', 'directory', 'Imported/eml/'))
        self.assertEqual(len(res['Maildir']['sha256']), 64)
        self.assertTrue(os.path.isdir(os.path.join(self.ap(self.A)['done'], 'Maildir', 'cur')))
        self.assertEqual(folders_of(self.maildir(self.A)), [
            'Imported/Maildir/INBOX', 'Imported/Maildir/Sent', 'Imported/eml/loose'])

    def test_producer_contract_in_the_account_inbox(self):
        ap = self.ap(self.A)
        data = mbox_bytes([raw('1@x')])
        # staging, *.tmp, an orphan sidecar and links are not import units
        write(os.path.join(ap['incoming'], 'half.mbox'), data[:10])
        write(os.path.join(ap['inbox'], 'part.mbox.tmp'), data)
        write(os.path.join(ap['inbox'], 'early.mbox.sha256'), b'0' * 64 + b'  early.mbox\n')
        os.symlink('/etc/passwd', os.path.join(ap['inbox'], 'link.eml'))
        good = self.drop(self.A, 'good.mbox', data)
        write(good + '.sha256', f'{hashlib.sha256(data).hexdigest()}  good.mbox\n'.encode())
        bad = self.drop(self.A, 'bad.mbox', mbox_bytes([raw('2@x')]))
        write(bad + '.sha256', b'0' * 64 + b'  bad.mbox\n')
        summary = self.imp.run()
        res = {r['original_name']: r for r in summary['results']}
        self.assertEqual(set(res), {'good.mbox', 'bad.mbox'})
        self.assertEqual(res['good.mbox']['status'], 'done')
        self.assertIn('sha256 verified', res['good.mbox']['detail']['checksum'])
        self.assertEqual(res['bad.mbox']['status'], 'failed')
        self.assertIn('mismatch', res['bad.mbox']['error'])
        self.assertIn(('alice_at_example_com/inbox/link.eml', 'symbolic link (not followed)'),
                      summary['ignored'])
        self.assertEqual(sorted(os.listdir(ap['inbox'])),
                         ['.incoming', 'early.mbox.sha256', 'link.eml', 'part.mbox.tmp'])

    def test_legacy_watch_folder_still_works(self):
        self.make(mappings=[{'pattern': 'alice@old.example-*.pst', 'account': self.A}])
        watch = self.imp.paths['watch']
        self.assertTrue(os.path.isdir(os.path.join(watch, '.incoming')))
        name = 'alice@old.example-20261109.pst'
        data = pst_bytes({'Inbox': [msg('w1@x')]})
        write(os.path.join(watch, name + '.sha256'),
              f'{hashlib.sha256(data).hexdigest()}  {name}\n'.encode())
        write(os.path.join(watch, name), data)
        write(os.path.join(watch, 'notes.mbox'), mbox_bytes([raw('n@x')]))  # PST only here
        summary = self.imp.run()
        run = summary['results'][0]
        self.assertEqual((run['status'], run['account'], run['source'], run['imported']),
                         ('done', self.A, 'watch', 1))
        self.assertEqual(run['detail']['mapped_by'], 'pattern alice@old.example-*.pst')
        self.assertEqual(folders_of(self.maildir(self.A)), ['PST/Inbox'])
        self.assertIn(name, os.listdir(self.ap(self.A)['done']))
        self.assertIn(('watch/notes.mbox', 'not a .pst file'), summary['ignored'])

    def test_legacy_archive_folder_is_still_read(self):
        self.make(mappings=[{'pattern': 'export-*.pst', 'account': self.A}])
        legacy = self.imp.paths['archive']
        for day in STEADY[:3]:
            write(os.path.join(legacy, f'export-{day:%Y%m%d}.pst.zst'), b'z')
        self.seed_done(self.A, STEADY[3:])
        names = [e['name'] for e in self.imp.archive_entries(self.A)]
        self.assertEqual(len(names), 7)
        self.assertEqual(self.imp.archive_entries(self.B), [])
        # they are one series with done/: a new export rotates across both
        self.export(self.A, D(2026, 11, 9))
        rot = self.imp.run()['rotation'][0]
        self.assertEqual(sorted(rot['deleted']), ['export-20260727.pst.zst',
                                                  'export-20261005.pst.zst'])
        self.assertFalse(os.path.exists(os.path.join(legacy, 'export-20260727.pst.zst')))


class LedgerTests(Flow):
    def test_ledger_row_contents(self):
        data = mbox_bytes([raw('1@x'), raw('2@x'), raw('1@x')])
        self.drop(self.A, 'Archive 2019.mbox', data)
        self.now = ts(2026, 11, 9, 7, 30)
        self.imp.run(trigger='manual')
        row = self.rows()[0]
        self.assertEqual({k: row[k] for k in (
            'sha256', 'size_bytes', 'original_name', 'detected_type', 'account',
            'target_prefix', 'source', 'trigger', 'status', 'imported', 'duplicates',
            'failed', 'too_large', 'error')}, {
            'sha256': hashlib.sha256(data).hexdigest(), 'size_bytes': len(data),
            'original_name': 'Archive 2019.mbox', 'detected_type': 'mbox',
            'account': self.A, 'target_prefix': 'Imported/mbox/', 'source': 'inbox',
            'trigger': 'manual', 'status': 'done', 'imported': 2, 'duplicates': 1,
            'failed': 0, 'too_large': 0, 'error': ''})
        for k in ('queued_at', 'started_at', 'finished_at'):
            self.assertEqual(row[k], '2026-11-09 07:30:00', k)
        self.assertEqual(row['finished_epoch'], self.now)
        d = row['detail']
        self.assertEqual(d['folders'], {'Imported/mbox/Archive 2019': 2})
        self.assertEqual(d['archived_as'], 'alice_at_example_com/done/Archive 2019.mbox')
        self.assertTrue(d['archive_verified'])
        self.assertEqual((d['checksum'], d['mapped_by']), ('no sidecar', 'import folder'))

    def test_statuses_queued_running_then_final(self):
        seen = []
        self.drop(self.A, 'a.eml', raw('1@x'))
        self.drop(self.A, 'b.eml', raw('2@x'))
        orig = self.convert

        def spy(path, typ, name, maildir, work, cache):
            seen.append({r['original_name']: r['status'] for r in self.rows()})
            return orig(path, typ, name, maildir, work, cache)
        self.imp.convert = spy
        self.imp.run()
        self.assertEqual(seen[0], {'a.eml': 'running', 'b.eml': 'queued'})
        self.assertEqual(seen[1], {'a.eml': 'done', 'b.eml': 'running'})
        self.assertEqual({r['status'] for r in self.rows()}, {'done'})

    def test_same_sha256_is_never_imported_twice(self):
        data = mbox_bytes([raw('1@x'), raw('2@x')])
        self.drop(self.A, 'first.mbox', data)
        self.assertEqual(self.imp.run()['results'][0]['imported'], 2)
        calls = []
        orig = self.convert
        self.imp.convert = lambda *a: calls.append(a) or orig(*a)
        self.drop(self.A, 'the same again.mbox', data)
        summary = self.imp.run()
        row = summary['results'][0]
        self.assertEqual((row['status'], row['imported'], row['duplicates']), ('skipped', 0, 0))
        self.assertIn("identical to 'first.mbox'", row['error'])
        self.assertIn('ledger #1', row['error'])
        self.assertEqual(calls, [])                           # the converter never ran
        self.assertFalse(summary['retry'])
        # recorded, kept (never deleted), out of the inbox, with the reason
        failed = self.ap(self.A)['failed']
        self.assertEqual(sorted(os.listdir(failed)),
                         ['the same again.mbox', 'the same again.mbox.reason.txt'])
        self.assertIn('already imported',
                      open(os.path.join(failed, 'the same again.mbox.reason.txt')).read())
        self.assertEqual(os.listdir(self.ap(self.A)['inbox']), ['.incoming'])
        self.assertEqual([r['status'] for r in self.rows()], ['skipped', 'done'])
        # the same file for ANOTHER account is a different import
        self.drop(self.B, 'first.mbox', data)
        self.assertEqual(self.imp.run()['results'][0]['status'], 'done')
        self.assertEqual(len(maildir_messages(self.maildir(self.B))), 2)

    def test_a_failed_import_does_not_block_a_retry_of_the_same_file(self):
        data = pst_bytes({}, exit=1)
        self.drop(self.A, 'x.pst', data)
        self.assertEqual(self.imp.run()['results'][0]['status'], 'failed')
        mi.retry_failed(self.ap(self.A), 'x.pst')
        self.assertEqual(self.imp.run()['results'][0]['status'], 'failed')   # tried again
        self.assertEqual([r['status'] for r in self.rows()], ['failed', 'failed'])

    def test_message_dedupe_across_formats(self):
        # the same message arrives in a PST, then an mbox, then an .eml
        no_id = msg('', subject='no message id', body='same text')
        self.drop(self.A, 'a.pst', pst_bytes({'Inbox': [msg('same@x'), no_id]}))
        r1 = self.imp.run()['results'][0]
        self.drop(self.A, 'b.mbox', mbox_bytes([raw('same@x'), no_id.encode()]))
        r2 = self.imp.run()['results'][0]
        self.drop(self.A, 'c.eml', raw('same@x'))
        self.drop(self.A, 'd.eml', no_id.encode().replace(b'\r\n', b'\n'))
        r3 = self.imp.run()['results']
        make_maildir(os.path.join(self.ap(self.A)['inbox'], 'md'),
                     {'': [(raw('same@x'), 'cur', '')]})
        r4 = self.imp.run()['results'][0]
        self.assertEqual((r1['imported'], r1['duplicates']), (2, 0))
        self.assertEqual((r2['imported'], r2['duplicates']), (0, 2))
        self.assertEqual([(r['imported'], r['duplicates']) for r in r3], [(0, 1), (0, 1)])
        self.assertEqual((r4['imported'], r4['duplicates']), (0, 1))
        self.assertEqual(folders_of(self.maildir(self.A)), ['PST/Inbox'])
        self.assertEqual(len(maildir_messages(self.maildir(self.A))), 2)   # stored once
        self.assertEqual([r['status'] for r in self.rows()], ['done'] * 5)

    def test_dedupe_against_mail_already_synced(self):
        pst.deliver(os.path.join(self.maildir(self.A), 'INBOX'), raw('synced@x'), flags='')
        self.drop(self.A, 'a.mbox', mbox_bytes([raw('synced@x'), raw('new@x')]))
        r = self.imp.run()['results'][0]
        self.assertEqual((r['imported'], r['duplicates']), (1, 1))

    def test_prefix_folders(self):
        self.drop(self.A, 'a.pst', pst_bytes({'Inbox': [msg('1@x')], 'Inbox/Sub': [msg('2@x')]}))
        self.drop(self.A, 'Sent.mbox', mbox_bytes([raw('3@x')]))
        self.drop(self.A, 'c.eml', raw('4@x'))
        self.drop(self.A, 'd.emlx', emlx_bytes(raw('5@x')))
        self.drop(self.A, 'e.msg', msg_bytes(raw('6@x')))
        make_maildir(os.path.join(self.ap(self.A)['inbox'], 'md'),
                     {'': [(raw('7@x'), 'cur', '')]})
        res = {r['original_name']: r['target_prefix'] for r in self.imp.run()['results']}
        self.assertEqual(res, {'a.pst': 'PST/', 'Sent.mbox': 'Imported/mbox/',
                               'c.eml': 'Imported/eml/', 'd.emlx': 'Imported/emlx/',
                               'e.msg': 'Imported/msg/', 'md': 'Imported/Maildir/'})
        self.assertEqual(folders_of(self.maildir(self.A)), [
            'Imported/Maildir/INBOX', 'Imported/eml', 'Imported/emlx', 'Imported/mbox/Sent',
            'Imported/msg', 'PST/Inbox', 'PST/Inbox/Sub'])
        # nothing merges into the synced INBOX any more
        self.assertFalse(os.path.exists(os.path.join(self.maildir(self.A), 'INBOX')))

    def test_imported_mail_is_marked_read(self):
        self.drop(self.A, 'a.pst', pst_bytes({'Inbox': [msg('1@x')]}))
        self.drop(self.A, 'b.mbox', mbox_bytes([raw('2@x')]))
        make_maildir(os.path.join(self.ap(self.A)['inbox'], 'md'),
                     {'': [(raw('3@x'), 'new', ''), (raw('4@x'), 'cur', 'F')]})
        self.imp.run()
        got = maildir_messages(self.maildir(self.A))
        self.assertEqual(len(got), 4)
        for folder, fn in got:
            self.assertIn('S', fn.split(':2,')[1], (folder, fn))
            self.assertTrue(os.path.exists(os.path.join(self.maildir(self.A), folder, 'cur', fn)))
        self.assertEqual(sum(1 for _, fn in got if fn.endswith(':2,FS')), 1)   # flags kept


class FailedFolderTests(Flow):
    def test_unknown_type_goes_to_failed_with_a_reason_file(self):
        self.drop(self.A, 'holiday.mbox', bytes(range(256)) * 40)     # not an mbox at all
        row = self.imp.run()['results'][0]
        self.assertEqual((row['status'], row['detected_type']), ('failed', ''))
        self.assertIn('unknown type', row['error'])
        failed = self.ap(self.A)['failed']
        self.assertEqual(sorted(os.listdir(failed)), ['holiday.mbox', 'holiday.mbox.reason.txt'])
        reason = open(os.path.join(failed, 'holiday.mbox.reason.txt')).read()
        self.assertIn('unknown type', reason)
        self.assertIn(self.A, reason)
        self.assertEqual(row['detail']['moved_to'], 'alice_at_example_com/failed/holiday.mbox')
        self.assertEqual(maildir_messages(self.maildir(self.A)), [])
        listed = mi.list_failed(failed)
        self.assertEqual([(f['name'], 'unknown type' in f['reason']) for f in listed],
                         [('holiday.mbox', True)])

    def test_msg_without_msgconvert_goes_to_failed_with_a_clear_message(self):
        self.msgconvert = NO_MSGCONVERT
        self.drop(self.A, 'letter.msg', msg_bytes(raw('1@x')))
        row = self.imp.run()['results'][0]
        self.assertEqual((row['status'], row['detected_type']), ('failed', 'msg'))
        self.assertIn('libemail-outlook-message-perl', row['error'])
        failed = self.ap(self.A)['failed']
        self.assertIn('libemail-outlook-message-perl',
                      open(os.path.join(failed, 'letter.msg.reason.txt')).read())
        # once the package is there, "move back to the inbox" imports it
        self.msgconvert = FAKE_MSGCONVERT
        mi.retry_failed(self.ap(self.A), 'letter.msg')
        self.assertEqual(os.listdir(failed), [])
        self.assertEqual(self.imp.run()['results'][0]['status'], 'done')

    def test_nothing_in_failed_or_done_is_overwritten(self):
        for body in (b'one', b'two'):
            self.drop(self.A, 'junk.bin', bytes(range(256)) + body)
            self.imp.run()
        self.assertEqual(sorted(n for n in os.listdir(self.ap(self.A)['failed'])
                                if not n.endswith('.txt')), ['2~junk.bin', 'junk.bin'])
        for mid in ('1@x', '2@x'):
            self.drop(self.A, 'same name.eml', raw(mid))
            self.imp.run()
        self.assertEqual(sorted(n for n in os.listdir(self.ap(self.A)['done'])
                                if not n.endswith('.sha256')), ['2~same name.eml', 'same name.eml'])
        with self.assertRaises(ValueError):
            mi.retry_failed(self.ap(self.A), '../inbox/x')
        with self.assertRaises(ValueError):
            mi.retry_failed(self.ap(self.A), 'junk.bin.reason.txt')

    def test_oversized_unit_is_refused(self):
        self.imp.max_bytes = 100
        self.drop(self.A, 'big.mbox', mbox_bytes([raw('1@x', body='x' * 500)]))
        row = self.imp.run()['results'][0]
        self.assertEqual(row['status'], 'failed')
        self.assertIn('byte limit', row['error'])

    def test_partial_container_is_imported_archived_and_reported(self):
        t = targz_bytes({'ok.eml': raw('1@x')},
                        extra=[tarinfo('sym.eml', tarfile.SYMTYPE, '/etc/passwd')])
        self.drop(self.A, 'home.tar.gz', t)
        row = self.imp.run()['results'][0]
        self.assertEqual((row['status'], row['imported'], row['detected_type']),
                         ('partial', 1, 'gzip'))
        self.assertEqual(row['detail']['refused_members'], 1)
        self.assertIn('home.tar.gz', os.listdir(self.ap(self.A)['done']))


# ---------------------------------------------------------------------------
# Retention: R1-R6
# ---------------------------------------------------------------------------

def entries(dates, stem='export'):
    return [{'name': f'{stem}-{d:%Y%m%d}.pst.zst', 'date': d, 'path': '', 'size': 1,
             'sidecar': None, 'is_dir': False, 'series': f'{stem}-*.pst'} for d in dates]


NOW = ts(2026, 11, 9, 7)
OKAY = dict(new_verified_copy=True, last_success=NOW, now=NOW)


class RetentionRuleTests(unittest.TestCase):
    TODAY = D(2026, 11, 9)

    def decide(self, dates, **kw):
        for k, v in OKAY.items():
            kw.setdefault(k, v)
        return mi.retention_decide(entries(dates), self.TODAY, **kw)

    def dates(self, rows):
        return sorted(e['date'].isoformat() for e in rows)

    def test_series_key(self):
        self.assertEqual(mi.series_key('ab1234c@example.edu-20261109.pst'),
                         'ab1234c@example.edu-*.pst')
        self.assertEqual(mi.series_key('ab1234c@example.edu-20261109.pst.zst'),
                         'ab1234c@example.edu-*.pst')
        self.assertEqual(mi.series_key('2~export-20261109.pst'), 'export-*.pst')
        self.assertEqual(mi.series_key('mail-20261109.tar.gz'), 'mail-*.tar.gz')
        self.assertIsNone(mi.series_key('Archive 2019.mbox'))
        self.assertIsNone(mi.series_key('export-2026.pst'))

    def test_r1_count_based_normal_rotation(self):
        dec = self.decide(STEADY + [D(2026, 11, 2), D(2026, 11, 9)])
        self.assertEqual(self.dates(dec['delete']), ['2026-07-27', '2026-10-05', '2026-10-12'])
        self.assertEqual(dec['refused'], [])
        self.assertEqual(dec['keep']['export-20261109.pst.zst'], 'recent')
        self.assertEqual(dec['keep']['export-20260928.pst.zst'], 'monthly')

    def test_r1_age_alone_never_deletes(self):
        # the same six copies, looked at years later: nothing new arrived,
        # nothing may go
        kept = [D(2026, 8, 31), D(2026, 9, 28), D(2026, 10, 19), D(2026, 10, 26),
                D(2026, 11, 2), D(2026, 11, 9)]
        for today in (D(2026, 11, 9), D(2027, 6, 1), D(2031, 1, 1)):
            dec = mi.retention_decide(entries(kept), today, **OKAY)
            self.assertEqual((dec['delete'], dec['refused']), ([], []), today)

    def test_r2_no_verified_new_copy_means_no_deletion(self):
        dec = self.decide(STEADY + [D(2026, 11, 2), D(2026, 11, 9)], new_verified_copy=False)
        self.assertEqual(dec['delete'], [])
        self.assertEqual([r for _e, r, _w in dec['refused']], ['R2'] * 3)
        dec = self.decide(STEADY + [D(2026, 11, 2), D(2026, 11, 9)],
                          blocked='1 import(s) in this run failed or were partial')
        self.assertEqual(dec['delete'], [])
        self.assertIn('partial', dec['refused'][0][2])

    def test_rotation_never_goes_below_min_keep(self):
        five = [D(2026, 11, d) for d in (1, 2, 3, 4, 5)]
        dec = self.decide(five, keep_recent=1, keep_monthly=0, min_keep=3)
        self.assertEqual(self.dates(dec['delete']), ['2026-11-01', '2026-11-02'])
        self.assertEqual(sorted((e['date'].day, r) for e, r, _w in dec['refused']),
                         [(3, 'R3'), (4, 'R3')])
        # three copies and a config that wants one: nothing can go
        dec = self.decide(five[:3], keep_recent=1, keep_monthly=0, min_keep=3)
        self.assertEqual(dec['delete'], [])
        self.assertEqual(len(dec['refused']), 2)
        # min_keep below 1 is treated as 1
        dec = self.decide(five[:2], keep_recent=1, keep_monthly=0, min_keep=0, max_delete=5)
        self.assertEqual(self.dates(dec['delete']), ['2026-11-01'])

    def test_rotation_never_deletes_the_newest_copy(self):
        for n in range(1, 12):
            dates = [D(2026, 11, 9) - datetime.timedelta(days=i) for i in range(n)]
            for kw in ({}, {'keep_recent': 1, 'keep_monthly': 0, 'min_keep': 1, 'max_delete': 50}):
                dec = self.decide(dates, **kw)
                self.assertNotIn('2026-11-09', self.dates(dec['delete']), (n, kw))
                self.assertLess(len(dec['delete']), max(n, 1), (n, kw))
        # even if the policy itself were to offer it (a broken policy)
        with patch.object(pst, 'plan_rotation', lambda e, *a, **k: ([], list(e))):
            dec = self.decide([D(2026, 11, d) for d in range(1, 10)], max_delete=50, min_keep=1)
            self.assertNotIn('2026-11-09', self.dates(dec['delete']))

    def test_stale_source_freezes_rotation(self):
        dates = STEADY + [D(2026, 11, 2), D(2026, 11, 9)]
        # daily schedule: frozen once the last success is over 48 h old
        limit = mi.stale_after_seconds({'kind': 'daily'})
        self.assertEqual(limit, 48 * 3600)
        self.assertEqual(mi.stale_after_seconds({'kind': 'hourly'}), 48 * 3600)   # floor
        self.assertEqual(mi.stale_after_seconds({'kind': 'weekly'}), 14 * 86400)  # 2x interval
        fresh = self.decide(dates, last_success=NOW - 47 * 3600, stale_after=limit)
        self.assertEqual(len(fresh['delete']), 3)
        stale = self.decide(dates, last_success=NOW - 49 * 3600, stale_after=limit)
        self.assertEqual(stale['delete'], [])
        self.assertEqual([r for _e, r, _w in stale['refused']], ['R4'] * 3)
        self.assertIn('stale', stale['summary'])
        never = self.decide(dates, last_success=None)
        self.assertEqual((never['delete'], never['refused'][0][1]), ([], 'R4'))

    def test_deletion_budget(self):
        # a backlog (or a typo such as keep_recent: 1): 24 copies, 18 surplus
        dec = self.decide(weekly(D(2026, 6, 1), 24))
        self.assertEqual(dec['delete'], [])
        self.assertEqual([r for _e, r, _w in dec['refused']], ['R5'] * 18)
        self.assertIn('nothing deleted', dec['summary'])
        # exactly at the budget is allowed
        self.assertEqual(len(self.decide(STEADY + [D(2026, 11, 2), D(2026, 11, 9)])['delete']), 3)
        # one over is not: delete NOTHING rather than a part
        over = self.decide(STEADY + [D(2026, 10, 1), D(2026, 11, 2), D(2026, 11, 9)])
        self.assertEqual((over['delete'], len(over['refused'])), ([], 4))
        # never more than half the set, whatever max_delete says
        four = [D(2026, 11, d) for d in (1, 2, 3, 4, 5)]
        dec = self.decide(four, keep_recent=1, keep_monthly=0, min_keep=1, max_delete=50)
        self.assertEqual((dec['delete'], {r for _e, r, _w in dec['refused']}), ([], {'R5'}))
        dec = self.decide(four, keep_recent=3, keep_monthly=0, min_keep=1, max_delete=50)
        self.assertEqual(len(dec['delete']), 2)

    def test_one_off_files_are_never_in_a_series(self):
        prev = mi.retention_preview(
            [{'name': 'Archive.mbox', 'date': D(2019, 1, 1), 'series': None}],
            self.TODAY, pst.normalize_config({}, []))
        self.assertIn('never rotated', prev['Archive.mbox'])


class RotationFlowTests(Flow):
    def test_normal_rotation_records_each_deletion_in_the_ledger(self):
        self.seed_done(self.A, STEADY)
        self.export(self.A, D(2026, 11, 2), ['1@x'])
        self.export(self.A, D(2026, 11, 9), ['1@x', '2@x'])
        summary = self.imp.run()
        self.assertEqual(self.done_dates(self.A), [
            '2026-08-31', '2026-09-28', '2026-10-19', '2026-10-26', '2026-11-02', '2026-11-09'])
        ev = self.events()
        self.assertEqual(sorted((e['action'], e['rule'], e['name']) for e in ev), [
            ('deleted', 'R1', 'export-20260727.pst.zst'),
            ('deleted', 'R1', 'export-20261005.pst.zst'),
            ('deleted', 'R1', 'export-20261012.pst.zst')])
        e = ev[0]
        self.assertEqual((e['account'], e['series'], e['at'], e['size_bytes']),
                         (self.A, 'export-*.pst', '2026-11-09 07:00:00', 3))
        self.assertIn(e['copy_date'], ('2026-07-27', '2026-10-05', '2026-10-12'))
        self.assertEqual(e['ledger_id'], summary['results'][-1]['id'])
        # sidecars of deleted copies go with them; nothing else is touched
        left = os.listdir(self.ap(self.A)['done'])
        self.assertNotIn('export-20261005.pst.sha256', left)
        self.assertEqual(len(left), 12)

    def test_rotation_is_per_series_and_per_account(self):
        self.seed_done(self.A, STEADY)
        self.seed_done(self.A, STEADY, stem='other')
        self.seed_done(self.B, STEADY)
        write(os.path.join(self.ap(self.A)['done'], 'Archive 2019.mbox'), b'x')
        self.export(self.A, D(2026, 11, 9))
        self.imp.run()
        names = os.listdir(self.ap(self.A)['done'])
        self.assertNotIn('export-20260727.pst.zst', names)
        self.assertIn('other-20260727.pst.zst', names)             # another series
        self.assertIn('Archive 2019.mbox', names)                  # a one-off
        self.assertEqual(len(self.done_dates(self.B)), 7)          # another account

    def test_partial_import_archives_the_file_but_blocks_rotation(self):
        self.seed_done(self.A, STEADY)
        before = self.done_dates(self.A)
        self.export(self.A, D(2026, 11, 9))
        self.force['export-20261109.pst'] = {'status': 'partial', 'failed': 1}
        summary = self.imp.run()
        row = summary['results'][0]
        self.assertEqual((row['status'], row['failed']), ('partial', 1))
        # archived ...
        self.assertEqual(self.done_dates(self.A), before + ['2026-11-09'])
        self.assertEqual(row['detail']['archived_as'],
                         'alice_at_example_com/done/export-20261109.pst')
        # ... and every older copy is still there, with the refusals on record
        self.assertEqual(summary['rotation'][0]['deleted'], [])
        ev = self.events()
        self.assertEqual({(e['action'], e['rule']) for e in ev}, {('refused', 'R2')})
        self.assertEqual(sorted(e['name'] for e in ev),
                         ['export-20260727.pst.zst', 'export-20261005.pst.zst'])
        self.assertIn('partial', ev[0]['reason'])
        # the next clean import rotates again
        self.force.clear()
        self.now = ts(2026, 11, 10, 7)
        self.export(self.A, D(2026, 11, 10), ['1@x', '9@x'])
        self.assertTrue(self.imp.run()['rotation'][0]['deleted'])

    def test_a_failed_file_in_the_same_run_blocks_rotation(self):
        self.seed_done(self.A, STEADY)
        self.export(self.A, D(2026, 11, 9))
        self.drop(self.A, 'junk.bin', bytes(range(256)))
        rot = self.imp.run()['rotation'][0]
        self.assertEqual(rot['deleted'], [])
        self.assertEqual(len(self.done_dates(self.A)), 8)
        self.assertEqual({e['rule'] for e in self.events()}, {'R2'})

    def test_an_unverified_archive_copy_blocks_rotation(self):
        self.seed_done(self.A, STEADY + [D(2026, 10, 1)])        # one surplus copy
        self.export(self.A, D(2026, 11, 9))
        with patch.object(self.imp, '_archive', side_effect=OSError('disk went away')):
            summary = self.imp.run()
        self.assertEqual(summary['results'][0]['status'], 'done')
        self.assertIn('could not archive', summary['results'][0]['detail']['archive_error'])
        self.assertEqual(summary['rotation'][0]['deleted'], [])
        self.assertEqual({e['rule'] for e in self.events()}, {'R2'})

    def test_min_keep_floor_in_a_real_run(self):
        self.make(keep_recent=1, keep_monthly=0, min_keep=3)
        self.seed_done(self.A, [D(2026, 11, 5), D(2026, 11, 6), D(2026, 11, 7), D(2026, 11, 8)])
        self.export(self.A, D(2026, 11, 9))
        self.imp.run()
        # five copies, the config wants one, the floor keeps three
        self.assertEqual(self.done_dates(self.A), ['2026-11-07', '2026-11-08', '2026-11-09'])
        ev = self.events()
        self.assertEqual(sorted((e['action'], e['rule'], e['copy_date']) for e in ev), [
            ('deleted', 'R1', '2026-11-05'), ('deleted', 'R1', '2026-11-06'),
            ('refused', 'R3', '2026-11-07'), ('refused', 'R3', '2026-11-08')])

    def test_deletion_budget_in_a_real_run(self):
        self.seed_done(self.A, weekly(D(2026, 6, 1), 22))
        self.export(self.A, D(2026, 11, 9))
        rot = self.imp.run()['rotation'][0]
        self.assertEqual(rot['deleted'], [])
        self.assertEqual(len(self.done_dates(self.A)), 23)
        ev = self.events()
        self.assertEqual({(e['action'], e['rule']) for e in ev}, {('refused', 'R5')})
        self.assertEqual(len(ev), 17)

    def test_stale_source_freeze_in_a_real_run(self):
        self.seed_done(self.A, STEADY)
        self.export(self.A, D(2026, 11, 9))
        with patch.object(mi.Ledger, 'last_done_epoch', lambda *a: self.now - 30 * 86400):
            rot = self.imp.run()['rotation'][0]
        self.assertEqual(rot['deleted'], [])
        self.assertEqual({(e['action'], e['rule']) for e in self.events()}, {('refused', 'R4')})
        self.assertEqual(len(self.done_dates(self.A)), 8)

    def test_last_done_epoch_is_per_series(self):
        self.export(self.A, D(2026, 11, 9))
        self.imp.run()
        led = self.ledger()
        try:
            self.assertEqual(led.last_done_epoch(self.A, 'export-*.pst'), self.now)
            self.assertIsNone(led.last_done_epoch(self.A, 'other-*.pst'))
            self.assertIsNone(led.last_done_epoch(self.B, 'export-*.pst'))
        finally:
            led.close()

    def test_month_of_failed_imports_then_month_of_no_imports_deletes_nothing(self):
        """ADR: "how do we prevent our rotation schedules from deleting
        data ... especially when there is no fresh archive for a day or two
        days or a month?" """
        # 10-01 is surplus by the policy (beyond 4 recent + 3 monthly): the
        # only thing standing between it and deletion is the guard
        self.make(max_delete_per_run=5)
        self.seed_done(self.A, STEADY + [D(2026, 10, 1)])       # last success: 11-08
        self.assertEqual([e['name'] for e in pst.plan_rotation(
            mi.list_archive(self.ap(self.A)['done']))[1]], ['export-20261001.pst.zst'])
        done = self.ap(self.A)['done']
        before = sorted(os.listdir(done))
        day0 = D(2026, 11, 10)
        # month 1: the nightly export arrives, and every one is broken
        for i in range(30):
            day = day0 + datetime.timedelta(days=i)
            self.now = ts(day.year, day.month, day.day, 7)
            self.export(self.A, day, exit=1)
            summary = self.imp.run()
            self.assertEqual(summary['results'][0]['status'], 'failed')
            self.assertEqual(sorted(os.listdir(done)), before, day)
        # month 2: nothing arrives at all
        for i in range(30, 60):
            day = day0 + datetime.timedelta(days=i)
            self.now = ts(day.year, day.month, day.day, 7)
            summary = self.imp.run()
            self.assertEqual((summary['results'], summary['rotation']), ([], []))
            self.assertEqual(sorted(os.listdir(done)), before, day)
        ev = self.events()
        self.assertEqual([e for e in ev if e['action'] == 'deleted'], [])    # ZERO deletions
        self.assertTrue(ev)
        self.assertEqual({(e['action'], e['rule'], e['name']) for e in ev},
                         {('refused', 'R2', 'export-20261001.pst.zst')})
        self.assertEqual(len(ev), 30)                                        # one per failed run
        self.assertEqual(max(e['at'] for e in ev)[:10], '2026-12-09')        # none in month 2
        self.assertEqual(len([r for r in self.rows() if r['status'] == 'failed']), 30)
        self.assertEqual(len(os.listdir(self.ap(self.A)['failed'])), 60)     # kept, with reasons
        # the source comes back with one good import. R2 is satisfied, and
        # this is where R4 has to hold: the last success BEFORE this run is
        # two months old, so one copy from a source that was dead that long
        # does not get the old copies pruned.
        day = day0 + datetime.timedelta(days=60)
        self.now = ts(day.year, day.month, day.day, 7)
        self.export(self.A, day)
        summary = self.imp.run()
        self.assertEqual(summary['results'][0]['status'], 'done')
        rot = summary['rotation'][0]
        self.assertEqual(rot['deleted'], [])
        self.assertIn('R4', rot['summary'])
        self.assertIn('stale', rot['summary'])
        frozen = [e for e in self.events() if e['rule'] == 'R4']
        self.assertTrue(frozen)
        self.assertEqual({e['action'] for e in frozen}, {'refused'})
        self.assertEqual({e['at'][:10] for e in frozen}, {day.isoformat()})
        self.assertEqual([n for n in before if n not in os.listdir(done)], [])
        # the next export arrives on schedule: the source is healthy again
        # and rotation resumes - within the budget, above the floor, newest kept
        day += datetime.timedelta(days=1)
        self.now = ts(day.year, day.month, day.day, 7)
        self.export(self.A, day, mids=('2@x',))
        summary = self.imp.run()
        self.assertEqual(summary['results'][0]['status'], 'done')
        deleted = summary['rotation'][0]['deleted']
        self.assertEqual(sorted(deleted), [
            'export-20260727.pst.zst', 'export-20261001.pst.zst',
            'export-20261005.pst.zst', 'export-20261012.pst.zst'])
        self.assertEqual(sorted(e['name'] for e in self.events() if e['action'] == 'deleted'),
                         sorted(deleted))
        self.assertIn(day.isoformat(), self.done_dates(self.A))
        self.assertGreaterEqual(len(self.done_dates(self.A)), 3)

    def test_a_run_with_nothing_to_import_touches_nothing(self):
        self.make(keep_recent=1, keep_monthly=0, min_keep=1, max_delete_per_run=50)
        self.seed_done(self.A, weekly(D(2026, 6, 1), 22))
        before = sorted(os.listdir(self.ap(self.A)['done']))
        for _ in range(3):
            self.assertEqual(self.imp.run()['rotation'], [])
        self.assertEqual(sorted(os.listdir(self.ap(self.A)['done'])), before)
        self.assertEqual(self.events(), [])


# ---------------------------------------------------------------------------
# Ledger schema in the index database
# ---------------------------------------------------------------------------

class LedgerSchemaTests(Tmp):
    def tables(self, conn):
        return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}

    def test_migration_is_safe_on_an_existing_index_database(self):
        # a 1.2.0 database: the index tables only, with data
        os.makedirs(os.path.join(self.tmp, 'u'))
        db = os.path.join(self.tmp, 'u', '.search_index.db')
        conn = sqlite3.connect(db)
        conn.execute(search_index._FTS5_SCHEMA)
        conn.execute(search_index._SEEN_SCHEMA)
        conn.execute("INSERT INTO emails(message_id, subject) VALUES ('m1', 'kept subject')")
        conn.execute("INSERT INTO seen_ids(message_id) VALUES ('m1')")
        conn.commit()
        conn.close()
        for _ in range(2):                                    # idempotent
            conn = search_index.get_index_db('u', self.tmp)
            self.assertLessEqual({'import_ledger', 'import_retention_events'}, self.tables(conn))
            self.assertEqual(conn.execute('SELECT subject FROM emails').fetchall(),
                             [('kept subject',)])
            self.assertEqual(conn.execute('SELECT count(*) FROM seen_ids').fetchone()[0], 1)
            conn.close()

    def test_a_ledger_table_from_an_older_build_gets_its_missing_columns(self):
        os.makedirs(os.path.join(self.tmp, 'u'))
        conn = sqlite3.connect(os.path.join(self.tmp, 'u', '.search_index.db'))
        conn.execute("CREATE TABLE import_ledger (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                     "sha256 TEXT NOT NULL DEFAULT '', size_bytes INTEGER NOT NULL DEFAULT 0, "
                     "original_name TEXT NOT NULL DEFAULT '', detected_type TEXT NOT NULL DEFAULT '', "
                     "account TEXT NOT NULL DEFAULT '', target_prefix TEXT NOT NULL DEFAULT '', "
                     "trigger TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'queued', "
                     "imported INTEGER NOT NULL DEFAULT 0, duplicates INTEGER NOT NULL DEFAULT 0, "
                     "failed INTEGER NOT NULL DEFAULT 0, too_large INTEGER NOT NULL DEFAULT 0, "
                     "started_at TEXT NOT NULL DEFAULT '', finished_at TEXT NOT NULL DEFAULT '', "
                     "error TEXT NOT NULL DEFAULT '', detail TEXT NOT NULL DEFAULT '{}')")
        conn.execute("INSERT INTO import_ledger(sha256, status) VALUES ('abc', 'done')")
        conn.commit()
        conn.close()
        led = mi.Ledger(search_index.get_index_db('u', self.tmp))
        row = led.rows()[0]
        self.assertEqual((row['sha256'], row['status'], row['source'], row['finished_epoch']),
                         ('abc', 'done', '', 0))
        led.add(status='queued', original_name='x', source='inbox')
        led.close()

    def test_rebuilding_the_search_index_keeps_the_ledger(self):
        led = mi.Ledger(search_index.get_index_db('u', self.tmp))
        led.add(status='done', sha256='a' * 64, original_name='x.mbox', account='a@x')
        led.add_event(now=NOW, account='a@x', series='x-*.pst', action='deleted', rule='R1',
                      name='x-20260101.pst')
        led.close()
        search_index.rebuild_index('u', self.tmp)
        led = mi.Ledger(search_index.get_index_db('u', self.tmp))
        self.assertEqual(len(led.rows()), 1)
        self.assertEqual(len(led.events()), 1)
        self.assertIsNotNone(led.find_done('a' * 64, 'a@x'))
        self.assertIsNone(led.find_done('a' * 64, 'other@x'))
        led.close()

    def test_unknown_ledger_field_is_an_error(self):
        led = mi.Ledger(search_index.get_index_db('u', self.tmp))
        with self.assertRaises(ValueError):
            led.add(status='done', password='x')
        led.close()


# ---------------------------------------------------------------------------
# App wiring: routes, the converter child, search, the CLI
# ---------------------------------------------------------------------------

class AppTests(unittest.TestCase):
    USER = 'fam'
    A = 'alice@example.com'
    B = 'bob.smith@example.org'

    def setUp(self):
        self._orig_data = ma.CONFIG['data_dir']
        self._td = tempfile.TemporaryDirectory(prefix='ma-import-app-')
        self.data = self._td.name
        ma.CONFIG['data_dir'] = self.data
        cfg = ma.get_user_config_dir(self.USER)
        (cfg / 'accounts.json').write_text(json.dumps([
            {'email': e, 'provider': 'gmail', 'enabled': True, 'sync_interval': 'manual'}
            for e in (self.A, self.B)]))
        ma.app.config['TESTING'] = True
        self.client = ma.app.test_client()
        with self.client.session_transaction() as s:
            s['username'] = self.USER
            s['login_time'] = time.time()
        self.apA = pst.account_paths(self.data, self.USER, self.A)
        self.apB = pst.account_paths(self.data, self.USER, self.B)

    def tearDown(self):
        ma.CONFIG['data_dir'] = self._orig_data
        self._td.cleanup()

    def run_import(self, **kw):
        with redirect_stdout(io.StringIO()):
            return ma.mail_import_user(self.USER, trigger='manual', stable_wait=0, **kw)

    def test_upload_route_writes_into_the_chosen_account_inbox(self):
        body = mbox_bytes([raw('up1@x', body='x' * 300_000), raw('up2@x')])
        name = 'Old laptop (2019).mbox'
        with patch.object(ma, '_spawn_mail_import') as spawn:
            r = self.client.post('/import/upload', query_string={
                'import': '1', 'account': self.B, 'name': name},
                data=body, content_type='application/octet-stream')
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.get_json()['sha256'], hashlib.sha256(body).hexdigest())
        spawn.assert_called_once_with(self.USER, self.B, name)
        inbox = os.path.join(self.data, self.USER, '_import', 'bob_smith_at_example_org', 'inbox')
        self.assertEqual(sorted(os.listdir(inbox)), ['.incoming', name, name + '.sha256'])
        self.assertEqual(os.listdir(os.path.join(inbox, '.incoming')), [])
        self.assertEqual(os.listdir(self.apA['inbox']), ['.incoming'])
        # the upload is then imported into THAT account
        res = self.run_import()['results'][0]
        self.assertEqual((res['account'], res['status'], res['imported']), (self.B, 'done', 2))
        self.assertIn('sha256 verified', res['detail']['checksum'])
        # refused: someone else's account, no account, a bad name, a second copy
        for qs, code in (({'account': 'eve@x.com', 'name': 'a.mbox'}, 400),
                         ({'name': 'a.mbox'}, 400),
                         ({'account': self.B, 'name': '../a.mbox'}, 400),
                         ({'account': self.B, 'name': '.hidden'}, 400)):
            r = self.client.post('/import/upload', query_string=qs, data=b'x',
                                 content_type='application/octet-stream')
            self.assertEqual(r.status_code, code, qs)
        self.client.post('/import/upload', query_string={'account': self.B, 'name': 'twice.eml'},
                         data=raw('t@x'), content_type='application/octet-stream')
        r = self.client.post('/import/upload', query_string={'account': self.B, 'name': 'twice.eml'},
                             data=raw('t@x'), content_type='application/octet-stream')
        self.assertEqual(r.status_code, 409)

    def test_page_shows_inbox_types_history_failed_and_archive(self):
        self.client.get('/import')                          # creates the folders
        write(os.path.join(self.apA['inbox'], 'export-20261109.pst'),
              pst_bytes({'Inbox': [msg('p@x', subject='Quarterly zebra report')]}))
        write(os.path.join(self.apA['inbox'], 'wrongly named.pst'), mbox_bytes([raw('m@x')]))
        write(os.path.join(self.apA['inbox'], 'junk.bin'), bytes(range(256)) * 4)
        page = self.client.get('/import').data.decode()
        self.assertIn('<h2>Import mail</h2>', page)
        self.assertIn(self.apA['inbox'], page)
        self.assertIn(self.apB['inbox'], page)
        self.assertIn('<span class="tag tag-type">pst</span>', page)
        self.assertIn('<span class="tag tag-type">mbox</span>', page)   # by content
        self.assertIn('<span class="tag tag-unknown">unknown</span>', page)
        self.assertIn('No imports yet.', page)
        self.run_import()
        page = self.client.get('/import').data.decode()
        self.assertNotIn('No imports yet.', page)
        self.assertEqual(page.count('tag tag-done'), 2)
        self.assertEqual(page.count('tag tag-failed'), 1)
        self.assertIn('unknown type', page)                              # failed item + reason
        self.assertIn('Move back to the inbox', page)
        self.assertIn('archived as alice_at_example_com/done/export-20261109.pst', page)
        self.assertIn('kept: recent', page)                              # dated series
        self.assertIn('kept: one-off file', page)                        # the mbox
        self.assertIn('No copy has been deleted', page)
        st = self.client.get('/api/import/status').get_json()
        self.assertFalse(st['running'])
        self.assertEqual(sorted(r['status'] for r in st['rows']), ['done', 'done', 'failed'])
        # imported mail is searchable, under its prefix folder
        hits = search_index.search_fts(self.USER, self.data, 'zebra')
        self.assertEqual([h['folder'] for h in hits['results']], ['PST/Inbox'])

    def test_retry_route_moves_a_failed_file_back(self):
        self.client.get('/import')
        write(os.path.join(self.apA['inbox'], 'junk.bin'), bytes(range(256)) * 4)
        self.run_import()
        self.assertIn('junk.bin', os.listdir(self.apA['failed']))
        r = self.client.post('/import/retry', data={'account': self.A, 'file': 'junk.bin'})
        self.assertEqual(r.status_code, 302)
        self.assertIn('junk.bin', os.listdir(self.apA['inbox']))
        self.assertEqual(os.listdir(self.apA['failed']), [])
        self.client.post('/import/retry', data={'account': 'eve@x.com', 'file': 'junk.bin'})
        self.assertIn('junk.bin', os.listdir(self.apA['inbox']))

    def test_run_route(self):
        self.client.get('/import')
        write(os.path.join(self.apA['inbox'], 'a.eml'), raw('1@x'))
        with patch.object(ma, '_spawn_mail_import') as spawn:
            self.client.post('/import/run', data={'account': self.A, 'file': 'a.eml'})
            spawn.assert_called_once_with(self.USER, self.A, 'a.eml')
            spawn.reset_mock()
            self.client.post('/import/run', data={})            # everything waiting
            spawn.assert_called_once_with(self.USER, None, None)
            spawn.reset_mock()
            self.client.post('/import/run', data={'account': self.B, 'file': 'a.eml'})
            self.client.post('/import/run', data={'account': self.A, 'file': '../../etc/passwd'})
            spawn.assert_not_called()

    def test_old_urls_and_endpoint_names(self):
        r = self.client.get('/pst-import')
        self.assertEqual(r.status_code, 302)
        self.assertTrue(r.location.endswith('/import'), r.location)
        with ma.app.test_request_context():
            self.assertEqual(ma.url_for('mail_import_page'), '/import')
            self.assertEqual(ma.url_for('mail_import_upload'), '/import/upload')
            self.assertEqual(ma.url_for('mail_import_run'), '/import/run')
            self.assertEqual(ma.url_for('mail_import_config'), '/import/config')
            self.assertEqual(ma.url_for('mail_import_retry'), '/import/retry')
            self.assertEqual(ma.url_for('mail_import_status'), '/api/import/status')
        dash = self.client.get('/dashboard').data.decode()
        self.assertIn('href="/import"', dash)
        self.assertIn('>Import mail</a>', dash)
        self.assertNotIn('Import PST', dash)

    def test_login_is_required(self):
        anon = ma.app.test_client()
        for url in ('/import', '/api/import/status', '/pst-import'):
            self.assertEqual(anon.get(url).status_code, 302, url)
        for url in ('/import/upload', '/import/run', '/import/config', '/import/retry'):
            self.assertEqual(anon.post(url).status_code, 302, url)
        self.assertFalse(os.path.exists(os.path.join(self.data, self.USER, '_import')))

    def test_template_extends_the_shared_layout_only(self):
        import re
        src = open(os.path.join(ROOT, 'templates', 'mail_import.html')).read()
        self.assertTrue(src.startswith('{% extends "base.html" %}'))
        self.assertEqual(set(re.findall(r'{%\s*block\s+(\w+)', src)),
                         {'title', 'head_extra', 'content', 'scripts'})
        for chrome in ('<header', '<nav', '<html', '<body', 'logo-header'):
            self.assertNotIn(chrome, src)
        self.assertFalse(os.path.exists(os.path.join(ROOT, 'templates', 'pst_import.html')))

    def test_import_folder_is_never_indexed_or_searched_as_mail(self):
        self.client.get('/import')
        make_maildir(os.path.join(self.apA['inbox'], 'Maildir'),
                     {'': [(raw('w@x', subject='waiting walrus'), 'cur', 'S')]})
        make_maildir(os.path.join(self.apA['done'], 'Old'),
                     {'': [(raw('o@x', subject='archived walrus'), 'cur', 'S')]})
        stats = search_index.index_maildir(self.USER, self.data)
        self.assertEqual(stats['indexed'], 0)
        self.assertEqual(search_index.search_fts(self.USER, self.data, 'walrus')['results'], [])
        self.assertEqual(ma.search_maildir(self.USER, 'walrus'), [])

    def test_scheduled_pass_default_daily_0600_and_per_account_inbox(self):
        self.client.post('/import/config', data={'enabled': '1'})
        cfg = ma.load_import_config(self.USER)
        self.assertEqual(cfg['schedule'], {'kind': 'daily', 'weekday': 0, 'hour': 6})
        self.client.get('/import')
        write(os.path.join(self.apA['inbox'], 'a.eml'), raw('1@x'))
        with patch.object(pst, 'STABLE_WAIT', 0), redirect_stdout(io.StringIO()):
            ma.scheduled_mail_import(now=ts(2026, 11, 9, 5, 30))    # before 06:00, never ran
            ma.scheduled_mail_import(now=ts(2026, 11, 9, 6, 30))
            write(os.path.join(self.apA['inbox'], 'b.eml'), raw('2@x'))
            ma.scheduled_mail_import(now=ts(2026, 11, 9, 9, 30))    # same day: not due
        led = ma.open_import_ledger(self.USER)
        rows = led.rows()
        led.close()
        self.assertEqual([(r['original_name'], r['trigger'], r['status']) for r in rows],
                         [('a.eml', 'scheduled', 'done')])
        self.assertIn('b.eml', os.listdir(self.apA['inbox']))

    def _cli(self, *args):
        env = dict(os.environ, MAIL_ARCHIVER_DATA=self.data, MAIL_ARCHIVER_AUTH='builtin',
                   MAIL_ARCHIVER_SECRET='test-only-secret', MAIL_ARCHIVER_PST_STABLE_WAIT='0')
        return subprocess.run([sys.executable, os.path.join(ROOT, 'app.py'), *args],
                              capture_output=True, text=True, env=env, cwd=ROOT, timeout=120)

    def test_cli_import_and_its_pst_import_alias(self):
        self.client.get('/import')
        write(os.path.join(self.apA['inbox'], 'a.mbox'), mbox_bytes([raw('1@x'), raw('2@x')]))
        write(os.path.join(self.apB['inbox'], 'b.eml'), raw('3@x'))
        r = self._cli('import', '--user', self.USER, '--manual', '--account', self.A,
                      '--file', 'a.mbox')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('a.mbox: done (mbox) - imported 2', r.stdout)
        self.assertIn('b.eml', os.listdir(self.apB['inbox']))            # only that file
        r = self._cli('pst-import', '--user', self.USER, '--manual')     # the old name
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('b.eml: done (eml) - imported 1', r.stdout)
        r = self._cli('import', '--user', self.USER, '--ledger')
        self.assertEqual(r.returncode, 0, r.stderr)
        lines = r.stdout.strip().splitlines()
        self.assertEqual(len(lines), 3)
        self.assertIn('alice@example.com / a.mbox -> Imported/mbox/', r.stdout)
        r = self._cli('pst-import', '--user', self.USER, '--ledger', '--json')
        self.assertEqual([x['status'] for x in json.loads(r.stdout)], ['done', 'done'])
        r = self._cli('import', '--user', self.USER, '--ledger', '--events')
        self.assertEqual(r.returncode, 0, r.stderr)
        r = self._cli('import')                                           # scheduled pass
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('Mail import check', r.stdout)

    def test_cron_wrapper_accepts_import_and_the_old_name(self):
        wrapper = os.path.join(ROOT, 'mail-archiver-scheduled-sync')
        self.assertEqual(subprocess.run(['bash', '-n', wrapper]).returncode, 0)
        src = open(wrapper).read()
        self.assertIn('import|pst-import)  job=import ;;', src)
        self.assertIn('exec python3 app.py "$job" "$@"', src)


if __name__ == '__main__':
    unittest.main()
