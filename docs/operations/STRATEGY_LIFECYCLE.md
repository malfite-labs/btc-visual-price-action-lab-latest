# Declarative strategy lifecycle

Use `scripts/manage_strategy.py` with the existing strategy pool. A normal JSON strategy uses the existing 8011 API and does not require a backend restart.

## States and gates

`DRAFT → VALIDATED → REGISTERED → TESTED → ENABLED → RUNNING`

`PAUSED`, `FAULTED`, and `ARCHIVED` remain distinct states. `ENABLED_NOT_RUNNING` is not runtime acceptance. A candidate is considered running only after a genuine natural 15-minute cycle updates its `last_cycle` and returns a healthy status.

1. Validate without production changes: `python scripts/manage_strategy.py validate <file.json>`.
2. Upload/register: `python scripts/manage_strategy.py upload <file.json>`. This makes a pre-change online backup first. The existing API validates, creates an independent $10,000 USD_RESEARCH account, and leaves the version disabled.
3. Review registration, version and stored definition: `versions <strategy_id>` and `list`.
4. Test the candidate in the isolated pool database before enabling. Do not use the production account to manufacture test cycles or fills. Preserve the test evidence and the JSON SHA256.
5. Enable only after the isolated checks and pre-change backup pass: `enable <strategy_id> <version>`.
6. After a genuine natural candle, run `check <strategy_id>`. It verifies enabled state, account presence, fresh quote, observed/recent cycle and reports status/action/position. No cycle is synthesized or replayed.
7. Pause with `pause <strategy_id>`. Existing positions remain under their strategy version and quote stop monitoring; this command does not force-close them.
8. Roll back by explicitly activating a previously validated version: `rollback <strategy_id> <version> --confirm-rollback`. A pre-change backup is required.
9. Archive only after pausing and confirming the account is flat: `archive <strategy_id>`. This sets a lifecycle marker and audit event; it does not delete the account, fills, or versions. Archived rows remain visible in `list`, and the cycle runner skips them.
10. Restore an archived strategy with `restore <strategy_id> <version>`. The stored document and digest are revalidated, the account must remain flat, and restoration leaves the strategy disabled (`PAUSED`); run isolated checks and explicitly enable it afterward.
11. Inspect immutable lifecycle history with `GET /api/v1/strategy-pool/<strategy_id>/lifecycle-events`.

The `ARCHIVED` implementation is source-complete and isolated-test verified. It is not active in an already-running 8011 process until a separately approved controlled deployment/reload; do not treat the current service as supporting archive commands before that deployment.

The CLI does not install or execute arbitrary Python strategy code. A strategy needing a new Python execution module requires separate review, isolated tests, and the controlled restart procedure.

## Operational rules

- New definitions default to disabled; registration does not imply activation or running.
- A failed candidate is isolated; do not pause healthy unrelated accounts automatically.
- Do not change the fill model, risk configuration or existing strategy definitions through this workflow.
- For restore/reload, use the verified ledger inventory and the project recovery entry point. Never treat a missing backup as permission to recreate history.
