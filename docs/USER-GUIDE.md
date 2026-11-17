# DEC-LLC Mail-Archiver 1.3.3: user guide

Mail-Archiver keeps a copy of your mail on your own server or computer and lets you search and read it in a browser.
It only downloads. It never deletes, moves or changes anything in your mailbox at the provider.

This guide is for people who use an installed Mail-Archiver. Installation is in the
[administrator guide](ADMIN-GUIDE.md); every limit and default is in the [feature reference](FEATURES.md).

## Contents

1. [Sign in](#sign-in)
2. [The page layout](#the-page-layout)
3. [Add a mail account](#add-a-mail-account)
4. [Sync now and schedules](#sync-now-and-schedules)
5. [Read the status of an account](#read-the-status-of-an-account)
6. [Search](#search)
7. [Open a message and its attachments](#open-a-message-and-its-attachments)
8. [Export search results](#export-search-results)
9. [Import mail from files](#import-mail-from-files)
10. [Check the server now](#check-the-server-now)
11. [First sync of a very large mailbox](#first-sync-of-a-very-large-mailbox)
12. [Passwords](#passwords)
13. [The Windows application](#the-windows-application)
14. [Privacy](#privacy)

## Sign in

Open the address your administrator gave you, for example `https://archive.example.org:8443`. With the Windows
application the address is `https://127.0.0.1:8400` and the browser opens it for you.

If the browser warns about the certificate, the installation uses a certificate it made itself. Ask your
administrator whether that is expected before you accept it.

There are two kinds of installation:

- **System accounts.** Sign in with the user name and password you use on that server. The login page has no
  "Create one" link.
- **Built-in accounts.** The login page says "No account? **Create one**". Choose a user name of letters and digits
  and a password of at least 6 characters. The user name is stored in lower case; type it in lower case when you
  sign in. If the administrator has closed registration, the link is missing here too; ask the administrator for
  an account.

You are signed out one hour after signing in ("Session expired. Please log in again."). A sync that is running
continues.

## The page layout

Every page has a sidebar on the left with your user name at the top:

| Entry | What it is for |
|---|---|
| Dashboard | your mail accounts, their status, the sync buttons |
| Search archive | search and read archived mail |
| Import mail | import mail from files (PST, mbox, eml and others) |
| Initial / rebase sync | download a very large folder over several days; compare server and archive |
| Add email account | add another mail account |
| Server settings | Microsoft sign-in apps ("OAuth Apps") |
| Admin: Admin overview, Storage, Host, Application | for administrators; greyed out for everyone else |
| About | version, data path, sync schedule summary |
| Log out | end the session |

The footer of every page shows the version, for example "DEC-LLC Mail-Archiver 1.3.3".

## Add a mail account

Choose **Add email account**, pick the **Email Provider**, type the **Email Address** and the credential, then
**Add Account**. The account appears on the Dashboard.

Most providers do not accept your normal password from a program like this. They want an **app password**: a
separate password that you create in your account's security settings and that only works for mail programs.

| Provider | What to enter |
|---|---|
| Gmail | An App Password. Turn on 2-Step Verification in your Google account, then create an app password under Security. |
| Outlook / Hotmail | Nothing to type. You are sent to Microsoft's page to sign in and allow access; see below. |
| Outlook / Hotmail (App Password) | An app password from your Microsoft account's security page. Use this when Microsoft sign-in is not set up on your installation. |
| Apple iCloud Mail | An App-Specific Password from your Apple account page. |
| Yahoo Mail | An app password from Yahoo's Account Security page. |
| Other IMAP Server | The **IMAP Server** name, the **Port** (993 unless your provider says otherwise) and your mail password. |

Whether your provider offers app passwords for your kind of account is up to the provider. If the sync says "The
mail server rejected the login.", the credential is the first thing to check.

**Microsoft sign-in.** After **Add Account** the browser goes to Microsoft. Sign in as the address you are adding
and accept the request. You come back to the Dashboard with "Microsoft account ... authenticated successfully." If
you see "No Microsoft OAuth2 app is configured", your administrator has to register one first (or you can add your
own under **Server settings**, which explains the steps). If your browser is already signed in to a different
Microsoft account, sign out there first or use a private window. Otherwise the sign-in is accepted for the wrong
mailbox. Later, **Sign in with Microsoft** on the account card repeats the sign-in when Microsoft asks for it.

**Changing an account.** **Settings** on the account card opens "Account Settings":

- a display name for the card;
- server name, port and **TLS mode** (SSL/TLS on port 993 is the default; choose STARTTLS for a server on port 143;
  do not choose Plaintext);
- a folder pattern, if you want only some folders (for example `INBOX Sent`, or `* !Trash !Spam`);
- a new password, under "Authentication";
- for Microsoft accounts, the sign-in state and **Re-authorize with Microsoft**.

**Disable** stops syncing an account without removing it. **Remove** takes it off the list; the mail already
archived stays on disk.

## Sync now and schedules

**Sync Now** on an account card downloads what is new for that account. **Sync All** at the top does all accounts.
A panel on the card shows progress. You can close the browser; the sync continues, and the panel comes back when you
open the Dashboard again.

While a sync runs, the buttons are switched off and a banner says "A sync is running." A second click does nothing
and nothing is queued. The first sync of a large mailbox can take hours.

Each account also syncs by itself. The list on the card sets how often:

| Choice | Meaning |
|---|---|
| Manual only | only when you press Sync Now |
| Every hour | once an hour |
| Every 6 hours, Every 12 hours | at fixed hours of the day |
| Daily (default) | once a day, at a fixed hour |

The hour is chosen by the server so that accounts at the same provider do not run at the same time. The **About**
page shows your slots under "Schedule". With the Windows application, scheduled syncs happen only while the program
is running.

## Read the status of an account

Each card shows the number of messages and folders, the size on disk, "Last sync:" and one status:

| Status | Meaning | What to do |
|---|---|---|
| Not synced | nothing has been downloaded yet | press **Sync Now** |
| Syncing... | a sync is running | wait |
| Synced | the last sync finished | nothing |
| Error | the last sync failed; the reason is under the address | read the reason; see below |
| Interrupted | a sync was cut off, for example by a restart of the server | nothing; the next sync continues where it stopped |

Common reasons and what they mean:

- "The mail server rejected the login." Enter a new app password under **Settings**, or sign in with Microsoft again.
- "Gmail's daily IMAP download limit was reached, so Google closed the connection." Nothing is wrong. Gmail allows a
  limited download per day; the next scheduled sync continues. Do not keep clicking Sync. For a very large mailbox
  see [First sync of a very large mailbox](#first-sync-of-a-very-large-mailbox).
- "Microsoft sign-in expired." Press **Sign in with Microsoft**.
- "Secure (TLS) connection to the mail server failed." or "Could not reach the mail server." Check server name and
  port under **Settings**; if they are right, tell your administrator.
- "This account is set to "Plaintext" (no TLS)." Choose SSL/TLS or STARTTLS under **Settings**.

## Search

Choose **Search archive**. With an empty box the page shows your 10 most recent messages and how many messages the
archive holds.

Type a word or phrase and press **Search**. The search covers subject, sender, recipients and the full text of every
message in all your accounts, including imported mail. At most 200 results are shown, newest first, with the matching
text highlighted. A search needs at least two characters.

| You want | Type in the search box | Set these filters |
|---|---|---|
| every message with a word | `invoice` | |
| an exact phrase | `"quarterly report"` | |
| either of two words | `budget OR forecast` | |
| one word but not another | `meeting NOT cancelled` | |
| words that start the same | `financ*` | |
| mail from one sender about a topic | `project` | From (sender): `carol@` |
| mail about a topic from March 2026 | `invoice` | From date `2026-03-01`, To date `2026-03-31` |
| mail about a topic with attachments, in one account | `report` | Account, Attachments: Has attachments |
| a word in the subject only | `important` | Search in: Subject only |
| the 10 most recent messages of March 2026 | leave the box empty | From date `2026-03-01`, To date `2026-03-31` |

`OR` and `NOT` are written in capitals. The filters are optional and narrow the results: **Account**, **From date**,
**To date**, **From (sender)**, **To (recipient)**, **Attachments** and **Search in**.

The filters need a word in the search box. With an empty box, or with `*` alone, the page does not search: it shows
the recent list, at most 10 messages, and only the **Account** and date filters apply to it. The sender, recipient,
attachment and "Search in" filters are ignored there. `*` does not mean "everything"; there is no way to list every
message of a month, so search for a word that the messages you want have in common.

If searches with `OR`, `NOT` or quotes find nothing although plain words do, the full-text index of your archive
could not be used and the page fell back to a simple search. Plain words and parts of words still work; tell your
administrator.

## Open a message and its attachments

Click a result. The message page shows the subject, From, To, Cc, Date, Reply-To and Message-ID, the account the
message belongs to, and the text.

- A message that has both a formatted and a plain version has the buttons **Formatted** and **Plain Text**.
- **Attachments** are listed above the text with their type and size. Click a name to download the file.
- **Back to Search Results** returns to the list.

Formatted mail is shown without running scripts. Pictures that a message loads from the internet are fetched by your
browser when you open it, as in a mail program that shows remote images. Use **Plain Text** if you do not want that.

## Export search results

Above the results:

- **Download as MBOX**: one file with all results, for import into a mail program such as Thunderbird.
- **Download as EML (zip)**: a zip file with one `.eml` file per message.

An export contains the messages the search returned (200 at most). It follows the search text and the Account and
date filters. The sender, recipient, attachment and "Search in" filters are not applied to the export, so check the
file if you used them.

**Download as .eml** under an open message searches for that message's subject and downloads the matches as a zip.
If other messages share the subject, they are in the zip too.

## Import mail from files

**Import mail** adds mail from files to one of your accounts: old Outlook data files, exports from other mail
programs, single saved messages. Imported mail is searchable like synced mail. You need at least one account to
import into.

Supported: Outlook PST and OST, mbox (Thunderbird, Google Takeout, Apple Mail export), Maildir folders, `.eml` files
or a folder of them, Apple `.emlx`, Outlook `.msg`, and zip or tar archives of any of these. The page recognises the
type from the content of the file, not from its name.

**Upload.** Under **Upload a file**, choose the account under "Import into", pick the file and press
**Upload and import**. The page shows the progress and then the result under **History**. To import a folder (a
Maildir, or a folder of `.eml` files), zip it first.

**Import folder.** Every account has its own import folder on the server, shown under **Import folders** as
"Folder:". If you can reach that folder (for example over a file share), copy files or folders into it. They appear
under "Waiting" with the detected type. Press **Import now** for one, or **Import everything waiting**. Under
**Scheduled import** you can tick "Enabled" and choose Every hour, Daily or Weekly, so that whatever lands in the
folder is imported by itself.

**What happens to the mail.**

- Only messages that are not already in the account are added. A message that is already there, synced or imported,
  is counted as a duplicate and skipped.
- Imported mail is marked as read. It is stored in folders named `PST/...` for Outlook data files and
  `Imported/mbox/...`, `Imported/eml`, `Imported/Maildir/...` and so on for the others. The folder name is shown
  with each search result.
- The file itself is kept in the account's `done` folder. A file that could not be imported, or that was imported
  before, goes to the `failed` folder with the reason.

**Reading the History.** One row per file:

| Column | Meaning |
|---|---|
| When | start time, and "manual" or "scheduled" |
| File | file name and the start of its checksum |
| Type | what the content was recognised as |
| Account / folder | where the mail went |
| Status | `done`: imported. `partial`: imported, but some parts were refused or failed. `failed`: nothing imported; the reason is under Details. `skipped`: this exact file was imported before. `queued`, `running`: in progress |
| Imported | new messages added |
| Duplicates | messages that were already in the account |
| Failed | messages that could not be read, and "(+N too large)" for messages over the size limit |
| Details | notes, the folders that received mail, where the file was moved |

**Failed and skipped files** lists what is in the `failed` folders. **Move back to the inbox** puts a file back for
another try, for example after your administrator installed a missing tool.

**Archive of imported files** lists the kept source files. If you import a regular export with the date in its name
(`backup-20261101.pst`, `backup-20261108.pst`), older copies of that series are removed over time, but never the
newest ones and never right after a failed import. Files without a date in the name are kept until someone deletes
them.

Messages you may see:

- "readpst is not installed ..." or "msgconvert is not installed ...": PST or `.msg` files cannot be imported until
  your administrator installs that tool. Other formats still work.
- "An import is already running. It picks up everything that is waiting.": one import runs at a time.

## Check the server now

Open **Initial / rebase sync** and your account. Under "Reconcile: server against archive", **Check the server now**
counts every folder at your provider and compares it with the archive. It only reads; nothing is changed.

| Result | Meaning |
|---|---|
| In sync | the archive has everything |
| New since last sync | mail arrived after the last sync; the next sync brings it |
| Missing locally | the provider has messages the archive does not; press **Fetch missing** |
| Archive has more | the archive kept messages you deleted or moved at the provider; this is what an archive is for |
| Download in progress | an initial / rebase sync is still working on this folder |
| Not in the archive | the folder was never synced, or your folder pattern leaves it out |

## First sync of a very large mailbox

Providers limit how much you may download per day. Gmail allows about 2,500 MB. A mailbox with many years of mail
does not fit, the normal sync stops every day at the limit, and your other folders wait behind the big one.

For this case, set up an **Initial / rebase sync** for the big folder:

1. Choose **Initial / rebase sync** in the sidebar, then **Set up…** next to the account (or
   **Initial / rebase sync…** on the account card).
2. Tick the folder. For Gmail the suggestion is `[Gmail]/All Mail`. You can type other folder names, one per line,
   exactly as the provider names them. **Check the server now** on the same page fills the list with your provider's
   folders.
3. Leave **Daily budget (MB)** and the **Time window** as they are unless you have a reason. The defaults are
   2,400 MB per day for Gmail and a window from 01:00 to 06:00.
4. Press **Start initial / rebase sync**.

From then on:

- The normal sync skips that folder and keeps your other folders up to date.
- Every day, inside the time window, a run downloads until the budget is used, the window closes or the provider
  stops it. The next day it continues where it stopped.
- The page shows how many messages are in the archive against the server's count, today's use of the budget, the
  days run so far, an estimate of the time left and a **Run log**.
- **Run now** starts a run immediately, outside the window, within the budget.
- **Pause** and **Resume** hold and continue the job. **Cancel job** ends it and hands the folder back to the normal
  sync. Mail already downloaded is always kept.
- When the folder is complete, the job closes by itself and the folder goes back into the normal sync.

With the Windows application this feature needs an extra program (isync). Without it the page says so, and the
normal sync does the work.

## Passwords

**Your Mail-Archiver password.**

- System accounts: it is your password on the server. Change it there, the way your administrator tells you. Nothing
  in Mail-Archiver changes it.
- Built-in accounts: this release has no page for changing or resetting the password. Ask your administrator, who can
  remove your sign-in so that you can register again with the same user name. Your archive stays.

**The password of a mail account.** If you change the password at your provider or create a new app password, enter
it in Mail-Archiver: **Settings** on the account card, section "Authentication", then **Save**. The page shows a
shortened hint of the stored password, its length and when it was saved, never the whole password.

## The Windows application

The Windows application is the same program in a single file. What is different for you:

- **Start.** Double-click `MailArchiver-1.3.3.exe`. Windows SmartScreen warns the first time, because the file is
  not code-signed yet. Your browser opens `https://127.0.0.1:8400`.
- **Only this computer.** The page cannot be opened from another computer or phone.
- **Tray icon.** The program keeps running in the notification area: **Open Mail-Archiver**,
  **Open archive folder**, **Open log folder**, **Quit**. Starting the EXE again just opens the browser.
- **Accounts.** Built-in accounts only: choose **Create one** on the login page.
- **Schedules** run only while the program is running. Start it at login if you want regular syncs.
- **Import.** mbox, Maildir, `.eml`, `.emlx` and zip or tar archives work. Outlook PST, OST and `.msg` files need
  extra programs; the Import mail page says which one is missing.
- **Initial / rebase sync** needs the extra program isync. **Check the server now** works without it.
- **Admin pages** show storage and sync information; restart, service and host figures are not available.
- **Your data** is in `%APPDATA%\MailArchiver\`. The EXE itself can be moved or replaced by a newer one.

## Privacy

- Each user sees only their own archive. Search, the message view, attachments and exports read only your own
  folder.
- Administrators of the web interface can see the addresses of your mail accounts, how large each archive is, and
  when a sync ran or failed and why. The admin pages do not show messages.
- The archive is stored as ordinary files on the server. Whoever administers the server itself, and whoever has its
  backups, can read those files. That is the same as for any mail stored on a server you do not run yourself.
- Your mail password is stored encrypted. For Microsoft accounts no password is stored; Mail-Archiver keeps the
  access granted by Microsoft, which you can withdraw in your Microsoft account.
- Mail-Archiver does not send your mail or any usage data anywhere. It connects to your mail providers, and to
  Microsoft for sign-in if you use it.
