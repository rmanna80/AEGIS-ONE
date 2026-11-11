#!/usr/bin/env python3
"""Demo 2 - AI Detection.

Flies the scenario's mission past its staged, static targets (people and
vehicles) and checks that the detector and tracker find them in the simulated
camera feed and that the results are stored.

Needs, in separate terminals:
    bash scripts/run_px4.sh gazebo-cam urban
    AEGIS_SCENARIO=urban AEGIS_CAMERA=stream bash scripts/run_backend.sh
    /usr/bin/python3 scripts/gz_camera_bridge.py
then:
    python scripts/demo2_detection.py --token "$AEGIS_OPERATOR_TOKEN"

Acceptance criteria (all must hold):
  1. The mission is accepted and completes (returned, landed, disarmed).
  2. Camera frames are labelled `simulated` for the whole flight and never stop.
  3. Every kind of staged target is found: at least one confirmed track whose
     label is among that kind's expected labels (e.g. person; car or truck).
  4. The detections are stored in this run's session directory.
  5. No CRITICAL alert is raised.
Reported but not pass/fail: inference latency, which labels were seen, how many
individual targets (the count of distinct track IDs is an upper bound, not a
count of objects).

Reset: the vehicle ends landed at home; run it again. `--rehearsal` skips the
"simulated camera" requirement so the script can be exercised with a video
file; a rehearsal can never count as a pass of Demo 2.

The drone flies a fixed route. Detections are displayed and logged only; no
code path steers the vehicle toward anything it sees.
"""

from __future__ import annotations

import argparse
import json
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--api", default="http://127.0.0.1:8000")
    parser.add_argument("--token", required=True, help="operator token (AEGIS_OPERATOR_TOKEN)")
    parser.add_argument("--timeout", type=float, default=420.0)
    parser.add_argument("--rehearsal", action="store_true", help="allow a non-simulated camera")
    args = parser.parse_args()

    system = call(args.api, "/api/system")
    print(f"mode={system['mode']} provenance={system['provenance']} source={system['source']}")
    if system["provenance"] != "simulated" or not system.get("simulator_attested"):
        reason = system.get("attestation_error") or f"provenance is {system['provenance']}"
        raise SystemExit(
            f"Refusing to run: not attached to an attested simulator.\nReason: {reason}"
        )

    scenario = call(args.api, "/api/scenario")
    props = scenario.get("props", [])
    if not props:
        raise SystemExit(
            f"Scenario '{scenario['scenario_id']}' has no staged targets. Start PX4 and the "
            "backend with the urban scenario (see this script's header)."
        )
    expected: dict[str, set[str]] = {}
    for prop in props:
        expected.setdefault(prop["kind"], set()).update(prop["expected_labels"])

    perception = call(args.api, "/api/perception")
    if not perception["enabled"]:
        raise SystemExit(f"Perception is off: {perception['reason']}")
    for _ in range(20):  # wait for the first frames
        if perception.get("provenance"):
            break
        time.sleep(0.5)
        perception = call(args.api, "/api/perception")
    camera_provenance = perception.get("provenance")
    print(
        f"camera={perception['camera_source']} provenance={camera_provenance} "
        f"detector={perception['detector']}"
    )
    if camera_provenance != "simulated" and not args.rehearsal:
        raise SystemExit(
            f"Refusing to run: camera frames are '{camera_provenance}', not 'simulated'. "
            "Start the Gazebo camera bridge, or pass --rehearsal to exercise the script."
        )

    mission = scenario["demo_mission"]
    result = call(args.api, "/api/missions", args.token, mission)
    print("submit:", result["status"]["phase"], "-", result["status"]["message"])

    seen: dict[str, dict[str, Any]] = {}  # label -> {"track_ids": set, "best_confidence": float}
    camera_provenances: set[str] = set()
    critical: set[str] = set()
    critical_details: list[str] = []
    camera_lost = False
    polls = 0
    last_line = ""
    status = result["status"]
    started = time.monotonic()
    if result["accepted"]:
        while time.monotonic() - started < args.timeout:
            snap = call(args.api, "/api/state")
            status = snap["mission"]
            codes = {a["code"] for a in snap["alerts"]}
            for alert in snap["alerts"]:
                if alert["severity"] == "critical" and alert["code"] not in critical:
                    critical.add(alert["code"])
                    critical_details.append(
                        f"{alert['code']} at {time.monotonic() - started:.1f}s "
                        f"({status['phase']}): {alert['message']}"
                    )
                    print(
                        f"[{time.monotonic() - started:6.1f}s] CRITICAL ALERT {critical_details[-1]}"
                    )
            camera_lost |= "camera.no_frames" in codes
            latest = call(args.api, "/api/perception/latest")["result"]
            if latest:
                polls += 1
                camera_provenances.add(latest["provenance"])
                for track in latest["tracks"]:
                    if track["state"] == "tentative":
                        continue
                    entry = seen.setdefault(
                        track["label"], {"track_ids": set(), "best_confidence": 0.0}
                    )
                    entry["track_ids"].add(track["track_id"])
                    entry["best_confidence"] = max(entry["best_confidence"], track["confidence"])
            line = f"{status['phase']:>10}  {status['message']}  seen={sorted(seen)}"
            if line != last_line:
                print(f"[{time.monotonic() - started:6.1f}s] {line}")
                last_line = line
            if status["phase"] in ("completed", "failed", "stalled", "cancelled"):
                break
            time.sleep(0.2)

    final = call(args.api, "/api/state")["state"]
    perception = call(args.api, "/api/perception")
    session = perception.get("recording")
    stored = 0
    if session:
        log_file = REPO_ROOT / "data" / "generated" / "sessions" / session / "detections.jsonl"
        if log_file.is_file():
            with log_file.open() as f:
                stored = sum(1 for _ in f)

    found_kinds = {kind: sorted(labels & set(seen)) for kind, labels in expected.items()}
    criteria = {
        "1_mission_completed": bool(result["accepted"])
        and status["phase"] == "completed"
        and final["armed"] is False,
        "2_camera_simulated_and_continuous": camera_provenances == {"simulated"}
        and not camera_lost,
        "3_every_target_kind_detected": all(found_kinds.values()),
        "4_detections_stored": stored > 0,
        "5_no_critical_alerts": not critical,
    }
    passed = all(criteria.values()) and not args.rehearsal
    report = {
        "demo": "demo2_ai_detection",
        "rehearsal": args.rehearsal,
        "scenario": scenario["scenario_id"],
        "vehicle_provenance": system["provenance"],
        "vehicle_source": system["source"],
        "camera_provenances_seen": sorted(camera_provenances),
        "detector": perception.get("detector"),
        "finished_at_unix_s": time.time(),
        "duration_s": round(time.monotonic() - started, 1),
        "criteria": criteria,
        "passed": passed,
        "staged_targets": {kind: sum(p["kind"] == kind for p in props) for kind in expected},
        "expected_labels_by_kind": {k: sorted(v) for k, v in expected.items()},
        "matched_labels_by_kind": found_kinds,
        "labels_seen": {
            label: {
                "distinct_track_ids": len(entry["track_ids"]),
                "best_confidence": round(entry["best_confidence"], 3),
            }
            for label, entry in sorted(seen.items())
        },
        "inference_latency_ms_p50": perception.get("inference_latency_ms_p50"),
        "inference_latency_ms_p95": perception.get("inference_latency_ms_p95"),
        "pipeline_fps": perception.get("fps"),
        "perception_polls": polls,
        "session": session,
        "stored_detection_frames": stored,
        "critical_alerts": critical_details,
        "final_mission_status": status,
    }
    out_dir = REPO_ROOT / "data" / "generated" / "demo_results"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"demo2_{time.strftime('%Y%m%dT%H%M%S')}.json"
    out.write_text(json.dumps(report, indent=2))

    for name, ok in criteria.items():
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    if status["phase"] == "cancelled":
        print(
            f"  note: the mission was interrupted ({status['message']}), so criterion 1 cannot pass"
        )
    for detail in critical_details:
        print(f"  critical alert: {detail}")
    for kind, labels in found_kinds.items():
        want = " or ".join(sorted(expected[kind]))
        print(f"  {kind}: expected {want}; matched {labels or 'nothing'}")
    print(f"  labels seen: {report['labels_seen']}")
    if session:
        print(
            f"  replay: AEGIS_MODE=replay AEGIS_REPLAY_SESSION={session} "
            "bash scripts/run_backend.sh"
        )
    verdict = "REHEARSAL (not a pass)" if args.rehearsal else ("PASSED" if passed else "FAILED")
    print(f"{verdict} - report: {out.relative_to(REPO_ROOT)}")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
