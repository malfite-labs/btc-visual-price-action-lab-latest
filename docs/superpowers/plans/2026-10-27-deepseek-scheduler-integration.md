# DeepSeek and Alpaca Scheduler Integration Plan

## Goal

Run the already authorized bounded DeepSeek vision evaluation and connect the persistent 15-minute paper scheduler to the existing read-only Alpaca market loop. Preserve the real-trading prohibition, disabled-by-default execution gate, isolated test ledgers, and C_NOT_CONFIGURED state.

## Steps

1. Reconfirm `codex.md`, clean Git baseline, ignored local secret handling, fixture separation, existing scheduler/runner contracts, and official DeepSeek image/pricing limits.
2. Add failing app integration tests for disabled default, complete closed-bar dispatch with one shared composite image and isolated strategy/account state, duplicate cycle suppression, and stop processing independent of AI.
3. Implement environment loading of only the project-local DeepSeek variable, initialize disabled-by-default paper runner/scheduler, dispatch eligible closed Alpaca bars, and run stop/recovery handling on fresh quotes.
4. Run offline tests, static checks, frontend regression/build, and inspect live Alpaca REST/WebSocket market-only status; keep execution unarmed.
5. Store the user-authorized DeepSeek key only in ignored project `.env`; run the 12 blinded image evaluations one-by-one with durable request/cost reservation and stop on any anomaly or cap.
6. Produce sanitized evidence/report, update `codex.md`, regenerate and validate the SHA-256 manifest outside the repository, run final checks, and commit only secret-free source/tests/docs.

## Safety gates

- Never call trading/account endpoints or use external order APIs.
- Never start the scheduler in autonomous mode; `PAPER_AUTOMATION_ENABLED` and cost approval remain false.
- No fixture answer or case label may enter a provider request.
- At most 12 authorized vision calls and at most USD 0.10 reserved total; no retry after any error.
- C remains unconfigured. Test fills, if any, stay in temporary test databases and do not enter formal research data.
