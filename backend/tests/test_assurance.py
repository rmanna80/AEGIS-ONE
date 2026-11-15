"""Requirements, hazards, traceability and result comparison."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from aegis.assurance import (
    FAILED,
    NOT_VERIFIED,
    VERIFIED_SIM,
    VERIFIED_UNIT,
    check_links,
    load,
    read_junit,
    scenario_runs,
    trace,
)
from aegis.trust.compare import compare_suites

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_every_requirement_links_to_checks_that_exist_and_to_a_hazard() -> None:
    """Runs in CI: a renamed test or criterion breaks the trace here, not silently."""
    assert check_links(REPO_ROOT) == []


def test_link_problems_are_found(tmp_path: Path) -> None:
    root = tmp_path
    (root / "assurance").mkdir()
    (root / "trust/scenarios").mkdir(parents=True)
    (root / "trust/scenarios/s.json").write_text(
        json.dumps({"id": "s", "criteria": [{"id": "C1"}]})
    )
    (root / "backend/tests").mkdir(parents=True)
    (root / "backend/tests/test_x.py").write_text("def test_real():\n    pass\n")
    requirements = [
        {
            "id": "R-1",
            "statement": "",
            "safety_critical": True,
            "verified_by": [
                {"kind": "unit_test", "ref": "backend/tests/test_x.py::test_gone"},
                {"kind": "scenario", "ref": "s", "criterion": "C9"},
                {"kind": "scenario", "ref": "nope", "criterion": "C1"},
            ],
        },
        {"id": "R-1", "statement": "", "safety_critical": False, "verified_by": []},
    ]
    (root / "assurance/requirements.json").write_text(json.dumps({"requirements": requirements}))
    (root / "assurance/hazards.json").write_text(
        json.dumps({"hazards": [{"id": "H-1", "mitigated_by": ["R-2"], "residual": ""}]})
    )
    problems = check_links(root)
    assert "duplicate requirement R-1" in problems
    assert "R-1: no test backend/tests/test_x.py::test_gone" in problems
    assert "R-1: s has no C9" in problems and "R-1: no scenario nope" in problems
    assert "H-1: no requirement R-2" in problems and "R-1 has no evidence linked" in problems


def junit_all(outcome: str = "passed") -> dict[str, str]:
    requirements, _ = load(REPO_ROOT)
    return {
        item["ref"]: outcome
        for r in requirements
        for item in r["verified_by"]
        if item["kind"] == "unit_test"
    }


def runs_all(status: str = "PASS", commit: str = "abc1234") -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for path in (REPO_ROOT / "trust/scenarios").glob("*.json"):
        doc = json.loads(path.read_text())
        criteria = [{"id": c["id"], "status": status} for c in doc["criteria"]]
        out[doc["id"]] = {
            "commit": commit,
            "uncommitted_changes": False,
            "provenance": ["simulated"],
            "runs": [{"criteria": criteria}, {"criteria": criteria}],
        }
    return out


def test_all_evidence_passing_is_verified_at_the_level_of_its_evidence() -> None:
    report = trace(REPO_ROOT, junit_all(), runs_all())
    statuses = {r["id"]: r["status"] for r in report["requirements"]}
    assert statuses["R-LINK-1"] == VERIFIED_SIM  # scenario evidence
    assert statuses["R-SEC-2"] == VERIFIED_UNIT  # unit tests only
    assert report["safety_critical_not_verified"] == []
    assert "score" not in json.dumps(report["counts"]).lower()


def test_one_failed_run_fails_the_requirement_and_it_is_listed_first() -> None:
    runs = runs_all()
    bad = copy.deepcopy(runs["link-loss-detection"]["runs"][1])
    for c in bad["criteria"]:
        if c["id"] == "C2":
            c["status"] = "FAIL"
    runs["link-loss-detection"]["runs"][1] = bad
    report = trace(REPO_ROOT, junit_all(), runs)
    first = report["requirements"][0]
    assert first["id"] == "R-LINK-1" and first["status"] == FAILED
    detail = next(e for e in first["evidence"] if e.get("criterion") == "C2")["detail"]
    assert detail.startswith("1 of 2 runs passed")
    assert "R-LINK-1" in report["safety_critical_not_verified"]
    assert next(h for h in report["hazards"] if h["id"] == "H-1")["open"]


def test_missing_or_skipped_evidence_is_not_verified() -> None:
    junit = junit_all()
    junit["backend/tests/test_api.py::test_physical_mode_cannot_be_configured"] = "skipped"
    runs = runs_all()
    del runs["gnss-loss-detection"]
    statuses = {r["id"]: r["status"] for r in trace(REPO_ROOT, junit, runs)["requirements"]}
    assert statuses["R-SAFE-2"] == NOT_VERIFIED and statuses["R-GNSS-1"] == NOT_VERIFIED


def test_the_open_port_hazard_stays_open_even_when_its_requirements_pass() -> None:
    report = trace(REPO_ROOT, junit_all(), runs_all())
    assert next(h for h in report["hazards"] if h["id"] == "H-5")["open"]


def test_junit_and_newest_commit_runs_are_read(tmp_path: Path) -> None:
    (tmp_path / "junit.xml").write_text(
        "<testsuites><testsuite>"
        '<testcase classname="backend.tests.test_a" name="test_ok"/>'
        '<testcase classname="backend.tests.test_a" name="test_p[1]"/>'
        '<testcase classname="backend.tests.test_a" name="test_p[2]"><failure/></testcase>'
        '<testcase classname="backend.tests.test_a" name="test_s"><skipped/></testcase>'
        "</testsuite></testsuites>"
    )
    junit = read_junit(tmp_path / "junit.xml")
    assert junit == {
        "backend/tests/test_a.py::test_ok": "passed",
        "backend/tests/test_a.py::test_p": "failed",
        "backend/tests/test_a.py::test_s": "skipped",
    }
    for i, (commit, started) in enumerate([("old", 1.0), ("new", 2.0), ("new", 3.0)]):
        (tmp_path / f"r{i}.json").write_text(
            json.dumps(
                {
                    "schema": "aegis.trust-result/1",
                    "scenario_id": "s",
                    "started_utc_s": started,
                    "software": {"git_commit": commit},
                    "simulated_or_physical": "simulated",
                    "criteria": [],
                }
            )
        )
    entry = scenario_runs([tmp_path])["s"]
    assert entry["commit"] == "new" and len(entry["runs"]) == 2


def suite(passed: str, median: float, low: float, high: float, runs: int = 3) -> dict[str, Any]:
    return {
        "suite_id": "core",
        "scenarios": {
            "s": {
                "scenario_sha256": ["x"],
                "criteria": {"C2": {"passed": passed, "description": "d", "safety_critical": True}},
                "measurements": {
                    "alert_raised_after_s:a": {
                        "runs_measured": runs,
                        "min": low,
                        "median": median,
                        "max": high,
                    }
                },
            }
        },
    }


def test_a_drop_in_passes_is_a_regression_and_timings_are_read_conservatively() -> None:
    result = compare_suites(suite("3 of 3", 0.2, 0.2, 0.3), suite("2 of 3", 0.25, 0.2, 0.4))
    assert result["regressions"] == 1 and result["safety_critical_regressions"] == 1
    assert result["timings"][0]["reading"] == "no difference shown by these runs"
    faster = compare_suites(suite("3 of 3", 2.0, 1.8, 2.4), suite("3 of 3", 1.0, 0.9, 1.1))
    assert faster["regressions"] == 0
    assert faster["timings"][0]["reading"] == "later median below the whole earlier range"
    few = compare_suites(suite("1 of 1", 2.0, 2.0, 2.0, 1), suite("1 of 1", 1.0, 1.0, 1.0, 1))
    assert few["timings"][0]["reading"] == "too few runs to compare"
    fixed = compare_suites(suite("1 of 3", 0.2, 0.2, 0.2), suite("3 of 3", 0.2, 0.2, 0.2))
    assert fixed["changes"][0]["kind"] == "FIXED"
