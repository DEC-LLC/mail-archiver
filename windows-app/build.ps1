<#
.SYNOPSIS
    Build MailArchiver-<version>.exe (PyInstaller onefile, windowed).

.DESCRIPTION
    Works from any checkout or extracted `git archive` of mail-archiver: all
    paths are relative to this script. Uses the MSYS2 UCRT64 toolchain only;
    every Python package comes from pacman (mingw-w64-ucrt-x86_64-*). The
    script stops if python resolves anywhere else or a package is missing.

    Windows PowerShell 5.1 compatible. From the repository root:

        powershell -NoProfile -ExecutionPolicy Bypass -File windows-app\build.ps1 -Commit <git sha>

.PARAMETER Commit
    Git commit the sources were exported from; stamped into BUILD_INFO and
    shown on the About page. Same order as packaging/build.sh on Linux:
    this parameter, else the BUILD_COMMIT environment variable, else
    `git rev-parse HEAD` when the tree is a git checkout and git is on
    PATH. A `git archive` has no .git, so pass -Commit there. The build
    stops when none of the three gives a commit; `-Commit unknown` says
    you mean it.

.PARAMETER Msys2Root
    MSYS2 installation directory. Default C:\msys64.

.PARAMETER OutDir
    Where the EXE goes. Default: windows-app\dist.

.PARAMETER SmokeTest
    After the build, start the EXE with a throw-away %APPDATA% on port
    18400 without opening a browser, wait until it listens, then stop it
    with --quit.

.OUTPUTS
    MailArchiver-<version>.exe, MailArchiver-<version>.exe.sha256 and
    MailArchiver-<version>-toolchain.txt (the MSYS2 package versions the EXE
    was built with) in -OutDir. The matching source archive,
    MailArchiver-<version>-source.zip, is made by packaging/make-source.sh.

.PARAMETER SignThumbprint
    Thumbprint of a code-signing certificate in Cert:\CurrentUser\My or
    Cert:\LocalMachine\My. When given, the EXE is signed (Authenticode,
    SHA-256) after the smoke test and before its sha256 is taken, and the
    build fails unless the signature then verifies as Valid. Without it the
    build is unchanged and reports UNSIGNED. Sign releases only with the
    company's sanctioned signer.

.PARAMETER TimestampUrl
    RFC 3161 timestamp server for the signature (so it stays valid after
    the certificate expires). Leave empty for no timestamp.

.PARAMETER SignTest
    Proves the signing step with a certificate Windows does not trust (a
    self-signed test certificate): the signature must be present, made by
    -SignThumbprint and match the file, but its chain need not be trusted.
    The output is named MailArchiver-<version>-TESTSIGNED.exe and must
    never be published.
#>
[CmdletBinding()]
param(
    [string]$Commit = '',
    [string]$Msys2Root = 'C:\msys64',
    [string]$OutDir = '',
    [switch]$SmokeTest,
    [string]$SignThumbprint = '',
    [string]$TimestampUrl = '',
    [switch]$SignTest
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

$PkgPrefix   = 'mingw-w64-ucrt-x86_64-'
$CompanyName = 'DEC-LLC'
$ProductName = 'DEC-LLC Mail-Archiver'

function Fail([string]$Message) {
    Write-Host ''
    Write-Host "BUILD FAILED: $Message" -ForegroundColor Red
    exit 1
}

function Step([string]$Message) {
    Write-Host ''
    Write-Host "=== $Message ==="
}

# Run a program without letting its stderr abort the script: under
# $ErrorActionPreference = 'Stop' Windows PowerShell 5.1 can turn stderr
# lines of a native program into terminating errors. The exit code is in
# $LASTEXITCODE afterwards; stdout is returned / displayed as usual.
function Invoke-Tool {
    param([string]$Exe, [string[]]$Arguments)
    $ErrorActionPreference = 'Continue'
    & $Exe @Arguments
}

function Write-TextFile([string]$Path, [string]$Text) {
    # UTF-8 without BOM, LF line ends (Out-File in 5.1 writes UTF-16 or a BOM).
    $encoding = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($Path, $Text.Replace("`r`n", "`n"), $encoding)
}

# ---------------------------------------------------------------
# Paths
# ---------------------------------------------------------------
$AppDir  = $PSScriptRoot
$Root    = Split-Path -Parent $AppDir
$WorkDir = Join-Path $AppDir 'build'
if ($OutDir -eq '') { $OutDir = Join-Path $AppDir 'dist' }
$Main    = Join-Path $AppDir 'main.py'

foreach ($needed in @('VERSION', 'app.py', 'layout.py', 'templates', 'static')) {
    if (-not (Test-Path (Join-Path $Root $needed))) {
        Fail "$needed not found in $Root - build.ps1 must sit in windows-app\ of a mail-archiver tree."
    }
}
if (-not (Test-Path $Main)) { Fail "main.py not found next to build.ps1." }

# PyInstaller puts the entry script's directory first on its search path, so
# any other module in windows-app\ would shadow the shared top-level one
# (that is how the stale imap_sync.py fork got into the 1.0.2 EXE).
$strays = @(Get-ChildItem -Path $AppDir -Filter '*.py' -File | Where-Object { $_.Name -ne 'main.py' })
if ($strays.Count -gt 0) {
    $names = ($strays | ForEach-Object { $_.Name }) -join ', '
    Fail "windows-app\ must hold no Python module except main.py (found: $names). The EXE bundles the top-level modules."
}

# ---------------------------------------------------------------
# Version and commit
# ---------------------------------------------------------------
$Version = (Get-Content -Path (Join-Path $Root 'VERSION') -Raw).Trim()
if ($Version -notmatch '^(\d+)\.(\d+)\.(\d+)') {
    Fail "VERSION holds '$Version'; expected MAJOR.MINOR.PATCH."
}
$VerTuple = "$($Matches[1]), $($Matches[2]), $($Matches[3]), 0"

if ($Commit -eq '' -and $env:BUILD_COMMIT) { $Commit = $env:BUILD_COMMIT }
if ($Commit -eq '' -and (Test-Path (Join-Path $Root '.git'))) {
    $git = Get-Command git -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($git) {
        $found = Invoke-Tool $git.Source @('-C', $Root, 'rev-parse', 'HEAD')
        if ($LASTEXITCODE -eq 0 -and $found) { $Commit = ("$found").Trim() }
    }
}
if ($Commit -eq '') {
    Fail "no commit to stamp. Pass -Commit <git sha> (a git archive has no .git), or set BUILD_COMMIT; '-Commit unknown' if that is really intended."
}
if ($Commit -ne 'unknown' -and $Commit -notmatch '^[0-9a-fA-F]{7,40}$') {
    Fail "-Commit '$Commit' is not a git commit id (7 to 40 hex digits) or 'unknown'."
}

Write-Host "=== $ProductName - Windows build ==="
Write-Host "Source : $Root"
Write-Host "Version: $Version"
Write-Host "Commit : $Commit"

# ---------------------------------------------------------------
# Toolchain: MSYS2 UCRT64, pacman packages only
# ---------------------------------------------------------------
Step 'Toolchain (MSYS2 UCRT64)'

$UcrtBin = Join-Path $Msys2Root 'ucrt64\bin'
$Python  = Join-Path $UcrtBin 'python.exe'
$Pacman  = Join-Path $Msys2Root 'usr\bin\pacman.exe'
if (-not (Test-Path $Python)) { Fail "$Python not found. Install MSYS2 and ${PkgPrefix}python (pacman -S)." }
if (-not (Test-Path $Pacman)) { Fail "$Pacman not found. Is MSYS2 installed in $Msys2Root ? (-Msys2Root overrides)" }

# MSYS2's Python switches os.sep to '/' when MSYSTEM is set, which changes
# every path PyInstaller records. Build with it unset, and with no Python
# environment overrides.
foreach ($name in @('MSYSTEM', 'PYTHONHOME', 'PYTHONPATH', 'PYTHONSTARTUP', 'VIRTUAL_ENV')) {
    if (Test-Path "Env:$name") { Remove-Item "Env:$name" }
}
$env:PYTHONDONTWRITEBYTECODE = '1'
# Only ucrt64\bin goes on PATH (first): PyInstaller resolves DLLs through
# PATH and must not find the MSYS (usr\bin) ones. pacman runs by full path.
$env:PATH = "$UcrtBin;$env:PATH"

$resolved = Get-Command python -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $resolved) { Fail "python is not on PATH even after adding $UcrtBin." }
$resolvedPath = [System.IO.Path]::GetFullPath($resolved.Source)
if ($resolvedPath -ine [System.IO.Path]::GetFullPath($Python)) {
    Fail "python resolves to $resolvedPath, not $Python. Only the MSYS2 UCRT64 Python may build this."
}
Write-Host "python : $resolvedPath"

# `pacman -Q` lists every installed package as "<name> <version>".
$installed = @{}
$listing = @(Invoke-Tool $Pacman @('-Q'))
if ($LASTEXITCODE -ne 0) { Fail "pacman -Q exited with code $LASTEXITCODE." }
foreach ($entry in $listing) {
    $parts = ("$entry").Trim() -split '\s+'
    if ($parts.Count -ge 2) { $installed[$parts[0]] = $parts[1] }
}

# Each entry lists alternative names for one requirement; the first one
# installed counts. (Their dependencies - werkzeug, jinja, six ... - come
# with them.)
$required = @(
    @("${PkgPrefix}python"),
    @("${PkgPrefix}python-flask"),
    @("${PkgPrefix}pyinstaller", "${PkgPrefix}python-pyinstaller"),
    @("${PkgPrefix}python-pillow"),
    @("${PkgPrefix}python-pystray")
)
$missing = @()
foreach ($alternatives in $required) {
    $line = $null
    foreach ($pkg in $alternatives) {
        if ($installed.ContainsKey($pkg)) { $line = "$pkg $($installed[$pkg])"; break }
    }
    if ($line) { Write-Host "  $line" } else { $missing += $alternatives[0] }
}
if ($missing.Count -gt 0) {
    Write-Host ''
    Write-Host 'Missing pacman packages:' -ForegroundColor Red
    foreach ($pkg in $missing) { Write-Host "  $pkg" -ForegroundColor Red }
    Write-Host 'Install them from an MSYS2 shell with: pacman -S --needed <name>'
    Write-Host "(${PkgPrefix}python-pystray is not in the MSYS2 repositories: build it with"
    Write-Host ' makepkg-mingw from windows-app\pystray-PKGBUILD and install it with pacman -U.)'
    Fail "$($missing.Count) required package(s) not installed. Packages come from pacman only."
}

# Optional: with python-cryptography in the toolchain the EXE makes its own
# self-signed certificate; without it, it needs an openssl.exe on the user's
# machine and otherwise serves plain HTTP on 127.0.0.1.
$cryptoPkg = "${PkgPrefix}python-cryptography"
if ($installed.ContainsKey($cryptoPkg)) {
    Write-Host "  $cryptoPkg $($installed[$cryptoPkg])   (optional: in-process TLS certificate)"
    $HaveCryptography = $true
} else {
    Write-Host "  $cryptoPkg not installed (optional)." -ForegroundColor Yellow
    Write-Host '  The EXE will serve HTTPS only where it finds an openssl.exe, else HTTP on 127.0.0.1.' -ForegroundColor Yellow
    $HaveCryptography = $false
}

if (Test-Path $WorkDir) { Remove-Item -Recurse -Force $WorkDir }
New-Item -ItemType Directory -Force -Path $WorkDir | Out-Null
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
$OutDir = (Resolve-Path $OutDir).Path

$toolCheck = Join-Path $WorkDir 'toolcheck.py'
Write-TextFile $toolCheck @'
import importlib.metadata as md
import os
import sys
import sysconfig

print('  python      %s  %s' % (sys.version.split()[0], sys.executable))
print('  platform    %s' % sysconfig.get_platform())
print('  os.sep      %r' % os.sep)
for dist in ('flask', 'werkzeug', 'pyinstaller', 'pillow', 'pystray', 'six'):
    try:
        version = md.version(dist)
    except md.PackageNotFoundError:
        version = 'NOT FOUND'
    print('  %-11s %s' % (dist, version))

import flask, PIL, PyInstaller, pystray, six  # noqa: E401,F401

problems = []
if os.sep != '\\':
    problems.append('os.sep is %r: MSYSTEM is still set' % os.sep)
if not sysconfig.get_platform().startswith('mingw'):
    problems.append('not a MinGW Python: %s' % sysconfig.get_platform())
prefix = os.path.normcase(os.path.abspath(sys.argv[1]))
if not os.path.normcase(os.path.abspath(sys.prefix)).startswith(prefix):
    problems.append('sys.prefix %s is outside %s' % (sys.prefix, sys.argv[1]))
for problem in problems:
    print('  PROBLEM: ' + problem)
sys.exit(1 if problems else 0)
'@
Invoke-Tool $Python @('-I', $toolCheck, (Join-Path $Msys2Root 'ucrt64'))
if ($LASTEXITCODE -ne 0) { Fail 'the Python toolchain check failed (see above).' }

# ---------------------------------------------------------------
# Modules to bundle: every top-level *.py except the gunicorn config
# ---------------------------------------------------------------
Step 'Application modules'
$modules = @(Get-ChildItem -Path $Root -Filter '*.py' -File |
    Where-Object { $_.Name -ne 'gunicorn.conf.py' } |
    ForEach-Object { $_.BaseName } | Sort-Object)
if ($modules -notcontains 'app') { Fail 'app.py is not among the top-level modules.' }
Write-Host ("  " + ($modules -join ', '))

# The shared code must import on Windows before it is worth freezing.
$importCheck = Join-Path $WorkDir 'importcheck.py'
Write-TextFile $importCheck @'
import importlib
import os
import sys

root, data = sys.argv[1], sys.argv[2]
sys.path.insert(0, root)
os.environ['MAIL_ARCHIVER_AUTH'] = 'builtin'
os.environ['MAIL_ARCHIVER_DATA'] = data
failed = []
for name in sys.argv[3:]:
    try:
        importlib.import_module(name)
    except Exception as exc:
        failed.append(name)
        print('  %-20s FAILED  %s: %s' % (name, type(exc).__name__, exc))
import app
print('  %d modules import, %d routes' % (len(sys.argv[3:]) - len(failed),
                                        len(list(app.app.url_map.iter_rules()))))
sys.exit(1 if failed else 0)
'@
$checkData = Join-Path $WorkDir 'importcheck-data'
New-Item -ItemType Directory -Force -Path $checkData | Out-Null
Invoke-Tool $Python (@('-B', $importCheck, $Root, $checkData) + $modules)
if ($LASTEXITCODE -ne 0) { Fail 'a top-level module does not import on Windows (see above).' }
Remove-Item -Recurse -Force $checkData -ErrorAction SilentlyContinue

# ---------------------------------------------------------------
# BUILD_INFO (read by layout.py for the About page) and version resource
# ---------------------------------------------------------------
Step 'BUILD_INFO and version resource'
$built = [DateTime]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ssZ')
if ($env:BUILD_BUILDER) {
    $builder = $env:BUILD_BUILDER
} else {
    $builder = "windows:$([System.Environment]::OSVersion.Version.ToString()) msys2-ucrt64"
}
$buildInfo = Join-Path $WorkDir 'BUILD_INFO'
Write-TextFile $buildInfo "commit=$Commit`nbuilt=$built`nbuilder=$builder`n"
Get-Content $buildInfo | ForEach-Object { Write-Host "  $_" }

$exeName = "MailArchiver-$Version"
$versionFile = Join-Path $WorkDir 'version_info.txt'
Write-TextFile $versionFile @"
# Generated by windows-app/build.ps1 from VERSION. Do not edit.
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers=($VerTuple),
    prodvers=($VerTuple),
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable('040904B0', [
        StringStruct('CompanyName', '$CompanyName'),
        StringStruct('FileDescription', '$ProductName'),
        StringStruct('FileVersion', '$Version'),
        StringStruct('InternalName', 'MailArchiver'),
        StringStruct('LegalCopyright', 'Copyright $CompanyName. Apache-2.0.'),
        StringStruct('OriginalFilename', '$exeName.exe'),
        StringStruct('ProductName', '$ProductName'),
        StringStruct('ProductVersion', '$Version')
      ])
    ]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
"@
Write-Host "  FileVersion / ProductVersion $Version ($VerTuple)"

# ---------------------------------------------------------------
# PyInstaller
# ---------------------------------------------------------------
Step 'PyInstaller'
$target = Join-Path $OutDir "$exeName.exe"
if (Test-Path $target) { Remove-Item -Force $target }

$pyiArgs = @(
    '-m', 'PyInstaller',
    '--noconfirm', '--clean',
    '--onefile', '--windowed',
    '--name', $exeName,
    '--distpath', $OutDir,
    '--workpath', (Join-Path $WorkDir 'pyinstaller'),
    '--specpath', $WorkDir,
    '--paths', $Root,
    '--add-data', "$(Join-Path $Root 'templates');templates",
    '--add-data', "$(Join-Path $Root 'static');static",
    '--add-data', "$(Join-Path $Root 'VERSION');.",
    '--add-data', "$buildInfo;.",
    '--version-file', $versionFile,
    '--hidden-import', 'pystray._win32'
)
foreach ($module in $modules) { $pyiArgs += @('--hidden-import', $module) }
if ($HaveCryptography) { $pyiArgs += @('--hidden-import', 'cryptography') }

$icon = $null
foreach ($candidate in @((Join-Path $AppDir 'MailArchiver.ico'), (Join-Path $Root 'static\favicon.ico'))) {
    if (Test-Path $candidate) { $icon = $candidate; break }
}
if ($icon) {
    Write-Host "  icon: $icon"
    $pyiArgs += @('--icon', $icon, '--add-data', "$icon;.")
    if ((Split-Path -Leaf $icon) -ne 'MailArchiver.ico') {
        # main.py looks for MailArchiver.ico for the tray; keep one name.
        $trayIcon = Join-Path $WorkDir 'MailArchiver.ico'
        Copy-Item $icon $trayIcon
        $pyiArgs += @('--add-data', "$trayIcon;.")
    }
} else {
    Write-Host '  no MailArchiver.ico in windows-app\ and no static\favicon.ico: default icon.'
}
$pyiArgs += $Main

Invoke-Tool $Python $pyiArgs
if ($LASTEXITCODE -ne 0) { Fail "PyInstaller exited with code $LASTEXITCODE." }
if (-not (Test-Path $target)) { Fail "PyInstaller reported success but $target does not exist." }

# ---------------------------------------------------------------
# Optional smoke test
# ---------------------------------------------------------------
if ($SmokeTest) {
    Step 'Smoke test'
    $smokeAppData = Join-Path $WorkDir 'smoke-appdata'
    New-Item -ItemType Directory -Force -Path $smokeAppData | Out-Null
    $saved = @{ APPDATA = $env:APPDATA; PORT = $env:MAIL_ARCHIVER_PORT; NOBROWSER = $env:MAIL_ARCHIVER_NO_BROWSER }
    $env:APPDATA = $smokeAppData
    $env:MAIL_ARCHIVER_PORT = '18400'
    $env:MAIL_ARCHIVER_NO_BROWSER = '1'
    $smokeOk = $false
    try {
        $proc = Start-Process -FilePath $target -PassThru
        $stateFile = Join-Path $smokeAppData 'MailArchiver\launcher.json'
        for ($i = 0; $i -lt 120 -and -not (Test-Path $stateFile) -and -not $proc.HasExited; $i++) {
            Start-Sleep -Milliseconds 500
        }
        if (Test-Path $stateFile) {
            Write-Host "  launcher.json: $((Get-Content $stateFile -Raw).Trim())"
            $client = New-Object System.Net.Sockets.TcpClient
            try { $client.Connect('127.0.0.1', 18400); $smokeOk = $client.Connected } finally { $client.Close() }
            # --quit goes to whichever instance holds the session's quit
            # event, so it is sent only when this build's instance came up.
            Invoke-Tool $target @('--quit')
            if (-not $proc.WaitForExit(30000)) {
                Write-Host '  the EXE did not stop after --quit; killing it.' -ForegroundColor Yellow
                Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
                $smokeOk = $false
            }
        } else {
            Write-Host '  no launcher.json: the EXE did not start, or a Mail-Archiver is already' -ForegroundColor Yellow
            Write-Host '  running in this Windows session (quit it from its tray icon first).' -ForegroundColor Yellow
            if (-not $proc.HasExited) { Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue }
        }
        $logFile = Join-Path $smokeAppData 'MailArchiver\logs\mail-archiver.log'
        if (Test-Path $logFile) { Get-Content $logFile -Tail 12 | ForEach-Object { Write-Host "  | $_" } }
    } finally {
        $env:APPDATA = $saved.APPDATA
        $env:MAIL_ARCHIVER_PORT = $saved.PORT
        $env:MAIL_ARCHIVER_NO_BROWSER = $saved.NOBROWSER
    }
    if (-not $smokeOk) { Fail 'smoke test: the EXE did not come up on 127.0.0.1:18400 and stop cleanly.' }
    Write-Host '  smoke test passed: started, listened on 127.0.0.1:18400, stopped with --quit.'
}

# ---------------------------------------------------------------
# Optional Authenticode signature (before the sha256 is taken)
# ---------------------------------------------------------------
$SignState = 'UNSIGNED'
if ($SignTest -and $SignThumbprint -eq '') { Fail '-SignTest needs -SignThumbprint.' }
if ($SignThumbprint -ne '') {
    Step 'Authenticode signature'
    $thumb = ($SignThumbprint -replace '[^0-9A-Fa-f]', '').ToUpper()
    if ($thumb.Length -ne 40) { Fail "-SignThumbprint '$SignThumbprint' is not a 40-digit certificate thumbprint." }
    $cert = $null
    foreach ($store in @('Cert:\CurrentUser\My', 'Cert:\LocalMachine\My')) {
        $found = Get-ChildItem -Path $store -ErrorAction SilentlyContinue |
            Where-Object { $_.Thumbprint -eq $thumb } | Select-Object -First 1
        if ($found) { $cert = $found; break }
    }
    if (-not $cert) { Fail "no certificate with thumbprint $thumb in CurrentUser\My or LocalMachine\My." }
    if (-not $cert.HasPrivateKey) { Fail "certificate $thumb has no private key on this machine." }
    $ekus = @($cert.EnhancedKeyUsageList | ForEach-Object { $_.ObjectId })
    if ($ekus -notcontains '1.3.6.1.5.5.7.3.3') { Fail "certificate $thumb is not a code-signing certificate." }
    if ($cert.NotAfter -lt (Get-Date)) { Fail "certificate $thumb expired on $($cert.NotAfter)." }
    Write-Host "  signer : $($cert.Subject)"
    Write-Host "  issuer : $($cert.Issuer)"
    Write-Host "  expires: $($cert.NotAfter.ToString('yyyy-MM-dd'))"

    if ($SignTest) {
        # A test-signed file gets its own name so it cannot be mistaken
        # for, or published as, the release.
        $testTarget = Join-Path $OutDir "$exeName-TESTSIGNED.exe"
        if (Test-Path $testTarget) { Remove-Item -Force $testTarget }
        if (Test-Path "$target.sha256") { Remove-Item -Force "$target.sha256" }
        Move-Item -Path $target -Destination $testTarget
        $target = $testTarget
    }

    $signArgs = @{ FilePath = $target; Certificate = $cert; HashAlgorithm = 'SHA256' }
    if ($TimestampUrl -ne '') { $signArgs['TimestampServer'] = $TimestampUrl }
    $null = Set-AuthenticodeSignature @signArgs
    $sig = Get-AuthenticodeSignature -FilePath $target
    Write-Host "  status : $($sig.Status) - $($sig.StatusMessage)"
    if (-not $sig.SignerCertificate) { Fail 'the EXE carries no signature after signing.' }
    if ($sig.SignerCertificate.Thumbprint -ne $thumb) { Fail 'the EXE is signed by a different certificate than -SignThumbprint.' }
    if ($sig.Status -eq 'HashMismatch' -or $sig.Status -eq 'NotSigned') { Fail "signature check: $($sig.Status)." }
    if ($TimestampUrl -ne '' -and -not $sig.TimeStamperCertificate) { Fail "no timestamp from $TimestampUrl." }
    if ($SignTest) {
        Write-Host '  TEST SIGNATURE: chain not required to be trusted. Do not publish this file.' -ForegroundColor Yellow
        $SignState = "TEST-SIGNED ($($sig.Status)) by $($cert.Subject)"
    } else {
        if ($sig.Status -ne 'Valid') { Fail "the signature does not verify as Valid on this machine ($($sig.Status)). Use -SignTest only to prove the step with a test certificate." }
        $SignState = "SIGNED by $($cert.Subject)"
    }
}

# ---------------------------------------------------------------
# Result
# ---------------------------------------------------------------
Step 'Result'
$item = Get-Item $target
$hash = (Get-FileHash -Path $target -Algorithm SHA256).Hash.ToLower()
Write-TextFile "$target.sha256" "$hash  $($item.Name)`n"
Write-Host "EXE    : $target"
Write-Host ("Size   : {0} bytes ({1:N1} MB)" -f $item.Length, ($item.Length / 1MB))
Write-Host "SHA256 : $hash"
Write-Host "Commit : $Commit"
Write-Host "Signed : $SignState"

# Toolchain record: what this EXE was built with, kept beside it so the same
# build can be set up again later (1.3.3).
$toolchainFile = Join-Path $OutDir "MailArchiver-$Version-toolchain.txt"
$ucrt = @($listing | ForEach-Object { ("$_").Trim() } | Where-Object { $_ -like "$PkgPrefix*" } | Sort-Object)
$pyVersion = (Invoke-Tool $Python @('-c', 'import sys; print(sys.version.split()[0])'))
$lines = @(
    "DEC-LLC Mail-Archiver $Version - Windows build toolchain",
    "commit    : $Commit",
    "built     : $built",
    "windows   : $([System.Environment]::OSVersion.Version.ToString())",
    "python    : $pyVersion ($Python)",
    "signed    : $SignState",
    "exe sha256: $hash",
    "",
    "MSYS2 UCRT64 packages (pacman -Q), $($ucrt.Count) installed:"
) + $ucrt
Write-TextFile $toolchainFile (($lines -join "`n") + "`n")
Write-Host "Toolchain record: $toolchainFile"
exit 0
