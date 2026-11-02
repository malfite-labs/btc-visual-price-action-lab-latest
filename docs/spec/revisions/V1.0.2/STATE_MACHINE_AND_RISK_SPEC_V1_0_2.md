# BTC Visual Price Action Lab V1.0 — Paper State Machine and Risk Spec V1.0.2

- Parent: `docs/spec/STATE_MACHINE_AND_RISK_SPEC_V1.md` (V1.0.1; preserved).
- The 1x notional cap, 2% planned risk, decimal arithmetic, downward quantity rounding, affordability checks, ledger atomicity and shadow formula remain unchanged except for the source/currency and cost rules below.

## Instrument, account and price domains

Alpaca phase uses spot `BTC/USD`; the frozen virtual account seed remains 10,000 USDT per agent. USD quote notional and USDT ledger equity are different units. No parity assumption or implicit FX conversion is permitted. Until the user approves either a USD/USDT valuation source/rule or a documented account-denomination amendment, sizing, entry, stop-exit, PnL and shadow comparisons involving this instrument must fail closed. No P1 readiness can be claimed while unresolved.

The normalized Alpaca stop reference is the latest Alpaca BTC/USD trade price and its provider event time (`TriggerReference.kind=ALPACA_SPOT_LAST_TRADE`). The reference is for trigger detection only; it is not a fill price. On a future approved Binance USDⓈ-M adapter, `TriggerReference.kind=BINANCE_MARK_PRICE` maps to the Binance mark-price stream. A source-mode change cannot reinterpret old position stops.

## Stop trigger and recovery state machine

`PROTECTION_ARMED → STOP_TRIGGER_RECORDED → STOP_EXIT_PENDING → BOOK_VALIDATED → EXIT_INTENT_COMMITTED → EXIT_FILLED → POSITION_FLAT`.

1. Long triggers when last trade `price <= stop_price`; short triggers when `price >= stop_price`; equality triggers. Persist the unique `(position_id, source_id, trade_id)` trigger, price, provider event time, local receive time and position/account state versions before attempting exit.
2. Missing/stale trade stream, unresolved event gap, invalid/stale/crossed book, insufficient displayed L2 depth, or unavailable currency/cost config keeps `STOP_EXIT_PENDING`; set `PROTECTION_UNCERTAIN`, lock that account against new opens, and do not write a fill, fee, close event or flat position.
3. Recovery first replays durable pending triggers and current account/position/ledger versions, then reconciles a fresh book snapshot. If trigger and open position remain valid, create the single deterministic close intent `stop:{position_id}:{source_id}:{trade_id}`. Simulated exit consumes opposing levels by size; no fill if visible depth is insufficient.
4. One DB transaction with unique keys and compare-and-set commits exit fill, paper costs, position close, round-trip closure and ledger events. A competing AI close sees consumed position version and returns existing result/duplicate; stop has priority if its trigger was persisted first. Repeated triggers, restart replay and retries return prior committed result and never double-charge.
5. After unobserved reconnect interval, do not infer that stop was or was not hit from a current last trade. If the adapter cannot prove the relevant persisted event history, keep protection uncertain and lock opens; do not synthesize a historical fill. Binance mark events use the same durable state machine but distinct event identity and price basis.
6. On restart, reconstruct pending stop before enabling decisions; reconcile ledger sequence, position version and unique fill/ledger references. Inconsistency yields `LEDGER_LOCKED`; unproven source interval yields `RECOVERING/PROTECTION_UNCERTAIN`, never `LIVE` or `PROTECTED`.

## One explicit internal paper-cost model

This is an internal deterministic simulation model, not Alpaca fees or an exchange execution promise.

- Entry/exit reference book: one timestamped normalized L2 snapshot; consume asks ascending for buys and bids descending for sells until requested quantity is covered. If the book is exhausted, quote stale or source currency/cost config missing, reject the whole intent; do not fabricate hidden liquidity or partial fills.
- `P_book = Σ(level_price × consumed_qty) / Σ(consumed_qty)`.
- `paper_slippage_rate = σ` is a versioned, user-approved nonnegative decimal buffer. `P_fill = P_book × (1+σ)` for buys; `P_fill = P_book × (1−σ)` for sells. If configured price protection would be breached, reject.
- `paper_fee = abs(P_fill × qty) × fee_rate`; fee rate is the versioned, user-approved internal paper taker rate. Fee applies separately to each entry/exit fill.
- Gross PnL uses simulated fill prices and direction; net PnL is gross PnL less entry/exit paper fees. Alpaca spot funding is `0` with reason `NOT_APPLICABLE_SPOT`; it is not a fetched or estimated rate. No Binance funding is queried or applied in Alpaca mode.
- `σ`, `fee_rate`, cost version/effective time, internal tick/step/min quantity and minimum notional remain TBC. Missing/invalid configuration rejects orders. Never silently default these values to zero.

## Unified position sizing

Once quote and ledger units are compatible and all configuration is approved, use Decimal arithmetic:

`R = 0.02 × E` (E is decision-time equity expressed in the same quote currency as the instrument, after the approved conversion rule, if any).

From a proposed side and stop, simulate the requested quantity through L2 and the configured buffer/fee model on both entry and stop-exit. Let `C_unit` be the resulting worst-case planned loss per BTC including entry-to-stop price loss, book-walk impact, both adverse slippage buffers and both paper fees. Then `q_risk=R/C_unit`; `q_1x=E/P_entry_worst`; `q_margin=M/IM_per_BTC`; `q_book` is visible opposing depth; and `q_filter` is the internal versioned symbol maximum. `q_raw=min(q_risk,q_1x,q_margin,q_book,q_filter)`.

`q=floor(q_raw/step_size)×step_size`. Recompute notional, required margin, total planned loss, costs and visible depth using rounded `q`; accept only when `q>=min_qty`, notional meets approved minimum, `notional<=E`, loss `<=R`, margin `<=M`, and all book/health/config constraints pass. Otherwise reject the intent. There is no upward rounding.

Because V1.0.2's fixed account currency is USDT and Alpaca instrument quote is USD, this formula is specified but not executable until the denomination gate above is resolved. No formula can legitimately compare those raw units as if equal.
