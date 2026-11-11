#!/usr/bin/env bash
# Start one simulated PX4 quadcopter (SIMULATION ONLY).
#
#   bash scripts/run_px4.sh sih      # PX4's built-in physics (no GPU, no Gazebo)
#   bash scripts/run_px4.sh gazebo      # Gazebo Harmonic, AEGIS open-field world
#   bash scripts/run_px4.sh gazebo-cam  # same, with a forward-facing RGB camera
#
# Reset procedure: Ctrl+C, then run it again. PX4 starts landed and disarmed at
# home. MAVLink for AEGIS is on UDP 14540 (localhost only).
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$REPO_ROOT/scripts/versions.env"
SIM="${1:-sih}"
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
    python3 "$REPO_ROOT/scripts/generate_gazebo_world.py" \
      "$REPO_ROOT/simulation/scenarios/open_field.json"
    cp "$REPO_ROOT/simulation/gazebo/worlds/aegis_open_field.sdf" \
      "$PX4_DIR/Tools/simulation/gz/worlds/"
    cd "$PX4_DIR"
    # HEADLESS=1 runs the Gazebo server without its GUI.
    PX4_GZ_WORLD=aegis_open_field exec make px4_sitl "$MODEL"
    ;;
  *)
    echo "usage: $0 [sih|gazebo|gazebo-cam]" >&2; exit 2 ;;
esac
