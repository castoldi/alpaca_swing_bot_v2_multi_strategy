"""Process ownership and lifecycle tests; no production services are started."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import psutil
import pytest

import runtime


class Process:
    def __init__(self, root, pid=123, created=100, service="bot", cwd=None, cmd=None):
        self.pid, self.created = pid, created
        self.root = Path(root)
        self.directory = Path(cwd) if cwd else self.root
        self.command = cmd or [str(self.root / ".venv/Scripts/python.exe"),
                               "bot.py", "--strategy", "ensemble", "--loop", "--interval", "30"]
        self.executable = self.command[0]
        self.alive, self.killed = True, False

    def exe(self): return self.executable
    def cmdline(self): return self.command
    def cwd(self): return str(self.directory)
    def create_time(self): return self.created
    def is_running(self): return self.alive
    def status(self): return "running"
    def ppid(self): return 0
    def name(self): return Path(self.executable).name
    def terminate(self): self.killed, self.alive = True, False
    def wait(self, timeout=None): return 0


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, "ROOT", tmp_path)
    monkeypatch.setattr(runtime, "RUN_DIR", tmp_path / "run")
    return tmp_path


def test_unregister_without_ownership_cannot_delete_another_instances_files(state):
    runtime.run_dir()
    runtime.pid_file("bot").write_text("888")
    runtime.meta_file("bot").write_text('{"pid":888,"owner_token":"new-instance"}')
    runtime.heartbeat_file("bot").write_text("new heartbeat")
    runtime.unregister("bot")
    assert runtime.pid_file("bot").read_text() == "888"
    assert runtime.heartbeat_file("bot").read_text() == "new heartbeat"


def test_foreign_bot_same_filename_is_not_owned(state):
    foreign = Process(state, cwd=state / "another-project")
    assert runtime.process_identity(foreign, "bot", state) is None


def test_owned_relative_and_absolute_bot_commands_are_recognized(state):
    proc = Process(state)
    assert runtime.process_identity(proc, "bot", state)["pid"] == 123
    proc.command[1] = str(state / "bot.py")
    proc.directory = state / "elsewhere"
    assert runtime.process_identity(proc, "bot", state)["process_created"] == 100


@pytest.mark.parametrize("command", [
    ["-c", "print('bot.py --strategy ensemble --loop')"],
    ["other.py", "bot.py", "--strategy", "ensemble", "--loop"],
    ["bot.py.backup", "--strategy", "ensemble", "--loop"],
])
def test_matching_words_in_arguments_do_not_prove_script_identity(state, command):
    proc = Process(state)
    proc.command = [proc.executable, *command]
    assert runtime.process_identity(proc, "bot", state) is None


def test_reused_pid_does_not_count_as_healthy(state, monkeypatch):
    proc = Process(state, created=time.time())
    monkeypatch.setattr(psutil, "Process", lambda pid: proc)
    runtime.run_dir()
    runtime.pid_file("bot").write_text("123")
    runtime.meta_file("bot").write_text(json.dumps({
        "pid":123, "process_created":proc.created - 300, "interval":30,
        "project_root":str(state),
    }))
    runtime.heartbeat_file("bot").write_text(runtime._now())
    assert runtime.read_status("bot")["healthy"] is False


@pytest.mark.parametrize("unreadable", [False, True])
def test_legacy_dashboard_requires_verified_module_directory(state, unreadable):
    proc = Process(state)
    proc.command = [proc.executable, "-m", "uvicorn", "dashboard.server:app",
                    "--app-dir", str(state / "foreign"), "--port", "8004"]
    if unreadable:
        proc.command = [proc.executable, "-m", "uvicorn", "dashboard.server:app", "--port", "8004"]
        def denied(): raise psutil.AccessDenied(proc.pid)
        proc.cwd = denied
    assert runtime.process_identity(proc, "dashboard", state) is None


def test_second_runtime_claim_is_rejected_without_overwriting_state(state, monkeypatch):
    proc = Process(state, pid=os.getpid(), created=time.time())
    monkeypatch.setattr(runtime, "service_processes", lambda *a, **k: [])
    monkeypatch.setattr(psutil, "Process", lambda pid: proc)
    runtime.register("bot", {"strategy":"ensemble", "interval":30})
    before = runtime.meta_file("bot").read_text()
    try:
        with pytest.raises(runtime.AlreadyRunning):
            runtime.register("bot", {"strategy":"regime", "interval":60})
        assert runtime.meta_file("bot").read_text() == before
    finally:
        runtime.unregister("bot")


def test_runtime_does_not_erase_new_owners_metadata(state, monkeypatch):
    proc = Process(state, pid=os.getpid(), created=time.time())
    monkeypatch.setattr(runtime, "service_processes", lambda *a, **k: [])
    monkeypatch.setattr(psutil, "Process", lambda pid: proc)
    runtime.register("bot", {"interval":30})
    runtime.meta_file("bot").write_text('{"pid":999,"owner_token":"replacement"}')
    runtime.pid_file("bot").write_text("999")
    runtime.heartbeat_file("bot").write_text("replacement heartbeat")
    runtime.heartbeat("bot")
    runtime.unregister("bot")
    assert runtime.pid_file("bot").read_text() == "999"
    assert runtime.heartbeat_file("bot").read_text() == "replacement heartbeat"


def test_process_lock_serializes_independent_processes_and_releases_after_crash(tmp_path):
    script = """
import sys,time
from pathlib import Path
from runtime import ServiceLock
with ServiceLock(Path(sys.argv[1]), timeout=3):
    with open(sys.argv[2], 'a') as out:
        out.write(sys.argv[3]+' start\\n');out.flush()
        time.sleep(.15)
        out.write(sys.argv[3]+' end\\n')
"""
    path, events = tmp_path / "guard.lock", tmp_path / "events"
    children = [subprocess.Popen([sys.executable, "-c", script, str(path), str(events), str(i)]) for i in range(2)]
    for child in children:
        assert child.wait(timeout=10) == 0
    lines = events.read_text().splitlines()
    assert lines in [["0 start", "0 end", "1 start", "1 end"], ["1 start", "1 end", "0 start", "0 end"]]
    crashing = "from runtime import ServiceLock;from pathlib import Path;import os,sys; guard=ServiceLock(Path(sys.argv[1]));guard.acquire();os._exit(0)"
    subprocess.run([sys.executable, "-c", crashing, str(path)], check=True)
    with runtime.ServiceLock(path, timeout=0):
        pass


def test_bot_dashboard_and_backtest_have_separate_runtime_records(state, monkeypatch):
    proc = Process(state, pid=os.getpid(), created=time.time())
    monkeypatch.setattr(runtime, "service_processes", lambda *a, **k: [])
    monkeypatch.setattr(psutil, "Process", lambda pid: proc)
    services = ["bot", "dashboard", "backtest_2025", "backtest_2026"]
    try:
        for service in services:
            runtime.register(service, {"strategy":"ensemble"})
        runtime.unregister("bot")
        for service in services[1:]:
            assert json.loads(runtime.meta_file(service).read_text())["service"] == service
            assert runtime.pid_file(service).exists()
    finally:
        for service in services:
            runtime.unregister(service)


def test_real_second_bot_process_cannot_claim_existing_runtime(tmp_path):
    project = str(Path(runtime.__file__).resolve().parent)
    script = tmp_path / "bot.py"
    script.write_text(f"""
import sys,time
from pathlib import Path
sys.path.insert(0, {project!r})
import runtime
runtime.ROOT=Path(__file__).parent
runtime.RUN_DIR=runtime.ROOT/'run'
try:
    with runtime.tracked('bot', {{'strategy':'ensemble','interval':30}}):
        (runtime.ROOT/'ready').write_text(str(__import__('os').getpid()))
        time.sleep(2)
except runtime.AlreadyRunning:
    sys.exit(7)
""")
    args = [sys.executable, str(script), "--strategy", "ensemble", "--loop"]
    first = subprocess.Popen(args)
    try:
        deadline = time.monotonic() + 5
        while not (tmp_path / "ready").exists() and time.monotonic() < deadline:
            time.sleep(.02)
        assert (tmp_path / "ready").exists()
        recorded = (tmp_path / "run/bot.meta.json").read_text()
        second = subprocess.run(args, timeout=5)
        assert second.returncode == 7
        assert (tmp_path / "run/bot.meta.json").read_text() == recorded
        assert first.wait(timeout=5) == 0
        assert not (tmp_path / "run/bot.pid").exists()
    finally:
        if first.poll() is None:
            first.terminate()
            first.wait(timeout=5)
