<#
    Testbook Error Logger - uninstaller.

    Removes the app, the launcher and the shortcuts. Keeps your errors.db and
    captured images unless you explicitly say otherwise. Leaves Ollama alone -
    uninstall that from Windows Settings > Apps if you want it gone too.
#>
[CmdletBinding()]
param([switch]$Silent, [switch]$RemoveData)

$ErrorActionPreference = 'Continue'

$AppName     = 'Testbook Error Logger'
$InstallRoot = Join-Path $env:LOCALAPPDATA 'TestbookErrorLogger'
$Db          = Join-Path $InstallRoot 'errors.db'
$Images      = Join-Path $InstallRoot 'errors_images'

function Say  ($m) { Write-Host $m }
function Ok   ($m) { Write-Host "  OK   $m" -ForegroundColor Green }
function Info ($m) { Write-Host "  $m" -ForegroundColor DarkGray }

Say ''
Say "  $AppName - uninstall"
Say '  ------------------------------------------'

# --- stop the server if it is running
$procs = Get-Process -Name 'testbook-server' -ErrorAction SilentlyContinue
if ($procs) {
    $procs | Stop-Process -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 1
    Ok 'stopped the running server'
}

# --- shortcuts
$links = @(
    (Join-Path ([Environment]::GetFolderPath('Desktop')) "$AppName.lnk"),
    (Join-Path ([Environment]::GetFolderPath('Startup')) "$AppName.lnk"),
    (Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\$AppName.lnk")
)
foreach ($l in $links) {
    if (Test-Path $l) { Remove-Item $l -Force -ErrorAction SilentlyContinue }
}
Ok 'shortcuts removed'

# --- app files
foreach ($d in @((Join-Path $InstallRoot 'app'), (Join-Path $InstallRoot 'extension'))) {
    if (Test-Path $d) { Remove-Item $d -Recurse -Force -ErrorAction SilentlyContinue }
}
Remove-Item (Join-Path $InstallRoot 'Start Error Logger.cmd') -Force -ErrorAction SilentlyContinue
Ok 'program files removed'

# --- data
$dropData = $RemoveData
if (-not $dropData -and -not $Silent -and (Test-Path $Db)) {
    $sizeKb = [math]::Round((Get-Item $Db).Length / 1KB)
    Say ''
    Say "  Your flashcard database is still here ($sizeKb KB):"
    Say "    $Db"
    $a = (Read-Host '  Delete it too? This cannot be undone [y/N]').Trim().ToLower()
    $dropData = ($a -eq 'y' -or $a -eq 'yes')
}

if ($dropData) {
    Remove-Item $Db -Force -ErrorAction SilentlyContinue
    Remove-Item $Images -Recurse -Force -ErrorAction SilentlyContinue
    Ok 'database and images deleted'
} elseif (Test-Path $Db) {
    Info "database kept at $Db"
}

if ((Test-Path $InstallRoot) -and -not (Get-ChildItem $InstallRoot -Force)) {
    Remove-Item $InstallRoot -Force -ErrorAction SilentlyContinue
}

Say ''
Say '  Done. Two things this did NOT touch:'
Say '    - Ollama and its models  (Settings > Apps > Ollama to remove)'
Say '    - the Chrome extension   (chrome://extensions > Remove)'
Say ''
if (-not $Silent) { Read-Host '  Press Enter to close' | Out-Null }
