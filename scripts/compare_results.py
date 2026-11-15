#!/usr/bin/env python3
"""Compare two TRUST suite results: did any verdict change, did any timing move?

    python scripts/compare_results.py data/generated/trust_results/suite_core_A.json \
                                      data/generated/trust_results/suite_core_B.json

Exit status 1 if anything regressed. A timing is only reported as moved when the later
median is outside the whole earlier range; with a few runs, anything else is reported
as "no difference shown".
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))

from aegis.trust.compare import compare_suites  # noqa: E402


def commit(side: dict) -> str:
    software = side.get("software") or {}
    return str(software.get("git_commit") or "?")[:7] + (
        "+changes" if software.get("git_uncommitted_changes") else ""
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("before", type=Path)
    parser.add_argument("after", type=Path)
    args = parser.parse_args()
    result = compare_suites(json.loads(args.before.read_text()), json.loads(args.after.read_text()))
    print(f"before: commit {commit(result['before'])}   after: commit {commit(result['after'])}")
    if not result["changes"]:
        print("No verdict changed.")
    for change in result["changes"]:
        mark = " [safety-critical]" if change.get("safety_critical") else ""
        where = change["scenario"] + (f" {change['criterion']}" if "criterion" in change else "")
        print(f"  {change['kind']:<16} {where}{mark}: {change['detail']}")
    for t in result["timings"]:
        a, b = t["before"], t["after"]
        print(
            f"  {t['scenario']} {t['measurement']}: median {a['median']} [{a['min']}..{a['max']}]"
            f" -> {b['median']} [{b['min']}..{b['max']}]: {t['reading']}"
        )
    print(
        f"{result['regressions']} regression(s), "
        f"{result['safety_critical_regressions']} safety-critical"
    )
    return 1 if result["regressions"] else 0


if __name__ == "__main__":
    sys.exit(main())
