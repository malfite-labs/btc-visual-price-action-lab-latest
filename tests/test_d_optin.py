import os
import subprocess
import sys
from pathlib import Path


def test_d_paper_optin_requires_explicit_boolean_and_existing_gate(tmp_path):
    script = r'''
import json, os
from pathlib import Path
from backend import app
root = Path(os.environ['D_TEST_ROOT'])
app.ROOT = root
app.DATA_ROOT = root / 'data'
app.formal_mode = 'RULE_DRIVEN'
app.paper_automation_enabled = True
os.environ.pop('D_AUTO_PAPER_ENABLED', None)
assert not app._d_auto_paper_requested()
path = root / 'data' / 'd' / 'auto-paper.json'
path.parent.mkdir(parents=True)
path.write_text(json.dumps({'mode': 'AUTO_PAPER', 'enabled': 1}), encoding='utf8')
assert not app._d_auto_paper_requested()
path.write_text(json.dumps({'mode': 'AUTO_PAPER', 'enabled': True}), encoding='utf8')
assert app._d_auto_paper_requested()
app.paper_automation_enabled = False
assert not app._d_auto_paper_requested()
os.environ['D_AUTO_PAPER_ENABLED'] = 'true'
assert not app._d_auto_paper_requested()
'''
    env = os.environ.copy()
    env.update(BTC_LAB_DB=str(tmp_path / 'paper.sqlite3'),
               BTC_LAB_CANDLE_DB=str(tmp_path / 'candles.sqlite3'),
               PAPER_AUTOMATION_ENABLED='false', D_TEST_ROOT=str(tmp_path))
    run = subprocess.run([sys.executable, '-c', script], cwd=Path(__file__).resolve().parents[1],
                         env=env, capture_output=True, text=True, timeout=30)
    assert run.returncode == 0, run.stdout + run.stderr
