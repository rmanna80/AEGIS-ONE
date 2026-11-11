#!/usr/bin/env python3
"""Demo 4 - Mapping.

Flies the scenario's mission with the depth sensor on, builds a 3D occupancy
map, scores it against the simulator's ground-truth geometry and saves it.

    bash scripts/run_px4.sh sih urban            # or the Gazebo depth vehicle, see README
    AEGIS_SCENARIO=urban AEGIS_DEPTH=raycast bash scripts/run_backend.sh
    python scripts/demo4_mapping.py --token "$AEGIS_OPERATOR_TOKEN"

Use the urban scenario: its route runs at 4 m between buildings. The open-field
survey flies at 12-15 m, above what a forward-looking 19 m depth sensor can see.

Acceptance criteria:
  1. The mission completes and the vehicle lands at home.
  2. Depth frames are labelled `simulated` and at least 50 were fused into the map.
  3. Precision >= 0.90: of the voxels mapped as occupied, the share that lie on
     (or within one voxel of) real geometry.
  4. Recall >= 0.80 of the obstacle surface the sensor actually observed.
  5. The map is saved to disk.
Reported, not pass/fail: surface coverage (how much of the scene the route got to
see), voxel counts and resolution.

Ground truth is the scenario's own geometry (boxes for buildings and proxy boxes
for people and vehicles). Under Gazebo the rendered models are not exactly those
boxes, so expect lower scores there than with the software depth sensor.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from _demo_common import Api, Flight, require_simulator, write_report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--api", default="http://127.0.0.1:8000")
    parser.add_argument("--token", required=True)
    parser.add_argument("--timeout", type=float, default=900.0)
    args = parser.parse_args()
    api = Api(args.api, args.token)

    system = require_simulator(api)
    nav = api.call("/api/navigation")
    if not nav["enabled"] or nav["depth_source"] is None:
        raise SystemExit("Start the backend with AEGIS_DEPTH=raycast (or stream).")
    scenario = api.call("/api/scenario")
    for _ in range(40):  # up to 20 s for the first depth frames
        if nav["frames_integrated"] > 0:
            break
        time.sleep(0.5)
        nav = api.call("/api/navigation")
    print(f"scenario={scenario['scenario_id']} depth={nav['depth_source']}")
    if nav["frames_integrated"] == 0:
        raise SystemExit(
            "No depth frames are reaching AEGIS, so there is nothing to map. For Gazebo, start "
            "the bridge:  /usr/bin/python3 scripts/gz_camera_bridge.py --depth\n"
            "and check its output. Nothing was sent to the vehicle."
        )

    result = api.call("/api/missions", scenario["demo_mission"])
    print("submit:", result["status"]["phase"], "-", result["status"]["message"])
    for issue in result["status"]["issues"]:
        print("   ", issue)
    flight = Flight(api)
    phase = flight.follow(args.timeout) if result["accepted"] else "rejected"

    nav = api.call("/api/navigation")
    accuracy = api.call("/api/map/accuracy")
    saved = api.call("/api/map/save", post=True)
    saved_path = Path(saved["saved"])

    criteria = {
        "1_mission_completed": phase == "completed" and flight.landed_at_home(),
        "2_depth_simulated_and_fused": nav["depth_provenance"] == "simulated"
        and nav["frames_integrated"] >= 50,
        "3_precision_at_least_0.90": accuracy["precision"] >= 0.90
        and accuracy["mapped_occupied_voxels"] > 0,
        "4_recall_of_observed_surface_at_least_0.80": accuracy["recall_of_observed_surface"]
        >= 0.80,
        "5_map_saved": saved_path.is_file() and saved_path.stat().st_size > 0,
    }
    print(
        f"  map: {accuracy['mapped_occupied_voxels']} occupied voxels at "
        f"{accuracy['resolution_m']} m from {nav['frames_integrated']} depth frames"
    )
    print(
        f"  precision {accuracy['precision']:.3f}  recall of observed surface "
        f"{accuracy['recall_of_observed_surface']:.3f}  surface coverage "
        f"{accuracy['surface_coverage']:.3f}"
    )
    print(f"  saved: {saved_path}")
    write_report(
        "demo4_mapping",
        {
            "scenario": scenario["scenario_id"],
            "vehicle_source": system["source"],
            "depth_source": nav["depth_source"],
            "depth_provenance": nav["depth_provenance"],
            "accuracy": accuracy,
            "map_file": str(saved_path),
            "map_bytes": saved_path.stat().st_size if saved_path.is_file() else 0,
            "critical_alerts": flight.critical,
            "duration_s": round(flight.elapsed(), 1),
            "final_mission_status": flight.status,
        },
        criteria,
        all(criteria.values()),
    )
    return 0 if all(criteria.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
