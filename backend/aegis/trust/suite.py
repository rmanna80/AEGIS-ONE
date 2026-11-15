"""Scenario suites: several scenarios, each run several times, summarised without a score.

A suite passes only if every run of every scenario passed. Repeats show the spread of
each measurement (fastest, median, slowest) and how many runs each criterion passed,
so a criterion that passes two runs out of three is visible as such instead of being
averaged into a percentage.
"""

from __future__ import annotations

import statistics
from pathlib import Path
from typing import Any

from pydantic import Field

from aegis.trust.contracts import Status, TestResult, _Model

SUITE_SCHEMA = "aegis.trust-suite/1"
SUITE_RESULT_SCHEMA = "aegis.trust-suite-result/1"


class Suite(_Model):
    schema_: str = Field(alias="schema")
    id: str = Field(pattern=r"^[A-Za-z0-9_.-]+$")
    title: str
    scenarios: list[str] = Field(min_length=1, description="Paths relative to the repository.")
    repeats: int = Field(default=3, ge=1, le=50)


def load_suite(path: Path) -> Suite:
    import json

    return Suite.model_validate(json.loads(path.read_text()))


def suite_status(results: list[TestResult]) -> tuple[Status, str]:
    """Worst run wins. BLOCKED (the environment did not fit) is reported apart from a
    test that ran and failed."""
    if not results:
        return Status.NOT_RUN, "nothing ran"
    for status in (Status.BLOCKED, Status.FAIL, Status.INCONCLUSIVE, Status.NOT_RUN):
        bad = [r for r in results if r.status is status]
        if bad:
            names = sorted({r.scenario_id for r in bad})
            return status, f"{len(bad)} of {len(results)} runs {status.value}: {', '.join(names)}"
    return Status.PASS, f"all {len(results)} runs passed"


def _spread(values: list[float]) -> dict[str, float | int]:
    return {
        "runs_measured": len(values),
        "min": round(min(values), 3),
        "median": round(statistics.median(values), 3),
        "max": round(max(values), 3),
    }


def summarise(results: list[TestResult]) -> dict[str, Any]:
    """Per scenario: run statuses, per-criterion pass counts, spread of the timings."""
    by_scenario: dict[str, list[TestResult]] = {}
    for result in results:
        by_scenario.setdefault(result.scenario_id, []).append(result)
    out: dict[str, Any] = {}
    for scenario_id, runs in by_scenario.items():
        criteria: dict[str, dict[str, Any]] = {}
        for run in runs:
            for c in run.criteria:
                entry = criteria.setdefault(
                    c.id,
                    {
                        "description": c.description,
                        "safety_critical": c.safety_critical,
                        "expected": c.expected,
                        "statuses": [],
                    },
                )
                entry["statuses"].append(c.status.value)
        for entry in criteria.values():
            entry["passed"] = f"{entry['statuses'].count('PASS')} of {len(entry['statuses'])}"
        timings: dict[str, dict[str, Any]] = {}
        for key in ("alert_raised_after_s", "alert_cleared_after_fault_end_s"):
            codes = {code for run in runs for code in run.measurements.get(key, {})}
            for code in sorted(codes):
                values = [
                    float(run.measurements[key][code])
                    for run in runs
                    if code in run.measurements.get(key, {})
                ]
                timings[f"{key}:{code}"] = _spread(values)
        fractions = [
            float(v)
            for run in runs
            if (v := run.measurements.get("incident_state_record_fraction")) is not None
        ]
        if fractions:
            timings["incident_state_record_fraction"] = _spread(fractions)
        status, why = suite_status(runs)
        out[scenario_id] = {
            "status": status.value,
            "why": why,
            "runs": [r.status.value for r in runs],
            "scenario_sha256": sorted({r.scenario_sha256 for r in runs}),
            "criteria": criteria,
            "measurements": timings,
        }
    return out
