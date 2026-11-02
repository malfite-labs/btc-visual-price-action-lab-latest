# Synthetic Vision Ground-Truth Rules V1

Status: frozen for dataset `adjudicated-v1` by the commit that introduces this file and its manifests. The rules were written from OHLC semantics before scoring any model response. They must not be tuned to historical DeepSeek answers.

## Input contract and boundaries

- A fixture contains exactly 384 closed, consecutive BTC/USD 15-minute candles. The scored global window is the last 288 candles (72 hours); the local window is the last 96 candles (24 hours) and is nested at the end of the global window.
- Each OHLC value is parsed as an exact decimal. Values must be finite and positive; `high >= max(open, close)`, `low <= min(open, close)`, and `high >= low`. Candle open timestamps must be consecutive by exactly 900 seconds. Invalid, missing, duplicated, or out-of-order bars make the fixture unscorable; no candle is synthesized or forward-filled.
- Endpoint change is `100 * (last.close / first.open - 1)`. It is reported separately from structural direction. The ending value is the final candle close, not the final candle open.
- Window high and low are the maximum high and minimum low. If an extreme ties, the earliest candle index is reported. Indices are zero-based within the scored window.

## Segment structure

- Divide the 288-bar global window into three consecutive 96-bar segments. Divide the 96-bar local window into three consecutive 32-bar segments. Each segment return uses its first candle open and last candle close.
- A segment is rising at `>= +0.25%`, falling at `<= -0.25%`, and otherwise neutral.
- A window is structurally `rising` only when its endpoint change is `>= +1.0%` and at least two of its three segments are rising. It is `falling` only when its endpoint change is `<= -1.0%` and at least two segments are falling.
- A window is `range` only when the absolute endpoint change is strictly below `1.0%` and all three segment returns have absolute values strictly below `1.5%`.
- All remaining cases are `mixed`. Thus endpoint direction alone never overrides conflicting segment structure. Exact thresholds are inclusive for rising/falling and exclusive for range as stated above.

## Local rebound versus confirmed reversal

- A local move is considered structurally rising only under the local 96-bar rules above. A rising move is a confirmed reversal only if all of these are true: (1) global 288-bar structure is `falling`; (2) local structure is `rising`; (3) local endpoint change is at least `+1.0%`; (4) among local bars 0–47 and 48–95, the second half has a strictly higher maximum high and a strictly higher minimum low; and (5) the final local close is strictly above the first-half maximum high.
- If local structure is rising but any reversal condition fails, label it `rally_not_confirmed_reversal`. Otherwise the event is `no_confirmed_reversal`. Ties do not confirm higher highs, higher lows, or a breakout.
- `assessment` is `reversal_structure` only for a confirmed reversal; `trend_structure` when both windows share the same rising/falling direction; `range_or_wait` when both are range; otherwise `mixed_transition`.

## Special cases and isolation

- The scoring evaluator consumes the frozen manifest only after receiving and validating a model's structured response. The provider request contains no fixture ID, manifest, score key, answer label, or expected classification.
- Historical results remain immutable. A later adjudication creates an additive dataset/version and correction note; it never edits an earlier result or retroactively tunes these thresholds.
- These deterministic synthetic fixtures test image-reading behavior only. They are not historical market data, profitability evidence, or proof of production trading ability.
