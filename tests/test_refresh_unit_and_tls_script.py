"""1.3.4: the token refresh unit takes the main service's settings, and the
TLS script does not leave a reader loop early."""
import os
import re
import subprocess
import textwrap

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

pytestmark = pytest.mark.skipif(os.name == 'nt', reason='systemd units and their shell wrappers are Linux packaging only')


def _read(rel):
    with open(os.path.join(ROOT, rel), encoding='utf-8') as f:
        return f.read()


def test_refresh_unit_starts_through_the_wrapper():
    unit = _read('packaging/systemd/mail-archiver-oauth-refresh.service')
    assert re.search(r'(?m)^ExecStart=/usr/libexec/mail-archiver-oauth-refresh$', unit)
    # no fixed list of writable paths: the data directory can be anywhere
    assert not re.search(r'(?m)^ReadWritePaths=', unit)
    assert not re.search(r'(?m)^ProtectSystem=strict$', unit)
    # system accounts: the token is stored in a user-owned directory
    caps = re.search(r'(?m)^AmbientCapabilities=(.*)$', unit).group(1).split()
    assert sorted(caps) == ['CAP_CHOWN', 'CAP_DAC_OVERRIDE', 'CAP_FOWNER']
    bound = re.search(r'(?m)^CapabilityBoundingSet=(.*)$', unit).group(1).split()
    assert sorted(bound) == sorted(caps)
    assert re.search(r'(?m)^ReadOnlyPaths=/opt/mail-archiver( |$)', unit)


def test_refresh_wrapper_runs_the_refresh_and_is_checked_by_fix_perms():
    assert _read('mail-archiver-oauth-refresh').rstrip().endswith('exec python3 oauth2_microsoft.py refresh-expiring')
    assert '"$ROOT/usr/libexec/mail-archiver-oauth-refresh"' in _read('mail-archiver-fix-perms')


def test_timer_is_enabled_at_first_installation_only():
    post = _read('debian/postinst')
    block = post[post.index('if [ -z "${2:-}" ]; then'):]
    block = block[:block.index('fi')]
    assert 'systemctl enable mail-archiver-oauth-refresh.timer' in block
    assert post.count('systemctl enable mail-archiver-oauth-refresh.timer') == 1
    spec = _read('packaging/rpm/mail-archiver.spec')
    sblock = spec[spec.index('if [ "$1" -eq 1 ]; then\n    systemctl enable mail-archiver-oauth-refresh.timer'):]
    assert spec.count('systemctl enable mail-archiver-oauth-refresh.timer') == 1 and sblock


def test_refresh_wrapper_is_packaged_for_deb_and_rpm():
    assert re.search(r'install -m 755 mail-archiver-oauth-refresh .*\$\(D\)/usr/libexec/', _read('debian/rules'))
    spec = _read('packaging/rpm/mail-archiver.spec')
    assert 'install -m 755 mail-archiver-oauth-refresh %{buildroot}/usr/libexec/mail-archiver-oauth-refresh' in spec
    files = spec[spec.index('\n%files'):spec.index('\n%post\n')] if spec.index('\n%post\n') > spec.index('\n%files') else spec[spec.index('\n%files'):spec.index('\n%changelog')]
    assert re.search(r'(?m)^/usr/libexec/mail-archiver-oauth-refresh$', files)
    install = spec[spec.index('\n%install'):spec.index('\n%files')]
    assert not re.search(r'(?m)^/usr/libexec/', install)   # a bare path there would be run as a command
    assert os.access(os.path.join(ROOT, 'mail-archiver-oauth-refresh'), os.X_OK)


def _env_helper(tmp_path, shown=None, systemctl_exit=0, load_state='loaded'):
    """Run mail-archiver-env with a stand-in systemctl that prints `shown`
    (the text of `systemctl show ... --value`), then `eval` its output in
    bash and report three variables."""
    fake = tmp_path / 'systemctl'
    (tmp_path / 'shown.txt').write_text(shown or '')
    (tmp_path / 'state.txt').write_text(load_state + '\n')
    fake.write_text(f'#!/bin/sh\ncase "$*" in *LoadState*) cat "{tmp_path}/state.txt";; '
                    f'*) cat "{tmp_path}/shown.txt";; esac\nexit {systemctl_exit}\n')
    fake.chmod(0o755)
    helper = os.path.join(ROOT, 'mail-archiver-env')
    script = textwrap.dedent(f'''
        set -euo pipefail
        exports=$("{helper}") || {{ echo "STOPPED"; exit 1; }}
        eval "$exports"
        printf 'DATA=[%s]\\n' "${{MAIL_ARCHIVER_DATA-unset}}"
        printf 'X=[%s]\\n' "${{MAIL_ARCHIVER_X:-unset}}"
        printf 'OTHER=[%s]\\n' "${{OTHER:-unset}}"
    ''')
    env = {'PATH': f'{tmp_path}:/usr/bin:/bin'}
    return subprocess.run(['bash', '-c', script], env=env, capture_output=True, text=True)


def test_env_helper_exports_only_the_mail_archiver_settings(tmp_path):
    r = _env_helper(tmp_path, 'PYTHONUNBUFFERED=1 MAIL_ARCHIVER_DATA=/srv/somewhere/mail OTHER=1\n')
    assert r.returncode == 0, r.stderr
    assert 'DATA=[/srv/somewhere/mail]' in r.stdout
    assert 'OTHER=[unset]' in r.stdout


def test_env_helper_undoes_systemd_quoting(tmp_path):
    """As `systemctl show` prints them: quoted, with backslash escapes."""
    cases = [
        ('"MAIL_ARCHIVER_DATA=/srv/my mail"', '/srv/my mail'),
        ('"MAIL_ARCHIVER_DATA=say \\"hi\\""', 'say "hi"'),
        ('"MAIL_ARCHIVER_DATA=pa\\$word"', 'pa$word'),
        ('"MAIL_ARCHIVER_DATA=back\\\\slash"', 'back\\slash'),
        ("\"MAIL_ARCHIVER_DATA=it's\"", "it's"),
        ('MAIL_ARCHIVER_DATA=', ''),
        ('"MAIL_ARCHIVER_DATA=a\\tb"', 'a\tb'),
        # systemd leaves these bare: they are not separators for it
        ('MAIL_ARCHIVER_DATA=/srv/a\u00a0b', '/srv/a\u00a0b'),
        ('MAIL_ARCHIVER_DATA=/srv/a\u3000b', '/srv/a\u3000b'),
        # break-out attempts through eval
        ('"MAIL_ARCHIVER_DATA=x\'; touch PWNED; echo \'"', "x'; touch PWNED; echo '"),
        ('"MAIL_ARCHIVER_DATA=\\$(touch PWNED)"', '$(touch PWNED)'),
    ]
    for shown, want in cases:
        r = _env_helper(tmp_path, 'A=1 ' + shown + ' "FOO=other \\"quoted\\" thing" MAIL_ARCHIVER_X=plain\n')
        assert r.returncode == 0, (shown, r.stderr)
        assert f'DATA=[{want}]' in r.stdout, (shown, r.stdout)
        assert 'X=[plain]' in r.stdout
        assert not os.path.exists('PWNED') and not (tmp_path / 'PWNED').exists()


def test_env_helper_stops_when_the_unit_is_not_loaded(tmp_path):
    """systemctl show answers exit 0 and an empty Environment for an unknown unit."""
    r = _env_helper(tmp_path, '', load_state='not-found')
    assert r.returncode != 0 and 'STOPPED' in r.stdout
    assert 'not loaded' in r.stderr


def test_env_helper_stops_when_settings_cannot_be_read_or_parsed(tmp_path):
    r = _env_helper(tmp_path, 'MAIL_ARCHIVER_DATA=/srv/a\n', systemctl_exit=1)
    assert r.returncode != 0 and 'STOPPED' in r.stdout and 'DATA=' not in r.stdout
    r = _env_helper(tmp_path, 'MAIL_ARCHIVER_DATA=/srv/a "MAIL_ARCHIVER_X=un balanced\n')
    assert r.returncode != 0 and 'STOPPED' in r.stdout and 'DATA=' not in r.stdout
    assert 'cannot parse' in r.stderr


def test_both_wrappers_use_the_env_helper_and_stop_without_it():
    for name in ('mail-archiver-oauth-refresh', 'mail-archiver-scheduled-sync'):
        w = _read(name)
        assert 'exports=$(/usr/libexec/mail-archiver-env) || {' in w
        assert 'exit 1; }' in w[w.index('exports=$('):w.index('eval "$exports"')]
        assert 'xargs' not in w and "tr ' '" not in w
    assert 'sync-schedule)      job=sync-schedule' in _read('mail-archiver-scheduled-sync')


def test_env_helper_is_packaged_and_checked():
    assert 'mail-archiver-env $(D)/usr/libexec/' in _read('debian/rules')
    spec = _read('packaging/rpm/mail-archiver.spec')
    files = spec[spec.index('\n%files'):spec.index('\n%preun')] if spec.index('\n%preun') > spec.index('\n%files') else spec[spec.index('\n%files'):]
    assert re.search(r'(?m)^/usr/libexec/mail-archiver-env$', files)
    assert '"$ROOT/usr/libexec/mail-archiver-env"' in _read('mail-archiver-fix-perms')
    assert os.access(os.path.join(ROOT, 'mail-archiver-env'), os.X_OK)


def test_refresh_unit_is_closed_off_from_password_files():
    unit = _read('packaging/systemd/mail-archiver-oauth-refresh.service')
    line = re.search(r'(?m)^InaccessiblePaths=(.*)$', unit).group(1)
    assert '-/etc/shadow' in line.split() and '-/etc/gshadow' in line.split()
    ro = re.search(r'(?m)^ReadOnlyPaths=(.*)$', unit).group(1).split()
    assert '/opt/mail-archiver' in ro and '-/run/systemd/system' in ro
    assert '-/root' in line.split()


def test_package_removal_stops_and_disables_the_units():
    prerm = _read('debian/prerm')
    assert 'if [ "$1" = "remove" ]; then' in prerm and 'systemctl disable --now "$u"' in prerm
    assert 'install -m 755 debian/prerm $(D)/DEBIAN/prerm' in _read('debian/rules')
    spec = _read('packaging/rpm/mail-archiver.spec')
    preun = spec[spec.index('\n%preun'):spec.index('\n%postun')]
    assert 'mail-archiver-oauth-refresh.timer' in preun and 'disable --now' in preun


def test_tls_script_reads_the_key_list_before_looping():
    script = _read('install-tls-cert.sh')
    assert 'done < <(key_candidates' not in script
    assert 'mapfile -t cands < <(key_candidates "$crt")' in script


def _key_scan(loop_body, tmp_path):
    """Run key_candidates with the given consumer the way systemd runs it:
    SIGPIPE ignored, and more candidates than a pipe buffer holds, so a
    writer that outlives its reader gets EPIPE on every further line."""
    script = _read('install-tls-cert.sh')
    func = script[script.index('key_candidates() {'):script.index('pubkey_of_cert()')]
    d = tmp_path / 'keys'
    d.mkdir()
    (d / 'privkey.pem').write_text('x')          # first candidate: the match
    for i in range(3000):
        (d / f'key-file-with-a-long-name-{i:04d}.key').write_text('x')
    body = "trap '' PIPE\n" + func + f'\ncrt="{d}/host.crt"; k=""\n' + loop_body + '\nsleep 0.3\necho "picked=$k"\n'
    return subprocess.run(['bash', '-c', body], capture_output=True, text=True)


OLD_LOOP = '''while read -r cand; do [ -r "$cand" ] || continue; k="$cand"; break; done < <(key_candidates "$crt")'''


def test_the_old_reader_loop_does_log_broken_pipe(tmp_path):
    """Guard for the test below: the condition really reproduces the bug."""
    r = _key_scan(OLD_LOOP, tmp_path)
    if 'Broken pipe' not in r.stderr:
        # bash 5.1 (EL 9) does not report EPIPE from `echo`; the symptom is
        # seen with bash 5.2 (Debian 12/13). Nothing to guard here then.
        import pytest
        pytest.skip('this bash does not report a write error from echo')
    assert 'Broken pipe' in r.stderr


def test_key_candidates_consumer_writes_no_broken_pipe(tmp_path):
    """The consumer as shipped: taken from the script, not rewritten here."""
    script = _read('install-tls-cert.sh')
    start = script.index('local cand cands=()')
    end = script.index('done', start) + len('done')
    loop = script[start:end].replace('local cand cands=()', 'cands=()')
    loop = loop.replace('if [ "$(pubkey_of_cert "$crt")" = "$(pubkey_of_key "$cand")" ]; then', 'if true; then')
    r = _key_scan(loop, tmp_path)
    assert r.returncode == 0, r.stderr[-300:]
    assert 'Broken pipe' not in r.stderr
    assert 'picked=' in r.stdout and 'privkey.pem' in r.stdout
