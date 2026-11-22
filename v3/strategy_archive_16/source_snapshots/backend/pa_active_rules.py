"""Read-only active-rule documentation for the frozen PA extension V1.0.0 implementation."""
import hashlib
import json
import re
from pathlib import Path
from zipfile import ZipFile

VERSION = '1.0.0'
SOURCES = (
    'scripts/pa09_pa10_candidates.py', 'scripts/pa09_pa10_execution.py',
    'backend/pa_extension.py', 'backend/strategy_pool.py', 'backend/domain.py',
    'backend/settings.py',
)
ROOT = Path(__file__).resolve().parents[1]
PINNED = {'scripts/pa09_pa10_candidates.py': '0ca34cfdbc9564528857c7a2ce9c985a46cc561542ab37cddf5669b4d6177db5', 'scripts/pa09_pa10_execution.py': '6345b1d0b12a26b338f51e5b3be4316215d52fa04a3753b9b99f3251ccda97bd', 'backend/pa_extension.py': 'df103c8c4568062d6ff1c4d4b4246ebae811997fbc21022aa761550b53341059', 'backend/strategy_pool.py': '0208e001cdf848e6a5044337258e4f0f258df4624c2fbe195184b98eabc6b9cf', 'backend/domain.py': '5a1c0514b6232634960639e79376d3af46f9a2c35d1e070f955ae2883874b464', 'backend/settings.py': 'a6bcf523ffb04fc57e55e4dd656b3aa3f6cc21761cc1101c657246beee14bda5'}

RULES = {
    'PA-09': {
        'type': '区间边界 Pinbar 反转',
        'market_conditions': '横盘区间，价格触及支撑或阻力后收回区间；不适用于明显均线分离的趋势。',
        'parameters': {
            'warmup_closed_bars': 61, 'range_bars': 40, 'atr_period': 14,
            'width_fraction_inclusive': [0.015, 0.06], 'boundary_atr_tolerance': 0.2,
            'min_touches_each': 2, 'touch_min_bar_spacing': 3, 'sma_periods': [20, 60],
            'sma_relative_difference_max': 0.015, 'long_wick_min_body_multiple': 2,
            'opposite_wick_max_body_multiple': 1, 'stop_atr_buffer': 0.1,
            'target_atr_inset': 0.15, 'minimum_reward_risk': 2,
        },
        'sections': [
            ['区间与趋势过滤', '信号前40根：S=min(Low)，R=max(High)，区间宽度 (R−S)/((R+S)/2) 为1.5%–6%（含边界）。'
             '支撑用 Low、阻力用 High，距离 ≤0.2×ATR；两侧各至少2次，独立触及间隔至少3根。'
             '信号前 SMA20、SMA60 的差除以 SMA60，绝对值 ≤1.5%。ATR14 为信号前14根真实波幅简单均值，必须大于0。'],
            ['LONG', 'S≤Close≤R；Close>Open；Open−Low≥2×实体；High−Close≤实体；|Low−S|≤0.2×ATR。'],
            ['SHORT', 'S≤Close≤R；Close<Open；High−Open≥2×实体；Close−Low≤实体；|High−R|≤0.2×ATR。'],
            ['固定止损与止盈', '多：SL=min(信号Low,S)−0.1×ATR；TP=R−0.15×ATR。空：SL=max(信号High,R)+0.1×ATR；TP=S+0.15×ATR。'
             '以信号收盘价先检查2R，再以含滑点的实际模拟入场价复核2R；目标价不随成交价重算，并须覆盖双边手续费。'],
            ['拒绝条件', '不满61根、区间/趋势/触及/形态校验失败、零实体、收盘出界、错误方向的保护价、低于2R、成本或仓位校验失败，不开仓。'],
        ],
        'position': {'target_mode': 'FIXED_RANGE_OPPOSITE_PRICE', 'max_hold_bars': 96},
    },
    'PA-10': {
        'type': '完整压力支撑互换',
        'market_conditions': '突破阻力或跌破支撑，经过延伸、回踩和确认后顺向入场。',
        'parameters': {
            'warmup_closed_bars': 41, 'level_history_bars': 40, 'atr_period': 14,
            'level_atr_tolerance': 0.2, 'min_touches': 2, 'touch_min_bar_spacing': 3,
            'breakout_atr': 0.2, 'breakout_body_fraction': 0.5,
            'extension_atr': 1, 'retest_atr': 0.2, 'max_close_through_atr': 0.1,
            'stop_atr_buffer': 0.1, 'window_bars_after_breakout': 16, 'target_r': 2.5,
        },
        'sections': [
            ['关键位与突破', '多用信号前40根最高 High，空用最低 Low。ATR14 取信号前真实波幅简单均值；ATR>0。'
             '至少2次独立触及（间隔≥3根、距离≤0.2×ATR）。方向 d：多=+1、空=−1。'
             'd×(Close−关键位)≥0.2×ATR，d×(Close−Open)>0，实体≥全幅50%；进入 BREAKOUT_CANDIDATE，冻结关键位、方向与 ATR。'],
            ['延伸与回踩', '下一根起，d×(顺向极值−关键位)≥1×冻结ATR，进入 EXTENSION_CONFIRMED。'
             '其后用多头 Low / 空头 High 检查 |反向极值−关键位|≤0.2×冻结ATR；进入 RETEST_OBSERVED，保存该根顺向极值为确认线。'],
            ['LONG / SHORT 最终确认', '多头收盘突破回踩根 High、且为阳线；空头收盘跌破回踩根 Low、且为阴线。'
             '阶段逐根推进，不在同一根跨出多阶段。等待确认时持续更新回踩以来的最低 Low（多）或最高 High（空）。'],
            ['16 根窗口、持久化与取消', '突破后第1至16根（含第16根）内完成确认。'
             'd×(Close−关键位)<−0.1×冻结ATR 即失效；断档或市场恢复会清除未完成形态。'
             '阶段、水平位、冻结ATR、突破时间、确认线和回踩极值保存在对应账户快照中；已处理周期不重放。'],
            ['结构止损与2.5R止盈', '多：SL=回踩以来最低 Low−0.1×冻结ATR；空：SL=回踩以来最高 High+0.1×冻结ATR。'
             'TP=实际模拟入场价+d×2.5×|入场价−止损价|。错误方向的保护价、成本或仓位校验失败，不成交。'],
        ],
        'position': {'target_mode': 'ACTUAL_FILL_2_5R', 'target_r': 2.5, 'max_hold_bars': 192},
    }
}
EXECUTION = {
    'entry': '仅处理已收盘的连续有效15分钟K线。信号确认后，下一根15分钟窗口内按有效流报价模拟成交：多用 ask、空用 bid，加不利滑点；不按历史 OHLC 开盘价回填。',
    'cancel': '报价无效或过期不成交；超过下一根窗口取消。调度或行情保护不允许时拒绝；重连清理旧信号，已处理周期、报价与成交去重。',
    'exit': '多头按 bid、空头按 ask 检查实时止损/止盈，按原费滑结算。连续收盘也可处理保护价与持仓超时，同根止损优先；缺口不制造历史成交。',
}

def active_rule_record(sid, version, costs, *, root=ROOT, provenance='运行配置导出'):
    if sid not in RULES or version != VERSION:
        return None
    observed = {n: hashlib.sha256((root/n).read_bytes()).hexdigest() for n in SOURCES}
    if observed != PINNED:
        raise ValueError('活动规则来源文件哈希不匹配')
    meta = RULES[sid]
    doc = {
        'document_kind': 'ACTIVE_RULE_JSON', 'original_json': None,
        'source': provenance, 'strategy_id': sid, 'version': version,
        'market': 'BTC/USD', 'timeframe': '15m', 'type': meta['type'],
        'market_conditions': meta['market_conditions'], 'parameters': meta['parameters'],
        'sections': meta['sections'], 'position': meta['position'],
        'runtime_config': {'execution': EXECUTION, 'cost_model': costs},
        'risk': {'initial_equity': '10000 USD_RESEARCH', 'leverage_limit': 5,
                 'risk_fraction': '0.02', 'max_concurrent_positions': 1,
                 'pyramiding': False},
        'source_sha256': observed,
    }
    raw = json.dumps(doc, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return {'strategy_id': sid, 'version': version, 'document_kind': 'ACTIVE_RULE_JSON',
            'document': raw, 'sha256': hashlib.sha256(raw.encode('utf8')).hexdigest()}


def demo_extension_rule_record(runtime_doc, entry_options, quote_max_age, *, root=ROOT):
    """Combine the verified PA signal rules with the active Demo execution settings."""
    sid = runtime_doc['strategy_id']
    record = active_rule_record(sid, runtime_doc['version'], {}, root=root)
    if record is None or runtime_doc.get('demo_extension') is not True:
        return None
    doc = json.loads(record['document'])
    doc.update(market='BTCUSDT', currency='USDT', source='Binance Demo 运行配置导出')
    doc['risk']['initial_equity'] = f"{entry_options['equity']} USDT"
    doc['position']['max_hold_bars'] = runtime_doc['position']['max_hold_bars']
    if sid == 'PA-09':
        doc['sections'][-2:] = [
            ['固定止损与止盈', '多：SL=min(信号Low,S)−0.1×ATR；TP=R−0.15×ATR。'
             '空：SL=max(信号High,R)+0.1×ATR；TP=S+0.15×ATR。目标价在信号时确定。'],
            ['入场校验', '报价加不利滑点得到预估入场价，校验最低2R、目标方向、成本及仓位后才提交订单。'],
        ]
    else:
        target_r = runtime_doc['position']['target_r']
        doc['position'].update(target_mode='ACTUAL_DEMO_FILL_R_MULTIPLE', target_r=target_r)
        doc['sections'][-1] = [
            '结构止损与止盈', '多：SL=回踩以来最低 Low−0.1×冻结ATR；空：SL=回踩以来最高 High+0.1×冻结ATR。'
            f'按 Demo 确认的入场均价和初始止损距离设置 {target_r}R 止盈。',
        ]
    doc['runtime_config'] = {
        'entry': '只读连续已收盘15分钟K线。下一根周期的开仓窗口内，多用 ask、空用 bid 预估后提交 Demo 订单。',
        'exit': '多用 bid、空用 ask 核验已确认仓位的止损和止盈；到期也通过有效报价提交退出。',
        'opening_window_seconds': entry_options['opening_window'].total_seconds(),
        'max_quote_age_seconds': quote_max_age.total_seconds(),
        'commission_source': 'BINANCE_DEMO_CONFIRMED_FILL',
        'order_estimate': {key: str(entry_options[key]) for key in (
            'fee_rate', 'slippage', 'quantity_step', 'minimum_quantity', 'minimum_notional', 'price_tick')},
    }
    for name in ('backend/binance_demo_pa.py', 'backend/binance_demo_pa_runtime.py',
                 'backend/binance_demo_hedge.py', 'backend/binance_demo_orders.py'):
        doc['source_sha256'][name] = hashlib.sha256((root / name).read_bytes()).hexdigest()
    raw = json.dumps(doc, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return {**record, 'document': raw, 'sha256': hashlib.sha256(raw.encode('utf8')).hexdigest()}


def deployed_release_record(sid, version, costs, listener_pid, *, root=ROOT):
    """Fallback for the existing 8011 only; never assert another deployment matches."""
    if sid not in RULES or version != VERSION:
        return None
    release = root/'logs/PA_MARKET_AUTO_RECOVERY_RC.zip'
    digest = hashlib.sha256(release.read_bytes()).hexdigest()
    if digest != '61d1375aa9d3121849d181e315a28342ed3384a5f5b63bba9cce7e04376fe7ec':
        raise ValueError('现网候选发布包校验失败')
    log = (root/'logs/pa-market-auto-recovery-deploy.log').read_text(encoding='utf-16')
    pids = re.findall(r'NEW_PID (\d+)',log)
    if not pids or int(pids[-1]) != int(listener_pid) or 'PROTECTION_PASS' not in log:
        raise ValueError('当前 8011 进程与已验证发布记录不匹配')
    with ZipFile(release) as archive:
        manifest=json.loads(archive.read('SHA256.json'))
        if any(manifest.get(n)!=PINNED[n] for n in SOURCES if n in manifest):
            raise ValueError('发布包中的运行规则源码校验失败')
    return active_rule_record(sid,version,costs,root=root,
                              provenance='当前部署运行配置导出（发布包、监听 PID 与源码 SHA-256 核验）')
