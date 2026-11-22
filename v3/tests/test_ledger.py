from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal as D

from backend.ledger import Ledger


def test_idempotent_open_close_atomicity_and_recovery(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    acct = ledger.account("A")
    args = dict(
        agent="A",
        direction="LONG",
        quantity=D("0.01"),
        price=D(100000),
        stop=D(98000),
        fee=D("0.50"),
        slippage=D(2),
        idempotency_key="open-1",
        expected_version=acct["version"],
    )
    first = ledger.open_position(**args)
    duplicate = ledger.open_position(**args)
    assert first["accepted"] and duplicate["duplicate"] and len(ledger.fills("A")) == 1
    acct = ledger.account("A")
    pending = ledger.request_stop_exit(
        agent="A",
        trigger_price=None,
        event_key="stop-1",
        quote_event_id=None,
        expected_version=acct["version"],
    )
    assert pending["state"] == "STOP_EXIT_PENDING"
    l2 = Ledger(tmp_path / "ledger.sqlite3")
    acct2 = l2.account("A")
    assert acct2["state"] == "STOP_EXIT_PENDING" and '"quantity": "0.01"' in acct2["position_json"]
    close = l2.close_position(
        agent="A",
        price=D(97900),
        fee=D("0.49"),
        slippage=D(3),
        idempotency_key="close-stop-1",
        expected_version=acct2["version"],
        reason="STOP_RECOVERY",
    )
    dup = l2.close_position(
        agent="A",
        price=D(97900),
        fee=D("0.49"),
        slippage=D(3),
        idempotency_key="close-stop-1",
        expected_version=acct2["version"],
        reason="STOP_RECOVERY",
    )
    assert close["accepted"] and dup["duplicate"] and l2.account("A")["state"] == "FLAT"
    assert len(l2.fills("A")) == 2 and D(l2.account("A")["cash"]) == D("9978.01")


def test_accounts_isolated_and_optimistic_version(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    a = ledger.account("A")
    result = ledger.open_position(
        agent="A",
        direction="SHORT",
        quantity=D("0.01"),
        price=D(100000),
        stop=D(102000),
        fee=D("0.50"),
        slippage=D(1),
        idempotency_key="a1",
        expected_version=a["version"],
    )
    assert result["accepted"] and ledger.account("B")["cash"] == "10000.00"
    conflict = ledger.open_position(
        agent="A",
        direction="SHORT",
        quantity=D("0.01"),
        price=D(100000),
        stop=D(102000),
        fee=D("0.50"),
        slippage=D(1),
        idempotency_key="a2",
        expected_version=a["version"],
    )
    assert conflict["reason"] == "ACCOUNT_VERSION_CONFLICT"


def test_only_complete_round_trips_trigger_50_trade_review(tmp_path):
    ledger = Ledger(tmp_path / "paper.sqlite3")
    for i in range(50):
        a = ledger.account("A")
        opened = ledger.open_position(
            agent="A",
            direction="LONG",
            quantity=D("0.001"),
            price=D(100000),
            stop=D(99000),
            fee=D("0.05"),
            slippage=D(0),
            idempotency_key=f"o-{i}",
            expected_version=a["version"],
        )
        assert opened["accepted"]
        a = ledger.account("A")
        closed = ledger.close_position(
            agent="A",
            price=D(100100),
            fee=D("0.06"),
            slippage=D(0),
            idempotency_key=f"c-{i}",
            expected_version=a["version"],
        )
        assert closed["accepted"]
    import sqlite3

    with sqlite3.connect(ledger.path) as db:
        assert db.execute("SELECT closed_count FROM evo_counts WHERE agent='A'").fetchone()[0] == 50
        row = db.execute("SELECT batch_end,status FROM evo_reviews WHERE agent='A'").fetchone()
        assert row == (50, "READY_FOR_REVIEW")
        assert db.execute("SELECT COUNT(*) FROM evo_trades WHERE agent='A'").fetchone()[0] == 50


def test_concurrent_agent_opens_keep_accounts_independent_and_atomic(tmp_path):
    ledger = Ledger(tmp_path / "parallel.sqlite3")

    def open_for(agent):
        account = ledger.account(agent)
        return ledger.open_position(
            agent=agent,
            direction="LONG" if agent != "B" else "SHORT",
            quantity=D("0.01"),
            price=D("100000"),
            stop=D("99000") if agent != "B" else D("101000"),
            fee=D("0.50"),
            slippage=D("1"),
            idempotency_key=f"parallel-{agent}",
            expected_version=account["version"],
        )

    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(open_for, "ABC"))
    assert all(result["accepted"] for result in results)
    assert [ledger.account(agent)["state"] for agent in "ABC"] == [
        "OPEN_LONG",
        "OPEN_SHORT",
        "OPEN_LONG",
    ]
    assert len(ledger.fills()) == 3
    assert all(ledger.account(agent)["cash"] == "9999.50" for agent in "ABC")
