"""Dashboard regressions for docs/code-review-2026-09-22.md (R08, R10)."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import config
from dashboard import server


# ── R08: bracket legs belong to their owned parent ────────────────────────────

def _o(coid, *, type_, status="filled", price=None, legs=None, at="2026-09-22T13:33:00+00:00"):
    return SimpleNamespace(
        client_order_id=coid, symbol="AMZN", side=SimpleNamespace(value="sell"),
        order_type=SimpleNamespace(value=type_), qty="90", filled_qty="90" if price else "0",
        filled_avg_price=price, limit_price=None, stop_price=None,
        status=SimpleNamespace(value=status), submitted_at=at, legs=legs,
    )


def test_bot_order_rows_include_exit_legs_and_skip_foreign_orders():
    entry = _o("swingv2-entry-ensemble-AMZN-5321df17", type_="market", price="255.78",
               legs=[_o("broker-tp", type_="limit", status="canceled"),
                     _o("broker-sl", type_="stop", price="235.10",
                        at="2026-09-24T15:00:00+00:00")])
    foreign = _o("someone-else", type_="market", price="250.00")

    rows = server.bot_order_rows([entry, foreign])

    assert [r["kind"] for r in rows] == ["stop", "entry", "tp"]
    assert {r["client_order_id"] for r in rows} == {"swingv2-entry-ensemble-AMZN-5321df17"}
    assert rows[0]["filled_avg_price"] == pytest.approx(235.10)


# ── R10: LAN token ────────────────────────────────────────────────────────────

@pytest.fixture
def lan_client(monkeypatch):
    monkeypatch.setattr(config, "DASHBOARD_TOKEN", "s3cret")
    return TestClient(server.app, client=("192.168.0.50", 50000))


def test_lan_request_without_token_is_rejected(lan_client):
    assert lan_client.get("/api/strategies").status_code == 401


def test_token_link_sets_cookie_and_strips_token(lan_client):
    first = lan_client.get("/api/strategies?token=s3cret&x=1", follow_redirects=False)
    assert first.status_code == 303
    assert "token" not in first.headers["location"] and "x=1" in first.headers["location"]
    assert lan_client.cookies.get(server.AUTH_COOKIE) == "s3cret"
    assert lan_client.get("/api/strategies").status_code == 200


def test_wrong_token_is_rejected(lan_client):
    assert lan_client.get("/api/strategies?token=nope").status_code == 401


def test_local_requests_never_need_the_token(monkeypatch):
    monkeypatch.setattr(config, "DASHBOARD_TOKEN", "s3cret")
    local = TestClient(server.app, client=("127.0.0.1", 50000))
    assert local.get("/api/strategies").status_code == 200


def test_unset_token_keeps_the_dashboard_open(monkeypatch):
    monkeypatch.setattr(config, "DASHBOARD_TOKEN", "")
    lan = TestClient(server.app, client=("192.168.0.50", 50000))
    assert lan.get("/api/strategies").status_code == 200
