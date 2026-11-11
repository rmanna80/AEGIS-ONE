#!/usr/bin/env python3
"""Obstacle detection and route replanning (simulation tests 3 and 4).

The route runs straight through a tower that route checking is NOT told about
in advance, so only the depth sensor can find it:

    AEGIS_DEPTH=raycast AEGIS_NAV_USE_PRIOR_MAP=false bash scripts/run_backend.sh
    python scripts/demo_obstacle_replan.py --token "$AEGIS_OPERATOR_TOKEN"

Works on any simulator (the software depth sensor needs no Gazebo). With Gazebo
use the depth vehicle and AEGIS_DEPTH=stream instead (see README).

Acceptance criteria:
  1. The mission is accepted (the obstacle is unknown, so pre-flight checks pass).
  2. In flight the obstacle is mapped and AEGIS commands Hold ("blocked").
  3. The vehicle stops at least the clearance distance short of the obstacle.
  4. A detour is planned for the remaining waypoints and passes validation.
  5. The detour is flown to completion; the vehicle lands at home.
  6. The vehicle never comes within the clearance distance of any obstacle.

This script plays the operator: it requests the detour and submits it. AEGIS
itself only stops the vehicle; it never re-routes on its own.
"""

from __future__ import annotations

import argparse
import math
import sys
import time

from _demo_common import Api, Flight, require_simulator, write_report


def distance_to_obstacle(point: tuple[float, float, float], obstacle: dict) -> float:
    """3D distance from a point to an obstacle (box or vertical cylinder on the ground)."""
    dx, dy = point[0] - obstacle["east_m"], point[1] - obstacle["north_m"]
    if obstacle["shape"] == "cylinder":
        horizontal = max(0.0, math.hypot(dx, dy) - obstacle["radius_m"])
        top = obstacle["height_m"]
    else:
        yaw = math.radians(obstacle.get("yaw_deg", 0.0))
        lx = dx * math.cos(yaw) + dy * math.sin(yaw)
        ly = -dx * math.sin(yaw) + dy * math.cos(yaw)
        sx, sy, top = obstacle["size_enu_m"]
        horizontal = math.hypot(max(abs(lx) - sx / 2, 0.0), max(abs(ly) - sy / 2, 0.0))
    return math.hypot(horizontal, max(point[2] - top, 0.0))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--api", default="http://127.0.0.1:8000")
    parser.add_argument("--token", required=True)
    parser.add_argument("--timeout", type=float, default=600.0)
    args = parser.parse_args()
    api = Api(args.api, args.token)

    system = require_simulator(api)
    nav = api.call("/api/navigation")
    if not nav["enabled"] or nav["depth_source"] is None:
        raise SystemExit("Start the backend with AEGIS_DEPTH=raycast (or stream).")
    if nav["uses_prior_map"]:
        raise SystemExit(
            "Start the backend with AEGIS_NAV_USE_PRIOR_MAP=false: with the prior map on, the "
            "blocked route is (correctly) rejected before take-off and there is nothing to detect."
        )
    scenario = api.call("/api/scenario")
    mission = scenario.get("replan_mission")
    if mission is None:
        raise SystemExit(f"Scenario '{scenario['scenario_id']}' has no replan_mission.")
    clearance = nav["clearance_m"]
    obstacles = scenario["obstacles"]

    result = api.call("/api/missions", mission)
    print("submit:", result["status"]["phase"], "-", result["status"]["message"])
    flight = Flight(api)
    phase = flight.follow(args.timeout) if result["accepted"] else "rejected"
    held = phase == "blocked"
    hold_message = flight.status.get("message", "")

    stop_distance = None
    plan: dict = {}
    replanned_ok = False
    if held:
        time.sleep(6.0)  # let the vehicle come to rest in Hold
        flight.poll()
        _, east, north, up = flight.track[-1]
        stop_distance = min(distance_to_obstacle((east, north, up), o) for o in obstacles)
        print(f"stopped {stop_distance:.1f} m from the nearest obstacle")
        plan = api.call(
            "/api/missions/plan",
            {"mission": mission, "from_waypoint": flight.status["current_waypoint"]},
            check=False,
        )
        if "mission" in plan:
            print(f"detour: {plan['detour_waypoints_added']} waypoint(s) added")
            check = api.call("/api/missions/validate", plan["mission"])
            replanned_ok = bool(check["valid"]) and plan["detour_waypoints_added"] > 0
            if replanned_ok:
                second = api.call("/api/missions", plan["mission"])
                print("resubmit:", second["status"]["phase"], "-", second["status"]["message"])
                for issue in second["status"]["issues"]:
                    print("   ", issue)
                if second["accepted"]:
                    phase = flight.follow(args.timeout)
        else:
            print("planning failed:", plan.get("detail"))

    closest = min(
        (distance_to_obstacle((e, n, u), o) for _, e, n, u in flight.track for o in obstacles),
        default=math.inf,
    )
    criteria = {
        "1_mission_accepted": bool(result["accepted"]),
        "2_obstacle_detected_and_hold_commanded": held and "Route blocked" in hold_message,
        "3_stopped_short_of_obstacle": stop_distance is not None and stop_distance >= clearance,
        "4_detour_planned_and_valid": replanned_ok,
        "5_detour_flown_and_landed_home": phase == "completed" and flight.landed_at_home(),
        "6_clearance_kept_throughout": closest >= clearance,
    }
    write_report(
        "demo_obstacle_replan",
        {
            "vehicle_source": system["source"],
            "depth_source": nav["depth_source"],
            "clearance_m": clearance,
            "hold_message": hold_message,
            "stop_distance_m": None if stop_distance is None else round(stop_distance, 2),
            "closest_approach_m": round(closest, 2),
            "detour_waypoints_added": plan.get("detour_waypoints_added"),
            "critical_alerts": flight.critical,
            "duration_s": round(flight.elapsed(), 1),
            "final_mission_status": flight.status,
            "navigation": api.call("/api/navigation"),
        },
        criteria,
        all(criteria.values()),
    )
    return 0 if all(criteria.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
