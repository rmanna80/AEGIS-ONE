"""A USB (UVC) webcam on this computer, read through Video4Linux.

This is AEGIS's first physical sensor. Its frames are labelled ``physical``. It is a
**bench** camera: it is not mounted on any vehicle, so nothing it sees is related to
the (simulated) flight, and nothing in AEGIS projects its detections into the world.
Detection stays at the level of object classes; there is no face recognition.

The camera is read continuously on a background thread and only the newest frame is
kept, so a slow detector processes the latest image instead of an ever older backlog.
Frames the detector had no time for are counted as dropped.

Timing. ``captured_at_unix_s`` is when this computer received the frame, not when it
was exposed. The V4L2 driver stamps each buffer; when that stamp is on this
computer's monotonic clock, the delay from the driver to AEGIS is measured and
reported. When it is not (some drivers, some OpenCV builds), the delay is reported as
not available rather than guessed. Exposure time and the USB transfer before the
driver stamp are never visible to software and are not included.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
import time
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

import cv2
import numpy as np

from aegis.contracts import Provenance
from aegis.perception.types import CameraFrame

log = logging.getLogger(__name__)

SYSFS_VIDEO = Path("/sys/class/video4linux")
REOPEN_AFTER_FAILURES = 30  # consecutive failed reads before the device is reopened
REOPEN_INTERVAL_S = 2.0
MAX_PLAUSIBLE_AGE_S = 5.0


class Capture(Protocol):
    """The part of ``cv2.VideoCapture`` this adapter uses (a stand-in for tests)."""

    def isOpened(self) -> bool: ...  # noqa: N802 - OpenCV's name
    def read(self) -> tuple[bool, Any]: ...
    def get(self, prop: int) -> float: ...
    def set(self, prop: int, value: float) -> bool: ...
    def release(self) -> None: ...


def open_v4l2(device: str) -> Capture:
    return cv2.VideoCapture(device, cv2.CAP_V4L2)


def find_usb_cameras(sysfs: Path = SYSFS_VIDEO) -> list[dict[str, str]]:
    """Video4Linux capture nodes driven by the USB video class driver, in device order.

    A UVC camera creates two nodes (image and metadata); the image node has index 0.
    On a Raspberry Pi 5 the low numbers belong to the camera interface and the ISP, so
    "/dev/video0" is usually not the webcam."""
    found: list[dict[str, str]] = []
    if not sysfs.is_dir():
        return found
    for node in sorted(sysfs.glob("video*"), key=lambda p: int(p.name[5:] or 0)):
        try:
            driver = (node / "device" / "driver").resolve().name
            index = (node / "index").read_text().strip() if (node / "index").exists() else "0"
            name = (node / "name").read_text().strip() if (node / "name").exists() else ""
        except OSError:
            continue
        if driver == "uvcvideo" and index == "0":
            found.append({"device": f"/dev/{node.name}", "name": name})
    return found


def resolve_device(spec: str, sysfs: Path = SYSFS_VIDEO) -> str:
    """``usb`` -> the first USB camera found; ``usb:2`` -> /dev/video2;
    ``usb:/dev/video4`` -> that path."""
    value = spec.removeprefix("usb").removeprefix(":")
    if not value:
        cameras = find_usb_cameras(sysfs)
        if not cameras:
            raise RuntimeError(
                "No USB camera found (no Video4Linux device uses the uvcvideo driver). "
                "Check it is plugged in and listed by: v4l2-ctl --list-devices"
            )
        return cameras[0]["device"]
    if value.isdigit():
        return f"/dev/video{value}"
    return value


def fourcc_text(value: float) -> str:
    code = int(value)
    text = "".join(chr((code >> 8 * i) & 0xFF) for i in range(4))
    return text if text.isprintable() and text.strip() else "unknown"


class UsbCameraAdapter:
    def __init__(
        self,
        spec: str = "usb",
        width: int = 640,
        height: int = 480,
        fps: float = 30.0,
        timeout_s: float = 1.0,
        opener: Callable[[str], Capture] | None = None,
        sysfs: Path = SYSFS_VIDEO,
    ) -> None:
        self.spec = spec
        self.requested: dict[str, Any] = {
            "width": width,
            "height": height,
            "fps": fps,
            "format": "MJPG",
        }
        self.timeout_s = timeout_s
        self._opener = opener  # None: open_v4l2, looked up when opening
        self._sysfs = sysfs
        self.device: str | None = None
        self.sensor_id = "usb-1"
        self.provenance = Provenance.PHYSICAL
        self.mount = "bench"  # not on a vehicle
        self.source = f"usb:{spec}"
        self.negotiated: dict[str, Any] = {}
        self._capture: Capture | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._pending: tuple[Any, float, float, float] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._new_frame = asyncio.Event()
        self._frame_id = 0
        self._delivered_at: deque[float] = deque(maxlen=90)
        self._ages_s: deque[float] = deque(maxlen=300)
        self.frames_delivered = 0
        self.frames_dropped = 0
        self.read_failures = 0
        self.reopens = 0
        self.driver_clock: str = "unknown"  # "monotonic", "other" or "unknown"
        self.last_error: str | None = None

    # ------------------------------------------------------------------ lifecycle

    def _open(self) -> Capture:
        self.device = resolve_device(self.spec, self._sysfs)
        self.source = f"usb:{self.device}"
        capture = (self._opener or open_v4l2)(self.device)
        if not capture.isOpened():
            capture.release()
            raise RuntimeError(
                f"Could not open {self.device}. Is it in use by another program, and is "
                "this user in the 'video' group?"
            )
        # Ask for compressed frames: most webcams only reach 30 fps at 640x480 in MJPG.
        capture.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter.fourcc(*"MJPG"))
        capture.set(cv2.CAP_PROP_FRAME_WIDTH, float(self.requested["width"]))
        capture.set(cv2.CAP_PROP_FRAME_HEIGHT, float(self.requested["height"]))
        capture.set(cv2.CAP_PROP_FPS, float(self.requested["fps"]))
        capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        # What the camera actually agreed to (it may ignore requests).
        self.negotiated = {
            "width": int(capture.get(cv2.CAP_PROP_FRAME_WIDTH)),
            "height": int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT)),
            "fps": round(capture.get(cv2.CAP_PROP_FPS), 2),
            "format": fourcc_text(capture.get(cv2.CAP_PROP_FOURCC)),
        }
        log.info("USB camera %s opened: %s", self.device, self.negotiated)
        return capture

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._capture = await asyncio.to_thread(self._open)
        self._stop.clear()
        self._thread = threading.Thread(target=self._reader, name="usb-camera", daemon=True)
        self._thread.start()

    async def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            await asyncio.to_thread(self._thread.join, 3.0)
            self._thread = None
        if self._capture is not None:
            self._capture.release()
            self._capture = None

    # --------------------------------------------------------------------- reader

    def _reader(self) -> None:
        failures = 0
        while not self._stop.is_set():
            capture = self._capture
            if capture is None:
                if self._stop.wait(REOPEN_INTERVAL_S):
                    return
                try:
                    self._capture = self._open()
                    self.reopens += 1
                    self.last_error = None
                except (RuntimeError, OSError, cv2.error) as exc:
                    self.last_error = str(exc)
                continue
            ok, image = capture.read()
            received_mono, received_unix = time.monotonic(), time.time()
            if not ok or image is None or getattr(image, "size", 0) == 0:
                failures += 1
                self.read_failures += 1
                if failures >= REOPEN_AFTER_FAILURES:
                    self.last_error = f"{self.device} stopped delivering frames (unplugged?)"
                    log.warning("%s; reopening", self.last_error)
                    with contextlib.suppress(Exception):
                        capture.release()
                    self._capture = None
                    failures = 0
                else:
                    time.sleep(0.02)
                continue
            failures = 0
            driver_s = capture.get(cv2.CAP_PROP_POS_MSEC) / 1000.0
            self._note_driver_time(driver_s, received_mono)
            with self._lock:
                if self._pending is not None:
                    self.frames_dropped += 1
                self._pending = (image, received_unix, received_mono, driver_s)
                self.frames_delivered += 1
                self._delivered_at.append(received_mono)
            if self._loop is not None:
                with contextlib.suppress(RuntimeError):  # loop closed during shutdown
                    self._loop.call_soon_threadsafe(self._new_frame.set)

    def _note_driver_time(self, driver_s: float, received_mono: float) -> None:
        """Keep the driver-to-AEGIS delay only when the driver stamp is plausibly on
        this computer's monotonic clock; otherwise say the delay is unknown."""
        if driver_s <= 0.0:
            return
        age = received_mono - driver_s
        if 0.0 <= age <= MAX_PLAUSIBLE_AGE_S:
            self.driver_clock = "monotonic"
            self._ages_s.append(age)
        elif self.driver_clock != "monotonic":
            self.driver_clock = "other"

    # ----------------------------------------------------------------- CameraSource

    async def read_frame(self) -> CameraFrame | None:
        try:
            await asyncio.wait_for(self._new_frame.wait(), timeout=self.timeout_s)
        except TimeoutError:
            return None
        self._new_frame.clear()
        with self._lock:
            pending, self._pending = self._pending, None
        if pending is None:
            return None
        image, received_unix, _, driver_s = pending
        self._frame_id += 1
        return CameraFrame(
            image=image,
            frame_id=self._frame_id,
            sensor_id=self.sensor_id,
            provenance=Provenance.PHYSICAL,
            source=self.source,
            captured_at_unix_s=received_unix,
            source_time_s=driver_s if driver_s > 0.0 else None,
        )

    # ---------------------------------------------------------------------- status

    @property
    def delivered_fps(self) -> float | None:
        times = list(self._delivered_at)
        if len(times) < 2 or times[-1] <= times[0]:
            return None
        return (len(times) - 1) / (times[-1] - times[0])

    def details(self) -> dict[str, Any]:
        ages = np.array(self._ages_s) * 1000.0 if self._ages_s else None
        delay: dict[str, Any]
        if ages is not None and self.driver_clock == "monotonic":
            delay = {
                "available": True,
                "median_ms": round(float(np.median(ages)), 1),
                "p95_ms": round(float(np.percentile(ages, 95)), 1),
                "note": "From the driver's buffer stamp to AEGIS; excludes exposure and "
                "the USB transfer before the stamp.",
            }
        else:
            delay = {
                "available": False,
                "reason": "the driver's timestamps are not on this computer's monotonic clock"
                if self.driver_clock == "other"
                else "no driver timestamps yet",
            }
        fps = self.delivered_fps
        return {
            "kind": "USB camera (Video4Linux)",
            "device": self.device,
            "mount": "bench: not on any vehicle",
            "requested": self.requested,
            "negotiated": self.negotiated,
            "delivered_fps": None if fps is None else round(fps, 1),
            "frames_delivered": self.frames_delivered,
            "frames_dropped_before_processing": self.frames_dropped,
            "read_failures": self.read_failures,
            "reopens": self.reopens,
            "driver_to_aegis_delay": delay,
            "last_error": self.last_error,
        }
