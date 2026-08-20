Name:           mail-archiver
Version:        1.0.8
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
* Thu Aug 20 2026 Madhav Diwan <madhav@decllc.biz> - 1.0.8-1
- OAuth Apps management: registered Azure apps are now named + reusable
  at two scopes (server-wide + per-user), with per-provider defaults and
  per-account pinning. Solves the "different Azure app per family member
  / per tenant / per email account" case.
- Data model v2: server config at ${DATA}/.oauth2_config.json becomes
  {apps: {srv-*: {...}}, defaults: {microsoft: srv-...}, _schema_version: 2}
  Per-user config at ${DATA}/${user}/.config/oauth_apps.json (same shape,
  usr-* prefix). Auto-migrates v1 legacy {microsoft: {client_id, secret}}
  to a named app srv-microsoft-default on first read, save-back to disk.
  Existing accounts (no oauth_app_id) resolve via the server default —
  seamless upgrade for the current mvdiwan@hotmail.com flow.
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
