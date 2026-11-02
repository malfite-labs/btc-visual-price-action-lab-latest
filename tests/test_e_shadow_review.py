from datetime import UTC,datetime,timedelta
from decimal import Decimal as D
from backend.e_shadow_review import EShadowReview
from backend.e_paper_store import EPaperStore
from backend.evolution import Metrics


def test_e_candidate_requires_40_real_closed_trades(tmp_path):
    store=EPaperStore(tmp_path/'shadow.sqlite');review=EShadowReview(store)
    result=review.propose(batch=40,candidate_md='# E candidate',now=datetime(2026,1,1,tzinfo=UTC))
    assert result['status']=='INSUFFICIENT_CLOSED_TRADES'


def test_e_candidate_comparison_obeys_seven_day_and_user_approval(tmp_path):
    store=EPaperStore(tmp_path/'shadow.sqlite')
    for i in range(40):
        a=store.account('E');assert store.open_position(agent='E',direction='LONG',quantity=D('0.1'),price=D('100'),stop=D('90'),fee=D('.01'),slippage=D('0'),idempotency_key=f'o{i}',expected_version=a['version'])['accepted']
        a=store.account('E');assert store.close_position(agent='E',price=D('100'),fee=D('.01'),slippage=D('0'),idempotency_key=f'c{i}',expected_version=a['version'])['accepted']
    review=EShadowReview(store); start=datetime(2026,1,1,tzinfo=UTC)
    assert review.propose(batch=40,candidate_md='# E candidate',now=start)['status']=='CANDIDATE_CREATED'
    c=Metrics(D(50),D(2),D('.05'),20);i=Metrics(D(20),D('1.5'),D('.05'),20)
    kwargs={'candidate':c,'incumbent':i,'candidate_initial_equity':D(10000),'incumbent_initial_equity':D(10000),'candidate_model_id':'m','incumbent_model_id':'m','candidate_cost_hash':'h','incumbent_cost_hash':'h','candidate_market_event_ids':['x'],'incumbent_market_event_ids':['x']}
    assert review.evaluate(now=start+timedelta(days=6),checkpoint_day=7,**kwargs)['outcome']=='CHECKPOINT_NOT_DUE'
    assert review.evaluate(now=start+timedelta(days=7),checkpoint_day=7,**kwargs)['outcome']=='PROMOTION_PENDING'
    assert review.activate(approved=False,old_position_open=False,at_new_cycle=True)['status']=='APPROVAL_REQUIRED'


def test_e_candidate_state_is_visible_from_read_only_account_snapshot(tmp_path):
    from backend.e_paper_store import read_e_store
    store=EPaperStore(tmp_path/'shadow.sqlite'); review=EShadowReview(store)
    for n in range(40):
        a=store.account('E');store.open_position(agent='E',direction='LONG',quantity=D('.1'),price=D('100'),stop=D('90'),fee=D('.01'),slippage=D('0'),idempotency_key=f'o{n}',expected_version=a['version'])
        a=store.account('E');store.close_position(agent='E',price=D('100'),fee=D('.01'),slippage=D('0'),idempotency_key=f'c{n}',expected_version=a['version'])
    review.propose(batch=40,candidate_md='# E candidate',now=datetime(2026,1,1,tzinfo=UTC))
    result=read_e_store(store.path)
    assert result['candidate']['status']=='RUNNING'
    assert result['candidate']['batch']==40
