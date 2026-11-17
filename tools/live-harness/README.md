# Live harness: every import format against an account that is syncing a live mailbox

`live_harness.py` answers one question before a release: **do local imports and the regular one-way IMAP sync work
together in the same account, for every import format, while new mail keeps arriving?**

It runs on a build machine and never touches an archive host. The package under test is built from this checkout
(debian:12 container) and installed in a throwaway debian:13 container in PAM mode with a real unix mailbox owner. That
container syncs **a test mailbox on an IMAP server you control** while:

- a sender delivers a new message to that mailbox **through the MTA** (SMTP to the configured server) every 2 seconds;
- one fixture per import format is delivered into the account's import folder and imported;
- a regular sync runs after every import, and one sync runs at the same moment as an import.

```
python3 tools/live-harness/live_harness.py            # build, run, report; exit 1 if any check fails
python3 tools/live-harness/live_harness.py --deb packaging/dist/mail-archiver_X-1_all.deb   # a package built earlier; build.sh writes to packaging/dist/
python3 tools/live-harness/live_harness.py --extra DIR   # DIR/fixture.pst and/or DIR/msg/*.msg made with Outlook
```

Needs: podman, and the configuration below. No pip; the containers use Debian packages only. About one minute.

## Configuration

Nothing about the server is built in. Every setting is an environment variable `MA_HARNESS_<KEY>`, or the same name in
`tools/live-harness/harness.local.conf` (ignored by git; copy `harness.example.conf`, which documents every key). The
environment wins over the file; `MA_HARNESS_CONFIG` names another file.

| Key | Meaning | Default |
|---|---|---|
| `MA_HARNESS_IMAP_HOST` | IMAP server of the test mailbox (implicit TLS) | required |
| `MA_HARNESS_IMAP_PORT` | IMAP port | 993 |
| `MA_HARNESS_SMTP_HOST` | SMTP server that accepts mail for the mailbox from this machine (plain SMTP, no authentication) | the IMAP host |
| `MA_HARNESS_SMTP_PORT` | SMTP port | 25 |
| `MA_HARNESS_ADDRESS` | address and IMAP login of the test mailbox | required |
| `MA_HARNESS_PASSWORD_FILE` | file holding the mailbox password; mounted read-only into the container, never printed | required |
| `MA_HARNESS_CA_FILE` | PEM CA certificate(s) for a server on a private CA, one certificate per file, comma-separated | none |
| `MA_HARNESS_FILLER_DIRS` | folders of message files to use as filler, comma-separated | none: generated mail |
| `MA_HARNESS_FILLER_SSH` | `user@host`: read the filler folders there over ssh | local folders |

With a required key missing the harness prints the missing keys and exits 2; a password or CA file that does not exist
is reported the same way. In both cases nothing is contacted.

Use a mailbox that exists only for this test. The harness delivers mail to it and, at the end, removes exactly the
messages that carry this run's `X-MA-Harness` header.

## Filler

By default the harness generates its filler mail (plain text, HTML alternatives, non-ASCII text, attachments). With
`MA_HARNESS_FILLER_DIRS` it reads real mail instead, for example the `cur/` of a few junk folders, which is a harder
test of the parsers; up to five folders are used, one per purpose (see the table), and they are only read. Each message
gets a harness Subject (a searchable token per fixture) and a harness Message-ID, so every expectation is an exact
number and does not depend on what the test mailbox already holds. Three messages are left without a Message-ID to
exercise the content-hash dedupe. Fixtures, filler and the container are removed at the end; the report
(`~/.cache/mail-archiver-live-harness/report-<run>.json`) holds counts only.

## Formats

| Fixture | Filler source | Tested |
|---|---|---|
| Maildir (messages in `cur/` and `new/`) | source a | yes |
| Maildir++ (with a `.Folder.Sub`) | source a | yes |
| mbox named `.dat` (wrong extension), incl. 3 messages without Message-ID | sources b and c | yes |
| single `.eml` | source b | yes |
| directory of `.eml` with a subdirectory | source b | yes |
| directory of Apple `.emlx` | source c | yes |
| zip of `.eml` | source a | yes |
| tar.gz containing a Maildir | source a | yes |
| gzip-compressed mbox | source d | yes |
| Outlook PST | **only Outlook can write one** (no packaged Linux tool does) | only with `--extra DIR/fixture.pst` |
| Outlook `.msg` | **only Outlook can write one**; six real ones ship as test files in the CPAN tarball `Email-Outlook-Message-0.921.tar.gz` (`t/files/*.msg`) and were used for the 1.3.1 run | only with `--extra DIR/msg/*.msg` |

For the two Outlook formats, export a junk folder to a `.pst` and save a few junk messages as `.msg` once, keep them
somewhere durable (not in this repo), and pass the directory with `--extra`. Until then the report lists them under
`not_tested`, and they are covered only by the unit tests with a fake `readpst` / `msgconvert`.

## What has to hold

| Group | Check |
|---|---|
| per format | ledger row `done`; detected type from content; imported == fixture size; 0 duplicates; 0 failed; the mail is under `PST/` or `Imported/<type>/`, in `cur/`, read, 0600, owned by the mailbox owner; a search for the fixture token returns exactly the fixture; the source left the inbox and is in `done/`; the next sync ends `ok` and leaves the imported mail alone |
| dedupe | the same file again is `skipped` (sha256); the same messages in another format are all duplicates, including the ones without a Message-ID |
| live sync | every sync ends `ok` with imported folders present; a sync running during an import ends `ok` |
| live mail | the MTA accepted every delivery; each reached the mailbox; each is in the archive exactly once |
| no leak | no `PST` / `Imported` folder on the server; same server folders as before; INBOX grew by exactly the deliveries; no other folder changed |
| the books | per server folder local == server; imported total as expected; local total == server total + imported total; Reconcile says in sync; no retention deletions; no failed ledger rows |
| cleanup | this run's deliveries are removed from the test mailbox (only messages carrying this run's `X-MA-Harness` header) and its counts are back where they started |

A failure prints the check with what was found and the run exits 1.

## Limits

- The container has no systemd; a small stand-in answers the two questions the package scripts and the cron wrapper ask.
  The WebUI and the cron timing are not exercised here (the unit tests cover those).
- It tests the checkout, not what is installed on a host.
- IMAP is implicit TLS only, and the SMTP delivery is unauthenticated: the server has to accept mail for the test
  mailbox from the machine that runs the harness.
