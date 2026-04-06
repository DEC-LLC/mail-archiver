# Build Mail Archiver Windows EXE
# Run from: C:\Users\vaultsync\Projects\mail-archiver-win\

$ErrorActionPreference = "Stop"
$base = "C:\Users\vaultsync\Projects\mail-archiver-win"
$python = "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe"
$pip = "$env:LOCALAPPDATA\Programs\Python\Python312\Scripts\pip.exe"

Write-Host "=== Mail Archiver Windows Build ==="
Write-Host "Python: $python"

# Install build dependencies (PyInstaller + Flask)
Write-Host "`n=== Installing build dependencies ==="
& $pip install pyinstaller flask pystray pillow --quiet 2>&1

# Verify
& $python -c "import flask; import PyInstaller; print('Dependencies OK')"

# Build EXE
Write-Host "`n=== Building EXE with PyInstaller ==="
Set-Location $base
& $python -m PyInstaller `
    --name "MailArchiver" `
    --onefile `
    --windowed `
    --add-data "mail_archiver\templates;templates" `
    --add-data "mail_archiver\static;static" `
    --add-data "mail_archiver\*.py;mail_archiver" `
    --hidden-import "flask" `
    --hidden-import "email" `
    --hidden-import "imaplib" `
    --hidden-import "sqlite3" `
    --hidden-import "pystray" `
    --hidden-import "PIL" `
    main.py 2>&1

Write-Host "`n=== Build complete ==="
if (Test-Path "$base\dist\MailArchiver.exe") {
    $size = (Get-Item "$base\dist\MailArchiver.exe").Length / 1MB
    Write-Host "EXE: $base\dist\MailArchiver.exe ($([math]::Round($size,1)) MB)"
} else {
    Write-Host "ERROR: EXE not found"
}
