"""mail-archiver-fix-perms: the packaged helper that makes the installed
application tree root's on every install and upgrade (1.3.1)."""
import os
import re
import stat
import subprocess
import sys
import tempfile
import unittest

import pytest

# The helper is a POSIX shell tool that sets unix owners and modes; it is
# not part of the Windows build.
pytestmark = pytest.mark.skipif(os.name == 'nt', reason='mail-archiver-fix-perms is Linux packaging only')

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOL = os.path.join(REPO, 'mail-archiver-fix-perms')


def mode(p):
    return stat.S_IMODE(os.lstat(p).st_mode)


class FixPermsBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = self._tmp.name
        self.app = os.path.join(self.root, 'opt', 'mail-archiver')
        for d in ('templates', 'static', 'certs', '__pycache__'):
            os.makedirs(os.path.join(self.app, d))
        os.makedirs(os.path.join(self.root, 'usr', 'libexec'))
        os.makedirs(os.path.join(self.root, 'etc', 'cron.d'))
        self.files = {
            'app': os.path.join(self.app, 'app.py'),
            'tpl': os.path.join(self.app, 'templates', 'base.html'),
            'key': os.path.join(self.app, 'certs', 'mail-archiver.key'),
            'secret': os.path.join(self.app, '.secret_key'),
            'stale': os.path.join(self.app, '__pycache__', 'old.cpython-311.pyc'),
            'cron': os.path.join(self.root, 'etc', 'cron.d', 'mail-archiver'),
            'helper': os.path.join(self.root, 'usr', 'libexec', 'mail-archiver-cred'),
        }
        for name, p in self.files.items():
            with open(p, 'w') as f:
                f.write('x = 1\n' if p.endswith('.py') else name + '\n')
        self.owner = f'{os.getuid()}:{os.getgid()}'

    def run_tool(self, *args, owner=None):
        return subprocess.run(['bash', TOOL, '--root', self.root, '--owner', owner or self.owner, *args],
                              capture_output=True, text=True)

    def loosen(self):
        os.chmod(self.app, 0o775)
        os.chmod(os.path.join(self.app, 'templates'), 0o777)
        os.chmod(self.files['app'], 0o664)
        os.chmod(self.files['tpl'], 0o666)
        os.chmod(self.files['cron'], 0o664)
        os.chmod(self.files['helper'], 0o775)
        os.chmod(self.files['key'], 0o640)
        os.chmod(self.files['secret'], 0o640)
        os.chmod(self.files['stale'], 0o666)


class FixPermsTests(FixPermsBase):
    def test_group_and_other_write_is_removed_everywhere(self):
        self.loosen()
        r = self.run_tool()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(mode(self.app), 0o755)
        self.assertEqual(mode(os.path.join(self.app, 'templates')), 0o755)
        self.assertEqual(mode(self.files['app']), 0o644)
        self.assertEqual(mode(self.files['tpl']), 0o644)
        self.assertEqual(mode(self.files['cron']), 0o644)
        self.assertEqual(mode(self.files['helper']), 0o755)
        self.assertIn('repair(s) made', r.stdout)
        self.assertRegex(r.stdout, r'mode 775 -> 755 .*/opt/mail-archiver\n')

    def test_certs_and_secret_key_are_left_to_the_service_account(self):
        self.loosen()
        self.run_tool()
        self.assertEqual(mode(self.files['key']), 0o640)
        self.assertEqual(mode(self.files['secret']), 0o640)
        self.assertEqual(open(self.files['key']).read(), 'key\n')

    def test_stale_bytecode_is_dropped_and_rebuilt_from_the_sources(self):
        self.loosen()
        self.run_tool()
        cache = os.path.join(self.app, '__pycache__')
        names = os.listdir(cache)
        self.assertNotIn('old.cpython-311.pyc', names)
        self.assertTrue(any(re.match(r'app\.cpython-\d+\.pyc$', n) for n in names), names)
        for n in names:
            self.assertEqual(mode(os.path.join(cache, n)) & 0o022, 0)

    def test_check_reports_and_changes_nothing(self):
        self.loosen()
        r = self.run_tool('--check')
        self.assertEqual(r.returncode, 1)
        self.assertIn('WRITABLE BY OTHERS (mode 775)', r.stdout)
        self.assertIn('STALE BYTECODE', r.stdout)
        self.assertEqual(mode(self.app), 0o775)
        self.assertEqual(mode(self.files['app']), 0o664)
        self.assertTrue(os.path.exists(self.files['stale']))

    def test_check_is_clean_after_a_repair_and_a_second_run_repairs_nothing(self):
        self.loosen()
        self.run_tool()
        r = self.run_tool('--check')
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn('ownership and permissions are correct', r.stdout)
        r = self.run_tool()
        self.assertNotIn('repair(s) made', r.stdout)

    def test_links_are_never_followed_or_changed(self):
        outside = os.path.join(self.root, 'outside')
        os.makedirs(outside)
        victim = os.path.join(outside, 'victim.txt')
        with open(victim, 'w') as f:
            f.write('v')
        os.chmod(victim, 0o666)
        os.chmod(outside, 0o777)
        os.symlink(victim, os.path.join(self.app, 'link-to-file'))
        os.symlink(outside, os.path.join(self.app, 'templates', 'link-to-dir'))
        r = self.run_tool()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(mode(victim), 0o666)
        self.assertEqual(mode(outside), 0o777)
        self.assertNotIn('outside', r.stdout)

    def test_a_missing_tree_is_not_an_error(self):
        empty = tempfile.mkdtemp(dir=self.root)
        r = subprocess.run(['bash', TOOL, '--root', empty], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_unknown_option_is_refused(self):
        r = subprocess.run(['bash', TOOL, '--nope'], capture_output=True, text=True)
        self.assertEqual(r.returncode, 2)

    @unittest.skipUnless(getattr(os, 'getuid', lambda: -1)() == 0, 'changing owners needs root (runs in the build container)')
    def test_wrong_owner_is_set_to_root(self):
        for p in (self.app, os.path.join(self.app, 'templates'), self.files['app'], self.files['stale']):
            os.chown(p, 1001, 1000)
        r = self.run_tool('--check', owner='0:0')
        self.assertEqual(r.returncode, 1)
        self.assertIn('WRONG OWNER 1001:1000', r.stdout)
        r = self.run_tool(owner='0:0')
        self.assertEqual(r.returncode, 0, r.stderr)
        for p in (self.app, os.path.join(self.app, 'templates'), self.files['app']):
            st = os.lstat(p)
            self.assertEqual((st.st_uid, st.st_gid), (0, 0), p)
        self.assertIn('owner 1001:1000 -> 0:0', r.stdout)


class PackagingWiringTests(unittest.TestCase):
    """The helper only helps if the package ships it and the scripts call it."""

    def read(self, rel):
        return open(os.path.join(REPO, rel)).read()

    def test_deb_postinst_and_rpm_post_call_the_helper(self):
        for rel in ('debian/postinst', 'packaging/rpm/mail-archiver.spec'):
            self.assertIn('/usr/libexec/mail-archiver-fix-perms || true', self.read(rel), rel)

    def test_build_ships_the_helper_in_both_packages(self):
        rules = self.read('debian/rules')
        self.assertRegex(rules, r'install -m 755 .*mail-archiver-fix-perms \$\(D\)/usr/libexec/')
        spec = self.read('packaging/rpm/mail-archiver.spec')
        self.assertIn('%{buildroot}/usr/libexec/mail-archiver-fix-perms', spec)
        self.assertRegex(spec, r'(?m)^/usr/libexec/mail-archiver-fix-perms$')

    def test_msgconvert_is_a_hard_dependency(self):
        control = self.read('debian/control')
        dep = re.search(r'(?m)^Depends: (.*)$', control).group(1)
        rec = re.search(r'(?m)^Recommends: (.*)$', control).group(1)
        self.assertIn('libemail-outlook-message-perl', dep)
        self.assertNotIn('libemail-outlook-message-perl', rec)
        spec = self.read('packaging/rpm/mail-archiver.spec')
        self.assertRegex(spec, r'(?m)^Requires:\s+/usr/bin/msgconvert$')
        self.assertNotRegex(spec, r'(?m)^Recommends:\s+/usr/bin/msgconvert$')


if __name__ == '__main__':
    unittest.main()
