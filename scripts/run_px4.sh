#!/usr/bin/env bash
# Start one simulated PX4 quadcopter (SIMULATION ONLY).
#
#   bash scripts/run_px4.sh sih      # PX4's built-in physics (no GPU, no Gazebo)
#   bash scripts/run_px4.sh gazebo      # Gazebo Harmonic, AEGIS open-field world
#   bash scripts/run_px4.sh gazebo-cam  # same, with a forward-facing RGB camera
#   bash scripts/run_px4.sh gazebo-depth       # same, with a forward depth camera
#   bash scripts/run_px4.sh gazebo-cam urban   # second argument picks the scenario
#
# Use the same scenario for the backend: AEGIS_SCENARIO=urban bash scripts/run_backend.sh
#
# If Gazebo cannot keep up (dashboard "Sim speed" well below 1.0x), lower the camera load:
#   AEGIS_LIGHT_SENSORS=1 bash scripts/run_px4.sh gazebo-depth urban
# and/or run without the Gazebo window:  HEADLESS=1 bash scripts/run_px4.sh ...
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
  gazebo|gazebo-cam|gazebo-depth)
    MODEL=gz_x500
    [ "$SIM" = "gazebo-cam" ] && MODEL=gz_x500_mono_cam
    [ "$SIM" = "gazebo-depth" ] && MODEL=gz_x500_depth
    python3 "$REPO_ROOT/scripts/generate_gazebo_world.py" "$SCENARIO_FILE"
    WORLD="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["gazebo_world"])' "$SCENARIO_FILE")"
    cp "$REPO_ROOT/simulation/gazebo/worlds/$WORLD.sdf" "$PX4_DIR/Tools/simulation/gz/worlds/"
    if grep -q "fuel.gazebosim.org" "$REPO_ROOT/simulation/gazebo/worlds/$WORLD.sdf"; then
      echo "This world uses models from Gazebo Fuel; the first launch downloads them (needs internet)."
    fi
    # Camera load. Reversible: see scripts/gz_light_sensors.py.
    if [ "${AEGIS_LIGHT_SENSORS:-0}" = "1" ]; then
      python3 "$REPO_ROOT/scripts/gz_light_sensors.py" apply "$PX4_DIR"
    else
      python3 "$REPO_ROOT/scripts/gz_light_sensors.py" restore "$PX4_DIR" | grep -v "already stock" || true
    fi
    # Gazebo renders camera sensors with OpenGL. Without GPU acceleration that falls back
    # to the CPU (llvmpipe), which is the usual reason a camera simulation runs far below real time.
    renderer() { glxinfo -B 2>/dev/null | grep -i "OpenGL renderer" | cut -d: -f2- | xargs || true; }
    if command -v glxinfo >/dev/null; then
      RENDERER="$(renderer)"
      case "$RENDERER" in
        *llvmpipe*|*softpipe*|"")
          # Ubuntu 24.04 under WSL2 ships the Direct3D 12 driver but does not select it.
          # If it works here, use it for this launch (AEGIS_NO_GPU=1 to opt out).
          if [ "${AEGIS_NO_GPU:-0}" != "1" ] && [ -z "${GALLIUM_DRIVER:-}" ] \
             && GALLIUM_DRIVER=d3d12 glxinfo -B 2>/dev/null | grep -qi "renderer string: D3D12"; then
            export GALLIUM_DRIVER=d3d12
            RENDERER="$(renderer)"
            echo "Graphics renderer: $RENDERER  (GPU enabled for this launch via GALLIUM_DRIVER=d3d12)"
          else
            echo "Graphics renderer: ${RENDERER:-unknown}"
            echo "WARNING: Gazebo will render on the CPU. Expect the simulation to run well below" >&2
            echo "         real time with cameras. Try AEGIS_LIGHT_SENSORS=1 and HEADLESS=1, and see" >&2
            echo "         docs/simulation/README.md (GPU acceleration under WSL2)." >&2
          fi ;;
        *) echo "Graphics renderer: $RENDERER" ;;
      esac
    else
      echo "Tip: 'sudo apt install mesa-utils' lets this script report whether Gazebo has GPU acceleration."
    fi
    cd "$PX4_DIR"
    # HEADLESS=1 runs the Gazebo server without its GUI.
    PX4_GZ_WORLD="$WORLD" exec make px4_sitl "$MODEL"
    ;;
  *)
    echo "usage: $0 [sih|gazebo|gazebo-cam|gazebo-depth] [scenario]" >&2; exit 2 ;;
esac
