#!/usr/bin/env bash
# One-time setup on Ubuntu 24.04 (WSL2). Installs PX4 + Gazebo Harmonic, then the
# AEGIS backend and frontend dependencies.
#
#   bash scripts/setup_wsl.sh            # full setup (Gazebo + PX4)
#   bash scripts/setup_wsl.sh --no-gazebo  # PX4 with the built-in SIH simulator only
#
# NOTE: the --no-gazebo path is what was exercised when this repository was
# created. The Gazebo path follows PX4's documented procedure but has not yet
# been run by the project; see docs/simulation/README.md.
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$REPO_ROOT/scripts/versions.env"

if ! grep -q 'VERSION_ID="24.04"' /etc/os-release; then
  echo "This script targets Ubuntu 24.04 (found: $(. /etc/os-release && echo "$PRETTY_NAME"))." >&2
  exit 1
fi

sudo apt-get update
sudo apt-get install -y git python3-venv python3-pip curl

if [ ! -d "$PX4_DIR/.git" ]; then
  git clone --depth 1 --branch "$PX4_VERSION" --recurse-submodules --shallow-submodules \
    https://github.com/PX4/PX4-Autopilot.git "$PX4_DIR"
fi
have="$(git -C "$PX4_DIR" describe --tags --always)"
[ "$have" = "$PX4_VERSION" ] || echo "WARNING: $PX4_DIR is at $have, expected $PX4_VERSION" >&2

if [ "${1:-}" = "--no-gazebo" ]; then
  bash "$PX4_DIR/Tools/setup/ubuntu.sh" --no-nuttx --no-sim-tools
else
  bash "$PX4_DIR/Tools/setup/ubuntu.sh" --no-nuttx   # also installs Gazebo Harmonic
fi
make -C "$PX4_DIR" px4_sitl_default

cd "$REPO_ROOT"
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -e '.[dev]'

# The dashboard needs Node.js 20+; Ubuntu 24.04's own package is too old.
node_major="$(node --version 2>/dev/null | sed -E 's/^v([0-9]+).*/\1/' || true)"
if [ -z "$node_major" ] || [ "$node_major" -lt 20 ]; then
  curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash -
  sudo apt-get install -y nodejs
fi
(cd frontend && npm ci && npm run build)
[ -f .env ] || cp .env.example .env
echo "Setup finished. Next: bash scripts/run_px4.sh sih   (see README.md)"
