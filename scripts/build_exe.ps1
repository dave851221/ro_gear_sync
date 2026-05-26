# Build RO_GearSync.exe via PyInstaller.
#
# Usage (from the project root):
#
#     .\scripts\build_exe.ps1
#
# The resulting one-dir distribution lands in ``.\dist\RO_GearSync\``.
# Ship that whole folder; users launch ``RO_GearSync.exe`` inside it.
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
# (paths to LDPlayer, OCR model selection, etc.). data/ and logs/ are
# created on first launch by the app itself, so we don't pre-create them.
$configSrc = Join-Path $projectRoot "config.ini"
$configDst = Join-Path $distDir "config.ini"
if (Test-Path $configSrc) {
    Copy-Item -Path $configSrc -Destination $configDst -Force
    Write-Host "Staged config.ini → $configDst"
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

Write-Host ""
Write-Host "Build OK — distribution in: $distDir"
Write-Host "Launch:                      $distDir\RO_GearSync.exe"
Write-Host ""
Write-Host "Ship the entire '$distDir' folder; recipients launch RO_GearSync.exe."
