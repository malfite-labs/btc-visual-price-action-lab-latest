from decimal import Decimal as D
from datetime import datetime, timezone

from backend.g_mirror import GMirror, CostModel


def q(bid="99.90", ask="100.10", at=None):
    at = at or datetime.now(timezone.utc).isoformat()
    return {"source": "BINANCE_USDM_PERP", "symbol": "BTC/USD", "currency": "USD_RESEARCH",
            "timestamp_valid": True, "age_ms": 10, "event_at": at, "received_at": at, "bid": bid, "ask": ask}


def event(fill_id, trade_id, effect, direction, qty="0.2", quote=None):
    return {"source_fill_id": fill_id, "c_trade_id": trade_id, "effect": effect,
            "direction": direction, "quantity": qty, "reason": "C_TEST", "quote": quote or q()}


def test_c_long_opens_equal_qty_short_on_executable_bid(tmp_path):
    g = GMirror(tmp_path / "g.sqlite3")
    result = g.handle_c_fill(event("c1", "ct1", "OPEN", "LONG"))
    assert result["accepted"]
    assert result["g_direction"] == "SHORT"
    assert D(result["quantity"]) == D("0.2")
    assert D(result["price"]) != D("100")
    assert g.status(q())["position"]["direction"] == "SHORT"


def test_c_short_opens_equal_qty_long_and_duplicate_is_idempotent(tmp_path):
    g = GMirror(tmp_path / "g.sqlite3")
    ev = event("c2", "ct2", "OPEN", "SHORT", quote=q("99.9", "100.1"))
    first = g.handle_c_fill(ev)
    again = g.handle_c_fill(ev)
    assert first["accepted"] and first["g_direction"] == "LONG"
    assert again["duplicate"]
    assert len(g.fills()) == 1


def test_c_close_mirrors_and_restart_keeps_pair_and_fills(tmp_path):
    path = tmp_path / "g.sqlite3"
    g = GMirror(path)
    g.handle_c_fill(event("c-open", "ct3", "OPEN", "LONG"))
    close = g.handle_c_fill(event("c-close", "ct3", "CLOSE", "LONG", quote=q("98.8", "99.0")))
    assert close["accepted"] and close["effect"] == "CLOSE"
    assert g.status(q())["position"] is None
    assert len(g.fills()) == 2
    restored = GMirror(path)
    assert len(restored.fills()) == 2
    assert restored.pairs()[0]["c_trade_id"] == "ct3"


def test_partial_close_adjusts_same_quantity_and_risk_exit_is_terminal(tmp_path):
    g = GMirror(tmp_path / "g.sqlite3")
    g.handle_c_fill(event("c-open", "ct4", "OPEN", "SHORT", "0.3"))
    partial = g.handle_c_fill(event("c-part", "ct4", "PARTIAL_CLOSE", "SHORT", "0.1"))
    assert partial["accepted"]
    assert D(g.status(q())["position"]["quantity"]) == D("0.2")
    assert g.monitor_quote(q("0.01", "0.02"))["accepted"]
    assert g.status(q())["position"] is None
    ignored = g.handle_c_fill(event("c-more", "ct4", "PARTIAL_OPEN", "SHORT", "0.1"))
    assert not ignored["accepted"] and ignored["reason"] == "PAIR_TERMINAL"
    reconciled = g.handle_c_fill(event("c-final-close", "ct4", "CLOSE", "SHORT", "0.2", quote={}))
    assert reconciled["accepted"] and reconciled["ignored"]
    assert g.pairs()[0]["status"] == "CLOSED_AFTER_RISK_EXIT"


def test_rejects_unexecutable_exact_quantity_without_resizing(tmp_path):
    g = GMirror(tmp_path / "g.sqlite3")
    result = g.handle_c_fill(event("c-big", "ct5", "OPEN", "LONG", "999999"))
    assert not result["accepted"]
    assert result["reason"] in {"LEVERAGE_LIMIT", "MARGIN_LIMIT", "RISK_LIMIT"}
    assert len(g.fills()) == 0

