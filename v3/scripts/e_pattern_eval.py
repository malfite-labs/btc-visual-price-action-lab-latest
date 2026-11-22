"""Run blinded E pattern probes against the frozen reference fixtures; never trades."""
import asyncio,hashlib,json,os
from decimal import Decimal
from pathlib import Path
from backend.e_visual import resolve_e_decision,validate_e_model_response
from backend.strategy import StrategyRegistry
from backend.vision_provider import ConfigurableVisionAdapter,VisionProviderConfig
ROOT=Path(__file__).resolve().parents[1];OUT=ROOT/'docs/reports/e-integration/e-pattern-eval'

def key():
 if os.getenv('DEEPSEEK_API_KEY'):return
 for line in (ROOT/'.env').read_text(encoding='utf-8-sig').splitlines():
  k,sep,v=line.partition('=')
  if sep and k.strip()=='DEEPSEEK_API_KEY':os.environ[k.strip()]=v.strip().strip("\"'");return

async def main():
 key(); refs=json.loads((OUT/'reference.json').read_text(encoding='utf8'))
 cfg=VisionProviderConfig(enabled=True,calls_authorized=True,model='deepseek-flash',thinking_mode='disabled',max_concurrency=1,max_output_tokens=1200,max_requests_per_day=12,max_call_cost_usd=Decimal('.05'),daily_budget_usd=Decimal('.60'),total_budget_usd=Decimal('.60'),input_price_per_million_usd=Decimal('.30'),output_price_per_million_usd=Decimal('1.20'),budget_db_path=str(OUT/'budget.sqlite3'))
 adapter=ConfigurableVisionAdapter(cfg);strategy=StrategyRegistry(ROOT/'strategies').current_strategy('E')
 if not strategy:raise RuntimeError('E_STRATEGY_INTEGRITY_FAILED')
 prompt=('只依据图像分析 BTC 15 分钟裸K线；上图为288根，下图编号 K01 至 K48 为最近48根。'
 '按照策略判断，允许观望。返回JSON：action=SKIP或OPEN；side=FLAT/LONG/SHORT；'
 'setup=E1/E2/E3/E4/CHOP/UNKNOWN；confidence=HIGH/MEDIUM/LOW/UNKNOWN；'
 'reasons为字符串数组。OPEN时 stop_anchor 与 reward_anchor 各为 {"candle":"K01至K48","field":"high或low"}。'
 '不要输出任何精确价格。\n'+strategy[1])
 results=[]
 for ref in refs:
  raw=(OUT/ref['ohlc_file']).read_bytes();image=(OUT/ref['chart_file']).read_bytes()
  if hashlib.sha256(raw).hexdigest()!=ref['ohlc_sha256'] or hashlib.sha256(image).hexdigest()!=ref['chart_sha256']:raise RuntimeError('FIXTURE_HASH_MISMATCH')
  response=await adapter._send_image_request(image,prompt,'e-pattern-v1:'+ref['case'])
  item={'case':ref['case'],'source':ref['source'],'reference_setup':ref['expected_setup'],
    'response':response,'status':response.get('status','RESPONSE_RECEIVED'),'chart_sha256':ref['chart_sha256'],
    'ohlc_sha256':ref['ohlc_sha256'],'candle_sha256':ref['candle_sha256'],'strategy_version':strategy[0]}
  if not response.get('status'):
   try:
    validate_e_model_response(response);mapping=resolve_e_decision(response,json.loads(raw),mapping_verified=False)
    item['mapping']=mapping;item['schema_valid']=True
    action=response.get('action');setup=response.get('setup')
    item['classification']='CORRECT_CANDIDATE' if ref['expected_setup']!='SKIP' and action=='OPEN' and setup==ref['expected_setup'] else 'CONSERVATIVE_SKIP' if ref['expected_setup']!='SKIP' and action=='SKIP' else 'CORRECT_SKIP' if ref['expected_setup']=='SKIP' and action=='SKIP' else 'MISMATCH'
    anchor=(response.get('stop_anchor') or {}).get('candle');item['stop_anchor_match']=anchor in ref.get('acceptable_stop_labels',[]) if action=='OPEN' and ref['expected_setup']!='SKIP' else None
    item['mapped_price_consistent']=bool(mapping.get('stop_price') and mapping.get('reward_price')) if action=='OPEN' else None
   except (ValueError,TypeError,KeyError) as e:item.update(schema_valid=False,classification='INVALID_RESPONSE',error=str(e))
  results.append(item);(OUT/'results.json').write_text(json.dumps(results,ensure_ascii=False,indent=2,default=str),encoding='utf8')
  print(ref['case'],item['status'],item.get('classification'),flush=True)
  if response.get('status'):break
 print('budget',json.dumps(adapter._budget.summary(),ensure_ascii=False),flush=True)

if __name__=='__main__':asyncio.run(main())
