"""Signed USD-M Demo transport. The production route has no write client."""
from decimal import Decimal
from contextlib import contextmanager
from threading import get_ident
from hashlib import sha256
import hmac
import json
import time
from urllib.parse import urlencode
from urllib.request import Request, build_opener
from urllib.error import HTTPError

from scripts.live_center import BinanceReader, READS, RejectRedirects
from scripts.live_copy_account_lock import account_owner

DEMO_URL = 'https://demo-fapi.binance.com'
PRODUCTION_URL = 'https://fapi.binance.com'
SYMBOL = 'BTCUSDT'


class DemoApiError(Exception):
    def __init__(self, operation, status, code):
        super().__init__(f'{operation}: HTTP {status}, Binance code {code}')
        self.code = code
        self.operation, self.status = operation, status
        self.method = None


class BinanceFuturesExchange(BinanceReader):
    """Unified USD-M futures transport parameterized by execution environment."""
    def __init__(self, key, secret, *, opener=None, clock=time.time, hedge=False,
                 base_url=DEMO_URL, environment='DEMO', allow_writes=True):
        if not key or not secret:
            raise ValueError(f'Independent {environment} credentials are required')
        if environment not in ('DEMO', 'PRODUCTION'):
            raise ValueError(f'Unsupported exchange environment: {environment}')
        self._key, self._secret = key, secret.encode()
        self._opener = opener or build_opener(RejectRedirects())
        self._clock = clock
        self._offset = 0
        self._write_thread = None
        self._base_url = base_url
        self.environment = environment
        self.allow_writes = bool(allow_writes)
        self.hedge = hedge

    @contextmanager
    def order_ownership(self):
        """Acquire the exchange-confirmed account identity before any write."""
        self.synchronize()
        balances = self._request('GET', '/fapi/v3/balance')
        aliases = [row.get('accountAlias') for row in balances if row.get('asset') == 'USDT']
        if len(aliases) != 1 or not isinstance(aliases[0], str) or not aliases[0].strip():
            raise ValueError('UNVERIFIED_ACCOUNT_IDENTITY')
        if self._write_thread is not None:
            raise RuntimeError('ACCOUNT_ALREADY_OWNED')
        with account_owner(self.environment, aliases[0]):
            self._write_thread = get_ident()
            try:
                yield aliases[0]
            finally:
                self._write_thread = None

    def _request(self, method, path, parameters=None, *, signed=True, clock_retry=True):
        if method != 'GET':
            if not self.allow_writes:
                raise RuntimeError('PRODUCTION_ORDER_DISABLED')
            if not signed or self._write_thread != get_ident():
                raise RuntimeError(f'{self.environment}_WRITE_REQUIRES_ACCOUNT_OWNERSHIP')
        params = dict(parameters or {})
        headers = {}
        if signed:
            params.update(timestamp=int(self._clock() * 1000) + self._offset, recvWindow=5000)
            raw = urlencode(params)
            params['signature'] = hmac.new(self._secret, raw.encode(), sha256).hexdigest()
            headers['X-MBX-APIKEY'] = self._key
        url = self._base_url + path
        if params:
            url += '?' + urlencode(params)
        try:
            with self._opener.open(Request(url, headers=headers, method=method), timeout=8) as response:
                return json.load(response)
        except HTTPError as exc:
            try:
                code = int(json.load(exc).get('code', 0))
            except (ValueError, TypeError, AttributeError):
                code = 0
            if method == 'GET' and signed and clock_retry and code == -1021:
                self.synchronize()
                return self._request(method, path, parameters, signed=signed, clock_retry=False)
            error = DemoApiError(path, exc.code, code)
            error.method = method
            raise error from None
        except (OSError, ValueError):
            error = DemoApiError(path, 0, 0)
            error.method = method
            raise error from None

    def synchronize(self):
        before = self._clock()
        server = self._request('GET', '/fapi/v1/time', signed=False)
        self._offset = int(server['serverTime']) - int((before+self._clock())*500)

    def entry(self, intent):
        return self._request('POST', '/fapi/v1/order', dict(
            symbol=SYMBOL, side='BUY' if intent.direction == 'LONG' else 'SELL',
            positionSide=intent.direction if self.hedge else 'BOTH', type='MARKET', quantity=format(intent.quantity,'f'),
            newClientOrderId=intent.client_order_id))

    def order(self, client_id):
        return self._request('GET', '/fapi/v1/order', dict(symbol=SYMBOL, origClientOrderId=client_id))

    def trades(self, order_id):
        return self._request('GET', '/fapi/v1/userTrades', dict(symbol=SYMBOL, orderId=order_id, limit=1000))

    def account(self):
        return self._request('GET', '/fapi/v3/account')

    def exposure(self):
        mode = self._request('GET', '/fapi/v1/positionSide/dual')
        if mode['dualSidePosition'] is not self.hedge:
            raise ValueError('Exchange position mode mismatch')
        positions = self._request('GET', '/fapi/v3/positionRisk')
        other = [p for p in positions if p['symbol'] != SYMBOL and Decimal(p['positionAmt'])]
        rows = [p for p in positions if p['symbol'] == SYMBOL and Decimal(p['positionAmt'])]
        sides = ('LONG','SHORT') if self.hedge else ('BOTH',)
        if other or len(rows) > 1 or any(p['positionSide'] not in sides for p in rows):
            raise ValueError('Conflicting exchange positions')
        net = Decimal(rows[0]['positionAmt']) if rows else Decimal(0)
        if rows and self.hedge and (net > 0) != (rows[0]['positionSide']=='LONG'):
            raise ValueError('Position side disagrees with signed quantity')
        orders = self._request('GET', '/fapi/v1/openOrders')
        protection = self._request('GET', '/fapi/v1/openAlgoOrders')
        return net, orders, protection

    def place_protection(self, client_id, *, side, kind, trigger, quantity):
        if kind not in ('STOP_MARKET', 'TAKE_PROFIT_MARKET'):
            raise ValueError('Unsupported conditional kind')
        return self._request('POST', '/fapi/v1/algoOrder', dict(
            symbol=SYMBOL, algoType='CONDITIONAL', side=side,
            positionSide=('LONG' if side=='SELL' else 'SHORT') if self.hedge else 'BOTH', type=kind,
            triggerPrice=format(trigger, 'f'), workingType='CONTRACT_PRICE',
            quantity=format(quantity,'f'), clientAlgoId=client_id,
            **({} if self.hedge else {'reduceOnly':'true'})))

    def protection(self, client_id):
        return self._request('GET', '/fapi/v1/algoOrder', dict(clientAlgoId=client_id))

    def cancel_protection(self, client_id):
        return self._request('DELETE', '/fapi/v1/algoOrder', dict(clientAlgoId=client_id))


    def reduce(self, client_id, side, quantity):
        return self._request('POST', '/fapi/v1/order', dict(
            symbol=SYMBOL, side=side, positionSide=('LONG' if side=='SELL' else 'SHORT') if self.hedge else 'BOTH', type='MARKET',
            quantity=format(quantity,'f'), newClientOrderId=client_id,
            **({} if self.hedge else {'reduceOnly':'true'})))


    def _read(self, operation):
        if operation not in {'exchange_info','ticker','brackets','commission','klines'}:
            raise ValueError('Unsupported Demo risk query')
        _, path, params = READS[operation]
        return self._request('GET', path, params,
                             signed=operation in {'brackets','commission'})

    def risk_snapshot(self):
        self.synchronize()
        net, orders, conditional = self.exposure()
        account = self.account()
        asset = next(row for row in account['assets'] if row['asset'] == 'USDT')
        symbol = self._request('GET', '/fapi/v1/symbolConfig', {'symbol':SYMBOL})
        if len(symbol) != 1 or symbol[0]['symbol'] != SYMBOL:
            raise ValueError('Symbol configuration unverified')
        return {'available': Decimal(asset['availableBalance']),
                'positions': [net] if net else [], 'orders': orders, 'conditional': conditional,
                'permissions': {'enableFutures': self._request('GET','/fapi/v1/accountConfig').get('canTrade') is True}, 'symbol':symbol}

    def preflight_report(self, *, planned_leverage=5, fixed_margin=None, budget=None):
        """Strict GET-only preflight check against actual exchange specs. Never modifies account settings."""
        from decimal import ROUND_CEILING
        self.synchronize()
        dual = self._request('GET', '/fapi/v1/positionSide/dual').get('dualSidePosition')
        if type(dual) is not bool:
            raise ValueError('持仓模式无法核实')
        self.hedge = dual
        balances = self._request('GET', '/fapi/v3/balance')
        usdt_bal = next((r for r in balances if r.get('asset') == 'USDT'), None)
        alias = usdt_bal.get('accountAlias') if usdt_bal else None
        if not isinstance(alias, str) or not alias.strip():
            raise ValueError('账户身份无法核实')
        account = self.account()
        asset = next((r for r in account.get('assets', []) if r.get('asset') == 'USDT'), None)
        if asset is None:
            raise ValueError('账户响应缺少 USDT')
        available = Decimal(asset['availableBalance'])
        wallet = Decimal(asset.get('walletBalance', '0'))
        positions = self._request('GET', '/fapi/v3/positionRisk')
        btc_positions = [p for p in positions if p.get('symbol') == SYMBOL and Decimal(p.get('positionAmt', '0')) != 0]
        other_positions = [p for p in positions if p.get('symbol') != SYMBOL and Decimal(p.get('positionAmt', '0')) != 0]
        orders = self._request('GET', '/fapi/v1/openOrders')
        algos = self._request('GET', '/fapi/v1/openAlgoOrders')
        symbol_cfg = self._request('GET', '/fapi/v1/symbolConfig', {'symbol': SYMBOL})
        if len(symbol_cfg) != 1 or symbol_cfg[0].get('symbol') != SYMBOL:
            raise ValueError('BTCUSDT 合约配置无法核实')
        margin_type = symbol_cfg[0].get('marginType')
        actual_leverage = int(symbol_cfg[0].get('leverage'))
        can_trade = self._request('GET', '/fapi/v1/accountConfig').get('canTrade') is True
        info = next(r for r in self._read('exchange_info')['symbols'] if r['symbol'] == SYMBOL)
        filters = {f['filterType']: f for f in info['filters']}
        lot, mlot = filters['LOT_SIZE'], filters['MARKET_LOT_SIZE']
        min_qty = max(Decimal(lot['minQty']), Decimal(mlot['minQty']))
        step_size = max(Decimal(lot['stepSize']), Decimal(mlot['stepSize']))
        tick_size = Decimal(filters['PRICE_FILTER']['tickSize'])
        min_notional = Decimal(filters['MIN_NOTIONAL']['notional'])
        price = Decimal(self._read('ticker')['price'])
        fee = Decimal(self._read('commission')['takerCommissionRate'])
        req_qty = max(min_qty, (min_notional / price / step_size).to_integral_value(rounding=ROUND_CEILING) * step_size)
        eff_lev = min(int(planned_leverage), actual_leverage, 5)
        est_min_notional = req_qty * price
        est_min_margin = (est_min_notional / eff_lev) + est_min_notional * fee * 2 + Decimal('0.10')

        blockers = []
        if not can_trade:
            blockers.append('账户未开通合约交易权限（canTrade=false）')
        if margin_type != 'ISOLATED':
            blockers.append(f'BTCUSDT 当前为 {margin_type}，须在交易所切换为逐仓（ISOLATED）；系统不会自动修改')
        if actual_leverage > 5:
            blockers.append(f'BTCUSDT 当前实际杠杆为 {actual_leverage}×，超过系统允许上限 5×，请在 Binance 调整至 ≤5×（系统不会自动修改）')
        elif actual_leverage > int(planned_leverage):
            blockers.append(f'BTCUSDT 当前实际杠杆 {actual_leverage}× 高于计划杠杆 {planned_leverage}×，请在 Binance 调整')
        if btc_positions or other_positions or orders or algos:
            blockers.append(f'账户存在未结敞口（BTC仓位 {len(btc_positions)} / 其他仓位 {len(other_positions)} / 普通单 {len(orders)} / 条件单 {len(algos)}）')
        if fixed_margin is not None and Decimal(fixed_margin) < est_min_margin:
            blockers.append(f'单笔保证金 {Decimal(fixed_margin):.2f} USDT 低于按当前规格估算的最低需求 {est_min_margin:.2f} USDT')
        if budget is not None and Decimal(budget) > available:
            blockers.append(f'总资金上限 {Decimal(budget):.2f} USDT 超过当前可用余额 {available:.2f} USDT')

        return {
            'environment': self.environment,
            'status': 'PASS' if not blockers else 'BLOCKED',
            'blockers': blockers,
            'account_alias': alias,
            'account_fingerprint': sha256(alias.encode('utf-8')).hexdigest()[:8],
            'can_trade': can_trade,
            'position_mode': 'HEDGE' if dual else 'ONE_WAY',
            'margin_type': margin_type,
            'actual_leverage': actual_leverage,
            'planned_leverage': int(planned_leverage),
            'available_usdt': str(available),
            'wallet_usdt': str(wallet),
            'price': str(price),
            'min_qty': str(min_qty),
            'step_size': str(step_size),
            'tick_size': str(tick_size),
            'min_notional': str(min_notional),
            'taker_fee': str(fee),
            'est_min_order_qty': str(req_qty),
            'est_min_margin_usdt': str(est_min_margin.quantize(Decimal('0.01'))),
            'btc_positions': len(btc_positions),
            'other_positions': len(other_positions),
            'open_orders': len(orders),
            'open_algos': len(algos),
        }


class DemoExchange(BinanceFuturesExchange):
    def __init__(self, key, secret, *, opener=None, clock=time.time, hedge=False):
        super().__init__(key, secret, opener=opener, clock=clock, hedge=hedge,
                         base_url=DEMO_URL, environment='DEMO', allow_writes=True)


class ProductionExchange(BinanceFuturesExchange):
    def __init__(self, key, secret, *, opener=None, clock=time.time, hedge=False, allow_writes=False):
        super().__init__(key, secret, opener=opener, clock=clock, hedge=hedge,
                         base_url=PRODUCTION_URL, environment='PRODUCTION', allow_writes=allow_writes)
