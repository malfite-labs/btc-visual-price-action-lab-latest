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
SYMBOL = 'BTCUSDT'


class DemoApiError(Exception):
    def __init__(self, operation, status, code):
        super().__init__(f'{operation}: HTTP {status}, Binance code {code}')
        self.code = code
        self.operation, self.status = operation, status
        self.method = None


class DemoExchange(BinanceReader):
    def __init__(self, key, secret, *, opener=None, clock=time.time, hedge=False):
        if not key or not secret:
            raise ValueError('Independent Demo credentials are required')
        self._key, self._secret = key, secret.encode()
        self._opener = opener or build_opener(RejectRedirects())
        self._clock = clock
        self._write_thread = None
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
        with account_owner('DEMO', aliases[0]):
            self._write_thread = get_ident()
            try:
                yield
            finally:
                self._write_thread = None

    def _request(self, method, path, parameters=None, *, signed=True, clock_retry=True):
        if method != 'GET' and (not signed or self._write_thread != get_ident()):
            raise RuntimeError('DEMO_WRITE_REQUIRES_ACCOUNT_OWNERSHIP')
        params = dict(parameters or {})
        headers = {}
        if signed:
            params.update(timestamp=int(self._clock() * 1000) + self._offset, recvWindow=5000)
            raw = urlencode(params)
            params['signature'] = hmac.new(self._secret, raw.encode(), sha256).hexdigest()
            headers['X-MBX-APIKEY'] = self._key
        url = DEMO_URL + path
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
            raise ValueError('Demo executor requires verified one-way mode')
        positions = self._request('GET', '/fapi/v3/positionRisk')
        other = [p for p in positions if p['symbol'] != SYMBOL and Decimal(p['positionAmt'])]
        rows = [p for p in positions if p['symbol'] == SYMBOL and Decimal(p['positionAmt'])]
        sides = ('LONG','SHORT') if self.hedge else ('BOTH',)
        if other or len(rows) > 1 or any(p['positionSide'] not in sides for p in rows):
            raise ValueError('Conflicting Demo positions')
        net = Decimal(rows[0]['positionAmt']) if rows else Decimal(0)
        if rows and self.hedge and (net > 0) != (rows[0]['positionSide']=='LONG'):
            raise ValueError('Demo position side disagrees with signed quantity')
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
            raise ValueError('Demo symbol configuration unverified')
        return {'available': Decimal(asset['availableBalance']),
                'positions': [net] if net else [], 'orders': orders, 'conditional': conditional,
                'permissions': {'enableFutures': self._request('GET','/fapi/v1/accountConfig').get('canTrade') is True}, 'symbol':symbol}
