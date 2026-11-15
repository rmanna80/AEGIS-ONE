"""Compare two TRUST suite results (an earlier and a later run of the same suite).

Two kinds of change are reported:

* A verdict changed: a criterion's pass count went down (REGRESSION) or up (FIXED).
  Any criterion that passed every earlier run and fails any later run is a regression.
* A timing moved: only when the later median lies outside the whole range of the
  earlier runs, and both sides have at least two runs. Otherwise the report says no
  difference is shown. With a handful of runs that is the honest reading: a smaller
  median inside the earlier range is not an improvement.
"""

from __future__ import annotations

from typing import Any


def _count(text: str) -> tuple[int, int]:
    passed, _, total = text.partition(" of ")
    return int(passed), int(total)


def compare_suites(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    changes: list[dict[str, Any]] = []
    timings: list[dict[str, Any]] = []
    for scenario_id in sorted(set(before["scenarios"]) | set(after["scenarios"])):
        old = before["scenarios"].get(scenario_id)
        new = after["scenarios"].get(scenario_id)
        if old is None or new is None:
            changes.append(
                {
                    "scenario": scenario_id,
                    "kind": "ADDED" if old is None else "REMOVED",
                    "detail": "only in the later suite" if old is None else "only in the earlier",
                }
            )
            continue
        if old.get("scenario_sha256") != new.get("scenario_sha256"):
            changes.append(
                {
                    "scenario": scenario_id,
                    "kind": "SCENARIO CHANGED",
                    "detail": "the scenario file differs, so its results are not like for like",
                }
            )
        for cid in sorted(set(old["criteria"]) | set(new["criteria"])):
            a, b = old["criteria"].get(cid), new["criteria"].get(cid)
            if a is None or b is None:
                continue
            (pa, ta), (pb, tb) = _count(a["passed"]), _count(b["passed"])
            rate_a, rate_b = pa / ta if ta else 0.0, pb / tb if tb else 0.0
            if rate_b < rate_a or (pa == ta and pb < tb):
                kind = "REGRESSION"
            elif rate_b > rate_a:
                kind = "FIXED" if pb == tb else "BETTER"
            else:
                continue
            changes.append(
                {
                    "scenario": scenario_id,
                    "criterion": cid,
                    "kind": kind,
                    "safety_critical": b.get("safety_critical", False),
                    "detail": f"{a['passed']} -> {b['passed']} ({b['description']})",
                }
            )
        for key in sorted(set(old["measurements"]) & set(new["measurements"])):
            a, b = old["measurements"][key], new["measurements"][key]
            entry = {
                "scenario": scenario_id,
                "measurement": key,
                "before": a,
                "after": b,
            }
            if a["runs_measured"] < 2 or b["runs_measured"] < 2:
                entry["reading"] = "too few runs to compare"
            elif b["median"] > a["max"]:
                entry["reading"] = "later median above the whole earlier range"
            elif b["median"] < a["min"]:
                entry["reading"] = "later median below the whole earlier range"
            else:
                entry["reading"] = "no difference shown by these runs"
            timings.append(entry)
    regressions = [c for c in changes if c["kind"] == "REGRESSION"]
    return {
        "before": {k: before.get(k) for k in ("suite_id", "started_utc_s", "software")},
        "after": {k: after.get(k) for k in ("suite_id", "started_utc_s", "software")},
        "regressions": len(regressions),
        "safety_critical_regressions": sum(1 for c in regressions if c.get("safety_critical")),
        "changes": changes,
        "timings": timings,
    }
