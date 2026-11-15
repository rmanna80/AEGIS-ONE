"""Camera evidence for incident packages.

The session video is an MP4 that cannot be read until recording stops, so incidents
cannot cut a clip out of it while the backend runs. Instead this keeps the camera's
recent past in memory (the same seconds kept before an incident's trigger) and, while
an incident is open, keeps every processed frame until it closes.

Frames are kept as small JPEGs only when the session is allowed to save camera video.
For a physical camera that is off by default, so only labels and boxes are kept and
the package says why it has no images.

Times are ``captured_at_unix_s``: this computer's wall clock when the frame reached
AEGIS. The flight log's ``utc`` is the same clock on the same computer, which is what
lets the two be cut to one window.
"""

from __future__ import annotations

import json
import threading
from collections import deque
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from aegis.perception.types import Image

JPEG_QUALITY = 70
MAX_WIDTH = 640
FRAME_ROWS = "detections.jsonl"
VIDEO_NAME = "video.mp4"
NOMINAL_FPS = 10.0  # the container's rate; playback is timed from the detection log


class CameraEvidence:
    def __init__(self, pre_s: float = 15.0, max_frames: int = 3000, keep_images: bool = True):
        self.pre_s = pre_s
        self.max_frames = max_frames
        self.keep_images = keep_images
        self._lock = threading.Lock()
        self._ring: deque[tuple[float, dict[str, Any], bytes | None]] = deque()
        self._capturing = False
        self._captured: list[tuple[float, dict[str, Any], bytes | None]] = []
        self.frames_over_limit = 0
        self.mount: str | None = None

    def observe(self, payload: dict[str, Any], image: Image | None) -> None:
        """One processed frame (called from a worker thread by the pipeline)."""
        jpeg = _jpeg(image) if self.keep_images and image is not None else None
        at = float(payload["captured_at_unix_s"])
        item = (at, payload, jpeg)
        with self._lock:
            if self._capturing:
                if len(self._captured) < self.max_frames:
                    self._captured.append(item)
                else:
                    self.frames_over_limit += 1
            self._ring.append(item)
            while self._ring and at - self._ring[0][0] > self.pre_s:
                self._ring.popleft()

    def begin(self) -> None:
        """An incident opened: keep the recent past and everything from now on."""
        with self._lock:
            if not self._capturing:
                self._captured = list(self._ring)
                self.frames_over_limit = 0
                self._capturing = True

    def end(self) -> list[tuple[float, dict[str, Any], bytes | None]]:
        with self._lock:
            captured, self._captured = self._captured, []
            self._capturing = False
            return captured


def _jpeg(image: Image) -> bytes | None:
    if image.shape[1] > MAX_WIDTH:
        scale = MAX_WIDTH / image.shape[1]
        image = cv2.resize(image, (MAX_WIDTH, max(2, round(image.shape[0] * scale / 2) * 2)))
    ok, encoded = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
    return bytes(encoded.tobytes()) if ok else None


def write_camera_evidence(
    folder: Path,
    items: list[tuple[float, dict[str, Any], bytes | None]],
    utc_start_s: float,
    utc_end_s: float,
    images_allowed: bool,
    mount: str | None,
) -> dict[str, Any]:
    """Write the frames inside the window: ``detections.jsonl`` and, when images were
    kept, ``video.mp4`` with one video frame per detection line. Returns a summary
    for the incident document."""
    inside = [item for item in items if utc_start_s <= item[0] <= utc_end_s]
    if not inside:
        return {"frames": 0, "note": "No camera frames were processed in this window."}
    inside.sort(key=lambda item: item[0])
    writer: cv2.VideoWriter | None = None
    size: tuple[int, int] | None = None  # (width, height), fixed by the first frame
    rows: list[str] = []
    for _, payload, jpeg in inside:
        if images_allowed and jpeg is not None:
            image = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
            if image is None:
                continue  # a corrupt image: leave the frame and its row out, together
            if writer is None or size is None:
                size = (int(image.shape[1]), int(image.shape[0]))
                fourcc = cv2.VideoWriter.fourcc(*"mp4v")
                writer = cv2.VideoWriter(str(folder / VIDEO_NAME), fourcc, NOMINAL_FPS, size)
            if (image.shape[1], image.shape[0]) != size:
                image = cv2.resize(image, size)
            writer.write(image)
        rows.append(json.dumps(payload, separators=(",", ":")))
    if writer is not None:
        writer.release()
    (folder / FRAME_ROWS).write_text("\n".join(rows) + "\n")
    first, last = inside[0][1], inside[-1][1]
    span = inside[-1][0] - inside[0][0]
    return {
        "frames": len(rows),
        "frames_per_second": round((len(rows) - 1) / span, 2) if span > 0 else None,
        "provenance": first.get("provenance"),
        "source": first.get("source"),
        "mount": mount,
        "detector": first.get("detector"),
        "video": VIDEO_NAME if writer is not None else None,
        "why_no_video": None
        if writer is not None
        else "camera video is not saved for this camera (physical camera: labels and boxes "
        "only, see AEGIS_RECORD_CAMERA_VIDEO)",
        "first_frame_utc_s": first["captured_at_unix_s"],
        "last_frame_utc_s": last["captured_at_unix_s"],
        "clock": "captured_at_unix_s: this computer's wall clock when the frame reached "
        "AEGIS, the same clock as the flight log's utc. Not the exposure time.",
    }
