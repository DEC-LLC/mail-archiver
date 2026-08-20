"""Gunicorn configuration for Mail Archiver.

Serves HTTPS on port 8443 with self-signed or Let's Encrypt certs.
If no certs found, falls back to HTTP-only on port 8400.

Threads scale with the number of email accounts under MAIL_ARCHIVER_DATA:
each active SSE progress stream (1.0.14+) holds one thread for the whole
sync duration, so a busy family NAS with 10 accounts + 4 users watching
needs ~14 thread-slots. The formula is base + total_accounts, capped.
Env overrides: MAIL_ARCHIVER_WORKERS, MAIL_ARCHIVER_THREADS_BASE,
MAIL_ARCHIVER_THREADS_MAX.
"""

import os
import json
from pathlib import Path

cert_dir = os.environ.get('MAIL_ARCHIVER_CERT_DIR', '/opt/mail-archiver/certs')
cert_file = os.path.join(cert_dir, 'mail-archiver.crt')
key_file = os.path.join(cert_dir, 'mail-archiver.key')

custom_port = os.environ.get('MAIL_ARCHIVER_PORT', '')

if os.path.isfile(cert_file) and os.path.isfile(key_file):
    bind = f'0.0.0.0:{custom_port or "8443"}'
    certfile = cert_file
    keyfile = key_file
else:
    bind = f'0.0.0.0:{custom_port or "8400"}'
    certfile = None
    keyfile = None


def _total_accounts():
    """Count total email accounts across all users under MAIL_ARCHIVER_DATA.
    Best-effort — returns 0 on any error so workers still start."""
    data_dir = Path(os.environ.get('MAIL_ARCHIVER_DATA',
                                    '/var/lib/mail-archiver'))
    total = 0
    try:
        for user_dir in data_dir.iterdir():
            accts = user_dir / '.config' / 'accounts.json'
            if accts.exists():
                try:
                    total += len(json.loads(accts.read_text()))
                except Exception:
                    pass
    except Exception:
        pass
    return total


# 2 worker PROCESSES for crash isolation. Concurrent SSE + UI hits scale
# via THREADS per worker — the default sync worker class runs one
# connection at a time (kills SSE); gthread has a per-worker thread pool.
workers = int(os.environ.get('MAIL_ARCHIVER_WORKERS', 2))
worker_class = 'gthread'

_cap = int(os.environ.get('MAIL_ARCHIVER_THREADS_MAX', 24))
_base = int(os.environ.get('MAIL_ARCHIVER_THREADS_BASE', 4))
_accts = _total_accounts()
threads = max(_base, min(_cap, _base + _accts))

timeout = 3600  # match mbsync 1h ceiling

print(f'gunicorn: workers={workers} threads={threads} '
      f'(accounts={_accts}, base={_base}, cap={_cap})',
      flush=True)
