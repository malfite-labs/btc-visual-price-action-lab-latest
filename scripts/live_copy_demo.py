"""Explicit, isolated Demo acceptance for a persisted C round trip."""
from datetime import UTC, datetime
from decimal import Decimal as D, ROUND_FLOOR, ROUND_CEILING
from hashlib import sha256
from uuid import uuid4
from pathlib import Path
import json
import os

from scripts.live_center import dpapi
from scripts.live_copy_v2 import order_quantity, paper_fills, planned_margin
from scripts.live_c_demo_exchange import DemoExchange
from scripts.live_c_demo_executor import DemoExecutor
from scripts.live_c_intent import EntryIntent


def hedge_params(direction, *, closing):
    if direction not in ('LONG', 'SHORT'):
        raise ValueError('INVALID_DIRECTION')
    buy = (direction == 'LONG') != closing
    return {'side': 'BUY' if buy else 'SELL', 'positionSide': direction}


def hedge_order_payload(direction, quantity, client_id, *, closing=False, protection=None, trigger=None):
    """Build an offline payload for quantity-based HEDGE orders."""
    if not quantity.is_finite() or quantity <= 0 or not client_id:
        raise ValueError('INVALID_HEDGE_QUANTITY_OR_ID')
    base = {'symbol':'BTCUSDT', 'quantity':format(quantity,'f'),
            **hedge_params(direction, closing=closing)}
    if protection is None:
        return {**base,'type':'MARKET','newClientOrderId':client_id}
    if (not closing or protection not in ('STOP_MARKET','TAKE_PROFIT_MARKET')
            or trigger is None or not trigger.is_finite() or trigger <= 0):
        raise ValueError('INVALID_HEDGE_PROTECTION')
    return {**base,'type':protection,'algoType':'CONDITIONAL','clientAlgoId':client_id,
            'triggerPrice':format(trigger,'f'),'workingType':'CONTRACT_PRICE'}


def round_trip(exchange, directory, opening, closing, *, fixed, budget, leverage, stop_fraction, risk_cap):
    if ((opening.action, closing.action) != ('OPEN', 'FULL_CLOSE')
            or (opening.trade_id, opening.direction, opening.quantity) !=
               (closing.trade_id, closing.direction, closing.quantity)
            or closing.cursor <= opening.cursor or opening.version != 'V3.1'):
        raise ValueError('UNVERIFIED_PAPER_ROUND_TRIP')
    intent, tick = prepare_entry(exchange, opening, 'DEMO-COPY-REPLAY',
        sha256(f'{directory.name}:{opening.event_id}'.encode()).hexdigest()[:28],
        fixed=fixed,budget=budget,leverage=leverage,stop_fraction=stop_fraction,risk_cap=risk_cap)
    engine = DemoExecutor(exchange,directory,intent.account_id,tick)
    return execute_round_trip(exchange,directory,opening,closing,intent,engine,tick)


def prepare_entry(exchange, opening, account_id, token, *, fixed, budget, leverage, stop_fraction, risk_cap,
                  target_fraction=None, exit_mode='LEGACY_BRACKET'):
    snapshot = exchange.risk_snapshot()
    if snapshot['positions'] or snapshot['orders'] or snapshot['conditional']:
        raise ValueError('DEMO_ACCOUNT_CONFLICT')
    settings = snapshot['symbol'][0]
    if settings['marginType'] != 'ISOLATED' or not snapshot['permissions']['enableFutures']:
        raise ValueError('DEMO_SETTINGS_INVALID')
    info = next(row for row in exchange._read('exchange_info')['symbols'] if row['symbol'] == 'BTCUSDT')
    filters = {r['filterType']:r for r in info['filters']}
    lot, market = filters['LOT_SIZE'], filters['MARKET_LOT_SIZE']
    quote = exchange._read('ticker')
    now = datetime.now(UTC)
    if (info['status'] != 'TRADING' or quote.get('symbol') != 'BTCUSDT'
            or not 0 <= now.timestamp()*1000-int(quote['time']) <= 60_000):
        raise ValueError('DEMO_MARKET_UNVERIFIED')
    price = D(quote['price'])
    tick = D(filters['PRICE_FILTER']['tickSize'])
    if stop_fraction is not None and (not stop_fraction.is_finite() or not 0 < stop_fraction < 1):
        raise ValueError('INVALID_TEST_STOP')
    long = opening.direction == 'LONG'
    stop = None
    if stop_fraction is not None:
        stop = price * (1-stop_fraction if long else 1+stop_fraction)
        stop = (stop/tick).to_integral_value(rounding=ROUND_FLOOR if long else ROUND_CEILING)*tick
    margin = planned_margin(opening, budget=budget, fixed=fixed)
    steps = [D(r['stepSize']) for r in (lot, market) if D(r['stepSize']) > 0]
    step = max(steps)
    if any(step % s for s in steps):
        raise ValueError('INCOMPATIBLE_QUANTITY_FILTERS')
    qty = order_quantity(margin=margin, available=snapshot['available'], price=price,
                         planned_leverage=leverage, actual_leverage=int(settings['leverage']),
                         step=step, minimum=max(D(lot['minQty']), D(market['minQty'])),
                         maximum=min(D(lot['maxQty']), D(market['maxQty'])),
                         min_notional=D(filters['MIN_NOTIONAL']['notional']), reserve=D('.10'),
                         fee=D(exchange._read('commission')['takerCommissionRate']),
                         risk_cap=risk_cap, stop_distance=abs(price-stop) if stop is not None else None)
    intent = EntryIntent(account_id,now.isoformat(),opening.version,'dcp-'+token,
                         opening.direction,qty,price,stop,stop,exit_mode,target_fraction)
    return intent, tick


def execute_round_trip(exchange,directory,opening,closing,intent,engine,tick):
    (directory/'source.json').write_text(json.dumps({
        'origin':'HISTORICAL_REPLAY', 'open':opening.event_id, 'close':closing.event_id,
        'trade':opening.trade_id, 'client_order':intent.client_order_id}),encoding='utf8')
    print('DEMO_LEDGER',directory,flush=True)
    try:
        entry = engine.accept(intent, now=datetime.now(UTC))
        print('DEMO_ENTRY',entry,flush=True)
        if entry == 'PROTECTED':
            restarted = DemoExecutor(exchange,directory,intent.account_id,tick)
            recovery = restarted.reconcile(intent,verify_only=True)
            print('DEMO_RECOVERY',recovery,flush=True)
            if recovery != 'PROTECTED':
                raise RuntimeError('DEMO_RECOVERY_FAILED')
    finally:
        exit_state = None
        if engine.active():
            exit_state = engine.exit_demo_position(intent)
            print('DEMO_EXIT',exit_state,flush=True)
        position, orders, algos = exchange.exposure()
        print('DEMO_FINAL',position,len(orders),len(algos),flush=True)
        if position or orders or algos:
            raise RuntimeError('DEMO_FINAL_EXPOSURE_NOT_FLAT')
    if exit_state != 'CLOSED':
        raise RuntimeError('DEMO_EXIT_NOT_RECONCILED')
    return entry


def main():
    from argparse import ArgumentParser
    parser = ArgumentParser(description=__doc__)
    parser.add_argument('paper_db',type=Path)
    parser.add_argument('--execute-isolated-demo',action='store_true',required=True)
    args = parser.parse_args()
    events = paper_fills(args.paper_db,0)
    closing = {e.trade_id:e for e in events if e.action == 'FULL_CLOSE'}
    opening = next(e for e in events if e.action == 'OPEN' and e.trade_id in closing)
    root = Path(os.environ['LOCALAPPDATA'])/'BTC Paper Lab V3 Demo Acceptance'
    key, secret = json.loads(dpapi((root/'credentials.dpapi').read_bytes(),decrypt=True))
    directory = root/('copy-v2-'+uuid4().hex)
    directory.mkdir()
    exchange = DemoExchange(key,secret)
    with exchange.order_ownership():
        result = round_trip(exchange,directory,opening,closing[opening.trade_id],
                            fixed=D(20),budget=D(100),leverage=5,stop_fraction=D('.003'),risk_cap=D(2))
    if result != 'PROTECTED':
        raise SystemExit(2)


if __name__ == '__main__':
    main()
