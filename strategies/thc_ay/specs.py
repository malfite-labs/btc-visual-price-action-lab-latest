"""Frozen V1.0.0 specifications for the 15 Aoying S / AY paper observation instances.

All instances are research hypotheses for forward paper observation only
(historical robustness conclusion: 0/15 passed; original source rule status: BLOCKED_RULE_INCOMPLETE).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

OBSERVATION_TIER = "熬鹰量化研究假设 / 前向纸盘观察"
STRATEGY_GROUP = "熬鹰研究"
ORIGINAL_EVIDENCE_STATUS = "BLOCKED_RULE_INCOMPLETE"
VERSION = "1.0.0"

TF_FACTOR_15M: Dict[str, int] = {
    "15m": 1,
    "1h": 4,
    "4h": 16,
    "1d": 96,
}

TF_STEP_MS: Dict[str, int] = {
    "15m": 15 * 60 * 1000,
    "1h": 60 * 60 * 1000,
    "4h": 4 * 60 * 60 * 1000,
    "1d": 24 * 60 * 60 * 1000,
}


@dataclass(frozen=True)
class ThcAySpec:
    strategy_id: str
    source_experiment: str
    family: str  # "S01", "S02", "COMBO", "AY02", "AY03", "AY04"
    name: str
    timeframe: str  # "15m", "1h", "4h", "1d"
    version: str
    config_sha256: str
    metrics_sha256: str
    min_daily_warmup: int
    cooldown_bars: int
    max_hold_bars: Optional[int]
    target_r: Optional[float]
    parameters: Dict[str, Any]
    description: str

    @property
    def factor_15m(self) -> int:
        return TF_FACTOR_15M[self.timeframe]

    @property
    def step_ms(self) -> int:
        return TF_STEP_MS[self.timeframe]

    @property
    def is_s_family(self) -> bool:
        return self.family in ("S01", "S02", "COMBO")

    @property
    def group(self) -> str:
        return "S系列" if self.is_s_family else "AY系列"


_S01_PARAMS: Dict[str, Any] = {
    "regime_mode": "adx_below",
    "adx_thresh": 25.0,
    "entry_mode": "donchian_n",
    "range_n": 48,
    "quantile_lo": 0.1,
    "quantile_hi": 0.9,
    "stop_mode": "pct",
    "stop_pct": 0.1,
    "tp_mode": "opposite_band",
    "use_ma120_filter": False,
    "ma_type": "sma",
    "exit_on_flip": False,
    "cooldown_bars": 0,
}

_S02_PARAMS: Dict[str, Any] = {
    "regime_mode": "adx_below",
    "adx_thresh": 25.0,
    "s02_ignore_regime": True,
    "breakout_n": 20,
    "s02_entry_mode": "close_break_n",
    "s02_stop_mode": "signal_bar_extreme_plus_buffer",
    "s02_stop_buffer_atr": 0.2,
    "s02_stop_atr_k": 1.5,
    "s02_tp_mode": "atr_tp",
    "s02_tp_atr_k": 3.0,
    "use_ma120_filter": False,
    "ma_type": "sma",
    "cooldown_bars": 0,
}

_COMBO_PARAMS: Dict[str, Any] = {
    "regime_mode": "adx_below",
    "adx_thresh": 25.0,
    "entry_mode": "donchian_n",
    "range_n": 48,
    "quantile_lo": 0.1,
    "quantile_hi": 0.9,
    "stop_mode": "pct",
    "stop_pct": 0.1,
    "tp_mode": "opposite_band",
    "use_ma120_filter": False,
    "ma_type": "sma",
    "exit_on_flip": False,
    "cooldown_bars": 0,
    "s02_ignore_regime": False,
    "breakout_n": 20,
    "s02_entry_mode": "close_break_n",
    "s02_stop_mode": "signal_bar_extreme_plus_buffer",
    "s02_stop_buffer_atr": 0.2,
    "s02_stop_atr_k": 1.5,
    "s02_tp_mode": "atr_tp",
    "s02_tp_atr_k": 3.0,
}

_SPECS_LIST: Tuple[ThcAySpec, ...] = (
    # --- S01 (4 timeframes) ---
    ThcAySpec(
        strategy_id="thc-s01-15m",
        source_experiment="S01_Q_BTC_15m_lab",
        family="S01",
        name="熬鹰研究·区间高空低多 15M",
        timeframe="15m",
        version=VERSION,
        config_sha256="e6eaefd907e878422a5b6e75a76052c8ea67bb80b13d016fbac6e01a950dfff9",
        metrics_sha256="a41033d69579d3209b0ac80d08bcc2d09df75de73e810733139e837e6ebb4ce1",
        min_daily_warmup=200,
        cooldown_bars=0,
        max_hold_bars=None,
        target_r=None,
        parameters=dict(_S01_PARAMS),
        description=f"[{OBSERVATION_TIER}] S01 震荡边界回归（15m 原周期决策；ADX14<25 判震荡，48 根前序唐奇安上下轨反向入场，10% 止损，对侧轨道止盈；原始规则状态 {ORIGINAL_EVIDENCE_STATUS}；历史稳健性 0 通过，仅作前向纸盘观察）。",
    ),
    ThcAySpec(
        strategy_id="thc-s01-1h",
        source_experiment="S01_Q_BTC_1H",
        family="S01",
        name="熬鹰研究·区间高空低多 1H",
        timeframe="1h",
        version=VERSION,
        config_sha256="aeaedaa5cdb3514cf413fdfdb83fa85a0e3810da7dc18a23ec1bc1cefe0e9784",
        metrics_sha256="b8ac929058093b859e48aeb0ade80714e929360f98e6a676997b539d7cdd5a77",
        min_daily_warmup=200,
        cooldown_bars=0,
        max_hold_bars=None,
        target_r=None,
        parameters=dict(_S01_PARAMS),
        description=f"[{OBSERVATION_TIER}] S01 震荡边界回归（1H 原周期 UTC 收盘决策；ADX14<25 判震荡，48 根前序唐奇安上下轨反向入场，10% 止损，对侧轨道止盈；原始规则状态 {ORIGINAL_EVIDENCE_STATUS}）。",
    ),
    ThcAySpec(
        strategy_id="thc-s01-4h",
        source_experiment="S01_Q_BTC_4H",
        family="S01",
        name="熬鹰研究·区间高空低多 4H",
        timeframe="4h",
        version=VERSION,
        config_sha256="33f6749513f7db65e3051583476d1ba069bc7cac101d042e0f6435bdc5fe3ba5",
        metrics_sha256="cbbda2750fbd55183d355fbf09a82bbd1d0fd49b8573dd0929e27c2e3cf79bb5",
        min_daily_warmup=200,
        cooldown_bars=0,
        max_hold_bars=None,
        target_r=None,
        parameters=dict(_S01_PARAMS),
        description=f"[{OBSERVATION_TIER}] S01 震荡边界回归（4H 原周期 UTC 收盘决策；ADX14<25 判震荡，48 根前序唐奇安上下轨反向入场，10% 止损，对侧轨道止盈；原始规则状态 {ORIGINAL_EVIDENCE_STATUS}）。",
    ),
    ThcAySpec(
        strategy_id="thc-s01-1d",
        source_experiment="S01_Q_BTC_1D",
        family="S01",
        name="熬鹰研究·区间高空低多 1D",
        timeframe="1d",
        version=VERSION,
        config_sha256="cde7989bb0c15642cb8ce5b391b3ad5e168ce397e4826816948fe946b74e41e6",
        metrics_sha256="7f90c5d346e426a283cb2dfe3818d7bf004e769a1cd1de44413ab123477e31aa",
        min_daily_warmup=200,
        cooldown_bars=0,
        max_hold_bars=None,
        target_r=None,
        parameters=dict(_S01_PARAMS),
        description=f"[{OBSERVATION_TIER}] S01 震荡边界回归（1D 原周期 UTC 收盘决策；ADX14<25 判震荡，48 根前序唐奇安上下轨反向入场，10% 止损，对侧轨道止盈；原始规则状态 {ORIGINAL_EVIDENCE_STATUS}）。",
    ),
    # --- S02 (4 timeframes) ---
    ThcAySpec(
        strategy_id="thc-s02-15m",
        source_experiment="S02_Q_BTC_15m_lab",
        family="S02",
        name="熬鹰研究·右侧突破追击 15M",
        timeframe="15m",
        version=VERSION,
        config_sha256="f2b3fa1897184bfcc151c577c64b4169132240ddddc9eccc3a6cc5d469c7232d",
        metrics_sha256="a431742ba81fe764a078c646e4bd1d546c58eaf318e037bff8209bf3f2ec2e57",
        min_daily_warmup=200,
        cooldown_bars=0,
        max_hold_bars=None,
        target_r=None,
        parameters=dict(_S02_PARAMS),
        description=f"[{OBSERVATION_TIER}] S02 右侧突破跟随（15m 原周期决策；20 根前序高低点收盘突破，信号 K 极值 ±0.2 ATR 止损，3.0 ATR 止盈；原始规则状态 {ORIGINAL_EVIDENCE_STATUS}）。",
    ),
    ThcAySpec(
        strategy_id="thc-s02-1h",
        source_experiment="S02_Q_BTC_1H",
        family="S02",
        name="熬鹰研究·右侧突破追击 1H",
        timeframe="1h",
        version=VERSION,
        config_sha256="4f2f2ae99c6ccaefa7d554948f41331be5998b7644e66a76cc915ce9e5818339",
        metrics_sha256="e409f71fea02a91908b15e3e1ccfa3b9ceb1dcd3f69298236c959505f35a260c",
        min_daily_warmup=200,
        cooldown_bars=0,
        max_hold_bars=None,
        target_r=None,
        parameters=dict(_S02_PARAMS),
        description=f"[{OBSERVATION_TIER}] S02 右侧突破跟随（1H 原周期 UTC 收盘决策；20 根前序高低点收盘突破，信号 K 极值 ±0.2 ATR 止损，3.0 ATR 止盈；原始规则状态 {ORIGINAL_EVIDENCE_STATUS}）。",
    ),
    ThcAySpec(
        strategy_id="thc-s02-4h",
        source_experiment="S02_Q_BTC_4H",
        family="S02",
        name="熬鹰研究·右侧突破追击 4H",
        timeframe="4h",
        version=VERSION,
        config_sha256="41a56dee3ecd1b3a83f7810202a4aadc98e881b8f3f30cfb33622dcaaf05b330",
        metrics_sha256="4a09cf2b32702f04c587eccf9b1fd0a626de7aabb631cfc4f7913f141106ee60",
        min_daily_warmup=200,
        cooldown_bars=0,
        max_hold_bars=None,
        target_r=None,
        parameters=dict(_S02_PARAMS),
        description=f"[{OBSERVATION_TIER}] S02 右侧突破跟随（4H 原周期 UTC 收盘决策；20 根前序高低点收盘突破，信号 K 极值 ±0.2 ATR 止损，3.0 ATR 止盈；原始规则状态 {ORIGINAL_EVIDENCE_STATUS}）。",
    ),
    ThcAySpec(
        strategy_id="thc-s02-1d",
        source_experiment="S02_Q_BTC_1D",
        family="S02",
        name="熬鹰研究·右侧突破追击 1D",
        timeframe="1d",
        version=VERSION,
        config_sha256="a739dfe0362dbd6c5504d761e00f5a53fd433388c8ff70e06636f77c3a2ad8b3",
        metrics_sha256="46bb1415d76fa8281ac9bb6f0cf8d6b97dce8e3cd3aa03220274735b7121668c",
        min_daily_warmup=200,
        cooldown_bars=0,
        max_hold_bars=None,
        target_r=None,
        parameters=dict(_S02_PARAMS),
        description=f"[{OBSERVATION_TIER}] S02 右侧突破跟随（1D 原周期 UTC 收盘决策；20 根前序高低点收盘突破，信号 K 极值 ±0.2 ATR 止损，3.0 ATR 止盈；原始规则状态 {ORIGINAL_EVIDENCE_STATUS}）。",
    ),
    # --- COMBO S01+S02 (4 timeframes) ---
    ThcAySpec(
        strategy_id="thc-s01-s02-combo-15m",
        source_experiment="COMBO_S01S02_Q_BTC_15m_lab",
        family="COMBO",
        name="熬鹰研究·区间／突破互斥组合 15M",
        timeframe="15m",
        version=VERSION,
        config_sha256="e759783116cee2b0d0d9dbbb5a51d7842872720a1496a8a00ae2216fa7153b1a",
        metrics_sha256="9f6779dfd3954068b2b4bf7157b851c115a2fcbfba27a1a9d3917ef9a208c625",
        min_daily_warmup=200,
        cooldown_bars=0,
        max_hold_bars=None,
        target_r=None,
        parameters=dict(_COMBO_PARAMS),
        description=f"[{OBSERVATION_TIER}] S01+S02 同周期单仓互斥组合（15m 原周期决策；ADX14<25 走 S01 区间回归，ADX14>=25 走 S02 右侧突破；独立记账；原始规则状态 {ORIGINAL_EVIDENCE_STATUS}）。",
    ),
    ThcAySpec(
        strategy_id="thc-s01-s02-combo-1h",
        source_experiment="COMBO_S01S02_Q_BTC_1H",
        family="COMBO",
        name="熬鹰研究·区间／突破互斥组合 1H",
        timeframe="1h",
        version=VERSION,
        config_sha256="fc8465cab6b47b41781cae0175ca325128ba2bd69987a6740c831e8ec4cd95b1",
        metrics_sha256="0d083115bf0264ab490a9e8fb56517901353d98e14d58d21c2ffd1bb9e2c5478",
        min_daily_warmup=200,
        cooldown_bars=0,
        max_hold_bars=None,
        target_r=None,
        parameters=dict(_COMBO_PARAMS),
        description=f"[{OBSERVATION_TIER}] S01+S02 同周期单仓互斥组合（1H 原周期 UTC 收盘决策；ADX14<25 走 S01，ADX14>=25 走 S02；独立记账；原始规则状态 {ORIGINAL_EVIDENCE_STATUS}）。",
    ),
    ThcAySpec(
        strategy_id="thc-s01-s02-combo-4h",
        source_experiment="COMBO_S01S02_Q_BTC_4H",
        family="COMBO",
        name="熬鹰研究·区间／突破互斥组合 4H",
        timeframe="4h",
        version=VERSION,
        config_sha256="c31700b6047bdc12f895addf2e685a616096f052d9f6d8dc93899ff41de17a9d",
        metrics_sha256="d00ea134ed2bb5f6885e4482ee4e5c52b2b06c327fbd03b342aba882e1dce07c",
        min_daily_warmup=200,
        cooldown_bars=0,
        max_hold_bars=None,
        target_r=None,
        parameters=dict(_COMBO_PARAMS),
        description=f"[{OBSERVATION_TIER}] S01+S02 同周期单仓互斥组合（4H 原周期 UTC 收盘决策；ADX14<25 走 S01，ADX14>=25 走 S02；独立记账；原始规则状态 {ORIGINAL_EVIDENCE_STATUS}）。",
    ),
    ThcAySpec(
        strategy_id="thc-s01-s02-combo-1d",
        source_experiment="COMBO_S01S02_Q_BTC_1D",
        family="COMBO",
        name="熬鹰研究·区间／突破互斥组合 1D",
        timeframe="1d",
        version=VERSION,
        config_sha256="51367b3fbb04b628474f223e4041a931e514040f224530c890cb0ad593e2db30",
        metrics_sha256="5088a7d75dd0a8ce71d5b2790bf05e5043df2cf0b3dd1b905fdac1df74bc34ee",
        min_daily_warmup=200,
        cooldown_bars=0,
        max_hold_bars=None,
        target_r=None,
        parameters=dict(_COMBO_PARAMS),
        description=f"[{OBSERVATION_TIER}] S01+S02 同周期单仓互斥组合（1D 原周期 UTC 收盘决策；ADX14<25 走 S01，ADX14>=25 走 S02；独立记账；原始规则状态 {ORIGINAL_EVIDENCE_STATUS}）。",
    ),
    # --- AY Series (3 x 15m) ---
    ThcAySpec(
        strategy_id="thc-ay02-15m",
        source_experiment="AY02",
        family="AY02",
        name="熬鹰研究·日线过滤与收敛突破 15M",
        timeframe="15m",
        version=VERSION,
        config_sha256="bdc673a3dc8a5a362abcf46b5b9c7004429a2ef86ce84eafb1fc7e2daebda4cd",
        metrics_sha256="b4e96c74b7c59e22f44003f05bd938a42bf364f9d2a0c33097c542335ba8a94e",
        min_daily_warmup=120,
        cooldown_bars=4,
        max_hold_bars=96,
        target_r=2.0,
        parameters={
            "daily_sma": 120,
            "channel_n": 32,
            "atr_n": 14,
            "compression_quantile": 0.3,
            "compression_lookback": 192,
            "sl_atr": 1.5,
            "tp_r": 2,
            "max_hold_bars": 96,
            "cooldown_bars": 4,
            "variant": "BASELINE",
        },
        description=f"[{OBSERVATION_TIER}] AY02 日线 SMA120 方向过滤 + 32 根通道宽度<=192 历史 30% 分位收敛突破，1.5 ATR 止损，2R 止盈，最长 96 根，4 根冷却（冻结 BASELINE；原始规则状态 {ORIGINAL_EVIDENCE_STATUS}）。",
    ),
    ThcAySpec(
        strategy_id="thc-ay03-15m",
        source_experiment="AY03",
        family="AY03",
        name="熬鹰研究·恐慌反转代理 15M",
        timeframe="15m",
        version=VERSION,
        config_sha256="24e467b9bb2895ab506c8699ca2349b00603c4e0f9b8ee486dab9c354e06196f",
        metrics_sha256="b4e96c74b7c59e22f44003f05bd938a42bf364f9d2a0c33097c542335ba8a94e",
        min_daily_warmup=120,
        cooldown_bars=4,
        max_hold_bars=32,
        target_r=2.0,
        parameters={
            "atr_n": 14,
            "shock_atr": 3,
            "lookback": 8,
            "wick_ratio": 0.4,
            "sl_buffer_atr": 0.25,
            "tp_r": 2,
            "max_hold_bars": 32,
            "cooldown_bars": 4,
            "variant": "BASELINE",
        },
        description=f"[{OBSERVATION_TIER}] AY03 恐慌反转代理（仅做多；8 根累计跌幅>3 ATR、下影>=40%、收盘突破前高，近 8 根低点-0.25 ATR 止损，2R 止盈，最长 32 根，4 根冷却；冻结 BASELINE；原始规则状态 {ORIGINAL_EVIDENCE_STATUS}）。",
    ),
    ThcAySpec(
        strategy_id="thc-ay04-15m",
        source_experiment="AY04",
        family="AY04",
        name="熬鹰研究·震荡区间反转 15M",
        timeframe="15m",
        version=VERSION,
        config_sha256="1c483895f6fc745579f76298a951a9701b2c332cb2b0177de02b0364c40b5903",
        metrics_sha256="b4e96c74b7c59e22f44003f05bd938a42bf364f9d2a0c33097c542335ba8a94e",
        min_daily_warmup=120,
        cooldown_bars=4,
        max_hold_bars=48,
        target_r=None,
        parameters={
            "channel_n": 48,
            "adx_n": 14,
            "adx_max": 20,
            "atr_n": 14,
            "sl_buffer_atr": 0.25,
            "max_hold_bars": 48,
            "cooldown_bars": 4,
            "variant": "BASELINE",
        },
        description=f"[{OBSERVATION_TIER}] AY04 震荡区间反转（ADX14<20，触及 48 根前序区间边缘并收回，区间中点止盈、区间外 0.25 ATR 止损，成本后至少 1R，最长 48 根，4 根冷却；冻结 BASELINE；原始规则状态 {ORIGINAL_EVIDENCE_STATUS}）。",
    ),
)

THC_AY_SPECS: Dict[str, ThcAySpec] = {s.strategy_id: s for s in _SPECS_LIST}
THC_AY_IDS: Tuple[str, ...] = tuple(s.strategy_id for s in _SPECS_LIST)
