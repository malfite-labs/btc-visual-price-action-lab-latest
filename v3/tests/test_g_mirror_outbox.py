import json
from datetime import datetime, timezone
from decimal import Decimal as D

from backend.ledger import Ledger
from backend.g_mirror import GMirror
from backend.execution import handle_confirmed_stop
from backend.domain import CostModel


def quote():
    now = datetime.now(timezone.utc).isoformat()
    return {"source": "BINANCE_USDM_PERP", "symbol": "BTC/USD", "currency": "USD_RESEARCH",
            "timestamp_valid": True, "age_ms": 10, "event_at": now, "received_at": now,
            "bid": "99.9", "ask": "100.1", "event_id": "q-1"}


def test_only_committed_c_fills_enter_outbox_and_dispatch_after_commit(tmp_path):
    ledger = Ledger(tmp_path / "paper.sqlite3", mirror_enabled=True)
    seen = []
    def dispatch(payload):
        assert ledger.account("C")["state"] == "OPEN_LONG"
        seen.append(payload)
        return {"accepted": True}
    ledger.set_mirror_dispatcher(dispatch)
    opened = ledger.open_position(agent="C", direction="LONG", quantity=D("0.2"), price=D("100"),
                                  stop=D("90"), fee=D("0.01"), slippage=D("0.04"),
                                  idempotency_key="c-open-1", expected_version=0,
                                  execution_quote=quote())
    ledger._mirror_dispatch_thread.join(2)
    assert opened["accepted"] and len(seen) == 1
    assert seen[0]["source_fill_id"] == opened["fill_id"]
    assert seen[0]["quote"]["event_id"] == "q-1"
    with ledger._connect() as db:
        row = db.execute("SELECT status,payload FROM mirror_outbox WHERE source_fill_id=?", (opened["fill_id"],)).fetchone()
    assert row["status"] == "DONE"
    assert json.loads(row["payload"])["effect"] == "OPEN"


def test_g_dispatch_exception_does_not_rollback_c_and_leaves_durable_retry(tmp_path):
    ledger = Ledger(tmp_path / "paper.sqlite3", mirror_enabled=True)
    ledger.set_mirror_dispatcher(lambda event: (_ for _ in ()).throw(RuntimeError("G unavailable")))
    result = ledger.open_position(agent="C", direction="SHORT", quantity=D("0.2"), price=D("100"),
                                  stop=D("110"), fee=D("0.01"), slippage=D("0.04"),
                                  idempotency_key="c-open-2", expected_version=0,
                                  execution_quote=quote())
    ledger._mirror_dispatch_thread.join(2)
    assert result["accepted"]
    assert ledger.account("C")["state"] == "OPEN_SHORT"
    with ledger._connect() as db:
        row = db.execute("SELECT status,attempts,last_error FROM mirror_outbox WHERE source_fill_id=?", (result["fill_id"],)).fetchone()
    assert tuple(row) == ("PENDING", 1, "RuntimeError")


def test_non_c_fills_never_dispatch_to_g(tmp_path):
    ledger = Ledger(tmp_path / "paper.sqlite3", mirror_enabled=True)
    seen = []
    ledger.set_mirror_dispatcher(seen.append)
    result = ledger.open_position(agent="A", direction="LONG", quantity=D("0.2"), price=D("100"),
                                  stop=D("90"), fee=D("0.01"), slippage=D("0.04"),
                                  idempotency_key="a-open", expected_version=0,
                                  execution_quote=quote())
    if ledger._mirror_dispatch_thread: ledger._mirror_dispatch_thread.join(2)
    assert result["accepted"] and seen == []
    with ledger._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM mirror_outbox").fetchone()[0] == 0


def test_c_quote_stop_close_is_mirrored_through_the_actual_protection_handler(tmp_path):
    ledger = Ledger(tmp_path / "paper.sqlite3", mirror_enabled=True)
    mirror = GMirror(tmp_path / "g.sqlite3")
    ledger.set_mirror_dispatcher(mirror.handle_c_fill)
    opened = ledger.open_position(agent="C", direction="LONG", quantity=D("0.2"), price=D("100"),
                                  stop=D("90"), fee=D("0.01"), slippage=D("0.04"),
                                  idempotency_key="stop-c-open", expected_version=0,
                                  execution_quote=quote())
    assert opened["accepted"]
    trigger = dict(quote(), bid="89", ask="89.1", event_id="stop-q2")
    outcome = handle_confirmed_stop(ledger=ledger, agent="C", trigger_event_id="stop-q2",
                                    quote=trigger, cost=CostModel())
    ledger._mirror_dispatch_thread.join(2)
    assert outcome["accepted"]
    assert ledger.account("C")["state"] == "FLAT"
    assert mirror.status(quote())["position"] is None
    assert len(mirror.fills()) == 2
    assert mirror.fills()[-1]["reason"].startswith("C_MIRROR_CLOSE:STOP_TRIGGER")

