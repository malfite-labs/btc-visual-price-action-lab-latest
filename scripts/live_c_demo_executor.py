"""Recoverable C Demo order lifecycle, independent of desktop and paper storage."""
from contextlib import closing
from dataclasses import asdict
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from hashlib import sha256
import json
import sqlite3

from scripts.live_c_demo_exchange import DemoApiError, SYMBOL
from scripts.live_c_intent import EntryIntent

TERMINAL = {'FILLED', 'CANCELED', 'EXPIRED', 'EXPIRED_IN_MATCH', 'REJECTED'}


class DemoExecutor:
    def __init__(self, exchange, root, account_id, tick):
        self.exchange, self.account_id, self.tick = exchange, account_id, Decimal(tick)
        if not account_id or not self.tick.is_finite() or self.tick <= 0:
            raise ValueError('Invalid Demo account or tick')
        self.path = root / 'c-v31-demo-execution.sqlite3'
        if self.path.is_symlink() or (self.path.exists() and self.path.stat().st_nlink != 1):
            raise ValueError('Unsafe Demo ledger path')
        with self.connection() as db, db:
            db.execute('CREATE TABLE IF NOT EXISTS bindings(account TEXT PRIMARY KEY)')
            db.execute('CREATE TABLE IF NOT EXISTS cycles(id TEXT PRIMARY KEY, state TEXT, evidence TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS writes(id TEXT PRIMARY KEY)')
            db.execute('INSERT OR IGNORE INTO bindings VALUES (?)', (account_id,))
            if db.execute('SELECT COUNT(*) FROM bindings').fetchone()[0] != 1:
                raise ValueError('Ledger belongs to another Demo account')

    def connection(self):
        return closing(sqlite3.connect(self.path, timeout=5))

    def _set(self, order_id, state, evidence):
        with self.connection() as db, db:
            db.execute('UPDATE cycles SET state=?, evidence=? WHERE id=?',
                       (state, json.dumps(evidence, default=str), order_id))
        return state

    def _once(self, action_id, action):
        with self.connection() as db, db:
            claimed = db.execute('INSERT OR IGNORE INTO writes VALUES (?)', (action_id,)).rowcount
        if claimed:
            try:
                action()
            except DemoApiError:
                # A failed response proves neither acceptance nor rejection.
                pass

    def active(self):
        with self.connection() as db:
            rows = db.execute("SELECT evidence FROM cycles WHERE state NOT IN ('CLOSED','NO_FILL')").fetchall()
        return [self._intent(json.loads(row[0])) for row in rows]

    @staticmethod
    def _intent(evidence):
        fields = evidence['intent']
        return EntryIntent(**{k: Decimal(v) if v is not None and k in {'quantity','reference_price','stop','original_stop','target_fraction'} else v
                              for k,v in fields.items()})

    def accept(self, intent, *, now, before_submit=None):
        from datetime import datetime, timedelta
        if (intent.account_id != self.account_id or now.utcoffset() is None
                or not datetime.fromisoformat(intent.cycle_id) <= now <
                       datetime.fromisoformat(intent.cycle_id)+timedelta(minutes=15)):
            return 'STALE_OR_WRONG_ACCOUNT'
        self.exchange.synchronize()
        net, orders, algos = self.exchange.exposure()
        if net or orders or algos:
            return 'ACCOUNT_CONFLICT'
        if before_submit is not None and not before_submit():
            return 'ENTRY_BLOCKED'
        order_id = intent.client_order_id
        with self.connection() as db, db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute("SELECT 1 FROM cycles WHERE state NOT IN ('CLOSED','NO_FILL')").fetchone():
                return 'RECONCILE_REQUIRED'
            if not db.execute('INSERT OR IGNORE INTO cycles VALUES (?, ?, ?)',
                              (order_id, 'ENTRY_UNKNOWN', json.dumps({'intent':asdict(intent)}, default=str))).rowcount:
                return 'DUPLICATE'
        self._once(order_id, lambda: self.exchange.entry(intent))
        return self.reconcile(intent)

    def _query_algo(self, client_id):
        try:
            return self.exchange.protection(client_id)
        except DemoApiError as exc:
            if exc.code == -2013:
                return None
            raise

    def _protect(self, intent, side, kind, quantity, trigger, *, verify_only=False):
        # The conditional request identifier is a different namespace to the normal order.
        client_id = 'c31a-' + sha256(f'{intent.client_order_id}|{kind}|{quantity}|{trigger}'.encode()).hexdigest()[:28]
        evidence = self._query_algo(client_id)
        if evidence is None and not verify_only:
            self._once(client_id, lambda: self.exchange.place_protection(
                client_id, side=side, kind=kind, trigger=trigger, quantity=quantity))
            evidence = self._query_algo(client_id)
        if evidence is None:
            return None
        if (evidence.get('clientAlgoId') != client_id or evidence.get('algoStatus') != 'NEW'
                or evidence.get('symbol') != SYMBOL or evidence.get('side') != side
                or evidence.get('positionSide') != (intent.direction if getattr(self.exchange,'hedge',False) else 'BOTH')
                or evidence.get('orderType') != kind
                or (not getattr(self.exchange,'hedge',False) and evidence.get('reduceOnly') is not True)
                or Decimal(evidence['quantity']) != quantity or Decimal(evidence['triggerPrice']) != trigger
                or not evidence.get('algoId')):
            return None
        return {'algo_id': evidence['algoId'], 'client_algo_id': client_id, 'type': kind, 'trigger': str(trigger)}

    def reconcile(self, intent, *, verify_only=False, bubble_query_error=False):
        oid = intent.client_order_id
        with self.connection() as db:
            row = db.execute('SELECT state,evidence FROM cycles WHERE id=?', (oid,)).fetchone()
        if not row:
            raise ValueError('Unknown cycle')
        if row[0] in ('CLOSED','NO_FILL'):
            return row[0]
        evidence = json.loads(row[1])
        evidence.pop('failure', None)
        phase = 'server_time'

        def failure(state, reason, **details):
            evidence['failure'] = {'phase': phase, 'reason': reason, **details}
            return self._set(oid, state, evidence)

        try:
            self.exchange.synchronize()
            phase = "entry_order"
            order = self.exchange.order(oid)
            expected_side = 'BUY' if intent.direction == 'LONG' else 'SELL'
            if (order.get('clientOrderId') != oid or order.get('symbol') != SYMBOL
                    or order.get('side') != expected_side
                    or order.get('positionSide') != (intent.direction if getattr(self.exchange,'hedge',False) else 'BOTH')
                    or Decimal(order['origQty']) != intent.quantity):
                return failure('RECONCILE_REQUIRED', 'Entry order identity or original quantity mismatch')
            phase = "entry_quantity"
            qty = Decimal(order['executedQty'])
            if not 0 <= qty <= intent.quantity:
                return failure('RECONCILE_REQUIRED', 'Executed quantity out of range')
            phase = "account_exposure"
            net, orders, algos = self.exchange.exposure()
            terminal = order['status'] in TERMINAL
            evidence['exposure'] = {'net': str(net), 'orders':len(orders), 'algos':len(algos)}
            evidence['order_id'] = order['orderId']
            evidence['entry_status'] = order['status']
            evidence['filled'] = str(qty)
            if not qty:
                state = 'NO_FILL' if terminal and not net and not orders and not algos else 'RECONCILE_REQUIRED'
                return self._set(oid, state, evidence)
            phase = "entry_trades"
            trades = self.exchange.trades(order['orderId'])
            if (len({t['id'] for t in trades}) != len(trades)
                    or any(t['orderId'] != order['orderId'] or t['symbol'] != SYMBOL for t in trades)
                    or sum(Decimal(t['qty']) for t in trades) != qty):
                return failure('RECONCILE_REQUIRED', 'Trade identifiers or summed quantity mismatch')
            average = sum(Decimal(t['qty']) * Decimal(t['price']) for t in trades) / qty
            evidence.update(average=str(average), entry_trades=trades)
            sign = 1 if intent.direction == 'LONG' else -1
            if not net and terminal and not orders:
                if verify_only:
                    return failure('FLAT', 'Position already flat; closure evidence requires reconciliation')
                return self._closed(oid, evidence, qty, algos)
            phase = 'ownership'
            owned = {evidence[k]['client_algo_id'] for k in ('stop','target','retiring_stop') if k in evidence}
            if any(a.get('clientAlgoId') not in owned for a in algos) and owned:
                return failure('RECONCILE_REQUIRED', 'Unexpected conditional order')
            if net != qty*sign or any(o.get('clientOrderId') != oid for o in orders):
                return failure('RECONCILE_REQUIRED', 'Position or open entry order mismatch')
            if intent.exit_mode == 'FOLLOW_PAPER':
                if any(a.get('clientAlgoId') not in owned for a in algos):
                    return failure('RECONCILE_REQUIRED', 'Unexpected conditional order')
                side = 'SELL' if sign == 1 else 'BUY'
                target = None
                if intent.target_fraction is not None:
                    raw = average * (1 + sign * intent.target_fraction)
                    rounding = ROUND_CEILING if sign == 1 else ROUND_FLOOR
                    target = (raw / self.tick).to_integral_value(rounding=rounding)*self.tick
                    if target <= 0 or (target-average)*sign <= 0:
                        return failure('PROTECTION_INCOMPLETE','Invalid target')
                if intent.stop is not None and (average-intent.stop)*sign <= 0:
                    return failure('PROTECTION_INCOMPLETE','Fill crossed configured stop')
                for label,kind,trigger in (('stop','STOP_MARKET',intent.stop),('target','TAKE_PROFIT_MARKET',target)):
                    if trigger is None:
                        continue
                    phase = label
                    previous = evidence.get(label)
                    if previous and Decimal(previous['quantity']) != qty:
                        return failure('PROTECTION_INCOMPLETE','Confirmed quantity changed')
                    protection = self._protect(intent,side,kind,qty,trigger,verify_only=verify_only or bool(previous))
                    if not protection or (previous and str(protection['algo_id']) != str(previous['algo_id'])):
                        return failure('PROTECTION_INCOMPLETE','Configured protection could not be verified')
                    evidence[label] = {**protection,'quantity':str(qty)}
                    self._set(oid,'PROTECTION_REQUIRED',evidence)
                if not terminal:
                    return failure('RECONCILE_REQUIRED','Entry not terminal')
                return self._set(oid,'PROTECTED' if intent.stop is not None else 'FOLLOW_PAPER',evidence)
            phase = 'stop'
            side = 'SELL' if intent.direction == 'LONG' else 'BUY'
            tp = intent.take_profit(average)
            rounding = ROUND_CEILING if sign == 1 else ROUND_FLOOR
            tp = (tp / self.tick).to_integral_value(rounding=rounding)*self.tick
            previous_stop = evidence.get('stop')
            if previous_stop and Decimal(previous_stop['quantity']) != qty:
                if verify_only:
                    return failure('PROTECTION_INCOMPLETE', 'Confirmed position differs from saved protection quantity')
                evidence['retiring_stop'] = previous_stop
                self._set(oid, 'PROTECTION_REQUIRED', evidence)
            stop = self._protect(intent, side, 'STOP_MARKET', qty, intent.stop, verify_only=verify_only)
            if not stop:
                return failure('PROTECTION_INCOMPLETE' if verify_only else 'PROTECTION_REQUIRED', 'Stop missing or does not match')
            if verify_only and (not previous_stop or str(stop['algo_id']) != str(previous_stop['algo_id'])):
                return failure('PROTECTION_INCOMPLETE', 'Exchange stop ID differs from saved stop ID')
            evidence['stop'] = {**stop, 'quantity':str(qty)}
            self._set(oid, 'PROTECTION_REQUIRED', evidence)
            retiring = evidence.get('retiring_stop')
            if retiring:
                if verify_only:
                    return failure('PROTECTION_INCOMPLETE', 'Previous protection still pending retirement')
                cid = retiring['client_algo_id']
                self._once('cancel:'+cid, lambda: self.exchange.cancel_protection(cid))
                net_now, _, open_algos = self.exchange.exposure()
                if net_now != qty*sign or any(a.get('clientAlgoId') == cid for a in open_algos):
                    return self._set(oid, 'PROTECTION_REQUIRED', evidence)
                evidence.pop('retiring_stop')
                self._set(oid, 'PROTECTION_REQUIRED', evidence)
            if not terminal:
                return self._set(oid, 'PARTIAL_PROTECTED', evidence)
            phase = 'take_profit'
            target = self._protect(intent, side, 'TAKE_PROFIT_MARKET', qty, tp, verify_only=verify_only)
            if not target:
                return failure('PROTECTION_INCOMPLETE' if verify_only else 'PROTECTION_REQUIRED', 'Take profit missing or does not match')
            previous_target = evidence.get('target')
            if verify_only and (not previous_target or str(target['algo_id']) != str(previous_target['algo_id'])):
                return failure('PROTECTION_INCOMPLETE', 'Exchange target ID differs from saved target ID')
            evidence['target'] = target
            return self._set(oid, 'PROTECTED', evidence)
        except DemoApiError as exc:
            result = failure('EXCHANGE_UNKNOWN', str(exc), exchange_code=exc.code)
            if bubble_query_error and exc.method=='GET':
                raise
            return result
        except (ValueError, TypeError, KeyError, ArithmeticError) as exc:
            return failure('RECONCILE_REQUIRED', type(exc).__name__)


    def _closed(self, oid, evidence, qty, algos):
        known = {evidence[k]['client_algo_id'] for k in ('stop','target','retiring_stop') if k in evidence}
        if any(a.get('clientAlgoId') not in known for a in algos):
            return self._set(oid, 'RECONCILE_REQUIRED', evidence)
        exit_ids = set()
        for cid in known:
            order = self._query_algo(cid)
            if order and order.get('actualOrderId') and int(order['actualOrderId']) > 0:
                exit_ids.add(int(order['actualOrderId']))
        if evidence.get('manual_exit'):
            exit_ids.add(self.exchange.order(evidence['manual_exit'])['orderId'])
        trades = [trade for order_id in exit_ids for trade in self.exchange.trades(order_id)]
        if (sum(Decimal(trade['qty']) for trade in trades) != qty
                or len({trade['id'] for trade in trades}) != len(trades)
                or any(trade['symbol'] != SYMBOL or trade['side'] ==
                       ('BUY' if evidence['intent']['direction']=='LONG' else 'SELL') for trade in trades)):
            return self._set(oid, 'RECONCILE_REQUIRED', evidence)
        for algo in algos:
            cid = algo['clientAlgoId']
            self._once('cancel:'+cid, lambda: self.exchange.cancel_protection(cid))
        net, orders, remaining = self.exchange.exposure()
        if net or orders or remaining:
            return self._set(oid, 'RECONCILE_REQUIRED', evidence)
        evidence['exit_trades'] = trades
        entry_value = sum(Decimal(t['price'])*Decimal(t['qty']) for t in evidence['entry_trades'])
        exit_value = sum(Decimal(t['price'])*Decimal(t['qty']) for t in trades)
        gross = (exit_value-entry_value)*(1 if evidence['intent']['direction']=='LONG' else -1)
        fees = {}
        for trade in evidence['entry_trades']+trades:
            asset = trade['commissionAsset']
            fees[asset] = fees.get(asset, Decimal(0)) + Decimal(trade['commission'])
        evidence['gross_pnl_usdt'] = str(gross)
        evidence['commissions_by_asset'] = {asset:str(fee) for asset,fee in fees.items()}
        evidence['net_pnl_before_funding_usdt'] = str(gross-fees.get('USDT',Decimal(0))) if set(fees)<= {'USDT'} else None
        evidence['account'] = {k:v for k,v in self.exchange.account().items()
                               if k in ('availableBalance','totalWalletBalance','totalMarginBalance')}
        return self._set(oid, 'CLOSED', evidence)

    def exit_demo_position(self, intent):
        """Explicit Demo-only reduce operation, including protected test exit."""
        oid = intent.client_order_id
        self.exchange.synchronize()
        net, orders, _ = self.exchange.exposure()
        with self.connection() as db:
            row = db.execute('SELECT evidence FROM cycles WHERE id=?', (oid,)).fetchone()
        if not row:
            raise ValueError('Unknown cycle')
        e = json.loads(row[0])
        if not net:
            return self.reconcile(intent)
        order = self.exchange.order(oid)
        if orders or order['status'] not in TERMINAL or net != Decimal(order['executedQty'])*(1 if intent.direction=='LONG' else -1):
            return self._set(oid, 'RECONCILE_REQUIRED', e)
        cid = 'c31x-' + sha256(oid.encode()).hexdigest()[:28]
        e['manual_exit'] = cid
        self._set(oid, 'RECONCILE_REQUIRED', e)
        self._once(cid, lambda: self.exchange.reduce(cid, 'SELL' if net > 0 else 'BUY', abs(net)))
        return self.reconcile(intent)
