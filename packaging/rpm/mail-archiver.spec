Name:           mail-archiver
Version:        1.0.0
Release:        1%{?dist}
Summary:        Self-hosted email archive with full-text search
License:        Apache-2.0
URL:            https://dec-llc.biz
BuildArch:      noarch

Requires:       python3-flask
Requires:       python3-gunicorn
Requires:       isync

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
/opt/mail-archiver/templates/*.html
/opt/mail-archiver/static/*.css
/etc/systemd/system/mail-archiver.service
%config(noreplace) /etc/cron.d/mail-archiver

%post
# Generate secret key if not present
if [ ! -f /opt/mail-archiver/.secret_key ]; then
    python3 -c "import secrets; print(secrets.token_hex(32))" > /opt/mail-archiver/.secret_key
    chmod 600 /opt/mail-archiver/.secret_key
fi
# Create default data directory
mkdir -p /datapool/email-archive 2>/dev/null || mkdir -p /var/lib/mail-archiver
systemctl daemon-reload
echo "Mail Archiver installed. Edit /etc/systemd/system/mail-archiver.service to set MAIL_ARCHIVER_DATA."
echo "Then: systemctl enable --now mail-archiver"

%preun
if [ "$1" = "0" ]; then
    systemctl stop mail-archiver 2>/dev/null || true
    systemctl disable mail-archiver 2>/dev/null || true
fi

%changelog
* Mon Apr 06 2026 Madhav Diwan <madhav@decllc.biz> - 1.0.0-1
- Initial package: Flask web UI + FTS5 search + OAuth2 + export
- 36K+ emails tested, 80ms query time
- Security: XSS protection, command injection fix, path traversal guard
