"""Camera adapters, the perception pipeline and its API, without a real model."""

from __future__ import annotations

import asyncio
import json
import struct
import time
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("cv2")
pytest.importorskip("scipy")

import cv2  # noqa: E402
import numpy as np  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from aegis.api import build_perception, create_app  # noqa: E402
from aegis.config import Settings  # noqa: E402
from aegis.contracts import Provenance  # noqa: E402
from aegis.hub import TelemetryHub  # noqa: E402
from aegis.perception.cameras import (  # noqa: E402
    StreamCameraAdapter,
    VideoFileCameraAdapter,
    decode_stream_frame,
)
from aegis.perception.pipeline import PerceptionPipeline  # noqa: E402
from aegis.perception.types import CameraFrame, Detection, Image, TrackState  # noqa: E402
from test_api import FakeVehicle  # noqa: E402


class BrightBlobDetector:
    """Stand-in backend: finds white rectangles. Lets the pipeline run with real pixels."""

    name = "test:bright-blob"

    def infer(self, image: Image) -> list[Detection]:
        mask = (image.min(axis=2) > 200).astype(np.uint8)
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        out = []
        for contour in contours:
            x, y, w, h = cv2.boundingRect(contour)
            if w * h >= 100:
                out.append(
                    Detection(
                        label="blob",
                        class_id=0,
                        confidence=0.9,
                        bbox_xyxy_px=(float(x), float(y), float(x + w), float(y + h)),
                    )
                )
        return out


def scene(frame_index: int, width: int = 320, height: int = 240) -> np.ndarray:
    """Dark frame with one white 30x50 rectangle moving right at 4 px/frame."""
    image = np.zeros((height, width, 3), dtype=np.uint8)
    x = 20 + 4 * frame_index
    image[100:150, x : x + 30] = 255
    return image


def frame(i: int, provenance: Provenance = Provenance.SIMULATED) -> CameraFrame:
    return CameraFrame(scene(i), i, "cam-test", provenance, "test:scene", time.time(), i / 30)


def encode_message(header: dict[str, Any], payload: bytes) -> bytes:
    head = json.dumps(header).encode()
    return struct.pack(">I", len(head)) + head + struct.pack(">I", len(payload)) + payload


# ------------------------------------------------------------------ frame decoding


def test_decode_raw_encodings_to_bgr() -> None:
    rgb = np.zeros((2, 3, 3), dtype=np.uint8)
    rgb[..., 0] = 255  # pure red in RGB
    out = decode_stream_frame({"width": 3, "height": 2, "encoding": "rgb8"}, rgb.tobytes())
    assert out.shape == (2, 3, 3) and tuple(out[0, 0]) == (0, 0, 255)  # red in BGR

    mono = np.full((2, 3), 7, dtype=np.uint8)
    out = decode_stream_frame({"width": 3, "height": 2, "encoding": "mono8"}, mono.tobytes())
    assert out.shape == (2, 3, 3) and tuple(out[1, 2]) == (7, 7, 7)

    ok, jpeg = cv2.imencode(".jpg", scene(0))
    assert ok and decode_stream_frame({"encoding": "jpeg"}, jpeg.tobytes()).shape == (240, 320, 3)


@pytest.mark.parametrize(
    "header,payload,message",
    [
        ({"width": 3, "height": 2, "encoding": "rgb8"}, b"\x00" * 17, "expected 18"),
        ({"width": 3, "height": 2, "encoding": "yuv"}, b"\x00" * 18, "unsupported encoding"),
        ({"height": 2, "encoding": "rgb8"}, b"\x00" * 18, "integer width and height"),
        ({"width": 99999, "height": 2, "encoding": "rgb8"}, b"\x00", "implausible"),
        ({"encoding": "jpeg"}, b"not a jpeg", "decodable JPEG"),
    ],
)
def test_malformed_frames_are_rejected(
    header: dict[str, Any], payload: bytes, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        decode_stream_frame(header, payload)


# ------------------------------------------------------------------- stream camera


async def test_stream_camera_receives_frames_over_tcp() -> None:
    camera = StreamCameraAdapter(port=0, timeout_s=2.0)
    await camera.start()
    assert camera._server is not None
    port = camera._server.sockets[0].getsockname()[1]
    assert (
        await asyncio.wait_for(StreamCameraAdapter(port=0, timeout_s=0.05).read_frame(), 1) is None
    )

    _, writer = await asyncio.open_connection("127.0.0.1", port)
    header = {
        "width": 320,
        "height": 240,
        "encoding": "bgr8",
        "sensor_id": "gz-cam",
        "provenance": "simulated",
        "source": "gazebo",
        "source_time_s": 12.5,
    }
    writer.write(encode_message(header, scene(3).tobytes()))
    await writer.drain()
    got = await camera.read_frame()
    assert got is not None
    assert got.provenance is Provenance.SIMULATED and got.source == "stream:gazebo"
    assert got.sensor_id == "gz-cam" and got.source_time_s == 12.5
    assert np.array_equal(got.image, scene(3))

    # A frame claiming to be physical is refused in this release; the stream stays up.
    writer.write(encode_message({**header, "provenance": "physical"}, scene(4).tobytes()))
    writer.write(encode_message({**header, "encoding": "rgb8", "width": 1}, b"xx"))
    writer.write(encode_message(header, scene(5).tobytes()))
    await writer.drain()
    got = await camera.read_frame()
    assert got is not None and np.array_equal(got.image, scene(5))
    assert camera.rejected_frames == 2

    # Broken framing closes the connection instead of reading garbage forever.
    writer.write(struct.pack(">I", 10_000_000))
    await writer.drain()
    await asyncio.sleep(0.1)
    writer.close()
    await camera.stop()


# --------------------------------------------------------------------- video file


def write_video(path: Path, frames: int = 12) -> None:
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), 60.0, (320, 240))
    assert writer.isOpened(), "OpenCV build cannot write mp4v"
    for i in range(frames):
        writer.write(scene(i))
    writer.release()


async def test_video_file_camera_plays_and_loops(tmp_path: Path) -> None:
    path = tmp_path / "clip.mp4"
    write_video(path, frames=12)
    camera = VideoFileCameraAdapter(path, loop=True)
    await camera.start()
    assert camera.fps == pytest.approx(60.0, rel=0.01)
    frames = [await camera.read_frame() for _ in range(15)]  # more than the clip holds
    await camera.stop()
    assert all(f is not None for f in frames)
    first = frames[0]
    assert first is not None
    assert first.provenance is Provenance.REPLAYED and first.source == "video:clip.mp4"
    assert (first.width, first.height) == (320, 240)
    assert [f.frame_id for f in frames if f] == list(range(1, 16))
    with pytest.raises(FileNotFoundError):
        VideoFileCameraAdapter(tmp_path / "missing.mp4")


# ------------------------------------------------------------------------ pipeline


class ListCamera:
    sensor_id = "cam-test"
    source = "test:scene"

    def __init__(self, frames: list[CameraFrame]) -> None:
        self.frames = frames

    async def start(self) -> None: ...
    async def stop(self) -> None: ...

    async def read_frame(self) -> CameraFrame | None:
        await asyncio.sleep(0.005)
        return self.frames.pop(0) if self.frames else None


async def test_pipeline_detects_tracks_records_and_reports(tmp_path: Path) -> None:
    record = tmp_path / "detections.jsonl"
    pipeline = PerceptionPipeline(ListCamera([]), BrightBlobDetector(), record_path=record)
    await pipeline.start()
    assert "camera.no_frames" in pipeline.status()["faults"]

    queue = pipeline.subscribe()
    result = None
    for i in range(12):
        result = await pipeline.process(frame(i))
    await pipeline.stop()

    assert result is not None and result.provenance is Provenance.SIMULATED
    assert len(result.detections) == 1 and result.detections[0].label == "blob"
    (track,) = result.tracks
    assert track.state is TrackState.CONFIRMED and track.track_id == 1
    assert track.bbox_xyxy_px[0] == pytest.approx(20 + 4 * 11, abs=3)
    assert track.velocity_px_s[0] > 0 and abs(track.velocity_px_s[1]) < 30

    status = pipeline.status()
    assert status["frames_processed"] == 12 and status["tracks"] == 1
    assert status["faults"] == [] and status["inference_latency_ms_p50"] > 0

    payload, jpeg = queue.get_nowait()
    assert payload["detector"] == "test:bright-blob" and jpeg[:2] == b"\xff\xd8"

    lines = [json.loads(line) for line in record.read_text().splitlines()]
    assert len(lines) == 12 and lines[-1]["tracks"][0]["track_id"] == 1
    assert all(line["provenance"] == "simulated" for line in lines)


async def test_operator_selection_is_validated_and_cleared_when_track_ends() -> None:
    pipeline = PerceptionPipeline(ListCamera([]), BrightBlobDetector())
    for i in range(5):
        await pipeline.process(frame(i))
    assert pipeline.select_track(99) is False
    assert pipeline.select_track(1) is True
    assert (await pipeline.process(frame(5))).selected_track_id == 1
    empty = CameraFrame(np.zeros((240, 320, 3), np.uint8), 99, "c", Provenance.SIMULATED, "s", 0.0)
    for _ in range(40):  # object gone for longer than the coast limit
        result = await pipeline.process(empty)
    assert result.tracks == () and result.selected_track_id is None
    assert pipeline.select_track(None) is True


async def test_tracks_are_reset_when_the_source_restarts() -> None:
    pipeline = PerceptionPipeline(ListCamera([]), BrightBlobDetector())
    for i in range(40):  # source time reaches 1.3 s
        await pipeline.process(frame(i))
    assert pipeline.latest is not None and len(pipeline.latest.tracks) == 1
    blank = np.zeros((240, 320, 3), np.uint8)
    restart = CameraFrame(blank, 0, "c", Provenance.REPLAYED, "s", 0.0, 0.0)
    result = await pipeline.process(restart)  # source time jumped back to 0: a new scene
    assert result.tracks == ()  # no stale "predicted" ghosts from the previous scene


# ----------------------------------------------------------------------------- API


def test_perception_is_optional(tmp_path: Path) -> None:
    pipeline, reason = build_perception(Settings(camera="none"))
    assert pipeline is None and "No camera configured" in reason
    pipeline, reason = build_perception(
        Settings(camera="stream", detector_model=tmp_path / "missing.onnx")
    )
    assert pipeline is None and "not found" in reason
    pipeline, reason = build_perception(Settings(camera="webcam"))
    assert pipeline is None and "Unknown AEGIS_CAMERA" in reason

    vehicle = FakeVehicle()
    settings = Settings(camera="none", frontend_dist=tmp_path / "x", data_dir=tmp_path)
    with TestClient(create_app(settings, TelemetryHub(vehicle, vehicle))) as client:
        assert client.get("/api/perception").json()["enabled"] is False
        assert client.post("/api/perception/select", json={"track_id": 1}).status_code == 409
        assert client.get("/api/health").json()["status"] == "ok"  # everything else still works


def test_video_websocket_and_selection_api(tmp_path: Path) -> None:
    frames = [frame(i % 40) for i in range(400)]
    pipeline = PerceptionPipeline(ListCamera(frames), BrightBlobDetector())
    vehicle = FakeVehicle()
    hub = TelemetryHub(vehicle, vehicle)
    settings = Settings(frontend_dist=tmp_path / "x", data_dir=tmp_path)
    with TestClient(create_app(settings, hub, pipeline)) as client:
        with client.websocket_connect("/ws/video") as ws:
            for _ in range(6):
                meta = ws.receive_json()
                jpeg = ws.receive_bytes()
            assert meta["width_px"] == 320 and meta["provenance"] == "simulated"
            assert jpeg[:2] == b"\xff\xd8"
            assert meta["tracks"], "the moving rectangle should be tracked by now"
            track_id = meta["tracks"][0]["track_id"]

        status = client.get("/api/perception").json()
        assert status["enabled"] is True and status["detector"] == "test:bright-blob"
        assert client.post("/api/perception/select", json={"track_id": 9999}).status_code == 404
        chosen = client.post("/api/perception/select", json={"track_id": track_id})
        assert chosen.status_code in (200, 404)  # the loop may have moved on; never a 500
        assert len(hub.extra_checks) == 1


def test_camera_port_conflict_disables_perception_but_not_the_backend(tmp_path: Path) -> None:
    """Regression: a busy stream port crashed startup and orphaned the MAVSDK server."""
    import socket

    with socket.socket() as blocker:
        blocker.bind(("127.0.0.1", 0))
        blocker.listen()
        port = blocker.getsockname()[1]
        camera = StreamCameraAdapter(port=port)
        pipeline = PerceptionPipeline(camera, BrightBlobDetector())
        vehicle = FakeVehicle()
        hub = TelemetryHub(vehicle, vehicle)
        settings = Settings(
            camera="stream",
            camera_stream_port=port,
            frontend_dist=tmp_path / "x",
            data_dir=tmp_path,
        )
        with TestClient(create_app(settings, hub, pipeline)) as client:
            status = client.get("/api/perception").json()
            assert status["enabled"] is False
            assert "Camera could not start" in status["reason"]
            assert "another AEGIS backend" in status["reason"]
            assert client.get("/api/health").json()["status"] == "ok"
            assert hub.extra_checks == []  # no permanent "no camera frames" alert
