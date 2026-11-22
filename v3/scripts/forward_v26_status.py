"""Read-only summary of isolated forward shadow ledger."""
import json,sqlite3,sys
from pathlib import Path
from datetime import datetime,timezone
from decimal import Decimal
ROOT=Path(__file__).resolve().parents[1];p=ROOT/'docs/reports/abc-forward-v26';db=p/'shadow-ledger.sqlite3';config=json.loads((p/'freeze.json').read_text(encoding='utf8'));status=json.loads((p/'status.json').read_text(encoding='utf8')) if (p/'status.json').exists() else {'state':'UNKNOWN'}
with sqlite3.connect(db.resolve().as_uri()+'?mode=ro',uri=True) as c:
 out={'status':status,'freeze_utc':config['frozen_utc'],'first_eligible_open_utc':config['earliest_forward_open_utc'],'first_eligible_close_utc':config['earliest_forward_close_utc'],'archived_forward_bars':c.execute('select count(*) from forward_bars').fetchone()[0],'completed_cycles':c.execute("select count(*) from forward_cycles where status='COMPLETE'").fetchone()[0],'skipped_cycles':c.execute("select count(*) from forward_cycles where status='SKIPPED'").fetchone()[0],'agents':{}}
 for a in 'ABC':
  rows=c.execute('select net_pnl from forward_trades where agent=?',(a,)).fetchall();account=c.execute('select cash,state from accounts where agent=?',(a,)).fetchone();review=c.execute('select batch,status,details from forward_reviews where agent=? order by batch desc limit 1',(a,)).fetchone();out['agents'][a]={'frozen_version':config[a]['version'],'cash':account[0],'position_state':account[1],'closed_trades':len(rows),'realized_net_pnl':str(sum((Decimal(x[0]) for x in rows),Decimal(0))),'review':review}
 print(json.dumps(out,ensure_ascii=False,indent=2,default=str))
