"""Verified process identities, OS-held singleton locks, and owned runtime files."""
from __future__ import annotations

import atexit
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import sys
import time
import uuid

import psutil

ROOT = Path(__file__).resolve().parent
RUN_DIR = ROOT / "run"
_owners: dict[str, tuple[str, "ServiceLock"]] = {}


class AlreadyRunning(RuntimeError):
    pass


class ServiceLock:
    """A persistent lock file whose OS lock is released even after a crash.

    Never unlink lock files: replacing the inode permits two simultaneous locks.
    Manager operations and service lifetimes use separate locks to avoid deadlock.
    """
    def __init__(self, path: Path, timeout: float = 0):
        self.path, self.timeout, self.file = Path(path), timeout, None

    def acquire(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open("a+b")
        if self.path.stat().st_size == 0:
            self.file.write(b"0")
            self.file.flush()
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                self.file.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except OSError:
                if time.monotonic() >= deadline:
                    self.file.close()
                    self.file = None
                    raise AlreadyRunning(f"Operation already running: {self.path.name}")
                time.sleep(.05)

    def close(self):
        if self.file is not None:
            self.file.close()
            self.file = None

    def __enter__(self): return self.acquire()
    def __exit__(self, *args): self.close()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalized(path) -> str:
    return os.path.normcase(str(Path(path).resolve()))


def read_json(path: Path) -> dict:
    try:
        result = json.loads(path.read_text(encoding="utf-8-sig"))
        return result if isinstance(result, dict) else {}
    except (OSError, ValueError):
        return {}


def atomic_write(path: Path, value: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        temp.write_text(value, encoding="utf-8")
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def run_dir() -> Path:
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    return RUN_DIR


def pid_file(service: str) -> Path: return run_dir() / f"{service}.pid"
def meta_file(service: str) -> Path: return run_dir() / f"{service}.meta.json"
def heartbeat_file(service: str) -> Path: return run_dir() / f"{service}.heartbeat"


def _option(cmd, flag, default=None):
    for i, arg in enumerate(cmd):
        if arg == flag and i + 1 < len(cmd):
            return cmd[i + 1]
        if arg.startswith(flag + "="):
            return arg.split("=", 1)[1]
    return default


def process_identity(proc, service: str, root: Path | None = None, port=8004) -> dict | None:
    """Match the actual script/module, not words elsewhere in the command line.

    Relative scripts require the process cwd. Legacy uvicorn must have a known
    module directory (cwd or explicit --app-dir); a shared interpreter alone
    cannot prove which project's module was loaded.
    """
    root = Path(root or ROOT)
    try:
        if not proc.is_running() or proc.status() == psutil.STATUS_ZOMBIE:
            return None
        exe, cmd, created = proc.exe(), proc.cmdline(), proc.create_time()
        if Path(exe).name.lower() not in {"python", "python3", "python.exe", "pythonw.exe"} or len(cmd) < 2:
            return None
        venv = {normalized(root / ".venv/Scripts" / name) for name in ("python.exe", "pythonw.exe")}
        bases = {normalized(Path(getattr(sys, "_base_executable", sys.executable)).with_name(name))
                 for name in ("python.exe", "pythonw.exe", "python", "python3")}
        if normalized(exe) not in venv | bases:
            return None
        try:
            cwd = proc.cwd()
        except psutil.AccessDenied:
            cwd = None
        expected = root / ("scripts/run_dashboard.py" if service == "dashboard" else f"{service}.py")
        script = Path(cmd[1])
        actual = script if script.is_absolute() else Path(cwd) / script if cwd else None
        match = actual is not None and normalized(actual) == normalized(expected)
        if service == "bot":
            match = match and _option(cmd[2:], "--strategy") is not None and not any(
                arg in cmd[2:] for arg in ("--pnl", "--rebuild-balance-history"))
        elif service == "dashboard":
            if cmd[1:4] == ["-m", "uvicorn", "dashboard.server:app"]:
                app_dir = _option(cmd[4:], "--app-dir")
                module_dir = Path(app_dir) if app_dir else Path(cwd) if cwd else None
                if module_dir is not None and not module_dir.is_absolute():
                    module_dir = Path(cwd) / module_dir if cwd else None
                match = module_dir is not None and normalized(module_dir) == normalized(root)
            match = match and int(_option(cmd[2:], "--port", 8004)) == port
        elif not re.fullmatch(r"backtest_\d{4}", service):
            return None
        if not match:
            return None
        return dict(pid=proc.pid, process_created=created, executable=exe,
                    command=cmd, project_root=str(root.resolve()), service=service,
                    parent_pid=proc.ppid(), cwd=cwd)
    except (psutil.Error, OSError, ValueError, TypeError):
        return None


def service_processes(service: str, root: Path | None = None, port=8004) -> list[dict]:
    found = []
    for proc in psutil.process_iter(["pid", "name"]):
        if "python" not in (proc.info.get("name") or "").lower():
            continue
        identity = process_identity(proc, service, root, port)
        if identity:
            found.append(identity)
    return found


def metadata_matches(meta: dict, identity: dict) -> bool:
    if meta.get("pid") != identity["pid"]:
        return False
    if meta.get("process_created") is not None:
        return (meta["process_created"] == identity["process_created"]
                and normalized(meta.get("project_root", "")) == normalized(identity["project_root"]))
    # One-time compatibility with metadata written before creation-time tracking.
    try:
        started = datetime.fromisoformat(meta["started_at"].replace("Z", "+00:00")).timestamp()
        return 0 <= started - identity["process_created"] <= 30
    except (KeyError, TypeError, ValueError):
        return False


def register(service: str, meta: dict | None = None) -> None:
    if service in _owners:
        raise AlreadyRunning(f"{service} already registered in this process")
    lock = ServiceLock(run_dir() / f"{service}.instance.lock").acquire()
    try:
        own = psutil.Process(os.getpid())
        ancestor_ids = {p.pid for p in own.parents()} if hasattr(own, "parents") else set()
        others = [p for p in service_processes(service, ROOT, (meta or {}).get("port", 8004))
                  if p["pid"] not in ancestor_ids | {own.pid}]
        if others:
            raise AlreadyRunning(f"{service} already exists: {[p['pid'] for p in others]}")
        token = uuid.uuid4().hex
        info = dict(meta or {})
        info.update(pid=own.pid, process_created=own.create_time(), project_root=str(ROOT.resolve()),
                    executable=own.exe(), command=own.cmdline(), service=service,
                    started_at=_now(), owner_token=token)
        atomic_write(meta_file(service), json.dumps(info, indent=2))
        atomic_write(pid_file(service), str(own.pid))
        _owners[service] = (token, lock)
        heartbeat(service)
        atexit.register(unregister, service)
    except BaseException:
        lock.close()
        raise


def _owns_files(service: str) -> bool:
    owner = _owners.get(service)
    meta = read_json(meta_file(service))
    return bool(owner and meta.get("pid") == os.getpid() and meta.get("owner_token") == owner[0])


def heartbeat(service: str) -> None:
    if _owns_files(service):
        atomic_write(heartbeat_file(service), _now())


def unregister(service: str) -> None:
    owner = _owners.get(service)
    if not owner:
        return
    try:
        if _owns_files(service):
            for path in (pid_file(service), meta_file(service), heartbeat_file(service)):
                path.unlink(missing_ok=True)
    finally:
        _owners.pop(service, None)
        owner[1].close()


@contextmanager
def tracked(service: str, meta: dict | None = None):
    register(service, meta)
    try:
        yield
    finally:
        unregister(service)


def read_status(service: str, default_interval: int = 30) -> dict:
    meta = read_json(meta_file(service))
    out = dict(running=False, healthy=False, pid=None, strategy=meta.get("strategy"),
               interval=meta.get("interval", default_interval), loop=meta.get("loop"),
               started_at=meta.get("started_at"), heartbeat_at=None, age_sec=None,
               max_age_sec=None, reason="no verified process")
    try:
        pid = int(pid_file(service).read_text(encoding="utf-8-sig").strip())
        identity = process_identity(psutil.Process(pid), service, ROOT, meta.get("port", 8004))
        out["pid"] = pid
        if not identity or not metadata_matches(meta, identity):
            out["reason"] = "PID identity or creation time mismatch"
            return out
        out["running"] = True
        hb = datetime.fromisoformat(heartbeat_file(service).read_text(encoding="utf-8").strip())
        if hb.tzinfo is None:
            hb = hb.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - hb).total_seconds()
        maximum = int(out["interval"]) * 2 * 60 + 300
        healthy = math.isfinite(age) and 0 <= age <= maximum and hb.timestamp() >= identity["process_created"]
        out.update(healthy=healthy, heartbeat_at=hb.isoformat(), age_sec=int(age), max_age_sec=maximum,
                   reason="looping" if healthy else "heartbeat stale or invalid")
    except (psutil.Error, OSError, ValueError, TypeError) as exc:
        out["reason"] = str(exc)
    return out
