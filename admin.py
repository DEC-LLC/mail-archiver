#!/usr/bin/env python3
"""Mail Archiver — /admin observability + service control (1.0.18).

Registered on the main Flask app via `admin.register(app)`. Renders a
single admin page with four cards (Service / Storage / Host / App) plus
three detail sub-pages. Access is PAM-group-gated: the logged-in user
must belong to the group named by MAIL_ARCHIVER_ADMIN_GROUP (default
mail-archiver-admins), or as a fallback to sudo/wheel if that group
doesn't exist.

The Restart Now button touches /run/mail-archiver/restart-requested;
the packaged mail-archiver-restart.path unit picks it up and runs
`systemctl restart mail-archiver` as root, so the WebUI never needs
direct systemctl privileges.
"""

import os
import time
import json
import fcntl
import subprocess
import threading
import pwd
import grp
from functools import wraps
from pathlib import Path

from flask import (
    Blueprint, render_template, redirect, url_for, session, jsonify,
    request, abort
)

admin_bp = Blueprint('admin', __name__, url_prefix='/admin')

# ---------------------------------------------------------------
# Config
# ---------------------------------------------------------------
ADMIN_GROUP = os.environ.get('MAIL_ARCHIVER_ADMIN_GROUP',
                             'mail-archiver-admins')
FALLBACK_GROUPS = ('sudo', 'wheel')
RESTART_REQUEST_MARKER = '/run/mail-archiver/restart-requested'
RESTART_PENDING_MARKER = '/run/mail-archiver/restart-pending'
VERSION_FILE = '/opt/mail-archiver/VERSION'

STORAGE_CACHE_TTL = 300  # seconds
_STORAGE_CACHE = {}       # {(user, email): {bytes, computed_at, computing}}
_STORAGE_CACHE_LOCK = threading.Lock()
_STORAGE_POOL_SEM = threading.Semaphore(2)  # max 2 concurrent `du`

# Load-history ring buffer for the host detail page
_LOAD_HISTORY = []  # list of (ts, load1)
_LOAD_HISTORY_LOCK = threading.Lock()
_LOAD_HISTORY_MAX = 30  # 30 samples * 30s = 15 min
_LOAD_SAMPLER_STARTED = False
_LOAD_SAMPLER_LOCK = threading.Lock()

# Session cache for admin group check — key is username, value is (is_admin, ts)
_ADMIN_CHECK_CACHE = {}
_ADMIN_CHECK_CACHE_LOCK = threading.Lock()
_ADMIN_CHECK_TTL = 60  # seconds — short enough to pick up new group memberships


# ---------------------------------------------------------------
# Admin auth
# ---------------------------------------------------------------

def _resolve_admin_group():
    """Return the group NAME to check, honoring env + fallbacks.
    If configured group doesn't exist and no fallback either, returns
    the configured name anyway so `getgrnam` raises KeyError upstream
    (deny by default).
    """
    try:
        grp.getgrnam(ADMIN_GROUP)
        return ADMIN_GROUP
    except KeyError:
        for fallback in FALLBACK_GROUPS:
            try:
                grp.getgrnam(fallback)
                return fallback
            except KeyError:
                continue
    return ADMIN_GROUP  # will raise later on check


def _is_admin(username):
    """Check whether username belongs to the resolved admin group.
    Cached per-session for _ADMIN_CHECK_TTL seconds. Returns bool.
    """
    now = time.time()
    with _ADMIN_CHECK_CACHE_LOCK:
        cached = _ADMIN_CHECK_CACHE.get(username)
        if cached and now - cached[1] < _ADMIN_CHECK_TTL:
            return cached[0]

    result = False
    try:
        group_name = _resolve_admin_group()
        gr = grp.getgrnam(group_name)
        pw = pwd.getpwnam(username)
        # Member either explicitly in gr_mem or via primary GID
        if username in gr.gr_mem or pw.pw_gid == gr.gr_gid:
            result = True
    except (KeyError, OSError):
        result = False

    with _ADMIN_CHECK_CACHE_LOCK:
        _ADMIN_CHECK_CACHE[username] = (result, now)
    return result


def requires_admin(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        username = session.get('username')
        if not username:
            return redirect(url_for('login'))
        if not _is_admin(username):
            group_name = _resolve_admin_group()
            html = (
                f'<h1>403 — Admin access required</h1>'
                f'<p>You must belong to the <code>{group_name}</code> '
                f'group to access this page.</p>'
                f'<p>An operator can grant access with:</p>'
                f'<pre>sudo usermod -aG {group_name} {username}</pre>'
                f'<p>Then log out and back in.</p>'
                f'<p><a href="/dashboard">Back to dashboard</a></p>'
            )
            return html, 403
        return f(*args, **kwargs)
    return decorated


# ---------------------------------------------------------------
# Service card
# ---------------------------------------------------------------

def _read_version():
    try:
        with open(VERSION_FILE) as f:
            return f.read().strip()
    except OSError:
        return 'unknown'


def _systemctl_show(unit, prop):
    try:
        out = subprocess.check_output(
            ['systemctl', 'show', unit, '-p', prop, '--value'],
            timeout=5, text=True
        )
        return out.strip()
    except (subprocess.SubprocessError, OSError):
        return ''


def _service_info():
    active = _systemctl_show('mail-archiver.service', 'ActiveState') or 'unknown'
    since_us = _systemctl_show('mail-archiver.service', 'ActiveEnterTimestampMonotonic')
    uptime_s = None
    if since_us and since_us.isdigit():
        # ActiveEnterTimestampMonotonic is us since kernel boot; compare
        # against /proc/uptime
        try:
            with open('/proc/uptime') as f:
                boot_uptime = float(f.read().split()[0])
            uptime_s = boot_uptime - (int(since_us) / 1_000_000)
            if uptime_s < 0:
                uptime_s = None
        except (OSError, ValueError):
            pass

    restart_pending = None
    if os.path.exists(RESTART_PENDING_MARKER):
        try:
            with open(RESTART_PENDING_MARKER) as f:
                restart_pending = f.read().strip()
        except OSError:
            pass

    restart_requested = os.path.exists(RESTART_REQUEST_MARKER)

    # Worker + thread counts — from env if set, else defaults from gunicorn.conf.py
    workers = os.environ.get('MAIL_ARCHIVER_WORKERS', '2')
    threads_base = int(os.environ.get('MAIL_ARCHIVER_THREADS_BASE', '4'))
    threads_max = int(os.environ.get('MAIL_ARCHIVER_THREADS_MAX', '24'))

    return {
        'version': _read_version(),
        'active_state': active,
        'uptime_s': uptime_s,
        'restart_pending': restart_pending,
        'restart_requested': restart_requested,
        'workers': workers,
        'threads_base': threads_base,
        'threads_max': threads_max,
    }


# ---------------------------------------------------------------
# Storage card
# ---------------------------------------------------------------

def _statvfs_summary(path):
    try:
        st = os.statvfs(path)
        total = st.f_blocks * st.f_frsize
        avail = st.f_bavail * st.f_frsize
        used = total - avail
        pct_free = (avail / total * 100.0) if total > 0 else 0.0
        band = 'green' if pct_free >= 20.0 else ('amber' if pct_free >= 5.0 else 'red')
        return {'total': total, 'used': used, 'avail': avail,
                'pct_free': pct_free, 'band': band, 'path': path}
    except OSError as e:
        return {'error': str(e), 'path': path}


def _slug(s):
    return s.replace('@', '_at_').replace('.', '_')


def _compute_du_bytes(path):
    try:
        out = subprocess.check_output(
            ['du', '-sb', path], timeout=300, text=True,
            stderr=subprocess.DEVNULL
        )
        return int(out.split()[0])
    except (subprocess.SubprocessError, OSError, ValueError, IndexError):
        return -1


def _background_du(user, email, path):
    with _STORAGE_POOL_SEM:
        try:
            b = _compute_du_bytes(path)
            with _STORAGE_CACHE_LOCK:
                _STORAGE_CACHE[(user, email)] = {
                    'bytes': b,
                    'computed_at': time.time(),
                    'computing': False,
                    'path': path,
                }
        except Exception:
            with _STORAGE_CACHE_LOCK:
                _STORAGE_CACHE[(user, email)] = {
                    'bytes': -1,
                    'computed_at': time.time(),
                    'computing': False,
                    'path': path,
                }


def _storage_per_account(data_dir):
    """Walk data dir, return list of {user, email, path, bytes, computing}.
    Kicks off background du computations for anything stale/uncached.
    """
    rows = []
    now = time.time()
    try:
        for user_dir in sorted(Path(data_dir).iterdir()):
            if not user_dir.is_dir():
                continue
            user = user_dir.name
            # Look at accounts.json to enumerate emails
            acct_file = user_dir / '.config' / 'accounts.json'
            if not acct_file.exists():
                continue
            try:
                data = json.loads(acct_file.read_text())
                emails = [
                    a.get('email') for a in (
                        data if isinstance(data, list)
                        else data.values() if isinstance(data, dict) else []
                    )
                    if isinstance(a, dict) and a.get('email')
                ]
            except (OSError, ValueError):
                continue
            for email in emails:
                slug = _slug(email)
                path = str(user_dir / slug)
                key = (user, email)
                with _STORAGE_CACHE_LOCK:
                    cached = _STORAGE_CACHE.get(key)
                stale = (not cached) or (now - cached.get('computed_at', 0) > STORAGE_CACHE_TTL)
                # Kick off background compute if stale and not already running
                if stale and (not cached or not cached.get('computing')):
                    with _STORAGE_CACHE_LOCK:
                        _STORAGE_CACHE[key] = {
                            'bytes': cached.get('bytes', -1) if cached else -1,
                            'computed_at': cached.get('computed_at', 0) if cached else 0,
                            'computing': True,
                            'path': path,
                        }
                    t = threading.Thread(
                        target=_background_du, args=(user, email, path),
                        daemon=True
                    )
                    t.start()
                    cached = _STORAGE_CACHE[key]
                rows.append({
                    'user': user,
                    'email': email,
                    'path': path,
                    'bytes': (cached.get('bytes', -1) if cached else -1),
                    'computing': (cached.get('computing', False) if cached else True),
                })
    except OSError as e:
        rows.append({'error': str(e)})
    return rows


# ---------------------------------------------------------------
# Host card
# ---------------------------------------------------------------

def _read_loadavg():
    try:
        with open('/proc/loadavg') as f:
            parts = f.read().split()
        return {'load1': float(parts[0]), 'load5': float(parts[1]),
                'load15': float(parts[2])}
    except (OSError, ValueError, IndexError):
        return {'load1': None, 'load5': None, 'load15': None}


def _read_meminfo():
    out = {'total_kb': 0, 'available_kb': 0, 'pct_used': 0.0}
    try:
        with open('/proc/meminfo') as f:
            for line in f:
                if line.startswith('MemTotal:'):
                    out['total_kb'] = int(line.split()[1])
                elif line.startswith('MemAvailable:'):
                    out['available_kb'] = int(line.split()[1])
        if out['total_kb'] > 0:
            out['pct_used'] = (
                (out['total_kb'] - out['available_kb']) / out['total_kb'] * 100.0
            )
    except (OSError, ValueError, IndexError):
        pass
    return out


def _read_thermal_max_c():
    max_c = None
    try:
        for p in Path('/sys/class/thermal').glob('thermal_zone*'):
            try:
                v = int((p / 'temp').read_text().strip())
                c = v / 1000.0 if v > 1000 else float(v)
                if max_c is None or c > max_c:
                    max_c = c
            except (OSError, ValueError):
                continue
    except OSError:
        pass
    return max_c


def _read_uptime_s():
    try:
        with open('/proc/uptime') as f:
            return float(f.read().split()[0])
    except (OSError, ValueError, IndexError):
        return None


def _read_os_release():
    d = {}
    try:
        with open('/etc/os-release') as f:
            for line in f:
                if '=' in line:
                    k, v = line.strip().split('=', 1)
                    d[k] = v.strip('"')
    except OSError:
        pass
    return d


def _read_diskstats_for(path):
    """Return {read_sectors, write_sectors} for the block device backing path,
    or {} if unresolvable.
    """
    try:
        st = os.stat(path)
        target_major, target_minor = os.major(st.st_dev), os.minor(st.st_dev)
    except OSError:
        return {}
    try:
        with open('/proc/diskstats') as f:
            for line in f:
                parts = line.split()
                if len(parts) < 14:
                    continue
                if int(parts[0]) == target_major and int(parts[1]) == target_minor:
                    return {
                        'device': parts[2],
                        'reads': int(parts[3]),
                        'read_sectors': int(parts[5]),
                        'writes': int(parts[7]),
                        'write_sectors': int(parts[9]),
                    }
    except (OSError, ValueError, IndexError):
        pass
    return {}


def _sample_load_forever():
    while True:
        try:
            la = _read_loadavg()
            with _LOAD_HISTORY_LOCK:
                _LOAD_HISTORY.append((time.time(), la.get('load1')))
                if len(_LOAD_HISTORY) > _LOAD_HISTORY_MAX:
                    del _LOAD_HISTORY[:len(_LOAD_HISTORY) - _LOAD_HISTORY_MAX]
        except Exception:
            pass
        time.sleep(30)


def _ensure_load_sampler():
    global _LOAD_SAMPLER_STARTED
    with _LOAD_SAMPLER_LOCK:
        if _LOAD_SAMPLER_STARTED:
            return
        t = threading.Thread(target=_sample_load_forever, daemon=True)
        t.start()
        _LOAD_SAMPLER_STARTED = True


# ---------------------------------------------------------------
# App card
# ---------------------------------------------------------------

def _oauth_token_summary(data_dir):
    """Walk *.oauth2.json for all users. Return two lists:
    - expiring_soon: tokens whose access token expires < 24h
    - needs_reauth: tokens whose refresh token is invalid
    """
    now = time.time()
    expiring_soon = []
    needs_reauth = []
    try:
        for user_dir in Path(data_dir).iterdir():
            if not user_dir.is_dir():
                continue
            cfg = user_dir / '.config'
            if not cfg.exists():
                continue
            try:
                token_files = list(cfg.glob('*.oauth2.json'))
            except OSError:
                continue
            for f in token_files:
                try:
                    d = json.loads(f.read_text())
                except (OSError, ValueError):
                    continue
                # Reconstruct email from filename
                # <slug>.oauth2.json => convert slug back to email
                slug = f.stem.replace('.oauth2', '')
                email = slug.replace('_at_', '@').replace('_', '.', 1)
                exp = d.get('expires_at', 0)
                if d.get('needs_reauth'):
                    needs_reauth.append({
                        'user': user_dir.name,
                        'email': email,
                        'file': str(f),
                        'last_updated': d.get('updated_at', ''),
                    })
                elif exp - now < 86400:
                    expiring_soon.append({
                        'user': user_dir.name,
                        'email': email,
                        'file': str(f),
                        'expires_in_s': int(exp - now),
                        'expires_in_min': int((exp - now) / 60),
                    })
    except OSError:
        pass
    return expiring_soon, needs_reauth


def _active_syncs():
    """Enumerate active syncs from the file-backed lifecycle store (1.1.0).
    Cross-worker visible — every worker reads the same state files.
    Includes all users (admin scope). Also reports the systemd scope
    unit name so the admin can inspect via journalctl if needed.
    """
    try:
        import sync_lifecycle as slc
        jobs = []
        for st in slc.list_all_states():
            if st.get('state') not in ('running', 'starting'):
                continue
            jobs.append({
                'job_id': st.get('job_id', ''),
                'user': st.get('user', ''),
                'email': st.get('email') or '__all__',
                'state': st.get('state', ''),
                'started': st.get('started', ''),
                'pid': st.get('mbsync_pid'),
                'scope_unit': st.get('scope_unit', ''),
            })
        return jobs
    except (ImportError, AttributeError):
        return []


def _recent_failure_rate(log_path='/var/log/mail-archiver-sync.log', n=100):
    """Parse tail of the sync log, count 'Sync failed for' vs 'Sync completed' or
    'Sync error'. Returns {total, failures, rate}.
    """
    if not os.path.exists(log_path):
        return {'total': 0, 'failures': 0, 'rate': None}
    try:
        # Read last ~64K then parse — cheap enough
        with open(log_path, 'rb') as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 65536))
            tail = f.read().decode('utf-8', 'replace')
    except OSError:
        return {'total': 0, 'failures': 0, 'rate': None}

    lines = tail.strip().split('\n')[-n:]
    total = 0
    failures = 0
    for line in lines:
        low = line.lower()
        if 'sync completed' in low or 'sync failed' in low or 'sync error' in low:
            total += 1
            if 'failed' in low or 'error' in low:
                failures += 1
    rate = (failures / total) if total > 0 else None
    return {'total': total, 'failures': failures, 'rate': rate}


# ---------------------------------------------------------------
# Routes
# ---------------------------------------------------------------

@admin_bp.route('/')
@requires_admin
def index():
    _ensure_load_sampler()
    import app as main_app
    data_dir = main_app.CONFIG['data_dir']

    service = _service_info()
    storage_root = _statvfs_summary(data_dir)
    storage_rows = _storage_per_account(data_dir)
    host = {
        'load': _read_loadavg(),
        'mem': _read_meminfo(),
        'thermal_c': _read_thermal_max_c(),
        'uptime_s': _read_uptime_s(),
        'os': _read_os_release(),
        'diskstats': _read_diskstats_for(data_dir),
    }
    expiring, needs_reauth = _oauth_token_summary(data_dir)
    app_card = {
        'active_syncs': _active_syncs(),
        'oauth_expiring_soon': expiring,
        'needs_reauth': needs_reauth,
        'failure_rate': _recent_failure_rate(),
    }
    return render_template(
        'admin.html',
        username=session.get('username', ''),
        admin_group=_resolve_admin_group(),
        service=service,
        storage_root=storage_root,
        storage_rows=storage_rows,
        host=host,
        app_card=app_card,
    )


@admin_bp.route('/restart', methods=['POST'])
@requires_admin
def restart():
    """Touch the marker file — the systemd path unit picks it up and
    runs `systemctl restart mail-archiver` as root. Returns 202 Accepted
    immediately; the actual restart happens ~1-2s later.
    """
    try:
        os.makedirs('/run/mail-archiver', mode=0o770, exist_ok=True)
    except OSError:
        pass
    try:
        # Atomic-ish: open + write + close. Path unit fires on PathExists.
        with open(RESTART_REQUEST_MARKER, 'w') as f:
            f.write(f'{time.time()} requested_by={session.get("username","?")}\n')
        try:
            # Match tmpfiles.d perm so the mail-archiver-restart executor
            # (root) can read it; the WebUI (mail-archiver) just wrote it.
            os.chmod(RESTART_REQUEST_MARKER, 0o660)
        except OSError:
            pass
        return jsonify({
            'status': 'requested',
            'eta': '~2s',
            'marker': RESTART_REQUEST_MARKER,
        }), 202
    except OSError as e:
        return jsonify({'status': 'error', 'detail': str(e)}), 500


@admin_bp.route('/storage')
@requires_admin
def storage_detail():
    _ensure_load_sampler()
    import app as main_app
    data_dir = main_app.CONFIG['data_dir']
    # /proc/mounts parse — list filesystems > 1GB
    mounts = []
    try:
        with open('/proc/mounts') as f:
            for line in f:
                parts = line.split()
                if len(parts) < 3:
                    continue
                dev, mnt, fstype = parts[0], parts[1], parts[2]
                if fstype in ('proc', 'sysfs', 'devtmpfs', 'tmpfs', 'cgroup',
                              'cgroup2', 'fusectl', 'nsfs', 'securityfs',
                              'pstore', 'bpf', 'devpts', 'mqueue', 'debugfs',
                              'tracefs', 'ramfs', 'overlay', 'binfmt_misc',
                              'autofs', 'rpc_pipefs', 'configfs'):
                    continue
                info = _statvfs_summary(mnt)
                if info.get('total', 0) > 1024 * 1024 * 1024:
                    info['device'] = dev
                    info['fstype'] = fstype
                    mounts.append(info)
    except OSError:
        pass

    logs_size = 0
    for lp in ('/var/log/mail-archiver-sync.log',):
        try:
            logs_size += os.path.getsize(lp)
        except OSError:
            pass

    app_size = 0
    for root, _, files in os.walk('/opt/mail-archiver'):
        for name in files:
            try:
                app_size += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass

    return render_template(
        'admin_storage.html',
        username=session.get('username', ''),
        data_dir=data_dir,
        mounts=mounts,
        rows=_storage_per_account(data_dir),
        logs_size=logs_size,
        app_size=app_size,
    )


@admin_bp.route('/host')
@requires_admin
def host_detail():
    _ensure_load_sampler()
    import app as main_app
    data_dir = main_app.CONFIG['data_dir']
    interfaces = []
    try:
        with open('/proc/net/dev') as f:
            for line in f.readlines()[2:]:
                parts = line.split()
                if len(parts) < 10:
                    continue
                iface = parts[0].rstrip(':')
                if iface == 'lo':
                    continue
                interfaces.append({
                    'iface': iface,
                    'rx_bytes': int(parts[1]),
                    'tx_bytes': int(parts[9]),
                })
    except (OSError, ValueError, IndexError):
        pass

    thermal = []
    try:
        for p in sorted(Path('/sys/class/thermal').glob('thermal_zone*')):
            try:
                zone = p.name
                temp_raw = int((p / 'temp').read_text().strip())
                temp_c = temp_raw / 1000.0 if temp_raw > 1000 else float(temp_raw)
                zone_type = ''
                try:
                    zone_type = (p / 'type').read_text().strip()
                except OSError:
                    pass
                thermal.append({'zone': zone, 'type': zone_type, 'temp_c': temp_c})
            except (OSError, ValueError):
                continue
    except OSError:
        pass

    with _LOAD_HISTORY_LOCK:
        history = list(_LOAD_HISTORY)

    return render_template(
        'admin_host.html',
        username=session.get('username', ''),
        load=_read_loadavg(),
        mem=_read_meminfo(),
        uptime_s=_read_uptime_s(),
        os_release=_read_os_release(),
        diskstats=_read_diskstats_for(data_dir),
        interfaces=interfaces,
        thermal=thermal,
        load_history=history,
    )


@admin_bp.route('/app')
@requires_admin
def app_detail():
    import app as main_app
    data_dir = main_app.CONFIG['data_dir']

    expiring, needs_reauth = _oauth_token_summary(data_dir)
    active = _active_syncs()
    failure_rate = _recent_failure_rate()

    # Full sync-history table — walk each user's sync_status.json
    sync_status = []
    try:
        for user_dir in sorted(Path(data_dir).iterdir()):
            if not user_dir.is_dir():
                continue
            f = user_dir / '.config' / 'sync_status.json'
            if not f.exists():
                continue
            try:
                d = json.loads(f.read_text())
            except (OSError, ValueError):
                continue
            for email, st in d.items():
                sync_status.append({
                    'user': user_dir.name,
                    'email': email,
                    'state': st.get('state', ''),
                    'started': st.get('started', ''),
                    'finished': st.get('finished', ''),
                    'exit_code': st.get('exit_code', ''),
                    'error': (st.get('error') or '')[:200],
                })
    except OSError:
        pass

    return render_template(
        'admin_app.html',
        username=session.get('username', ''),
        expiring=expiring,
        needs_reauth=needs_reauth,
        active_syncs=active,
        failure_rate=failure_rate,
        sync_status=sync_status,
    )


@admin_bp.route('/oauth/<user>/<path:email>/refresh', methods=['POST'])
@requires_admin
def oauth_refresh(user, email):
    """Force-refresh the OAuth access token for a specific account."""
    import app as main_app
    try:
        # 1.0.16: _refresh_oauth_tokens_before_sync(username, account_email)
        # returns the per-email report dict from refresh_all_oauth_tokens().
        result = main_app._refresh_oauth_tokens_before_sync(user, email)
        # result is {email: {refreshed, needs_reauth, ...}} — grab the one row
        row = (result or {}).get(email, {})
        refreshed = bool(row.get('refreshed'))
        needs_reauth = bool(row.get('needs_reauth'))
        detail = row.get('reason') or row.get('detail') or ''
        return jsonify({'status': 'ok', 'refreshed': refreshed,
                        'needs_reauth': needs_reauth, 'detail': detail}), 200
    except Exception as e:
        return jsonify({'status': 'error', 'detail': str(e)}), 500


@admin_bp.route('/sync/<job_id>/cancel', methods=['POST'])
@requires_admin
def sync_cancel(job_id):
    """1.1.0: cancel via `systemctl stop mail-archiver-sync-<jobid>.scope`.
    Clean, auditable, no signal-guessing. For batched syncs (1.0.15+
    chunk loop) the scope hosts a placeholder while children run under
    the mail-archiver.service unit — in that case we fall back to
    os.killpg on the recorded mbsync_pid. Safe because 1.0.17 chunking
    makes cancel-and-resume painless.
    """
    import signal
    import sync_lifecycle as slc
    try:
        state = slc.read_state(job_id)
        if not state:
            return jsonify({'status': 'not_found'}), 404
        # Try clean systemd stop first — always safe if scope exists.
        stopped = slc.stop_scope(job_id)
        # Also write cancelled state so the SSE stream emits an error
        # event and the dashboard clears the progress panel.
        slc.update_state(job_id, state='cancelled',
                         finished=slc.iso_utc_now(),
                         error='cancelled by operator')
        # Batched-mode fallback: if the mbsync child is still running
        # under mail-archiver.service (not our scope), signal its pgid.
        pid = state.get('mbsync_pid')
        if pid:
            try:
                os.killpg(os.getpgid(int(pid)), signal.SIGTERM)
            except (OSError, ValueError):
                pass
        return jsonify({
            'status': 'ok',
            'scope_stopped': stopped,
            'pid': pid,
        }), 200
    except Exception as e:
        return jsonify({'status': 'error', 'detail': str(e)}), 500


# ---------------------------------------------------------------
# Registration hook
# ---------------------------------------------------------------

def register(app, jinja_env=None):
    """Attach the admin blueprint to the main Flask app. Also expose
    the is_admin check to Jinja so the dashboard can conditionally
    render the Admin link.
    """
    app.register_blueprint(admin_bp)

    def _tpl_is_admin():
        u = session.get('username')
        return bool(u and _is_admin(u))
    app.jinja_env.globals['is_admin'] = _tpl_is_admin
