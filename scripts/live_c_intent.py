"""C V3.1 intent and idempotency contract for isolated execution tests."""
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from hashlib import sha256
import sqlite3

from scripts.live_center import StrategySignal


@dataclass(frozen=True)
class EntryIntent:
    account_id: str
    cycle_id: str
    version: str
    client_order_id: str
    direction: str
    quantity: Decimal
    reference_price: Decimal
    stop: Decimal | None
    original_stop: Decimal | None
    exit_mode: str = 'LEGACY_BRACKET'
    target_fraction: Decimal | None = None

    def take_profit(self, average_fill: Decimal) -> Decimal:
        sign = Decimal(1 if self.direction == 'LONG' else -1)
        distance = (average_fill - self.original_stop) * sign
        if not distance.is_finite() or distance <= 0:
            raise ValueError('ACTUAL_FILL_ON_WRONG_SIDE_OF_STOP')
        return average_fill + sign * distance * Decimal('1.3')


def build_c_intent(account_id, report, *, now):
    if not account_id or now.utcoffset() is None:
        return None
    if report.get('state') != 'PASS' or report.get('risk_status') != 'PASS':
        return None
    signal = report.get('signal')
    if (not isinstance(signal, StrategySignal) or signal.strategy_id != 'C' or signal.strategy_version != 'V3.1'
            or signal.market_source != 'BINANCE_USDM_PERP' or signal.direction not in ('LONG', 'SHORT')
            or signal.action != ('BUY' if signal.direction == 'LONG' else 'SELL')
            or signal.cycle_at.utcoffset() is None or signal.expires_at.utcoffset() is None
            or not signal.cycle_at <= now < signal.expires_at):
        return None
    cycle = signal.cycle_at.astimezone(UTC)
    if cycle.minute % 15 or cycle.second or cycle.microsecond or signal.expires_at != cycle + timedelta(minutes=15):
        return None
    quantity, price, stop = (report.get(k) for k in ('quantity', 'price', 'rounded_stop'))
    try:
        original_stop = Decimal(signal.stop_basis)
    except (TypeError, ArithmeticError, ValueError):
        return None
    if not original_stop.is_finite() or original_stop <= 0:
        return None
    if any(not isinstance(v, Decimal) or not v.is_finite() or v <= 0 for v in (quantity, price, stop)):
        return None
    if (price > stop) != (signal.direction == 'LONG') or price == stop:
        return None
    if not (stop <= original_stop < price if signal.direction == 'LONG' else price < original_stop <= stop):
        return None
    cycle_id = cycle.isoformat()
    identity = sha256(f'{account_id}|C|{cycle_id}'.encode()).hexdigest()[:30]
    return EntryIntent(account_id, cycle_id, signal.strategy_version, f'c31-{identity}',
                       signal.direction, quantity, price, stop, original_stop)


def reserve_fixture_intent(path, intent):
    """Claim a cycle once in a dedicated test database, including across restart."""
    with closing(sqlite3.connect(path, timeout=5)) as db, db:
        db.execute('''CREATE TABLE IF NOT EXISTS c_entry_intents (
            account_id TEXT, cycle_id TEXT, client_order_id TEXT UNIQUE,
            PRIMARY KEY (account_id, cycle_id))''')
        return db.execute('INSERT OR IGNORE INTO c_entry_intents VALUES (?, ?, ?)',
                          (intent.account_id, intent.cycle_id, intent.client_order_id)).rowcount == 1
