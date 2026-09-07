"""Windowless scheduled watchdog; all recovery delegates to the shared manager."""
from __future__ import annotations

import logging
from pathlib import Path
import subprocess
import sys

import runtime
from service_manager import Manager

PROJECT_DIR = Path(__file__).resolve().parent
LOGS_DIR = PROJECT_DIR / "logs"
LOGS_DIR.mkdir(exist_ok=True)
MANAGE_PS1 = PROJECT_DIR / "scripts/manage.ps1"
logging.basicConfig(filename=str(LOGS_DIR / "keepalive.log"), level=logging.INFO,
                    format="%(asctime)s %(levelname)-8s %(message)s", encoding="utf-8")
log = logging.getLogger("keepalive")


def _manage(command: str) -> None:
    args = ["pwsh", "-NonInteractive", "-NoProfile", "-WindowStyle", "Hidden",
            "-File", str(MANAGE_PS1), command]
    with (LOGS_DIR / "keepalive_manage.log").open("a", encoding="utf-8") as out:
        # Wait for the manager: Task Scheduler IgnoreNew must cover recovery too.
        subprocess.run(args, cwd=PROJECT_DIR, stdout=out, stderr=out, check=True,
                       timeout=120, creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)


def bot_healthy() -> tuple[bool, str]:
    state = Manager(PROJECT_DIR).status("bot")
    return state["healthy"], state["reason"]


def dashboard_healthy() -> tuple[bool, str]:
    state = Manager(PROJECT_DIR).status("dashboard")
    return state["healthy"], state["reason"]


def main() -> None:
    try:
        with runtime.ServiceLock(PROJECT_DIR / "run/watchdog.instance.lock"):
            for service, check in (("bot", bot_healthy), ("dashboard", dashboard_healthy)):
                healthy, reason = check()
                if not healthy:
                    log.info("%s unhealthy (%s); requesting manager recovery", service, reason)
                    _manage(f"start-{service}")
    except runtime.AlreadyRunning:
        log.debug("Existing watchdog is already checking/recovering services")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        log.exception("Watchdog failed")
        sys.exit(1)
