"""1.3.3: every build has its sources.

Until 1.3.2 the rpm was built with `rpmbuild -bb` from loose files and the
deb with `dpkg-deb` from a hand-made tree: no SRPM, no source tarball and no
Debian source package ever existed. These tests keep the pieces that make
them agree with each other.
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def read(rel):
    path = ROOT / rel
    if not path.is_file():
        pytest.skip(f'{rel} is not part of this tree')
    return path.read_text(encoding='utf-8')


VERSION = (ROOT / 'VERSION').read_text(encoding='utf-8').strip()


def test_spec_version_is_the_version_file():
    spec = read('packaging/rpm/mail-archiver.spec')
    assert re.search(r'(?m)^Version:\s+(\S+)$', spec).group(1) == VERSION


def test_spec_changelog_starts_with_this_version():
    spec = read('packaging/rpm/mail-archiver.spec')
    first = spec.split('%changelog\n', 1)[1].splitlines()[0]
    assert first.endswith(f'- {VERSION}-1'), first


def test_debian_changelog_starts_with_this_version():
    first = read('debian/changelog').splitlines()[0]
    assert first.startswith(f'mail-archiver ({VERSION}-1) '), first


def test_spec_builds_from_a_source_tarball():
    spec = read('packaging/rpm/mail-archiver.spec')
    assert re.search(r'(?m)^Source0:\s+%\{name\}-%\{version\}\.tar\.gz$', spec)
    assert '%setup -q' in spec
    assert '_sourcedir' not in spec            # the loose-file build is gone
    assert '%undefine __brp_mangle_shebangs' in spec


def test_build_script_makes_source_packages():
    build = read('packaging/build.sh')
    assert 'rpmbuild --define "_topdir $top" -ba' in build       # -ba: SRPM too
    assert 'dpkg-buildpackage -us -uc -d --build=source,all' in build
    source = read('packaging/make-source.sh')
    assert '--add-virtual-file="${PREFIX}BUILD_INFO:$INFO"' in source
    assert 'gzip -n' in source                                    # same commit, same bytes


def test_both_packages_install_the_same_cron_file():
    assert 'packaging/cron.d/mail-archiver %{buildroot}/etc/cron.d/mail-archiver' in read(
        'packaging/rpm/mail-archiver.spec')
    assert 'packaging/cron.d/mail-archiver $(D)/etc/cron.d/mail-archiver' in read('debian/rules')
    cron = read('packaging/cron.d/mail-archiver')
    jobs = [line for line in cron.splitlines() if line and not line.startswith('#')]
    assert [j.split()[0] for j in jobs] == ['0', '30', '20']
    assert all(' root /usr/libexec/mail-archiver-scheduled-sync' in j for j in jobs)


def test_deb_and_rpm_ship_the_same_modules():
    spec = read('packaging/rpm/mail-archiver.spec')
    rules = read('debian/rules')
    in_spec = set(re.findall(r'(?m)^/opt/mail-archiver/(\w[\w.]*\.py)$', spec))
    block = re.search(r'(?s)MODULES := (.*?)\nUNITS', rules).group(1)
    in_rules = set(re.findall(r'[\w.]+\.py', block))
    assert in_spec == in_rules
    top_level = {p.name for p in ROOT.glob('*.py')}
    assert in_spec == top_level, 'a top-level module is missing from the packages'


def test_no_built_package_is_tracked():
    listed = [p.name for p in (ROOT / 'packaging').glob('*') if p.suffix in ('.rpm', '.deb')]
    gitignore = read('.gitignore')
    assert 'packaging/*.deb' in gitignore and 'packaging/*.rpm' in gitignore
    assert 'packaging/dist/' in gitignore
    # anything of that kind lying in the checkout is untracked build output
    assert all(name.startswith('mail-archiver') for name in listed)


def test_windows_build_writes_a_toolchain_record():
    ps1 = read('windows-app/build.ps1')
    assert 'MailArchiver-$Version-toolchain.txt' in ps1
    assert 'MSYS2 UCRT64 packages (pacman -Q)' in ps1


def test_rpm_starts_on_a_fresh_el_install():
    """1.3.3: three things stopped the service on EL 9 (found by installing it)."""
    spec = read('packaging/rpm/mail-archiver.spec')
    post = spec.split('%post\n', 1)[1]
    assert '\nmkdir -p /var/lib/mail-archiver\n' in post
    assert 'mkdir -p /datapool' not in spec
    assert re.search(r'(?m)^Requires:\s+cronie$', spec)
    assert "sed -i '/^SupplementaryGroups=shadow$/d'" in spec
    unit = read('mail-archiver.service')
    assert 'MAIL_ARCHIVER_DATA=/var/lib/mail-archiver' in unit
    assert 'SupplementaryGroups=shadow' in unit        # Debian still needs it


def test_system_ca_bundle_is_found_on_debian_and_el(tmp_path, monkeypatch):
    import importlib
    import os
    import sys
    sys.path.insert(0, str(ROOT))
    os.environ.setdefault('MAIL_ARCHIVER_AUTH', 'builtin')
    os.environ.setdefault('MAIL_ARCHIVER_SECRET', 'test-only-secret')
    os.environ.setdefault('MAIL_ARCHIVER_DATA', str(tmp_path))
    ma = importlib.import_module('app')
    el = tmp_path / 'ca-bundle.crt'
    el.write_text('x', encoding='utf-8')
    monkeypatch.setattr(ma, 'SYSTEM_CA_BUNDLES', (str(tmp_path / 'missing.crt'), str(el)))
    assert ma._default_ca_file() == str(el)
    monkeypatch.setattr(ma, 'SYSTEM_CA_BUNDLES', (str(tmp_path / 'a.crt'), str(tmp_path / 'b.crt')))
    assert ma._default_ca_file() == str(tmp_path / 'a.crt')     # nothing found: the first, as before


def test_licence_text_is_complete_and_shipped():
    licence = read('LICENSE')
    assert 'TERMS AND CONDITIONS FOR USE, REPRODUCTION, AND DISTRIBUTION' in licence
    assert 'END OF TERMS AND CONDITIONS' in licence
    assert 'Copyright' in read('NOTICE')
    assert re.search(r'(?m)^%license LICENSE NOTICE$', read('packaging/rpm/mail-archiver.spec'))
    assert 'debian/copyright $(D)/usr/share/doc/$(P)/copyright' in read('debian/rules')


def test_container_image_holds_no_secret_key():
    cf = read('Containerfile')
    assert 'MAIL_ARCHIVER_SECRET_FILE=/data/.secret_key' in cf
    assert not re.search(r'(?m)^RUN .*secret', cf)           # nothing generated at image build
