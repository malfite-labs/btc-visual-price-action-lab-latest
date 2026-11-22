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

def signal_c(bars,i):
    if i<12:return None
    d=trend(bars,i);b=bars[i];p=bars[i-1];prior=bars[i-8:i];c=n(b,'close');o=n(b,'open')
    def make(direction,setup):
        x=result(bars,i,direction,setup); nearby=bars[max(0,i-20):i]
        level=max(n(y,'high') for y in nearby) if direction=='LONG' else min(n(y,'low') for y in nearby)
        x['obstacle']=level if (direction=='LONG' and level>c) or (direction=='SHORT' and level<c) else None
        return x
    if i>=96:
        broad=trend(bars,i,96);q=bars[i-2]
        if broad==-1 and n(q,'close')>n(q,'open') and n(p,'close')>n(p,'open') and c<o and c<n(p,'low'):return make('SHORT','T4')
        if broad==1 and n(q,'close')<n(q,'open') and n(p,'close')<n(p,'open') and c>o and c>n(p,'high'):return make('LONG','T4')
    q=bars[i-2]
    if d==1 and n(q,'close')>n(q,'open') and n(p,'close')<n(p,'open') and c>o and c>n(p,'high'):return make('LONG','T1')
    if d==-1 and n(q,'close')<n(q,'open') and n(p,'close')>n(p,'open') and c<o and c<n(p,'low'):return make('SHORT','T1')
    if d==1 and c>max(n(x,'high') for x in prior):return make('LONG','T2')
    if d==-1 and c<min(n(x,'low') for x in prior):return make('SHORT','T2')
    recent=bars[i-2:i+1]
    strong=all((n(x,'high')-n(x,'low'))>0 and abs(n(x,'close')-n(x,'open'))>=D('.5')*(n(x,'high')-n(x,'low')) for x in recent)
    if strong and d==1 and all(n(x,'close')>n(x,'open') for x in recent) and n(recent[0],'close')<n(recent[1],'close')<c:return make('LONG','T3')
    if strong and d==-1 and all(n(x,'close')<n(x,'open') for x in recent) and n(recent[0],'close')>n(recent[1],'close')>c:return make('SHORT','T3')
    return None
