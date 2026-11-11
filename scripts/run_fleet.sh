#!/usr/bin/env bash
# Start several simulated PX4 quadcopters on the built-in simulator (SIMULATION ONLY).
#
#   bash scripts/run_fleet.sh 2 sar_field      # two vehicles (default 2, at most 4)
#
# Then start the backend with the same count:
#   AEGIS_FLEET_SIZE=2 AEGIS_SCENARIO=sar_field AEGIS_THERMAL=model bash scripts/run_backend.sh
#
# Vehicle i (from 0) is PX4 instance i: MAVLink system id i+1, UDP port 14540+i, named
# sim-(i+1) in AEGIS. Each starts 12 m south of the previous one so they do not share a
# take-off spot. The built-in simulator has no collisions and no shared world: the
# vehicles cannot see or touch each other; AEGIS's separation monitor is what watches them.
#
# Stop them all with Ctrl+C.
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$REPO_ROOT/scripts/versions.env"
COUNT="${1:-2}"
SCENARIO="${2:-open_field}"
case "$COUNT" in 1|2|3|4) ;; *) echo "Vehicle count must be 1 to 4 (got '$COUNT')." >&2; exit 2 ;; esac
[ -f "$REPO_ROOT/simulation/scenarios/$SCENARIO.json" ] || { echo "Unknown scenario '$SCENARIO'." >&2; exit 2; }
BUILD="$PX4_DIR/build/px4_sitl_default"
[ -x "$BUILD/bin/px4" ] || { echo "PX4 SITL is not built in $PX4_DIR (run scripts/setup_wsl.sh)." >&2; exit 1; }

LEFTOVER="$(pgrep -af "bin/px4|gz sim" | grep -v "run_fleet.sh" | cut -c1-110 || true)"
if [ -n "$LEFTOVER" ]; then
  if [ "${AEGIS_KILL_STALE:-0}" = "1" ]; then
    pkill -f "bin/px4" || true; sleep 2; pkill -9 -f "bin/px4" || true; sleep 1
  else
    echo "A simulator is already running:" >&2
    echo "$LEFTOVER" | sed 's/^/  /' >&2
    echo "Stop it first (Ctrl+C in its terminal), or: AEGIS_KILL_STALE=1 bash scripts/run_fleet.sh $*" >&2
    exit 1
  fi
fi

# PX4's default simulated home; later vehicles are placed south of it.
HOME_LAT=47.397742
HOME_LON=8.545594
SPACING_M=12
PIDS=()
cleanup() { echo; echo "Stopping ${#PIDS[@]} vehicle(s)."; kill "${PIDS[@]}" 2>/dev/null || true; wait 2>/dev/null || true; }
trap cleanup EXIT INT TERM

for i in $(seq 0 $((COUNT - 1))); do
  LAT="$(python3 -c "print(f'{$HOME_LAT - $i * $SPACING_M / 111320.0:.7f}')")"
  DIR="$BUILD/instance_$i"
  mkdir -p "$DIR"
  LOG="$DIR/aegis_px4.log"
  ( cd "$DIR" && PX4_SIM_MODEL=sihsim_quadx PX4_HOME_LAT="$LAT" PX4_HOME_LON="$HOME_LON" \
      exec "$BUILD/bin/px4" -i "$i" -d "$BUILD/etc" ) > "$LOG" 2>&1 &
  PIDS+=("$!")
  echo "sim-$((i + 1)): PX4 instance $i, UDP $((14540 + i)), home $((i * SPACING_M)) m south of sim-1, log $LOG"
done

echo "Waiting for the vehicles to be ready..."
for i in $(seq 0 $((COUNT - 1))); do
  for _ in $(seq 1 60); do
    grep -q "Ready for takeoff" "$BUILD/instance_$i/aegis_px4.log" 2>/dev/null && break
    sleep 1
  done
  if grep -q "Ready for takeoff" "$BUILD/instance_$i/aegis_px4.log" 2>/dev/null; then
    echo "sim-$((i + 1)): ready for takeoff"
  else
    echo "sim-$((i + 1)): NOT ready after 60 s; see $BUILD/instance_$i/aegis_px4.log" >&2
  fi
done
echo "Fleet running. Start the backend with AEGIS_FLEET_SIZE=$COUNT. Ctrl+C here stops every vehicle."
wait
