"""Isolated tests for Binance API DPAPI storage, masking, read-only connection probe, and running-service protection."""
import io
import json
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlparse

import pytest

from scripts.live_copy_env import (
    DEMO_ENV,
    PROD_ENV,
    get_api_credential_summary,
    list_production_accounts,
    load_credentials,
    mask_api_key,
    save_api_credentials,
    test_api_connection as probe_api_connection,
)


class _Resp(io.BytesIO):
    def __init__(self, payload):
        super().__init__(json.dumps(payload).encode('utf-8'))

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class _FakeOpener:
    def __init__(self, *, can_trade=True, error_code=None, http_status=401, network_fail=False, leverage=5):
        self.can_trade = can_trade
        self.error_code = error_code
        self.http_status = http_status
        self.network_fail = network_fail
        self.leverage = leverage
        self.methods = []

    def open(self, req, timeout=8):
        parsed = urlparse(req.full_url)
        self.methods.append((req.get_method(), parsed.path))
        if self.network_fail:
            raise OSError('connection timed out')
        if parsed.path == '/fapi/v1/time':
            return _Resp({'serverTime': 1700000000000})
        if self.error_code is not None:
            body = io.BytesIO(json.dumps({'code': self.error_code, 'msg': 'rejected'}).encode('utf-8'))
            raise HTTPError(req.full_url, self.http_status, 'ERR', {}, body)
        if parsed.path == '/fapi/v3/balance':
            return _Resp([{'asset': 'USDT', 'accountAlias': 'MOCKALIAS', 'availableBalance': '1234.56'}])
        if parsed.path == '/fapi/v3/account':
            return _Resp({'assets': [{'asset': 'USDT', 'availableBalance': '1234.56'}]})
        if parsed.path == '/fapi/v1/accountConfig':
            return _Resp({'canTrade': self.can_trade})
        if parsed.path == '/fapi/v1/positionSide/dual':
            return _Resp({'dualSidePosition': True})
        if parsed.path == '/fapi/v1/symbolConfig':
            return _Resp([{'symbol': 'BTCUSDT', 'marginType': 'ISOLATED', 'leverage': self.leverage}])
        raise AssertionError(f'Unexpected {req.get_method()} {parsed.path}')


def test_mask_api_key_hides_middle_characters():
    masked = mask_api_key('Dk4A3s7H9nVYQ2pMZ8fL6tWkX0eR1uPz')
    assert masked.startswith('Dk4A3s')
    assert masked.endswith('1uPz')
    assert '••••••••' in masked
    assert '7H9nVYQ2pMZ8fL6tWkX0eR' not in masked


def test_demo_and_production_dpapi_isolated_save_and_multi_account_preservation(tmp_path):
    demo_root = tmp_path / 'demo'
    prod_dpapi = tmp_path / 'live-v1a.dpapi'

    # 1. Save Demo via DPAPI
    res_demo = save_api_credentials(
        DEMO_ENV, api_key='DEMO_KEY_1234567890', api_secret='DEMO_SECRET_ABCDEF',
        name='我的Demo账户', base_root=demo_root,
    )
    assert res_demo['name'] == '我的Demo账户'
    # DPAPI blob must NOT contain plaintext secret
    raw_blob = (demo_root / 'credentials.dpapi').read_bytes()
    assert b'DEMO_SECRET_ABCDEF' not in raw_blob
    assert b'DEMO_KEY_1234567890' not in raw_blob

    # Summary returns masked key only, never plaintext secret
    sum_demo = get_api_credential_summary(DEMO_ENV, base_root=demo_root)
    assert sum_demo['configured'] is True
    assert sum_demo['secret_saved'] is True
    assert 'DEMO_SECRET_ABCDEF' not in json.dumps(sum_demo)
    assert 'DEMO_KEY_1234567890' not in json.dumps(sum_demo)

    # 2. Save Production primary + secondary account without overwriting each other
    res_p1 = save_api_credentials(
        PROD_ENV, api_key='PROD_KEY_ONE_99999', api_secret='PROD_SEC_ONE_ZZZZ',
        name='正式主账户', prod_config_path=prod_dpapi,
    )
    res_p2 = save_api_credentials(
        PROD_ENV, api_key='PROD_KEY_TWO_88888', api_secret='PROD_SEC_TWO_YYYY',
        name='正式子账户', prod_config_path=prod_dpapi,
    )
    assert res_p1['account_id'] != res_p2['account_id']
    assert b'PROD_SEC_ONE_ZZZZ' not in prod_dpapi.read_bytes()
    accs = list_production_accounts(config_path=prod_dpapi)
    assert {a['name'] for a in accs} == {'正式主账户', '正式子账户'}

    # Remark-only update preserves existing encrypted credentials
    save_api_credentials(
        PROD_ENV, name='正式主账户-重命名', account_id=res_p1['account_id'], prod_config_path=prod_dpapi,
    )
    k1, s1, _, _ = load_credentials(PROD_ENV, account_id=res_p1['account_id'], prod_config_path=prod_dpapi)
    assert k1 == 'PROD_KEY_ONE_99999' and s1 == 'PROD_SEC_ONE_ZZZZ'


def test_running_account_protection_blocks_overwrite(tmp_path):
    demo_root = tmp_path / 'demo'
    with pytest.raises(ValueError, match='当前API正在被跟单服务使用'):
        save_api_credentials(
            DEMO_ENV, api_key='NEW_KEY_11111111', api_secret='NEW_SEC_11111111',
            name='Demo', base_root=demo_root, is_in_use=True,
        )


def test_connection_probe_strictly_readonly_and_classifies_statuses(tmp_path):
    # Healthy -> 连接正常 (GET only!)
    op_ok = _FakeOpener(can_trade=True, leverage=5)
    r_ok = probe_api_connection(PROD_ENV, api_key='K1234567890', api_secret='S1234567890', opener=op_ok)
    assert r_ok['status'] == '连接正常'
    assert r_ok['ok'] is True
    assert r_ok['available_usdt'] == '1234.56'
    assert r_ok['actual_leverage'] == 5
    assert all(m == 'GET' for m, _ in op_ok.methods)

    # canTrade=False -> 权限不足
    r_perm = probe_api_connection(PROD_ENV, api_key='K1234567890', api_secret='S1234567890', opener=_FakeOpener(can_trade=False))
    assert r_perm['status'] == '权限不足' and r_perm['ok'] is False

    # Bad signature (-1022) -> API认证失败
    r_auth = probe_api_connection(DEMO_ENV, api_key='BADKEY', api_secret='BADSEC', opener=_FakeOpener(error_code=-1022, http_status=401))
    assert r_auth['status'] == 'API认证失败' and r_auth['ok'] is False

    # Network timeout -> 网络连接失败
    r_net = probe_api_connection(DEMO_ENV, api_key='K1234567890', api_secret='S1234567890', opener=_FakeOpener(network_fail=True))
    assert r_net['status'] == '网络连接失败' and r_net['ok'] is False

    # Unconfigured -> 未配置
    r_none = probe_api_connection(DEMO_ENV, base_root=tmp_path / 'empty_demo')
    assert r_none['status'] == '未配置' and r_none['ok'] is False
