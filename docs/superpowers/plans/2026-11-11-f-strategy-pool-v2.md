# F Strategy Pool v2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** Add backward-compatible v2 support that faithfully represents and simulates both frozen F BTC/USD 15m candidates, and deliver two validated JSON files that register paused.

**Architecture:** Keep v1 validation/evaluation/position handling unchanged. Add a bounded v2 AST and a `position` execution object for direction, next-open entries, swing/ATR stops, R target, holding limit, and same-bar priority. Apply v2 behavior to isolated backtests and the strategy-pool paper lifecycle without deploying, restarting services, uploading, or enabling strategies.

**Tech Stack:** Python, Decimal, SQLite, pytest, JSON.

**Spec:** `docs/superpowers/specs/2026-11-11-f-strategy-pool-v2.md`

## Global Constraints

- Preserve the existing uncommitted workspace changes; modify only strategy-pool files, new tests, two F JSON documents, the approved spec/plan, and `codex.md`.
- Keep v1 schema and behavior backward-compatible.
- Keep v2 data-only: no eval/exec, imports, or user-provided code.
- Keep uploaded F documents paused by default. Do not upload, enable, deploy, restart services, write production ledgers, or submit orders.
- Use temporary databases and synthetic bars in tests; historical gaps split simulations and cannot carry positions.
- Reuse the existing `CostModel` and `calculate_position_size`; preserve 2% account risk, 5x cap, 0.05% per-side taker fees, 2bps adverse slippage, and the target-covers-fees gate.
- F 15m parameters: breakout 40-bar high/low and 40% body; pullback 3-bar SMA20 touch and 60% body; both SMA20/60 with five-bar SMA20 slope, ATR14, stop buffer 0.1 ATR, stop distance 0.6–2.5 ATR, target 2R, max hold 192 bars.

## Interfaces

- v2 series operators: `open/high/low/close/volume:[offset]` where offset 0 is the latest closed bar; `sma:[period,offset]`; `atr:[period,offset]`; `highest_high/lowest_low:[period,offset]` over a window ending `offset` bars before the latest; `body_fraction:[offset]`.
- v2 entry expression: `{ "long": <boolean AST>, "short": <boolean AST> }`.
- v2 `position`: `{ "direction":"both", "entry_timing":"next_open", "stop":{"kind":"swing_atr","lookback":3,"atr_period":14,"buffer_atr":0.1,"min_distance_atr":0.6,"max_distance_atr":2.5}, "target_r":2, "max_hold_bars":192, "same_bar_priority":"stop" }`.
- Existing `evaluate(expr,bars,params,previous_bars=None)` remains the v1 entry point; add a v2 evaluator returning a direction (`LONG`, `SHORT`, or `None`) and a stop-construction helper returning an exact Decimal stop or a stable rejection reason.

## Review Focus

- v1 fixture accepts and evaluates identically before and after the change.
- v2 indicators use only prior/current closed bars with no look-ahead; lookback bounds include offsets.
- Stop placement is based on signal bar plus two prior bars, ATR14, and the signal-close distance gate; entry uses the next bar open.
- Long and short sizing, fees, slippage, target and stop gap handling use the correct executable side.
- Same-bar stop and target chooses stop; time exit occurs on the 192nd held bar; gaps censor/reset positions and warm-up.
- Paused strategy entries do not execute; existing v1 positions retain v1 lifecycle.
- Both committed JSON fixtures pass `validate_strategy` and exactly encode the approved first-round F candidate rules.

### Task 1: Add v2 schema and causal indicator expressions

**Files:**
- Modify: `backend/strategy_pool.py`
- Test: `tests/test_strategy_pool_v2.py`

**Interfaces:** Uses existing v1 `validate_strategy` and `evaluate`; produces v2 `validate_strategy`, expression evaluation, `evaluate_entry_v2(doc,bars)` and `build_v2_stop(doc,direction,bars)`.

- [x] Write failing tests for v2 schema acceptance/rejection, bounds, long/short breakout and pullback expressions, exact SMA/ATR/body/high-low values, and no look-ahead.
- [x] Run the focused tests and confirm expected failures against the missing v2 interface.
- [x] Implement the bounded v2 AST and validators, keeping v1 branches unchanged.
- [x] Run the focused test file and existing v1 strategy-pool tests.

### Task 2: Add v2 directional position and exit handling

**Files:**
- Modify: `backend/strategy_pool.py`
- Test: `tests/test_strategy_pool_v2.py`

**Interfaces:** Consumes `evaluate_entry_v2` and `build_v2_stop`; extends versioned position JSON with direction, target, held-bar count, and pending next-open state. Existing v1 positions remain readable and keep their prior shape/logic.

- [x] Write failing tests for long/short next-open entries, risk/leverage sizing, target fee gate, STOP/TARGET/TIME outcomes, gap censoring, same-bar stop priority, quote-side stop/target management, and v1 compatibility.
- [x] Run focused tests and confirm expected failures.
- [x] Implement shared direction-aware sizing/closing, v2 next-open lifecycle, OHLC backtest exits, quote monitor handling, and gap segmentation.
- [x] Run focused tests plus existing strategy-pool core/API tests.

### Task 3: Deliver and verify both F candidate documents

**Files:**
- Create: `config/strategy_pool/f-trend-breakout-v1.json`
- Create: `config/strategy_pool/f-trend-pullback-v1.json`
- Modify: `tests/test_strategy_pool_v2.py`
- Modify: `codex.md`

**Interfaces:** Uses the approved v2 schema and execution helpers from Tasks 1–2.

- [x] Add tests loading both JSON documents and assert their key rules and exact parameters.
- [x] Run tests and confirm both documents initially fail until added.
- [x] Add the two documents with descriptive IDs and paused-on-upload-compatible content.
- [x] Run focused tests, the full strategy-pool suite, and the project test suite; run Ruff/compile checks used by the repository.
- [x] Re-read all final diffs and the approved spec; verify no services, production DBs, orders, or unrelated files changed; update `codex.md` with results and any root cause/Rule/Checklist/Anti-pattern.

## Workspace Decision

Work in the existing checkout. The project already contains extensive uncommitted changes needed for this feature context; creating a separate checkout would omit those changes. Preserve them and review only this plan's files.
