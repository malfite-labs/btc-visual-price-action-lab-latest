import hashlib
import json
from pathlib import Path

from backend.strategy import StrategyRegistry


def test_e_registered_only_when_both_immutable_files_match(tmp_path):
    root = tmp_path / 'strategies'
    e = root / 'E'
    e.mkdir(parents=True)
    seed = b'fixed seed'
    md = b'E strategy rules'
    (e / 'HANBA_SEED_V1.md').write_bytes(seed)
    (e / 'E-V1.0.md').write_bytes(md)
    (e / 'E-V1.0.metadata.json').write_text(json.dumps({
        'agent': 'E', 'version': 'E-V1.0', 'seed_sha256': hashlib.sha256(seed).hexdigest(),
        'md_sha256': hashlib.sha256(md).hexdigest(), 'immutable': True,
    }), encoding='utf-8')
    registry = StrategyRegistry(root)
    assert registry.current_strategy('E') == ('E-V1.0', md.decode())
    assert registry.list()[-1]['status'] == 'READY_FOR_VALIDATION'
    (e / 'HANBA_SEED_V1.md').write_text('changed', encoding='utf-8')
    assert registry.current_strategy('E') is None
    assert registry.list()[-1]['status'] == 'INTEGRITY_ERROR'


def test_v11_candidate_registered_without_activating_or_changing_seed(tmp_path):
    root=tmp_path/'strategies';e=root/'E';e.mkdir(parents=True)
    seed=b'seed';md=b'v1';v11=b'v11 candidate'
    (e/'HANBA_SEED_V1.md').write_bytes(seed);(e/'E-V1.0.md').write_bytes(md);(e/'E-V1.1-QUANT.md').write_bytes(v11)
    (e/'E-V1.0.metadata.json').write_text(json.dumps({'agent':'E','version':'E-V1.0','seed_sha256':hashlib.sha256(seed).hexdigest(),'md_sha256':hashlib.sha256(md).hexdigest(),'immutable':True}),encoding='utf8')
    (e/'E-V1.1-QUANT.metadata.json').write_text(json.dumps({'agent':'E','version':'E-V1.1-QUANT','seed_sha256':hashlib.sha256(seed).hexdigest(),'md_sha256':hashlib.sha256(v11).hexdigest(),'immutable':True,'status':'CANDIDATE_NOT_ACTIVE'}),encoding='utf8')
    registry=StrategyRegistry(root);result=registry.list()[-1]
    assert result['current_version']=='E-V1.0'
    assert result['candidate_version']=='E-V1.1-QUANT'
    assert registry.current_strategy('E')==('E-V1.0','v1')
    assert (e/'HANBA_SEED_V1.md').read_bytes()==seed
    (e/'E-V1.1-QUANT.md').write_bytes(b'altered')
    assert registry.list()[-1]['candidate_version'] is None
    assert registry.current_strategy('E')==('E-V1.0','v1')


def test_auto_paper_registration_keeps_v10_current_and_names_the_driver(tmp_path):
    root=tmp_path/'strategies';e=root/'E';e.mkdir(parents=True)
    seed=b'seed';md=b'v1';v11=b'v11 paper'
    (e/'HANBA_SEED_V1.md').write_bytes(seed);(e/'E-V1.0.md').write_bytes(md);(e/'E-V1.1-QUANT.md').write_bytes(v11)
    (e/'E-V1.0.metadata.json').write_text(json.dumps({'agent':'E','version':'E-V1.0','seed_sha256':hashlib.sha256(seed).hexdigest(),'md_sha256':hashlib.sha256(md).hexdigest(),'immutable':True}),encoding='utf8')
    (e/'E-V1.1-QUANT.metadata.json').write_text(json.dumps({'agent':'E','version':'E-V1.1-QUANT','seed_sha256':hashlib.sha256(seed).hexdigest(),'md_sha256':hashlib.sha256(v11).hexdigest(),'immutable':True,'status':'AUTO_PAPER'}),encoding='utf8')
    registry=StrategyRegistry(root);result=registry.list()[-1]
    assert result['current_version']=='E-V1.0'
    assert result['candidate_version']=='E-V1.1-QUANT'
    assert result['candidate_status']=='AUTO_PAPER'
    assert result['status']=='AUTO_PAPER'
    assert result['runtime_mode']=='AUTO_PAPER'
    assert result['driving_version']=='E-V1.1-QUANT'
    assert result['archived_version']=='E-V1.0'
    assert registry.current_strategy('E')==('E-V1.0','v1')


def test_repo_e_metadata_reports_auto_paper_driver():
    registry=StrategyRegistry(Path(__file__).resolve().parents[1]/'strategies')
    result=next(row for row in registry.list() if row['agent']=='E')
    assert result['candidate_version']=='E-V1.1-QUANT'
    assert result['candidate_status']=='AUTO_PAPER'
    assert result['driving_version']=='E-V1.1-QUANT'
    assert result['current_version']=='E-V1.0'
    assert registry.current_strategy('E')[0]=='E-V1.0'


def test_live_runtime_confirms_or_withholds_auto_paper_display():
    from backend.strategy import present_e_runtime
    registered={'agent':'E','current_version':'E-V1.0','status':'READY_FOR_VALIDATION','candidate_version':'E-V1.1-QUANT','candidate_status':'CANDIDATE_NOT_ACTIVE'}
    shown=present_e_runtime(registered, {'present':True,'mode':'AUTO_PAPER','version':'E-V1.1-QUANT'})
    assert shown['candidate_version']=='E-V1.1-QUANT'
    assert shown['candidate_status']=='AUTO_PAPER' and shown['driving_version']=='E-V1.1-QUANT'
    assert registered['candidate_status']=='CANDIDATE_NOT_ACTIVE'
    paused=present_e_runtime({'agent':'E','status':'AUTO_PAPER','candidate_status':'AUTO_PAPER','candidate_version':'E-V1.1-QUANT','runtime_mode':'AUTO_PAPER'}, {'present':True,'mode':'NOT_AUTO','version':'E-V1.0'})
    assert paused['status']=='REGISTERED_NOT_RUNNING'
    assert paused['candidate_version']=='E-V1.1-QUANT'
