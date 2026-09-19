<#
    Error Logger - one-click installer (Windows)

    Installs, in order:
      1. Ollama            (winget, else silent download from ollama.com)
      2. The AI model      (Ollama Cloud sign-in, or a fully-local model)
      3. The server        (prebuilt exe, else a Python venv from source)
      4. Shortcuts         (Desktop + Start Menu, optional run-at-login)
      5. The Chrome extension (copies files, opens chrome://extensions)

    Safe to re-run: it upgrades an existing install in place and never
    touches the errors database.

    Usage (normally launched by Install.bat):
      powershell -ExecutionPolicy Bypass -File install.ps1
      ... -Mode cloud        skip the model prompt, use Ollama Cloud
      ... -Mode local        skip the model prompt, pull the local model
      ... -Silent            no prompts at all (implies -Mode local unless set)
      ... -SkipModel         keep the model as-is (repair an install quickly)
      ... -NoStart           install only, do not launch the server
      ... -NoBrowser         do not open the extensions page at the end
#>
[CmdletBinding()]
param(
    [ValidateSet('cloud', 'local', 'ask')]
    [string]$Mode = 'ask',
    [switch]$Silent,
    [switch]$SkipModel,
    [switch]$NoStart,
    [switch]$NoBrowser
)

$ErrorActionPreference = 'Stop'
try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch {}

# ---------------------------------------------------------------- constants
$AppName      = 'Error Logger'
$InstallRoot  = Join-Path $env:LOCALAPPDATA 'ErrorLogger'
$AppDir       = Join-Path $InstallRoot 'app'
$ExtDir       = Join-Path $InstallRoot 'extension'
$LauncherName = 'Start Error Logger.cmd'
$Launcher     = Join-Path $InstallRoot $LauncherName
$Port         = 8787
$OllamaUrl    = 'http://localhost:11434'
$CloudModel   = 'deepseek-v3.1:671b-cloud'
$LocalModel   = 'qwen2.5:7b-instruct'
$SourceDir    = Split-Path -Parent $PSScriptRoot   # repo root when run from installer/

if ($Silent -and $Mode -eq 'ask') { $Mode = 'local' }

# ---------------------------------------------------------------- helpers
function Say  ($m) { Write-Host $m }
function Step ($n, $m) { Write-Host ''; Write-Host "[$n/7] $m" -ForegroundColor Cyan }
function Ok   ($m) { Write-Host "      OK   $m" -ForegroundColor Green }
function Warn ($m) { Write-Host "      WARN $m" -ForegroundColor Yellow }
function Fail ($m) { Write-Host "      FAIL $m" -ForegroundColor Red }
function Info ($m) { Write-Host "      $m" -ForegroundColor DarkGray }

function Stop-Installer ($message) {
    Fail $message
    if (-not $Silent) { Read-Host '      Press Enter to exit' | Out-Null }
    exit 1
}

function Ask-YesNo ($question, [bool]$defaultYes = $true) {
    if ($Silent) { return $defaultYes }
    $suffix = if ($defaultYes) { '[Y/n]' } else { '[y/N]' }
    while ($true) {
        $a = (Read-Host "      $question $suffix").Trim().ToLower()
        if ($a -eq '')            { return $defaultYes }
        if ($a -eq 'y' -or $a -eq 'yes') { return $true }
        if ($a -eq 'n' -or $a -eq 'no')  { return $false }
    }
}

function Find-Ollama {
    $cmd = Get-Command ollama -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    foreach ($p in @(
        (Join-Path $env:LOCALAPPDATA 'Programs\Ollama\ollama.exe'),
        (Join-Path $env:ProgramFiles 'Ollama\ollama.exe')
    )) { if (Test-Path $p) { return $p } }
    return $null
}

function Test-Http ($url, $timeoutSec = 3) {
    try {
        Invoke-WebRequest -Uri $url -TimeoutSec $timeoutSec -UseBasicParsing | Out-Null
        return $true
    } catch { return $false }
}

function Wait-For ($url, $seconds, $label) {
    for ($i = 0; $i -lt $seconds; $i++) {
        if (Test-Http $url) { return $true }
        if ($i -eq 5) { Info "still waiting for $label ..." }
        Start-Sleep -Seconds 1
    }
    return $false
}

function New-Shortcut ($linkPath, $target, $workDir, $desc) {
    $sh = New-Object -ComObject WScript.Shell
    $sc = $sh.CreateShortcut($linkPath)
    $sc.TargetPath       = $target
    $sc.WorkingDirectory = $workDir
    $sc.Description      = $desc
    $sc.Save()
}

# ---------------------------------------------------------------- banner
Say ''
Say '  ============================================================'
Say "   $AppName  -  installer"
Say '  ============================================================'
Info "target folder : $InstallRoot"

# ================================================================= 1. Ollama
Step 1 'Checking Ollama'
$ollama = Find-Ollama

if ($ollama) {
    # `ollama --version` prints a "not running" warning first when the service
    # is down, so pick the line that actually carries the version.
    $ver = (& $ollama --version 2>&1 | Select-String -Pattern 'version' -SimpleMatch |
            Where-Object { $_ -notmatch 'could not connect' } | Select-Object -First 1)
    if ($ver) { Ok "already installed  ($($ver.ToString().Trim()))" } else { Ok 'already installed' }
} else {
    Say '      Ollama is not installed. It runs the AI that categorises your mistakes.'
    if (-not (Ask-YesNo 'Install Ollama now? (~700 MB download)' $true)) {
        Stop-Installer 'Ollama is required. Install it from https://ollama.com and re-run this installer.'
    }

    $installed = $false
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        Info 'installing via winget ...'
        try {
            winget install --id Ollama.Ollama -e --silent --accept-package-agreements --accept-source-agreements | Out-Null
            $installed = $null -ne (Find-Ollama)
        } catch { $installed = $false }
    }

    if (-not $installed) {
        Info 'downloading OllamaSetup.exe from ollama.com ...'
        $setup = Join-Path $env:TEMP 'OllamaSetup.exe'
        try {
            $pref = $ProgressPreference
            $ProgressPreference = 'SilentlyContinue'
            Invoke-WebRequest -Uri 'https://ollama.com/download/OllamaSetup.exe' -OutFile $setup -UseBasicParsing
            $ProgressPreference = $pref
            Info 'running the installer (silent, no admin needed) ...'
            Start-Process -FilePath $setup -ArgumentList '/VERYSILENT', '/NORESTART' -Wait
            Remove-Item $setup -ErrorAction SilentlyContinue
        } catch {
            Fail "download failed: $($_.Exception.Message)"
            Stop-Installer 'Install Ollama manually from https://ollama.com then re-run this installer.'
        }
    }

    # A freshly-installed Ollama is not on this shell's PATH yet.
    $env:Path = "$env:Path;" + (Join-Path $env:LOCALAPPDATA 'Programs\Ollama')
    $ollama = Find-Ollama
    if (-not $ollama) {
        Stop-Installer 'Ollama installed but ollama.exe was not found. Reboot and re-run this installer.'
    }
    Ok 'Ollama installed'
}

# ================================================================= 2. serve
Step 2 'Starting the Ollama service'
if (Test-Http "$OllamaUrl/api/tags") {
    Ok 'already running'
} else {
    Start-Process -FilePath $ollama -ArgumentList 'serve' -WindowStyle Hidden
    if (Wait-For "$OllamaUrl/api/tags" 30 'Ollama') {
        Ok "running at $OllamaUrl"
    } else {
        Warn "Ollama did not answer on $OllamaUrl within 30s."
        Warn 'Continuing anyway - capture still works, just without AI categorisation.'
    }
}

# ================================================================= 3. model
Step 3 'Choosing the AI model'

if ($Mode -eq 'ask' -and $SkipModel) { $Mode = 'cloud' }

if ($Mode -eq 'ask') {
    Say ''
    Say '      1) Ollama Cloud  - DeepSeek V3.1 (671B). Best quality, nothing to'
    Say '                        download, free preview tier. Needs a quick sign-in'
    Say '                        in your browser (free account).'
    Say "      2) Fully local   - $LocalModel. ~4.7 GB download, works"
    Say '                        offline forever, slightly rougher categorisation.'
    Say ''
    $choice = (Read-Host '      Pick 1 or 2 [1]').Trim()
    $Mode = if ($choice -eq '2') { 'local' } else { 'cloud' }
}

$Model = if ($Mode -eq 'local') { $LocalModel } else { $CloudModel }

if ($SkipModel) {
    Info "skipped (-SkipModel) - the launcher will use $Model"
} elseif ($Mode -eq 'cloud') {
    Info 'signing in to Ollama Cloud - a browser window will open ...'
    Say ''
    & $ollama signin
    Say ''
    Info 'verifying cloud access (the first call can take ~20s) ...'
    $cloudOk = $false
    try {
        $body = @{ model = $CloudModel; prompt = 'hi'; stream = $false } | ConvertTo-Json
        Invoke-RestMethod -Uri "$OllamaUrl/api/generate" -Method Post -Body $body `
            -ContentType 'application/json' -TimeoutSec 90 | Out-Null
        $cloudOk = $true
    } catch {
        Warn "cloud call failed: $($_.Exception.Message)"
    }

    if ($cloudOk) {
        Ok "Ollama Cloud ready  ($CloudModel)"
    } else {
        Warn 'Could not reach the cloud model.'
        if (Ask-YesNo "Fall back to the local model ($LocalModel, ~4.7 GB)?" $true) {
            $Mode = 'local'
        } else {
            Warn 'Keeping cloud mode. Run "ollama signin" later if capture fails with a 401.'
        }
    }
}

if ($Mode -eq 'local' -and -not $SkipModel) {
    $Model = $LocalModel
    Info "pulling $LocalModel - this is the long part, grab a chai ..."
    Say ''
    & $ollama pull $LocalModel
    Say ''
    if ($LASTEXITCODE -eq 0) {
        Ok "local model ready  ($LocalModel)"
    } else {
        Warn 'Model pull failed. You can retry later with:'
        Info "  ollama pull $LocalModel"
    }
}

# ================================================================= 4. files
Step 4 'Installing the app'
New-Item -ItemType Directory -Force -Path $AppDir, $ExtDir | Out-Null

# The release payload ships a prebuilt exe next to this script; a plain copy of
# the repo does not, so fall back to running from source in a venv.
$payloadExe = Join-Path $PSScriptRoot 'app\error-logger-server.exe'
$repoExe    = Join-Path $SourceDir  'server\dist\error-logger-server.exe'
$srcExe     = $null
if (Test-Path $payloadExe) { $srcExe = $payloadExe } elseif (Test-Path $repoExe) { $srcExe = $repoExe }

$payloadExt = Join-Path $PSScriptRoot 'extension'
$srcExt     = if (Test-Path (Join-Path $payloadExt 'manifest.json')) { $payloadExt } else { Join-Path $SourceDir 'extension' }
if (-not (Test-Path (Join-Path $srcExt 'manifest.json'))) {
    Stop-Installer "Extension files not found next to this installer (looked in $srcExt)."
}

$RunMode = 'exe'
if ($srcExe) {
    Copy-Item $srcExe (Join-Path $AppDir 'error-logger-server.exe') -Force
    Ok 'server installed (standalone, no Python needed)'
} else {
    # ---- source mode: needs Python 3.9+
    $RunMode = 'source'
    Info 'no prebuilt server found - installing from source instead'
    $py = Get-Command py -ErrorAction SilentlyContinue
    if (-not $py) { $py = Get-Command python -ErrorAction SilentlyContinue }
    if (-not $py) {
        Stop-Installer 'Python 3.9+ is required for source mode. Install it from https://python.org (tick "Add python.exe to PATH") and re-run.'
    }
    foreach ($d in @('server', 'webapp')) {
        $from = Join-Path $SourceDir $d
        if (-not (Test-Path $from)) { Stop-Installer "missing source folder: $from" }
        $to = Join-Path $AppDir $d
        New-Item -ItemType Directory -Force -Path $to | Out-Null
        Copy-Item (Join-Path $from '*') $to -Recurse -Force `
            -Exclude 'venv', 'build', 'dist', '__pycache__', 'errors.db', 'errors_images', '*.log'
    }
    Info 'creating the Python environment (a minute or two) ...'
    $venv = Join-Path $AppDir 'venv'
    & $py.Source -m venv $venv
    $venvPy = Join-Path $venv 'Scripts\python.exe'
    & $venvPy -m pip install --quiet --upgrade pip
    & $venvPy -m pip install --quiet -r (Join-Path $AppDir 'server\requirements.txt')
    if ($LASTEXITCODE -ne 0) { Stop-Installer 'pip install failed - see the output above.' }
    Ok 'server installed from source'
}

# ---- extension (always copied, so the recipient has a stable path for Chrome)
Copy-Item (Join-Path $srcExt '*') $ExtDir -Recurse -Force
Ok "extension files at  $ExtDir"

# ---- launcher, with the chosen model baked in
$runLine = if ($RunMode -eq 'exe') {
    '"%~dp0app\error-logger-server.exe"'
} else {
    '"%~dp0app\venv\Scripts\python.exe" "%~dp0app\server\app.py"'
}

$launcherText = @"
@echo off
title $AppName
set "OLLAMA_URL=$OllamaUrl"
set "OLLAMA_MODEL=$Model"
set "OLLAMA_TIMEOUT=180"

echo Starting $AppName ...
echo   model     : %OLLAMA_MODEL%
echo   dashboard : http://localhost:$Port/
echo.
echo Leave this window open while you study. Close it to stop the server.
echo.

rem Give the server a moment to bind the port, then open the dashboard.
start "" /min cmd /c "timeout /t 3 /nobreak >nul & start http://localhost:$Port/"

$runLine
echo.
echo Server stopped.
pause
"@
Set-Content -Path $Launcher -Value $launcherText -Encoding ASCII
Ok "launcher created  ($LauncherName)"

# ================================================================= 5. shortcuts
Step 5 'Creating shortcuts'
$desktop = [Environment]::GetFolderPath('Desktop')
New-Shortcut (Join-Path $desktop "$AppName.lnk") $Launcher $InstallRoot $AppName
Ok 'Desktop shortcut'

$startMenu = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs'
New-Shortcut (Join-Path $startMenu "$AppName.lnk") $Launcher $InstallRoot $AppName
Ok 'Start Menu shortcut'

$startupLnk = Join-Path ([Environment]::GetFolderPath('Startup')) "$AppName.lnk"
if (Ask-YesNo 'Start the logger automatically when Windows starts?' $true) {
    New-Shortcut $startupLnk $Launcher $InstallRoot $AppName
    Ok 'run-at-login enabled'
} else {
    Remove-Item $startupLnk -ErrorAction SilentlyContinue
    Info 'run-at-login skipped'
}

# ================================================================= 6. start
Step 6 'Starting the server'
if ($NoStart) {
    Info 'skipped (-NoStart)'
} elseif (Test-Http "http://localhost:$Port/api/stats") {
    Ok "already running on port $Port"
} else {
    Start-Process -FilePath $Launcher -WorkingDirectory $InstallRoot
    if (Wait-For "http://localhost:$Port/api/stats" 45 'the server') {
        Ok "dashboard live at http://localhost:$Port/"
    } else {
        Warn "the server has not answered on port $Port yet - check the console window it opened."
    }
}

# ================================================================= 7. Chrome
Step 7 'Chrome extension'
Say ''
Say '      Chrome cannot install an unpacked extension by itself, so this is the'
Say '      one manual step. The folder path is already on your clipboard:'
Say ''
Write-Host "        $ExtDir" -ForegroundColor White
Say ''
Say '        1. Open  chrome://extensions  (this installer opens it for you)'
Say '        2. Turn on  Developer mode   (top-right toggle)'
Say '        3. Click    Load unpacked'
Say '        4. Paste the path above into the folder picker, Enter, Select Folder'
Say '        5. Pin the extension to the toolbar (puzzle icon)'
Say ''
try { Set-Clipboard -Value $ExtDir } catch { Warn 'could not write to the clipboard - copy the path above manually' }

$browser = $null
foreach ($p in @(
    (Join-Path $env:ProgramFiles 'Google\Chrome\Application\chrome.exe'),
    (Join-Path ${env:ProgramFiles(x86)} 'Google\Chrome\Application\chrome.exe'),
    (Join-Path $env:LOCALAPPDATA 'Google\Chrome\Application\chrome.exe'),
    (Join-Path $env:ProgramFiles 'BraveSoftware\Brave-Browser\Application\brave.exe'),
    (Join-Path ${env:ProgramFiles(x86)} 'Microsoft\Edge\Application\msedge.exe')
)) { if ($p -and (Test-Path $p)) { $browser = $p; break } }

if ($NoBrowser) {
    Info 'browser launch skipped (-NoBrowser)'
} elseif ($browser) {
    $extPage = if ($browser -like '*msedge*') { 'edge://extensions' } else { 'chrome://extensions' }
    Start-Process $browser $extPage
    Ok "opened $extPage"
} else {
    Warn 'no Chromium browser found - open chrome://extensions yourself'
}

# ================================================================= done
Say ''
Say '  ============================================================'
Write-Host '   Done.' -ForegroundColor Green
Say '  ============================================================'
Say "   Dashboard   http://localhost:$Port/"
Say "   Model       $Model  ($Mode)"
Say "   Installed   $InstallRoot"
Say "   Your data   $InstallRoot\errors.db  (never touched by re-installs)"
Say '   Start later Desktop shortcut, or Start Menu > Error Logger'
Say '   Remove      run Uninstall.bat from this installer folder'
Say ''
Say '   Daily use: finish a mock -> open the solutions page -> click the'
Say '              floating "Log Mistakes" button -> review at the dashboard.'
Say ''
if (-not $Silent) { Read-Host '   Press Enter to close' | Out-Null }
