# State machine and risk contract V1.0.3

Implementation supplement to the unchanged V1.0.1 baseline and V1.0.2 Alpaca source overlay. This implementation is an internal research ledger only; auto decisions and simulated entries remain disabled without an authorized vision model and user-approved cost settings.

## 1. Position states and atomic transitions

`FLAT -> OPEN_LONG | OPEN_SHORT -> STOP_EXIT_PENDING -> CLOSING -> FLAT`. Any restart reconciliation mismatch transitions to `RECOVERY_REQUIRED`; a row with missing position payload is not closed automatically. The SQLite account state version is incremented at every accepted transition. Mutations run in `BEGIN IMMEDIATE`, and fill, account state, cash event, and audit event share one transaction.

Every fill has a unique idempotency key; close/stop event keys are unique across fills. Re-delivery returns the previous outcome. A stop-pending state is idempotent and has priority over a later active model close. After restart a persisted pending stop remains pending; no historical price is synthesized. Only a fresh, correctly sourced quote may resolve it.

## 2. Spot stop reference and recovery

- Alpaca BTC/USD spot is not a perpetual mark-price feed.
- Long stop trigger: `bid <= stop_price`; executable reference is the bid.
- Short stop trigger: `ask >= stop_price`; executable reference is the ask.
- Once a reliable quote confirms a trigger, persist `STOP_EXIT_PENDING` before attempting an exit.
- If the quote/book is absent, stale, wrong source or unusable, keep the event pending and set protection state uncertain. After recovery use a fresh quote, walk available exit-side depth, apply adverse slippage and fee, then atomically close. Never backfill at the stop price or candle close.
- Stop wins if it commits before a model close; a model close that has already committed makes a later stop event an idempotent no-op.

## 3. Research cost and sizing formula

All calculations use Decimal. The current configured assumptions are non-production research defaults: `f=0.001` fee per side (10 bp), `s=0.001` adverse slippage per side (10 bp), quote/book max age 2,000 ms, simulated tick 0.01 USD, BTC quantity step/minimum `0.00000001`, minimum notional 1 USD, price guard 50 bp. These values are implementation hypotheses, not verified Alpaca account fees or empirical slippage. No Alpaca spot funding fee applies. Values require approval and calibration before user trial.

For equity `E`, available collateral `C`, entry executable reference `P`, stop `S`, direction `d`, fee `f`, slippage `s`, risk fraction `r=0.02`, leverage `L=1`:

`D = |P-S| + P(f+s) + S(f+s)`

`Q_risk = (E*r)/D`

`Q_target = (E*L)/P`

`Q_cash = C/[P*(1+f+s)]`

`Q_liquidity = min(total executable entry-side depth, total executable exit-side depth)`

`Q = floor_to_step(min(Q_risk,Q_target,Q_cash,Q_liquidity), quantity_step)`

Then recompute notional and `Q*D`; reject unless `Q >= minimum_quantity`, `Q*P >= minimum_notional`, notional does not exceed either `E*L` or available collateral, and recomputed planned loss `<= E*r`. Entry and exit simulated fills walk displayed order-book levels for VWAP, apply adverse-only slip, round price adversely to the configured tick and reject if depth is insufficient or deviation from L1 exceeds the price guard. Fees are then computed from rounded fill notional. No zero-cost default is allowed.

The current code provides the calculation/preflight and persistence primitives. Automatic execution is gated off, and tests do not represent live fills or profits.

## 4. Account and ledger invariants

Three independent internal USD_RESEARCH accounts each begin with 10,000.00. No USD/USDT equivalence is assumed. A future USDT mode requires an audited conversion feed and separate equity series. One active position maximum per agent; no add-to-position. No cash borrowing; shorts are internal paper exposure, not routed orders. Fee events are debited once per fill. Slippage is embedded in simulated fill price and must not be debited a second time.

## 5. Model decision guard

One durable decision slot per `(agent, completed 15m candle, MD version)` is reserved before a model request. All three agents receive the exact same image bytes/hash but only their own MD/context. The result is rejected after the next candle deadline, for invalid action, missing stop on open, mismatched model/hash/version or timeout. A test fake is always labeled `TEST_SIMULATION_ONLY`. The generic production adapter is `MODEL_NOT_CONFIGURED` until separately authorized/credentialed; no paid request is made by this implementation.
