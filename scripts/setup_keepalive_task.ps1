<#
.SYNOPSIS
    Register (or remove) the AlpacaSwingBotKeepAlive Windows Scheduled Task.

.DESCRIPTION
    Creates a Task Scheduler job that fires keep_alive.py every 30 minutes
    using pythonw.exe — no console window ever appears.

    The task delegates all start/stop logic to manage.ps1 (idempotent, safe
    against duplicates). If bot + dashboard are both healthy it exits silently
    in under a second and nothing is restarted.

    Run as Administrator so the task gets "Run with highest privileges".
    The task runs under the current interactive user so it shares the same
    environment, credentials, and file access as a normal session.

.PARAMETER IntervalMinutes
    How often the watchdog fires (default: 30).

.PARAMETER Unregister
    Pass this switch to remove the task instead of registering it.

.EXAMPLE
    # Register (run from project root as Admin)
    pwsh scripts\setup_keepalive_task.ps1

    # Change interval to 15 minutes
    pwsh scripts\setup_keepalive_task.ps1 -IntervalMinutes 15

    # Remove the task
    pwsh scripts\setup_keepalive_task.ps1 -Unregister
#>
[CmdletBinding()]
param(
    [int]$IntervalMinutes = 30,
    [switch]$Unregister
)

$ErrorActionPreference = 'Stop'
$TaskName = 'AlpacaSwingBotKeepAlive'
$Root     = Split-Path -Parent $PSScriptRoot
$Pythonw  = Join-Path $Root '.venv\Scripts\pythonw.exe'
$Script   = Join-Path $Root 'keep_alive.py'
$LogsDir  = Join-Path $Root 'logs'

# ── Remove ────────────────────────────────────────────────────────────────────
if ($Unregister) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "Task '$TaskName' removed." -ForegroundColor Yellow
    exit 0
}

# ── Pre-flight checks ─────────────────────────────────────────────────────────
if (-not (Test-Path $Pythonw)) {
    Write-Error "pythonw.exe not found: $Pythonw`nActivate the venv and try again."
    exit 1
}
if (-not (Test-Path $Script)) {
    Write-Error "keep_alive.py not found: $Script"
    exit 1
}
if (-not (Test-Path $LogsDir)) { New-Item -ItemType Directory -Path $LogsDir | Out-Null }

# ── Build task components ─────────────────────────────────────────────────────

# Action: pythonw.exe "keep_alive.py"  (no window, no console)
$action = New-ScheduledTaskAction `
    -Execute      $Pythonw `
    -Argument     "`"$Script`"" `
    -WorkingDirectory $Root

# Trigger: once at boot then repeat every N minutes, indefinitely
$trigger = New-ScheduledTaskTrigger `
    -Once `
    -At                  ([datetime]::Today) `
    -RepetitionInterval  (New-TimeSpan -Minutes $IntervalMinutes)

# Settings: 10-min execution limit, skip if already running (IgnoreNew)
$settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit      (New-TimeSpan -Minutes 10) `
    -MultipleInstances       IgnoreNew `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries

# Principal: current user, highest privilege, interactive session
$principal = New-ScheduledTaskPrincipal `
    -UserId    ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) `
    -LogonType Interactive `
    -RunLevel  Highest

# ── Register (overwrites if already exists) ───────────────────────────────────
Register-ScheduledTask `
    -TaskName   $TaskName `
    -Action     $action `
    -Trigger    $trigger `
    -Settings   $settings `
    -Principal  $principal `
    -Description "Alpaca Swing Bot V2 watchdog — keeps bot + dashboard alive. Runs keep_alive.py every ${IntervalMinutes}min via pythonw (no window)." `
    -Force | Out-Null

Write-Host ""
Write-Host "Task registered: '$TaskName'" -ForegroundColor Green
Write-Host "  Interval  : every $IntervalMinutes minutes" -ForegroundColor Cyan
Write-Host "  Executable: $Pythonw" -ForegroundColor Cyan
Write-Host "  Script    : $Script" -ForegroundColor Cyan
Write-Host "  Watchdog log : $LogsDir\keepalive.log" -ForegroundColor Cyan
Write-Host "  Manage log   : $LogsDir\keepalive_manage.log" -ForegroundColor Cyan
Write-Host ""
Write-Host "Task Scheduler commands:" -ForegroundColor DarkGray
Write-Host "  Run now    : Start-ScheduledTask -TaskName '$TaskName'"
Write-Host "  Status     : Get-ScheduledTaskInfo -TaskName '$TaskName'"
Write-Host "  Edit GUI   : taskschd.msc"
Write-Host "  Remove     : pwsh scripts\setup_keepalive_task.ps1 -Unregister"
