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
        if not opens or not closes or not all(valid(f) and _aware_utc(f.get('occurred_at')) is not None for _,f in rows):
            issues.append(key+': 缺少完整开平仓或价格费用'); continue
        if max(_aware_utc(f['occurred_at']) for f in opens) >= min(_aware_utc(f['occurred_at']) for f in closes):
            issues.append(key+': 开平仓时间冲突'); continue
        oq=sum((dec(f['quantity']) for f in opens),Decimal(0)); cq=sum((dec(f['quantity']) for f in closes),Decimal(0))
        directions={str(f.get('position_direction') or f.get('direction') or '').upper() for _,f in rows}
        if oq != cq or len(directions)!=1 or not directions <= {'LONG','SHORT'}:
            issues.append(key+': 数量或方向不一致'); continue
        direction=next(iter(directions)); buy=sum((dec(f['price'])*dec(f['quantity']) for f in opens),Decimal(0)); sell=sum((dec(f['price'])*dec(f['quantity']) for f in closes),Decimal(0)); fees=sum((dec(f['fee']) for _,f in rows),Decimal(0))
        if strategy in ('D','G'):
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
    valid.sort(); counts=defaultdict(int)
    for dt,_,_ in valid:counts[dt]+=1
    valid=[x for x in valid if counts[x[0]]==1]
    peak=None; worst=None; at=None
    for _,v,t in valid:
        peak=max(peak or v,v); drop=(peak-v)/peak*100
        if worst is None or drop>worst: worst=drop; at=t
    return {'points':len(valid),'max_drawdown':str(worst) if len(valid)>=2 else None,'at':at if len(valid)>=2 else None,'start':valid[0][2] if valid else None,'end':valid[-1][2] if valid else None,'coverage':None,'coverage_reason':'事件与手动快照并非连续采样，无法计算15分钟覆盖率','basis':'仅真实观测点；非完整历史或全时段最大回撤'}

class ReadOnlyClient:
    def __init__(self): self.opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    def get(self,path):
        if not path.startswith('/api/v1/'): raise ValueError('API_PATH_INVALID')
        req=urllib.request.Request('http://127.0.0.1:8012'+path,method='GET')
        with self.opener.open(req,timeout=8) as res: return json.load(res)

def load_snapshot(client=None, store=None):
    use_live = client is None
    client = client or ReadOnlyClient()
    store = store or (ReadOnlyStore(Path(__file__).resolve().parents[1]) if use_live else EmptyStore())
    errors=[]
    def get(path,default):
        try: return client.get('/api/v1'+path)
        except Exception as e: errors.append(path+': '+type(e).__name__); return default
    asof=datetime.now(timezone.utc).isoformat(); pool=store.pool()
    agents={i['agent']:i for i in get('/agents',[]) if i.get('agent')}; stats=get('/statistics/abc',{}).get('agents',{})
    d=store.d_status();e=get('/e/accounts',{});g=get('/g-mirror/status',{});registry=get('/strategies',{}).get('items',[])
    versions={i.get('agent'):i for i in registry};allfills=get('/fills',{}).get('items',[]);gfill=get('/g-mirror/fills?limit=2000',{}).get('items',[]);rows=[]
    for short,sid in IDS:
        state={};fills=[];version=None;source='GET /api/v1/';description=None;registered=False;name=short;initial=None
        if short in ('A','B','C'):
            a=agents.get(short,{});state=stats.get(short,{});fills=[f for f in allfills if f.get('agent')==short];version=(a.get('active_rule') or {}).get('version')
            source+='statistics/abc, fills, agents/account';initial=get('/agents/'+short+'/account',{}).get('initial_equity');registered=short in agents
            description=(a.get('active_rule') or {}).get('label');name=description or short
        elif short=='D':
            state={'equity':d.get('equity'),'realized_net_pnl':d.get('realized_pnl'),'unrealized_pnl':d.get('unrealized_pnl'),'state':d.get('status'),'position':(d.get('account') or {}).get('position'),'last_cycle':(d.get('decision') or {}).get('candle_close')}
            fills=d.get('fills') or [];version=d.get('rule_version');source='SQLite mode=ro/query_only: d/trading.sqlite3, d/formal.sqlite3；规则版本为本地文件';registered=bool(version);name='D 急跌反转';description='D 15分钟信号与1小时背景；执行版本以D状态为准'
        elif short=='E':
            formal=e.get('formal') or {};state={'equity':formal.get('equity'),'realized_net_pnl':formal.get('realized_pnl'),'unrealized_pnl':formal.get('unrealized_pnl'),'state':(e.get('runtime') or {}).get('mode'),'position':(formal.get('account') or {}).get('position_json')}
            fills=formal.get('fills') or [];version=(versions.get('E') or {}).get('driving_version');source+='e/accounts, strategies';registered=bool(e.get('formal'));name='E V1.1量化';description='E V1.1量化策略，执行版本以driving_version为准'
        elif short=='G':
            state={'equity':g.get('equity'),'realized_net_pnl':g.get('realized'),'unrealized_pnl':g.get('unrealized'),'state':g.get('follow_state'),'fees':g.get('fees'),'position':g.get('position'),'last_cycle':g.get('last_cycle')}
            fills=gfill;version=g.get('version');source+='g-mirror/status, g-mirror/fills';registered=g.get('registered') is True;name='G C事件镜像';description='G由C成交事件驱动镜像，不是独立15分钟信号'
        else:
            account=pool.get(sid,{});doc=account.get('document') or {};fills=account.get('fills') or [];version=account.get('active_version')
            state={'equity':account.get('equity'),'realized_net_pnl':account.get('realized'),'unrealized_pnl':account.get('unrealized'),'state':account.get('status'),'fees':account.get('fees'),'last_cycle':account.get('last_cycle'),'position':account.get('position')}
            initial=(doc.get('risk') or {}).get('initial_equity');registered=sid in pool;description=doc.get('description') or ('自定义执行器；规则来源需核验' if short in ('PA-09','PA-10') else None);name=doc.get('name') or sid
            source='SQLite mode=ro/query_only: '+('pa-v1-extensions/'+sid+'.sqlite3' if short in ('PA-09','PA-10') else 'strategy-pool.sqlite3')
        if short in {x[0] for x in PA[:8]}:trades,issues=pair_pool_fills(fills,sid)
        else:trades,issues=closed_trades(fills,short)
        metrics=performance(trades);metrics['sample']='部分成交无法验证' if issues else metrics['sample'];metrics['closed']=None if not registered else metrics['closed']
        events=[];points=[]
        if short in ('D','G'):
            events,calendar_issues=realized_calendar_events(fills,short);issues.extend(calendar_issues)
            if short=='G' and len(fills)>=2000:issues.append('UNVERIFIED: G接口达到2000条上限，历史可能截断')
        elif short in ('A','B','C','E'):
            ledger,audit=store.paper_evidence(short);events=audited_calendar_events(trades,fills,audit)
            by_id={str(x['trade_id']):x for x in events};validated=[]
            for trade in trades:
                event=by_id.get(str(trade['trade_id']))
                if event is None:issues.append('UNVERIFIED: 缺少对应平仓审计净盈亏');continue
                trade['net_pnl']=event['net_pnl'];trade['verification']='AUDITED';validated.append(trade)
            trades=validated;points=flat_equity_points(trades,fills,ledger,audit)
        elif short in {x[0] for x in PA[:8]}:events=pool_calendar_events(trades)
        metrics=performance(trades);metrics['sample']='部分成交无法验证' if issues else metrics['sample'];metrics['closed']=None if not registered else metrics['closed']
        vals=[str(f.get('occurred_at') or f.get('created_at') or f.get('at') or '') for f in fills]
        initial_n=dec(initial);equity_n=dec(state.get('equity'));net_return=str((equity_n-initial_n)*100/initial_n) if initial_n is not None and initial_n>0 and equity_n is not None else None
        rows.append({'id':short,'name':name,'strategy_id':sid,'version':version,'registered':registered,'description':description,'state':state.get('state'),'initial_equity':initial,'equity':state.get('equity'),'realized':state.get('realized_net_pnl'),'unrealized':state.get('unrealized_pnl'),'net_return':net_return,'fees':state.get('fees'),'funding':None,'position':state.get('position'),'last_cycle':state.get('last_cycle'),'last_trade':max(vals) if vals else None,'source':source,'as_of':asof,'metrics':metrics,'trades':trades,'fills':fills,'issues':issues,'realized_events':events,'equity_points':points,'completeness':'UNVERIFIED: 部分事件缺失或冲突' if issues else '样本不足' if len(trades)<30 else '需人工核查'})
    errors.extend(store.errors)
    return {'as_of':asof,'kind':'正式本地模拟快照；各接口/数据库非原子','strategies':rows,'errors':errors,'comparison':'不同资金模型及时间窗口不可排名'}


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
    observed.sort();counts=defaultdict(int)
    for t,_,_ in observed:counts[t]+=1
    observed=[x for x in observed if counts[x[0]]==1]
    if not observed:return []
    start=observed[0][0].timestamp(); duration=max(1.0,observed[-1][0].timestamp()-start); low=min(x[1] for x in observed);span=max(Decimal('0.01'),max(x[1] for x in observed)-low)
    return [{'time':raw,'x':50+(float(t.timestamp()-start)/duration)*max(1,width-80),'y':height-45-float((value-low)/span)*max(1,height-100)} for t,value,raw in observed]


def pair_pool_fills(fills, strategy_id):
    """Pair a full PA-01..08 single-position lifecycle; ambiguity stays unverified."""
    rows=[]; issues=[]; keys=set()
    for f in fills:
        key=f.get('idempotency_key'); at=_aware_utc(f.get('created_at')); cycle=_aware_utc(f.get('cycle'))
        if not isinstance(key,str) or not key.startswith(strategy_id+':') or key in keys or at is None or cycle is None:
            return [],['UNVERIFIED: 缺少或重复事件键/时间']
        keys.add(key); rows.append((at,cycle,f))
    rows.sort(key=lambda x:x[0]); results=[]; active=None; previous=None
    for at,cycle,f in rows:
        if previous is not None and at<=previous: return [],['UNVERIFIED: 成交时间相同或乱序']
        previous=at; reason=f.get('reason');key=f['idempotency_key'];side=f.get('side');qty=dec(f.get('qty'));price=dec(f.get('price'));fee=dec(f.get('fee'))
        if side not in ('BUY','SELL') or qty is None or price is None or fee is None or qty<=0 or price<=0 or fee<0 or not f.get('version'):
            issues.append('UNVERIFIED: 成交字段缺失');active=None;continue
        if reason in ('ENTRY','ENTRY_NEXT_OPEN') and key.startswith(strategy_id+':open:'):
            if active is not None:return [],['UNVERIFIED: 多个开仓缺少退出，生命周期存在歧义']
            active=(at,cycle,f);continue
        if reason not in ('STOP','TARGET','TIME','STOP_QUOTE','TARGET_QUOTE') or not (key.startswith(strategy_id+':close:') or key.startswith(strategy_id+':quote-')):
            issues.append('UNVERIFIED: 非已知退出事件');active=None;continue
        if active is None:issues.append('UNVERIFIED: 缺少开仓');continue
        otime,ocycle,entry=active;active=None
        if f['version']!=entry['version'] or cycle<ocycle or side==entry['side'] or qty!=dec(entry['qty']):
            issues.append('UNVERIFIED: 版本/数量/方向/周期不匹配');continue
        direction='LONG' if entry['side']=='BUY' else 'SHORT'; sign=1 if direction=='LONG' else -1
        net=(price-dec(entry['price']))*qty*sign-fee-dec(entry['fee'])
        results.append({'trade_id':'pool-lifecycle:'+entry['idempotency_key'],'opened_at':entry['created_at'],'closed_at':f['created_at'],'direction':direction,'quantity':str(qty),'entry_price':str(dec(entry['price'])),'exit_price':str(price),'fees':str(fee+dec(entry['fee'])),'net_pnl':str(net),'exit_reason':reason,'stop':None,'target':None,'evidence':'单持仓生命周期：唯一事件键、时间、版本、方向及全部数量匹配','verification':'VERIFIED_LIFECYCLE'})
    if active is not None:issues.append('UNVERIFIED: 未闭合或缺少退出')
    return results,issues


def realized_calendar_events(fills,strategy):
    """G mirror stores per-exit net including its allocated entry fee."""
    if strategy not in ('D','G'):return [],['UNVERIFIED: 此来源没有逐次退出的已核实净盈亏']
    groups=defaultdict(list);issues=[];events=[]
    for f in fills:
        key=f.get('g_trade_id') if strategy=='G' else f.get('trade_id')
        if not key:issues.append('UNVERIFIED: 缺少交易编号');continue
        groups[str(key)].append(f)
    from datetime import timedelta
    tz=timezone(timedelta(hours=8))
    for key,rows in groups.items():
        ids=[str(f.get('fill_id')) for f in rows if f.get('fill_id') is not None]
        if len(ids)!=len(rows) or len(ids)!=len(set(ids)) or any(_aware_utc(f.get('occurred_at')) is None for f in rows):issues.append('UNVERIFIED: 镜像事件编号重复/缺失或时间缺失');continue
        rows=sorted(rows,key=lambda f:_aware_utc(f['occurred_at']));remaining=Decimal(0); direction=None; pending=[]; valid=True; previous=None
        for f in rows:
            at=_aware_utc(f['occurred_at']);q=dec(f.get('quantity'));effect=f.get('effect');direct=f.get('direction') or f.get('position_direction')
            if previous is not None and at<=previous:valid=False;break
            previous=at
            if q is None or q<=0 or direct not in ('LONG','SHORT') or (direction and direct!=direction):valid=False;break
            if effect=='OPEN':direction=direct;remaining+=q
            elif effect in ('CLOSE','PARTIAL_CLOSE') and direction and q<=remaining:
                pnl=dec(f.get('pnl'))
                if pnl is None:valid=False;break
                remaining-=q
                pending.append({'date':at.astimezone(tz).date().isoformat(),'time':f['occurred_at'],'net_pnl':str(pnl),'trade_id':key,'source':strategy+'已记录逐笔退出净盈亏'})
            else:valid=False;break
        if valid:events.extend(pending)
        else:issues.append('UNVERIFIED: 镜像生命周期不完整或冲突')
    return sorted(events,key=lambda x:x['time']),issues


def flat_equity_points(trades,fills,ledger,audit):
    """Only cash after an audited full close is observed flat account equity."""
    closes={str(f.get('fill_id')):f for f in fills if f.get('position_effect')=='CLOSE' and f.get('fill_id')}
    by_trade={str(t.get('trade_id')):t for t in trades}; points=[]
    for row in audit:
        if row.get('kind')!='POSITION_CLOSED':continue
        try:payload=json.loads(row.get('payload') or '{}')
        except (TypeError,ValueError):continue
        trade=by_trade.get(str(payload.get('trade_id'))); fid=str(payload.get('exit_fill_id') or ''); fill=closes.get(fid)
        matches=[e for e in ledger if str(e.get('reference'))==fid and e.get('kind')=='CLOSE_PNL']
        if not trade or not fill or str(fill.get('trade_id'))!=str(payload.get('trade_id')) or len(matches)!=1:continue
        event=matches[0];at=_aware_utc(event.get('occurred_at'));cash=dec(event.get('cash_after'))
        if at is None or cash is None or cash<=0 or event.get('occurred_at')!=fill.get('occurred_at'):continue
        points.append({'time':event['occurred_at'],'equity':str(cash),'source':'已审计完整平仓时账本现金（空仓权益）','kind':'FORMAL_NATURAL_FLAT_CLOSE','trade_id':payload['trade_id']})
    return sorted(points,key=lambda x:x['time'])


class ReadOnlyStore:
    """Local ledger snapshots with SQLite read-only URI, never a transaction lock for writing."""
    def __init__(self, root): self.data=Path(root).resolve()/'data';self.errors=[]
    def _connect(self,relative):
        import sqlite3
        p=(self.data/relative).resolve()
        if self.data not in p.parents or not p.is_file():return None
        db=sqlite3.connect(p.as_uri()+'?mode=ro',uri=True,timeout=3)
        db.row_factory=sqlite3.Row;db.execute('PRAGMA query_only=ON');db.execute('BEGIN');return db
    def paper_evidence(self,agent):
        path='e/formal.sqlite3' if agent=='E' else 'paper.sqlite3';slot='A' if agent=='E' else agent
        db=None
        try:
            db=self._connect(path)
            if db is None:return [],[]
            events=[dict(r) for r in db.execute('SELECT kind,reference,cash_after,occurred_at FROM ledger_events WHERE agent=?',(slot,))]
            audit=[dict(r) for r in db.execute('SELECT kind,payload,occurred_at FROM audit_events WHERE agent=? AND kind=?',(slot,'POSITION_CLOSED'))]
            return events,audit
        except Exception as exc:self.errors.append(path+': '+type(exc).__name__);return [],[]
        finally:
            if db is not None:db.close()
    def d_status(self):
        out={};db=None
        try:
            db=self._connect('d/trading.sqlite3')
            if db is not None:
                r=db.execute('SELECT cash,position_json,realized_pnl FROM d_account WHERE id=1').fetchone()
                if r:
                    pos=json.loads(r['position_json']) if r['position_json'] else None
                    out.update(account={'position':pos},equity=r['cash'] if pos is None else None,realized_pnl=r['realized_pnl'],unrealized_pnl='0' if pos is None else None)
                fills=[]
                for row in db.execute('SELECT f.*,o.operation,o.response_json FROM d_fills f LEFT JOIN d_operations o ON o.idempotency_key=f.idempotency_key ORDER BY f.occurred_at,f.fill_id'):
                    item=dict(row)
                    try:response=json.loads(item.pop('response_json') or '{}')
                    except ValueError:response={}
                    if response.get('accepted') is True and response.get('fill_id')==item.get('fill_id') and response.get('trade_id')==item.get('trade_id') and item.get('operation')==item.get('effect'):
                        item['position_direction']='LONG'
                        if item['effect']=='CLOSE':item['pnl']=response.get('net_pnl')
                    fills.append(item)
                out['fills']=fills
        except Exception as exc:self.errors.append('d/trading.sqlite3: '+type(exc).__name__)
        finally:
            if db is not None:db.close()
        db=None
        try:
            db=self._connect('d/formal.sqlite3')
            if db is not None:
                r=db.execute('SELECT protection,reason FROM d_runtime_status WHERE id=1').fetchone()
                if r:out['status']=r['protection'];out['reason']=r['reason']
                r=db.execute('SELECT close_at,status,reason FROM d_cycles ORDER BY close_at DESC LIMIT 1').fetchone()
                if r:out['decision']={'candle_close':r['close_at'],'action':r['status'],'reason':r['reason']}
            cfg=self.data.parent/'config'/'strategy_d'/'D_source_recovered_2026-11-21.json'
            if cfg.is_file():out['rule_version']=json.loads(cfg.read_text(encoding='utf-8-sig')).get('version')
        except Exception as exc:self.errors.append('d/formal.sqlite3: '+type(exc).__name__)
        finally:
            if db is not None:db.close()
        return out
    def pool(self):
        out={};db=None
        try:
            db=self._connect('strategy-pool.sqlite3')
            if db is not None:
                q='SELECT s.id,s.active_version,s.enabled,s.paused_at,s.archived_at,a.cash,a.position,a.realized,a.last_cycle,a.last_status,a.last_action,v.document FROM strategies s LEFT JOIN accounts a ON a.strategy_id=s.id LEFT JOIN versions v ON v.strategy_id=s.id AND v.version=s.active_version'
                for row in db.execute(q):
                    a=dict(row);sid=a['id'];doc=json.loads(a.get('document') or '{}');pos=json.loads(a['position']) if a.get('position') else None
                    fills=[dict(x) for x in db.execute('SELECT version,cycle,side,qty,price,fee,slippage,reason,idempotency_key,created_at FROM fills WHERE strategy_id=? ORDER BY id',(sid,))]
                    fees=[dec(f.get('fee')) for f in fills]; cash=dec(a.get('cash'))
                    out[sid]={'id':sid,'active_version':a['active_version'],'enabled':bool(a['enabled']),'status':a.get('last_status'),'equity':str(cash) if cash is not None and pos is None else None,'cash':a.get('cash'),'realized':a.get('realized'),'unrealized':'0' if pos is None else None,'last_cycle':a.get('last_cycle'),'position':pos,'fees':str(sum(fees,Decimal(0))) if all(v is not None for v in fees) else None,'fills':fills,'document':doc}
        except Exception as exc:self.errors.append('strategy-pool.sqlite3: '+type(exc).__name__)
        finally:
            if db is not None:db.close()
        for sid in ('PA-09','PA-10'):
            db=None
            try:
                db=self._connect('pa-v1-extensions/'+sid+'.sqlite3')
                if db is None:continue
                row=db.execute('SELECT value FROM snapshot').fetchone()
                if row is None:continue
                s=json.loads(row['value']);fills=[json.loads(r['value']) for r in db.execute('SELECT value FROM fills ORDER BY rowid')];pos=s.get('position');cash=dec(s.get('cash'))
                out[sid]={'id':sid,'active_version':s.get('version'),'enabled':s.get('enabled'),'status':s.get('last_status'),'equity':str(cash) if cash is not None and not pos else None,'cash':s.get('cash'),'realized':None,'unrealized':'0' if not pos else None,'last_cycle':s.get('last'),'position':pos,'fees':None,'fills':fills,'document':{}}
            except Exception as exc:self.errors.append(sid+'.sqlite3: '+type(exc).__name__)
            finally:
                if db is not None:db.close()
        return out

class EmptyStore:
    errors=()
    def pool(self):return {}
    def d_status(self):return {}
    def paper_evidence(self,agent):return [],[]


def audited_calendar_events(trades,fills,audit):
    """Calendar uses the ledger's persisted net, never a reconstructed mark."""
    from datetime import timedelta
    zone=timezone(timedelta(hours=8));by_trade={str(t.get('trade_id')):t for t in trades};events=[];seen=set()
    for row in audit:
        if row.get('kind')!='POSITION_CLOSED':continue
        try:payload=json.loads(row.get('payload') or '{}')
        except (TypeError,ValueError):continue
        tid=str(payload.get('trade_id') or '');fid=str(payload.get('exit_fill_id') or '');net=dec(payload.get('net_pnl'))
        matching=[f for f in fills if str(f.get('fill_id'))==fid and str(f.get('trade_id'))==tid and f.get('position_effect')=='CLOSE']
        if tid not in by_trade or len(matching)!=1 or net is None or fid in seen:continue
        at=_aware_utc(matching[0].get('occurred_at'))
        if at is None:continue
        seen.add(fid);events.append({'date':at.astimezone(zone).date().isoformat(),'time':matching[0]['occurred_at'],'net_pnl':str(net),'trade_id':tid,'source':'POSITION_CLOSED审计净盈亏'})
    return sorted(events,key=lambda x:x['time'])


def pool_calendar_events(trades):
    from datetime import timedelta
    zone=timezone(timedelta(hours=8));events=[]
    for trade in trades:
        if trade.get('verification')!='VERIFIED_LIFECYCLE':continue
        at=_aware_utc(trade.get('closed_at'));net=dec(trade.get('net_pnl'))
        if at is None or net is None:continue
        events.append({'date':at.astimezone(zone).date().isoformat(),'time':trade['closed_at'],'net_pnl':str(net),'trade_id':trade['trade_id'],'source':'已核验单持仓生命周期'})
    return sorted(events,key=lambda x:x['time'])
