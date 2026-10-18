# Build QueenBee.exe (Windows): a folder you can zip and hand to someone who has no Python.
#
#   powershell -ExecutionPolicy Bypass -File scripts\build-exe.ps1
#
# Result: dist\QueenBee\QueenBee.exe  (zip the whole dist\QueenBee folder).
# The person who receives it still needs Claude Code installed and signed in (and Git
# for the branch mode); they do NOT need Python.

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

if (-not (Test-Path ".venv\Scripts\python.exe")) {
    & powershell -ExecutionPolicy Bypass -File "$PSScriptRoot\install.ps1" -NoShortcuts
}
$py = ".venv\Scripts\python.exe"
& $py -m pip install --quiet pyinstaller

# The demo projects are Python files that must reach the user as files (PyInstaller would otherwise skip them).
# pywinpty's terminal needs these two programs beside its DLLs; PyInstaller does not pick up .exe files itself.
$winptyDir = & $py -c "import winpty, os; print(os.path.dirname(winpty.__file__))"

# Windows reads the exe's Details tab (product, company, version) when it judges a download; an exe with none of them
# looks like a dropper. No UPX either: packed exes are what antivirus heuristics flag most.
$ver = & $py -c "import queenbee; print(queenbee.__version__)"
$parts = ($ver.Split('.') + @('0', '0', '0', '0'))[0..3] -join ', '
New-Item -ItemType Directory -Force build | Out-Null
@"
VSVersionInfo(
  ffi=FixedFileInfo(filevers=($parts), prodvers=($parts), mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('040904B0', [
      StringStruct('CompanyName', 'QueenBee'),
      StringStruct('FileDescription', 'QueenBee'),
      StringStruct('FileVersion', '$ver'),
      StringStruct('InternalName', 'QueenBee'),
      StringStruct('LegalCopyright', 'QueenBee contributors'),
      StringStruct('OriginalFilename', 'QueenBee.exe'),
      StringStruct('ProductName', 'QueenBee'),
      StringStruct('ProductVersion', '$ver')])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
"@ | Set-Content -Path build\version_info.txt -Encoding UTF8

& $py -m PyInstaller --noconfirm --clean --windowed --noupx --name QueenBee `
    --icon queenbee\assets\queenbee.ico --version-file build\version_info.txt `
    --collect-submodules queenbee --collect-data queenbee `
    --collect-all webview --hidden-import pystray._win32 --hidden-import winpty `
    --add-data "queenbee\mother\demo_free;queenbee\mother\demo_free" `
    --add-binary "$winptyDir\OpenConsole.exe;winpty" --add-binary "$winptyDir\winpty-agent.exe;winpty" `
    queenbee\launcher.py
if ($LASTEXITCODE -ne 0) {
    Write-Host "The build failed. If QueenBee is running from dist\, close it first (its files are locked)." -ForegroundColor Red
    exit 1
}

Write-Host ""
Write-Host "Built dist\QueenBee\QueenBee.exe" -ForegroundColor Green
