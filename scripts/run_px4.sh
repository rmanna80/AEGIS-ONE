#!/usr/bin/env bash
# Start one simulated PX4 quadcopter (SIMULATION ONLY).
#
#   bash scripts/run_px4.sh sih      # PX4's built-in physics (no GPU, no Gazebo)
#   bash scripts/run_px4.sh gazebo      # Gazebo Harmonic, AEGIS open-field world
#   bash scripts/run_px4.sh gazebo-cam  # same, with a forward-facing RGB camera
#   bash scripts/run_px4.sh gazebo-cam urban   # second argument picks the scenario
#
# Use the same scenario for the backend: AEGIS_SCENARIO=urban bash scripts/run_backend.sh
#
# Reset procedure: Ctrl+C, then run it again. PX4 starts landed and disarmed at
# home. MAVLink for AEGIS is on UDP 14540 (localhost only).
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$REPO_ROOT/scripts/versions.env"
SIM="${1:-sih}"
SCENARIO="${2:-open_field}"
SCENARIO_FILE="$REPO_ROOT/simulation/scenarios/$SCENARIO.json"
[ -f "$SCENARIO_FILE" ] || { echo "Unknown scenario '$SCENARIO' (no $SCENARIO_FILE)." >&2; exit 2; }
BUILD="$PX4_DIR/build/px4_sitl_default"
[ -x "$BUILD/bin/px4" ] || { echo "PX4 SITL is not built in $PX4_DIR (run scripts/setup_wsl.sh)." >&2; exit 1; }

case "$SIM" in
  sih)
    cd "$BUILD"
    # -d: no interactive shell, so it also runs without a terminal.
    PX4_SIM_MODEL=sihsim_quadx exec ./bin/px4 -d
    ;;
  gazebo|gazebo-cam)
    MODEL=gz_x500
    [ "$SIM" = "gazebo-cam" ] && MODEL=gz_x500_mono_cam
    python3 "$REPO_ROOT/scripts/generate_gazebo_world.py" "$SCENARIO_FILE"
    WORLD="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["gazebo_world"])' "$SCENARIO_FILE")"
    cp "$REPO_ROOT/simulation/gazebo/worlds/$WORLD.sdf" "$PX4_DIR/Tools/simulation/gz/worlds/"
    if grep -q "fuel.gazebosim.org" "$REPO_ROOT/simulation/gazebo/worlds/$WORLD.sdf"; then
      echo "This world uses models from Gazebo Fuel; the first launch downloads them (needs internet)."
    fi
    cd "$PX4_DIR"
    # HEADLESS=1 runs the Gazebo server without its GUI.
    PX4_GZ_WORLD="$WORLD" exec make px4_sitl "$MODEL"
    ;;
  *)
    echo "usage: $0 [sih|gazebo|gazebo-cam] [scenario]" >&2; exit 2 ;;
esac
