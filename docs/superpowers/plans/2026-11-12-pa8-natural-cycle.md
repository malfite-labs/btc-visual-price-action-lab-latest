# PA8 Natural Cycle Integration Plan

**Goal:** Dispatch every confirmed 15-minute close to the isolated PA strategy pool in RULE_DRIVEN mode without changing ABC/D/E behavior.

**Architecture:** Add one isolated strategy-pool dispatch helper and call it after the existing ABC paper scheduler completes in the RULE_DRIVEN branch, before that branch returns. Reuse the already validated closed bars and fetched quote; keep the existing non-RULE_DRIVEN hook on the same helper to prevent duplicate code. Let StrategyPool's persisted cycle key provide idempotence and persist each account's last cycle/status/action; log outer dispatch failures with the close time.

**Scope:** `backend/app.py`, focused tests in `tests/test_app_scheduler_wiring.py` and `tests/test_time_exit_gate.py`, and `codex.md`. Existing PA JSON, ABC/D/E rule code, cost model, and execution logic are immutable.

## Steps

1. Add a failing RULE_DRIVEN `process_closed_bar` integration test with an isolated StrategyPool, real 288-bar closed snapshot, shared quote, and mocked ABC scheduler result.
2. Verify the test fails because `strategy_pool.cycle` is unreachable in the RULE_DRIVEN branch.
3. Add the isolated dispatch helper and call it after the ABC scheduler in RULE_DRIVEN, and reuse the helper at the existing non-RULE_DRIVEN location.
4. Verify duplicate bars are idempotent, WAIT results persist cycle/status/action, and a StrategyPool exception leaves the ABC return result intact.
5. Verify invalid/incomplete bars and stale quote gates prevent PA dispatch; retain existing REST/WebSocket watermark dedupe tests.
6. Run the focused integration and strategy-pool suites, then confirm the application module compiles.
7. Back up active databases/code, verify C's live SHORT/stop recovery gate, perform the separately authorized 8011 controlled reload, enable only passing strategies, and wait for a real natural close.

## Acceptance

- `process_closed_bar → strategy_pool.cycle` is exercised by tests in RULE_DRIVEN mode.
- The same close is not recorded twice, and PA errors are logged but isolated from ABC/D/E.
- All eight accounts receive a real natural `last_cycle` after activation; no account state is manually written for acceptance.
- Exactly one shared Alpaca stream remains connected, ABC/D/E configuration and C's position/stop remain intact, and live trading stays disabled.
