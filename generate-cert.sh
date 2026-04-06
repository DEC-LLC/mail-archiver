#!/bin/bash
# Generate a self-signed TLS certificate for Mail Archiver.
# Called automatically by the RPM/DEB postinst if no cert exists.
# To replace with Let's Encrypt, see README.md.

set -euo pipefail

CERT_DIR="${1:-/opt/mail-archiver/certs}"
CERT_FILE="$CERT_DIR/mail-archiver.crt"
KEY_FILE="$CERT_DIR/mail-archiver.key"

# Don't overwrite existing certs (user may have installed Let's Encrypt)
if [ -f "$CERT_FILE" ] && [ -f "$KEY_FILE" ]; then
    echo "Certificates already exist at $CERT_DIR — skipping generation."
    exit 0
fi

mkdir -p "$CERT_DIR"

# Detect hostname for the certificate CN/SAN
HOSTNAME=$(hostname -f 2>/dev/null || hostname)

echo "Generating self-signed TLS certificate for $HOSTNAME..."

openssl req -x509 -newkey rsa:2048 -nodes \
    -keyout "$KEY_FILE" \
    -out "$CERT_FILE" \
    -days 3650 \
    -subj "/O=Mail Archiver/CN=$HOSTNAME" \
    -addext "subjectAltName=DNS:$HOSTNAME,DNS:localhost,IP:127.0.0.1" \
    2>/dev/null

chmod 640 "$KEY_FILE"
chmod 644 "$CERT_FILE"

# If mail-archiver user exists, set ownership
if id -u mail-archiver >/dev/null 2>&1; then
    chown mail-archiver:mail-archiver "$KEY_FILE" "$CERT_FILE"
fi

echo "Certificate generated:"
echo "  Cert: $CERT_FILE"
echo "  Key:  $KEY_FILE"
echo "  Valid for 10 years, self-signed."
echo "  To use Let's Encrypt instead, see: README.md → HTTPS section"
