"""Four bounded E-only known-answer image anchor probes; never trades."""
import asyncio, hashlib, json, os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from backend.book_pa_chart_v03 import render_numbered_chart
from backend.market import Candle
from backend.vision_provider import ConfigurableVisionAdapter, VisionProviderConfig

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'docs/reports/e-integration/real-anchor-probe'

def load_key():
    if os.getenv('DEEPSEEK_API_KEY'): return
    for line in (ROOT/'.env').read_text(encoding='utf-8-sig').splitlines():
        key,sep,value=line.partition('=')
        if sep and key.strip()=='DEEPSEEK_API_KEY':
            os.environ[key.strip()]=value.strip().strip("\"'")
            return

async def main():
    OUT.mkdir(parents=True,exist_ok=True); load_key()
    cfg=VisionProviderConfig(enabled=True,calls_authorized=True,model='deepseek-flash',thinking_mode='disabled',
        max_concurrency=1,max_output_tokens=256,max_requests_per_day=4,max_call_cost_usd=Decimal('.05'),
        daily_budget_usd=Decimal('.20'),total_budget_usd=Decimal('.20'),input_price_per_million_usd=Decimal('.30'),
        output_price_per_million_usd=Decimal('1.20'),budget_db_path=str(OUT/'budget.sqlite3'))
    adapter=ConfigurableVisionAdapter(cfg);end=datetime(2026,1,4,tzinfo=UTC)
    results=[]
    for n,label in enumerate((7,19,31,43),1):
        candles=[]
        for i in range(288):
            t=end-timedelta(minutes=15*(288-i)); high='145' if i==240+label-1 else '103'
            candles.append(Candle('BTC/USD','15Min',t.isoformat(),(t+timedelta(minutes=15)).isoformat(),
                '100',high,'98','101','0',0,None,True))
        path=OUT/f'case-{n}.png';meta=render_numbered_chart(candles,path)
        prompt=('Look at the lower LOCAL panel only. Which single numbered candle K01 to K48 has the distinctly highest wick? '
                'Return only JSON with the field candle, e.g. {"candle":"K01"}. Do not infer from the example.')
        response=await adapter._send_image_request(path.read_bytes(),prompt,f'e-anchor-probe-v1:{n}')
        observed=response.get('candle');expected=f'K{label:02}'
        result={'case':n,'expected':expected,'observed':observed,'matched':observed==expected,
                'chart_sha256':meta['chart_sha256'],'candle_sha256':meta['candle_sha256'],
                'expected_bar_open_at':meta['numbered_candles'][label-1]['open_at'],
                'expected_ohlc_high':meta['numbered_candles'][label-1]['high'],
                'status':response.get('status','RESPONSE_RECEIVED'),'usage':response.get('usage'),
                'estimated_cost_usd':response.get('estimated_cost_usd'),
                'provider_diagnostics':response.get('provider_diagnostics')}
        results.append(result);(OUT/'results.json').write_text(json.dumps(results,ensure_ascii=False,indent=2,default=str),encoding='utf8')
        print('case',n,'status',result['status'],'match',result['matched'],flush=True)
        if response.get('status'): break
    print('budget',json.dumps(adapter._budget.summary(),ensure_ascii=False),flush=True)

if __name__=='__main__': asyncio.run(main())
