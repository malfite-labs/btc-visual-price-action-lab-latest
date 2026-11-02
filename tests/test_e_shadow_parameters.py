import json
from pathlib import Path
import pytest
from backend.e_shadow_parameters import validate_shadow_change

ROOT=Path(__file__).resolve().parents[1]/'strategies'/'E'

def test_whitelist_accepts_one_authorized_change_without_touching_formal():
    result=validate_shadow_change(ROOT,{'E2_drop_window':6})
    assert result['parameter']=='E2_drop_window' and result['value']==6 and result['baseline']==8

@pytest.mark.parametrize('change',[{}, {'E2_drop_window':7},{'E2_drop_window':6,'E3_breakout_window':3},{'minimum_2R':1},{'E1_range_window':48},{'E3_breakout_window':11},{'E2_drop_window':True}])
def test_whitelist_rejects_locked_out_of_range_and_multiple(change):
    with pytest.raises(ValueError):validate_shadow_change(ROOT,change)

def test_whitelist_rejects_changed_file(tmp_path):
    import shutil
    for name in ('E-shadow-parameter-whitelist.json','E-V1.1-QUANT.metadata.json','E-V1.1-QUANT.md','HANBA_SEED_V1.md'):
        shutil.copyfile(ROOT/name,tmp_path/name)
    p=tmp_path/'E-shadow-parameter-whitelist.json';v=json.loads(p.read_text(encoding='utf8'));v['parameters']['E2_drop_window']['values'].append(100);p.write_text(json.dumps(v),encoding='utf8')
    with pytest.raises(ValueError,match='WHITELIST_INTEGRITY'):validate_shadow_change(tmp_path,{'E2_drop_window':100})

def test_whitelist_cannot_be_broadened_by_editing_metadata_too(tmp_path):
    import shutil,hashlib
    for name in ('E-shadow-parameter-whitelist.json','E-V1.1-QUANT.metadata.json','E-V1.1-QUANT.md','HANBA_SEED_V1.md'):shutil.copyfile(ROOT/name,tmp_path/name)
    p=tmp_path/'E-shadow-parameter-whitelist.json';v=json.loads(p.read_text(encoding='utf8'));v['parameters']['E2_drop_window']['values'].append(100);p.write_text(json.dumps(v),encoding='utf8')
    m=tmp_path/'E-V1.1-QUANT.metadata.json';metadata=json.loads(m.read_text(encoding='utf8'));metadata['shadow_whitelist']['sha256']=hashlib.sha256(p.read_bytes()).hexdigest();m.write_text(json.dumps(metadata),encoding='utf8')
    with pytest.raises(ValueError):validate_shadow_change(tmp_path,{'E2_drop_window':100})

def test_candidate_requires_40_closed_trades_and_survives_reopen(tmp_path):
    from datetime import datetime,UTC
    from decimal import Decimal as D
    from backend.e_paper_store import EPaperStore
    from backend.e_shadow_review import EShadowReview
    from backend.e_shadow_parameters import propose_parameter_candidate
    path=tmp_path/'shadow.sqlite';store=EPaperStore(path);review=EShadowReview(store);now=datetime(2026,1,1,tzinfo=UTC)
    kw={'strategy_dir':ROOT,'change':{'E2_drop_window':6},'batch':40,'now':now}
    assert propose_parameter_candidate(review,**kw)['status']=='INSUFFICIENT_CLOSED_TRADES'
    for i in range(40):
        a=store.account('E');assert store.open_position(agent='E',direction='LONG',quantity=D('.1'),price=D('100'),stop=D('90'),fee=D('.01'),slippage=D('0'),idempotency_key=f'o{i}',expected_version=a['version'])['accepted']
        a=store.account('E');assert store.close_position(agent='E',price=D('100'),fee=D('.01'),slippage=D('0'),idempotency_key=f'c{i}',expected_version=a['version'])['accepted']
    assert propose_parameter_candidate(review,**kw)['status']=='CANDIDATE_CREATED'
    reopened=EShadowReview(EPaperStore(path))
    assert propose_parameter_candidate(reopened,**kw)['status']=='BATCH_ALREADY_RECORDED'

def test_shadow_e2_window_changes_only_candidate_and_not_formal():
    from backend.e_shadow_parameters import detect_shadow_setups
    from backend.e_quant_v11 import detect_setups
    from test_e_quant_v11 import e2_window
    rows=e2_window();before=detect_setups(rows)
    candidate=detect_shadow_setups(ROOT,rows,{'E2_drop_window':6})
    assert candidate['selected']['setup']=='E2'
    assert candidate['selected']['reward']!=before['selected']['reward']
    assert detect_setups(rows)==before

def test_formal_account_cannot_run_shadow_change(tmp_path):
    from backend.e_quant_execution import run_quant_cycle
    from backend.e_paper_store import EPaperStore
    from backend.domain import CostModel
    from test_e_quant_execution import sample
    rows,now,quote=sample();store=EPaperStore(tmp_path/'formal.sqlite')
    result=run_quant_cycle(store=store,bars=rows,quote=quote,now=now,cost=CostModel(),shadow_change={'E2_drop_window':6},strategy_dir=ROOT)
    assert result['reason']=='SHADOW_ACCOUNT_REQUIRED' and store.fills()==[]

def test_whitelisted_candidate_executes_only_in_tagged_shadow_store(tmp_path):
    import sqlite3
    from datetime import datetime,timedelta
    from backend.e_quant_execution import run_quant_cycle
    from backend.e_paper_store import EPaperStore
    from backend.e_quant_v11 import detect_setups
    from backend.domain import CostModel
    from test_e_quant_v11 import e2_window
    rows=e2_window()
    for b in rows:b.update(symbol='BTC/USD',interval='15Min',source='ALPACA_US_BTCUSD_SPOT',currency='USD_RESEARCH')
    now=datetime.fromisoformat(rows[-1]['close_at'])+timedelta(seconds=3)
    candidate=detect_setups(rows,_shadow_change={'E2_drop_window':6})['selected'];ask=float(candidate['stop'])+.04
    quote={'source':'ALPACA_US_BTCUSD_SPOT','currency':'USD_RESEARCH','timestamp_valid':True,'age_ms':0,'bid':str(ask-.01),'ask':str(ask),'event_at':now.isoformat()}
    store=EPaperStore(tmp_path/'shadow.sqlite')
    with sqlite3.connect(store.path) as db:db.execute("INSERT INTO e_store_identity VALUES('account_role','SHADOW')")
    result=run_quant_cycle(store=store,bars=rows,quote=quote,now=now,cost=CostModel(),shadow_change={'E2_drop_window':6},strategy_dir=ROOT)
    assert result['status']=='PAPER_FILLED' and store.account('E')['state']=='OPEN_LONG'
