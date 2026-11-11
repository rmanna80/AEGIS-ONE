"""Camera sources.

* :class:`VideoFileCameraAdapter` - a saved video file, paced at its own frame rate.
* :class:`StreamCameraAdapter` - frames pushed over TCP by a small sender process
  (the Gazebo camera bridge today; a Raspberry Pi camera later).

Frame-stream wire format (one TCP connection, sender -> AEGIS), repeated::

    uint32 BE  header length
    bytes      UTF-8 JSON header
    uint32 BE  payload length
    bytes      payload

Header keys: ``width``, ``height``, ``encoding`` ("rgb8", "bgr8", "mono8" or
"jpeg"), ``sensor_id``, ``provenance`` ("simulated" or "replayed"), ``source``,
and optionally ``source_time_s``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import struct
import time
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from aegis.contracts import Provenance
from aegis.perception.types import CameraFrame, Image

log = logging.getLogger(__name__)

MAX_HEADER_BYTES = 4096
MAX_PAYLOAD_BYTES = 64 * 1024 * 1024
ACCEPTED_PROVENANCE = {Provenance.SIMULATED, Provenance.REPLAYED}


class VideoFileCameraAdapter:
    """Plays a video file as a camera. Prerecorded footage is labelled ``replayed``."""

    def __init__(self, path: Path, loop: bool = True, sensor_id: str = "video-1") -> None:
        if not path.is_file():
            raise FileNotFoundError(f"Video file not found: {path}")
        self.path = path
        self.loop = loop
        self.sensor_id = sensor_id
        self.provenance = Provenance.REPLAYED
        self.source = f"video:{path.name}"
        self._capture: cv2.VideoCapture | None = None
        self._frame_id = 0
        self._next_due = 0.0
        self.fps = 30.0

    async def start(self) -> None:
        self._capture = cv2.VideoCapture(str(self.path))
        if not self._capture.isOpened():
            raise RuntimeError(f"OpenCV could not open {self.path}")
        fps = self._capture.get(cv2.CAP_PROP_FPS)
        self.fps = fps if 1.0 <= fps <= 240.0 else 30.0
        self._next_due = time.monotonic()

    async def stop(self) -> None:
        if self._capture is not None:
            self._capture.release()
            self._capture = None

    async def read_frame(self) -> CameraFrame | None:
        capture = self._capture
        if capture is None:
            return None
        delay = self._next_due - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)
        ok, image = await asyncio.to_thread(capture.read)
        if not ok:
            if not self.loop:
                return None
            capture.set(cv2.CAP_PROP_POS_FRAMES, 0)
            ok, image = await asyncio.to_thread(capture.read)
            if not ok:
                return None
        # If inference is slower than the video, do not try to catch up in a burst.
        self._next_due = max(self._next_due + 1.0 / self.fps, time.monotonic())
        self._frame_id += 1
        video_time = capture.get(cv2.CAP_PROP_POS_MSEC) / 1000.0
        return CameraFrame(
            image=image,
            frame_id=self._frame_id,
            sensor_id=self.sensor_id,
            provenance=self.provenance,
            source=self.source,
            captured_at_unix_s=time.time(),
            source_time_s=video_time,
        )


def decode_stream_frame(header: dict[str, Any], payload: bytes) -> Image:
    """Decode one frame-stream payload to a BGR image. Raises ``ValueError`` if malformed."""
    encoding = header.get("encoding")
    if encoding == "jpeg":
        image = cv2.imdecode(np.frombuffer(payload, dtype=np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            raise ValueError("payload is not a decodable JPEG")
        return image
    try:
        width, height = int(header["width"]), int(header["height"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("header needs integer width and height") from exc
    if not (0 < width <= 8192 and 0 < height <= 8192):
        raise ValueError(f"implausible frame size {width}x{height}")
    channels = {"rgb8": 3, "bgr8": 3, "mono8": 1}.get(str(encoding))
    if channels is None:
        raise ValueError(f"unsupported encoding {encoding!r}")
    if len(payload) != width * height * channels:
        raise ValueError(
            f"payload is {len(payload)} bytes, expected {width * height * channels} "
            f"for {width}x{height} {encoding}"
        )
    array = np.frombuffer(payload, dtype=np.uint8).reshape(height, width, channels)
    if encoding == "rgb8":
        return cv2.cvtColor(array, cv2.COLOR_RGB2BGR)
    if encoding == "mono8":
        return cv2.cvtColor(array, cv2.COLOR_GRAY2BGR)
    return array.copy()


class StreamCameraAdapter:
    """Listens on a TCP port for one frame sender at a time. Keeps only the newest frame."""

    def __init__(self, host: str = "127.0.0.1", port: int = 5600, timeout_s: float = 1.0) -> None:
        self.host, self.port, self.timeout_s = host, port, timeout_s
        self.sensor_id = "stream-1"
        self.source = f"stream:{port}"
        self._server: asyncio.Server | None = None
        self._latest: CameraFrame | None = None
        self._new_frame = asyncio.Event()
        self._frame_id = 0
        self.rejected_frames = 0

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, self.host, self.port)
        log.info("Camera stream listening on %s:%d", self.host, self.port)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            with contextlib.suppress(Exception):
                await self._server.wait_closed()
            self._server = None

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        log.info("Camera sender connected: %s", peer)
        try:
            while True:
                (header_len,) = struct.unpack(">I", await reader.readexactly(4))
                if not 0 < header_len <= MAX_HEADER_BYTES:
                    raise ValueError(f"header length {header_len} out of range")
                header = json.loads(await reader.readexactly(header_len))
                (payload_len,) = struct.unpack(">I", await reader.readexactly(4))
                if not 0 < payload_len <= MAX_PAYLOAD_BYTES:
                    raise ValueError(f"payload length {payload_len} out of range")
                payload = await reader.readexactly(payload_len)
                try:
                    self._accept(header, payload)
                except ValueError as exc:
                    self.rejected_frames += 1
                    log.warning("Rejected camera frame: %s", exc)
        except (asyncio.IncompleteReadError, ConnectionError):
            log.info("Camera sender disconnected: %s", peer)
        except (ValueError, json.JSONDecodeError) as exc:
            # Framing is broken; the byte stream cannot be resynchronised.
            log.warning("Closing malformed camera stream from %s: %s", peer, exc)
        finally:
            writer.close()

    def _accept(self, header: dict[str, Any], payload: bytes) -> None:
        if not isinstance(header, dict):
            raise ValueError("header is not a JSON object")
        try:
            provenance = Provenance(str(header.get("provenance")))
        except ValueError as exc:
            raise ValueError(f"unknown provenance {header.get('provenance')!r}") from exc
        if provenance not in ACCEPTED_PROVENANCE:
            raise ValueError(f"provenance {provenance.value!r} is not accepted in this release")
        image = decode_stream_frame(header, payload)
        self._frame_id += 1
        self.sensor_id = str(header.get("sensor_id", "stream-1"))[:64]
        source_time = header.get("source_time_s")
        self._latest = CameraFrame(
            image=image,
            frame_id=self._frame_id,
            sensor_id=self.sensor_id,
            provenance=provenance,
            source=f"stream:{str(header.get('source', 'unknown'))[:64]}",
            captured_at_unix_s=time.time(),
            source_time_s=float(source_time) if isinstance(source_time, int | float) else None,
        )
        self._new_frame.set()

    async def read_frame(self) -> CameraFrame | None:
        try:
            await asyncio.wait_for(self._new_frame.wait(), timeout=self.timeout_s)
        except TimeoutError:
            return None
        self._new_frame.clear()
        return self._latest
