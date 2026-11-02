# Second Round Engineering Remediation Plan

> **For agentic workers:** execute natively under the latest user authorization; do not use real or paid model calls or activate paper trading.

**Goal:** Close the gaps between the frozen V1 requirements and the current implementation for configurable multimodal requests and safely testable paper execution orchestration.

**Architecture:** Keep the provider adapter configuration-driven and disabled by default; enforce image payload shape, budget, model timeout and concurrency before any HTTP request. Add a deterministic orchestrator that can only mutate an explicitly test-scoped ledger in this round, while composing existing market, decision, risk, execution and ledger services. Preserve the current commit's passed evidence and write all second-round outcomes as new artifacts.

**Tech Stack:** Python 3.12, FastAPI, httpx, SQLite, pytest, React/TypeScript/Vitest.

**Spec:** `docs/spec/PRODUCT_REQUIREMENTS_V1_FROZEN.md`, `docs/spec/API_CONTRACT_V1.md`, `docs/spec/STATE_MACHINE_AND_RISK_SPEC_V1.md`, `docs/spec/ACCEPTANCE_MATRIX_V1.md`.

## Global Constraints

- Alpaca BTC/USD spot is read-only; no live order API, account API, or broker paper order API.
- External vision calls require credentials, image-capability verification, and explicit authorization; default configuration is disabled and budget is zero.
- C remains `C_NOT_CONFIGURED`; no user MD content may be invented.
- Test events and fills use a separate temporary database and `TEST_SIMULATION_ONLY` mode, never the formal research ledger.
- Do not overwrite prior reports, screenshots, or acceptance evidence; create second-round reports and retain the prior commit/hash manifest.

## Review Focus

- Text-only or malformed model results must not reach execution; test with mocked image-capable OpenAI-compatible transport and malformed multimodal responses.
- Identical model results and repeated candle delivery must not create duplicate decision or fill; test multiple sequential 15m periods and repeated event replay.
- A stale account version or pending stop must defeat an older active close; test concurrency and stop/close interleavings.
- Stale quotes, missing book depth, disconnected feeds, and timeout must block entry without synthetic execution; test explicit fault fixtures.
- Reconstructed service objects must recover account position and ledger results from the same isolated test database; test restart and balance reconciliation.

### Task 1: Baseline and provenance audit

**Files:** Read-only: tracked source/spec/report files, prior SHA manifests, project and legacy project Git state.

- [ ] Verify current HEAD, clean status, 18 existing tests, six view labels, no tracked `.env`, no secret values displayed.
- [ ] Compare prior checksums; document that hashes identify changed bytes but cannot reconstruct missing pre-hash versions.
- [ ] Keep the existing commit as immutable prior evidence; do not edit prior acceptance reports.

### Task 2: Configurable multimodal adapter

**Files:** Modify `backend/vision.py`, `backend/settings.py`, `.env.example`; create focused adapter tests.

**Interfaces:** A disabled-by-default adapter accepts a base URL, model id, secret environment-variable name, timeout, concurrency, per-call/daily caps, and configured cost estimate; emits one image-bearing request using a data URL; records response model, usage, latency and sanitized errors.

- [ ] Add tests for image-bearing payload, timeout, capability response, concurrency, budget exhaustion, missing credentials and disabled default; verify no external network is used.
- [ ] Add minimal implementation passing those tests; require explicit image capability and nonzero cost estimate plus user authorization gate before actual provider calls.

### Task 3: Test-only paper orchestration

**Files:** Create `backend/orchestration.py`; integrate with existing `backend/vision.py`, `backend/execution.py`, `backend/ledger.py`; add focused tests.

**Interfaces:** `PaperCycleRunner.run_cycle(...)` takes a closed-bar snapshot, market freshness state, isolated temporary `Ledger`, shared image, three independent strategy contexts and decisions. It validates cycle, image hash, mode, MD version, account version, stop, quote/book freshness and idempotency before calling existing risk/fill/ledger primitives.

- [ ] Test redelivery across multiple periods, all three accounts, concurrency, timeout/malformed output, stale feed, stale quote, no book depth, stop priority, duplicate results, restart recovery and exact equity.
- [ ] Keep production construction disabled; the runner must reject a production ledger path unless explicit test scope is selected, and the production app must not start a scheduling loop.

### Task 4: Regression and evidence

**Files:** Create second-round test report, configuration review, implementation review, and final report under `docs/reports/`; update project memory.

- [ ] Run Python + frontend tests, production build, dependency audits and lint.
- [ ] Verify six views with test API data and compare rendered fill markers to actual isolated ledger records.
- [ ] Recompute current SHA manifest; verify all entries; commit the second round.
- [ ] Report external validation as blocked until user supplies credentials/config and separately authorizes a real image call.

## Gaps / self-review

Historical pre-implementation bytes for the six checksum-mismatched paths are not present in the current Git history; the preserved hash manifest cannot restore them. Their exact change provenance will be reported as unknown. Actual vendor image support cannot be truthfully marked verified without an authorized provider capability response/image request; this round only proves request construction and strict fail-closed behavior.
