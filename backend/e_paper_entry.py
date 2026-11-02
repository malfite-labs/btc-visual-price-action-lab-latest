"""E isolated entry prototype; never registered with the running scheduler."""
from decimal import Decimal, InvalidOperation
from backend.e_visual import resolve_e_decision, validate_e_model_response
from backend.execution import prepare_entry


def submit_e_entry(*, ledger, decision, bars, quote, cycle_id, mapping_validated, cost):
    if mapping_validated is not True:
        return {'accepted':False, 'reason':'VISUAL_MAPPING_NOT_VALIDATED'}
    try:
        validate_e_model_response(decision)
        if decision.get('action') != 'OPEN':
            return {'accepted':False,'reason':'NO_ENTRY'}
        if decision.get('confidence') != 'HIGH':
            return {'accepted':False,'reason':'CONFIDENCE_NOT_HIGH'}
        mapped=resolve_e_decision(decision,bars,mapping_verified=True)
        if not quote or quote.get('currency')!='USD_RESEARCH' or quote.get('timestamp_valid') is not True:
            return {'accepted':False,'reason':'INVALID_QUOTE'}
        account=ledger.account('E')
        if account['state']!='FLAT':
            return {'accepted':False,'reason':'POSITION_NOT_FLAT'}
        direction=decision['side']
        stop=Decimal(mapped['stop_price'])+cost.simulated_tick*(-1 if direction=='LONG' else 1)
        metrics=ledger.account_metrics('E',quote,cost)
        pre=prepare_entry(equity=Decimal(metrics['equity']),available_cash=Decimal(metrics['available_cash']),
            direction=direction,stop=stop,quote=quote,orderbook=None,cost=cost,
            quote_age_ms=int(quote['age_ms']),book_age_ms=0)
        if not pre.get('accepted'):
            return pre
        entry=Decimal(pre['fill_price']); reward=Decimal(mapped['reward_price'])
        slip=cost.adverse_slippage_bps/Decimal('10000')
        exit_price=reward*(1-slip if direction=='LONG' else 1+slip)
        gross=(exit_price-entry)*(1 if direction=='LONG' else -1)
        reward_after_cost=gross-(entry+exit_price)*cost.fee_rate
        risk=Decimal(pre['planned_loss'])/Decimal(pre['quantity'])
        if risk <= 0 or reward_after_cost < 2*risk:
            return {'accepted':False,'reason':'REWARD_RISK_BELOW_2'}
        ref=Decimal(quote['ask' if direction=='LONG' else 'bid']); qty=Decimal(pre['quantity'])
        result=ledger.open_position(agent='E',direction=direction,quantity=qty,price=entry,stop=stop,
            fee=Decimal(pre['fee']),slippage=abs(entry-ref)*qty,idempotency_key='E-entry:'+cycle_id,
            expected_version=account['version'],decision_id='E:'+cycle_id,strategy_version='E-V1.0',
            rationale='; '.join(decision['reasons']),quote_id=quote.get('event_id'),cost_model=cost)
        return {**result,'mapping':mapped,'preflight':pre}
    except (KeyError,TypeError,ValueError,InvalidOperation,ArithmeticError):
        return {'accepted':False,'reason':'INVALID_ENTRY_CONTEXT'}
