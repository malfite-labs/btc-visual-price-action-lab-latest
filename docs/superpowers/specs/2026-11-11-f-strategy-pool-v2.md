# F Strategy Pool v2 Design

## Goal

Represent and simulate the two frozen F 15-minute candidates faithfully in the strategy pool, while keeping existing v1 strategies unchanged and leaving both F registrations paused by default.

## Source rules

Use the frozen first-round candidates from `logs/f-4h-candidates/`, as evaluated in `logs/f-15m-evaluation-20261110T081138Z/`:

- Both directions, BTC/USD, 15m; SMA20>SMA60 and SMA20 is greater than its value five bars earlier for long; mirrored for short.
- Breakout: close beyond the preceding 40-bar high/low, matching candle direction, body/range at least 0.40.
- Pullback: at least one of the previous 3 lows/highs touches SMA20; current close clears the previous bar high/low and SMA20, matching candle direction, body/range at least 0.60.
- Stop is the signal bar and preceding two bars' low/high with a 0.1 ATR(14) buffer. Signal-close-to-stop distance must be 0.6–2.5 ATR.
- Enter at the next bar open, target 2R from actual entry to stop, max hold 192 15m bars (48h), stop wins same-bar stop/target ambiguity.
- Fixed pool risk values: $10,000, USD_RESEARCH, 5x leverage cap, 2% risk. Preserve the target-cost gate, taker fees and slippage configured by the pool.

## Proposed format and compatibility

- Keep `btc-lab-declarative-strategy/v1` parsing, execution, and examples unchanged.
- Add `btc-lab-declarative-strategy/v2` with a `position` object for direction, next-open entry, swing-plus-ATR stop, target R, maximum holding bars, and same-bar priority.
- Extend the expression allow-list with bounded numeric operations needed by these rules: open/high/low series; highest/lowest over a specified prior-bar window; ATR; candle body fraction; and SMA at a bounded prior offset. Keep AST-only interpretation, existing byte/depth/node/lookback limits, finite-number checks, and no dynamic code execution.
- Store long and short entry conditions separately so direction cannot be inferred ambiguously from one boolean result.
- Apply v2 execution semantics consistently to isolated backtests and the pool cycle/quote management paths. Existing v1 position lifecycle remains unchanged. Upload continues to register paused; this work does not upload, enable, deploy, restart services, write runtime ledgers, or send orders.

## Outputs

- Two ready-to-upload JSON files under `config/strategy_pool/`: one breakout candidate and one pullback candidate, each with versioned IDs, full parameters and explicit rule descriptions.
- Focused tests for validation and expression bounds, both long/short signal branches, next-open fill, 2R target, stop sizing bounds, max-hold exit, ambiguous stop-first handling, v1 compatibility, paused upload, and isolated backtest/runtime lifecycle.
- Update this design into an implementation plan and the project `codex.md` after approval and completion.

## Safety and behavior

- Preserve the existing v1 schema and behavior; reject malformed v2 execution settings and unsupported AST nodes.
- New uploads remain `REGISTERED`/paused. Tests use temporary databases and synthetic candles. No strategy activation, production account writes, deployment, or service restart is part of this change.
- The v2 simulator must not fill at the signal candle close. If the next open is unavailable or a data gap interrupts the sequence, do not invent a fill; record a skipped/censored entry and resume only after the configured warm-up.

## Review points

- Confirm v2 format versioning and top-level `position` object.
- Confirm that “exact support” includes both isolated backtesting and the disabled-by-default pool execution lifecycle, not merely creating JSON accepted by the validator.
- Confirm no strategy is uploaded or enabled by this implementation; deliver the two valid files for the user to upload.
