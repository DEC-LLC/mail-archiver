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

## Configuration

All via environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `MAIL_ARCHIVER_DATA` | `/datapool/email-archive` | Where mail archives are stored |
| `MAIL_ARCHIVER_AUTH` | `pam` | Auth mode: `pam` (system users) or `builtin` (self-registration) |
| `MAIL_ARCHIVER_PORT` | `8400` | Listen port (only used with `app.run()`) |
| `MAIL_ARCHIVER_SECRET_FILE` | — | Path to file containing Flask secret key |
| `MAIL_ARCHIVER_SECRET` | (random) | Flask secret key (fallback if no file) |

## Files

```
app.py                  # Flask application
oauth2_microsoft.py     # Microsoft OAuth2 Authorization Code flow
search_index.py         # SQLite FTS5 search engine
templates/              # Jinja2 HTML templates
  login.html
  register.html
  dashboard.html
  add_account.html
  search.html           # Advanced search with filters + export
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
