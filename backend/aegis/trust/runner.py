"""Runs one scenario against a live AEGIS backend through its public API.

The runner is a client: it uses the same endpoints an operator would, with the
operator token, and injects faults only through the backend's own fault injector
(simulator only, labelled). It never talks to a vehicle directly.

Steps: check the environment, record the configuration, fly the mission, take a
baseline, inject the fault, measure, evaluate, collect evidence, and return the
vehicle to home.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from aegis.trust.contracts import (
    Criterion,
    CriterionResult,
    Scenario,
    Status,
    TestResult,
    overall,
)

MONITORED_PREFIXES = ("nav.", "telemetry.", "battery.implausible", "control.", "security.", "link.")


class ApiClient(Protocol):
    def call(self, path: str, body: Any = None, post: bool = False, check: bool = True) -> Any: ...


def load_scenario(path: Path) -> tuple[Scenario, str]:
    text = path.read_text()
    return Scenario.model_validate(json.loads(text)), hashlib.sha256(text.encode()).hexdigest()


class Runner:
    def __init__(
        self,
        api: ApiClient,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        log: Callable[[str], None] = print,
    ) -> None:
        self.api, self.sleep, self.clock, self.log = api, sleep, clock, log

    # ---------------------------------------------------------------- environment

    def blocked_reason(self, scenario: Scenario) -> str | None:
        """Why the scenario cannot start here, or None when the environment fits."""
        system = self.api.call("/api/system")
        env = scenario.environment
        if system.get("provenance") != env.provenance:
            return (
                f"the vehicle's data is '{system.get('provenance')}', "
                f"the scenario needs '{env.provenance}'"
            )
        if not system.get("simulator_attested"):
            return "the vehicle has not proved it is a simulator"
        if not str(system.get("source", "")).startswith(env.simulator):
            return f"the vehicle is '{system.get('source')}', the scenario needs '{env.simulator}'"
        if system.get("scenario_id") != env.scenario_id:
            return (
                f"the backend runs scenario '{system.get('scenario_id')}', "
                f"this test needs '{env.scenario_id}'"
            )
        security = self.api.call("/api/security")
        if env.needs_fault_injection and not security["faults"]["enabled"]:
            return "fault injection is off (start the backend with AEGIS_FAULT_INJECTION=true)"
        if scenario.fault.kind not in security["faults"]["kinds"]:
            return f"this backend cannot inject '{scenario.fault.kind}'"
        if not self.api.call("/api/incidents")["enabled"]:
            return "the backend is not recording, so no evidence could be kept"
        phase = self.api.call("/api/state")["mission"]["phase"]
        if phase in ("uploading", "active", "returning"):
            return f"a mission is already in progress ({phase})"
        return None

    # -------------------------------------------------------------------- helpers

    def _alerts(self) -> set[str]:
        return {a["code"] for a in self.api.call("/api/state")["alerts"]}

    def _wait(
        self, condition: Callable[[], bool], timeout_s: float, step_s: float = 0.2
    ) -> float | None:
        started = self.clock()
        while self.clock() - started <= timeout_s:
            if condition():
                return self.clock() - started
            self.sleep(step_s)
        return None

    # ------------------------------------------------------------------------ run

    def run(self, scenario: Scenario, scenario_sha256: str) -> TestResult:
        started_utc = time.time()
        started = self.clock()
        session = self.api.call("/api/replay/session")
        system = self.api.call("/api/system")
        measurements: dict[str, Any] = {}
        evidence: dict[str, Any] = {"session_id": session.get("session_id")}

        def finish(status: Status, reason: str, criteria: list[CriterionResult]) -> TestResult:
            return TestResult(
                scenario_id=scenario.id,
                scenario_sha256=scenario_sha256,
                title=scenario.title,
                purpose=scenario.purpose,
                status=status,
                status_reason=reason,
                started_utc_s=round(started_utc, 3),
                duration_s=round(self.clock() - started, 1),
                environment={
                    **scenario.environment.model_dump(),
                    "vehicle_source": system.get("source"),
                    "operation_class": system.get("operation_class"),
                },
                simulated_or_physical=str(system.get("provenance")),
                software=session.get("software", {}),
                device=session.get("device", {}),
                expected_behaviour=scenario.expected_behaviour,
                criteria=criteria,
                measurements=measurements,
                evidence=evidence,
                limitations=scenario.limitations
                + [
                    "The fault is injected into AEGIS's copy of the telemetry; the vehicle "
                    "itself keeps receiving correct data.",
                    "One run under one set of conditions. It does not show behaviour under "
                    "other conditions, and it is not a safety certification.",
                ],
            )

        not_run = [
            CriterionResult(
                id=c.id,
                description=c.description,
                status=Status.NOT_RUN,
                expected=self._expected(c),
                observed="not measured",
                safety_critical=c.safety_critical,
            )
            for c in scenario.criteria
        ]
        reason = self.blocked_reason(scenario)
        if reason is not None:
            return finish(Status.BLOCKED, reason, not_run)

        # ---- fly
        flying = scenario.fault.inject_when == "flying_mission"
        if flying:
            mission = self.api.call("/api/scenario").get(scenario.mission)
            if mission is None:
                return finish(
                    Status.BLOCKED, f"the scenario file has no '{scenario.mission}'", not_run
                )
            accepted = self.api.call("/api/missions", mission)
            if not accepted["accepted"]:
                return finish(
                    Status.BLOCKED,
                    "the mission was not accepted: " + accepted["status"]["message"],
                    not_run,
                )

            def in_cruise() -> bool:
                state = self.api.call("/api/state")["state"]
                return bool(
                    state
                    and state["flight_mode"] == "MISSION"
                    and (state["relative_altitude_m"] or 0.0) > 8.0
                )

            if self._wait(in_cruise, 120.0, 0.5) is None:
                self._recover()
                return finish(Status.INCONCLUSIVE, "the vehicle never reached cruise", not_run)

        # ---- baseline
        baseline: set[str] = set()
        end = self.clock() + scenario.baseline_s
        while self.clock() < end:
            baseline |= {c for c in self._alerts() if c.startswith(MONITORED_PREFIXES)}
            self.sleep(0.5)
        measurements["alerts_before_fault"] = sorted(baseline)

        # ---- fault and measurements
        fault = scenario.fault
        incidents_before = {i["incident_id"] for i in self.api.call("/api/incidents")["incidents"]}
        self.api.call(
            "/api/faults", {"kind": fault.kind, "duration_s": fault.duration_s, **fault.parameters}
        )
        injected_at = self.clock()
        self.log(f"  fault '{fault.kind}' injected for {fault.duration_s:g} s")
        labelled = False
        raised: dict[str, float] = {}
        cleared: dict[str, float] = {}
        watched = {c.code for c in scenario.criteria if c.code}
        horizon = fault.duration_s + max(
            [c.max_s or 0.0 for c in scenario.criteria if c.measure == "alert_cleared_within_s"]
            + [5.0]
        )
        while self.clock() - injected_at <= horizon:
            snapshot = self.api.call("/api/state")
            elapsed = self.clock() - injected_at
            if snapshot["state"] and snapshot["state"].get("injected_faults"):
                labelled = True
            active = {a["code"] for a in snapshot["alerts"]}
            for code in watched:
                if code in active and code not in raised:
                    raised[code] = elapsed
                gone = code in raised and code not in active and code not in cleared
                if gone and elapsed >= fault.duration_s:
                    cleared[code] = elapsed - fault.duration_s
            self.sleep(0.2)
        measurements["alert_raised_after_s"] = {k: round(v, 2) for k, v in raised.items()}
        measurements["alert_cleared_after_fault_end_s"] = {
            k: round(v, 2) for k, v in cleared.items()
        }
        measurements["snapshot_labelled_during_fault"] = labelled
        phase_after = self.api.call("/api/state")["mission"]["phase"]
        measurements["mission_phase_after_fault"] = phase_after

        # ---- incident evidence
        incident: dict[str, Any] | None = None

        def packaged() -> bool:
            nonlocal incident
            for item in self.api.call("/api/incidents")["incidents"]:
                if item["incident_id"] not in incidents_before and item["status"] in (
                    "packaged",
                    "failed",
                ):
                    incident = item
                    return True
            return False

        self._wait(packaged, 60.0, 1.0)
        verification: dict[str, Any] | None = None
        if incident is not None and incident["status"] == "packaged":
            detail = self.api.call(f"/api/incidents/{incident['incident_id']}")
            verification = detail.get("verification")
            evidence.update(
                incident_id=incident["incident_id"],
                incident_folder=incident.get("folder"),
                incident_manifest_sha256=incident.get("manifest_sha256"),
                incident_completeness=detail["incident"]["completeness"],
            )
        completeness = evidence.get("incident_completeness") or {}
        measurements["incident_state_record_fraction"] = completeness.get("fraction_of_expected")
        measurements["incident_seconds_without_records"] = completeness.get(
            "seconds_without_state_records"
        )
        measurements["incident_seconds_without_link"] = completeness.get("seconds_without_link")
        measurements["incident_packaged"] = (
            incident is not None and incident["status"] == "packaged"
        )
        measurements["incident_verifies"] = bool(verification and verification["valid"])

        # ---- evaluate, then put the vehicle back
        criteria = [self._evaluate(c, measurements) for c in scenario.criteria]
        if flying:
            measurements["landed_after_test"] = self._recover()
        status, why = overall(criteria)
        return finish(status, why, criteria)

    def _recover(self) -> bool:
        """Return the vehicle to home and wait for it to land and disarm."""
        self.api.call("/api/vehicle/return", post=True, check=False)

        def landed() -> bool:
            state = self.api.call("/api/state")["state"]
            return bool(state and state["armed"] is False)

        return self._wait(landed, 240.0, 1.0) is not None

    # ----------------------------------------------------------------- evaluation

    @staticmethod
    def _expected(c: Criterion) -> str:
        if c.measure == "alert_raised_within_s":
            return f"alert {c.code} within {c.max_s:g} s of the fault"
        if c.measure == "alert_cleared_within_s":
            return f"alert {c.code} clears within {c.max_s:g} s of the fault ending"
        if c.measure == "mission_phase_in":
            return f"mission phase is one of {c.phases}"
        if c.measure == "no_alert_before_fault":
            return (
                f"no alert starting with {c.prefixes or list(MONITORED_PREFIXES)} before the fault"
            )
        return {
            "incident_packaged": "an incident package is produced",
            "incident_verifies": "the incident package verifies against its manifest",
            "snapshot_labelled": "altered data is labelled as injected",
        }.get(c.measure, c.measure)

    def _evaluate(self, c: Criterion, m: dict[str, Any]) -> CriterionResult:
        def result(status: Status, observed: str) -> CriterionResult:
            return CriterionResult(
                id=c.id,
                description=c.description,
                status=status,
                expected=self._expected(c),
                observed=observed,
                safety_critical=c.safety_critical,
            )

        if c.measure == "alert_raised_within_s":
            seen = m["alert_raised_after_s"].get(c.code)
            if seen is None:
                return result(Status.FAIL, f"{c.code} was never raised")
            ok = seen <= (c.max_s or 0.0)
            return result(Status.PASS if ok else Status.FAIL, f"raised after {seen:.2f} s")
        if c.measure == "alert_cleared_within_s":
            if c.code not in m["alert_raised_after_s"]:
                return result(
                    Status.INCONCLUSIVE, f"{c.code} was never raised, so clearing is undefined"
                )
            seen = m["alert_cleared_after_fault_end_s"].get(c.code)
            if seen is None:
                return result(Status.FAIL, f"{c.code} was still active when measurement ended")
            ok = seen <= (c.max_s or 0.0)
            return result(
                Status.PASS if ok else Status.FAIL, f"cleared {seen:.2f} s after the fault ended"
            )
        if c.measure == "mission_phase_in":
            phase = m["mission_phase_after_fault"]
            return result(
                Status.PASS if phase in (c.phases or []) else Status.FAIL, f"phase '{phase}'"
            )
        if c.measure == "no_alert_before_fault":
            prefixes = tuple(c.prefixes or MONITORED_PREFIXES)
            early = [a for a in m["alerts_before_fault"] if a.startswith(prefixes)]
            return result(
                Status.FAIL if early else Status.PASS, f"alerts before the fault: {early or 'none'}"
            )
        if c.measure == "incident_packaged":
            ok = m["incident_packaged"]
            return result(
                Status.PASS if ok else Status.FAIL, "packaged" if ok else "no package was produced"
            )
        if c.measure == "incident_verifies":
            if not m["incident_packaged"]:
                return result(Status.INCONCLUSIVE, "there is no package to verify")
            ok = m["incident_verifies"]
            return result(
                Status.PASS if ok else Status.FAIL,
                "manifest verified" if ok else "verification failed",
            )
        if c.measure == "incident_completeness_at_least":
            fraction = m.get("incident_state_record_fraction")
            if fraction is None:
                return result(Status.INCONCLUSIVE, "there is no package to measure")
            missing = m.get("incident_seconds_without_records")
            ok = fraction >= (c.min_fraction or 0.0)
            return result(
                Status.PASS if ok else Status.FAIL,
                f"{fraction:.0%} of the expected state records; {missing} s with none",
            )
        if c.measure == "snapshot_labelled":
            ok = m["snapshot_labelled_during_fault"]
            return result(
                Status.PASS if ok else Status.INCONCLUSIVE,
                "labelled"
                if ok
                else "no labelled snapshot was observed (a link fault hides the state)",
            )
        return result(Status.INCONCLUSIVE, f"unknown measure '{c.measure}'")
