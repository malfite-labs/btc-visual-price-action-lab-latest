from datetime import UTC, datetime, timedelta
import pytest
from backend.e_visual import resolve_e_decision


def bars():
    end = datetime(2026, 1, 4, tzinfo=UTC)
    return [{'open_at': (end-timedelta(minutes=15*(288-i))).isoformat(),
             'close_at': (end-timedelta(minutes=15*(287-i))).isoformat(),
             'open':'100','high':'110','low':'90','close':'105','is_closed':True}
            for i in range(288)]


def test_e_maps_numbered_anchor_to_same_snapshot_and_never_trusts_model_price():
    decision={'action':'OPEN','side':'LONG','setup':'E1','confidence':'HIGH',
              'stop_anchor':{'candle':'K01','field':'low'}, 'reward_anchor':{'candle':'K48','field':'high'},
              'reasons':['support reclaim'], 'stop_price':1}
    result=resolve_e_decision(decision,bars(),mapping_verified=False)
    assert result['stop_price'] == '90'
    assert result['reward_price'] == '110'
    assert result['order_allowed'] is False
    assert result['reason'] == 'VISUAL_MAPPING_NOT_VALIDATED'
    assert result['stop_bar_open_at'] == bars()[-48]['open_at']


def test_e_rejects_invalid_anchor_or_snapshot():
    d={'action':'OPEN','side':'LONG','setup':'E1','confidence':'HIGH',
       'stop_anchor':{'candle':'K99','field':'low'},'reward_anchor':{'candle':'K48','field':'high'},'reasons':['x']}
    with pytest.raises(ValueError): resolve_e_decision(d,bars(),mapping_verified=False)
    d['stop_anchor']['candle']='K01'
    broken=bars();broken[-2]['is_closed']=False
    with pytest.raises(ValueError): resolve_e_decision(d,broken,mapping_verified=False)


def test_e_rejects_malformed_skip_decision():
    from backend.e_visual import validate_e_model_response
    with pytest.raises(ValueError):
        validate_e_model_response({'action':'SKIP','side':'LONG','setup':'E1','confidence':'HIGH','reasons':[]})
    with pytest.raises(ValueError):
        validate_e_model_response({'action':'SKIP','side':'FLAT','setup':'CHOP','confidence':'LOW','reasons':[]})
