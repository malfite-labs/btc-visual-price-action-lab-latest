#!/usr/bin/env bash
# Linux counterpart of scripts/recover_backend.ps1 — restart offline-safe backend only.
# Does NOT arm paper automation. Does NOT clear LONG_PROTECTION.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/common.sh"
export PAPER_AUTOMATION_ENABLED=false
export BTC_LAB_OFFLINE="${BTC_LAB_OFFLINE:-1}"
"${SCRIPT_DIR}/stop.sh" backend || true
"${SCRIPT_DIR}/start.sh" backend
