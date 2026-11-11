import asyncio
import json
from pathlib import Path

import pytest

from aegis.contracts import Provenance, Vec3
from aegis.hub import TelemetryHub
from aegis.interfaces import MissionExecutor, VehicleTelemetrySource
from aegis.recording import FlightRecorder, RecordedTelemetryAdapter, load_recording
from conftest import make_state


def write_recording(path: Path, n: int = 5) -> None:
    recorder = FlightRecorder(path)
    recorder.open("rec-1", Provenance.SIMULATED, "px4_sitl:sih_quadx")
    for i in range(n):
        recorder.record_state(make_state(position_enu_m=Vec3(x=float(i), y=0.0, z=10.0)))
    recorder.record_event("mission", {"phase": "active", "mission_id": "m1", "total_waypoints": 2})
    recorder.close()


def test_recorder_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "flight.jsonl"
    write_recording(path)
    header, records = load_recording(path)
    assert header["provenance"] == "simulated"
    assert header["source"] == "px4_sitl:sih_quadx"
    assert [r["kind"] for r in records] == ["state"] * 5 + ["mission"]
    assert records[3]["data"]["position_enu_m"]["x"] == 3.0
    assert all(b["t"] >= a["t"] for a, b in zip(records, records[1:], strict=False))


def test_malformed_recording_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "bad.jsonl"
    path.write_text('{"kind": "header", "t": 0, "data": {"format_version": 1}}\nnot json\n')
    with pytest.raises(ValueError, match="malformed record"):
        load_recording(path)

    path.write_text(json.dumps({"kind": "state", "t": 0, "data": {}}) + "\n")
    with pytest.raises(ValueError, match="missing header"):
        load_recording(path)

    path.write_text(json.dumps({"kind": "header", "t": 0, "data": {"format_version": 99}}) + "\n")
    with pytest.raises(ValueError, match="unsupported format_version"):
        load_recording(path)


async def test_replay_relabels_provenance_and_advances(tmp_path: Path) -> None:
    path = tmp_path / "flight.jsonl"
    write_recording(path)
    adapter = RecordedTelemetryAdapter(path, speed=1000.0, loop=False)
    first = await adapter.get_state()
    assert first.provenance is Provenance.REPLAYED
    assert first.recorded_provenance is Provenance.SIMULATED
    assert first.source == f"recording:{tmp_path.name}"  # session recordings use the folder name
    assert first.position_enu_m is not None and first.position_enu_m.x == 0.0

    await adapter.start()
    for _ in range(200):
        if adapter.finished:
            break
        await asyncio.sleep(0.01)
    last = await adapter.get_state()
    await adapter.stop()
    assert adapter.finished
    assert last.position_enu_m is not None and last.position_enu_m.x == 4.0
    assert last.provenance is Provenance.REPLAYED


def test_replay_is_read_only(tmp_path: Path) -> None:
    path = tmp_path / "flight.jsonl"
    write_recording(path)
    adapter = RecordedTelemetryAdapter(path)
    assert isinstance(adapter, VehicleTelemetrySource)
    assert not isinstance(adapter, MissionExecutor)


def test_bundled_sample_recording_is_valid() -> None:
    """The sample shipped in data/sample must stay loadable (it backs replay mode)."""
    sample = Path(__file__).resolve().parents[2] / "data" / "sample" / "demo1_survey_sih.jsonl"
    header, records = load_recording(sample)
    states = [r for r in records if r["kind"] == "state"]
    assert header["provenance"] == "simulated"
    assert len(states) > 100
    peak = max(s["data"]["relative_altitude_m"] or 0.0 for s in states)
    assert peak > 10.0  # it really is a flight, not a parked vehicle


async def test_replay_reproduces_mission_status_in_sync(tmp_path: Path) -> None:
    path = tmp_path / "flight.jsonl"
    write_recording(path)  # the mission event is written after the last state
    adapter = RecordedTelemetryAdapter(path, speed=1000.0, loop=False)
    assert adapter.replayed_mission_status().phase.value == "idle"

    hub = TelemetryHub(adapter)
    await hub.start()
    for _ in range(200):
        if adapter.finished:
            break
        await asyncio.sleep(0.01)
    adapter.playback_time_s = adapter.duration_s + 1.0  # past the mission event
    await hub.tick()
    await hub.stop()
    assert hub.mission.phase.value == "active"
    assert hub.snapshot()["playback"]["duration_s"] == adapter.duration_s
