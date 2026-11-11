#!/usr/bin/env python3
"""Demo 3 - Search and rescue.

Plans a search pattern over the scenario's search area, flies it with the thermal
camera on, lists the heat sources found for the operator, records the operator's
decisions and writes a search report.

    bash scripts/run_px4.sh sih sar_field
    AEGIS_SCENARIO=sar_field AEGIS_THERMAL=model bash scripts/run_backend.sh
    python scripts/demo3_sar.py --token "$AEGIS_OPERATOR_TOKEN"

The thermal camera is a software model (see docs): simulated data, on any simulator.

This script stands in for the operator in two places and says so when it does:
it submits the planned mission, and after landing it reviews each candidate. Its
"decisions" are read from the scenario's answer key, purely to exercise the review
workflow. AEGIS itself never confirms a candidate and never steers toward one.

Acceptance criteria:
  1. The planned search pattern passes validation and the mission completes, landing at home.
  2. Thermal frames are labelled `simulated`.
  3. The thermal camera saw at least 95% of the search area.
  4. Every heat source that can be seen from above was listed as a candidate,
     within 3 m of its true position.
  5. No candidate was reported where there is no heat source.
  6. Before the operator's review every candidate was `unreviewed`: none auto-confirmed.
  7. A review without the operator token is refused.
  8. The search report is saved.
Reported, not pass/fail: sources hidden from above (expected misses), position
errors, and how many "person-sized" candidates were not people.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from _demo_common import Api, Flight, require_simulator, write_report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--api", default="http://127.0.0.1:8000")
    parser.add_argument("--token", required=True)
    parser.add_argument("--timeout", type=float, default=1200.0)
    parser.add_argument("--altitude", type=float, help="search altitude, metres above home")
    args = parser.parse_args()
    api = Api(args.api, args.token)

    system = require_simulator(api)
    sar = api.call("/api/sar")
    if not sar["enabled"]:
        raise SystemExit(
            f"Search support is off: {sar['reason']}\nStart the backend with "
            "AEGIS_SCENARIO=sar_field AEGIS_THERMAL=model."
        )
    scenario = api.call("/api/scenario")
    print(f"scenario={scenario['scenario_id']} thermal={sar['thermal_source']}")

    body = {"altitude_rel_m": args.altitude} if args.altitude else {}
    plan = api.call("/api/sar/plan", body)
    summary = plan["summary"]
    print(
        f"plan: {summary['passes']} passes at {summary['altitude_rel_m']:g} m, swath "
        f"{summary['swath_width_m']} m, spacing {summary['pass_spacing_m']} m, path "
        f"{summary['path_length_m']:.0f} m, about {summary['estimated_search_time_s']:.0f} s"
    )
    for issue in plan["issues"]:
        print("   issue:", issue)

    print("(this script, standing in for the operator, submits the planned mission)")
    result = api.call("/api/missions", plan["mission"])
    print("submit:", result["status"]["phase"], "-", result["status"]["message"])
    flight = Flight(api)
    phase = flight.follow(args.timeout) if result["accepted"] else "rejected"

    sar = api.call("/api/sar")
    before = api.call("/api/sar/candidates")["candidates"]
    accuracy = api.call("/api/sar/accuracy")
    visible = [s for s in accuracy["sources"] if s["expected_detectable"]]
    hidden = [s for s in accuracy["sources"] if not s["expected_detectable"]]
    print(f"  thermal frames: {sar['frames_processed']}  coverage: {sar['coverage_fraction']:.3f}")
    print(f"  candidates listed for review: {len(before)}")
    for s in accuracy["sources"]:
        where = (
            f"candidate {s['candidate_id']}, {s['position_error_m']} m off, '{s['category']}'"
            if s["detected"]
            else "not found" + ("" if s["expected_detectable"] else " (hidden from above)")
        )
        print(f"    {s['id']:<18} {s['kind']:<7} {where}")

    unauthorised = Api(args.api, None).call(
        f"/api/sar/candidates/{before[0]['candidate_id']}/review" if before else "/api/sar",
        {"decision": "confirmed"} if before else None,
        check=False,
    )

    print("(this script, standing in for the operator, reviews each candidate from the answer key)")
    kinds = {s["candidate_id"]: s for s in accuracy["sources"] if s["detected"]}
    for candidate in before:
        truth = kinds.get(candidate["candidate_id"])
        is_person = truth is not None and truth["kind"] == "person"
        api.call(
            f"/api/sar/candidates/{candidate['candidate_id']}/review",
            {
                "decision": "confirmed" if is_person else "dismissed",
                "note": f"demo answer key: {truth['kind'] if truth else 'no source here'}",
            },
        )
    after = api.call("/api/sar/candidates")["candidates"]
    saved = api.call("/api/sar/report/save", post=True)["saved"]

    person_sized = [c for c in before if c["category"] == "person-sized warm object"]
    not_people = [
        c for c in person_sized if kinds.get(c["candidate_id"], {}).get("kind") != "person"
    ]
    criteria = {
        "1_planned_mission_valid_and_completed": not plan["issues"]
        and phase == "completed"
        and flight.landed_at_home(),
        "2_thermal_labelled_simulated": sar["thermal_provenance"] == "simulated",
        "3_coverage_at_least_0.95": sar["coverage_fraction"] >= 0.95,
        "4_every_visible_source_found_within_3m": bool(visible)
        and all(s["detected"] and s["position_error_m"] <= 3.0 for s in visible),
        "5_no_candidate_without_a_source": not accuracy["candidates_without_a_source"],
        "6_nothing_confirmed_before_review": bool(before)
        and all(c["status"] == "unreviewed" for c in before),
        "7_review_refused_without_token": unauthorised.get("http_error") == 401,
        "8_report_saved": Path(saved["json"]).is_file() and Path(saved["markdown"]).is_file(),
    }
    confirmed = sum(1 for c in after if c["status"] == "confirmed")
    print(
        f"  operator decisions: {confirmed} confirmed, "
        f"{sum(1 for c in after if c['status'] == 'dismissed')} dismissed"
    )
    print(
        f"  'person-sized' candidates: {len(person_sized)}, of which not people: {len(not_people)}"
        f"  |  hidden from above and missed: {sum(1 for s in hidden if not s['detected'])}"
    )
    print(f"  report: {saved['markdown']}")
    write_report(
        "demo3_sar",
        {
            "scenario": scenario["scenario_id"],
            "vehicle_source": system["source"],
            "thermal_source": sar["thermal_source"],
            "thermal_provenance": sar["thermal_provenance"],
            "plan": summary,
            "final_phase": phase,
            "accuracy": accuracy,
            "candidates_before_review": before,
            "candidates_after_review": after,
            "critical_alerts": flight.critical,
            "elapsed_s": round(flight.elapsed(), 1),
        },
        criteria,
        all(criteria.values()),
    )
    return 0 if all(criteria.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
