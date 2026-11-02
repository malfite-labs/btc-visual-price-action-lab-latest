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
