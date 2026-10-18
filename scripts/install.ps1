# QueenBee: one-step install for Windows.
#
#   powershell -ExecutionPolicy Bypass -File scripts\install.ps1
#
# Makes a private Python environment in .venv (nothing is installed system-wide),
# installs QueenBee into it and puts a "QueenBee" shortcut on your desktop and
# Start menu. Run it again any time: it only fills in what is missing.
# -NoShortcuts skips the shortcuts.

param([switch]$NoShortcuts)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

function Need($cmd, $hint) {
    if (-not (Get-Command $cmd -ErrorAction SilentlyContinue)) {
        Write-Host "Missing: $cmd. $hint" -ForegroundColor Red
        exit 1
    }
}

Need python "Install Python 3.11 or newer from python.org, then run this again."
$version = & python -c "import sys; print('%d.%d' % sys.version_info[:2])"
if ([version]$version -lt [version]"3.11") {
    Write-Host "Python $version found; QueenBee needs 3.11 or newer." -ForegroundColor Red
    exit 1
}
Need git "Install Git from git-scm.com (queens use it to keep each worker's changes apart)."
if (-not (Get-Command claude -ErrorAction SilentlyContinue)) {
    Write-Host "Note: Claude Code ('claude') was not found. QueenBee drives it; install it and sign in once" `
        "by running 'claude' in a terminal." -ForegroundColor Yellow
}

if (-not (Test-Path ".venv\Scripts\python.exe")) {
    Write-Host "Creating the private environment (.venv) ..."
    python -m venv .venv
}
Write-Host "Installing QueenBee ..."
& .venv\Scripts\python.exe -m pip install --quiet --upgrade pip
& .venv\Scripts\python.exe -m pip install --quiet -e ".[window]"

if (-not $NoShortcuts) {
    & .venv\Scripts\python.exe -m queenbee.mother --shortcuts | Out-Null
    Write-Host "Shortcut 'QueenBee' created on your desktop and in the Start menu."
}

Write-Host ""
Write-Host "Done. Open 'QueenBee' from your desktop," -ForegroundColor Green
Write-Host "or run:  .venv\Scripts\python.exe -m queenbee.mother"
