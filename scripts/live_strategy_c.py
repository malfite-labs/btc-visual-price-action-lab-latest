"""Project the active paper C signal without invoking its scheduler or ledger."""
import hashlib
import importlib.util
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

def closed_window(closed_bars, cycle_at):
    step = timedelta(minutes=15)
    if cycle_at.utcoffset() is None or len(closed_bars) < 288:
        raise ValueError('需要连续 288 根已收盘 15 分钟 K 线')
    cycle = cycle_at.astimezone(UTC)
    if cycle.minute % 15 or cycle.second or cycle.microsecond:
        raise ValueError('策略周期未对齐')
    bars = closed_bars[-288:]
    start = cycle - step * 287
    for index, bar in enumerate(bars):
        at = datetime.fromisoformat(bar['close_at'].replace('Z', '+00:00'))
        if at != start + step * index or bar.get('is_closed') is not True:
            raise ValueError('K 线未收盘或存在断档')
    return bars

class StrategyCAdapter:
    def __init__(self):
        root = Path(__file__).resolve().parents[1] / 'config' / 'rule_v26'
        config = json.loads((root / 'profile.json').read_text(encoding='utf8'))['C']
        path = root / 'v3-signals.py'
        if config['file'] != path.name or hashlib.sha256(path.read_bytes()).hexdigest() != config['sha256']:
            raise ValueError('C rule integrity mismatch')
        self.sha256 = config['sha256']
        self.version = config['version']
        self.max_leverage = int(config['max_leverage'])
        self.risk_fraction = Decimal(config['risk_fraction'])
        spec = importlib.util.spec_from_file_location('live_read_only_c_rules', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.signal = module.signal_c

    def evaluate(self, *, closed_bars, cycle_at):
        from scripts.live_center import StrategySignal

        bars = closed_window(closed_bars, cycle_at)
        value = self.signal(bars, len(bars)-1)
        direction = value['direction'] if value else None
        return StrategySignal('C', self.version, f'C|{self.version}|{cycle_at.isoformat()}', cycle_at,
                              {'LONG': 'BUY', 'SHORT': 'SELL', None: 'WAIT'}[direction], direction,
                              str(value['stop']) if value else None, None, cycle_at, 'BINANCE_USDM_PERP',
                              cycle_at + timedelta(minutes=15), '新周期或行情失效')
