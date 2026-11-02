# P0 Environment Audit — V1.0.3 implementation

Audit performed in the authorized project directory on 2026-10-27 local time. No files in `C:\Users\admin\Documents\量化` were modified.

| Item | Observed evidence | Result |
|---|---|---|
| Windows | PowerShell on Windows 11 x64, local project path writable | PASS |
| Python | System Python 3.14.5; `uv sync --extra test` selected an isolated CPython 3.12.11 runtime and created project `.venv` | PASS; no global Python modification |
| Node/npm | Node v22.14.0, npm 10.9.2; frontend dependency lock generated | PASS |
| Git | Git 2.54.0.windows.1 available; project repository initialized for this implementation | PASS |
| uv | uv 0.12.4 | PASS |
| Secret file | The explicitly authorized `C:\Users\admin\Documents\量化\.env` exists (287 bytes at check); only ALPACA_API_KEY/ALPACA_SECRET_KEY values were loaded in process memory | Data-only validation succeeded; values never emitted or copied |
| Isolation | `.venv`, npm `node_modules`, SQLite data, `.env`, logs and runtime PIDs are ignored | PASS |
| Bind scope | Launcher binds API/UI to `127.0.0.1`; no public interface binding | PASS |

No trading/account/asset/position API was requested. No order endpoint exists in the implementation. No paid model provider was called. No trading service was run.
