#!/usr/bin/env bash
# Start the AEGIS backend (serves the built dashboard at http://127.0.0.1:8000).
#   AEGIS_HOST=0.0.0.0 bash scripts/run_backend.sh  # reachable from the network (see docs/raspberry-pi)
#   bash scripts/run_backend.sh                    # live PX4 SITL
#   AEGIS_MODE=replay bash scripts/run_backend.sh  # replay the bundled sample flight
#   AEGIS_ACCESS_LOG=1 bash scripts/run_backend.sh # also print every HTTP request
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
PORT="${AEGIS_PORT:-8000}"
if ss -ltn 2>/dev/null | grep -q "[:.]${PORT}[[:space:]]"; then
  echo "Port ${PORT} is in use: an AEGIS backend is probably already running in another terminal." >&2
  echo "Stop it first (Ctrl+C there, or: pkill -f 'uvicorn aegis.api')." >&2
  exit 1
fi
# No backend is running, so any MAVSDK server still alive is a leftover from a crashed run.
pkill -f mavsdk_server 2>/dev/null || true
if [ ! -f frontend/dist/index.html ]; then
  echo "Dashboard is not built yet. Run: (cd frontend && npm ci && npm run build)" >&2
  exit 1
fi
HOST="${AEGIS_HOST:-127.0.0.1}"
if [ "$HOST" != "127.0.0.1" ] && [ "$HOST" != "localhost" ]; then
  echo "WARNING: listening on $HOST. The dashboard is reachable from the network over plain HTTP;" >&2
  echo "         the operator token is the only protection. An SSH tunnel is safer (docs/raspberry-pi)." >&2
fi
exec .venv/bin/uvicorn aegis.api:create_app --factory --host "$HOST" --port "$PORT" ${AEGIS_ACCESS_LOG:+--access-log} ${AEGIS_ACCESS_LOG:---no-access-log}
