from decimal import Decimal as D

from backend.domain import CostModel
from backend.execution import handle_research_liquidation
from backend.ledger import Ledger


def test_gap_loss_is_not_capped_and_negative_account_cannot_reopen(tmp_path):
    ledger = Ledger(tmp_path / "isolated.sqlite")
    assert ledger.open_position(
        agent="C",
        direction="LONG",
        quantity=D("400"),
        price=D("100"),
        stop=D("99.99"),
        fee=D("20"),
        slippage=D("0"),
        idempotency_key="entry",
        expected_version=0,
        research_c_5x=True,
    )["accepted"]
    quote = {
        "source": "ALPACA_US_BTCUSD_SPOT",
        "age_ms": 0,
        "timestamp_valid": True,
        "bid": "50",
        "ask": "50.1",
        "bid_qty": "1000",
        "ask_qty": "1000",
        "event_id": "q1",
    }
    assert handle_research_liquidation(
        ledger=ledger,
        agent="C",
        quote=quote,
        cost=CostModel(),
        event_id="q1",
        maintenance_rate=D("0.005"),
    )["accepted"]
    account = ledger.account("C")
    assert D(account["cash"]) < 0
    assert len(ledger.fills("C")) == 2
    reopened = ledger.open_position(
        agent="C",
        direction="LONG",
        quantity=D("1"),
        price=D("100"),
        stop=D("99.99"),
        fee=D("0.05"),
        slippage=D("0"),
        idempotency_key="retry",
        expected_version=account["version"],
        research_c_5x=True,
    )
    assert not reopened["accepted"] and ledger.account("C")["cash"] == account["cash"]
    with ledger._session() as db:
        events = db.execute("SELECT amount FROM ledger_events WHERE agent='C'").fetchall()
    assert D("10000") + sum((D(r["amount"]) for r in events), D("0")) == D(account["cash"])
