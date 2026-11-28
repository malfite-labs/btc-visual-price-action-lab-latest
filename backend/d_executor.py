"""D execution safety checks; order dispatch remains disabled pending integration."""
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation


def validate_quote(quote, now, max_age_ms=60000):
    """Validate provider identity and both event and receive timestamps."""
    try:
        if (quote.get('symbol') != 'BTC/USD' or quote.get('source') != 'BINANCE_USDM_PERP'
                or quote.get('currency') != 'USD_RESEARCH'):
            return False
        if quote.get('timestamp_valid') is not True or now.tzinfo is None:
            return False
        event = datetime.fromisoformat(quote['event_at'].replace('Z', '+00:00'))
        received = datetime.fromisoformat(quote['received_at'].replace('Z', '+00:00'))
        if event.tzinfo is None or received.tzinfo is None:
            return False
        now = now.astimezone(timezone.utc)
        event = event.astimezone(timezone.utc)
        received = received.astimezone(timezone.utc)
        bid, ask = Decimal(str(quote['bid'])), Decimal(str(quote['ask']))
        return (bid.is_finite() and ask.is_finite() and 0 < bid <= ask
                and event <= received <= now
                and 0 <= (now - event).total_seconds() * 1000 <= max_age_ms
                and 0 <= (now - received).total_seconds() * 1000 <= max_age_ms
                and bool(quote.get('event_id')))
    except (AttributeError, KeyError, TypeError, ValueError, InvalidOperation):
        return False

class IntentStore:
    """Durable once-only candidate capture with full quote evidence.

    Order execution is intentionally not enabled here. A caller must reconcile
    committed positions and circuit state before marking any intent consumed.
    """
    def __init__(self, path):
        import sqlite3
        self.path = str(path)
        with sqlite3.connect(self.path, timeout=10) as db:
            db.execute('''CREATE TABLE IF NOT EXISTS d_execution_intents
                (signal_id TEXT PRIMARY KEY, signal_json TEXT NOT NULL,
                 quote_json TEXT NOT NULL, status TEXT NOT NULL)''')

    def capture(self, signal, quote, now):
        import json
        import sqlite3
        if not validate_quote(quote, now):
            return {'accepted': False, 'reason': 'INVALID_QUOTE'}
        signal_id = signal.get('signal_close')
        try:
            close = datetime.fromisoformat(signal_id.replace('Z', '+00:00'))
            event = datetime.fromisoformat(quote['event_at'].replace('Z', '+00:00'))
            if close.tzinfo is None or not 0 <= (event-close).total_seconds() < 900:
                return {'accepted': False, 'reason': 'CANDIDATE_EXPIRED'}
            if not 0 <= (now-close).total_seconds() < 900:
                return {'accepted': False, 'reason': 'CANDIDATE_EXPIRED'}
        except (AttributeError, TypeError, ValueError):
            return {'accepted': False, 'reason': 'INVALID_SIGNAL'}
        sj = json.dumps(signal, sort_keys=True, separators=(',', ':'))
        qj = json.dumps(quote, sort_keys=True, separators=(',', ':'))
        with sqlite3.connect(self.path, timeout=10) as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT signal_json,quote_json,status FROM d_execution_intents WHERE signal_id=?', (signal_id,)).fetchone()
            if old:
                if old[0] != sj:
                    return {'accepted': False, 'reason': 'SIGNAL_ID_CONFLICT'}
                return {'accepted': True, 'duplicate': True, 'status': old[2]}
            db.execute('INSERT INTO d_execution_intents VALUES (?,?,?,?)', (signal_id,sj,qj,'PENDING'))
            return {'accepted': True, 'duplicate': False, 'status': 'PENDING'}

    def pending(self):
        """Return durable intent and original execution evidence after restart."""
        import json
        import sqlite3
        with sqlite3.connect(self.path, timeout=10) as db:
            rows = db.execute("SELECT signal_id,signal_json,quote_json,status FROM d_execution_intents WHERE status='PENDING' ORDER BY signal_id").fetchall()
        return [{'signal_id': row[0], 'signal': json.loads(row[1]),
                 'quote': json.loads(row[2]), 'status': row[3]} for row in rows]

    def resolve(self, signal_id, expected_status, outcome):
        """CAS terminal intent state; errors never cause a new order attempt."""
        import sqlite3
        if expected_status != 'PENDING' or outcome not in ('FILLED', 'REJECTED', 'EXPIRED'):
            raise ValueError('INVALID_INTENT_TRANSITION')
        with sqlite3.connect(self.path, timeout=10) as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT status FROM d_execution_intents WHERE signal_id=?', (signal_id,)).fetchone()
            if row is None:
                return {'accepted': False, 'reason': 'INTENT_NOT_FOUND'}
            if row[0] == outcome:
                return {'accepted': True, 'duplicate': True}
            if row[0] != expected_status:
                return {'accepted': False, 'reason': 'INTENT_ALREADY_RESOLVED'}
            db.execute('UPDATE d_execution_intents SET status=? WHERE signal_id=?', (outcome, signal_id))
            return {'accepted': True, 'duplicate': False}

    def reconcile(self, signal_id, now):
        """Resolve committed trades or expire stale attempts without placing orders.

        The intent table must reside in the same database as d_used_signals.
        """
        import json
        import sqlite3
        with sqlite3.connect(self.path, timeout=10) as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT quote_json,status FROM d_execution_intents WHERE signal_id=?', (signal_id,)).fetchone()
            if not row:
                return {'status': 'NOT_FOUND'}
            if row[1] != 'PENDING':
                return {'status': row[1]}
            try:
                trade = db.execute('SELECT trade_id FROM d_used_signals WHERE signal_id=?', (signal_id,)).fetchone()
            except sqlite3.Error:
                return {'status': 'BLOCKED', 'reason': 'LEDGER_IDENTITY_UNAVAILABLE'}
            if trade:
                db.execute("UPDATE d_execution_intents SET status='FILLED' WHERE signal_id=?", (signal_id,))
                return {'status': 'FILLED', 'trade_id': trade[0]}
            quote = json.loads(row[0])
            if not validate_quote(quote, now):
                db.execute("UPDATE d_execution_intents SET status='EXPIRED' WHERE signal_id=?", (signal_id,))
                return {'status': 'EXPIRED', 'reason': 'QUOTE_STALE'}
            return {'status': 'PENDING', 'quote': quote}


def handoff_observed_intent(observation_path, intents, quote, now):
    """Capture a pending observed signal while holding the observation write lock.

    No order or source-state mutation occurs. Callers must reconcile and acknowledge
    in a later step. The capture remains safe to repeat after a process crash.
    """
    import json
    import sqlite3
    from pathlib import Path
    if Path(observation_path).resolve() == Path(intents.path).resolve():
        raise ValueError('INTENT_AND_OBSERVATION_STORES_MUST_DIFFER')
    with sqlite3.connect(str(observation_path), timeout=10) as db:
        db.execute('BEGIN IMMEDIATE')
        try:
            row = db.execute('SELECT payload FROM d_state WHERE id=1').fetchone()
            if not row:
                return {'accepted': False, 'reason': 'OBSERVATION_STATE_MISSING'}
            state = json.loads(row[0])
            signal = state.get('signal')
            if state.get('phase') != 'PENDING' or not isinstance(signal, dict):
                return {'accepted': False, 'reason': 'NO_PENDING_SIGNAL'}
            if signal.get('setup') != 'D_CONFIRM' or signal.get('direction') != 'LONG':
                return {'accepted': False, 'reason': 'INVALID_SIGNAL'}
            return intents.capture(signal, quote, now)
        finally:
            db.rollback()


def process_pending_entry(ledger, intents, signal_id, signal_bar, now, entry_gate=None):
    """Open one durable D intent in an isolated ledger, never refresh its quote.

    The first post-signal quote and the source candle are fixed evidence.
    Reconciliation checks committed signal IDs before any retry.
    """
    from datetime import timedelta
    from pathlib import Path
    from backend.d_margin import account_metrics
    from backend.d_risk import preflight_entry

    if Path(ledger.path).resolve() != Path(intents.path).resolve():
        return {'status': 'BLOCKED', 'reason': 'INTENT_LEDGER_MISMATCH'}
    pending = next((item for item in intents.pending() if item['signal_id'] == signal_id), None)
    reconciled = intents.reconcile(signal_id, now)
    if reconciled.get('status') != 'PENDING':
        return reconciled
    if pending is None:
        return {'status': 'BLOCKED', 'reason': 'INTENT_STATE_CHANGED'}
    signal, quote = pending['signal'], pending['quote']
    try:
        close = datetime.fromisoformat(signal_id.replace('Z', '+00:00'))
        candle_close = datetime.fromisoformat(signal_bar['close_at'].replace('Z', '+00:00'))
        candle_open = datetime.fromisoformat(signal_bar['open_at'].replace('Z', '+00:00'))
        valid_bar = (close.tzinfo is not None and candle_close.tzinfo is not None
                     and candle_open.tzinfo is not None and candle_close == close
                     and candle_close - candle_open == timedelta(minutes=15)
                     and signal_bar['symbol'] == 'BTC/USD' and signal_bar['interval'] == '15Min'
                     and signal_bar['source'] == 'BINANCE_USDM_PERP'
                     and signal_bar['currency'] == 'USD_RESEARCH' and signal_bar['is_closed'] is True
                     and Decimal(str(signal_bar['close'])) == Decimal(str(signal['close']))
                     and Decimal(str(signal_bar['volume'])).is_finite()
                     and Decimal(str(signal_bar['volume'])) > 0)
    except (KeyError, TypeError, ValueError, ArithmeticError):
        valid_bar = False
    if not valid_bar:
        return {'status': 'BLOCKED', 'reason': 'SIGNAL_CANDLE_MISMATCH'}
    if not validate_quote(quote, now):
        return {'status': 'BLOCKED', 'reason': 'QUOTE_STALE_OR_INVALID'}
    account = ledger.account()
    if account.get('position'):
        return {'status': 'BLOCKED', 'reason': 'POSITION_EXISTS'}
    metrics = account_metrics(account['cash'], None, quote['bid'])
    if not metrics.get('allow_open'):
        return {'status': 'BLOCKED', 'reason': metrics['status']}
    preflight = preflight_entry(signal, quote, metrics['equity'], metrics['available_cash'],
                                signal_bar['volume'], now)
    if not preflight.get('accepted'):
        resolved = intents.resolve(signal_id, 'PENDING', 'REJECTED')
        return {'status': 'REJECTED' if resolved.get('accepted') else 'BLOCKED',
                'reason': preflight.get('reason', 'PREFLIGHT_REJECTED')}
    queued = enqueue_verified_quote(ledger, quote, now)
    if not queued.get('accepted'):
        return {'status': 'BLOCKED', 'reason': queued.get('reason')}
    first = pending_verified_quote(ledger, now)
    if first.get('status') != 'READY' or first['quote']['event_id'] != quote['event_id']:
        return {'status': 'BLOCKED', 'reason': 'QUOTE_QUEUE_ORDER'}
    opened = ledger.open_position(quantity=preflight['quantity'], price=preflight['price'],
                                  stop=preflight['stop'], leverage=preflight['leverage'],
                                  idempotency_key='D-entry:' + signal_id,
                                  expected_version=account['version'], signal_id=signal_id,
                                  atr=signal['atr'], entry_bar_close=(close + timedelta(minutes=15)).isoformat(),
                                  quote_event_id=quote['event_id'], entry_gate=entry_gate)
    if not opened.get('accepted'):
        return {'status': 'BLOCKED', 'reason': opened.get('reason'), 'execution': opened}
    outcome = intents.reconcile(signal_id, now)
    if outcome.get('status') != 'FILLED':
        return {'status': 'BLOCKED', 'reason': 'OPEN_RECONCILIATION_PENDING', 'execution': opened}
    protected = process_verified_quote(ledger, now)
    return {'status': 'FILLED', 'execution': opened, 'protection': protected}


def enqueue_verified_quote(ledger, quote, now):
    """Persist quote and complete evidence atomically in the D ledger database.

    Existing queue rows with missing evidence stay unreadable by this interface.
    """
    import json
    import sqlite3
    if not validate_quote(quote, now):
        return {'accepted': False, 'reason': 'INVALID_QUOTE'}
    event_id = quote['event_id']
    event_at = datetime.fromisoformat(quote['event_at'].replace('Z', '+00:00')).astimezone(timezone.utc).isoformat()
    bid, ask = Decimal(str(quote['bid'])), Decimal(str(quote['ask']))
    evidence = json.dumps(quote, sort_keys=True, separators=(',', ':'), allow_nan=False)
    with sqlite3.connect(ledger.path, timeout=10) as db:
        db.execute('BEGIN IMMEDIATE')
        db.execute('CREATE TABLE IF NOT EXISTS d_quote_evidence (event_id TEXT PRIMARY KEY, payload TEXT NOT NULL)')
        prior = db.execute('SELECT event_at,bid,ask FROM d_quotes WHERE event_id=?', (event_id,)).fetchone()
        if prior:
            old = db.execute('SELECT payload FROM d_quote_evidence WHERE event_id=?', (event_id,)).fetchone()
            try:
                original = json.loads(old[0]) if old else None
                same_source = original is not None and all(
                    original.get(field) == quote.get(field)
                    for field in ('event_id', 'symbol', 'source', 'currency', 'timestamp_valid')
                )
                same_source = (same_source
                               and datetime.fromisoformat(original['event_at'].replace('Z', '+00:00')).astimezone(timezone.utc).isoformat() == prior[0]
                               and Decimal(str(original['bid'])) == Decimal(prior[1])
                               and Decimal(str(original['ask'])) == Decimal(prior[2]))
            except (ValueError, TypeError, KeyError, AttributeError, InvalidOperation):
                same_source = False
            if same_source and prior[0] == event_at and Decimal(prior[1]) == bid and Decimal(prior[2]) == ask:
                return {'accepted': True, 'duplicate': True}
            return {'accepted': False, 'reason': 'QUOTE_EVIDENCE_CONFLICT'}
        newest = db.execute('SELECT event_at FROM d_quotes ORDER BY seq DESC LIMIT 1').fetchone()
        if newest and event_at <= newest[0]:
            return {'accepted': False, 'reason': 'STALE_OR_CONFLICTING_QUOTE'}
        db.execute("INSERT INTO d_quotes(event_id,event_at,bid,ask,status) VALUES(?,?,?,?,'PENDING')",
                   (event_id,event_at,str(bid),str(ask)))
        db.execute('INSERT INTO d_quote_evidence VALUES(?,?)', (event_id,evidence))
        return {'accepted': True, 'duplicate': False}


def pending_verified_quote(ledger, now, *, allow_expired=False):
    """Return stored first quote only when its original evidence is still valid."""
    import json
    import sqlite3
    with sqlite3.connect(ledger.path, timeout=10) as db:
        try:
            # The evidence table is initialized lazily by the first queued
            # quote. Its absence is safe only when nothing awaits execution.
            if not db.execute("SELECT 1 FROM d_quotes WHERE status='PENDING' LIMIT 1").fetchone():
                return {'status':'EMPTY'}
            row = db.execute("SELECT q.event_id,e.payload,q.event_at,q.bid,q.ask FROM d_quotes q LEFT JOIN d_quote_evidence e ON e.event_id=q.event_id WHERE q.status='PENDING' ORDER BY q.seq LIMIT 1").fetchone()
        except sqlite3.Error:
            return {'status':'BLOCKED','reason':'EVIDENCE_UNAVAILABLE'}
    if not row:
        return {'status':'EMPTY'}
    if not row[1]:
        return {'status':'BLOCKED','reason':'EVIDENCE_UNAVAILABLE','event_id':row[0]}
    try:
        quote = json.loads(row[1])
    except (ValueError,TypeError):
        return {'status':'BLOCKED','reason':'INVALID_EVIDENCE','event_id':row[0]}
    try:
        received_at = datetime.fromisoformat(quote['received_at'].replace('Z', '+00:00'))
        originally_valid = validate_quote(quote, received_at)
    except (KeyError, AttributeError, TypeError, ValueError):
        originally_valid = False
    if not originally_valid:
        return {'status':'BLOCKED','reason':'QUOTE_STALE_OR_INVALID','event_id':row[0]}
    event_at = datetime.fromisoformat(quote['event_at'].replace('Z', '+00:00')).astimezone(timezone.utc).isoformat()
    if (quote['event_id'] != row[0] or event_at != row[2]
            or Decimal(str(quote['bid'])) != Decimal(row[3])
            or Decimal(str(quote['ask'])) != Decimal(row[4])):
        return {'status':'BLOCKED','reason':'EVIDENCE_MISMATCH','event_id':row[0]}
    if not validate_quote(quote,now):
        if allow_expired and now.tzinfo is not None and now >= received_at:
            return {'status':'EXPIRED','quote':quote}
        return {'status':'BLOCKED','reason':'QUOTE_STALE_OR_INVALID','event_id':row[0]}
    return {'status':'READY','quote':quote}


def plan_verified_position_event(account, quote, now):
    """Plan protection from fresh evidence; never place or alter an order."""
    from decimal import ROUND_DOWN
    from backend.d_margin import account_metrics
    from backend.d_position_policy import plan_position_event
    if not validate_quote(quote,now):
        return {'action':'BLOCKED','reason':'QUOTE_STALE_OR_INVALID'}
    position=account.get('position')
    metrics=account_metrics(account.get('cash'),position,quote['bid'])
    if metrics.get('status') == 'VALUATION_UNAVAILABLE':
        return {'action':'BLOCKED','reason':'VALUATION_UNAVAILABLE'}
    if not position:
        return {'action':'HOLD','reason':'FLAT'}
    if metrics.get('liquidation_due') or position.get('stop_due'):
        price=(Decimal(str(quote['bid'])) * Decimal('0.9998') / Decimal('0.01')).to_integral_value(rounding=ROUND_DOWN)*Decimal('0.01')
        return {'action':'LIQUIDATION' if metrics.get('liquidation_due') else 'STOP_DUE',
                'quantity':str(position['quantity']),'price':str(price)}
    return plan_position_event(position,quote,now)


def process_verified_quote(ledger, now):
    """Process one queued fresh quote, preserving retry state on any failure.

    Recovery of expired trigger quotes and cross-database daily-risk flattening
    remain external requirements. This function is not connected to production.
    """
    for _ in range(100):
        evidence = pending_verified_quote(ledger, now, allow_expired=True)
        if evidence['status'] != 'EXPIRED':
            break
        quote = evidence['quote']
        account = ledger.account()
        position = account.get('position')
        if position and Decimal(str(quote['bid'])) <= Decimal(position['stop_price']):
            marked = ledger.mark_stop_due(event_id=quote['event_id'],
                                          expected_version=account['version'],
                                          trade_id=position['trade_id'])
            if not marked.get('accepted'):
                return {'status':'BLOCKED','result':marked}
        completed = ledger.complete_quote(quote['event_id'])
        if not completed.get('accepted'):
            return {'status':'BLOCKED','result':completed}
    else:
        return {'status':'BLOCKED','reason':'EXPIRED_QUEUE_LIMIT'}
    if evidence['status'] != 'READY':
        return evidence
    quote = evidence['quote']
    event_id = quote['event_id']
    for _ in range(5):
        account = ledger.account()
        position = account.get('position')
        plan = plan_verified_position_event(account, quote, now)
        action = plan['action']
        if action == 'BLOCKED':
            return {'status':'BLOCKED','reason':plan['reason']}
        if action == 'HOLD':
            result=ledger.complete_quote(event_id)
            return {'status':'COMPLETE' if result.get('accepted') else 'BLOCKED','result':result}
        trade_id=position['trade_id']
        version=account['version']
        key=f'D-quote:{event_id}:{trade_id}:{action}'
        if action == 'UPDATE_STOP':
            result=ledger.update_stop(stop=plan['stop'],trade_id=trade_id,idempotency_key=key,
                expected_version=version,current_bar_open=plan['current_bar_open'],quote_event_id=event_id)
        else:
            if action == 'STOP':
                marked=ledger.mark_stop_due(event_id=event_id,expected_version=version,trade_id=trade_id)
                if not marked.get('accepted'):
                    return {'status':'BLOCKED','result':marked}
                version=ledger.account()['version']
                action='STOP_DUE'
            if action == 'LIQUIDATION' and position.get('stop_due'):
                action='STOP_DUE'
            result=ledger.close_position(quantity=plan['quantity'],price=plan['price'],reason=action,
                trade_id=trade_id,idempotency_key=key,expected_version=version,quote_event_id=event_id,
                bar_close=plan.get('bar_close'))
        if not result.get('accepted'):
            return {'status':'BLOCKED','result':result}
    return {'status':'BLOCKED','reason':'TRANSITION_LIMIT'}

