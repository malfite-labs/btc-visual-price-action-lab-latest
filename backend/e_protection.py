"""Quote-driven E protection using the existing frozen execution helpers."""
import hashlib
import threading
from pathlib import Path
from backend.e_paper_store import EPaperStore
from backend.execution import handle_confirmed_stop, recover_pending_stop

_protection_lock=threading.RLock()


def protect_e_accounts(paths, quote, cost):
    # Quote-loop and closed-bar tasks share E ledgers; stop checks precede exits.
    with _protection_lock:
        return _protect_e_accounts(paths,quote,cost)


def _protect_e_accounts(paths, quote, cost):
    quote=quote or {}
    material='|'.join(str(quote.get(k,'')) for k in ('event_at','bid','ask','source'))
    event='E-quote:'+hashlib.sha256(material.encode()).hexdigest()
    results={}
    for kind,path in paths.items():
        if not Path(path).exists():
            continue
        try:
            ledger=EPaperStore(path)
            state=ledger.account('E')['state']
            if state == 'STOP_EXIT_PENDING':
                result=recover_pending_stop(ledger=ledger,agent='E',quote=quote,cost=cost,event_id=event+':'+kind)
            elif state in {'OPEN_LONG','OPEN_SHORT'}:
                from backend.e_trailing import monitor_protective_stop
                try:
                    result=monitor_protective_stop(ledger,quote,cost)
                except Exception:
                    result={'accepted':False,'reason':'TRAILING_PROTECTION_ERROR'}
                if not result.get('accepted') and ledger.account('E')['state'] in {'OPEN_LONG','OPEN_SHORT'}:
                    result=handle_confirmed_stop(ledger=ledger,agent='E',trigger_event_id=event+':'+kind,quote=quote,cost=cost)
                if not result.get('accepted') and ledger.account('E')['state']=='OPEN_SHORT':
                    from backend.e_short_liquidation import protect_e_short_margin
                    margin=protect_e_short_margin(ledger,quote,cost)
                    if margin.get('accepted'):result=margin
                if not result.get('accepted') and ledger.account('E')['state'] in {'OPEN_LONG','OPEN_SHORT'}:
                    from backend.e_structure import execute_structure_due
                    structure=execute_structure_due(ledger,quote,cost)
                    if structure.get('reason')!='NO_STRUCTURE_EXIT_DUE':result=structure
            else:
                continue
            results[kind]=result
        except Exception as exc:
            results[kind]={'accepted':False,'reason':'PROTECTION_ERROR','error_class':type(exc).__name__}
    return results
