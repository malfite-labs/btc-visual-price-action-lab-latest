# Vision Chart Clarity Implementation Plan

**Goal:** Produce a higher-clarity offline chart candidate and blind comparison plan while preserving original fixtures, OHLC and A/B strategy MDs.

**Architecture:** Keep the current renderer available as `legacy`; add an explicitly selectable `v2` renderer for candidate charts. The fixture generator creates a separate `layout_v2` directory, while preflight and comparison builders keep fixture labels outside `ProviderInput`. The app default and all market, ledger, Alpaca and scheduler code remain untouched.

**Tech Stack:** Python, Pillow, pytest, existing BTC Lab chart and fixture modules.

**Spec:** Current user request; baseline `e1dc581af1490add328c4198e531e5dbcb60a57d`.

## Global Constraints

- Preserve the 288-candle global and 96-candle local windows.
- Do not alter fixture OHLC or existing answer labels.
- Pass the full original A/B MD and unchanged decision schema.
- Do not call any real model API or start paper trading.
- Do not modify Alpaca, ledger, frontend or scheduler core modules.

## Review Focus

- The optimized panel identifiers and UTC time ticks are visible at original resolution.
- Legacy and candidate variants hash the exact same global OHLC selection.
- The candidate has enough horizontal pixels for 288 global candles.
- The disputed global-direction label is flagged without rewriting blind answers.
- Repeated comparison items carry identical image, prompt and MD hashes.

---

### Task 1: Add chart and blind-plan regression tests

**Files:**
- Modify: `tests/test_charts.py`
- Modify: `tests/test_vision_fixtures.py`
- Create: `tests/test_vision_comparison_plan.py`

- [ ] Verify explicit 1920x1280 v2 layout metadata, both panel windows, UTC axis ticks, price scales and unchanged legacy output.
- [ ] Verify all original/v2 OHLC hashes match while image hashes differ.
- [ ] Verify 10 comparison requests (original/v2 range and reversal A/B plus repeated v2 reversal A/B), with repeated request inputs identical and labels absent from provider inputs.
- [ ] Run targeted tests and confirm the missing renderer/plan behavior fails.

### Task 2: Implement selectable chart variant and hierarchy prompt

**Files:**
- Modify: `backend/charts.py`
- Modify: `backend/vision_prompt.py`
- Modify: `tests/vision_fixture_factory.py`
- Modify: `tests/vision_eval_runner.py`
- Create: `tests/vision_comparison_plan.py`

- [ ] Add `layout_version="legacy"|"v2"`; keep legacy as the production default.
- [ ] Render v2 with explicit ASCII GLOBAL/LOCAL labels, each panel's candle count/range, independent USD scale, UTC timestamps, and 1920x1280 canvas.
- [ ] Keep source candles and A/B MD unchanged; generate v2 fixtures in a new directory.
- [ ] Add prompt instructions to assess global, local and their relationship in that order without exposing any ground truth.
- [ ] Build a deterministic blind comparison plan with distinct evaluator-only labels and exact duplicate input hashes for repeat cases.

### Task 3: Audit, validate and report

**Files:**
- Create: `docs/reports/VISION_CHART_LAYOUT_AUDIT_2026-10-27.md`
- Modify: `codex.md`

- [ ] Preserve the case_04 labels and report the global-direction ambiguity from the OHLC statistics.
- [ ] Run the full Python and frontend test suites, lint, formatting and frontend build.
- [ ] Confirm no API calls, trading, or protected-module changes.
- [ ] Record future call/cost estimate and the disputed-label test gate.
- [ ] Commit the report, tests and candidate renderer.
