"""Persistence of multiple-testing evidence on research experiments."""
import sqlite3

import numpy as np
import pytest

from dashboard import db as db_mod
from research.significance import evaluate


@pytest.fixture
def db(tmp_path, monkeypatch):
    """Point the db module at a throwaway file, freshly migrated."""
    monkeypatch.setattr(db_mod, "_DB", tmp_path / "test.db")
    db_mod.init_db()
    return db_mod


def _report():
    rng = np.random.default_rng(2)
    return evaluate(rng.normal(0.01, 0.05, size=120), trials=40)


def test_evidence_round_trips(db):
    report = _report()
    db.log_experiment(
        "widen the ensemble vote threshold", "ensemble_threshold 0.30 -> 0.28",
        "ensemble", 120.0, 80.0, verdict="rejected", evidence=report.as_dict(),
    )

    row = db.get_experiments(1)[0]
    assert row["trials"] == 40
    assert row["n_trades"] == 120
    assert row["t_stat"] == pytest.approx(report.t_stat)
    assert row["hurdle_t"] == pytest.approx(report.hurdle_t)
    assert row["haircut_sharpe"] == pytest.approx(report.haircut_sharpe)
    assert row["significant"] == int(report.significant)
    assert row["evidence_method"] == report.method
    assert row["combined_pnl"] == pytest.approx(200.0)


def test_legacy_call_still_works_and_leaves_evidence_null(db):
    """Pre-existing callers must not break; they just record no evidence."""
    db.log_experiment("older experiment", "some change", "regime", 10.0, 20.0)

    row = db.get_experiments(1)[0]
    assert row["verdict"] == "pending"
    assert row["trials"] is None
    assert row["t_stat"] is None
    assert row["significant"] is None


def test_kept_without_evidence_is_warned_about(db, caplog):
    """The discipline: you may record it, but it does not pass silently."""
    with caplog.at_level("WARNING"):
        db.log_experiment("kept on vibes", "tweak", "breakout", 5.0, 5.0,
                          verdict="KEPT")

    assert any("no significance evidence" in r.message for r in caplog.records)


def test_kept_with_evidence_is_not_warned_about(db, caplog):
    with caplog.at_level("WARNING"):
        db.log_experiment("kept on evidence", "tweak", "breakout", 5.0, 5.0,
                          verdict="kept", evidence=_report().as_dict())

    assert not any("no significance evidence" in r.message for r in caplog.records)


def test_migration_adds_columns_to_a_legacy_table(tmp_path, monkeypatch):
    """A DB created before this change must gain the columns, keeping its rows."""
    path = tmp_path / "legacy.db"
    con = sqlite3.connect(str(path))
    con.executescript("""
        CREATE TABLE research_experiments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            description TEXT NOT NULL,
            changes_made TEXT,
            strategy_tested TEXT,
            result_2025_pnl REAL,
            result_2026_pnl REAL,
            combined_pnl REAL,
            verdict TEXT DEFAULT 'pending'
        );
        INSERT INTO research_experiments
            (timestamp, description, verdict)
            VALUES ('2026-01-01T00:00:00+00:00', 'from before the change', 'kept');
    """)
    con.commit()
    con.close()

    monkeypatch.setattr(db_mod, "_DB", path)
    db_mod.init_db()

    rows = db_mod.get_experiments(10)
    assert len(rows) == 1
    assert rows[0]["description"] == "from before the change"
    # Never captured, and not reconstructable after the fact — NULL is honest.
    assert rows[0]["trials"] is None

    db_mod.log_experiment("after migration", "c", "regime", 1.0, 2.0,
                          evidence=_report().as_dict())
    assert db_mod.get_experiments(1)[0]["trials"] == 40
