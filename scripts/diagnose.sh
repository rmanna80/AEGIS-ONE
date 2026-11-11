#!/usr/bin/env bash
# Collect what is needed to debug a slow or unresponsive setup. Run it WHILE the
# problem is happening and paste the whole output. It only reads; it changes nothing.
cd "$(dirname "${BASH_SOURCE[0]}")/.."
API="http://127.0.0.1:${AEGIS_PORT:-8000}"
echo "== time: $(date +%T)   cores: $(nproc)   load: $(cut -d' ' -f1-3 /proc/loadavg)"
free -m | awk 'NR==2{print "== memory MB: total " $2 ", used " $3 ", available " $7}'
echo "== graphics: $(glxinfo -B 2>/dev/null | grep -i "OpenGL renderer" || echo "glxinfo not installed (sudo apt install mesa-utils)")"

echo "== backend response times (3 tries)"
for i in 1 2 3; do
  curl -s -o /dev/null -m 5 -w "   /api/health  %{http_code}  %{time_total}s\n" "$API/api/health" \
    || echo "   /api/health  NO RESPONSE within 5 s"
  sleep 1
done

echo "== backend health: $(curl -s -m 5 "$API/api/health")"
echo "== camera models: $(python3 scripts/gz_light_sensors.py status "${PX4_DIR:-$HOME/PX4-Autopilot}" 2>&1 | tr '\n' ' ')"
echo "== vehicle"
curl -s -m 5 "$API/api/state" | python3 -c '
import json, sys, time
try:
    d = json.load(sys.stdin)
except Exception as exc:
    print("   no state:", exc); raise SystemExit
s = d["state"] or {}
print("   connected:", s.get("connected"), " source:", s.get("source"),
      " sim speed:", s.get("sim_real_time_factor"), " mode:", s.get("flight_mode"))
print("   state age s:", round(time.time() - s.get("received_at_unix_s", 0), 2),
      " mission:", d["mission"]["phase"], "-", d["mission"]["message"])
print("   alerts:", [(a["code"], a["severity"]) for a in d["alerts"]])
'
echo "== navigation: $(curl -s -m 5 "$API/api/navigation")"
echo "== perception: $(curl -s -m 5 "$API/api/perception")"

echo "== busiest processes"
ps -eo pcpu,pmem,etimes,args --sort=-pcpu | head -9 | cut -c1-130
echo "== AEGIS processes"
pgrep -af "uvicorn aegis|mavsdk_server|gz_camera_bridge|bin/px4|gz sim|ruby.*gz" | cut -c1-120 || echo "   none found"
