#!/usr/bin/env python3
"""Demo 5 - Two vehicles search one area.

Cuts the search area into one strip per vehicle, flies the strips at different
heights at the same time, and pools what both thermal cameras find into one
candidate list for the operator.

    bash scripts/run_fleet.sh 2 sar_field
    AEGIS_FLEET_SIZE=2 AEGIS_SCENARIO=sar_field AEGIS_THERMAL=model bash scripts/run_backend.sh
    python scripts/demo5_fleet_search.py --token "$AEGIS_OPERATOR_TOKEN"

This script stands in for the operator: it submits each vehicle's planned mission.
The vehicles do not coordinate by themselves; each flies only the mission it was
given. AEGIS watches their separation and would Hold both if they came too close.

Acceptance criteria:
  1. At least two attested simulated vehicles are connected, in one shared frame.
  2. Each vehicle's planned pattern is valid and does not conflict with the others'.
  3. A mission that crosses a route already being flown, at the same height, is refused.
  4. Every vehicle completes its mission and lands at its own home.
  5. Together the thermal cameras saw at least 95% of the search area.
  6. Every heat source visible from above was listed, within 3 m.
  7. Airborne vehicles were never inside the Hold distance of each other, and no
     automatic separation Hold was commanded.
  8. No candidate was confirmed without the operator.
Reported, not pass/fail: closest approach, time from first upload to last landing.
"""

from __future__ import annotations

import argparse
import math
import sys
import time

from _demo_common import TERMINAL, Api, require_simulator, write_report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--api", default="http://127.0.0.1:8000")
    parser.add_argument("--token", required=True)
    parser.add_argument("--timeout", type=float, default=1200.0)
    args = parser.parse_args()
    api = Api(args.api, args.token)

    system = require_simulator(api)
    fleet = api.call("/api/fleet")
    sar = api.call("/api/sar")
    if not sar["enabled"]:
        raise SystemExit(f"Search support is off: {sar['reason']}")
    for _ in range(40):  # up to 20 s for every vehicle to connect and attest
        ready = [
            v
            for v in fleet["vehicles"]
            if v["simulator_attested"] and v["state"] and v["state"]["position_enu_m"]
        ]
        if len(ready) == fleet["size"]:
            break
        time.sleep(0.5)
        fleet = api.call("/api/fleet")
    homes = {
        v["vehicle_id"]: (v["state"]["position_enu_m"]["x"], v["state"]["position_enu_m"]["y"])
        for v in ready
    }
    print(
        f"fleet: {fleet['size']} configured, {len(ready)} ready: "
        + ", ".join(f"{vid} at E {h[0]:.0f} N {h[1]:.0f}" for vid, h in homes.items())
    )
    if len(ready) < 2:
        raise SystemExit(
            "This demo needs two vehicles. Start them with: bash scripts/run_fleet.sh 2 sar_field\n"
            "and the backend with AEGIS_FLEET_SIZE=2. Nothing was sent to any vehicle."
        )

    plans = api.call("/api/sar/plan/fleet", {})["plans"]
    for plan in plans:
        s = plan["summary"]
        print(
            f"plan {plan['vehicle_id']}: {s['passes']} passes at {s['altitude_rel_m']:g} m, "
            f"{s['path_length_m']:.0f} m, issues: {plan['issues'] or 'none'}"
        )

    print("(this script, standing in for the operator, submits each vehicle's mission)")
    started = time.monotonic()
    first, second = plans[0], plans[1]
    accepted = {}
    result = api.call(f"/api/fleet/{first['vehicle_id']}/missions", first["mission"])
    accepted[first["vehicle_id"]] = result["accepted"]
    print(
        f"submit {first['vehicle_id']}:",
        result["status"]["phase"],
        "-",
        result["status"]["message"],
    )

    # Criterion 3: give the second vehicle the FIRST vehicle's route. It must be refused.
    clash = dict(first["mission"], mission_id="demo5-deliberate-conflict")
    refused = api.call(f"/api/fleet/{second['vehicle_id']}/missions", clash)
    print(
        f"deliberate conflict for {second['vehicle_id']}:",
        refused["status"]["phase"],
        "-",
        "; ".join(refused["status"]["issues"])[:160],
    )
    for plan in plans[1:]:
        result = api.call(f"/api/fleet/{plan['vehicle_id']}/missions", plan["mission"])
        accepted[plan["vehicle_id"]] = result["accepted"]
        print(
            f"submit {plan['vehicle_id']}:",
            result["status"]["phase"],
            "-",
            result["status"]["message"],
        )

    # Follow every vehicle to a terminal phase, measuring separation from telemetry.
    closest: tuple[float, float, float] | None = None  # 3D, horizontal, vertical
    inside_hold = False
    limits = fleet["limits"]
    phases: dict[str, str] = {}
    deadline = time.monotonic() + args.timeout
    last_line = ""
    while time.monotonic() < deadline:
        fleet = api.call("/api/fleet")
        phases = {v["vehicle_id"]: v["mission"]["phase"] for v in fleet["vehicles"]}
        for pair in fleet["separations"]:
            h, v = pair["horizontal_m"], pair["vertical_m"]
            if closest is None or math.hypot(h, v) < closest[0]:
                closest = (math.hypot(h, v), h, v)
            if h < limits["hold_horizontal_m"] and v < limits["hold_vertical_m"]:
                inside_hold = True
        line = "  ".join(
            f"{v['vehicle_id']}: {v['mission']['phase']} {v['mission']['current_waypoint']}/"
            f"{v['mission']['total_waypoints']}"
            for v in fleet["vehicles"]
        )
        if line != last_line:
            print(f"[{time.monotonic() - started:6.1f}s] {line}")
            last_line = line
        if all(phases[vid] in TERMINAL for vid in accepted):
            break
        time.sleep(0.5)
    elapsed = time.monotonic() - started

    fleet = api.call("/api/fleet")
    landed = {}
    for v in fleet["vehicles"]:
        p = v["state"]["position_enu_m"]
        home = homes[v["vehicle_id"]]
        landed[v["vehicle_id"]] = (
            math.hypot(p["x"] - home[0], p["y"] - home[1]) < 3.0 and v["state"]["armed"] is False
        )
    sar = api.call("/api/sar")
    accuracy = api.call("/api/sar/accuracy")
    candidates = api.call("/api/sar/candidates")["candidates"]
    visible = [s for s in accuracy["sources"] if s["expected_detectable"]]
    sensors = sorted({c["sensor"] for c in candidates})
    print(f"  coverage: {sar['coverage_fraction']:.3f}  thermal frames: {sar['frames_processed']}")
    print(f"  candidates: {len(candidates)}, first seen by: {', '.join(sensors)}")
    for s in accuracy["sources"]:
        print(
            f"    {s['id']:<18} "
            + (
                f"candidate {s['candidate_id']}, {s['position_error_m']} m off"
                if s["detected"]
                else "not found" + ("" if s["expected_detectable"] else " (hidden from above)")
            )
        )
    if closest is not None:
        print(
            f"  closest approach in the air: {closest[1]:.1f} m apart, {closest[2]:.1f} m in height"
        )
    print(f"  time from first upload to last landing: {elapsed:.0f} s")

    criteria = {
        "1_two_attested_vehicles_in_one_frame": len(ready) >= 2
        and system["provenance"] == "simulated"
        and all(
            math.hypot(a[0] - b[0], a[1] - b[1]) > 3.0
            for i, a in enumerate(homes.values())
            for b in list(homes.values())[i + 1 :]
        ),
        "2_plans_valid_and_conflict_free": all(not p["issues"] for p in plans)
        and all(accepted.values()),
        "3_conflicting_mission_refused": not refused["accepted"]
        and refused["status"]["phase"] == "rejected",
        "4_all_completed_and_landed_at_own_home": all(
            phases.get(vid) == "completed" for vid in accepted
        )
        and all(landed.values()),
        "5_coverage_at_least_0.95": sar["coverage_fraction"] >= 0.95,
        "6_every_visible_source_found_within_3m": bool(visible)
        and all(s["detected"] and s["position_error_m"] <= 3.0 for s in visible),
        "7_separation_kept_and_no_automatic_hold": not inside_hold
        and fleet["holds_commanded"] == 0,
        "8_nothing_confirmed_without_operator": bool(candidates)
        and all(c["status"] == "unreviewed" for c in candidates),
    }
    write_report(
        "demo5_fleet_search",
        {
            "scenario": accuracy["scenario"],
            "vehicles": list(homes),
            "homes_enu_m": homes,
            "plans": [{"vehicle_id": p["vehicle_id"], **p["summary"]} for p in plans],
            "final_phases": phases,
            "closest_approach_m": None
            if closest is None
            else {"horizontal": closest[1], "vertical": closest[2]},
            "holds_commanded": fleet["holds_commanded"],
            "accuracy": accuracy,
            "candidate_sensors": sensors,
            "elapsed_s": round(elapsed, 1),
        },
        criteria,
        all(criteria.values()),
    )
    return 0 if all(criteria.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
