"""Isolate a valid B pending stop while failing closed for inconsistent accounts."""
import json
from decimal import Decimal
def recovery_blocked(ledger, runner=None):
    for a in 'ABC':
        account=ledger.account(a);state=account['state']
        if state in ('RECOVERY_REQUIRED','CLOSING') or (a!='B' and state=='STOP_EXIT_PENDING'):return True
        if state=='STOP_EXIT_PENDING':
            try:
                p=json.loads(account['position_json']);pending=json.loads(account['pending_stop_json'])
                if not p['trade_id'] or Decimal(p['quantity'])<=0 or not pending['event_key']:return True
            except (KeyError,TypeError,ValueError,ArithmeticError):return True
        if runner is not None and a in ('A','C') and state in ('OPEN_LONG','OPEN_SHORT'):
            try:
                p=json.loads(account['position_json']);meta=runner._meta(a)
                if not meta or meta[1]!=p['trade_id'] or Decimal(p['quantity'])<=0 or Decimal(p['stop_price'])<=0:return True
            except (KeyError,TypeError,ValueError,ArithmeticError):return True
    return False
