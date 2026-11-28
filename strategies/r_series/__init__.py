"""R-Series (R01-R06) TD Sequential Independent Strategy Group for BTC Paper Lab V3.1."""
from strategies.r_series.strategies import R_SERIES_IDS, R_SPECS, PrecomputedMultiTFContext, RSignalEngine, RStrategySpec
from strategies.r_series.runtime import RSeriesLedger

__all__ = [
    "R_SERIES_IDS",
    "R_SPECS",
    "RStrategySpec",
    "PrecomputedMultiTFContext",
    "RSignalEngine",
    "RSeriesLedger",
]
