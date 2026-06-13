"""Runtime PID / heartbeat tracking for the bot (and any long-running process).

Writes small state files into ./run/ so external tooling (scripts/manage.ps1)
can tell whether a process is alive AND actively looping ("healthy"), instead of
blindly spawning a duplicate.

Files written for a service named "bot":
    run/bot.pid          single line: the OS process id
    run/bot.meta.json    {"pid", "strategy", "interval", "started_at", "cmd"}
    run/bot.heartbeat    single line: ISO timestamp, refreshed every loop pass

"Healthy" = pid is alive AND heartbeat is fresh (updated within ~2 intervals).
A pidfile whose process is dead, or whose heartbeat is stale, is considered
"dead/hung" and may be safely replaced.
"""
from __future__ import annotations

import atexit
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).parent
RUN_DIR = ROOT / "run"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def run_dir() -> Path:
    RUN_DIR.mkdir(exist_ok=True)
    return RUN_DIR


def pid_file(service: str) -> Path:
    return run_dir() / f"{service}.pid"


def meta_file(service: str) -> Path:
    return run_dir() / f"{service}.meta.json"


def heartbeat_file(service: str) -> Path:
    return run_dir() / f"{service}.heartbeat"


def register(service: str, meta: Optional[dict] = None) -> None:
    """Claim ownership: write pid + meta + first heartbeat, and clean up on exit."""
    run_dir()
    pid = os.getpid()
    pid_file(service).write_text(str(pid), encoding="utf-8")
    info = {"pid": pid, "started_at": _now()}
    if meta:
        info.update(meta)
    meta_file(service).write_text(json.dumps(info, indent=2), encoding="utf-8")
    heartbeat(service)
    atexit.register(unregister, service)


def heartbeat(service: str) -> None:
    """Refresh the liveness timestamp — call once per loop iteration."""
    try:
        heartbeat_file(service).write_text(_now(), encoding="utf-8")
    except OSError:
        pass


def unregister(service: str) -> None:
    """Remove our state files on clean shutdown (best effort)."""
    for f in (pid_file(service), meta_file(service), heartbeat_file(service)):
        try:
            f.unlink(missing_ok=True)
        except OSError:
            pass
