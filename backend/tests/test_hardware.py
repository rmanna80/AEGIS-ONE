"""Phase 8: provenance by evidence, operation classes, adapter conformance and inventory."""

from __future__ import annotations

import importlib
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from aegis.adapters.px4_sitl import NotAttestedError, PX4SITLAdapter
from aegis.api import create_app
from aegis.config import OperatingMode, Settings
from aegis.contracts import Provenance
from aegis.hardware import ADAPTERS, OPERATION_CLASSES, Evidence, operation_class
from aegis.hub import TelemetryHub
from aegis.interfaces import MissionExecutor, VehicleTelemetrySource
from aegis.recording import RecordedTelemetryAdapter
from conftest import make_mission
from test_api import AUTH, TOKEN, FakeVehicle
from test_px4_attestation import GAZEBO, REAL_GENERIC_QUAD, SIH, adapter_for

REPO_ROOT = Path(__file__).resolve().parents[2]


async def test_provenance_follows_the_evidence_never_an_assumption() -> None:
    fresh = PX4SITLAdapter()
    assert fresh.provenance is Provenance.UNKNOWN  # nothing has answered yet

    simulator, autopilot = adapter_for(SIH)
    await simulator._attest_simulator()
    assert simulator.provenance is Provenance.SIMULATED

    hardware, _ = adapter_for(REAL_GENERIC_QUAD)
    await hardware._attest_simulator()
    assert hardware.provenance is Provenance.PHYSICAL
    assert hardware.source == "px4:not-a-simulator"

    unknown_airframe, _ = adapter_for({"SYS_AUTOSTART": 4500})
    await unknown_airframe._attest_simulator()
    assert unknown_airframe.provenance is Provenance.PHYSICAL  # unproven is not "simulated"

    silent, quiet = adapter_for(GAZEBO)
    quiet.fail_next = 1
    await silent._attest_simulator()
    assert silent.provenance is Provenance.UNKNOWN  # no answer yet: still not simulated

    # A simulator swapped for hardware on the same link is relabelled at the next check.
    autopilot.params = dict(REAL_GENERIC_QUAD)
    simulator._attested_airframe = None
    await simulator._attest_simulator()
    assert simulator.provenance is Provenance.PHYSICAL


async def test_physical_autopilot_cannot_be_commanded_in_any_mode() -> None:
    hardware, _ = adapter_for(REAL_GENERIC_QUAD)
    await hardware._attest_simulator()
    sent: list[str] = []

    def recorder(name: str):  # type: ignore[no-untyped-def]
        async def call() -> None:
            sent.append(name)

        return call

    hardware._system.action = SimpleNamespace(
        hold=recorder("hold"), return_to_launch=recorder("return"), land=recorder("land")
    )
    hardware._last_rx = __import__("time").monotonic()  # the link itself is alive
    for command in (hardware.hold, hardware.return_to_home, hardware.land):
        with pytest.raises(NotAttestedError):
            await command()
    with pytest.raises(NotAttestedError):
        await hardware.simulate_external_hold()
    result = await hardware.submit_mission(make_mission())
    assert not result.accepted and "commands are disabled" in result.status.message
    assert sent == []  # nothing reached the autopilot


def test_operation_classes() -> None:
    assert operation_class("simulation", Provenance.SIMULATED) == "simulation"
    assert operation_class("simulation", Provenance.UNKNOWN) == "simulation"
    assert operation_class("replay", Provenance.REPLAYED) == "recorded-data playback"
    assert operation_class("monitor", Provenance.PHYSICAL) == "physical monitoring (read-only)"
    # Even in simulation mode, hardware on the link is reported as what it is.
    assert operation_class("simulation", Provenance.PHYSICAL) == "physical monitoring (read-only)"
    unavailable = {c["class"] for c in OPERATION_CLASSES if not c["available"]}
    assert unavailable == {"hardware-in-the-loop testing", "physical aircraft operation"}
    with pytest.raises(ValueError, match="Physical flight is not available"):
        Settings(mode="physical")  # type: ignore[arg-type]
    assert "physical" not in {m.value for m in OperatingMode}


def test_every_listed_adapter_exists_and_nothing_claims_hardware_it_lacks() -> None:
    for entry in ADAPTERS:
        path = entry["implementation"]
        if path is None:
            assert entry["evidence"] == Evidence.NOT_IMPLEMENTED.value
            continue
        module_name, class_name = path.rsplit(".", 1)
        assert hasattr(importlib.import_module(module_name), class_name), path
    hardware_tested = [a["name"] for a in ADAPTERS if a["evidence"] == Evidence.HARDWARE_TESTED]
    assert hardware_tested == ["Device monitor"]  # the only thing run on physical equipment


def test_adapters_satisfy_their_interfaces() -> None:
    px4 = PX4SITLAdapter()
    assert isinstance(px4, VehicleTelemetrySource) and isinstance(px4, MissionExecutor)
    replay = RecordedTelemetryAdapter(REPO_ROOT / "data/sample/demo1_survey_sih.jsonl")
    assert isinstance(replay, VehicleTelemetrySource)
    assert not isinstance(replay, MissionExecutor)  # a recording cannot be commanded
    assert replay.provenance is Provenance.REPLAYED


async def test_replay_reports_each_states_own_recorded_provenance() -> None:
    replay = RecordedTelemetryAdapter(REPO_ROOT / "data/sample/demo1_survey_sih.jsonl")
    state = await replay.get_state()
    assert state.provenance is Provenance.REPLAYED
    assert state.recorded_provenance is Provenance.SIMULATED


def test_api_reports_operation_class_and_inventory(tmp_path: Path) -> None:
    vehicle = FakeVehicle()
    settings = Settings(
        operator_token=TOKEN, record_flights=False, data_dir=tmp_path, frontend_dist=tmp_path / "x"
    )
    with TestClient(create_app(settings, TelemetryHub(vehicle, vehicle))) as client:
        assert client.get("/api/system").json()["operation_class"] == "simulation"
        inventory = client.get("/api/hardware").json()
        kinds = {a["kind"] for a in inventory["adapters"]}
        assert {"RGB camera", "depth sensor", "thermal sensor"} <= kinds
        assert len(inventory["operation_classes"]) == 5

    monitor = Settings(mode=OperatingMode.MONITOR, data_dir=tmp_path, frontend_dist=tmp_path / "x")
    with TestClient(create_app(monitor, TelemetryHub(vehicle, None))) as client:
        # Read-only: there is no executor, so every command endpoint refuses.
        assert client.post("/api/vehicle/hold", headers=AUTH).status_code == 403
        assert client.get("/api/system").json()["commands_enabled"] is False
