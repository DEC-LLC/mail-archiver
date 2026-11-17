"""The initial / rebase sync and Reconcile on Windows (1.3.1), proved on
Linux and on Windows.

The download of a bulk job is done by isync (`mbsync`), which on Windows
is an optional program. Without it the page still renders and says so,
every action that would set up or start a download refuses with the same
message, and Reconcile (IMAP LIST / STATUS / EXAMINE against the local
Maildir, pure Python) works.

On Linux the tests set `platform_compat.IS_WINDOWS` and make mbsync
"not on PATH"; the engine tests also make fcntl / pwd / grp / resource
unimportable. No mail server, no mbsync, no /tmp, no POSIX-only call.
"""

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

_DATA = tempfile.mkdtemp(prefix='ma-test-data-')
os.environ.setdefault('MAIL_ARCHIVER_DATA', _DATA)
os.environ['MAIL_ARCHIVER_AUTH'] = 'builtin'
os.environ.setdefault('MAIL_ARCHIVER_SECRET', 'test-only-secret')
os.environ.pop('MAIL_ARCHIVER_SECRET_FILE', None)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import platform_compat as compat    # noqa: E402
import app as ma                    # noqa: E402
import bulk_sync as bulk            # noqa: E402
import bulk_sync_routes as routes   # noqa: E402
import sync_lifecycle as slc        # noqa: E402

POSIX_ONLY = ('fcntl', 'pwd', 'grp', 'resource')
USER = 'winbulkuser'
EMAIL = 'big.box@gmail.com'
ALLMAIL = '[Gmail]/All Mail'
READ_ONLY_COMMANDS = {'LIST', 'STATUS', 'EXAMINE', 'UID SEARCH', 'LOGOUT'}


def no_tools(name):
    """platform_compat.have_tool on a computer without isync."""
    return None


class FakeImapConn:
    """An imaplib-shaped connection that records the commands it is sent."""

    def __init__(self, boxes):
        self.boxes = boxes      # name -> {'uids': [...], 'uidnext', 'uidvalidity'}
        self.commands = []

    def list(self, *a):
        self.commands.append('LIST')
        out = [f'(\\HasNoChildren) "/" "{name}"'.encode() for name in self.boxes]
        out.append(b'(\\Noselect \\HasChildren) "/" "[Gmail]"')
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
        return 'OK', [' '.join(str(u) for u in self.selected['uids']
                               if lo <= u <= hi).encode()]

    def logout(self):
        self.commands.append('LOGOUT')
        return 'BYE', []

    def __getattr__(self, name):        # store / expunge / append / delete ...
        def record(*a, **k):
            self.commands.append(name.upper())
            return 'OK', [b'']
        return record


def fill(maildir, folder, count, sub='cur', start=0):
    """`count` message files in a folder of a Maildir, named the way this
    platform names them (no ':' on Windows)."""
    path = bulk.folder_maildir(maildir, folder)
    for d in ('cur', 'new', 'tmp'):
        os.makedirs(os.path.join(path, d), exist_ok=True)
    for i in range(start, start + count):
        name = f'17000{i:05d}.M{i}P1.host' + (compat.maildir_info('S') if sub == 'cur' else '')
        with open(os.path.join(path, sub, name), 'wb') as f:
            f.write(b'x' * 100)
    return path


def sync_state(path, max_uid, uidvalidity=7):
    with open(os.path.join(path, '.mbsyncstate'), 'w', encoding='utf-8') as f:
        f.write(f'FarUidValidity {uidvalidity}\nNearUidValidity 1\n'
                f'MaxPulledUid {max_uid}\n\n')


class WindowsCase(unittest.TestCase):
    """IS_WINDOWS set, no external program on PATH."""

    block_posix_modules = False

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='ma-win-bulk-')
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        patches = [patch.object(compat, 'IS_WINDOWS', True),
                   patch.object(compat, 'have_tool', no_tools)]
        if self.block_posix_modules:
            patches.append(patch.dict(sys.modules, {n: None for n in POSIX_ONLY}))
        for p in patches:
            p.start()
            self.addCleanup(p.stop)


# ---------------------------------------------------------------------------
# The engine: state file, lock, Reconcile
# ---------------------------------------------------------------------------

class EngineTests(WindowsCase):
    block_posix_modules = True

    def setUp(self):
        super().setUp()
        self.cfg = os.path.join(self.tmp, 'config')
        self.maildir = os.path.join(self.tmp, 'MailArchiver', 'me', 'big_box_at_gmail_com')
        os.makedirs(self.cfg)
        os.makedirs(self.maildir)
        self.store = bulk.JobStore(self.cfg, EMAIL)

    def test_needs_isync_is_said_only_where_isync_is_optional(self):
        self.assertEqual(bulk.isync_missing(), bulk.NEEDS_ISYNC)
        self.assertIn('needs isync', bulk.NEEDS_ISYNC)
        self.assertIn('mbsync', bulk.NEEDS_ISYNC)
        with patch.object(compat, 'have_tool', lambda name: 'C:\\tools\\mbsync.exe'):
            self.assertEqual(bulk.isync_missing(), '')
        # Linux: isync is a dependency of the package; nothing changes there
        with patch.object(compat, 'IS_WINDOWS', False):
            self.assertEqual(bulk.isync_missing(), '')

    def test_state_file_is_written_under_the_lock_without_fcntl(self):
        job = bulk.new_job(EMAIL, [ALLMAIL], budget_bytes=1000, window_start='01:00',
                           window_end='06:00', profile='gmail')
        bulk.start_job(self.store, job)
        self.assertEqual(self.store.load()['job']['folders'], [ALLMAIL])
        bulk.set_status(self.store, 'pause')
        self.assertEqual(self.store.load()['job']['status'], 'paused')
        with self.assertRaises(bulk.BulkError):
            bulk.start_job(self.store, job)             # one open job per account
        self.assertEqual(sorted(os.listdir(self.cfg)),
                         ['bulk_sync.lock', bulk.state_filename(EMAIL)])
        with open(self.store.path, encoding='utf-8') as f:
            self.assertEqual(json.load(f)['job']['status'], 'paused')

    def test_updates_from_several_threads_are_all_kept(self):
        def bump(doc):
            doc['n'] = doc.get('n', 0) + 1

        def work():
            for _ in range(15):
                self.store.update(bump)
        # Real Windows: four threads, the msvcrt lock, nothing lost. Faked
        # Windows on Linux has no lock at all (no msvcrt), so one thread:
        # the point there is that update() works without fcntl.
        threads = [threading.Thread(target=work) for _ in range(4 if os.name == 'nt' else 1)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(120)
        self.assertEqual(self.store.load()['n'], 15 * len(threads))

    def test_reconcile_compares_server_and_local_maildir(self):
        fill(self.maildir, 'INBOX', 5)
        sync_state(bulk.folder_maildir(self.maildir, 'INBOX'), max_uid=5)
        allmail = fill(self.maildir, ALLMAIL, 3)
        fill(self.maildir, ALLMAIL, 2, sub='new', start=3)
        sync_state(allmail, max_uid=5)
        conn = FakeImapConn({
            'INBOX': {'uids': [1, 2, 3, 4, 5], 'uidnext': 6},
            ALLMAIL: {'uids': list(range(1, 9)), 'uidnext': 9},     # 3 newer than the sync
            'Never Synced': {'uids': [1, 2], 'uidnext': 3},
        })
        out = bulk.reconcile(bulk.ImapReadOnly(conn), self.maildir)
        rows = {r['folder']: r for r in out['folders']}
        self.assertEqual(sorted(rows), ['INBOX', 'Never Synced', ALLMAIL])
        self.assertEqual((rows['INBOX']['verdict'], rows['INBOX']['local_messages'],
                          rows['INBOX']['server_messages']), ('in-sync', 5, 5))
        self.assertEqual((rows[ALLMAIL]['verdict'], rows[ALLMAIL]['local_messages'],
                          rows[ALLMAIL]['new_since_last_sync'], rows[ALLMAIL]['missing']),
                         ('new-mail', 5, 3, 0))
        self.assertEqual((rows['Never Synced']['verdict'], rows['Never Synced']['in_archive']),
                         ('not-archived', False))
        self.assertEqual(out['server_folders'], ['INBOX', 'Never Synced', ALLMAIL])
        self.assertLessEqual(set(conn.commands), READ_ONLY_COMMANDS)

    def test_reconcile_without_any_isync_state(self):
        """The Windows sync writes no .mbsyncstate: the counts are still
        compared, and a difference is reported as missing."""
        fill(self.maildir, 'INBOX', 4)
        conn = FakeImapConn({'INBOX': {'uids': [1, 2, 3, 4, 5, 6], 'uidnext': 7}})
        out = bulk.reconcile(bulk.ImapReadOnly(conn), self.maildir, ['INBOX'])
        row = out['folders'][0]
        self.assertEqual((row['verdict'], row['delta'], row['missing'], row['last_sync']),
                         ('missing', 2, 2, None))
        self.assertLessEqual(set(conn.commands), READ_ONLY_COMMANDS)

    def test_folder_names_keep_the_slash_local_paths_use_the_platform_separator(self):
        path = bulk.folder_maildir(self.maildir, ALLMAIL)
        self.assertEqual(path, os.path.join(self.maildir, '[Gmail]', 'All Mail'))
        self.assertEqual(bulk.exclusion_pattern(None, [ALLMAIL]), '* "![Gmail]/All Mail"')
        for bad in ('../x', '/abs', 'a//b', ''):
            with self.assertRaises(bulk.BulkError):
                bulk.validate_folder(bad)

    def test_importer_folders_are_left_out_of_the_regular_sync(self):
        # config dir with the platform's separator, a trailing one included
        user_root = os.path.dirname(self.maildir)
        config_dir = os.path.join(user_root, '.config') + os.sep
        os.makedirs(config_dir)
        os.makedirs(os.path.join(self.maildir, 'PST', 'Inbox', 'cur'))
        self.assertEqual(bulk.base_sync_pattern(config_dir, {'email': EMAIL}),
                         '* "!PST" "!PST/*"')

    def test_pid_probe_never_signals(self):
        self.assertTrue(bulk._pid_alive(os.getpid()))
        self.assertFalse(bulk._pid_alive(0))
        self.assertFalse(bulk._pid_alive('not a pid'))
        gone = subprocess.Popen([sys.executable, '-c', 'pass'])
        gone.wait()
        self.assertFalse(bulk._pid_alive(gone.pid))


class SupervisorTests(unittest.TestCase):
    """The stop path where select() cannot watch stdin (Windows): the
    supervisor is told to stop through its stdin and ends the child."""

    CHILD = 'import time; print("started", flush=True); time.sleep(120)'

    def supervisor(self):
        code = (
            'import sys\n'
            f'sys.path.insert(0, {ROOT!r})\n'
            'import platform_compat\n'
            'platform_compat.IS_WINDOWS = True\n'
            'import bulk_sync\n'
            'sys.exit(bulk_sync.supervise(sys.argv[1:], grace=5))\n')
        return subprocess.Popen(
            [sys.executable, '-c', code, sys.executable, '-c', self.CHILD],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True)

    def _stops(self, how):
        proc = self.supervisor()
        try:
            self.assertEqual(proc.stdout.readline().strip(), 'started')
            self.assertIsNone(proc.poll())
            started = time.time()
            if how == 'line':
                proc.stdin.write('stop\n')
                proc.stdin.flush()
            proc.stdin.close()                  # 'eof': the runner died
            rc = proc.wait(timeout=30)
            self.assertNotEqual(rc, 0)          # the child was stopped, it did not finish
            self.assertLess(time.time() - started, 25)
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.stdout.close()

    def test_a_stop_line_ends_the_child(self):
        self._stops('line')

    def test_end_of_file_on_stdin_ends_the_child(self):
        self._stops('eof')

    def test_exit_code_passes_through(self):
        code = (
            'import sys\n'
            f'sys.path.insert(0, {ROOT!r})\n'
            'import platform_compat\n'
            'platform_compat.IS_WINDOWS = True\n'
            'import bulk_sync\n'
            'sys.exit(bulk_sync.supervise(sys.argv[1:], grace=5))\n')
        proc = subprocess.Popen(
            [sys.executable, '-c', code, sys.executable, '-c', 'import sys; sys.exit(7)'],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            self.assertEqual(proc.wait(timeout=30), 7)
        finally:
            proc.stdin.close()


# ---------------------------------------------------------------------------
# With the app: the page, the actions, Reconcile
# ---------------------------------------------------------------------------

class AppTests(WindowsCase):
    def setUp(self):
        super().setUp()
        self._orig_jobs = slc.JOB_STATE_DIR
        slc.JOB_STATE_DIR = os.path.join(self.tmp, 'jobs')
        os.makedirs(slc.JOB_STATE_DIR)
        self.addCleanup(self._restore)
        shutil.rmtree(os.path.join(ma.CONFIG['data_dir'], USER), ignore_errors=True)
        cfg = ma.get_user_config_dir(USER)
        with open(os.path.join(str(cfg), 'accounts.json'), 'w', encoding='utf-8') as f:
            json.dump([{'email': EMAIL, 'provider': 'gmail', 'enabled': True,
                        'sync_interval': 'daily'}], f)
        self.maildir = routes.account_maildir(USER, EMAIL)
        ma.app.config['TESTING'] = True
        self.spawned = []
        p = patch.object(routes, 'spawn_manual_run',
                         lambda u, e: self.spawned.append((u, e)))
        p.start()
        self.addCleanup(p.stop)
        self.conn = FakeImapConn({
            'INBOX': {'uids': [1, 2, 3, 4, 5, 6], 'uidnext': 7},
            ALLMAIL: {'uids': list(range(1, 11)), 'uidnext': 11}})
        p = patch.object(routes, 'IMAP_FACTORY',
                         lambda username, acct: bulk.ImapReadOnly(self.conn))
        p.start()
        self.addCleanup(p.stop)

    def _restore(self):
        slc.JOB_STATE_DIR = self._orig_jobs
        shutil.rmtree(os.path.join(ma.CONFIG['data_dir'], USER), ignore_errors=True)

    def client(self):
        c = ma.app.test_client()
        with c.session_transaction() as s:
            s['username'] = USER
            s['login_time'] = time.time()
        return c

    def flashes(self, c):
        with c.session_transaction() as s:
            return [m for _cat, m in s.get('_flashes', [])]

    def job(self):
        return routes.job_store(USER, EMAIL).load()['job']

    def open_job_directly(self):
        """A job that exists already (set up while isync was installed)."""
        job = bulk.new_job(EMAIL, [ALLMAIL], budget_bytes=1000, window_start='00:00',
                           window_end='00:00', profile='gmail')
        bulk.start_job(bulk.JobStore(str(ma.get_user_config_dir(USER)), EMAIL), job)
        return job

    def test_overview_page_renders_and_says_it_needs_isync(self):
        r = self.client().get('/bulk-sync')
        self.assertEqual(r.status_code, 200)
        html = r.get_data(as_text=True)
        self.assertIn(EMAIL, html)
        self.assertIn('id="bs-needs-isync"', html)
        self.assertIn('needs isync', html)
        self.assertIn('was not found on PATH', html)

    def test_account_page_renders_with_the_actions_disabled(self):
        r = self.client().get(f'/bulk-sync/{EMAIL}')
        self.assertEqual(r.status_code, 200)
        html = r.get_data(as_text=True)
        self.assertIn('id="bs-needs-isync"', html)
        self.assertIn('needs isync', html)
        import re
        start = re.search(r'<button[^>]*>Start initial / rebase sync</button>', html)
        self.assertIsNotNone(start)
        self.assertIn('disabled', start.group(0))
        # Reconcile stays available
        check = re.search(r'<button[^>]*>Check the server now</button>', html)
        self.assertIsNotNone(check)
        self.assertNotIn('disabled', check.group(0))

    def test_start_is_refused_with_the_message(self):
        c = self.client()
        r = c.post(f'/bulk-sync/{EMAIL}/start', data={
            'folders': [ALLMAIL], 'budget_mb': '1500',
            'window_start': '02:00', 'window_end': '05:30'})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.flashes(c), [bulk.NEEDS_ISYNC])
        self.assertIsNone(self.job())
        self.assertEqual(self.spawned, [])
        with self.assertRaises(bulk.BulkError) as cm:
            routes.create_job(USER, EMAIL, [ALLMAIL])
        self.assertEqual(str(cm.exception), bulk.NEEDS_ISYNC)
        r = c.get(f'/bulk-sync/{EMAIL}')            # and the page still renders
        self.assertEqual(r.status_code, 200)

    def test_run_now_is_refused_with_the_message(self):
        self.open_job_directly()
        c = self.client()
        r = c.post(f'/bulk-sync/{EMAIL}/run')
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.flashes(c), [bulk.NEEDS_ISYNC])
        self.assertEqual(self.spawned, [])
        html = c.get(f'/bulk-sync/{EMAIL}').get_data(as_text=True)
        import re
        run = re.search(r'<button[^>]*>Run now</button>', html)
        self.assertIsNotNone(run)
        self.assertIn('disabled', run.group(0))
        self.assertIn('Cancel job', html)           # an existing job can still be cancelled

    def test_fetch_missing_is_refused_with_the_message(self):
        c = self.client()
        r = c.post(f'/bulk-sync/{EMAIL}/fetch-missing', data={'folder': 'INBOX'})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.flashes(c), [bulk.NEEDS_ISYNC])
        self.assertIsNone(self.job())
        self.assertEqual(self.spawned, [])

    def test_a_run_started_some_other_way_is_refused_too(self):
        with self.assertRaises(bulk.BulkError) as cm:
            routes.start_mbsync(USER, EMAIL, os.path.join(self.tmp, 'rc'))
        self.assertEqual(str(cm.exception), bulk.NEEDS_ISYNC)
        buf = io.StringIO()
        with redirect_stdout(buf):
            routes.scheduled_bulk_sync()
        self.assertIn('Nothing run: ' + bulk.NEEDS_ISYNC, buf.getvalue())
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = routes.cli(['--user', USER, '--account', EMAIL, '--manual'])
        self.assertEqual(rc, 1)
        self.assertIn(bulk.NEEDS_ISYNC, buf.getvalue())

    def test_reconcile_works_without_isync(self):
        fill(self.maildir, 'INBOX', 6)
        fill(self.maildir, ALLMAIL, 4)
        c = self.client()
        r = c.post(f'/bulk-sync/{EMAIL}/reconcile')
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.flashes(c), [])
        rec = routes.job_store(USER, EMAIL).load()['reconcile']
        self.assertTrue(rec['ok'], rec)
        rows = {r['folder']: r for r in rec['folders']}
        self.assertEqual((rows['INBOX']['verdict'], rows['INBOX']['local_messages']),
                         ('in-sync', 6))
        self.assertEqual((rows[ALLMAIL]['server_messages'], rows[ALLMAIL]['local_messages'],
                          rows[ALLMAIL]['delta']), (10, 4, 6))
        self.assertLessEqual(set(self.conn.commands), READ_ONLY_COMMANDS)
        r = c.get(f'/bulk-sync/{EMAIL}')
        self.assertEqual(r.status_code, 200)
        html = r.get_data(as_text=True)
        self.assertIn('In sync', html)
        self.assertIn('Checked ', html)
        self.assertIn('id="bs-needs-isync"', html)

    def test_local_folder_names_are_written_with_a_slash(self):
        fill(self.maildir, ALLMAIL, 1)
        fill(self.maildir, 'Lists/python', 1)
        self.assertEqual(sorted(routes.local_folders(USER, EMAIL)),
                         ['Lists/python', ALLMAIL])

    def test_status_api_answers(self):
        self.open_job_directly()
        r = self.client().get('/api/bulk-sync/status')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()['accounts'][0]['job']['folders'], [ALLMAIL])


class LinuxUnchangedTests(unittest.TestCase):
    """Without the Windows flag nothing asks for isync up front: a host
    without mbsync says so when a run starts, as before 1.3.1."""

    def test_no_needs_isync_refusal_on_linux(self):
        if os.name == 'nt':
            self.skipTest('this asserts the Linux behaviour')
        with patch.object(compat, 'have_tool', no_tools):
            self.assertEqual(bulk.isync_missing(), '')
        self.assertEqual(compat.detached_popen_kwargs(), {'start_new_session': True})


if __name__ == '__main__':
    unittest.main()
