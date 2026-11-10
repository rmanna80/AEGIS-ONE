#!/usr/bin/env python3
"""Demo 1 - Autonomous Survey.

Submits the scenario's demo mission through the AEGIS API (exactly as the
dashboard does), follows it over the telemetry endpoint and checks the
acceptance criteria. Needs PX4 SITL and the backend running.

    python scripts/demo1_survey.py --token "$AEGIS_OPERATOR_TOKEN"

Acceptance criteria (all must hold):
  1. The mission is accepted and PX4 enters Mission mode.
  2. Every waypoint is reported reached.
  3. The vehicle never leaves the mission geofence.
  4. The vehicle returns, lands within 3 m of home and disarms.
  5. No CRITICAL alert is raised during the flight.

Results are written to data/generated/demo_results/. Reset: the vehicle ends
landed and disarmed at home, so the demo can simply be run again.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]


def call(base: str, path: str, token: str | None = None, body: Any = None) -> Any:
    request = urllib.request.Request(base + path, method="POST" if body is not None else "GET")
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        request.add_header("Content-Type", "application/json")
    if token:
        request.add_header("X-Aegis-Operator-Token", token)
    try:
        with urllib.request.urlopen(request, data=data, timeout=60) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"{path} -> HTTP {exc.code}: {exc.read().decode()}") from exc


def inside(point: tuple[float, float], polygon: list[list[float]]) -> bool:
    x, y = point
    hit = False
    for i in range(len(polygon)):
        (x1, y1), (x2, y2) = polygon[i], polygon[(i + 1) % len(polygon)]
        if (y1 > y) != (y2 > y) and x < x1 + (y - y1) * (x2 - x1) / (y2 - y1):
            hit = not hit
    return hit


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--api", default="http://127.0.0.1:8000")
    parser.add_argument("--token", required=True, help="operator token (AEGIS_OPERATOR_TOKEN)")
    parser.add_argument("--timeout", type=float, default=420.0)
    args = parser.parse_args()

    system = call(args.api, "/api/system")
    print(f"mode={system['mode']} provenance={system['provenance']} source={system['source']}")
    if system["provenance"] != "simulated" or not system.get("simulator_attested"):
        raise SystemExit("Refusing to run: the backend is not attached to an attested simulator.")

    mission = call(args.api, "/api/scenario")["demo_mission"]
    fence = mission["constraints"]["geofence"]["polygon_enu_m"]
    result = call(args.api, "/api/missions", args.token, mission)
    print("submit:", result["status"]["phase"], "-", result["status"]["message"])

    track: list[dict[str, float]] = []
    critical: set[str] = set()
    left_fence = False
    last_line = ""
    status = result["status"]
    started = time.monotonic()
    if result["accepted"]:
        while time.monotonic() - started < args.timeout:
            snap = call(args.api, "/api/state")
            state, status = snap["state"], snap["mission"]
            if state and state["position_enu_m"]:
                p = state["position_enu_m"]
                track.append({"t": round(time.monotonic() - started, 2), **p})
                left_fence |= not inside((p["x"], p["y"]), fence)
            critical |= {a["code"] for a in snap["alerts"] if a["severity"] == "critical"}
            line = f"{status['phase']:>10}  {status['message']}"
            if line != last_line:
                print(f"[{time.monotonic() - started:6.1f}s] {line}")
                last_line = line
            if status["phase"] in ("completed", "failed", "stalled", "cancelled"):
                break
            time.sleep(0.25)

    final = call(args.api, "/api/state")["state"]
    home_error = math.hypot(final["position_enu_m"]["x"], final["position_enu_m"]["y"])
    criteria = {
        "1_mission_accepted": bool(result["accepted"]),
        "2_all_waypoints_reached": status["current_waypoint"] == len(mission["waypoints"])
        and status["phase"] == "completed",
        "3_stayed_inside_geofence": bool(track) and not left_fence,
        "4_landed_at_home_and_disarmed": home_error < 3.0
        and final["armed"] is False
        and final["in_air"] is False,
        "5_no_critical_alerts": not critical,
    }
    passed = all(criteria.values())
    report = {
        "demo": "demo1_autonomous_survey",
        "provenance": system["provenance"],
        "source": system["source"],
        "finished_at_unix_s": time.time(),
        "duration_s": round(time.monotonic() - started, 1),
        "criteria": criteria,
        "passed": passed,
        "home_error_m": round(home_error, 2),
        "max_altitude_enu_m": round(max((p["z"] for p in track), default=0.0), 2),
        "critical_alerts": sorted(critical),
        "final_mission_status": status,
        "track_samples": len(track),
    }
    out_dir = REPO_ROOT / "data" / "generated" / "demo_results"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"demo1_{time.strftime('%Y%m%dT%H%M%S')}.json"
    out.write_text(json.dumps(report, indent=2))
    for name, ok in criteria.items():
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    print(f"{'PASSED' if passed else 'FAILED'} - report: {out.relative_to(REPO_ROOT)}")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
