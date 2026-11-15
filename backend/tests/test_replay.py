"""AEGIS REPLAY: evidence manifests, incident capture and packaging, timeline, playback."""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

import pytest

from aegis.contracts import BatteryState, Provenance, Vec3
from aegis.hub import TelemetryHub
from aegis.recording import FlightRecorder, RecordedTelemetryAdapter, load_recording
from aegis.replay.evidence import MANIFEST_NAME, verify_manifest, write_manifest
from aegis.replay.incidents import IncidentManager, import_package, slice_flight_log
from aegis.replay.timeline import build_timeline
from aegis.security import AuditLog
from conftest import make_state
from test_api import FakeVehicle

# --------------------------------------------------------------------- manifests


def package(folder: Path) -> dict[str, object]:
    folder.mkdir()
    (folder / "events.jsonl").write_text('{"kind":"state"}\n')
    (folder / "incident.json").write_text("{}")
    return write_manifest(folder, "test")


def test_manifest_verifies_an_untouched_package(tmp_path: Path) -> None:
    manifest = package(tmp_path / "p")
    report = verify_manifest(tmp_path / "p", str(manifest["manifest_sha256"]))
    assert report["valid"] and report["files"] == 2 and report["problems"] == []


def test_a_changed_removed_or_added_file_is_reported(tmp_path: Path) -> None:
    folder = tmp_path / "p"
    package(folder)
    (folder / "events.jsonl").write_text('{"kind":"state","edited":true}\n')
    changed = verify_manifest(folder)
    assert not changed["valid"] and "events.jsonl" in " ".join(changed["problems"])

    package(tmp_path / "q")
    (tmp_path / "q" / "incident.json").unlink()
    assert not verify_manifest(tmp_path / "q")["valid"]

    package(tmp_path / "r")
    (tmp_path / "r" / "extra.txt").write_text("added later")
    added = verify_manifest(tmp_path / "r")
    assert not added["valid"] and "extra.txt" in " ".join(added["problems"])


def test_a_rewritten_manifest_is_caught_only_with_the_recorded_hash(tmp_path: Path) -> None:
    """Editing a file and re-hashing it into the manifest makes the package
    self-consistent. Only the hash kept outside the package (the audit log) exposes it."""
    folder = tmp_path / "p"
    manifest = package(folder)
    (folder / "events.jsonl").write_text("forged\n")
    write_manifest(folder, "test")
    assert verify_manifest(folder)["valid"]  # the limit of a self-contained hash
    assert not verify_manifest(folder, str(manifest["manifest_sha256"]))["valid"]


def test_a_folder_without_a_manifest_does_not_verify(tmp_path: Path) -> None:
    assert not verify_manifest(tmp_path)["valid"]


# ------------------------------------------------------------- incident capture


async def fly(hub: TelemetryHub, vehicle: FakeVehicle, ticks: int, **overrides: object) -> None:
    vehicle.overrides = dict(overrides)
    for _ in range(ticks):
        await hub.tick()
        await asyncio.sleep(0.01)


async def session(tmp_path: Path) -> tuple[TelemetryHub, FakeVehicle, IncidentManager, AuditLog]:
    vehicle = FakeVehicle()
    recorder = FlightRecorder(tmp_path / "flight.jsonl")
    hub = TelemetryHub(vehicle, vehicle, recorder, rate_hz=100.0)
    audit = AuditLog(tmp_path / "audit.jsonl")
    manager = IncidentManager(
        hub, tmp_path, audit, {"session_id": "s", "software": {}}, pre_s=5.0, post_s=0.05
    )
    return hub, vehicle, manager, audit


async def wait_packaged(manager: IncidentManager) -> dict[str, object]:
    for _ in range(200):
        await asyncio.sleep(0.02)
        if manager.incidents and manager.incidents[-1]["status"] in ("packaged", "failed"):
            break
    return manager.incidents[-1]


async def test_a_critical_alert_opens_an_incident_and_clearing_packages_it(
    tmp_path: Path,
) -> None:
    hub, vehicle, manager, audit = await session(tmp_path)
    await fly(hub, vehicle, 20)
    await manager.step()
    assert manager.incidents == []  # nothing critical, nothing opened

    await fly(hub, vehicle, 15, connected=False)
    await manager.step()
    assert manager.incidents[0]["status"] == "open"
    assert manager.incidents[0]["alert_codes"] == ["link.lost"]

    await fly(hub, vehicle, 10)
    await manager.step()  # alerts clear: the post-event window starts
    await fly(hub, vehicle, 10)
    await manager.step()
    incident = await wait_packaged(manager)
    assert incident["status"] == "packaged", incident

    folder = Path(str(incident["folder"]))
    assert set(os.listdir(folder)) == {
        "events.jsonl",
        "incident.json",
        "audit_excerpt.jsonl",
        "session.json",
        MANIFEST_NAME,
    }
    document = manager.document(str(incident["incident_id"]))
    assert document is not None and document["verification"]["valid"]
    body = document["incident"]
    assert body["root_cause"].startswith("Not determined")
    assert body["completeness"]["state_records_without_link"] == 15
    assert [a["code"] for a in body["alerts"]] == ["link.lost", "link.lost"]
    # The manifest hash is anchored outside the package.
    events = [json.loads(line) for line in (tmp_path / "audit.jsonl").read_text().splitlines()]
    anchored = [e for e in events if e["event"] == "incident_packaged"]
    assert anchored[0]["manifest_sha256"] == incident["manifest_sha256"]

    # The slice replays on its own, as a replay of simulated data.
    adapter = RecordedTelemetryAdapter(folder / "events.jsonl", loop=False)
    state = await adapter.get_state()
    assert state.provenance is Provenance.REPLAYED
    assert state.recorded_provenance is Provenance.SIMULATED

    # Tampering after packaging is reported.
    (folder / "events.jsonl").write_text("edited\n")
    tampered = manager.document(str(incident["incident_id"]))
    assert tampered is not None and not tampered["verification"]["valid"]


async def test_operator_mark_and_export_import(tmp_path: Path) -> None:
    hub, vehicle, manager, _ = await session(tmp_path)
    assert manager.mark("too early") is None  # nothing is being recorded yet
    await fly(hub, vehicle, 10)
    marked = manager.mark("Odd yaw", "seen on the map")
    assert marked is not None and marked["trigger"] == "operator mark"
    await fly(hub, vehicle, 5)
    await manager.stop()  # an open incident is packaged, not lost
    incident = manager.incidents[-1]
    assert incident["status"] == "packaged" and "stopped" in str(incident["closed_because"])

    archive = tmp_path / "export.zip"
    data = manager.export_zip(str(incident["incident_id"]))
    assert data is not None
    archive.write_bytes(data)
    imported = import_package(archive, tmp_path / "imported")
    assert imported["verification"]["valid"]


def test_import_refuses_archives_that_escape_the_folder(tmp_path: Path) -> None:
    import zipfile

    archive = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("../outside.txt", "x")
    with pytest.raises(ValueError, match="unsafe"):
        import_package(archive, tmp_path / "out")


# --------------------------------------------------------------- slice / timeline


def write_flight(path: Path) -> None:
    """20 states 0.1 s apart; link down for records 8-11; an alert and a fault event."""
    lines = [
        json.dumps(
            {
                "kind": "header",
                "t": 0.0,
                "data": {
                    "format_version": 1,
                    "vehicle_id": "v",
                    "provenance": "simulated",
                    "source": "px4",
                },
            }
        )
    ]
    for i in range(20):
        state = make_state(
            connected=not 8 <= i < 12,
            position_enu_m=Vec3(x=float(i), y=0.0, z=10.0),
            battery=BatteryState(voltage_v=15.0, remaining_fraction=0.8),
        ).model_dump(mode="json")
        lines.append(
            json.dumps({"kind": "state", "t": i * 0.1, "utc": 1000.0 + i * 0.1, "data": state})
        )
        if i == 2:
            lines.append(
                json.dumps(
                    {
                        "kind": "mission",
                        "t": 0.21,
                        "utc": 1000.21,
                        "data": {
                            "phase": "active",
                            "mission_id": "m1",
                            "current_waypoint": 1,
                            "total_waypoints": 5,
                        },
                    }
                )
            )
        if i == 8:
            lines.append(
                json.dumps(
                    {
                        "kind": "fault",
                        "t": 0.8,
                        "utc": 1000.8,
                        "data": {"kind": "link_drop", "duration_s": 0.4},
                    }
                )
            )
            lines.append(
                json.dumps(
                    {
                        "kind": "alert",
                        "t": 0.81,
                        "utc": 1000.81,
                        "data": {
                            "code": "link.lost",
                            "severity": "critical",
                            "active": True,
                            "message": "Vehicle link lost.",
                        },
                    }
                )
            )
    path.write_text("\n".join(lines) + "\n")


def test_slice_is_rebased_and_summarised(tmp_path: Path) -> None:
    write_flight(tmp_path / "flight.jsonl")
    lines, summary = slice_flight_log(tmp_path / "flight.jsonl", 0.5, 1.5, "INC-1")
    records = [json.loads(line) for line in lines]
    assert records[0]["kind"] == "header" and records[0]["data"]["incident_id"] == "INC-1"
    # The mission status in force when the window opens is carried in, and says so.
    assert records[1]["kind"] == "mission" and records[1]["carried_from_before_the_window"]
    assert records[1]["t"] == 0.0 and records[1]["t_session"] == 0.21
    body = records[2:]
    assert body[0]["t"] == 0.0 and body[0]["t_session"] == 0.5
    assert all(0.0 <= r["t"] <= 1.0 for r in body)
    assert summary["state_records"] == 11
    assert summary["state_records_without_link"] == 4
    assert summary["injected_faults_seen"] == ["link_drop"]
    assert summary["utc_start_s"] == 1000.5  # both clocks are kept, side by side
    (tmp_path / "slice.jsonl").write_text("\n".join(lines) + "\n")
    load_recording(tmp_path / "slice.jsonl")  # a valid recording in its own right


def test_timeline_marks_faults_alerts_and_link_changes(tmp_path: Path) -> None:
    write_flight(tmp_path / "flight.jsonl")
    _, records = load_recording(tmp_path / "flight.jsonl")
    timeline = build_timeline(records)
    labels = [e["label"] for e in timeline]
    assert labels == [
        "mission active, 1 of 5 waypoints reached",
        "vehicle link lost",
        "fault injected: link_drop for 0.4 s",
        "link.lost raised",
        "vehicle link restored",
    ]
    assert [e["t"] for e in timeline] == sorted(e["t"] for e in timeline)


def test_the_shipped_sample_incident_verifies_and_shows_the_link_loss() -> None:
    """data/sample/incident_link_loss was captured from a live PX4 SITL flight by the
    link-loss TRUST scenario. It is the package the README's replay example uses."""
    folder = Path(__file__).resolve().parents[2] / "data/sample/incident_link_loss"
    report = verify_manifest(folder)
    assert report["valid"], report["problems"]
    _, records = load_recording(folder / "events.jsonl")
    labels = [e["label"] for e in build_timeline(records)]
    lost, restored = labels.index("vehicle link lost"), labels.index("vehicle link restored")
    assert lost < labels.index("link.lost raised") < restored < labels.index("link.lost cleared")
    document = json.loads((folder / "incident.json").read_text())
    assert document["completeness"]["fraction_of_expected"] >= 0.95
    assert "operator_token" not in (folder / "session.json").read_text()


# ------------------------------------------------------------------- playback


async def test_replay_reports_each_state_as_it_was_labelled_when_recorded(tmp_path: Path) -> None:
    """The header only holds what was known when the file was opened (often before the
    simulator had identified itself). Found on the first live replay of an incident."""
    write_flight(tmp_path / "flight.jsonl")
    text = (
        (tmp_path / "flight.jsonl")
        .read_text()
        .replace(
            '"provenance": "simulated", "source": "px4"', '"provenance": "unknown", "source": "px4"'
        )
    )
    (tmp_path / "flight.jsonl").write_text(text)
    adapter = RecordedTelemetryAdapter(tmp_path / "flight.jsonl", loop=False)
    assert adapter.header["provenance"] == "unknown"
    state = await adapter.get_state()
    assert state.provenance is Provenance.REPLAYED
    assert state.recorded_provenance is Provenance.SIMULATED


async def test_pause_seek_and_speed(tmp_path: Path) -> None:
    write_flight(tmp_path / "flight.jsonl")
    adapter = RecordedTelemetryAdapter(tmp_path / "flight.jsonl", loop=False)
    await adapter.start()
    try:
        adapter.paused = True
        adapter.seek(1.0)
        await asyncio.sleep(0.15)
        state = await adapter.get_state()
        assert adapter.playback_time_s == 1.0  # paused: the clock does not move
        assert state.position_enu_m is not None and state.position_enu_m.x == 10.0
        assert state.connected is False  # the link loss is part of what is replayed

        adapter.seek(99.0)
        assert adapter.playback_time_s == adapter.duration_s  # clamped to the recording
        adapter.seek(0.2)
        adapter.set_speed(4.0)
        adapter.paused = False
        await asyncio.sleep(0.3)
        assert 0.8 <= adapter.playback_time_s <= 1.9  # about 4x real time
        with pytest.raises(ValueError):
            adapter.set_speed(0.0)
    finally:
        await adapter.stop()


# ------------------------------------------------------------ camera evidence


def camera_payload(at: float, frame_id: int, provenance: str = "simulated") -> dict[str, object]:
    return {
        "frame_id": frame_id,
        "captured_at_unix_s": at,
        "provenance": provenance,
        "source": "stream:test",
        "detector": "test",
        "width_px": 160,
        "height_px": 120,
        "detections": [],
        "tracks": [],
    }


def camera_image(frame_id: int) -> object:
    import numpy as np

    image = np.zeros((120, 160, 3), np.uint8)
    image[40:80, frame_id % 100 : frame_id % 100 + 40] = 255
    return image


def test_camera_buffer_keeps_the_recent_past_then_everything_while_open() -> None:
    pytest.importorskip("cv2")
    from aegis.replay.camera_evidence import CameraEvidence

    evidence = CameraEvidence(pre_s=1.0, max_frames=25)
    for i in range(30):  # 3 s at 10 frames per second
        evidence.observe(camera_payload(100.0 + i * 0.1, i), camera_image(i))
    evidence.begin()  # only the last second comes along
    for i in range(30, 50):
        evidence.observe(camera_payload(100.0 + i * 0.1, i), camera_image(i))
    frames = evidence.end()
    ids = [payload["frame_id"] for _, payload, _ in frames]
    assert ids[0] >= 19 and ids == sorted(ids)
    assert len(frames) == 25 and evidence.frames_over_limit > 0  # bounded, and says so
    assert all(jpeg for _, _, jpeg in frames)
    assert evidence.end() == []


def test_a_physical_camera_buffer_keeps_no_images() -> None:
    pytest.importorskip("cv2")
    from aegis.replay.camera_evidence import CameraEvidence

    evidence = CameraEvidence(keep_images=False)
    evidence.begin()
    evidence.observe(camera_payload(1.0, 1, "physical"), camera_image(1))
    [(_, _, jpeg)] = evidence.end()
    assert jpeg is None


async def test_an_incident_package_carries_the_camera_inside_its_window(tmp_path: Path) -> None:
    pytest.importorskip("cv2")
    import cv2

    from aegis.perception.playback import RecordedPerceptionPlayer
    from aegis.replay.camera_evidence import CameraEvidence

    hub, vehicle, manager, _ = await session(tmp_path)
    manager.camera = CameraEvidence(pre_s=5.0)
    manager.camera.mount = "vehicle"
    early = time.time() - 60.0  # long before the incident: must not be packaged
    manager.camera.observe(camera_payload(early, 0), camera_image(0))
    frame = 1

    async def fly_with_camera(ticks: int, **overrides: object) -> None:
        nonlocal frame
        vehicle.overrides = dict(overrides)
        for _ in range(ticks):
            await hub.tick()
            assert manager.camera is not None
            manager.camera.observe(camera_payload(time.time(), frame), camera_image(frame))
            frame += 1
            await asyncio.sleep(0.01)

    await fly_with_camera(10)
    await fly_with_camera(15, connected=False)
    await manager.step()
    await fly_with_camera(10)
    await manager.step()
    await fly_with_camera(10)
    await manager.step()
    incident = await wait_packaged(manager)
    assert incident["status"] == "packaged", incident

    folder = Path(str(incident["folder"]))
    document = json.loads((folder / "incident.json").read_text())
    camera = document["camera"]
    rows = [json.loads(x) for x in (folder / "detections.jsonl").read_text().splitlines()]
    assert camera["frames"] == len(rows) >= 20 and camera["video"] == "video.mp4"
    assert all(row["frame_id"] != 0 for row in rows)  # the frame from a minute ago is out
    assert camera["mount"] == "vehicle"
    capture = cv2.VideoCapture(str(folder / "video.mp4"))
    assert int(capture.get(cv2.CAP_PROP_FRAME_COUNT)) == len(rows)  # one frame per row
    capture.release()
    assert "video.mp4" not in document["not_in_this_package"]["kept_in_the_session_folder"]
    assert verify_manifest(folder)["valid"]

    # The camera replays with the incident, on the incident's own clock.
    adapter = RecordedTelemetryAdapter(folder / "events.jsonl", loop=False)
    player = RecordedPerceptionPlayer(folder, adapter, adapter.wall_clock_at_t0())
    player._open()  # what start() does, without its background loop
    queue = player.subscribe()
    adapter.seek(adapter.duration_s)
    assert player.step()
    record, _ = queue.get_nowait()
    assert record["provenance"] == "replayed" and record["recorded_provenance"] == "simulated"


async def test_a_physical_camera_incident_has_boxes_and_says_why_there_is_no_video(
    tmp_path: Path,
) -> None:
    pytest.importorskip("cv2")
    from aegis.replay.camera_evidence import CameraEvidence

    hub, vehicle, manager, _ = await session(tmp_path)
    manager.camera = CameraEvidence(pre_s=5.0, keep_images=False)
    manager.camera.mount = "bench"
    await fly(hub, vehicle, 5)
    marked = manager.mark("bench test")
    assert marked is not None
    for i in range(10):
        manager.camera.observe(camera_payload(time.time(), i, "physical"), camera_image(i))
        await fly(hub, vehicle, 1)
    await manager.stop()
    folder = Path(str(manager.incidents[-1]["folder"]))
    camera = json.loads((folder / "incident.json").read_text())["camera"]
    assert camera["frames"] == 10 and camera["video"] is None
    assert "physical camera" in camera["why_no_video"]
    assert camera["provenance"] == "physical" and camera["mount"] == "bench"
    assert not (folder / "video.mp4").exists()


async def test_packages_are_signed_by_the_device_and_the_audit_log_says_by_whom(
    tmp_path: Path,
) -> None:
    from aegis.identity import DeviceKey

    hub, vehicle, manager, _ = await session(tmp_path)
    manager.signer = DeviceKey.load_or_create(tmp_path / "device.pem")
    await fly(hub, vehicle, 5)
    manager.mark("signed")
    await fly(hub, vehicle, 3)
    await manager.stop()
    incident = manager.incidents[-1]
    assert incident["signed_by"] == manager.signer.fingerprint
    document = manager.document(str(incident["incident_id"]))
    assert document is not None
    assert document["verification"]["valid"]
    assert document["signature"]["valid"] and document["signature"]["key_trusted"]
    records = [json.loads(x) for x in (tmp_path / "audit.jsonl").read_text().splitlines()]
    [packaged] = [r for r in records if r["event"] == "incident_packaged"]
    assert packaged["signed_by"] == manager.signer.fingerprint
