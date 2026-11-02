"""Budgeted DeepSeek research proposals; no execution API access."""
import asyncio, hashlib, json, os
from decimal import Decimal
from io import BytesIO
from PIL import Image, ImageDraw
from backend.vision_provider import VisionBudget, ConfigurableVisionAdapter, VisionProviderConfig

ALLOWED={'A':('A_PULLBACK','A_SECOND_BREAK'),'B':('B_FALSE_BREAK','B_WICK','B_ENGULF'),'C':('T1','T2','T3','T4')}
def reservation(prompt):
    return (Decimal(len(prompt.encode('utf8'))+1280)*Decimal('.30')+Decimal(1000)*Decimal('1.20'))/Decimal(1000000)
def validate_proposal(agent, value):
    if value.get('setup') not in ALLOWED[agent] or any(not isinstance(value.get(x),str) or not value[x].strip() for x in ('analysis','reason')):
        raise ValueError('Invalid model proposal')
    return value['setup']
def chart(data, cutoff):
    bars=[b for b in data if b['close_at'] < cutoff][-288:]
    if len(bars)!=288: raise ValueError('insufficient training bars')
    image=Image.new('RGB',(900,400),'white'); draw=ImageDraw.Draw(image)
    lo=min(float(b['low']) for b in bars);hi=max(float(b['high']) for b in bars)
    if hi<=lo:raise ValueError('invalid price range')
    def y(p):return 380-(float(p)-lo)/(hi-lo)*350
    for i,b in enumerate(bars):
        x=12+i*3; color='#15784a' if float(b['close'])>=float(b['open']) else '#bd3b3b'
        draw.line((x,y(b['high']),x,y(b['low'])),fill=color);draw.line((x-1,y(b['open']),x-1,y(b['close'])),fill=color,width=2)
    draw.text((10,5),'Last 288 available training candles only; no validation candles',fill='black')
    out=BytesIO();image.save(out,format='PNG');return out.getvalue()
def generate(root, run_path, agent, index, evidence, base_filter, bars, cutoff):
    target=run_path/('round-'+str(index))/'candidates'/(agent+'-ai.json');target.parent.mkdir(exist_ok=True)
    if target.exists():
        record=json.loads(target.read_text(encoding='utf8'));return validate_proposal(agent,record['response'])
    prompt=('You are a research analyst for simulated BTC 15-minute trading. Only use this training evidence; no live trading. '
      'Choose exactly one existing executable setup as a research candidate, not arbitrary code. State mechanisms, losses and costs, limitations. '
      'No guarantee of improvement; avoid selecting solely for number of trades. Return JSON with nonempty strings setup, analysis, reason. '
      'Allowed setups: '+json.dumps(ALLOWED[agent])+'; current filter: '+str(base_filter)+'; training evidence: '+json.dumps(evidence,ensure_ascii=False))
    image=chart(bars,cutoff);request='evolution:'+run_path.name+':'+str(index)+':'+agent
    cap=Decimal(json.loads((run_path/'manifest.json').read_text(encoding='utf8'))['model_budget_usd'])
    local=VisionBudget(str(run_path/'run-budget.sqlite3'));local.bind_total_limit(cap)
    amount=reservation(prompt)
    if amount>Decimal('.05'):raise RuntimeError('AI_CALL_RESERVATION_TOO_LARGE')
    ok,why=local.reserve(request,amount,cap,100,total_limit=cap)
    if not ok:raise RuntimeError('RUN_BUDGET_OR_UNRESOLVED_REQUEST: '+why)
    env=root/'.env'
    if not os.getenv('DEEPSEEK_API_KEY') and env.exists():
        for line in env.read_text(encoding='utf-8-sig').splitlines():
            k,s,v=line.partition('=')
            if s and k.strip()=='DEEPSEEK_API_KEY':os.environ[k.strip()]=v.strip().strip('"\'')
    provider=ConfigurableVisionAdapter(VisionProviderConfig(enabled=True,calls_authorized=True,model='deepseek-flash',thinking_mode='disabled',max_concurrency=1,max_output_tokens=1000,max_requests_per_day=1000,max_call_cost_usd=Decimal('.05'),daily_budget_usd=Decimal('5'),total_budget_usd=Decimal('5'),input_price_per_million_usd=Decimal('.30'),output_price_per_million_usd=Decimal('1.20'),budget_db_path=str(root/'docs/reports/abc-replay-v1/round1-budget.sqlite3')))
    response=asyncio.run(provider._send_image_request(image,prompt,request))
    record={'request_id':request,'agent':agent,'round':index,'training_cutoff':cutoff,'prompt':prompt,'image_sha256':hashlib.sha256(image).hexdigest(),'reserved_usd':str(amount),'response':response}
    target.write_text(json.dumps(record,ensure_ascii=False,indent=2,default=str)+'\n',encoding='utf8')
    local.mark(request,'RESPONSE_RECORDED',response.get('estimated_cost_usd'))
    try: return validate_proposal(agent,response)
    except ValueError as e:raise RuntimeError('MODEL_PROPOSAL_INVALID: '+str(response.get('status','invalid_schema'))) from e
