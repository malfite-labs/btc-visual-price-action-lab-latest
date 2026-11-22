"""Isolated E V1.1 setup-first observation; no order or service lifecycle operations."""
import asyncio,json,sqlite3
from contextlib import closing
from datetime import UTC,datetime
from pathlib import Path
from backend.book_pa_chart_v03 import render_numbered_chart
from backend.e_quant_v11 import detect_setups
from backend.market import Candle


class EQuantObserver:
    def __init__(self,db_path,image_dir,registry,*,recover_interrupted=False,preflight=None):
        self.db_path=Path(db_path);self.image_dir=Path(image_dir);self.registry=registry;self.preflight=preflight
        self.db_path.parent.mkdir(parents=True,exist_ok=True);self.image_dir.mkdir(parents=True,exist_ok=True)
        with closing(sqlite3.connect(self.db_path)) as db,db:
            db.execute('CREATE TABLE IF NOT EXISTS e_quant_observations(close_at TEXT PRIMARY KEY,status TEXT NOT NULL,result_json TEXT NOT NULL)')
            if recover_interrupted:
                db.execute("UPDATE e_quant_observations SET status='INTERRUPTED',result_json=? WHERE status='PENDING'", (json.dumps({'status':'INTERRUPTED','reason':'OUTCOME_UNKNOWN_NO_RETRY'}),))

    async def observe(self,bars,close,adapter):
        if close.tzinfo is None:raise ValueError('TIMEZONE_REQUIRED')
        key=close.astimezone(UTC).isoformat()
        result=detect_setups(bars)
        actual=datetime.fromisoformat(bars[-1]['close_at'].replace('Z','+00:00'))
        if actual.astimezone(UTC)!=close.astimezone(UTC):raise ValueError('SNAPSHOT_MISMATCH')
        entry=next((x for x in self.registry.list() if x['agent']=='E'),None)
        if not entry or entry.get('candidate_version')!='E-V1.1-QUANT':return {'status':'STRATEGY_INTEGRITY_ERROR','order_eligible':False}
        version=self.registry.root/'E'/'E-V1.1-QUANT.md'
        strategy=version.read_text(encoding='utf-8-sig')
        with closing(sqlite3.connect(self.db_path)) as db,db:
            if not db.execute('INSERT OR IGNORE INTO e_quant_observations VALUES(?,?,?)',(key,'PENDING','{}')).rowcount:
                return {'status':'DUPLICATE_BAR','order_eligible':False}
        output={'event_id':'E-V1.1:'+key,'status':result['status'],'candidate':result['selected'],
                'candidates':result['candidates'],'snapshot_sha256':result['snapshot_sha256'],
                'strategy_version':'E-V1.1-QUANT','visual_status':'NOT_REQUESTED',
                'order_eligible':False,'order_reason':'VISUAL_AND_EXECUTION_NOT_VALIDATED'}
        candidate=result['selected']
        if candidate:
            if self.preflight:
                try:
                    output['risk_preflight']=await asyncio.wait_for(self.preflight(bars,candidate),timeout=15)
                except Exception as exc:
                    output['risk_preflight']={'order_eligible':False,'reason':'PREFLIGHT_UNAVAILABLE','error_class':type(exc).__name__}
            else:
                output['risk_preflight']={'order_eligible':False,'reason':'PREFLIGHT_NOT_CONFIGURED'}
            try:
                index=candidate['anchor']['index']
                if not 240<=index<288:
                    output['visual_status']='ANCHOR_OUTSIDE_NUMBERED_CHART'
                elif not adapter.configured:
                    output['visual_status']='MODEL_UNAVAILABLE'
                else:
                    defaults={'symbol':'BTC/USD','interval':'15Min','volume':'0','trade_count':0,'vwap':None,'is_closed':True}
                    candle_fields=set(Candle.__dataclass_fields__)
                    candles=[Candle(**{k:v for k,v in {**defaults,**b}.items() if k in candle_fields}) for b in bars]
                    path=self.image_dir/(close.astimezone(UTC).strftime('%Y%m%dT%H%M%SZ')+'.png')
                    evidence=render_numbered_chart(candles,path);anchor=f'K{index-239:02}'
                    prompt=('仅基于图像质检程序提出的 '+candidate['setup']+' '+candidate['side']+' 结构，'
                      '不判断盈利，不输出价格。返回JSON，verdict为CONFIRM/CONTRADICT/UNCERTAIN，'
                      'anchor_candle为结构失效相关K01至K48或null，reason为文本。\n'+strategy)
                    timeout=float(getattr(getattr(adapter,'config',None),'timeout_seconds',60))
                    response=await asyncio.wait_for(adapter._send_image_request(path.read_bytes(),prompt,'E-V1.1:'+key),timeout=timeout)
                    output.update(chart_sha256=evidence['chart_sha256'],candle_sha256=evidence['candle_sha256'],
                                  expected_anchor=anchor,provider_response=response)
                    if response.get('status'):
                        output['visual_status']='MODEL_FAILED'
                    elif response.get('verdict') not in {'CONFIRM','CONTRADICT','UNCERTAIN'} or not isinstance(response.get('reason'),str):
                        output['visual_status']='MODEL_RESPONSE_INVALID'
                    elif response['verdict']=='UNCERTAIN':
                        output['visual_status']='MODEL_UNCERTAIN'
                    elif response['verdict']=='CONTRADICT':
                        output['visual_status']='MODEL_CONTRADICTION_UNVERIFIED'
                    elif response.get('anchor_candle')!=anchor:
                        output['visual_status']='ANCHOR_MISMATCH'
                    else:
                        output['visual_status']='VISUAL_MATCH_UNVALIDATED'
            except TimeoutError:
                output['visual_status']='MODEL_TIMEOUT'
            except Exception as exc:
                output['visual_status']='OBSERVATION_ERROR';output['error_class']=type(exc).__name__
        with closing(sqlite3.connect(self.db_path)) as db,db:
            db.execute('UPDATE e_quant_observations SET status=?,result_json=? WHERE close_at=?',
                       (output['status'],json.dumps(output,ensure_ascii=False,default=str),key))
        return output


def selected_observe_strategy(config_dir):
    """Select only a recognized explicitly configured observe-only strategy."""
    import os
    explicit=os.getenv('E_OBSERVE_STRATEGY')
    if explicit is None:
        path=Path(config_dir)/'observe-version.json'
        if not path.exists():return 'E-V1.0'
        try:
            value=json.loads(path.read_text(encoding='utf8'))
            if value.get('mode')!='OBSERVE_ONLY':return None
            explicit=value.get('version')
        except (OSError,ValueError,TypeError):
            return None
    return explicit if explicit in {'E-V1.0','E-V1.1-QUANT'} else None


def read_quant_observations(path,limit=50):
    path=Path(path)
    if not path.exists():return {'status':'NOT_STARTED','items':[]}
    if not 1<=limit<=200:raise ValueError('INVALID_LIMIT')
    try:
        with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True)) as db:
            rows=db.execute('SELECT close_at,status,result_json FROM e_quant_observations ORDER BY close_at DESC LIMIT ?', (limit,)).fetchall()
        return {'status':'SIGNAL_ONLY','items':[{'close_at':at,'status':status,'result':json.loads(data)} for at,status,data in rows]}
    except (sqlite3.Error,ValueError):
        return {'status':'UNAVAILABLE','items':[]}
