<#
.SYNOPSIS
    Single control point for the Alpaca Swing Bot V2 bot + dashboard.

    The whole point of this script is to NEVER spawn a duplicate. Both the bot
    and the dashboard are idempotent: "start" first checks whether a live, HEALTHY
    instance is already running and, if so, does nothing. Duplicate looping bots
    were the cause of the email flood.

.DESCRIPTION
    Health model:
      Bot       = run/bot.pid process is alive AND run/bot.heartbeat is fresh
                  (refreshed within ~2 loop intervals). Alive-but-stale = "hung".
      Dashboard = HTTP probe of http://localhost:8004 succeeds.

    State files (all under ./run/, git-ignored):
      run/bot.pid / bot.meta.json / bot.heartbeat   (written by bot.py via runtime.py)
      run/dashboard.pid / dashboard.meta.json        (written here)

.EXAMPLE
    pwsh scripts/manage.ps1 status
    pwsh scripts/manage.ps1 start-bot -Strategy ensemble -Interval 30
    pwsh scripts/manage.ps1 start-dashboard
    pwsh scripts/manage.ps1 stop-bot
    pwsh scripts/manage.ps1 restart-dashboard
#>
[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet('status', 'start-bot', 'stop-bot', 'restart-bot',
                 'start-dashboard', 'stop-dashboard', 'restart-dashboard')]
    [string]$Command = 'status',

    [string]$Strategy = 'ensemble',
    [int]$Interval = 30,
    [int]$Port = 8004
)

$ErrorActionPreference = 'Stop'
$Root    = Split-Path -Parent $PSScriptRoot
$RunDir  = Join-Path $Root 'run'
$Pythonw = Join-Path $Root '.venv\Scripts\pythonw.exe'
$Python  = if (Test-Path $Pythonw) { $Pythonw } else { Join-Path $Root '.venv\Scripts\python.exe' }
$LogsDir = Join-Path $Root 'logs'
$DashUrl = "http://localhost:$Port"

if (-not (Test-Path $RunDir))  { New-Item -ItemType Directory -Path $RunDir  | Out-Null }
if (-not (Test-Path $LogsDir)) { New-Item -ItemType Directory -Path $LogsDir | Out-Null }
if (-not (Test-Path $Python)) {
    Write-Host "WARNING: venv pythonw not found at $Python — falling back to 'python'" -ForegroundColor Yellow
    $Python = 'python'
}

# ── helpers ──────────────────────────────────────────────────────────────────

function Read-Pid($service) {
    $f = Join-Path $RunDir "$service.pid"
    if (Test-Path $f) { return [int](Get-Content $f -Raw).Trim() }
    return $null
}

function Get-LiveProc($procId) {
    if (-not $procId) { return $null }
    return Get-Process -Id $procId -ErrorAction SilentlyContinue
}

function Get-BotProcesses {
    # Any python running THIS project's bot.py loop (scoped by --strategy invocation).
    # Match both python.exe AND pythonw.exe — manage.ps1 launches with pythonw.exe
    # (no console window), so a python.exe-only filter never catches the real process.
    Get-CimInstance Win32_Process -Filter "(Name='python.exe' OR Name='pythonw.exe')" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -like '*bot.py*--strategy*' }
}

function Get-DashboardProcesses {
    # uvicorn serving this project's dashboard on the chosen port (python.exe or pythonw.exe).
    Get-CimInstance Win32_Process -Filter "(Name='python.exe' OR Name='pythonw.exe')" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -like "*uvicorn*dashboard.server:app*$Port*" }
}

function Get-PortOwnerPid($port) {
    $c = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($c) { return [int]$c.OwningProcess }
    return $null
}

function Test-DashboardHealthy {
    try {
        $r = Invoke-WebRequest -Uri $DashUrl -UseBasicParsing -TimeoutSec 4 -ErrorAction Stop
        return ($r.StatusCode -ge 200 -and $r.StatusCode -lt 500)
    } catch { return $false }
}

function Get-BotHealth {
    # Returns a hashtable: Alive, Healthy, Pid, AgeSec, Reason
    $procId = Read-Pid 'bot'
    $proc = Get-LiveProc $procId
    if (-not $proc) {
        # pidfile dead/missing — is there an orphan bot anyway?
        $orphan = Get-BotProcesses | Select-Object -First 1
        if ($orphan) {
            return @{ Alive = $true; Healthy = $false; Pid = [int]$orphan.ProcessId
                      AgeSec = $null; Reason = 'orphan bot.py running without a valid pidfile' }
        }
        return @{ Alive = $false; Healthy = $false; Pid = $null; AgeSec = $null; Reason = 'not running' }
    }

    $hbFile = Join-Path $RunDir 'bot.heartbeat'
    $metaFile = Join-Path $RunDir 'bot.meta.json'
    $interval = $Interval
    if (Test-Path $metaFile) {
        try { $interval = [int](Get-Content $metaFile -Raw | ConvertFrom-Json).interval } catch {}
    }
    if (-not (Test-Path $hbFile)) {
        return @{ Alive = $true; Healthy = $false; Pid = $procId; AgeSec = $null; Reason = 'no heartbeat file' }
    }
    $hb = [datetimeoffset]::Parse((Get-Content $hbFile -Raw).Trim())
    $ageSec = [int]([datetimeoffset]::UtcNow - $hb).TotalSeconds
    # fresh if within 2 intervals + 5 min buffer (covers a long fetch pass)
    $maxAge = ($interval * 2 * 60) + 300
    if ($ageSec -le $maxAge) {
        return @{ Alive = $true; Healthy = $true; Pid = $procId; AgeSec = $ageSec; Reason = 'looping' }
    }
    return @{ Alive = $true; Healthy = $false; Pid = $procId; AgeSec = $ageSec
              Reason = "heartbeat stale (${ageSec}s > ${maxAge}s) — hung" }
}

function Stop-Tree($procId) {
    if (-not $procId) { return }
    & taskkill /PID $procId /T /F 2>$null | Out-Null
    # taskkill can return before the process is gone (or silently no-op on some
    # pythonw children). Verify and fall back to Stop-Process so a "stop" that
    # claims success can be trusted by the follow-up "start".
    for ($i = 0; $i -lt 10; $i++) {
        if (-not (Get-LiveProc $procId)) { return }
        Start-Sleep -Milliseconds 300
        Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue
    }
}

function Wait-PortFree($port, $timeoutSec = 8) {
    # Block until nothing is LISTENing on $port (or timeout). Returns $true if freed.
    $deadline = (Get-Date).AddSeconds($timeoutSec)
    while ((Get-Date) -lt $deadline) {
        if (-not (Get-PortOwnerPid $port)) { return $true }
        Start-Sleep -Milliseconds 300
    }
    return (-not (Get-PortOwnerPid $port))
}

# ── bot ──────────────────────────────────────────────────────────────────────

function Stop-Bot {
    $procId = Read-Pid 'bot'
    if ($procId) { Write-Host "Stopping bot (pidfile PID $procId)..."; Stop-Tree $procId }
    # sweep any stragglers (duplicates / orphans) so none keep emailing
    foreach ($p in Get-BotProcesses) {
        Write-Host "Stopping straggler bot PID $($p.ProcessId)..."
        Stop-Tree $p.ProcessId
    }
    foreach ($f in 'bot.pid','bot.meta.json','bot.heartbeat') {
        Remove-Item (Join-Path $RunDir $f) -ErrorAction SilentlyContinue
    }
    Write-Host "Bot stopped." -ForegroundColor Green
}

function Start-Bot {
    $h = Get-BotHealth
    if ($h.Healthy) {
        Write-Host "Bot already running & healthy (PID $($h.Pid), last loop $($h.AgeSec)s ago). Not starting a duplicate." -ForegroundColor Green
        return
    }
    if ($h.Alive) {
        Write-Host "Bot PID $($h.Pid) is $($h.Reason) — replacing it." -ForegroundColor Yellow
        Stop-Bot
    }

    Write-Host "Starting bot: $Strategy (loop, every $Interval min)..."
    $stdout = Join-Path $LogsDir 'bot.out.log'
    $proc = Start-Process -FilePath $Python `
        -ArgumentList @('bot.py', '--strategy', $Strategy, '--loop', '--interval', "$Interval") `
        -WorkingDirectory $Root -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput $stdout -RedirectStandardError (Join-Path $LogsDir 'bot.err.log')

    # bot.py writes run/bot.pid itself (the real loop PID). Wait for it.
    $deadline = (Get-Date).AddSeconds(20)
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Milliseconds 500
        $procId = Read-Pid 'bot'
        if ($procId -and (Get-LiveProc $procId)) {
            Write-Host "Bot started — loop PID $procId (launcher $($proc.Id))." -ForegroundColor Green
            return
        }
    }
    Write-Host "Bot launched (launcher PID $($proc.Id)) but pidfile not confirmed yet — check $stdout" -ForegroundColor Yellow
}

# ── dashboard ────────────────────────────────────────────────────────────────

function Stop-Dashboard {
    $procId = Read-Pid 'dashboard'
    if ($procId) { Write-Host "Stopping dashboard (pidfile PID $procId)..."; Stop-Tree $procId }
    $owner = Get-PortOwnerPid $Port
    if ($owner) { Write-Host "Stopping process on port $Port (PID $owner)..."; Stop-Tree $owner }
    # Sweep any uvicorn (python.exe OR pythonw.exe) bound to this port that slipped
    # past the pidfile/port-owner kills.
    foreach ($p in Get-DashboardProcesses) {
        Write-Host "Stopping straggler dashboard PID $($p.ProcessId)..."
        Stop-Tree $p.ProcessId
    }
    # Don't return "stopped" until the port is actually free — otherwise a follow-up
    # Start-Dashboard probes the dying process, sees it healthy, and refuses to replace it.
    if (-not (Wait-PortFree $Port)) {
        $owner = Get-PortOwnerPid $Port
        if ($owner) {
            Write-Host "Port $Port still held by PID $owner — force-killing." -ForegroundColor Yellow
            Stop-Tree $owner
            Wait-PortFree $Port | Out-Null
        }
    }
    foreach ($f in 'dashboard.pid','dashboard.meta.json') {
        Remove-Item (Join-Path $RunDir $f) -ErrorAction SilentlyContinue
    }
    if (Get-PortOwnerPid $Port) {
        Write-Host "WARNING: port $Port is still in use after stop." -ForegroundColor Red
    } else {
        Write-Host "Dashboard stopped." -ForegroundColor Green
    }
}

function Start-Dashboard {
    if (Test-DashboardHealthy) {
        $owner = Get-PortOwnerPid $Port
        Write-Host "Dashboard already running & healthy on $DashUrl (PID $owner). Not starting a duplicate." -ForegroundColor Green
        return
    }
    $owner = Get-PortOwnerPid $Port
    if ($owner) {
        Write-Host "Port $Port held by PID $owner but not responding — replacing it." -ForegroundColor Yellow
        Stop-Dashboard
    }

    Write-Host "Starting dashboard on port $Port..."
    $stdout = Join-Path $LogsDir 'dashboard.out.log'
    $proc = Start-Process -FilePath $Python `
        -ArgumentList @('-m', 'uvicorn', 'dashboard.server:app', '--host', '0.0.0.0', '--port', "$Port") `
        -WorkingDirectory $Root -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput $stdout -RedirectStandardError (Join-Path $LogsDir 'dashboard.err.log')

    Set-Content -Path (Join-Path $RunDir 'dashboard.pid') -Value $proc.Id -Encoding utf8
    @{ pid = $proc.Id; port = $Port; started_at = (Get-Date).ToUniversalTime().ToString('o') } |
        ConvertTo-Json | Set-Content -Path (Join-Path $RunDir 'dashboard.meta.json') -Encoding utf8

    $deadline = (Get-Date).AddSeconds(25)
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Milliseconds 700
        if (Test-DashboardHealthy) {
            $owner = Get-PortOwnerPid $Port
            Write-Host "Dashboard up at $DashUrl (PID $owner)." -ForegroundColor Green
            return
        }
    }
    Write-Host "Dashboard launched (PID $($proc.Id)) but $DashUrl not responding yet — check $stdout" -ForegroundColor Yellow
}

# ── status ───────────────────────────────────────────────────────────────────

function Show-Status {
    Write-Host "=== Alpaca Swing Bot V2 — status ===" -ForegroundColor Cyan
    $h = Get-BotHealth
    if ($h.Healthy) {
        Write-Host ("BOT       : HEALTHY   PID {0}  last loop {1}s ago" -f $h.Pid, $h.AgeSec) -ForegroundColor Green
    } elseif ($h.Alive) {
        Write-Host ("BOT       : UNHEALTHY PID {0}  ({1})" -f $h.Pid, $h.Reason) -ForegroundColor Yellow
    } else {
        Write-Host  "BOT       : STOPPED   (not running)" -ForegroundColor DarkGray
    }

    if (Test-DashboardHealthy) {
        Write-Host ("DASHBOARD : HEALTHY   PID {0}  {1}" -f (Get-PortOwnerPid $Port), $DashUrl) -ForegroundColor Green
    } else {
        $owner = Get-PortOwnerPid $Port
        if ($owner) { Write-Host ("DASHBOARD : UNHEALTHY PID {0} on port {1} (no HTTP response)" -f $owner, $Port) -ForegroundColor Yellow }
        else        { Write-Host  "DASHBOARD : STOPPED   (nothing on port $Port)" -ForegroundColor DarkGray }
    }
    Write-Host "PID files : $RunDir"
}

# ── dispatch ─────────────────────────────────────────────────────────────────

switch ($Command) {
    'status'             { Show-Status }
    'start-bot'          { Start-Bot }
    'stop-bot'           { Stop-Bot }
    'restart-bot'        { Stop-Bot; Start-Bot }
    'start-dashboard'    { Start-Dashboard }
    'stop-dashboard'     { Stop-Dashboard }
    'restart-dashboard'  { Stop-Dashboard; Start-Dashboard }
}
