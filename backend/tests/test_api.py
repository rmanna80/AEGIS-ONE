"""API tests against an in-memory vehicle (no PX4 needed)."""

from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from aegis.api import create_app
from aegis.config import OperatingMode, Settings
from aegis.contracts import BatteryState, Provenance, VehicleState
from aegis.hub import TelemetryHub
from aegis.missions import Mission, MissionPhase, MissionResult, MissionStatus, validate_mission
from aegis.recording import FlightRecorder, load_recording
from conftest import make_mission, make_state

TOKEN = "test-operator-token"
AUTH = {"X-Aegis-Operator-Token": TOKEN}


class FakeVehicle:
    """Implements both protocols; records the commands it receives."""

    vehicle_id = "test-1"
    provenance = Provenance.SIMULATED
    source = "test:fake"

    def __init__(self) -> None:
        self.overrides: dict[str, Any] = {}
        self.commands: list[str] = []
        self.status = MissionStatus()

    async def start(self) -> None: ...
    async def stop(self) -> None: ...

    async def get_state(self) -> VehicleState:
        return make_state(received_at_unix_s=time.time(), **self.overrides)

    async def submit_mission(self, mission: Mission) -> MissionResult:
        issues = validate_mission(mission)
        phase = MissionPhase.REJECTED if issues else MissionPhase.ACTIVE
        self.status = MissionStatus(
            mission_id=mission.mission_id,
            name=mission.name,
            phase=phase,
            total_waypoints=len(mission.waypoints),
            issues=issues,
        )
        if not issues:
            self.commands.append("submit_mission")
        return MissionResult(accepted=not issues, status=self.status)

    async def mission_status(self) -> MissionStatus:
        return self.status

    async def hold(self, reason: str | None = None) -> None:
        self.commands.append("hold" if reason is None else f"hold:{reason}")
        if reason is not None:
            self.status = self.status.model_copy(
                update={"phase": MissionPhase.BLOCKED, "message": reason}
            )

    async def return_to_home(self) -> None:
        self.commands.append("return_to_home")

    async def land(self) -> None:
        self.commands.append("land")


def make_client(
    tmp_path: Path, with_executor: bool = True
) -> tuple[TestClient, FakeVehicle, TelemetryHub]:
    vehicle = FakeVehicle()
    recorder = FlightRecorder(tmp_path / "flight.jsonl")
    hub = TelemetryHub(vehicle, vehicle if with_executor else None, recorder, rate_hz=50.0)
    settings = Settings(
        mode=OperatingMode.SIMULATION if with_executor else OperatingMode.MONITOR,
        operator_token=TOKEN,
        data_dir=tmp_path,
        frontend_dist=tmp_path / "no-ui",
    )
    return TestClient(create_app(settings, hub)), vehicle, hub


@pytest.fixture
def ctx(tmp_path: Path) -> Iterator[tuple[TestClient, FakeVehicle, TelemetryHub]]:
    client, vehicle, hub = make_client(tmp_path)
    with client:
        yield client, vehicle, hub


def test_health_and_system(ctx: tuple[TestClient, FakeVehicle, TelemetryHub]) -> None:
    client, _, _ = ctx
    assert client.get("/api/health").json()["status"] == "ok"
    system = client.get("/api/system").json()
    assert system["mode"] == "simulation"
    assert system["provenance"] == "simulated"
    assert system["commands_enabled"] is True


def test_websocket_streams_labelled_snapshots(
    ctx: tuple[TestClient, FakeVehicle, TelemetryHub],
) -> None:
    client, _, _ = ctx
    with client.websocket_connect("/ws/telemetry") as ws:
        messages = [ws.receive_json() for _ in range(3)]
    live = [m for m in messages if m["state"] is not None]
    assert live, "expected at least one snapshot with vehicle state"
    state = live[-1]["state"]
    assert state["provenance"] == "simulated"
    assert state["position_enu_m"] == {"x": 0.0, "y": 0.0, "z": 10.0}
    assert "position_enu_m" in state["derived_fields"]
    assert live[-1]["mission"]["phase"] == "idle"


def test_scenario_endpoint_serves_environment_and_demo_mission(
    ctx: tuple[TestClient, FakeVehicle, TelemetryHub],
) -> None:
    client, _, _ = ctx
    scenario = client.get("/api/scenario").json()
    assert scenario["scenario_id"] == "open_field"
    assert len(scenario["obstacles"]) >= 3
    Mission.model_validate(scenario["demo_mission"])


def test_commands_require_operator_token(ctx: tuple[TestClient, FakeVehicle, TelemetryHub]) -> None:
    client, vehicle, _ = ctx
    body = make_mission().model_dump(mode="json")
    assert client.post("/api/missions", json=body).status_code == 401
    wrong = {"X-Aegis-Operator-Token": "nope"}
    assert client.post("/api/missions", json=body, headers=wrong).status_code == 401
    for path in ("/api/vehicle/hold", "/api/vehicle/return", "/api/vehicle/land"):
        assert client.post(path).status_code == 401
    assert vehicle.commands == []


def test_operator_can_submit_and_cancel(ctx: tuple[TestClient, FakeVehicle, TelemetryHub]) -> None:
    client, vehicle, hub = ctx
    mission = make_mission()
    response = client.post("/api/missions", json=mission.model_dump(mode="json"), headers=AUTH)
    assert response.status_code == 200 and response.json()["accepted"] is True
    assert hub.geofence == mission.constraints.geofence
    assert client.post("/api/vehicle/hold", headers=AUTH).json()["command"] == "hold"
    assert client.post("/api/vehicle/return", headers=AUTH).status_code == 200
    assert client.post("/api/vehicle/land", headers=AUTH).status_code == 200
    assert vehicle.commands == ["submit_mission", "hold", "return_to_home", "land"]


def test_invalid_mission_is_rejected_with_reasons(
    ctx: tuple[TestClient, FakeVehicle, TelemetryHub],
) -> None:
    client, vehicle, hub = ctx
    mission = make_mission()
    mission.waypoints[0].east_m = 500.0  # far outside the fence
    body = mission.model_dump(mode="json")

    check = client.post("/api/missions/validate", json=body).json()
    assert check["valid"] is False and check["issues"]

    result = client.post("/api/missions", json=body, headers=AUTH).json()
    assert result["accepted"] is False
    assert result["status"]["phase"] == "rejected"
    assert vehicle.commands == [] and hub.geofence is None


def test_schema_violations_return_422(ctx: tuple[TestClient, FakeVehicle, TelemetryHub]) -> None:
    client, _, _ = ctx
    response = client.post("/api/missions", json={"mission_id": "x"}, headers=AUTH)
    assert response.status_code == 422


def test_monitor_mode_refuses_commands_even_with_token(tmp_path: Path) -> None:
    client, vehicle, _ = make_client(tmp_path, with_executor=False)
    with client:
        assert client.get("/api/system").json()["commands_enabled"] is False
        response = client.post("/api/vehicle/hold", headers=AUTH)
        assert response.status_code == 403
        assert vehicle.commands == []


async def test_hub_raises_and_clears_alert_on_injected_fault(tmp_path: Path) -> None:
    """Fault injection: battery degradation produces an alert, recovery clears it."""
    vehicle = FakeVehicle()
    recorder = FlightRecorder(tmp_path / "flight.jsonl")
    hub = TelemetryHub(vehicle, vehicle, recorder)

    await hub.tick()
    assert hub.alerts.active == []

    vehicle.overrides = {"battery": BatteryState(remaining_fraction=0.08)}
    await hub.tick()
    assert [a.code for a in hub.alerts.active] == ["battery.critical"]
    assert hub.snapshot()["alerts"][0]["severity"] == "critical"

    vehicle.overrides = {"connected": False}
    await hub.tick()
    assert [a.code for a in hub.alerts.active] == ["link.lost"]

    vehicle.overrides = {}
    await hub.tick()
    assert hub.alerts.active == []

    recorder.close()
    _, records = load_recording(tmp_path / "flight.jsonl")
    kinds = [r["kind"] for r in records]
    assert kinds.count("state") == 3  # nothing is recorded while the link is down
    assert kinds.count("alert") >= 3


def test_physical_mode_cannot_be_configured() -> None:
    with pytest.raises(ValueError, match="Physical flight is not available"):
        Settings(mode="physical")  # type: ignore[arg-type]


def test_active_mission_is_readable_by_every_display(
    ctx: tuple[TestClient, FakeVehicle, TelemetryHub],
) -> None:
    client, _, _ = ctx
    assert client.get("/api/missions/active").json() == {"mission": None}
    mission = make_mission().model_copy(update={"mission_id": "flown-by-script"})
    assert client.post("/api/missions", json=mission.model_dump(), headers=AUTH).json()["accepted"]
    active = client.get("/api/missions/active").json()["mission"]
    assert active["mission_id"] == "flown-by-script"
    assert len(active["waypoints"]) == len(mission.waypoints)
