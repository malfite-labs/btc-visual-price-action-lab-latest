"""Read-only statistics; fees and slippage are already included in closed trade net PnL."""
import json,os,sqlite3,subprocess
from datetime import datetime,timezone
from decimal import Decimal as D
from pathlib import Path
from backend.market import _StreamLease

def shadow_state(folder, status):
    state=status.get('state','UNKNOWN')
    if state != 'RUNNING':return state
    if not status.get('updated_utc'):return 'UNKNOWN'
    updated=datetime.fromisoformat(status['updated_utc'].replace('Z','+00:00'))
    if updated.tzinfo is None or not 0 <= (datetime.now(timezone.utc)-updated).total_seconds() <= 30:return 'STALE'
    pid=status.get('pid')
    if not isinstance(pid,int) or pid<=0:return 'UNKNOWN'
    if os.name == 'nt':
        try:
            result=subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-Command',f"$p=Get-CimInstance Win32_Process -Filter 'ProcessId={pid}'; if ($p -and $p.CommandLine -match 'run_forward_v26\\.py') {{ exit 0 }} else {{ exit 1 }}"],capture_output=True,timeout=15,creationflags=subprocess.CREATE_NO_WINDOW)
        except (OSError,subprocess.TimeoutExpired):return 'UNKNOWN'
        if result.returncode:return 'OFFLINE'
    else:
        try:
            if 'run_forward_v26.py' not in Path(f'/proc/{pid}/cmdline').read_bytes().decode(errors='replace'):return 'OFFLINE'
        except OSError:return 'OFFLINE'
    lease=_StreamLease(folder/'forward-worker.lock')
    if lease.acquire():
        lease.release()
        return 'OFFLINE'
    return 'RUNNING' if lease.failure_reason=='LOCAL_STREAM_LEASE_HELD' else 'UNKNOWN'

def closed_summary(records):
    nets=[D(str(x['net_pnl'])) for x in records];total=sum(nets,D(0));wins=sum((x for x in nets if x>0),D(0));loss=-sum((x for x in nets if x<0),D(0));equity=peak=D('10000');dd=D(0)
    for net in nets:
        equity+=net;peak=max(peak,equity);dd=max(dd,(peak-equity)/peak)
    return {'realized_net_pnl':str(total.quantize(D('.01'))),'closed_trades':len(nets),'win_rate_pct':str((D(sum(x>0 for x in nets))*100/len(nets)).quantize(D('.01'))) if nets else None,'profit_factor':str((wins/loss).quantize(D('.0001'))) if loss else None,'max_drawdown_pct':str((dd*100).quantize(D('.0001'))) if nets else None,'drawdown_basis':'closed_trade_realized_equity_only'}

def shadow_stats(root):
    folder=Path(root)/'docs/reports/abc-forward-v26';db=folder/'shadow-ledger.sqlite3';config=json.loads((folder/'freeze.json').read_text(encoding='utf8'));status=json.loads((folder/'status.json').read_text(encoding='utf8'));status['state']=shadow_state(folder,status)
    result={}
    with sqlite3.connect(db.resolve().as_uri()+'?mode=ro',uri=True,timeout=5) as c:
        for a in 'ABC':
            total=c.execute('select count(*) from forward_trades where agent=?',(a,)).fetchone()[0]
            review=c.execute('select batch,status,details from forward_reviews where agent=? order by batch desc limit 1',(a,)).fetchone()
            completed=c.execute("select coalesce(max(batch),0) from forward_reviews where agent=? and status='COMPLETED'",(a,)).fetchone()[0]
            version=c.execute('select batch,activation_open,created_at from forward_versions where agent=? and activation_open<=? order by batch desc limit 1',(a,datetime.now(timezone.utc).isoformat())).fetchone()
            result[a]={'version':('V3->batch '+str(version[0]) if version else config[a]['version']),'closed_since_review':max(0,total-completed),'total_closed':total,'remaining_to_40':max(0,40-(total-completed)),'latest_evolution_at':version[2] if version else None,'review_status':review[1] if review else 'WAITING_FOR_40','review_batch':review[0] if review else None,'feed_status':status.get('state'),'latest_bar':status.get('last_bar_close'),'latest_quote':status.get('last_quote_at'),'status_updated_at':status.get('updated_utc')}
    return result

def build_stats(db_path,root,ledger,quote,cost,scheduler_enabled):
    out={}
    try:shadow=shadow_stats(root)
    except (OSError,sqlite3.Error,ValueError,KeyError):shadow={a:{'feed_status':'UNAVAILABLE','closed_since_review':None,'version':None} for a in 'ABC'}
    with sqlite3.connect(Path(db_path).resolve().as_uri()+'?mode=ro',uri=True,timeout=5) as c:
        c.row_factory=sqlite3.Row
        for a in 'ABC':
            acc=dict(c.execute('select * from accounts where agent=?',(a,)).fetchone());fills=[dict(x) for x in c.execute('select * from fills where agent=? order by occurred_at,fill_id',(a,))]
            audit=[json.loads(x[0]) for x in c.execute("select payload from audit_events where agent=? and kind='POSITION_CLOSED' order by occurred_at,event_id",(a,))]
            completed_ids={x['trade_id'] for x in fills if x['position_effect']=='CLOSE'};audit_ids=[x.get('trade_id') for x in audit]
            s=closed_summary(audit) if len(audit_ids)==len(set(audit_ids)) and set(audit_ids)==completed_ids else {'realized_net_pnl':None,'closed_trades':None,'win_rate_pct':None,'profit_factor':None,'max_drawdown_pct':None,'drawdown_basis':'UNAVAILABLE_INCOMPLETE_LEDGER'}
            metrics=ledger.account_metrics(a,quote,cost);pos=json.loads(acc['position_json']) if acc['position_json'] else None
            decision=c.execute('select candle_close,action,outcome,raw_response from decisions where agent=? order by candle_close desc limit 1',(a,)).fetchone()
            dec=None
            if decision:
                raw=json.loads(decision['raw_response'] or '{}');dec={'candle_close':decision['candle_close'],'action':decision['action'],'outcome':decision['outcome'],'reason':raw.get('rationale'),'execution':raw.get('execution')}
                if dec['execution']:dec['execution']={k:v for k,v in dec['execution'].items() if k!='audit'}
            maintenance=liquidation=None
            if a=='C' and pos and quote and quote.get('timestamp_valid') and 0<=int(quote.get('age_ms',10**9))<=cost.max_quote_age_ms:
                qty=D(pos['quantity']);mark=D(quote['ask' if pos['direction']=='SHORT' else 'bid']);entry=D(pos['entry_price']);cash=D(acc['cash']);rate=D('.01');reserve=cost.fee_rate+cost.adverse_slippage_bps/D(10000)
                maintenance=str(qty*mark*rate);denom=qty*(D(1)+rate+reserve) if pos['direction']=='SHORT' else qty*(D(1)-rate-reserve);numer=cash+entry*qty if pos['direction']=='SHORT' else entry*qty-cash
                liquidation=str(numer/denom) if numer>0 and denom>0 else None
            out[a]={**s,'cash':acc['cash'],'equity':metrics.get('equity'),'unrealized_pnl':metrics.get('unrealized_pnl'),'valuation_status':metrics.get('valuation_status'),'available_cash':metrics.get('available_cash'),'state':acc['state'],'position':pos,'pending_stop':json.loads(acc['pending_stop_json']) if acc['pending_stop_json'] else None,'fees':str(sum((D(x['fee']) for x in fills),D(0))),'slippage':str(sum((D(x['slippage']) for x in fills),D(0))),'last_fill':fills[-1] if fills else None,'last_decision':dec,'maintenance_margin':maintenance,'liquidation_reference_price':liquidation,'monitor_state':'ACTIVE' if scheduler_enabled and quote and quote.get('timestamp_valid') is True and 0<=int(quote.get('age_ms',10**9))<=cost.max_quote_age_ms else 'UNVERIFIED_OR_STALE','shadow':shadow[a]}
    return {'updated_at':datetime.now(timezone.utc).isoformat(),'quote':quote,'agents':out,'cost_note':'Net PnL already includes fees and execution prices include slippage; no second subtraction.'}
