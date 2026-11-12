#!/usr/bin/env bash
# Set up AEGIS ONE on a Raspberry Pi as a companion computer (SIMULATION ONLY).
#
#   bash scripts/setup_pi.sh
#
# Installs the AEGIS backend and dashboard. It does NOT install PX4 or Gazebo: the
# simulated autopilot keeps running on your PC and sends its MAVLink to the Pi over
# the network (see docs/raspberry-pi/README.md). Nothing here connects the Pi to a
# real flight controller.
#
# Needs a 64-bit Raspberry Pi OS (Bookworm or Trixie) or Ubuntu on ARM64, and internet.
# STATUS: written from the package documentation and checked for syntax; it has not
# been run on a Raspberry Pi yet.
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

ARCH="$(uname -m)"
if [ "$ARCH" != "aarch64" ] && [ "${AEGIS_SETUP_ANY_ARCH:-0}" != "1" ]; then
  echo "This script is for a 64-bit ARM system (found: $ARCH)." >&2
  echo "On a Raspberry Pi this means a 32-bit operating system is installed: re-flash" >&2
  echo "the card with 'Raspberry Pi OS (64-bit)' in Raspberry Pi Imager." >&2
  exit 1
fi
MODEL="$(tr -d '\0' < /proc/device-tree/model 2>/dev/null || echo "unknown computer")"
echo "Setting up AEGIS ONE on: $MODEL ($(. /etc/os-release && echo "$PRETTY_NAME"))"

sudo apt-get update
sudo apt-get install -y git curl ca-certificates python3 python3-venv

# Python 3.12 or newer is required. Raspberry Pi OS Trixie ships 3.13; Bookworm ships
# 3.11, in which case uv downloads a suitable Python for the virtual environment.
if python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)'; then
  python3 -m venv .venv
  .venv/bin/pip install --upgrade pip
  .venv/bin/pip install -e '.[perception]'
else
  echo "System Python is older than 3.12; using uv to provide one."
  command -v uv >/dev/null || curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
  uv venv --python 3.12 .venv
  uv pip install --python .venv/bin/python -e '.[perception]'
fi
bash scripts/get_detector_model.sh || echo 'Model download failed; object detection stays off until it succeeds.' >&2

# The dashboard is built here so the Pi can serve it. About 5 minutes on a Pi 5.
node_major="$(node --version 2>/dev/null | sed -E 's/^v([0-9]+).*/\1/' || true)"
if [ -z "$node_major" ] || [ "$node_major" -lt 20 ]; then
  curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash -
  sudo apt-get install -y nodejs
fi
(cd frontend && npm ci && npm run build)

if [ ! -f .env ]; then
  cp .env.example .env
  TOKEN="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
  {
    echo ""
    echo "# Added by scripts/setup_pi.sh"
    echo "AEGIS_OPERATOR_TOKEN=$TOKEN"
    echo "# The simulated autopilot runs on another machine, so listen on the network:"
    echo "AEGIS_PX4_URL=udpin://0.0.0.0:14540"
  } >> .env
  echo "Created .env with a new operator token."
fi

IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
cat <<NEXT

Setup finished.
  1. Check this computer:      .venv/bin/python scripts/pi_benchmark.py
  2. Start the backend here:   bash scripts/run_backend.sh
  3. On the PC running PX4:    bash scripts/link_companion.sh ${IP:-<this Pi's IP address>}
  4. On the PC, open a tunnel: ssh -L 8000:127.0.0.1:8000 $(whoami)@${IP:-<this Pi's IP address>}
     and browse to http://127.0.0.1:8000
Your operator token is in .env on this Pi (AEGIS_OPERATOR_TOKEN).
Full guide: docs/raspberry-pi/README.md
NEXT
