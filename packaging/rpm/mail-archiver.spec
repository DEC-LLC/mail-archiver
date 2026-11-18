Name:           mail-archiver
Version:        1.3.4
Release:        1%{?dist}
Summary:        Self-hosted email archive with full-text search
License:        Apache-2.0
URL:            https://github.com/DEC-LLC/mail-archiver
# `git archive` of the release commit plus BUILD_INFO: packaging/make-source.sh
Source0:        %{name}-%{version}.tar.gz
BuildArch:      noarch

# The package is files copied into place. Leave them byte for byte as the
# source has them (the deb ships the same files): no shebang rewriting.
%undefine __brp_mangle_shebangs

Requires:       python3-flask
Requires:       python3-gunicorn
Requires:       isync
Requires:       python3-pam
# The three scheduled jobs are a file in /etc/cron.d (1.3.3: was not required,
# so a minimal EL install had no scheduled sync).
Requires:       cronie
# 1.3.0: PST import (readpst; EPEL libpst)
Requires:       /usr/bin/readpst
Recommends:     zstd
# .msg import (msgconvert, perl-Email-Outlook-Message). Required since 1.3.1:
# as a weak dependency it was never pulled in by an upgrade.
Requires:       /usr/bin/msgconvert
# XOAUTH2 SASL plugin — Microsoft Exchange IMAP requires it. Rocky/RHEL
# gets it via EPEL as cyrus-sasl-xoauth2. Weak dep so install still
# proceeds on hosts without EPEL (personal accounts still work).
Recommends:     cyrus-sasl-xoauth2

%description
Mail Archiver is a self-hosted email archive supporting Gmail, iCloud,
and Outlook/Hotmail via IMAP. Features SQLite FTS5 search index,
Microsoft OAuth2, MBOX/EML export, scheduled sync, mail import (PST,
mbox, Maildir, eml/emlx, msg, zip/tar) from per-account import folders
with an import ledger, and a budgeted initial / rebase sync for very
large folders.

%prep
%setup -q

%build
# nothing to build

%install
mkdir -p %{buildroot}/opt/mail-archiver/templates
mkdir -p %{buildroot}/opt/mail-archiver/static
mkdir -p %{buildroot}/etc/systemd/system
mkdir -p %{buildroot}/etc/cron.d
mkdir -p %{buildroot}/usr/libexec
mkdir -p %{buildroot}/usr/lib/systemd/system
mkdir -p %{buildroot}/usr/lib/tmpfiles.d

# Application files
install -m 644 app.py %{buildroot}/opt/mail-archiver/app.py
install -m 644 search_index.py %{buildroot}/opt/mail-archiver/search_index.py
install -m 644 oauth2_microsoft.py %{buildroot}/opt/mail-archiver/oauth2_microsoft.py
install -m 644 imap_sync.py %{buildroot}/opt/mail-archiver/imap_sync.py
install -m 644 mail_batcher.py %{buildroot}/opt/mail-archiver/mail_batcher.py
install -m 644 admin.py %{buildroot}/opt/mail-archiver/admin.py
install -m 644 sync_lifecycle.py %{buildroot}/opt/mail-archiver/sync_lifecycle.py
install -m 644 pst_import.py %{buildroot}/opt/mail-archiver/pst_import.py
install -m 644 mail_import.py %{buildroot}/opt/mail-archiver/mail_import.py
install -m 644 bulk_sync.py %{buildroot}/opt/mail-archiver/bulk_sync.py
install -m 644 bulk_sync_routes.py %{buildroot}/opt/mail-archiver/bulk_sync_routes.py
install -m 644 layout.py %{buildroot}/opt/mail-archiver/layout.py
install -m 644 platform_compat.py %{buildroot}/opt/mail-archiver/platform_compat.py
# BUILD_INFO comes with the source (commit, date); the builder line is added here.
{ cat BUILD_INFO; echo "builder=$(. /etc/os-release 2>/dev/null; echo "${ID:-unknown}:${VERSION_ID%%%%.*}")"; } > %{buildroot}/opt/mail-archiver/BUILD_INFO
chmod 644 %{buildroot}/opt/mail-archiver/BUILD_INFO
install -m 644 gunicorn.conf.py %{buildroot}/opt/mail-archiver/gunicorn.conf.py
install -m 644 VERSION %{buildroot}/opt/mail-archiver/VERSION
install -m 755 generate-cert.sh %{buildroot}/opt/mail-archiver/generate-cert.sh
install -m 755 install-tls-cert.sh %{buildroot}/opt/mail-archiver/install-tls-cert.sh
install -m 644 tls-sources.conf.example %{buildroot}/opt/mail-archiver/tls-sources.conf.example
install -m 755 mail-archiver-cred %{buildroot}/usr/libexec/mail-archiver-cred
install -m 755 mail-archiver-scheduled-sync %{buildroot}/usr/libexec/mail-archiver-scheduled-sync
install -m 755 mail-archiver-fix-perms %{buildroot}/usr/libexec/mail-archiver-fix-perms
install -m 755 mail-archiver-oauth-refresh %{buildroot}/usr/libexec/mail-archiver-oauth-refresh
install -m 755 mail-archiver-env %{buildroot}/usr/libexec/mail-archiver-env

# 1.0.18: restart trigger — path+service pair. WebUI (as mail-archiver)
# touches /run/mail-archiver/restart-requested; path unit fires the
# service which runs `systemctl restart mail-archiver` as root.
install -m 644 packaging/systemd/mail-archiver-restart.path %{buildroot}/usr/lib/systemd/system/mail-archiver-restart.path
install -m 644 packaging/systemd/mail-archiver-restart.service %{buildroot}/usr/lib/systemd/system/mail-archiver-restart.service
# 1.1.0: parent slice for sync scopes + oauth-refresh timer/service
install -m 644 packaging/systemd/mail-archiver-syncs.slice %{buildroot}/usr/lib/systemd/system/mail-archiver-syncs.slice
install -m 644 packaging/systemd/mail-archiver-oauth-refresh.service %{buildroot}/usr/lib/systemd/system/mail-archiver-oauth-refresh.service
install -m 644 packaging/systemd/mail-archiver-oauth-refresh.timer %{buildroot}/usr/lib/systemd/system/mail-archiver-oauth-refresh.timer
install -m 644 packaging/tmpfiles.d/mail-archiver.conf %{buildroot}/usr/lib/tmpfiles.d/mail-archiver.conf

# Templates
for t in templates/*.html; do
    install -m 644 "$t" %{buildroot}/opt/mail-archiver/templates/
done

# Static files
for s in static/*.css static/*.svg; do
    install -m 644 "$s" %{buildroot}/opt/mail-archiver/static/
done

# systemd service
install -m 644 mail-archiver.service %{buildroot}/etc/systemd/system/mail-archiver.service
# EL has no group `shadow` (the unit asks for it so pam_unix can check
# passwords on Debian); with the line present the unit fails with 216/GROUP.
sed -i '/^SupplementaryGroups=shadow$/d' %{buildroot}/etc/systemd/system/mail-archiver.service

# cron job for scheduled sync (the same file the deb installs)
install -m 644 packaging/cron.d/mail-archiver %{buildroot}/etc/cron.d/mail-archiver

%files
%license LICENSE NOTICE
%dir /opt/mail-archiver
%dir /opt/mail-archiver/templates
%dir /opt/mail-archiver/static
/opt/mail-archiver/app.py
/opt/mail-archiver/search_index.py
/opt/mail-archiver/oauth2_microsoft.py
/opt/mail-archiver/imap_sync.py
/opt/mail-archiver/mail_batcher.py
/opt/mail-archiver/admin.py
/opt/mail-archiver/sync_lifecycle.py
/opt/mail-archiver/pst_import.py
/opt/mail-archiver/mail_import.py
/opt/mail-archiver/bulk_sync.py
/opt/mail-archiver/bulk_sync_routes.py
/opt/mail-archiver/layout.py
/opt/mail-archiver/platform_compat.py
/opt/mail-archiver/BUILD_INFO
/opt/mail-archiver/gunicorn.conf.py
/opt/mail-archiver/generate-cert.sh
/opt/mail-archiver/install-tls-cert.sh
/opt/mail-archiver/tls-sources.conf.example
/opt/mail-archiver/VERSION
/usr/libexec/mail-archiver-cred
/usr/libexec/mail-archiver-scheduled-sync
/usr/libexec/mail-archiver-oauth-refresh
/usr/libexec/mail-archiver-env
/usr/libexec/mail-archiver-fix-perms
/opt/mail-archiver/templates/*.html
/opt/mail-archiver/static/*.css
/opt/mail-archiver/static/*.svg
/etc/systemd/system/mail-archiver.service
/usr/lib/systemd/system/mail-archiver-restart.path
/usr/lib/systemd/system/mail-archiver-restart.service
/usr/lib/systemd/system/mail-archiver-syncs.slice
/usr/lib/systemd/system/mail-archiver-oauth-refresh.service
/usr/lib/systemd/system/mail-archiver-oauth-refresh.timer
/usr/lib/tmpfiles.d/mail-archiver.conf
%config(noreplace) /etc/cron.d/mail-archiver

%post
# Ensure service user + group exist BEFORE we touch anything
getent group mail-archiver >/dev/null || groupadd -r mail-archiver
getent passwd mail-archiver >/dev/null || \
    useradd -r -g mail-archiver -d /var/lib/mail-archiver -s /sbin/nologin \
            -c "Mail Archiver service" mail-archiver

# Generate secret key if not present — group-readable so mbsync PassCmd
# running AS the target user (who is in mail-archiver group per the loop
# below) can decrypt per-user IMAP credentials.
if [ ! -f /opt/mail-archiver/.secret_key ]; then
    python3 -c "import secrets; print(secrets.token_hex(32))" > /opt/mail-archiver/.secret_key
fi
chown root:mail-archiver /opt/mail-archiver/.secret_key
chmod 0640 /opt/mail-archiver/.secret_key

# Certs dir must be writable by service user — generate-cert.sh runs as
# mail-archiver, self-signs into here on first boot.
mkdir -p /opt/mail-archiver/certs
chown mail-archiver:mail-archiver /opt/mail-archiver/certs
chmod 0750 /opt/mail-archiver/certs

# 1.3.1: the application tree is root's, on every install and upgrade.
# dpkg/rpm do not reset the owner or mode of a directory that already
# exists, so a tree left by an old copy-the-files deploy stayed writable by
# an ordinary account (seen in 2026-11: /opt/mail-archiver owned by uid 1001,
# mode 775). The helper repairs owner and mode, prints each repair, and
# rebuilds __pycache__ as root.
if [ -x /usr/libexec/mail-archiver-fix-perms ]; then
    /usr/libexec/mail-archiver-fix-perms || true
fi

# Cron log preseed — cron runs as root, WebUI service runs as mail-archiver;
# 0640 root:mail-archiver keeps both able to append.
touch /var/log/mail-archiver-sync.log /var/log/mail-archiver-import.log /var/log/mail-archiver-bulk-sync.log
chown root:mail-archiver /var/log/mail-archiver-sync.log /var/log/mail-archiver-import.log /var/log/mail-archiver-bulk-sync.log
chmod 0640 /var/log/mail-archiver-sync.log /var/log/mail-archiver-import.log /var/log/mail-archiver-bulk-sync.log

# Default data root: the directory the service unit names (MAIL_ARCHIVER_DATA,
# ReadWritePaths). Until 1.3.2 another directory was created here and the
# service could not start on a fresh EL install.
mkdir -p /var/lib/mail-archiver

# Add all local human users to mail-archiver group so mbsync PassCmd can
# read the shared secret key. Additive — never removes memberships. Safe
# to re-run on upgrade.
for U in $(getent passwd | awk -F: '$3 >= 1000 && $3 < 60000 && $7 !~ /nologin|false/ {print $1}'); do
    id -nG "$U" 2>/dev/null | tr ' ' '\n' | grep -qx mail-archiver && continue
    usermod -aG mail-archiver "$U" 2>/dev/null || true
done

systemctl daemon-reload

# Regenerate ~/.mbsyncrc for every provisioned user under the effective
# MAIL_ARCHIVER_DATA path. Stale cached mbsyncrc files (PassCmd format
# churn across 1.0.10 / 1.0.13) silently break sync after upgrade —
# this closes the class of bug at install time.
EFFECTIVE_DATA=$(systemctl show mail-archiver -p Environment --value 2>/dev/null \
    | tr ' ' '\n' | grep '^MAIL_ARCHIVER_DATA=' | tail -1 | cut -d= -f2-)
[ -z "$EFFECTIVE_DATA" ] && EFFECTIVE_DATA=/var/lib/mail-archiver
if [ -d "$EFFECTIVE_DATA" ]; then
    for ACCT in "$EFFECTIVE_DATA"/*/.config/accounts.json; do
        [ -f "$ACCT" ] || continue
        USR=$(echo "$ACCT" | awk -F/ '{print $(NF-2)}')
        cd /opt/mail-archiver && MAIL_ARCHIVER_DATA="$EFFECTIVE_DATA" \
            MAIL_ARCHIVER_AUTH=pam \
            MAIL_ARCHIVER_SECRET_FILE=/opt/mail-archiver/.secret_key \
            python3 -c "import app; app.generate_mbsyncrc('$USR')" 2>/dev/null || true

        # 1.0.15: fix accounts.json + parent dir chain perms so the
        # gunicorn worker (running as mail-archiver) can descend to the
        # accounts.json for the thread autoscaler. ALL three need
        # mail-archiver group + traversal bits.
        CFG_DIR=$(dirname "$ACCT")
        USR_DIR=$(dirname "$CFG_DIR")
        chgrp mail-archiver "$ACCT" 2>/dev/null || true
        chmod 0640 "$ACCT" 2>/dev/null || true
        chgrp mail-archiver "$CFG_DIR" 2>/dev/null || true
        chmod 0710 "$CFG_DIR" 2>/dev/null || true
        chgrp mail-archiver "$USR_DIR" 2>/dev/null || true
        chmod 0710 "$USR_DIR" 2>/dev/null || true
    done
fi

# 1.0.16: OAuth token files + legacy .pass credential files must be
# readable+writable by BOTH the target Linux user (mbsync PassCmd cat)
# AND the mail-archiver group (the Flask process that auto-refreshes
# OAuth tokens BEFORE spawning mbsync). Pre-1.0.16 they landed as
# 0600 mail-archiver:mail-archiver — target user couldn't read, and
# refresh chain never ran. Fix: 0640 <user>:mail-archiver.
if [ -d "$EFFECTIVE_DATA" ]; then
    for U_DIR in "$EFFECTIVE_DATA"/*/; do
        [ -d "${U_DIR}.config" ] || continue
        USR=$(basename "$U_DIR")
        for f in "${U_DIR}.config/"*.oauth2.json \
                 "${U_DIR}.config/"*.token \
                 "${U_DIR}.config/"*.pass; do
            [ -f "$f" ] || continue
            chown "${USR}:mail-archiver" "$f" 2>/dev/null || \
                chgrp mail-archiver "$f" 2>/dev/null || true
            chmod 0640 "$f" 2>/dev/null || true
        done
    done
fi

# 1.0.17: deferred-restart when a sync is in flight. Install ALWAYS
# succeeds; restart is deferred until the operator does it manually
# (or the marker is cleared on next clean restart via ExecStartPost).
# The 1.0.16 deploy killed an in-flight 19k-message INBOX sync because
# postinst restarted the service mid-transfer — this closes that class
# of bug without ever refusing an install.
active_mbsync=0
if systemctl is-active --quiet mail-archiver; then
    mainpid=$(systemctl show mail-archiver -p MainPID --value)
    if [ -n "$mainpid" ] && [ "$mainpid" != "0" ]; then
        cgroup=$(sed -n '1s|.*:.*:||p' /proc/$mainpid/cgroup 2>/dev/null)
        if [ -n "$cgroup" ] && [ -d "/sys/fs/cgroup${cgroup}" ]; then
            for p in $(cat /sys/fs/cgroup${cgroup}/cgroup.procs 2>/dev/null); do
                [ -r /proc/$p/comm ] && \
                    [ "$(cat /proc/$p/comm 2>/dev/null)" = "mbsync" ] && \
                    active_mbsync=$((active_mbsync+1))
            done
        fi
    fi
fi

if [ "$active_mbsync" -gt 0 ]; then
    install -d /run/mail-archiver 2>/dev/null || mkdir -p /run/mail-archiver
    NEW_VER=$(rpm -q --qf '%{VERSION}-%{RELEASE}' mail-archiver 2>/dev/null || echo unknown)
    echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) $NEW_VER" \
        > /run/mail-archiver/restart-pending
    cat >&2 <<EOF
================================================================
 Mail Archiver upgrade installed — service NOT restarted.

 Reason: $active_mbsync mbsync process(es) currently syncing mail.
 A restart now would interrupt in-flight syncs mid-download.

 The new binaries are on disk. The running service is still on
 the previous version until you restart it.

 When your syncs complete, run:
   systemctl restart mail-archiver

 The WebUI is also showing a restart-pending banner.
================================================================
EOF
    logger -t mail-archiver -p daemon.warning \
        "Upgrade installed; restart deferred — $active_mbsync active mbsync process(es). Run 'systemctl restart mail-archiver' after syncs finish." 2>/dev/null || true
else
    systemctl daemon-reload
    if systemctl is-active --quiet mail-archiver; then
        systemctl restart mail-archiver 2>/dev/null || true
    fi
    rm -f /run/mail-archiver/restart-pending 2>/dev/null || true
fi

# 1.0.18: admin page + Restart button — set up group, runtime dir, and
# the packaged path/service pair that turns a WebUI touch of
# /run/mail-archiver/restart-requested into a root `systemctl restart`.
getent group mail-archiver-admins >/dev/null || \
    groupadd -r mail-archiver-admins 2>/dev/null || true
systemd-tmpfiles --create /usr/lib/tmpfiles.d/mail-archiver.conf 2>/dev/null || true
systemctl daemon-reload 2>/dev/null || true
systemctl enable mail-archiver-restart.path 2>/dev/null || true
systemctl start mail-archiver-restart.path 2>/dev/null || true

# 1.1.0: sync-scope parent slice + proactive OAuth-refresh timer
systemctl enable mail-archiver-syncs.slice 2>/dev/null || true
systemctl start mail-archiver-syncs.slice 2>/dev/null || true
# Switched on at the first installation only (1.3.4): an administrator who
# disabled the timer keeps it disabled across upgrades.
if [ "$1" -eq 1 ]; then
    systemctl enable mail-archiver-oauth-refresh.timer 2>/dev/null || true
    systemctl start mail-archiver-oauth-refresh.timer 2>/dev/null || true
fi
systemctl reset-failed mail-archiver-oauth-refresh.service 2>/dev/null || true

echo "Mail Archiver installed. Data directory: /var/lib/mail-archiver (to change it, use a drop-in: systemctl edit mail-archiver)."
echo "Then: systemctl enable --now mail-archiver"
echo ""
echo "Admin access: grant users membership in 'mail-archiver-admins':"
echo "  sudo usermod -aG mail-archiver-admins <username>"
echo "  (takes effect within a minute; no restart needed)"

%preun
if [ "$1" = "0" ]; then
    # Removal (not upgrade): stop and switch off every unit of the package.
    for u in mail-archiver.service mail-archiver-oauth-refresh.timer \
             mail-archiver-oauth-refresh.service mail-archiver-restart.path \
             mail-archiver-syncs.slice; do
        systemctl disable --now "$u" 2>/dev/null || true
    done
fi

%postun
if [ "$1" = "0" ]; then
    systemctl daemon-reload 2>/dev/null || true
    systemctl reset-failed 'mail-archiver*' 2>/dev/null || true
fi

%changelog
* Tue Nov 17 2026 Madhav Diwan <madhav@decllc.biz> - 1.3.4-1
- Fix: mail-archiver-oauth-refresh.service failed on every installation
  whose data directory is not the default. It now starts through
  /usr/libexec/mail-archiver-oauth-refresh, which takes the MAIL_ARCHIVER_*
  settings of mail-archiver.service; the unit no longer lists fixed
  writable paths (ProtectSystem=full).
- The refresh unit has CAP_CHOWN, CAP_DAC_OVERRIDE and CAP_FOWNER (to store a
  token in a user-owned directory with system accounts) and a read-only
  program directory. The timer is enabled at first installation only.
- Fix: both wrappers take the service's settings through the new
  /usr/libexec/mail-archiver-env, which undoes systemd's quoting (a value
  with a space or a quote was lost); they stop when the settings cannot be
  read. `mail-archiver-scheduled-sync sync-schedule` prints the plan.
- The refresh unit cannot read /etc/shadow or write to /root, cron spools or
  the package database. Removing the package stops and disables its units.
- Fix: install-tls-cert.sh no longer lets a passphrase-protected key file
  hide the matching key.
- Fix: the administrator Host page answered HTTP 500 once three load
  samples existed.
- Documentation: the backup command works on an installation without
  drop-ins.

* Mon Nov 16 2026 Madhav Diwan <madhav@decllc.biz> - 1.3.3-1
- Built from a source tarball (Source0); an SRPM is produced with every
  build. The cron file is the one the deb installs (comment wording only).
- Fix: search_index.py did not import on Python 3.9 (this platform), so
  search and mail import failed in the rpm from 1.3.0 to 1.3.2.
- The service starts on a fresh EL 9 install: /var/lib/mail-archiver is
  created, the unit no longer names the group shadow, cronie is required,
  and the CA bundle of the platform is found.
- Server-wide OAuth apps are written by administrators only; with built-in
  sign-in, administrators are the names in MAIL_ARCHIVER_ADMIN_USERS.

* Mon Nov 16 2026 Madhav Diwan <madhav@decllc.biz> - 1.3.2-1
- Security: the built-in IMAP client (hosts without isync) does STARTTLS for
  accounts set to it and never logs in over an unencrypted connection.
- Search export reads only files in the signed-in user's own folder.
- A "syncing" status with no sync running reads "interrupted"; corrected on
  read, on the hourly schedule and at service start (gunicorn post_worker_init).

* Sat Nov 14 2026 Madhav Diwan <madhav@decllc.biz> - 1.3.1-1
- Shared code base for the Windows application: POSIX-only calls go through
  platform_compat.py (new module, installed with the others). No change in
  behaviour on Linux.
- Security: /email/view and /email/attachment accept only paths inside the
  signed-in user's own folder (was: anywhere in the data directory).
- Security: the built-in IMAP client (imaplib) verifies the server
  certificate and host name; it checked neither before.
- imaplib sync: custom IMAP server host honoured; a refused sign-in is an
  error, not a clean sync.
- Every install and upgrade runs /usr/libexec/mail-archiver-fix-perms: the
  application tree, the helpers, the cron file and the units are set to
  root:root without group/other write, and __pycache__ is rebuilt by root.
- msgconvert (.msg import) is a hard requirement instead of a weak one.
* Sun Nov 08 2026 Madhav Diwan <madhav@decllc.biz> - 1.3.0-1
- PST import. Outlook .pst exports import into a chosen account of a user
  with readpst (pst-utils), keeping the PST folder tree as Maildir folders.
  Messages already in the account (Message-ID, else a content hash) are
  skipped, so a weekly full export adds only new mail. Imported mail is
  indexed and searchable. Conversion runs as the mailbox owner.
- Scheduled import from <data_dir>/<user>/_import/watch/ (producer contract:
  .incoming/ staging, <file>.pst.sha256 sidecar renamed in first, atomic
  rename of <name>-YYYYMMDD.pst). Sidecar verified, two-check stability,
  filename -> account mapping, per-user hourly/daily/weekly schedule run by
  a second line in /etc/cron.d/mail-archiver through the same wrapper.
- Imported PSTs move to _import/archive/ (zstd when installed). Retention:
  last 4 copies + newest of each of the 3 previous months; rotation stops
  while the latest import is not ok. Failed files go to _import/failed/.
- WebUI: Import PST page (status, history, settings, upload, retry).
* Fri Aug 21 2026 Madhav Diwan <madhav@decllc.biz> - 1.1.0-1
- Sync lifecycle is now a first-class citizen. Runtime-model change,
  not new user-facing features — dashboard/settings/OAuth flows are
  unchanged for the operator. Semver minor bump because internals
  moved substantially: the in-process _SYNC_JOBS dict is retired,
  every mbsync invocation runs as a transient systemd scope, per-job
  state is written to /run/mail-archiver/sync-jobs/<id>.json.
- Headline: systemd-scoped mbsync (new sync_lifecycle.py module +
  mail-archiver-syncs.slice unit). Each sync is
  `systemd-run --scope --unit=mail-archiver-sync-<id>.scope
  --slice=mail-archiver-syncs.slice --uid=<pam-uid> --gid=<pam-gid>
  -- mbsync -V <channel>`. Consequences worth naming: (1) mbsync
  SURVIVES gunicorn worker recycling (systemd owns the scope, not
  gunicorn); (2) cross-worker visibility — any worker can serve any
  job's SSE stream because state is on disk not in a per-process
  dict; (3) Cancel Sync = `systemctl stop <scope>` — clean and
  auditable, no signal-guessing; (4) the systemd-run --uid/--gid
  drop-priv is the recommended replacement for the 1.0.13
  subprocess(user=,group=)+Ambient CAP_SETUID dance (the caps
  themselves are kept for now — 1.1.1 can consider removing).
- File-backed job state at /run/mail-archiver/sync-jobs/<id>.json,
  atomically written every ~2s via tempfile+rename. Schema includes
  job_id / user / email / started / scope_unit / mbsync_pid / state
  / progress (all batch_*, folder_chunk_*, folders_done, msg_new_*
  fields) / exit_code / error / finished / last_updated. tmpfiles.d
  config packaged; ensure_state_dir() runtime fallback for dev.
- Cross-worker + logout/login SSE auto-reattach: dashboard route
  enumerates active jobs for the logged-in user and renders each
  account card with a `data-active-job` attribute; DOMContentLoaded
  JS opens EventSource for every such panel. Same event stream any
  worker can serve. New /api/sync/active endpoint returns the same
  list as JSON for JS callers that want to reattach on-demand.
- Folded 1.0.16 follow-up: proactive OAuth token refresh timer.
  mail-archiver-oauth-refresh.timer fires oneshot service every 10
  min (starting 5min after boot) — walks every *.oauth2.json, mints
  fresh access_tokens for anything expiring within 15 min. New
  `python3 oauth2_microsoft.py refresh-expiring` CLI entrypoint
  drives the walk. Kills first-sync-after-idle latency: the sync
  path no longer spends 200–500ms on the refresh POST because the
  token is always fresh from the last timer tick.
- Folded 1.0.17 follow-up: chunk-only progress mode. When the
  intra-folder chunk loop is running, mail_batcher marks the
  progress dict `chunk_mode=True`; app.py's _line_cb skips
  overwriting folder_msg_done from mbsync's C:/M: totals (they
  reset every chunk). Cleared when the chunk loop exits so post-run
  line_cb writes still work.
- Folded 1.0.18 follow-up: Storage card polish — client-side search
  (filter by email substring), sort-by (bytes-desc | email | user),
  top-20 cutoff with "Show all N accounts" toggle. Host card
  sparkline shows "Collecting samples… (up to 15min after service
  start)" placeholder when fewer than 3 samples in the ring.
- Cancel-sync flow rewritten: primary path is `systemctl stop
  mail-archiver-sync-<id>.scope`; fallback for batched-mode syncs
  (mbsync children live under mail-archiver.service, not the scope)
  is os.killpg on recorded mbsync_pid. State file flips to
  `cancelled` so the SSE stream emits a clean error event and the
  dashboard progress panel clears.
- Retired: _SYNC_JOBS dict + _SYNC_JOBS_LOCK removed from app.py.
  admin.py's _active_syncs() reads from sync_lifecycle.list_all_states()
  instead. No migration needed — restart-time state loss was never a
  regression from the 1.0.14 behavior, and existing on-disk state
  files land in the new format on first write after upgrade.

* Thu Aug 20 2026 Madhav Diwan <madhav@decllc.biz> - 1.0.18-1
- Admin page (/admin) — PAM-group-gated observability + service control.
  Four cards: Service (version, uptime, workers, threads, restart-pending
  banner, Restart Now button), Storage (statvfs on data root with
  20%/5% warning bands + per-user/per-account du breakdown cached 5min
  with async computation), Host (loadavg / meminfo / diskstats /
  thermal / uptime / OS release), App (active syncs from _SYNC_JOBS,
  OAuth tokens expiring <24h, accounts flagged needs_reauth, recent
  sync-failure rate from the sync log tail). Each card links to a
  detail page: /admin/storage (per-mount df + log/app footprint),
  /admin/host (network interfaces, thermal zones, 15-min load
  sparkline sampled 30s), /admin/app (full sync history table + Force
  Refresh Now button per OAuth account + Cancel Sync button per
  active job — safe because 1.0.17 chunking makes cancel-and-resume
  painless).
- Restart Now mechanism: WebUI touches /run/mail-archiver/restart-requested
  as the mail-archiver user. New mail-archiver-restart.path unit
  (PathExists) fires mail-archiver-restart.service (Type=oneshot, root)
  which rm's the marker and runs `systemctl restart mail-archiver`.
  Preserves the "WebUI has zero direct systemctl privileges" property
  — the WebUI can only write one path; only root can execute the
  restart. Journal has both mail-archiver-restart.service and
  mail-archiver.service entries for a clean audit trail. Packaged units
  at /usr/lib/systemd/system/; enabled by postinst.
- Admin gate: MAIL_ARCHIVER_ADMIN_GROUP env (default 'mail-archiver-admins',
  auto-created r by postinst — never adds users, operator decision).
  Fallback to sudo/wheel if the configured group doesn't exist. Check
  cached per-username 60s. Non-admin users get 403 with a usermod
  hint. Dashboard header conditionally shows the Admin link only for
  admins (jinja global 'is_admin').
- tmpfiles.d config packaged (/usr/lib/tmpfiles.d/mail-archiver.conf)
  creates /run/mail-archiver 0770 root:mail-archiver on boot AND at
  install time (postinst runs systemd-tmpfiles --create).

* Thu Aug 20 2026 Madhav Diwan <madhav@decllc.biz> - 1.0.17-1
- Intra-folder chunked sync (mail_batcher.py): huge folders (default:
  > MAIL_ARCHIVER_CHUNK_THRESHOLD = 2500 msgs) now sync in
  MAIL_ARCHIVER_CHUNK_WALL_SECONDS chunks (default 180s each). Each
  chunk = fresh mbsync = fresh IMAP + TLS + reset server-side idle
  timer. mbsync's native maildir state means resume-mid-folder is
  automatic (atomic per-message commit). Aborts after 2 consecutive
  no-progress chunks. Fixes the case where a single 19,971-message
  INBOX would fail as one long mbsync connection.
- SSE progress dict + dashboard renderProgress extended with
  folder_chunk_current / folder_chunk_wall_seconds /
  folder_msg_done / folder_msg_total — chunk-level counters win
  over the coarser C:/B:/M: parse when present.
- Postinst defers service restart when sync is active: upgrade
  always installs cleanly; if mbsync is running under the mail-archiver
  cgroup, /run/mail-archiver/restart-pending is dropped, journal logs
  a warning, WebUI shows an amber "restart pending" banner. Operator
  runs `systemctl restart mail-archiver` after syncs complete —
  ExecStartPost=-/bin/rm auto-clears the marker on restart. Fixes the
  1.0.16 deploy that killed a mid-transfer INBOX sync (SIGTERM'd the
  whole cgroup).

* Thu Aug 20 2026 Madhav Diwan <madhav@decllc.biz> - 1.0.16-1
- P0 FIX (OAuth auto-refresh): Every OAuth account failed to sync ~1h
  after the user clicked "Sign in with Microsoft", because run_sync
  fired mbsync with the stale access_token cached on disk at authorize
  time (Microsoft MSA/AAD access tokens live 60 min). Users would have
  had to click Sign in every hour — untenable for a background archive.
  Fix: _refresh_oauth_tokens_before_sync runs INSIDE the Flask process
  BEFORE every mbsync spawn (both run_sync and _run_sync_background /
  SSE path). Uses the 90-day-lived refresh_token to mint fresh access
  tokens; refresh_all_oauth_tokens in oauth2_microsoft persists them
  atomically (tempfile+rename) with fcntl.flock across the read-check-
  refresh-write critical section, so parallel mbsync spawns can't
  clobber the rotated refresh_token. Guarantees offline_access in the
  refresh scope even when the stored scope omits it (else Microsoft
  returns an access_token but no refresh_token → chain silently breaks
  after one more hour). Perms preserved: 0640 <user>:mail-archiver so
  both mbsync (as target user) can `cat .token` and gunicorn (as
  mail-archiver) can write refreshed values.
- Refresh-token-invalid handling: On HTTP 400 error=invalid_grant
  (refresh_token >90d old, revoked, or client_secret rotated), the
  account's .oauth2.json gets needs_reauth=true, sync_status.json
  surfaces "Microsoft sign-in expired. Click 'Sign in with Microsoft'
  on the account settings page to renew." and the endpoint stops
  being hammered until the user re-authorizes. New TokenNeedsReauth
  exception + mark_token_needs_reauth helper in oauth2_microsoft.
- Postinst: OAuth token files (*.oauth2.json, *.token) and legacy
  *.pass credential files now chowned <user>:mail-archiver 0640 on
  upgrade so the target user (mbsync PassCmd `cat`) AND the
  mail-archiver group (Flask refresh writer + batcher measurement
  subprocess) can both read+write. Closes the 1.0.15 follow-up about
  legacy .pass files being 0600 mail-archiver:mail-archiver.

* Thu Aug 20 2026 Madhav Diwan <madhav@decllc.biz> - 1.0.15-1
- Batched sync for large accounts. New env MAIL_ARCHIVER_BATCH_THRESHOLD
  (default 5000). Before invoking mbsync, open ONE cheap IMAP connection
  and STATUS every folder. When the total exceeds threshold, pack folders
  smallest-first into batches <= threshold and run mbsync once per batch
  with a temp mbsyncrc that overrides Patterns to that batch's folders.
  Fresh mbsync per batch = fresh IMAP connection + fresh TLS session,
  which eliminates the class of "3-hour sync fails at 90%" server-side
  timeout errors. Failed batches retry once, then continue to the next
  batch — partial progress > total failure (exit code 2 signals partial
  success). Accounts smaller than the threshold take the single-mbsync
  fast path unchanged. New module mail_batcher.py; plan_batches() is a
  pure function for unit testing.
- gunicorn.conf.py: print(..., flush=True) so the resolved worker/thread
  count line appears in journalctl at boot (silent buffering bug in
  1.0.14 hid the autoscaler decision from operators).
- Postinst fixes accounts.json perms to 0640 <user>:mail-archiver + parent
  .config dir to 0710 <user>:mail-archiver, so the gunicorn worker
  (running as mail-archiver) can descend into ~/.config and enumerate
  the account list for the thread autoscaler. New accounts written by
  save_accounts() also get 0640 g+r from the start.
- Fix Outlook/Hotmail personal-account IMAP host to outlook.office.com
  (was outlook.office365.com — mismatched OAuth token audience caused
  AUTHENTICATIONFAILED for personal MSA accounts). Aligns with the
  1.0.9 OAuth scope alignment. Postinst regenerates all mbsyncrc files,
  propagating the corrected host on upgrade.

* Thu Aug 20 2026 Madhav Diwan <madhav@decllc.biz> - 1.0.14-1
- SSE progress stream: Sync Now + Sync All are now fire-and-poll —
  POST returns 202 with a job_id, and the browser opens an EventSource
  to /api/sync/<job_id>/stream which emits parsed mbsync progress
  (folders_done/total, msg_new_done/total) as `progress` events and
  a final `done`/`error` event. mbsync stderr is verbose-mode piped
  through subprocess.Popen and parsed via the canonical C: B: M:
  format regex. Heartbeat every 5s. Dashboard grew inline per-account
  progress bar + refresh link. Cron path (`python3 app.py
  scheduled-sync`) stays synchronous — cron doesn't need SSE.
- gunicorn worker class → gthread with account-scaled threads.
  Formula: max(base, min(cap, base + total_accounts)) where base=4,
  cap=24. Env overrides MAIL_ARCHIVER_WORKERS,
  MAIL_ARCHIVER_THREADS_BASE, MAIL_ARCHIVER_THREADS_MAX. Rationale:
  each concurrent SSE stream holds one thread for the sync duration;
  each UI page load uses one for <1s. 10 accounts + 4 users clicking
  around → 14 thread slots wanted; formula gives 14. Startup log
  emits `gunicorn: workers=N threads=M (accounts=A, base=B, cap=C)`
  so operators can see the resolved value in journalctl.
- Add cyrus-sasl-xoauth2 as a Recommends dep (Rocky/EPEL). Microsoft
  Exchange IMAP requires XOAUTH2 SASL mechanism; without the plugin
  mbsync fails with "selected: XOAUTH2 available: SCRAM/PLAIN/...".
  DEB counterpart is libsasl2-modules-kdexoauth2 (already added to
  Depends in DEBIAN/control this release).
- Postinst regenerates ~/.mbsyncrc for every user with an existing
  accounts.json under the effective MAIL_ARCHIVER_DATA. Kills the
  class of "stale cached mbsyncrc silently breaks sync after
  upgrade" bugs (bit us with 1.0.10 helper switch and 1.0.13
  subprocess switch — users only saw sync failures next time they
  clicked Sync Now, hours or days later). Also restarts the service
  when already active so new gunicorn config + code take effect.

* Thu Aug 20 2026 Madhav Diwan <madhav@decllc.biz> - 1.0.13-1
- Kernel-level setuid via subprocess kwargs — no more su/runuser wrapper.
  Ends the ping-pong (1.0.5 su→runuser fallback, 1.0.12 fixed runuser
  args) by doing what the original QA report actually recommended:
  subprocess.run(user=uid, group=gid, cwd=home, env=…) drops privs
  via the kernel using our ambient CAP_SETUID+CAP_SETGID. Portable —
  runuser requires root on Debian (only setuid on Rocky/RHEL), su
  prompts for password on non-root callers, both hit today.
- Unit change (required companion to the code fix): promote CAP_SETUID
  and CAP_SETGID from CapabilityBoundingSet-only to AmbientCapabilities
  so systemd puts them in the Effective set for the mail-archiver
  process. Without this, subprocess(user=…) raises PermissionError
  because CapEff lacks SETUID. Verified live-current unit had only
  4 caps in CapEff (0x0f) — adds 2 more (0xcf).
- New sync env: pass a minimal explicit env {HOME, USER, LOGNAME,
  SHELL, PATH} to mbsync when running as target user — otherwise
  inheritance would leak HOME=/var/lib/mail-archiver, breaking the
  PassCmd helper's per-user .oauth2 token lookup.

* Thu Aug 20 2026 Madhav Diwan <madhav@decllc.biz> - 1.0.12-1
- HOTFIX: `runuser -u <user> -s /bin/sh -- mbsync ...` errors with
  "options --{shell,fast,command,session-command,login} and --user
  are mutually exclusive". runuser(1) forbids combining -u/--user
  with -s/--shell (unlike su). With -u, args after `--` are exec'd
  directly with no shell — no -s needed. Drop -s /bin/sh from the
  runuser branch; leave the su fallback branch untouched (su uses
  -s to force /bin/sh over the target user's login shell). Also
  removes any residual shlex concern about mbsync_arg since it's
  now a distinct argv element, not a shell string.

* Thu Aug 20 2026 Madhav Diwan <madhav@decllc.biz> - 1.0.11-1
- HOTFIX: NameError: 'shutil' is not defined in run_sync at the runuser
  vs su fallback check (line ~710). Latent since 1.0.5 — the shutil
  module was only imported inside _has_mbsync, but the 1.0.5 change
  to swap `su` for `runuser` added a `shutil.which('runuser')` call
  at module scope in run_sync that assumed a top-level import.
  Every account, every provider, every sync attempt returned 500
  the moment the user actually clicked Sync Now (all sync attempts
  before today were blocked upstream by PAM login-loop or the OAuth
  scope error, so nothing ever reached run_sync). Add `import shutil`
  at module top; remove the now-redundant inline import from
  _has_mbsync. Zero behavioral change beyond letting sync run.

* Thu Aug 20 2026 Madhav Diwan <madhav@decllc.biz> - 1.0.10-1
- Add-page OAuth picker (Item 1): /account/add now shows the same
  OAuth-app dropdown that was previously only on /account/<email>/edit,
  so onboarding a Hotmail account is one form (pick app + email + save)
  instead of add → dashboard → Settings → pick → back → Sign in. JS
  toggles the picker's visibility to match the provider's auth type.
- imap_sync.py chown-on-write (T5 deferred from 1.0.5): ImapSyncer now
  accepts chown_uid/chown_gid kwargs and best-effort chowns every
  Maildir dir + message file + .synced_uids.json after write. app.py
  resolves the target PAM user via pwd.getpwnam in _run_sync_imaplib
  and threads uid/gid through. Prevents dual-uid corruption when
  mbsync is later swapped onto the same Maildir tree.
- PassCmd hardening (T8 deferred from 1.0.5): new
  /usr/libexec/mail-archiver-cred helper — fixed sys.path
  (/opt/mail-archiver, no traversal to writable dirs), argv-based
  args (no shell interpolation of user data). generate_mbsyncrc now
  emits PassCmd "/usr/libexec/mail-archiver-cred <shlex-quoted user>
  <shlex-quoted email>" instead of the old inlined python3 -c form.
  Closes two real vulnerabilities: (a) PAM user drops app.py under
  ${DATA} and it gets imported during every mbsync sync, (b) LDAP
  username with apostrophe or shell metacharacters injects into
  mbsync's /bin/sh -c context.
- sync_status.json flock (T11 deferred from 1.0.5): cron (root) and
  WebUI (mail-archiver) writes now flock a sibling .lock + atomic
  os.replace via tempfile-rename. Both directions of the race are
  eliminated; degraded non-locked fallback preserved for filesystems
  that don't support flock. New _write_sync_status_locked and
  _read_sync_status_locked helpers.
- Package: ships mail-archiver-cred at /usr/libexec/mail-archiver-cred
  (mode 0755 root:root) in both RPM and DEB. build.sh + spec + DEB
  layout updated to include the helper. No new runtime deps.

* Thu Aug 20 2026 Madhav Diwan <madhav@decllc.biz> - 1.0.9-1
- HOTFIX (OAuth completion blocker for personal Microsoft accounts):
  Change IMAP scope from https://outlook.office365.com/... to
  https://outlook.office.com/... (drop the "365"). The .office365.com
  variant is work/school-tenant-only; Azure rejects it with
  "The provided resource value for the input parameter 'scope' is not
  valid" for any personal MSA (hotmail.com/outlook.com/live.com).
  The .office.com variant is the unified endpoint accepted by both
  personal + work/school flows. Blocks OAuth token exchange until fixed
  — no accounts have ever successfully completed the flow with the old
  scope (no installation had any .oauth2.json / .token file despite
  multiple sign-in attempts since the OAuth flow was wired 4+ months ago).

* Thu Aug 20 2026 Madhav Diwan <madhav@decllc.biz> - 1.0.8-1
- OAuth Apps management: registered Azure apps are now named + reusable
  at two scopes (server-wide + per-user), with per-provider defaults and
  per-account pinning. Solves the "different Azure app per user
  / per tenant / per email account" case.
- Data model v2: server config at ${DATA}/.oauth2_config.json becomes
  {apps: {srv-*: {...}}, defaults: {microsoft: srv-...}, _schema_version: 2}
  Per-user config at ${DATA}/${user}/.config/oauth_apps.json (same shape,
  usr-* prefix). Auto-migrates v1 legacy {microsoft: {client_id, secret}}
  to a named app srv-microsoft-default on first read, save-back to disk.
  Existing accounts (no oauth_app_id) resolve via the server default —
  seamless upgrade for accounts added before this release.
- Server Settings page rewritten: two-table view (Server-wide + Your
  Private) with redacted secret hints, Make-default / Edit / Delete
  buttons per row. Delete refuses if any account still pins that app.
- Per-account edit page: OAuth section grows "OAuth App" dropdown
  listing every visible app (scope-labeled, default-marked, tenant-
  visible). Blank = provider default (user default > server default).
  Currently-pinned app shown redacted above the picker.
- Extended MicrosoftOAuth2 to accept per-app tenant (defaults 'common');
  AUTHORITY URL derived from tenant so single-tenant apps + verified-
  domain apps work without touching source.
- oauth2_authorize / callback / refresh + the imaplib sync path all
  route through resolve_oauth_app() so per-account overrides take
  effect end-to-end. Callback pins the app_id it started with so
  operator races on the settings page don't corrupt token exchange.
- Delete-app safety: server-scope deletes walk every user's
  accounts.json to find pinned references, refuse with a preview list.
- Backward compat verified with an in-repo migration smoke test.

* Thu Aug 20 2026 Madhav Diwan <madhav@decllc.biz> - 1.0.7-1
- Settings pages now show current configuration state — was previously
  silent overwrite. Every place where a credential/secret/cert can be
  saved now shows what's already there (redacted) so operator sees
  BEFORE typing whether they're replacing an existing value.
- Per-account edit page:
    * Authentication section (OAuth): shows access-token head…tail,
      length, expiry (relative + absolute), refresh-token presence,
      .token file presence, scope. Warns loudly if refresh token or
      .token file is missing (silent-sync-failure classes).
    * Authentication section (password): shows saved-credential
      head…tail + length + set-time. "Enter" vs "Replace" label on
      the input.
    * Client cert section: shows filename + size + sha256 head + upload
      time for cert and key separately. Flags a red state if the DB
      entry points at a file that's no longer on disk.
- Server Settings (OAuth2) page: shows redacted client_secret hint
  (head…tail + length) instead of just a generic "saved" indicator.
  Warns explicitly when not configured.
- FIX (behavior, not just UX): /oauth2/settings POST used to require
  BOTH client_id AND client_secret to be non-empty on every submit —
  the "leave blank to keep" hint on the form was a lie, and you
  couldn't update the client_id without re-typing the secret. Now
  correctly preserves the existing secret when the field is left blank.
- Redaction rules: strings ≤ 10 chars collapse to bullet-string of the
  actual length (never leak head/tail on short secrets). Longer strings
  show first4…last4. Applies to all credential/token/secret displays.
- Cert integrity display uses first 12 chars of SHA-256 — enough for
  "same file as before" verification without leaking key material.

* Thu Aug 20 2026 Madhav Diwan <madhav@decllc.biz> - 1.0.6-1
- Per-account Settings page (GH-style /account/<email>/edit) reached
  from a Settings button on every dashboard row. Consolidates every
  configuration knob for an account into one page — was previously
  spread across "Update Key", "Sign in with Microsoft", and (for
  everything else) not editable at all.
- Editable per-account fields: display name, IMAP host, port, TLS
  mode (SSL/STARTTLS/none), folder pattern (mbsync Patterns), and
  connection timeout. All override the PROVIDERS[] defaults so an
  operator can switch a Gmail account to a paid Exchange host, run
  STARTTLS on 143, or restrict sync to INBOX only.
- Client certificate upload (mutual-TLS IMAP): per-account PEM cert
  + key upload widget. Stored under ${DATA}/${user}/.config/,
  chowned to the target PAM user, mbsyncrc generator wires them
  into ClientCertificate / ClientKey directives. Removal button
  wipes both files atomically.
- Server Settings link now appears in the dashboard header
  (opens /oauth2/settings for the site-wide Microsoft OAuth2 client
  credentials). Previously reachable only by typing the URL.
- generate_mbsyncrc extended to honor all new per-account fields
  with backward-compatible fallbacks (missing field → PROVIDERS
  default → mbsync default).
- Upload cap: MAX_CONTENT_LENGTH set to 256 KiB; friendly 413
  handler redirects with a hint instead of Flask's default page.

* Thu Aug 20 2026 Madhav Diwan <madhav@decllc.biz> - 1.0.5-1
- Fix WebUI-triggered sync failing with "su: Authentication failure" —
  swap `su - <user>` → `runuser -u <user>` so the WebUI's mail-archiver
  service context (non-root, CAP_SETUID granted) can spawn per-user
  mbsync without a password prompt (QA finding T10).
- Fix PAM login-loop on Rocky/RHEL: python3-pam ships lowercase `pam`
  there, uppercase `PAM` on Debian. Import shim tries both (T1).
- Fix gunicorn worker SIGKILL during large first-time syncs: raise
  worker timeout 120s → 3600s to match mbsync subprocess timeout (T2).
- Fix Microsoft OAuth2 token files unreadable by mbsync PassCmd:
  chown token + config dir to the target PAM user in save_oauth2_tokens
  (T4). Unblocks Hotmail account sync.
- UX: /account/add auto-redirects to Microsoft OAuth consent for
  oauth2 providers (was: silent redirect to dashboard leaving user
  with no way to complete auth).
- UX: Dashboard shows "Sign in with Microsoft" button for OAuth
  accounts instead of the credential-update input (which never
  applied to OAuth). Password-provider accounts now correctly label
  the update button per provider (App Password, App-Specific
  Password, etc.).
- Package: postinst chowns secret_key to root:mail-archiver 0640 and
  adds all local human users to mail-archiver group so mbsync
  PassCmd-as-user can decrypt per-user IMAP creds. Certs dir + cron
  log preseeded with correct ownership.
- Require python3-pam.

* Mon Apr 06 2026 Madhav Diwan <madhav@decllc.biz> - 1.0.0-1
- Initial package: Flask web UI + FTS5 search + OAuth2 + export
- 36K+ emails tested, 80ms query time
- Security: XSS protection, command injection fix, path traversal guard
