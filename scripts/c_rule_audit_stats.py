"""Read-only formal C signal statistics since the audit rollout; never places orders."""
import json,sqlite3
from pathlib import Path
from collections import Counter
from datetime import datetime,timezone
ROOT=Path(__file__).resolve().parents[1]
def stats(db,start):
 with sqlite3.connect(Path(db).resolve().as_uri()+'?mode=ro',uri=True) as c:
  rows=c.execute("select decision_id,candle_close,outcome,raw_response from decisions where agent='C' and mode='RULE_DRIVEN' and action in ('OPEN_LONG','OPEN_SHORT')").fetchall()
  chosen=[r for r in rows if datetime.fromisoformat(r[1].replace('Z','+00:00')).astimezone(timezone.utc)>=start]
  reasons=Counter();filled=0
  for ident,_,outcome,raw in chosen:
   opened=c.execute("select count(*) from fills where agent='C' and decision_id=? and position_effect='OPEN'",(ident,)).fetchone()[0]>0
   if opened:filled+=1
   elif outcome=='RISK_REJECTED':
    try:reason=json.loads(raw).get('execution',{}).get('reason') or 'DETAIL_UNAVAILABLE'
    except (ValueError,TypeError,AttributeError):reason='DETAIL_UNAVAILABLE'
    reasons[reason]+=1
  return {'since_utc':start.isoformat(),'signal_count':len(chosen),'filled_count':filled,'rejections':dict(reasons),'other_unfilled':len(chosen)-filled-sum(reasons.values())}
if __name__=='__main__':
 config=json.loads((ROOT/'docs/reports/formal-rule-v26/c-audit-rollout.json').read_text(encoding='utf-8-sig'))
 print(json.dumps(stats(ROOT/'data/paper.sqlite3',datetime.fromisoformat(config['activated_at_utc'].replace('Z','+00:00'))),ensure_ascii=False,indent=2))
