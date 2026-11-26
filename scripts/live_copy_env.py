"""Unified environment isolation paths and credential loader for Demo & Production copy trading."""
from dataclasses import dataclass
from hashlib import sha256
import json
import os
from pathlib import Path

from scripts.live_center import Configuration, dpapi

DEMO_ENV = 'BINANCE_DEMO'
PROD_ENV = 'BINANCE_PRODUCTION'
VALID_ENVS = (DEMO_ENV, PROD_ENV)


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


def make_exchange(environment=DEMO_ENV, *, account_id=None, allow_writes=False, base_root=None, prod_config_path=None):
    from scripts.live_c_demo_exchange import DemoExchange, ProductionExchange
    key, secret, label, resolved_id = load_credentials(
        environment, account_id=account_id, base_root=base_root, prod_config_path=prod_config_path
    )
    if environment == DEMO_ENV:
        return DemoExchange(key, secret), label, resolved_id
    return ProductionExchange(key, secret, allow_writes=bool(allow_writes)), label, resolved_id


def config_ledger_dir(environment, cid, config, all_configs=None, *, base_root=None) -> Path:
    paths = env_paths(environment, base_root=base_root)
    if environment == DEMO_ENV and config.get('legacy_demo_ledger'):
        if all_configs is not None and sum(bool(c.get('legacy_demo_ledger')) for c in all_configs.values()) != 1:
            raise ValueError('多个配置关联同一旧账本，归属待核对。')
        return paths.ledgers
    return paths.ledgers / cid


def alias_fingerprint(alias: str) -> str:
    return sha256(alias.encode('utf-8')).hexdigest()[:8]


def copy_config_payload(source_config, *, target_environment, new_cid, created_at, account_label, account_id=None):
    """Copy only strategy & sizing parameters to another environment; never copy runtime/auth/ledger."""
    return {
        'strategy': source_config['strategy'],
        'mode': '定额跟单',
        'fixed': str(source_config.get('fixed', '20')),
        'budget': str(source_config.get('budget', '100')),
        'leverage': str(source_config.get('leverage', '5')),
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
