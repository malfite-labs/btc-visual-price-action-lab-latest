"""V3 research snapshot. GET-only; never imports or modifies trading services.

Closed-trade conservatism and missing-equity handling adapted from the old
strategy_performance.py and strategy_research_view.py, with V3 sources.
"""
from __future__ import annotations
import csv, json, urllib.parse, urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

PA = [('PA-01','pa-trend-40-breakout'),('PA-02','pa-sma20-pullback-confirm'),('PA-03','pa-full-engulfing-reversal'),('PA-04','pa-gapless-three-bar-reversal'),('PA-05','pa-trend-false-break-reclaim'),('PA-06','pa-inside-bar-break'),('PA-07','pa-big-candle-continuation'),('PA-08','pa-three-bar-hhhl'),('PA-09','PA-09'),('PA-10','PA-10')]
IDS = [('A','A'),('B','B'),('C','C'),('D','D'),('E','E'),('G','G')] + PA

def dec(v):
    try:
        x=Decimal(str(v)); return x if x.is_finite() else None
    except (InvalidOperation, ValueError, TypeError): return None

def closed_trades(fills, strategy):
    groups=defaultdict(list); issues=[]
    for f in fills:
        key=f.get('g_trade_id') if strategy=='G' else f.get('trade_id')
        if not key:
            issues.append('成交缺少稳定交易编号，未配对'); continue
        effect=str(f.get('position_effect') or f.get('effect') or '').upper()
        if effect=='OPEN': kind='OPEN'
        elif effect in ('CLOSE','PARTIAL_CLOSE','RISK_EXIT','STOP_EXIT'): kind='CLOSE'
        else:
            issues.append('成交方向不明：'+effect); continue
        groups[str(key)].append((kind,f))
    result=[]
    for key, rows in groups.items():
        ids=[str(f['fill_id']) for _,f in rows if f.get('fill_id') is not None]
        if len(ids)!=len(set(ids)): issues.append(key+': 重复成交编号'); continue
        opens=[f for k,f in rows if k=='OPEN']; closes=[f for k,f in rows if k=='CLOSE']
        def valid(f):
            return all(dec(f.get(x)) is not None for x in ('quantity','price','fee')) and dec(f['quantity'])>0 and dec(f['price'])>0 and dec(f['fee'])>=0
        if not opens or not closes or not all(valid(f) and f.get('occurred_at') for _,f in rows):
            issues.append(key+': 缺少完整开平仓或价格费用'); continue
        oq=sum((dec(f['quantity']) for f in opens),Decimal(0)); cq=sum((dec(f['quantity']) for f in closes),Decimal(0))
        directions={str(f.get('position_direction') or f.get('direction') or '').upper() for _,f in rows}
        if oq != cq or len(directions)!=1 or not directions <= {'LONG','SHORT'}:
            issues.append(key+': 数量或方向不一致'); continue
        direction=next(iter(directions)); buy=sum((dec(f['price'])*dec(f['quantity']) for f in opens),Decimal(0)); sell=sum((dec(f['price'])*dec(f['quantity']) for f in closes),Decimal(0)); fees=sum((dec(f['fee']) for _,f in rows),Decimal(0))
        if strategy=='G':
            pnls=[dec(f.get('pnl')) for f in closes]
            if any(p is None for p in pnls): issues.append(key+': 镜像净盈亏缺失'); continue
            net=sum(pnls,Decimal(0))
        else: net=(sell-buy)*(1 if direction=='LONG' else -1)-fees
        opened=min((str(f.get('occurred_at') or '') for f in opens)); ended=max((str(f.get('occurred_at') or '') for f in closes))
        result.append({'trade_id':key,'opened_at':opened,'closed_at':ended,'direction':direction,'quantity':str(oq),'entry_price':str(buy/oq),'exit_price':str(sell/cq),'fees':str(fees),'net_pnl':str(net),'exit_reason':'; '.join(str(f.get('reason') or f.get('rationale') or '') for f in closes),'stop':None,'target':None})
    return sorted(result,key=lambda x:x['closed_at']),issues

def performance(trades):
    vals=[dec(t.get('net_pnl')) for t in trades]; vals=[v for v in vals if v is not None]; wins=[v for v in vals if v>0]; losses=[v for v in vals if v<0]
    return {'closed':len(vals),'win_rate':str(Decimal(len(wins))*100/len(vals)) if vals else None,'avg_win':str(sum(wins)/len(wins)) if wins else None,'avg_loss':str(sum(losses)/len(losses)) if losses else None,'profit_factor':str(sum(wins)/(-sum(losses))) if losses else None,'sample':'样本不足' if len(vals)<30 else '有样本，非盈利保证'}

def equity_analysis(points):
    valid=[]
    for p in points:
        v=dec(p.get('equity')); at=p.get('time')
        try: dt=datetime.fromisoformat(str(at).replace('Z','+00:00'))
        except (TypeError,ValueError): continue
        if v is not None and v>0 and dt.tzinfo is not None: valid.append((dt,v,str(at)))
    valid.sort(); peak=None; worst=None; at=None
    for _,v,t in valid:
        peak=max(peak or v,v); drop=(peak-v)/peak*100
        if worst is None or drop>worst: worst=drop; at=t
    return {'points':len(valid),'max_drawdown':str(worst) if len(valid)>=2 else None,'at':at if len(valid)>=2 else None,'basis':'仅本窗口真实采样点，非完整历史'}

class ReadOnlyClient:
    def __init__(self): self.opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    def get(self,path):
        if not path.startswith('/api/v1/'): raise ValueError('API_PATH_INVALID')
        req=urllib.request.Request('http://127.0.0.1:8012'+path,method='GET')
        with self.opener.open(req,timeout=8) as res: return json.load(res)

def load_snapshot(client=None):
    client=client or ReadOnlyClient(); errors=[]
    def get(path,default):
        try: return client.get('/api/v1'+path)
        except Exception as e: errors.append(path+': '+type(e).__name__); return default
    asof=datetime.now(timezone.utc).isoformat(); pool={i['id']:i for i in get('/strategy-pool',{}).get('items',[]) if i.get('id')}; agents={i['agent']:i for i in get('/agents',[]) if i.get('agent')}; stats=get('/statistics/abc',{}).get('agents',{}); d=get('/d/status',{}); e=get('/e/accounts',{}); g=get('/g-mirror/status',{}); registry=get('/strategies',{}).get('items',[]); versions={i.get('agent'):i for i in registry}; allfills=get('/fills',{}).get('items',[]); gfill=get('/g-mirror/fills',{}).get('items',[]); rows=[]
    for short,sid in IDS:
        state={}; fills=[]; version=None; source='GET /api/v1/'; description=None; registered=False; name=short
        if short in 'ABC' and len(short)==1:
            a=agents.get(short,{}); state=stats.get(short,{}); fills=[f for f in allfills if f.get('agent')==short]; version=(a.get('active_rule') or {}).get('version'); source+='statistics/abc, fills, agents/account'; initial=get('/agents/'+short+'/account',{}).get('initial_equity'); registered=short in agents; description=(a.get('active_rule') or {}).get('label'); name=description or short
        elif short=='D':
            state={'equity':d.get('equity'),'realized_net_pnl':d.get('realized_pnl'),'unrealized_pnl':d.get('unrealized_pnl'),'state':d.get('status'),'fees':None,'position':(d.get('account') or {}).get('position'),'last_cycle':(d.get('decision') or {}).get('candle_close')}; fills=d.get('fills') or [];version=d.get('rule_version');source+='d/status';initial=None; registered=bool(d.get('rule_version')); description='D 15分钟信号与1小时背景；执行版本以D状态为准'; name='D 急跌反转'
        elif short=='E':
            formal=e.get('formal') or {};state={'equity':formal.get('equity'),'realized_net_pnl':formal.get('realized_pnl'),'unrealized_pnl':formal.get('unrealized_pnl'),'state':(e.get('runtime') or {}).get('mode'),'position':(formal.get('account') or {}).get('position_json')};fills=formal.get('fills') or [];version=(versions.get('E') or {}).get('driving_version');source+='e/accounts, strategies';initial=None; registered=bool(e.get('formal')); description='E V1.1量化策略；实际执行版本以driving_version为准'; name='E V1.1量化'
        elif short=='G':
            state={'equity':g.get('equity'),'realized_net_pnl':g.get('realized'),'unrealized_pnl':g.get('unrealized'),'state':g.get('follow_state'),'fees':g.get('fees'),'position':g.get('position'),'last_cycle':g.get('last_cycle')};fills=gfill;version=g.get('version');source+='g-mirror/status, g-mirror/fills';initial=None; registered=g.get('registered') is True; description='G由C成交事件驱动镜像，不是独立15分钟信号'; name='G C事件镜像'
        else:
            a=pool.get(sid,{});state={'equity':a.get('equity'),'realized_net_pnl':a.get('realized'),'unrealized_pnl':a.get('unrealized'),'state':a.get('status'),'fees':a.get('fees'),'last_cycle':a.get('last_cycle'),'position':a.get('position')};version=a.get('active_version');fills=get('/strategy-pool/'+urllib.parse.quote(sid,safe='')+'/fills?limit=2000',{}).get('items',[]);source+='strategy-pool';initial=None; version_rows=get('/strategy-pool/'+urllib.parse.quote(sid,safe='')+'/versions',{}).get('items',[]); active=next((v for v in version_rows if v.get('version')==version),{}); doc=active.get('document') or {}; doc=json.loads(doc) if isinstance(doc,str) else doc; initial=(doc.get('risk') or {}).get('initial_equity'); registered=sid in pool; description=doc.get('description') or ('自定义执行器；规则细节以活动来源为准' if short in ('PA-09','PA-10') else None); name=doc.get('name') or sid
        trades,issues=closed_trades(fills,short); metrics=performance(trades); metrics['sample']='部分成交无法验证' if issues else metrics['sample']; metrics['closed']=None if not registered else metrics['closed']; vals=[str(f.get('occurred_at') or '') for f in fills]; initial_n=dec(initial); equity_n=dec(state.get('equity')); net_return=str((equity_n-initial_n)*100/initial_n) if initial_n is not None and initial_n>0 and equity_n is not None else None;
        rows.append({'id':short,'name':name,'strategy_id':sid,'version':version,'registered':registered,'description':description,'state':state.get('state'),'initial_equity':initial,'equity':state.get('equity'),'realized':state.get('realized_net_pnl'),'unrealized':state.get('unrealized_pnl'),'net_return':net_return,'fees':state.get('fees'),'funding':None,'position':state.get('position'),'last_cycle':state.get('last_cycle'),'last_trade':max(vals) if vals else None,'source':source,'as_of':asof,'metrics':metrics,'trades':trades,'fills':fills,'issues':issues,'equity_points':[], 'completeness':'样本不足或来源不完整' if issues or len(trades)<30 else '需人工核查'})
    return {'as_of':asof,'kind':'本地模拟运行快照；各接口非原子','strategies':rows,'errors':errors,'comparison':'不同资金模型及时间窗口不可排名'}

def export_snapshot(snapshot, output):
    out=validate_export_dir(output);out.mkdir(parents=True,exist_ok=True)
    paths=[out/'research_snapshot.json',out/'research_overview.csv',out/'research_report.md']
    paths[0].write_text(json.dumps(snapshot,ensure_ascii=False,indent=2,default=str),encoding='utf-8')
    cols=['id','strategy_id','version','state','initial_equity','equity','realized','unrealized','net_return','fees','funding','closed','win_rate','avg_win','avg_loss','profit_factor','sample','last_cycle','last_trade','source','as_of','completeness']
    flattened=[{**r,**r.get('metrics',{})} for r in snapshot['strategies']]
    with paths[1].open('w',newline='',encoding='utf-8-sig') as f:
        w=csv.DictWriter(f,fieldnames=cols,extrasaction='ignore');w.writeheader();w.writerows(flattened)
    lines=['# 模拟策略研究快照','','时间：'+snapshot['as_of'],'',snapshot.get('comparison',''),'','样本不足和无法配对的成交未作为闭合交易；资金费及缺失历史不推断。','','| 策略 | 版本 | 状态 | 权益 | 已实现 | 未实现 | 已验证闭合笔数 | 完整性 |','|---|---|---|---:|---:|---:|---:|---|']
    for r in flattened:
        vals=[r.get(k) for k in ('id','version','state','equity','realized','unrealized','closed','completeness')]
        lines.append('| '+' | '.join(str(x if x is not None else '—').replace('|','/') for x in vals)+' |')
    paths[2].write_text('\n'.join(lines)+'\n',encoding='utf-8')
    return paths


def validate_export_dir(output, protected_roots=None):
    target=Path(output).resolve()
    project=Path(__file__).resolve().parents[1]
    protected_roots=protected_roots or [Path.home()/'Documents'/'量化升级版'/'btc-paper-lab-v3'/'windows-binance-deploy', Path.home()/'Documents'/'btc-visual-price-action-lab', project, *([project.parent] if (project.parent/'.git').exists() else [])]
    for root in protected_roots:
        root=Path(root).resolve()
        if target==root or root in target.parents:raise ValueError('研究导出目录不能位于正式或旧项目内部')
    return target


# Reused from legacy research center: break curves on missing bars.
from datetime import UTC
from typing import Any
BAR_SECONDS = 900

def _aware_utc(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(UTC)



def equity_curve_segments(rows: list[dict[str, Any]], strategy_id: str) -> dict[str, list[Any]]:
    """Return trusted contiguous 15m segments; invalid/missing samples break lines."""
    selected = [row for row in rows if row.get("strategy_id") == strategy_id]
    selected.sort(key=lambda row: _aware_utc(row.get("cycle_close_at_utc")) or datetime.min.replace(tzinfo=UTC))
    segments: list[list[dict[str, Any]]] = []
    invalid_points: list[dict[str, Any]] = []
    gaps: list[dict[str, Any]] = []
    current: list[dict[str, Any]] = []
    previous: dict[str, Any] | None = None

    def flush() -> None:
        nonlocal current
        if current:
            segments.append(current)
            current = []

    for row in selected:
        timestamp = _aware_utc(row.get("cycle_close_at_utc"))
        value = row.get("equity_usd")
        valid = bool(row.get("equity_valid")) and value is not None and timestamp is not None
        if not valid:
            flush()
            previous = None
            invalid_points.append(row)
            continue
        point = {"cycle_close_at_utc": timestamp.isoformat(), "equity_usd": str(value)}
        if previous is not None:
            previous_time = _aware_utc(previous["cycle_close_at_utc"])
            delta = int((timestamp - previous_time).total_seconds()) if previous_time else 0
            if delta != BAR_SECONDS:
                flush()
                missing = max(0, delta // BAR_SECONDS - 1) if delta > 0 and delta % BAR_SECONDS == 0 else None
                gaps.append({"from_utc": previous["cycle_close_at_utc"], "to_utc": timestamp.isoformat(),
                             "missing_cycles": missing, "reason": "NON_CONSECUTIVE_OR_UNALIGNED_CYCLES"})
        current.append(point)
        previous = point
    flush()
    return {"segments": segments, "invalid_points": invalid_points, "gaps": gaps}




def curve_coordinates(points, width, height):
    observed=[]
    for item in points:
        t=_aware_utc(item.get('time')); value=dec(item.get('equity'))
        if t is not None and value is not None and value>0: observed.append((t,value,item['time']))
    observed.sort();
    if not observed:return []
    start=observed[0][0].timestamp(); duration=max(1.0,observed[-1][0].timestamp()-start); low=min(x[1] for x in observed);span=max(Decimal('0.01'),max(x[1] for x in observed)-low)
    return [{'time':raw,'x':50+(float(t.timestamp()-start)/duration)*max(1,width-80),'y':height-45-float((value-low)/span)*max(1,height-100)} for t,value,raw in observed]
