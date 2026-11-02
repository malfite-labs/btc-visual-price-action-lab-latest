"""Independent read-only-source BTC OHLC archive; no trading ledger dependency."""
import json,sqlite3
from datetime import datetime,timezone,timedelta
from decimal import Decimal
from pathlib import Path

def utc(value):
    x=datetime.fromisoformat(str(value).replace('Z','+00:00'))
    if x.tzinfo is None:raise ValueError('NAIVE_TIMESTAMP')
    return x.astimezone(timezone.utc)

class CandleArchive:
    def __init__(self,path):
        self.path=Path(path);self.path.parent.mkdir(parents=True,exist_ok=True)
        with sqlite3.connect(self.path,timeout=10) as c:c.execute('create table if not exists candles(symbol text not null,open_at text not null,close_at text not null,payload text not null,primary key(symbol,open_at))')
    def store(self,rows):
        valid=[];now=datetime.now(timezone.utc)
        for r in rows:
            try:
                start=utc(r['open_at']);end=utc(r['close_at']);o,h,l,c=(Decimal(str(r[k])) for k in ('open','high','low','close'))
                if r.get('symbol')!='BTC/USD' or str(r.get('interval','')).lower() not in ('15min','15m') or r.get('source')!='ALPACA_US_BTCUSD_SPOT' or r.get('currency')!='USD_RESEARCH' or r.get('is_closed') is not True or end>now or end-start!=timedelta(minutes=15) or start.minute%15 or start.second or start.microsecond or not all(x.is_finite() and x>0 for x in (o,h,l,c)) or l>min(o,c) or h<max(o,c):continue
                payload={**r,'open_at':start.isoformat(),'close_at':end.isoformat()};valid.append(('BTC/USD',start.isoformat(),end.isoformat(),json.dumps(payload,ensure_ascii=False,default=str)))
            except (KeyError,TypeError,ValueError,ArithmeticError):continue
        with sqlite3.connect(self.path,timeout=10) as c:
            c.executemany('insert or ignore into candles values(?,?,?,?)',valid)
    def read(self,start,end):
        with sqlite3.connect(self.path,timeout=10) as c:
            rows=c.execute('select payload from candles where symbol=? and open_at>=? and open_at<? order by open_at',('BTC/USD',start.astimezone(timezone.utc).isoformat(),end.astimezone(timezone.utc).isoformat())).fetchall()
        return [json.loads(row[0]) for row in rows]
