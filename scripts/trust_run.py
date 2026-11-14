#!/usr/bin/env python3
"""AEGIS TRUST: run test scenarios against a live backend and keep structured results.

    bash scripts/run_px4.sh sih
    AEGIS_FAULT_INJECTION=true bash scripts/run_backend.sh
    python scripts/trust_run.py --token "$AEGIS_OPERATOR_TOKEN" trust/scenarios/*.json

Each scenario flies the simulated vehicle, injects one controlled fault into AEGIS's own
copy of the telemetry, measures what the monitors did, and writes a result with the
evidence behind it to data/generated/trust_results/. Statuses: PASS, FAIL, INCONCLUSIVE,
BLOCKED (the environment does not fit the scenario), NOT RUN, SKIPPED.

A PASS is evidence about this software, on this machine, under these conditions. It is
not a certification and does not show that a system is safe.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from _demo_common import REPO_ROOT, Api

sys.path.insert(0, str(REPO_ROOT / "backend"))
from aegis.trust.contracts import Status  # noqa: E402
from aegis.trust.runner import Runner, load_scenario  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("scenarios", nargs="+", type=Path)
    parser.add_argument("--api", default="http://127.0.0.1:8000")
    parser.add_argument("--token", required=True)
    args = parser.parse_args()
    runner = Runner(Api(args.api, args.token))
    out_dir = REPO_ROOT / "data" / "generated" / "trust_results"
    out_dir.mkdir(parents=True, exist_ok=True)

    statuses = []
    for path in args.scenarios:
        try:
            scenario, digest = load_scenario(path)
        except Exception as exc:  # an invalid scenario is reported, not run
            print(f"\n{path}: NOT RUN - the scenario file is invalid: {exc}")
            statuses.append(Status.NOT_RUN)
            continue
        print(f"\n== {scenario.id}: {scenario.title}")
        print(f"   why: {scenario.purpose}")
        result = runner.run(scenario, digest)
        for c in result.criteria:
            flag = " [safety-critical]" if c.safety_critical else ""
            print(f"   {c.status.value:<12} {c.id} {c.description}{flag}")
            print(f"                expected: {c.expected}")
            print(f"                observed: {c.observed}")
        print(
            f"   RESULT: {result.status.value} ({result.status_reason}), "
            f"{result.simulated_or_physical} data, {result.duration_s:.0f} s"
        )
        if result.evidence.get("incident_id"):
            print(
                f"   evidence: incident {result.evidence['incident_id']} "
                f"(manifest {str(result.evidence.get('incident_manifest_sha256'))[:16]}...)"
            )
        out = out_dir / f"{scenario.id}_{time.strftime('%Y%m%dT%H%M%S')}.json"
        out.write_text(json.dumps(result.model_dump(mode="json", by_alias=True), indent=2))
        print(f"   saved: {out.relative_to(REPO_ROOT)}")
        statuses.append(result.status)

    print(
        "\nsummary: "
        + ", ".join(
            f"{sum(1 for s in statuses if s is status)} {status.value}"
            for status in Status
            if any(s is status for s in statuses)
        )
    )
    return 0 if all(s is Status.PASS for s in statuses) else 1


if __name__ == "__main__":
    sys.exit(main())
