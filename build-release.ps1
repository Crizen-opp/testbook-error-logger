<#
    Builds the shareable one-click package.

    Produces  release\TestbookErrorLogger-Setup\  and a zip of it:

        TestbookErrorLogger-Setup\
          READ-ME-FIRST.txt    <- quick start
          README.md            <- full manual
          setup-guide.html     <- same manual, opens in a browser
          Install.bat          <- what the other person double-clicks
          Uninstall.bat
          install.ps1
          uninstall.ps1
          app\testbook-server.exe
          extension\...

    Your errors.db, captured images and logs are never copied in - the check at
    the end fails the build if any slipped through.

    Usage:
      powershell -ExecutionPolicy Bypass -File build-release.ps1
        -SkipBuild    reuse server\dist\testbook-server.exe as-is
        -NoZip        leave the folder, don't zip it
#>
[CmdletBinding()]
param([switch]$SkipBuild, [switch]$NoZip)

$ErrorActionPreference = 'Stop'

$Root      = $PSScriptRoot
$ServerDir = Join-Path $Root 'server'
$OutRoot   = Join-Path $Root 'release'
$Payload   = Join-Path $OutRoot 'TestbookErrorLogger-Setup'
$Exe       = Join-Path $ServerDir 'dist\testbook-server.exe'

function Step ($m) { Write-Host ''; Write-Host "==> $m" -ForegroundColor Cyan }
function Ok   ($m) { Write-Host "    OK   $m" -ForegroundColor Green }
function Info ($m) { Write-Host "    $m" -ForegroundColor DarkGray }

# Returns a python.exe that can actually run PyInstaller. The dev venv is used
# when it still works; a venv whose base Python was uninstalled (a very common
# way for this to break) is bypassed in favour of a dedicated build venv.
function Test-Python ($exe) {
    if (-not $exe -or -not (Test-Path $exe)) { return $false }
    try { & $exe -c 'pass' 2>&1 | Out-Null; return ($LASTEXITCODE -eq 0) } catch { return $false }
}

function Get-BuildPython {
    $devVenv = Join-Path $ServerDir 'venv\Scripts\python.exe'
    if (Test-Python $devVenv) {
        & $devVenv -c 'import PyInstaller' 2>&1 | Out-Null
        if ($LASTEXITCODE -ne 0) {
            Info 'installing PyInstaller into serverenv ...'
            & $devVenv -m pip install --quiet pyinstaller
        }
        if ($LASTEXITCODE -eq 0) { return $devVenv }
    } elseif (Test-Path $devVenv) {
        Info 'serverenv is broken (its base Python is gone) - using a separate build venv'
    }

    $buildVenv   = Join-Path $ServerDir '.buildvenv'
    $buildVenvPy = Join-Path $buildVenv 'Scripts\python.exe'
    if (-not (Test-Python $buildVenvPy)) {
        $sys = Get-Command py -ErrorAction SilentlyContinue
        if (-not $sys) { $sys = Get-Command python -ErrorAction SilentlyContinue }
        if (-not $sys) { throw 'No Python found on PATH. Install Python 3.9+ from python.org and re-run.' }
        Info "creating build venv with $($sys.Source) ..."
        Remove-Item $buildVenv -Recurse -Force -ErrorAction SilentlyContinue
        & $sys.Source -m venv $buildVenv
        if ($LASTEXITCODE -ne 0) { throw 'could not create the build venv' }
        & $buildVenvPy -m pip install --quiet --upgrade pip
        Info 'installing build dependencies (fastapi, uvicorn, httpx, pyinstaller) ...'
        & $buildVenvPy -m pip install --quiet -r (Join-Path $ServerDir 'requirements.txt') pyinstaller
        if ($LASTEXITCODE -ne 0) { throw 'build dependency install failed' }
    }
    return $buildVenvPy
}

# ---------------------------------------------------------------- 1. build exe
if ($SkipBuild) {
    Step 'Skipping PyInstaller build (-SkipBuild)'
    if (-not (Test-Path $Exe)) { throw "No existing exe at $Exe - drop -SkipBuild." }
} else {
    Step 'Building testbook-server.exe with PyInstaller'
    $venvPy = Get-BuildPython
    Info "python: $venvPy"
    Push-Location $ServerDir
    try {
        & $venvPy -m PyInstaller --noconfirm --clean testbook_server.spec
        if ($LASTEXITCODE -ne 0) { throw 'PyInstaller failed - see the output above.' }
    } finally { Pop-Location }
    Ok 'exe built'
}

$exeInfo = Get-Item $Exe
Info ("exe: {0}  ({1:N1} MB, built {2})" -f $exeInfo.Name, ($exeInfo.Length / 1MB), $exeInfo.LastWriteTime)

# ---------------------------------------------------------------- 2. assemble
Step 'Assembling the payload'
# Empty the folder rather than deleting it - an open Explorer window or shell
# sitting in it locks the directory itself, and that is not a build failure.
if (Test-Path $Payload) {
    Get-ChildItem $Payload -Force | Remove-Item -Recurse -Force -ErrorAction Stop
}
New-Item -ItemType Directory -Force -Path (Join-Path $Payload 'app'),
                                         (Join-Path $Payload 'extension') | Out-Null

Copy-Item $Exe (Join-Path $Payload 'app\testbook-server.exe') -Force

# Extension: only the files the browser actually loads.
foreach ($f in @('manifest.json', 'background.js', 'content.js', 'content.css', 'popup.html', 'popup.js', 'icon.png')) {
    $src = Join-Path $Root "extension\$f"
    if (-not (Test-Path $src)) { throw "extension\$f is missing" }
    Copy-Item $src (Join-Path $Payload 'extension') -Force
}

foreach ($f in @('Install.bat', 'Uninstall.bat', 'install.ps1', 'uninstall.ps1', 'READ-ME-FIRST.txt', 'README.md', 'setup-guide.html')) {
    Copy-Item (Join-Path $Root "installer\$f") $Payload -Force
}
Ok 'files copied'

# ---------------------------------------------------------------- 3. privacy check
Step 'Checking for personal data'
$leaks = Get-ChildItem $Payload -Recurse -File |
         Where-Object { $_.Name -match '\.db$|\.db\.bak|\.log$' -or $_.Name -like 'errors_images*' }
if ($leaks) {
    $leaks | ForEach-Object { Write-Host "    LEAK $($_.FullName)" -ForegroundColor Red }
    throw 'Personal data found in the payload - build aborted.'
}
Ok 'no database, images or logs in the payload'

$count = (Get-ChildItem $Payload -Recurse -File).Count
$size  = (Get-ChildItem $Payload -Recurse -File | Measure-Object Length -Sum).Sum / 1MB
Info ("{0} files, {1:N1} MB" -f $count, $size)

# ---------------------------------------------------------------- 4. zip
if ($NoZip) {
    Step 'Zip skipped (-NoZip)'
} else {
    Step 'Zipping'
    $zip = Join-Path $OutRoot ('TestbookErrorLogger-Setup-{0}.zip' -f (Get-Date -Format 'yyyyMMdd'))
    if (Test-Path $zip) { Remove-Item $zip -Force }
    Compress-Archive -Path $Payload -DestinationPath $zip
    Ok ("{0}  ({1:N1} MB)" -f (Split-Path $zip -Leaf), ((Get-Item $zip).Length / 1MB))
    Write-Host ''
    Write-Host "    Send this file: $zip" -ForegroundColor White
}

Write-Host ''
Write-Host '    Tell them: unzip everything first, then double-click Install.bat.' -ForegroundColor DarkGray
Write-Host ''
