"""Isolated, event-driven reverse paper mirror of committed C ledger fills."""
from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from decimal import Decimal, ROUND_UP
from pathlib import Path
from threading import RLock
from uuid import uuid4

from backend.domain import CostModel, ceil_step, floor_step, planned_loss_for_fill

D = Decimal


class GMirror:
    VERSION = "1.0.0"

    def __init__(self, path: str | Path, *, initial_equity: D = D("10000.00"), cost: CostModel = CostModel()):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.cost = cost
        self._lock = RLock()
        with self._connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS g_strategy(strategy_id TEXT PRIMARY KEY, version TEXT NOT NULL, name TEXT NOT NULL, source_strategy TEXT NOT NULL, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS g_account(singleton INTEGER PRIMARY KEY CHECK(singleton=1), cash TEXT NOT NULL, state TEXT NOT NULL, position_json TEXT, version INTEGER NOT NULL DEFAULT 0, enabled INTEGER NOT NULL DEFAULT 1, follow_state TEXT NOT NULL DEFAULT 'WAIT_FOR_C_NEXT_OPEN', error TEXT, last_cycle TEXT NOT NULL DEFAULT 'WAIT_FOR_C_NEXT_OPEN', last_action TEXT NOT NULL DEFAULT 'WAIT_FOR_C_NEXT_OPEN');
                CREATE TABLE IF NOT EXISTS g_fills(fill_id TEXT PRIMARY KEY, source_fill_id TEXT UNIQUE NOT NULL, c_trade_id TEXT NOT NULL, g_trade_id TEXT NOT NULL, effect TEXT NOT NULL, direction TEXT NOT NULL, quantity TEXT NOT NULL, price TEXT NOT NULL, fee TEXT NOT NULL, slippage TEXT NOT NULL, pnl TEXT NOT NULL, reason TEXT NOT NULL, occurred_at TEXT NOT NULL, quote_id TEXT, decision_id TEXT, c_fill_at TEXT);
                CREATE TABLE IF NOT EXISTS g_pairs(c_trade_id TEXT PRIMARY KEY, g_trade_id TEXT NOT NULL, c_direction TEXT NOT NULL, g_direction TEXT NOT NULL, quantity_opened TEXT NOT NULL, status TEXT NOT NULL, opened_at TEXT NOT NULL, closed_at TEXT, last_event_id TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS g_events(source_fill_id TEXT PRIMARY KEY, c_trade_id TEXT NOT NULL, status TEXT NOT NULL, reason TEXT, result_json TEXT, processed_at TEXT NOT NULL);
            """)
            columns = {row[1] for row in db.execute("PRAGMA table_info(g_account)")}
            if "last_action" not in columns:
                db.execute("ALTER TABLE g_account ADD COLUMN last_action TEXT NOT NULL DEFAULT 'WAIT_FOR_C_NEXT_OPEN'")
            db.execute("INSERT OR IGNORE INTO g_strategy VALUES('G',?,'G 反向镜像策略','C',?)", (self.VERSION, self._now()))
            fill_columns = {row[1] for row in db.execute("PRAGMA table_info(g_fills)")}
            for name in ("decision_id", "c_fill_at"):
                if name not in fill_columns:
                    db.execute(f"ALTER TABLE g_fills ADD COLUMN {name} TEXT")
            db.execute("INSERT OR IGNORE INTO g_account(singleton,cash,state,follow_state) VALUES(1,?,'FLAT','WAIT_FOR_C_NEXT_OPEN')", (str(initial_equity),))

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        db.row_factory = sqlite3.Row
        return db

    @staticmethod
    def _now():
        return datetime.now(UTC).isoformat()

    @staticmethod
    def _money(value: D, *, up=False) -> D:
        return value.quantize(D("0.01"), rounding=ROUND_UP if up else "ROUND_HALF_UP")

    def handle_c_fill(self, event: dict) -> dict:
        """Consume one committed C fill. Exact source quantity is copied or rejected."""
        source_id = str(event["source_fill_id"])
        trade_id = str(event["c_trade_id"])
        effect = str(event["effect"]).upper()
        c_direction = str(event["direction"]).upper()
        qty = D(str(event["quantity"]))
        quote = event.get("quote") or {}
        if effect not in {"OPEN", "CLOSE", "PARTIAL_OPEN", "PARTIAL_CLOSE"}:
            return self._record_reject(source_id, trade_id, "UNSUPPORTED_EFFECT")
        if c_direction not in {"LONG", "SHORT"} or qty <= 0:
            return self._record_reject(source_id, trade_id, "INVALID_SOURCE_FILL")
        if qty != floor_step(qty, self.cost.quantity_step):
            return self._record_reject(source_id, trade_id, "QUANTITY_PRECISION")
        with self._lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            prior = db.execute("SELECT result_json FROM g_events WHERE source_fill_id=?", (source_id,)).fetchone()
            if prior:
                db.commit()
                return {**json.loads(prior["result_json"] or "{}"), "duplicate": True}
            pair = db.execute("SELECT * FROM g_pairs WHERE c_trade_id=?", (trade_id,)).fetchone()
            account = db.execute("SELECT * FROM g_account WHERE singleton=1").fetchone()
            pos = json.loads(account["position_json"]) if account["position_json"] else None
            opening = effect in {"OPEN", "PARTIAL_OPEN"}
            closing = effect in {"CLOSE", "PARTIAL_CLOSE"}
            if closing and pair and pair["status"] == "RISK_EXITED" and not pos:
                final_status = "CLOSED_AFTER_RISK_EXIT" if effect == "CLOSE" else "RISK_EXITED"
                db.execute("UPDATE g_pairs SET status=?,last_event_id=? WHERE c_trade_id=?", (final_status, source_id, trade_id))
                result = {"accepted": True, "ignored": True, "reason": "G_ALREADY_RISK_EXITED", "terminal": effect == "CLOSE"}
                self._record_event(db, source_id, trade_id, "RECONCILED_AFTER_RISK_EXIT", result["reason"], result)
                db.commit()
                return result
            if not self._valid_quote(quote):
                db.rollback()
                return self._record_reject(source_id, trade_id, "QUOTE_STALE_OR_INVALID")
            if opening:
                if pair and pair["status"] != "OPEN":
                    return self._abort_record(db, source_id, trade_id, "PAIR_TERMINAL")
                if pair and pair["c_direction"] != c_direction:
                    return self._abort_record(db, source_id, trade_id, "SOURCE_DIRECTION_CHANGED")
                if pos and (pos["c_trade_id"] != trade_id or pos["direction"] != self._reverse(c_direction)):
                    return self._abort_record(db, source_id, trade_id, "G_POSITION_CONFLICT")
                result = self._open(db, event, qty, quote, account, pos, pair)
            else:
                if not pair or pair["status"] != "OPEN" or not pos or pos["c_trade_id"] != trade_id:
                    return self._abort_record(db, source_id, trade_id, "NO_OPEN_MIRROR_PAIR")
                if c_direction != pair["c_direction"]:
                    return self._abort_record(db, source_id, trade_id, "SOURCE_DIRECTION_MISMATCH")
                if qty > D(pos["quantity"]):
                    return self._abort_record(db, source_id, trade_id, "CLOSE_QTY_EXCEEDS_G_POSITION")
                if effect == "CLOSE" and qty != D(pos["quantity"]):
                    return self._abort_record(db, source_id, trade_id, "FULL_CLOSE_QTY_MISMATCH")
                result = self._close(db, event, qty, quote, account, pos, pair, terminal=(effect == "CLOSE" or qty == D(pos["quantity"])))
            if not result.get("accepted"):
                db.rollback()
                return self._record_reject(source_id, trade_id, result.get("reason", "REJECTED"))
            self._record_event(db, source_id, trade_id, "PROCESSED", None, result)
            db.commit()
            return result

    def _open(self, db, event, qty, quote, account, pos, pair):
        g_direction = self._reverse(event["direction"])
        side = "BUY" if g_direction == "LONG" else "SELL"
        price, fee, slip = self._fill(side, qty, quote)
        cash = D(account["cash"])
        equity = self._equity(cash, pos, quote)
        current_qty = D(pos["quantity"]) if pos else D("0")
        new_qty = current_qty + qty
        notional = new_qty * price
        if notional > equity * D("5"):
            return {"accepted": False, "reason": "LEVERAGE_LIMIT"}
        if notional / D("5") + fee > cash:
            return {"accepted": False, "reason": "MARGIN_LIMIT"}
        total_entry_fee = (D(pos["entry_fee"]) if pos else D("0")) + fee
        old_notional = current_qty * D(pos["entry_price"]) if pos else D("0")
        avg = (old_notional + qty * price) / new_qty
        try:
            stop = self._emergency_stop(g_direction, avg, new_qty, equity, total_entry_fee)
        except ValueError:
            return {"accepted": False, "reason": "RISK_BUDGET_TOO_SMALL_FOR_EMERGENCY_STOP"}
        planned = planned_loss_for_fill(new_qty, avg, stop, self.cost, total_entry_fee)
        if planned > equity * D("0.02"):
            return {"accepted": False, "reason": "RISK_LIMIT"}
        g_trade_id = pos["g_trade_id"] if pos else str(uuid4())
        next_pos = {"c_trade_id": event["c_trade_id"], "g_trade_id": g_trade_id, "direction": g_direction,
                    "quantity": str(new_qty), "entry_price": str(avg), "entry_fee": str(total_entry_fee),
                    "entry_slippage": str((D(pos["entry_slippage"]) if pos else D("0")) + slip),
                    "stop_price": str(stop), "opened_at": pos["opened_at"] if pos else self._now()}
        now = self._now()
        db.execute("UPDATE g_account SET cash=?,state=?,position_json=?,version=version+1,follow_state='MIRRORING',last_cycle=?,last_action='MIRROR_OPEN',error=NULL WHERE singleton=1",
                   (str(cash-fee), "OPEN_"+g_direction, json.dumps(next_pos), event.get('quote', {}).get('event_at') or self._now()))
        fill_id = self._insert_fill(db, event, g_trade_id, "OPEN", g_direction, qty, price, fee, slip, D("0"), str(event.get("reason", "C_OPEN")), quote)
        opened = pos["opened_at"] if pos else now
        db.execute("INSERT INTO g_pairs(c_trade_id,g_trade_id,c_direction,g_direction,quantity_opened,status,opened_at,last_event_id) VALUES(?,?,?,?,?,'OPEN',?,?) ON CONFLICT(c_trade_id) DO UPDATE SET quantity_opened=?,last_event_id=?",
                   (event["c_trade_id"], g_trade_id, event["direction"], g_direction, str(qty), opened, event["source_fill_id"], str(new_qty), event["source_fill_id"]))
        return {"accepted": True, "effect": "OPEN", "fill_id": fill_id, "g_trade_id": g_trade_id, "g_direction": g_direction,
                "quantity": str(qty), "price": str(price), "fee": str(fee), "stop_price": str(stop)}

    def _close(self, db, event, qty, quote, account, pos, pair, terminal):
        side = "SELL" if pos["direction"] == "LONG" else "BUY"
        price, fee, slip = self._fill(side, qty, quote)
        old_qty, entry = D(pos["quantity"]), D(pos["entry_price"])
        gross = self._money((price-entry)*qty*(1 if pos["direction"] == "LONG" else -1))
        entry_fee_part = D(pos["entry_fee"]) * qty / old_qty
        entry_slip_part = D(pos["entry_slippage"]) * qty / old_qty
        cash = D(account["cash"]) + gross - fee
        rem = old_qty - qty
        now = self._now()
        if rem == 0:
            db.execute("UPDATE g_account SET cash=?,state='FLAT',position_json=NULL,version=version+1,follow_state=?,last_cycle=?,last_action=?,error=NULL WHERE singleton=1",
                       (str(cash), "WAIT_FOR_C_NEXT_OPEN" if terminal else "PAIR_CLOSED", event.get('quote', {}).get('event_at') or self._now(), 'MIRROR_CLOSE' if terminal else 'MIRROR_PARTIAL_CLOSE'))
        else:
            next_pos = dict(pos)
            next_pos["quantity"] = str(rem)
            next_pos["entry_fee"] = str(D(pos["entry_fee"])-entry_fee_part)
            next_pos["entry_slippage"] = str(D(pos["entry_slippage"])-entry_slip_part)
            db.execute("UPDATE g_account SET cash=?,position_json=?,version=version+1,follow_state='MIRRORING',last_cycle=?,last_action='MIRROR_PARTIAL_CLOSE' WHERE singleton=1", (str(cash), json.dumps(next_pos), event.get('quote', {}).get('event_at') or self._now()))
        reason = "CLOSE" if terminal else "PARTIAL_CLOSE"
        if terminal:
            db.execute("UPDATE g_pairs SET status='CLOSED',closed_at=?,last_event_id=? WHERE c_trade_id=?", (now, event["source_fill_id"], event["c_trade_id"]))
        else:
            db.execute("UPDATE g_pairs SET last_event_id=? WHERE c_trade_id=?", (event["source_fill_id"], event["c_trade_id"]))
        fill_id = self._insert_fill(db, event, pos["g_trade_id"], reason, pos["direction"], qty, price, fee, slip, gross-fee-entry_fee_part,
                                    "C_MIRROR_CLOSE:"+str(event.get("reason", "C_CLOSE")), quote)
        return {"accepted": True, "effect": reason, "fill_id": fill_id, "g_trade_id": pos["g_trade_id"], "quantity": str(qty),
                "price": str(price), "fee": str(fee), "net_pnl": str(gross-fee-entry_fee_part), "terminal": terminal}

    def monitor_quote(self, quote: dict) -> dict:
        if not self._valid_quote(quote):
            return {"accepted": False, "reason": "QUOTE_STALE_OR_INVALID"}
        with self._lock, self._connect() as db:
            row = db.execute("SELECT * FROM g_account WHERE singleton=1").fetchone()
            if not row["position_json"]:
                return {"accepted": False, "reason": "FLAT"}
            pos = json.loads(row["position_json"])
            mark = D(str(quote["bid"] if pos["direction"] == "LONG" else quote["ask"]))
            stop = D(pos["stop_price"])
            hit = mark <= stop if pos["direction"] == "LONG" else mark >= stop
            if not hit:
                return {"accepted": False, "reason": "NO_STOP"}
        result = self._emergency_close(pos, quote)
        return result

    def _emergency_close(self, pos, quote):
        with self._lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM g_account WHERE singleton=1").fetchone()
            current = json.loads(row["position_json"]) if row["position_json"] else None
            if not current or current["g_trade_id"] != pos["g_trade_id"]:
                db.rollback(); return {"accepted": False, "reason": "POSITION_CHANGED"}
            fake = {"source_fill_id": "risk:"+str(uuid4()), "c_trade_id": pos["c_trade_id"], "reason": "MIRROR_RISK_EXIT"}
            pair = db.execute("SELECT * FROM g_pairs WHERE c_trade_id=?", (pos["c_trade_id"],)).fetchone()
            result = self._close(db, fake, D(pos["quantity"]), quote, row, pos, pair, terminal=False)
            if result.get("accepted"):
                db.execute("UPDATE g_pairs SET status='RISK_EXITED',last_event_id=? WHERE c_trade_id=?", (fake["source_fill_id"], pos["c_trade_id"]))
                db.execute("UPDATE g_account SET follow_state='WAIT_FOR_C_NEXT_OPEN',last_cycle=?,last_action='MIRROR_RISK_EXIT',error='MIRROR_RISK_EXIT' WHERE singleton=1", (quote.get('event_at') or self._now(),))
                self._record_event(db, fake["source_fill_id"], pos["c_trade_id"], "RISK_EXIT", "MIRROR_RISK_EXIT", result)
                db.commit(); return {**result, "reason": "MIRROR_RISK_EXIT"}
            db.rollback(); return result

    def status(self, quote: dict | None = None) -> dict:
        with self._connect() as db:
            row = dict(db.execute("SELECT * FROM g_account WHERE singleton=1").fetchone())
            pos = json.loads(row["position_json"]) if row["position_json"] else None
            cash = D(row["cash"])
            valid_mark = self._valid_quote(quote or {}) if pos else True
            equity = self._equity(cash, pos, quote) if pos and valid_mark else None if pos else cash
            fills = db.execute("SELECT COUNT(*) FROM g_fills").fetchone()[0]
            return {"id": "G", "name": "G 反向镜像策略", "strategy_name_en": "C MIRROR REVERSE", "version": self.VERSION, "registered": True, "enabled": bool(row["enabled"]),
                    "status": row["follow_state"], "follow_state": row["follow_state"], "position": pos,
                    "position_direction": pos["direction"] if pos else None, "position_amount": str(D(pos["quantity"])*D(pos["entry_price"])) if pos else "0",
                    "equity": str(equity) if equity is not None else None, "cash": row["cash"], "realized": self.realized(), "unrealized": str(equity-cash) if equity is not None else None,
                    "fees": self.fees(), "last_cycle": row["last_cycle"], "last_action": row["error"] or row["last_action"],
                    "last_error": row["error"], "trade_count": fills, "source": "C", "leverage_limit": "5x",
                    "leverage": str((D(pos["quantity"])*D(pos["entry_price"])/equity).quantize(D("0.01"))) if pos and equity is not None and equity > 0 else None,
                    "valuation_status": "FRESH" if valid_mark else "UNVALUED_STALE_OR_UNAVAILABLE_QUOTE"}

    def fills(self):
        with self._connect() as db: return [dict(r) for r in db.execute("SELECT * FROM g_fills ORDER BY occurred_at,fill_id")]

    def pairs(self):
        with self._connect() as db: return [dict(r) for r in db.execute("SELECT * FROM g_pairs ORDER BY opened_at")]

    def realized(self):
        with self._connect() as db: return str(sum((D(r[0]) for r in db.execute("SELECT pnl FROM g_fills WHERE effect IN ('CLOSE','PARTIAL_CLOSE')")), D("0")))

    def fees(self):
        with self._connect() as db: return str(sum((D(r[0]) for r in db.execute("SELECT fee FROM g_fills")), D("0")))

    def _record_reject(self, source_id, trade_id, reason):
        with self._lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            result = {"accepted": False, "reason": reason}
            self._record_event(db, source_id, trade_id, "REJECTED", reason, result)
            db.execute("UPDATE g_account SET error=?,last_cycle=?,last_action='C_EVENT_REJECTED' WHERE singleton=1", (reason, self._now()))
            db.commit(); return result

    def _abort_record(self, db, source_id, trade_id, reason):
        db.rollback()
        return self._record_reject(source_id, trade_id, reason)

    def _record_event(self, db, source_id, trade_id, status, reason, result):
        db.execute("INSERT OR IGNORE INTO g_events VALUES(?,?,?,?,?,?)", (source_id, trade_id, status, reason, json.dumps(result), self._now()))

    def _insert_fill(self, db, event, g_trade, effect, direction, qty, price, fee, slip, pnl, reason, quote):
        fill = str(uuid4())
        db.execute("INSERT INTO g_fills(fill_id,source_fill_id,c_trade_id,g_trade_id,effect,direction,quantity,price,fee,slippage,pnl,reason,occurred_at,quote_id,decision_id,c_fill_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (fill, event["source_fill_id"], event["c_trade_id"], g_trade, effect, direction, str(qty), str(price), str(fee), str(slip), str(pnl), reason,
                    self._now(), quote.get("event_id"), event.get("decision_id"), event.get("c_fill_at")))
        return fill

    def _fill(self, side, qty, quote):
        base = D(str(quote["ask"] if side == "BUY" else quote["bid"]))
        slip_rate = self.cost.adverse_slippage_bps / D("10000")
        raw = base * (D("1") + slip_rate if side == "BUY" else D("1") - slip_rate)
        price = ceil_step(raw, self.cost.simulated_tick) if side == "BUY" else floor_step(raw, self.cost.simulated_tick)
        fee = self._money(qty*price*self.cost.fee_rate, up=True)
        return price, fee, abs(price-base)*qty

    def _emergency_stop(self, direction, price, qty, equity, entry_fee):
        # Find the furthest valid tick whose conservative loss reserve stays within 2%.
        step = self.cost.simulated_tick
        max_distance = price-step if direction == "LONG" else equity*D("0.02")/qty
        max_ticks = int(max_distance/step)
        lo, hi, best = 1, max_ticks, None
        while lo <= hi:
            mid = (lo+hi)//2
            raw = price-step*mid if direction == "LONG" else price+step*mid
            stop = floor_step(raw, step) if direction == "LONG" else ceil_step(raw, step)
            risk = planned_loss_for_fill(qty, price, stop, self.cost, entry_fee)
            if risk <= equity*D("0.02"):
                best = stop
                lo = mid+1
            else:
                hi = mid-1
        if best is None:
            raise ValueError("RISK_BUDGET_TOO_SMALL_FOR_EMERGENCY_STOP")
        return best

    def _valid_quote(self, quote):
        try:
            at = datetime.fromisoformat(str(quote.get("received_at") or quote["event_at"]).replace("Z", "+00:00"))
            now = datetime.now(UTC)
            quote_age_ms = (now-at.astimezone(UTC)).total_seconds()*1000
            return (at.tzinfo is not None and 0 <= quote_age_ms <= self.cost.max_quote_age_ms
                    and quote.get("source") == "BINANCE_USDM_PERP" and quote.get("symbol") == "BTC/USD" and quote.get("currency") == "USD_RESEARCH"
                    and quote.get("timestamp_valid") is True and 0 <= int(quote.get("age_ms", 10**9)) <= self.cost.max_quote_age_ms
                    and D(str(quote["bid"])) > 0 and D(str(quote["ask"])) >= D(str(quote["bid"])))
        except (ValueError, TypeError, KeyError): return False

    @staticmethod
    def _reverse(direction): return "SHORT" if direction == "LONG" else "LONG"

    @staticmethod
    def _equity(cash, pos, quote):
        if not pos: return cash
        mark = D(str(quote["bid"] if pos["direction"] == "LONG" else quote["ask"]))
        return cash + (mark-D(pos["entry_price"]))*D(pos["quantity"])*(1 if pos["direction"] == "LONG" else -1)

