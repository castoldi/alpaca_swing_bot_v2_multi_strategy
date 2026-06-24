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


# ── Health readout (mirrors manage.ps1 Get-BotHealth / keep_alive.bot_healthy) ──

def _pid_alive(pid: int) -> bool:
    """True if a process with this pid currently exists."""
    if not pid:
        return False
    try:
        import psutil  # available (keep_alive.py depends on it)
        p = psutil.Process(pid)
        return p.is_running() and p.status() != psutil.STATUS_ZOMBIE
    except Exception:
        # Fallback without psutil: signal 0 probe (POSIX) / always-unknown on Win.
        try:
            os.kill(pid, 0)
            return True
        except (OSError, ProcessLookupError):
            return False
        except Exception:
            return False


def read_status(service: str, default_interval: int = 30) -> dict:
    """Inspect a service's pid/meta/heartbeat files and report liveness + health.

    Single source of truth for "is the bot looping?" — the heartbeat freshness
    formula here must stay in sync with manage.ps1 Get-BotHealth and
    keep_alive.py (max_age = interval * 2 * 60 + 300).

    Returns a JSON-safe dict:
        running, healthy (bool), pid, strategy, interval, loop, started_at,
        heartbeat_at, age_sec, max_age_sec, reason.
    """
    out = {
        "running": False, "healthy": False, "pid": None,
        "strategy": None, "interval": default_interval, "loop": None,
        "started_at": None, "heartbeat_at": None, "age_sec": None,
        "max_age_sec": None, "reason": "not running",
    }

    pid = None
    pf = pid_file(service)
    if pf.exists():
        try:
            pid = int(pf.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            pid = None
    out["pid"] = pid

    # Meta (strategy / interval / started_at) — useful even if the proc is gone.
    mf = meta_file(service)
    if mf.exists():
        try:
            meta = json.loads(mf.read_text(encoding="utf-8"))
            out["strategy"] = meta.get("strategy")
            out["interval"] = int(meta.get("interval", default_interval))
            out["loop"] = meta.get("loop")
            out["started_at"] = meta.get("started_at")
        except Exception:
            pass

    if pid is None:
        out["reason"] = "no pid file"
        return out
    if not _pid_alive(pid):
        out["reason"] = f"pid {pid} not running"
        return out
    out["running"] = True

    hbf = heartbeat_file(service)
    if not hbf.exists():
        out["reason"] = "no heartbeat file"
        return out
    try:
        hb_text = hbf.read_text(encoding="utf-8").strip()
        hb = datetime.fromisoformat(hb_text)
        if hb.tzinfo is None:
            hb = hb.replace(tzinfo=timezone.utc)
        out["heartbeat_at"] = hb.isoformat()
        age = int((datetime.now(timezone.utc) - hb).total_seconds())
        out["age_sec"] = age
        max_age = out["interval"] * 2 * 60 + 300  # same formula as manage.ps1
        out["max_age_sec"] = max_age
        if age <= max_age:
            out["healthy"] = True
            out["reason"] = "looping"
        else:
            out["reason"] = f"heartbeat stale ({age}s > {max_age}s) — hung"
    except Exception as exc:
        out["reason"] = f"heartbeat error: {exc}"
    return out
