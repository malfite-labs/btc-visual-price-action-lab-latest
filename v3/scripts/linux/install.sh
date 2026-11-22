#!/usr/bin/env bash
# Install Python venv + frontend deps for Linux. Does not start services or Alpaca.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "${SCRIPT_DIR}/common.sh"

cd "$APP_ROOT"
echo "[install] APP_ROOT=$APP_ROOT"
echo "[install] Python: $(python3 --version)"
echo "[install] Node: $(node --version) npm $(npm --version)"

if [[ ! -x "$VENV_PY" ]]; then
  python3 -m venv .venv
fi
.venv/bin/python -m pip install -U 'pip>=25' 'setuptools>=70' 'wheel>=0.43'
# Editable install is known-broken (multi top-level packages); install declared deps directly.
.venv/bin/python -m pip install \
  'fastapi>=0.115,<1' 'uvicorn[standard]>=0.34,<1' 'httpx>=0.28,<1' \
  'websockets>=14,<16' 'pydantic>=2.10,<3' 'pillow>=13,<14' \
  'pytest>=9.0.3,<10' 'ruff>=0.9,<1'

cd "$APP_ROOT/frontend"
if [[ -f package-lock.json ]]; then
  npm ci
else
  npm install
fi
npm run build

# Tighten secrets
find "$APP_ROOT" -type f -name '*.env' -exec chmod 600 {} \;
chmod 700 "$APP_ROOT" 2>/dev/null || true

echo "[install] DONE"
.venv/bin/python -c 'import fastapi,uvicorn,httpx,websockets,pydantic,PIL; print("py_ok", fastapi.__version__, PIL.__version__)'
test -f "$APP_ROOT/frontend/dist/index.html"
echo "[install] frontend dist present"
