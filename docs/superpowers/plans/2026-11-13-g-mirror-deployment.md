# G Mirror Strategy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add G V1.0 as an independently accounted, quote-executed inverse follower of committed C fills, deploy it into paper execution, and show it in the existing desktop console.

**Architecture:** `Ledger` stores C fill snapshots and a durable G mirror outbox in the same transaction as each C fill. A post-commit listener sends events to `GMirror`, which persists independent G account/fill/pair/event state in `data/g/mirror.sqlite3` and deduplicates by C fill ID. The existing quote handler monitors G emergency stops. Read-only `/api/v1/g-mirror/*` endpoints feed the desktop client.

**Tech Stack:** Python, SQLite, FastAPI, Tkinter/ttk, pytest; existing `CostModel`, simulated tick/quantity step, and Alpaca quote stream.

**Spec:** `docs/superpowers/specs/2026-11-13-g-mirror-design.md`

## Global Constraints

- Do not change C signals, risk settings, stop/exit choices, existing matching model, or ABCDE/PA8 behavior.
- C's current trade is flat after its recorded `RULE_TIME_EXIT`; do not replay it. G watches only new C fills after persisted activation time.
- G exact-copy quantity; reject if precision, quote, fee reserve, 2% emergency stop risk, margin or 5x maximum fails.
- A G failure must not propagate into or roll back C. G closes only against its paired C trade and never clears fills by observing a flat account.
- Use the existing Alpaca quote connection; no backtest, no production test order, no real trading.
- Any 8011 reload is limited to one, after a verified backup and safe restart preflight; reuse the project's existing recovery entry without fabricating the missing `freeze.json`.
- Preserve all pre-existing uncommitted workspace changes.

## Files and Interfaces

- Create `backend/g_mirror.py`: `GMirror(db_path, cost=CostModel(), initial_equity=Decimal('10000'), risk_fraction=Decimal('0.02'), max_leverage=Decimal('5'))`; `process_c_fill(event) -> dict`; `monitor_quote(quote) -> dict`; `status() -> dict`; `fills(limit=...) -> list[dict]`.
- Modify `backend/ledger.py`: add `mirror_outbox(source_fill_id PRIMARY KEY, payload_json, status, result_json, updated_at)`, an optional post-commit listener, and `execution_quote` plumbing on C-only fill emission. Existing C ledger fills/business calculations remain unchanged.
- Modify C call sites in `backend/orchestration.py`, `backend/rule_cycle.py`, and `backend/execution.py` to pass the same validated execution quote into the durable outbox. Preserve quote-driven stops and close priority.
- Modify `backend/app.py`: instantiate G on its separate database, attach listener, recover pending post-activation outbox events, monitor G on existing quote events, and expose read-only status/fill endpoints. Catch and log G exceptions at the integration boundary.
- Modify `scripts/local_dashboard.py`: insert G after E and before PA; read G via its own status/fills endpoints; add G search/category/detail rendering; keep G state switch read-only and leave all existing actions untouched.
- Tests: `tests/test_g_mirror.py`, `tests/test_g_mirror_api_isolated.py`, and additions to `tests/test_desktop_console.py`.

## Review Focus

1. Source outbox commit succeeds with each accepted C fill and never on a rejected/duplicate source fill — test atomic insertion and same-C idempotency.
2. Crash/restart after G commit but before outbox acknowledgement cannot duplicate a G fill — test repeated dispatch against a reopened G database.
3. Invalid/stale quote, quantity step, insufficient margin and risk-stop rounding reject the whole mirror — test boundary values and preserve account.
4. G risk exit followed by C close cannot reopen G; partial fill sequence changes only the paired quantity — test event ordering and terminal pair state.
5. Any G consumer exception leaves C's already committed fill/account unchanged and does not fail the C cycle — test throwing listener/processor path.

## Task 1: Independent G Mirror Ledger and Event Rules

**Files:** create `backend/g_mirror.py`, `tests/test_g_mirror.py`.

- [ ] Write RED tests for exact reverse direction/quantity and quote-side execution with slippage/fees.
- [ ] Run `pytest tests/test_g_mirror.py -q`; expected initial failure is missing `GMirror`.
- [ ] Implement isolated schema, activation epoch, event/pair/fill persistence, exact quantity/margin/risk checks, partial fill accounting, emergency stop and status reporting.
- [ ] Add tests for invalid quote, precision, risk and margin rejection, duplicate source IDs, partial fills, emergency exit terminal state, and reopening the DB.
- [ ] Re-run `pytest tests/test_g_mirror.py -q`; all tests pass and balances/fees/PnL reconcile from fills.

## Task 2: Durable C Fill Outbox and Quote Propagation

**Files:** modify `backend/ledger.py`, `backend/orchestration.py`, `backend/rule_cycle.py`, `backend/execution.py`; extend `tests/test_g_mirror.py` or add `tests/test_ledger_mirror_outbox.py`.

- [ ] Write RED tests proving accepted C open/close inserts exactly one quote-bearing outbox event in the same transaction; rejected, duplicate and non-C fills do not publish a mirror event.
- [ ] Run the targeted test and confirm it fails for absent outbox/listener behavior.
- [ ] Add optional `execution_quote` argument propagation to actual C open/close routes, including confirmed stop, stop recovery, liquidation and RULE_DRIVEN target/time exits.
- [ ] Dispatch listener only after commit, isolate and log listener exceptions, persist dispatch result, and allow safe retry by source fill ID. Do not change any existing fill math or return contract.
- [ ] Run the new test plus `pytest tests/test_ledger.py tests/test_execution.py tests/test_rule_cycle.py -q`.

## Task 3: App Event Integration, Recovery and Read-Only API

**Files:** modify `backend/app.py`; create `tests/test_g_mirror_api_isolated.py` and/or `tests/test_g_mirror_integration.py`.

- [ ] Write RED isolated integration tests that submit C fills to a temp ledger and verify app-facing dispatch/status/fills without production paths or Alpaca network access.
- [ ] Add the G instance on its separate file, attach/replay outbox events after activation, invoke `monitor_quote` in the existing quote event path, and expose GET status/fill endpoints.
- [ ] Catch G failures so C quote handling and cycle results remain intact; do not add a WS, scheduler, or C inference call.
- [ ] Test both inverse directions, paired C close, stop-quote close, duplicate outbox replay, status shape, independent DB and injected G failure.
- [ ] Run `pytest tests/test_g_mirror.py tests/test_g_mirror_api_isolated.py tests/test_g_mirror_integration.py -q` and `py_compile` on touched backend files.

## Task 4: Desktop G Row and Account Detail

**Files:** modify `scripts/local_dashboard.py`, `tests/test_desktop_console.py`.

- [ ] Write RED tests for 14-row order, dynamic totals (5+1+8), separate G filter, WAIT/open/fault status mapping, G history endpoint, and API-failure isolation.
- [ ] Add G to console snapshot/view model from the read-only API; preserve ABCDE and PA IDs/categories and ensure the G switch is read-only (no POST callback).
- [ ] Add G detail rendering for source trade, C/G directions, quantity/price/time/delay, G stop, last mirror event/reason and fills.
- [ ] Run desktop tests, `py_compile scripts/local_dashboard.py`, and `scripts/local_dashboard.py --check` against the isolated integration endpoint where possible.

## Task 5: Backup, Controlled Reload, Activation and Live Verification

**Files:** create deployment evidence under a unique `logs/g-mirror-deployment-<UTC>/` folder; update `codex.md`.

- [ ] Create a verified SQLite online backup of paper, PA pool, D/E, controls and any existing G DB; record code/config copies without touching existing workspace changes.
- [ ] Preflight current C state is FLAT or its persisted stop/position is fully recoverable; C has no pending stop, incomplete cycle/decision, or mismatched open fill; inspect all open accounts and startup monitors; validate recovery prerequisites and one listener/stream.
- [ ] If safe and required, stop only the identified 8011 process and invoke the project's existing recovery function once. If any C/G protection preflight fails, stop deployment and leave G disabled.
- [ ] Verify health, live trading disabled, single Alpaca WS, C/ABCDE/PA8 state unchanged, G registered/enabled, G initial equity and `WAIT_FOR_C_NEXT_OPEN` (or valid waiting state), and no production fills fabricated.
- [ ] Launch/refresh only the desktop client; inspect all 14 rows, G detail and PA category; run final scoped tests and record actual commands/results, root cause, Rule, Checklist and Anti-pattern in `codex.md`.

**Completion contract:** all safety tests pass; G is enabled with its own $10,000 USD_RESEARCH account; first new C fill is the first eligible mirror; all route/dashboard GETs read actual production state; one existing Alpaca stream; no historical replay, synthetic production orders, or C mutation.
