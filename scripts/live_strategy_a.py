"""Read-only Strategy A projection. This module has no execution dependency."""
import hashlib
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from scripts.live_center import StrategySignal
from scripts.rule_evolution_v2 import signal, validate


class StrategyAAdapter:
    def __init__(self, profile_dir=None):
        root = Path(profile_dir) if profile_dir else Path(__file__).resolve().parents[1] / 'config' / 'rule_v26'
        profile = json.loads((root / 'profile.json').read_text(encoding='utf8'))['A']
        payload = (root / profile['file']).read_bytes()
        if hashlib.sha256(payload).hexdigest() != profile['sha256']:
            raise ValueError('A 活动规则校验失败')
        self.sha256 = profile['sha256']
        self.version = profile['version']
        self.max_leverage = int(profile['max_leverage'])
        self.risk_fraction = Decimal(profile['risk_fraction'])
        self.rule = json.loads(payload)
        validate(self.rule)

    def evaluate(self, *, closed_bars, cycle_at):
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
        decision = signal(self.rule, bars, 287)
        direction = decision['direction'] if decision else None
        action = {'LONG': 'BUY', 'SHORT': 'SELL', None: 'WAIT'}[direction]
        identity = hashlib.sha256(f'A|{self.version}|{cycle.isoformat()}'.encode()).hexdigest()
        return StrategySignal('A', self.version, identity, cycle, action, direction,
                              str(decision['stop']) if decision else None,
                              f"{self.rule['target_r']}R，需另做成本核验" if decision else None,
                              cycle, 'BINANCE_USDM_PERP', cycle + step, '新周期或行情失效')
