"""Make the project root importable so tests can `import strategy`, `import config`, etc."""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def isolate_database(tmp_path, monkeypatch):
    """Redirect every test's database writes to a throwaway file.

    `dashboard/swing_bot_v2.db` is the live trading database — it holds the real
    trade history the bot's P&L is computed from. Tests that drive `run_once`
    stub individual db functions one by one, so any call they miss lands in
    production: a suite run once wrote 17 bogus balance snapshots (with fake
    account equity) straight into the real equity curve.

    Rather than expect every test to remember, the path itself is redirected for
    all of them. `db._DB` is read inside `_con()` on each call, so patching the
    module attribute is enough to catch every write.
    """
    from dashboard import db as db_mod

    monkeypatch.setattr(db_mod, "_DB", tmp_path / "test_swing_bot.db")
    db_mod.init_db()


@pytest.fixture(autouse=True)
def isolate_earnings_calendar(tmp_path, monkeypatch):
    """Keep tests offline and prevent fake schedules reaching the live archive."""
    import earnings_calendar
    monkeypatch.setattr(earnings_calendar, '_STORE', earnings_calendar.CalendarStore(
        tmp_path / 'earnings.db', fetcher=lambda _: []))


@pytest.fixture(autouse=True)
def open_live_signal_window(request, monkeypatch):
    """Legacy run_once fixtures use synthetic timestamps far in the past.

    The live fill-window rule has its own tests, marked ``real_signal_window``;
    everywhere else the window is held open so those tests keep exercising the
    entry path they were written for. The one-evaluation-per-bar cursor stays
    real (and per-test isolated through the database fixture).
    """
    if request.node.get_closest_marker("real_signal_window"):
        return
    if "bot" not in sys.modules:
        try:
            import bot  # noqa: F401
        except Exception:
            return
    monkeypatch.setattr(sys.modules["bot"], "_signal_is_actionable", lambda *_a, **_k: True)


@pytest.fixture(autouse=True)
def isolate_bot_alerts(tmp_path, monkeypatch):
    """Never email or touch run/ markers from tests; tests may still override."""
    if "bot" not in sys.modules:
        try:
            import bot  # noqa: F401
        except Exception:
            return
    bot_module = sys.modules["bot"]
    monkeypatch.setattr(bot_module, "_ALERT_MARKER", tmp_path / "alerts.json")
    monkeypatch.setattr(bot_module, "_KILL_SWITCH_MARKER", tmp_path / "killswitch.date")
    monkeypatch.setattr(bot_module, "send_notification", lambda *_a, **_k: False)


@pytest.fixture(autouse=True)
def skip_protection_audit(request, monkeypatch):
    """Legacy run_once fakes do not model broker stop orders.

    The audit has dedicated tests marked ``protection_audit``.
    """
    if request.node.get_closest_marker("protection_audit") or "bot" not in sys.modules:
        return
    monkeypatch.setattr(sys.modules["bot"], "_audit_protection", lambda: [])
