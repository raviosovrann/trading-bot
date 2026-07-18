#!/usr/bin/env bash
# Build the SPA and serve it (+ the API) from a single uvicorn on :8000 for the
# Playwright smoke. Uses a throwaway data dir seeded with one operator so the
# test can log in. No network venue is touched (the smoke only creates a
# dry-run bot).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"

# Prefer the repo virtualenv (it has fastapi/uvicorn/cryptography); fall back to
# whatever python is on PATH.
PY="python"
[ -x "$REPO_ROOT/.venv/bin/python" ] && PY="$REPO_ROOT/.venv/bin/python"

DATA_DIR="$(mktemp -d)"
export TRADINGBOT_DATA_DIR="$DATA_DIR"
export TRADINGBOT_UI_DIST="$REPO_ROOT/ui/dist"
export PYTHONPATH="$REPO_ROOT/src"
export TRADINGBOT_SECRETS_KEY="$($PY -c 'from tradingbot.service.crypto import generate_key; print(generate_key())')"

# Seed one operator: username "operator", password "e2e-pass".
$PY - "$DATA_DIR" <<'PY'
import json, sys, pathlib
from tradingbot.service.auth import hash_password
data = {"users": [{"username": "operator", "password_hash": hash_password("e2e-pass")}]}
pathlib.Path(sys.argv[1], "users.json").write_text(json.dumps(data))
PY

# Always rebuild the SPA so the smoke runs against the current source, not a
# stale bundle left over from an earlier build.
(cd "$REPO_ROOT/ui" && npm run build)

exec $PY -m uvicorn tradingbot.service.main:create_service_app --factory --host 127.0.0.1 --port 8000
