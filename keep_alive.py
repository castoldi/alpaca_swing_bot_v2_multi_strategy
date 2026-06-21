"""Keep-alive watchdog: checks bot + dashboard health, restarts via manage.ps1.

Designed to run every 30 minutes under Windows Task Scheduler using pythonw.exe
so no console window ever appears.  All output goes to logs/keepalive.log.

Health criteria (mirrors manage.ps1):
  Bot       : run/bot.pid alive AND run/bot.heartbeat fresh within
              (interval_min * 2 * 60) + 300 seconds
  Dashboard : HTTP GET http://localhost:8004 returns 2xx-4xx

If healthy  → no action (silent exit, no new task spawned)
If unhealthy → call manage.ps1 start-bot / start-dashboard (idempotent, no dupe)
"""
from __future__ import annotations

import json
import logging
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import psutil
import requests

PROJECT_DIR = Path(__file__).parent
RUN_DIR = PROJECT_DIR / "run"
LOGS_DIR = PROJECT_DIR / "logs"
LOGS_DIR.mkdir(exist_ok=True)

BOT_PID_FILE = RUN_DIR / "bot.pid"
BOT_META_FILE = RUN_DIR / "bot.meta.json"
BOT_HEARTBEAT_FILE = RUN_DIR / "bot.heartbeat"
DASHBOARD_URL = "http://localhost:8004"
MANAGE_PS1 = PROJECT_DIR / "scripts" / "manage.ps1"

DEFAULT_INTERVAL_MIN = 30
DEFAULT_STRATEGY = "ensemble"

logging.basicConfig(
    filename=str(LOGS_DIR / "keepalive.log"),
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    encoding="utf-8",
)
log = logging.getLogger("keepalive")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _manage(command: str, **kwargs) -> None:
    """Fire a manage.ps1 command in background (non-blocking, no window)."""
    args = [
        "pwsh", "-NonInteractive", "-NoProfile", "-WindowStyle", "Hidden",
        "-File", str(MANAGE_PS1), command,
    ]
    for k, v in kwargs.items():
        args += [f"-{k}", str(v)]
    log_path = LOGS_DIR / "keepalive_manage.log"
    with open(log_path, "a", encoding="utf-8") as fh:
        fh.write(f"\n--- {datetime.now().isoformat()} manage.ps1 {command} ---\n")
        subprocess.Popen(
            args,
            cwd=str(PROJECT_DIR),
            stdout=fh,
            stderr=fh,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )


def _read_pid(path: Path) -> int | None:
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _proc_alive(pid: int) -> bool:
    try:
        p = psutil.Process(pid)
        return p.is_running() and p.status() != psutil.STATUS_ZOMBIE
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False


def _bot_interval_min() -> int:
    """Read loop interval from run/bot.meta.json, fallback to default."""
    try:
        return int(json.loads(BOT_META_FILE.read_text(encoding="utf-8")).get("interval", DEFAULT_INTERVAL_MIN))
    except Exception:
        return DEFAULT_INTERVAL_MIN


# ---------------------------------------------------------------------------
# Health checks
# ---------------------------------------------------------------------------

def bot_healthy() -> tuple[bool, str]:
    """Returns (healthy, reason)."""
    pid = _read_pid(BOT_PID_FILE)
    if pid is None:
        return False, "no pid file"
    if not _proc_alive(pid):
        return False, f"pid {pid} dead"
    if not BOT_HEARTBEAT_FILE.exists():
        return False, "no heartbeat file"
    try:
        hb_text = BOT_HEARTBEAT_FILE.read_text(encoding="utf-8").strip()
        hb = datetime.fromisoformat(hb_text)
        if hb.tzinfo is None:
            hb = hb.replace(tzinfo=timezone.utc)
        interval_min = _bot_interval_min()
        max_age_sec = interval_min * 2 * 60 + 300  # same formula as manage.ps1
        age_sec = (datetime.now(timezone.utc) - hb).total_seconds()
        if age_sec > max_age_sec:
            return False, f"heartbeat stale ({age_sec:.0f}s > {max_age_sec}s)"
        return True, f"pid {pid} alive, hb {age_sec:.0f}s ago"
    except Exception as exc:
        return False, f"heartbeat error: {exc}"


def dashboard_healthy() -> tuple[bool, str]:
    """Returns (healthy, reason)."""
    try:
        r = requests.get(DASHBOARD_URL, timeout=5)
        ok = 200 <= r.status_code < 500
        return ok, f"HTTP {r.status_code}"
    except requests.ConnectionError:
        return False, "connection refused"
    except requests.Timeout:
        return False, "timeout"
    except Exception as exc:
        return False, str(exc)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    bot_ok, bot_reason = bot_healthy()
    if bot_ok:
        log.debug("Bot healthy — %s", bot_reason)
    else:
        log.info("Bot NOT healthy (%s) — calling manage.ps1 start-bot", bot_reason)
        _manage("start-bot", Strategy=DEFAULT_STRATEGY, Interval=DEFAULT_INTERVAL_MIN)

    dash_ok, dash_reason = dashboard_healthy()
    if dash_ok:
        log.debug("Dashboard healthy — %s", dash_reason)
    else:
        log.info("Dashboard NOT healthy (%s) — calling manage.ps1 start-dashboard", dash_reason)
        _manage("start-dashboard")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        log.exception("Watchdog crashed: %s", exc)
        sys.exit(1)
