"""Fixed, research-only, causal candle-pattern hypotheses."""
from decimal import Decimal as D
TICK=D('.01')
def n(b,k):return D(str(b[k]))
def trend(bars,i,length=12):
    if i<length:return 0
    last=n(bars[i-1],'close');first=n(bars[i-length],'close')
    return 1 if last>first else -1 if last<first else 0
def result(bars,i,direction,setup):
    recent=bars[i-3:i+1]
    stop=min(n(x,'low') for x in recent)-TICK if direction=='LONG' else max(n(x,'high') for x in recent)+TICK
    return {'direction':direction,'stop':stop,'target':None,'setup':setup}

def signal_a(bars,i):
    if i<12:return None
    d=trend(bars,i); p=bars[i-1]; b=bars[i]
    if d==1 and n(p,'close')<n(p,'open') and n(b,'close')>n(b,'open') and n(b,'close')>n(p,'high'):return result(bars,i,'LONG','A_PULLBACK')
    if d==-1 and n(p,'close')>n(p,'open') and n(b,'close')<n(b,'open') and n(b,'close')<n(p,'low'):return result(bars,i,'SHORT','A_PULLBACK')
    recent=bars[i-5:i];older=bars[i-10:i-5]
    if d==1 and n(b,'close')>max(n(x,'high') for x in recent) and max(n(x,'high') for x in recent)>max(n(x,'high') for x in older):return result(bars,i,'LONG','A_SECOND_BREAK')
    if d==-1 and n(b,'close')<min(n(x,'low') for x in recent) and min(n(x,'low') for x in recent)<min(n(x,'low') for x in older):return result(bars,i,'SHORT','A_SECOND_BREAK')
    return None

def signal_b(bars,i):
    if i<10:return None
    b=bars[i];p=bars[i-1];prior=bars[i-10:i];lo=min(n(x,'low') for x in prior);hi=max(n(x,'high') for x in prior);o=n(b,'open');c=n(b,'close');h=n(b,'high');l=n(b,'low');body=abs(c-o)
    if l<lo and c>lo:return result(bars,i,'LONG','B_FALSE_BREAK')
    if h>hi and c<hi:return result(bars,i,'SHORT','B_FALSE_BREAK')
    if l<=lo*D('1.001') and c>o and min(o,c)-l>=2*max(body,TICK):return result(bars,i,'LONG','B_WICK')
    if h>=hi*D('.999') and c<o and h-max(o,c)>=2*max(body,TICK):return result(bars,i,'SHORT','B_WICK')
    if l<=lo*D('1.001') and n(p,'close')<n(p,'open') and c>o and o<=n(p,'close') and c>=n(p,'open'):return result(bars,i,'LONG','B_ENGULF')
    if h>=hi*D('.999') and n(p,'close')>n(p,'open') and c<o and o>=n(p,'close') and c<=n(p,'open'):return result(bars,i,'SHORT','B_ENGULF')
    return None

def signal_b_filtered(bars, i):
    if i < 51:
        return None
    candidate = signal_b(bars, i)
    if candidate is None:
        return None
    fast = sum(n(b, 'close') for b in bars[i-11:i+1]) / D(12)
    slow = sum(n(b, 'close') for b in bars[i-47:i+1]) / D(48)
    previous_fast = sum(n(b, 'close') for b in bars[i-15:i-3]) / D(12)
    direction = candidate['direction']
    if direction == 'LONG' and fast > slow and fast > previous_fast:
        return candidate
    if direction == 'SHORT' and fast < slow and fast < previous_fast:
        return candidate
    return None
