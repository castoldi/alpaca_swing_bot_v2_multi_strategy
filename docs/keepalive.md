# Keep-alive Watchdog

## What it does

`keep_alive.py` is a watchdog script that runs every 30 minutes via Windows Task Scheduler. It checks whether the bot and the dashboard are healthy and restarts whichever one is not, then exits silently. It runs under `pythonw.exe` so no console window ever appears.

**Healthy means:**
- Bot: `run/bot.pid` process is alive AND `run/bot.heartbeat` was written within `(interval * 2 * 60) + 300` seconds (same formula as `manage.ps1 Get-BotHealth`)
- Dashboard: `GET http://localhost:8004` returns a 2xx–4xx HTTP status

If both are healthy the script finishes in under one second and does nothing.

## Files

| File | Purpose |
|------|---------|
| `keep_alive.py` | Watchdog logic (health checks + delegate to manage.ps1) |
| `scripts/setup_keepalive_task.ps1` | Registers / removes the Windows Scheduled Task |
| `logs/keepalive.log` | Watchdog log — one entry per unhealthy event |
| `logs/keepalive_manage.log` | stdout/stderr from manage.ps1 calls spawned by the watchdog |

## One-time setup (Admin PowerShell)

```powershell
pwsh scripts\setup_keepalive_task.ps1
```

Verify it was registered:

```powershell
Get-ScheduledTask -TaskName AlpacaSwingBotKeepAlive | Format-List
Get-ScheduledTaskInfo -TaskName AlpacaSwingBotKeepAlive
```

Fire it manually right now to test:

```powershell
Start-ScheduledTask -TaskName AlpacaSwingBotKeepAlive
```

## Changing the interval

Re-register with a different interval (overwrites the existing task):

```powershell
pwsh scripts\setup_keepalive_task.ps1 -IntervalMinutes 15
```

The interval in `setup_keepalive_task.ps1` controls Task Scheduler frequency. The bot loop interval (`--interval 30`) is separate and set by manage.ps1 / keep_alive.py's `DEFAULT_INTERVAL_MIN`.

## Removing the task

```powershell
pwsh scripts\setup_keepalive_task.ps1 -Unregister
```

## Why pythonw and not a .bat or PowerShell script?

- `pythonw.exe` is a true windowless Python interpreter — no flash of a console window even for a fraction of a second.
- `.bat` files and PowerShell scripts always open a `cmd.exe` / `pwsh` window briefly unless wrapped in a VBScript shim.
- `pythonw.exe` is already in the venv, requires no extra tooling.

## Why not call bot.py / uvicorn directly?

`keep_alive.py` delegates all start/restart work to `manage.ps1` which contains the full idempotency logic (checks health before spawning, kills stragglers, waits for pidfile confirmation). Duplicating that logic in Python would create a maintenance burden and divergence risk.

## Logging

The watchdog only logs when something is wrong. A healthy run produces no log entries (debug level, suppressed). Look here when debugging:

```powershell
# Recent watchdog decisions
Get-Content logs\keepalive.log -Tail 50

# stdout/stderr from manage.ps1 calls
Get-Content logs\keepalive_manage.log -Tail 100
```

## Task Scheduler notes

- **LogonType: Interactive** — task runs only while the user is logged in. This is intentional: the bot needs the user's `.env` credentials and network session. If you need it to run headless (no login), change to `S4U` or a service account in `setup_keepalive_task.ps1`.
- **MultipleInstances: IgnoreNew** — if the previous watchdog run is still active (shouldn't happen in <10s, but possible if manage.ps1 is slow), new fires are skipped.
- **ExecutionTimeLimit: 10 min** — watchdog is killed if it hangs, preventing cascading restarts.

## AI guidance

- Do NOT edit `keep_alive.py` to start the bot directly with `subprocess`. Always delegate to `manage.ps1` to preserve idempotency.
- The heartbeat max-age formula in `keep_alive.py` (`interval * 2 * 60 + 300`) MUST stay in sync with `manage.ps1 Get-BotHealth`. If you change one, change the other.
- The Task Scheduler task is owned by the current Windows user. Changing the user requires re-running `setup_keepalive_task.ps1` as that user.
- `logs/keepalive.log` and `logs/keepalive_manage.log` are git-ignored (via `logs/` in `.gitignore`).
