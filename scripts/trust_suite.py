#!/usr/bin/env python3
"""AEGIS TRUST: run a suite of scenarios, each several times, and summarise the spread.

    bash scripts/run_px4.sh sih
    AEGIS_FAULT_INJECTION=true bash scripts/run_backend.sh
    python scripts/trust_suite.py --token "$AEGIS_OPERATOR_TOKEN" trust/suites/core.json

Each run is saved on its own under data/generated/trust_results/, and the suite summary
next to them. The suite passes only if every run of every scenario passed; there is no
overall score. A suite of five scenarios run three times takes about half an hour.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from _demo_common import REPO_ROOT, Api

sys.path.insert(0, str(REPO_ROOT / "backend"))
from aegis.trust.contracts import Status, TestResult  # noqa: E402
from aegis.trust.runner import Runner, load_scenario  # noqa: E402
from aegis.trust.suite import (  # noqa: E402
    SUITE_RESULT_SCHEMA,
    load_suite,
    suite_status,
    summarise,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("suite", type=Path)
    parser.add_argument("--api", default="http://127.0.0.1:8000")
    parser.add_argument("--token", required=True)
    parser.add_argument("--repeats", type=int, help="override the suite's repeat count")
    args = parser.parse_args()

    suite = load_suite(args.suite)
    repeats = args.repeats or suite.repeats
    api = Api(args.api, args.token)
    runner = Runner(api, log=lambda _: None)
    out_dir = REPO_ROOT / "data" / "generated" / "trust_results"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%S")
    started = time.time()

    results: list[TestResult] = []
    files: list[str] = []
    print(f"Suite {suite.id}: {len(suite.scenarios)} scenarios x {repeats} runs")
    for name in suite.scenarios:
        scenario, digest = load_scenario(REPO_ROOT / name)
        for run in range(1, repeats + 1):
            result = runner.run(scenario, digest)
            results.append(result)
            out = out_dir / f"{scenario.id}_{time.strftime('%Y%m%dT%H%M%S')}_run{run}.json"
            out.write_text(json.dumps(result.model_dump(mode="json", by_alias=True), indent=2))
            files.append(str(out.relative_to(REPO_ROOT)))
            failed = [c.id for c in result.criteria if c.status is not Status.PASS]
            print(
                f"  {scenario.id:<28} run {run}/{repeats}: {result.status.value:<12} "
                f"{result.duration_s:5.0f} s" + (f"  not passed: {failed}" if failed else "")
            )
            if result.status is Status.BLOCKED:
                print(f"    blocked: {result.status_reason}; skipping this scenario's repeats")
                break

    status, why = suite_status(results)
    summary = summarise(results)
    session = api.call("/api/replay/session")
    document = {
        "schema": SUITE_RESULT_SCHEMA,
        "suite_id": suite.id,
        "title": suite.title,
        "status": status.value,
        "status_reason": why,
        "repeats": repeats,
        "started_utc_s": round(started, 3),
        "duration_s": round(time.time() - started, 1),
        "software": session.get("software"),
        "device": session.get("device"),
        "scenarios": summary,
        "run_files": files,
        "limitations": [
            "Runs on one simulator and one machine. The spread is of these runs only and is "
            "not a statistical guarantee.",
            "Pass limits were chosen by the developers, not derived from a hazard analysis.",
            "Not a safety certification.",
        ],
    }
    out = out_dir / f"suite_{suite.id}_{stamp}.json"
    out.write_text(json.dumps(document, indent=2))

    print(f"\nSUITE {status.value}: {why}")
    for scenario_id, entry in summary.items():
        print(f"\n  {scenario_id}: {entry['status']}  runs {entry['runs']}")
        for cid, c in entry["criteria"].items():
            mark = " [safety-critical]" if c["safety_critical"] else ""
            print(f"    {cid:<4} {c['passed']:<8} {c['description']}{mark}")
        for key, spread in entry["measurements"].items():
            print(
                f"    {key}: min {spread['min']}  median {spread['median']}  "
                f"max {spread['max']}  ({spread['runs_measured']} runs)"
            )
    print(f"\nsaved: {out.relative_to(REPO_ROOT)}")
    return 0 if status is Status.PASS else 1


if __name__ == "__main__":
    sys.exit(main())
