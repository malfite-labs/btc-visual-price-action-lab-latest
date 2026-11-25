"""Continuous fixed-copy bridge. An injected broker permits offline acceptance."""
from dataclasses import asdict
from datetime import UTC, datetime
from decimal import Decimal
from hashlib import sha256
import json
from types import SimpleNamespace
from contextlib import closing
from pathlib import Path
import sqlite3

from scripts.live_copy_demo import prepare_entry
from scripts.live_c_demo_executor import DemoExecutor
from scripts.live_copy_v2 import paper_fills


def opening_block(follower, fill, command):
    action = command() if command else 'run'
    if action in ('pause','stop'):
        return 'MANUAL_PAUSE' if action == 'pause' else 'STOP_PENDING'
    age = (datetime.now(UTC)-datetime.fromisoformat(fill.occurred_at)).total_seconds()
    if not 0 <= age <= 60:
        return 'FILL_OUTSIDE_60_SECOND_WINDOW'
    cursor = fill.cursor
    while later := paper_fills(follower.source,cursor,strategy=follower.strategy):
        if any(row.trade_id == fill.trade_id and row.action == 'FULL_CLOSE' for row in later):
            return 'PAPER_POSITION_ALREADY_CLOSED'
        cursor = later[-1].cursor
    return None


class FollowExecutor:
    def __init__(self, follower, exchange, directory, account, *, leverage, stop_fraction, risk_cap,
                 target_fraction=None, exit_mode='LEGACY_BRACKET'):
        self.follower, self.exchange, self.directory, self.account = follower, exchange, directory, account
        self.settings = dict(leverage=leverage, stop_fraction=stop_fraction,risk_cap=risk_cap,
                             target_fraction=target_fraction,exit_mode=exit_mode)
        with follower.connect() as db, db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS linked_orders(trade TEXT PRIMARY KEY, intent TEXT, tick TEXT);
                CREATE TABLE IF NOT EXISTS executed_events(event TEXT PRIMARY KEY, result TEXT);
                CREATE TABLE IF NOT EXISTS skipped_event_details(
                    event TEXT PRIMARY KEY, reason TEXT NOT NULL, decided_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS entry_timings(
                    event TEXT PRIMARY KEY, filled_at TEXT NOT NULL, discovered_at TEXT,
                    entered_at TEXT NOT NULL, discovery_delay_seconds REAL, execution_delay_seconds REAL NOT NULL);
            """)

    def can_stop(self):
        """Confirm local ownership and exchange flatness without submitting orders."""
        with self.follower.connect() as db:
            linked = {json.loads(raw)['client_order_id'] for raw, in db.execute(
                'SELECT intent FROM linked_orders')}
        ledger = Path(self.directory)/'c-v31-demo-execution.sqlite3'
        if ledger.exists():
            with closing(sqlite3.connect(ledger.as_uri()+'?mode=ro',uri=True)) as db:
                cycles = dict(db.execute('SELECT id,state FROM cycles'))
                writes = db.execute('SELECT count(*) FROM writes').fetchone()[0]
        else:
            cycles, writes = {}, 0
        if set(cycles) != linked or (writes and not cycles):
            raise RuntimeError('STOP_LEDGER_OWNERSHIP_MISMATCH')
        self.exchange.synchronize()
        exposure = self.exchange.exposure()
        if any(state not in ('CLOSED','NO_FILL') for state in cycles.values()):
            return False
        if any(exposure):
            raise RuntimeError('STOP_UNASSOCIATED_EXPOSURE')
        return True

    def tick(self, *, paused=False, pause_reason=None, command=None):
        with self.follower.connect() as db:
            links = db.execute('SELECT trade,intent,tick FROM linked_orders').fetchall()
        active = {}
        for trade, raw, tick in links:
            intent = DemoExecutor._intent({'intent':json.loads(raw)})
            engine = DemoExecutor(self.exchange,self.directory,self.account,tick)
            if intent in engine.active():
                result = engine.reconcile(intent,bubble_query_error=True)
                if result not in ('PROTECTED','FOLLOW_PAPER','CLOSED','NO_FILL'):
                    raise RuntimeError(result)
                if result in ('PROTECTED','FOLLOW_PAPER'):
                    active[trade] = (engine,intent)
            else:
                with engine.connection() as db:
                    if db.execute('SELECT 1 FROM cycles WHERE id=?',(intent.client_order_id,)).fetchone() is None:
                        raise RuntimeError('UNSUBMITTED_INTENT_REQUIRES_RECONCILIATION')
        if not active and any(self.exchange.exposure()):
            raise RuntimeError('UNASSOCIATED_EXPOSURE')
        self.follower.poll(paused=paused)
        if paused:
            watermark = paper_fills(self.follower.source,strategy=self.follower.strategy)
            while self.follower.cursor() < watermark:
                self.follower.poll(paused=True)
        with self.follower.connect() as db:
            rows = db.execute("""SELECT i.event_id,i.payload,i.result FROM inbox i
                LEFT JOIN executed_events e ON e.event=i.event_id
                WHERE i.status='PLANNED' AND e.event IS NULL ORDER BY i.cursor""").fetchall()
        completed = []
        for event, raw, planned in rows:
            fill, plan = SimpleNamespace(**json.loads(raw)),json.loads(planned)
            result = 'SKIPPED'
            entered_at = datetime.now(UTC)
            reason = plan.get('reason')
            if fill.action == 'OPEN':
                filled_at = datetime.fromisoformat(fill.occurred_at)
                discovered_at = getattr(fill,'discovered_at',None)
                with self.follower.connect() as db, db:
                    db.execute('INSERT OR IGNORE INTO entry_timings VALUES (?,?,?,?,?,?)',
                        (event,fill.occurred_at,discovered_at,entered_at.isoformat(),
                         (datetime.fromisoformat(discovered_at)-filled_at).total_seconds() if discovered_at else None,
                         (entered_at-filled_at).total_seconds()))
            if plan['action']=='OPEN' and not paused:
                link = next((link for link in links if link[0]==fill.trade_id),None)
                if link:
                    result = 'RECOVERED'
                else:
                    reason = 'ACTIVE_FOLLOWER_POSITION' if active else opening_block(self.follower,fill,command)
                if not link and not reason:
                    token = sha256(f'{self.account}|{event}'.encode()).hexdigest()[:28]
                    try:
                        intent,tick = prepare_entry(self.exchange,fill,self.account,token,
                            fixed=Decimal(plan['planned_margin']),budget=self.follower.budget,**self.settings)
                    except ValueError as exc:
                        if str(exc) != 'SKIPPED_MIN_ORDER':
                            raise
                        result = str(exc)
                    else:
                        def before_submit():
                            nonlocal reason
                            reason = opening_block(self.follower,fill,command)
                            if reason:
                                return False
                            with self.follower.connect() as db, db:
                                db.execute('INSERT INTO linked_orders VALUES (?,?,?)',
                                    (fill.trade_id,json.dumps(asdict(intent),default=str),str(tick)))
                            return True

                        engine = DemoExecutor(self.exchange,self.directory,self.account,tick)
                        outcome = engine.accept(intent,now=datetime.now(UTC),before_submit=before_submit)
                        if outcome in ('PROTECTED','FOLLOW_PAPER'):
                            active[fill.trade_id] = (engine,intent)
                            result = outcome
                        elif outcome == 'NO_FILL':
                            result = outcome
                        elif outcome != 'ENTRY_BLOCKED':
                            raise RuntimeError(outcome)
            elif plan['action']=='FULL_CLOSE' and fill.trade_id in active:
                engine,intent = active[fill.trade_id]
                result = engine.exit_demo_position(intent)
                if result != 'CLOSED':
                    raise RuntimeError(result)
                del active[fill.trade_id]
            elif plan['action']=='OPEN':
                reason = 'PAUSED'
            elif plan['action']=='FULL_CLOSE':
                reason = 'NO_ACTIVE_LINKED_POSITION'
            if (result=='SKIPPED' and reason=='NO_ASSOCIATED_POSITION'
                    and fill.action in ('FULL_CLOSE','PARTIAL_CLOSE')
                    and not any(link[0]==fill.trade_id for link in links)):
                reason = 'ORIGINAL_OPEN_NOT_FOLLOWED'
            with self.follower.connect() as db, db:
                db.execute('INSERT INTO executed_events VALUES (?,?)',(event,result))
                if result in ('SKIPPED','SKIPPED_MIN_ORDER'):
                    if reason == 'PAUSED':
                        reason = pause_reason or 'PAUSED'
                    db.execute('INSERT INTO skipped_event_details VALUES (?,?,?)',
                        (event,reason or ('MINIMUM_ORDER_REQUIREMENTS' if result=='SKIPPED_MIN_ORDER'
                                         else 'UNSUPPORTED_EXECUTION_ACTION:'+plan['action']),datetime.now(UTC).isoformat()))
            completed.append((event,result))
        return completed
