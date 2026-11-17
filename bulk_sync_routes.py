"""Mail Archiver: large-mailbox / rebase sync, wired to the app (1.3.0).

`bulk_sync.py` is the engine. This module gives it the app's pieces:
accounts and per-user config dir, the generated .mbsyncrc, the mbsync
spawn as the mailbox owner, the no-overlapping-syncs guard, the credential
path for the read-only IMAP count, the WebUI routes and the command line.

    app.py bulk-sync                       hourly cron pass over every job
    app.py bulk-sync --list                every job on this host
    app.py bulk-sync --user U --account E --manual     one run now (WebUI)
    app.py bulk-sync --user U --account E --reconcile [--folder F ...]
                                           server count against the archive

It does not import app.py at import time (app.py imports this module);
`_ma()` finds the running app module when a function needs it.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import bulk_sync as bulk
import platform_compat as compat

_APP = None

STATE_GLOB = 'bulk_sync_*.json'
LOG_NAME = 'bulk_sync.log'
RECONCILE_TIMEOUT = 300
_JSON_MARK = 'BULK-SYNC-JSON '
MAX_BUDGET_MB = 10 * 1000 * 1000        # 10 TB a day: far above any mailbox


def _ma():
    """The app module: `__main__` under `python3 app.py ...`, `app` under
    gunicorn and in the tests."""
    main = sys.modules.get('__main__')
    if main is not None and _APP is not None and getattr(main, 'app', None) is _APP:
        return main
    import app
    return app


# ---------------------------------------------------------------------
# Accounts, paths, state
# ---------------------------------------------------------------------

def find_account(username, email):
    for a in _ma().load_accounts(username):
        if a.get('email') == email:
            return a
    return None


def account_maildir(username, email):
    return str(Path(_ma().CONFIG['data_dir']) / username
               / bulk.safe_account_name(email))


def account_profile(acct):
    ma = _ma()
    provider = ma.PROVIDERS.get(acct.get('provider', ''), {})
    return bulk.provider_profile(acct.get('provider'),
                                 acct.get('host') or provider.get('host', ''))


def job_store(username, email):
    ma = _ma()
    return bulk.JobStore(
        str(ma.get_user_config_dir(username)), email,
        writer=lambda filename, data: ma._save_user_json(username, filename, data))


def mbsyncrc_path(username):
    """Where generate_mbsyncrc writes the user's .mbsyncrc."""
    ma = _ma()
    archive_dir = Path(ma.CONFIG['data_dir']) / username
    if ma.CONFIG['auth_mode'] == 'pam':
        info = compat.user_info(username)
        if info is not None:
            return Path(info.home) / '.mbsyncrc'
    return archive_dir / '.mbsyncrc'


def regenerate_config(username):
    """Rewrite the regular .mbsyncrc, so an exclusion that began or ended
    takes effect at the next regular sync."""
    _ma().generate_mbsyncrc(username)


def heal_stuck_exclusions(username, accounts=None):
    """Regenerate the user's .mbsyncrc when an account without an open job
    is still excluded in it (bulk.exclusion_is_stuck: the runner died
    between recording the job complete and regenerating the config).
    Cheap: one file read. Returns the accounts that were stuck. Called by
    the hourly pass and when the page is loaded; never raises."""
    try:
        ma = _ma()
        accounts = ma.load_accounts(username) if accounts is None else accounts
        try:
            rc_text = mbsyncrc_path(username).read_text(encoding='utf-8')
        except OSError:
            return []
        config_dir = str(ma.get_user_config_dir(username))
        stuck = [a['email'] for a in accounts
                 if bulk.exclusion_is_stuck(
                     rc_text, job_store(username, a['email']).load(), a,
                     config_dir)]
        if stuck:
            regenerate_config(username)
            for email in stuck:
                _log(username, email)(
                    'the finished job\'s folder exclusion was still in the sync '
                    'configuration; removed (the regular sync includes the '
                    'folders again)')
        return stuck
    except Exception as e:
        print(f'{username}: could not check for a left-over folder exclusion: '
              f'{type(e).__name__}: {e}', flush=True)
        return []


def parse_budget_mb(value):
    """The daily budget form field as megabytes. Raises BulkError (shown on
    the form) for anything that is not a finite, non-negative number;
    clamps an absurdly large one to MAX_BUDGET_MB."""
    import math
    text = str(value if value is not None else '').strip()
    try:
        mb = float(text) if text else 0.0
    except (TypeError, ValueError):
        raise bulk.BulkError('The daily budget must be a number of megabytes.')
    if not math.isfinite(mb):
        raise bulk.BulkError('The daily budget must be a finite number of megabytes.')
    if mb < 0:
        raise bulk.BulkError('The daily budget cannot be negative.')
    return min(mb, MAX_BUDGET_MB)


def _log(username, email):
    """Append to the user's bulk_sync.log (and stdout, which is the cron
    log). Lines carry counts, reasons and redacted mbsync output only."""
    ma = _ma()

    def log(msg):
        line = f'{time.strftime("%Y-%m-%dT%H:%M:%S%z")} {email}: {msg}'
        print(line, flush=True)
        try:
            path = ma.get_user_config_dir(username) / LOG_NAME
            new = not path.exists()
            with open(path, 'a', encoding='utf-8') as f:
                f.write(line + '\n')
            if new:
                os.chmod(str(path), 0o640)
                ma._chown_user(path, username)
        except OSError:
            pass
    return log


# ---------------------------------------------------------------------
# The runner's dependencies
# ---------------------------------------------------------------------

def _owner_record(info):
    """platform_compat.UserInfo under both sets of names: app.py's
    _mbsync_child_env reads the pwd-style ones (pw_dir, pw_shell)."""
    from types import SimpleNamespace
    return SimpleNamespace(
        name=info.name, uid=info.uid, gid=info.gid, home=info.home, shell=info.shell,
        pw_name=info.name, pw_uid=info.uid, pw_gid=info.gid, pw_dir=info.home,
        pw_shell=info.shell)


def _owner_popen_kwargs(username):
    """Run a child as the mailbox owner, exactly as a normal sync spawns
    mbsync: kernel setuid with the owner's supplementary groups, the
    owner's home, and the minimal environment the PassCmd helper needs."""
    ma = _ma()
    if ma.CONFIG['auth_mode'] != 'pam':
        return {}
    info = compat.user_info(username)
    if info is None:
        raise bulk.BulkError(f'User {username} does not exist on this host.')
    pw = _owner_record(info)
    kw = {'cwd': pw.pw_dir, 'env': ma._mbsync_child_env(pw, username)}
    if compat.current_uid() != pw.pw_uid:
        kw.update({'user': pw.pw_uid, 'group': pw.pw_gid,
                   'extra_groups': ma._supplementary_gids(username, pw.pw_gid)})
    return kw


def prepare_config(username, email, folders):
    """Write the bulk run's config next to the state file and return
    (path, text). It is cut from the user's generated .mbsyncrc; if that
    file does not carry the job's exclusion yet (or the account is not in
    it), it is regenerated first."""
    ma = _ma()
    acct = find_account(username, email)
    if not acct:
        raise bulk.BulkError(f'{email} is not one of {username}\'s accounts.')
    channel = bulk.safe_account_name(email)
    rc = mbsyncrc_path(username)
    config_dir = ma.get_user_config_dir(username)
    want = f'Patterns {bulk.regular_sync_pattern(str(config_dir), acct)}'
    try:
        source = rc.read_text(encoding='utf-8')
    except OSError:
        source = ''
    if f'IMAPAccount {channel}' not in source.splitlines() \
            or want not in source.splitlines():
        ma.generate_mbsyncrc(username)
        source = rc.read_text(encoding='utf-8')
    text = bulk.build_bulk_rc(source, channel, folders)
    path = config_dir / (bulk.state_filename(email)[:-5] + '.mbsyncrc')
    with open(path, 'w', encoding='utf-8', newline='\n') as f:
        f.write(text)
    os.chmod(str(path), 0o600)
    ma._chown_user(path, username)
    return str(path), text


def start_mbsync(username, email, rc_path):
    """Start the pull of the job's folders with the host's isync."""
    ma = _ma()
    if bulk.isync_missing():
        raise bulk.BulkError(bulk.isync_missing())
    if not ma._has_mbsync():
        raise bulk.BulkError('isync (mbsync) is not installed on this host.')
    return bulk.spawn_supervised(
        [bulk.MBSYNC, '-c', rc_path, bulk.safe_account_name(email)],
        **_owner_popen_kwargs(username))


def _claim(username, email):
    ma = _ma()
    job_id, blocker = ma._new_sync_job(username, email, source='bulk')
    if job_id:
        ma._update_sync_job(
            job_id, state='running',
            last_line='Initial / rebase sync is downloading.')
    return job_id, blocker


def _release(job_id, ok, message):
    ma = _ma()
    ma._update_sync_job(
        job_id, state='done' if ok else 'error',
        exit_code=0 if ok else 1,
        error=None if ok else (message or 'Initial / rebase sync failed.'),
        last_line=message or '',
        finished=time.strftime('%Y-%m-%d %H:%M:%S'))


def _heartbeat(job_id, pid, message):
    _ma()._update_sync_job(job_id, mbsync_pid=pid, last_line=message)


def _server_busy(username, email, own_job_id=None):
    """Another sync (any user, any source) in flight on this account's
    IMAP server: the hourly sync's one-lane-per-server rule."""
    ma = _ma()
    acct = find_account(username, email)
    if not acct:
        return None
    server = ma._account_server(acct)
    ma._slc.reap_orphaned_states()
    for st in ma._slc.list_all_states():
        if st.get('state') not in ma._slc.ACTIVE_STATES or st.get('job_id') == own_job_id:
            continue
        user, target = st.get('user'), st.get('target') or '__all__'
        for other in ma.load_accounts(user) if user else []:
            if (target == '__all__' or other['email'] == target) \
                    and ma._account_server(other) == server:
                return st
    return None


def _server_counts(username, email, folders):
    result = reconcile_account(username, email, folders, exact=False, save=False)
    return {r['folder']: {'messages': r['server_messages'], 'uidnext': r.get('uidnext')}
            for r in result.get('folders', []) if r.get('server_messages') is not None}


def _index(username, email):
    from search_index import index_maildir
    index_maildir(username, _ma().CONFIG['data_dir'], account_filter=email)


def make_runner(username, email, log=None, **overrides):
    """The engine's runner for one account, with the app's real pieces.
    Tests pass `start=`, `claim=`, ... to replace any of them."""
    ma = _ma()

    def refresh_then_prepare(folders):
        try:
            ma._refresh_oauth_tokens_before_sync(username, email)
        except Exception:
            pass
        return prepare_config(username, email, folders)

    deps = dict(
        store=job_store(username, email),
        account_maildir=account_maildir(username, email),
        prepare_config=refresh_then_prepare,
        start=lambda rc_path: start_mbsync(username, email, rc_path),
        claim=lambda: _claim(username, email),
        release=_release,
        heartbeat=_heartbeat,
        server_busy=lambda token: _server_busy(username, email, token),
        server_counts=lambda folders: _server_counts(username, email, folders),
        on_closed=lambda: regenerate_config(username),
        after_run=lambda: _index(username, email),
        redact=ma.redact_sync_output,
        log=log or _log(username, email),
    )
    deps.update(overrides)
    return bulk.BulkJobRunner(**deps)


def run_account(username, email, trigger='cron', **overrides):
    return make_runner(username, email, **overrides).run(trigger)


# ---------------------------------------------------------------------
# Job actions (used by the routes; each returns the new state document)
# ---------------------------------------------------------------------

def create_job(username, email, folders, *, budget_mb=None, window_start=None,
               window_end=None, kind='initial', now=None):
    acct = find_account(username, email)
    if not acct:
        raise bulk.BulkError('No such account.')
    if bulk.isync_missing():
        raise bulk.BulkError(bulk.isync_missing())
    if not acct.get('enabled', True):
        raise bulk.BulkError('This account is disabled. Enable it first.')
    profile = account_profile(acct)
    defaults = bulk.PROVIDER_DEFAULTS[profile]
    folders = bulk.validate_folders(folders)
    budget_mb = defaults['budget_mb'] if budget_mb is None else budget_mb
    maildir = account_maildir(username, email)
    messages = size = 0
    for f in folders:
        n, b = bulk.MaildirMeter(bulk.folder_maildir(maildir, f)).measure()
        messages, size = messages + n, size + b
    now = time.time() if now is None else now
    job = bulk.new_job(
        email, folders, kind=kind, profile=profile, created_by=username,
        budget_bytes=int(float(budget_mb) * bulk.MB),
        window_start=window_start or defaults['window_start'],
        window_end=window_end or defaults['window_end'],
        original_pattern=acct.get('folder_pattern') or None, now=now,
        local={'messages': messages, 'bytes': size, 'at': bulk.fmt_time(now)})
    doc = bulk.start_job(job_store(username, email), job)
    regenerate_config(username)
    return doc


def change_status(username, email, action):
    doc = bulk.set_status(job_store(username, email), action)
    if action == 'cancel':
        regenerate_config(username)
    return doc


def spawn_manual_run(username, email):
    """Start one run in its own process, so it outlives the request and any
    gunicorn worker recycle (as the manual PST import does)."""
    ma = _ma()
    proc = subprocess.Popen(
        [sys.executable, ma.APP_SCRIPT, 'bulk-sync', '--user', username,
         '--account', email, '--manual'],
        cwd=os.path.dirname(ma.APP_SCRIPT), env=dict(os.environ),
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        **compat.detached_popen_kwargs())       # Linux: start_new_session
    threading.Thread(target=proc.wait, daemon=True).start()


# ---------------------------------------------------------------------
# Reconcile
# ---------------------------------------------------------------------

def _real_imap_client(username, acct):
    """An authenticated, read-only IMAP client for the account, through the
    credential path the sync already uses: the stored password via
    load_credential, or the OAuth access token file for XOAUTH2."""
    ma = _ma()
    from mail_batcher import _connect_imap, _authenticate
    email = acct['email']
    provider = ma.PROVIDERS.get(acct.get('provider', ''), {})
    host = acct.get('host') or provider.get('host', '')
    port = int(acct.get('port') or provider.get('port', 993))
    tls_default = 'ssl' if provider.get('tls', True) else 'none'
    tls_mode = (acct.get('tls_mode') or tls_default).lower()
    auth_type = acct.get('auth_type') or provider.get('auth', 'password')
    if auth_type == 'oauth2':
        token = ma.get_user_config_dir(username) / f'{bulk.safe_account_name(email)}.token'
        try:
            credential = token.read_text(encoding='utf-8').strip()
        except OSError:
            credential = ''
    else:
        credential = ma.load_credential(username, email) or ''
    if not credential:
        raise bulk.BulkError('This account has no stored password or sign-in.')
    try:
        conn = _connect_imap(host, port, tls_mode)
        _authenticate(conn, email, credential, auth_type)
    except Exception as e:
        msg = ma.redact_sync_output(f'{type(e).__name__}: {e}') or ''
        raise bulk.BulkError('Could not sign in to the mail server: '
                             + msg.replace(credential, '<redacted>')[:300])
    return bulk.ImapReadOnly(conn)


# Tests replace this with a factory that returns a fake client.
IMAP_FACTORY = _real_imap_client


def reconcile_inline(username, email, folders=None, exact=True):
    """Server against archive. Runs as the mailbox owner (the credential
    is the owner's), or directly in builtin mode."""
    acct = find_account(username, email)
    if not acct:
        return {'ok': False, 'error': 'No such account.', 'folders': [],
                'server_folders': []}
    job = job_store(username, email).load().get('job')
    active = job['folders'] if bulk.job_is_open(job) else []
    client = None
    try:
        client = IMAP_FACTORY(username, acct)
        out = bulk.reconcile(client, account_maildir(username, email),
                             folders, active_folders=active, exact=exact)
        out.update({'ok': True, 'error': None})
    except bulk.BulkError as e:
        out = {'ok': False, 'error': str(e), 'folders': [], 'server_folders': []}
    except Exception as e:
        out = {'ok': False, 'folders': [], 'server_folders': [],
               'error': _ma().redact_sync_output(
                   f'The mail server could not be read: {type(e).__name__}: {e}')[:300]}
    finally:
        if client is not None:
            client.logout()
    out.update({'email': email, 'checked': bulk.fmt_time(time.time())})
    return out


def _reconcile_as_owner(username, email, folders, exact):
    ma = _ma()
    kw = _owner_popen_kwargs(username)
    if 'user' not in kw:
        return reconcile_inline(username, email, folders, exact)
    child = (
        'import json, sys\n'
        'sys.path.insert(0, %r)\n'
        'import app, bulk_sync_routes\n'
        'out = bulk_sync_routes.reconcile_inline(%r, %r, %r, %r)\n'
        'print(%r + json.dumps(out))\n'
    ) % (os.path.dirname(ma.APP_SCRIPT), username, email,
         list(folders) if folders else None, bool(exact), _JSON_MARK)
    try:
        res = subprocess.run([sys.executable, '-c', child],
                             stdin=subprocess.DEVNULL, capture_output=True,
                             text=True, timeout=RECONCILE_TIMEOUT, **kw)
    except subprocess.TimeoutExpired:
        return {'ok': False, 'error': 'The mail server did not answer in time.',
                'folders': [], 'server_folders': [], 'email': email,
                'checked': bulk.fmt_time(time.time())}
    for line in reversed((res.stdout or '').splitlines()):
        if line.startswith(_JSON_MARK):
            try:
                return json.loads(line[len(_JSON_MARK):])
            except ValueError:
                break
    return {'ok': False, 'folders': [], 'server_folders': [], 'email': email,
            'checked': bulk.fmt_time(time.time()),
            'error': 'The server check could not be run as the mailbox owner '
                     f'(exit code {res.returncode}).'}


def reconcile_account(username, email, folders=None, exact=True, save=True):
    """Count the account's folders on the server (read-only) and compare
    with the archive. The result is kept in the state file for the page."""
    ma = _ma()
    try:
        ma._refresh_oauth_tokens_before_sync(username, email)
    except Exception:
        pass
    result = _reconcile_as_owner(username, email, folders, exact)
    if save:
        def fn(doc):
            doc['reconcile'] = result
            job = doc.get('job')
            if result.get('ok') and bulk.job_is_open(job):
                server = dict(job.get('server') or {})
                for r in result['folders']:
                    if r['folder'] in job['folders'] and r.get('server_messages') is not None:
                        server[r['folder']] = {'messages': r['server_messages'],
                                               'uidnext': r.get('uidnext'),
                                               'checked': result['checked']}
                job['server'] = server
        job_store(username, email).update(fn)
    return result


# ---------------------------------------------------------------------
# Scheduler and command line
# ---------------------------------------------------------------------

def iter_jobs():
    """(username, email, state document) for every account that has a bulk
    sync state file."""
    ma = _ma()
    data_dir = Path(ma.CONFIG['data_dir'])
    if not data_dir.exists():
        return
    for userdir in sorted(data_dir.iterdir()):
        if not userdir.is_dir() or userdir.name.startswith('.'):
            continue
        if not any((userdir / '.config').glob(STATE_GLOB)):
            continue
        username = userdir.name
        for acct in ma.load_accounts(username):
            store = job_store(username, acct['email'])
            if os.path.exists(store.path):
                yield username, acct['email'], store.load()


def scheduled_bulk_sync():
    """Hourly cron entry: run every open job that may run now. Jobs on
    different IMAP servers run side by side; on one server, one after
    another."""
    ma = _ma()
    print(f'=== Initial / rebase sync check at '
          f'{time.strftime("%Y-%m-%d %H:%M:%S")} ===', flush=True)
    if bulk.isync_missing():
        print(f'Nothing run: {bulk.isync_missing()}', flush=True)
        print('=== Done ===', flush=True)
        return
    lanes = {}
    healed = set()
    for username, email, doc in iter_jobs():
        if username not in healed:
            healed.add(username)
            heal_stuck_exclusions(username)
        job = doc.get('job')
        if not job or job.get('status') != 'active':
            continue
        ok, why = bulk.eligibility(job, time.time(), 'cron')
        if not ok:
            print(f'{username}/{email}: not now ({why})', flush=True)
            continue
        acct = find_account(username, email)
        lanes.setdefault(ma._account_server(acct), []).append((username, email))

    def lane(items):
        for username, email in items:
            try:
                res = run_account(username, email, 'cron')
            except Exception as e:
                print(f'{username}/{email}: failed: {type(e).__name__}: '
                      f'{ma.redact_sync_output(str(e))}', flush=True)
                continue
            if not res.get('ran'):
                print(f'{username}/{email}: skipped ({res.get("reason")})', flush=True)

    if lanes:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=min(ma.MAX_PARALLEL_SYNCS, len(lanes))) as pool:
            for f in [pool.submit(lane, items) for items in lanes.values()]:
                f.result()
    print('=== Done ===', flush=True)


def print_jobs():
    print(f'{"user/account":44} {"status":10} {"local":>9} {"server":>9} '
          f'{"today":>10} {"budget":>10} {"days":>4}  last stop / folders')
    n = 0
    for username, email, doc in iter_jobs():
        v = bulk.job_view(doc.get('job'))
        if not v:
            continue
        n += 1
        num = lambda x: '-' if x is None else f'{x:,}'
        print(f'{(username + "/" + email)[:44]:44} {v["state"]:10} '
              f'{num(v["messages_local"]):>9} {num(v["messages_server"]):>9} '
              f'{v["today_text"]:>10} {v["budget_text"]:>10} {v["days_run"]:>4}  '
              f'{v["last_stop_reason"] or "-"} / {", ".join(v["folders"])}')
    if not n:
        print('(no initial / rebase sync jobs)')


def print_reconcile(result):
    if not result.get('ok'):
        print(f'Reconcile failed: {result.get("error")}')
        return 1
    print(f'Reconcile of {result["email"]} at {result["checked"]} '
          f'(IMAP LIST / STATUS / EXAMINE only)')
    print(f'{"folder":34} {"server":>9} {"local":>9} {"delta":>8} {"new":>6} '
          f'{"missing":>8}  verdict')
    num = lambda x: '-' if x is None else f'{x:,}'
    for r in result['folders']:
        delta = r.get('delta')
        print(f'{r["folder"][:34]:34} {num(r.get("server_messages")):>9} '
              f'{num(r.get("local_messages")):>9} '
              f'{"-" if delta is None else format(delta, "+,"):>8} '
              f'{num(r.get("new_since_last_sync")):>6} {num(r.get("missing")):>8}  '
              f'{r["verdict_label"]}')
        print(f'    {r["explanation"]}')
    return 0


def cli(argv):
    import argparse
    ap = argparse.ArgumentParser(prog='app.py bulk-sync')
    ap.add_argument('--list', action='store_true', help='list every job')
    ap.add_argument('--user')
    ap.add_argument('--account')
    ap.add_argument('--manual', action='store_true',
                    help='run the account\'s job once now (ignores the window)')
    ap.add_argument('--reconcile', action='store_true',
                    help='server count against the archive, read-only')
    ap.add_argument('--folder', action='append',
                    help='with --reconcile: only this folder (repeatable)')
    ap.add_argument('--json', action='store_true')
    a = ap.parse_args(argv)
    if a.list:
        print_jobs()
        return 0
    if a.manual or a.reconcile:
        if not (a.user and a.account):
            ap.error('--manual and --reconcile need --user and --account')
        if not find_account(a.user, a.account):
            print(f'{a.account} is not one of {a.user}\'s accounts.')
            return 1
    if a.reconcile:
        result = reconcile_account(a.user, a.account, a.folder)
        if a.json:
            print(json.dumps(result, indent=2))
            return 0 if result.get('ok') else 1
        return print_reconcile(result)
    if a.manual:
        if bulk.isync_missing():
            _log(a.user, a.account)('not run: ' + bulk.isync_missing())
            return 1
        try:
            res = run_account(a.user, a.account, 'manual')
        except Exception as e:
            # The WebUI starts this detached with no terminal: say it in
            # the user's bulk_sync.log.
            _log(a.user, a.account)('run failed: ' + (_ma().redact_sync_output(
                f'{type(e).__name__}: {e}') or ''))
            return 1
        if not res.get('ran'):
            print(f'{a.user}/{a.account}: not run ({res.get("reason")}'
                  + (f': {res["detail"]}' if res.get('detail') else '') + ')')
        return 0
    if a.user or a.account:
        ap.error('--user/--account need --manual or --reconcile')
    scheduled_bulk_sync()
    return 0


# ---------------------------------------------------------------------
# WebUI
# ---------------------------------------------------------------------

def account_context(username, acct):
    """What the page shows for one account."""
    email = acct['email']
    doc = job_store(username, email).load()
    job = doc.get('job')
    profile = account_profile(acct)
    defaults = bulk.PROVIDER_DEFAULTS[profile]
    view = bulk.job_view(job)
    runs = []
    for r in reversed((job or {}).get('runs') or []):
        runs.append(dict(r, reason_label=bulk.STOP_REASONS.get(r.get('reason'), r.get('reason')),
                         bytes_text=bulk.fmt_bytes(r.get('bytes'))))
    return {
        'email': email,
        'enabled': acct.get('enabled', True),
        'profile': profile,
        'defaults': defaults,
        'job': view,
        'open': bulk.job_is_open(job),
        'runs': runs,
        'days': list(reversed(bulk.daily_totals(job or {}))),
        'reconcile': doc.get('reconcile'),
        'history': doc.get('history') or [],
        'folder_pattern': acct.get('folder_pattern') or '',
    }


def local_folders(username, email, limit=200):
    """Folders the archive already has for the account (for the chooser)."""
    base = account_maildir(username, email)
    out = []
    for root, dirs, _files in os.walk(base):
        dirs[:] = sorted(d for d in dirs if d not in ('cur', 'new', 'tmp'))
        if os.path.isdir(os.path.join(root, 'cur')) and root != base:
            # a folder name, as the server and mbsync write it: with '/'
            out.append(os.path.relpath(root, base).replace(os.sep, '/'))
            if len(out) >= limit:
                break
    return out


def register(app):
    """Attach the bulk-sync routes to the Flask app (called once by app.py)."""
    global _APP
    _APP = app
    from flask import (render_template, request, redirect, url_for, session,
                       jsonify, flash, abort)

    def login_required(f):
        return _ma().login_required(f)

    def _acct_or_404(email):
        acct = find_account(session['username'], email)
        if not acct:
            # Also the answer for another user's account: a job is only
            # ever looked up below the logged-in user's own accounts.
            abort(404)
        return acct

    def _back(email):
        return redirect(url_for('bulk_sync_account', email=email))

    @app.route('/bulk-sync')
    @login_required
    def bulk_sync_page():
        username = session['username']
        loaded = _ma().load_accounts(username)
        heal_stuck_exclusions(username, loaded)
        accounts = [account_context(username, a) for a in loaded]
        return render_template('bulk_sync.html', username=username,
                               accounts=accounts, account=None,
                               needs_isync=bulk.isync_missing())

    @app.route('/bulk-sync/<email>')
    @login_required
    def bulk_sync_account(email):
        username = session['username']
        acct = _acct_or_404(email)
        heal_stuck_exclusions(username)
        ctx = account_context(username, acct)
        suggested = list(ctx['defaults']['folders'])
        for f in local_folders(username, email):
            if f not in suggested:
                suggested.append(f)
        for f in (ctx['reconcile'] or {}).get('server_folders') or []:
            if f not in suggested:
                suggested.append(f)
        return render_template('bulk_sync.html', username=username,
                               accounts=None, account=ctx,
                               suggested_folders=suggested,
                               needs_isync=bulk.isync_missing())

    @app.route('/bulk-sync/<email>/start', methods=['POST'])
    @login_required
    def bulk_sync_start(email):
        username = session['username']
        _acct_or_404(email)
        folders = request.form.getlist('folders')
        folders += (request.form.get('folders_other') or '').splitlines()
        try:
            budget = parse_budget_mb(request.form.get('budget_mb'))
            create_job(username, email, folders, budget_mb=budget,
                       window_start=request.form.get('window_start'),
                       window_end=request.form.get('window_end'))
            flash('Initial / rebase sync set up. The regular sync now skips '
                  'the chosen folders until the job is complete.')
        except (bulk.BulkError, ValueError, OverflowError) as e:
            flash(str(e) if isinstance(e, bulk.BulkError)
                  else 'The daily budget must be a number of megabytes.')
        return _back(email)

    @app.route('/bulk-sync/<email>/settings', methods=['POST'])
    @login_required
    def bulk_sync_settings(email):
        username = session['username']
        _acct_or_404(email)
        try:
            budget = parse_budget_mb(request.form.get('budget_mb'))
            bulk.update_settings(job_store(username, email),
                                 budget_bytes=int(budget * bulk.MB),
                                 window_start=request.form.get('window_start') or '',
                                 window_end=request.form.get('window_end') or '')
            flash('Budget and time window saved.')
        except (bulk.BulkError, ValueError, OverflowError) as e:
            flash(str(e) if isinstance(e, bulk.BulkError)
                  else 'The daily budget must be a number of megabytes.')
        return _back(email)

    @app.route('/bulk-sync/<email>/<any(pause,resume,cancel):action>', methods=['POST'])
    @login_required
    def bulk_sync_action(email, action):
        username = session['username']
        _acct_or_404(email)
        try:
            change_status(username, email, action)
            flash({'pause': 'Paused. The folders stay out of the regular sync '
                            'until you resume or cancel.',
                   'resume': 'Resumed.',
                   'cancel': 'Cancelled. The regular sync includes the folders '
                             'again; all downloaded mail was kept.'}[action])
        except bulk.BulkError as e:
            flash(str(e))
        return _back(email)

    @app.route('/bulk-sync/<email>/run', methods=['POST'])
    @login_required
    def bulk_sync_run(email):
        username = session['username']
        _acct_or_404(email)
        job = job_store(username, email).load().get('job')
        ok, why = bulk.eligibility(job, time.time(), 'manual')
        if bulk.isync_missing():
            flash(bulk.isync_missing())
        elif not ok:
            flash({'budget': 'Today\'s budget is used up. Raise it, or wait '
                             'until tomorrow.',
                   'paused': 'The job is paused. Resume it first.'}.get(
                       why, 'There is no open initial / rebase sync to run.'))
        elif (bulk.job_view(job) or {}).get('running'):
            flash('A run is already in progress.')
        else:
            spawn_manual_run(username, email)
            flash('Run started. It continues in the background; this page '
                  'shows the progress.')
        return _back(email)

    @app.route('/bulk-sync/<email>/reconcile', methods=['POST'])
    @login_required
    def bulk_sync_reconcile(email):
        username = session['username']
        _acct_or_404(email)
        result = reconcile_account(username, email)
        if not result.get('ok'):
            flash(f'The server could not be checked: {result.get("error")}')
        return _back(email)

    @app.route('/bulk-sync/<email>/fetch-missing', methods=['POST'])
    @login_required
    def bulk_sync_fetch_missing(email):
        username = session['username']
        _acct_or_404(email)
        try:
            folder = bulk.validate_folder(request.form.get('folder') or '')
            if bulk.isync_missing():
                raise bulk.BulkError(bulk.isync_missing())
            job = job_store(username, email).load().get('job')
            if bulk.job_is_open(job):
                if folder not in job['folders']:
                    raise bulk.BulkError(
                        'This account already has an initial / rebase sync '
                        'for other folders. Let it finish or cancel it first.')
                if job['status'] != 'active':
                    raise bulk.BulkError('The job is paused. Resume it first.')
            else:
                create_job(username, email, [folder], kind='fetch-missing')
            spawn_manual_run(username, email)
            flash(f'Fetching "{folder}" from the server now (download only). '
                  f'The regular sync skips the folder until this is complete.')
        except bulk.BulkError as e:
            flash(str(e))
        return _back(email)

    @app.route('/api/bulk-sync/status')
    @login_required
    def bulk_sync_status():
        username = session['username']
        only = request.args.get('email')
        out = []
        for acct in _ma().load_accounts(username):
            if only and acct['email'] != only:
                continue
            ctx = account_context(username, acct)
            out.append({'email': acct['email'], 'job': ctx['job'],
                        'last_run': ctx['runs'][0] if ctx['runs'] else None})
        return jsonify({'accounts': out})
