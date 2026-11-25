"""Desktop-only Binance account reader and pending allocation configuration."""
import ctypes
import hashlib
import hmac
import json
import os
import ssl
import tempfile
import time
from ctypes import wintypes
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_CEILING
from pathlib import Path
from typing import Literal, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener


FUTURES = 'https://fapi.binance.com'
WALLET = 'https://api.binance.com'
READS = {
    'time': (FUTURES, '/fapi/v1/time', {}),
    'exchange_info': (FUTURES, '/fapi/v1/exchangeInfo', {}),
    'ticker': (FUTURES, '/fapi/v2/ticker/price', {'symbol': 'BTCUSDT'}),
    'klines': (FUTURES, '/fapi/v1/klines', {'symbol': 'BTCUSDT', 'interval': '15m', 'limit': 289}),
    'commission': (FUTURES, '/fapi/v1/commissionRate', {'symbol': 'BTCUSDT'}),
    'brackets': (FUTURES, '/fapi/v1/leverageBracket', {'symbol': 'BTCUSDT'}),
    'spot_account': (WALLET, '/api/v3/account', {'omitZeroBalances': 'true'}),
    'balances': (FUTURES, '/fapi/v3/balance', {}),
    'account': (FUTURES, '/fapi/v3/account', {}),
    'positions': (FUTURES, '/fapi/v3/positionRisk', {}),
    'orders': (FUTURES, '/fapi/v1/openOrders', {}),
    'conditional': (FUTURES, '/fapi/v1/openAlgoOrders', {}),
    'mode': (FUTURES, '/fapi/v1/positionSide/dual', {}),
    'config': (FUTURES, '/fapi/v1/accountConfig', {}),
    'symbol': (FUTURES, '/fapi/v1/symbolConfig', {'symbol': 'BTCUSDT'}),
    'permissions': (WALLET, '/sapi/v1/account/apiRestrictions', {}),
}
PUBLIC_READS = frozenset(('time', 'exchange_info', 'ticker', 'klines'))


@dataclass(frozen=True)
class LiveAccount:
    account_id: str
    name: str
    api_key: str = field(repr=False)
    api_secret: str = field(repr=False)
    exchange_alias: str
    account_type: Literal['main', 'subaccount'] = 'subaccount'


@dataclass(frozen=True)
class CapitalBudget:
    usdt: Decimal
    planned_leverage: int


@dataclass(frozen=True)
class StrategyBinding:
    account_id: str
    strategy_id: str
    strategy_version: str
    budget: CapitalBudget


@dataclass(frozen=True)
class StrategySignal:
    strategy_id: str
    strategy_version: str
    signal_id: str
    cycle_at: datetime
    action: Literal['BUY', 'SELL', 'EXIT', 'WAIT']
    direction: Literal['LONG', 'SHORT'] | None
    stop_basis: str | None
    target_basis: str | None
    signal_at: datetime
    market_source: str
    expires_at: datetime
    invalidation: str


class SignalAdapter(Protocol):
    def evaluate(self, *, closed_bars: tuple, cycle_at: datetime) -> StrategySignal: ...


@dataclass(frozen=True)
class LiveOrder:
    account_id: str
    strategy_id: str
    strategy_version: str
    exchange_order_id: str


@dataclass(frozen=True)
class LivePosition:
    account_id: str
    strategy_id: str
    strategy_version: str
    symbol: str
    direction: Literal['LONG', 'SHORT']
    quantity: Decimal


class AccountReadError(RuntimeError):
    pass


class RejectRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class BinanceReader:
    def __init__(self, api_key, api_secret, *, opener=None, clock=time.time):
        self._api_key = api_key
        self._secret = api_secret.encode('ascii')
        self._clock = clock
        self._offset = 0
        self._opener = opener or build_opener(HTTPSHandler(context=ssl.create_default_context()), RejectRedirects())

    def _read(self, operation):
        if operation not in READS:
            raise AccountReadError('只允许预定义的账户读取请求')
        host, path, fixed = READS[operation]
        query = urlencode(fixed)
        headers = {}
        if operation not in PUBLIC_READS:
            query = urlencode({**fixed, 'recvWindow': 5000, 'timestamp': int(self._clock() * 1000) + self._offset})
            signed = hmac.new(self._secret, query.encode('ascii'), hashlib.sha256).hexdigest()
            query += '&signature=' + signed
            headers['X-MBX-APIKEY'] = self._api_key
        try:
            request = Request(host + path + ('?' + query if query else ''), headers=headers, method='GET')
            with self._opener.open(request, timeout=8) as response:
                result = json.load(response)
        except HTTPError as exc:
            try:
                code = json.loads(exc.read(8192)).get('code')
            except (ValueError, AttributeError, OSError):
                code = None
            explanation = {
                -2014: 'API Key 格式错误', -2015: 'API Key、IP 白名单或读取权限不匹配',
                -1022: '签名验证失败，请核对密钥', -1021: '请求时间偏差，请核对系统时钟',
            }.get(code if isinstance(code, int) else None, '交易所拒绝读取')
            raise AccountReadError(f'{operation}：{explanation}（HTTP {exc.code}）') from None
        except (URLError, OSError):
            raise AccountReadError(f'{operation}：无法连接 Binance 或请求超时') from None
        except (ValueError, TypeError):
            raise AccountReadError(f'{operation}：响应格式无效') from None
        return result

    def snapshot(self):
        start = self._clock()
        try:
            self._offset = int(self._read('time')['serverTime']) - int((start + self._clock()) * 500)
            balance = next(row for row in self._read('balances') if row['asset'] == 'USDT')
            account = self._read('account')
            asset = next(row for row in account['assets'] if row['asset'] == 'USDT')
            available = Decimal(balance['availableBalance'])
            equity = Decimal(asset['marginBalance'])
            wallet = Decimal(asset['walletBalance'])
            alias = balance['accountAlias']
            if not all(value.is_finite() for value in (available, equity, wallet)) or not isinstance(alias, str) or not alias:
                raise ValueError
            positions = [row for row in self._read('positions')
                         if Decimal(row['positionAmt']) != 0]
            orders = self._read('orders')
            conditional = self._read('conditional')
            mode = self._read('mode')['dualSidePosition']
            config = self._read('config')
            symbol = self._read('symbol')
            permissions = self._read('permissions')
            if (not isinstance(mode, bool) or not isinstance(config, dict) or not isinstance(permissions, dict)
                    or any(not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows)
                           for rows in (orders, conditional, symbol)) or permissions.get('enableReading') is not True):
                raise ValueError
        except (KeyError, ValueError, TypeError, StopIteration, InvalidOperation):
            raise AccountReadError('账户数据或读取权限无法确认') from None
        other = sum(row.get('symbol') != 'BTCUSDT' for row in positions + orders + conditional)
        positions, orders, conditional = ([row for row in rows if row.get('symbol') == 'BTCUSDT']
                                           for rows in (positions, orders, conditional))
        return dict(source='Binance USDⓈ-M · 真实只读', available=available, equity=equity,
                    wallet=wallet, initial_margin=asset.get('initialMargin'),
                    position_margin=asset.get('positionInitialMargin'), order_margin=asset.get('openOrderInitialMargin'),
                    other_activity=other,
                    exchange_alias=alias, positions=positions, orders=orders, conditional=conditional,
                    hedge=mode, config=config, symbol=symbol, permissions=permissions)

    def spot_usdt(self):
        """Report spot evidence separately. It never contributes to the margin budget."""
        try:
            rows = self._read('spot_account')['balances']
            if not isinstance(rows, list):
                return None
            row = next((item for item in rows if item['asset'] == 'USDT'), None)
            value = Decimal(row['free']) + Decimal(row['locked']) if row else Decimal(0)
            return value if value.is_finite() and value >= 0 else None
        except (AccountReadError, KeyError, TypeError, ArithmeticError):
            return None

    def configuration_preflight(self, snapshot, **parameters):
        return self.order_preflight(snapshot, **parameters, configuration_only=True)

    def order_preflight(self, snapshot, *, strategy_id, version, budget, leverage, account_type,
                        configuration_only=False, closed_bars=None):
        """Read-only order sizing evidence. Unverified inputs cannot produce a pass."""
        from datetime import UTC, datetime
        from scripts.live_strategy_a import StrategyAAdapter

        result = {'state': 'UNVERIFIED', 'reason': '策略尚无经过验证的资金预检适配器。'}
        if strategy_id not in ('A', 'C'):
            return result
        try:
            if strategy_id == 'C':
                from scripts.live_strategy_c import StrategyCAdapter
                adapter = StrategyCAdapter()
            else:
                adapter = StrategyAAdapter()
            if version != adapter.version:
                return {**result, 'reason': f'绑定版本与 {strategy_id} 活动规则 {adapter.version} 不一致。'}
            result['version_verified'] = True
            budget = Decimal(budget)
            leverage = int(leverage)
            if not budget.is_finite() or budget <= 0 or not 1 <= leverage <= 5:
                return {**result, 'state': 'FAIL', 'reason': '预算必须大于零，计划杠杆须为 1–5 倍。'}
            info = next(row for row in self._read('exchange_info')['symbols'] if row['symbol'] == 'BTCUSDT')
            ticker = self._read('ticker')
            if ticker.get('symbol') != 'BTCUSDT' or not 0 <= self._clock() * 1000 - int(ticker['time']) <= 60_000:
                raise ValueError
            price = Decimal(ticker['price'])
            allowed = min(leverage, adapter.max_leverage, 5)
            result = {**result, **market_order_minimum(info, price, snapshot['available'], budget, allowed),
                      'price': price, 'available': snapshot['available'], 'budget': budget,
                      'allowed_leverage': allowed, 'risk_status': 'UNVERIFIED', 'fees_verified': False}
            if budget > snapshot['available']:
                detail = ' 现货 USDT 不计入合约可用资金。' if snapshot.get('spot_usdt') else ''
                return {**result, 'state': 'FAIL', 'reason': '输入预算超过合约可用余额。' + detail}
            if result['margin'] > min(budget, snapshot['available']):
                return {**result, 'state': 'FAIL', 'reason': f'{strategy_id} {adapter.version} 原始上限 {adapter.max_leverage}×，最低保证金已超过预算或可用余额。'}
            if account_type == 'main' and (snapshot['positions'] or snapshot['orders'] or snapshot['conditional'] or snapshot.get('other_activity')):
                return {**result, 'state': 'FAIL', 'reason': '主账户已有合约持仓或挂单，存在仓位或资金冲突。'}
            if snapshot['permissions'].get('enableFutures') is not True:
                return {**result, 'reason': '合约交易权限未核实或未开通。'}
            if any(context.get('marginType') != 'ISOLATED' for context in snapshot['symbol']) or not snapshot['symbol']:
                return {**result, 'reason': '未确认 BTCUSDT 使用逐仓。本次不会修改交易所配置。'}
            brackets_response = self._read('brackets')
            if isinstance(brackets_response, list):
                brackets_response = next(item for item in brackets_response if item['symbol'] == 'BTCUSDT')
            brackets = brackets_response['brackets']
            bracket = next(item for item in brackets
                           if Decimal(str(item['notionalFloor'])) <= result['notional'] < Decimal(str(item['notionalCap'])))
            actual = int(snapshot['symbol'][0]['leverage'])
            allowed = min(allowed, int(bracket['initialLeverage']), actual)
            if configuration_only and allowed < leverage:
                return {**result, 'state': 'FAIL', 'reason': f'计划杠杆 {leverage}× 超过当前允许的 {allowed}×。'}
            fee = Decimal(self._read('commission')['takerCommissionRate'])
            if not fee.is_finite() or fee < 0:
                raise ValueError
            result.update(market_order_minimum(info, price, snapshot['available'], budget, allowed, fee=fee))
            result['allowed_leverage'] = allowed
            result['fees_verified'] = True
            if not result['fits']:
                return {**result, 'state': 'FAIL', 'reason': '计入实际费率和资金预留后，预算或可用余额不足。'}
            if configuration_only:
                return {**result, 'state': 'PASS', 'reason': '账户、活动规则与最小订单资金可行性已核验。'}
            now = self._clock() * 1000
            closed = tuple({'close_at': datetime.fromtimestamp((row[6] + 1) / 1000, UTC).isoformat(),
                            'is_closed': True, **dict(zip(('open','high','low','close'), row[1:5]))}
                           for row in self._read('klines') if row[6] < now) if closed_bars is None else closed_bars
            if not closed:
                return {**result, 'reason': '没有足够的已收盘 K 线。'}
            cycle = datetime.fromisoformat(closed[-1]['close_at'])
            try:
                decision = adapter.evaluate(closed_bars=closed, cycle_at=cycle)
            except ValueError:
                return {**result, 'reason': '已收盘 15 分钟 K 线不足、断档或周期未对齐。'}
            if datetime.fromtimestamp(now / 1000, UTC) >= decision.expires_at:
                return {**result, 'reason': '信号过期，等待更新的已收盘周期。'}
            if decision.action == 'WAIT':
                return {**result, 'state': 'NO_SIGNAL', 'reason': '等待下一个有效信号。'}
            if not decision.stop_basis:
                return {**result, 'reason': '当前信号未提供可验证的原始止损。'}
            stop = Decimal(decision.stop_basis)
            if not stop.is_finite() or stop <= 0:
                return {**result, 'reason': '当前信号的原始止损无效。'}
            from decimal import ROUND_FLOOR
            tick = Decimal(result['tick_size'])
            if not tick.is_finite() or tick <= 0:
                raise ValueError('invalid tick size')
            stop = (stop / tick).to_integral_value(
                rounding=ROUND_FLOOR if decision.direction == 'LONG' else ROUND_CEILING) * tick
            distance = (price - stop) if decision.direction == 'LONG' else (stop - price)
            risk = distance * result['quantity'] + result['fees']
            result['minimum_order_risk'] = risk
            result['strategy_risk_limit'] = min(budget, snapshot['available']) * adapter.risk_fraction
            if distance <= 0:
                return {**result, 'state': 'FAIL', 'risk_status': 'FAIL',
                        'reason': '现价已越过原始止损，等待下一次有效信号。'}
            if risk > result['strategy_risk_limit']:
                return {**result, 'state': 'FAIL', 'risk_status': 'FAIL',
                        'reason': f"最小订单预计亏损 {risk:.4f} USDT，超过原始风险限额 {result['strategy_risk_limit']:.4f} USDT。"}
            return {**result, 'state': 'PASS', 'risk_status': 'PASS', 'reason': '当前估算通过，条件变化后须重新预检。'}
        except (KeyError, StopIteration, TypeError, ValueError, ArithmeticError, OSError):
            return {**result, 'reason': '输入、策略规则、市场报价或交易限制证据不完整。'}
        except AccountReadError as exc:
            return {**result, 'reason': str(exc)}


def market_order_minimum(info, price, available, budget, leverage, *, fee=Decimal('0.0005')):
    if not (info['status'] == 'TRADING' and price.is_finite() and price > 0
            and available.is_finite() and budget.is_finite() and budget > 0 and 1 <= leverage <= 5):
        raise ValueError('无法验证市场或预算')
    filters = {item['filterType']: item for item in info['filters']}
    lot = filters['MARKET_LOT_SIZE']
    step = Decimal(lot['stepSize']) or Decimal(filters['LOT_SIZE']['stepSize'])
    minimum = max(Decimal(lot['minQty']), Decimal(filters['LOT_SIZE']['minQty']),
                  Decimal(filters['MIN_NOTIONAL']['notional']) / price)
    quantity = (minimum / step).to_integral_value(rounding=ROUND_CEILING) * step
    notional = quantity * price
    # Planning estimates: two 5 bps fees and 0.10 USDT cash reserve.
    margin = notional / leverage
    fees = notional * 2 * fee
    reserve = max(Decimal('0.10'), notional * Decimal('0.001'))
    required = margin + fees + reserve
    return {'quantity': quantity, 'notional': notional, 'required': required,
            'market_min_notional': Decimal(filters['MIN_NOTIONAL']['notional']),
            'margin': margin, 'fees': fees, 'reserve': reserve,
            'tick_size': filters['PRICE_FILTER']['tickSize'],
            'fits': quantity <= Decimal(lot['maxQty']) and required <= min(budget, available)}


class _Blob(ctypes.Structure):
    _fields_ = [('size', wintypes.DWORD), ('data', ctypes.POINTER(ctypes.c_ubyte))]


def dpapi(payload: bytes, *, decrypt=False):
    if os.name != 'nt':
        raise OSError('Windows DPAPI 不可用')
    crypt = ctypes.WinDLL('crypt32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    fn = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    fn.argtypes = [ctypes.POINTER(_Blob), ctypes.c_void_p, ctypes.c_void_p,
                   ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_Blob)]
    fn.restype = wintypes.BOOL
    buffer = ctypes.create_string_buffer(payload)
    source = _Blob(len(payload), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    target = _Blob()
    if not fn(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(target.data, target.size)
    finally:
        kernel.LocalFree(target.data)


class Configuration:
    def __init__(self, path=None):
        self.path = Path(path) if path else Path(os.environ['LOCALAPPDATA']) / 'BTC Paper Lab V3' / 'live-v1a.dpapi'

    def load(self):
        if not self.path.exists():
            return {'accounts': {}, 'bindings': {}}
        return json.loads(dpapi(self.path.read_bytes(), decrypt=True))

    def save(self, data):
        encrypted = dpapi(json.dumps(data, ensure_ascii=False).encode('utf8'))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=self.path.parent, delete=False) as stream:
            temp = Path(stream.name)
            stream.write(encrypted)
        try:
            os.replace(temp, self.path)
        finally:
            temp.unlink(missing_ok=True)


def make_binding(account_id, strategy_id, strategy_version, amount, leverage, available):
    try:
        budget = Decimal(amount)
        leverage = int(leverage)
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError('请输入有效的 USDT 预算和整数杠杆') from None
    if not budget.is_finite() or budget <= 0 or budget > available:
        raise ValueError('预算必须大于零且不超过本次核验的可用 USDT')
    if not 1 <= leverage <= 5:
        raise ValueError('计划杠杆须为 1 至 5 倍')
    if not strategy_version.strip():
        raise ValueError('请填写计划绑定的策略版本')
    return StrategyBinding(account_id, strategy_id, strategy_version.strip(), CapitalBudget(budget, leverage))
