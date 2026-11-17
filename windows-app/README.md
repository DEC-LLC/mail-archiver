# Mail-Archiver for Windows

`MailArchiver-<version>.exe` is the same application the Linux packages ship
(the top-level `app.py` and its modules, `templates/`, `static/`), frozen with
PyInstaller as one windowed EXE, plus the launcher in `main.py`. Nothing is
installed; Python is not needed on the machine that runs it.

Downloads: GitHub Releases, <https://github.com/DEC-LLC/mail-archiver/releases>. The EXE is not code-signed yet:
Windows SmartScreen warns on first start. Verify the sha256 against `SHA256SUMS` from the same release
(`(Get-FileHash .\MailArchiver-<version>.exe -Algorithm SHA256).Hash` must equal the value in the line that
`Select-String MailArchiver-<version>.exe .\SHA256SUMS` prints; that one file covers every file of the release).

Using the application is described in the [user guide](../docs/USER-GUIDE.md); settings are in the
[administrator guide](../docs/ADMIN-GUIDE.md).

## Contents

1. [What the launcher does](#what-the-launcher-does)
2. [Toolchain](#toolchain)
3. [Build](#build)
4. [Source and toolchain record](#source-and-toolchain-record)
5. [What works on Windows, and what is refused](#what-works-on-windows-and-what-is-refused)

## What the launcher does

| | |
|---|---|
| Data | `%APPDATA%\MailArchiver\` - the archive, `.secret_key`, `certs\`, `logs\mail-archiver.log` (rotated, 5 x 2 MB), `launcher.json` |
| Accounts | built-in (`MAIL_ARCHIVER_AUTH=builtin`), registered in the web page; PAM does not exist on Windows |
| Address | `https://127.0.0.1:8400` - loopback only. `MAIL_ARCHIVER_PORT` changes the port |
| TLS | self-signed certificate for `localhost` / `127.0.0.1`, made on first start and kept in `certs\` (put your own `mail-archiver.crt` + `.key` there to replace it). It is made in-process when the EXE was built with `python-cryptography`, otherwise with an `openssl.exe` found on the machine. With neither, or with `MAIL_ARCHIVER_TLS=off`, the address is `http://127.0.0.1:8400` |
| Tray | icon with Open Mail-Archiver / Open archive folder / Open log folder / Quit (pystray). Without pystray the EXE runs in the background with no window |
| Schedules | the hourly passes (scheduled sync, scheduled import, initial / rebase sync) run inside the program at the top of every hour, only while it is running |
| Administrators | the Mail-Archiver users named in `MAIL_ARCHIVER_ADMIN_USERS` (comma-separated); by default nobody |
| Second launch | opens the browser on the running instance and exits |
| Stop | tray Quit, or `MailArchiver-<version>.exe --quit` |
| Other switches | `--no-browser` (or `MAIL_ARCHIVER_NO_BROWSER=1`): do not open the browser. `MAIL_ARCHIVER_DATA`: keep the archive somewhere else (the key, certificate and logs stay in `%APPDATA%\MailArchiver\`) |

The application starts helper processes as `python <module>.py ...`. In the
EXE `sys.executable` is the EXE, so `main.py` also acts as that interpreter:
started with a bundled module's file name (or `-c <code>`) as its first
argument it runs that and exits.

## Toolchain

The EXE is built on a Windows build machine with MSYS2. The toolchain is MSYS2 UCRT64 in `C:\msys64` (another
location: `-Msys2Root`), packages from pacman only - never pip:

    pacman -S --needed mingw-w64-ucrt-x86_64-python mingw-w64-ucrt-x86_64-python-flask \
        mingw-w64-ucrt-x86_64-pyinstaller mingw-w64-ucrt-x86_64-python-pillow \
        mingw-w64-ucrt-x86_64-python-six
    # optional, recommended: the EXE then makes its own TLS certificate
    pacman -S --needed mingw-w64-ucrt-x86_64-python-cryptography

pystray is not in the MSYS2 repositories; build it as a pacman package from
`pystray-PKGBUILD/` (in an "MSYS2 UCRT64" shell, which needs `base-devel`):

    cd windows-app/pystray-PKGBUILD
    makepkg-mingw --syncdeps --cleanbuild --noconfirm
    pacman -U --noconfirm mingw-w64-ucrt-x86_64-python-pystray-0.19.5-1-any.pkg.tar.zst

## Build

From the root of a checkout or an extracted `git archive`, in PowerShell:

    powershell -NoProfile -ExecutionPolicy Bypass -File windows-app\build.ps1 -Commit <git sha>

Result: `windows-app\dist\MailArchiver-<version>.exe` and a `.sha256` next to
it; the script prints the size and SHA-256. Add `-SmokeTest` to start the
EXE once on port 18400 with a throw-away `%APPDATA%` and stop it again.

`build.ps1`

- stops unless `python` is `C:\msys64\ucrt64\bin\python.exe` and every
  required package shows in `pacman -Q` (it prints the lines);
- runs with `MSYSTEM` unset - MSYS2's Python uses `/` as the path separator
  when it is set. The EXE removes it too, and restarts itself once if it was
  started from an MSYS2 shell;
- reads the version from `VERSION` and writes it into the EXE's version
  resource (CompanyName DEC-LLC, ProductName DEC-LLC Mail-Archiver);
- writes `BUILD_INFO` (`commit=`, `built=`, `builder=`; shown on the About
  page) the way `packaging/build.sh` does: the commit is `-Commit`, else
  the `BUILD_COMMIT` environment variable, else `git rev-parse HEAD`. A
  `git archive` has no `.git`, so pass `-Commit`;
- bundles every top-level `*.py` (except `gunicorn.conf.py`), `templates\`,
  `static\`, `VERSION` and `BUILD_INFO`, after checking that each module
  imports on Windows;
- uses `windows-app\MailArchiver.ico` (or `static\favicon.ico`) as the EXE
  and tray icon when one exists.

`windows-app\` must hold no Python file other than `main.py`: PyInstaller
would prefer it over the top-level module of the same name.

### Signing

The build can sign the EXE with a code-signing certificate that is in the
Windows certificate store of the build machine:

    powershell -NoProfile -ExecutionPolicy Bypass -File windows-app\build.ps1 -Commit <git sha> `
        -SignThumbprint <40-digit thumbprint> -TimestampUrl <RFC 3161 server>

The EXE is signed (Authenticode, SHA-256) after the smoke test and before
its SHA-256 is taken, and the build fails unless the signature verifies as
`Valid`. The last line of the output is `Signed : SIGNED by <subject>`, or
`Signed : UNSIGNED` when no thumbprint was given.

`-SignTest` proves the step with a certificate Windows does not trust (a
self-signed test certificate). The signature must be present and match the
file, the chain need not be trusted, and the output is named
`MailArchiver-<version>-TESTSIGNED.exe`. That file is never published.

Releases are signed only with DEC-LLC's own code-signing certificate. As of
1.3.3 there is none yet, so the published EXE is unsigned and Windows
SmartScreen warns on first start.

## Source and toolchain record

Each Windows release on GitHub is accompanied by two files:

| File | Content |
|---|---|
| `MailArchiver-<version>-source.zip` | the exact source tree the EXE was built from (the tree of the commit shown as "Build commit" on the About page) |
| `MailArchiver-<version>-toolchain.txt` | the package versions of the build toolchain: the MSYS2 packages (`pacman -Q`) the build ran with, including Python, Flask, PyInstaller, Pillow, pystray and, when present, python-cryptography |

With the source zip, the toolchain list and the instructions above, the EXE can be rebuilt. A rebuilt EXE is not
expected to be byte-identical. `build.ps1` writes the EXE, its `.sha256` and the toolchain file;
`packaging/make-source.sh` makes the source zip from the same commit. The sha256 of every release file is in
`SHA256SUMS`.

## What works on Windows, and what is refused

This list is read from the platform checks in the modules
(`platform_compat.py` and its callers).

Works: built-in accounts, adding IMAP accounts, sync with the built-in IMAP
client (`imap_sync.py`) over SSL/TLS or STARTTLS, the hourly schedule while
the program runs, full-text search, reading and exporting mail, the About
page, "Check the server now" (Reconcile), and mail import of mbox, Maildir /
Maildir++, `.eml` (single or a directory), Apple `.emlx` and zip / tar
archives of them, with the import ledger and the retention rules.

Needs a program on `PATH`, and says so on the page when it is absent:
PST/OST import (`readpst`), `.msg` import (`msgconvert`), `.zst` archives
(`zstd`), initial / rebase sync (`mbsync`).

Refused or not present: an account set to "Plaintext" (the built-in IMAP
client never logs in without TLS); PAM logins and per-user OS accounts (one
Windows user, built-in accounts only); the admin panels for service status,
restart and host health, which read systemd, `/proc` and `/sys`; cron (the
schedules run only while the EXE is running); file ownership changes;
access from other computers (the program listens on `127.0.0.1` only).

Message files are named with `!2,` instead of `:2,`, because NTFS does not
allow a colon in a file name. A private CA for the mail server is trusted by
naming its PEM file in `MAIL_ARCHIVER_CA_FILE` before the program starts.
