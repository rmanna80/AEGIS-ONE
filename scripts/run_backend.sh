#!/usr/bin/env bash
# Start the AEGIS backend (serves the built dashboard at http://127.0.0.1:8000).
#   bash scripts/run_backend.sh                    # live PX4 SITL
#   AEGIS_MODE=replay bash scripts/run_backend.sh  # replay the bundled sample flight
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
if [ ! -f frontend/dist/index.html ]; then
  echo "Dashboard is not built yet. Run: (cd frontend && npm ci && npm run build)" >&2
  exit 1
fi
exec .venv/bin/uvicorn aegis.api:create_app --factory --host 127.0.0.1 --port "${AEGIS_PORT:-8000}"
