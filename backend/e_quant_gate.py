"""E V1.1 quote/risk diagnostics; this module never places an order."""
from datetime import datetime,UTC,timedelta
from decimal import Decimal,InvalidOperation
from backend.e_quant_v11 import detect_setups
from backend.execution import prepare_entry


def evaluate_quant_candidate(*,store,bars,candidate,quote,now,cost,_shadow_change=None):
    def reject(reason,**extra):return {'order_eligible':False,'reason':reason,**extra}
    try:
        if any(b.get('symbol')!='BTC/USD' or b.get('source')!='ALPACA_US_BTCUSD_SPOT'
               or b.get('currency')!='USD_RESEARCH' or str(b.get('interval','')).lower() not in {'15min','15m'} for b in bars):
            return reject('SNAPSHOT_SOURCE_INVALID')
        result=detect_setups(bars,_shadow_change=_shadow_change)
        if not candidate or result.get('selected') != candidate:
            return reject('SNAPSHOT_OR_CANDIDATE_MISMATCH')
        close=datetime.fromisoformat(bars[-1]['close_at'].replace('Z','+00:00'))
        if now.tzinfo is None or close.tzinfo is None or not close.astimezone(UTC)<=now.astimezone(UTC)<close.astimezone(UTC)+timedelta(minutes=15):
            return reject('SIGNAL_EXPIRED')
        if not quote:return reject('QUOTE_UNAVAILABLE')
        if quote.get('source')!='ALPACA_US_BTCUSD_SPOT' or quote.get('currency')!='USD_RESEARCH' or quote.get('timestamp_valid') is not True:
            return reject('QUOTE_INVALID')
        quote_time=datetime.fromisoformat(str(quote['event_at']).replace('Z','+00:00'))
        if quote_time.tzinfo is None or quote_time.astimezone(UTC)<close.astimezone(UTC) or quote_time.astimezone(UTC)>now.astimezone(UTC):
            return reject('QUOTE_TIME_INVALID')
        actual_age=int((now.astimezone(UTC)-quote_time.astimezone(UTC)).total_seconds()*1000)
        age=max(int(quote.get('age_ms',10**9)),actual_age)
        if not 0<=age<=cost.max_quote_age_ms:return reject('QUOTE_STALE')
        if store.account('E')['state']!='FLAT':return reject('POSITION_NOT_FLAT')
        metrics=store.account_metrics('E',quote,cost);side=candidate['side'];stop=Decimal(candidate['stop']);reward=Decimal(candidate['reward'])
        pre=prepare_entry(equity=Decimal(metrics['equity']),available_cash=Decimal(metrics['available_cash']),direction=side,
                          stop=stop,quote=quote,orderbook=None,cost=cost,quote_age_ms=age,book_age_ms=0)
        if not pre.get('accepted'):return reject('RISK_REJECTED',preflight=pre)
        entry=Decimal(pre['fill_price']);slip=cost.adverse_slippage_bps/Decimal(10000)
        reward_fill=reward*(1-slip if side=='LONG' else 1+slip)
        gross=(reward_fill-entry)*(1 if side=='LONG' else -1)
        net_reward=gross-(entry+reward_fill)*cost.fee_rate
        risk=Decimal(pre['planned_loss'])/Decimal(pre['quantity'])
        if risk<=0 or net_reward<2*risk:return reject('RR_BELOW_2',risk_per_unit=str(risk),reward_per_unit=str(net_reward))
        return reject('VISUAL_AND_EXECUTION_NOT_VALIDATED',risk_per_unit=str(risk),reward_per_unit=str(net_reward),preflight=pre)
    except (KeyError,ValueError,TypeError,ArithmeticError,InvalidOperation):
        return reject('INVALID_CONTEXT')
