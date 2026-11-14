# Desktop Console PA8 Sync Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Extend the existing Windows paper console from ABCDE to live-read all 13 strategy accounts without changing trading services.

**Architecture:** Keep the Tkinter desktop app and its ABCDE snapshot source. Add a separate read-only strategy-pool fetch, merge the eight fixed strategy IDs into the existing table, and use existing enable/fills endpoints for confirmed controls and details. Keep PA failures isolated from ABCDE reads.

**Tech Stack:** Python 3.12, Tkinter, urllib JSON API, pytest.

**Spec:** User request on 2026-11-12, “升级 BTC 桌面纸盘控制台，同步显示全部13个策略”.

## Global Constraints

- Do not restart backend 8011 or alter market/WebSocket processes.
- Do not change any strategy rules, accounts, enabled flags, fills, or positions during implementation.
- PA rows use actual registered strategy IDs and read `/api/v1/strategy-pool`.
- PA details read existing `/fills` API; PA controls call existing `/enable` only after confirmation.
- Desktop app must not open a market WebSocket.
- Preserve ABCDE data sources and existing column order.

## Review Focus

- Strategy pool endpoint unavailable while ABCDE endpoints remain healthy: preserve ABCDE and show PA-specific errors.
- Flat vs pending/open PA positions: show zero amount/flat correctly and do not imply 5x is currently used.
- Statuses `RUNNING`, `ENABLED_NOT_RUNNING`, `PAUSED`, `FAULTED`: display distinct Chinese labels.
- UTC cycle timestamps: convert to local timezone for table and detail views.
- Existing PA enabled flags: refresh/startup must be read-only and must not change them.

### Task 1: Add tested PA row mapping and detail view

**Files:**
- Modify: `scripts/local_dashboard.py`
- Test: `tests/test_desktop_console.py`

- [ ] Test row names, real IDs, status distinctions, flat position, 5x cap label, and local cycle formatting.
- [ ] Test PA API errors do not prevent ABCDE snapshot rows.
- [ ] Add PA current-position summary and existing fills API detail table.
- [ ] Add confirmed enable/pause controls using the registered ID and active version.
- [ ] Add 13-row scrollable/resizable table and include PA data in manual and 15-second refresh.

### Task 2: Verify running desktop client

**Files:**
- Update `codex.md` with Rule, Checklist, Anti-pattern, validation and retrospective.
- Deliver screenshot under this task's log directory.

- [ ] Run focused pytest, py_compile, existing desktop `--check`, and manual GUI screenshot verification.
- [ ] Restart only the desktop client; verify backend process identity/state is unchanged.
- [ ] Verify 5 ABCDE + 8 PA rows and each PA row against real API IDs/state.
