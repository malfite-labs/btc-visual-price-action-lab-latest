#!/usr/bin/env bash
# Offline verification suite (no real Alpaca, no paper automation arming).
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/common.sh"
assert_venv

EVID="${APP_ROOT}/docs/reports/linux-offline-acceptance"
mkdir -p "$EVID"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
REPORT="${EVID}/verify-${STAMP}.json"
export BTC_LAB_OFFLINE=1
export PAPER_AUTOMATION_ENABLED=false
export VISION_ENABLED=false
export VISION_CALLS_AUTHORIZED=false
export ALPACA_CREDENTIALS_FILE=/home/box/btc-visual-price-lab/secrets/alpaca-offline-empty.env

cd "$APP_ROOT"
python_bin="$VENV_PY"

"$python_bin" - <<'PY' | tee "${EVID}/sqlite-and-freeze-${STAMP}.txt"
import hashlib, json, sqlite3, os
from pathlib import Path
root=Path('.').resolve()
out={"cwd":str(root)}
# freeze 20
freeze=json.loads((root/'docs/reports/abc-forward-v26/freeze.json').read_text())
items=[]
for k in ('A','B','C'):
  items.append((freeze[k]['file'], freeze[k]['sha256'], root/'docs/reports/abc-forward-v26'/freeze[k]['file']))
items.append((freeze['C']['v24_archive'], freeze['C']['archive_sha256'], root/'docs/reports/abc-forward-v26'/freeze['C']['v24_archive']))
for path,h in freeze['risk_execution_hashes'].items():
  items.append((path,h,root/path))
ok=sum(1 for _,exp,p in items if p.is_file() and hashlib.sha256(p.read_bytes()).hexdigest()==exp)
out['freeze_matched']=ok
out['freeze_total']=len(items)
# profile
prof=json.loads((root/'config/rule_v26/profile.json').read_text())
out['formal_versions']={a:prof[a]['version'] for a in 'ABC'}
out['formal_hash_ok']={a: hashlib.sha256((root/'config/rule_v26'/prof[a]['file']).read_bytes()).hexdigest()==prof[a]['sha256'] for a in 'ABC'}
# sqlite
dbs=['data/paper.sqlite3','docs/reports/abc-forward-v26/shadow-ledger.sqlite3','data/market-candles.sqlite3']
sql={}
for rel in dbs:
  p=root/rel
  con=sqlite3.connect(f'file:{p}?mode=ro',uri=True)
  qc=con.execute('PRAGMA quick_check').fetchone()[0]
  sql[rel]={'quick_check':qc}
  tables={r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
  if 'accounts' in tables:
    sql[rel]['accounts']=[dict(zip([c[1] for c in con.execute('PRAGMA table_info(accounts)')],row)) for row in con.execute('SELECT * FROM accounts')]
    # strip huge position details for file? keep as-is for recovery evidence
  if 'evo_counts' in tables:
    sql[rel]['evo_counts']=con.execute('SELECT * FROM evo_counts').fetchall()
  if 'forward_versions' in tables:
    sql[rel]['forward_versions']=con.execute('SELECT agent,batch,activation_open,rule_sha256,created_at FROM forward_versions').fetchall()
  if 'forward_gap_events' in tables:
    un=con.execute('''SELECT e.close_at,e.reason FROM forward_gap_events e LEFT JOIN forward_gap_resolutions r ON r.close_at=e.close_at WHERE r.close_at IS NULL''').fetchall()
    sql[rel]['unresolved_gaps']=un
  con.close()
out['sqlite']=sql
print(json.dumps(out,indent=2,default=str))
open(os.environ.get('REPORT_PATH','/tmp/verify-partial.json'),'w').write(json.dumps(out,indent=2,default=str))
PY

# Lock exclusivity test
"$python_bin" - <<'PY' | tee "${EVID}/lock-exclusivity-${STAMP}.txt"
from pathlib import Path
from backend.market import _StreamLease
import tempfile, os
p=Path('docs/reports/linux-offline-acceptance')/'lock-test.lock'
a=_StreamLease(p)
b=_StreamLease(p)
assert a.acquire() is True
assert b.acquire() is False
assert b.failure_reason=='LOCAL_STREAM_LEASE_HELD'
a.release()
assert b.acquire() is True
b.release()
print('LOCK_EXCLUSIVITY=PASS')
PY

# Module import + profile
"$python_bin" - <<'PY' | tee "${EVID}/imports-${STAMP}.txt"
import os
os.environ['PAPER_AUTOMATION_ENABLED']='false'
os.environ['ALPACA_CREDENTIALS_FILE']='/home/box/btc-visual-price-lab/secrets/alpaca-offline-empty.env'
from backend.rule_cycle import load_profile
from backend.domain import CostModel
from backend import execution, ledger, market
prof,rule,mod=load_profile()
print('IMPORTS=PASS')
print('formal', {a:prof[a]['version'] for a in 'ABC'})
cm=CostModel()
print('fill_model', cm.provenance().get('fill_model'))
print('lease_linux', hasattr(__import__('fcntl'),'flock'))
# Ensure credentials blocked
creds=market.load_market_credentials('/home/box/btc-visual-price-lab/secrets/alpaca-offline-empty.env')
print('offline_creds', creds)
assert creds is None
print('OFFLINE_CREDS_BLOCKED=PASS')
PY

# Targeted pytest (isolated)
"$python_bin" -m pytest tests/test_simple_fill.py tests/test_rule_cycle.py tests/test_auto_protection.py tests/test_recovery_guard.py -q --tb=line \
  | tee "${EVID}/pytest-${STAMP}.txt"

# Frontend dist
test -f frontend/dist/index.html && echo FRONTEND_DIST=PASS | tee "${EVID}/frontend-${STAMP}.txt"

echo "[verify] artifact dir $EVID"
ls -la "$EVID" | tail -20
