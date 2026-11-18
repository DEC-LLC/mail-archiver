# DEC-LLC Mail-Archiver 1.3.4: administrator guide

This guide is for the person who installs and runs Mail-Archiver. Users read the [user guide](USER-GUIDE.md); every
feature with its limits is in the [feature reference](FEATURES.md).

The commands in this guide were run with packages in throw-away containers: the `.deb` on Debian 13 with systemd,
the `.rpm` on Rocky Linux 9 with systemd. Most were first run with the 1.3.2 packages; a separate reviewer ran a
named set again with the 1.3.3 packages. Statements that could not be run there are marked "not verified in a
container", or "not verified". Everything about Windows was read from the code and the build script, not run.
Which check was run with which version is in [Verification status](#verification-status).

Examples use the host name `archive.example.org`, the system user `alice` and the mail account `bob@example.org`.

## Contents

1. [Requirements](#requirements)
2. [Install](#install)
3. [Upgrade](#upgrade)
4. [Uninstall](#uninstall)
5. [What the package installs](#what-the-package-installs)
6. [First start and first sign-in](#first-start-and-first-sign-in)
7. [Sign-in modes and administrators](#sign-in-modes-and-administrators)
8. [Settings: environment variables](#settings-environment-variables)
9. [Data directory](#data-directory)
10. [TLS for the web interface](#tls-for-the-web-interface)
11. [Private CA for outgoing IMAP connections](#private-ca-for-outgoing-imap-connections)
12. [Reverse proxy](#reverse-proxy)
13. [Accounts and schedules](#accounts-and-schedules)
14. [Mail import administration](#mail-import-administration)
15. [Large-mailbox sync](#large-mailbox-sync)
16. [Backups](#backups)
17. [Logs](#logs)
18. [Permissions check](#permissions-check)
19. [Troubleshooting](#troubleshooting)
20. [Security notes](#security-notes)
21. [Command line](#command-line)
22. [EL 9: known limits in this release](#el-9-known-limits-in-this-release)
23. [Verification status](#verification-status)

## Requirements

| | Debian / Ubuntu | EL 9 | Windows |
|---|---|---|---|
| System | Debian 12 or 13, Ubuntu 22.04 or 24.04, with systemd | Rocky Linux 9 or another EL 9, with systemd | 64-bit Windows |
| Python | 3.9 or later (the distribution's `python3`) | the system `python3` (3.9) | bundled in the EXE |
| Pulled in by the package | `python3-flask`, `gunicorn`, `isync`, `python3-pam`, `libsasl2-modules-kdexoauth2`, `pst-utils`, `libemail-outlook-message-perl`; recommended: `zstd` | `python3-flask`, `python3-gunicorn`, `isync`, `python3-pam`, `cronie`, `readpst`, `msgconvert`; recommended: `zstd`, `cyrus-sasl-xoauth2` | nothing |
| Not pulled in, needed | a cron daemon (`cron`) for the schedules; `openssl` for the certificate | the repositories EPEL and CRB, and one package from the `devel` repository | nothing |
| Network | outgoing IMAP (993, or 143 with STARTTLS); incoming TCP 8443 | the same | outgoing IMAP only |
| Disk | room for the mail, plus the search index, plus imported source files in `done/` | the same | the same, under `%APPDATA%` |

The Debian package was installed and run on Debian 13. On Debian 12, Ubuntu 22.04 and Ubuntu 24.04 only the
dependency resolution was checked (`apt-get install --dry-run`); running it there is not verified in a container.

systemd is required. The install script of the package calls `systemctl`; on a system without systemd (a plain
container, for example) the installation ends with an error.

## Install

Downloads are on GitHub Releases: <https://github.com/DEC-LLC/mail-archiver/releases>. The release has one
`SHA256SUMS` for all its files. The packages are not signed in this release, so that file is the integrity check;
check each download against it first:

```bash
sha256sum -c SHA256SUMS --ignore-missing
```

At the end of the installation the package prints where the data is and how to name administrators:

```
Mail Archiver installed. Data directory: /var/lib/mail-archiver (to change it, use a drop-in: systemctl edit mail-archiver).
Then: systemctl enable --now mail-archiver

Admin access: grant users membership in 'mail-archiver-admins':
  sudo usermod -aG mail-archiver-admins <username>
  (takes effect within a minute; no restart needed)
```

On EL 9 do not follow that message as it stands: sign-in with system accounts does not work there in this release,
and the group is not consulted for built-in accounts. Follow the [EL 9](#el-9) steps instead.

Both points are described here: the drop-in in [Choosing the data directory](#choosing-the-data-directory) (do it
before the first account is added), administrators in
[Sign-in modes and administrators](#sign-in-modes-and-administrators). Membership of the admin group takes effect
within a minute, without a restart.

### Debian and Ubuntu

```bash
sudo apt-get update
sudo apt-get install ./mail-archiver_*.deb cron
sudo systemctl enable --now mail-archiver
```

The package does not start the service by itself. `cron` is named because the package does not depend on it and a
minimal system may not have it; without a cron daemon no scheduled sync or import runs.

Then open `https://archive.example.org:8443` and continue with
[First start and first sign-in](#first-start-and-first-sign-in).

### EL 9

Read [EL 9: known limits in this release](#el-9-known-limits-in-this-release) first. The rpm installs, the service
starts with the packaged unit, and sync, full-text search and mail import work. Sign-in with system accounts does
not work on EL 9 in this release: set built-in accounts.

The rpm needs two programs that the standard EL 9 repositories do not carry:

- `readpst` is in the package `libpst`, which is only in the `devel` repository of Rocky Linux (AppStream has just
  `libpst-libs`). Enable that repository for this one transaction.
- `msgconvert` is in `perl-Email-Outlook-Message`, which no EL repository carries. DEC-LLC builds it from the CPAN
  source and attaches `perl-Email-Outlook-Message-0.921-1.el9.noarch.rpm` and its source rpm to the same GitHub
  release. Its own dependencies come from EPEL and CRB.

```bash
sudo dnf install epel-release
sudo dnf config-manager --set-enabled crb
sudo dnf install --enablerepo=devel libpst \
    ./perl-Email-Outlook-Message-0.921-1.el9.noarch.rpm ./mail-archiver-*.noarch.rpm
```

`cronie`, the cron daemon that runs the schedules, is a dependency of the rpm and is installed with it.

Then set built-in accounts and name the administrators (system-account sign-in does not work on EL 9 in this
release, see the EL 9 section):

```bash
sudo mkdir -p /etc/systemd/system/mail-archiver.service.d
printf '[Service]\nEnvironment=MAIL_ARCHIVER_AUTH=builtin\nEnvironment=MAIL_ARCHIVER_ADMIN_USERS=erin\n' |
    sudo tee /etc/systemd/system/mail-archiver.service.d/el9.conf >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable mail-archiver crond
sudo systemctl restart mail-archiver crond
```

`restart` is used on purpose: `enable --now` does not restart a service that is already running, so a drop-in
written after the service was started would not apply.

Open `https://<host>:8443/`, register the user `erin` first (that name is the administrator; `erin` is an example,
use your own name in the drop-in and here), then the others.
When every user has registered, you can close registration with `MAIL_ARCHIVER_REGISTRATION=closed`; see
[Sign-in modes and administrators](#sign-in-modes-and-administrators).

No `MAIL_ARCHIVER_CA_FILE` is needed for mail servers with a certificate from a public CA: the system CA bundle of
EL (`/etc/pki/tls/certs/ca-bundle.crt`) is found automatically.

Opening TCP 8443 in the host firewall (`firewall-cmd --permanent --add-port=8443/tcp && firewall-cmd --reload` with
firewalld) and SELinux in enforcing mode are not verified in a container.

### Windows

1. Download `MailArchiver-<version>.exe` (for this release `MailArchiver-1.3.4.exe`) and `SHA256SUMS`.
2. Check the file in PowerShell. The first command prints the sha256 of the EXE, the second the line for it in
   `SHA256SUMS`; the hexadecimal value must be the same in both (upper and lower case do not matter):

   ```powershell
   (Get-FileHash .\MailArchiver-<version>.exe -Algorithm SHA256).Hash
   Select-String MailArchiver-<version>.exe .\SHA256SUMS
   ```

3. Double-click the EXE. **The EXE is not code-signed yet**, so Windows SmartScreen warns before the first start
   ("More info", then "Run anyway"). That is why the sha256 check matters.
4. The browser opens `https://127.0.0.1:8400`. The certificate was made by the program on first start, so the browser
   asks once whether to trust it.
5. Choose **Create one** on the login page and register a user.

Nothing is installed. The EXE can be kept anywhere; the data is in `%APPDATA%\MailArchiver\`. The program listens on
this computer only (`127.0.0.1`); other computers cannot reach it.

Settings are environment variables that must be set before the program starts, for example in PowerShell:

```powershell
setx MAIL_ARCHIVER_ADMIN_USERS "alice"
setx MAIL_ARCHIVER_DATA "D:\MailArchive"
```

Then quit the program from its tray icon and start it again.

## Upgrade

### Debian and Ubuntu

```bash
sudo apt-get install ./mail-archiver_*.deb
```

### EL 9

```bash
sudo dnf upgrade ./mail-archiver-*.noarch.rpm
```

(The upgrade from 1.3.3 to 1.3.4 was run with `dnf upgrade` and with `dnf install` on Rocky Linux 9, and with
`apt-get install` on Debian 13: the service restarts on the new version, mail, accounts and settings are unchanged.)

### What an upgrade does

- It replaces `/etc/systemd/system/mail-archiver.service`. Keep your settings in a drop-in
  (`/etc/systemd/system/mail-archiver.service.d/*.conf`), never in the unit file. Drop-ins survive.
- It keeps the data directory, `/opt/mail-archiver/.secret_key` and `/opt/mail-archiver/certs/`.
- It regenerates every user's isync configuration and repairs ownership with `mail-archiver-fix-perms`. The
  regenerated configuration names the system CA bundle, not the file in `MAIL_ARCHIVER_CA_FILE`. If you set that
  variable for a private CA, each account's **Settings** must be saved once after the upgrade or reinstall, on
  Debian as on EL 9; see [Private CA](#private-ca-for-outgoing-imap-connections). Installations that use only
  public CAs, or that added their CA to the operating system's trust store, need nothing.
- It restarts the running service, unless a sync is downloading at that moment. In that case the new files are
  installed, the service keeps running the old version, the Dashboard shows "Mail Archiver update installed but not
  yet running", and you restart when the syncs are done: `sudo systemctl restart mail-archiver`.

### Windows

Quit the running program, replace the EXE with the new one, start it. The data in `%APPDATA%\MailArchiver\` is used
as it is.

## Uninstall

### Debian and Ubuntu

From 1.3.4 removing the package stops and switches off all of its units; no separate stop is needed:

```bash
sudo apt-get remove mail-archiver
```

### EL 9

```bash
sudo dnf remove mail-archiver
```

Run on Debian 13 and Rocky Linux 9 with the 1.3.4 packages: nothing of the package keeps running and no unit stays
enabled or failed. With 1.3.3 and older, stop the units first:
`sudo systemctl disable --now mail-archiver mail-archiver-restart.path mail-archiver-oauth-refresh.timer`.

`dnf remove mail-archiver` also removes the dependencies that were installed with it and that nothing else needs:
`python3-flask`, `python3-gunicorn`, `isync`, `python3-pam`, `zstd` and `cronie`. If `cronie` goes, other cron jobs
of the host stop running. To keep the dependencies:

```bash
sudo dnf remove --noautoremove mail-archiver
```

### What stays behind

Removal never deletes mail. These stay and can be deleted by hand when you are sure:

| Path | Content |
|---|---|
| the data directory (`/var/lib/mail-archiver`) | all archived mail, accounts, search indexes |
| `/opt/mail-archiver/.secret_key` | the key that decrypts the stored mail passwords |
| `/opt/mail-archiver/certs/` | the web interface certificate |
| `/opt/mail-archiver/__pycache__` | compiled Python files, made at installation and not removed with the package; safe to delete |
| `/etc/systemd/system/mail-archiver.service.d/` | your drop-ins |
| `/etc/mail-archiver/` | your `tls-sources.conf` and CA file, if you made them |
| `/var/log/mail-archiver-*.log` | cron logs |
| user `mail-archiver`, groups `mail-archiver` and `mail-archiver-admins` | |

Dependencies: on Debian and Ubuntu, `apt-get remove mail-archiver` leaves the packages that were pulled in as
dependencies installed (`apt-get autoremove` removes them later). On EL 9, `dnf remove` removes them together with
the package unless `--noautoremove` is given; see above.

### Windows

Quit the program and delete the EXE. Delete `%APPDATA%\MailArchiver\` to remove the archive, the key and the logs.

## What the package installs

| Path | What |
|---|---|
| `/opt/mail-archiver/` | the application: Python modules, `templates/`, `static/`, `VERSION`, `BUILD_INFO`, `gunicorn.conf.py`, `generate-cert.sh`, `install-tls-cert.sh`, `tls-sources.conf.example` |
| `/opt/mail-archiver/.secret_key` | created at install, `root:mail-archiver`, mode 0640 |
| `/opt/mail-archiver/certs/` | created at install, owned by `mail-archiver`, mode 0750 |
| `/etc/systemd/system/mail-archiver.service` | the web service (gunicorn) |
| `mail-archiver-restart.path`, `mail-archiver-restart.service` | restart on request from the Admin page |
| `mail-archiver-oauth-refresh.timer`, `mail-archiver-oauth-refresh.service` | background refresh of Microsoft tokens; see the note below |
| `mail-archiver-syncs.slice` | a systemd slice, reserved for resource limits |
| `/usr/lib/tmpfiles.d/mail-archiver.conf` | creates `/run/mail-archiver` and `/run/mail-archiver/sync-jobs` (0770 `root:mail-archiver`) |
| `/etc/cron.d/mail-archiver` | the three hourly jobs |
| `/usr/libexec/mail-archiver-scheduled-sync` | wrapper that runs a job with the service's settings |
| `/usr/libexec/mail-archiver-oauth-refresh` | wrapper that runs the token refresh with the service's settings (since 1.3.4) |
| `/usr/libexec/mail-archiver-env` | prints the service's `MAIL_ARCHIVER_*` settings for the two wrappers (since 1.3.4) |
| `/usr/libexec/mail-archiver-cred` | hands a stored password to isync |
| `/usr/libexec/mail-archiver-fix-perms` | ownership and mode check |
| `/var/log/mail-archiver-sync.log`, `-import.log`, `-bulk-sync.log` | created empty, `root:mail-archiver`, mode 0640 |

The five `mail-archiver-*` helper units are in `/lib/systemd/system` on Debian and `/usr/lib/systemd/system` on EL.
List everything with `dpkg -L mail-archiver` or `rpm -ql mail-archiver`.

**Users and groups.** The install script creates the system user `mail-archiver` (home `/var/lib/mail-archiver`, no
login shell), the group `mail-archiver` and the empty group `mail-archiver-admins`. It adds every existing local
account with a uid from 1000 to 59999 and a login shell to the group `mail-archiver`.

**Service unit.** `mail-archiver.service` runs
`/usr/bin/gunicorn --config /opt/mail-archiver/gunicorn.conf.py app:app` as user `mail-archiver` in
`/opt/mail-archiver`. Before it starts, `generate-cert.sh` makes a self-signed certificate if there is none and
`install-tls-cert.sh` looks for a CA-issued one. The unit sets `MAIL_ARCHIVER_SECRET_FILE`, `MAIL_ARCHIVER_AUTH=pam`,
`MAIL_ARCHIVER_HTTPS=1`, `MAIL_ARCHIVER_CERT_DIR` and `MAIL_ARCHIVER_DATA=/var/lib/mail-archiver`. It keeps six
capabilities (`CAP_CHOWN`, `CAP_DAC_OVERRIDE`, `CAP_DAC_READ_SEARCH`, `CAP_FOWNER`, `CAP_SETUID`, `CAP_SETGID`),
because with system accounts it must write into each user's folder and start the download as that user.

**Cron file.** `/etc/cron.d/mail-archiver`, all three as root, every hour:

| Minute | Command | Log |
|---|---|---|
| 0 | `/usr/libexec/mail-archiver-scheduled-sync` | `/var/log/mail-archiver-sync.log` |
| 20 | `/usr/libexec/mail-archiver-scheduled-sync bulk-sync` | `/var/log/mail-archiver-bulk-sync.log` |
| 30 | `/usr/libexec/mail-archiver-scheduled-sync import` | `/var/log/mail-archiver-import.log` |

The wrapper reads the `MAIL_ARCHIVER_*` settings of `mail-archiver.service`, drop-ins included, so the cron jobs use
the same data directory and key as the web interface.

**Note on the token refresh timer.** The package switches on a timer that starts
`mail-archiver-oauth-refresh.service` every 10 minutes. From 1.3.4 the unit starts through
`/usr/libexec/mail-archiver-oauth-refresh`, which reads the `MAIL_ARCHIVER_*` settings of `mail-archiver.service`
the same way the cron wrapper does, so it uses the same data directory wherever that is. It runs as the
`mail-archiver` user with three file capabilities (`CAP_CHOWN`, `CAP_DAC_OVERRIDE`, `CAP_FOWNER`), which it needs to
store a token in a user's own configuration directory when system accounts are used. Those capabilities are broad,
so the unit is closed off from what it has no business with: `/usr`, `/boot`, `/etc`, the program directory,
`/var/spool`, the package database and systemd's own unit directories are read-only for it, and the password files
(`/etc/shadow`, `/etc/gshadow`), `/root`, the sudo configuration and the system's private key directories are hidden
from it.

Up to 1.3.3 the run failed on every installation whose data directory is not the built-in default
(`MAIL_ARCHIVER_DATA=... does not exist`, or `status=226/NAMESPACE` before 1.3.3). That was harmless for syncing,
because Microsoft tokens are refreshed before every sync anyway.

The timer is switched on at the first installation only. An upgrade leaves it as it was, so if you switched it off
on an older version, switch it on again after upgrading to 1.3.4:

```bash
sudo systemctl enable --now mail-archiver-oauth-refresh.timer
```

Both wrappers take the settings through `/usr/libexec/mail-archiver-env`, which reads the `Environment=` lines of
the unit and its drop-ins with systemd's quoting undone, so a value that systemd quotes (one with a `$`, a quote or
a space) arrives as the service has it; `sudo /usr/libexec/mail-archiver-env` prints what they get. Settings given
through `EnvironmentFile=`, and removals through `UnsetEnvironment=`, are not seen by them. If the unit is not
loaded, or its settings cannot be read or understood, a wrapper stops with a message in its log
(`cannot take the settings of mail-archiver.service`) and does nothing, instead of running with the built-in
defaults.

**A data directory whose path contains a space is not supported.** The settings are carried correctly, but the
isync configuration is written without quoting, so every sync fails.

**Licence files.** The rpm installs `LICENSE` and `NOTICE` as licence files (`rpm -qL mail-archiver` lists them);
the deb installs `/usr/share/doc/mail-archiver/copyright` and `/usr/share/doc/mail-archiver/NOTICE`.

**Not installed.** No logrotate file. No firewall rule.

**Windows.** One EXE. It writes only under `%APPDATA%\MailArchiver\`: the archive, `.secret_key`, `certs\`,
`logs\mail-archiver.log` (5 files of 2 MB) and `launcher.json`.

## First start and first sign-in

1. Start the service: `sudo systemctl enable --now mail-archiver`. Check it:

   ```bash
   systemctl is-active mail-archiver
   sudo journalctl -u mail-archiver -n 20
   ```

   The journal shows the certificate being made and gunicorn "Listening at: https://0.0.0.0:8443".
2. Open `https://archive.example.org:8443`. With the self-signed certificate the browser warns once.
3. Sign in.
   - Debian and Ubuntu (system accounts): use an account of the host that has a password, for example the one
     you use for SSH.
   - Built-in accounts (EL 9, Windows): choose **Create one** and register.
4. Make yourself an administrator. With system accounts: `sudo gpasswd -a alice mail-archiver-admins` (the package
   message shows `usermod -aG mail-archiver-admins alice`, which does the same). The **Admin** entries in the
   sidebar become active within a minute, without a new sign-in or a restart. With built-in accounts the
   administrators are the names in `MAIL_ARCHIVER_ADMIN_USERS`, not the group; see the next section.
5. Add a mail account (**Add email account**) and press **Sync Now**.

If the same host name already serves another site that sends an HSTS header, browsers refuse the self-signed
certificate without offering an exception. Install a CA-issued certificate; see
[TLS for the web interface](#tls-for-the-web-interface).

## Sign-in modes and administrators

`MAIL_ARCHIVER_AUTH` selects the mode.

| | `pam`: system accounts | `builtin`: built-in accounts |
|---|---|---|
| Users are | accounts of the host, checked through PAM's `login` service | registered on the login page; stored in `<data>/.users.json` (salted PBKDF2-SHA256 hashes) |
| Mail files are owned by | each user's system account; the download runs as that user | the service account `mail-archiver` |
| isync configuration | `~/.mbsyncrc` in the user's home directory | `<data>/<user>/.mbsyncrc` |
| Choose it when | the host already has an account per person (a file server, a NAS) and people should reach their Maildir with their own account | there are no system accounts for the users, or on EL 9 and Windows |
| Self-registration | no | yes, by anyone who can reach the login page, until `MAIL_ARCHIVER_REGISTRATION=closed` is set |

To switch a Debian or Ubuntu installation to built-in accounts:

```bash
sudo mkdir -p /etc/systemd/system/mail-archiver.service.d
sudo tee /etc/systemd/system/mail-archiver.service.d/auth.conf >/dev/null <<'EOF'
[Service]
Environment=MAIL_ARCHIVER_AUTH=builtin
EOF
sudo systemctl daemon-reload && sudo systemctl restart mail-archiver
```

Choose the mode before users add accounts. Archives are not moved or re-owned when the mode changes.

> **Warning: switching from system accounts to built-in accounts makes every existing archive claimable.** A
> built-in user reads the archive folder `<data>/<user name>`. After the switch nobody is registered yet, and
> registration is open. Whoever registers a built-in user with the name of an existing folder reads that archive:
> the mail, and the accounts stored with it.
>
> Do one of these:
>
> - **Register every existing user name yourself first**, before anyone else can reach the login page (for example
>   with the port closed in the host firewall). List the names with `sudo ls /var/lib/mail-archiver`. Then set
>   `MAIL_ARCHIVER_REGISTRATION=closed`, restart, and hand each person their password. A built-in user name
>   consists of letters and digits only and is stored in lower case; a folder with any other name cannot be
>   registered by anyone.
> - **Or start built-in mode on an empty data directory** (see
>   [Choosing the data directory](#choosing-the-data-directory)) and leave the old archives where they are.

To close registration when all accounts exist:

```ini
[Service]
Environment=MAIL_ARCHIVER_REGISTRATION=closed
```

Put it in a drop-in, then `sudo systemctl daemon-reload && sudo systemctl restart mail-archiver`. The login page
then has no "Create one" link, and the registration address answers "Registration is closed on this installation.
Ask its administrator for an account." To add a user later, open registration again for that moment.

**System accounts created after the installation.** Add each new user to the group `mail-archiver`:

```bash
sudo usermod -aG mail-archiver alice
```

Without it **Sync Now** works for that user, but the hourly scheduled sync fails with "The mail server rejected the
login", because the cron job cannot read the key that decrypts the stored password. The `bulk-sync` commands run
for that user fail the same way, with "no persistent secret key available".

**Administrators (Linux).** Members of `mail-archiver-admins`:

```bash
sudo gpasswd -a alice mail-archiver-admins     # takes effect within 60 seconds
id alice
```

`MAIL_ARCHIVER_ADMIN_GROUP` names a different group. If the configured group does not exist at all, the groups
`sudo` and then `wheel` are used. That fallback does not apply to an existing, empty group.

**Administrators (built-in accounts: Windows, and Linux with `MAIL_ARCHIVER_AUTH=builtin`).** The users named in
`MAIL_ARCHIVER_ADMIN_USERS` (comma-separated). By default nobody. The admin group is not consulted for built-in
accounts (since 1.3.3; before, a built-in user was an administrator when a system account of the same name was in
the group). On Linux set it in a drop-in of the service:

```ini
[Service]
Environment=MAIL_ARCHIVER_ADMIN_USERS=erin
```

Register the administrator's user name yourself before giving others access to the login page: registration is
open by default, and the first person to register a name owns it. The variable is ignored with system accounts.
After an upgrade to 1.3.3 of a Linux installation with built-in accounts, nobody is an administrator until this
variable is set.

**Resetting a built-in password.** There is no page for it. Remove the user's entry from `.users.json` and let the
user register again with the same name; the archive under that name is kept.

```bash
sudo systemctl stop mail-archiver
sudo python3 - <<'EOF'
import json
p = "/var/lib/mail-archiver/.users.json"
users = json.load(open(p))
users.pop("erin", None)
json.dump(users, open(p, "w"), indent=2)
EOF
sudo systemctl start mail-archiver
```

## Settings: environment variables

On Linux, set them in a drop-in file of the service, never in the packaged unit file, and restart:

```bash
sudo mkdir -p /etc/systemd/system/mail-archiver.service.d
sudo tee /etc/systemd/system/mail-archiver.service.d/port.conf >/dev/null <<'EOF'
[Service]
Environment=MAIL_ARCHIVER_PORT=9443
EOF
sudo systemctl daemon-reload && sudo systemctl restart mail-archiver
sudo /usr/libexec/mail-archiver-env
```

The last command prints the settings in effect. Every example in this guide writes **its own file** in that
directory (`auth.conf`, `port.conf`, `data.conf`, `tls.conf`, `ca.conf`, `el9.conf`), each complete with its
`[Service]` line. systemd applies all `*.conf` files there together, so one example does not undo another; to take
a setting back, delete its file, then `daemon-reload` and restart. `sudo systemctl edit mail-archiver` edits one
more such file (`override.conf`) in an editor; it is interactive and was not run in a container.

The cron jobs pick the same values up through the wrapper. "Unit" in the default column means the packaged unit
file sets it.

### General

| Variable | Default | Meaning |
|---|---|---|
| `MAIL_ARCHIVER_DATA` | unit: `/var/lib/mail-archiver`; Windows: `%APPDATA%\MailArchiver`; in the code, when unset on Linux: `/datapool/email-archive` | data directory |
| `MAIL_ARCHIVER_AUTH` | unit: `pam`; Windows: always `builtin` | `pam` or `builtin` |
| `MAIL_ARCHIVER_PORT` | 8443 when a certificate is present, else 8400; Windows: 8400 | listening port |
| `MAIL_ARCHIVER_HTTPS` | unit: `1` | `1`: a request that arrived without TLS is redirected to `https://`. A proxy that sends `X-Forwarded-Proto: https` is accepted as TLS. It does not switch the listener |
| `MAIL_ARCHIVER_CERT_DIR` | `/opt/mail-archiver/certs` | where gunicorn looks for `mail-archiver.crt` and `mail-archiver.key`; with both present it serves HTTPS |
| `MAIL_ARCHIVER_SECRET_FILE` | unit: `/opt/mail-archiver/.secret_key` | file with the secret key (session cookies, encryption of stored mail passwords) |
| `MAIL_ARCHIVER_SECRET` | none | the secret key itself, used when no key file is readable. With neither, sessions work but mail passwords cannot be stored |
| `MAIL_ARCHIVER_CA_FILE` | the system CA bundle: `/etc/ssl/certs/ca-certificates.crt` (Debian, Ubuntu) or `/etc/pki/tls/certs/ca-bundle.crt` (EL), whichever exists | CA file for outgoing IMAP; needed only for a private CA, see [Private CA](#private-ca-for-outgoing-imap-connections) |
| `MAIL_ARCHIVER_CRED_HELPER` | `/usr/libexec/mail-archiver-cred` | program isync calls for a stored password |
| `MAIL_ARCHIVER_RUN_DIR` | `/run/mail-archiver`; Windows: `.run` in the data directory | sync job state files and restart markers |
| `MAIL_ARCHIVER_HOST` | `0.0.0.0`; Windows `127.0.0.1` | bind address, only when `app.py` is started directly without gunicorn |

### Administrators

| Variable | Default | Meaning |
|---|---|---|
| `MAIL_ARCHIVER_ADMIN_GROUP` | `mail-archiver-admins` | Linux: group whose members are administrators |
| `MAIL_ARCHIVER_ADMIN_USERS` | empty | built-in accounts (Windows, or Linux with `MAIL_ARCHIVER_AUTH=builtin`): administrator user names, comma-separated |
| `MAIL_ARCHIVER_REGISTRATION` | `open` | built-in accounts: `closed` turns self-registration off (the login page then has no "Create one" link and new registrations are refused). Any other value means open. Since 1.3.3 |

### Web server (gunicorn, Linux)

| Variable | Default | Meaning |
|---|---|---|
| `MAIL_ARCHIVER_WORKERS` | 2 | worker processes |
| `MAIL_ARCHIVER_THREADS_BASE` | 4 | threads per worker before accounts are counted |
| `MAIL_ARCHIVER_THREADS_MAX` | 24 | upper limit of threads per worker. Threads = base + number of mail accounts, capped here; each open progress panel holds one thread |

### Web interface certificate (`install-tls-cert.sh`, Linux)

| Variable | Default | Meaning |
|---|---|---|
| `MAIL_ARCHIVER_TLS_CERT` | none | certificate (full chain) to install; wins over discovery |
| `MAIL_ARCHIVER_TLS_KEY` | found automatically | its private key |
| `MAIL_ARCHIVER_TLS_SOURCES` | `/etc/mail-archiver/tls-sources.conf` | file with extra certificate locations, one glob per line |

### Sync

| Variable | Default | Meaning |
|---|---|---|
| `MAIL_ARCHIVER_SCHEDULE_START_HOUR` | 0 | hour of the first schedule slot on every mail server (0 to 23) |
| `MAIL_ARCHIVER_MAX_PARALLEL_SYNCS` | 4 | mail servers synced at the same time by a scheduled run |
| `MAIL_ARCHIVER_BATCH_THRESHOLD` | 5000 | **Sync Now**: accounts with more messages are synced in folder batches; 0 switches batching off |
| `MAIL_ARCHIVER_CHUNK_THRESHOLD` | 2500 | a single folder with more messages is downloaded in time-boxed chunks |
| `MAIL_ARCHIVER_CHUNK_WALL_SECONDS` | 180 | length of one chunk |

### Mail import

| Variable | Default | Meaning |
|---|---|---|
| `MAIL_ARCHIVER_PST_MAX_BYTES` | 53687091200 (50 GiB) | largest file or directory accepted |
| `MAIL_ARCHIVER_IMPORT_MAX_BYTES` | value of `MAIL_ARCHIVER_PST_MAX_BYTES` | the same limit for import folders and upload; overrides it |
| `MAIL_ARCHIVER_PST_MAX_MESSAGE_BYTES` | 268435456 (256 MiB) | larger single messages are skipped and counted |
| `MAIL_ARCHIVER_PST_TIMEOUT` | 21600 | time limit of `readpst`, seconds |
| `MAIL_ARCHIVER_PST_STABLE_WAIT` | 30 | seconds between the two checks that a waiting file has stopped changing |
| `MAIL_ARCHIVER_READPST` | `readpst` | the readpst program |
| `MAIL_ARCHIVER_MSGCONVERT` | `msgconvert` | the msgconvert program |
| `MAIL_ARCHIVER_ZSTD` | `zstd` | the zstd program |
| `MAIL_ARCHIVER_IMPORT_MAX_UNPACK_BYTES` | 107374182400 (100 GiB) | most an archive may unpack to |
| `MAIL_ARCHIVER_IMPORT_MAX_RATIO` | 200 | largest expansion ratio of an archive |
| `MAIL_ARCHIVER_IMPORT_RATIO_FLOOR` | 67108864 (64 MiB) | the ratio is judged only above this much output |
| `MAIL_ARCHIVER_IMPORT_MAX_MEMBERS` | 2000000 | most files in one archive |

### Windows launcher only

| Variable | Default | Meaning |
|---|---|---|
| `MAIL_ARCHIVER_TLS` | on | `off` (or `0`, `no`, `false`): serve plain HTTP on `127.0.0.1` |
| `MAIL_ARCHIVER_NO_BROWSER` | unset | `1`: do not open the browser at start (same as `--no-browser`) |

`MAIL_ARCHIVER_JOB_ID` appears in the environment of a download process. The application sets it; it is not a
setting.

## Data directory

```
<data>/                                     /var/lib/mail-archiver by default
  .users.json                               built-in accounts (builtin mode only)
  .oauth2_config.json                       server-wide Microsoft OAuth apps
  <user>/
    .search_index.db                        full-text index and import ledger (SQLite)
    .mbsyncrc                               isync configuration (builtin mode; with system accounts it is ~/.mbsyncrc)
    .config/
      accounts.json                         the user's mail accounts and their settings
      <account>.pass                        encrypted password of an account
      <account>.oauth2.json, <account>.token   Microsoft tokens of an account
      <account>.cert.pem, <account>.key.pem    uploaded client certificate and key (extension as uploaded)
      oauth_apps.json                       the user's private Microsoft OAuth apps
      sync_status.json                      last sync result per account
      mail_import.json                      import schedule and retention settings
      mail_import_state.json                when the last scheduled import ran
      bulk_sync_<account>.json              initial / rebase sync job of an account
      bulk_sync.log                         log of the user's initial / rebase sync runs
    <account>/                              the archive of one account, Maildir
      INBOX/cur  INBOX/new  INBOX/tmp
      <folder>/cur ...
      PST/...  Imported/...                 imported mail
    _import/
      <account>/inbox/                      files waiting to be imported
      <account>/inbox/.incoming/            staging; ignored by the importer
      <account>/done/                       imported files and their .sha256
      <account>/failed/                     refused files and their .reason.txt
      watch/  archive/                      older PST-only layout, still read
      .work/  .state/                       private work directories
```

`<account>` is the address with `@` written as `_at_` and every dot as `_`: `bob@example.org` is
`bob_at_example_org`.

Message files are plain Maildir files. With system accounts they belong to the user (directories 0700 for imported
mail), so the user can also read them with any Maildir program or over a file share.

### Choosing the data directory

The data directory is `/var/lib/mail-archiver` unless you set another one. **Choose it before the first mail
account is added.** For a new installation without accounts this is supported:

```bash
sudo mkdir -p /srv/mail-archive
sudo mkdir -p /etc/systemd/system/mail-archiver.service.d
sudo tee /etc/systemd/system/mail-archiver.service.d/data.conf >/dev/null <<'EOF'
[Service]
Environment=MAIL_ARCHIVER_DATA=/srv/mail-archive
ReadWritePaths=/srv/mail-archive
EOF
sudo systemctl daemon-reload && sudo systemctl restart mail-archiver
```

The **About** page shows the path in use under "Data path". The directory must exist before the service starts;
systemd refuses to start the unit when a path in `ReadWritePaths` is missing (`status=226/NAMESPACE`).

On a host with SELinux in enforcing mode the new path may need a file context
(`semanage fcontext` and `restorecon`); not verified in a container.

### Moving a data directory that already holds mail

**Not supported by a documented procedure in this release.** Stopping the service, moving the files and setting
`MAIL_ARCHIVER_DATA` as above is not enough and breaks an existing archive:

- The search index stores the absolute path of every message. After the move the results still point to the old
  directory, and the viewer answers 403 for them.
- Each user's isync configuration keeps the old `Path`. **Sync Now** then fails with "Maildir error: cannot open
  store", or, if the old directory still exists, isync silently writes new mail there.

What is known to be needed in addition, **not verified**:

- every account's **Settings** saved once by its user, which writes the isync configuration again with the new
  path;
- each user's search index rebuilt. The only way the program offers is the **Rebuild the search index** button on
  the **Import mail** page, and that page shows the button only when it finds the index damaged.

Whether these two steps are sufficient was not tested, and other stored state (import ledger entries, sync job
files) was not examined. If you must move an archive, keep a complete backup of the old directory and the key file
(see [Backups](#backups)) until every user has checked search, the viewer and a sync. Where the storage allows it,
mounting the new storage at the old path avoids the move.

## TLS for the web interface

The service always listens with TLS on port 8443 once a certificate exists in `/opt/mail-archiver/certs/`.

**Self-signed default.** At the first start `generate-cert.sh` makes `mail-archiver.crt` and `mail-archiver.key`:
RSA 2048, valid 10 years, for the host's full name, `localhost` and `127.0.0.1`. It never overwrites an existing
pair.

**Using a certificate from a CA.** `install-tls-cert.sh` runs at every service start. It installs a certificate
only if it is issued by a CA (not self-signed), valid for at least one more day, matches its private key and carries
this host's full name (`hostname -f`) in its subject alternative names. The newest such certificate wins. The first
time, the self-signed pair is kept as `*.self-signed.bak`. Where it looks, in order:

1. `MAIL_ARCHIVER_TLS_CERT` and `MAIL_ARCHIVER_TLS_KEY`. An explicit pair always wins.
2. Globs in `/etc/mail-archiver/tls-sources.conf`, one per line.
3. Built-in locations: `/etc/letsencrypt/live/*/fullchain.pem` (certbot), `/etc/ssl/certs/<name>.crt` and `.pem`,
   `/etc/pki/tls/certs/<name>.crt`, `/etc/cockpit/ws-certs.d/*.cert` and `*.crt`,
   `/etc/ssl/certs/openmediavault-*.crt`. `<name>` is the full or the short host name.

The private key is found by itself: next to the certificate, then in `/etc/ssl/private` and
`/etc/pki/tls/private`. Every candidate is confirmed by comparing public keys.

Explicit pair:

```bash
sudo mkdir -p /etc/systemd/system/mail-archiver.service.d
sudo tee /etc/systemd/system/mail-archiver.service.d/tls.conf >/dev/null <<'EOF'
[Service]
Environment=MAIL_ARCHIVER_TLS_CERT=/etc/ssl/example/archive.fullchain.pem
Environment=MAIL_ARCHIVER_TLS_KEY=/etc/ssl/example/archive.key
EOF
sudo systemctl daemon-reload && sudo systemctl restart mail-archiver
```

Or a location file:

```bash
sudo mkdir -p /etc/mail-archiver
sudo cp /opt/mail-archiver/tls-sources.conf.example /etc/mail-archiver/tls-sources.conf
echo '/etc/ssl/example/*.fullchain.pem' | sudo tee -a /etc/mail-archiver/tls-sources.conf
sudo systemctl restart mail-archiver
```

See what was chosen:

```bash
sudo journalctl -u mail-archiver | grep install-tls-cert
```

Use a file that holds the full chain (server certificate, then intermediates). A renewed certificate is picked up at
the next restart of the service, so restart it from the renewal hook of your certificate tool. With certbot the
default layout is found without configuration; certbot itself was not run in a container.

**Changing the port.** `Environment=MAIL_ARCHIVER_PORT=9443` in the drop-in, then restart.

**Windows.** The program makes its own certificate for `localhost` and `127.0.0.1` and keeps it in
`%APPDATA%\MailArchiver\certs\`. Put your own `mail-archiver.crt` and `mail-archiver.key` there to replace it. If no
certificate can be made, or with `MAIL_ARCHIVER_TLS=off`, the address is `http://127.0.0.1:8400`.

## Private CA for outgoing IMAP connections

Both sync engines verify the mail server's certificate and host name. If your IMAP server has a certificate from a
private CA, the archive must trust that CA.

**Recommended on Linux: add the CA to the operating system's trust store.** It then applies to every connection the
archive makes.

```bash
# Debian, Ubuntu
sudo cp example-private-ca.crt /usr/local/share/ca-certificates/example-private-ca.crt
sudo update-ca-certificates

# EL 9
sudo cp example-private-ca.crt /etc/pki/ca-trust/source/anchors/example-private-ca.crt
sudo update-ca-trust
```

The CA certificate must be a proper CA certificate (basic constraints `CA:TRUE`, key usage `keyCertSign`); current
Python versions reject others.

**`MAIL_ARCHIVER_CA_FILE`.** Names one PEM file with CA certificates.

- isync download (Linux): the file is written into each user's isync configuration as `CertificateFile`, in addition
  to the system's certificates. The configuration is written when an account is added or its settings are saved, so
  after changing the variable each user opens each existing account's **Settings** and presses **Save Settings**.
  The file must be readable by the users. **An install, reinstall or upgrade of the package rewrites every user's
  isync configuration with the system CA bundle**, not with this variable; afterwards the accounts must be saved
  again, or syncs to that server fail with a TLS error. This is the same on Debian and on EL 9.
- Built-in IMAP client (the Windows application; a Linux host without isync): the file is trusted in addition to the
  operating system's store.
- With system accounts on Linux, "Check the server now" and the folder measurement before a large sync do **not**
  see this variable in this release. With only `MAIL_ARCHIVER_CA_FILE` set, the download works and "Check the server
  now" fails with `CERTIFICATE_VERIFY_FAILED`. Adding the CA to the operating system's trust store avoids this.

Example (Debian, verified for the download):

```bash
sudo install -D -m 644 example-private-ca.crt /etc/mail-archiver/private-ca.pem
sudo mkdir -p /etc/systemd/system/mail-archiver.service.d
sudo tee /etc/systemd/system/mail-archiver.service.d/ca.conf >/dev/null <<'EOF'
[Service]
Environment=MAIL_ARCHIVER_CA_FILE=/etc/mail-archiver/private-ca.pem
EOF
sudo systemctl daemon-reload && sudo systemctl restart mail-archiver
```

On Windows set `MAIL_ARCHIVER_CA_FILE` to the path of the PEM file before starting the program, or import the CA
into the Windows certificate store.

## Reverse proxy

A reverse proxy is optional. Use one to serve the archive on port 443 or under a name that other services share.
The application keeps listening with TLS on 8443, so the proxy connects to it with HTTPS. The proxy must pass the
`Host` header and `X-Forwarded-Proto`, must not buffer (the sync progress is a server-sent event stream) and must
allow large request bodies (mail import uploads).

nginx, verified on Debian 13:

```nginx
server {
    listen 443 ssl;
    server_name archive.example.org;

    ssl_certificate     /etc/ssl/example/proxy.fullchain.pem;
    ssl_certificate_key /etc/ssl/example/proxy.key;

    client_max_body_size 0;          # mail import uploads can be many GB
    proxy_request_buffering off;     # stream uploads to the application
    proxy_buffering off;             # live sync progress
    proxy_read_timeout 3600s;

    location / {
        proxy_pass https://127.0.0.1:8443;
        proxy_ssl_verify off;        # the hop to 127.0.0.1 uses the local certificate
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto https;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    }
}
```

For any other proxy the same five points apply: HTTPS to `127.0.0.1:8443` without verifying that certificate, the
original `Host` header, `X-Forwarded-Proto: https`, no response buffering and a read time-out of an hour.

Notes:

- The redirect URI of a Microsoft OAuth app is built from the address in the browser. Register the proxy's address
  (`https://archive.example.org/oauth2/callback`).
- The application still listens on `0.0.0.0:8443`. Close that port in the host firewall if all access should go
  through the proxy.
- The "Upload a file" text of the Import mail page says that a reverse proxy also limits the upload size; with
  `client_max_body_size 0` there is no limit besides the application's own.

## Accounts and schedules

Mail accounts belong to users, and only the user can add one: sign in as the user, **Add email account**. There is
no administrator page or command that adds an account for someone else. What the administrator provides:

- the sign-in (a system account, or the address of the page for self-registration);
- group membership for system accounts made after the installation (`usermod -aG mail-archiver <user>`);
- for Microsoft accounts, a server-wide OAuth app on **Server settings** (see the
  [feature reference](FEATURES.md#microsoft-sign-in-oauth2)); the client secret of the Azure registration expires
  and must be renewed there;
- the private CA, if the mail server needs one.

Per provider:

| Provider | What the user needs |
|---|---|
| Gmail | 2-step verification on, then an App Password from the Google account's security page; IMAP is used on `imap.gmail.com:993` |
| Outlook / Hotmail | a configured OAuth app, then "Sign in with Microsoft" |
| Outlook / Hotmail (App Password) | 2-step verification on, then an app password from the Microsoft account's security page |
| Apple iCloud Mail | an app-specific password from the Apple account page |
| Yahoo Mail | 2-step verification on, then an app password from Yahoo's account security page |
| Other IMAP Server | host, port and the account's password |

Whether a provider still offers app passwords for a given account type is the provider's decision and was not
checked for this guide.

**Schedules.** Each account's schedule is the list on its Dashboard card: Manual only, Every hour, Every 6 hours,
Every 12 hours, Daily (default). The hourly cron job starts what is due. Print the plan:

```bash
sudo /usr/libexec/mail-archiver-scheduled-sync sync-schedule
```

```
server                   slot  interval last start  due  account
archive.example.org      00:00 daily    11-17 03:40 no   alice/bob@example.org
```

`MAIL_ARCHIVER_SCHEDULE_START_HOUR` moves the first slot, `MAIL_ARCHIVER_MAX_PARALLEL_SYNCS` limits how many mail
servers are synced at once. How slots are assigned: [feature reference](FEATURES.md#scheduled-sync).

Run the scheduled sync by hand (it starts only accounts that are due):

```bash
sudo /usr/libexec/mail-archiver-scheduled-sync
```

## Mail import administration

Users import on the **Import mail** page. What matters to the administrator:

**Import folders.** `<data>/<user>/_import/<account>/inbox/` is created when the user opens the Import mail page.
With system accounts the folders belong to the user, mode 0750. A file or directory placed in `inbox/` is imported
into that account. The importer refuses a folder that is a symbolic link or does not belong to the owner, and never
follows links inside it.

**Delivering files by program.** Write the file into `inbox/.incoming/`, write `<name>.sha256` in `sha256sum` format
(`<hex>  <name>`), rename the `.sha256` into `inbox/`, then rename the file into `inbox/`. Files must end up owned by
the user. The importer takes an item only when its size and modification time are unchanged across two checks, and
verifies the sidecar when there is one.

**Schedule.** Per user, on the page under **Scheduled import**. It is off until the user ticks "Enabled". The cron
job at minute 30 runs the import for every user whose schedule is due. Run one user's waiting files now:

```bash
sudo /usr/libexec/mail-archiver-scheduled-sync import --user alice --manual
```

```
  old-mail.mbox -> bob@example.org (import folder)
  old-mail.mbox: done (mbox) - imported 1, duplicates 1, failed 0
```

**Ledger.**

```bash
sudo /usr/libexec/mail-archiver-scheduled-sync import --user alice --ledger
sudo /usr/libexec/mail-archiver-scheduled-sync import --user alice --ledger --events     # retention log
sudo /usr/libexec/mail-archiver-scheduled-sync import --user alice --ledger --limit 10 --json
```

**Failed files** stay in `<account>/failed/` with `<name>.reason.txt` until a person removes them or moves them
back (**Move back to the inbox** on the page). Nothing there is deleted by the importer.

**Retention.** Imported source files are kept in `<account>/done/`. Only dated series are thinned out, by the rules
R1 to R6 in the [feature reference](FEATURES.md#mail-import). One-off files stay until you delete them, so watch the
disk when users upload large PST files: a PST is kept (compressed with zstd) in addition to the mail imported from
it.

**Tools.** `readpst` (PST/OST), `msgconvert` (`.msg`) and `zstd` (`.zst` archives, compression of archived PST and
mbox files). The Debian package depends on the first two and recommends `zstd`. When a tool is missing, the files
that need it are refused with a message and everything else is imported.

**Limits** are environment variables; see [Mail import](#mail-import) in the settings tables.

## Large-mailbox sync

For a mailbox whose first sync does not finish in a day (a large Gmail "All Mail"), the user sets up an
**Initial / rebase sync** job. It is described in the [feature reference](FEATURES.md#initial--rebase-sync-for-very-large-folders)
and the [user guide](USER-GUIDE.md#first-sync-of-a-very-large-mailbox). For the administrator:

- The cron job at minute 20 starts the runs. Its log is `/var/log/mail-archiver-bulk-sync.log`; each user also has
  `<data>/<user>/.config/bulk_sync.log`.
- A run needs isync and lasts at most 6 hours.
- While a job is open the regular sync leaves the job's folders out.

```bash
sudo /usr/libexec/mail-archiver-scheduled-sync bulk-sync --list
sudo /usr/libexec/mail-archiver-scheduled-sync bulk-sync --manual --user alice --account bob@example.org
sudo /usr/libexec/mail-archiver-scheduled-sync bulk-sync --reconcile --user alice --account bob@example.org
sudo /usr/libexec/mail-archiver-scheduled-sync bulk-sync --reconcile --user alice --account bob@example.org --folder INBOX
```

`--manual` runs the account's job once now and ignores the time window, not the budget. `--reconcile` is the
read-only comparison of server and archive ("Check the server now"); add `--json` for machine-readable output.

If Gmail closes a regular sync with its daily limit, the status says "Gmail's daily IMAP download limit was reached".
Nothing is wrong; the next sync continues. Clicking Sync again does not help. An initial / rebase sync with the
default budget of 2,400 MB per day stays under the limit.

## Backups

**What to back up.**

| Path | Why |
|---|---|
| the data directory | the mail, the accounts, the search indexes with the import ledgers |
| `/opt/mail-archiver/.secret_key` | without it the stored mail passwords (`*.pass`) cannot be decrypted and every user must enter them again |
| `/etc/systemd/system/mail-archiver.service.d/` and `/etc/mail-archiver/` | your settings |
| `/opt/mail-archiver/certs/` | optional; the certificate is made or found again at start |
| `~/.mbsyncrc` of each user (system accounts) | not needed; it is generated |

Keep the key file and the data directory together, and protect the backup like the mail itself: the data directory
holds every user's mail in readable form, encrypted mail passwords and Microsoft refresh tokens.

**What not to copy while a sync or import runs.**

- A folder that isync is writing to: its state files (`.mbsyncstate`, `.mbsyncstate.journal`, `.mbsyncstate.new`,
  `.uidvalidity`) must match the message files next to them. A copy taken in the middle can make a restored archive
  download messages again.
- `.search_index.db` while it is being written. It is a SQLite database; copy it only when nothing writes to it, or
  leave it out and rebuild the index after a restore. Leaving it out loses the import ledger.
- `_import/.work/` and `inbox/.incoming/`: work in progress.

The simple, safe way is a short stop:

```bash
sudo systemctl stop mail-archiver
sudo mkdir -p /etc/systemd/system/mail-archiver.service.d
sudo tar -C / -czf /root/mail-archiver-backup.tar.gz \
    var/lib/mail-archiver opt/mail-archiver/.secret_key opt/mail-archiver/certs \
    etc/systemd/system/mail-archiver.service.d
sudo systemctl start mail-archiver
```

**`var/lib/mail-archiver` is the default data directory.** If you set `MAIL_ARCHIVER_DATA`, put that path there
instead, without the leading `/` (the About page shows it as "Data path"); otherwise the command succeeds and the
archive contains no mail. If `/etc/mail-archiver/` exists, add `etc/mail-archiver` to the list. Check what was
saved:

```bash
sudo tar -tzf /root/mail-archiver-backup.tar.gz | head -20
```

Stopping the service does not stop a download that cron started. Before the backup, check that no sync is
registered:

```bash
grep -lE '"state":"(starting|running|syncing)"' /run/mail-archiver/sync-jobs/*.json
```

No file name in the output means none is running ("No such file or directory" when the directory is empty). The
**Application** admin page shows the same under "Active syncs".

A file-system snapshot of the data directory taken while nothing is syncing is equally good. Restoring to a different
host was not verified in a container.

**Windows.** Quit the program and copy `%APPDATA%\MailArchiver\` (and the data directory, if `MAIL_ARCHIVER_DATA`
points elsewhere).

## Logs

| Where | What |
|---|---|
| `sudo journalctl -u mail-archiver` | the web service: start, certificate choice, gunicorn, token refresh lines (`mail-archiver oauth-refresh:`), Python errors |
| `/var/log/mail-archiver-sync.log` | the hourly scheduled sync: which accounts were due, started, skipped and why |
| `/var/log/mail-archiver-import.log` | the hourly import check |
| `/var/log/mail-archiver-bulk-sync.log` | the hourly initial / rebase sync check |
| `<data>/<user>/.config/bulk_sync.log` | one user's initial / rebase sync runs, with isync's last lines |
| `<data>/<user>/.config/sync_status.json` | last result per account; `raw_error` holds the end of isync's output for a failed sync |
| `/run/mail-archiver/sync-jobs/*.json` | state of running and recently finished syncs (kept about an hour) |
| **Admin**, **Application** | active syncs, recent sync history of all users, failure rate |
| `sudo journalctl -u mail-archiver-restart.service` | restarts requested from the Admin page |

The three files in `/var/log` grow without rotation; add a logrotate rule if they get large. Passwords and tokens are
removed from isync output before it is stored.

**Windows.** `%APPDATA%\MailArchiver\logs\mail-archiver.log`, also reachable from the tray icon ("Open log folder").

## Permissions check

```bash
sudo /usr/libexec/mail-archiver-fix-perms --check
```

```
mail-archiver-fix-perms: ownership and permissions are correct
```

`--check` changes nothing and exits 1 when something is wrong, with one line per finding (`WRONG OWNER`,
`WRITABLE BY OTHERS`, `STALE BYTECODE`). Without `--check` it repairs: everything in `/opt/mail-archiver` except
`certs/` and `.secret_key`, the helpers in `/usr/libexec`, the cron file and the unit files become `root:root`
without group or other write permission, and the compiled Python files are rebuilt by root. The package runs it on
every install and upgrade.

Run the check after anything other than the package manager has touched `/opt/mail-archiver`. Application files that
an ordinary account can write are a way to run code as the service or, through cron, as root.

## Troubleshooting

| Symptom | Cause | Action |
|---|---|---|
| Service does not start, journal: `Failed at step NAMESPACE` | a path in `ReadWritePaths` does not exist (the data directory) | create the directory, restart |
| Service does not start, journal: `Failed at step GROUP`, `Failed to determine supplementary groups` | the rpm before 1.3.3 on EL 9: its unit named the group `shadow`, which EL does not have | upgrade to 1.3.3; its unit starts as packaged |
| Login page has no "Create one" link | registration is closed (`MAIL_ARCHIVER_REGISTRATION=closed`), or the installation uses system accounts | system accounts: sign in with an account of the host. Built-in accounts: the administrator opens registration again to add a user |
| Browser refuses the certificate without an exception button | another site on the same host name sends HSTS | install a CA-issued certificate |
| Login page returns to itself with "Invalid username or password." for a correct system password | PAM cannot check the password: on EL 9 see the EL 9 section; elsewhere check that the unit still has `NoNewPrivileges=false` and the `shadow` group | use built-in accounts on EL 9 |
| Login page says "PAM (system account) sign-in is not available on Windows." | `MAIL_ARCHIVER_AUTH=pam` was set on Windows | remove the variable |
| "Session expired. Please log in again." | sessions last 1 hour | sign in again |
| "403 — Admin access required" | system accounts: the user is not in the admin group. Built-in accounts: the name is not in `MAIL_ARCHIVER_ADMIN_USERS` | `gpasswd -a <user> mail-archiver-admins`, wait a minute; or set the variable in a drop-in and restart |
| "A sync of ... is already running (started ...). Your click was ignored and nothing was queued." | a sync of that account, or Sync All, is in progress | wait; a first sync of a large mailbox can take hours |
| "A sync of ... is still registered but has reported nothing for over 30 minutes. A new sync was NOT started" | a stalled download | look at **Admin**, **Application**; if no `mbsync` process of that user is running, restart the service, which clears the dead job |
| Status **Interrupted** | the service was restarted or the download ended while the status said "Syncing..." | nothing; the next sync continues |
| Dashboard banner "Mail Archiver update installed but not yet running." | the package was upgraded during a sync | `sudo systemctl restart mail-archiver` when the syncs are done |
| "The mail server rejected the login." on **Sync Now** | wrong password, or the provider wants an app password | re-enter it under **Settings** |
| "The mail server rejected the login." only on scheduled syncs, **Sync Now** works | the system user is not in the group `mail-archiver` | `sudo usermod -aG mail-archiver <user>` |
| "The stored password or token for this account could not be read. Re-enter it in the account's Settings." | the password file is missing or the secret key changed | re-enter the password; check `.secret_key` was restored with the data |
| "Secure (TLS) connection to the mail server failed. ..." | the server's certificate is not trusted (a private CA), or host or port is wrong. After a package upgrade or reinstall: the isync configuration was rewritten with the system CA bundle and no longer names your private CA file. On EL 9 with an rpm before 1.3.3: the system CA bundle was not found | add the CA to the operating system's trust store ([Private CA](#private-ca-for-outgoing-imap-connections)); with `MAIL_ARCHIVER_CA_FILE`, save the account's Settings again; on EL 9 upgrade to 1.3.3 |
| "Check the server now": "The server could not be checked: Could not sign in to the mail server: SSLCertVerificationError ..." | private CA known to isync only | add the CA to the operating system's trust store |
| "This account is set to "Plaintext" (no TLS). The built-in IMAP client does not send a password over an unencrypted connection. ..." | TLS mode Plaintext on Windows, on a host without isync, or in "Check the server now" | choose SSL/TLS (993) or STARTTLS (143) in **Settings** |
| "Could not reach the mail server. Check the network, and that the server name and port are right." | DNS, firewall or a wrong host | check from the host: `openssl s_client -connect imap.example.org:993` |
| "Gmail's daily IMAP download limit was reached, so Google closed the connection." | the daily IMAP allowance is used up | wait for the next scheduled sync; for the first sync use an initial / rebase sync |
| "Not signed in to Microsoft yet. ..." | no token for the account | **Sign in with Microsoft** on the account |
| "Microsoft sign-in expired. Click "Sign in with Microsoft" on the account settings page to renew." | Microsoft rejected the refresh token | sign in again |
| "Microsoft accepted the sign-in, but it belongs to a different mailbox than ..." | the browser was signed in to another Microsoft account | sign out at Microsoft or use a private window, sign in as the right address |
| "No Microsoft OAuth2 app is configured — go to Server Settings → OAuth Apps to add one." | no app registered | add one on **Server settings** |
| "Microsoft login failed: ..." or "OAuth2 error: ..." | Microsoft refused: wrong redirect URI, expired client secret, missing permission | compare the redirect URI shown on **Server settings** with the registration; renew the secret |
| "The archive on the server has a file-permission problem, so mail could not be written." | files in the user's archive belong to another account | make the user's archive folder owned by the user again |
| "Another sync of this account was running at the same time." | two isync processes on the same folders | wait for the other to finish |
| Import mail: "readpst is not installed (package pst-utils): PST files are refused until it is." | `readpst` missing | install `pst-utils` (Debian) or `libpst` (EL) |
| Import mail: "msgconvert is not installed (package libemail-outlook-message-perl): Outlook .msg files are refused until it is." | `msgconvert` missing | install the package |
| Import: file in `failed/`, reason "unknown type: not a mail format this importer knows (PST, mbox, Maildir, eml, emlx, msg, zip, tar)" | the content is not mail | check the file |
| Import: status `skipped` | the same file (sha256) was already imported into this account | nothing to do |
| Import: "An import is already running. It picks up everything that is waiting." | one import per user at a time | wait |
| Import upload: "upload refused: ..." or "The import folders are not used." | an import folder is a link or not the owner's | restore the folder ownership |
| Import mail page: "The import history cannot be read: the index database is damaged" | damaged `.search_index.db` | **Rebuild the search index** on that page |
| Import mail page answers with an error page on EL 9 | a version before 1.3.3 (its search index module did not load on Python 3.9) | upgrade to 1.3.3 |
| Initial / rebase sync: "The initial / rebase sync needs isync (the mbsync program), which was not found on PATH." | Windows without isync | install isync or use the regular sync |
| Initial / rebase sync: "Today's budget is used up." on **Run now** | the daily budget | raise the budget or wait until tomorrow |
| "Upload rejected: file exceeds 256 KiB." | a client certificate upload that is not a certificate | pick the right file |
| No scheduled sync ever runs | no cron daemon, or all accounts on "Manual only" | install and start cron; check `/var/log/mail-archiver-sync.log` |
| `mail-archiver-oauth-refresh.service` in `systemctl --failed` (journal: `MAIL_ARCHIVER_DATA=... does not exist`, or `Failed at step NAMESPACE` before 1.3.3) | version 1.3.3 or older: the token refresh unit did not use the data directory of the main service | upgrade to 1.3.4 or later; harmless for syncing meanwhile (see [What the package installs](#what-the-package-installs)) |
| **Sync Now** fails with "Maildir error: cannot open store", or opened search results answer 403, after the data directory was changed | the isync configuration and the search index still name the old directory | see [Moving a data directory that already holds mail](#moving-a-data-directory-that-already-holds-mail) |

## Security notes

- **HTTPS.** The web interface is served with TLS only. With the self-signed default, users must accept a browser
  warning; install a CA-issued certificate for anything beyond a test.
- **Sessions** end 1 hour after sign-in. Cookies are signed with the secret key. The packaged unit does not set the
  `Secure` cookie attribute; the listener itself offers no plain HTTP.
- **Own-folder rule.** The viewer, the attachment download and the export open only files under the signed-in
  user's own folder. Search reads only the user's own index.
- **What administrators can see.** The admin pages show every user's account addresses, archive sizes, sync states
  and sync error texts, and can restart the service, cancel a sync and refresh a Microsoft token. They do not show
  message content. An administrator of the web interface is not automatically able to read mail; a person with root
  on the host is, because the mail is stored as plain files.
- **Stored secrets.** Mail passwords are encrypted with a key derived from `.secret_key`. Microsoft tokens and the
  client secrets of OAuth apps are stored as files in the data directory, protected by file permissions, not
  encrypted. Protect the host and the backups accordingly.
- **Mail server connections** verify certificate and host name. The built-in IMAP client never sends a password
  without TLS. isync follows the account's TLS mode, so do not choose "Plaintext" on the Linux packages either.
- **Built-in accounts.** Anyone who can reach the login page can register, unless `MAIL_ARCHIVER_REGISTRATION=closed`
  is set (create the accounts first, then close it). Do not expose an installation with open registration to
  networks whose users should not have an archive. Administrators are the names in `MAIL_ARCHIVER_ADMIN_USERS`, on
  every platform; register those names yourself before giving others access.
- **Switching from system accounts to built-in accounts.** A built-in user reads the archive folder of the same
  name. After the switch, whoever registers a user with the name of an existing folder reads that archive. Register
  every existing user name yourself first and then close registration, or start built-in mode on an empty data
  directory; see [Sign-in modes and administrators](#sign-in-modes-and-administrators).
- **Server settings.** Server-wide OAuth apps can be added, edited, deleted or made the default only by an
  administrator (since 1.3.3). Every user manages their own private apps.
- **Browser sessions.** The session cookie is `HttpOnly` and `SameSite=Lax`. The forms carry no CSRF token: do not
  put the application behind a domain that also serves pages you do not control. Signing out is a link, not a form.
- **Privileges.** With system accounts the service keeps capabilities that let it write to any user's files and
  start processes as any user. Downloads and import conversions run as the mailbox owner, and untrusted import files
  are parsed without the service's capabilities. With built-in accounts everything runs as `mail-archiver`, and you
  may add `NoNewPrivileges=true` in a drop-in (verified: the service starts and serves the login page).
- **Application files** must stay root-owned; see [Permissions check](#permissions-check).
- **HTML mail** is shown in a sandboxed frame without scripts. Remote images in a message are loaded by the browser.
- **Windows.** The program listens on `127.0.0.1` only. The EXE is not code-signed yet; verify the sha256.
- **Unsigned packages.** The rpm, the source rpm and the deb are not signed in this release. The package manager
  cannot tell a genuine file from a changed one, so check every download against `SHA256SUMS` from the same release
  page before installing it.
- **Reporting a vulnerability.** Privately, with the form "Report a vulnerability" under the Security tab of the
  repository: <https://github.com/DEC-LLC/mail-archiver/security/advisories/new>. The policy is in
  [SECURITY.md](../SECURITY.md).

## Command line

The application has four subcommands. On an installed system three of them go through the wrapper, which loads the
service's settings and runs `python3 app.py <job>` in `/opt/mail-archiver` as root.

| Purpose | Command |
|---|---|
| Scheduled sync pass (what cron runs at minute 0) | `sudo /usr/libexec/mail-archiver-scheduled-sync` |
| Print the schedule plan | `sudo /usr/libexec/mail-archiver-scheduled-sync sync-schedule` |
| Scheduled import pass (cron, minute 30) | `sudo /usr/libexec/mail-archiver-scheduled-sync import` |
| Import everything waiting for one user | `sudo /usr/libexec/mail-archiver-scheduled-sync import --user alice --manual` |
| Import one waiting file | `sudo /usr/libexec/mail-archiver-scheduled-sync import --user alice --manual --account bob@example.org --file old-mail.mbox` |
| Import ledger, newest first | `sudo /usr/libexec/mail-archiver-scheduled-sync import --user alice --ledger [--limit N] [--json]` |
| Retention log | `sudo /usr/libexec/mail-archiver-scheduled-sync import --user alice --ledger --events` |
| Initial / rebase sync pass (cron, minute 20) | `sudo /usr/libexec/mail-archiver-scheduled-sync bulk-sync` |
| List initial / rebase sync jobs | `sudo /usr/libexec/mail-archiver-scheduled-sync bulk-sync --list` |
| Run one job now | `sudo /usr/libexec/mail-archiver-scheduled-sync bulk-sync --manual --user alice --account bob@example.org` |
| Compare server and archive | `sudo /usr/libexec/mail-archiver-scheduled-sync bulk-sync --reconcile --user alice --account bob@example.org [--folder NAME] [--json]` |

Notes:

- The wrapper accepts `scheduled-sync` (the default), `import` (old name `pst-import`), `bulk-sync` and, from
  1.3.4, `sync-schedule` (prints the plan and starts nothing). Without the service's settings `app.py` looks
  at the wrong data directory and prints an empty plan.
- `import --user` needs `--manual` or `--ledger`. `--account` is used together with `--file`.
- `bulk-sync --manual` and `--reconcile` need both `--user` and `--account`.

Other commands used in this guide:

```bash
systemctl status mail-archiver
sudo systemctl restart mail-archiver
sudo journalctl -u mail-archiver -n 50
sudo /usr/libexec/mail-archiver-env
sudo /usr/libexec/mail-archiver-fix-perms --check
```

Windows: `MailArchiver-<version>.exe --quit` stops the running program; `--no-browser` starts it without opening the
browser.

## EL 9: known limits in this release

Checked on Rocky Linux 9 (system Python 3.9, isync 1.4.4, python3-pam 2.0.2 from EPEL).

| # | What happens | Why | What to do |
|---|---|---|---|
| 1 | sign-in with a system account always answers "Invalid username or password." | the application expects a different Python PAM module than the `python3-pam` of EPEL 9, and on EL the service account cannot check other users' passwords | use built-in accounts: `MAIL_ARCHIVER_AUTH=builtin` in a drop-in, and name the administrators in `MAIL_ARCHIVER_ADMIN_USERS` |
| 2 | `cyrus-sasl-xoauth2` (recommended by the rpm, needed by isync for Microsoft sign-in) is not installed | not found in BaseOS, AppStream, CRB, EPEL or `devel` | Microsoft sign-in on EL 9 is not verified; EPEL has a package named `sasl-xoauth2`, which was not tried |
| 3 | after an upgrade or reinstall, accounts that use a private CA file (`MAIL_ARCHIVER_CA_FILE`) lose it until their Settings are saved again; the same happens on Debian | the package script rewrites each user's isync configuration with the system CA bundle | save each such account's Settings once after the upgrade, or add the CA to the operating system's trust store instead |

Fixed in 1.3.3 (these stopped the 1.3.0 to 1.3.2 rpm from working on EL 9):

- the service did not start (`status=216/GROUP`, then `status=226/NAMESPACE`): the unit named the group `shadow`,
  which EL does not have, and the package created another data directory than the one the unit uses;
- every sync failed its certificate check: only the Debian location of the system CA bundle was known;
- no full-text index, and **Import mail** answered with an error page: the search index module did not load on
  Python 3.9;
- no scheduled runs on a minimal install: `cronie` was not a dependency.

What works on EL 9 with built-in accounts: the installation with its dependencies (`cronie` included), the service
on port 8443 with the packaged unit, registration and sign-in, administrators from `MAIL_ARCHIVER_ADMIN_USERS`,
adding an account, **Sync Now** with isync and the system CA bundle (no `MAIL_ARCHIVER_CA_FILE` for a public CA),
full-text search, export, the viewer, mail import, "Check the server now", the schedule plan, the permissions
check, reinstall and removal of the package.

## Verification status

Most checks were first run with the 1.3.2 packages, for the first version of this guide. With the 1.3.3 packages a
separate reviewer ran the checks listed under "1.3.3" again, in containers with systemd. A check that appears only
in a "1.3.2" list was not repeated with 1.3.3.

**Debian 13, the 1.3.3 `.deb`:** installation and start of the service; sign-in with a system account; the admin
group and its 60-second delay; adding an account against a local test IMAP server; **Sync Now**; search with a
word, a phrase, `OR`, `NOT` and a prefix; mbox and eml export; the viewer, an attachment download and the 403 for a
foreign path; import by upload and by command; the ledger commands; the schedule plan; the scheduled-sync wrapper;
the initial / rebase sync commands `--list` and `--reconcile`; the permissions check; the port drop-in; the switch
to built-in accounts and the administrator rule; Restart from the Admin page; reinstall; uninstall.

**Rocky Linux 9, the 1.3.3 `.rpm`:** `dnf install` with its dependencies; the built-in accounts drop-in;
registration, sign-in and the administrator; adding an account; **Sync Now** with the system CA bundle; full-text
search; export; the viewer; import by upload and by command; "Check the server now"; the schedule plan; the
permissions check; reinstall; removal.

**Debian 13, the 1.3.2 `.deb`** (first run): installation with `apt-get install`; file list,
unit files, cron file, users, groups and log files; `mail-archiver-fix-perms --check`; enabling and starting the
service; sign-in with a system account; the About page; the admin group and the 60-second delay; the admin pages;
Restart from the Admin page; adding an "Other IMAP Server" account against a local Dovecot; **Sync Now** over SSL/TLS
and over STARTTLS; the scheduled sync with and without membership of the group `mail-archiver`; search with words,
phrases, `OR`, prefix, filters and `*`; mbox and eml export; the viewer, an attachment download and the 403 for a
foreign path; import by upload, by import folder and by command; the ledger commands; an unknown file going to
`failed/`; an initial / rebase sync job from set-up to completion; reconcile from the page and the command line;
`MAIL_ARCHIVER_CA_FILE` and the operating system trust store with a private CA; `MAIL_ARCHIVER_TLS_CERT`,
`tls-sources.conf`; `MAIL_ARCHIVER_PORT`; the data directory drop-in (the service starting on the new path; an
archive that already holds mail is in the not-verified list below); built-in accounts and
the password reset; the nginx reverse proxy; the backup command; reinstall and removal. The administrator rule for
built-in accounts changed in 1.3.3 and is covered by the 1.3.3 list only.

**Rocky Linux 9, the 1.3.2 `.rpm`** (first run): the installation, and the defects listed as fixed in the EL 9
section. That rpm did not start as packaged; the statements of this guide about EL 9 rest on the 1.3.3 run above.

**Dependency resolution only** (`apt-get install --dry-run`): Debian 12, Ubuntu 22.04, Ubuntu 24.04.

**Not verified:**

- moving a data directory that already holds mail: the plain move breaks the archive, and the additional steps
  named in [that section](#moving-a-data-directory-that-already-holds-mail) were not tested;
- the token refresh unit refreshing a real Microsoft token (from 1.3.4 its run ends without error on Debian 13 and
  Rocky Linux 9; no installation with a Microsoft account was available to test an actual refresh);
- Microsoft sign-in on EL 9, including the EPEL package `sasl-xoauth2`;
- anything on Windows (read from `windows-app/main.py`, `platform_compat.py` and the modules);
- Microsoft sign-in and every OAuth step (needs a real registration), and syncs against Gmail, Microsoft, iCloud and
  Yahoo; only a local IMAP server was used;
- PST and `.msg` import with real files (the tools were installed; the formats were not exercised);
- an upgrade from a version older than 1.3.2, and the deferred restart during a running sync;
- SELinux, firewalld, certbot;
- the provider limits and budgets of the initial / rebase sync;
- restoring a backup on another host.
