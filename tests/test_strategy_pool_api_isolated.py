import os
import subprocess
import sys
from pathlib import Path


ROOT=Path(__file__).resolve().parents[1]


def test_strategy_pool_api_upload_and_enable_are_isolated(tmp_path):
    code=r'''
import asyncio, json
from datetime import datetime, timedelta, timezone
from starlette.requests import Request
import backend.app as app

def request(body):
    data=json.dumps(body).encode() if isinstance(body,dict) else body
    sent=False
    async def receive():
        nonlocal sent
        if sent:return {"type":"http.request","body":b"","more_body":False}
        sent=True;return {"type":"http.request","body":data,"more_body":False}
    return Request({"type":"http","method":"POST","path":"/","headers":[]},receive)

async def main():
    doc={"format":"btc-lab-declarative-strategy/v1","strategy_id":"api-isolated","version":"1.0","name":"API isolated","market":"BTC/USD","timeframe":"15m","description":"API isolated","parameters":{"limit":105},"risk":{"initial_equity":"10000","currency":"USD_RESEARCH","max_leverage":5,"risk_fraction":"0.02"},"entry":{"op":"gt","args":[{"op":"close","args":[1]}, {"op":"param","args":["limit"]}]},"exit":{"op":"lt","args":[{"op":"close","args":[1]}, {"op":"param","args":["limit"]}]}}
    uploaded=await app.strategy_pool_upload(request(json.dumps(doc).encode()))
    assert uploaded["validation"]["valid"] is True
    assert uploaded["enabled"] is False
    assert app.strategy_pool.list()[0]["status"]=="REGISTERED"
    enabled=await app.strategy_pool_enable("api-isolated",request({"version":"1.0","enabled":True}))
    assert enabled["status"]=="ENABLED_NOT_RUNNING"
    assert enabled["items"][0]["status"]=="ENABLED_NOT_RUNNING"
    assert app.strategy_pool.backtest_path.startswith(__import__('os').environ['BTC_LAB_STRATEGY_POOL_DB'].rsplit('.',1)[0])
    end=datetime.now(timezone.utc).replace(minute=(datetime.now(timezone.utc).minute//15)*15,second=0,microsecond=0)
    start=end-timedelta(minutes=30)
    rows=[]
    for i,close in enumerate((100,110)):
        opened=start+timedelta(minutes=15*i);closed=opened+timedelta(minutes=15)
        rows.append({"symbol":"BTC/USD","interval":"15m","source":"ALPACA_US_BTCUSD_SPOT","currency":"USD_RESEARCH","is_closed":True,"open_at":opened.isoformat(),"close_at":closed.isoformat(),"open":close,"high":close+1,"low":close-1,"close":close,"volume":1})
    app.candle_archive.store(rows)
    report=await app.strategy_pool_backtest("api-isolated",request({"version":"1.0","start":start.isoformat(),"end":end.isoformat()}))
    assert report["strategy_version"]=="1.0"
    assert len(report["equity_curve"])==2
    assert report["open_position"] is not None
    await app.strategy_pool_enable("api-isolated",request({"version":"1.0","enabled":False}))
    await app.strategy_pool_archive("api-isolated")
    assert app.strategy_pool.list()[0]["status"]=="ARCHIVED"
    events=await app.strategy_pool_lifecycle_events("api-isolated")
    assert events["items"][-1]["action"]=="ARCHIVE"
    await app.strategy_pool_restore("api-isolated",request({"version":"1.0"}))
    restored=app.strategy_pool.list()[0]
    assert restored["status"]=="PAUSED" and restored["enabled"] is False

asyncio.run(main())
'''
    env=os.environ.copy()
    env.update({
        "BTC_LAB_DB":str(tmp_path/"paper.sqlite3"),
        "BTC_LAB_CANDLE_DB":str(tmp_path/"candles.sqlite3"),
        "BTC_LAB_STRATEGY_POOL_DB":str(tmp_path/"strategy-pool.sqlite3"),
        "PAPER_AUTOMATION_ENABLED":"false",
    })
    result=subprocess.run([sys.executable,"-c",code],cwd=ROOT,env=env,text=True,capture_output=True,timeout=30)
    assert result.returncode==0, result.stdout+result.stderr
