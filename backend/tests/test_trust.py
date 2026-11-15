"""AEGIS TRUST: scenario contracts, verdict rules, and the runner against a scripted
backend. The scripted backend lets the verdict logic be tested without a simulator;
the live runs against PX4 SITL are reported separately (docs/trust/README.md)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from aegis.faults import KINDS
from aegis.trust.contracts import CriterionResult, Scenario, Status, overall
from aegis.trust.runner import Runner, load_scenario

REPO_ROOT = Path(__file__).resolve().parents[2]
SCENARIOS = sorted((REPO_ROOT / "trust/scenarios").glob("*.json"))


def criterion(cid: str, status: Status) -> CriterionResult:
    return CriterionResult(id=cid, description="", status=status, expected="", observed="")


# ---------------------------------------------------------------------- contracts


@pytest.mark.parametrize("path", SCENARIOS, ids=lambda p: p.stem)
def test_shipped_scenarios_are_valid_and_use_faults_that_exist(path: Path) -> None:
    scenario, digest = load_scenario(path)
    assert len(digest) == 64
    assert scenario.fault.kind in KINDS
    assert scenario.purpose and scenario.expected_behaviour and scenario.limitations
    assert any(c.safety_critical for c in scenario.criteria)
    assert scenario.environment.provenance == "simulated"  # none may target hardware


def test_unknown_fields_are_rejected() -> None:
    scenario, _ = load_scenario(SCENARIOS[0])
    data = scenario.model_dump(by_alias=True)
    data["criterai"] = []
    with pytest.raises(ValidationError):
        Scenario.model_validate(data)


def test_one_failure_fails_the_scenario_and_a_missing_verdict_never_passes() -> None:
    passes = [criterion(f"C{i}", Status.PASS) for i in range(9)]
    assert overall(passes)[0] is Status.PASS
    assert overall([*passes, criterion("C9", Status.FAIL)])[0] is Status.FAIL
    assert overall([*passes, criterion("C9", Status.INCONCLUSIVE)])[0] is Status.INCONCLUSIVE
    assert overall([*passes, criterion("C9", Status.NOT_RUN)])[0] is Status.INCONCLUSIVE
    status, why = overall(
        [criterion("A", Status.INCONCLUSIVE), criterion("B", Status.FAIL), *passes]
    )
    assert status is Status.FAIL and "B" in why


# ------------------------------------------------------------------ scripted backend


class ScriptedBackend:
    """Answers the runner's API calls from a clock the runner itself advances."""

    def __init__(self, raise_alert_after_s: float | None = 0.4, package: bool = True) -> None:
        self.now = 0.0
        self.fault_at: float | None = None
        self.fault_s = 0.0
        self.raise_after = raise_alert_after_s
        self.package = package
        self.system: dict[str, Any] = {
            "provenance": "simulated",
            "simulator_attested": True,
            "source": "px4_sitl:sih_quadx",
            "scenario_id": "open_field",
            "operation_class": "simulation",
        }
        self.faults_enabled = True
        self.phase = "idle"
        self.armed = False
        self.posted: list[str] = []
        self.extra_alerts: list[str] = []
        self.restarts = 0

    def sleep(self, seconds: float) -> None:
        self.now += seconds

    def clock(self) -> float:
        return self.now

    def _fault_active(self) -> bool:
        return self.fault_at is not None and self.now - self.fault_at < self.fault_s

    def call(self, path: str, body: Any = None, post: bool = False, check: bool = True) -> Any:
        if body is not None or post:
            self.posted.append(path)
        if path == "/api/system":
            return self.system
        if path == "/api/health":
            return {"bridge_restarts": self.restarts}
        if path == "/api/replay/session":
            return {"session_id": "S1", "software": {"git_commit": "abc"}, "device": {}}
        if path == "/api/security":
            return {"faults": {"enabled": self.faults_enabled, "kinds": list(KINDS)}}
        if path == "/api/scenario":
            return {"demo_mission": {"name": "demo"}}
        if path == "/api/missions":
            self.phase, self.armed = "active", True
            return {"accepted": True, "status": {"message": ""}}
        if path == "/api/faults":
            self.fault_at, self.fault_s = self.now, float(body["duration_s"])
            return {}
        if path == "/api/vehicle/return":
            self.phase, self.armed = "idle", False
            return {}
        if path == "/api/incidents":
            done = self.fault_at is not None and not self._fault_active() and self.package
            items = (
                [
                    {
                        "incident_id": "INC-1",
                        "status": "packaged",
                        "folder": "/x",
                        "manifest_sha256": "f" * 64,
                    }
                ]
                if done
                else []
            )
            return {"enabled": True, "incidents": items}
        if path == "/api/incidents/INC-1":
            return {
                "incident": {
                    "completeness": {
                        "fraction_of_expected": 0.99,
                        "seconds_without_state_records": 0.0,
                        "seconds_without_link": 12.0,
                    }
                },
                "verification": {"valid": True},
            }
        if path == "/api/state":
            alerts = [{"code": c} for c in self.extra_alerts]
            raised = (
                self.raise_after is not None
                and self._fault_active()
                and self.fault_at is not None
                and self.now - self.fault_at >= self.raise_after
            )
            if raised:
                alerts.append({"code": "link.lost"})
            return {
                "state": {
                    "flight_mode": "MISSION",
                    "relative_altitude_m": 10.0,
                    "armed": self.armed,
                    "injected_faults": ["link_drop"] if self._fault_active() else [],
                },
                "mission": {"phase": self.phase},
                "alerts": alerts,
            }
        raise AssertionError(f"unexpected call {path}")


def run(backend: ScriptedBackend) -> Any:
    scenario, digest = load_scenario(REPO_ROOT / "trust/scenarios/link_loss_detection.json")
    runner = Runner(backend, sleep=backend.sleep, clock=backend.clock, log=lambda _: None)
    return runner.run(scenario, digest)


def by_id(result: Any) -> dict[str, Status]:
    return {c.id: c.status for c in result.criteria}


def test_a_backend_that_behaves_passes_and_the_result_says_what_it_was_run_on() -> None:
    backend = ScriptedBackend()
    result = run(backend)
    assert result.status is Status.PASS, result.status_reason
    assert result.simulated_or_physical == "simulated"
    assert result.software == {"git_commit": "abc"}
    assert result.evidence["incident_id"] == "INC-1"
    assert 0.3 <= result.measurements["alert_raised_after_s"]["link.lost"] <= 0.7
    assert "/api/vehicle/return" in backend.posted  # the vehicle is brought home afterwards
    assert any("not a safety certification" in text for text in result.limitations)


def test_a_missed_detection_fails_and_clearing_is_then_inconclusive_not_passed() -> None:
    result = run(ScriptedBackend(raise_alert_after_s=None))
    statuses = by_id(result)
    assert result.status is Status.FAIL
    assert statuses["C2"] is Status.FAIL  # never raised
    assert statuses["C3"] is Status.INCONCLUSIVE  # cannot clear what was never raised


def test_a_late_detection_fails() -> None:
    result = run(ScriptedBackend(raise_alert_after_s=9.0))
    assert by_id(result)["C2"] is Status.FAIL and result.status is Status.FAIL


def test_missing_evidence_fails_the_evidence_criteria() -> None:
    statuses = by_id(run(ScriptedBackend(package=False)))
    assert statuses["C5"] is Status.FAIL
    # With no package there is nothing to verify or measure: no verdict, not a pass.
    assert statuses["C6"] is Status.INCONCLUSIVE and statuses["C7"] is Status.INCONCLUSIVE
    assert statuses["C2"] is Status.PASS


def test_an_alert_before_the_fault_fails_the_baseline() -> None:
    backend = ScriptedBackend()
    backend.extra_alerts = ["battery.implausible"]
    assert by_id(run(backend))["C1"] is Status.FAIL


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        ({"provenance": "physical"}, "physical"),
        ({"provenance": "unknown"}, "unknown"),
        ({"simulator_attested": False}, "not proved"),
        ({"scenario_id": "urban"}, "urban"),
    ],
)
def test_the_runner_refuses_an_environment_that_does_not_fit(
    change: dict[str, Any], expected: str
) -> None:
    backend = ScriptedBackend()
    backend.system.update(change)
    result = run(backend)
    assert result.status is Status.BLOCKED and expected in result.status_reason
    assert all(c.status is Status.NOT_RUN for c in result.criteria)
    assert backend.posted == []  # nothing was commanded and no fault was injected


def test_blocked_when_fault_injection_is_off() -> None:
    backend = ScriptedBackend()
    backend.faults_enabled = False
    result = run(backend)
    assert result.status is Status.BLOCKED and backend.posted == []


# ------------------------------------------------------------- new measures, suites


class CrashingBackend(ScriptedBackend):
    """A bridge crash: data stops (stale), the bridge is restarted, the mission is
    marked as no longer tracked."""

    def __init__(self, stray_alert: str | None = None, restarts: bool = True) -> None:
        super().__init__(raise_alert_after_s=0.4)
        self.stray_alert, self.does_restart = stray_alert, restarts

    def call(self, path: str, body: Any = None, post: bool = False, check: bool = True) -> Any:
        if path == "/api/faults":
            assert body["kind"] == "bridge_crash"
            self.restarts += 1 if self.does_restart else 0
            self.phase = "failed"
        answer = super().call(path, body, post, check)
        if path == "/api/state":
            codes = [a["code"] for a in answer["alerts"]]
            answer["alerts"] = [
                {"code": "telemetry.stale" if c == "link.lost" else c} for c in codes
            ]
            if self.stray_alert and self._fault_active():
                answer["alerts"].append({"code": self.stray_alert})
            answer["state"]["connected"] = True
        return answer


def run_crash(backend: ScriptedBackend) -> Any:
    scenario, digest = load_scenario(REPO_ROOT / "trust/scenarios/bridge_crash_recovery.json")
    runner = Runner(backend, sleep=backend.sleep, clock=backend.clock, log=lambda _: None)
    return runner.run(scenario, digest)


def test_any_of_several_alerts_meets_the_criterion_and_restarts_are_counted() -> None:
    result = run_crash(CrashingBackend())
    statuses = by_id(result)
    assert statuses["C2"] is Status.PASS  # telemetry.stale counts for "data stopped"
    assert result.measurements["alert_raised_after_s"]["link.lost or telemetry.stale"] < 1.0
    assert statuses["C3"] is Status.PASS and statuses["C5"] is Status.PASS
    assert statuses["C6"] is Status.PASS  # mission reported as no longer tracked
    assert statuses["C7"] is Status.PASS  # Return worked afterwards
    assert result.status is Status.PASS, result.status_reason


def test_a_bridge_that_is_not_restarted_fails() -> None:
    assert by_id(run_crash(CrashingBackend(restarts=False)))["C3"] is Status.FAIL


def test_a_misdiagnosis_alongside_the_right_alert_is_a_failure() -> None:
    result = run_crash(CrashingBackend(stray_alert="telemetry.frozen"))
    assert by_id(result)["C11"] is Status.FAIL
    assert "telemetry.frozen" in next(c.observed for c in result.criteria if c.id == "C11")


def test_suite_status_is_the_worst_run_and_counts_are_not_averaged() -> None:
    from aegis.trust.suite import suite_status, summarise

    good = run(ScriptedBackend())
    late = run(ScriptedBackend(raise_alert_after_s=9.0))
    status, why = suite_status([good, good, late])
    assert status is Status.FAIL and "1 of 3" in why
    summary = summarise([good, good, late])["link-loss-detection"]
    assert summary["criteria"]["C2"]["passed"] == "2 of 3"
    assert summary["runs"] == ["PASS", "PASS", "FAIL"]
    # The third run never raised the alert at all, so it has no time to add to the spread:
    # the spread says it covers two runs, and the pass count above shows the failure.
    spread = summary["measurements"]["alert_raised_after_s:link.lost"]
    assert spread["runs_measured"] == 2 and spread["max"] < 1.0
    assert suite_status([])[0] is Status.NOT_RUN


def test_the_shipped_suite_lists_scenarios_that_exist() -> None:
    from aegis.trust.suite import load_suite

    suite = load_suite(REPO_ROOT / "trust/suites/core.json")
    for name in suite.scenarios:
        load_scenario(REPO_ROOT / name)
