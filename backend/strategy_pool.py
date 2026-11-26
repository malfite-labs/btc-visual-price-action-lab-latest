"""Sandboxed, declarative strategy pool primitives.

Uploaded strategies are data only. This module deliberately has no eval/exec,
plugin loading, filesystem references from documents, or dynamic imports.
"""
from __future__ import annotations

import hashlib, json, math, sqlite3, threading
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

from backend.domain import BookLevel, CostModel, calculate_position_size

FORMAT = 'btc-lab-declarative-strategy/v1'
FORMAT_V2 = 'btc-lab-declarative-strategy/v2'
MAX_BYTES, MAX_DEPTH, MAX_NODES, MAX_LOOKBACK = 64_000, 12, 256, 200
OPS_V1 = {'and','or','not','gt','gte','lt','lte','eq','crosses_above','crosses_below','sma','ema','rsi','close','volume','param'}
OPS_V2 = (OPS_V1 - {'crosses_above', 'crosses_below'}) | {'open','high','low','atr','highest_high','lowest_low','body_fraction'}
FIELDS_V1 = {'format','strategy_id','version','name','market','timeframe','description','parameters','risk','entry','exit'}
FIELDS_V2 = FIELDS_V1 - {'exit'} | {'position'}

class StrategyValidationError(ValueError): pass

@contextmanager
def _database(path, timeout=10):
    """Commit/rollback a transaction and always close the Windows file handle."""
    conn = sqlite3.connect(str(path), timeout=timeout)
    try:
        with conn:
            yield conn
    finally:
        conn.close()

def _finite(x):
    if isinstance(x, bool) or not isinstance(x, (int,float,Decimal)) or not math.isfinite(float(x)):
        raise StrategyValidationError('数值必须为有限数字')
    return Decimal(str(x))

def _walk(node, depth=0, count=None, version=1, defined_params=None):
    if count is None: count=[0]
    count[0]+=1
    if depth>MAX_DEPTH or count[0]>MAX_NODES: raise StrategyValidationError('表达式深度或节点数超限')
    if isinstance(node, (int,float,Decimal)):
        _finite(node); return
    if isinstance(node, list):
        if len(node)>MAX_NODES: raise StrategyValidationError('数组长度超限')
        for x in node: _walk(x,depth+1,count,version,defined_params)
        return
    if not isinstance(node,dict): raise StrategyValidationError('表达式必须为对象或有限数字')
    if set(node)!={'op','args'}: raise StrategyValidationError('表达式仅允许 op/args 字段')
    op,args=node['op'],node['args']
    if op not in (OPS_V2 if version == 2 else OPS_V1) or not isinstance(args,list): raise StrategyValidationError(f'未知或当前版本不支持的运算符：{op}')
    arity={'not':1,'gt':2,'gte':2,'lt':2,'lte':2,'eq':2,'crosses_above':2,'crosses_below':2,'sma':1,'ema':1,'rsi':1,'close':1,'volume':1,'param':1}
    if version == 2:
        arity.update({'open':1,'high':1,'low':1,'atr':(1,2),'highest_high':2,'lowest_low':2,'body_fraction':1})
        arity['sma']=(1,2)
    if op in arity:
        allowed=arity[op] if isinstance(arity[op],tuple) else (arity[op],)
        if len(args) not in allowed: raise StrategyValidationError(f'{op} 参数数量错误')
    if op in ('and','or') and not 2<=len(args)<=16: raise StrategyValidationError(f'{op} 需要 2–16 个子条件')
    if version == 1 and op in ('sma','ema','rsi','close','volume') or version == 2 and op in ('ema','rsi'):
        n=args[0]
        if not isinstance(n,int) or isinstance(n,bool) or not 1<=n<=MAX_LOOKBACK: raise StrategyValidationError('周期/回看长度必须为 1–200 整数')
    if version == 2 and op in ('open','high','low','close','volume','body_fraction'):
        offset=args[0]
        if not isinstance(offset,int) or isinstance(offset,bool) or not 0<=offset<MAX_LOOKBACK: raise StrategyValidationError('v2 bar offset 必须为 0–199 整数')
    if version == 2 and op in ('sma','atr'):
        period=args[0];offset=args[1] if len(args)==2 else 0
        if not isinstance(period,int) or isinstance(period,bool) or not 1<=period<=MAX_LOOKBACK: raise StrategyValidationError('指标周期必须为 1–200 整数')
        if not isinstance(offset,int) or isinstance(offset,bool) or not 0<=offset<MAX_LOOKBACK or period+offset>MAX_LOOKBACK: raise StrategyValidationError('指标偏移/历史窗口超限')
    if version == 2 and op in ('highest_high','lowest_low'):
        period,offset=args
        if not isinstance(period,int) or isinstance(period,bool) or not 1<=period<=MAX_LOOKBACK: raise StrategyValidationError('极值周期必须为 1–200 整数')
        if not isinstance(offset,int) or isinstance(offset,bool) or not 0<=offset<MAX_LOOKBACK or period+offset>MAX_LOOKBACK: raise StrategyValidationError('极值偏移/历史窗口超限')
    if op=='param' and (not isinstance(args[0],str) or len(args[0])>48): raise StrategyValidationError('参数名无效')
    for x in args:
        if isinstance(x,str) and op=='param':
            if not x.isidentifier(): raise StrategyValidationError('参数引用名无效')
            if defined_params is not None and x not in defined_params:
                raise StrategyValidationError(f'参数引用未定义：{x}')
            continue
        _walk(x,depth+1,count,version,defined_params)

def validate_strategy(raw: bytes|str|dict):
    if isinstance(raw,bytes):
        if len(raw)>MAX_BYTES: raise StrategyValidationError('文件超过 64KB')
        raw=raw.decode('utf-8')
    if isinstance(raw,str):
        if len(raw.encode())>MAX_BYTES: raise StrategyValidationError('文件超过 64KB')
        try: doc=json.loads(raw,parse_constant=lambda _: (_ for _ in ()).throw(StrategyValidationError('NaN/Infinity 禁止')))
        except (ValueError,UnicodeError) as e: raise StrategyValidationError(f'JSON 无效：{e}') from e
    else: doc=raw
    if not isinstance(doc,dict) or doc.get('format') not in (FORMAT,FORMAT_V2): raise StrategyValidationError('不支持的格式版本')
    strategy_format=doc['format'];version=2 if strategy_format==FORMAT_V2 else 1
    expected_fields=FIELDS_V2 if version==2 else FIELDS_V1
    if set(doc)!=expected_fields: raise StrategyValidationError('字段必须且只能包含：'+', '.join(sorted(expected_fields)))
    if not isinstance(doc['strategy_id'],str) or not 1<=len(doc['strategy_id'])<=48 or not all(c.isalnum() or c in '-_' for c in doc['strategy_id']): raise StrategyValidationError('strategy_id 仅允许字母、数字、-_')
    if not isinstance(doc['version'],str) or not doc['version'] or len(doc['version'])>32: raise StrategyValidationError('version 必须为非空字符串')
    if not isinstance(doc['name'],str) or not doc['name'] or len(doc['name'])>80: raise StrategyValidationError('name 无效')
    if doc['market']!='BTC/USD' or doc['timeframe']!='15m': raise StrategyValidationError('仅支持 BTC/USD 15m')
    if not isinstance(doc['description'],str) or len(doc['description'])>1000: raise StrategyValidationError('description 无效')
    params=doc['parameters']
    if not isinstance(params,dict) or len(params)>32: raise StrategyValidationError('parameters 必须为最多 32 项对象')
    for k,v in params.items():
        if not isinstance(k,str) or not k.isidentifier(): raise StrategyValidationError('参数名无效')
        _finite(v)
    risk=doc['risk']
    if risk!={'initial_equity':'10000','currency':'USD_RESEARCH','max_leverage':5,'risk_fraction':'0.02'}: raise StrategyValidationError('风险设置必须使用固定研究账户：$10000、5x 上限、2% 风险')
    defined_params=set(params.keys())
    if version == 1:
        _walk(doc['entry'],version=1,defined_params=defined_params);_walk(doc['exit'],version=1,defined_params=defined_params)
    else:
        entry=doc['entry']
        if not isinstance(entry,dict) or set(entry)!={'long','short'}: raise StrategyValidationError('v2 entry 必须包含 long 和 short 条件')
        _walk(entry['long'],version=2,defined_params=defined_params);_walk(entry['short'],version=2,defined_params=defined_params)
        _validate_v2_position(doc['position'])
    encoded=json.dumps(doc,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode()
    return doc,hashlib.sha256(encoded).hexdigest()


def _validate_v2_position(position):
    fields={'direction','entry_timing','stop','target_r','max_hold_bars','same_bar_priority'}
    if not isinstance(position,dict) or set(position)!=fields: raise StrategyValidationError('v2 position 字段无效')
    if position['direction'] not in ('long','short','both') or position['entry_timing']!='next_open' or position['same_bar_priority']!='stop': raise StrategyValidationError('v2 direction、entry_timing 或 same_bar_priority 无效')
    stop=position['stop'];stop_fields={'kind','lookback','atr_period','buffer_atr','min_distance_atr','max_distance_atr'}
    if not isinstance(stop,dict) or set(stop)!=stop_fields or stop['kind']!='swing_atr': raise StrategyValidationError('v2 stop 配置无效')
    for key in ('lookback','atr_period'):
        if not isinstance(stop[key],int) or isinstance(stop[key],bool) or not 1<=stop[key]<=MAX_LOOKBACK: raise StrategyValidationError(f'v2 stop {key} 超限')
    buffer=_finite(stop['buffer_atr']);minimum=_finite(stop['min_distance_atr']);maximum=_finite(stop['max_distance_atr'])
    if buffer<0 or minimum<=0 or maximum<minimum: raise StrategyValidationError('v2 ATR 风险距离无效')
    target=_finite(position['target_r'])
    hold=position['max_hold_bars']
    if target<=0 or not isinstance(hold,int) or isinstance(hold,bool) or not 1<=hold<=10_000: raise StrategyValidationError('v2 target_r/max_hold_bars 无效')

def _series(name, period, bars):
    if name in ('close','volume'):
        vals=[Decimal(str(b[name])) for b in bars]
        if period>len(vals): raise StrategyValidationError('历史数据不足')
        return vals[-period]
    vals=[Decimal(str(b['close'])) for b in bars]
    if period>len(vals): raise StrategyValidationError('历史数据不足')
    if name=='sma': return sum(vals[-period:],Decimal(0))/period
    if name=='ema':
        alpha=Decimal(2)/Decimal(period+1); cur=sum(vals[:period],Decimal(0))/period
        for v in vals[period:]: cur=alpha*v+(1-alpha)*cur
        return cur
    if name=='rsi':
        if len(vals)<period+1: raise StrategyValidationError('RSI 历史数据不足')
        diffs=[vals[i]-vals[i-1] for i in range(len(vals)-period,len(vals))]
        gains=sum((max(x,Decimal(0)) for x in diffs),Decimal(0))/period
        losses=sum((max(-x,Decimal(0)) for x in diffs),Decimal(0))/period
        return Decimal(50) if gains==losses==0 else Decimal(100) if losses==0 else Decimal(100)-Decimal(100)/(1+gains/losses)
    raise StrategyValidationError('未登记指标')


def _v2_value(expr,bars,params):
    if isinstance(expr,(int,float,Decimal)) and not isinstance(expr,bool): return _finite(expr)
    if not isinstance(expr,dict): raise StrategyValidationError('v2 表达式节点无效')
    op,args=expr['op'],expr['args']
    if op=='param':
        if args[0] not in params: raise StrategyValidationError('未定义参数：'+args[0])
        return _finite(params[args[0]])
    if op in ('and','or'):
        values=[bool(_v2_value(item,bars,params)) for item in args]
        return all(values) if op=='and' else any(values)
    if op=='not': return not bool(_v2_value(args[0],bars,params))
    if op in ('gt','gte','lt','lte','eq'):
        left,right=(_v2_value(item,bars,params) for item in args)
        return {'gt':left>right,'gte':left>=right,'lt':left<right,'lte':left<=right,'eq':left==right}[op]
    if op in ('open','high','low','close','volume'):
        offset=args[0];index=len(bars)-1-offset
        if index<0: raise StrategyValidationError('历史数据不足')
        return Decimal(str(bars[index][op]))
    if op in ('sma','ema','rsi'):
        period=args[0];offset=args[1] if len(args)>1 else 0;end=len(bars)-offset
        if end<period: raise StrategyValidationError('历史数据不足')
        if op=='sma':
            values=[Decimal(str(bar['close'])) for bar in bars[end-period:end]]
            return sum(values,Decimal(0))/period
        return _series(op,period,bars[:end])
    if op=='atr':
        period=args[0];offset=args[1] if len(args)>1 else 0;end=len(bars)-offset;start=end-period
        if start<0: raise StrategyValidationError('历史数据不足')
        true_ranges=[]
        for index in range(start,end):
            bar=bars[index];high=Decimal(str(bar['high']));low=Decimal(str(bar['low']))
            if index==0: true_ranges.append(high-low);continue
            previous=Decimal(str(bars[index-1]['close']))
            true_ranges.append(max(high-low,abs(high-previous),abs(low-previous)))
        return sum(true_ranges,Decimal(0))/period
    if op in ('highest_high','lowest_low'):
        period,offset=args;end=len(bars)-offset;start=end-period
        if start<0: raise StrategyValidationError('历史数据不足')
        values=[Decimal(str(bar['high' if op=='highest_high' else 'low'])) for bar in bars[start:end]]
        return max(values) if op=='highest_high' else min(values)
    if op=='body_fraction':
        offset=args[0];index=len(bars)-1-offset
        if index<0: raise StrategyValidationError('历史数据不足')
        bar=bars[index];span=Decimal(str(bar['high']))-Decimal(str(bar['low']))
        return abs(Decimal(str(bar['close']))-Decimal(str(bar['open'])))/span if span>0 else Decimal(0)
    raise StrategyValidationError('v2 未登记运算符')


def evaluate_v2(expr,bars,params):
    """Evaluate a bounded v2 expression against closed bars; offset 0 is the latest bar."""
    return bool(_v2_value(expr,bars,params))


def evaluate_entry_v2(doc,bars):
    long=evaluate_v2(doc['entry']['long'],bars,doc['parameters'])
    short=evaluate_v2(doc['entry']['short'],bars,doc['parameters'])
    if long and short: return None
    if long and doc['position']['direction'] in ('long','both'): return 'LONG'
    if short and doc['position']['direction'] in ('short','both'): return 'SHORT'
    return None


def explain_v2(expr,bars,params):
    """Return a bounded expression tree with each boolean condition's result."""
    op=expr['op']; args=expr['args']
    if op in ('and','or'):
        children=[explain_v2(child,bars,params) for child in args]
        result=all(x['result'] for x in children) if op=='and' else any(x['result'] for x in children)
        return {'op':op,'result':result,'children':children}
    if op=='not':
        child=explain_v2(args[0],bars,params)
        return {'op':op,'result':not child['result'],'children':[child]}
    value=_v2_value(expr,bars,params)
    return {'op':op,'args':args,'result':bool(value),'value':str(value) if isinstance(value,Decimal) else value}


def explain_entry_v2(doc,bars):
    """Explain both sides so WAIT decisions have actionable rule-level reasons."""
    long=explain_v2(doc['entry']['long'],bars,doc['parameters'])
    short=explain_v2(doc['entry']['short'],bars,doc['parameters'])
    direction=doc.get('position',{}).get('direction','long')
    reasons=[]
    if direction in ('long','both') and not long['result']: reasons.append('LONG 条件未全部满足')
    if direction in ('short','both') and not short['result']: reasons.append('SHORT 条件未全部满足')
    if long['result'] and short['result']: reasons.append('多空同时触发，拒绝开仓')
    return {'long':long,'short':short,'reasons':reasons}


def build_v2_stop(doc,direction,bars):
    """Return the signal-time swing/ATR stop and an optional stable rejection reason."""
    cfg=doc['position']['stop'];lookback=cfg['lookback'];atr_period=cfg['atr_period']
    if len(bars)<max(lookback,atr_period+1): return None,'INSUFFICIENT_WARMUP'
    current=bars[-1];atr_value=_v2_value({'op':'atr','args':[atr_period,0]},bars,{})
    if atr_value<=0: return None,'INVALID_ATR'
    recent=bars[-lookback:]
    if direction=='LONG': stop=min(Decimal(str(bar['low'])) for bar in recent)-atr_value*_finite(cfg['buffer_atr'])
    elif direction=='SHORT': stop=max(Decimal(str(bar['high'])) for bar in recent)+atr_value*_finite(cfg['buffer_atr'])
    else: return None,'INVALID_DIRECTION'
    close=Decimal(str(current['close']));distance=abs(close-stop)/atr_value
    if distance<_finite(cfg['min_distance_atr']) or distance>_finite(cfg['max_distance_atr']): return None,'STOP_DISTANCE_OUT_OF_RANGE'
    return stop,None

def evaluate(expr,bars,params,previous_bars=None):
    op,args=expr['op'],expr['args']
    def ev(x):
        if isinstance(x,(int,float,str,Decimal)) and not isinstance(x,bool):
            return _finite(x) if not isinstance(x,str) else x
        q,a=x['op'],x['args']
        if q=='param':
            if a[0] not in params: raise StrategyValidationError('未定义参数：'+a[0])
            return _finite(params[a[0]])
        if q in ('close','volume','sma','ema','rsi'): return _series(q,a[0],bars)
        if q=='and': return all(bool(ev(t)) for t in a)
        if q=='or': return any(bool(ev(t)) for t in a)
        if q=='not': return not bool(ev(a[0]))
        if q in ('crosses_above','crosses_below'):
            if not previous_bars: return False
            old=ev_prev(a[0]) ; old2=ev_prev(a[1]); new1,new2=ev(a[0]),ev(a[1])
            return old<=old2 and new1>new2 if q=='crosses_above' else old>=old2 and new1<new2
        x,y=ev(a[0]),ev(a[1])
        return {'gt':lambda:x>y,'gte':lambda:x>=y,'lt':lambda:x<y,'lte':lambda:x<=y,'eq':lambda:x==y}[q]()
    def ev_prev(x): return _series(x['op'],x['args'][0],previous_bars) if isinstance(x,dict) and x.get('op') in ('close','volume','sma','ema','rsi') else ev(x)
    return bool(ev(expr))

class StrategyPool:
    """Immutable versions and isolated paper accounts with explicit DB lifetimes."""
    def __init__(self, path):
        self.path = str(path)
        self.backtest_path = str(Path(self.path).with_name(Path(self.path).stem + '-backtests.sqlite3'))
        self.lock = threading.RLock()
        with _database(self.path) as c:
            c.executescript('''CREATE TABLE IF NOT EXISTS strategies(id TEXT PRIMARY KEY,active_version TEXT,enabled INTEGER NOT NULL DEFAULT 0,paused_at TEXT,archived_at TEXT);
            CREATE TABLE IF NOT EXISTS versions(strategy_id TEXT NOT NULL,version TEXT NOT NULL,sha256 TEXT NOT NULL,document TEXT NOT NULL,uploaded_at TEXT NOT NULL,enabled_at TEXT,PRIMARY KEY(strategy_id,version),UNIQUE(strategy_id,sha256));
            CREATE TABLE IF NOT EXISTS accounts(strategy_id TEXT PRIMARY KEY,cash TEXT NOT NULL,position TEXT,realized TEXT NOT NULL DEFAULT '0',last_cycle TEXT,last_status TEXT,last_action TEXT);
            CREATE TABLE IF NOT EXISTS fills(id INTEGER PRIMARY KEY AUTOINCREMENT,strategy_id TEXT NOT NULL,version TEXT NOT NULL,cycle TEXT NOT NULL,side TEXT NOT NULL,qty TEXT NOT NULL,price TEXT NOT NULL,fee TEXT NOT NULL,slippage TEXT NOT NULL,reason TEXT NOT NULL,idempotency_key TEXT NOT NULL UNIQUE,created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS backtests(id TEXT PRIMARY KEY,strategy_id TEXT,version TEXT,result TEXT,created_at TEXT);''')
            strategy_columns = {r[1] for r in c.execute('PRAGMA table_info(strategies)').fetchall()}
            if 'archived_at' not in strategy_columns: c.execute('ALTER TABLE strategies ADD COLUMN archived_at TEXT')
            c.execute('''CREATE TABLE IF NOT EXISTS lifecycle_events(
                event_id TEXT PRIMARY KEY,strategy_id TEXT NOT NULL,action TEXT NOT NULL,
                version TEXT,details TEXT NOT NULL,occurred_at TEXT NOT NULL)''')
            c.execute('''CREATE TABLE IF NOT EXISTS gap_reconciliations(
                reconciliation_id TEXT PRIMARY KEY,strategy_id TEXT NOT NULL,from_cycle TEXT NOT NULL,
                through_cycle TEXT NOT NULL,state TEXT NOT NULL,evidence_json TEXT NOT NULL,
                created_at TEXT NOT NULL,completed_at TEXT,processing_cycle TEXT,
                UNIQUE(strategy_id,from_cycle,through_cycle))''')
            c.execute('''CREATE TABLE IF NOT EXISTS decision_diagnostics(
                strategy_id TEXT NOT NULL,cycle TEXT NOT NULL,version TEXT NOT NULL,
                action TEXT NOT NULL,details TEXT NOT NULL,created_at TEXT NOT NULL,
                PRIMARY KEY(strategy_id,cycle))''')
            columns = {r[1] for r in c.execute('PRAGMA table_info(accounts)').fetchall()}
            if 'last_status' not in columns: c.execute('ALTER TABLE accounts ADD COLUMN last_status TEXT')
            if 'last_action' not in columns: c.execute('ALTER TABLE accounts ADD COLUMN last_action TEXT')
        with _database(self.backtest_path) as c:
            c.execute('CREATE TABLE IF NOT EXISTS reports(id TEXT PRIMARY KEY,strategy_id TEXT,version TEXT,result TEXT,created_at TEXT)')

    @staticmethod
    def _doc(c, sid, version):
        row = c.execute('SELECT document FROM versions WHERE strategy_id=? AND version=?', (sid, version)).fetchone()
        if not row: raise StrategyValidationError('版本不存在')
        return json.loads(row['document'] if isinstance(row, sqlite3.Row) else row[0])

    @staticmethod
    def _required_bars(doc):
        periods = []
        def visit(node):
            if isinstance(node, dict):
                op=node.get('op');args=node.get('args',[])
                if op in ('sma','ema','rsi','close','volume') and args:
                    if doc.get('format')==FORMAT_V2:
                        offset=int(args[1]) if len(args)>1 and isinstance(args[1],int) else 0
                        periods.append((int(args[0])+offset+(1 if op=='rsi' else 0)) if op in ('sma','ema','rsi') else int(args[0])+1)
                    else:periods.append(int(args[0])+(1 if op=='rsi' else 0))
                if doc.get('format')==FORMAT_V2 and op in ('highest_high','lowest_low') and len(args)==2:
                    periods.append(int(args[0])+int(args[1]))
                if doc.get('format')==FORMAT_V2 and op=='atr' and args:
                    periods.append(int(args[0])+(int(args[1]) if len(args)>1 else 0))
                if doc.get('format')==FORMAT_V2 and op in ('open','high','low','body_fraction') and args:
                    periods.append(int(args[0])+1)
                for value in node.values(): visit(value)
            elif isinstance(node, list):
                for value in node: visit(value)
        visit(doc['entry'])
        if doc.get('format') == FORMAT:
            visit(doc['exit'])
        else:
            stop=doc['position']['stop'];periods.extend([stop['lookback'],stop['atr_period']+1])
        return max(periods or [1])

    @staticmethod
    def _validate_bars(bars, allow_gaps=False):
        if not isinstance(bars, list) or not bars: raise StrategyValidationError('K线数据为空')
        normalized=[]; previous=None
        for bar in bars:
            try:
                item={key: Decimal(str(bar[key])) for key in ('open','high','low','close','volume')}
                ts=bar.get('close_at') or bar.get('timestamp') or bar.get('open_at')
                if not ts or any(not v.is_finite() for v in item.values()): raise ValueError()
                if min(item[k] for k in ('open','high','low','close'))<=0 or item['volume']<0: raise ValueError()
                if item['high'] < max(item['open'],item['close'],item['low']) or item['low'] > min(item['open'],item['close'],item['high']): raise ValueError()
                parsed=datetime.fromisoformat(str(ts).replace('Z','+00:00'))
                if parsed.tzinfo is None: raise ValueError()
                if previous and (parsed-previous).total_seconds()<=0: raise StrategyValidationError('K线乱序或重复')
                if previous and not allow_gaps and (parsed-previous).total_seconds()!=900: raise StrategyValidationError('K线不连续或乱序')
                previous=parsed
                normalized.append({**bar, **{k: float(v) for k,v in item.items()}})
            except StrategyValidationError: raise
            except (KeyError,TypeError,ValueError,InvalidOperation) as exc: raise StrategyValidationError('K线字段或OHLCV无效') from exc
        return normalized

    @staticmethod
    def _new_position(strategy_id, doc, bars, cycle, fee_rate, slippage, quantity_step, min_quantity, min_notional, cash, entry_reference=None, direction='LONG', stop_price=None):
        close=Decimal(str(bars[-1]['close']));direction=direction.upper();side=Decimal(1) if direction=='LONG' else Decimal(-1)
        stop=Decimal(str(stop_price)) if stop_price is not None else Decimal(str(bars[-1]['low']))
        reference=Decimal(entry_reference) if entry_reference is not None else close
        entry=reference*(1+slippage*side);equity=Decimal(cash)
        if stop<=0 or (direction=='LONG' and entry<=stop) or (direction=='SHORT' and entry>=stop) or direction not in ('LONG','SHORT'): raise StrategyValidationError('止损无效或风险距离不为正')
        cost=CostModel(fee_rate=fee_rate,adverse_slippage_bps=slippage*Decimal(10000),quantity_step=quantity_step,minimum_quantity=min_quantity,minimum_notional=min_notional)
        sized=calculate_position_size(equity=equity,available_cash=equity,entry_price=entry,stop_price=stop,direction=direction,bids=[BookLevel(entry,Decimal('1e30'))],asks=[BookLevel(entry,Decimal('1e30'))],cost=cost,risk_fraction=Decimal('0.02'),leverage=Decimal('5'))
        if not sized.accepted:
            reason={'BELOW_MINIMUM':'数量低于最小成交要求','STOP_ON_WRONG_SIDE':'止损无效','INVALID_INPUT':'权益或报价无效','LEVERAGE_OR_CASH_LIMIT':'保证金不足','RISK_LIMIT_AFTER_ROUNDING':'仓位舍入后超过风险上限'}.get(sized.reason,sized.reason or '仓位风险校验失败')
            raise StrategyValidationError(reason)
        qty=sized.quantity; notional=sized.notional; fee=notional*fee_rate; margin=notional/Decimal('5')
        pos={'version':doc['version'],'entry':str(entry),'stop':str(stop),'qty':str(qty),'entry_fee':str(fee),'margin':str(margin),'fee_rate':str(fee_rate),'slippage_rate':str(slippage),'opened_cycle':cycle,'mark':str(entry)}
        if doc.get('format')==FORMAT_V2:
            target_r=Decimal(str(doc['position']['target_r']));pos.update({'direction':direction,'target':str(entry+abs(entry-stop)*target_r*side),'bars_held':0,'max_hold_bars':doc['position']['max_hold_bars']})
            target=Decimal(pos['target']);exit_target=target*(1-slippage*side);target_gross=(exit_target-entry)*side
            if target_gross<=(entry+exit_target)*fee_rate: raise StrategyValidationError('TARGET_CANNOT_COVER_COSTS')
        return pos, fee, notional*slippage

    @staticmethod
    def _exit_reason(pos, doc, bars):
        close=Decimal(str(bars[-1]['close'])); low=Decimal(str(bars[-1]['low']))
        if low<=Decimal(pos['stop']): return 'STOP', min(close,Decimal(pos['stop']))
        if evaluate(doc['exit'],bars,doc['parameters'],previous_bars=bars[:-1]): return 'RULE', close
        return None, None

    @staticmethod
    def _exit_reason_v2(pos,doc,bars,bars_held):
        bar=bars[-1];direction=pos['direction'];stop=Decimal(pos['stop']);target=Decimal(pos['target'])
        op=Decimal(str(bar['open']));high=Decimal(str(bar['high']));low=Decimal(str(bar['low']));close=Decimal(str(bar['close']))
        stop_hit=low<=stop if direction=='LONG' else high>=stop
        target_hit=high>=target if direction=='LONG' else low<=target
        if stop_hit:
            reference=min(stop,op) if direction=='LONG' else max(stop,op)
            return 'STOP',reference
        if target_hit:return 'TARGET',target
        if bars_held>=int(doc['position']['max_hold_bars']):return 'TIME',close
        return None,None

    @staticmethod
    def _close_position(pos, doc, bars, fee_rate, slippage):
        v2=doc.get('format')==FORMAT_V2
        if v2:
            held=int(pos.get('bars_held',0))+1
            reason,reference=StrategyPool._exit_reason_v2(pos,doc,bars,held)
        else:reason,reference=StrategyPool._exit_reason(pos,doc,bars)
        if not reason: return None
        qty=Decimal(pos['qty']); entry=Decimal(pos['entry']);direction=pos.get('direction','LONG');side=Decimal(1) if direction=='LONG' else Decimal(-1);px=reference*(1-slippage*side)
        gross=(px-entry)*qty*side; fee=abs(px*qty)*fee_rate
        net=gross-Decimal(pos.get('entry_fee','0'))-fee
        return {'price':px,'gross':gross,'fee':fee,'net':net,'reason':reason,'slippage':abs(reference*qty-px*qty),'direction':direction}

    def upload(self, content):
        doc,digest=validate_strategy(content);now=datetime.now(timezone.utc).isoformat()
        with self.lock, _database(self.path) as c:
            c.execute('BEGIN IMMEDIATE')
            if c.execute('select 1 from versions where strategy_id=? and version=?',(doc['strategy_id'],doc['version'])).fetchone(): raise StrategyValidationError('版本已存在；版本不可覆盖')
            c.execute('insert or ignore into strategies(id) values(?)',(doc['strategy_id'],))
            c.execute('insert into versions values(?,?,?,?,?,NULL)',(doc['strategy_id'],doc['version'],digest,json.dumps(doc,sort_keys=True),now))
            c.execute("insert or ignore into accounts(strategy_id,cash,realized) values(?,'10000.00','0')",(doc['strategy_id'],))
        return {'strategy_id':doc['strategy_id'],'version':doc['version'],'sha256':digest,'enabled':False,'document':doc}

    def list(self):
        with _database(self.path) as c:
            c.row_factory=sqlite3.Row; now=datetime.now(timezone.utc)
            items=[]
            rows=c.execute('select s.id,s.active_version,s.enabled,s.paused_at,s.archived_at,a.cash,a.position,a.realized,a.last_cycle,a.last_status,a.last_action from strategies s join accounts a on a.strategy_id=s.id order by s.id').fetchall()
            for row in rows:
                item=dict(row);state_error=False
                try: pos=json.loads(item['position']) if item['position'] else None
                except (ValueError,TypeError):pos=None;state_error=True
                item['position_version']=pos.get('version') if pos else None
                active=bool(item['enabled'] and item['active_version'])
                running=False
                if active and item['last_cycle']:
                    try:
                        elapsed=(now-datetime.fromisoformat(item['last_cycle'].replace('Z','+00:00'))).total_seconds()
                        running=0<=elapsed<=1800 and not str(item.get('last_status') or '').startswith('BLOCKED')
                    except ValueError: pass
                latest_status=str(item.get('last_status') or '')
                if item.get('archived_at'):
                    item['status']='ARCHIVED'
                elif active:
                    item['status']='FAULTED' if state_error or latest_status.startswith('BLOCKED:') and 'STALE_QUOTE' not in latest_status else 'RUNNING' if running else 'ENABLED_NOT_RUNNING'
                else:
                    item['status']='FAULTED' if state_error else 'PAUSED' if item.get('paused_at') else 'REGISTERED'
                cash=Decimal(item['cash']);realized=Decimal(item['realized']); unrealized=Decimal(0); fees=Decimal(0)
                if pos:
                    item['position_direction']=('PENDING_' if pos.get('pending_entry') else '')+pos.get('direction','LONG');item['position_amount']=str(Decimal(pos['entry'])*Decimal(pos['qty'])) if not pos.get('pending_entry') else '0'
                    if not pos.get('pending_entry'):
                        mark=Decimal(pos.get('mark',pos['entry']));qty=Decimal(pos['qty'])
                        side=Decimal(1) if pos.get('direction','LONG')=='LONG' else Decimal(-1)
                        unrealized=(mark-Decimal(pos['entry']))*qty*side-abs(mark*qty)*Decimal(pos.get('fee_rate','0.0005'))-abs(mark*qty)*Decimal(pos.get('slippage_rate','0.0002'))
                else: item['position_direction']='UNKNOWN' if state_error else 'FLAT';item['position_amount']='UNKNOWN' if state_error else '0'
                if not pos: unrealized=Decimal(0)
                fills=c.execute('select fee,version,side from fills where strategy_id=?',(item['id'],)).fetchall()
                fees=sum((Decimal(f['fee']) for f in fills),Decimal(0));item['fees']=str(fees)
                item['equity']=None if state_error else str(cash+unrealized);item['unrealized']=None if state_error else str(unrealized)
                item['leverage']='UNKNOWN' if state_error else '5' if pos else None;item['action']='STATE_ERROR' if state_error else item.get('last_action') or 'NO_CYCLE'
                item['enabled']=bool(item['enabled']);items.append(item)
            return items

    def versions(self,sid):
        with _database(self.path) as c:
            c.row_factory=sqlite3.Row
            return [dict(x) for x in c.execute('select strategy_id,version,sha256,document,uploaded_at,enabled_at from versions where strategy_id=? order by uploaded_at',(sid,)).fetchall()]

    def fills(self,sid,limit=500):
        with _database(self.path) as c:
            c.row_factory=sqlite3.Row
            return [dict(x) for x in c.execute('select version,cycle,side,qty,price,fee,slippage,reason,idempotency_key,created_at from fills where strategy_id=? order by id desc limit ?',(sid,limit)).fetchall()]

    def diagnostics(self,sid,limit=200):
        with _database(self.path) as c:
            c.row_factory=sqlite3.Row
            return [dict(x) | {'details':json.loads(x['details'])} for x in c.execute(
                'select strategy_id,cycle,version,action,details,created_at from decision_diagnostics where strategy_id=? order by cycle desc limit ?',
                (sid,limit)).fetchall()]

    def diff(self,sid,old_version,new_version):
        with _database(self.path) as c:
            old=self._doc(c,sid,old_version);new=self._doc(c,sid,new_version)
        keys=sorted(set(old)|set(new));return {'from':old_version,'to':new_version,'changes':[{'field':k,'old':old.get(k),'new':new.get(k)} for k in keys if old.get(k)!=new.get(k)]}

    def enable(self,sid,version,enabled=True):
        with self.lock,_database(self.path) as c:
            c.execute('BEGIN IMMEDIATE')
            row=c.execute('select archived_at from strategies where id=?',(sid,)).fetchone()
            if not row: raise StrategyValidationError('策略不存在')
            if row[0]: raise StrategyValidationError('策略已归档；必须先执行恢复校验')
            if enabled:
                if not c.execute('select 1 from versions where strategy_id=? and version=?',(sid,version)).fetchone(): raise StrategyValidationError('版本不存在')
                c.execute('update strategies set active_version=?,enabled=1,paused_at=NULL where id=?',(version,sid))
                c.execute('update versions set enabled_at=? where strategy_id=? and version=?',(datetime.now(timezone.utc).isoformat(),sid,version))
            else:
                c.execute('update strategies set enabled=0,paused_at=? where id=?',(datetime.now(timezone.utc).isoformat(),sid))
                if not c.execute('select changes()').fetchone()[0]:raise StrategyValidationError('策略不存在')
        return {'status':'ENABLED_NOT_RUNNING' if enabled else 'REGISTERED_PAUSED'}

    def archive(self,sid):
        now=datetime.now(timezone.utc).isoformat()
        with self.lock,_database(self.path) as c:
            c.row_factory=sqlite3.Row;c.execute('BEGIN IMMEDIATE')
            row=c.execute('select s.enabled,s.active_version,s.archived_at,a.position from strategies s join accounts a on a.strategy_id=s.id where s.id=?',(sid,)).fetchone()
            if not row: raise StrategyValidationError('策略不存在')
            if row['archived_at']: return {'strategy_id':sid,'status':'ARCHIVED','archived_at':row['archived_at']}
            if row['enabled']: raise StrategyValidationError('请先暂停策略再归档')
            if row['position']:
                try: position=json.loads(row['position'])
                except (ValueError,TypeError): raise StrategyValidationError('持仓状态无法确认，禁止归档')
                if position: raise StrategyValidationError('持仓中禁止归档')
            c.execute('update strategies set archived_at=? where id=?',(now,sid))
            c.execute('insert into lifecycle_events values(?,?,?,?,?,?)',(
                hashlib.sha256(f'{sid}|ARCHIVE|{now}'.encode()).hexdigest(),sid,'ARCHIVE',row['active_version'],
                json.dumps({'account_preserved':True,'versions_preserved':True,'fills_preserved':True},sort_keys=True),now))
        return {'strategy_id':sid,'status':'ARCHIVED','archived_at':now}

    def restore(self,sid,version):
        now=datetime.now(timezone.utc).isoformat()
        with self.lock,_database(self.path) as c:
            c.row_factory=sqlite3.Row;c.execute('BEGIN IMMEDIATE')
            row=c.execute('select s.archived_at,a.position from strategies s join accounts a on a.strategy_id=s.id where s.id=?',(sid,)).fetchone()
            if not row: raise StrategyValidationError('策略不存在')
            if not row['archived_at']: raise StrategyValidationError('策略未归档')
            if row['position']:
                try: position=json.loads(row['position'])
                except (ValueError,TypeError): raise StrategyValidationError('持仓状态无法确认，禁止恢复')
                if position: raise StrategyValidationError('恢复前必须先核对持仓')
            stored=c.execute('select document,sha256 from versions where strategy_id=? and version=?',(sid,version)).fetchone()
            if not stored: raise StrategyValidationError('版本不存在')
            doc,digest=validate_strategy(stored['document'])
            if digest!=stored['sha256'] or doc['strategy_id']!=sid or doc['version']!=version:
                raise StrategyValidationError('归档版本完整性校验失败')
            c.execute('update strategies set archived_at=NULL,active_version=?,enabled=0,paused_at=? where id=?',(version,now,sid))
            c.execute('insert into lifecycle_events values(?,?,?,?,?,?)',(
                hashlib.sha256(f'{sid}|RESTORE|{version}|{now}'.encode()).hexdigest(),sid,'RESTORE',version,
                json.dumps({'validated':True,'enabled':False,'sha256':digest},sort_keys=True),now))
        return {'strategy_id':sid,'version':version,'status':'REGISTERED_PAUSED','sha256':digest}

    def lifecycle_events(self,sid):
        with _database(self.path) as c:
            c.row_factory=sqlite3.Row
            return [dict(row) for row in c.execute('select event_id,strategy_id,action,version,details,occurred_at from lifecycle_events where strategy_id=? order by occurred_at',(sid,)).fetchall()]

    def gap_reconciliations(self,sid):
        with _database(self.path) as c:
            c.row_factory=sqlite3.Row
            return [dict(row) for row in c.execute('select * from gap_reconciliations where strategy_id=? order by created_at',(sid,)).fetchall()]

    def recover_gaps(self, bars, cycle, *, healthy):
        """Reconcile untouched positions using complete closed candles before this cycle."""
        if not healthy:
            return []
        close = cycle if isinstance(cycle, datetime) else datetime.fromisoformat(str(cycle).replace('Z', '+00:00'))
        through = close - __import__('datetime').timedelta(minutes=15)
        results = []
        for account in self.list():
            if account.get('last_status') != 'BLOCKED_GAP_RECONCILIATION_REQUIRED' or not account.get('position'):
                continue
            sid = account['id']
            try:
                start = datetime.fromisoformat(str(account['last_cycle']).replace('Z', '+00:00'))
                gap = []
                for bar in bars:
                    opened = datetime.fromisoformat(str(bar['open_at']).replace('Z', '+00:00'))
                    if start <= opened < through:
                        gap.append(bar)
                audit = self.reconcile_candle_gap(sid, expected_last_cycle=start, through_cycle=through,
                    bars=gap, expected_position=json.loads(account['position']), accepted_standard='CANDLE_OHLC_PROXY_V1')
                results.append({'id': sid, 'status': audit['status']})
            except (KeyError, TypeError, ValueError, StrategyValidationError) as exc:
                results.append({'id': sid, 'status': 'BLOCKED', 'reason': str(exc)})
        return results

    def reconcile_candle_gap(self,sid,*,expected_last_cycle,through_cycle,bars,expected_position,accepted_standard):
        """Audit an operator-approved OHLC gap proxy without fabricating a processed cycle."""
        if accepted_standard!='CANDLE_OHLC_PROXY_V1':
            raise StrategyValidationError('CANDLE_PROXY_ACCEPTANCE_REQUIRED')
        def parse_time(value):
            parsed=value if isinstance(value,datetime) else datetime.fromisoformat(str(value).replace('Z','+00:00'))
            if parsed.tzinfo is None or parsed.utcoffset() is None: raise StrategyValidationError('周期时间必须含时区')
            return parsed.astimezone(timezone.utc)
        start=parse_time(expected_last_cycle);end=parse_time(through_cycle)
        seconds=(end-start).total_seconds()
        if seconds<=0 or seconds%900 or not isinstance(bars,list) or len(bars)!=int(seconds//900):
            raise StrategyValidationError('缺口K线数量或时间范围不匹配')
        checked=self._validate_bars(bars)
        previous=start
        highs=[];lows=[]
        for raw,bar in zip(bars,checked):
            if (raw.get('symbol')!='BTC/USD' or str(raw.get('interval','')).lower() not in ('15min','15m')
                    or raw.get('source')!='BINANCE_USDM_PERP' or raw.get('currency')!='USD_RESEARCH'
                    or raw.get('is_closed') is not True):
                raise StrategyValidationError('缺口K线来源或收盘状态无效')
            opened=parse_time(raw.get('open_at'));closed=parse_time(raw.get('close_at'))
            if opened!=previous or closed-opened!=__import__('datetime').timedelta(minutes=15):
                raise StrategyValidationError('缺口K线不连续')
            previous=closed;highs.append(Decimal(str(bar['high'])));lows.append(Decimal(str(bar['low'])))
        if previous!=end: raise StrategyValidationError('缺口最后K线与核对范围不符')
        now=datetime.now(timezone.utc).isoformat()
        with self.lock,_database(self.path) as c:
            c.row_factory=sqlite3.Row;c.execute('BEGIN IMMEDIATE')
            row=c.execute('''select s.active_version,s.enabled,a.position,a.last_cycle,a.last_status,a.cash,a.realized
                from strategies s join accounts a on a.strategy_id=s.id where s.id=?''',(sid,)).fetchone()
            if not row: raise StrategyValidationError('策略不存在')
            if not row['enabled'] or row['active_version']!=expected_position.get('version'):
                raise StrategyValidationError('策略未启用或持仓版本已变化')
            if parse_time(row['last_cycle'])!=start or row['last_status']!='BLOCKED_GAP_RECONCILIATION_REQUIRED':
                raise StrategyValidationError('策略周期或阻断状态已变化')
            try: position=json.loads(row['position']) if row['position'] else None
            except (ValueError,TypeError) as exc: raise StrategyValidationError('持仓JSON无效') from exc
            if not position or position.get('pending_entry') or position.get('direction') not in ('LONG','SHORT'):
                raise StrategyValidationError('没有可核对的已成交持仓')
            protected=('version','direction','entry','stop','target','qty','opened_cycle','bars_held','max_hold_bars')
            if any(position.get(key)!=expected_position.get(key) for key in protected):
                raise StrategyValidationError('持仓方向/数量/价格/根数已变化')
            version_row=c.execute('select sha256,document from versions where strategy_id=? and version=?',(sid,row['active_version'])).fetchone()
            if not version_row: raise StrategyValidationError('活动策略版本不存在')
            doc,digest=validate_strategy(version_row['document'])
            if digest!=version_row['sha256'] or doc.get('format')!=FORMAT_V2 or doc['version']!=position['version']:
                raise StrategyValidationError('策略版本校验失败或不是v2')
            if int(position.get('max_hold_bars',0))!=int(doc['position']['max_hold_bars']):
                raise StrategyValidationError('持仓时限与活动规则不一致')
            fill_rows=c.execute('select id,side,qty,cycle,reason,idempotency_key from fills where strategy_id=? order by id',(sid,)).fetchall()
            keys=[item['idempotency_key'] for item in fill_rows]
            if len(keys)!=len(set(keys)): raise StrategyValidationError('成交幂等键重复')
            signed=Decimal(0);invalid_sides=[]
            for fill in fill_rows:
                qty=Decimal(fill['qty'])
                if fill['side'] not in ('BUY','SELL'): invalid_sides.append(fill['id'])
                else: signed += qty if fill['side']=='BUY' else -qty
                fill_time=parse_time(fill['cycle'])
                if start<fill_time<=end: raise StrategyValidationError('缺口内已有成交事件，需人工核对')
            expected_signed=Decimal(position['qty'])*(Decimal(1) if position['direction']=='LONG' else Decimal(-1))
            if invalid_sides: raise StrategyValidationError('成交账本存在无效买卖方向')
            opened=parse_time(position.get('opened_cycle'))
            open_side='BUY' if position['direction']=='LONG' else 'SELL'
            matching=[f for f in fill_rows if f['side']==open_side and Decimal(f['qty'])==Decimal(position['qty'])
                      and parse_time(f['cycle'])==opened and f['reason'] in ('ENTRY','ENTRY_NEXT_OPEN')]
            if len(matching)!=1: raise StrategyValidationError('当前持仓无法唯一匹配到实际开仓成交')
            current_open_id=matching[0]['id']
            if any(f['id']>current_open_id for f in fill_rows):
                raise StrategyValidationError('当前持仓开仓成交之后存在其他成交事件，需人工核对')
            held=int(position['bars_held']);exit_checks=[]
            for index,bar in enumerate(checked,1):
                held+=1
                reason,_=self._exit_reason_v2(position,doc,[bar],held)
                if reason:
                    raise StrategyValidationError(f'缺口K线触发强制退出：{reason}，第{index}根')
                exit_checks.append({'close_at':str(bars[index-1]['close_at']),'result':'NO_EXIT'})
            pending=c.execute("select 1 from gap_reconciliations where strategy_id=? and state='AWAITING_NEXT_NATURAL_CYCLE'",(sid,)).fetchone()
            if pending: raise StrategyValidationError('已有待处理缺口核对记录')
            bar_hash=hashlib.sha256(json.dumps(bars,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()
            position_hash=hashlib.sha256(json.dumps(position,sort_keys=True,separators=(',',':')).encode()).hexdigest()
            evidence={'standard':accepted_standard,'statement':'User accepts 15-minute candle high/low proxy; missing bid/ask ticks may create simulation error.',
                      'from_cycle':start.isoformat(),'through_cycle':end.isoformat(),'bar_count':len(checked),'bars_sha256':bar_hash,
                      'position_before':position,'position_sha256':position_hash,'bars_held_before':int(position['bars_held']),
                      'bars_held_after_reconciliation':held,'max_hold_bars':int(position['max_hold_bars']),
                      'max_high':str(max(highs)),'min_low':str(min(lows)),'exit_checks':exit_checks,
                      'fill_count':len(fill_rows),'unique_idempotency_keys':len(keys),'historical_net_signed_fill_qty':str(signed),
                      'current_position_open_fill_id':current_open_id,'current_position_open_fill_matches':True,
                      'historical_fill_ledger_note':'Historical unmatched entry fills are preserved as pre-existing records; current active position is uniquely matched to its latest opening fill. No historical rows were altered.',
                      'last_cycle_unchanged_until_natural_processing':True}
            encoded=json.dumps(evidence,sort_keys=True,separators=(',',':'),ensure_ascii=False)
            rec_id=hashlib.sha256(f'{sid}|{start.isoformat()}|{end.isoformat()}|{bar_hash}|{position_hash}'.encode()).hexdigest()
            c.execute('insert into gap_reconciliations(reconciliation_id,strategy_id,from_cycle,through_cycle,state,evidence_json,created_at) values(?,?,?,?,?,?,?)',
                      (rec_id,sid,start.isoformat(),end.isoformat(),'AWAITING_NEXT_NATURAL_CYCLE',encoded,now))
            position['bars_held']=held
            c.execute('update accounts set position=?,last_status=?,last_action=? where strategy_id=?',
                      (json.dumps(position,sort_keys=True),'BLOCKED_GAP_RECONCILED_AWAITING_NATURAL_CYCLE','GAP_RECONCILED_AWAITING_NATURAL_CYCLE',sid))
        return {'strategy_id':sid,'status':'AWAITING_NEXT_NATURAL_CYCLE','reconciliation_id':rec_id,
                'from_cycle':start.isoformat(),'through_cycle':end.isoformat(),'bar_count':len(checked),
                'bars_held':held,'evidence_sha256':hashlib.sha256(encoded.encode()).hexdigest()}

    def cycle(self,bars,cycle,*,max_age_ms=60000,quote_age_ms=0,quote=None,fee_rate=Decimal('0.0005'),slippage=Decimal('0.0002'),quantity_step=Decimal('0.00000001'),min_quantity=Decimal('0.00000001'),min_notional=Decimal('1.00')):
        """Process only the natural cycle supplied by the shared market consumer."""
        cycle_dt=cycle if isinstance(cycle,datetime) else datetime.fromisoformat(str(cycle).replace('Z','+00:00'))
        if cycle_dt.tzinfo is None or cycle_dt.utcoffset() is None:raise StrategyValidationError('周期时间必须含时区')
        key=cycle_dt.isoformat(); results=[]
        try:
            checked=self._validate_bars(bars)
            latest=datetime.fromisoformat(str(bars[-1].get('close_at') or bars[-1].get('timestamp') or bars[-1].get('open_at')).replace('Z','+00:00'))
            if latest.tzinfo is None or latest.astimezone(timezone.utc)!=cycle_dt.astimezone(timezone.utc):
                raise StrategyValidationError('CYCLE_MISMATCH')
        except (ValueError,TypeError,KeyError,InvalidOperation,StrategyValidationError) as exc:
            reason='BLOCKED_CYCLE_MISMATCH' if str(exc)=='CYCLE_MISMATCH' else 'BLOCKED_INVALID_BARS:'+str(exc)
            with _database(self.path) as read_conn:
                affected=read_conn.execute('select s.id from strategies s join accounts a on a.strategy_id=s.id where s.enabled=1 or a.position is not null order by s.id').fetchall()
                stored='BLOCKED:CYCLE_MISMATCH' if reason=='BLOCKED_CYCLE_MISMATCH' else 'BLOCKED:INVALID_BARS'
                for row in affected:read_conn.execute('update accounts set last_status=?,last_action=? where strategy_id=?',(stored,reason,row[0]))
            return [{'id':row[0],'status':reason} for row in affected]
        with self.lock,_database(self.path) as c:
            c.row_factory=sqlite3.Row;c.execute('BEGIN IMMEDIATE')
            strategies=c.execute('select s.*,a.position,a.cash,a.realized,a.last_cycle from strategies s join accounts a on a.strategy_id=s.id order by s.id').fetchall()
            for index,strategy in enumerate(strategies):
                sid=strategy['id'];savepoint=f'pool_strategy_{index}';c.execute(f'SAVEPOINT {savepoint}')
                gap_audit=None;diagnostic=None
                try:
                    if strategy['archived_at']:
                        c.execute(f'RELEASE {savepoint}');results.append({'id':sid,'status':'ARCHIVED'});continue
                    pos=json.loads(strategy['position']) if strategy['position'] else None
                    gap_audit=c.execute("select reconciliation_id,through_cycle from gap_reconciliations where strategy_id=? and state='AWAITING_NEXT_NATURAL_CYCLE' order by created_at desc limit 1",(sid,)).fetchone()
                    reconciled_next_cycle=False
                    if gap_audit:
                        through_dt=datetime.fromisoformat(str(gap_audit['through_cycle']).replace('Z','+00:00'))
                        expected_next=through_dt+__import__('datetime').timedelta(minutes=15)
                        if cycle_dt.astimezone(timezone.utc)!=expected_next.astimezone(timezone.utc):
                            c.execute('update accounts set last_status=?,last_action=? where strategy_id=?',
                                      ('BLOCKED_GAP_RECONCILED_AWAITING_NATURAL_CYCLE','GAP_RECONCILED_AWAITING_NATURAL_CYCLE',sid))
                            c.execute(f'RELEASE {savepoint}');results.append({'id':sid,'status':'BLOCKED_GAP_RECONCILED_AWAITING_NATURAL_CYCLE'});continue
                        reconciled_next_cycle=True
                    if strategy['last_cycle'] and strategy['last_cycle']>=key:
                        c.execute(f'RELEASE {savepoint}');results.append({'id':sid,'status':'DUPLICATE_OR_STALE'});continue
                    if not pos and not strategy['enabled']:
                        c.execute(f'RELEASE {savepoint}');continue
                    version=pos['version'] if pos else strategy['active_version']
                    doc=self._doc(c,sid,version)
                    if len(checked)<self._required_bars(doc): raise StrategyValidationError('指标所需K线不足')
                    decision='WAIT'
                    if pos and doc.get('format')==FORMAT_V2 and not pos.get('pending_entry') and strategy['last_cycle'] and not reconciled_next_cycle:
                        prior_cycle=datetime.fromisoformat(str(strategy['last_cycle']).replace('Z','+00:00'))
                        if (cycle_dt-prior_cycle).total_seconds()!=900:
                            # Never erase a filled position just because cycle continuity was lost.
                            # Keep its ledger and last processed cycle intact; quote monitoring remains
                            # responsible for protective exits until an operator reconciles the gap.
                            decision='BLOCKED_GAP_RECONCILIATION_REQUIRED'
                            c.execute('update accounts set last_status=?,last_action=? where strategy_id=?',(decision,decision,sid))
                    if decision=='BLOCKED_GAP_RECONCILIATION_REQUIRED':
                        pass
                    elif not pos and doc.get('format')==FORMAT_V2 and (quote_age_ms<0 or quote_age_ms>max_age_ms or not self._valid_quote(quote)):
                        decision='BLOCKED:QUOTE_UNAVAILABLE_OR_STALE'
                    elif pos and doc.get('format')==FORMAT_V2 and pos.get('pending_entry'):
                        signal_dt=datetime.fromisoformat(str(pos['signal_cycle']).replace('Z','+00:00'))
                        prior_dt=datetime.fromisoformat(str(checked[-2].get('close_at') or checked[-2].get('timestamp') or checked[-2].get('open_at')).replace('Z','+00:00'))
                        status='CENSORED_GAP' if (cycle_dt-signal_dt).total_seconds()!=900 or prior_dt!=signal_dt else 'CANCELED_NO_NEXT_BAR_QUOTE'
                        action='CANCEL_PENDING_GAP' if status=='CENSORED_GAP' else 'CANCEL_PENDING_NO_QUOTE'
                        c.execute('update accounts set position=NULL,last_cycle=?,last_status=?,last_action=? where strategy_id=?',(key,status,action,sid));decision=action
                    elif pos:
                        closed=self._close_position(pos,doc,checked,Decimal(pos.get('fee_rate',fee_rate)),Decimal(pos.get('slippage_rate',slippage)))
                        if closed:
                            idem=f'{sid}:close:{key}'
                            close_side='SELL' if pos.get('direction','LONG')=='LONG' else 'BUY'
                            cursor=c.execute('insert or ignore into fills(strategy_id,version,cycle,side,qty,price,fee,slippage,reason,idempotency_key,created_at) values(?,?,?,?,?,?,?,?,?,?,?)',(sid,version,key,close_side,pos['qty'],str(closed['price']),str(closed['fee']),str(closed['slippage']),closed['reason'],idem,datetime.now(timezone.utc).isoformat()))
                            if cursor.rowcount!=1:raise StrategyValidationError('平仓订单幂等冲突')
                            c.execute('update accounts set cash=?,position=NULL,realized=?,last_cycle=?,last_status=?,last_action=? where strategy_id=?',(str(Decimal(strategy['cash'])+(closed['gross']-closed['fee'])),str(Decimal(strategy['realized'])+closed['net']),key,closed['reason'],'CLOSE_'+closed['reason'],sid));decision='CLOSE_'+closed['reason']
                        else:
                            mark=Decimal(str(checked[-1]['close']));pos['mark']=str(mark)
                            if doc.get('format')==FORMAT_V2:pos['bars_held']=int(pos.get('bars_held',0))+1
                            c.execute('update accounts set position=?,last_cycle=?,last_status=?,last_action=? where strategy_id=?',(json.dumps(pos),key,'OK','HOLD',sid))
                            side=Decimal(1) if pos.get('direction','LONG')=='LONG' else Decimal(-1)
                            unrealized=(mark-Decimal(pos['entry']))*Decimal(pos['qty'])*side
                            decision='HOLD'
                    elif doc.get('format')==FORMAT_V2 and (direction:=evaluate_entry_v2(doc,checked)) is None:
                        diagnostic=explain_entry_v2(doc,checked)
                        c.execute('update accounts set last_cycle=?,last_status=?,last_action=? where strategy_id=?',(key,'OK','WAIT',sid))
                    elif doc.get('format')==FORMAT_V2:
                        diagnostic=explain_entry_v2(doc,checked)
                        stop,rejection=build_v2_stop(doc,direction,checked)
                        if rejection:
                            diagnostic['reasons']=[rejection]
                            decision='WAIT_'+rejection
                            c.execute('update accounts set last_cycle=?,last_status=?,last_action=? where strategy_id=?',(key,'OK',decision,sid))
                        else:
                            pending={'version':version,'pending_entry':True,'direction':direction,'stop':str(stop),'signal_cycle':key}
                            c.execute('update accounts set position=?,last_cycle=?,last_status=?,last_action=? where strategy_id=?',(json.dumps(pending),key,'OK','SIGNAL_'+direction,sid));decision='SIGNAL_'+direction
                    elif not evaluate(doc['entry'],checked,doc['parameters'],previous_bars=checked[:-1]):
                        c.execute('update accounts set last_cycle=?,last_status=?,last_action=? where strategy_id=?',(key,'OK','WAIT',sid))
                    elif quote_age_ms<0 or quote_age_ms>max_age_ms: decision='BLOCKED_STALE_QUOTE'
                    elif quote is None or not self._valid_quote(quote): decision='BLOCKED_INVALID_QUOTE'
                    else:
                        direction='LONG'
                        reference=quote['ask']
                        position,fee,slip_cost=self._new_position(sid,doc,checked,key,Decimal(fee_rate),Decimal(slippage),Decimal(quantity_step),Decimal(min_quantity),Decimal(min_notional),Decimal(strategy['cash']),entry_reference=reference,direction=direction)
                        notional=Decimal(position['entry'])*Decimal(position['qty']);idem=f'{sid}:open:{key}'
                        cursor=c.execute('insert or ignore into fills(strategy_id,version,cycle,side,qty,price,fee,slippage,reason,idempotency_key,created_at) values(?,?,?,?,?,?,?,?,?,?,?)',(sid,version,key,'BUY',position['qty'],position['entry'],str(fee),str(slip_cost),'ENTRY',idem,datetime.now(timezone.utc).isoformat()))
                        if cursor.rowcount!=1:raise StrategyValidationError('开仓订单幂等冲突')
                        c.execute('update accounts set cash=?,position=?,last_cycle=?,last_status=?,last_action=? where strategy_id=?',(str(Decimal(strategy['cash'])-fee),json.dumps(position),key,'OK','OPEN_LONG',sid));decision='OPEN_LONG'
                    if decision.startswith('BLOCKED') and decision!='BLOCKED_GAP_RECONCILIATION_REQUIRED':
                        c.execute('update accounts set last_cycle=?,last_status=?,last_action=? where strategy_id=?',(key,decision,decision,sid))
                    if reconciled_next_cycle and not decision.startswith('BLOCKED'):
                        audit_row=c.execute("select evidence_json from gap_reconciliations where reconciliation_id=?",(gap_audit['reconciliation_id'],)).fetchone()
                        evidence=json.loads(audit_row['evidence_json'])
                        evidence['next_natural_cycle']={'cycle':key,'decision':decision,'processed_at':datetime.now(timezone.utc).isoformat()}
                        encoded=json.dumps(evidence,sort_keys=True,separators=(',',':'),ensure_ascii=False)
                        c.execute("update gap_reconciliations set state='COMPLETED',evidence_json=?,completed_at=?,processing_cycle=? where reconciliation_id=?",
                                  (encoded,datetime.now(timezone.utc).isoformat(),key,gap_audit['reconciliation_id']))
                    if diagnostic is not None:
                        c.execute('''INSERT OR REPLACE INTO decision_diagnostics
                            (strategy_id,cycle,version,action,details,created_at) VALUES(?,?,?,?,?,?)''',
                            (sid,key,version,decision,json.dumps(diagnostic,sort_keys=True,ensure_ascii=False),datetime.now(timezone.utc).isoformat()))
                    c.execute(f'RELEASE {savepoint}')
                except Exception as exc:
                    c.execute(f'ROLLBACK TO {savepoint}');c.execute(f'RELEASE {savepoint}')
                    detail=str(exc) if isinstance(exc,(ValueError,KeyError,InvalidOperation,TypeError,StrategyValidationError)) else 'INTERNAL_ERROR:'+type(exc).__name__
                    decision='BLOCKED:'+detail
                    if gap_audit:
                        c.execute('update accounts set last_status=?,last_action=? where strategy_id=?',('BLOCKED_GAP_RECONCILED_AWAITING_NATURAL_CYCLE',decision,sid))
                    else:
                        c.execute('update accounts set last_cycle=?,last_status=?,last_action=? where strategy_id=?',(key,decision,decision,sid))
                results.append({'id':sid,'status':decision})
        return results

    @staticmethod
    def _valid_quote(quote):
        try:
            bid=Decimal(str(quote['bid']));ask=Decimal(str(quote['ask']))
            received=datetime.fromisoformat(str(quote['received_at']).replace('Z','+00:00'))
            event=datetime.fromisoformat(str(quote['event_at']).replace('Z','+00:00'))
            now=datetime.now(timezone.utc)
            return (quote.get('timestamp_valid') is True and quote.get('source')=='BINANCE_USDM_PERP' and quote.get('symbol')=='BTC/USD' and quote.get('currency')=='USD_RESEARCH'
                    and received.tzinfo is not None and event.tzinfo is not None and received<=now and event<=received
                    and bid.is_finite() and ask.is_finite() and 0<bid<=ask)
        except (KeyError,TypeError,ValueError,InvalidOperation): return False

    def monitor_quote(self,quote,*,max_age_ms=60000,fee_rate=Decimal('0.0005'),slippage=Decimal('0.0002')):
        """Shared quote-loop stop monitor for every pool account, including paused entries."""
        if not self._valid_quote(quote) or int(quote.get('age_ms',10**9))<0 or int(quote.get('age_ms',10**9))>max_age_ms:
            return []
        bid=Decimal(str(quote['bid']));ask=Decimal(str(quote['ask']));event=str(quote['event_at']);results=[]
        event_id=hashlib.sha256((event+'|'+str(quote['bid'])+'|'+quote['source']).encode()).hexdigest()
        with self.lock,_database(self.path) as c:
            c.row_factory=sqlite3.Row;c.execute('BEGIN IMMEDIATE')
            rows=c.execute('select a.strategy_id,a.cash,a.position,a.realized from accounts a where a.position is not null order by a.strategy_id').fetchall()
            for index,row in enumerate(rows):
                sid=row['strategy_id'];savepoint=f'quote_position_{index}';c.execute(f'SAVEPOINT {savepoint}')
                try:
                    pos=json.loads(row['position']) if row['position'] else None
                    if pos.get('pending_entry'):
                        direction=pos['direction'];signal_dt=datetime.fromisoformat(str(pos['signal_cycle']).replace('Z','+00:00'));event_dt=datetime.fromisoformat(str(event).replace('Z','+00:00'))
                        if event_dt<=signal_dt or int(quote.get('age_ms',10**9))>max_age_ms:
                            c.execute(f'RELEASE {savepoint}');continue
                        version=pos['version'];doc=self._doc(c,sid,version);reference=ask if direction=='LONG' else bid
                        pricebar=[{'close':str(reference)}]
                        position,entry_fee,entry_slip=self._new_position(sid,doc,pricebar,event,Decimal(fee_rate),Decimal(slippage),Decimal('0.00000001'),Decimal('0.00000001'),Decimal('1.00'),Decimal(row['cash']),entry_reference=str(reference),direction=direction,stop_price=Decimal(pos['stop']))
                        fill_side='BUY' if direction=='LONG' else 'SELL';idem=f'{sid}:open:{event_id}'
                        cursor=c.execute('insert or ignore into fills(strategy_id,version,cycle,side,qty,price,fee,slippage,reason,idempotency_key,created_at) values(?,?,?,?,?,?,?,?,?,?,?)',(sid,version,event,fill_side,position['qty'],position['entry'],str(entry_fee),str(entry_slip),'ENTRY_NEXT_OPEN',idem,datetime.now(timezone.utc).isoformat()))
                        if cursor.rowcount:
                            c.execute('update accounts set cash=?,position=?,last_status=?,last_action=? where strategy_id=?',(str(Decimal(row['cash'])-entry_fee),json.dumps(position),'ENTRY_FILLED_WAIT_CYCLE','OPEN_'+direction,sid))
                            results.append({'id':sid,'status':'OPEN_'+direction})
                        else:results.append({'id':sid,'status':'DUPLICATE_OR_STALE'})
                        c.execute(f'RELEASE {savepoint}');continue
                    stop=Decimal(pos['stop']);qty=Decimal(pos['qty']);direction=pos.get('direction','LONG');v2=bool(pos.get('target'))
                    trigger=(bid<=stop or bid>=Decimal(pos['target'])) if direction=='LONG' and v2 else (ask>=stop or ask<=Decimal(pos['target'])) if v2 else bid<=stop
                    if trigger:
                        exit_slippage=Decimal(pos.get('slippage_rate',slippage));position_fee=Decimal(pos.get('fee_rate',fee_rate))
                        reference=bid if direction=='LONG' else ask;side=Decimal(1) if direction=='LONG' else Decimal(-1);px=reference*(1-exit_slippage*side);gross=(px-Decimal(pos['entry']))*qty*side;fee=abs(px*qty)*position_fee;net=gross-Decimal(pos.get('entry_fee','0'))-fee
                        stop_hit=(reference<=stop if direction=='LONG' else reference>=stop);reason=('STOP_QUOTE' if stop_hit else 'TARGET_QUOTE') if v2 else 'STOP_QUOTE';action='CLOSE_'+reason.removesuffix('_QUOTE')
                        idem=f"{sid}:quote-{reason.lower()}:{event_id}"
                        cursor=c.execute('insert or ignore into fills(strategy_id,version,cycle,side,qty,price,fee,slippage,reason,idempotency_key,created_at) values(?,?,?,?,?,?,?,?,?,?,?)',(sid,pos['version'],event,'SELL' if direction=='LONG' else 'BUY',str(qty),str(px),str(fee),str(abs(reference*qty-px*qty)),reason,idem,datetime.now(timezone.utc).isoformat()))
                        if cursor.rowcount:
                            c.execute('update accounts set cash=?,position=NULL,realized=?,last_status=?,last_action=? where strategy_id=?',(str(Decimal(row['cash'])+gross-fee),str(Decimal(row['realized'])+net),'OK',action,sid))
                            results.append({'id':sid,'status':action})
                    else:
                        pos['mark']=str(bid)
                        c.execute('update accounts set position=? where strategy_id=?',(json.dumps(pos),sid))
                    c.execute(f'RELEASE {savepoint}')
                except Exception as exc:
                    c.execute(f'ROLLBACK TO {savepoint}');c.execute(f'RELEASE {savepoint}')
                    c.execute('update accounts set last_status=?,last_action=? where strategy_id=?',('BLOCKED:QUOTE_POSITION_ERROR:'+type(exc).__name__,'FAULTED',sid))
                    results.append({'id':sid,'status':'FAULTED'})
        return results

    def backtest(self,sid,version,bars,*,start=None,end=None,fee_rate=Decimal('0.0005'),slippage=Decimal('0.0002'),quantity_step=Decimal('0.00000001'),min_quantity=Decimal('0.00000001'),min_notional=Decimal('1.00')):
        """Run the same signal, size, fee, slippage and exit helpers without touching paper accounts."""
        with _database(self.path) as c: doc=self._doc(c,sid,version)
        if doc.get('format') == FORMAT_V2:
            return self._backtest_v2(sid,version,doc,bars,start,end,fee_rate,slippage,quantity_step,min_quantity,min_notional)
        checked=self._validate_bars(bars)
        selected=[]
        for bar in checked:
            ts=datetime.fromisoformat(str(bar.get('close_at') or bar.get('timestamp') or bar.get('open_at')).replace('Z','+00:00'))
            if start and ts<datetime.fromisoformat(str(start).replace('Z','+00:00')):continue
            if end and ts>datetime.fromisoformat(str(end).replace('Z','+00:00')):continue
            selected.append(bar)
        if len(selected)<self._required_bars(doc):raise StrategyValidationError('所选历史区间数据不足')
        initial=Decimal('10000');cash=initial;realized=Decimal(0);fees=Decimal(0);pos=None;fills=[];curve=[];peak=initial;maxdd=Decimal(0)
        for i in range(self._required_bars(doc),len(selected)+1):
            window=selected[:i];bar=window[-1];cycle=str(bar.get('close_at') or bar.get('timestamp') or bar.get('open_at'))
            if pos:
                closed=self._close_position(pos,doc,window,Decimal(fee_rate),Decimal(slippage))
                if closed:
                    fees+=closed['fee'];cash+=closed['gross']-closed['fee'];realized+=closed['net'];fills.append({'cycle':cycle,'side':'SELL','version':pos['version'],'qty':pos['qty'],'price':str(closed['price']),'fee':str(closed['fee']),'slippage':str(closed['slippage']),'net_pnl':str(closed['net']),'reason':closed['reason'],'idempotency_key':f'{sid}:bt:{cycle}:close'})
                    pos=None
            elif evaluate(doc['entry'],window,doc['parameters'],previous_bars=window[:-1]):
                try:
                    pos,fee,slip_cost=self._new_position(sid,doc,window,cycle,Decimal(fee_rate),Decimal(slippage),Decimal(quantity_step),Decimal(min_quantity),Decimal(min_notional),cash)
                    cash-=fee;fees+=fee;fills.append({'cycle':cycle,'side':'BUY','version':version,'qty':pos['qty'],'price':pos['entry'],'fee':str(fee),'slippage':str(slip_cost),'net_pnl':'0','reason':'ENTRY','idempotency_key':f'{sid}:bt:{cycle}:open'})
                except StrategyValidationError: pass
            mark=Decimal(str(bar['close']))
            if pos:
                open_qty=Decimal(pos['qty']);open_notional=abs(mark*open_qty)
                unrealized=(mark-Decimal(pos['entry']))*open_qty-open_notional*Decimal(fee_rate)-open_notional*Decimal(slippage)
            else:unrealized=Decimal(0)
            equity=cash+unrealized;peak=max(peak,equity);draw=(peak-equity)/peak if peak else 0;maxdd=max(maxdd,draw)
            curve.append({'cycle':cycle,'equity':str(equity)})
        gaps=[]
        for a,b in zip(selected,selected[1:]):
            ta=datetime.fromisoformat(str(a.get('close_at') or a.get('timestamp') or a.get('open_at')).replace('Z','+00:00'));tb=datetime.fromisoformat(str(b.get('close_at') or b.get('timestamp') or b.get('open_at')).replace('Z','+00:00'))
            if (tb-ta).total_seconds()!=900:gaps.append({'from':ta.isoformat(),'to':tb.isoformat()})
        closed_trades=[f for f in fills if f['side']=='SELL'];wins=sum(Decimal(t['net_pnl'])>0 for t in closed_trades)
        final_equity=Decimal(curve[-1]['equity']);final_unrealized=Decimal(curve[-1]['equity'])-cash if pos else Decimal(0)
        report={'strategy_id':sid,'strategy_version':version,'start':start or (selected[0].get('close_at') or selected[0].get('timestamp')),'end':end or (selected[-1].get('close_at') or selected[-1].get('timestamp')),'trades':fills,'net_pnl':str(final_equity-initial),'realized_net_pnl':str(realized),'unrealized_net_pnl':str(final_unrealized),'fees':str(fees),'win_rate':wins/len(closed_trades) if closed_trades else 0,'max_drawdown':str(maxdd),'equity_curve':curve,'data_gaps':gaps,'open_position':pos,'initial_equity':str(initial)}
        report_id=hashlib.sha256(json.dumps(report,sort_keys=True).encode()).hexdigest()
        with _database(self.backtest_path) as c:c.execute('insert or replace into reports values(?,?,?,?,?)',(report_id,sid,version,json.dumps(report,sort_keys=True),datetime.now(timezone.utc).isoformat()))
        return report

    def _backtest_v2(self,sid,version,doc,bars,start,end,fee_rate,slippage,quantity_step,min_quantity,min_notional):
        checked=self._validate_bars(bars,allow_gaps=True);selected=[]
        for bar in checked:
            ts=datetime.fromisoformat(str(bar.get('close_at') or bar.get('timestamp') or bar.get('open_at')).replace('Z','+00:00'))
            if start and ts<datetime.fromisoformat(str(start).replace('Z','+00:00')):continue
            if end and ts>datetime.fromisoformat(str(end).replace('Z','+00:00')):continue
            selected.append(bar)
        warm=self._required_bars(doc)
        if len(selected)<warm:raise StrategyValidationError('所选历史区间数据不足')
        initial=Decimal('10000');cash=initial;fees=Decimal(0);realized=Decimal(0);fills=[];curve=[];pos=None;pending=None;peak=initial;maxdd=Decimal(0);segments=[];censored_positions=0;skipped_entries=0;ambiguous=0
        segment=[];segment_id=0;gaps=[]
        def finish_position(pos,bar,window,held):
            nonlocal cash,fees,realized,ambiguous
            stop=Decimal(pos['stop']);target=Decimal(pos['target']);hi=Decimal(str(bar['high']));lo=Decimal(str(bar['low']))
            if (lo<=stop and hi>=target) if pos['direction']=='LONG' else (hi>=stop and lo<=target):ambiguous+=1
            pos['bars_held']=held
            close=self._close_position(pos,doc,window,Decimal(fee_rate),Decimal(slippage))
            if not close:return False
            fees+=close['fee'];cash+=close['gross']-close['fee'];realized+=close['net']
            fills.append({'cycle':str(bar.get('close_at') or bar.get('timestamp')),'side':'SELL' if pos['direction']=='LONG' else 'BUY','direction':pos['direction'],'version':version,'qty':pos['qty'],'price':str(close['price']),'fee':str(close['fee']),'slippage':str(close['slippage']),'net_pnl':str(close['net']),'reason':close['reason'],'segment':segment_id})
            return True
        for bar in selected:
            ts=datetime.fromisoformat(str(bar.get('close_at') or bar.get('timestamp') or bar.get('open_at')).replace('Z','+00:00'))
            if segment and (ts-datetime.fromisoformat(str(segment[-1].get('close_at') or segment[-1].get('timestamp') or segment[-1].get('open_at')).replace('Z','+00:00'))).total_seconds()!=900:
                previous_ts=datetime.fromisoformat(str(segment[-1].get('close_at') or segment[-1].get('timestamp') or segment[-1].get('open_at')).replace('Z','+00:00'))
                gaps.append({'from':previous_ts.isoformat(),'to':ts.isoformat(),'missing_bars':int((ts-previous_ts).total_seconds()/900)-1})
                if pending:skipped_entries+=1;pending=None
                if pos:censored_positions+=1;pos=None
                segment=[];segment_id+=1
            segment.append(bar)
            if len(segment)>warm:
                window=segment
                if pending:
                    direction,stop,signal_ts=pending;pending=None
                    try:
                        pos,entry_fee,entry_slip=self._new_position(sid,doc,window[:-1],signal_ts,Decimal(fee_rate),Decimal(slippage),Decimal(quantity_step),Decimal(min_quantity),Decimal(min_notional),cash,entry_reference=str(bar['open']),direction=direction,stop_price=stop)
                        cash-=entry_fee;fees+=entry_fee
                        fills.append({'cycle':str(bar.get('close_at') or bar.get('timestamp')),'side':'BUY' if direction=='LONG' else 'SELL','direction':direction,'version':version,'qty':pos['qty'],'price':pos['entry'],'fee':str(entry_fee),'slippage':str(entry_slip),'net_pnl':'0','reason':'ENTRY_NEXT_OPEN','segment':segment_id})
                    except StrategyValidationError:skipped_entries+=1
                if pos and finish_position(pos,bar,window,1 if pos.get('bars_held',0)==0 else int(pos['bars_held'])+1):pos=None
                elif not pos:
                    direction=evaluate_entry_v2(doc,window)
                    if direction:
                        stop,rejection=build_v2_stop(doc,direction,window)
                        if rejection:skipped_entries+=1
                        else:pending=(direction,str(stop),str(bar.get('close_at') or bar.get('timestamp')))
            mark=Decimal(str(bar['close']));unrealized=Decimal(0)
            if pos:
                side=Decimal(1) if pos['direction']=='LONG' else Decimal(-1);qty=Decimal(pos['qty']);unrealized=(mark-Decimal(pos['entry']))*qty*side-abs(mark*qty)*Decimal(fee_rate)-abs(mark*qty)*Decimal(slippage)
                pos['bars_held']=int(pos.get('bars_held',0))+1
            equity=cash+unrealized;peak=max(peak,equity);maxdd=max(maxdd,(peak-equity)/peak if peak else Decimal(0));curve.append({'cycle':str(bar.get('close_at') or bar.get('timestamp')),'equity':str(equity),'segment':segment_id})
        if pending:skipped_entries+=1
        if pos:censored_positions+=1
        exits=[f for f in fills if f['reason'] not in ('ENTRY_NEXT_OPEN',)];wins=sum(Decimal(t['net_pnl'])>0 for t in exits)
        report={'strategy_id':sid,'strategy_version':version,'start':start or (selected[0].get('close_at') or selected[0].get('timestamp')),'end':end or (selected[-1].get('close_at') or selected[-1].get('timestamp')),'trades':fills,'net_pnl':str(Decimal(curve[-1]['equity'])-initial),'realized_net_pnl':str(realized),'unrealized_net_pnl':str(Decimal(curve[-1]['equity'])-cash if pos else 0),'fees':str(fees),'win_rate':wins/len(exits) if exits else 0,'max_drawdown':str(maxdd),'equity_curve':curve,'data_gaps':gaps,'segment_count':segment_id+1,'censored_positions_at_gaps_or_end':censored_positions,'skipped_entries':skipped_entries,'same_bar_stop_target_ambiguities':ambiguous,'open_position':pos,'initial_equity':str(initial)}
        report_id=hashlib.sha256(json.dumps(report,sort_keys=True).encode()).hexdigest()
        with _database(self.backtest_path) as c:c.execute('insert or replace into reports values(?,?,?,?,?)',(report_id,sid,version,json.dumps(report,sort_keys=True),datetime.now(timezone.utc).isoformat()))
        return report
