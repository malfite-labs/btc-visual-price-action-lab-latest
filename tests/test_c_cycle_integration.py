import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

import pytest

from backend.domain import CostModel
from backend.ledger import Ledger
from backend.orchestration import TestOnlyPaperCycleRunner
from backend.vision import AgentOrchestrator, FakeVisionAdapter


class COnlyAdapter(FakeVisionAdapter):
    def __init__(self, action, stop):
        self.action, self.stop = action, stop

    async def decide(self, **kwargs):
        result = await super().decide(**kwargs)
        result.update(action=self.action, stop_loss=self.stop, rationale="isolated test")
        return result


@pytest.mark.parametrize(
    ("open_action", "close_action", "stop"),
    [("OPEN_LONG", "CLOSE_LONG", "99.9"), ("OPEN_SHORT", "CLOSE_SHORT", "100.1")],
)
def test_isolated_model_decision_through_c_5x_ledger(tmp_path, open_action, close_action, stop):
    db = tmp_path / "isolated.sqlite"
    ledger = Ledger(db)
    runner = TestOnlyPaperCycleRunner(
        ledger,
        AgentOrchestrator(str(db)),
        CostModel(),
        enable_c_5x=True,
        c_maintenance_rate=D("0.01"),
    )
    quote = {
        "source": "BINANCE_USDM_PERP",
        "currency": "USD_RESEARCH",
        "bid": "100",
        "ask": "100",
        "bid_qty": "1000",
        "ask_qty": "1000",
        "age_ms": 0,
        "timestamp_valid": True,
        "event_id": "q1",
    }
    book = {
        "source": "BINANCE_USDM_PERP",
        "age_ms": 0,
        "bids": [{"p": "100", "s": "1000"}],
        "asks": [{"p": "100", "s": "1000"}],
    }
    start = datetime.now(UTC)

    def cycle(action, close, stop_value):
        return asyncio.run(
            runner.run_cycle(
                adapter=COnlyAdapter(action, stop_value),
                image=b"isolated",
                candle_close=close,
                cycle_deadline=close + timedelta(minutes=15),
                now=close + timedelta(seconds=1),
                strategies={"C": ("v1.0.0", "C private test strategy")},
                quote=quote,
                orderbook=book,
                feed_healthy=True,
            )
        )

    result = cycle(open_action, start, stop)
    assert result["C"]["execution"]["accepted"]
    fill = ledger.fills("C")[0]
    assert D(fill["quantity"]) * D(fill["price"]) > D("10000")
    result = cycle(close_action, start + timedelta(minutes=15), None)
    assert result["C"]["execution"]["accepted"] and ledger.account("C")["state"] == "FLAT"
    assert len(ledger.fills("C")) == 2 and not ledger.fills("A") and not ledger.fills("B")

