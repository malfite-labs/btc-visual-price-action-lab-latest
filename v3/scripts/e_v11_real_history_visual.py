"""Four real historical E snapshots; rule-derived anchors, no independent adjudication."""
import asyncio,json,os,sqlite3
from decimal import Decimal
from pathlib import Path
from backend.market import Candle
from backend.book_pa_chart_v03 import render_numbered_chart
from backend.e_quant_v11 import detect_setups
from backend.vision_provider import ConfigurableVisionAdapter,VisionProviderConfig
ROOT=Path(__file__).resolve().parents[1]; OUT=ROOT/'docs/reports/e-integration/e-v11-real-history-visual'; OUT.mkdir(parents=True,exist_ok=True)
async def main():
 for line in (ROOT/'.env').read_text(encoding='utf-8-sig').splitlines():
  k,sep,v=line.partition('=')
  if sep and k.strip()=='DEEPSEEK_API_KEY':os.environ.setdefault(k.strip(),v.strip().strip("\"'"))
 cfg=VisionProviderConfig(enabled=True,calls_authorized=True,model='deepseek-flash',thinking_mode='disabled',max_concurrency=1,max_output_tokens=1000,max_requests_per_day=4,max_call_cost_usd=Decimal('.05'),daily_budget_usd=Decimal('.20'),total_budget_usd=Decimal('.20'),input_price_per_million_usd=Decimal('.30'),output_price_per_million_usd=Decimal('1.20'),budget_db_path=str(OUT/'budget.sqlite3'))
 adapter=ConfigurableVisionAdapter(cfg)
 c=sqlite3.connect((ROOT/'data/market-candles.sqlite3').resolve().as_uri()+'?mode=ro',uri=True);bars=[json.loads(x[0]) for x in c.execute("select payload from candles where symbol='BTC/USD' and close_at <= '2026-11-02T03:30:00+00:00' order by open_at")];c.close();audit=json.loads((ROOT/'docs/reports/e-integration/e-v11-23-candidate-audit.json').read_text(encoding='utf8'));md=(ROOT/'strategies/E/E-V1.1-QUANT.md').read_text(encoding='utf-8-sig');results=[]
 for name in ('E1','E2','E3','E4'):
  item=next(x for x in audit['candidates'] if x['setup']==name);i=next(i for i,b in enumerate(bars) if b['close_at']==item['close_at']);rows=bars[i-287:i+1];candidate=detect_setups(rows)['selected'];j=candidate['anchor']['index'];(OUT/(name+'.ohlc.json')).write_text(json.dumps(rows,ensure_ascii=False,indent=2),encoding='utf8')
  candles=[Candle(**{k:v for k,v in b.items() if k in Candle.__dataclass_fields__}) for b in rows];chart=OUT/(name+'.png');evidence=render_numbered_chart(candles,chart)
  prompt='仅依据图像质检程序提出的 '+name+' '+candidate['side']+' 结构，按提供的量化规则检查明显矛盾，不判断盈利，不输出价格。返回JSON：verdict 为 CONFIRM/CONTRADICT/UNCERTAIN，anchor_candle 为失效结构 K01 至 K48 或 null，reason 为理由。\n'+md
  response=await adapter._send_image_request(chart.read_bytes(),prompt,'e-v11-real-history:'+name);label=f'K{j-239:02}' if 240<=j<288 else None
  result={'setup':name,'source':'ALPACA_US_BTCUSD_SPOT_ARCHIVE','close_at':item['close_at'],'snapshot_sha256':item['snapshot_sha256'],'chart_sha256':evidence['chart_sha256'],'candle_sha256':evidence['candle_sha256'],'candidate':candidate,'rule_derived_expected_anchor':label,'expected_anchor_independent':False,'response':response,'anchor_match':label is not None and response.get('anchor_candle')==label};results.append(result);(OUT/'results.json').write_text(json.dumps(results,ensure_ascii=False,indent=2,default=str),encoding='utf8');print(name,'status',response.get('status','RESPONSE_RECEIVED'),'verdict',response.get('verdict'),'anchor',response.get('anchor_candle'),'expected',label,flush=True)
  if response.get('status'):break
 print('budget_summary',json.dumps(adapter._budget.summary(),default=str),flush=True)
if __name__=='__main__':asyncio.run(main())
