<#
.SYNOPSIS
    Install catsole on Windows: the service, the firmware, and autostart.

.DESCRIPTION
    Paste into PowerShell:

        irm https://raw.githubusercontent.com/teterw/catsole/main/install.ps1 | iex

    It installs Python if there is none, puts the app and its own virtual
    environment in %LOCALAPPDATA%\catsole, flashes the board if it is
    plugged in, and registers a logon task so catsole starts with Windows.
    Running it again updates in place, keeping config.json and the lyrics
    cache.

    To remove it:

        & ([scriptblock]::Create((irm https://raw.githubusercontent.com/teterw/catsole/main/install.ps1))) -Uninstall
#>
param(
    [switch]$Uninstall,
    # Skip flashing the board, e.g. when it already runs this version.
    [switch]$NoFlash,
    [string]$Dir = (Join-Path $env:LOCALAPPDATA 'catsole'),
    [string]$Branch = 'main',
    # Install from a local checkout instead of downloading. For testing.
    [string]$Source = '',
    [string]$TaskName = 'catsole'
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'   # Invoke-WebRequest crawls with the bar on
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
if ($env:CATSOLE_NO_FLASH) { $NoFlash = $true }

$Repo = 'teterw/catsole'
$App = Join-Path $Dir 'app'
$Venv = Join-Path $Dir 'venv'
$Tools = Join-Path $Dir 'tools'
$LogFile = Join-Path $env:LOCALAPPDATA 'catsole\catsole.log'
$Fqbn = 'arduino:renesas_uno:unor4wifi'
$Core = 'arduino:renesas_uno@1.6.0'
$Libraries = @('U8g2@2.35.30', 'ArduinoJson@7.4.2')

function Say([string]$text) { Write-Host "==> $text" -ForegroundColor Cyan }

function Remove-Tree([string]$path) {
    # The Arduino folders nest past Windows' 260-character limit, which
    # Remove-Item in Windows PowerShell cannot cross; rmdir can, given \\?\.
    if (Test-Path -LiteralPath $path) {
        cmd /c "rmdir /s /q `"\\?\$path`"" | Out-Null
    }
}
function Note([string]$text) { Write-Host "    $text" }

function Stop-Catsole {
    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($task) { Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue }
    # The task restarts on failure, so the process is stopped after the task.
    Get-CimInstance Win32_Process -Filter "Name like 'python%'" |
        Where-Object { $_.CommandLine -and $_.CommandLine -like "*$App*run.py*" } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
}

if ($Uninstall) {
    Say "Removing catsole"
    Stop-Catsole
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    }
    Remove-Tree $Dir
    $shortArd = Join-Path $env:LOCALAPPDATA 'catsole-ard'
    if ($Dir -eq (Join-Path $env:LOCALAPPDATA 'catsole')) { Remove-Tree $shortArd }
    Say "Removed. The board keeps its firmware; it just waits for a PC again."
    return
}

# ---- Python ------------------------------------------------------------------

function Test-Python([string]$exe, [string[]]$pre = @()) {
    # The Microsoft Store stub named python.exe prints nothing and opens the
    # Store, so a candidate has to actually report a usable version.
    try {
        $out = & $exe @pre -c "import sys; print('%d.%d|%s' % (sys.version_info[0], sys.version_info[1], sys.executable))" 2>$null
    } catch { return $null }
    if (-not $out -or $out -notmatch '^(\d+)\.(\d+)\|(.+)$') { return $null }
    if ([int]$Matches[1] -ne 3 -or [int]$Matches[2] -lt 10) { return $null }
    return $Matches[3].Trim()
}

function Find-Python {
    if (Get-Command py -ErrorAction SilentlyContinue) {
        $found = Test-Python 'py' @('-3')
        if ($found) { return $found }
    }
    foreach ($name in 'python', 'python3') {
        if (Get-Command $name -ErrorAction SilentlyContinue) {
            $found = Test-Python $name
            if ($found) { return $found }
        }
    }
    $local = Get-ChildItem (Join-Path $env:LOCALAPPDATA 'Programs\Python') -Filter python.exe -Recurse -ErrorAction SilentlyContinue |
        Sort-Object FullName -Descending | Select-Object -First 1
    if ($local) { return Test-Python $local.FullName }
    return $null
}

Say "Looking for Python 3.10 or newer"
$Python = Find-Python
if (-not $Python) {
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        throw "Python 3.10+ is needed. Install it from https://www.python.org/downloads/ and run this again."
    }
    Say "Installing Python with winget"
    winget install -e --id Python.Python.3.13 --scope user --silent --accept-package-agreements --accept-source-agreements | Out-Host
    $Python = Find-Python
    if (-not $Python) { throw "Python was installed but could not be found. Open a new PowerShell window and run this again." }
}
Note $Python

# ---- the app -----------------------------------------------------------------

Say "Stopping any running copy"
Stop-Catsole

Say "Fetching catsole ($Branch)"
New-Item -ItemType Directory -Force $Dir | Out-Null
$staging = Join-Path $env:TEMP ("catsole-" + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Force $staging | Out-Null
try {
    if ($Source) {
        $fresh = Join-Path $staging 'src'
        New-Item -ItemType Directory -Force $fresh | Out-Null
        foreach ($part in 'arduino', 'server', 'README.md', 'install.ps1', 'install.sh') {
            $from = Join-Path $Source $part
            if (Test-Path $from) { Copy-Item -Recurse $from $fresh }
        }
        foreach ($junk in 'server\cache', 'server\config.json') {
            $p = Join-Path $fresh $junk
            if (Test-Path $p) { Remove-Item -Recurse -Force $p }
        }
    } else {
        $zip = Join-Path $staging 'catsole.zip'
        Invoke-WebRequest -UseBasicParsing "https://github.com/$Repo/archive/refs/heads/$Branch.zip" -OutFile $zip
        Expand-Archive $zip -DestinationPath $staging
        $fresh = Get-ChildItem $staging -Directory | Where-Object { $_.Name -like 'catsole-*' } | Select-Object -First 1 -ExpandProperty FullName
    }

    # Keep what is yours across an update: settings and fetched lyrics.
    foreach ($keep in 'server\config.json', 'server\cache') {
        $old = Join-Path $App $keep
        if (Test-Path $old) { Move-Item $old (Join-Path $fresh $keep) -Force }
    }
    Remove-Tree $App
    Move-Item $fresh $App
} finally {
    Remove-Tree $staging
}

Say "Installing Python packages (a minute or two the first time)"
$VenvPython = Join-Path $Venv 'Scripts\python.exe'
if (-not (Test-Path $VenvPython)) {
    & $Python -m venv $Venv
    if ($LASTEXITCODE) { throw "Could not create the virtual environment." }
}
& $VenvPython -m pip install --quiet --disable-pip-version-check --upgrade pip
& $VenvPython -m pip install --quiet --disable-pip-version-check -r (Join-Path $App 'server\requirements.txt')
if ($LASTEXITCODE) { throw "Installing the Python packages failed; see the messages above." }

# ---- the board -----------------------------------------------------------------

function Find-Board {
    # The R4 enumerates as "USB Serial Device (COMn)" under Arduino's USB ids.
    $dev = Get-CimInstance Win32_PnPEntity -Filter "DeviceID LIKE 'USB%VID_2341&PID_1002%'" -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -match '\((COM\d+)\)' } | Select-Object -First 1
    if ($dev -and $dev.Name -match '\((COM\d+)\)') { return $Matches[1] }
    return $null
}

if ($NoFlash) {
    Say "Skipping the firmware, as asked"
} else {
    $port = Find-Board
    if (-not $port) {
        Say "No board found, so the firmware was not flashed"
        Note "Plug it in and run this command again to flash it."
    } else {
        Say "Flashing the board on $port"
        $cli = Join-Path $Tools 'arduino-cli.exe'
        if (-not (Test-Path $cli)) {
            New-Item -ItemType Directory -Force $Tools | Out-Null
            $cliZip = Join-Path $Tools 'arduino-cli.zip'
            Invoke-WebRequest -UseBasicParsing 'https://downloads.arduino.cc/arduino-cli/arduino-cli_latest_Windows_64bit.zip' -OutFile $cliZip
            Expand-Archive $cliZip -DestinationPath $Tools -Force
            Remove-Item $cliZip
        }
        # A private Arduino setup, so nothing here touches an Arduino IDE
        # you may already have, and its library versions stay pinned. The
        # board's 2017 compiler opens its headers by paths with '..' still
        # in them, which break Windows' 260-character limit from a deep
        # folder, so it lives somewhere short.
        $arduino = Join-Path $Dir 'ard'
        if ($arduino.Length -gt 48) { $arduino = Join-Path $env:LOCALAPPDATA 'catsole-ard' }
        $cfg = Join-Path $arduino 'cli.yaml'
        New-Item -ItemType Directory -Force $arduino | Out-Null
        @(
            'directories:',
            "  data: '$(Join-Path $arduino 'd')'",
            "  downloads: '$(Join-Path $arduino 'dl')'",
            "  user: '$(Join-Path $arduino 'u')'"
        ) | Set-Content -Encoding ascii $cfg

        Note "Board support and libraries (large the first time)"
        & $cli --config-file $cfg core update-index | Out-Null
        & $cli --config-file $cfg core install $Core | Out-Null
        if ($LASTEXITCODE) { throw "Could not install the UNO R4 board support." }
        & $cli --config-file $cfg lib install @Libraries | Out-Null
        if ($LASTEXITCODE) { throw "Could not install the display libraries." }

        Note "Compiling and uploading"
        & $cli --config-file $cfg compile --upload -p $port --fqbn $Fqbn (Join-Path $App 'arduino\catsole') | Out-Null
        if ($LASTEXITCODE) { throw "Flashing failed. Unplug the board, plug it back in, and run this again." }
        Note "Flashed."
    }
}

# ---- autostart -----------------------------------------------------------------

Say "Setting catsole to start when you log in"
$pythonw = Join-Path $Venv 'Scripts\pythonw.exe'
& powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $App 'server\autostart.ps1') `
    -Install -Pythonw $pythonw -TaskName $TaskName | Out-Null
if ($LASTEXITCODE) { throw "Registering the logon task failed." }

Say "Starting it now"
Start-ScheduledTask -TaskName $TaskName
$deadline = (Get-Date).AddSeconds(20)
$ok = $false
while ((Get-Date) -lt $deadline) {
    try {
        Invoke-RestMethod -UseBasicParsing 'http://127.0.0.1:8730/api/state' -TimeoutSec 2 | Out-Null
        $ok = $true
        break
    } catch { Start-Sleep -Milliseconds 700 }
}

Write-Host ""
if ($ok) {
    Say "catsole is running."
    Note "Control panel: http://127.0.0.1:8730"
} else {
    Say "Installed, but it has not answered yet. The log will say why:"
    Note $LogFile
}
Note "It starts by itself whenever you log in."
