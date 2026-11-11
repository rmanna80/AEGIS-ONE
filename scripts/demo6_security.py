#!/usr/bin/env python3
"""Demo 6 - Anomaly detection and security monitoring.

Flies the scenario's mission and, during the flight, deliberately corrupts AEGIS's
own copy of the telemetry in five ways. The vehicle is never touched by those
faults: it keeps flying on correct data. The demo checks that each kind of bad data
is noticed, that a clean flight raises nothing, that a control input from outside
the operator's command path is noticed, that guessing the operator token is
noticed, and that the command audit log shows when it has been edited.

    bash scripts/run_px4.sh sih
    AEGIS_FAULT_INJECTION=true bash scripts/run_backend.sh
    python scripts/demo6_security.py --token "$AEGIS_OPERATOR_TOKEN"

Fault injection is off unless the backend is started with AEGIS_FAULT_INJECTION=true,
needs the operator token, and is refused unless the vehicle is an attested simulator.

Acceptance criteria:
  1. In 12 s of normal flight, no anomaly or security alert is raised.
  2. Each injected telemetry fault raises its expected alert within 15 s:
     gps_jump -> nav.position_jump, gps_drift -> nav.position_drift,
     freeze -> telemetry.frozen, battery_drop -> battery.implausible,
     clock_rewind -> telemetry.clock_reversed.
  3. Every altered snapshot was labelled with the fault that altered it.
  4. The vehicle kept flying its mission through all five telemetry faults
     (the monitors only alert; they did not command anything).
  5. A Hold from outside the operator's command path raises control.unexpected_mode.
  6. Five requests with a wrong token raise security.auth_failures and are refused.
  7. The audit log verifies, and an edited copy of it is detected.
  8. After the operator's Return, the vehicle lands at home.
Reported, not pass/fail: time from injection to alert for each fault.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from pathlib import Path

from _demo_common import REPO_ROOT, Api, require_simulator, write_report

sys.path.insert(0, str(REPO_ROOT / "backend"))
from aegis.security import verify_audit_log  # noqa: E402

FAULTS = [
    ("gps_drift", "nav.position_drift", {"rate_m_s": 3.0, "duration_s": 9.0}),
    ("freeze", "telemetry.frozen", {"duration_s": 5.0}),
    ("battery_drop", "battery.implausible", {"to_fraction": 0.2, "duration_s": 4.0}),
    ("clock_rewind", "telemetry.clock_reversed", {"seconds": 30.0, "duration_s": 4.0}),
    ("gps_jump", "nav.position_jump", {"east_m": 25.0, "duration_s": 5.0}),
]
MONITORED = (
    "nav.",
    "telemetry.frozen",
    "telemetry.clock",
    "battery.implausible",
    "control.",
    "mission.off_route",
    "security.",
)


def alert_codes(api: Api) -> set[str]:
    return {a["code"] for a in api.call("/api/state")["alerts"]}


def wait_for(api: Api, code: str, timeout_s: float) -> tuple[float | None, bool]:
    """Seconds until ``code`` is active (None if never), and whether the snapshots seen
    meanwhile carried a fault label."""
    started = time.monotonic()
    labelled = False
    while time.monotonic() - started < timeout_s:
        snap = api.call("/api/state")
        if snap["state"] and snap["state"].get("injected_faults"):
            labelled = True
        if code in {a["code"] for a in snap["alerts"]}:
            return time.monotonic() - started, labelled
        time.sleep(0.2)
    return None, labelled


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--api", default="http://127.0.0.1:8000")
    parser.add_argument("--token", required=True)
    args = parser.parse_args()
    api = Api(args.api, args.token)

    system = require_simulator(api)
    security = api.call("/api/security")
    if not security["faults"]["enabled"]:
        raise SystemExit(
            "Fault injection is off. Start the backend with AEGIS_FAULT_INJECTION=true.\n"
            "Nothing was sent to the vehicle."
        )
    if not security["anomaly"]["enabled"]:
        raise SystemExit("Anomaly detection is off (AEGIS_ANOMALY=false).")
    scenario = api.call("/api/scenario")

    print("(this script, standing in for the operator, submits the mission)")
    result = api.call("/api/missions", scenario["demo_mission"])
    print("submit:", result["status"]["phase"], "-", result["status"]["message"])
    if not result["accepted"]:
        raise SystemExit("The mission was not accepted; nothing to test.")
    for _ in range(240):  # until it is flying the mission, clear of take-off
        state = api.call("/api/state")["state"]
        if state["flight_mode"] == "MISSION" and (state["relative_altitude_m"] or 0) > 8.0:
            break
        time.sleep(0.5)

    print("12 s of normal flight...")
    baseline: set[str] = set()
    end = time.monotonic() + 12.0
    while time.monotonic() < end:
        baseline |= {c for c in alert_codes(api) if c.startswith(MONITORED)}
        time.sleep(0.5)
    print("  alerts in normal flight:", sorted(baseline) or "none")

    latencies: dict[str, float | None] = {}
    labelled: dict[str, bool] = {}
    for kind, expected, params in FAULTS:
        # An alert left over from the previous fault must not be counted as this one's.
        clear_by = time.monotonic() + 30.0
        while expected in alert_codes(api) and time.monotonic() < clear_by:
            time.sleep(0.5)
        if expected in alert_codes(api):
            print(f"  {kind:<13} skipped: {expected} was still up from an earlier fault")
            latencies[kind], labelled[kind] = None, False
            continue
        api.call("/api/faults", {"kind": kind, **params})
        latency, was_labelled = wait_for(api, expected, 15.0)
        latencies[kind], labelled[kind] = latency, was_labelled
        print(
            f"  {kind:<13} -> {expected:<26} "
            + (f"raised after {latency:.1f} s" if latency is not None else "NOT RAISED")
            + ("" if was_labelled else "  (snapshot was not labelled)")
        )
        time.sleep(params["duration_s"] + 1.0 - min(latency or 0.0, params["duration_s"]))
    after_faults = api.call("/api/state")
    still_flying = after_faults["mission"]["phase"] in ("active", "returning")
    print(f"  mission after the telemetry faults: {after_faults['mission']['phase']}")

    api.call("/api/faults", {"kind": "external_hold"})
    hold_latency, _ = wait_for(api, "control.unexpected_mode", 15.0)
    print(
        "  external_hold -> control.unexpected_mode   "
        + (f"raised after {hold_latency:.1f} s" if hold_latency is not None else "NOT RAISED")
    )

    wrong = Api(args.api, "not-the-token")
    refusals = [
        wrong.call("/api/vehicle/hold", post=True, check=False).get("http_error") for _ in range(5)
    ]
    auth_latency, _ = wait_for(api, "security.auth_failures", 10.0)
    print(
        f"  5 wrong tokens: refused {refusals.count(401)} of 5, security.auth_failures "
        + (f"raised after {auth_latency:.1f} s" if auth_latency is not None else "NOT RAISED")
    )

    print("(this script, standing in for the operator, commands Return)")
    api.call("/api/vehicle/return", post=True)
    landed = False
    for _ in range(600):
        state = api.call("/api/state")["state"]
        p = state["position_enu_m"]
        if state["armed"] is False and abs(p["x"]) < 3.0 and abs(p["y"]) < 3.0:
            landed = True
            break
        time.sleep(0.5)

    security = api.call("/api/security")
    audit_file = Path(security["audit"]["file"]) if security["audit"]["file"] else None
    audit_ok = tamper_caught = False
    events: list[str] = []
    if audit_file is not None and audit_file.is_file():
        verdict = verify_audit_log(audit_file)
        audit_ok = bool(verdict["valid"]) and verdict["records"] > 0
        lines = audit_file.read_text().splitlines()
        events = [json.loads(line)["event"] for line in lines]
        with tempfile.TemporaryDirectory() as folder:
            copy = Path(folder) / "audit.jsonl"
            record = json.loads(lines[len(lines) // 2])
            record["path"] = "/api/edited"
            lines[len(lines) // 2] = json.dumps(record)
            copy.write_text("\n".join(lines) + "\n")
            tamper_caught = not verify_audit_log(copy)["valid"]
        print(
            f"  audit log: {verdict['records']} records, chain "
            f"{'intact' if audit_ok else 'BROKEN'}; edited copy "
            f"{'detected' if tamper_caught else 'NOT detected'}"
        )

    criteria = {
        "1_no_alert_in_normal_flight": not baseline,
        "2_every_telemetry_fault_detected": all(v is not None for v in latencies.values()),
        "3_altered_snapshots_labelled": all(labelled.values()),
        "4_vehicle_kept_flying_through_telemetry_faults": still_flying,
        "5_outside_hold_detected": hold_latency is not None,
        "6_token_guessing_refused_and_detected": refusals.count(401) == 5
        and auth_latency is not None,
        "7_audit_log_verifies_and_shows_edits": audit_ok
        and tamper_caught
        and "auth_failed" in events
        and "command_outcome" in events,
        "8_landed_at_home_after_operator_return": landed,
    }
    write_report(
        "demo6_security",
        {
            "scenario": scenario["scenario_id"],
            "vehicle_source": system["source"],
            "alerts_in_normal_flight": sorted(baseline),
            "detection_latency_s": {
                **{k: None if v is None else round(v, 1) for k, v in latencies.items()},
                "external_hold": None if hold_latency is None else round(hold_latency, 1),
                "auth_failures": None if auth_latency is None else round(auth_latency, 1),
            },
            "detections": security["anomaly"]["detections"],
            "audit_records": len(events),
            "posture": security["posture"],
        },
        criteria,
        all(criteria.values()),
    )
    return 0 if all(criteria.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
