"""Anomaly detection, fault injection, audit log and security monitoring."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from aegis.anomaly import DETECTORS, AnomalyMonitor
from aegis.api import create_app
from aegis.config import OperatingMode, Settings
from aegis.contracts import BatteryState, Severity, Vec3, VehicleState
from aegis.faults import FaultInjector, FaultRequest
from aegis.hub import TelemetryHub
from aegis.missions import MissionPhase, Waypoint
from aegis.security import AuditLog, SecurityMonitor, is_loopback, posture, verify_audit_log
from conftest import make_mission, make_state
from test_api import AUTH, TOKEN, FakeVehicle

REPO_ROOT = Path(__file__).resolve().parents[2]


class Flight:
    """Feeds a monitor a physically consistent flight at 10 Hz, with optional tampering."""

    def __init__(self) -> None:
        self.vehicle = FakeVehicle()
        self.monitor = AnomalyMonitor(TelemetryHub(self.vehicle, self.vehicle))
        self.t = 100.0
        self.east = 0.0
        self.phase = MissionPhase.ACTIVE
        self.mission = make_mission().model_copy(
            update={"waypoints": [Waypoint(east_m=40.0, north_m=0.0, altitude_rel_m=10.0)]}
        )

    def step(self, seconds: float = 0.1, speed: float = 4.0, **overrides: Any) -> VehicleState:
        self.t += seconds
        self.east += speed * seconds
        values: dict[str, Any] = {
            "vehicle_time_s": self.t,
            "position_enu_m": Vec3(x=self.east, y=0.0, z=10.0),
            "velocity_ned_m_s": Vec3(x=0.0, y=speed, z=0.0),  # NED: y is east
            "battery": BatteryState(voltage_v=16.0, remaining_fraction=0.9),
        }
        values.update(overrides)
        state = make_state(**values)
        self.monitor.observe(state, self.phase, self.mission, self.t)
        return state

    def fly(self, seconds: float, **overrides: Any) -> None:
        for _ in range(round(seconds * 10)):
            self.step(**overrides)

    def raised(self) -> set[str]:
        return {code for code, count in self.monitor.detections.items() if count}


# ------------------------------------------------------------------------ detectors


def test_a_consistent_flight_raises_nothing() -> None:
    flight = Flight()
    flight.fly(9.0)  # 36 m along a 40 m leg
    assert flight.raised() == set()


def test_recorded_real_flight_raises_no_navigation_or_telemetry_anomaly() -> None:
    """The sample PX4 SITL recording must not trip the physics checks (false alarms)."""
    vehicle = FakeVehicle()
    monitor = AnomalyMonitor(TelemetryHub(vehicle, vehicle))
    phase = MissionPhase.IDLE
    states = 0
    for line in (REPO_ROOT / "data/sample/demo1_survey_sih.jsonl").read_text().splitlines():
        record = json.loads(line)
        if record["kind"] == "mission":
            phase = MissionPhase(record["data"]["phase"])
        elif record["kind"] == "state":
            states += 1
            state = VehicleState.model_validate(record["data"])
            monitor.observe(state, phase, None, 1000.0 + record["t"])
    assert states > 500
    raised = {code for code, count in monitor.detections.items() if count}
    # The recording carries no command times, so arming looks unexplained here.
    assert raised <= {"control.unexpected_arm"}


def test_position_jump_is_caught_within_a_second() -> None:
    flight = Flight()
    flight.fly(5.0)
    flight.east += 25.0  # the position leaps; velocity says nothing happened
    flight.fly(1.2)
    assert "nav.position_jump" in flight.raised()
    [(code, severity, message)] = [f for f in flight.monitor._latched.items()][0:1] and [
        (c, s, m) for c, (_, s, m) in flight.monitor._latched.items() if c == "nav.position_jump"
    ]
    assert severity is Severity.CRITICAL and "spoofing" in message


def test_slow_drift_is_caught_as_drift_not_as_a_jump() -> None:
    flight = Flight()
    flight.fly(5.0)
    for _ in range(120):  # position slides east 1.5 m/s faster than the velocity reports
        flight.east += 0.15
        flight.step()
    assert "nav.position_drift" in flight.raised()
    assert "nav.position_jump" not in flight.raised()


def test_clock_reversal_and_frozen_telemetry() -> None:
    flight = Flight()
    flight.fly(3.0)
    flight.t -= 30.0
    flight.step()
    assert "telemetry.clock_reversed" in flight.raised()

    frozen = Flight()
    frozen.fly(3.0)
    state = frozen.step()
    for i in range(30):  # the same snapshot keeps arriving for 3 s
        frozen.monitor.observe(state.model_copy(), frozen.phase, None, frozen.t + 0.1 * (i + 1))
    assert "telemetry.frozen" in frozen.raised()


def test_battery_that_rises_or_plunges_is_flagged() -> None:
    rising = Flight()
    rising.fly(2.0, battery=BatteryState(voltage_v=15.0, remaining_fraction=0.5))
    rising.fly(1.0, battery=BatteryState(voltage_v=15.0, remaining_fraction=0.7))
    assert "battery.implausible" in rising.raised()
    plunging = Flight()
    plunging.fly(2.0)
    plunging.fly(1.0, battery=BatteryState(voltage_v=14.0, remaining_fraction=0.3))
    assert "battery.implausible" in plunging.raised()


def test_off_route_only_counts_while_flying_the_mission() -> None:
    flight = Flight()
    flight.fly(2.0)
    for _ in range(20):
        flight.step(position_enu_m=Vec3(x=flight.east, y=30.0, z=10.0))
    assert "mission.off_route" in flight.raised()
    idle = Flight()
    idle.phase = MissionPhase.IDLE
    for _ in range(20):
        idle.step(position_enu_m=Vec3(x=idle.east, y=30.0, z=10.0))
    assert "mission.off_route" not in idle.raised()


def test_mode_change_is_unexpected_only_without_an_operator_command() -> None:
    outside = Flight()
    outside.fly(2.0)
    outside.fly(0.5, flight_mode="HOLD")
    assert "control.unexpected_mode" in outside.raised()

    ours = Flight()
    ours.fly(2.0)
    ours.vehicle.last_command_at = ours.t  # type: ignore[attr-defined]  # operator pressed Hold
    ours.fly(0.5, flight_mode="HOLD")
    assert "control.unexpected_mode" not in ours.raised()

    normal_end = Flight()
    normal_end.fly(2.0)
    normal_end.fly(0.5, flight_mode="RETURN_TO_LAUNCH")  # mission complete, or a PX4 failsafe
    assert "control.unexpected_mode" not in normal_end.raised()


def test_alerts_latch_then_clear_and_the_monitor_cannot_command() -> None:
    flight = Flight()
    flight.fly(5.0)
    flight.east += 25.0
    flight.fly(1.2)
    monitor = flight.monitor
    monitor._latched["nav.position_jump"] = (
        __import__("time").monotonic(),
        Severity.CRITICAL,
        "x",
    )
    assert [c for c, _, _ in monitor.findings()] == ["nav.position_jump"]
    monitor._latched["nav.position_jump"] = (
        __import__("time").monotonic() - 60.0,
        Severity.CRITICAL,
        "x",
    )
    assert monitor.findings() == []
    assert flight.vehicle.commands == []
    assert not hasattr(monitor, "executor")
    assert set(monitor.status()["detectors"]) == set(DETECTORS)  # type: ignore[arg-type]


# ------------------------------------------------------------------ fault injection


def test_injected_faults_alter_the_copy_label_it_and_expire() -> None:
    injector = FaultInjector()
    state = make_state(position_enu_m=Vec3(x=1.0, y=2.0, z=10.0), vehicle_time_s=50.0)
    assert injector.apply(state) is state  # nothing active: untouched
    injector.inject(FaultRequest(kind="gps_jump", east_m=20.0, north_m=-5.0, duration_s=5.0))
    injector.inject(FaultRequest(kind="clock_rewind", seconds=30.0))
    injector.inject(FaultRequest(kind="battery_drop", to_fraction=0.1))
    altered = injector.apply(state)
    assert altered.position_enu_m == Vec3(x=21.0, y=-3.0, z=10.0)
    assert altered.vehicle_time_s == 20.0
    assert altered.battery is not None and altered.battery.remaining_fraction == 0.1
    assert altered.injected_faults == ("battery_drop", "clock_rewind", "gps_jump")
    assert state.position_enu_m == Vec3(x=1.0, y=2.0, z=10.0)  # the original is not modified
    assert {f["kind"] for f in injector.active()} == {"gps_jump", "clock_rewind", "battery_drop"}
    injector.clear()
    assert injector.apply(state) is state and injector.active() == []
    with pytest.raises(ValueError):
        injector.inject(FaultRequest(kind="steal_the_drone"))


def test_freeze_repeats_one_snapshot() -> None:
    injector = FaultInjector()
    injector.inject(FaultRequest(kind="freeze"))
    first = injector.apply(make_state(vehicle_time_s=10.0, received_at_unix_s=1.0))
    later = injector.apply(make_state(vehicle_time_s=11.0, received_at_unix_s=2.0))
    assert later.vehicle_time_s == first.vehicle_time_s == 10.0
    assert later.received_at_unix_s == 2.0 and later.injected_faults == ("freeze",)


def client_with(tmp_path: Path, **settings: Any) -> tuple[TestClient, FakeVehicle, TelemetryHub]:
    vehicle = FakeVehicle()
    hub = TelemetryHub(vehicle, vehicle, rate_hz=50.0)
    config = Settings(
        operator_token=TOKEN, record_flights=False, frontend_dist=tmp_path / "x", **settings
    )
    return TestClient(create_app(config, hub)), vehicle, hub


def test_fault_injection_is_off_by_default_and_needs_an_attested_simulator(tmp_path: Path) -> None:
    client, vehicle, _ = client_with(tmp_path)
    with client:
        body = {"kind": "gps_jump"}
        assert client.post("/api/faults", json=body, headers=AUTH).status_code == 409
        assert client.get("/api/security").json()["faults"]["enabled"] is False
    client, vehicle, _ = client_with(tmp_path, fault_injection=True)
    with client:
        refused = client.post("/api/faults", json=body, headers=AUTH)
        assert refused.status_code == 409 and "attested" in refused.json()["detail"]
        vehicle.attested = True  # type: ignore[attr-defined]
        assert client.post("/api/faults", json=body).status_code == 401
        assert client.post("/api/faults", json=body, headers=AUTH).json()["injected"] == "gps_jump"
        assert (
            client.post("/api/faults", json={"kind": "nonsense"}, headers=AUTH).status_code == 422
        )
        assert client.get("/api/security").json()["faults"]["active"][0]["kind"] == "gps_jump"
        assert client.delete("/api/faults", headers=AUTH).json() == {"active": []}
    monitor = Settings(
        mode=OperatingMode.MONITOR, fault_injection=True, frontend_dist=tmp_path / "x"
    )
    with TestClient(create_app(monitor, TelemetryHub(FakeVehicle(), None))) as client:
        assert client.get("/api/security").json()["faults"]["enabled"] is False


# ------------------------------------------------------------------ audit, security


def test_audit_log_chain_detects_edits_deletions_and_reordering(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    log = AuditLog(path)
    for i in range(5):
        log.append("command_request", client="127.0.0.1", path=f"/api/x/{i}")
    assert verify_audit_log(path) == {"valid": True, "records": 5, "first_bad_line": None}
    lines = path.read_text().splitlines()

    edited = tmp_path / "edited.jsonl"
    record = json.loads(lines[2])
    record["path"] = "/api/vehicle/land"
    edited.write_text("\n".join([*lines[:2], json.dumps(record), *lines[3:]]) + "\n")
    assert verify_audit_log(edited) == {"valid": False, "records": 2, "first_bad_line": 3}

    deleted = tmp_path / "deleted.jsonl"
    deleted.write_text("\n".join([*lines[:1], *lines[2:]]) + "\n")
    assert verify_audit_log(deleted)["first_bad_line"] == 2

    swapped = tmp_path / "swapped.jsonl"
    swapped.write_text("\n".join([lines[1], lines[0], *lines[2:]]) + "\n")
    assert verify_audit_log(swapped)["valid"] is False


def test_commands_and_failures_are_audited_without_the_token(tmp_path: Path) -> None:
    vehicle = FakeVehicle()
    hub = TelemetryHub(vehicle, vehicle, rate_hz=50.0)
    settings = Settings(operator_token=TOKEN, data_dir=tmp_path, frontend_dist=tmp_path / "x")
    with TestClient(create_app(settings, hub)) as client:
        for _ in range(5):
            client.post("/api/vehicle/hold", headers={"X-Aegis-Operator-Token": "guess"})
        assert client.post("/api/vehicle/hold", headers=AUTH).status_code == 200
        status = client.get("/api/security").json()
        assert status["auth_failures_recent"] == 5 and status["command_requests_total"] == 1
        events = [r["event"] for r in status["audit"]["recent"]]
        assert events == ["auth_failed"] * 5 + ["command_request", "command_outcome"]
        codes = {a["code"] for a in client.get("/api/alerts").json()}
        # The alert appears on the next telemetry tick.
        for _ in range(20):
            if "security.auth_failures" in codes:
                break
            __import__("time").sleep(0.05)
            codes = {a["code"] for a in client.get("/api/alerts").json()}
        assert "security.auth_failures" in codes
        recorded = json.dumps(list(client.app.state.audit.recent))  # type: ignore[attr-defined]
    assert TOKEN not in recorded and "guess" not in recorded
    assert client.app.state.audit.count == 7  # type: ignore[attr-defined]


def test_remote_command_clients_and_posture() -> None:
    assert is_loopback("127.0.0.1") and is_loopback("::1") and not is_loopback("192.168.1.20")
    monitor = SecurityMonitor(AuditLog())
    monitor.authorised("127.0.0.1", "/api/vehicle/hold")
    assert monitor.findings() == []
    monitor.authorised("192.168.1.20", "/api/vehicle/hold")
    assert [c for c, _, _ in monitor.findings()] == ["security.remote_command"]

    def levels(**kwargs: Any) -> dict[str, str]:
        base: dict[str, Any] = {
            "operator_token": "x" * 40,
            "token_generated": False,
            "px4_url": "udpin://127.0.0.1:14540",
            "cors_origins": ["http://localhost:5173"],
            "fault_injection": False,
            "mode": "simulation",
        }
        base.update(kwargs)
        return {c["id"]: c["level"] for c in posture(**base)}

    good = levels()
    assert good["operator_token"] == good["mavlink_listener"] == good["cors"] == "ok"
    assert good["mavsdk_grpc"] == "warning"  # a known exposure this release cannot close
    assert levels(operator_token="short")["operator_token"] == "warning"
    assert levels(px4_url="udpin://0.0.0.0:14540")["mavlink_listener"] == "warning"
    assert levels(cors_origins=["*"])["cors"] == "warning"
    assert levels(fault_injection=True)["fault_injection"] == "info"
