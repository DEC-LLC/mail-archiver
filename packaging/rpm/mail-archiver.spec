Name:           mail-archiver
Version:        1.0.6
Release:        1%{?dist}
Summary:        Self-hosted email archive with full-text search
License:        Apache-2.0
URL:            https://dec-llc.biz
BuildArch:      noarch

Requires:       python3-flask
Requires:       python3-gunicorn
Requires:       isync
Requires:       python3-pam

%description
Mail Archiver is a self-hosted email archive with full-text search.
Supports Gmail, iCloud, and Outlook/Hotmail via IMAP. Features SQLite
FTS5 search index, Microsoft OAuth2, export as MBOX/EML, and scheduled
sync with per-account intervals.

%install
mkdir -p %{buildroot}/opt/mail-archiver/templates
mkdir -p %{buildroot}/opt/mail-archiver/static
mkdir -p %{buildroot}/etc/systemd/system
mkdir -p %{buildroot}/etc/cron.d

# Application files
install -m 644 %{_sourcedir}/app.py %{buildroot}/opt/mail-archiver/app.py
install -m 644 %{_sourcedir}/search_index.py %{buildroot}/opt/mail-archiver/search_index.py
install -m 644 %{_sourcedir}/oauth2_microsoft.py %{buildroot}/opt/mail-archiver/oauth2_microsoft.py
[ -f %{_sourcedir}/imap_sync.py ] && install -m 644 %{_sourcedir}/imap_sync.py %{buildroot}/opt/mail-archiver/imap_sync.py || true
install -m 644 %{_sourcedir}/gunicorn.conf.py %{buildroot}/opt/mail-archiver/gunicorn.conf.py
install -m 755 %{_sourcedir}/generate-cert.sh %{buildroot}/opt/mail-archiver/generate-cert.sh

# Templates
for t in %{_sourcedir}/templates/*.html; do
    install -m 644 "$t" %{buildroot}/opt/mail-archiver/templates/
done

# Static files
for s in %{_sourcedir}/static/*.css; do
    install -m 644 "$s" %{buildroot}/opt/mail-archiver/static/
done

# systemd service
install -m 644 %{_sourcedir}/mail-archiver.service %{buildroot}/etc/systemd/system/mail-archiver.service

# cron job for scheduled sync
cat > %{buildroot}/etc/cron.d/mail-archiver << 'CRON'
# Hourly scheduled sync — respects per-account intervals, auto-staggered
0 * * * * root cd /opt/mail-archiver && python3 app.py scheduled-sync >> /var/log/mail-archiver-sync.log 2>&1
CRON

%files
%dir /opt/mail-archiver
%dir /opt/mail-archiver/templates
%dir /opt/mail-archiver/static
/opt/mail-archiver/app.py
/opt/mail-archiver/search_index.py
/opt/mail-archiver/oauth2_microsoft.py
/opt/mail-archiver/imap_sync.py
/opt/mail-archiver/gunicorn.conf.py
/opt/mail-archiver/generate-cert.sh
/opt/mail-archiver/templates/*.html
/opt/mail-archiver/static/*.css
/etc/systemd/system/mail-archiver.service
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

# Cron log preseed — cron runs as root, WebUI service runs as mail-archiver;
# 0640 root:mail-archiver keeps both able to append.
touch /var/log/mail-archiver-sync.log
chown root:mail-archiver /var/log/mail-archiver-sync.log
chmod 0640 /var/log/mail-archiver-sync.log

# Default data root
mkdir -p /datapool/email-archive 2>/dev/null || mkdir -p /var/lib/mail-archiver

# Add all local human users to mail-archiver group so mbsync PassCmd can
# read the shared secret key. Additive — never removes memberships. Safe
# to re-run on upgrade.
for U in $(getent passwd | awk -F: '$3 >= 1000 && $3 < 60000 && $7 !~ /nologin|false/ {print $1}'); do
    id -nG "$U" 2>/dev/null | tr ' ' '\n' | grep -qx mail-archiver && continue
    usermod -aG mail-archiver "$U" 2>/dev/null || true
done

systemctl daemon-reload
echo "Mail Archiver installed. Edit /etc/systemd/system/mail-archiver.service to set MAIL_ARCHIVER_DATA."
echo "Then: systemctl enable --now mail-archiver"

%preun
if [ "$1" = "0" ]; then
    systemctl stop mail-archiver 2>/dev/null || true
    systemctl disable mail-archiver 2>/dev/null || true
fi

%changelog
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
  (T4). Unblocks family Hotmail account sync.
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
