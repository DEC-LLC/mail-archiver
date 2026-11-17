"""1.3.2: a "syncing" status entry that nothing is running reads as
"interrupted".

Seen on a long-running host: a user's `__all__` entry had said "syncing" for
two weeks. The job registry (sync_lifecycle) reaped the dead job, but
sync_status.json was never corrected.
"""

import json
import os
import sys
import tempfile
import time
from pathlib import Path

import pytest

_DATA = tempfile.mkdtemp(prefix='ma-test-data-')
os.environ.setdefault('MAIL_ARCHIVER_DATA', _DATA)
os.environ['MAIL_ARCHIVER_AUTH'] = 'builtin'
os.environ.setdefault('MAIL_ARCHIVER_SECRET', 'test-only-secret')
os.environ.pop('MAIL_ARCHIVER_SECRET_FILE', None)

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import app as ma                    # noqa: E402
import sync_lifecycle as slc        # noqa: E402

FMT = '%Y-%m-%d %H:%M:%S'


@pytest.fixture
def user(tmp_path, monkeypatch):
    monkeypatch.setitem(ma.CONFIG, 'data_dir', str(tmp_path / 'data'))
    monkeypatch.setattr(slc, 'JOB_STATE_DIR', str(tmp_path / 'jobs'))
    os.makedirs(tmp_path / 'jobs')
    name = 'staleuser'
    (tmp_path / 'data' / name / '.config').mkdir(parents=True)
    return name


def _write(user, status):
    f = Path(ma.CONFIG['data_dir']) / user / '.config' / 'sync_status.json'
    f.write_text(json.dumps(status), encoding='utf-8')
    return f


def _ago(seconds):
    return time.strftime(FMT, time.localtime(time.time() - seconds))


def _dead_pid():
    pid = 4_000_000
    while ma._pc.pid_alive(pid):
        pid += 1
    return pid


def test_stale_syncing_entry_cleared_on_read(user):
    f = _write(user, {
        '__all__': {'state': 'syncing', 'started': '2026-10-31 02:00:01'},
        'ok@example.org': {'state': 'ok', 'finished': _ago(60)},
    })
    status = ma.get_sync_status(user)
    assert status['__all__']['state'] == 'interrupted'
    assert status['__all__']['started'] == '2026-10-31 02:00:01'
    assert 'did not finish' in status['__all__']['error']
    assert status['ok@example.org']['state'] == 'ok'
    on_disk = json.loads(f.read_text(encoding='utf-8'))
    assert on_disk['__all__']['state'] == 'interrupted'     # corrected, not only shown


def test_entry_whose_writer_is_dead_is_cleared_at_once(user):
    _write(user, {'a@example.org': {'state': 'syncing', 'started': _ago(30),
                                    'pid': _dead_pid(),
                                    'boot_id': slc.current_boot_id()}})
    assert ma.get_sync_status(user)['a@example.org']['state'] == 'interrupted'


def test_entry_from_an_earlier_boot_is_cleared(user, monkeypatch):
    monkeypatch.setattr(slc, 'current_boot_id', lambda: 'boot-now')
    _write(user, {'a@example.org': {'state': 'syncing', 'started': _ago(30),
                                    'pid': os.getpid(), 'boot_id': 'boot-before'}})
    assert ma.get_sync_status(user)['a@example.org']['state'] == 'interrupted'


def test_running_sync_is_left_alone_live_writer(user):
    f = _write(user, {'a@example.org': {'state': 'syncing', 'started': _ago(7 * 3600),
                                        'pid': os.getpid(),
                                        'boot_id': slc.current_boot_id()}})
    before = f.read_text(encoding='utf-8')
    assert ma.get_sync_status(user)['a@example.org']['state'] == 'syncing'
    assert f.read_text(encoding='utf-8') == before


def test_running_sync_is_left_alone_registered_job(user):
    # An old-style entry (no pid), far past the age limit, but a registered
    # job for the same target is in flight.
    job_id, blocking = ma._new_sync_job(user, 'a@example.org', source='cron')
    assert job_id and blocking is None
    _write(user, {'a@example.org': {'state': 'syncing', 'started': _ago(9 * 3600)},
                  '__all__': {'state': 'syncing', 'started': _ago(9 * 3600)}})
    status = ma.get_sync_status(user)
    assert status['a@example.org']['state'] == 'syncing'
    assert status['__all__']['state'] == 'syncing'           # overlaps the running job
    slc.update_state(job_id, state='done')
    status = ma.get_sync_status(user)
    assert status['a@example.org']['state'] == 'interrupted'
    assert status['__all__']['state'] == 'interrupted'


def test_recent_old_style_entry_is_left_alone(user):
    _write(user, {'a@example.org': {'state': 'syncing', 'started': _ago(600)}})
    assert ma.get_sync_status(user)['a@example.org']['state'] == 'syncing'


def test_entry_rewritten_meanwhile_is_not_overwritten(user):
    stale = {'state': 'syncing', 'started': '2026-10-31 02:00:01'}
    f = _write(user, {'a@example.org': {'state': 'ok', 'finished': _ago(5)}})
    done = ma._replace_sync_status_entries(
        f, {'a@example.org': stale}, {'a@example.org': ma._interrupted_entry(stale)})
    assert done == []
    assert json.loads(f.read_text(encoding='utf-8'))['a@example.org']['state'] == 'ok'


def test_new_syncing_entries_record_their_owner(user, monkeypatch):
    monkeypatch.setattr(ma, '_refresh_oauth_tokens_before_sync', lambda *a, **k: {})
    monkeypatch.setattr(ma, '_has_mbsync', lambda: False)
    seen = {}

    def fake_imaplib(username, account_email, status, key, status_file):
        seen.update(json.loads(Path(status_file).read_text(encoding='utf-8'))[key])
        status[key] = {'state': 'ok', 'finished': time.strftime(FMT)}

    monkeypatch.setattr(ma, '_run_sync_imaplib', fake_imaplib)
    ma.run_sync(user, 'a@example.org')
    assert seen['state'] == 'syncing'
    assert seen['pid'] == os.getpid()


def test_service_start_heals_every_user(user):
    _write(user, {'__all__': {'state': 'syncing', 'started': '2026-10-31 02:00:01'}})
    other = Path(ma.CONFIG['data_dir']) / 'otheruser' / '.config'
    other.mkdir(parents=True)
    (other / 'sync_status.json').write_text(json.dumps(
        {'b@example.org': {'state': 'syncing', 'started': _ago(8 * 3600)},
         'c@example.org': {'state': 'error', 'finished': _ago(60), 'error': 'x'}}),
        encoding='utf-8')
    assert ma.heal_stale_sync_status_all() == 2
    assert ma.heal_stale_sync_status_all() == 0
    assert json.loads((other / 'sync_status.json').read_text(
        encoding='utf-8'))['c@example.org']['state'] == 'error'


def test_dashboard_shows_interrupted(user):
    src = (ROOT / 'templates' / 'dashboard.html').read_text(encoding='utf-8')
    assert "== 'interrupted' %}Interrupted" in src
