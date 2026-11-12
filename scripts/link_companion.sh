#!/usr/bin/env bash
# Send the SIMULATED autopilot's MAVLink to a companion computer on the network.
#
#   bash scripts/link_companion.sh 192.168.1.50          # the Raspberry Pi's address
#   bash scripts/link_companion.sh 192.168.1.50 14540 1  # port, PX4 instance
#
# Run it on the PC, after `scripts/run_px4.sh ...` shows "Ready for takeoff". It adds
# one more MAVLink link to the running PX4 simulator, aimed at the companion computer;
# the simulator's normal local link stays as it is. The link lasts until PX4 stops.
#
# It only talks to a PX4 SITL instance on this machine. It cannot reach a real aircraft.
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$REPO_ROOT/scripts/versions.env"
TARGET="${1:-}"
PORT="${2:-14540}"
INSTANCE="${3:-0}"
if [ -z "$TARGET" ]; then
  echo "Usage: bash scripts/link_companion.sh <companion IP address> [port] [PX4 instance]" >&2
  exit 2
fi
BIN="$PX4_DIR/build/px4_sitl_default/bin"
[ -x "$BIN/px4-mavlink" ] || { echo "PX4 SITL tools not found in $BIN." >&2; exit 1; }
if ! "$BIN/px4-param" --instance "$INSTANCE" show SYS_AUTOSTART >/dev/null 2>&1; then
  echo "No PX4 simulator instance $INSTANCE is running. Start it first: bash scripts/run_px4.sh sih" >&2
  exit 1
fi
LOCAL=$((14590 + INSTANCE))
"$BIN/px4-mavlink" --instance "$INSTANCE" start -x -u "$LOCAL" -r 4000000 -f -m onboard -o "$PORT" -t "$TARGET"
echo "PX4 instance $INSTANCE now also sends MAVLink to $TARGET:$PORT (from local port $LOCAL)."
echo "On the companion computer, AEGIS must listen with AEGIS_PX4_URL=udpin://0.0.0.0:$PORT."
if command -v ping >/dev/null && ! ping -c 1 -W 2 "$TARGET" >/dev/null 2>&1; then
  echo "WARNING: $TARGET did not answer a ping. Check the address and that both are on the same network." >&2
fi
