"""Legacy B recovery entry; all exits now share quote-side full fills."""
from backend.execution import recover_pending_stop


def recover_b_stop_depth(*, ledger, quote, orderbook, cost, event_id):
    return recover_pending_stop(ledger=ledger, agent='B', quote=quote, cost=cost, event_id=event_id)
