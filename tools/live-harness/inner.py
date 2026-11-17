#!/usr/bin/env python3
"""Runs INSIDE the harness container, as root, with the mail-archiver package installed
in PAM mode. Each command prints one line `JSON:{...}` for the driver. Never prints the
password or any mail content (counts, names and harness markers only)."""
import contextlib, imaplib, io, json, os, pwd, re, shutil, sqlite3, ssl, subprocess, sys, time

os.environ.setdefault('MAIL_ARCHIVER_DATA', '/data')
os.environ['MAIL_ARCHIVER_AUTH'] = 'pam'
os.environ.setdefault('MAIL_ARCHIVER_SECRET_FILE', '/opt/mail-archiver/.secret_key')
sys.path.insert(0, '/opt/mail-archiver')
os.chdir('/opt/mail-archiver')
import app                      # noqa: E402
import bulk_sync as bulk        # noqa: E402

U = 'harness'
E = os.environ['HARNESS_ADDR']
HOST = os.environ['HARNESS_IMAP']
PORT = int(os.environ.get('HARNESS_IMAP_PORT', '993'))
RUN = os.environ['HARNESS_RUN']
SLUG = bulk.safe_account_name(E)
DATA = app.CONFIG['data_dir']
M = os.path.join(DATA, U, SLUG)
IMPORT = os.path.join(DATA, U, '_import', SLUG)
PW = open('/secret/pw').read().strip()
IMPORT_TOPS = ('PST', 'Imported')


def out(obj):
    print('JSON:' + json.dumps(obj), flush=True)


def quiet(fn, *a, **k):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        r = fn(*a, **k)
    return r, buf.getvalue()


def imap():
    c = imaplib.IMAP4_SSL(HOST, PORT, ssl_context=ssl.create_default_context())
    c.login(E, PW)
    return c


def server_state():
    c = imap()
    counts = {}
    for l in c.list()[1]:
        s = l.decode()
        name = s.rsplit(' "', 1)[-1].rstrip('"') if s.endswith('"') else s.split()[-1]
        typ, d = c.status('"%s"' % name, '(MESSAGES)')
        if typ == 'OK':
            counts[name] = int(d[0].decode().rsplit(' ', 1)[-1].rstrip(')'))
    c.select('INBOX', readonly=True)
    typ, d = c.search(None, 'HEADER', 'X-MA-Harness', RUN)
    marked = len(d[0].split()) if typ == 'OK' and d and d[0] else 0
    c.logout()
    return {'folders': dict(sorted(counts.items())), 'harness_in_inbox': marked}


def local_boxes():
    boxes = {}
    for d, ds, fs in os.walk(M):
        if os.path.basename(d) == 'cur':
            box = os.path.relpath(os.path.dirname(d), M)
            boxes[box] = len(fs) + len(os.listdir(os.path.join(os.path.dirname(d), 'new')))
    return dict(sorted(boxes.items()))


def is_import_box(box):
    return box.split('/')[0] in IMPORT_TOPS


def local_state():
    boxes = local_boxes()
    uid = pwd.getpwnam(U).pw_uid
    unread = wrong_mode = wrong_owner = in_new = 0
    for d, ds, fs in os.walk(M):
        rel = os.path.relpath(d, M)
        if not is_import_box(rel) or os.path.basename(d) not in ('cur', 'new'):
            continue
        for f in fs:
            p = os.path.join(d, f)
            st = os.lstat(p)
            in_new += os.path.basename(d) == 'new'
            unread += not (':2,' in f and 'S' in f.split(':2,')[1])
            wrong_mode += (st.st_mode & 0o777) != 0o600
            wrong_owner += st.st_uid != uid
    seqs = {}
    marker = ('X-MA-Harness: %s/' % RUN).encode()
    for box in boxes:
        if is_import_box(box):
            continue
        for sub in ('cur', 'new'):
            dd = os.path.join(M, box, sub)
            for f in os.listdir(dd):
                with open(os.path.join(dd, f), 'rb') as fh:
                    head = fh.read(16384)
                i = head.find(marker)
                if i >= 0:
                    seq = head[i + len(marker):i + len(marker) + 12].split(b'\r')[0].split(b'\n')[0].decode()
                    seqs[seq] = seqs.get(seq, 0) + 1
    return {'boxes': boxes,
            'imported_total': sum(n for b, n in boxes.items() if is_import_box(b)),
            'synced_total': sum(n for b, n in boxes.items() if not is_import_box(b)),
            'imported_unread': unread, 'imported_in_new': in_new,
            'imported_wrong_mode': wrong_mode, 'imported_wrong_owner': wrong_owner,
            'harness_seqs': seqs}


def ledger():
    db = os.path.join(DATA, U, '.search_index.db')
    if not os.path.exists(db):
        return {'rows': [], 'events': []}
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    try:
        rows = [dict(r) for r in con.execute(
            'select id, original_name, detected_type, target_prefix, status, imported, duplicates, failed, '
            'too_large, error from import_ledger order by id')]
        ev = [dict(r) for r in con.execute('select action, rule, name from import_retention_events order by id')]
    except sqlite3.Error as e:
        rows, ev = [{'error': str(e)}], []
    con.close()
    return {'rows': rows, 'events': ev}


def cmd_setup():
    u = pwd.getpwnam(U)
    app.get_user_config_dir(U)
    app.save_accounts(U, [{'email': E, 'provider': 'custom', 'host': HOST, 'port': PORT, 'tls_mode': 'ssl',
                           'enabled': True, 'sync_interval': 'manual', 'auth_type': 'app_password'}])
    app.save_credential(U, E, PW)
    os.makedirs(M, exist_ok=True)
    subprocess.run(['chown', '-R', '%d:%d' % (u.pw_uid, u.pw_gid), M], check=True)
    os.chmod(M, 0o700)
    app.generate_mbsyncrc(U)
    imp = app.make_importer(U, log=lambda m: None)
    refusals = imp.ensure_dirs()
    out({'refusals': refusals, 'server': server_state(),
         'mbsync': subprocess.run(['mbsync', '--version'], capture_output=True, text=True).stdout.split('\n')[0],
         'version': open('/opt/mail-archiver/VERSION').read().strip(),
         'build': open('/opt/mail-archiver/BUILD_INFO').read().split('\n')[0]})


def cmd_sync():
    t = time.time()
    _, log = quiet(app.run_sync, U, E)
    st = app.get_sync_status(U).get(E, {})
    rc = open(os.path.join(pwd.getpwnam(U).pw_dir, '.mbsyncrc')).read()
    out({'state': st.get('state'), 'error': (st.get('error') or '')[:300],
         'far_side_error': 'far side box' in json.dumps(st),
         'seconds': round(time.time() - t, 1),
         'patterns': [l for l in rc.splitlines() if l.startswith('Patterns')],
         'local': local_state()})


def cmd_import(fixture, name):
    """Deliver /fixtures/<fixture> into the account inbox as <name> the way a producer
    does (.incoming, then rename), then run one manual import pass."""
    u = pwd.getpwnam(U)
    inbox = os.path.join(IMPORT, 'inbox')
    stage = os.path.join(inbox, '.incoming', name)
    src = os.path.join('/fixtures', fixture)
    if os.path.isdir(src):
        shutil.copytree(src, stage, symlinks=True)
    else:
        shutil.copy(src, stage)
    subprocess.run(['chown', '-R', '%d:%d' % (u.pw_uid, u.pw_gid), stage], check=True)
    os.rename(stage, os.path.join(inbox, name))
    before = ledger()['rows']
    t = time.time()
    summary, log = quiet(app.mail_import_user, U, trigger='manual', stable_wait=1)
    rows = ledger()['rows'][len(before):]
    out({'new_rows': rows, 'seconds': round(time.time() - t, 1),
         'summary_error': (summary or {}).get('error') if isinstance(summary, dict) else None,
         'ran': summary is not None,
         'inbox_left': sorted(n for n in os.listdir(inbox) if not n.startswith('.')),
         'done': sorted(os.listdir(os.path.join(IMPORT, 'done'))),
         'failed': sorted(os.listdir(os.path.join(IMPORT, 'failed'))),
         'local': local_state(), 'log_tail': [l for l in log.splitlines() if 'Traceback' in l or 'Error' in l][-3:]})


def cmd_search(token):
    r, _ = quiet(app.search_maildir, U, token, max_results=2000)
    r = r[0] if isinstance(r, tuple) else r
    out({'token': token, 'hits': len(r)})


def cmd_state():
    out({'server': server_state(), 'local': local_state(), 'ledger': ledger()})


def cmd_reconcile():
    p = subprocess.run(['/usr/libexec/mail-archiver-scheduled-sync', 'bulk-sync', '--reconcile',
                        '--user', U, '--account', E], capture_output=True, text=True)
    lines = [l for l in p.stdout.splitlines() if re.search(r'\d\s+[+-]?\d+\s+\d+\s+\d+\s+\S', l)]
    out({'rc': p.returncode, 'rows': len(lines), 'in_sync': sum('In sync' in l for l in lines),
         'not_in_sync': [re.sub(r'\s+', ' ', l.strip())[:120] for l in lines if 'In sync' not in l]})


def cmd_cleanup_server():
    """Remove only this run's harness messages from the test mailbox."""
    c = imap()
    c.select('INBOX')
    typ, d = c.search(None, 'HEADER', 'X-MA-Harness', RUN)
    ids = d[0].split() if typ == 'OK' and d and d[0] else []
    for i in ids:
        c.store(i, '+FLAGS', '\\Deleted')
    c.expunge()
    c.logout()
    out({'removed': len(ids), 'server': server_state()})


if __name__ == '__main__':
    cmd = sys.argv[1]
    {'setup': cmd_setup, 'sync': cmd_sync, 'state': cmd_state, 'reconcile': cmd_reconcile,
     'cleanup-server': cmd_cleanup_server,
     'import': lambda: cmd_import(sys.argv[2], sys.argv[3]),
     'search': lambda: cmd_search(sys.argv[2])}[cmd]()
