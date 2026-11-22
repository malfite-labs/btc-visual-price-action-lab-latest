"""Read-only presentation of the independent D trading ledger."""
import json
import sqlite3
from decimal import Decimal
from pathlib import Path

from backend.d_executor import validate_quote
from backend.d_margin import account_metrics


def read_execution_account(path, quote, now):
    """Read a consistent D snapshot. Never initialize a missing ledger."""
    path = Path(path).resolve()
    unavailable = {'status': 'UNAVAILABLE', 'reason': 'D_LEDGER_UNAVAILABLE', 'account': None,
                   'equity': None, 'realized_pnl': None, 'unrealized_pnl': None, 'fills': []}
    if not path.is_file():
        return unavailable
    try:
        with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=5) as db:
            db.row_factory = sqlite3.Row
            db.execute('BEGIN')
            identity = db.execute('SELECT kind FROM d_ledger_identity WHERE id=1').fetchone()
            if not identity or identity['kind'] != 'D_RESEARCH_PAPER_V1':
                return {**unavailable, 'reason': 'D_LEDGER_IDENTITY_INVALID'}
            row = db.execute('SELECT cash,version,position_json,realized_pnl FROM d_account WHERE id=1').fetchone()
            if not row:
                return {**unavailable, 'reason': 'D_ACCOUNT_MISSING'}
            position = json.loads(row['position_json']) if row['position_json'] else None
            fills = []
            for item in db.execute('SELECT * FROM d_fills ORDER BY rowid'):
                item = dict(item)
                fills.append({**item, 'agent': 'D', 'position_effect': item['effect'],
                              'position_direction': 'LONG', 'rationale': item['reason']})
            account = {'agent': 'D', 'currency': 'USD_RESEARCH', 'cash': row['cash'],
                       'version': row['version'], 'state': 'OPEN_LONG' if position else 'FLAT',
                       'position_json': row['position_json'], 'position': position}
            realized = str(Decimal(row['realized_pnl']))
        # A flat account needs no quote. An open position requires a fresh quote
        # and must never silently expose a stale or guessed account equity.
        if position and not validate_quote(quote or {}, now):
            return {'status': 'READY', 'reason': 'VALUATION_UNAVAILABLE', 'account': account,
                    'equity': None, 'realized_pnl': realized, 'unrealized_pnl': None, 'fills': fills}
        metrics = account_metrics(row['cash'], position, quote['bid'] if position else '1')
        if metrics['status'] in ('VALUATION_UNAVAILABLE', 'EQUITY_INVALID'):
            return {'status': 'READY', 'reason': metrics['status'], 'account': account,
                    'equity': None, 'realized_pnl': realized, 'unrealized_pnl': None, 'fills': fills}
        return {'status': 'READY', 'reason': metrics['status'], 'account': account,
                'equity': f"{metrics['equity']:.2f}", 'realized_pnl': realized,
                'unrealized_pnl': f"{metrics['unrealized_pnl']:.2f}",
                'margin': f"{metrics['margin']:.2f}", 'available_cash': f"{metrics['available_cash']:.2f}",
                'fills': fills}
    except (sqlite3.Error, ValueError, TypeError, KeyError, ArithmeticError):
        return {**unavailable, 'reason': 'D_LEDGER_READ_ERROR'}
