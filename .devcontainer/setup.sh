#!/usr/bin/env bash
# One-time setup when the codespace / dev container is created.
# The venv lives outside the workspace so it never clashes with a host backend/.venv.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$HOME/.venvs/latticeflow"

echo "==> Python venv at $VENV"
python3 -m venv "$VENV"
"$VENV/bin/python" -m pip install --upgrade pip
echo "==> CPU torch"
"$VENV/bin/pip" install torch==2.6.0 --index-url https://download.pytorch.org/whl/cpu
echo "==> Backend requirements"
"$VENV/bin/pip" install -r "$ROOT/backend/requirements.txt"

echo "==> Caching the MACE-MP-0 small checkpoint (about 40 MB)"
# Non-fatal: if this fails, the backend retries the download on its first start.
(cd "$ROOT/backend" && "$VENV/bin/python" -c "
from app.core.umlip_engine import resolve_mace_checkpoint
path, name = resolve_mace_checkpoint('small', allow_download=True, timeout_s=300)
print(f'{name}: {path}')
") || echo "WARNING: checkpoint download failed; the backend will retry on start."

echo "==> Frontend dependencies and production build"
cd "$ROOT/frontend"
npm ci
npm run build

echo "==> Setup done."
