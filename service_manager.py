"""Implementation behind scripts/manage.ps1; no shell-based process matching."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import time

import psutil
import requests

import runtime


class Manager:
    def __init__(self, root: Path = runtime.ROOT, port: int = 8004):
        self.root = Path(root).resolve()
        self.run_dir = self.root / "run"
        self.port = port

    def processes(self, service):
        return runtime.service_processes(service, self.root, self.port)

    def port_owners(self):
        return {c.pid for c in psutil.net_connections(kind="tcp")
                if c.status == psutil.CONN_LISTEN and c.laddr.port == self.port and c.pid}

    def http_healthy(self):
        try:
            return 200 <= requests.get(f"http://localhost:{self.port}/", timeout=4).status_code < 400
        except requests.RequestException:
            return False

    def status(self, service):
        processes = self.processes(service)
        ids = {p["pid"] for p in processes}
        parents = {p["parent_pid"] for p in processes}
        # A venv launcher and its interpreter child form ONE running instance.
        workers = [p for p in processes if p["pid"] not in parents]
        result = dict(healthy=False, running=bool(processes), pid=None,
                      pids=sorted(ids), instances=len(workers), reason="not running")
        if service == "dashboard":
            owners = self.port_owners()
            if owners - ids:
                return dict(result, running=True, pid=next(iter(owners)), pids=sorted(owners),
                            reason="foreign or unverified port owner", foreign=True)
            if len(owners) == 1 and len(workers) == 1:
                healthy = self.http_healthy()
                identity = next((p for p in processes if p["pid"] in owners), None)
                try:
                    fresh = runtime.process_identity(psutil.Process(identity["pid"]), service, self.root, self.port)
                    verified = (fresh is not None and fresh["process_created"] == identity["process_created"]
                                and self.port_owners() == owners)
                except (psutil.Error, TypeError):
                    verified = False
                if not verified:
                    return dict(result, reason="listener identity changed during readiness", foreign=True)
                return dict(result, pid=next(iter(owners)), healthy=healthy,
                            reason="responding" if healthy else "HTTP health failed")
            return dict(result, reason="no unique verified dashboard listener")
        if len(workers) != 1:
            return dict(result, reason="multiple instances" if workers else "not running")
        worker = workers[0]
        result["pid"] = worker["pid"]
        meta = runtime.read_json(self.run_dir / f"{service}.meta.json")
        if service.startswith("backtest_"):
            return dict(result, healthy=True, reason="running finite job")
        # Identity was established independently of PID files. A verified orphan
        # with its own fresh heartbeat can be adopted without rewriting its token.
        try:
            interval = int(runtime._option(worker["command"], "--interval", 30))
            hb = datetime.fromisoformat((self.run_dir / f"{service}.heartbeat").read_text().strip())
            if hb.tzinfo is None:
                hb = hb.replace(tzinfo=timezone.utc)
            age = (datetime.now(timezone.utc) - hb).total_seconds()
            identity_ok = not meta or runtime.metadata_matches(meta, worker)
            try:
                fresh = runtime.process_identity(psutil.Process(worker["pid"]), service, self.root, self.port)
                identity_ok = (identity_ok and fresh is not None
                               and fresh["process_created"] == worker["process_created"])
            except psutil.Error:
                identity_ok = False
            healthy = identity_ok and hb.timestamp() >= worker["process_created"] and 0 <= age <= interval * 120 + 300
            return dict(result, healthy=healthy, reason="looping" if healthy else "stale heartbeat or identity",
                        strategy=runtime._option(worker["command"], "--strategy"), interval=interval)
        except (OSError, ValueError, TypeError):
            return dict(result, reason="missing or invalid heartbeat")

    def stop_identity(self, identity):
        try:
            proc = psutil.Process(identity["pid"])
            actual = runtime.process_identity(proc, identity["service"], self.root, self.port)
            if actual is None or actual["process_created"] != identity["process_created"]:
                # Stopping the verified interpreter can make its launcher exit
                # between the inventory snapshot and its own stop. Treat only
                # a promptly exited launcher as gone; a live changed PID stays
                # fail-closed in case Windows reused it for another process.
                try:
                    proc.wait(timeout=1)
                    return
                except psutil.TimeoutExpired:
                    pass
                raise RuntimeError(f"Process identity changed for PID {identity['pid']}; stop refused")
            # psutil checks PID reuse; never kill an unverified descendant tree.
            proc.terminate()
            proc.wait(timeout=8)
        except psutil.NoSuchProcess:
            return

    def settings(self, strategy=None, interval=None):
        saved = runtime.read_json(self.run_dir / "bot.settings.json")
        meta = runtime.read_json(self.run_dir / "bot.meta.json")
        processes = self.processes("bot")
        parents = {p["parent_pid"] for p in processes}
        workers = [p for p in processes if p["pid"] not in parents]
        command = workers[0]["command"] if len(workers) == 1 else []
        strategy = strategy or runtime._option(command, "--strategy") or meta.get("strategy") or saved.get("strategy") or "ensemble"
        if interval is None:
            # argparse defines 30 minutes when a verified live command omits it.
            interval = runtime._option(command, "--interval", 30) if command else meta.get("interval", saved.get("interval", 30))
        if not re.fullmatch(r"[a-z][a-z0-9_]*", strategy) or int(interval) <= 0:
            raise ValueError("Invalid saved strategy or interval")
        return strategy, int(interval)

    def stop(self, service):
        processes = self.processes(service)
        if service == "dashboard" and self.port_owners() - {p["pid"] for p in processes}:
            raise RuntimeError("Dashboard port belongs to a foreign or unverified process; stop refused")
        if service == "bot":
            strategy, interval = self.settings()
            runtime.atomic_write(self.run_dir / "bot.settings.json", json.dumps(dict(strategy=strategy, interval=interval)))
        # Newer child first, then its launcher; both must independently match.
        for identity in sorted(processes, key=lambda p: p["process_created"], reverse=True):
            self.stop_identity(identity)
        # A direct invocation can claim its lifetime lock while this manager is
        # stopping an older instance. Never erase that new owner's files.
        with runtime.ServiceLock(self.run_dir / f"{service}.instance.lock", timeout=1):
            if self.processes(service):
                raise RuntimeError(f"Verified {service} processes remain; replacement blocked")
            if service == "dashboard" and self.port_owners():
                raise RuntimeError("Dashboard port is still occupied; replacement blocked")
            for suffix in ("pid", "meta.json", "heartbeat"):
                (self.run_dir / f"{service}.{suffix}").unlink(missing_ok=True)

    def launch(self, service, strategy, interval):
        interpreter = self.root / ".venv/Scripts/pythonw.exe"
        if not interpreter.exists():
            interpreter = self.root / ".venv/Scripts/python.exe"
        if not interpreter.exists():
            raise RuntimeError("Project interpreter missing; refusing an unrelated Python fallback")
        if service == "bot":
            args = [str(interpreter), str(self.root / "bot.py"), "--strategy", strategy,
                    "--loop", "--interval", str(interval)]
        else:
            args = [str(interpreter), str(self.root / "scripts/run_dashboard.py"), "--port", str(self.port)]
        logs = self.root / "logs"
        logs.mkdir(exist_ok=True)
        with (logs / f"{service}.out.log").open("a", encoding="utf-8") as out, (logs / f"{service}.err.log").open("a", encoding="utf-8") as err:
            return subprocess.Popen(args, cwd=self.root, stdout=out, stderr=err,
                                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)

    def start(self, service, strategy=None, interval=None):
        status = self.status(service)
        if status.get("foreign"):
            raise RuntimeError("Dashboard port belongs to a foreign or unverified process; start refused")
        if status["healthy"]:
            return status
        strategy, interval = self.settings(strategy, interval)
        self.stop(service)
        if service == "bot":
            runtime.atomic_write(self.run_dir / "bot.settings.json", json.dumps(dict(strategy=strategy, interval=interval)))
        self.launch(service, strategy, interval)
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            status = self.status(service)
            if status["healthy"]:
                return status
            if status.get("foreign"):
                break
            time.sleep(.4)
        raise RuntimeError(f"{service} failed verified readiness; inspect logs before retrying")

    def inventory(self):
        services = ["bot", "dashboard", *sorted(p.stem for p in self.root.glob("backtest_[0-9][0-9][0-9][0-9].py"))]
        records = {service: self.processes(service) for service in services}
        runtime.atomic_write(self.run_dir / "processes.json", json.dumps(dict(checked_at=runtime._now(), services=records), indent=2))
        return records

    def run(self, command, strategy=None, interval=None):
        if command == "status":
            self.inventory()
            return {service: self.status(service) for service in ["bot", "dashboard"]}
        operation, service = command.split("-", 1)
        if operation not in {"start", "stop", "restart"} or service not in {"bot", "dashboard"}:
            raise ValueError("Unsupported manager command")
        with runtime.ServiceLock(self.run_dir / f"{service}.manager.lock", timeout=45):
            if operation == "stop":
                self.stop(service)
                return self.status(service)
            if operation == "restart":
                strategy, interval = self.settings(strategy, interval)
                self.stop(service)
            return self.start(service, strategy, interval)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command")
    parser.add_argument("--strategy")
    parser.add_argument("--interval", type=int)
    parser.add_argument("--port", type=int, default=8004)
    args = parser.parse_args()
    manager = Manager(port=args.port)
    try:
        result = manager.run(args.command, args.strategy, args.interval)
        if args.command != "status":
            manager.inventory()
            print(json.dumps(result))
        else:
            for service, status in result.items():
                verdict = "HEALTHY" if status["healthy"] else "UNHEALTHY" if status["running"] else "STOPPED"
                print(f"{service.upper():10}: {verdict} PID {status['pid']} all PIDs {status['pids']} ({status['reason']})")
            records = runtime.read_json(manager.run_dir / "processes.json").get("services", {})
            for service, processes in records.items():
                if service.startswith("backtest_") and processes:
                    print(f"{service}: PIDs {[p['pid'] for p in processes]}")
            print(f"Process registry: {manager.run_dir / 'processes.json'}")
        return 0
    except (RuntimeError, OSError, psutil.Error, ValueError) as exc:
        print(f"Manager refused operation: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
