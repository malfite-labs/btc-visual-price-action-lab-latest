"""E-only user-authorized candidate envelope; does not modify active rules."""
import hashlib
import json
from pathlib import Path


def validate_shadow_change(strategy_dir,change):
    root=Path(strategy_dir)
    try:
        metadata=json.loads((root/'E-V1.1-QUANT.metadata.json').read_text(encoding='utf8'))
        ref=metadata['shadow_whitelist']
        if ref['file']!='E-shadow-parameter-whitelist.json':raise ValueError('WHITELIST_INTEGRITY')
        content=(root/ref['file']).read_bytes()
        if hashlib.sha256(content).hexdigest()!=ref['sha256']:raise ValueError('WHITELIST_INTEGRITY')
        formal='8fd83c6169c6e0a1d5f680abbfa1cb19cd7bea0cc9c73066a91f2b006964cf43'
        seed='65eab5d3a24057e37653214f70d81e8a62e05162b02187c6194ab95e1accd907'
        if metadata['md_sha256']!=formal or metadata['seed_sha256']!=seed or hashlib.sha256((root/'E-V1.1-QUANT.md').read_bytes()).hexdigest()!=formal or hashlib.sha256((root/'HANBA_SEED_V1.md').read_bytes()).hexdigest()!=seed:raise ValueError('FORMAL_OR_SEED_INTEGRITY')
        whitelist=json.loads(content)
        authorized={'E2_drop_window':([6,8,10,12],8),'E3_breakout_window':(list(range(3,11)),8),'E4_breakdown_window':(list(range(3,11)),8)}
        for key,(values,baseline) in authorized.items():
            r=whitelist['parameters'].get(key) or {}
            if r.get('status')!='AUTHORIZED' or r.get('values')!=values or r.get('baseline')!=baseline:raise ValueError('WHITELIST_INTEGRITY')
        if (whitelist['parameters'].get('E1_range_window') or {}).get('status')!='LOCKED':raise ValueError('WHITELIST_INTEGRITY')
        if not isinstance(change,dict) or len(change)!=1:raise ValueError('ONE_PARAMETER_REQUIRED')
        name,value=next(iter(change.items()));rule=whitelist['parameters'].get(name)
        if name not in authorized or not rule or rule.get('status')!='AUTHORIZED' or type(value) is not int or value not in rule['values'] or value==rule['baseline']:raise ValueError('PARAMETER_NOT_ALLOWED')
        return {'parameter':name,'value':value,'baseline':rule['baseline'],'whitelist_sha256':ref['sha256'],'formal_sha256':metadata['md_sha256'],'seed_sha256':metadata['seed_sha256']}
    except (OSError,KeyError,TypeError,json.JSONDecodeError) as exc:
        raise ValueError('INVALID_CANDIDATE_CONTEXT') from exc


def propose_parameter_candidate(review,*,strategy_dir,change,batch,now):
    """Persist a one-parameter candidate via the existing 40-trade review gate."""
    candidate=validate_shadow_change(strategy_dir,change)
    return review.propose(batch=batch,candidate_md=json.dumps(candidate,sort_keys=True,ensure_ascii=False),now=now)


def detect_shadow_setups(strategy_dir,rows,change):
    from backend.e_quant_v11 import detect_setups
    candidate=validate_shadow_change(strategy_dir,change)
    result=detect_setups(rows,_shadow_change={candidate['parameter']:candidate['value']})
    return {**result,'shadow_candidate':candidate}
