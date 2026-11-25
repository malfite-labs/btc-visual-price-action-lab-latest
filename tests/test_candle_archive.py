from datetime import datetime,timezone
from backend.candle_archive import CandleArchive

def test_archive_closed_real_unique_and_range(tmp_path):
 a=CandleArchive(tmp_path/'market.sqlite3');bar={'symbol':'BTC/USD','interval':'15Min','open_at':'2026-10-28T11:30:00+00:00','close_at':'2026-10-28T11:45:00+00:00','open':'100','high':'102','low':'99','close':'101','volume':'1','is_closed':True,'source':'BINANCE_USDM_PERP','currency':'USD_RESEARCH'}
 a.store([bar,bar]);assert len(a.read(datetime(2026,10,28,11,tzinfo=timezone.utc),datetime(2026,10,28,12,tzinfo=timezone.utc)))==1
 a.store([{**bar,'is_closed':False,'open_at':'2026-10-28T11:45:00+00:00','close_at':'2026-10-28T12:00:00+00:00'}]);assert len(a.read(datetime(2026,10,28,11,tzinfo=timezone.utc),datetime(2026,10,28,12,tzinfo=timezone.utc)))==1

def test_archive_rejects_bad_source_and_nonclosed_and_future(tmp_path):
 from datetime import timedelta
 a=CandleArchive(tmp_path/'invalid.sqlite3');now=datetime.now(timezone.utc);start=now.replace(minute=(now.minute//15)*15,second=0,microsecond=0)
 b={'symbol':'BTC/USD','interval':'15Min','open_at':start.isoformat(),'close_at':(start+timedelta(minutes=15)).isoformat(),'open':'100','high':'101','low':'99','close':'100','is_closed':True,'source':'BINANCE_USDM_PERP','currency':'USD_RESEARCH'}
 a.store([b,{**b,'source':'OTHER'},{**b,'high':'1'}]);assert a.read(start,start+timedelta(minutes=15))==[]

