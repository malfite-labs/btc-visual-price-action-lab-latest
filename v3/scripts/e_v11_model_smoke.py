"""Blinded V1.1 candidate visual cross-check on four deterministic local fixtures."""
import asyncio,hashlib,json,os,sys
from pathlib import Path
from decimal import Decimal
from backend.market import Candle
from backend.book_pa_chart_v03 import render_numbered_chart
from backend.e_quant_v11 import detect_setups
from backend.vision_provider import ConfigurableVisionAdapter,VisionProviderConfig
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'tests'))
from test_e_quant_v11 import range_window,e2_window
OUT=ROOT/'docs/reports/e-integration/e-v11-model-smoke'

def fixture(name):
 d=e2_window() if name=='E2' else range_window()
 if name=='E1':
  d[-2].update(open=99.8,high=100,low=99.4,close=99.5);d[-1].update(open=99.5,high=100.2,low=99.4,close=100)
 elif name=='E3':
  d[-4].update(open=100,high=101.8,low=99.9,close=101.5);d[-3].update(open=101.5,high=101.6,low=100.4,close=100.6);d[-2].update(open=100.6,high=100.9,low=100.5,close=100.7);d[-1].update(open=100.7,high=101.3,low=100.6,close=101.2)
 elif name=='E4':
  d[-4].update(open=100,high=100.1,low=98.2,close=98.5);d[-3].update(open=98.5,high=99.6,low=98.4,close=99.4);d[-2].update(open=99.4,high=99.5,low=99.1,close=99.2);d[-1].update(open=99.2,high=99.3,low=98.7,close=98.8)
 return d

async def main():
 OUT.mkdir(parents=True,exist_ok=True)
 for line in (ROOT/'.env').read_text(encoding='utf-8-sig').splitlines():
  k,sep,v=line.partition('=')
  if sep and k.strip()=='DEEPSEEK_API_KEY':os.environ.setdefault(k.strip(),v.strip().strip("\"'"))
 cfg=VisionProviderConfig(enabled=True,calls_authorized=True,model='deepseek-flash',thinking_mode='disabled',max_concurrency=1,max_output_tokens=1000,max_requests_per_day=4,max_call_cost_usd=Decimal('.05'),daily_budget_usd=Decimal('.20'),total_budget_usd=Decimal('.20'),input_price_per_million_usd=Decimal('.30'),output_price_per_million_usd=Decimal('1.20'),budget_db_path=str(OUT/'budget.sqlite3'))
 adapter=ConfigurableVisionAdapter(cfg);md=(ROOT/'strategies/E/E-V1.1-QUANT.md').read_text(encoding='utf-8-sig');report=[]
 for name in ('E1','E2','E3','E4'):
  rows=fixture(name);result=detect_setups(rows);candidate=next(x for x in result['candidates'] if x['setup']==name)
  (OUT/(name+'.ohlc.json')).write_text(json.dumps(rows,ensure_ascii=False,indent=2),encoding='utf8')
  candles=[Candle('BTC/USD','15Min',r['open_at'],r['close_at'],str(r['open']),str(r['high']),str(r['low']),str(r['close']),'0',0,None,True) for r in rows]
  chart=OUT/(name+'.png');meta=render_numbered_chart(candles,chart)
  label=f"K{candidate['anchor']['index']-239:02}"
  prompt=('请仅基于图片核验程序提出的 '+name+' 候选：形态是否与图像相符？请独立指出失效结构对应的 K01–K48 蜡烛编号。'
      '返回JSON：verdict 为 CONFIRM、CONTRADICT、UNCERTAIN 之一；stop_candle 为编号或 null；reason 为简短文字。不要输出价格。\n'+md)
  response=await adapter._send_image_request(chart.read_bytes(),prompt,'e-v11-smoke:'+name)
  item={'setup':name,'source':'SYNTHETIC_DESIGN','detector_candidate':candidate,'reference_anchor':label,
    'response':response,'chart_sha256':meta['chart_sha256'],'candle_sha256':meta['candle_sha256'],
    'reference_type':'program_rule_fixture_not_independently_adjudicated'}
  report.append(item);(OUT/'results.json').write_text(json.dumps(report,ensure_ascii=False,indent=2,default=str),encoding='utf8')
  print(name,'status',response.get('status','RESPONSE_RECEIVED'),'verdict',response.get('verdict'),'anchor',response.get('stop_candle'),'reference',label,flush=True)
  if response.get('status'):break
 print('budget',json.dumps(adapter._budget.summary(),ensure_ascii=False),flush=True)

if __name__=='__main__':asyncio.run(main())
