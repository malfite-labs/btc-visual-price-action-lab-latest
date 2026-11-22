"""E visual evidence validation. This module never submits simulated orders."""
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
import re


def resolve_e_decision(decision: dict, bars: list[dict], *, mapping_verified: bool) -> dict:
    """Resolve K01..K48 chart references against the exact closed 288-bar snapshot."""
    if len(bars) != 288:
        raise ValueError('CLOSED_288_REQUIRED')
    previous = None
    for bar in bars:
        try:
            start = datetime.fromisoformat(bar['open_at'].replace('Z', '+00:00'))
            end = datetime.fromisoformat(bar['close_at'].replace('Z', '+00:00'))
            values = [Decimal(str(bar[k])) for k in ('open', 'high', 'low', 'close')]
            if (start.tzinfo is None or end.tzinfo is None or end-start != timedelta(minutes=15)
                or (previous is not None and start != previous) or bar.get('is_closed') is not True
                or any(not v.is_finite() or v <= 0 for v in values)
                or values[2] > min(values[0], values[3]) or values[1] < max(values[0], values[3])):
                raise ValueError('INVALID_SNAPSHOT')
            previous = end
        except (KeyError, TypeError, InvalidOperation) as exc:
            raise ValueError('INVALID_SNAPSHOT') from exc
    if not isinstance(decision, dict):
        raise ValueError('INVALID_DECISION')
    if decision.get('action') == 'SKIP':
        return {'order_allowed': False, 'reason': 'SKIP'}
    side, setup = decision.get('side'), decision.get('setup')
    if decision.get('action') != 'OPEN' or (side, setup) not in {
        ('LONG','E1'), ('LONG','E2'), ('LONG','E3'), ('SHORT','E4')
    } or decision.get('confidence') not in {'HIGH','MEDIUM','LOW','UNKNOWN'}:
        raise ValueError('INVALID_DECISION')
    if not isinstance(decision.get('reasons'), list) or not decision['reasons'] or not all(
        isinstance(x, str) and x.strip() for x in decision['reasons']
    ):
        raise ValueError('REASONS_REQUIRED')
    resolved = {}
    for name in ('stop', 'reward'):
        anchor = decision.get(name + '_anchor')
        if not isinstance(anchor, dict) or set(anchor) != {'candle','field'}:
            raise ValueError('INVALID_ANCHOR')
        label, field = anchor['candle'], anchor['field']
        if not isinstance(label, str) or not re.fullmatch(r'K(?:0[1-9]|[1-3][0-9]|4[0-8])', label):
            raise ValueError('INVALID_ANCHOR')
        expected = ('low' if side == 'LONG' else 'high') if name == 'stop' else ('high' if side == 'LONG' else 'low')
        if field != expected:
            raise ValueError('INVALID_ANCHOR_FIELD')
        bar = bars[-48 + int(label[1:]) - 1]
        resolved[name+'_price'] = str(bar[field])
        resolved[name+'_bar_open_at'] = bar['open_at']
    resolved.update(order_allowed=False, reason=('VISUAL_MAPPING_NOT_VALIDATED' if not mapping_verified
                    else 'EXECUTION_GATES_NOT_IMPLEMENTED'))
    return resolved


def validate_e_model_response(decision: dict) -> None:
    """Reject incomplete or internally inconsistent E model responses."""
    if not isinstance(decision, dict):
        raise ValueError('INVALID_DECISION')
    reasons = decision.get('reasons')
    if not isinstance(reasons, list) or not reasons or not all(isinstance(x, str) and x.strip() for x in reasons):
        raise ValueError('REASONS_REQUIRED')
    if decision.get('confidence') not in {'HIGH', 'MEDIUM', 'LOW', 'UNKNOWN'}:
        raise ValueError('INVALID_CONFIDENCE')
    if decision.get('action') == 'SKIP':
        if decision.get('side') != 'FLAT' or decision.get('setup') not in {'CHOP','UNKNOWN','E1','E2','E3','E4'}:
            raise ValueError('INVALID_SKIP')
    elif decision.get('action') != 'OPEN':
        raise ValueError('INVALID_ACTION')
