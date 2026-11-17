# Security policy

## Supported versions

Security fixes are made in the latest release. Upgrade to it before reporting a problem; the release notes in
[CHANGELOG.md](CHANGELOG.md) list the security fixes of each version.

## Reporting a vulnerability

Please report it privately: on the repository's **Security** tab choose **Report a vulnerability**
(https://github.com/DEC-LLC/mail-archiver/security/advisories/new). Do not open a public issue for it.

Include the version (About page, or `cat /opt/mail-archiver/VERSION`), the platform, the sign-in mode (system
accounts or built-in accounts), and the steps to reproduce.

## What to know when you run it

The administrator guide has a section "Security notes" ([docs/ADMIN-GUIDE.md](docs/ADMIN-GUIDE.md)). The points most
often relevant: serve it only over HTTPS; with built-in accounts, registration is open unless
`MAIL_ARCHIVER_REGISTRATION=closed`; the Windows EXE and the Linux packages are not signed yet, so verify the sha256
from `SHA256SUMS`.
