# Mail Archiver

**Archive your email. Search it instantly. Read it anywhere. One app — Windows, Linux, NAS, container, cloud.**

A self-hosted email archive with full-text search that runs on **Windows** (standalone EXE — no install needed), **Linux** (RPM/DEB), **NAS** (bare metal), or **containers**. Supports Gmail, iCloud, Outlook, Yahoo, and any IMAP server. No cloud dependencies, no subscription — install anywhere, your data stays yours.

[![Windows](https://img.shields.io/badge/Windows-EXE-blue.svg)]()
[![RPM](https://img.shields.io/badge/Rocky%2FRHEL-RPM-red.svg)]()
[![DEB](https://img.shields.io/badge/Debian%2FUbuntu-DEB-orange.svg)]()
[![Container](https://img.shields.io/badge/Container-Podman%2FDocker-lightgrey.svg)]()
[![License](https://img.shields.io/badge/license-Apache%202.0-green.svg)](LICENSE)

## Download

| Platform | Download | Size | Install |
|----------|----------|------|---------|
| **Windows** | [MailArchiver.exe](https://github.com/mvdiwan/DECLLC-GITLAB/releases/latest) | 17.9 MB | Download and double-click. No installation needed. |
| **Rocky/RHEL/Fedora** | [mail-archiver.rpm](https://github.com/mvdiwan/DECLLC-GITLAB/releases/latest) | 47 KB | `sudo dnf install mail-archiver-*.rpm` |
| **Debian/Ubuntu** | [mail-archiver.deb](https://github.com/mvdiwan/DECLLC-GITLAB/releases/latest) | 35 KB | `sudo dpkg -i mail-archiver_*.deb` |
| **Container** | Build from source | — | `podman build -t mail-archiver .` |

**Windows app** — just download the EXE. No Python, no dependencies, no installation. It opens your browser to a local web UI. Data is stored in `%APPDATA%\MailArchiver\`. Move the EXE anywhere, your data stays put.

## Features

- **Windows desktop app** — standalone EXE, no install, no dependencies, system tray icon
- **Cross-platform IMAP sync** — Python imaplib backend (Windows/Mac) with mbsync fast-path (Linux)
- **6 providers** — Gmail, iCloud, Outlook (OAuth2), Yahoo, Outlook (app password), any IMAP server
- **Full-text search** — SQLite FTS5 index with phrase, boolean (AND/OR/NOT), and prefix queries
- **Email viewer** — click any search result to read the full email with headers, body, and attachments
- **Attachment download** — download individual attachments directly from the viewer
- **Advanced filters** — date range, sender, recipient, attachment, per-account, subject/body/sender search
- **Export** — download search results as MBOX (for email clients) or EML zip (individual files)
- **Microsoft OAuth2** — secure sign-in via Azure AD, no password stored locally
- **Automatic sync** — scheduled syncs (hourly, 6h, 12h, daily) with staggered execution
- **Dual auth** — PAM mode (NAS/server) or built-in accounts (Windows/container)
- **Plain files** — Maildir on disk, accessible via SMB, NFS, or any Maildir client

## Quick Start (Windows)

1. Download `MailArchiver.exe` from [Releases](https://github.com/mvdiwan/DECLLC-GITLAB/releases/latest)
2. Double-click. Your browser opens to `http://127.0.0.1:8400`
3. Register an account, add your email, click Sync
4. Search your entire archive instantly

## Day-to-Day Usage

### Windows

Mail Archiver runs a small local web server on your computer. Leave it running in your system tray and open `http://127.0.0.1:8400` in your browser whenever you need it — or let your whole family use it from their own devices by browsing to the machine you leave it running on. Your laptop, a dedicated desktop, a back room home server, a cloud VM — anywhere. If you close the program, just double-click the EXE again — it picks up right where you left off. Your archive and settings are saved in `%APPDATA%\MailArchiver\` and persist between sessions.

It can also run as a regular Windows service — always on, always archiving for you and your family or employees, without being touched or needing a password.

### Linux / NAS

Mail Archiver runs as a systemd service in the background. It starts automatically at boot. To access it, open `http://your-server:8400` in any browser on your network. No need to start or stop anything — it's always running.

### Multi-User

Every person gets their own login and their own private archive. One installation serves your whole family or small office — each user registers their own account, adds their own email providers, and searches only their own mail. Connect from any device on your network.

## Quick Start (Container)

```bash
# Build
podman build -t mail-archiver .

# Run — your mail archive persists in ./data/
podman run -d --name mail-archiver \
    -p 8400:8400 \
    -v ./data:/data:Z \
    mail-archiver
```

Open `http://localhost:8400`, create an account, add your email, and hit Sync.

## Quick Start (Bare Metal / NAS)

```bash
# Debian/Ubuntu
sudo apt install isync python3-flask gunicorn

# Run with PAM auth (authenticates against system users)
export MAIL_ARCHIVER_AUTH=pam
export MAIL_ARCHIVER_DATA=/path/to/archive
gunicorn --bind 0.0.0.0:8400 --workers 2 --timeout 120 app:app
```

Or use the included deploy script: `./deploy.sh root@your-server /path/to/archive`

## How It Works

1. **Log in** with your system account (PAM mode) or create an account (container mode)
2. **Add email accounts** — enter your email and an app-specific password (or sign in with Microsoft OAuth2)
3. **Sync** — the app runs mbsync to pull all mail via IMAP into local Maildir
4. **Search** — full-text search across all accounts, with filters for date, sender, recipient, attachments
5. **Export** — download matching emails as MBOX or EML for backup, legal hold, or migration

Each user's data lives in `$MAIL_ARCHIVER_DATA/<username>/`:
```
data/
  madhav/
    .config/
      accounts.json        # registered email accounts
      sync_status.json     # last sync results
    .search_index.db       # FTS5 search index (SQLite)
    madhav_diwan_at_gmail_com/
      INBOX/cur/           # maildir files
      Sent/cur/
      ...
```

## Supported Providers

| Provider | Auth Method | Setup |
|----------|------------|-------|
| Gmail | App Password | Enable 2FA, then generate at [myaccount.google.com](https://myaccount.google.com) → Security → App Passwords |
| iCloud Mail | App-Specific Password | Generate at [appleid.apple.com](https://appleid.apple.com) → Sign-In → App-Specific Passwords |
| Outlook/Hotmail | OAuth2 | Configure Azure AD app at Settings → OAuth2, then "Sign in with Microsoft" |
| Outlook/Hotmail | App Password (fallback) | Enable 2FA at account.microsoft.com/security, create an App Password |

Any IMAP server works if you add it to the `PROVIDERS` dict in `app.py`.

## Search Syntax

The search box supports SQLite FTS5 query syntax:

| Query | What it does |
|-------|-------------|
| `invoice` | Emails containing "invoice" anywhere |
| `"quarterly report"` | Exact phrase match |
| `budget OR forecast` | Emails with either word |
| `meeting NOT cancelled` | Emails about meetings, excluding cancelled |
| `financ*` | Prefix match: finance, financial, financing |

Combine with filters (all optional) to narrow results:
- **Account** — search within a single mailbox
- **Date range** — emails from a specific period
- **From (sender)** — substring match on sender address
- **To (recipient)** — substring match on recipient address
- **Attachments** — only with or only without attachments

When you open the search page with no query, it shows your 10 most recent emails.

## HTTPS

Mail Archiver ships with HTTPS enabled by default. On first start, a self-signed TLS certificate is automatically generated. Your browser will show a security warning — that's expected for self-signed certs, but the connection is encrypted.

**Default ports:**
- `8443` — HTTPS (when certs are present)
- `8400` — HTTP fallback (if cert generation fails)

**Windows:** HTTPS works automatically if OpenSSL is available on the system. If not, it falls back to HTTP on port 8400.

### Changing the Port

**Linux (systemd):**
```bash
sudo systemctl edit mail-archiver
```
Add under `[Service]`:
```ini
Environment=MAIL_ARCHIVER_PORT=9443
```
Then restart: `sudo systemctl restart mail-archiver`

**Windows:** Set the `MAIL_ARCHIVER_PORT` environment variable before launching the EXE.

### Using Let's Encrypt (for internet-facing installs)

If you have a domain name pointing to your server, you can replace the self-signed cert with a real one:

```bash
# Install certbot
sudo apt install certbot    # Debian/Ubuntu
sudo dnf install certbot    # Rocky/RHEL

# Get a certificate
sudo certbot certonly --standalone -d mail.yourdomain.com --agree-tos

# Copy certs to Mail Archiver
sudo cp /etc/letsencrypt/live/mail.yourdomain.com/fullchain.pem /opt/mail-archiver/certs/mail-archiver.crt
sudo cp /etc/letsencrypt/live/mail.yourdomain.com/privkey.pem /opt/mail-archiver/certs/mail-archiver.key
sudo chown mail-archiver:mail-archiver /opt/mail-archiver/certs/*

# Restart
sudo systemctl restart mail-archiver
```

Certbot auto-renews. Set up a cron or timer to copy renewed certs and restart the service.

### Disabling HTTPS

If you're behind a reverse proxy (nginx, Caddy, HAProxy) that handles TLS, you can run Mail Archiver as HTTP-only:

```bash
sudo systemctl edit mail-archiver
```
```ini
[Service]
Environment=MAIL_ARCHIVER_HTTPS=0
Environment=MAIL_ARCHIVER_PORT=8400
ExecStart=
ExecStart=/usr/bin/gunicorn --bind 0.0.0.0:8400 --workers 2 --timeout 120 app:app
```

## Configuration

All via environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `MAIL_ARCHIVER_DATA` | `/var/lib/mail-archiver` | Where mail archives are stored |
| `MAIL_ARCHIVER_AUTH` | `pam` | Auth mode: `pam` (system users) or `builtin` (self-registration) |
| `MAIL_ARCHIVER_PORT` | `8443` (HTTPS) / `8400` (HTTP) | Listen port |
| `MAIL_ARCHIVER_HTTPS` | `1` | Set to `0` to disable HTTPS redirect |
| `MAIL_ARCHIVER_CERT_DIR` | `/opt/mail-archiver/certs` | TLS certificate directory |
| `MAIL_ARCHIVER_SECRET_FILE` | — | Path to file containing Flask secret key |
| `MAIL_ARCHIVER_SECRET` | (random) | Flask secret key (fallback if no file) |

### Changing the Data Directory

```bash
# 1. Create the new directory and set ownership
sudo mkdir -p /your/path
sudo chown -R mail-archiver:mail-archiver /your/path

# 2. Override in systemd
sudo systemctl edit mail-archiver
# Add:
#   [Service]
#   Environment=MAIL_ARCHIVER_DATA=/your/path
#   ReadWritePaths=/your/path

# 3. If SELinux is enabled
sudo semanage fcontext -a -t httpd_var_lib_t "/your/path(/.*)?"
sudo restorecon -Rv /your/path

# 4. Restart
sudo systemctl restart mail-archiver
```

## Files

```
app.py                  # Flask application
oauth2_microsoft.py     # Microsoft OAuth2 Authorization Code flow
search_index.py         # SQLite FTS5 search engine
imap_sync.py            # Cross-platform IMAP sync (imaplib stdlib)
gunicorn.conf.py        # Gunicorn config (auto-detects HTTPS)
generate-cert.sh        # Self-signed certificate generator
templates/              # Jinja2 HTML templates
  login.html
  register.html
  dashboard.html
  add_account.html
  search.html           # Advanced search with filters + export
  view_email.html       # Full email viewer with attachments
  oauth2_settings.html  # Microsoft OAuth2 configuration
static/
  style.css             # Clean, minimal CSS
Containerfile           # For podman/docker builds
deploy.sh               # Bare-metal deployment script
mail-archiver.service   # systemd unit file
```

## Roadmap

Features we're building over time. Contributions and feature requests welcome.

### Near-term

- **Block remote content by default** — don't auto-load remote images in HTML emails (tracking pixels, malicious content). Add a "Load remote content" button per email
- **Consistent HTML/plain toggle** — always show the toggle when HTML is present, even if there's no plain text alternative (generate plain from HTML)
- **Batch import** — drag-and-drop upload of PST, MBOX, and EML files for one-time migration from desktop email clients, old servers, or forensic dumps
- **Visual setup guides** — screenshot walkthroughs for Google App Password, Microsoft OAuth2, and Apple App-Specific Password setup
- **Windows EXE/MSI installer** — standalone desktop app using Python imaplib sync backend (no mbsync dependency), bundled with PyInstaller

### Search & organization

- **Conversation threading** — group related emails into threads (References/In-Reply-To header chaining)
- **Deduplication** — detect and merge duplicate emails across accounts (Message-ID + content hash)
- **Folder management** — create new IMAP folders from the web UI, move emails between folders
- **Saved searches** — bookmark frequently used queries and filters

### Bulk operations

- **Bulk subscription removal** — detect mailing lists and newsletters, unsubscribe in batch (List-Unsubscribe header)
- **Bulk sender/recipient removal** — delete all emails from a specific sender, or to a specific recipient, across all accounts
- **Bulk email type cleanup** — identify and remove categories (marketing, social notifications, automated alerts) in one action
- **Tag and label** — apply custom tags to emails for manual categorization

### Safety & compliance

- **Spam detection and removal** — score emails with SpamAssassin-style heuristics, quarantine or auto-delete
- **Antivirus scanning** — scan attachments on sync using ClamAV, quarantine infected messages
- **Legal hold** — lock down accounts or date ranges to prevent deletion (for compliance, litigation, regulatory)
- **Retention policies** — auto-archive or auto-delete emails older than a configurable threshold per account

### Multi-provider & sync

- **Multi-provider mailbox sync** — sync the same mailbox from multiple providers (Gmail + Outlook + iCloud) into a unified view
- **Google OAuth2** — native OAuth2 flow for Gmail (in addition to app passwords)
- **Apple OAuth2** — if/when Apple supports OAuth2 for third-party IMAP
- **Exchange Web Services (EWS)** — support for on-premises Exchange servers
- **CalDAV/CardDAV** — archive contacts and calendars alongside email

### Platform support

- **Windows desktop app** — native Windows installer (MSI/EXE) with imaplib sync backend, system tray icon, scheduled sync
- **macOS app** — DMG/PKG installer with native sync scheduler
- **Cross-platform imaplib backend** — Python stdlib IMAP sync for environments where mbsync isn't available (auto-detected: mbsync if present, imaplib fallback)

### Integration

- **VaultSync plugin** — integrate as a VaultSync backup module for email-specific backup and recovery
- **IMAP server** — serve the archive as a read-only IMAP server so email clients (Thunderbird, Outlook, Apple Mail) can browse the archive directly
- **Webhook notifications** — notify on new email, sync errors, quarantine events via webhook, email, or Slack

## License

Apache License 2.0. See [LICENSE](LICENSE) for details.

## Author

**DEC-LLC** (Diwan Enterprise Consulting LLC)
[dec-llc.biz](https://dec-llc.biz)
