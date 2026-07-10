# Build RO_GearSync.exe via PyInstaller.
#
# Usage (from the project root):
#
#     .\scripts\build_exe.ps1
#
# The resulting one-dir distribution lands in ``.\dist\RO_GearSync_v{ver}\``
# (public) plus ``.\dist\RO_GearSync_Internal_v{ver}\`` (guild-internal,
# config.ini pre-filled from ``initialize\config.ini``). Ship the whole
# folder; users launch ``RO_GearSync.exe`` inside it.
#
# Why this wrapper exists
# -----------------------
# PyInstaller's tkinter detection runs in an isolated subprocess that
# doesn't inherit ``sys.path``. On the developer's Python install (the
# one at ``C:\Software\Python\Python313``) Tcl/Tk lives under
# ``<base>\tcl\``, which Python finds via runtime path-fixing but
# PyInstaller's subprocess does not — so the build "succeeds" silently
# and the resulting .exe dies with ``ModuleNotFoundError: No module
# named 'tkinter'``.
#
# Exporting ``TCL_LIBRARY`` and ``TK_LIBRARY`` to the actual Tcl/Tk
# script directories fixes the detection. This wrapper does that
# automatically by walking the venv's ``base_prefix`` for Tcl folders.

$ErrorActionPreference = "Stop"
# PyInstaller writes its INFO logs to stderr; PowerShell would normally
# treat each stderr line as an error record (and abort on $ErrorActionPreference).
# Redirect at process boundary instead so the build messages stream cleanly.

$projectRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$venvPython  = Join-Path $projectRoot ".venv\Scripts\python.exe"
$venvPI      = Join-Path $projectRoot ".venv\Scripts\pyinstaller.exe"
$specFile    = Join-Path $projectRoot "build_app.spec"

# Ask Python where its base install (and thus Tcl/Tk) lives. Works for
# both venv (sys.base_prefix → system Python) and a plain install.
$basePrefix = & $venvPython -c "import sys; print(sys.base_prefix)"
$basePrefix = $basePrefix.Trim()
Write-Host "Base Python prefix: $basePrefix"

# Probe for Tcl/Tk script directories — the two common layouts are
# <base>\tcl\tcl8.6 (Windows installer default) and <base>\lib\tcl8.6
# (alternate layout used by some custom builds).
$tclCandidates = @("$basePrefix\tcl\tcl8.6", "$basePrefix\lib\tcl8.6")
$tkCandidates  = @("$basePrefix\tcl\tk8.6",  "$basePrefix\lib\tk8.6")

$tclLib = $tclCandidates | Where-Object { Test-Path (Join-Path $_ "init.tcl") } | Select-Object -First 1
$tkLib  = $tkCandidates  | Where-Object { Test-Path (Join-Path $_ "tk.tcl")  } | Select-Object -First 1

if (-not $tclLib -or -not $tkLib) {
    Write-Error "Could not locate Tcl/Tk script libraries under $basePrefix. PyInstaller's tkinter detection will fail."
}
Write-Host "TCL_LIBRARY = $tclLib"
Write-Host "TK_LIBRARY  = $tkLib"

$env:TCL_LIBRARY = $tclLib
$env:TK_LIBRARY  = $tkLib

# --clean wipes previous build/ caches; --noconfirm overwrites dist/
# without prompting. Both are wanted in CI-style use.
# cmd.exe wrapper turns stderr into ordinary stdout so PowerShell's
# error pipeline doesn't choke on PyInstaller's INFO messages.
cmd.exe /c "`"$venvPI`" --clean --noconfirm `"$specFile`" 2>&1"

if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller exited with code $LASTEXITCODE"
}

$distDir = Join-Path $projectRoot "dist\RO_GearSync"

# Stage the user-editable config.ini next to the .exe. It is NOT
# bundled inside the .exe on purpose — users are expected to edit it
# (paths to LDPlayer, OCR model selection, etc.). logs/ is created on
# first launch by the app itself.
#
# IMPORTANT: We do NOT copy the project-root config.ini. The maintainer
# routinely edits their working-tree copy with dev tweaks (custom paths,
# OCR presets they're benchmarking) that must NOT leak into the shipped
# bundle. Instead we invoke AppConfig.write_default() — the same code
# the running app uses to materialise a config.ini on first launch
# (config.py: _DEFAULT_CONFIG). One source of truth, no git dependency,
# and the shipped file is byte-identical to what a fresh first-run
# would produce.
$configDst = Join-Path $distDir "config.ini"
$srcDir = Join-Path $projectRoot "src"
$pyCmd = "import sys; sys.path.insert(0, r'$srcDir'); from ro_gearsync.utils.config import AppConfig; from pathlib import Path; AppConfig.write_default(Path(r'$configDst'))"
& $venvPython -c $pyCmd
if ($LASTEXITCODE -ne 0) {
    throw "Failed to materialise default config.ini (exit $LASTEXITCODE)"
}
Write-Host "Staged config.ini (from _DEFAULT_CONFIG) → $configDst"

# Inject the roster-sync OAuth client into the staged config.ini, sourced
# from the gitignored .google_oauth_client.json at the repo root. Google
# treats a desktop-app client secret as non-confidential, so shipping it
# inside the bundle is fine — but the public GitHub repo is not, hence
# the file never enters git and the injection happens only at build time.
$oauthJson = Join-Path $projectRoot ".google_oauth_client.json"
if (Test-Path $oauthJson) {
    $pyInject = @"
import json, re
from pathlib import Path
cfg = Path(r'$configDst')
inner = json.loads(Path(r'$oauthJson').read_text(encoding='utf-8'))
inner = inner.get('installed') or inner.get('web') or inner
text = cfg.read_text(encoding='utf-8')
text = re.sub(r'(?m)^oauth_client_id =.*$', 'oauth_client_id = ' + inner['client_id'], text)
text = re.sub(r'(?m)^oauth_client_secret =.*$', 'oauth_client_secret = ' + inner['client_secret'], text)
cfg.write_text(text, encoding='utf-8')
"@
    & $venvPython -c $pyInject
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to inject OAuth client into config.ini (exit $LASTEXITCODE)"
    }
    Write-Host "Injected [google_sheet] OAuth client into staged config.ini"
} else {
    Write-Host "NOTE: .google_oauth_client.json not found - shipped config.ini has an empty [google_sheet] OAuth client (roster sync will need manual setup)."
}

# Pre-create an empty data/ folder so end users have an obvious place
# to drop their existing guild_scores.xlsx before first launch (see
# README §1.4). The app would create it on first launch anyway, but
# shipping it visible avoids the "where do I put the Excel?" question.
$dataDir = Join-Path $distDir "data"
if (-not (Test-Path $dataDir)) {
    New-Item -ItemType Directory -Path $dataDir | Out-Null
    Write-Host "Staged empty data/ → $dataDir"
}

# Bundle README.md alongside the .exe so end users get the usage guide
# without having to hunt for it on the share. The README is shipped as-is
# (Markdown) — guild officers either read it in VS Code / Notepad or
# render it on GitHub.
$readmeSrc = Join-Path $projectRoot "README.md"
$readmeDst = Join-Path $distDir "README.md"
if (Test-Path $readmeSrc) {
    Copy-Item -Path $readmeSrc -Destination $readmeDst -Force
    Write-Host "Staged README.md → $readmeDst"
}

# ---------------------------------------------------------------------------
# Post-package staging (2026-07-10): seed workbooks, versioned folder names,
# and the guild-internal variant. Inputs live in <root>\initialize\:
#   guild_scores.xlsx / league_scores.xlsx  — seed rosters copied into data/
#                                             (tracked in git)
#   config.ini                              — internal config with REAL
#                                             secrets (gitignored, never
#                                             committed). Keep it in sync
#                                             whenever _DEFAULT_CONFIG in
#                                             utils/config.py changes!
# ---------------------------------------------------------------------------

# Read the package version so the output folders carry it.
$version = & $venvPython -c "import sys; sys.path.insert(0, r'$srcDir'); import ro_gearsync; print(ro_gearsync.__version__)"
if ($LASTEXITCODE -ne 0) { throw "Failed to read package version" }
$version = $version.Trim()
Write-Host "Package version: $version"

# Seed workbooks so a fresh install starts from the guild's current
# rosters instead of empty files.
$initDir = Join-Path $projectRoot "initialize"
foreach ($wb in @("guild_scores.xlsx", "league_scores.xlsx")) {
    $wbSrc = Join-Path $initDir $wb
    if (Test-Path $wbSrc) {
        Copy-Item -Path $wbSrc -Destination (Join-Path $dataDir $wb) -Force
        Write-Host "Seeded $wb → data\"
    } else {
        Write-Warning "initialize\$wb not found — data\ ships without it"
    }
}

# Rename the dist folder to RO_GearSync_v{version} — ready for the
# maintainer to inspect and compress as RO_GearSync_v{version}.7z.
$publicDir = Join-Path $projectRoot "dist\RO_GearSync_v$version"
if (Test-Path $publicDir) { Remove-Item -Path $publicDir -Recurse -Force }
Move-Item -Path $distDir -Destination $publicDir
Write-Host "Renamed dist folder → $publicDir"

# Guild-internal variant: identical bundle, but config.ini replaced with
# the pre-filled initialize\config.ini (real sheet_url / API key). Ready
# to compress as RO_GearSync_Internal_v{version}.rar (password-protect!).
$internalDir = Join-Path $projectRoot "dist\RO_GearSync_Internal_v$version"
$internalConfig = Join-Path $initDir "config.ini"
if (Test-Path $internalConfig) {
    if (Test-Path $internalDir) { Remove-Item -Path $internalDir -Recurse -Force }
    Copy-Item -Path $publicDir -Destination $internalDir -Recurse
    Copy-Item -Path $internalConfig -Destination (Join-Path $internalDir "config.ini") -Force
    Write-Host "Staged internal variant (pre-filled config.ini) → $internalDir"
} else {
    Write-Warning "initialize\config.ini not found — internal variant skipped"
}

Write-Host ""
Write-Host "Build OK — public bundle:    $publicDir"
if (Test-Path $internalDir) {
    Write-Host "          internal bundle:  $internalDir"
}
Write-Host "Launch:                      $publicDir\RO_GearSync.exe"
Write-Host ""
Write-Host "Compress each folder as-is: RO_GearSync_v$version.7z / RO_GearSync_Internal_v$version.rar"
