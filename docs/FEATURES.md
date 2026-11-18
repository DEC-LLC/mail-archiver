# DEC-LLC Mail-Archiver 1.3.4: feature reference

One section per feature: what it does, where it is in the web interface, its limits and defaults, what is different
in the Windows application, and the release it arrived in. Numbers are the defaults in the code of 1.3.4.
Release notes are in [CHANGELOG.md](../CHANGELOG.md).

Menu names are written as the sidebar shows them: **Dashboard**, **Search archive**, **Import mail**,
**Initial / rebase sync**, **Add email account**, **Server settings**, **Admin** (**Admin overview**, **Storage**,
**Host**, **Application**), **About**, **Log out**.

## Contents

1. [Platforms at a glance](#platforms-at-a-glance)
2. [Accounts and providers](#accounts-and-providers)
3. [TLS modes for the mail server connection](#tls-modes-for-the-mail-server-connection)
4. [Microsoft sign-in (OAuth2)](#microsoft-sign-in-oauth2)
5. [Sync](#sync)
6. [Scheduled sync](#scheduled-sync)
7. [Sync status](#sync-status)
8. [Search](#search)
9. [Viewer and attachments](#viewer-and-attachments)
10. [Export](#export)
11. [Mail import](#mail-import)
12. [Initial / rebase sync for very large folders](#initial--rebase-sync-for-very-large-folders)
13. [Reconcile: "Check the server now"](#reconcile-check-the-server-now)
14. [About page, sidebar and version](#about-page-sidebar-and-version)
15. [Sign-in modes and administrators](#sign-in-modes-and-administrators)
16. [Admin panels](#admin-panels)
17. [HTTPS certificate of the web interface](#https-certificate-of-the-web-interface)
18. [Permissions check](#permissions-check)
19. [Windows application](#windows-application)

## Platforms at a glance

| | Debian / Ubuntu package | EL 9 package | Windows application |
|---|---|---|---|
| Runs as | systemd service, gunicorn | systemd service, gunicorn | desktop program with a tray icon |
| Address | `https://<host>:8443` | `https://<host>:8443` | `https://127.0.0.1:8400`, this computer only |
| Sign-in | system accounts (PAM) by default, or built-in accounts | built-in accounts (see below) | built-in accounts |
| Sync engine | isync (`mbsync`) | isync (`mbsync`) | built-in Python IMAP client |
| Schedules | cron, every hour | cron, every hour | inside the program, while it runs |
| Full-text index (SQLite FTS5) | yes | yes | yes |
| Mail import | all formats | all formats | mbox, Maildir, eml, emlx, zip/tar; PST and msg need extra programs |
| Initial / rebase sync | yes | yes | only with `mbsync` on PATH |

**EL 9 in this release.** The EL 9 package runs on the system Python 3.9 and the service starts after installation.
With built-in accounts, sync, full-text search, export, mail import and "Check the server now" work as on Debian;
the system CA bundle is found without a setting. Three limits remain:

1. Sign-in with system accounts does not work, so use built-in accounts (`MAIL_ARCHIVER_AUTH=builtin` and
   `MAIL_ARCHIVER_ADMIN_USERS`).
2. Microsoft sign-in is not verified: the SASL plug-in the rpm recommends, `cyrus-sasl-xoauth2`, was not available
   in the repositories.
3. After a reinstall or upgrade, an account that used a private CA file loses it until its **Settings** are saved
   again. This is the same on Debian.

Details are in the [administrator guide](ADMIN-GUIDE.md#el-9-known-limits-in-this-release).

## Accounts and providers

**What it does.** Each user adds the mail accounts they want to archive. An account is an address, a provider and a
credential. Mail is stored as Maildir files under the user's own folder; nothing is changed or deleted on the mail
server.

**Where.** **Add email account** in the sidebar, or the **Add Email Account** button on the **Dashboard**. Each
account is a card on the Dashboard with the buttons **Sync Now**, **Disable** / **Enable**, a schedule list,
**Settings**, **Initial / rebase sync…** and **Remove**. **Remove** takes the account off the list and deletes its
stored credential; archived mail stays on disk.

**Providers.**

| Provider in the list | IMAP server | Port | Credential |
|---|---|---|---|
| Gmail | `imap.gmail.com` | 993 | App Password |
| Outlook / Hotmail | `outlook.office.com` | 993 | Microsoft sign-in (OAuth2), no password stored |
| Outlook / Hotmail (App Password) | `outlook.office.com` | 993 | App Password |
| Apple iCloud Mail | `imap.mail.me.com` | 993 | App-Specific Password |
| Yahoo Mail | `imap.mail.yahoo.com` | 993 | App Password |
| Other IMAP Server | you enter it | 993 by default | Password |

**Per-account settings** (**Settings** on the account card, page "Account Settings"):

| Field | Default | Notes |
|---|---|---|
| Display name | provider name | label only |
| IMAP host | provider's server | blank means the provider default |
| Port | provider's port | 1 to 65535; 993 for SSL/TLS, 143 for STARTTLS |
| TLS mode | SSL/TLS | see the next section |
| Folder pattern | `*` (all folders) | isync pattern, for example `INBOX Sent` or `* !Trash !Spam`; used by isync only |
| Connection timeout (seconds) | isync's 20 | 1 to 3600; used by isync only |
| OAuth App | automatic | Microsoft accounts only |
| Client certificate (mutual TLS) | none | PEM certificate (`.pem` `.crt` `.cer`) and key (`.pem` `.key`), 256 KiB at most; used by isync only |

The same page shows whether a password is stored (redacted, with its length and age) and lets you replace it.

**Limits.** The address is also the IMAP login name. An account is identified by address plus provider, so the same
address cannot be added twice with one provider. Stored passwords are encrypted with a key derived from the
installation's secret key; without that key file they cannot be read back.

**Windows.** The same providers. Folder pattern, timeout and client certificate have no effect, because the built-in
IMAP client syncs every folder the server lists.

**Since.** 1.0.0; per-account settings and client certificates 1.0.6.

## TLS modes for the mail server connection

| TLS mode in the list | Meaning | isync (Linux packages) | Built-in IMAP client |
|---|---|---|---|
| SSL/TLS (IMAPS, port 993), the default | TLS from the first byte | yes | yes |
| STARTTLS (usually port 143) | plain connection upgraded to TLS before the login | yes | yes, since 1.3.2 |
| Plaintext (NOT recommended) | no encryption | follows the setting | **refused** |

The built-in IMAP client is the sync engine of the Windows application and of a Linux host without isync. On every
platform it is also what "Check the server now" and the folder measurement before a large sync use. It never logs in
without TLS. An account set to Plaintext fails there with:

> This account is set to "Plaintext" (no TLS). The built-in IMAP client does not send a password over an unencrypted
> connection. Edit the account and choose SSL/TLS (port 993) or STARTTLS (port 143).

Both engines verify the server's certificate and host name. Trust comes from the operating system's certificate
store; a private CA is added as described in the
[administrator guide](ADMIN-GUIDE.md#private-ca-for-outgoing-imap-connections).

**Since.** Three TLS modes 1.0.6; certificate verification in the built-in client 1.3.1; STARTTLS and the Plaintext
refusal in the built-in client 1.3.2.

## Microsoft sign-in (OAuth2)

**What it does.** The provider "Outlook / Hotmail" signs in through Microsoft's own page (OAuth2 authorization code
flow, scope `IMAP.AccessAsUser.All` on `outlook.office.com`). The archive stores an access token and a refresh token
per account, never the Microsoft password. Access tokens are refreshed before every sync.

**What it needs.** An application registration in Microsoft Entra ID (Azure) that you create yourself: an
Application (client) ID, a client secret and the redirect URI `https://<address of the archive>/oauth2/callback`.
The page **Server settings** (title "OAuth Apps") shows the exact redirect URI for the address you are using and
the registration steps.

**Where.**

- **Server settings**: lists "Server-wide OAuth Apps" and "Your Private OAuth Apps". **+ Add new OAuth App** asks
  for scope (Private or Server-wide), friendly name, tenant (`common` by default), client ID and client secret. The
  first app of a scope becomes its default; **Make default** changes it. An app that an account still uses cannot be
  deleted.
- **Add email account** with provider "Outlook / Hotmail": an optional **OAuth App** choice, then a redirect to
  Microsoft.
- Account card: **Sign in with Microsoft**. Account Settings: **Re-authorize with Microsoft** and
  **Force Token Refresh**, with the token's age and expiry.

Which app an account uses: the app chosen on the account, else the user's default, else the server-wide default.

**Limits.**

- Only an administrator can add, edit, delete or set the default of a server-wide app (since 1.3.3). Every user can
  manage their own private apps.
- When Microsoft rejects the refresh token, the account is marked as needing a new sign-in and its sync fails with
  "Microsoft sign-in expired. Click "Sign in with Microsoft" on the account settings page to renew."
- On the Linux packages isync needs a SASL XOAUTH2 plug-in. The Debian package depends on
  `libsasl2-modules-kdexoauth2`. On EL 9 the rpm only recommends `cyrus-sasl-xoauth2`, which the repositories used
  for the check did not carry.
- The sign-in flow was not exercised for this document (it needs a real Microsoft registration).

**Windows.** The same pages. The redirect URI is `https://127.0.0.1:8400/oauth2/callback`.

**Since.** 1.0.0; named apps at two scopes 1.0.8; refresh before every sync 1.0.16.

## Sync

**What it does.** Downloads new mail of an account from its IMAP server into the archive. Sync only pulls: the
generated isync configuration carries `Sync Pull`, `Create Near` and `Expunge None`, so nothing is pushed to the
server and a message deleted on the server stays in the archive.

**Where.** **Sync Now** on an account card, or **Sync All** at the top of the Dashboard. A live panel on the card
shows progress; it reattaches after a page reload or a new login.

**Rules and limits.**

- One sync at a time per account. A click while that account (or "Sync All") is already syncing starts nothing and
  queues nothing; the buttons stay off and a banner says so. The refusal reads "A sync of ... is already running
  (started ...). Your click was ignored and nothing was queued."
- A running sync that has reported nothing for 30 minutes is shown as stalled. It still blocks a new sync of that
  account.
- One isync run lasts at most 1 hour in the scheduled path.
- Large accounts (Linux, **Sync Now**): above 5,000 messages on the server (`MAIL_ARCHIVER_BATCH_THRESHOLD`) the
  folders are synced in batches, each a fresh connection. A single folder above 2,500 messages
  (`MAIL_ARCHIVER_CHUNK_THRESHOLD`) is pulled in chunks of 180 seconds (`MAIL_ARCHIVER_CHUNK_WALL_SECONDS`), so an
  interrupted download loses little.
- After a successful sync the search index is updated for the account.
- A disabled account is skipped by every sync.

**Windows.** The built-in IMAP client downloads every folder, message by message, and remembers the downloaded UIDs
per folder. There is no batching and no chunking. Message files are named with `!2,` instead of `:2,`.

**Since.** 1.0.0; live progress 1.0.14; batches 1.0.15; chunks 1.0.17; no overlapping syncs and download-only
configuration 1.1.7.

## Scheduled sync

**What it does.** Every account has a schedule: **Manual only**, **Every hour**, **Every 6 hours**,
**Every 12 hours** or **Daily (default)**. A check runs once an hour and starts the accounts that are due.

**Where.** The schedule list on the account card. The **About** page shows a summary: how many accounts are
scheduled, their slots and how many are due.

**How slots work.**

- Accounts are grouped by the IMAP server they use. Each group is a lane.
- The n accounts of a lane get fixed hours of the day: start, start + 24/n, start + 2 x 24/n and so on. The start
  hour is 0 (`MAIL_ARCHIVER_SCHEDULE_START_HOUR`). A 6-hourly or 12-hourly account repeats from its slot; an hourly
  account runs every hour.
- Within a lane accounts never run together. A lane with more than 24 accounts starts more than one per hour, one
  after another, 10 seconds apart.
- Different lanes run in parallel, at most 4 at once (`MAIL_ARCHIVER_MAX_PARALLEL_SYNCS`), started 10 seconds apart.
- If a sync on that server is still running, the lane waits for the next hourly check. A missed hour (host was off)
  is caught up the same way.
- An account is due once per slot, counted from when its last sync started, so the time of day does not drift.

**Linux.** The hourly check is a cron job at minute 0. An administrator can print the plan; see the
[administrator guide](ADMIN-GUIDE.md#command-line).

**Windows.** The check runs inside the program at the top of every hour, only while the program is running.

**Since.** Schedules 1.0.0; fixed slots and lanes 1.2.0.

## Sync status

The Dashboard shows one status per account.

| Status | Meaning |
|---|---|
| Not synced | no sync has finished yet |
| Syncing... | a sync is running |
| Synced | the last sync finished without error; "Last sync:" shows when |
| Error | the last sync failed; the first 200 characters of the reason are shown on the card |
| Interrupted | the status said "Syncing..." but no sync was running any more |

**Interrupted** appears when a sync was cut off by a service restart, a reboot or a process that ended. The entry is
corrected when the status is read, at the hourly check and when the service starts. A sync that is really running is
never touched. An older status entry without a record of its process is taken as abandoned after six hours with no
sync job in flight. Nothing needs to be done: the next sync starts normally and continues where the last one stopped.

Error texts are written for the person reading them. Examples from the code:

- "Gmail's daily IMAP download limit was reached, so Google closed the connection. ... the next scheduled sync
  continues where this one stopped."
- "The mail server rejected the login." with a hint for the provider.
- "Secure (TLS) connection to the mail server failed. The server certificate may have changed, or the server name or
  port is wrong."
- "Could not reach the mail server. Check the network, and that the server name and port are right."

Passwords and tokens that isync can echo in verbose output are removed before anything is stored or shown.

**Since.** Clear error texts 1.1.9; credential redaction 1.1.12; Interrupted 1.3.2.

## Search

**What it does.** Full-text search over all archived mail of the signed-in user, synced or imported. The index is a
SQLite FTS5 table per user (`.search_index.db` in the user's folder) with word stemming.

**Where.** **Search archive** in the sidebar.

**The search box** takes FTS5 syntax. A query needs at least 2 characters.

| Query | Finds |
|---|---|
| `invoice` | messages containing the word |
| `"quarterly report"` | the exact phrase |
| `budget OR forecast` | either word |
| `meeting NOT cancelled` | the first word without the second |
| `financ*` | words starting with `financ` |
| `*` alone (or an empty box) | not a search: the recent list, at most 10 messages (see below) |

**Filters** (all optional): **Account**, **From date**, **To date**, **From (sender)**, **To (recipient)**,
**Attachments** (Any / Has attachments / No attachments) and **Search in** (Everything, Subject only, Body only,
Sender only). The sender, recipient, attachment and "Search in" filters work only together with a word in the
search box; for example `report` with **From (sender)** `carol@`, or `invoice` with **Attachments** set to
"Has attachments".

**Limits and defaults.**

- At most 200 results per search, newest first, each with a highlighted snippet.
- With an empty search box, or with `*` or `**` alone in it, the page shows the recent list: the 10 most recent
  messages at most, and the total number in the archive. Only the **Account**, **From date** and **To date** filters
  apply to that list; the sender, recipient, attachment and "Search in" filters are ignored. `*` does not mean
  "everything": there is no query that lists all messages.
- A single character other than `*` returns nothing.
- If the index cannot be used, search falls back to a plain case-insensitive substring search: at most 100 results,
  30 seconds, only the Account filter, no `OR` / `NOT` / phrase syntax.
- The index is updated after every successful sync and import. A damaged index file can be rebuilt from the mail
  with **Rebuild the search index**, which the **Import mail** page offers when it detects the damage. The damaged
  file is set aside, not deleted.

**Windows.** The same. The fallback search is done in Python because there is no `grep`.

**Since.** 1.0.0.

## Viewer and attachments

**What it does.** Clicking a search result opens the message: subject, From, To, Cc, Date, Reply-To, Message-ID, the
account it belongs to, the body and a list of attachments with type and size. A message with both an HTML and a
plain-text part has the buttons **Formatted** and **Plain Text**. Each attachment name is a download link.

**Own-folder rule.** The viewer and the attachment download only open files inside the signed-in user's own folder.
A path to another user's message, or to any other file, is answered with 403.

**Limits.** The HTML part is shown in a sandboxed frame without scripts. Remote images referenced by the message are
not blocked. **Download as .eml** under a message runs a search for the message's subject and exports the matches as
a zip of `.eml` files, so it can contain more than one message.

**Since.** 1.0.0; own-folder rule 1.3.1.

## Export

**What it does.** Downloads the results of the current search.

**Where.** On the results page: **Download as MBOX** and **Download as EML (zip)**.

| Format | File | Content |
|---|---|---|
| MBOX | `search_<query>.mbox` | all results in one mbox file, for mail programs |
| EML (zip) | `search_<query>.zip` | one numbered `.eml` file per message |

**Limits.** An export holds what the search returned, so at most 200 messages. The export links carry the query and
the Account and date filters; the sender, recipient, attachment and "Search in" filters are not applied to the
export. Only files in the user's own folder are read (since 1.3.2).

**Since.** 1.0.0.

## Mail import

**What it does.** Imports mail files into one of the user's accounts. Imported mail is searchable like synced mail.
Only messages that are not already in the account are added.

**Where.** **Import mail** in the sidebar. The page has the sections **Upload a file**, **Import folders**,
**History**, **Failed and skipped files**, **Archive of imported files** (with the **Retention log**) and
**Scheduled import**.

**Formats.** The type is recognised from the content. The file name only decides when the content does not.

| Format | Recognised by | Needs |
|---|---|---|
| Outlook PST / OST | PST magic bytes | `readpst` |
| mbox (Thunderbird, Google Takeout, Apple Mail export) | a `From ` line followed by mail headers | |
| Maildir / Maildir++ | a directory with `cur/`, `new/`, `tmp/` | |
| `.eml`, or a directory of them | a mail header block | |
| Apple `.emlx` | a byte-count line, then mail headers | |
| Outlook `.msg` | OLE compound file with MSG streams | `msgconvert` |
| zip, tar, tar.gz / .bz2 / .xz / .zst, or one file compressed with gzip, bzip2, xz or zstd | magic bytes | `zstd` for `.zst` |

A file that is none of these is moved to the account's `failed/` folder with a reason file. A file that needs a
missing tool is refused with a message that names the tool.

**Two ways in.**

1. **Upload a file**: choose the account under "Import into", pick a file, **Upload and import**. Upload limit:
   50 GiB (`MAIL_ARCHIVER_IMPORT_MAX_BYTES`). To import a directory (a Maildir, or a folder of `.eml` files), zip it
   first or copy it into the import folder.
2. **Import folder**: every account has one, shown on the page as "Folder:". Whatever is put in its `inbox/` is
   imported into that account, a file or a directory. **Import now** imports one waiting item,
   **Import everything waiting** imports all.

```
<data>/<user>/_import/<account>/inbox/             waiting files and directories
<data>/<user>/_import/<account>/inbox/.incoming/   staging for programs that deliver files; ignored
<data>/<user>/_import/<account>/done/              imported files
<data>/<user>/_import/<account>/failed/            refused files, each with <name>.reason.txt
```

`<account>` is the address with `@` written as `_at_` and dots as `_` (`bob@example.org` becomes
`bob_at_example_org`).

**What an import does.**

1. Waits until the item has stopped changing: two checks 30 seconds apart for scheduled runs
   (`MAIL_ARCHIVER_PST_STABLE_WAIT`), 5 seconds for a manual run. Names starting with a dot and `*.tmp` are ignored.
   Symbolic links are never followed.
2. Computes the sha256. If a `<name>.sha256` sidecar is present it must match. **A file whose sha256 was already
   imported into the same account is not imported again**; it is recorded as `skipped` and moved to `failed/`.
3. Converts as the owner of the mailbox, in a private work directory on the data disk.
4. Unpacks archives and detects every member on its own. Members with absolute or `..` paths, links, devices and
   encrypted members are refused, and the import is then `partial`. An archive inside an archive is unpacked; a third
   level is not.
5. Skips duplicates: a message already in the account, in any folder, is matched by Message-ID; a message without one
   by a hash of its From, To, Cc, Subject, Date and content.
6. Delivers new messages, marked read, under a folder prefix: `PST/<folder tree>` for PST files,
   `Imported/mbox/<file name>`, `Imported/Maildir/<folder>`, `Imported/eml`, `Imported/emlx`, `Imported/msg`. The
   regular sync leaves these local folders alone.
7. Updates the search index and moves the file to `done/` with a `.sha256` next to it. PST and mbox files are
   compressed to `.zst` there when `zstd` is installed.

**Ledger (History).** One row per file: number, time, trigger, file name, first 16 characters of the sha256, type,
account and target folder, status, and the counts imported / duplicates / failed (plus "too large"). Statuses:
`queued`, `running`, `done`, `partial`, `failed`, `skipped`. The page shows the newest 50 rows. The ledger is kept in
the user's index database and survives a rebuild of the search index.

**Schedule.** Section **Scheduled import**. It is **off until "Enabled" is ticked**. Choices: Every hour, Daily,
Weekly, Manual only; the preset is Daily at 06:00. The check runs once an hour; a missed time runs at the next check.
One import runs per user at a time.

**Retention of imported files in `done/`.** Only a dated series (`<name>-YYYYMMDD.<ext>`, for example a nightly
export) is ever thinned out. A file without a date in its name is kept until a person removes it.

| Rule | What it guarantees | Default |
|---|---|---|
| R1 | Counts, never age: keep the newest copies, plus the newest copy of each earlier month that has one | newest 4, and 3 months |
| R2 | Delete only right after a successful, verified import of a new copy of the same series in the same run; a failed or partial import in the run, or no import, deletes nothing | |
| R3 | Never the newest copy; never fewer copies than the floor | floor 3 |
| R4 | Frozen while the last successful import of the series is older than twice the expected interval, at least 48 hours, or when there never was one | |
| R5 | A limited number of deletions per run and never more than half the set; a plan over the limit deletes nothing | 3 per run |
| R6 | Every deletion and every refused deletion is recorded and shown in the **Retention log** | |

The four numbers are fields in **Scheduled import** ("Keep imported files"). Allowed ranges: newest copies 1 to 52,
months 0 to 24, floor 1 to 50, deletions per run 1 to 50.

**Other limits.**

| Limit | Default | Setting |
|---|---|---|
| Largest file or directory | 50 GiB | `MAIL_ARCHIVER_PST_MAX_BYTES`, `MAIL_ARCHIVER_IMPORT_MAX_BYTES` |
| Largest single message (larger ones are skipped and counted) | 256 MiB | `MAIL_ARCHIVER_PST_MAX_MESSAGE_BYTES` |
| Most an archive may unpack to | 100 GiB | `MAIL_ARCHIVER_IMPORT_MAX_UNPACK_BYTES` |
| Largest expansion ratio of an archive, judged above 64 MiB of output | 200 | `MAIL_ARCHIVER_IMPORT_MAX_RATIO`, `MAIL_ARCHIVER_IMPORT_RATIO_FLOOR` |
| Most files in one archive | 2,000,000 | `MAIL_ARCHIVER_IMPORT_MAX_MEMBERS` |
| `readpst` time limit | 6 hours | `MAIL_ARCHIVER_PST_TIMEOUT` |
| `msgconvert` time limit per message | 300 seconds | |
| Free space kept on the data disk | 1 GiB | |

**PST watch folder (older layout).** `<data>/<user>/_import/watch/` still accepts `.pst` files. The account comes
from file name rules in **Scheduled import**, or from a name of the form `<account address>-YYYYMMDD.pst`.

**Windows.** mbox, Maildir, `.eml`, `.emlx` and zip / tar archives work. PST/OST needs `readpst` on PATH, `.msg`
needs `msgconvert` on PATH, `.zst` needs `zstd` on PATH; without them the page says which program is missing and
imports everything else.

**EL 9.** Works since 1.3.3 (import by upload and by command were run on Rocky Linux 9). With the rpm of 1.3.0 to
1.3.2 the page answered with an error.

**Since.** 1.3.0; `msgconvert` a required package 1.3.1; Windows 1.3.1; EL 9 1.3.3.

## Initial / rebase sync for very large folders

**What it does.** Providers limit how much an account may download over IMAP per day. A first sync of a very large
folder, such as Gmail's `[Gmail]/All Mail`, does not finish in one run and keeps the other folders waiting. An
initial / rebase sync is a job for chosen folders of one account. It downloads them over several days, a bounded
amount per day, outside the regular sync.

**Where.** **Initial / rebase sync** in the sidebar (one row per account, **Set up…** or **Open**), or
**Initial / rebase sync…** on the account card.

**Setting up.** Tick folders, or name them one per line exactly as the server names them (at most 20), set
**Daily budget (MB)** and the **Time window**, then **Start initial / rebase sync**.

| Provider | Default daily budget | Suggested folder |
|---|---|---|
| Gmail | 2,400 MB (Google allows about 2,500 MB per day) | `[Gmail]/All Mail` |
| Outlook / Microsoft 365 | 10,000 MB | `Archive` |
| iCloud Mail | 5,000 MB | `Archive` |
| Yahoo Mail | 5,000 MB | `Archive` |
| Other IMAP server | 10,000 MB | none |

Only Gmail's figure is a published limit; the others are cautious starting values. A budget of 0 means no budget.
The default window is 01:00 to 06:00; the same time twice means all day.

**How it runs.**

- While the job is open, the regular sync skips the chosen folders. The account's own folder pattern is not changed
  and comes back when the job closes. The page shows both patterns.
- A check runs once an hour (Linux: minute 20). It starts a run when the job is active, the time is inside the
  window and the day's budget is not used up. A run pulls only the chosen folders, download-only.
- A run stops when the budget is reached, when the window closes, when the provider closes the connection, or after
  6 hours at most. After a provider stop, a time-out or an error the job waits for the next day. isync's own state
  carries the download over, so nothing is fetched twice.
- **Run now** starts a run at once. It ignores the time window, not the budget.
- **Pause** / **Resume** stop and restart the schedule; the folders stay out of the regular sync while paused.
- **Cancel job** closes the job and gives the folders back to the regular sync. Downloaded mail is kept.
- The job closes itself when isync reports every chosen folder complete. The folders then return to the regular
  sync (the "exclusion hand-off").

**What the page shows.** Messages in the archive against the server count, size in the archive, today's use against
the budget, days run, an estimate of the time left, the last stop reason and the next run. The **Run log** lists each
run (the latest 60 are shown) with its stop reason: Complete, Daily budget reached, Time window closed, Provider
daily limit, Connection timed out, Error, Not complete yet, Paused, Cancelled, Interrupted. **Earlier jobs** lists up
to 20 closed jobs.

**Limits.** One open job per account. Nothing on the mail server is changed and no mail in the archive is deleted.
The job state file never holds credentials.

**Windows.** Needs isync's `mbsync` on PATH. Without it no job can be set up, and the page says: "The initial /
rebase sync needs isync (the mbsync program), which was not found on PATH." Reconcile works without it.

**Since.** 1.3.0.

## Reconcile: "Check the server now"

**What it does.** Counts every folder on the mail server and compares it with the archive. The check only reads
(IMAP LIST, STATUS and a read-only EXAMINE). It tells new mail since the last sync apart from mail that is missing.

**Where.** On an account's **Initial / rebase sync** page, section "Reconcile: server against archive", button
**Check the server now**. It takes a few seconds per folder on a large mailbox.

| Result | Meaning |
|---|---|
| In sync | server and archive hold the same number of messages |
| New since last sync | the difference arrived after the last sync; the next sync pulls it |
| Missing locally | messages on the server are not in the archive; **Fetch missing** is offered |
| Archive has more | the archive keeps messages that were deleted or moved on the server; expected |
| Download in progress | an initial / rebase sync is still downloading this folder |
| Not in the archive | the folder was never synced, or the folder pattern leaves it out |
| Could not check | the server did not answer for this folder |

**Fetch missing** starts a one-off, download-only pull of that folder as a small initial / rebase sync. It needs
isync.

**Limits.** Uses the built-in IMAP client, so an account set to Plaintext is refused. The connection trusts the
operating system's certificate store.

**Windows.** Works. **Fetch missing** needs `mbsync` on PATH.

**Since.** 1.3.0; Windows 1.3.1.

## About page, sidebar and version

**Sidebar.** Every page has a fixed sidebar on the left with the entries listed at the top of this document and the
signed-in user's name. The **Admin** entries are listed for everyone and are disabled for users who are not
administrators.

**Footer.** Every page shows "DEC-LLC Mail-Archiver" and the running version. When signed in it links to the About
page.

**About** (sidebar, title "About DEC-LLC Mail-Archiver") shows: Version, Build commit, Built (time and builder),
Package version, Install date, Host, Data path, Sync backend, isync version, Schedule and Links. The schedule summary
of a normal user covers their own accounts; an administrator sees all users. No address, key or credential is shown.

The two entries under "Links" go to the project page, <https://github.com/DEC-LLC/mail-archiver>, and to its
CHANGELOG (since 1.3.3).

**Since.** 1.3.0.

## Sign-in modes and administrators

| | System accounts (PAM) | Built-in accounts |
|---|---|---|
| Setting | `MAIL_ARCHIVER_AUTH=pam` | `MAIL_ARCHIVER_AUTH=builtin` |
| Who can sign in | every account of the operating system that PAM's `login` service accepts | users who registered on the page |
| New users | created by the system administrator | **Create one** on the login page: username (letters and digits, stored in lower case), password of at least 6 characters |
| Mail files belong to | the user's system account | the service account |
| Default on | the Linux packages | Windows, and any host without system accounts |

A session ends 1 hour after sign-in; the page then asks to sign in again.

**Limits.** With built-in accounts, anyone who can reach the login page can create an account, unless
`MAIL_ARCHIVER_REGISTRATION=closed` is set (since 1.3.3): the login page then has no "Create one" link and new
registrations are refused. A built-in user reads the archive folder of the same name, so read the warning in the
[administrator guide](ADMIN-GUIDE.md#sign-in-modes-and-administrators) before switching an installation that has
mail from system accounts to built-in accounts. There is no page for changing or resetting a built-in password.
System-account sign-in is not available on Windows, and does not work on EL 9 in this release.

**Administrators.**

- System accounts (Linux): members of the group `mail-archiver-admins` (`MAIL_ARCHIVER_ADMIN_GROUP` names another group). The package
  creates the group empty, so after installation nobody is an administrator. A change of membership takes effect
  within 60 seconds. Only if the configured group does not exist are `sudo` and then `wheel` used instead.
- Built-in accounts (Windows, and Linux with `MAIL_ARCHIVER_AUTH=builtin`): the Mail-Archiver users named in
  `MAIL_ARCHIVER_ADMIN_USERS`, separated by commas. By default nobody. Since 1.3.3 the group is not consulted for
  built-in accounts.

A user who is not an administrator gets "403 — Admin access required" on the admin pages.

**Since.** Both modes 1.0.0; administrators 1.0.18; Windows administrators 1.3.1.

## Admin panels

For administrators only. **Admin overview** has four cards; three have a detail page.

| Sidebar entry | Shows |
|---|---|
| Admin overview | Service (version, state, uptime, workers, thread range, **Restart Now**), Storage (data path, total / used / free, size per account with a filter and sorting), Host (load, memory, temperature, uptime, operating system), App (active syncs, failure rate, Microsoft tokens expiring within 24 hours, accounts that need a new sign-in) |
| Storage | file systems larger than 1 GB, size of every account of every user, size of the application and of the sync log |
| Host | load, memory, a 15-minute load history, disk I/O of the data volume, network interfaces, thermal zones |
| Application | active syncs with **Cancel**, Microsoft tokens expiring within 24 hours with **Force Refresh**, accounts needing re-authorization, recent sync history of all users, failure rate |

**Restart Now** asks systemd to restart the service; the page itself has no right to run `systemctl`.

**Limits.** Account sizes are computed in the background and cached for 5 minutes. **Cancel** marks the job
cancelled and signals the download process; with system accounts the service may not be allowed to signal a process
of another user, so the download can continue although the job reads cancelled. The admin pages show the addresses
of all users' accounts, their sizes and their sync errors, never message content.

**Windows.** Storage and Application work. Restart, service state and the Host figures are replaced by a note,
because they read systemd, `/proc` and `/sys`.

**Since.** 1.0.18.

## HTTPS certificate of the web interface

**Linux packages.** The service creates a self-signed certificate on first start (RSA 2048, 10 years, for the host's
name, `localhost` and `127.0.0.1`) and listens on port 8443. On every start it then looks for a certificate issued
by a CA for this host and installs the newest suitable one: an explicit pair named in `MAIL_ARCHIVER_TLS_CERT` and
`MAIL_ARCHIVER_TLS_KEY`, else the locations listed in `/etc/mail-archiver/tls-sources.conf`, else built-in locations
(certbot, generic Debian and EL paths, cockpit, OpenMediaVault). A certificate qualifies when it is not self-signed,
is valid for at least one more day, matches its key and carries the host's full name. A renewed certificate is
picked up at the next service start.

**Windows.** The program creates a self-signed certificate for `localhost` and `127.0.0.1` on first start and keeps
it in `%APPDATA%\MailArchiver\certs\`. A `mail-archiver.crt` and `mail-archiver.key` placed there are used instead.

Details: [administrator guide, TLS](ADMIN-GUIDE.md#tls-for-the-web-interface).

**Since.** HTTPS with a self-signed certificate 1.0.4; discovery of a CA-issued certificate 1.1.6.

## Permissions check

`/usr/libexec/mail-archiver-fix-perms` makes the installed application owned by root and not writable by anyone
else: `/opt/mail-archiver` (except `certs/` and `.secret_key`, which belong to the service), the helpers in
`/usr/libexec`, the cron file and the unit files. It rebuilds the compiled Python files as root. The package runs it
on every install and upgrade and prints each repair. `mail-archiver-fix-perms --check` only reports and exits 1 when
something is wrong.

**Windows.** Not present.

**Since.** 1.3.1.

## Windows application

`MailArchiver-<version>.exe` is the same application as the Linux packages, frozen into one file with a small
launcher. Nothing is installed.

| | |
|---|---|
| Start | double-click; the browser opens `https://127.0.0.1:8400` |
| Reachable from | this computer only (loopback) |
| Data | `%APPDATA%\MailArchiver\`: the archive, `.secret_key`, `certs\`, `logs\mail-archiver.log` |
| Tray icon | Open Mail-Archiver, Open archive folder, Open log folder, Quit |
| Second start | opens the browser on the running program |
| Stop | tray **Quit**, or `MailArchiver-<version>.exe --quit` |
| Schedules | sync, import and initial / rebase sync are checked at the top of every hour while the program runs |
| Code signature | none yet; Windows SmartScreen warns on first start. Check the sha256 against `SHA256SUMS` |

What differs from Linux is listed in each section above. In short: built-in accounts only; the built-in IMAP client
does every sync; PST, `.msg`, `.zst` and the initial / rebase sync need extra programs on PATH; no service, restart
or host panels; administrators come from `MAIL_ARCHIVER_ADMIN_USERS`.

Build instructions: [windows-app/README.md](../windows-app/README.md).

**Since.** Rebuilt from the shared code base in 1.3.1; STARTTLS and the Plaintext refusal 1.3.2.
