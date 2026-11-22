"""Forward-only input guards; no live trading API."""
from datetime import datetime

def dt(s):
 x=datetime.fromisoformat(str(s).replace('Z','+00:00'))
 if x.tzinfo is None or x.utcoffset() is None:raise ValueError('NAIVE_TIMESTAMP')
 return x
def eligible(bar,start,now):
 try:return bar.get('is_closed') is True and dt(bar['open_at'])>=dt(start) and dt(bar['close_at'])<=now and (dt(bar['close_at'])-dt(bar['open_at'])).total_seconds()==900
 except (KeyError,ValueError,TypeError):return False
def quote_fresh(q,not_before):
 try:return q.get('source')=='ALPACA_US_BTCUSD_SPOT' and q.get('currency')=='USD_RESEARCH' and q.get('timestamp_valid') is True and 0<=int(q['age_ms'])<=60000 and dt(q['event_at'])>=dt(not_before) and float(q['bid'])>0 and float(q['ask'])>=float(q['bid'])
 except (KeyError,ValueError,TypeError):return False

def next_open(now):
 from datetime import timedelta,timezone
 if now.tzinfo is None or now.utcoffset() is None:raise ValueError('NAIVE_TIMESTAMP')
 now=now.astimezone(timezone.utc)
 return now.replace(minute=(now.minute//15)*15,second=0,microsecond=0)+timedelta(minutes=15)

def valid_context(bars,expected=288):
 from decimal import Decimal
 try:
  if len(bars)!=expected:return False
  for b in bars:
   if b.get('is_closed') is not True or b.get('source')!='ALPACA_US_BTCUSD_SPOT' or b.get('currency')!='USD_RESEARCH' or (dt(b['close_at'])-dt(b['open_at'])).total_seconds()!=900:return False
   o,h,l,c=(Decimal(str(b[k])) for k in ('open','high','low','close'))
   if not all(x.is_finite() and x>0 for x in (o,h,l,c)) or l>min(o,c) or h<max(o,c) or l>h:return False
  return all(dt(x['close_at'])==dt(y['open_at']) for x,y in zip(bars,bars[1:]))
 except (KeyError,ValueError,TypeError,ArithmeticError):return False
