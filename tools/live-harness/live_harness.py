#!/usr/bin/env python3
"""Live harness: every local import format against an account that is syncing a LIVE mailbox.

Runs on a build machine. Nothing is installed on, or changed on, an archive host: the
package under test is installed in a throwaway debian:13 podman container (PAM mode, a
real unix mailbox owner), which syncs a TEST mailbox on an IMAP server you control while

  * a sender delivers a new message to that mailbox through the MTA every few seconds,
  * one fixture per import format is dropped into the account's import folder and imported,
  * a regular sync runs after every import (and one runs at the same time as an import).

Filler is generated mail, or (MA_HARNESS_FILLER_DIRS) real junk mail read from folders
you name. Every filler message gets a harness Subject and Message-ID, so each
expectation is an exact number. Fixtures and the container are removed at the end; the
report holds counts only, never mail content.

Configuration (server, mailbox, password file, CA file) comes from MA_HARNESS_*
environment variables or from tools/live-harness/harness.local.conf (not committed);
see harness.example.conf. With nothing configured the harness names the missing keys
and exits 2 without connecting anywhere.

What must hold (the report lists each as PASS/FAIL):
  per format    ledger row `done`, expected detected type, imported == fixture size,
                0 duplicates, 0 failed; mail under PST/ or Imported/, read, 0600, owned by
                the mailbox owner; searchable; source file moved to done/
  dedupe        the same file again is skipped (sha256); the same messages in another
                format are all duplicates, including messages without a Message-ID
  live sync     every sync ends `ok` with imported folders present; every message sent
                through the MTA is in the archive exactly once
  no leak       the server gains only the MTA deliveries: no PST/Imported folder, no
                other folder count changes
  the books     per server folder local == server; Reconcile says in sync;
                local total == server total + imported total; no retention deletions

usage: live_harness.py [--deb PATH] [--interval S] [--per-format N] [--keep-container]
                       [--extra DIR]   (DIR may hold fixture.pst and/or msg/*.msg made with Outlook)
"""
import argparse, email, email.policy, gzip, io, json, mailbox, os, re, shutil, smtplib, subprocess
import sys, tarfile, tempfile, threading, time, zipfile
import email.message, email.utils

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
LOCAL_CONF = os.path.join(HERE, 'harness.local.conf')
PREFIX = 'MA_HARNESS_'

# key -> (default, what it is). A default of None means the key is required.
KEYS = {
    'IMAP_HOST':     (None,  'IMAP server of the test mailbox (implicit TLS)'),
    'IMAP_PORT':     ('993', 'IMAP port (implicit TLS)'),
    'SMTP_HOST':     ('',    'SMTP server that delivers to the test mailbox; default: IMAP_HOST'),
    'SMTP_PORT':     ('25',  'SMTP port (plain SMTP, no authentication)'),
    'ADDRESS':       (None,  'address (and IMAP login) of the test mailbox'),
    'PASSWORD_FILE': (None,  'file holding the mailbox password (never printed)'),
    'CA_FILE':       ('',    'PEM CA certificate(s) the server needs, comma-separated; empty for a public CA'),
    'FILLER_DIRS':   ('',    'folders of message files to use as filler, comma-separated; empty: generated mail'),
    'FILLER_SSH':    ('',    'user@host: read FILLER_DIRS there over ssh instead of locally'),
}
CFG = {}

# Filler sources, one per purpose. With FILLER_DIRS they map onto the folders given
# (in turn, when there are fewer than five); without, the mail is generated.
SOURCES = ('a', 'b', 'c', 'd', 'e')


def read_conf(path):
    """KEY=value lines of a harness config file ('#' comments, optional quotes)."""
    out = {}
    with open(path, encoding='utf-8') as f:
        for n, line in enumerate(f, 1):
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            key, sep, value = line.partition('=')
            key, value = key.strip(), value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in '"\'':
                value = value[1:-1]
            if not sep or not key.startswith(PREFIX) or key[len(PREFIX):] not in KEYS:
                sys.stderr.write(f'{path}:{n}: not a harness setting: {line[:60]}\n')
                sys.exit(2)
            out[key[len(PREFIX):]] = value
    return out


def load_config(environ=None, conf=None):
    """Fill CFG from MA_HARNESS_* variables, then the local config file, then defaults.
    Exits 2, naming every missing or unusable key, before anything is contacted."""
    environ = os.environ if environ is None else environ
    conf = environ.get(PREFIX + 'CONFIG') or (LOCAL_CONF if conf is None else conf)
    from_file = read_conf(conf) if os.path.isfile(conf) else {}
    missing, bad = [], []
    for key, (default, _) in KEYS.items():
        value = environ.get(PREFIX + key) or from_file.get(key) or default
        if value is None:
            missing.append(key)
        CFG[key] = value or ''
    if missing:
        sys.stderr.write(
            'live harness: not configured. Missing: '
            + ', '.join(PREFIX + k for k in missing) + '\n'
            + ''.join(f'  {PREFIX}{k}: {KEYS[k][1]}\n' for k in missing)
            + f'Set them in the environment or in {conf}\n'
            + f'(copy {os.path.join(HERE, "harness.example.conf")}). Nothing was contacted.\n')
        sys.exit(2)
    CFG['SMTP_HOST'] = CFG['SMTP_HOST'] or CFG['IMAP_HOST']
    for key in ('IMAP_PORT', 'SMTP_PORT'):
        if not CFG[key].isdigit():
            bad.append(f'{PREFIX}{key}: not a port number: {CFG[key]!r}')
        else:
            CFG[key] = int(CFG[key])
    CFG['PASSWORD_FILE'] = os.path.abspath(os.path.expanduser(CFG['PASSWORD_FILE']))
    if not os.path.isfile(CFG['PASSWORD_FILE']):
        bad.append(f'{PREFIX}PASSWORD_FILE: no such file: {CFG["PASSWORD_FILE"]}')
    CFG['CA_FILE'] = [os.path.abspath(os.path.expanduser(p.strip())) for p in CFG['CA_FILE'].split(',') if p.strip()]
    bad += [f'{PREFIX}CA_FILE: no such file: {p}' for p in CFG['CA_FILE'] if not os.path.isfile(p)]
    CFG['FILLER_DIRS'] = [p.strip() for p in CFG['FILLER_DIRS'].split(',') if p.strip()]
    if CFG['FILLER_SSH'] and not CFG['FILLER_DIRS']:
        bad.append(f'{PREFIX}FILLER_SSH is set but {PREFIX}FILLER_DIRS is empty')
    if not CFG['FILLER_SSH']:
        CFG['FILLER_DIRS'] = [os.path.abspath(os.path.expanduser(p)) for p in CFG['FILLER_DIRS']]
        bad += [f'{PREFIX}FILLER_DIRS: no such folder: {p}' for p in CFG['FILLER_DIRS'] if not os.path.isdir(p)]
    if bad:
        sys.stderr.write('live harness: configuration not usable:\n' + ''.join(f'  {b}\n' for b in bad)
                         + 'Nothing was contacted.\n')
        sys.exit(2)
    return CFG

results = []


def check(group, name, ok, detail=''):
    results.append({'group': group, 'check': name, 'ok': bool(ok), 'detail': str(detail)})
    print(f"  [{'PASS' if ok else 'FAIL'}] {group}: {name}" + (f"  ({detail})" if detail and not ok else ''), flush=True)


def sh(argv, **k):
    return subprocess.run(argv, capture_output=True, text=True, **k)


# ---------------------------------------------------------------- filler

FILLER_WORDS = ('archive folder message weekly report invoice meeting notes update schedule ticket order '
                'reminder draft summary release build review answer question offer notice').split()


def generated_message(source, i):
    """One made-up message, different for every (source, i): plain text, every third
    with an HTML alternative, every fifth with non-ASCII text, every seventh with an
    attachment."""
    seed = SOURCES.index(source) * 100003 + i
    w = [FILLER_WORDS[(seed * 7 + k * 13) % len(FILLER_WORDS)] for k in range(40)]
    m = email.message.EmailMessage()
    m['From'] = f'Sender {seed % 17} <sender{seed % 17}@filler.invalid>'
    m['To'] = 'recipient@filler.invalid'
    m['Date'] = email.utils.formatdate(1700000000 + seed * 61)
    m['Subject'] = ' '.join(w[:4]) + (' \u2013 gr\u00fc\u00dfe' if i % 5 == 0 else '')
    m['Message-ID'] = f'<generated-{source}-{i}@filler.invalid>'
    text = '\n'.join(' '.join(w[k:k + 8]) for k in range(0, 40, 8)) + f'\n\nfiller {source}/{i}\n'
    if i % 5 == 0:
        text += 'na\u00efve caf\u00e9 \u2014 \u65e5\u672c\u8a9e\n'
    m.set_content(text)
    if i % 3 == 0:
        m.add_alternative('<html><body><p>' + '</p><p>'.join(text.split('\n')) + '</p></body></html>', subtype='html')
    if i % 7 == 0:
        m.add_attachment(('filler %d\n' % seed).encode() * 50, maintype='application', subtype='octet-stream',
                         filename=f'filler-{seed}.bin')
    return m.as_bytes()


def fetch_filler(dest, source, count, skip=0):
    """`count` filler messages (after `skip`) as a list of file paths: generated, or
    read from the folder FILLER_DIRS gives this source (locally or over ssh)."""
    dirs = CFG['FILLER_DIRS']
    if not dirs:
        os.makedirs(dest, exist_ok=True)
        files = []
        for i in range(skip, skip + count):
            files.append(os.path.join(dest, f'{i:06d}'))
            with open(files[-1], 'wb') as f:
                f.write(generated_message(source, i))
        return files
    path = dirs[SOURCES.index(source) % len(dirs)]
    if not CFG['FILLER_SSH']:
        names = sorted(n for n in os.listdir(path) if os.path.isfile(os.path.join(path, n)))
        if names:
            skip %= len(names)
        return [os.path.join(path, n) for n in (names[skip:] + names[:skip])[:count]]
    remote = (f"cd {shquote(path)} && ls | sort | tail -n +{skip + 1} | head -n {count} | "
              f"tar -cf - -T -")
    os.makedirs(dest, exist_ok=True)
    p = subprocess.run(['ssh', '-o', 'BatchMode=yes', CFG['FILLER_SSH'], remote], capture_output=True)
    if p.returncode != 0:
        raise RuntimeError(f'filler {source}: {p.stderr.decode()[:200]}')
    with tarfile.open(fileobj=io.BytesIO(p.stdout)) as t:
        t.extractall(dest, filter='data')
    return sorted(os.path.join(dest, f) for f in os.listdir(dest))


def shquote(s):
    return "'" + s.replace("'", "'\\''") + "'"


def harness_message(raw, run, tag, i, message_id=True):
    """A junk message with a harness Subject (searchable token) and Message-ID. Returns
    bytes, or None when the junk is too malformed to re-serialise."""
    try:
        m = email.message_from_bytes(raw, policy=email.policy.compat32)
        for h in ('Subject', 'Message-ID', 'Message-Id'):
            del m[h]
        m['Subject'] = f'{token(run, tag)} filler {i}'
        if message_id:
            m['Message-ID'] = f'<fixture-{tag}-{i}-{run}@harness.invalid>'
        m['X-MA-Fixture'] = f'{run}/{tag}/{i}'
        out = m.as_bytes()
        email.message_from_bytes(out)
        return out.replace(b'\r\n', b'\n')
    except Exception:
        return None


def token(run, tag):
    return re.sub(r'[^a-z0-9]', '', f'maharness{run}{tag}'.lower())


def filler_set(work, run, tag, source, n, skip=0, message_id=True):
    files = fetch_filler(os.path.join(work, 'junk', tag), source, n + 15, skip)
    msgs = []
    for f in files:
        b = harness_message(open(f, 'rb').read(), run, tag, len(msgs), message_id)
        if b:
            msgs.append(b)
        if len(msgs) == n:
            break
    if len(msgs) < n:
        raise RuntimeError(f'{tag}: only {len(msgs)} usable filler messages from source {source}, need {n}')
    return msgs


# ---------------------------------------------------------------- fixtures

def write_maildir(root, msgs, prefix='m'):
    for d in ('cur', 'new', 'tmp'):
        os.makedirs(os.path.join(root, d), exist_ok=True)
    for i, b in enumerate(msgs):
        sub, info = ('cur', ':2,') if i % 2 else ('new', '')
        with open(os.path.join(root, sub, f'{1700000000 + i}.{prefix}{i}.harness{info}'), 'wb') as f:
            f.write(b)


def write_mbox(path, msgs):
    with open(path, 'wb') as f:
        for i, b in enumerate(msgs):
            f.write(b'From harness@harness.invalid Sat Nov 14 12:00:00 2026\n')
            f.write(re.sub(rb'(?m)^(>*From )', rb'>\1', b))
            f.write(b'\n' if b.endswith(b'\n') else b'\n\n')


def build_fixtures(work, run, n, extra):
    fx = os.path.join(work, 'fixtures')
    os.makedirs(fx)
    plan = []          # (fixture name in /fixtures, name in inbox, label, expected type or None, n, token tag)

    sets = {}
    sets['maildir'] = filler_set(work, run, 'maildir', 'a', n, 0)
    write_maildir(os.path.join(fx, 'maildir-export'), sets['maildir'])
    plan.append(('maildir-export', 'maildir-export', 'Maildir', 'maildir', n, 'maildir'))

    sets['maildirpp'] = filler_set(work, run, 'maildirpp', 'a', n, 100)
    root = os.path.join(fx, 'maildirpp-export')
    half = n // 2
    write_maildir(root, sets['maildirpp'][:half])
    write_maildir(os.path.join(root, '.Offers.2026'), sets['maildirpp'][half:], 's')
    open(os.path.join(root, '.Offers.2026', 'maildirfolder'), 'w').close()
    plan.append(('maildirpp-export', 'maildirpp-export', 'Maildir++', 'maildir', n, 'maildirpp'))

    # mbox: n with Message-ID plus 3 WITHOUT one (content-hash dedupe path)
    sets['mbox'] = filler_set(work, run, 'mbox', 'b', n, 0)
    sets['noid'] = filler_set(work, run, 'noid', 'c', 3, 0, message_id=False)
    write_mbox(os.path.join(fx, 'takeout.dat'), sets['mbox'] + sets['noid'])     # wrong extension on purpose
    plan.append(('takeout.dat', 'takeout.dat', 'mbox (named .dat)', 'mbox', n + 3, 'mbox'))

    sets['eml1'] = filler_set(work, run, 'eml1', 'b', 1, 60)
    with open(os.path.join(fx, 'single.eml'), 'wb') as f:
        f.write(sets['eml1'][0])
    plan.append(('single.eml', 'single.eml', 'single .eml', 'eml', 1, 'eml1'))

    sets['emldir'] = filler_set(work, run, 'emldir', 'b', min(n, 25), 62)
    d = os.path.join(fx, 'loose-eml')
    os.makedirs(os.path.join(d, 'sub'))
    for i, b in enumerate(sets['emldir']):
        with open(os.path.join(d, 'sub' if i % 5 == 0 else '', f'mail{i}.eml'), 'wb') as f:
            f.write(b)
    plan.append(('loose-eml', 'loose-eml', 'directory of .eml', 'directory', len(sets['emldir']), 'emldir'))

    sets['emlx'] = filler_set(work, run, 'emlx', 'c', 10, 5)
    d = os.path.join(fx, 'apple-mail')
    os.makedirs(d)
    for i, b in enumerate(sets['emlx']):
        with open(os.path.join(d, f'{1000 + i}.emlx'), 'wb') as f:
            f.write(str(len(b)).encode() + b'\n' + b +
                    b'<?xml version="1.0" encoding="UTF-8"?>\n<plist version="1.0"><dict>'
                    b'<key>flags</key><integer>8590195713</integer></dict></plist>\n')
    plan.append(('apple-mail', 'apple-mail', 'directory of Apple .emlx', None, 10, 'emlx'))

    sets['zip'] = filler_set(work, run, 'zip', 'a', n, 200)
    with zipfile.ZipFile(os.path.join(fx, 'export.zip'), 'w', zipfile.ZIP_DEFLATED) as z:
        for i, b in enumerate(sets['zip']):
            z.writestr(f'export/{"a" if i % 2 else "b"}/msg{i}.eml', b)
    plan.append(('export.zip', 'export.zip', 'zip of .eml', None, n, 'zip'))

    sets['tar'] = filler_set(work, run, 'tar', 'a', n, 300)
    tmp = os.path.join(work, 'tar-src', 'Takeout', 'Mail')
    write_maildir(tmp, sets['tar'])
    with tarfile.open(os.path.join(fx, 'takeout.tgz'), 'w:gz') as t:
        t.add(os.path.join(work, 'tar-src', 'Takeout'), arcname='Takeout')
    plan.append(('takeout.tgz', 'takeout.tgz', 'tar.gz with a Maildir', None, n, 'tar'))

    sets['gz'] = filler_set(work, run, 'gz', 'd', 5, 0)
    raw = io.BytesIO()
    for b in sets['gz']:
        raw.write(b'From harness@harness.invalid Sat Nov 14 12:00:00 2026\n' + re.sub(rb'(?m)^(>*From )', rb'>\1', b) + b'\n')
    with gzip.open(os.path.join(fx, 'old-mail.mbox.gz'), 'wb') as g:
        g.write(raw.getvalue())
    plan.append(('old-mail.mbox.gz', 'old-mail.mbox.gz', 'gzip-compressed mbox', None, 5, 'gz'))

    # Fixtures only Outlook can make (no packaged Linux tool writes PST or MSG).
    skipped = []
    pst = os.path.join(extra, 'fixture.pst') if extra else ''
    if pst and os.path.isfile(pst):
        shutil.copy(pst, os.path.join(fx, 'outlook-export.pst'))
        plan.append(('outlook-export.pst', 'outlook-export.pst', 'Outlook PST', 'pst', None, None))
    else:
        skipped.append('Outlook PST (needs a junk-folder export made in Outlook: --extra DIR/fixture.pst)')
    msgs = sorted(f for f in os.listdir(os.path.join(extra, 'msg'))) if extra and os.path.isdir(os.path.join(extra, 'msg')) else []
    if msgs:
        shutil.copytree(os.path.join(extra, 'msg'), os.path.join(fx, 'outlook-msg'))
        plan.append(('outlook-msg', 'outlook-msg', 'directory of Outlook .msg', None, len(msgs), None))
    else:
        skipped.append('Outlook .msg (needs junk messages saved from Outlook: --extra DIR/msg/*.msg)')

    # Dedupe fixtures.
    shutil.copy(os.path.join(fx, 'takeout.dat'), os.path.join(fx, 'takeout-copy.dat'))
    again = []
    for k in ('maildir', 'maildirpp', 'mbox', 'emldir', 'emlx', 'zip', 'tar', 'gz'):
        again += sets[k][:3]
    again += sets['noid']
    write_mbox(os.path.join(fx, 'everything-again.mbox'), again)
    return plan, skipped, len(again), fx, sets


# ---------------------------------------------------------------- container

class Box:
    def __init__(self, name):
        self.name = name

    def run(self, *cmd, check=True):
        p = sh(['podman', 'exec', self.name, *cmd])
        if check and p.returncode != 0:
            raise RuntimeError(f'{" ".join(cmd)[:80]} failed: {(p.stdout + p.stderr)[-600:]}')
        return p

    def harness(self, *args):
        p = self.run('python3', '/harness/inner.py', *args)
        for line in reversed(p.stdout.splitlines()):
            if line.startswith('JSON:'):
                return json.loads(line[5:])
        raise RuntimeError(f'inner {args}: no result. {(p.stdout + p.stderr)[-600:]}')


SYSTEMCTL_STUB = r'''#!/bin/sh
# Container stand-in for systemctl. The package scripts and the cron wrapper ask systemd
# for the service environment; nothing else is running here, so the service is "inactive".
case "$1" in
    show)
        case " $* " in
            *" Environment "*) echo "MAIL_ARCHIVER_DATA=/data MAIL_ARCHIVER_AUTH=pam MAIL_ARCHIVER_SECRET_FILE=/opt/mail-archiver/.secret_key" ;;
            *" MainPID "*) echo 0 ;;
        esac ;;
    is-active|is-enabled) exit 3 ;;
esac
exit 0
'''


def build_deb(work):
    src = os.path.join(work, 'src')
    os.makedirs(src)
    commit = sh(['git', '-C', REPO, 'rev-parse', 'HEAD']).stdout.strip()
    dirty = bool(sh(['git', '-C', REPO, 'status', '--porcelain']).stdout.strip())
    files = sh(['git', '-C', REPO, 'ls-files', '-co', '--exclude-standard']).stdout.split('\n')
    for f in filter(None, files):
        s, d = os.path.join(REPO, f), os.path.join(src, f)
        if os.path.isfile(s):
            os.makedirs(os.path.dirname(d), exist_ok=True)
            shutil.copy2(s, d)
    # 1.3.3: the package is built from a source tarball, as a release is
    # (packaging/build.sh deb). The harness also tests uncommitted work, so
    # it makes its own tarball of the copied tree, not a `git archive`.
    version = open(os.path.join(src, 'VERSION'), encoding='utf-8').read().strip()
    stamp = commit + ('-dirty' if dirty else '')
    with open(os.path.join(src, 'BUILD_INFO'), 'w', encoding='utf-8') as f:
        f.write(f'commit={stamp}\nbuilt={time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}\n')
    out = os.path.join(work, 'pkg')
    os.makedirs(out)
    tarball = f'mail-archiver-{version}.tar.gz'
    p = sh(['tar', '-czf', os.path.join(out, tarball), '-C', src,
            '--transform', f's,^\\.,mail-archiver-{version},', '.'])
    if p.returncode != 0:
        raise RuntimeError('source tarball failed: ' + (p.stdout + p.stderr)[-400:])
    script = ('set -e; apt-get -qq update >/dev/null 2>&1; DEBIAN_FRONTEND=noninteractive apt-get -qq install -y '
              '--no-install-recommends dpkg-dev make xz-utils >/dev/null 2>&1; umask 022; '
              f'bash /src/packaging/build.sh deb /out/{tarball} /out >/tmp/b.log 2>&1 || '
              '{ tail -20 /tmp/b.log; exit 1; }')
    p = sh(['podman', 'run', '--rm', '-v', f'{src}:/src:ro', '-v', f'{out}:/out',
            'docker.io/library/debian:12', 'bash', '-c', script])
    if p.returncode != 0:
        raise RuntimeError('package build failed: ' + (p.stdout + p.stderr)[-800:])
    debs = [f for f in os.listdir(out) if f.endswith('_all.deb')]
    return os.path.join(out, sorted(debs)[-1]), stamp


def start_container(work, deb, fx, run, pw_file):
    name = f'ma-live-harness-{run}'
    with open(os.path.join(work, 'systemctl'), 'w') as f:
        f.write(SYSTEMCTL_STUB)
    os.chmod(os.path.join(work, 'systemctl'), 0o755)
    p = sh(['podman', 'run', '-d', '--name', name,
            '-e', f'HARNESS_ADDR={CFG["ADDRESS"]}', '-e', f'HARNESS_RUN={run}',
            '-e', f'HARNESS_IMAP={CFG["IMAP_HOST"]}', '-e', f'HARNESS_IMAP_PORT={CFG["IMAP_PORT"]}',
            '-v', f'{fx}:/fixtures:ro', '-v', f'{pw_file}:/secret/pw:ro', '-v', f'{deb}:/deb/ma.deb:ro',
            '-v', f'{HERE}/inner.py:/harness/inner.py:ro', '-v', f'{work}/systemctl:/usr/bin/systemctl:ro',
            # a private CA: the server's certificate must verify inside the fresh container
            *[x for i, c in enumerate(CFG['CA_FILE'])
              for x in ('-v', f'{c}:/usr/local/share/ca-certificates/ma-harness-ca-{i}.crt:ro')],
            'docker.io/library/debian:13', 'sleep', 'infinity'])
    if p.returncode != 0:
        raise RuntimeError('container start: ' + p.stderr[-400:])
    box = Box(name)
    box.run('bash', '-c',
            'set -e; apt-get -qq update >/dev/null 2>&1; cp /deb/ma.deb /root/ma.deb; '
            'DEBIAN_FRONTEND=noninteractive apt-get -qq install -y /root/ma.deb zstd xz-utils >/tmp/apt.log 2>&1 '
            '|| { tail -15 /tmp/apt.log; exit 1; }; '
            'update-ca-certificates >/dev/null 2>&1; '
            'useradd -m -s /bin/bash harness; usermod -aG mail-archiver harness; mkdir -p /data; '
            'install -d -o mail-archiver -g mail-archiver /run/mail-archiver /run/mail-archiver/sync-jobs')
    return box


# ---------------------------------------------------------------- live mail

class Sender(threading.Thread):
    """Delivers one filler message to the test mailbox through the MTA every `interval` s."""

    def __init__(self, msgs, run, interval):
        super().__init__(daemon=True)
        self.msgs, self.run_id, self.interval = msgs, run, interval
        self.sent, self.errors, self.plain = [], [], 0
        self.stop_flag = threading.Event()

    def one(self, seq):
        raw = self.msgs[seq % len(self.msgs)]
        m = email.message_from_bytes(raw, policy=email.policy.compat32)
        for h in ('Subject', 'Message-ID', 'X-MA-Fixture', 'To'):
            del m[h]
        m['Subject'] = f'{token(self.run_id, "live")} delivery {seq}'
        m['Message-ID'] = f'<live-{seq}-{self.run_id}@harness.invalid>'
        m['To'] = CFG['ADDRESS']
        m['X-MA-Harness'] = f'{self.run_id}/{seq}'
        try:
            self.smtp(m.as_bytes())
        except smtplib.SMTPException as e:
            # the MTA's content filter refused this junk: deliver a plain message instead
            self.plain += 1
            body = (f'From: {CFG["ADDRESS"]}\r\nTo: {CFG["ADDRESS"]}\r\nSubject: {token(self.run_id, "live")} delivery {seq}\r\n'
                    f'Message-ID: <live-{seq}-{self.run_id}@harness.invalid>\r\nX-MA-Harness: {self.run_id}/{seq}\r\n'
                    f'Date: {email.utils.formatdate()}\r\n\r\nplain harness delivery {seq} ({type(e).__name__})\r\n')
            self.smtp(body.encode())

    def smtp(self, data):
        with smtplib.SMTP(CFG['SMTP_HOST'], CFG['SMTP_PORT'], timeout=60) as s:
            s.sendmail(CFG['ADDRESS'], [CFG['ADDRESS']], data)

    def run(self):
        seq = 0
        while not self.stop_flag.is_set():
            try:
                self.one(seq)
                self.sent.append(str(seq))
            except Exception as e:
                self.errors.append(f'{seq}: {type(e).__name__}: {str(e)[:120]}')
            seq += 1
            self.stop_flag.wait(self.interval)


# ---------------------------------------------------------------- the run

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--deb')
    ap.add_argument('--interval', type=float, default=2.0)
    ap.add_argument('--per-format', type=int, default=30)
    ap.add_argument('--extra')
    ap.add_argument('--keep-container', action='store_true')
    ap.add_argument('--report-dir', default=os.path.expanduser('~/.cache/mail-archiver-live-harness'))
    a = ap.parse_args()
    load_config()

    run = time.strftime('%m%d%H%M%S')
    work = tempfile.mkdtemp(prefix=f'ma-live-harness-{run}-', dir=os.environ.get('TMPDIR') or None)
    os.chmod(work, 0o700)
    pw_file = CFG['PASSWORD_FILE']
    box = None
    sender = None
    report = {'run': run, 'address': CFG['ADDRESS'], 'mta': CFG['SMTP_HOST'], 'imap': CFG['IMAP_HOST'], 'started': time.strftime('%Y-%m-%d %H:%M:%S %Z')}
    try:
        print(f'run {run}: building fixtures from filler mail', flush=True)
        plan, skipped, again_n, fx, sets = build_fixtures(work, run, a.per_format, a.extra)
        live = filler_set(work, run, 'live', 'e', 40, 800)
        if a.deb:
            deb, commit = os.path.abspath(a.deb), 'given package'
        else:
            print('building the package from this checkout (debian:12 container)', flush=True)
            deb, commit = build_deb(work)
        print('starting the container and installing the package (debian:13)', flush=True)
        box = start_container(work, deb, fx, run, pw_file)
        s0 = box.harness('setup')
        report.update(version=s0['version'], build=s0['build'], source=commit, isync=s0['mbsync'],
                      server_before=s0['server']['folders'])
        print(f"package {s0['version']} ({s0['build']}), {s0['mbsync']}; server before: {s0['server']['folders']}", flush=True)
        check('setup', 'import folders created without refusals', not s0['refusals'], s0['refusals'])
        base = s0['server']['folders']

        sender = Sender(live, run, a.interval)
        sender.start()
        syncs = []

        def sync(label):
            r = box.harness('sync')
            syncs.append({'label': label, 'state': r['state'], 'seconds': r['seconds']})
            check('live sync', f'sync {label} ends ok', r['state'] == 'ok' and not r['far_side_error'], r['error'])
            return r

        r = sync('#0 before any import')
        check('live sync', 'baseline sync pulled every server folder',
              all(r['local']['boxes'].get(k, -1) >= v for k, v in base.items()), r['local']['boxes'])
        imported_expected = 0
        formats = []
        for idx, (fixture, name, label, etype, n, tag) in enumerate(plan):
            before = box.harness('state')
            concurrent = None
            if idx == 0:        # a sync and an import at the same moment
                holder = {}
                t = threading.Thread(target=lambda: holder.update(r=box.harness('sync')))
                t.start()
                imp = box.harness('import', fixture, name)
                t.join()
                concurrent = holder.get('r')
                check('concurrency', 'a sync running during an import ends ok',
                      bool(concurrent) and concurrent['state'] == 'ok', concurrent and concurrent['error'])
            else:
                imp = box.harness('import', fixture, name)
            rows = imp['new_rows']
            row = rows[-1] if rows else {}
            got = sum(r.get('imported') or 0 for r in rows)
            dup = sum(r.get('duplicates') or 0 for r in rows)
            failed = sum(r.get('failed') or 0 for r in rows)
            grew = imp['local']['imported_total'] - before['local']['imported_total']
            g = f'format {label}'
            check(g, 'import pass ran and recorded a ledger row', imp['ran'] and bool(rows), imp.get('log_tail'))
            check(g, 'ledger status is done', row.get('status') == 'done', row)
            if etype:
                check(g, f'detected from content as {etype}', row.get('detected_type') == etype, row.get('detected_type'))
            if n is not None:
                check(g, f'imported == {n}', got == n, f'imported {got}, duplicates {dup}, failed {failed}')
                check(g, f'archive grew by {n} under PST/ or Imported/', grew == n, grew)
                imported_expected += n
            else:
                check(g, 'imported > 0', got > 0, row)
                imported_expected += got
            check(g, '0 duplicates, 0 failed', dup == 0 and failed == 0, f'dup {dup} failed {failed}')
            check(g, 'source left the inbox and is kept in done/', not imp['inbox_left'] and any(name in d for d in imp['done']),
                  f"inbox {imp['inbox_left']} done {imp['done']} failed {imp['failed']}")
            loc = imp['local']
            check(g, 'imported mail is read, in cur/, 0600 and owned by the mailbox owner',
                  not (loc['imported_unread'] or loc['imported_in_new'] or loc['imported_wrong_mode'] or loc['imported_wrong_owner']),
                  {k: loc[k] for k in loc if k.startswith('imported_')})
            if tag:
                want = n if tag != 'mbox' else n - 3      # the 3 without a Message-ID carry their own token
                hits = box.harness('search', token(run, tag))['hits']
                check(g, f'searchable ({want} hits for the fixture token)', hits == want, hits)
            r = sync(f'after {label}')
            check(g, 'the sync left the imported mail alone',
                  r['local']['imported_total'] == loc['imported_total'], r['local']['imported_total'])
            formats.append({'format': label, 'type': row.get('detected_type'), 'prefix': row.get('target_prefix'),
                            'expected': n, 'imported': got, 'duplicates': dup, 'failed': failed,
                            'import_seconds': imp['seconds']})

        # ---- dedupe
        before = box.harness('state')
        imp = box.harness('import', 'takeout-copy.dat', 'takeout-copy.dat')
        row = imp['new_rows'][-1] if imp['new_rows'] else {}
        check('dedupe', 'the same file again is skipped by its sha256', row.get('status') == 'skipped' and
              imp['local']['imported_total'] == before['local']['imported_total'], row)
        imp = box.harness('import', 'everything-again.mbox', 'everything-again.mbox')
        row = imp['new_rows'][-1] if imp['new_rows'] else {}
        check('dedupe', f'the same {again_n} messages in another format are all duplicates (3 without a Message-ID)',
              row.get('imported') == 0 and row.get('duplicates') == again_n, row)
        sync('after the dedupe imports')

        # ---- stop the mail, let it land, final sync
        sender.stop_flag.set()
        sender.join(90)
        sent = sender.sent
        check('live mail', f'the MTA accepted every delivery ({len(sent)} sent)', not sender.errors and len(sent) > 0, sender.errors[:3])
        deadline = time.time() + 180
        st = box.harness('state')
        while st['server']['harness_in_inbox'] < len(sent) and time.time() < deadline:
            time.sleep(5)
            st = box.harness('state')
        check('live mail', 'every delivery reached the mailbox', st['server']['harness_in_inbox'] == len(sent),
              f"{st['server']['harness_in_inbox']} of {len(sent)}")
        r = sync('final')
        st = box.harness('state')
        seqs = st['local']['harness_seqs']
        check('live mail', 'every delivery is in the archive exactly once',
              sorted(seqs, key=int) == sorted(sent, key=int) and all(v == 1 for v in seqs.values()),
              f'archive has {len(seqs)} of {len(sent)}; repeated: {[k for k, v in seqs.items() if v != 1][:5]}')
        srv = st['server']['folders']
        boxes = st['local']['boxes']
        check('no leak', 'no PST or Imported folder on the server',
              not any(k.split('/')[0].split('.')[0] in ('PST', 'Imported') for k in srv), list(srv))
        check('no leak', 'the server has the same folders as before', set(srv) == set(base), f'{sorted(base)} -> {sorted(srv)}')
        check('no leak', 'INBOX grew by exactly the MTA deliveries', srv.get('INBOX') == base.get('INBOX', 0) + len(sent),
              f"{base.get('INBOX')} + {len(sent)} != {srv.get('INBOX')}")
        check('no leak', 'no other server folder changed', all(srv[k] == base[k] for k in base if k != 'INBOX'),
              {k: (base[k], srv.get(k)) for k in base if k != 'INBOX' and srv.get(k) != base[k]})
        check('the books', 'per server folder, local == server', all(boxes.get(k) == v for k, v in srv.items()),
              {k: (v, boxes.get(k)) for k, v in srv.items() if boxes.get(k) != v})
        check('the books', f'imported total == {imported_expected}', st['local']['imported_total'] == imported_expected,
              st['local']['imported_total'])
        check('the books', 'local total == server total + imported total',
              sum(boxes.values()) == sum(srv.values()) + imported_expected, f'{sum(boxes.values())} vs {sum(srv.values())} + {imported_expected}')
        rec = box.harness('reconcile')
        check('the books', 'Reconcile reports every folder in sync', rec['rc'] == 0 and rec['rows'] == len(srv) and
              rec['in_sync'] == rec['rows'], rec)
        check('the books', 'no retention deletions', not [e for e in st['ledger']['events'] if e.get('action') == 'deleted'],
              st['ledger']['events'][:3])
        check('the books', 'no failed ledger rows', not [x for x in st['ledger']['rows'] if x.get('status') in ('failed', 'partial')],
              [x for x in st['ledger']['rows'] if x.get('status') in ('failed', 'partial')][:3])
        cl = box.harness('cleanup-server')
        check('cleanup', 'this run\'s deliveries were removed from the test mailbox; it is back to its starting counts',
              cl['removed'] == len(sent) and cl['server']['folders'] == base, cl)
        report.update(formats=formats, syncs=syncs, sent=len(sent), sent_as_plain_fallback=sender.plain,
                      not_tested=skipped, server_after=srv, local_boxes=boxes,
                      ledger_rows=len(st['ledger']['rows']))
    finally:
        if sender:
            sender.stop_flag.set()
        if box and not a.keep_container:
            sh(['podman', 'rm', '-f', box.name])
        shutil.rmtree(work, ignore_errors=True)

    failed = [r for r in results if not r['ok']]
    report.update(results=results, passed=len(results) - len(failed), failed=len(failed),
                  finished=time.strftime('%Y-%m-%d %H:%M:%S %Z'))
    os.makedirs(a.report_dir, exist_ok=True)
    path = os.path.join(a.report_dir, f'report-{run}.json')
    with open(path, 'w') as f:
        json.dump(report, f, indent=2)
    print(f"\n{len(results) - len(failed)} passed, {len(failed)} failed; not tested: {report.get('not_tested')}")
    print('report:', path)
    for r in failed:
        print(f"  FAIL {r['group']}: {r['check']}  ({r['detail'][:300]})")
    sys.exit(1 if failed else 0)


if __name__ == '__main__':
    main()
