#!/usr/bin/env bash
# Make the MAVSDK control ports reachable from this computer only.
#
#   sudo bash scripts/lock_mavsdk_port.sh            # add the rule (safe to run again)
#   sudo bash scripts/lock_mavsdk_port.sh --remove   # take it away
#   sudo bash scripts/lock_mavsdk_port.sh --status   # show it
#
# Why: the MAVSDK server bundled with AEGIS listens on every network interface (port
# 50051, one more per extra fleet vehicle) with no authentication, and has no option to
# listen on this computer only. Anything that can reach that port can command the
# autopilot without the AEGIS operator token. This drops connections to those ports
# that arrive from any interface other than loopback. Nothing else is changed.
#
# Check it from ANOTHER computer on the network (this one cannot test itself):
#   nc -zv <this computer's address> 50051        # must fail / time out
#
# The rule is lost on reboot unless installed as a service (deploy/aegis-firewall.service).
set -euo pipefail

PORTS="${AEGIS_MAVSDK_PORTS:-50051-50060}"
TABLE="aegis"

if [ "$(id -u)" -ne 0 ]; then
  echo "Needs root: sudo bash $0 ${*:-}" >&2
  exit 1
fi
command -v nft >/dev/null || { echo "nftables is not installed (sudo apt install nftables)." >&2; exit 1; }

case "${1:-}" in
  --remove)
    nft delete table inet "$TABLE" 2>/dev/null && echo "Removed the AEGIS rule." || echo "No AEGIS rule was present."
    exit 0 ;;
  --status)
    nft list table inet "$TABLE" 2>/dev/null || echo "No AEGIS rule is present."
    exit 0 ;;
  "") ;;
  *) echo "Unknown option $1" >&2; exit 2 ;;
esac

# Create-then-delete-then-create makes this safe to run any number of times.
nft -f - <<RULES
table inet $TABLE
delete table inet $TABLE
table inet $TABLE {
  chain input {
    type filter hook input priority filter; policy accept;
    iifname != "lo" tcp dport $PORTS drop comment "AEGIS: MAVSDK control from this computer only"
  }
}
RULES
echo "MAVSDK control ports $PORTS now accept connections from this computer only."
echo "Check from another computer: nc -zv <this computer's address> 50051   (must fail)"
