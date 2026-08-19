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
