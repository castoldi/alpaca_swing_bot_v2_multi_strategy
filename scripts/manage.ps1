<#
.SYNOPSIS
    Verified singleton control for this project's bot and dashboard.
.DESCRIPTION
    Delegates to service_manager.py, which serializes lifecycle operations and
    verifies executable, project command, creation time, and health before acting.
    Status also writes run/processes.json with bot/dashboard/backtest PIDs.
#>
[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet('status', 'start-bot', 'stop-bot', 'restart-bot',
                 'start-dashboard', 'stop-dashboard', 'restart-dashboard')]
    [string]$Command = 'status',
    [string]$Strategy,
    [Nullable[int]]$Interval,
    [int]$Port = 8004
)
$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$ProjectPython = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $ProjectPython)) {
    throw "Project interpreter missing: $ProjectPython"
}
$ManagerArgs = @((Join-Path $ProjectRoot 'service_manager.py'), $Command, '--port', "$Port")
if ($PSBoundParameters.ContainsKey('Strategy')) { $ManagerArgs += @('--strategy', $Strategy) }
if ($PSBoundParameters.ContainsKey('Interval')) { $ManagerArgs += @('--interval', "$Interval") }
& $ProjectPython @ManagerArgs
exit $LASTEXITCODE
