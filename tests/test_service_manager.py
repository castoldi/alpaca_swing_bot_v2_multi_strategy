"""Manager behavior using a simulated process table, never the running bot."""
import importlib
import json
from pathlib import Path
import subprocess
import sys
import time

import psutil
import pytest

import runtime
from tests.test_process_control import Process


@pytest.fixture
def manager(tmp_path):
    module = importlib.import_module("service_manager")
    return module.Manager(tmp_path)


def test_foreign_dashboard_port_blocks_start_and_stop(manager, monkeypatch):
    monkeypatch.setattr(manager, "processes", lambda service: [])
    monkeypatch.setattr(manager, "port_owners", lambda: {999})
    monkeypatch.setattr(manager, "http_healthy", lambda: True)
    launches = []
    monkeypatch.setattr(manager, "launch", lambda *a: launches.append(a))
    for command in ("start-dashboard", "stop-dashboard", "restart-dashboard"):
        with pytest.raises(RuntimeError, match="unverified|foreign"):
            manager.run(command)
    assert launches == []


def test_foreign_dashboard_owner_is_visible_but_not_adopted(manager, monkeypatch):
    monkeypatch.setattr(manager, "processes", lambda service: [])
    monkeypatch.setattr(manager, "port_owners", lambda: {999})
    state = manager.status("dashboard")
    assert state["running"] and not state["healthy"] and state["foreign"]
    assert state["pids"] == [999]


def test_stale_pid_file_never_authorizes_stopping_foreign_process(manager, monkeypatch):
    manager.run_dir.mkdir()
    (manager.run_dir / "bot.pid").write_text("999")
    monkeypatch.setattr(manager, "processes", lambda service: [])
    foreign = Process(manager.root / "foreign", pid=999)
    monkeypatch.setattr(psutil, "Process", lambda pid: foreign)
    manager.run("stop-bot")
    assert not foreign.killed


def test_creation_time_rechecked_immediately_before_termination(manager, monkeypatch):
    proc = Process(manager.root, created=100)
    snapshot = runtime.process_identity(proc, "bot", manager.root)
    proc.created = 200
    monkeypatch.setattr(psutil, "Process", lambda pid: proc)
    with pytest.raises(RuntimeError, match="identity"):
        manager.stop_identity(snapshot)
    assert not proc.killed


def test_stop_does_not_terminate_unrelated_children(manager, monkeypatch):
    proc = Process(manager.root)
    snapshot = runtime.process_identity(proc, "bot", manager.root)
    monkeypatch.setattr(psutil, "Process", lambda pid: proc)
    manager.stop_identity(snapshot)
    assert proc.killed


def test_healthy_bot_is_adopted_without_launch_or_settings_override(manager, monkeypatch):
    monkeypatch.setattr(manager, "status", lambda service: dict(healthy=True, pid=123, pids=[123]))
    calls = []
    monkeypatch.setattr(manager, "launch", lambda *a: calls.append(a))
    result = manager.run("start-bot", strategy="regime", interval=60)
    assert result["pid"] == 123 and calls == []


def test_recovery_preserves_saved_strategy_and_interval(manager, monkeypatch):
    manager.run_dir.mkdir()
    (manager.run_dir / "bot.meta.json").write_text(json.dumps({"strategy":"regime", "interval":60}))
    launched = []
    monkeypatch.setattr(manager, "processes", lambda service: [])
    monkeypatch.setattr(manager, "status", lambda service: dict(healthy=bool(launched), pid=123))
    monkeypatch.setattr(manager, "launch", lambda service, strategy, interval: launched.append((strategy, interval)))
    manager.run("start-bot")
    assert launched == [("regime", 60)]


def test_latest_runtime_settings_take_precedence_over_old_saved_defaults(manager):
    manager.run_dir.mkdir()
    (manager.run_dir / "bot.settings.json").write_text('{"strategy":"ensemble","interval":30}')
    (manager.run_dir / "bot.meta.json").write_text('{"strategy":"regime","interval":60}')
    assert manager.settings() == ("regime", 60)


def test_verified_orphan_command_settings_override_stale_saved_defaults(manager, monkeypatch):
    proc = Process(manager.root)
    proc.command[3] = "regime"
    proc.command[-1] = "60"
    identity = runtime.process_identity(proc, "bot", manager.root)
    monkeypatch.setattr(manager, "processes", lambda service: [identity])
    manager.run_dir.mkdir()
    (manager.run_dir / "bot.settings.json").write_text('{"strategy":"ensemble","interval":30}')
    assert manager.settings() == ("regime", 60)


def test_verified_orphan_uses_parser_default_interval_over_stale_settings(manager, monkeypatch):
    proc = Process(manager.root)
    proc.command = proc.command[:-2]  # bot.py --strategy ensemble --loop
    identity = runtime.process_identity(proc, "bot", manager.root)
    monkeypatch.setattr(manager, "processes", lambda service: [identity])
    manager.run_dir.mkdir()
    (manager.run_dir / "bot.settings.json").write_text('{"strategy":"ensemble","interval":60}')
    assert manager.settings() == ("ensemble", 30)


def test_dashboard_readiness_rechecks_identity_after_http(manager, monkeypatch):
    proc = Process(manager.root)
    proc.command = [proc.executable, str(manager.root / "scripts/run_dashboard.py"), "--port", "8004"]
    snapshot = runtime.process_identity(proc, "dashboard", manager.root)
    monkeypatch.setattr(manager, "processes", lambda service: [snapshot])
    monkeypatch.setattr(manager, "port_owners", lambda: {123})
    monkeypatch.setattr(psutil, "Process", lambda pid: proc)
    def response():
        proc.created += 1
        return True
    monkeypatch.setattr(manager, "http_healthy", response)
    assert not manager.status("dashboard")["healthy"]


def test_bot_readiness_rechecks_identity_after_heartbeat(manager, monkeypatch):
    proc = Process(manager.root, created=time.time() - 10)
    snapshot = runtime.process_identity(proc, "bot", manager.root)
    monkeypatch.setattr(manager, "processes", lambda service: [snapshot])
    manager.run_dir.mkdir()
    (manager.run_dir / "bot.heartbeat").write_text(runtime._now())
    calls = 0
    def refreshed(*args):
        nonlocal calls
        calls += 1
        result = dict(snapshot)
        if calls:
            result["process_created"] += 1
        return result
    monkeypatch.setattr(runtime, "process_identity", refreshed)
    monkeypatch.setattr(psutil, "Process", lambda pid: proc)
    assert not manager.status("bot")["healthy"]


def test_stop_cannot_remove_state_during_a_new_runtime_claim(manager, monkeypatch):
    manager.run_dir.mkdir()
    state = manager.run_dir / "bot.pid"
    state.write_text("123")
    monkeypatch.setattr(manager, "processes", lambda service: [])
    with runtime.ServiceLock(manager.run_dir / "bot.instance.lock"):
        with pytest.raises(runtime.AlreadyRunning):
            manager.run("stop-bot")
    assert state.read_text() == "123"


def test_inventory_stores_multiple_pids_under_distinct_services(manager, monkeypatch):
    (manager.root / "backtest_2025.py").touch()
    (manager.root / "backtest_2026.py").touch()
    expected = {"bot":[101,102], "dashboard":[201,202], "backtest_2025":[301], "backtest_2026":[401]}
    monkeypatch.setattr(manager, "processes", lambda service: [{"pid":pid} for pid in expected[service]])
    manager.inventory()
    records = json.loads((manager.run_dir / "processes.json").read_text())["services"]
    assert {service:[p["pid"] for p in records[service]] for service in records} == expected


def test_parallel_starts_make_exactly_one_launch(tmp_path):
    # The real manager owns serialization; only OS process launch/health is faked.
    script = """
import sys,time
from pathlib import Path
from service_manager import Manager
m=Manager(Path(sys.argv[1]))
marker=m.root/'launched'
m.processes=lambda service: []
m.status=lambda service: dict(healthy=marker.exists(),pid=123)
def launch(*args):
    time.sleep(.2)
    with marker.open('a') as f: f.write('spawn\\n')
m.launch=launch
m.run('start-bot')
"""
    children = [subprocess.Popen([sys.executable, "-c", script, str(tmp_path)]) for _ in range(2)]
    for child in children:
        assert child.wait(timeout=15) == 0
    assert (tmp_path / "launched").read_text().splitlines() == ["spawn"]


def test_watchdog_does_not_override_recovery_settings(monkeypatch, tmp_path):
    import keep_alive
    monkeypatch.setattr(keep_alive, "PROJECT_DIR", tmp_path)
    calls = []
    monkeypatch.setattr(keep_alive, "bot_healthy", lambda: (False, "stale"))
    monkeypatch.setattr(keep_alive, "dashboard_healthy", lambda: (True, "healthy"))
    monkeypatch.setattr(keep_alive, "_manage", lambda *a, **kw: calls.append((a, kw)))
    keep_alive.main()
    assert calls == [(("start-bot",), {})]
