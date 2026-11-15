"""Requirements, hazards and the evidence for each (traceability).

Statuses for a requirement, weakest evidence wins:

* FAILED: some linked check failed in its latest results.
* NOT VERIFIED: some linked check has no result, was skipped, or gave no verdict.
* VERIFIED IN SIMULATION: every linked check passed, and at least one is a scenario
  flown against PX4 SITL.
* VERIFIED BY UNIT TESTS: every linked check passed, all of them unit tests.

There is no overall score. A requirement is never "partly" verified, and a failed
safety-critical requirement is listed first, whatever the others say.
"""

from __future__ import annotations

import ast
import json
import xml.etree.ElementTree as ElementTree
from pathlib import Path
from typing import Any

FAILED = "FAILED"
NOT_VERIFIED = "NOT VERIFIED"
VERIFIED_SIM = "VERIFIED IN SIMULATION"
VERIFIED_UNIT = "VERIFIED BY UNIT TESTS"
ORDER = [FAILED, NOT_VERIFIED, VERIFIED_SIM, VERIFIED_UNIT]


def load(root: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    requirements = json.loads((root / "assurance/requirements.json").read_text())["requirements"]
    hazards = json.loads((root / "assurance/hazards.json").read_text())["hazards"]
    return requirements, hazards


def _test_functions(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    return {
        node.name
        for node in tree.body
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name.startswith("test")
    }


def check_links(root: Path) -> list[str]:
    """Problems with the requirement and hazard files themselves (empty when sound)."""
    requirements, hazards = load(root)
    problems: list[str] = []
    ids = [r["id"] for r in requirements]
    problems += [f"duplicate requirement {i}" for i in {i for i in ids if ids.count(i) > 1}]
    scenarios: dict[str, set[str]] = {}
    for path in (root / "trust/scenarios").glob("*.json"):
        doc = json.loads(path.read_text())
        scenarios[doc["id"]] = {c["id"] for c in doc["criteria"]}
    functions: dict[str, set[str]] = {}
    for r in requirements:
        if not r.get("verified_by"):
            problems.append(f"{r['id']} has no evidence linked")
        for item in r.get("verified_by", []):
            if item["kind"] == "scenario":
                if item["ref"] not in scenarios:
                    problems.append(f"{r['id']}: no scenario {item['ref']}")
                elif item["criterion"] not in scenarios[item["ref"]]:
                    problems.append(f"{r['id']}: {item['ref']} has no {item['criterion']}")
            elif item["kind"] == "unit_test":
                file, _, name = item["ref"].partition("::")
                if file not in functions:
                    path = root / file
                    functions[file] = _test_functions(path) if path.is_file() else set()
                if name not in functions[file]:
                    problems.append(f"{r['id']}: no test {item['ref']}")
            else:
                problems.append(f"{r['id']}: unknown evidence kind {item['kind']}")
    known = set(ids)
    used: set[str] = set()
    for h in hazards:
        for rid in h["mitigated_by"]:
            used.add(rid)
            if rid not in known:
                problems.append(f"{h['id']}: no requirement {rid}")
    problems += [f"{rid} addresses no hazard" for rid in sorted(known - used)]
    return problems


def read_junit(path: Path) -> dict[str, str]:
    """pytest JUnit XML -> {"backend/tests/test_x.py::test_y": passed|failed|skipped}."""
    results: dict[str, str] = {}
    for case in ElementTree.parse(path).getroot().iter("testcase"):
        module = case.get("classname", "").replace(".", "/") + ".py"
        name = case.get("name", "").split("[", 1)[0]
        key = f"{module}::{name}"
        if case.find("failure") is not None or case.find("error") is not None:
            outcome = "failed"
        elif case.find("skipped") is not None:
            outcome = "skipped"
        else:
            outcome = "passed"
        # Parametrised cases share a key: any failure fails it, any skip leaves it unverified.
        previous = results.get(key)
        if previous == "failed" or outcome == "failed":
            results[key] = "failed"
        elif previous == "skipped" or outcome == "skipped":
            results[key] = "skipped"
        else:
            results[key] = "passed"
    return results


def scenario_runs(result_dirs: list[Path]) -> dict[str, dict[str, Any]]:
    """For each scenario: every run made with the newest software commit that has runs."""
    runs: dict[str, list[dict[str, Any]]] = {}
    for folder in result_dirs:
        for path in folder.glob("*.json"):
            try:
                doc = json.loads(path.read_text())
            except ValueError:
                continue
            if doc.get("schema") != "aegis.trust-result/1":
                continue
            doc["_file"] = str(path)
            runs.setdefault(doc["scenario_id"], []).append(doc)
    latest: dict[str, dict[str, Any]] = {}
    for scenario_id, items in runs.items():
        items.sort(key=lambda d: d["started_utc_s"])
        newest = items[-1]
        commit = (newest.get("software") or {}).get("git_commit")
        same = [d for d in items if (d.get("software") or {}).get("git_commit") == commit]
        latest[scenario_id] = {
            "commit": commit,
            "uncommitted_changes": any(
                (d.get("software") or {}).get("git_uncommitted_changes") for d in same
            ),
            "provenance": sorted({d.get("simulated_or_physical") for d in same}),
            "runs": same,
        }
    return latest


def _evidence(
    item: dict[str, Any], junit: dict[str, str], runs: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    if item["kind"] == "unit_test":
        outcome = junit.get(item["ref"])
        status = {"passed": "PASS", "failed": "FAIL", "skipped": "SKIPPED"}.get(
            outcome or "", "NO RESULT"
        )
        return {**item, "status": status, "detail": outcome or "not run"}
    entry = runs.get(item["ref"])
    if entry is None:
        return {**item, "status": "NO RESULT", "detail": "no recorded run"}
    statuses = [
        c["status"]
        for run in entry["runs"]
        for c in run["criteria"]
        if c["id"] == item["criterion"]
    ]
    passed = statuses.count("PASS")
    detail = f"{passed} of {len(statuses)} runs passed at commit {str(entry['commit'])[:7]}"
    if entry["uncommitted_changes"]:
        detail += " (with uncommitted changes)"
    if "FAIL" in statuses:
        status = "FAIL"
    elif statuses and passed == len(statuses):
        status = "PASS"
    else:
        status = "NO VERDICT"
    return {**item, "status": status, "detail": detail, "data": entry["provenance"]}


def trace(root: Path, junit: dict[str, str], runs: dict[str, dict[str, Any]]) -> dict[str, Any]:
    requirements, hazards = load(root)
    traced: list[dict[str, Any]] = []
    for r in requirements:
        evidence = [_evidence(item, junit, runs) for item in r["verified_by"]]
        kinds = {e["kind"] for e in evidence}
        if any(e["status"] == "FAIL" for e in evidence):
            status = FAILED
        elif any(e["status"] != "PASS" for e in evidence):
            status = NOT_VERIFIED
        elif "scenario" in kinds:
            status = VERIFIED_SIM
        else:
            status = VERIFIED_UNIT
        traced.append({**r, "status": status, "evidence": evidence})
    traced.sort(key=lambda r: (ORDER.index(r["status"]), not r["safety_critical"], r["id"]))
    by_id = {r["id"]: r for r in traced}
    traced_hazards = []
    for h in hazards:
        weakest = [by_id[rid]["status"] for rid in h["mitigated_by"] if rid in by_id]
        open_ = h["residual"].startswith("OPEN") or any(
            s in (FAILED, NOT_VERIFIED) for s in weakest
        )
        traced_hazards.append(
            {
                **h,
                "requirement_statuses": dict(zip(h["mitigated_by"], weakest, strict=False)),
                "open": open_,
            }
        )
    counts = {s: sum(1 for r in traced if r["status"] == s) for s in ORDER}
    return {
        "schema": "aegis.traceability/1",
        "counts": counts,
        "safety_critical_not_verified": [
            r["id"]
            for r in traced
            if r["safety_critical"] and r["status"] in (FAILED, NOT_VERIFIED)
        ],
        "requirements": traced,
        "hazards": traced_hazards,
        "limits": [
            "Verified means the linked checks passed. It does not mean the requirement is the "
            "right one, that the checks are sufficient, or that the system is safe.",
            "Scenario evidence is from PX4 SITL. Nothing here has been checked on physical "
            "hardware.",
            "Requirements and hazards were written by the developers; there has been no "
            "formal hazard analysis and no independent review.",
        ],
    }


def to_markdown(report: dict[str, Any], header: str) -> str:
    lines = [header, ""]
    counts = report["counts"]
    lines.append(
        "Requirements: "
        + ", ".join(f"{n} {status.lower()}" for status, n in counts.items() if n)
        + ". No overall score is given."
    )
    if report["safety_critical_not_verified"]:
        lines.append(
            "\n**Safety-critical requirements not verified: "
            + ", ".join(report["safety_critical_not_verified"])
            + ".**"
        )
    lines += [
        "",
        "## Requirements",
        "",
        "| ID | Safety-critical | Requirement | Status | Evidence |",
        "| --- | --- | --- | --- | --- |",
    ]
    for r in report["requirements"]:
        evidence = "<br>".join(
            f"{e['status']}: {e['ref']}"
            + (f" {e['criterion']}" if e.get("criterion") else "")
            + (f" ({e['detail']})" if e["kind"] == "scenario" else "")
            for e in r["evidence"]
        )
        lines.append(
            f"| {r['id']} | {'yes' if r['safety_critical'] else 'no'} | {r['statement']} | "
            f"{r['status']} | {evidence} |"
        )
    lines += [
        "",
        "## Hazards",
        "",
        "| ID | Hazard | Requirements | Open | Residual |",
        "| --- | --- | --- | --- | --- |",
    ]
    for h in report["hazards"]:
        reqs = ", ".join(f"{rid} ({s.lower()})" for rid, s in h["requirement_statuses"].items())
        lines.append(
            f"| {h['id']} | {h['hazard']} | {reqs} | {'yes' if h['open'] else 'no'} | "
            f"{h['residual']} |"
        )
    lines += ["", "## Limits", ""] + [f"- {text}" for text in report["limits"]]
    return "\n".join(lines) + "\n"
