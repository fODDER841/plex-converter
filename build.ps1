<#
.SYNOPSIS
    Build PlexConverter.exe and install it with a Start Menu shortcut.

.DESCRIPTION
    Run from the project root:  .\build.ps1
    To build without installing: .\build.ps1 -NoInstall

.NOTES
    Requires Python 3.10+ with pip.  FFmpeg must be on PATH or configured
    in the app's FFmpeg Settings — it is NOT bundled into the exe.
#>
param(
    [switch]$NoInstall
)

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot

function Step($n, $total, $msg) {
    Write-Host "`n[$n/$total] $msg" -ForegroundColor Cyan
}

Write-Host "=== Plex Converter Build ===" -ForegroundColor White

# ── 1. Dependencies ──────────────────────────────────────────────────────────
Step 1 4 "Installing build dependencies (pyinstaller, pillow)..."
python -m pip install pyinstaller --pre pillow --quiet
if ($LASTEXITCODE -ne 0) { throw "pip install failed" }

# ── 2. Icon ───────────────────────────────────────────────────────────────────
Step 2 4 "Generating icon..."
python "$root\make_icon.py"
if ($LASTEXITCODE -ne 0) { throw "Icon generation failed" }

# ── 3. PyInstaller ────────────────────────────────────────────────────────────
Step 3 4 "Building with PyInstaller (this takes ~30-60 s)..."
Set-Location $root
python -m PyInstaller build.spec --noconfirm --clean
if ($LASTEXITCODE -ne 0) { throw "PyInstaller build failed" }

$exe = "$root\dist\PlexConverter.exe"
if (-not (Test-Path $exe)) { throw "Exe not found at expected path: $exe" }
$sizeMB = [math]::Round((Get-Item $exe).Length / 1MB, 1)
Write-Host "  Built: $exe  ($sizeMB MB)" -ForegroundColor Green

# ── 4. Install ────────────────────────────────────────────────────────────────
if ($NoInstall) {
    Write-Host "`nSkipped install (-NoInstall).  Exe at: $exe" -ForegroundColor Yellow
} else {
    Step 4 4 "Installing to Start Menu..."

    $dest = "$env:LOCALAPPDATA\Programs\PlexConverter"
    New-Item -ItemType Directory -Force -Path $dest | Out-Null
    Copy-Item $exe "$dest\PlexConverter.exe" -Force

    $lnk = "$env:APPDATA\Microsoft\Windows\Start Menu\Programs\Plex Converter.lnk"
    $sh   = New-Object -ComObject WScript.Shell
    $sc   = $sh.CreateShortcut($lnk)
    $sc.TargetPath       = "$dest\PlexConverter.exe"
    $sc.WorkingDirectory = $dest
    $sc.Description      = "Convert video files to Plex-compatible MKV"
    $sc.IconLocation     = "$dest\PlexConverter.exe"
    $sc.Save()

    Write-Host "  Installed : $dest\PlexConverter.exe" -ForegroundColor Green
    Write-Host "  Shortcut  : $lnk"  -ForegroundColor Green
    Write-Host "`nDone. Search 'Plex' in the Start menu to launch." -ForegroundColor White
}
