#!/bin/bash
# Install the host's CA-issued OpenMediaVault TLS certificate into the
# Mail Archiver cert directory, replacing the self-signed pair.
#
# WHY: mail-archiver ships a self-signed cert (generate-cert.sh). On an OMV
# NAS, OMV's own nginx already serves a certificate issued by the site CA.
# Browsers that have seen OMV's UI cache an HSTS policy for the HOSTNAME —
# HSTS is host-scoped and ignores the port — so once HSTS is pinned, the
# self-signed cert on :8443 can no longer be click-through accepted:
#   MOZILLA_PKIX_ERROR_SELF_SIGNED_CERT, with no "add exception" offered.
# Serving the same CA-issued chain on :8443 removes the problem properly.
#
# Idempotent and safe to run on every service start (ExecStartPre): it only
# copies when the source differs, and leaves the existing cert untouched if
# no suitable CA-issued OMV cert is found.
#
# Usage: install-omv-cert.sh [CERT_DIR]

set -euo pipefail

CERT_DIR="${1:-/opt/mail-archiver/certs}"
DEST_CRT="$CERT_DIR/mail-archiver.crt"
DEST_KEY="$CERT_DIR/mail-archiver.key"
FQDN="$(hostname -f 2>/dev/null || hostname)"

log() { echo "install-omv-cert: $*"; }

pubkey_of_cert() { openssl x509 -in "$1" -noout -pubkey 2>/dev/null; }
pubkey_of_key()  { openssl pkey -in "$1" -pubout 2>/dev/null; }

best_crt=""; best_key=""; best_start=0
shopt -s nullglob
for crt in /etc/ssl/certs/openmediavault-*.crt; do
    key="/etc/ssl/private/$(basename "${crt%.crt}").key"
    [ -r "$crt" ] && [ -r "$key" ] || continue

    subject=$(openssl x509 -in "$crt" -noout -subject 2>/dev/null) || continue
    issuer=$(openssl x509 -in "$crt" -noout -issuer 2>/dev/null) || continue
    # Skip self-signed — swapping one self-signed cert for another fixes nothing.
    [ "${subject#subject=}" = "${issuer#issuer=}" ] && continue
    # Must still be valid (allow 1 day of slack for clock skew).
    openssl x509 -in "$crt" -noout -checkend 86400 >/dev/null 2>&1 || continue
    # Key must actually match the certificate.
    [ "$(pubkey_of_cert "$crt")" = "$(pubkey_of_key "$key")" ] || continue
    # Prefer a cert that covers this host's FQDN.
    if ! openssl x509 -in "$crt" -noout -ext subjectAltName 2>/dev/null \
         | grep -qF "DNS:$FQDN"; then
        continue
    fi
    # Among candidates, take the most recently issued.
    start=$(date -d "$(openssl x509 -in "$crt" -noout -startdate | cut -d= -f2)" +%s 2>/dev/null || echo 0)
    if [ "$start" -ge "$best_start" ]; then
        best_start=$start; best_crt=$crt; best_key=$key
    fi
done
shopt -u nullglob

if [ -z "$best_crt" ]; then
    log "no valid CA-issued OMV certificate for $FQDN — leaving existing cert in place"
    exit 0
fi

# Already installed and current? Nothing to do.
if [ -f "$DEST_CRT" ] && cmp -s "$best_crt" "$DEST_CRT"; then
    log "already serving $(basename "$best_crt") — up to date"
    exit 0
fi

mkdir -p "$CERT_DIR"
# Keep one backup of whatever we are replacing (first time only).
if [ -f "$DEST_CRT" ] && [ ! -f "$DEST_CRT.self-signed.bak" ]; then
    cp -a "$DEST_CRT" "$DEST_CRT.self-signed.bak"
    [ -f "$DEST_KEY" ] && cp -a "$DEST_KEY" "$DEST_KEY.self-signed.bak"
    log "backed up previous cert to $(basename "$DEST_CRT").self-signed.bak"
fi

install -m 0644 "$best_crt" "$DEST_CRT"
install -m 0640 "$best_key" "$DEST_KEY"
if id -u mail-archiver >/dev/null 2>&1; then
    chown mail-archiver:mail-archiver "$DEST_CRT" "$DEST_KEY"
fi

chain=$(grep -c 'BEGIN CERTIFICATE' "$DEST_CRT" || true)
log "installed $(basename "$best_crt") ($chain cert(s) in chain)"
log "  issuer: $(openssl x509 -in "$DEST_CRT" -noout -issuer | cut -d= -f2-)"
log "  expires: $(openssl x509 -in "$DEST_CRT" -noout -enddate | cut -d= -f2)"
[ "$chain" -lt 2 ] && log "  WARNING: no intermediate in chain — clients may not build a path"
exit 0
