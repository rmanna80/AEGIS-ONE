"""Synchronized playback of a recorded session's camera video and detections.

Follows the telemetry replay clock: at replay time ``t`` it shows the frame that
was captured ``t`` seconds after the flight recording started, so video, boxes,
track IDs and vehicle state stay aligned (including across loops and at any
replay speed). Everything it emits is labelled ``replayed``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Protocol

import cv2

from aegis.contracts import Provenance
from aegis.perception.pipeline import DETECTIONS_FILE, VIDEO_FILE, VideoMessage, encode_jpeg

log = logging.getLogger(__name__)


class ReplayClock(Protocol):
    playback_time_s: float


class RecordedPerceptionPlayer:
    def __init__(self, session_dir: Path, clock: ReplayClock, flight_started_at_unix_s: float):
        detections = session_dir / DETECTIONS_FILE
        self._video_path = session_dir / VIDEO_FILE
        if not detections.is_file() or not self._video_path.is_file():
            raise FileNotFoundError(f"{session_dir} has no recorded camera data")
        self._records: list[dict[str, Any]] = []
        with detections.open(encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    self._records.append(json.loads(line))
        if not self._records:
            raise ValueError(f"{detections} is empty")
        self.session = session_dir.name
        self._clock = clock
        self._t0 = flight_started_at_unix_s
        original = self._records[0].get("provenance", "simulated")
        self._original = Provenance(original)
        self.detector = SimpleNamespace(name=f"{self._records[0].get('detector')} (recorded)")
        self.latest: dict[str, Any] | None = None
        self.selected_track_id: int | None = None
        self._subscribers: set[asyncio.Queue[VideoMessage]] = set()
        self._task: asyncio.Task[None] | None = None
        self._capture: cv2.VideoCapture | None = None
        self._next = 0  # index of the next record / video frame to read
        self._last_target = float("-inf")
        self.frames_shown = 0

    async def start(self) -> None:
        self._open()
        self._task = asyncio.create_task(self._loop(), name="perception-playback")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        if self._capture is not None:
            self._capture.release()
            self._capture = None

    def _open(self) -> None:
        if self._capture is not None:
            self._capture.release()
        self._capture = cv2.VideoCapture(str(self._video_path))
        if not self._capture.isOpened():
            raise RuntimeError(f"OpenCV could not open {self._video_path}")
        self._next = 0

    def subscribe(self) -> asyncio.Queue[VideoMessage]:
        queue: asyncio.Queue[VideoMessage] = asyncio.Queue(maxsize=2)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[VideoMessage]) -> None:
        self._subscribers.discard(queue)

    def select_track(self, track_id: int | None) -> bool:
        if track_id is not None and (
            self.latest is None or track_id not in {t["track_id"] for t in self.latest["tracks"]}
        ):
            return False
        self.selected_track_id = track_id
        return True

    async def _loop(self) -> None:
        while True:
            try:
                self.step()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Perception playback step failed")
                await asyncio.sleep(0.5)
            await asyncio.sleep(0.03)

    def step(self) -> bool:
        """Advance to the replay clock. Returns True if a new frame was published."""
        target = self._t0 + self._clock.playback_time_s
        if target < self._last_target - 0.5:  # the replay looped or was rewound
            self._open()
            self.latest = None
        self._last_target = target

        due = self._next
        while due < len(self._records) and self._records[due]["captured_at_unix_s"] <= target:
            due += 1
        if due == self._next or self._capture is None:
            return False
        # Frames are stored in order; skip the ones that are already in the past.
        for _ in range(due - self._next - 1):
            self._capture.grab()
        ok, image = self._capture.read()
        self._next = due
        if not ok:
            return False

        record = dict(self._records[due - 1])
        record["recorded_provenance"] = self._original.value
        record["provenance"] = Provenance.REPLAYED.value
        record["source"] = f"recording:{self.session}"
        ids = {t["track_id"] for t in record["tracks"]}
        if self.selected_track_id not in ids:
            self.selected_track_id = None
        record["selected_track_id"] = self.selected_track_id
        self.latest = record
        self.frames_shown += 1
        if self._subscribers:
            jpeg = encode_jpeg(image)
            for queue in list(self._subscribers):
                if queue.full():
                    with contextlib.suppress(asyncio.QueueEmpty):
                        queue.get_nowait()
                queue.put_nowait((record, jpeg))
        return True

    def status(self) -> dict[str, Any]:
        latest = self.latest
        return {
            "enabled": True,
            "detector": self.detector.name,
            "camera_source": f"recording:{self.session}",
            "provenance": Provenance.REPLAYED.value,
            "frames_processed": self.frames_shown,
            "fps": None,
            "inference_latency_ms_p50": latest["inference_latency_ms"] if latest else None,
            "inference_latency_ms_p95": None,
            "last_frame_age_s": None,
            "tracks": len(latest["tracks"]) if latest else 0,
            "selected_track_id": self.selected_track_id,
            "faults": [],
            "last_error": None,
            "recording": None,
            "recorded_frames": len(self._records),
        }
