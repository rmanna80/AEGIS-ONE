#!/usr/bin/env python3
"""Traceability: each requirement, the checks linked to it, and their latest results.

    python scripts/trace.py                       # runs the unit tests, reads scenario results
    python scripts/trace.py --junit results.xml   # use a test run you already have
    python scripts/trace.py --write-docs          # also update docs/assurance/TRACEABILITY.md

Scenario results are read from data/generated/trust_results/ and data/sample/trust_results/;
for each scenario the runs made with the newest software commit are used. Unit test
results come from a fresh pytest run unless --junit is given.

The report lists requirements weakest first. There is no overall score.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))

from aegis.assurance import check_links, read_junit, scenario_runs, to_markdown, trace  # noqa: E402


def git(*args: str) -> str:
    try:
        return subprocess.run(
            ["git", *args], cwd=REPO_ROOT, capture_output=True, text=True, timeout=10
        ).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--junit", type=Path, help="existing pytest JUnit XML")
    parser.add_argument("--write-docs", action="store_true")
    args = parser.parse_args()

    problems = check_links(REPO_ROOT)
    if problems:
        print("The requirement and hazard files have problems:")
        for problem in problems:
            print(f"  {problem}")
        return 2

    out_dir = REPO_ROOT / "data/generated/assurance"
    out_dir.mkdir(parents=True, exist_ok=True)
    junit_path = args.junit
    if junit_path is None:
        junit_path = out_dir / "junit.xml"
        print("Running the unit tests ...")
        subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "backend", f"--junitxml={junit_path}"],
            cwd=REPO_ROOT,
            capture_output=True,
        )
    junit = read_junit(junit_path)
    runs = scenario_runs(
        [REPO_ROOT / "data/generated/trust_results", REPO_ROOT / "data/sample/trust_results"]
    )
    report = trace(REPO_ROOT, junit, runs)
    commit = git("rev-parse", "--short", "HEAD")
    dirty = bool(git("status", "--porcelain"))
    report["generated_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    report["commit"] = commit + (" (with uncommitted changes)" if dirty else "")
    stamp = time.strftime("%Y%m%dT%H%M%S")
    (out_dir / f"trace_{stamp}.json").write_text(json.dumps(report, indent=2))
    header = (
        "# Traceability\n\n"
        f"Generated {report['generated_utc']} at commit {report['commit']} by "
        "`python scripts/trace.py`. Unit tests from that run; scenario results are the "
        "runs made with each scenario's newest software commit."
    )
    markdown = to_markdown(report, header)
    (out_dir / f"trace_{stamp}.md").write_text(markdown)
    if args.write_docs:
        target = REPO_ROOT / "docs/assurance/TRACEABILITY.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(markdown)
        print(f"updated {target.relative_to(REPO_ROOT)}")

    for status, count in report["counts"].items():
        print(f"  {count:>3}  {status}")
    for r in report["requirements"]:
        if r["status"] in ("FAILED", "NOT VERIFIED"):
            weak = [e for e in r["evidence"] if e["status"] != "PASS"]
            mark = " [safety-critical]" if r["safety_critical"] else ""
            print(f"  {r['status']}: {r['id']}{mark}")
            for e in weak:
                print(f"      {e['status']}: {e['ref']} {e.get('criterion', '')} ({e['detail']})")
    open_hazards = [h["id"] for h in report["hazards"] if h["open"]]
    print(f"  hazards with open items: {', '.join(open_hazards) or 'none'}")
    print(f"saved: {(out_dir / f'trace_{stamp}.md').relative_to(REPO_ROOT)}")
    return 1 if report["counts"]["FAILED"] else 0


if __name__ == "__main__":
    sys.exit(main())
