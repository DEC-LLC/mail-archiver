"""Gunicorn configuration for Mail Archiver.

Serves HTTPS on port 8443 with self-signed or Let's Encrypt certs.
If no certs found, falls back to HTTP-only on port 8400.
"""

import os

cert_dir = os.environ.get('MAIL_ARCHIVER_CERT_DIR', '/opt/mail-archiver/certs')
cert_file = os.path.join(cert_dir, 'mail-archiver.crt')
key_file = os.path.join(cert_dir, 'mail-archiver.key')

# Port override: MAIL_ARCHIVER_PORT (default 8443 for HTTPS, 8400 for HTTP)
custom_port = os.environ.get('MAIL_ARCHIVER_PORT', '')

# HTTPS if certs exist, HTTP otherwise
if os.path.isfile(cert_file) and os.path.isfile(key_file):
    bind = f'0.0.0.0:{custom_port or "8443"}'
    certfile = cert_file
    keyfile = key_file
else:
    bind = f'0.0.0.0:{custom_port or "8400"}'
    certfile = None
    keyfile = None

workers = 2
timeout = 120
