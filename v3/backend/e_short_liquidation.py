"""One-times E short research margin guard; uses frozen risk and exit primitives."""
import json
from decimal import Decimal,InvalidOperation
from backend.margin import assess_margin
from backend.execution import recover_pending_stop


def protect_e_short_margin(store,quote,cost):
    account=store.account('E')
    if account['state']!='OPEN_SHORT' or not account.get('position_json'):return {'accepted':False,'reason':'NO_SHORT_POSITION'}
    if not quote or quote.get('source')!='BINANCE_USDM_PERP' or quote.get('currency')!='USD_RESEARCH' or quote.get('timestamp_valid') is not True or not 0<=int(quote.get('age_ms',10**9))<=cost.max_quote_age_ms:return {'accepted':False,'reason':'QUOTE_UNAVAILABLE_OR_STALE'}
    try:
        position=json.loads(account['position_json']);price=Decimal(str(quote['ask']));bid=Decimal(str(quote['bid']));qty=Decimal(position['quantity'])
        if not price.is_finite() or not bid.is_finite() or bid<=0 or price<bid:return {'accepted':False,'reason':'QUOTE_INVALID'}
        if position.get('leverage','1')!='1':return {'accepted':False,'reason':'E_SHORT_LEVERAGE_UNSUPPORTED'}
        risk=assess_margin(cash=Decimal(account['cash']),direction='SHORT',quantity=qty,entry_price=Decimal(position['entry_price']),exit_quote=price,leverage=Decimal('1'),maintenance_rate=Decimal('0.01'))
        reserve=qty*price*(cost.fee_rate+cost.adverse_slippage_bps/Decimal(10000))
        if risk.equity>risk.maintenance_margin+reserve:return {'accepted':False,'reason':'LIQUIDATION_NOT_TRIGGERED'}
        event=str(quote.get('event_id') or quote.get('event_at') or '')
        if not event:return {'accepted':False,'reason':'QUOTE_EVENT_MISSING'}
        pending=store.request_stop_exit(agent='E',trigger_price=price,event_key='liquidation:E:'+event,quote_event_id=event,expected_version=account['version'],trigger_evidence={k:quote.get(k) for k in ('source','event_at','age_ms','timestamp_valid','bid','ask')})
        if not pending.get('accepted') or pending.get('duplicate'):return pending
        return recover_pending_stop(ledger=store,agent='E',quote=quote,cost=cost,event_id=event,reason='STOP_LIQUIDATION')
    except (KeyError,ValueError,TypeError,ArithmeticError,InvalidOperation):return {'accepted':False,'reason':'INVALID_RISK_INPUT'}

