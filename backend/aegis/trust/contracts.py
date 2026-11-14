"""Versioned contracts for test scenarios and their results."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

SCENARIO_SCHEMA = "aegis.trust-scenario/1"
RESULT_SCHEMA = "aegis.trust-result/1"


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Status(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    INCONCLUSIVE = "INCONCLUSIVE"  # ran, but a measurement needed for the verdict is missing
    NOT_RUN = "NOT RUN"
    BLOCKED = "BLOCKED"  # could not start: the environment does not meet the scenario's needs
    SKIPPED = "SKIPPED"


class Environment(_Model):
    simulator: str = Field(description="What the vehicle must be, e.g. 'px4_sitl'.")
    scenario_id: str = Field(description="AEGIS scenario the backend must be running.")
    provenance: str = "simulated"
    needs_fault_injection: bool = True
    devices: list[str] = Field(default_factory=lambda: ["backend host"])


class Fault(_Model):
    kind: str
    duration_s: float = Field(gt=0.0, le=120.0)
    parameters: dict[str, float] = Field(default_factory=dict)
    inject_when: str = Field(
        default="flying_mission",
        description="'flying_mission': in Mission mode above 8 m. 'on_ground': before any flight.",
    )


class Criterion(_Model):
    id: str
    description: str
    measure: str = Field(
        description="alert_raised_within_s | alert_cleared_within_s | mission_phase_in | "
        "incident_packaged | incident_verifies | snapshot_labelled | no_alert_before_fault | "
        "incident_completeness_at_least"
    )
    min_fraction: float | None = None
    code: str | None = None
    max_s: float | None = None
    phases: list[str] | None = None
    prefixes: list[str] | None = None
    safety_critical: bool = False


class Scenario(_Model):
    schema_: str = Field(alias="schema")
    id: str = Field(pattern=r"^[A-Za-z0-9_.-]+$")
    title: str
    purpose: str = Field(description="Why this is tested: the requirement or risk behind it.")
    environment: Environment
    initial_conditions: str
    mission: str = Field(
        default="demo_mission", description="Key of a mission in the scenario file."
    )
    baseline_s: float = Field(default=8.0, ge=0.0, le=120.0)
    fault: Fault
    expected_behaviour: str
    criteria: list[Criterion] = Field(min_length=1)
    random_seed: int | None = Field(
        default=None, description="None: the scenario has no randomised element of its own."
    )
    max_duration_s: float = Field(default=300.0, gt=0.0, le=1800.0)
    limitations: list[str] = Field(default_factory=list)


class CriterionResult(_Model):
    id: str
    description: str
    status: Status
    expected: str
    observed: str
    safety_critical: bool = False


class TestResult(_Model):
    __test__ = False  # not a pytest class

    schema_: str = Field(default=RESULT_SCHEMA, alias="schema")
    scenario_id: str
    scenario_sha256: str
    title: str
    purpose: str
    status: Status
    status_reason: str
    started_utc_s: float
    duration_s: float
    environment: dict[str, Any]
    simulated_or_physical: str
    software: dict[str, Any]
    device: dict[str, Any]
    expected_behaviour: str
    criteria: list[CriterionResult]
    measurements: dict[str, Any]
    evidence: dict[str, Any]
    limitations: list[str]


def overall(criteria: list[CriterionResult]) -> tuple[Status, str]:
    """A scenario passes only if every criterion passes. One failure fails it, however
    many others pass; a missing measurement makes it inconclusive, never a pass."""
    failed = [c.id for c in criteria if c.status is Status.FAIL]
    if failed:
        return Status.FAIL, "failed: " + ", ".join(failed)
    unclear = [c.id for c in criteria if c.status is not Status.PASS]
    if unclear:
        return Status.INCONCLUSIVE, "no verdict for: " + ", ".join(unclear)
    return Status.PASS, "every criterion passed"
