# Alpaca Connection Manager and DeepSeek Vision Evaluation Plan

> **For agentic workers:** Use the approved implementation and verification workflow task by task. Keep the real paper scheduler disabled throughout.

**Goal:** Enforce one Alpaca stream owner per local BTC Lab project, avoid retries on Alpaca 406, and run the approved bounded DeepSeek synthetic-chart evaluation with durable sanitized evidence.

**Architecture:** The existing `AlpacaMarketData` instance remains the sole provider adapter. Add an in-process entry guard and OS-level nonblocking lease, make 406 a terminal blocked state for the current process, and wrap app background tasks in reliable lifespan cleanup. Extend the explicit vision evaluator to persist diagnostics and score only blinded manifest-backed fields; keep request and cost reservations durable and fail closed.

**Tech Stack:** Python 3.12, asyncio, FastAPI lifespan, SQLite, httpx, pytest, Windows `msvcrt` file locking with POSIX `fcntl` fallback.

**Spec:** This user request; project contracts in `docs/spec/API_CONTRACT_V1.md`, `docs/spec/STATE_MACHINE_AND_RISK_SPEC_V1.md`, `docs/spec/ACCEPTANCE_MATRIX_V1.md`; current parser/stream audit in `docs/reports/DEEPSEEK_ALPACA_HEALTH_AUDIT_2026-10-27.md`.

## Global Constraints

- No real order/account endpoint, no formal simulated trading, and paper scheduler gates remain false.
- Alpaca market source remains BTC/USD spot; no source switch and no 2,000 ms quote-freshness threshold change.
- DeepSeek model remains `deepseek-flash`; at most 12 requests and USD 0.10 total reserved cost; stop after any anomalous response until its evidence is assessed.
- Ground truth stays outside the model request; A/B use their full separate MDs, same fixture image bytes, and the strict existing JSON decision contract.
- C remains `C_NOT_CONFIGURED`; old quant project files are not read or modified.

## Review Focus

- Duplicate app/task startup: test the same adapter and competing local app instances; exactly one provider socket may be created.
- 406 backoff: assert no retry socket after connection-limit response; ordinary transport loss still reconnects with exponential backoff.
- Lifespan cancellation: assert provider context exits, stream status becomes `STOPPED`, and the OS lease is released.
- Evaluator failure evidence: persist status, request ID, model/fingerprint, finish reason, usage, cost estimate, response hash, and redacted excerpt before stopping.
- Scoring integrity: keep answer keys evaluator-only; score trend and structure only against declared manifest fields, while recording unscored action/stop judgment rather than inventing truth.

---

### Task 1: Single-owner Alpaca WebSocket lifecycle

**Files:**
- Modify: `backend/market.py`
- Modify: `backend/app.py`
- Test: `tests/test_market_diagnostics.py`
- Test: new/extended `tests/test_market_lifecycle.py`

**Interfaces:**
- `AlpacaMarketData(..., stream_lock_path: Path | None = None)` acquires one local adapter guard and one process lease before the only `websockets.connect` call.
- A 406 returns `CONNECTION_LIMIT_BLOCKED` without retry; transport failures retain exponential retry; stop/cancel closes the context and releases the lease.
- App lifespan creates one quote manager, rejects duplicate lifespan startup, and always cancels/gathers/clears background tasks in `finally`.

- [x] Write failing tests for same-instance duplicate entry, cross-instance lock contention, 406 no-retry, ordinary reconnect, and graceful stop.
- [x] Run the targeted tests and confirm they fail for the intended lifecycle behavior.
- [x] Implement the single-owner lease and terminal 406 handling without adding a provider connection path.
- [x] Run the targeted tests, then the full Python suite.

### Task 2: Durable bounded real-vision evidence and scoring

**Files:**
- Modify: `tests/run_real_vision_eval.py`
- Modify: `tests/test_real_vision_eval_budget.py`
- Test: new evaluator evidence/scoring tests as needed
- Output: ignored `data/vision-eval-run.json`

**Interfaces:**
- Offline preflight returns all 12 prepared blinded inputs before any network call.
- Each attempt reserves a durable request and worst-case cost before HTTP, then atomically persists one sanitized result record.
- Any HTTP/parser/schema/usage anomaly persists evidence and stops the run. Valid decisions can proceed to the next distinct fixture; invalid actions/stops are never executed.
- Scores include global/local trend and structure classification only where the manifest has explicit ground truth; action/stop placement and A/B style remain descriptive unless ground truth exists.

- [x] Add tests for sanitized atomic response evidence and scoring false positives/negative phrasing.
- [x] Verify all 12 offline image/MD/hash/leak/schema checks, prior call count, and the USD 0.10 worst-case budget before external access.
- [x] Implement sanitized atomic evidence persistence and explicit scored/unscored metrics.
- [x] Run offline evaluator and full test suite with network calls mocked/disabled.

### Task 3: Controlled real read-only validation and delivery

**Files:**
- Create/update: `docs/reports/ALPACA_CONNECTION_DEEPSEEK_VISION_REVIEW_2026-10-27.md`
- Modify: `codex.md`
- Modify: `docs/SHA256SUMS.txt`

- [x] Check official DeepSeek image, thinking/response, pricing docs and verify budget arithmetic.
- [x] Confirm no BTC Lab residual process; verify live read-only Alpaca data and perform one controlled disconnect/reconnect, stopping on 406.
- [x] Run the blinded evaluator under durable count/cost limits; assess each truncated response before deciding whether another request was justified.
- [x] Re-run full pytest (116 passed), Ruff/format, manifest verification, and staged secret scan; commit only sanitized evidence and project code.

### Delivery evidence

- Actual Alpaca recovery used a single manager with one deliberate socket drop; it reconnected once, delivered live quote events and shut down with the lease released.
- DeepSeek used 6 calls total: 4 valid decisions, two token-truncated failures. The aggregate usage-based peak-rate estimate stayed below budget; the full visual suite remains incomplete and is not paper-trial ready.
- Final evidence and limitations are recorded in `docs/reports/ALPACA_CONNECTION_DEEPSEEK_VISION_REVIEW_2026-10-27.md`.
