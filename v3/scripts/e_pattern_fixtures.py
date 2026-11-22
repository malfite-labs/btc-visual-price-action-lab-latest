"""Synthetic, independently specified E1-E4 visual fixtures; no trading."""
import json,hashlib
from dataclasses import asdict
from datetime import UTC,datetime,timedelta
from pathlib import Path
from backend.market import Candle
from backend.book_pa_chart_v03 import render_numbered_chart

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'docs/reports/e-integration/e-pattern-eval'
# Control points are authored before any model call; values in USD, times are last 48 candles.
CASES={
 'E1-positive':([(0,100),(6,107),(12,95),(18,106),(24,95),(30,106),(36,96),(40,92),(41,94),(42,98),(47,101)],'E1',41),
 'E1-negative':([(0,100),(6,107),(12,95),(18,106),(24,95),(30,106),(36,99),(42,101),(47,100)],'SKIP',None),
 'E1-boundary':([(0,100),(6,107),(12,95),(18,106),(24,95),(30,106),(36,96),(42,92),(47,93)],'SKIP',None),
 'E2-positive':([(0,110),(12,111),(24,109),(28,106),(32,101),(35,94),(37,90),(39,95),(41,96),(43,99),(47,102)],'E2',38),
 'E2-negative':([(0,110),(12,111),(24,109),(28,106),(32,101),(35,97),(39,94),(43,91),(47,87)],'SKIP',None),
 'E2-boundary':([(0,110),(12,111),(24,109),(28,106),(32,101),(35,95),(38,90),(41,94),(44,90),(47,90)],'SKIP',None),
 'E3-positive':([(0,100),(6,105),(12,97),(18,105),(24,97),(30,105),(34,100),(37,106),(39,109),(41,106),(43,108),(47,112)],'E3',42),
 'E3-negative':([(0,100),(6,105),(12,97),(18,105),(24,97),(30,105),(36,100),(42,103),(47,101)],'SKIP',None),
 'E3-boundary':([(0,100),(6,105),(12,97),(18,105),(24,97),(30,105),(36,100),(39,108),(43,100),(47,98)],'SKIP',None),
 'E4-positive':([(0,110),(6,104),(12,108),(18,102),(24,106),(30,101),(34,103),(37,98),(39,94),(41,98),(43,95),(47,91)],'E4',42),
 'E4-negative':([(0,110),(6,104),(12,108),(18,102),(24,106),(30,101),(36,103),(42,102),(47,104)],'SKIP',None),
 'E4-boundary':([(0,110),(6,104),(12,108),(18,102),(24,106),(30,101),(36,103),(39,96),(43,101),(47,105)],'SKIP',None),
}

def make_bars(points):
    def value(i):
        for (a,x),(b,y) in zip(points,points[1:]):
            if a<=i<=b: return x+(y-x)*(i-a)/(b-a)
        return points[-1][1]
    end=datetime(2026,1,4,tzinfo=UTC); rows=[]
    for i in range(288):
        local=i-240
        close=100 if local<0 else value(local)
        op=100 if local<=0 else value(local-1)
        hi=max(op,close)+.45;lo=min(op,close)-.45
        t=end-timedelta(minutes=15*(288-i));scale=lambda x:str(round(x*1000,2))
        rows.append(Candle('BTC/USD','15Min',t.isoformat(),(t+timedelta(minutes=15)).isoformat(),scale(op),scale(hi),scale(lo),scale(close),'0',0,None,True))
    return rows

def build():
    OUT.mkdir(parents=True,exist_ok=True); ref=[]
    for name,(points,expected,anchor) in CASES.items():
        bars=make_bars(points); target=OUT/(name+'.png');meta=render_numbered_chart(bars,target)
        bars_path=OUT/(name+'.ohlc.json');bars_path.write_text(json.dumps([asdict(b) for b in bars],ensure_ascii=False,indent=2),encoding='utf8')
        ref.append({'case':name,'expected_setup':expected,'expected_stop_label':f'K{anchor:02}' if anchor else None,
          'source':'SYNTHETIC_DESIGN_NOT_MARKET','ohlc_file':bars_path.name,'ohlc_sha256':hashlib.sha256(bars_path.read_bytes()).hexdigest(),
          'chart_file':target.name,'chart_sha256':meta['chart_sha256'],'candle_sha256':meta['candle_sha256'],
          'reference_type':'AUTHOR_SPECIFIED_BLUEPRINT_NOT_INDEPENDENT_HUMAN_ADJUDICATION'})
    (OUT/'reference.json').write_text(json.dumps(ref,ensure_ascii=False,indent=2),encoding='utf8')
    print('prepared',len(ref),'cases')

if __name__=='__main__': build()
