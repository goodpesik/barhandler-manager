<#
.SYNOPSIS
    barhandler-manager installer for Windows.

.DESCRIPTION
    Installs Python 3.11+ via winget if missing, drops the manager
    under %USERPROFILE%\.barhandler-manager, creates a venv, installs
    dependencies, and registers a Scheduled Task that starts the
    service at user logon. Re-running upgrades to the latest release
    without touching config.yaml or printers.json.

    Behavior:
        not installed         → full install + start
        installed + running   → no-op (use -Force to upgrade)
        installed + stopped   → restart the service

    After a successful install, start.ps1 / stop.ps1 / status.ps1
    land in the install dir for manual control alongside the
    Scheduled Task.

.NOTES
    USB driver requirement: ESC/POS printers on Windows need a
    libusb-compatible driver bound to the printer's USB interface.
    Use Zadig (https://zadig.akeo.ie) to install the WinUSB driver
    against your printer's "USB Printing Support" interface — Windows'
    own USBPRINT driver isn't libusb-compatible and pyusb can't reach
    it.

.PARAMETER Force
    Reinstall / upgrade even if the manager is already running.
#>

param([switch]$Force)

$ErrorActionPreference = 'Stop'
$Repo         = 'goodpesik/barhandler-manager'
$InstallDir   = Join-Path $env:USERPROFILE '.barhandler-manager'
$TaskName     = 'BarhandlerManager'

function Write-Step($msg) { Write-Host "▸ $msg" -ForegroundColor Cyan }
function Write-Warn($msg) { Write-Host "⚠ $msg" -ForegroundColor Yellow }
function Die($msg)        { Write-Host "✗ $msg" -ForegroundColor Red; exit 1 }

function Test-Running {
    try {
        $r = Invoke-WebRequest -Uri 'http://localhost:9999/health' -UseBasicParsing -TimeoutSec 1
        return ($r.StatusCode -eq 200)
    } catch { return $false }
}

function Test-Installed {
    (Test-Path (Join-Path $InstallDir '.venv\Scripts\python.exe')) -and `
    (Test-Path (Join-Path $InstallDir 'main.py'))
}

# --- short-circuit if already installed ------------------------------
if ((Test-Installed) -and (-not $Force)) {
    if (Test-Running) {
        Write-Step "barhandler-manager is already installed and running at http://localhost:9999"
        Write-Step "    → re-run with -Force to upgrade to the latest release"
        exit 0
    } else {
        Write-Step "installed but not running — starting it"
        Start-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        exit 0
    }
}

# --- ensure Python 3.11+ ---------------------------------------------
function Get-PythonExe {
    foreach ($name in 'python3.11', 'python3.12', 'python3', 'python') {
        $cmd = Get-Command $name -ErrorAction SilentlyContinue
        if ($cmd) {
            $ver = & $cmd.Source --version 2>&1
            if ($ver -match 'Python 3\.(11|12|13)') { return $cmd.Source }
        }
    }
    return $null
}

$python = Get-PythonExe
if (-not $python) {
    Write-Step "Python 3.11+ not found — installing via winget"
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        Die "winget not available. Install Python 3.11+ from python.org and re-run."
    }
    winget install --silent --id Python.Python.3.11 --accept-package-agreements --accept-source-agreements
    # Refresh PATH for current session
    $env:Path = [System.Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' + [System.Environment]::GetEnvironmentVariable('Path', 'User')
    $python = Get-PythonExe
    if (-not $python) { Die "Python install reported success but no python on PATH" }
}
Write-Step "python: $(& $python --version)"

# --- download latest release zip -------------------------------------
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
Set-Location $InstallDir

Write-Step "fetching latest release"
$TarballUrl = "https://github.com/$Repo/archive/refs/heads/production.zip"
$Tmp        = New-Item -ItemType Directory -Path (Join-Path $env:TEMP "bhm-$(Get-Random)") -Force
$ZipPath    = Join-Path $Tmp 'src.zip'
Invoke-WebRequest -Uri $TarballUrl -OutFile $ZipPath -UseBasicParsing
Expand-Archive -Path $ZipPath -DestinationPath $Tmp -Force
$SrcRoot = (Get-ChildItem -Path $Tmp -Directory | Select-Object -First 1).FullName

# Preserve user config across re-installs.
foreach ($keep in 'config.yaml', 'printers.json', 'terminals.json') {
    $src = Join-Path $InstallDir $keep
    if (Test-Path $src) { Copy-Item $src (Join-Path $Tmp "$keep.bak") -Force }
}

# Copy source over, excluding our venv + user data.
$exclude = @('.venv', 'config.yaml', 'printers.json', 'terminals.json')
Get-ChildItem -Path $SrcRoot | Where-Object { $exclude -notcontains $_.Name } | ForEach-Object {
    Copy-Item -Path $_.FullName -Destination $InstallDir -Recurse -Force
}

# Restore user config.
foreach ($keep in 'config.yaml', 'printers.json', 'terminals.json') {
    $bak = Join-Path $Tmp "$keep.bak"
    if (Test-Path $bak) { Copy-Item $bak (Join-Path $InstallDir $keep) -Force }
}

Remove-Item $Tmp -Recurse -Force

# --- venv + deps ------------------------------------------------------
$VenvDir = Join-Path $InstallDir '.venv'
if (-not (Test-Path $VenvDir)) {
    Write-Step "creating virtualenv"
    & $python -m venv $VenvDir
}
$VenvPython = Join-Path $VenvDir 'Scripts\python.exe'
$VenvPip    = Join-Path $VenvDir 'Scripts\pip.exe'

Write-Step "installing Python dependencies"
& $VenvPython -m pip install --upgrade pip | Out-Null
& $VenvPip install -r (Join-Path $InstallDir 'requirements.txt')

# --- scheduled task --------------------------------------------------
Write-Step "registering Scheduled Task '$TaskName' (runs at logon)"
$Action  = New-ScheduledTaskAction `
    -Execute $VenvPython `
    -Argument "$InstallDir\main.py" `
    -WorkingDirectory $InstallDir
$Trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$Settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -DontStopOnIdleEnd `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Days 365)
$Principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive

Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $Action `
    -Trigger $Trigger `
    -Settings $Settings `
    -Principal $Principal | Out-Null

Start-ScheduledTask -TaskName $TaskName

# --- helper scripts --------------------------------------------------
$StartPs1 = @"
# Start barhandler-manager (or report that it's already running).
try {
    `$r = Invoke-WebRequest -Uri 'http://localhost:9999/health' -UseBasicParsing -TimeoutSec 1
    if (`$r.StatusCode -eq 200) {
        Write-Host '✓ already running at http://localhost:9999' -ForegroundColor Green
        exit 0
    }
} catch {}
Write-Host '▸ starting barhandler-manager' -ForegroundColor Cyan
Start-ScheduledTask -TaskName '$TaskName'
Start-Sleep -Seconds 2
try {
    `$r = Invoke-WebRequest -Uri 'http://localhost:9999/health' -UseBasicParsing -TimeoutSec 2
    Write-Host '✓ running at http://localhost:9999' -ForegroundColor Green
} catch {
    Write-Host '⚠ didn''t answer within 2s' -ForegroundColor Yellow
}
"@
$StopPs1 = @"
Write-Host '▸ stopping barhandler-manager' -ForegroundColor Cyan
Stop-ScheduledTask -TaskName '$TaskName'
"@
$StatusPs1 = @"
try {
    `$r = Invoke-WebRequest -Uri 'http://localhost:9999/health' -UseBasicParsing -TimeoutSec 1
    `$r.Content
    Write-Host '`n✓ running' -ForegroundColor Green
} catch {
    Write-Host '✗ not reachable on http://localhost:9999' -ForegroundColor Red
}
"@
$UpdatePs1 = @"
# Pull the latest release. Equivalent to:
#   irm https://github.com/$Repo/releases/latest/download/install.ps1 | iex
# but with -Force so install.ps1 runs upgrade-mode without prompting.
`$url = 'https://github.com/$Repo/releases/latest/download/install.ps1'
`$script = Invoke-WebRequest -Uri `$url -UseBasicParsing
Invoke-Expression "& { `$(`$script.Content) } -Force"
"@

Set-Content -Path (Join-Path $InstallDir 'start.ps1')  -Value $StartPs1
Set-Content -Path (Join-Path $InstallDir 'stop.ps1')   -Value $StopPs1
Set-Content -Path (Join-Path $InstallDir 'status.ps1') -Value $StatusPs1
Set-Content -Path (Join-Path $InstallDir 'update.ps1') -Value $UpdatePs1

# .cmd wrappers so double-click works without changing execution policy
Set-Content -Path (Join-Path $InstallDir 'start.cmd') -Value "@powershell -NoProfile -ExecutionPolicy Bypass -File `"%~dp0start.ps1`""
Set-Content -Path (Join-Path $InstallDir 'stop.cmd')  -Value "@powershell -NoProfile -ExecutionPolicy Bypass -File `"%~dp0stop.ps1`""
Set-Content -Path (Join-Path $InstallDir 'status.cmd') -Value "@powershell -NoProfile -ExecutionPolicy Bypass -File `"%~dp0status.ps1`""
Set-Content -Path (Join-Path $InstallDir 'update.cmd') -Value "@powershell -NoProfile -ExecutionPolicy Bypass -File `"%~dp0update.ps1`""

# --- smoke test -------------------------------------------------------
Start-Sleep -Seconds 3
if (Test-Running) {
    Write-Step "✓ manager is up at http://localhost:9999"
} else {
    Write-Warn "manager didn't answer /health within 3s — check $InstallDir\bhm.log"
}

Write-Host @"

╭──────────────────────────────────────────────────────────────╮
│  Installed under: $InstallDir
│  Helpers (double-click .cmd or run .ps1):
│   $InstallDir\start.cmd
│   $InstallDir\stop.cmd
│   $InstallDir\status.cmd
│   $InstallDir\update.cmd    ← fetches the latest release
│
│  USB driver:
│   ESC/POS USB printers need libusb-compatible drivers on
│   Windows. Use Zadig (https://zadig.akeo.ie) to bind WinUSB
│   to your printer's 'USB Printing Support' interface — the
│   stock USBPRINT driver isn't libusb-compatible.
│
│  Next steps:
│   1. Open your POS web app
│   2. Settings → Integrations → "Use device manager" → toggle ON
│   3. Click "Discover printers"
╰──────────────────────────────────────────────────────────────╯
"@
