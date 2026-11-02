# Visual Prompt Integrity and Paper Scheduler Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Remove arbitrary prompt byte limits without modifying MD, enforce provider-native/request/cost limits, and deliver an inactive-by-default, testable paper-cycle scheduler with offline evidence.

**Architecture:** Keep prompt construction as a lossless pure function. Make provider configuration own output-token and call-budget caps, reject real provider limits explicitly, and keep the production app scheduler disabled unless all gates are explicitly enabled. Test scheduler behavior against synthetic data and an isolated ledger only.

**Tech Stack:** Python, pytest, SQLite, httpx, React/TypeScript tests, existing FastAPI application.

**Spec:** User authorization in current task; `docs/spec/ENGINEERING_SPEC_V1_REVISED.md`; `docs/spec/STATE_MACHINE_AND_RISK_SPEC_V1.md`; `docs/spec/ACCEPTANCE_MATRIX_V1.md`.

## Global Constraints

- No real trading/account API calls and no formal unattended paper trading.
- No DeepSeek API calls unless a newly rotated key is safely verifiable; this turn will fail closed if rotation evidence is unavailable.
- Never truncate, summarize, or rewrite strategy MD to satisfy a size check.
- Keep scoring ground truth outside provider inputs; preserve leak detection and API-key checks.
- Keep C unconfigured and do not change the legacy project.
- No test-only fills in the production ledger.

## Review Focus

- Arbitrarily long strategy MD must be byte-for-byte present in the prompt.
- Provider context/request limits must fail explicitly without truncating MD.
- Cost reservation must cap requests and maximum completion tokens before network access.
- Duplicate closed-candle events must not create duplicate decisions or fills.
- Model/config/data gates must prevent startup execution by default.

---

### Task 1: Lossless prompt and blinded preflight

**Files:** `backend/vision_prompt.py`, `tests/vision_eval_runner.py`, `tests/test_vision_preflight.py`

**Interfaces:** `build_decision_prompt(...) -> str`; `build_preflight_plan(...) -> (evaluations, evidence)`.

- [x] Replace the old byte-limit test with long multilingual MD preservation and answer-leak positive/negative tests; verify it fails against current implementation.
- [x] Remove only arbitrary byte thresholds; retain allow-list, credential-shape, ground-truth and image/MD hash checks.
- [x] Verify 12/12 preflight cases pass offline and the exact MD appears unmodified.

### Task 2: Provider limits and bounded spend

**Files:** `backend/vision_provider.py`, `.env.example`, `tests/test_vision_adapter.py`

**Interfaces:** `VisionProviderConfig.max_output_tokens`, `max_requests_per_day`; durable budget reservation.

- [x] Add tests for configurable max output tokens, duplicate request/call-count exhaustion, provider context error, HTTP request body cap, and `finish_reason=length`.
- [x] Reserve conservative cost using configured peak input/output rates and output-token ceiling; fail closed if rate, per-call cap, request cap, or daily cap is absent.
- [x] Keep complete MD untouched and map supplier context/size rejection to an explicit safe status.

### Task 3: Inactive-by-default cycle scheduler

**Files:** new `backend/scheduler.py`, `backend/app.py`, `tests/test_scheduler.py`, `tests/test_orchestration_e2e.py`

**Interfaces:** scheduler accepts closed candles, closed-candle metadata, already-rendered shared image bytes, adapter, isolated strategy/ledger dependencies, and fresh quote/book providers; the app does not wire or start it in this round.

- [x] Test closed-candle idempotency, same-image hash for A/B/C, independent strategy/account state, outage denial, no-config denial, restart/stop recovery, and isolated-ledger fills.
- [x] Implement an explicit gated scheduler that schedules only once per UTC 15-minute close and is not automatically activated by app lifespan.
- [x] Keep stop watcher independent and prioritize pending stop exits; no test result writes to the project’s formal ledger.

### Task 4: Frontend/ledger regression and delivery evidence

**Files:** existing chart tests, `docs/reports/PROMPT_AND_SCHEDULER_REMEDIATION_V1.md`, `README.md`, `codex.md`, `docs/SHA256SUMS.txt`.

- [x] Assert frontend markers project only persisted ledger fills; verify strategy/image trace survives controlled execution.
- [x] Run Python, frontend, lint, build and hash-manifest checks with provider key unset and no live services.
- [x] Record real vision blocked state, zero calls/cost, scheduler disabled state, actual test results and remaining gates.
- [x] Update codex memory with Rule/Checklist/Anti-pattern and regenerate/round-trip verify the manifest.


**Scope result:** The persisted scheduler and cost-gated runner are implemented and controlled tests pass. Automatic wiring from the market polling loop into the scheduler is intentionally still a readiness blocker; FastAPI keeps the scheduler disabled. Real DeepSeek calls remained blocked because rotated-key provenance was not verifiable.
