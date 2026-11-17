# Changelog

Release notes for DEC-LLC Mail-Archiver. Older releases are described in the
`%changelog` of `packaging/rpm/mail-archiver.spec` and in the git history.

## 1.3.3 (2026-11-16)

This release is about how the program is built and published. Preparing it for the public found defects, which
are fixed here; apart from them the installed program is the same as 1.3.2.

### Security
- **Server-wide OAuth apps are written by administrators only.** Any signed-in user could add, edit, delete or set
  the default of a server-wide app (the client id and secret used for every user's Microsoft sign-in). Private apps
  are unchanged.
- **Administrators with built-in sign-in are the names in `MAIL_ARCHIVER_ADMIN_USERS`,** on Linux as on Windows. On
  Linux a built-in user used to be an administrator when a *system* account of the same name was in the admin
  group; because anyone can register a built-in name, registering an administrator's login name gave the admin
  pages. Installations that use system accounts (the default of the Linux packages) are not affected. **If you run
  built-in sign-in on Linux, set `MAIL_ARCHIVER_ADMIN_USERS` after upgrading**, or nobody is an administrator.

- **Self-registration can be closed.** With built-in sign-in, `MAIL_ARCHIVER_REGISTRATION=closed` removes the
  "Create one" link and refuses new registrations.
- The session cookie is sent with `SameSite=Lax`; passwords of built-in accounts are compared in constant time.
- **Container image:** the secret key is created on first start in the data volume. It used to be generated when the
  image was built, so every container started from one image shared it.
- Both packages now carry the licence: the rpm installs `LICENSE` and `NOTICE` (`%license`); the deb installs
  `NOTICE` and a `copyright` file that points to the system's copy of the licence. The `LICENSE` file in the source
  is the full Apache-2.0 text; the copyright line moved to `NOTICE`.

### Fixed: EL 9
The rpm did not work on EL 9 from 1.3.0 to 1.3.2. Installing it on Rocky Linux 9 showed:
- **Search and mail import.** `search_index.py` used a type annotation that only Python 3.10 and later can
  evaluate; EL 9 runs Python 3.9, so the module failed to import.
- **The service did not start.** The unit named the group `shadow`, which EL does not have, and the package created
  a different data directory from the one the unit uses. The rpm now ships the unit without that line and creates
  `/var/lib/mail-archiver`.
- **Every sync failed its certificate check.** Only the Debian location of the system CA bundle was known. The EL
  location is found too.
- **No scheduled runs on a minimal install.** `cronie` is now required.
- Still open on EL 9: sign-in with system accounts (use built-in accounts), and Microsoft sign-in is not verified.
  See `docs/ADMIN-GUIDE.md`.
- The test suite now also runs on EL 9, and a test checks every shipped module for Python 3.9 compatibility. The deb
  and the Windows application were not affected by any of this.

### Source packages
- Every build now starts from one source tarball, `mail-archiver-<version>.tar.gz` (a `git archive` of the release
  commit plus `BUILD_INFO`), made by `packaging/make-source.sh`. The same commit always gives the same bytes.
- The rpm is built from that tarball (`Source0`) and an **SRPM** is produced with it. `rpmbuild --rebuild` of the SRPM
  gives the files of the released rpm.
- The deb is built from a real `debian/` directory and a **Debian source package** (`.dsc`, `.orig.tar.gz`,
  `.debian.tar.xz`) is produced with it. `dpkg-buildpackage` from the `.dsc` gives the files of the released deb.
- `packaging/build.sh` runs both builds in containers (EL 9 for the rpm, Debian 12 for the deb) and writes
  `SHA256SUMS`. Neither build step needs git or the checkout, only the tarball.
- Both packages install the same cron file; the spec's version is kept in the spec and checked against `VERSION`.
- Windows: each EXE comes with `MailArchiver-<version>-source.zip` (the same source tree) and
  `MailArchiver-<version>-toolchain.txt` (the versions of the build toolchain).
- The rpm is now named with its distribution tag: `mail-archiver-1.3.3-1.el9.noarch.rpm`.

### Changes visible in the program
- The About page links to the public project page and to this changelog.
- Example texts in the account and import pages use neutral names.

### Documentation
- New: `docs/FEATURES.md`, `docs/ADMIN-GUIDE.md`, `docs/USER-GUIDE.md`. The README is rewritten around them.

### Tests and tools
- Test data uses `example.org` addresses and neutral user names.
- The live harness takes its server, mailbox, password file and CA file from `MA_HARNESS_*` variables or a local
  configuration file (`tools/live-harness/harness.example.conf`); nothing is built in. It generates its own filler
  mail and builds its package from a source tarball, as a release does.

## 1.3.2 (2026-11-16)

### Security
- **The built-in IMAP client never logs in without TLS.** Up to 1.3.1 an account set to "Plaintext" (or a provider
  entry without TLS) was opened with no encryption and the password sent in the clear. This is the client the Windows
  application always uses, and the fallback on a Linux host without isync. Such an account now fails with a message
  that says to choose SSL/TLS or STARTTLS; nothing is sent to the server. Accounts on SSL/TLS (port 993) are
  unchanged. The same holds for "Check the server now" (Reconcile) and for the folder measurement before a batched
  sync. The isync download itself (the Linux packages) still follows the account's setting.
- **STARTTLS in the built-in IMAP client.** An account set to "STARTTLS" (usually port 143) is upgraded to TLS before
  the login, with the same certificate and host-name check as SSL/TLS accounts. Until now this client ignored the
  account's TLS mode. If the server offers no STARTTLS, or its certificate does not verify, the sync fails without
  logging in.
- Search export (mbox / eml zip) only reads files in the signed-in user's own folder, like the viewer since 1.3.1.
  The paths already came from the user's own search index; this is a second check.

### Sync status
- A sync status left at "Syncing..." by a sync that no longer runs (service restarted, process ended, host
  rebooted) now reads **Interrupted**, with the time the sync had started. It is corrected when the status is read,
  on the hourly schedule and when the service starts. A sync that is running is never touched: a registered sync job
  for the same account, or a live process that wrote the entry, keeps it at "Syncing...". New "syncing" entries
  record the process that wrote them; an older entry without that record is taken as abandoned after six hours with
  no sync job in flight.

### Windows
- `windows-app/build.ps1` can sign the EXE (Authenticode, SHA-256): `-SignThumbprint <thumbprint>` and optionally
  `-TimestampUrl <url>`. The build fails unless the signature verifies as Valid. `-SignTest` proves the step with a
  certificate Windows does not trust and names the output `MailArchiver-<version>-TESTSIGNED.exe`, which is never
  published. Without these parameters the build is unchanged and reports `UNSIGNED`.
- `MailArchiver-1.3.2.exe` is still **unsigned**.

### Housekeeping
- Git tags `v1.3.0`, `v1.3.1` and `v1.3.2` exist (the last tag before them was `v1.0.4`).

## 1.3.1 (2026-11-14)

### Windows
- The Windows application (`MailArchiver-1.3.1.exe`) is rebuilt from the current code base; the last Windows build was
  1.0.2. It is built from the same modules as the Linux package: everything that only exists on POSIX (file locks,
  account lookups, ownership, running a child as another user) now goes through one module, `platform_compat.py`, and
  the separate copy `windows-app/imap_sync.py` is gone.
- Works on Windows: accounts, sync through Python's imaplib with the hourly schedule, search, viewer, export, the About
  page, footer version and sidebar, "Check the server now" (Reconcile), and mail import of mbox, Maildir / Maildir++,
  `.eml`, a directory of `.eml`, Apple `.emlx` and zip / tar containers, with the import ledger, per-account import
  folders and retention rules.
- Refused on Windows with a message on the page, never an error page: PST/OST import unless `readpst` is on PATH, `.msg`
  import unless `msgconvert` is on PATH, the initial / rebase sync unless isync (`mbsync`) is on PATH, system-account
  (PAM) sign-in, the Restart button, and the service / journal / host panels.
- Message files are named with `!2,` instead of `:2,` on Windows (a colon is not allowed in an NTFS file name). Names on
  Linux are unchanged.
- Administrators on Windows are the Mail Archiver users named in `MAIL_ARCHIVER_ADMIN_USERS`; by default nobody is.
- The build uses the MSYS2 UCRT64 toolchain installed with `pacman` (`windows-app/build.ps1`); `pystray`, which MSYS2
  does not carry, is built as a pacman package from `windows-app/pystray-PKGBUILD`. No pip.

### Security
- A signed-in user could open another user's message, or its attachments, by passing that message's path to
  `/email/view` or `/email/attachment`: the check only required the path to be inside the data directory. Both now
  require the path to be inside the signed-in user's own folder.
- The built-in IMAP client (the sync on Windows and on hosts without isync, and the folder-size probe and "Check the
  server now" everywhere) did not verify the server's certificate or host name, so it would have sent an account's
  password to any server that answered. It now verifies both against the operating system's trusted certificates, plus
  the file named in `MAIL_ARCHIVER_CA_FILE` for a private CA. Syncs through isync were always verified.

### Fixes
- Sync through imaplib: on a server that sends folder names unquoted (Dovecot, Courier), every folder was read as the
  hierarchy delimiter, none could be opened, and the sync was recorded as clean with nothing new. Names are now read
  in every form the protocol allows, and a sync in which no folder could be opened is reported as an error. Found
  with the Windows build against the test mailbox.
- Sync through imaplib (hosts without isync, and Windows): an account with a custom IMAP server connected to an empty
  host name, and a refused sign-in was recorded as a clean sync with nothing new. The account's own server is used, and
  a refused sign-in is reported as an error.

- Sync (scheduled and "Sync now") of an account whose address contains a hyphen or a plus sign. The sync asked isync for a channel name
  with those characters removed, which does not exist in the generated configuration ("No channel or group named ...
  defined"), so such an account failed on every sync. Present since 1.0.2; found with a live test mailbox.

### Packaging
- Every install and upgrade now runs `/usr/libexec/mail-archiver-fix-perms`: `/opt/mail-archiver` and everything in it
  (except `certs/` and `.secret_key`, which belong to the service account), the helpers in `/usr/libexec`, the cron file
  and the unit files are set to `root:root` with no group/other write, each repair is printed, and `__pycache__` is rebuilt
  by root. `mail-archiver-fix-perms --check` reports without changing anything (exit 1 when something is wrong).
  Reason: on one host the application directories were still owned by an ordinary account with mode 775 from an old
  copy-the-files deploy; the package manager never resets an existing directory.
- `libemail-outlook-message-perl` (`msgconvert`, for `.msg` import) is a hard dependency (`Depends` / `Requires`), no
  longer a recommendation: an upgrade never installed it, so `.msg` files were refused after an upgrade.
- EL: `msgconvert` is in no EL repository, so `packaging/perl-Email-Outlook-Message/` builds
  `perl-Email-Outlook-Message` 0.921 from the CPAN tarball in a Rocky 9 container; the mail-archiver rpm installs with it.
- `platform_compat.py` is installed with the other modules.
- EL 9: the rpm also needs `readpst`; the `libpst` package that carries it is only in Rocky's `devel` repository
  (AppStream has just `libpst-libs`). Install with `dnf install --enablerepo=devel libpst ...`; see the README.
  `perl-Email-Outlook-Message` 0.921-1.el9 is built, with its source rpm, by
  `packaging/perl-Email-Outlook-Message/build.sh`.
- The sha256 of the Windows EXE is in `windows-app/MailArchiver-1.3.1.exe.sha256`. It is not code-signed.

## 1.3.0 (2026-11-14)

### Mail import
- General mail importer, built from the PST importer. Formats: Outlook PST/OST
  (`readpst`), mbox (Thunderbird, Google Takeout, Apple Mail export), Maildir and
  Maildir++, a single `.eml` or a directory of them, Apple `.emlx`, Outlook `.msg`
  (only through Debian's `msgconvert`; refused with a clear message when it is not
  installed), and zip / tar(.gz/.bz2/.xz) containers whose members are detected
  one by one.
- One import folder per mail account:
  `<data>/<user>/_import/<account>/{inbox,done,failed}/`. The folder decides the
  target account. The WebUI upload writes into `inbox/`. The legacy
  `_import/watch/` folder with filename rules still works for PST files.
- The type is detected from the content; the extension is only a fallback.
  A file that cannot be identified moves to `failed/` with a `.reason.txt`.
- Import ledger in the per-user SQLite database (`.search_index.db`): one row per
  file with sha256, size, type, account, target folder, status, counts and times.
  A file whose sha256 was already imported into the account is skipped.
  Message-level dedupe (Message-ID, else a content hash) works across all
  formats and all folders of the account. The import page shows the history from
  the ledger.
- Imported folders sit under a prefix in the account: `PST/` for PST files,
  `Imported/<type>/` for the others. Imported mail is marked read and is
  searchable.
- Retention of imported source files can never rotate to empty: count-based
  keeps, pruning only after a verified successful import in the same run, never
  below `min_keep` (3), never the newest copy, frozen while the source is stale,
  a per-run deletion budget, and every deletion or refusal recorded in the
  ledger. A partial import is archived but blocks rotation.
- Default schedule: daily at 06:00, checked by the hourly cron (minute 30).

### Initial / rebase sync for very large folders
- New WebUI feature (account action "Initial / rebase sync…"): a per-account job
  that downloads chosen folders (for example `[Gmail]/All Mail`) over several
  days with the installed isync, download-only, inside a time window and a daily
  byte budget (Gmail default 2400 MB/day). The regular sync skips those folders
  while the job is open and takes them back when it completes.
- Stops cleanly at the budget, the end of the window, or the provider's daily
  limit, and resumes the next day. Progress, run log, pause / resume / cancel.
  Cancel keeps all downloaded mail.
- Reconcile view: server message count (read-only IMAP STATUS / EXAMINE) against
  the archive, with messages that arrived since the last sync explained, and a
  "Fetch missing" action.
- Hourly cron check at minute 20. No credentials are stored or logged.

### WebUI
- "About DEC-LLC Mail-Archiver" page (`/about`): version, build commit, package
  version, install date, host, data path, sync backend and isync version,
  schedule summary, links.
- The version is shown in the footer of every page.
- Fixed left sidebar on every page (shared layout). Admin entries are listed for
  everyone and disabled for non-administrators; the admin pages themselves still
  answer 403 to non-administrators.

### Packaging
- New modules `mail_import.py`, `bulk_sync.py`, `bulk_sync_routes.py`,
  `layout.py`; `BUILD_INFO` file with the build commit.
- Depends on `pst-utils`; recommends `zstd` and `libemail-outlook-message-perl`.
- Cron: `import` at minute 30 and `bulk-sync` at minute 20, next to the hourly
  sync at minute 0.

### Repository
- `main` now carries the 1.1.7 to 1.2.0 work.

## 1.2.0 (2026-11-04)
- Fixed daily sync slots, one lane per IMAP server, servers in parallel.
- Packages are built under umask 022 (cron refused the group-writable cron file).

## 1.1.7 to 1.1.12 (2026-10-31 to 2026-11-04)
- No overlapping syncs; download-only isync configuration; cron wrapper that
  takes the service environment; clearer sync errors; the sync panel no longer
  sits on "Reconnecting…"; credentials echoed by `mbsync -V` are never stored or
  shown.
