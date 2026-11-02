"""Chronological E V1.1 signal-only replay of the entire available local candle archive."""
import json,sqlite3
from collections import Counter
from pathlib import Path
from backend.e_quant_v11 import detect_setups

ROOT=Path(__file__).resolve().parents[1]
def main():
 db=sqlite3.connect((ROOT/'data/market-candles.sqlite3').resolve().as_uri()+'?mode=ro',uri=True)
 rows=[json.loads(r[0]) for r in db.execute("select payload from candles where symbol='BTC/USD' order by open_at")];db.close()
 counts=Counter(); reasons=Counter();first={};valid=0
 for i in range(287,len(rows)):
  window=rows[i-287:i+1]
  try:r=detect_setups(window)
  except (ValueError,TypeError,KeyError):reasons['SNAPSHOT_INVALID']+=1;continue
  valid+=1;reasons[r['status']]+=1
  for reason in r['diagnostics']:reasons[reason]+=1
  for x in r['candidates']:
   counts[x['setup']]+=1;first.setdefault(x['setup'],{'closed_at':window[-1]['close_at'],'anchor':x['anchor']})
  if r['selected']:reasons['VISUAL_AND_EXECUTION_NOT_VALIDATED']+=1
 summary={'mode':'SIGNAL_ONLY','source':'local archived Alpaca BTC/USD closed OHLC','bar_count':len(rows),
  'start':rows[0]['open_at'] if rows else None,'end':rows[-1]['close_at'] if rows else None,
  'examined_windows':max(0,len(rows)-287),'valid_windows':valid,'setups':{k:counts[k] for k in ('E1','E2','E3','E4')},
  'reason_counts':dict(reasons),'first_examples':first,
  'per_setup':{k:{'detected':counts[k],'candidate_signals':counts[k],'risk_not_evaluated':counts[k],
                  'visual_execution_not_validated':counts[k],'order_eligible':0,'paper_filled':0} for k in ('E1','E2','E3','E4')},
  'order_eligible':0,'paper_filled':0,
  'pnl_after_cost':None,'max_drawdown':None,'costs':None,
  'limitation':'No archived executable next quote or fully validated visual/execution chain; no hypothetical fills or PnL manufactured.'}
 p=ROOT/'docs/reports/e-integration/e-v11-replay.json';p.write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf8')
 print(json.dumps(summary,ensure_ascii=False,indent=2))
if __name__=='__main__':main()
