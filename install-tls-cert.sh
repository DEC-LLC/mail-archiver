#!/bin/bash
# Discover and install a CA-issued TLS certificate for Mail Archiver,
# replacing the self-signed pair that generate-cert.sh creates at install.
#
# WHY THIS EXISTS
# ---------------
# A self-signed cert stops being click-through acceptable as soon as any
# other service on the same HOSTNAME sends an HSTS header: HSTS is scoped
# to the host and ignores the port, so a management UI on :443 pins the
# policy for :8443 too and browsers then refuse to offer an exception
# (Firefox: MOZILLA_PKIX_ERROR_SELF_SIGNED_CERT, no "add exception").
# Most hosts already have a real CA-issued cert somewhere; this finds it.
#
# HOST-AGNOSTIC BY DESIGN
# -----------------------
# Nothing here is tied to a particular NAS/distro. Resolution order:
#
#   1. $MAIL_ARCHIVER_TLS_CERT (+ $MAIL_ARCHIVER_TLS_KEY) — explicit,
#      always wins. This is the supported interface for any platform we
#      do not ship a default rule for.
#   2. Cert globs listed in /etc/mail-archiver/tls-sources.conf, one per
#      line — extend for a new platform without editing this script.
#   3. Built-in globs for common providers (certbot, generic Debian and
#      RHEL layouts, cockpit, OpenMediaVault) — all equal citizens.
#   4. Nothing suitable found: leave the existing cert alone.
#
# A candidate must be CA-issued (subject != issuer — swapping one
# self-signed cert for another gains nothing), currently valid, have a
# private key that actually matches, and carry this host's FQDN in its
# SAN. Newest issue date wins. The full chain is installed when present,
# because clients need the intermediate to build a path.
#
# Idempotent: safe to run on every service start (ExecStartPre), which is
# also how renewals get picked up.
#
# Usage: install-tls-cert.sh [CERT_DIR]

set -euo pipefail

CERT_DIR="${1:-/opt/mail-archiver/certs}"
DEST_CRT="$CERT_DIR/mail-archiver.crt"
DEST_KEY="$CERT_DIR/mail-archiver.key"
CONF="${MAIL_ARCHIVER_TLS_SOURCES:-/etc/mail-archiver/tls-sources.conf}"
FQDN="$(hostname -f 2>/dev/null || hostname)"
SHORT="${FQDN%%.*}"

log() { echo "install-tls-cert: $*"; }

# Built-in cert locations. Order does not imply priority — the newest
# valid, matching certificate wins regardless of which rule found it.
builtin_globs() {
    cat <<GLOBS
/etc/letsencrypt/live/*/fullchain.pem
/etc/ssl/certs/${FQDN}.crt
/etc/ssl/certs/${FQDN}.pem
/etc/ssl/certs/${SHORT}.crt
/etc/pki/tls/certs/${FQDN}.crt
/etc/pki/tls/certs/${SHORT}.crt
/etc/cockpit/ws-certs.d/*.cert
/etc/cockpit/ws-certs.d/*.crt
/etc/ssl/certs/openmediavault-*.crt
GLOBS
}

# Candidate private keys for a given certificate path: conventional names
# first, then a scan of the cert's own directory and the standard private
# directories. Scanning is safe because every candidate is confirmed by
# comparing public keys — a wrong file can never match.
key_candidates() {
    local crt="$1" dir base stem
    dir=$(dirname "$crt"); base=$(basename "$crt"); stem="${base%.*}"
    cat <<KEYS
$dir/privkey.pem
$dir/$stem.key
$dir/$stem-key.pem
$dir/$stem.cert.key
/etc/ssl/private/$stem.key
/etc/ssl/private/$stem.pem
/etc/pki/tls/private/$stem.key
KEYS
    local f
    shopt -s nullglob
    for f in "$dir"/*.key "$dir"/*.pem \
             /etc/ssl/private/*.key /etc/ssl/private/*.pem \
             /etc/pki/tls/private/*.key; do
        [ -f "$f" ] && echo "$f"
    done
    shopt -u nullglob
}

pubkey_of_cert() { openssl x509 -in "$1" -noout -pubkey 2>/dev/null; }
pubkey_of_key()  { openssl pkey -in "$1" -pubout 2>/dev/null; }

# Echoes "<startepoch> <cert> <key>" when the pair is usable, else nothing.
evaluate_pair() {
    local crt="$1" key="${2:-}"
    [ -r "$crt" ] || return 0

    local subject issuer
    subject=$(openssl x509 -in "$crt" -noout -subject 2>/dev/null) || return 0
    issuer=$(openssl x509 -in "$crt" -noout -issuer 2>/dev/null) || return 0
    [ "${subject#subject=}" = "${issuer#issuer=}" ] && return 0
    openssl x509 -in "$crt" -noout -checkend 86400 >/dev/null 2>&1 || return 0

    # Cert must actually be for this host.
    local san
    san=$(openssl x509 -in "$crt" -noout -ext subjectAltName 2>/dev/null || true)
    echo "$san" | grep -qF "DNS:$FQDN" || return 0

    local k
    if [ -n "$key" ]; then
        [ -r "$key" ] || return 0
        [ "$(pubkey_of_cert "$crt")" = "$(pubkey_of_key "$key")" ] || return 0
        k="$key"
    else
        k=""
        local cand
        while read -r cand; do
            [ -r "$cand" ] || continue
            if [ "$(pubkey_of_cert "$crt")" = "$(pubkey_of_key "$cand")" ]; then
                k="$cand"; break
            fi
        done < <(key_candidates "$crt")
        [ -n "$k" ] || return 0
    fi

    local start
    start=$(date -d "$(openssl x509 -in "$crt" -noout -startdate | cut -d= -f2)" +%s 2>/dev/null || echo 0)
    echo "$start $crt $k"
}

best_crt=""; best_key=""; best_start=-1

# 1. Explicit override wins outright.
if [ -n "${MAIL_ARCHIVER_TLS_CERT:-}" ]; then
    result=$(evaluate_pair "$MAIL_ARCHIVER_TLS_CERT" "${MAIL_ARCHIVER_TLS_KEY:-}")
    if [ -n "$result" ]; then
        best_start=${result%% *}; rest=${result#* }
        best_crt=${rest%% *}; best_key=${rest#* }
        log "using MAIL_ARCHIVER_TLS_CERT=$best_crt"
    else
        log "WARNING: MAIL_ARCHIVER_TLS_CERT=$MAIL_ARCHIVER_TLS_CERT is unusable"
        log "  (must be CA-issued, unexpired, key-matched, and cover $FQDN)"
    fi
fi

# 2 + 3. Config-file globs, then built-ins.
if [ -z "$best_crt" ]; then
    globlist=$(mktemp) || exit 0
    trap 'rm -f "$globlist"' EXIT
    { [ -r "$CONF" ] && grep -vE '^\s*(#|$)' "$CONF"; builtin_globs; } > "$globlist" || true

    shopt -s nullglob
    while read -r glob; do
        for crt in $glob; do
            result=$(evaluate_pair "$crt")
            [ -n "$result" ] || continue
            start=${result%% *}; rest=${result#* }
            if [ "$start" -gt "$best_start" ]; then
                best_start=$start; best_crt=${rest%% *}; best_key=${rest#* }
            fi
        done
    done < "$globlist"
    shopt -u nullglob
fi

if [ -z "$best_crt" ]; then
    log "no CA-issued certificate found for $FQDN — keeping existing cert"
    log "  set MAIL_ARCHIVER_TLS_CERT/_KEY, or add a glob to $CONF"
    exit 0
fi

if [ -f "$DEST_CRT" ] && cmp -s "$best_crt" "$DEST_CRT"; then
    log "already serving $best_crt — up to date"
    exit 0
fi

mkdir -p "$CERT_DIR"
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
log "installed $best_crt ($chain cert(s) in chain)"
log "  issuer:  $(openssl x509 -in "$DEST_CRT" -noout -issuer | cut -d= -f2-)"
log "  expires: $(openssl x509 -in "$DEST_CRT" -noout -enddate | cut -d= -f2)"
[ "$chain" -lt 2 ] && log "  NOTE: no intermediate in chain — if your CA uses one, clients may fail to build a path"
exit 0
