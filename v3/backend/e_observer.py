"""Observe-only E vision pipeline. It cannot place orders or alter ABC accounts."""
import asyncio
import hashlib
import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from backend.book_pa_chart_v03 import render_numbered_chart
from backend.e_visual import resolve_e_decision, validate_e_model_response
from backend.market import Candle


class EObserver:
    def __init__(self, db_path, snapshot_dir, registry, *, recover_interrupted=False):
        self.db_path = str(db_path)
        self.snapshot_dir = Path(snapshot_dir)
        self.registry = registry
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.db_path)) as db, db:
            db.execute('CREATE TABLE IF NOT EXISTS e_observations (close_at TEXT PRIMARY KEY, status TEXT NOT NULL, result_json TEXT NOT NULL)')
            if recover_interrupted:
                db.execute("UPDATE e_observations SET status='INTERRUPTED', result_json=? WHERE status='PENDING'", (json.dumps({'status':'INTERRUPTED','reason':'OUTCOME_UNKNOWN_NO_RETRY'}),))

    async def observe(self, bars, close, adapter):
        if close.tzinfo is None:
            raise ValueError('TIMEZONE_REQUIRED')
        key = close.astimezone(UTC).isoformat()
        # Validate the window before recording or sending anything.
        resolve_e_decision({'action': 'SKIP'}, bars, mapping_verified=False)
        if datetime.fromisoformat(bars[-1]['close_at'].replace('Z', '+00:00')).astimezone(UTC) != close.astimezone(UTC):
            raise ValueError('SNAPSHOT_CLOSE_MISMATCH')
        strategy = self.registry.current_strategy('E')
        if strategy is None:
            return {'status':'STRATEGY_INTEGRITY_ERROR'}
        with closing(sqlite3.connect(self.db_path)) as db, db:
            inserted = db.execute('INSERT OR IGNORE INTO e_observations VALUES(?,?,?)', (key, 'PENDING', '{}')).rowcount
            if not inserted:
                return {'status':'DUPLICATE_CYCLE'}
        response = None
        evidence = None
        try:
            if not adapter.configured:
                result = {'status':'MODEL_UNAVAILABLE'}
            else:
                path = self.snapshot_dir / (close.astimezone(UTC).strftime('%Y%m%dT%H%M%SZ') + '.png')
                evidence = render_numbered_chart([Candle(**b) for b in bars], path)
                prompt = ('只依据图像分析BTC 15分钟K线。K01至K48为最近48根已收盘K线。'
                          '不得输出价格。返回JSON：action为SKIP或OPEN，side为FLAT/LONG/SHORT，'
                          'setup为E1/E2/E3/E4或CHOP/UNKNOWN，confidence为HIGH/MEDIUM/LOW/UNKNOWN，'
                          'reasons为字符串数组；OPEN时stop_anchor和reward_anchor分别为'
                          '{"candle":"K01至K48","field":"high或low"}。\n' + strategy[1])
                timeout = float(getattr(getattr(adapter, 'config', None), 'timeout_seconds', 60))
                response = await asyncio.wait_for(adapter._send_image_request(path.read_bytes(), prompt, 'E:' + key), timeout=timeout)
                if response.get('status'):
                    result = {'status':'MODEL_FAILED','reason':response['status']}
                else:
                    validate_e_model_response(response)
                    resolved = resolve_e_decision(response, bars, mapping_verified=False)
                    result = {'status':'OBSERVE_ONLY','decision':response,'mapping':resolved,
                              'chart_sha256':evidence['chart_sha256'], 'candle_sha256':evidence['candle_sha256'],
                              'strategy_version':strategy[0], 'strategy_sha256':hashlib.sha256(strategy[1].encode('utf-8')).hexdigest()}
        except TimeoutError:
            result = {'status':'MODEL_TIMEOUT'}
        except Exception as exc:
            result = {'status':'OBSERVATION_FAILED','error_class':type(exc).__name__, 'reason':str(exc)}
        if isinstance(response, dict):
            result['provider_response'] = response
        if evidence:
            result.setdefault('chart_sha256', evidence['chart_sha256'])
            result.setdefault('candle_sha256', evidence['candle_sha256'])
        result['event_id'] = 'E:' + key
        result['closed_bar_time'] = key
        with closing(sqlite3.connect(self.db_path)) as db, db:
            db.execute('UPDATE e_observations SET status=?,result_json=? WHERE close_at=?',
                       (result['status'],json.dumps(result,ensure_ascii=False,default=str),key))
        return result


async def dispatch_e_observation(observer, bars, close, adapter):
    """Keep observer failures and snapshot mutations out of the ABC scheduler."""
    try:
        return await observer.observe([dict(bar) for bar in bars], close, adapter)
    except Exception as exc:
        return {'status': 'OBSERVATION_FAILED', 'error_class': type(exc).__name__}


def read_observations(path, limit=50):
    """Return stored observation evidence without creating a database or accounts."""
    path = Path(path)
    if not path.exists():
        return {'status': 'NOT_STARTED', 'items': []}
    if not 1 <= limit <= 200:
        raise ValueError('INVALID_LIMIT')
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as db:
            rows = db.execute('SELECT close_at,status,result_json FROM e_observations ORDER BY close_at DESC LIMIT ?', (limit,)).fetchall()
        return {'status': 'OBSERVE_ONLY', 'items': [{'close_at': close, 'status': status, 'result': json.loads(result)} for close, status, result in rows]}
    except (sqlite3.Error, ValueError):
        return {'status': 'UNAVAILABLE', 'items': []}


def create_e_observe_adapter(budget_dir, image_capability_verified, *, strategy_version="E-V1.0"):
    """Create an E-only bounded model adapter; never alters the ABC configuration."""
    import os
    from decimal import Decimal
    from backend.vision_provider import ConfigurableVisionAdapter, VisionProviderConfig
    explicit = os.getenv('E_OBSERVE_ENABLED')
    if explicit is None:
        try:
            setting=json.loads((Path(budget_dir)/'observe-config.json').read_text(encoding='utf8'))
            enabled=setting == {'enabled': True, 'mode':'OBSERVE_ONLY'}
        except (OSError,ValueError,TypeError):
            enabled=False
    else:
        enabled=explicit.lower() == 'true'
    if not enabled:
        return None
    if strategy_version not in {'E-V1.0','E-V1.1-QUANT'}:
        raise ValueError('INVALID_E_STRATEGY')
    budget_name='quant-observe-budget.sqlite3' if strategy_version=='E-V1.1-QUANT' else 'observe-budget.sqlite3'
    config=VisionProviderConfig(enabled=True,calls_authorized=True,model='deepseek-flash',thinking_mode='disabled',
        max_concurrency=1,max_output_tokens=1200,max_requests_per_day=4,
        max_call_cost_usd=Decimal('0.05'),daily_budget_usd=Decimal('0.20'),total_budget_usd=Decimal('0.20'),
        input_price_per_million_usd=Decimal('0.30'),output_price_per_million_usd=Decimal('1.20'),
        budget_db_path=str(Path(budget_dir)/budget_name))
    adapter=ConfigurableVisionAdapter(config)
    adapter.image_capability_verified = image_capability_verified is True
    return adapter
