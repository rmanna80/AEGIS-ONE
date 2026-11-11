"""Session recording and synchronized camera/detection playback."""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("cv2")
pytest.importorskip("scipy")

import cv2  # noqa: E402
import numpy as np  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from aegis.api import create_app  # noqa: E402
from aegis.config import OperatingMode, Settings  # noqa: E402
from aegis.contracts import Provenance  # noqa: E402
from aegis.perception.pipeline import PerceptionPipeline  # noqa: E402
from aegis.perception.playback import RecordedPerceptionPlayer  # noqa: E402
from aegis.perception.types import CameraFrame  # noqa: E402
from test_cameras_pipeline import BrightBlobDetector, ListCamera, scene  # noqa: E402
from test_recording import write_recording  # noqa: E402

T0 = 1_700_000_000.0
FRAME_PERIOD_S = 0.1


class Clock:
    playback_time_s = 0.0


async def record_session(folder: Path, frames: int = 30) -> None:
    """Frame i shows the rectangle at x = 20 + 4 i and was captured at T0 + 0.1 i."""
    pipeline = PerceptionPipeline(ListCamera([]), BrightBlobDetector(), record_dir=folder)
    await pipeline.start()
    for i in range(frames):
        captured = T0 + i * FRAME_PERIOD_S
        frame = CameraFrame(scene(i), i, "cam", Provenance.SIMULATED, "stream:gazebo", captured, i)
        await pipeline.process(frame)
    await pipeline.stop()


def blob_left_edge(jpeg: bytes) -> int:
    image = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_GRAYSCALE)
    columns = np.flatnonzero(image[125] > 128)
    return int(columns[0])


async def test_session_holds_one_video_frame_per_detection_line(tmp_path: Path) -> None:
    await record_session(tmp_path, frames=30)
    lines = (tmp_path / "detections.jsonl").read_text().splitlines()
    capture = cv2.VideoCapture(str(tmp_path / "video.mp4"))
    assert len(lines) == 30
    assert int(capture.get(cv2.CAP_PROP_FRAME_COUNT)) == 30
    capture.release()


async def test_playback_follows_the_replay_clock_and_keeps_video_and_boxes_aligned(
    tmp_path: Path,
) -> None:
    await record_session(tmp_path, frames=30)
    clock = Clock()
    player = RecordedPerceptionPlayer(tmp_path, clock, flight_started_at_unix_s=T0)
    player._open()
    queue = player.subscribe()

    clock.playback_time_s = 0.05
    assert player.step() is True
    record, jpeg = queue.get_nowait()
    assert record["frame_id"] == 0
    assert record["provenance"] == "replayed" and record["recorded_provenance"] == "simulated"
    assert record["source"] == f"recording:{tmp_path.name}"
    assert player.step() is False  # nothing new until the clock moves

    clock.playback_time_s = 2.05  # jump ahead: frames 1..19 are skipped, 20 is shown
    assert player.step() is True
    record, jpeg = queue.get_nowait()
    assert record["frame_id"] == 20
    box_x1 = record["detections"][0]["bbox_xyxy_px"][0]
    assert box_x1 == pytest.approx(20 + 4 * 20, abs=1)
    assert blob_left_edge(jpeg) == pytest.approx(box_x1, abs=3)  # picture matches its boxes

    clock.playback_time_s = 0.25  # the replay looped back to the start
    assert player.step() is True
    record, jpeg = queue.get_nowait()
    assert record["frame_id"] == 2
    assert blob_left_edge(jpeg) == pytest.approx(20 + 4 * 2, abs=3)

    status = player.status()
    assert status["provenance"] == "replayed" and status["recording"] is None
    assert status["detector"] == "test:bright-blob (recorded)"
    assert player.select_track(424242) is False
    await player.stop()


def test_player_rejects_a_session_without_camera_data(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="no recorded camera data"):
        RecordedPerceptionPlayer(tmp_path, Clock(), T0)


async def test_replay_session_end_to_end_through_the_api(tmp_path: Path) -> None:
    session = tmp_path / "sessions" / "20260101T000000"
    await record_session(session, frames=20)
    write_recording(session / "flight.jsonl", n=20)

    settings = Settings(
        mode=OperatingMode.REPLAY,
        replay_session="latest",
        replay_speed=20.0,
        data_dir=tmp_path,
        frontend_dist=tmp_path / "no-ui",
    )
    assert settings.resolved_replay_session() == session
    with TestClient(create_app(settings)) as client:
        system = client.get("/api/system").json()
        assert system["provenance"] == "replayed" and system["commands_enabled"] is False
        status = client.get("/api/perception").json()
        assert status["enabled"] is True and status["provenance"] == "replayed"
        listed = client.get("/api/recordings").json()
        assert listed[0]["session"] == "20260101T000000" and listed[0]["has_camera"] is True
        assert client.post("/api/vehicle/hold").status_code == 403


def test_replay_of_a_flight_only_session_reports_no_camera(tmp_path: Path) -> None:
    session = tmp_path / "sessions" / "20260101T000000"
    session.mkdir(parents=True)
    write_recording(session / "flight.jsonl")
    settings = Settings(
        mode=OperatingMode.REPLAY,
        replay_session="20260101T000000",
        data_dir=tmp_path,
        frontend_dist=tmp_path / "no-ui",
    )
    with TestClient(create_app(settings)) as client:
        status = client.get("/api/perception").json()
        assert status["enabled"] is False and "no recorded camera data" in status["reason"]
        assert client.get("/api/health").json()["status"] == "ok"

    with pytest.raises(ValueError, match="not a recorded session"):
        Settings(replay_session="nope", data_dir=tmp_path).resolved_replay_session()
