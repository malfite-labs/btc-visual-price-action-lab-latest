"""E-V1.1 OHLC-only setup detector. Does not authorize or place orders."""
import hashlib
import json
from decimal import Decimal, InvalidOperation
from statistics import median
from backend.e_visual import resolve_e_decision

D=Decimal


def _window(bars,end):
    """Fixed range order; the end is exclusive and may not include a breakout bar."""
    eps,tol,_ = _scale(bars,end-1)
    for length in (64,48,96,144):
        start=end-length
        if start<1: continue
        chunk=bars[start:end]
        lo=min(b['low'] for b in chunk);hi=max(b['high'] for b in chunk);mid=(hi+lo)/2
        if mid<=0 or not D('.005') <= (hi-lo)/mid <= D('.06'):continue
        touch=max(D('.15')*(hi-lo),tol)
        lows=[i for i,b in enumerate(chunk) if b['low']-lo<=touch]
        highs=[i for i,b in enumerate(chunk) if hi-b['high']<=touch]
        if not lows or not highs or lows[-1]-lows[0]<6 or highs[-1]-highs[0]<6:continue
        if sum(lo<=b['close']<=hi for b in chunk)*10 < len(chunk)*7:continue
        return {'start':start,'end':end-1,'lo':lo,'hi':hi}
    return None


def _scale(bars,t):
    if t<33: raise ValueError('INSUFFICIENT_SCALE_HISTORY')
    tr=[max(bars[i]['high']-bars[i]['low'],abs(bars[i]['high']-bars[i-1]['close']),abs(bars[i]['low']-bars[i-1]['close'])) for i in range(t-32,t)]
    u=median(tr)
    if u<=0:raise ValueError('INVALID_RANGE_SCALE')
    return max(D('.0005')*bars[t]['close'],D('.15')*u),max(D('.0005')*bars[t]['close'],D('.20')*u),u


def _ref(bars,index):
    return {'index':index,'open_at':bars[index]['open_at']}


def detect_setups(rows,*,_shadow_change=None):
    """Validate a contiguous snapshot, then detect all four E setups deterministically."""
    resolve_e_decision({'action':'SKIP'},rows,mapping_verified=False)
    try:
        change=_shadow_change or {}
        if not isinstance(change,dict) or len(change)>1:raise ValueError('INVALID_SHADOW_CHANGE')
        allowed={'E2_drop_window':set(range(6,13,2)),'E3_breakout_window':set(range(3,11)),'E4_breakdown_window':set(range(3,11))}
        if any(k not in allowed or type(v) is not int or v not in allowed[k] for k,v in change.items()):raise ValueError('INVALID_SHADOW_CHANGE')
        bars=[{**b,**{k:D(str(b[k])) for k in ('open','high','low','close')}} for b in rows]
        t=len(bars)-1;eps,tol,u=_scale(bars,t);cur=bars[t];out=[];diagnostics=[]
        region=_window(bars,t-1)
        if region:
            lo,hi=region['lo'],region['hi'];prev=bars[t-1]
            if prev['low']<=lo+tol and prev['close']<=lo+tol and cur['close']>=lo+eps and cur['close']>cur['open'] and cur['close']>prev['close']:
                j=min((t-1,t),key=lambda i:(bars[i]['low'],i))
                out.append({'setup':'E1','side':'LONG','stop':str(bars[j]['low']-D('.15')*u),'reward':str(hi),'anchor':_ref(bars,j),'range':region})
        else:diagnostics.append('RANGE_INVALID')
        k=change.get('E2_drop_window',8)
        j=min(range(t-5,t),key=lambda i:(bars[i]['low'],i));low=bars[j]['low'];base=bars[t-k]['close'];minlow=min(bars[i]['low'] for i in range(t-k+1,t+1))
        if ((base-minlow)/base>=max(D('.008'),D('2.5')*u/cur['close']) and cur['low']>low and cur['close']>cur['open'] and cur['close']>max(bars[t-2]['high'],bars[t-1]['high'])+eps and cur['close']>=low+D('.35')*(base-low)):
            out.append({'setup':'E2','side':'LONG','stop':str(low-D('.15')*u),'reward':str(base),'anchor':_ref(bars,j)})
        for direction,setup in (('LONG','E3'),('SHORT','E4')):
            lookback=change.get('E3_breakout_window' if direction=='LONG' else 'E4_breakdown_window',8)
            for b in range(t-lookback,t):
                box=_window(bars,b)
                if not box:continue
                e,_,_=_scale(bars,b);edge=box['hi'] if direction=='LONG' else box['lo']
                broken=(bars[b]['close']>edge+e and bars[b-1]['close']<=edge+e) if direction=='LONG' else (bars[b]['close']<edge-e and bars[b-1]['close']>=edge-e)
                if not broken:continue
                retests=[]
                for r in range(b+1,t):
                    er,tr,_=_scale(bars,r)
                    if (bars[r]['low']<=edge+tr and bars[r]['close']>=edge-er) if direction=='LONG' else (bars[r]['high']>=edge-tr and bars[r]['close']<=edge+er):retests.append(r)
                confirmed=(cur['close']>edge+eps and cur['close']>cur['open'] and cur['low']>=box['lo']) if direction=='LONG' else (cur['close']<edge-eps and cur['close']<cur['open'])
                if retests and confirmed:
                    group=range(b+1,t+1);j=min(group,key=lambda i:(bars[i]['low'],i)) if direction=='LONG' else max(group,key=lambda i:(bars[i]['high'],-i))
                    stop=(bars[j]['low']-D('.15')*u) if direction=='LONG' else (bars[j]['high']+D('.15')*u)
                    target=edge+(box['hi']-box['lo'])*(1 if direction=='LONG' else -1)
                    # Provisional conservative visual obstacle proxy: a pre-breakout local swing.
                    # It is tagged as engineering inference and never authorizes an order.
                    if direction=='LONG':
                        obstacles=[bars[i]['high'] for i in range(1,b-1) if bars[i]['high']>bars[i-1]['high'] and bars[i]['high']>=bars[i+1]['high'] and cur['close']<bars[i]['high']<target]
                        if obstacles:target=min(obstacles)
                    else:
                        obstacles=[bars[i]['low'] for i in range(1,b-1) if bars[i]['low']<bars[i-1]['low'] and bars[i]['low']<=bars[i+1]['low'] and target<bars[i]['low']<cur['close']]
                        if obstacles:target=max(obstacles)
                    out.append({'setup':setup,'side':direction,'stop':str(stop),'reward':str(target),'anchor':_ref(bars,j),'breakout':_ref(bars,b),'retest':_ref(bars,retests[0]),'range':box,'reward_note':'ENGINEERING_LOCAL_SWING_PROXY_OR_BOX_PROJECTION'})
                elif not retests:diagnostics.append('NOT_RETESTED')
                break
        directions={x['side'] for x in out}
        for x in out:
            if 'range' in x:x['range']={k:str(v) if isinstance(v,Decimal) else v for k,v in x['range'].items()}
        selected=None if len(directions)>1 else next((x for name in ('E3','E1','E2','E4') for x in out if x['setup']==name),None)
        snapshot_sha256=hashlib.sha256(json.dumps(rows,ensure_ascii=False,sort_keys=True,separators=(',',':'),default=str).encode('utf8')).hexdigest()
        return {'strategy_version':'E-V1.1-QUANT','snapshot_sha256':snapshot_sha256,'closed_bar_time':rows[-1]['close_at'],'status':'CONFLICT_SKIP' if len(directions)>1 else 'SETUP_DETECTED' if out else 'NO_SETUP','candidates':out,'selected':selected,'diagnostics':diagnostics,'order_eligible':False,'order_reason':'VISUAL_AND_EXECUTION_NOT_VALIDATED'}
    except (KeyError,InvalidOperation,ArithmeticError) as exc:
        raise ValueError('INVALID_SNAPSHOT') from exc
