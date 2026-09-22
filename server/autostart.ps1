<#
.SYNOPSIS
    Register catsole to start at logon.

.DESCRIPTION
    Creates a Scheduled Task that launches the service in your interactive
    logon session.

    This deliberately does NOT install a Windows service. Services run in
    session 0, and the System Media Transport Controls are per-user-session,
    so a service would see no media session at all and lyrics mode would be
    permanently blank.

    The task launches pythonw.exe so no console window lingers, which means
    the log file is the only place output goes. See the path printed by
    -Status.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File autostart.ps1 -Install
    powershell -ExecutionPolicy Bypass -File autostart.ps1 -Status
    powershell -ExecutionPolicy Bypass -File autostart.ps1 -Uninstall
#>

[CmdletBinding()]
param(
    [switch]$Install,
    [switch]$Uninstall,
    [switch]$Status,
    # The board enumerates a few seconds after logon, and LibreHardwareMonitor
    # (if you use it) needs longer still.
    [int]$DelaySeconds = 20
)

$ErrorActionPreference = 'Stop'

$TaskName = 'catsole'
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RunScript = Join-Path $ScriptDir 'run.py'
$LogFile = Join-Path $env:LOCALAPPDATA 'catsole\catsole.log'

function Get-PythonwPath {
    # Prefer the pythonw sitting beside whichever python is on PATH, so this
    # follows the interpreter you actually installed the requirements into.
    $python = (Get-Command python -ErrorAction SilentlyContinue).Source
    if ($python) {
        $candidate = Join-Path (Split-Path -Parent $python) 'pythonw.exe'
        if (Test-Path $candidate) { return $candidate }
    }
    $pythonw = (Get-Command pythonw -ErrorAction SilentlyContinue).Source
    if ($pythonw) { return $pythonw }
    throw "Could not find pythonw.exe. Is Python on PATH?"
}

function Show-Status {
    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if (-not $task) {
        Write-Host "catsole autostart: not installed"
        Write-Host "Install it with:  .\autostart.ps1 -Install"
        return
    }

    $info = Get-ScheduledTaskInfo -TaskName $TaskName
    Write-Host "catsole autostart: installed"
    Write-Host "  State        : $($task.State)"
    Write-Host "  Last run     : $($info.LastRunTime)"
    Write-Host "  Last result  : $($info.LastTaskResult)"
    Write-Host "  Log file     : $LogFile"
    if (Test-Path $LogFile) {
        Write-Host ""
        Write-Host "Last few log lines:"
        Get-Content $LogFile -Tail 5 | ForEach-Object { Write-Host "  $_" }
    }
}

function Install-Task {
    if (-not (Test-Path $RunScript)) {
        throw "run.py not found next to this script (looked in $ScriptDir)"
    }

    $pythonw = Get-PythonwPath
    Write-Host "Using interpreter: $pythonw"

    $action = New-ScheduledTaskAction -Execute $pythonw `
        -Argument "`"$RunScript`"" -WorkingDirectory $ScriptDir

    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
    $trigger.Delay = "PT${DelaySeconds}S"

    # ExecutionTimeLimit 0 means "never kill it"; this is a long-running
    # service, not a job that finishes.
    $settings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -DontStopOnIdleEnd `
        -RestartCount 3 `
        -RestartInterval (New-TimeSpan -Minutes 1) `
        -ExecutionTimeLimit (New-TimeSpan -Seconds 0) `
        -StartWhenAvailable

    $principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" `
        -LogonType Interactive -RunLevel Limited

    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
        -Settings $settings -Principal $principal -Force `
        -Description "Drives the catsole OLED over USB serial." | Out-Null

    Write-Host "Installed. catsole will start $DelaySeconds seconds after you log in."
    Write-Host "Start it now with:  Start-ScheduledTask -TaskName $TaskName"
    Write-Host "Logs:               $LogFile"
}

function Uninstall-Task {
    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if (-not $task) {
        Write-Host "Nothing to remove: catsole autostart is not installed."
        return
    }
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Removed. catsole will no longer start at logon."
}

if ($Install) { Install-Task }
elseif ($Uninstall) { Uninstall-Task }
else { Show-Status }
