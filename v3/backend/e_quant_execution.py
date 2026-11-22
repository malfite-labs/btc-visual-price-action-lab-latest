"""E-only deterministic paper-cycle adapter; no model or market connections."""
import json
import asyncio
import logging
import sqlite3
from contextlib import closing
from decimal import Decimal
from datetime import UTC
from backend.e_quant_gate import evaluate_quant_candidate
from backend.e_quant_v11 import detect_setups

_explanation_tasks=set()


async def run_quant_with_explanation(*,explain,**cycle):
    """Finish and persist the deterministic cycle before launching optional explanation."""
    result=await asyncio.to_thread(run_quant_cycle,**cycle)
    if explain is not None:
        async def background():
            try:
                await asyncio.wait_for(explain(),timeout=60)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logging.getLogger(__name__).warning('E explanation failed: %s',type(exc).__name__)
        task=asyncio.create_task(background())
        _explanation_tasks.add(task)
        task.add_done_callback(_explanation_tasks.discard)
    return result


def run_quant_cycle(*,store,bars,quote,now,cost,shadow_change=None,strategy_dir=None,allow_short=False):
    """Run one idempotent cycle in the supplied E-only store, never in ABC stores."""
    try:
        close=bars[-1]['close_at']
        from datetime import datetime
        event=datetime.fromisoformat(close.replace('Z','+00:00'))
        if event.tzinfo is None:raise ValueError('TIMEZONE_REQUIRED')
        key=event.astimezone(UTC).isoformat()
    except (IndexError,KeyError,TypeError,ValueError):
        return {'status':'REJECTED','reason':'INVALID_CLOSE_TIME'}
    with closing(sqlite3.connect(store.path,timeout=10)) as db,db:
        db.execute('CREATE TABLE IF NOT EXISTS e_quant_cycles(close_at TEXT PRIMARY KEY,status TEXT NOT NULL,range_key TEXT,result_json TEXT NOT NULL)')
        if not db.execute('INSERT OR IGNORE INTO e_quant_cycles VALUES(?,?,?,?)',(key,'PENDING',None,'{}')).rowcount:
            return {'status':'DUPLICATE_BAR','reason':'DUPLICATE_BAR'}
    output={'status':'REJECTED','reason':'CYCLE_FAILED'}
    execution_started=False
    try:
        aligned=event.astimezone(UTC)
        if aligned.minute % 15 or aligned.second or aligned.microsecond:
            raise ValueError('SNAPSHOT_TIME_UNALIGNED')
        if shadow_change is not None:
            from backend.e_shadow_parameters import validate_shadow_change
            if strategy_dir is None:raise ValueError('SHADOW_STRATEGY_DIR_REQUIRED')
            with closing(sqlite3.connect(store.path,timeout=10)) as db:
                role=db.execute("SELECT value FROM e_store_identity WHERE key='account_role'").fetchone()
            if role!=('SHADOW',):raise ValueError('SHADOW_ACCOUNT_REQUIRED')
            validated=validate_shadow_change(strategy_dir,shadow_change)
            shadow_change={validated['parameter']:validated['value']}
        detected=detect_setups(bars,_shadow_change=shadow_change);candidate=detected['selected']
        output={'status':detected['status'],'reason':detected['status'],'snapshot_sha256':detected['snapshot_sha256'],'candidate':candidate}
        if candidate:
            gate=evaluate_quant_candidate(store=store,bars=bars,candidate=candidate,quote=quote,now=now,cost=cost,_shadow_change=shadow_change)
            if gate['reason']=='VISUAL_AND_EXECUTION_NOT_VALIDATED':
                gate={**gate,'reason':'QUANT_PREFLIGHT_PASSED'}
            output['preflight']=gate
            if gate['reason']!='QUANT_PREFLIGHT_PASSED':
                output.update(status='REJECTED',reason=gate['reason'])
            elif candidate['side']=='SHORT' and allow_short is not True:
                output.update(status='REJECTED',reason='SHORT_UNSUPPORTED')
            else:
                box=candidate.get('range') if candidate['setup']=='E1' else None
                range_key=json.dumps([box['lo'],box['hi']],separators=(',',':')) if box else None
                with closing(sqlite3.connect(store.path,timeout=10)) as db,db:
                    if range_key and db.execute("SELECT 1 FROM e_quant_cycles WHERE range_key=? AND status IN ('EXECUTING','PAPER_FILLED') AND close_at!=?",(range_key,key)).fetchone():
                        output.update(status='REJECTED',reason='E1_RANGE_ALREADY_TRADED')
                    else:
                        db.execute("UPDATE e_quant_cycles SET status='EXECUTING',range_key=? WHERE close_at=?",(range_key,key))
                if output['status']!='REJECTED':
                    execution_started=True
                    pre=gate['preflight'];qty=Decimal(pre['quantity']);price=Decimal(pre['fill_price'])
                    opened=store.open_position(agent='E',direction=candidate['side'],quantity=qty,price=price,
                        stop=Decimal(candidate['stop']),fee=Decimal(pre['fee']),slippage=abs(price-Decimal(quote['ask' if candidate['side']=='LONG' else 'bid']))*qty,
                        idempotency_key='E-V1.1:'+key,expected_version=store.account('E')['version'],
                        decision_id='E-V1.1:'+key,strategy_version='E-V1.1-QUANT',
                        rationale=json.dumps({'setup':candidate['setup'],'snapshot_sha256':detected['snapshot_sha256'],'anchor':candidate['anchor']},ensure_ascii=False),
                        quote_id=quote.get('event_id'),cost_model=cost)
                    output.update(status='PAPER_FILLED' if opened.get('accepted') else 'REJECTED',reason='PAPER_FILLED' if opened.get('accepted') else opened.get('reason','EXECUTION_REJECTED'),execution=opened)
    except ValueError as exc:
        reason=str(exc) if str(exc) in {'SNAPSHOT_TIME_UNALIGNED','SHADOW_ACCOUNT_REQUIRED','SHADOW_STRATEGY_DIR_REQUIRED'} else 'INVALID_SNAPSHOT'
        output={'status':'EXECUTING' if execution_started else 'REJECTED','reason':'EXECUTION_OUTCOME_UNKNOWN' if execution_started else reason}
    except Exception as exc:
        logging.getLogger(__name__).exception('E deterministic cycle failed')
        output={'status':'EXECUTING' if execution_started else 'REJECTED','reason':'EXECUTION_OUTCOME_UNKNOWN' if execution_started else 'CYCLE_ERROR','error_class':type(exc).__name__}
    with closing(sqlite3.connect(store.path,timeout=10)) as db,db:
        db.execute('UPDATE e_quant_cycles SET status=?,result_json=? WHERE close_at=?',(output['status'],json.dumps(output,ensure_ascii=False,default=str),key))
    return output
