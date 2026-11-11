"""Perception pipeline: camera -> detector -> tracker -> subscribers / recorder."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections import deque
from pathlib import Path
from typing import IO, Any

import cv2
import numpy as np

from aegis.perception.tracker import MultiObjectTracker
from aegis.perception.types import CameraFrame, CameraSource, InferenceBackend, PerceptionResult

log = logging.getLogger(__name__)

STALE_CAMERA_S = 2.0
SLOW_INFERENCE_MS = 500.0
PREVIEW_MAX_WIDTH = 960
PREVIEW_JPEG_QUALITY = 70

VideoMessage = tuple[dict[str, Any], bytes]  # (result JSON, preview JPEG)


class PerceptionPipeline:
    def __init__(
        self,
        camera: CameraSource,
        detector: InferenceBackend,
        tracker: MultiObjectTracker | None = None,
        record_path: Path | None = None,
    ) -> None:
        self.camera = camera
        self.detector = detector
        self.tracker = tracker or MultiObjectTracker()
        self.latest: PerceptionResult | None = None
        self.selected_track_id: int | None = None
        self._record_path = record_path
        self._record: IO[str] | None = None
        self._subscribers: set[asyncio.Queue[VideoMessage]] = set()
        self._task: asyncio.Task[None] | None = None
        self._latencies: deque[float] = deque(maxlen=200)
        self._frame_times: deque[float] = deque(maxlen=60)
        self._last_frame_at = 0.0
        self._last_source_time: float | None = None
        self.frames_processed = 0
        self.last_error: str | None = None

    async def start(self) -> None:
        await self.camera.start()
        if self._record_path is not None:
            self._record_path.parent.mkdir(parents=True, exist_ok=True)
            self._record = self._record_path.open("w", encoding="utf-8")
        self._task = asyncio.create_task(self._loop(), name="perception")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        await self.camera.stop()
        if self._record is not None:
            self._record.close()
            self._record = None

    def subscribe(self) -> asyncio.Queue[VideoMessage]:
        queue: asyncio.Queue[VideoMessage] = asyncio.Queue(maxsize=2)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[VideoMessage]) -> None:
        self._subscribers.discard(queue)

    def select_track(self, track_id: int | None) -> bool:
        """Operator selection: highlight only. It never changes vehicle behaviour."""
        if track_id is not None and (
            self.latest is None or track_id not in {t.track_id for t in self.latest.tracks}
        ):
            return False
        self.selected_track_id = track_id
        return True

    async def _loop(self) -> None:
        while True:
            try:
                frame = await self.camera.read_frame()
                if frame is None:
                    await asyncio.sleep(0.05)
                    continue
                await self.process(frame)
                self.last_error = None
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = repr(exc)
                log.exception("Perception frame failed")
                await asyncio.sleep(0.5)

    async def process(self, frame: CameraFrame) -> PerceptionResult:
        """Run one frame through detection and tracking (public for tests)."""
        # Source time running backwards means a new scene (a looping video, a
        # restarted simulation): old tracks must not be carried into it.
        if (
            frame.source_time_s is not None
            and self._last_source_time is not None
            and frame.source_time_s < self._last_source_time - 0.5
        ):
            self.tracker.reset()
        self._last_source_time = frame.source_time_s

        started = time.perf_counter()
        detections = await asyncio.to_thread(self.detector.infer, frame.image)
        latency_ms = (time.perf_counter() - started) * 1000.0

        now = time.monotonic()
        self._frame_times.append(now)
        tracks = self.tracker.update(detections, fps=self.fps or 30.0)
        if self.selected_track_id not in {t.track_id for t in tracks}:
            self.selected_track_id = None  # the selected object is gone

        result = PerceptionResult(
            frame_id=frame.frame_id,
            sensor_id=frame.sensor_id,
            provenance=frame.provenance,
            source=frame.source,
            captured_at_unix_s=frame.captured_at_unix_s,
            source_time_s=frame.source_time_s,
            width_px=frame.width,
            height_px=frame.height,
            detector=self.detector.name,
            inference_latency_ms=latency_ms,
            detections=tuple(detections),
            tracks=tuple(tracks),
            selected_track_id=self.selected_track_id,
        )
        self.latest = result
        self._latencies.append(latency_ms)
        self._last_frame_at = now
        self.frames_processed += 1

        payload = result.model_dump(mode="json")
        if self._record is not None:
            self._record.write(json.dumps(payload, separators=(",", ":")) + "\n")
        if self._subscribers:
            jpeg = await asyncio.to_thread(_preview_jpeg, frame)
            for queue in list(self._subscribers):
                if queue.full():
                    with contextlib.suppress(asyncio.QueueEmpty):
                        queue.get_nowait()
                queue.put_nowait((payload, jpeg))
        return result

    @property
    def fps(self) -> float | None:
        if len(self._frame_times) < 2:
            return None
        span = self._frame_times[-1] - self._frame_times[0]
        return (len(self._frame_times) - 1) / span if span > 0 else None

    def status(self) -> dict[str, Any]:
        latencies = np.array(self._latencies) if self._latencies else None
        age = time.monotonic() - self._last_frame_at if self._last_frame_at else None
        p95 = float(np.percentile(latencies, 95)) if latencies is not None else None
        faults: list[str] = []
        if age is None or age > STALE_CAMERA_S:
            faults.append("camera.no_frames")
        if p95 is not None and p95 > SLOW_INFERENCE_MS:
            faults.append("inference.slow")
        latest = self.latest
        return {
            "enabled": True,
            "detector": self.detector.name,
            "camera_source": latest.source if latest else getattr(self.camera, "source", None),
            "provenance": latest.provenance.value if latest else None,
            "frames_processed": self.frames_processed,
            "fps": self.fps,
            "inference_latency_ms_p50": float(np.median(latencies))
            if latencies is not None
            else None,
            "inference_latency_ms_p95": p95,
            "last_frame_age_s": age,
            "tracks": len(latest.tracks) if latest else 0,
            "selected_track_id": self.selected_track_id,
            "faults": faults,
            "last_error": self.last_error,
            "recording": str(self._record_path.name) if self._record_path else None,
        }


def _preview_jpeg(frame: CameraFrame) -> bytes:
    """Downscaled JPEG for the dashboard. Overlays are drawn client-side from the JSON."""
    image = frame.image
    if frame.width > PREVIEW_MAX_WIDTH:
        scale = PREVIEW_MAX_WIDTH / frame.width
        image = cv2.resize(image, (PREVIEW_MAX_WIDTH, round(frame.height * scale)))
    ok, encoded = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, PREVIEW_JPEG_QUALITY])
    if not ok:
        raise RuntimeError("JPEG encoding failed")
    return encoded.tobytes()
