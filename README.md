# DEC-LLC Mail-Archiver

Mail-Archiver is a self-hosted mail archive with a web interface. It downloads your IMAP accounts to plain Maildir
files on your own machine, indexes them for full-text search, and lets each user search, read and export their own
mail in a browser. It can also import mail from files such as Outlook PST, mbox and `.eml`.

Current release: **1.3.3**. Licence: Apache-2.0. Release notes: [CHANGELOG.md](CHANGELOG.md).

## Features in 1.3.3

Details, limits and defaults for each point are in the [feature reference](docs/FEATURES.md).

**Sync**

- Download-only IMAP sync: nothing is pushed to, changed on or deleted from the mail server.
- Providers: Gmail, Outlook / Hotmail (Microsoft sign-in or app password), Apple iCloud Mail, Yahoo Mail, and any
  other IMAP server.
- Sync engine: isync (`mbsync`) on Linux; a built-in Python IMAP client on Windows and on hosts without isync.
- Per-account settings: server, port, TLS mode, folder pattern, time-out, client certificate.
- Schedules per account: manual, hourly, every 6 or 12 hours, daily. Fixed daily slots, one lane per mail server,
  servers in parallel.
- One sync per account at a time; extra clicks are ignored, never queued.
- Live progress on the Dashboard; a status per account, including **Interrupted** for a sync that was cut off.

**Search and viewing**

- Full-text search (SQLite FTS5): words, phrases, `OR`, `NOT`, prefix search.
- Filters: account, date range, sender, recipient, attachments, subject / body / sender only.
- Message viewer with headers, formatted or plain text, and attachment download.
- Export of search results as mbox or as a zip of `.eml` files.

**Import**

- Imports Outlook PST/OST, mbox, Maildir, `.eml`, Apple `.emlx`, Outlook `.msg`, and zip / tar archives of them.
- By upload in the browser, or from a per-account import folder, on demand or on a schedule.
- The type is detected from the content. Messages already in the account are skipped.
- An import ledger records every file. Imported source files are kept, with retention rules that can never rotate a
  series to empty.

**Large mailboxes**

- "Initial / rebase sync": downloads chosen folders over several days within a daily byte budget and a time window,
  and resumes where it stopped. The regular sync skips those folders until the job is complete.
- "Check the server now" (Reconcile): a read-only comparison of every folder on the server with the archive.
- Large accounts are synced in folder batches and time-boxed chunks.

**Administration**

- Sign-in with system accounts (PAM) or with built-in accounts.
- Admin pages for administrators: service state and restart, storage per account, host load, active syncs, sync
  history.
- About page with version, build, data path and schedule summary.
- Command-line access to the scheduled jobs, the import ledger and Reconcile.
- A permissions check for the installed application files.

**Security**

- The web interface is served over HTTPS: a self-signed certificate by default, a CA-issued one when present.
- Mail server connections verify certificate and host name. The built-in IMAP client refuses to log in without TLS.
- Each user can open only messages in their own folder.
- Stored mail passwords are encrypted. Microsoft accounts use OAuth2; no Microsoft password is stored.

**Platforms**

| Platform | Package | State in this release |
|---|---|---|
| Debian 12 / 13, Ubuntu 22.04 / 24.04 | `.deb` | all features; checked on Debian 13 |
| Rocky Linux 9 and other EL 9 | `.rpm` | sync, full-text search, mail import and "Check the server now" work with built-in accounts; sign-in with system accounts does not work and Microsoft sign-in is not verified; checked on Rocky Linux 9. See [EL 9 limits](docs/ADMIN-GUIDE.md#el-9-known-limits-in-this-release) |
| Windows (64-bit) | single EXE with a tray icon | single computer, `https://127.0.0.1:8400`; PST, `.msg` and the initial / rebase sync need extra programs |

## Downloads

All downloads are on GitHub Releases: <https://github.com/DEC-LLC/mail-archiver/releases>

| File | For |
|---|---|
| `mail-archiver_1.3.3-1_all.deb` | Debian and Ubuntu |
| `mail-archiver-1.3.3-1.el9.noarch.rpm` | EL 9 |
| `perl-Email-Outlook-Message-0.921-1.el9.noarch.rpm` | EL 9 dependency (`msgconvert`), built by DEC-LLC because no EL repository carries it |
| `perl-Email-Outlook-Message-0.921-1.el9.src.rpm` | its source rpm |
| `libpst` (not in the release) | EL 9 dependency (`readpst`), from the `devel` repository of Rocky Linux |
| `MailArchiver-1.3.3.exe` | Windows |
| `MailArchiver-1.3.3-source.zip`, `MailArchiver-1.3.3-toolchain.txt` | the source tree and the toolchain versions of the Windows build |
| `mail-archiver-1.3.3-1.el9.src.rpm` | source rpm (SRPM) |
| `mail-archiver-1.3.3.tar.gz` | source tarball |
| `mail-archiver_1.3.3-1.dsc`, `mail-archiver_1.3.3.orig.tar.gz`, `mail-archiver_1.3.3-1.debian.tar.xz` | Debian source package |
| `SHA256SUMS` | one file for the whole release: the sha256 of every other release file |

## Verify your download

The release has a single `SHA256SUMS` that covers every release file listed above. Download it from the same
release page as the files.

Linux, in the directory with the downloads (`--ignore-missing` skips the files you did not download):

```bash
sha256sum -c SHA256SUMS --ignore-missing
```

Windows (PowerShell). The first command prints the sha256 of the EXE, the second the line for it in `SHA256SUMS`;
the hexadecimal value must be the same in both (upper and lower case do not matter):

```powershell
(Get-FileHash .\MailArchiver-1.3.3.exe -Algorithm SHA256).Hash
Select-String MailArchiver-1.3.3.exe .\SHA256SUMS
```

**Nothing in this release is signed.** The Windows EXE is not code-signed yet, so Windows SmartScreen warns before
the first start. The rpm, the source rpm and the deb carry no package signature either, so the package manager has
no signature to check. `SHA256SUMS` from the same release page is the integrity check: verify the sha256 before you
install or run a file.

## Quick start

### Debian and Ubuntu

```bash
sudo apt-get update
sudo apt-get install ./mail-archiver_*.deb cron
sudo systemctl enable --now mail-archiver
sudo gpasswd -a "$USER" mail-archiver-admins     # optional: make yourself an administrator
```

Open `https://<host>:8443`, accept the self-signed certificate, and sign in with your account on that host. Choose
**Add email account**, then **Sync Now**.

### EL 9

```bash
sudo dnf install epel-release && sudo dnf config-manager --set-enabled crb
sudo dnf install --enablerepo=devel libpst \
    ./perl-Email-Outlook-Message-0.921-1.el9.noarch.rpm ./mail-archiver-*.noarch.rpm
```

Sign-in with system accounts does not work on EL 9 in this release, so the service is set to built-in accounts
there before it is started. Search, mail import and sync then work as on Debian. Continue with the
[EL 9 steps in the administrator guide](docs/ADMIN-GUIDE.md#el-9).

### Windows

1. Download `MailArchiver-1.3.3.exe` and `SHA256SUMS`, and verify the sha256 as shown above.
2. Double-click the EXE. Confirm the SmartScreen warning.
3. The browser opens `https://127.0.0.1:8400`; accept the program's own certificate.
4. Choose **Create one** to register a user, then **Add email account** and **Sync Now**.

The program keeps running as a tray icon. Data is in `%APPDATA%\MailArchiver\`.

## Documentation

| Document | For |
|---|---|
| [docs/USER-GUIDE.md](docs/USER-GUIDE.md) | people who use the archive |
| [docs/ADMIN-GUIDE.md](docs/ADMIN-GUIDE.md) | installation, settings, TLS, backups, troubleshooting, command line |
| [docs/FEATURES.md](docs/FEATURES.md) | every feature with its limits, defaults and platform differences |
| [CHANGELOG.md](CHANGELOG.md) | release notes |
| [windows-app/README.md](windows-app/README.md) | the Windows application and how it is built |

## Building from source

**Linux packages.** [packaging/build.sh](packaging/build.sh) builds everything from a checkout, in containers
(it needs `git` and `podman`; nothing is installed on your machine):

```bash
packaging/build.sh            # from the current commit
packaging/build.sh all v1.3.3 # from a tag
```

It makes the source tarball with [packaging/make-source.sh](packaging/make-source.sh) (a `git archive` of the commit
plus `BUILD_INFO`), then the SRPM and rpm in a Rocky Linux 9 container and the Debian source package and deb in a
Debian 12 container, and writes them to `packaging/dist/` with a `SHA256SUMS` for the files it built. The
`SHA256SUMS` of a release is assembled over all release files, including the Windows files and the
`perl-Email-Outlook-Message` packages, which this script does not build.

To rebuild from the published sources instead:

```bash
rpmbuild --rebuild mail-archiver-1.3.3-1.el9.src.rpm              # on EL 9, package rpm-build
dpkg-source -x mail-archiver_1.3.3-1.dsc && cd mail-archiver-1.3.3 && dpkg-buildpackage -us -uc -d   # on Debian 12
```

The version comes from the file `VERSION`; the commit is shown on the About page.

`packaging/perl-Email-Outlook-Message/build.sh` builds the EL 9 `msgconvert` package from the CPAN source in a Rocky
Linux 9 container.

**Windows.** See [windows-app/README.md](windows-app/README.md): MSYS2 UCRT64 toolchain, PyInstaller,
`windows-app\build.ps1`.

**Container image.** The [Containerfile](Containerfile) builds a Debian 12 image that serves plain HTTP on port
8400 with built-in accounts and no scheduler. It is meant for trying the program out behind your own TLS proxy; no
image is published. The image carries `VERSION`, so the pages show the version. The secret key (session cookies,
encryption of stored mail passwords) is not in the image: it is created in the data volume `/data` on first start.
Keep that volume; without it the stored mail passwords cannot be read and every session ends.

Python dependencies come from the distribution's packages only; nothing is installed with pip.

## Reporting a security issue

Please report vulnerabilities privately, not in a public issue. The policy is in [SECURITY.md](SECURITY.md); the
form is "Report a vulnerability" under the Security tab of the repository:
<https://github.com/DEC-LLC/mail-archiver/security/advisories/new>.

Other bugs and requests: <https://github.com/DEC-LLC/mail-archiver/issues>.

## Licence

Apache License 2.0. The full text is in [LICENSE](LICENSE), the copyright line in [NOTICE](NOTICE). The rpm installs
both files; the deb installs `NOTICE` and a `copyright` file that points to the system's copy of the licence.

Copyright 2026 DEC-LLC (Diwan Enterprise Consulting LLC).
