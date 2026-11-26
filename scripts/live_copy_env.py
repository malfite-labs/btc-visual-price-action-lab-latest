"""Unified environment isolation paths and credential loader for Demo & Production copy trading."""
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from hashlib import sha256
import json
import os
from pathlib import Path
import time
from uuid import uuid4

from scripts.live_center import Configuration, dpapi

DEMO_ENV = 'BINANCE_DEMO'
PROD_ENV = 'BINANCE_PRODUCTION'
VALID_ENVS = (DEMO_ENV, PROD_ENV)
MAX_COPY_LEVERAGE = 10


@dataclass(frozen=True)
class CopyEnvPaths:
    environment: str
    root: Path
    configs: Path
    runtime: Path
    ledgers: Path
    switch_queue: Path
    env_title: str
    mode_code: str
    default_account_label: str


def env_paths(environment=DEMO_ENV, *, base_root=None) -> CopyEnvPaths:
    if environment not in VALID_ENVS:
        raise ValueError(f'UNSUPPORTED_COPY_ENVIRONMENT:{environment}')
    if environment == DEMO_ENV:
        root = Path(base_root) if base_root is not None else Path(os.environ['LOCALAPPDATA']) / 'BTC Paper Lab V3 Demo Acceptance'
        runtime = root / 'dry-v2-service'
        return CopyEnvPaths(
            environment=DEMO_ENV,
            root=root,
            configs=root / 'copy-configs.json',
            runtime=runtime,
            ledgers=root / 'continuous-copy',
            switch_queue=runtime / 'pending-demo-switch.json',
            env_title='Binance Demo',
            mode_code='DEMO',
            default_account_label='Binance USDⓈ-M Demo',
        )
    root = Path(base_root) if base_root is not None else Path(os.environ['LOCALAPPDATA']) / 'BTC Paper Lab V3' / 'production-copy'
    runtime = root / 'runtime'
    return CopyEnvPaths(
        environment=PROD_ENV,
        root=root,
        configs=root / 'copy-configs.json',
        runtime=runtime,
        ledgers=root / 'ledgers',
        switch_queue=runtime / 'pending-switch.json',
        env_title='Binance 正式实盘',
        mode_code='PRODUCTION',
        default_account_label='Binance USDⓈ-M 正式账户',
    )


def list_production_accounts(config_path=None):
    """Return metadata of bound Binance production accounts (never exposing secrets)."""
    data = Configuration(config_path).load()
    accounts = []
    for aid, row in (data.get('accounts') or {}).items():
        if not isinstance(row, dict) or not row.get('api_key') or not row.get('api_secret'):
            continue
        accounts.append({
            'account_id': aid,
            'name': row.get('name') or '正式账户',
            'account_type': row.get('account_type') or 'main',
            'exchange_alias': row.get('exchange_alias') or '',
        })
    return accounts


def load_credentials(environment=DEMO_ENV, *, account_id=None, base_root=None, prod_config_path=None):
    """Load DPAPI-protected API credentials strictly scoped to one environment."""
    if environment == DEMO_ENV:
        paths = env_paths(DEMO_ENV, base_root=base_root)
        try:
            key, secret = json.loads(dpapi((paths.root / 'credentials.dpapi').read_bytes(), decrypt=True))
        except (OSError, ValueError, TypeError) as exc:
            raise ValueError('本机 Demo 凭据不可用') from exc
        if not key or not secret:
            raise ValueError('本机 Demo 凭据不完整')
        return key, secret, paths.default_account_label, 'DEMO'
    if environment == PROD_ENV:
        data = Configuration(prod_config_path).load()
        accounts = data.get('accounts') or {}
        if not accounts:
            raise ValueError('尚未绑定 Binance 正式账户凭据')
        chosen_id = account_id if account_id in accounts else next(iter(accounts))
        row = accounts[chosen_id]
        key, secret = row.get('api_key'), row.get('api_secret')
        if not key or not secret:
            raise ValueError('正式账户凭据不完整')
        name = row.get('name') or '正式账户'
        label = f"Binance USDⓈ-M · {name}"
        return key, secret, label, chosen_id
    raise ValueError(f'UNSUPPORTED_COPY_ENVIRONMENT:{environment}')


def make_exchange(environment=DEMO_ENV, *, account_id=None, allow_writes=False, allow_leverage_sync=False,
                  base_root=None, prod_config_path=None):
    from scripts.live_c_demo_exchange import DemoExchange, ProductionExchange
    key, secret, label, resolved_id = load_credentials(
        environment, account_id=account_id, base_root=base_root, prod_config_path=prod_config_path
    )
    if environment == DEMO_ENV:
        return DemoExchange(key, secret, allow_writes=bool(allow_writes), allow_leverage_sync=bool(allow_leverage_sync)), label, resolved_id
    return ProductionExchange(key, secret, allow_writes=bool(allow_writes), allow_leverage_sync=bool(allow_leverage_sync)), label, resolved_id


def config_ledger_dir(environment, cid, config, all_configs=None, *, base_root=None) -> Path:
    paths = env_paths(environment, base_root=base_root)
    if environment == DEMO_ENV and config.get('legacy_demo_ledger'):
        if all_configs is not None and sum(bool(c.get('legacy_demo_ledger')) for c in all_configs.values()) != 1:
            raise ValueError('多个配置关联同一旧账本，归属待核对。')
        return paths.ledgers
    return paths.ledgers / cid


def alias_fingerprint(alias: str) -> str:
    return sha256(alias.encode('utf-8')).hexdigest()[:8]


def mask_api_key(api_key: str) -> str:
    """Partially mask an API key so the full secret/key is never exposed in UI or logs."""
    raw = str(api_key or '').strip()
    if not raw:
        return ''
    if len(raw) <= 8:
        return raw[:2] + '••••' + raw[-2:]
    return raw[:6] + '••••••••' + raw[-4:]


def _demo_meta_path(base_root=None) -> Path:
    return env_paths(DEMO_ENV, base_root=base_root).root / 'demo-api-meta.json'


def get_demo_remark(base_root=None) -> str:
    try:
        meta = json.loads(_demo_meta_path(base_root).read_text(encoding='utf8'))
        name = str(meta.get('name') or '').strip()
        if name:
            return name
    except (OSError, ValueError, TypeError):
        pass
    return 'Binance Demo'


def get_api_credential_summary(environment=DEMO_ENV, *, account_id=None, base_root=None, prod_config_path=None) -> dict:
    """Return non-sensitive metadata and masked API key for UI display. Never returns plaintext secrets."""
    if environment == DEMO_ENV:
        paths = env_paths(DEMO_ENV, base_root=base_root)
        name = get_demo_remark(base_root=base_root)
        try:
            key, secret = json.loads(dpapi((paths.root / 'credentials.dpapi').read_bytes(), decrypt=True))
            configured = bool(key and secret)
            return {
                'environment': DEMO_ENV,
                'configured': configured,
                'masked_key': mask_api_key(key) if configured else '',
                'secret_saved': configured,
                'name': name,
                'account_id': 'DEMO',
                'exchange_alias': '',
                'accounts': [{'account_id': 'DEMO', 'name': name, 'masked_key': mask_api_key(key) if configured else ''}],
            }
        except (OSError, ValueError, TypeError):
            return {
                'environment': DEMO_ENV,
                'configured': False,
                'masked_key': '',
                'secret_saved': False,
                'name': name,
                'account_id': 'DEMO',
                'exchange_alias': '',
                'accounts': [],
            }
    if environment == PROD_ENV:
        data = Configuration(prod_config_path).load()
        accounts_map = data.get('accounts') or {}
        items = []
        for aid, row in accounts_map.items():
            if not isinstance(row, dict) or not row.get('api_key') or not row.get('api_secret'):
                continue
            items.append({
                'account_id': aid,
                'name': str(row.get('name') or 'Binance正式主账户'),
                'account_type': str(row.get('account_type') or 'main'),
                'exchange_alias': str(row.get('exchange_alias') or ''),
                'masked_key': mask_api_key(row['api_key']),
            })
        chosen = None
        if account_id:
            chosen = next((x for x in items if x['account_id'] == account_id), None)
        if chosen is None and items:
            chosen = items[0]
        if chosen is None:
            return {
                'environment': PROD_ENV,
                'configured': False,
                'masked_key': '',
                'secret_saved': False,
                'name': 'Binance正式主账户',
                'account_id': '',
                'exchange_alias': '',
                'accounts': [],
            }
        return {
            'environment': PROD_ENV,
            'configured': True,
            'masked_key': chosen['masked_key'],
            'secret_saved': True,
            'name': chosen['name'],
            'account_id': chosen['account_id'],
            'exchange_alias': chosen['exchange_alias'],
            'accounts': items,
        }
    raise ValueError(f'UNSUPPORTED_COPY_ENVIRONMENT:{environment}')


def save_api_credentials(
    environment=DEMO_ENV,
    *,
    api_key=None,
    api_secret=None,
    name='',
    account_id=None,
    exchange_alias='',
    base_root=None,
    prod_config_path=None,
    is_in_use=False,
) -> dict:
    """Save Binance API credentials via Windows DPAPI, preserving existing accounts & runtime protection."""
    if environment not in VALID_ENVS:
        raise ValueError(f'UNSUPPORTED_COPY_ENVIRONMENT:{environment}')
    if is_in_use:
        raise ValueError('当前API正在被跟单服务使用，请完成安全停止及仓位对账后再修改。')

    clean_key = str(api_key or '').strip()
    clean_secret = str(api_secret or '').strip()
    remark = str(name or '').strip()

    if bool(clean_key) != bool(clean_secret):
        raise ValueError('新增或替换密钥时，请同时填写完整的 API Key 和 Secret Key。')

    if environment == DEMO_ENV:
        paths = env_paths(DEMO_ENV, base_root=base_root)
        paths.root.mkdir(parents=True, exist_ok=True)
        cred_path = paths.root / 'credentials.dpapi'
        if not clean_key and not clean_secret:
            if not cred_path.exists():
                raise ValueError('请填写 Demo 虚拟盘 API Key 与 Secret Key。')
            # Only updating remark name; verify existing DPAPI blob is still valid
            old_key, old_secret = json.loads(dpapi(cred_path.read_bytes(), decrypt=True))
            if not old_key or not old_secret:
                raise ValueError('现有 Demo 凭据不完整，请重新输入 API Key 与 Secret Key。')
            clean_key = old_key
        else:
            encrypted = dpapi(json.dumps([clean_key, clean_secret]).encode('utf8'))
            tmp = cred_path.with_suffix('.dpapi.tmp')
            tmp.write_bytes(encrypted)
            tmp.replace(cred_path)
        meta_path = _demo_meta_path(base_root=base_root)
        meta_tmp = meta_path.with_suffix('.json.tmp')
        final_name = remark or 'Binance Demo'
        meta_tmp.write_text(json.dumps({'name': final_name, 'updated_at': datetime.now().isoformat()}, ensure_ascii=False, indent=2), encoding='utf8')
        meta_tmp.replace(meta_path)
        return {
            'environment': DEMO_ENV,
            'account_id': 'DEMO',
            'name': final_name,
            'masked_key': mask_api_key(clean_key),
        }

    cfg = Configuration(prod_config_path)
    data = cfg.load()
    accounts = data.setdefault('accounts', {})
    target_id = account_id if (account_id and account_id in accounts) else (account_id or uuid4().hex)
    existing = accounts.get(target_id) if isinstance(accounts.get(target_id), dict) else {}
    if not clean_key and not clean_secret:
        if not existing.get('api_key') or not existing.get('api_secret'):
            raise ValueError('请填写正式实盘 API Key 与 Secret Key。')
        clean_key = existing['api_key']
        clean_secret = existing['api_secret']
    final_name = remark or str(existing.get('name') or 'Binance正式主账户')
    accounts[target_id] = {
        **existing,
        'name': final_name,
        'api_key': clean_key,
        'api_secret': clean_secret,
        'account_type': str(existing.get('account_type') or 'main'),
        'exchange_alias': str(exchange_alias or existing.get('exchange_alias') or ''),
    }
    cfg.save(data)
    return {
        'environment': PROD_ENV,
        'account_id': target_id,
        'name': final_name,
        'masked_key': mask_api_key(clean_key),
    }


def test_api_connection(
    environment=DEMO_ENV,
    *,
    api_key=None,
    api_secret=None,
    account_id=None,
    base_root=None,
    prod_config_path=None,
    opener=None,
) -> dict:
    """Strictly read-only live connection check against Binance USD-M Futures (Demo or Production)."""
    from scripts.live_c_demo_exchange import DemoApiError, DemoExchange, ProductionExchange

    checked_at = datetime.now().strftime('%H:%M:%S')
    clean_key = str(api_key or '').strip()
    clean_secret = str(api_secret or '').strip()

    if bool(clean_key) != bool(clean_secret):
        return {
            'status': 'API认证失败',
            'ok': False,
            'latency_ms': None,
            'auth_status': '输入不完整',
            'futures_perm': '未核实',
            'available_usdt': None,
            'position_mode': '—',
            'actual_leverage': None,
            'margin_type': '—',
            'account_alias': '',
            'checked_at': checked_at,
            'detail': '测试新密钥时请同时填写 API Key 和 Secret Key。',
        }

    if not clean_key:
        try:
            clean_key, clean_secret, _, _ = load_credentials(
                environment, account_id=account_id, base_root=base_root, prod_config_path=prod_config_path
            )
        except ValueError:
            return {
                'status': '未配置',
                'ok': False,
                'latency_ms': None,
                'auth_status': '未配置密钥',
                'futures_perm': '未核实',
                'available_usdt': None,
                'position_mode': '—',
                'actual_leverage': None,
                'margin_type': '—',
                'account_alias': '',
                'checked_at': checked_at,
                'detail': '尚未配置该环境的 Binance API 密钥。',
            }

    exchange = (
        DemoExchange(clean_key, clean_secret, opener=opener, allow_writes=False, allow_leverage_sync=False)
        if environment == DEMO_ENV
        else ProductionExchange(clean_key, clean_secret, opener=opener, allow_writes=False, allow_leverage_sync=False)
    )

    t0 = time.perf_counter()
    try:
        exchange.synchronize()
        latency_ms = max(1, int((time.perf_counter() - t0) * 1000))
        balances = exchange._request('GET', '/fapi/v3/balance')
        usdt_bal = next((r for r in balances if r.get('asset') == 'USDT'), None)
        alias = str((usdt_bal or {}).get('accountAlias') or '')
        account = exchange.account()
        asset = next((r for r in account.get('assets', []) if r.get('asset') == 'USDT'), None)
        available = str(Decimal(asset['availableBalance']).quantize(Decimal('0.01'))) if asset else None
        can_trade = exchange._request('GET', '/fapi/v1/accountConfig').get('canTrade') is True
        dual = exchange._request('GET', '/fapi/v1/positionSide/dual').get('dualSidePosition')
        pos_mode = '双向持仓 (HEDGE)' if dual is True else ('单向持仓 (ONE_WAY)' if dual is False else '未知')
        sym_cfg = exchange._request('GET', '/fapi/v1/symbolConfig', {'symbol': 'BTCUSDT'})
        actual_lev = int(sym_cfg[0]['leverage']) if sym_cfg else None
        margin_type = str(sym_cfg[0].get('marginType') or '—') if sym_cfg else '—'
        if not can_trade:
            return {
                'status': '权限不足',
                'ok': False,
                'latency_ms': latency_ms,
                'auth_status': '已认证（只读）',
                'futures_perm': '未开通合约交易（canTrade=false）',
                'available_usdt': available,
                'position_mode': pos_mode,
                'actual_leverage': actual_lev,
                'margin_type': margin_type,
                'account_alias': alias,
                'checked_at': checked_at,
                'detail': 'API 签名有效，但账户未开通 USDⓈ-M 合约交易权限。',
            }
        return {
            'status': '连接正常',
            'ok': True,
            'latency_ms': latency_ms,
            'auth_status': '认证通过',
            'futures_perm': '交易、查询（仅只读核实，不代表真实下单验收）',
            'available_usdt': available,
            'position_mode': pos_mode,
            'actual_leverage': actual_lev,
            'margin_type': margin_type,
            'account_alias': alias,
            'checked_at': checked_at,
            'detail': f'已连接 {exchange.environment} USDⓈ-M 合约账户。',
        }
    except DemoApiError as exc:
        if exc.status == 0:
            st, auth, detail = '网络连接失败', '连接超时或网络不可达', f'无法连接 Binance 服务器（{exc.operation}）。'
        elif exc.code in (-2014, -1022) or exc.status in (401,):
            st, auth, detail = 'API认证失败', '签名或密钥无效', f'API Key 或 Secret Key 校验失败（代码 {exc.code}）。'
        elif exc.code == -2015 or exc.status == 403:
            st, auth, detail = '权限不足', '密钥权限或 IP 白名单受限', f'API Key 权限不足、环境不匹配或 IP 未在白名单（代码 {exc.code}）。'
        else:
            st, auth, detail = 'API认证失败', f'交易所返回错误 {exc.code}', f'请求被拒绝（HTTP {exc.status}，代码 {exc.code}）。'
        return {
            'status': st,
            'ok': False,
            'latency_ms': None,
            'auth_status': auth,
            'futures_perm': '未核实',
            'available_usdt': None,
            'position_mode': '—',
            'actual_leverage': None,
            'margin_type': '—',
            'account_alias': '',
            'checked_at': checked_at,
            'detail': detail,
        }
    except Exception as exc:
        return {
            'status': '网络连接失败',
            'ok': False,
            'latency_ms': None,
            'auth_status': '检测异常',
            'futures_perm': '未核实',
            'available_usdt': None,
            'position_mode': '—',
            'actual_leverage': None,
            'margin_type': '—',
            'account_alias': '',
            'checked_at': checked_at,
            'detail': str(exc)[:120],
        }


def copy_config_payload(source_config, *, target_environment, new_cid, created_at, account_label, account_id=None):
    """Copy only strategy & sizing parameters to another environment; never copy runtime/auth/ledger."""
    return {
        'strategy': source_config['strategy'],
        'mode': '定额跟单',
        'fixed': str(source_config.get('fixed', '20')),
        'budget': str(source_config.get('budget', '100')),
        'leverage': str(source_config.get('leverage', '5')),
        'auto_sync_leverage': str(source_config.get('auto_sync_leverage', '1')),
        'stop': str(source_config.get('stop', '')),
        'tp': str(source_config.get('tp', '')),
        'emergency': '',
        'maximum': '',
        'slippage': '',
        'follow_tp': '1',
        'follow_sl': '1',
        'custom_tp': str(source_config.get('custom_tp', '0')),
        'custom_sl': str(source_config.get('custom_sl', '0')),
        'flatten_on_stop': '0',
        'account': account_label,
        'account_id': account_id,
        'environment': target_environment,
        'execution_mode': 'PRODUCTION' if target_environment == PROD_ENV else 'DEMO',
        'copy_mode': 'FIXED',
        'production_order_enabled': False,
        'copy_config_id': new_cid,
        'created_at': created_at,
        'archived_at': None,
    }
