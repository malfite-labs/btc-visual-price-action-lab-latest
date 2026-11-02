from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import UTC
from decimal import ROUND_HALF_UP, ROUND_UP, Decimal
from pathlib import Path
from threading import RLock
from uuid import uuid4

from backend.domain import CostModel, PositionState, planned_loss_for_fill
from backend.settings import INITIAL_EQUITY, PAPER_CURRENCY

D = Decimal
CENT = D("0.01")


def _money(value: Decimal, *, conservative_fee: bool = False) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_UP if conservative_fee else ROUND_HALF_UP)


class Ledger:
    """Small append-only SQLite paper ledger; every account mutation is one BEGIN IMMEDIATE tx."""

    def __init__(self, path: str | Path = "data/paper.sqlite3") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        with self._session() as db:
            db.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS accounts(
                  agent TEXT PRIMARY KEY, currency TEXT NOT NULL, cash TEXT NOT NULL,
                  state TEXT NOT NULL DEFAULT 'FLAT', version INTEGER NOT NULL DEFAULT 0,
                  position_json TEXT, pending_stop_json TEXT
                );
                CREATE TABLE IF NOT EXISTS fills(
                  fill_id TEXT PRIMARY KEY, agent TEXT NOT NULL, side TEXT NOT NULL,
                  quantity TEXT NOT NULL, price TEXT NOT NULL, fee TEXT NOT NULL,
                  slippage TEXT NOT NULL, occurred_at TEXT NOT NULL, idempotency_key TEXT UNIQUE NOT NULL,
                  trade_id TEXT NOT NULL, position_effect TEXT NOT NULL DEFAULT 'OPEN',
                  position_direction TEXT NOT NULL DEFAULT 'LONG', decision_id TEXT,
                  strategy_version TEXT, rationale TEXT, quote_id TEXT
                );
                CREATE TABLE IF NOT EXISTS ledger_events(
                  event_id TEXT PRIMARY KEY, agent TEXT NOT NULL, kind TEXT NOT NULL,
                  amount TEXT NOT NULL, cash_after TEXT NOT NULL, idempotency_key TEXT UNIQUE NOT NULL,
                  reference TEXT NOT NULL, occurred_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit_events(
                  event_id TEXT PRIMARY KEY, agent TEXT NOT NULL, kind TEXT NOT NULL,
                  payload TEXT NOT NULL, occurred_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS evo_counts(agent TEXT PRIMARY KEY, closed_count INTEGER NOT NULL DEFAULT 0, next_batch INTEGER NOT NULL DEFAULT 50);
                CREATE TABLE IF NOT EXISTS evo_trades(trade_id TEXT PRIMARY KEY, agent TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS evo_reviews(review_id TEXT PRIMARY KEY, agent TEXT NOT NULL, batch_end INTEGER NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS evo_candidates(agent TEXT PRIMARY KEY, candidate_id TEXT NOT NULL, status TEXT NOT NULL, started_at TEXT NOT NULL);
                """
            )
            cols = {r[1] for r in db.execute("PRAGMA table_info(fills)")}
            for name, definition in (
                ("position_effect", "TEXT NOT NULL DEFAULT 'OPEN'"),
                ("position_direction", "TEXT NOT NULL DEFAULT 'LONG'"),
                ("decision_id", "TEXT"),
                ("strategy_version", "TEXT"),
                ("rationale", "TEXT"),
                ("quote_id", "TEXT"),
                ("cost_model_json", "TEXT"),
            ):
                if name not in cols:
                    db.execute(f"ALTER TABLE fills ADD COLUMN {name} {definition}")
            for agent in ("A", "B", "C"):
                db.execute(
                    "INSERT OR IGNORE INTO accounts(agent,currency,cash) VALUES(?,?,?)",
                    (agent, PAPER_CURRENCY, INITIAL_EQUITY),
                )
                db.execute("INSERT OR IGNORE INTO evo_counts(agent) VALUES(?)", (agent,))

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        return db

    @contextmanager
    def _session(self):
        db = self._connect()
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _iso_now() -> str:
        from datetime import datetime

        return datetime.now(UTC).isoformat()

    def account(self, agent: str) -> dict:
        with self._session() as db:
            row = db.execute("SELECT * FROM accounts WHERE agent=?", (agent,)).fetchone()
        if row is None:
            raise KeyError(agent)
        return dict(row)

    def accounts(self) -> list[dict]:
        with self._session() as db:
            return [dict(r) for r in db.execute("SELECT * FROM accounts ORDER BY agent")]

    def record_system_event(self, kind: str, payload: dict) -> None:
        """Persist a bounded operational reason, without credentials or provider bodies."""
        with self._lock, self._connect() as db:
            db.execute(
                "INSERT INTO audit_events VALUES(?,?,?,?,?)",
                (
                    str(uuid4()),
                    "SYSTEM",
                    kind,
                    json.dumps(payload, ensure_ascii=False),
                    self._iso_now(),
                ),
            )

    def open_position(
        self,
        *,
        agent: str,
        direction: str,
        quantity: Decimal,
        price: Decimal,
        stop: Decimal,
        fee: Decimal,
        slippage: Decimal,
        idempotency_key: str,
        expected_version: int,
        decision_id: str | None = None,
        strategy_version: str | None = None,
        rationale: str | None = None,
        quote_id: str | None = None,
        cost_model: CostModel = CostModel(),
        risk_fraction: Decimal = D("0.02"),
        research_c_5x: bool = False,
    ) -> dict:
        if agent not in {"A", "B", "C"} or direction not in {"LONG", "SHORT"}:
            raise ValueError("invalid agent or direction")
        if research_c_5x and agent != "C":
            raise ValueError("RESEARCH_5X_ONLY_FOR_C")
        if not risk_fraction.is_finite() or not D("0") < risk_fraction <= D("1"):
            raise ValueError("INVALID_RISK_FRACTION")
        if min(quantity, price, stop) <= 0:
            raise ValueError("quantity, price and stop must be positive")
        if (direction == "LONG" and stop >= price) or (direction == "SHORT" and stop <= price):
            raise ValueError("stop must be on loss side")
        with self._lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute(
                "SELECT fill_id FROM fills WHERE idempotency_key=?", (idempotency_key,)
            ).fetchone()
            if old:
                db.commit()
                return {"accepted": True, "duplicate": True, "fill_id": old["fill_id"]}
            row = db.execute("SELECT * FROM accounts WHERE agent=?", (agent,)).fetchone()
            if row["version"] != expected_version:
                db.rollback()
                return {"accepted": False, "reason": "ACCOUNT_VERSION_CONFLICT"}
            if row["state"] != PositionState.FLAT:
                db.rollback()
                return {"accepted": False, "reason": "POSITION_NOT_FLAT"}
            fee = _money(fee, conservative_fee=True)
            notional = quantity * price
            cash = D(row["cash"])
            planned_loss = planned_loss_for_fill(quantity, price, stop, cost_model, fee)
            if (
                quantity
                != (quantity / cost_model.quantity_step).to_integral_value()
                * cost_model.quantity_step
            ):
                db.rollback()
                return {"accepted": False, "reason": "QUANTITY_PRECISION"}
            if quantity < cost_model.minimum_quantity or notional < cost_model.minimum_notional:
                db.rollback()
                return {"accepted": False, "reason": "BELOW_MINIMUM"}
            if fee < _money(notional * cost_model.fee_rate, conservative_fee=True):
                db.rollback()
                return {"accepted": False, "reason": "FEE_RESERVE_INSUFFICIENT"}
            if fee > _money(notional * cost_model.fee_rate, conservative_fee=True):
                db.rollback()
                return {"accepted": False, "reason": "FEE_AMOUNT_MISMATCH"}
            leverage = D("5") if research_c_5x else D("1")
            margin = notional / leverage
            if (
                notional > cash * leverage
                or margin + fee > cash
                or planned_loss > cash * risk_fraction
            ):
                db.rollback()
                return {"accepted": False, "reason": "ONE_X_OR_RISK_LIMIT"}
            new_cash = cash - fee
            position = {
                "direction": direction,
                "quantity": str(quantity),
                "entry_price": str(price),
                "stop_price": str(stop),
                "entry_fee": str(fee),
                "entry_slippage": str(slippage),
                "planned_loss": str(planned_loss),
                "trade_id": str(uuid4()),
            }
            if research_c_5x:
                position.update(
                    {
                        "leverage": str(leverage),
                        "initial_margin": str(margin),
                        "research_only": True,
                    }
                )
            trade_id = position["trade_id"]
            now, fill_id = self._iso_now(), str(uuid4())
            db.execute(
                "UPDATE accounts SET cash=?,state=?,version=version+1,position_json=? WHERE agent=?",
                (str(new_cash), "OPEN_" + direction, json.dumps(position), agent),
            )
            db.execute(
                "INSERT INTO fills(fill_id,agent,side,quantity,price,fee,slippage,occurred_at,idempotency_key,trade_id,position_effect,position_direction,decision_id,strategy_version,rationale,quote_id,cost_model_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    fill_id,
                    agent,
                    "BUY" if direction == "LONG" else "SELL",
                    str(quantity),
                    str(price),
                    str(fee),
                    str(slippage),
                    now,
                    idempotency_key,
                    trade_id,
                    "OPEN",
                    direction,
                    decision_id,
                    strategy_version,
                    rationale,
                    quote_id,
                    json.dumps(cost_model.provenance(), sort_keys=True),
                ),
            )
            self._ledger_insert(
                db, agent, "OPEN_FEE", -fee, new_cash, idempotency_key + ":ledger", fill_id, now
            )
            db.execute(
                "INSERT INTO audit_events VALUES(?,?,?,?,?)",
                (str(uuid4()), agent, "POSITION_OPENED", json.dumps(position), now),
            )
            db.commit()
            return {
                "accepted": True,
                "duplicate": False,
                "fill_id": fill_id,
                "trade_id": trade_id,
                "account_version": expected_version + 1,
            }

    @staticmethod
    def _ledger_insert(db, agent, kind, amount, cash_after, key, reference, now):
        db.execute(
            "INSERT INTO ledger_events VALUES(?,?,?,?,?,?,?,?)",
            (str(uuid4()), agent, kind, str(amount), str(cash_after), key, reference, now),
        )

    def request_stop_exit(
        self,
        *,
        agent: str,
        trigger_price: Decimal | None,
        event_key: str,
        quote_event_id: str | None,
        expected_version: int,
        trigger_evidence: dict | None = None,
    ) -> dict:
        """Persist confirmed stop before exit attempt; missing quote is recoverable, never fabricated."""
        with self._lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM accounts WHERE agent=?", (agent,)).fetchone()
            if row["state"] == PositionState.STOP_EXIT_PENDING:
                db.commit()
                return {
                    "accepted": True,
                    "duplicate": True,
                    "state": PositionState.STOP_EXIT_PENDING,
                }
            if row["version"] != expected_version:
                db.rollback()
                return {"accepted": False, "reason": "ACCOUNT_VERSION_CONFLICT"}
            if row["state"] == PositionState.FLAT:
                db.commit()
                return {"accepted": False, "duplicate": True, "reason": "ALREADY_FLAT"}
            if row["pending_stop_json"]:
                db.commit()
                return {
                    "accepted": True,
                    "duplicate": True,
                    "state": PositionState.STOP_EXIT_PENDING,
                }
            payload = {
                "event_key": event_key,
                "trigger_price": str(trigger_price) if trigger_price else None,
                "quote_event_id": quote_event_id,
                "triggered_at": self._iso_now(),
            }
            if trigger_evidence is not None:
                payload["trigger_evidence"] = trigger_evidence
            if trigger_price is None:
                db.execute(
                    "UPDATE accounts SET state=?,pending_stop_json=?,version=version+1 WHERE agent=?",
                    (PositionState.STOP_EXIT_PENDING, json.dumps(payload), agent),
                )
                db.execute(
                    "INSERT INTO audit_events VALUES(?,?,?,?,?)",
                    (
                        str(uuid4()),
                        agent,
                        "STOP_EXIT_PENDING_NO_QUOTE",
                        json.dumps(payload),
                        payload["triggered_at"],
                    ),
                )
                db.commit()
                return {
                    "accepted": True,
                    "state": PositionState.STOP_EXIT_PENDING,
                    "account_version": expected_version + 1,
                }
            db.execute(
                "UPDATE accounts SET state=?,pending_stop_json=?,version=version+1 WHERE agent=?",
                (PositionState.STOP_EXIT_PENDING, json.dumps(payload), agent),
            )
            db.commit()
            return {
                "accepted": True,
                "state": PositionState.STOP_EXIT_PENDING,
                "account_version": expected_version + 1,
                "ready_to_close": True,
            }

    def close_position(
        self,
        *,
        agent: str,
        price: Decimal,
        fee: Decimal,
        slippage: Decimal,
        idempotency_key: str,
        expected_version: int,
        reason: str = "MODEL_CLOSE",
        decision_id: str | None = None,
        strategy_version: str | None = None,
        quote_id: str | None = None,
        rationale: str | None = None,
        cost_model: CostModel = CostModel(),
    ) -> dict:
        with self._lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute(
                "SELECT fill_id FROM fills WHERE idempotency_key=?", (idempotency_key,)
            ).fetchone()
            if old:
                db.commit()
                return {"accepted": True, "duplicate": True, "fill_id": old["fill_id"]}
            row = db.execute("SELECT * FROM accounts WHERE agent=?", (agent,)).fetchone()
            if row["version"] != expected_version:
                db.rollback()
                return {"accepted": False, "reason": "ACCOUNT_VERSION_CONFLICT"}
            if row["state"] == PositionState.STOP_EXIT_PENDING and not reason.startswith("STOP_"):
                db.rollback()
                return {"accepted": False, "reason": "STOP_EXIT_HAS_PRIORITY"}
            if row["state"] not in {"OPEN_LONG", "OPEN_SHORT", PositionState.STOP_EXIT_PENDING}:
                db.rollback()
                return {"accepted": False, "reason": "NOT_CLOSABLE"}
            if not row["position_json"]:
                db.rollback()
                return {"accepted": False, "reason": "POSITION_MISSING"}
            pos = json.loads(row["position_json"])
            direction, qty = pos["direction"], D(pos["quantity"])
            entry = D(pos["entry_price"])
            fee = _money(fee, conservative_fee=True)
            if fee < _money(qty * price * cost_model.fee_rate, conservative_fee=True):
                db.rollback()
                return {"accepted": False, "reason": "FEE_RESERVE_INSUFFICIENT"}
            if fee > _money(qty * price * cost_model.fee_rate, conservative_fee=True):
                db.rollback()
                return {"accepted": False, "reason": "FEE_AMOUNT_MISMATCH"}
            gross = _money((price - entry) * qty * (1 if direction == "LONG" else -1))
            total_fee = fee + D(pos["entry_fee"])
            total_slip = slippage + D(pos["entry_slippage"])
            net = gross - fee - D(pos["entry_fee"])
            # Slippage is already embedded in the adverse simulated fill price; do not charge it twice.
            cash = D(row["cash"]) + gross - fee
            now, fill_id = self._iso_now(), str(uuid4())
            trade_id = pos["trade_id"]
            db.execute(
                "UPDATE accounts SET cash=?,state='FLAT',version=version+1,position_json=NULL,pending_stop_json=NULL WHERE agent=?",
                (str(cash), agent),
            )
            db.execute(
                "INSERT INTO fills(fill_id,agent,side,quantity,price,fee,slippage,occurred_at,idempotency_key,trade_id,position_effect,position_direction,decision_id,strategy_version,rationale,quote_id,cost_model_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    fill_id,
                    agent,
                    "SELL" if direction == "LONG" else "BUY",
                    str(qty),
                    str(price),
                    str(fee),
                    str(slippage),
                    now,
                    idempotency_key,
                    trade_id,
                    "CLOSE",
                    direction,
                    decision_id,
                    strategy_version,
                    rationale or reason,
                    quote_id,
                    json.dumps(cost_model.provenance(), sort_keys=True),
                ),
            )
            self._ledger_insert(
                db, agent, "CLOSE_PNL", gross - fee, cash, idempotency_key + ":ledger", fill_id, now
            )
            close = {
                "trade_id": trade_id,
                "gross_pnl": str(gross),
                "net_pnl": str(net),
                "fees_total": str(total_fee),
                "slippage_total": str(total_slip),
                "reason": reason,
                "closed_at": now,
                "exit_fill_id": fill_id,
            }
            db.execute(
                "INSERT INTO audit_events VALUES(?,?,?,?,?)",
                (str(uuid4()), agent, "POSITION_CLOSED", json.dumps(close), now),
            )
            # A complete round trip increments its evolution counter in the same transaction.
            inserted = db.execute(
                "INSERT OR IGNORE INTO evo_trades VALUES(?,?)", (trade_id, agent)
            ).rowcount
            if inserted:
                count, next_batch = db.execute(
                    "SELECT closed_count,next_batch FROM evo_counts WHERE agent=?", (agent,)
                ).fetchone()
                count += 1
                if count >= next_batch:
                    active = db.execute(
                        "SELECT status FROM evo_candidates WHERE agent=?", (agent,)
                    ).fetchone()
                    review_status = (
                        "QUEUED" if active and active[0] == "RUNNING" else "READY_FOR_REVIEW"
                    )
                    db.execute(
                        "INSERT OR IGNORE INTO evo_reviews VALUES(?,?,?,?,?)",
                        (str(uuid4()), agent, next_batch, review_status, now),
                    )
                    next_batch += 50
                db.execute(
                    "UPDATE evo_counts SET closed_count=?,next_batch=? WHERE agent=?",
                    (count, next_batch, agent),
                )
            db.commit()
            return {
                "accepted": True,
                "fill_id": fill_id,
                "trade_id": trade_id,
                "net_pnl": str(net),
                "account_version": expected_version + 1,
            }

    def fills(self, agent: str | None = None) -> list[dict]:
        with self._session() as db:
            sql, args = "SELECT * FROM fills ORDER BY occurred_at,fill_id", ()
            if agent:
                sql, args = sql.replace(" ORDER BY", " WHERE agent=? ORDER BY"), (agent,)
            return [dict(r) for r in db.execute(sql, args)]

    def account_metrics(
        self, agent: str, quote: dict | None, cost_model: CostModel = CostModel()
    ) -> dict:
        row = self.account(agent)
        cash = D(row["cash"])
        if not row["position_json"]:
            return {
                "cash": str(cash),
                "equity": str(cash),
                "unrealized_pnl": "0.00",
                "available_cash": str(cash),
                "valuation_status": "FLAT",
            }
        import json

        pos = json.loads(row["position_json"])
        if (
            not quote
            or quote.get("source") != "ALPACA_US_BTCUSD_SPOT"
            or quote.get("timestamp_valid", True) is not True
            or not 0 <= int(quote.get("age_ms", 10**9)) <= cost_model.max_quote_age_ms
        ):
            return {
                "cash": str(cash),
                "equity": None,
                "unrealized_pnl": None,
                "available_cash": "0",
                "valuation_status": "UNVALUED_STALE_OR_UNAVAILABLE_QUOTE",
            }
        qty, entry = D(pos["quantity"]), D(pos["entry_price"])
        side = "bid" if pos["direction"] == "LONG" else "ask"
        mark = D(str(quote.get(side, "0")))
        if mark <= 0:
            return {
                "cash": str(cash),
                "equity": None,
                "unrealized_pnl": None,
                "available_cash": "0",
                "valuation_status": "EXECUTABLE_SIDE_MISSING",
            }
        gross = (mark - entry) * qty * (1 if pos["direction"] == "LONG" else -1)
        close_reserve = (
            qty * mark * (cost_model.fee_rate + cost_model.adverse_slippage_bps / D("10000"))
        )
        unrealized = _money(gross - close_reserve)
        equity = cash + unrealized
        reserved = D(pos.get("initial_margin", str(qty * entry)))
        available = max(D("0"), equity - reserved - close_reserve)
        return {
            "cash": str(cash),
            "equity": str(_money(equity)),
            "unrealized_pnl": str(unrealized),
            "available_cash": str(_money(available)),
            "used_margin": str(_money(reserved)),
            "valuation_status": "MARKED_TO_ALPACA_SPOT_EXECUTABLE_SIDE",
            "mark_price": str(mark),
            "mark_basis": "ALPACA_SPOT_BID" if pos["direction"] == "LONG" else "ALPACA_SPOT_ASK",
        }
