"""1.3.1: a logged-in user can open only messages under their OWN folder.

Up to 1.3.0 `/email/view` and `/email/attachment` accepted any file under
the data directory, so one user could read another user's mail by path.
"""

import os
import sys
import tempfile
import time
from pathlib import Path

_DATA = tempfile.mkdtemp(prefix='ma-test-data-')
os.environ.setdefault('MAIL_ARCHIVER_DATA', _DATA)
os.environ['MAIL_ARCHIVER_AUTH'] = 'builtin'
os.environ.setdefault('MAIL_ARCHIVER_SECRET', 'test-only-secret')
os.environ.pop('MAIL_ARCHIVER_SECRET_FILE', None)

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import app as ma  # noqa: E402

MESSAGE = (b"From: a@example.com\r\nTo: b@example.com\r\n"
           b"Subject: private-subject-4471\r\n"
           b"Message-ID: <own-folder-test@example.com>\r\n\r\nbody\r\n")


def _message(username):
    cur = Path(ma.CONFIG['data_dir']) / username / 'acct' / 'INBOX' / 'cur'
    cur.mkdir(parents=True, exist_ok=True)
    path = cur / '1700000000.1.abcdef'
    path.write_bytes(MESSAGE)
    return str(path)


def _client(username):
    ma.app.config['TESTING'] = True
    client = ma.app.test_client()
    with client.session_transaction() as sess:
        sess['username'] = username
        sess['login_time'] = time.time()
    return client


def test_own_message_opens():
    path = _message('viewowner')
    resp = _client('viewowner').get('/email/view', query_string={'path': path})
    assert resp.status_code == 200
    assert b'private-subject-4471' in resp.data


def test_another_users_message_is_refused():
    path = _message('viewowner')
    for route in ('/email/view', '/email/attachment'):
        resp = _client('viewintruder').get(route, query_string={'path': path, 'index': 0})
        assert resp.status_code == 403, route


def test_a_user_whose_name_prefixes_another_is_refused():
    # "viewowner" must not be reachable by a user called "view".
    path = _message('viewowner')
    resp = _client('view').get('/email/view', query_string={'path': path})
    assert resp.status_code == 403


def test_application_files_in_the_data_directory_are_refused():
    secret = Path(ma.CONFIG['data_dir']) / '.users.json'
    secret.write_text('{}', encoding='utf-8')
    resp = _client('viewowner').get('/email/view', query_string={'path': str(secret)})
    assert resp.status_code == 403


def test_search_export_leaves_out_another_users_file():
    """1.3.2: the export fence is the user's own folder too."""
    mine = _message('exportowner')
    theirs = _message('exportother')
    probe = [{'path': mine, 'filepath': mine, 'file': mine},
             {'path': theirs, 'filepath': theirs, 'file': theirs}]
    with ma.app.test_request_context('/search'):
        resp = ma._export_results(probe, 'mbox', 'q', 'exportowner')
        resp.direct_passthrough = False
        body = resp.get_data()
    assert body.count(b'Message-ID: <own-folder-test@example.com>') == 1
