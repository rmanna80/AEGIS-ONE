"""USB webcam adapter. No camera exists where these tests run: the device is a
stand-in that behaves like OpenCV's capture. The real webcam is checked with
scripts/camera_check.py on the device it is plugged into."""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("cv2")

import cv2  # noqa: E402
import numpy as np  # noqa: E402

from aegis.contracts import Provenance  # noqa: E402
from aegis.perception.pipeline import PerceptionPipeline  # noqa: E402
from aegis.perception.playback import RecordedPerceptionPlayer  # noqa: E402
from aegis.perception.usb_camera import (  # noqa: E402
    UsbCameraAdapter,
    find_usb_cameras,
    fourcc_text,
    resolve_device,
)
from test_cameras_pipeline import BrightBlobDetector  # noqa: E402


class FakeCapture:
    """Delivers frames at ``fps``. ``driver_clock``: 'monotonic' stamps buffers on
    time.monotonic() (``delay_s`` before delivery), 'relative' counts from zero."""

    def __init__(
        self,
        fps: float = 100.0,
        driver_clock: str = "monotonic",
        delay_s: float = 0.015,
        fail_after: int | None = None,
        opened: bool = True,
    ) -> None:
        self.fps, self.driver_clock, self.delay_s = fps, driver_clock, delay_s
        self.fail_after, self.opened = fail_after, opened
        self.props: dict[int, float] = {}
        self.frames = 0
        self.stamp_ms = 0.0
        self.released = False

    def isOpened(self) -> bool:  # noqa: N802
        return self.opened

    def read(self) -> tuple[bool, Any]:
        time.sleep(1.0 / self.fps)
        if self.fail_after is not None and self.frames >= self.fail_after:
            return False, None
        self.frames += 1
        if self.driver_clock == "monotonic":
            self.stamp_ms = (time.monotonic() - self.delay_s) * 1000.0
        else:
            self.stamp_ms = self.frames * 1000.0 / self.fps
        image = np.zeros((480, 640, 3), np.uint8)
        image[200:280, 100 + self.frames % 400 : 180 + self.frames % 400] = 255
        return True, image

    def get(self, prop: int) -> float:
        if prop == cv2.CAP_PROP_POS_MSEC:
            return self.stamp_ms
        if prop == cv2.CAP_PROP_FOURCC:
            return float(cv2.VideoWriter.fourcc(*"MJPG"))
        return self.props.get(prop, 0.0)

    def set(self, prop: int, value: float) -> bool:
        self.props[prop] = value
        return True

    def release(self) -> None:
        self.released = True


async def until(condition: Any, timeout_s: float = 5.0) -> None:
    for _ in range(int(timeout_s / 0.05)):
        if condition():
            return
        await asyncio.sleep(0.05)


def fake_pi5_sysfs(root: Path) -> Path:
    """Video4Linux nodes as on a Raspberry Pi 5 with one USB webcam: camera-interface
    and ISP nodes first, then the webcam's image node and its metadata node."""
    sysfs = root / "video4linux"
    for number, driver, index, name in [
        (0, "rp1-cfe", "0", "rp1-cfe-csi2_ch0"),
        (19, "pispbe", "0", "pispbe-output0"),
        (36, "uvcvideo", "0", "HD Webcam"),
        (37, "uvcvideo", "1", "HD Webcam"),
    ]:
        node = sysfs / f"video{number}"
        (node / "device").mkdir(parents=True)
        target = root / "drivers" / driver
        target.mkdir(parents=True, exist_ok=True)
        os.symlink(target, node / "device" / "driver")
        (node / "index").write_text(index + "\n")
        (node / "name").write_text(name + "\n")
    return sysfs


# ---------------------------------------------------------------------- finding it


def test_the_webcam_is_found_among_the_pi5_camera_nodes(tmp_path: Path) -> None:
    sysfs = fake_pi5_sysfs(tmp_path)
    assert find_usb_cameras(sysfs) == [{"device": "/dev/video36", "name": "HD Webcam"}]
    assert resolve_device("usb", sysfs) == "/dev/video36"
    assert resolve_device("usb:4", sysfs) == "/dev/video4"
    assert resolve_device("usb:/dev/video2", sysfs) == "/dev/video2"


def test_no_webcam_says_how_to_check(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="v4l2-ctl --list-devices"):
        resolve_device("usb", tmp_path / "missing")


def test_fourcc_text() -> None:
    assert fourcc_text(float(cv2.VideoWriter.fourcc(*"MJPG"))) == "MJPG"
    assert fourcc_text(0.0) == "unknown"


# --------------------------------------------------------------------- reading it


def adapter_for(capture: FakeCapture, **kwargs: Any) -> tuple[UsbCameraAdapter, list[str]]:
    opened: list[str] = []

    def opener(device: str) -> FakeCapture:
        opened.append(device)
        return capture

    return UsbCameraAdapter("usb:/dev/video36", opener=opener, **kwargs), opened


async def test_frames_are_physical_bench_frames_and_the_negotiation_is_reported() -> None:
    capture = FakeCapture()
    camera, opened = adapter_for(capture, width=1280, height=720, fps=30.0)
    await camera.start()
    try:
        frame = await camera.read_frame()
        assert frame is not None
        assert frame.provenance is Provenance.PHYSICAL
        assert frame.source == "usb:/dev/video36" and opened == ["/dev/video36"]
        assert abs(frame.captured_at_unix_s - time.time()) < 1.0
        assert camera.mount == "bench"
        # What was asked for is set on the device, and what it reports back is shown.
        assert capture.props[cv2.CAP_PROP_FRAME_WIDTH] == 1280.0
        assert capture.props[cv2.CAP_PROP_BUFFERSIZE] == 1.0
        assert camera.details()["negotiated"]["format"] == "MJPG"
    finally:
        await camera.stop()
    assert capture.released


async def test_a_slow_consumer_gets_the_newest_frame_and_drops_are_counted() -> None:
    camera, _ = adapter_for(FakeCapture(fps=100.0))
    await camera.start()
    try:
        await camera.read_frame()
        await asyncio.sleep(0.2)  # a slow detector: about 20 frames arrive meanwhile
        frame = await camera.read_frame()
        assert frame is not None and frame.frame_id == 2
        details = camera.details()
        assert details["frames_dropped_before_processing"] >= 10
        assert 60.0 <= details["delivered_fps"] <= 110.0
    finally:
        await camera.stop()


async def test_driver_delay_is_measured_only_on_the_monotonic_clock() -> None:
    camera, _ = adapter_for(FakeCapture(driver_clock="monotonic", delay_s=0.015))
    await camera.start()
    await asyncio.sleep(0.3)
    delay = camera.details()["driver_to_aegis_delay"]
    await camera.stop()
    assert delay["available"] and 10.0 <= delay["median_ms"] <= 40.0

    other, _ = adapter_for(FakeCapture(driver_clock="relative"))
    await other.start()
    await asyncio.sleep(0.3)
    delay = other.details()["driver_to_aegis_delay"]
    await other.stop()
    assert delay == {
        "available": False,
        "reason": "the driver's timestamps are not on this computer's monotonic clock",
    }


async def test_an_unplugged_camera_is_reported_and_reopened() -> None:
    capture = FakeCapture(fps=500.0, fail_after=5)
    camera, opened = adapter_for(capture)
    await camera.start()
    await until(lambda: len(opened) >= 2)
    error = camera.details()["last_error"]
    await camera.stop()
    assert len(opened) >= 2  # tried again after the device stopped delivering
    assert error is None or "stopped delivering" in error


async def test_a_camera_that_will_not_open_says_why() -> None:
    camera, _ = adapter_for(FakeCapture(opened=False))
    with pytest.raises(RuntimeError, match="video' group"):
        await camera.start()


# ------------------------------------------------------- pipeline and recording


async def test_physical_video_is_not_saved_but_detections_are_and_replay_shows_boxes(
    tmp_path: Path,
) -> None:
    camera, _ = adapter_for(FakeCapture(fps=60.0))
    pipeline = PerceptionPipeline(
        camera, BrightBlobDetector(), record_dir=tmp_path, record_video=False
    )
    await pipeline.start()
    await until(lambda: pipeline.frames_processed >= 15)
    status = pipeline.status()
    await pipeline.stop()

    assert status["provenance"] == "physical"
    assert status["camera_mount"] == "bench"
    assert status["recording_video"] is False
    assert status["camera"]["kind"] == "USB camera (Video4Linux)"
    assert not (tmp_path / "video.mp4").exists()
    lines = [json.loads(x) for x in (tmp_path / "detections.jsonl").read_text().splitlines()]
    assert len(lines) >= 15 and all(line["provenance"] == "physical" for line in lines)
    assert any(line["detections"] for line in lines)

    class Clock:
        playback_time_s = 0.0

    clock = Clock()
    player = RecordedPerceptionPlayer(tmp_path, clock, lines[0]["captured_at_unix_s"])
    queue = player.subscribe()
    clock.playback_time_s = lines[5]["captured_at_unix_s"] - lines[0]["captured_at_unix_s"]
    assert player.step()
    record, jpeg = queue.get_nowait()
    assert record["provenance"] == "replayed" and record["recorded_provenance"] == "physical"
    image = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
    assert image.shape[:2] == (480, 640)
    assert player.status()["video_recorded"] is False


def test_record_video_default_depends_on_the_camera(tmp_path: Path) -> None:
    from aegis.api import build_perception
    from aegis.config import Settings
    from aegis.hub import TelemetryHub
    from test_api import FakeVehicle

    model = Path(__file__).resolve().parents[2] / "models/yolo11n.onnx"
    if not model.is_file():
        pytest.skip("detector model not downloaded")
    vehicle = FakeVehicle()
    hub = TelemetryHub(vehicle, vehicle)
    usb, reason = build_perception(Settings(camera="usb", detector_model=model), hub, tmp_path)
    assert usb is not None, reason
    assert isinstance(usb, PerceptionPipeline) and usb.record_video is False
    always, _ = build_perception(
        Settings(camera="usb", detector_model=model, record_camera_video="always"), hub, tmp_path
    )
    assert isinstance(always, PerceptionPipeline) and always.record_video is True


def test_the_reader_thread_does_not_outlive_stop() -> None:
    async def run() -> None:
        camera, _ = adapter_for(FakeCapture())
        await camera.start()
        await camera.read_frame()
        await camera.stop()

    asyncio.run(run())
    assert not any(t.name == "usb-camera" for t in threading.enumerate())


async def test_the_detector_rate_limit_holds_and_still_shows_current_frames() -> None:
    """AEGIS_DETECTOR_MAX_FPS: on the Pi the detector otherwise uses every core it can."""
    camera, _ = adapter_for(FakeCapture(fps=100.0))
    pipeline = PerceptionPipeline(camera, BrightBlobDetector(), max_fps=5.0)
    await pipeline.start()
    await asyncio.sleep(1.5)
    processed = pipeline.frames_processed
    latest = pipeline.latest
    await pipeline.stop()
    assert 6 <= processed <= 9  # about 5 per second, not ~100
    assert latest is not None and time.time() - latest.captured_at_unix_s < 0.5
    assert pipeline.status()["max_fps"] == 5.0
