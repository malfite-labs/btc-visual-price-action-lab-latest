# Quick start (Windows)

Prerequisites: Python 3.12+, Node.js 20+, npm and Git. Use a fresh checkout without existing accounts.

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install 'fastapi>=0.115,<1' 'uvicorn[standard]>=0.34,<1' 'httpx>=0.28,<1' 'websockets>=14,<16' 'pydantic>=2.10,<3' 'pillow>=13,<14' 'pytest>=9.0.3,<10'
Set-Location frontend
npm ci
Set-Location ..
.\.venv\Scripts\python.exe -m pytest tests/test_simple_fill.py tests/test_rule_cycle.py -q
```

These tests use isolated temporary databases. They do not establish that the entire suite passes. Editable installation with `pip install -e .` currently fails because setuptools discovers multiple top-level packages; the command above installs declared dependencies without packaging the project. Full clean-install execution of this guide has not yet been verified.

For a local HTTP API with scheduling and model calls disabled, set `PAPER_AUTOMATION_ENABLED=false`, `VISION_ENABLED=false`, and `VISION_CALLS_AUTHORIZED=false` in the terminal, then run `.\.venv\Scripts\python.exe -m uvicorn backend.app:app --host 127.0.0.1 --port 8011`. This initializes local SQLite state; it is not a read-only demo. Without credentials, market data may be unavailable. In a second terminal run `cd frontend; npm run dev -- --port 5174 --strictPort`; visit http://127.0.0.1:5174/.

For external data, create an ignored file with `ALPACA_API_KEY=` and `ALPACA_SECRET_KEY=` and set `ALPACA_CREDENTIALS_FILE` to its absolute path. A configured backend may connect to Alpaca; use one owner. Model research requires separate credentials, budget and authorization. Do not enable paper automation merely to browse: it can create simulated orders.

Frozen forward configuration and historical datasets require sanitization before distribution. Do not assume a clean checkout can reproduce a prior live session or ledger.
