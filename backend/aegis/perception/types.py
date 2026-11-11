"""Perception data contracts.

Image coordinates: pixels, origin at the top-left corner, x to the right and y
down. Boxes are ``(x1, y1, x2, y2)`` in the coordinates of the frame they were
detected in. Motion is **image-space only** (pixels per second); nothing here
is a 3D position or velocity.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from numpy.typing import NDArray
from pydantic import BaseModel, ConfigDict, Field

from aegis.contracts import Provenance

Image = NDArray[Any]  # H x W x 3 uint8, BGR (OpenCV order)
BBox = tuple[float, float, float, float]


@dataclass(frozen=True)
class CameraFrame:
    image: Image
    frame_id: int
    sensor_id: str
    provenance: Provenance
    source: str  # e.g. "video:walk.mp4", "stream:gazebo"
    captured_at_unix_s: float  # host wall clock when the frame was received/decoded
    source_time_s: float | None = None  # sensor/simulation/video time, when known

    @property
    def width(self) -> int:
        return int(self.image.shape[1])

    @property
    def height(self) -> int:
        return int(self.image.shape[0])


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Detection(_Model):
    label: str
    class_id: int
    confidence: float = Field(ge=0.0, le=1.0)
    bbox_xyxy_px: BBox


class TrackState(StrEnum):
    TENTATIVE = "tentative"  # seen, not yet confirmed by enough detections
    CONFIRMED = "confirmed"
    COASTING = "coasting"  # confirmed, currently unmatched (e.g. occluded); box is predicted


class Track(_Model):
    track_id: int
    label: str
    class_id: int
    state: TrackState
    bbox_xyxy_px: BBox  # filtered estimate (prediction while coasting)
    confidence: float  # confidence of the last matched detection
    velocity_px_s: tuple[float, float]  # image-space motion of the box centre
    age_frames: int
    frames_since_update: int
    history_px: tuple[tuple[float, float], ...]  # recent box centres, oldest first


class PerceptionResult(_Model):
    """Everything derived from one camera frame."""

    frame_id: int
    sensor_id: str
    provenance: Provenance
    source: str
    captured_at_unix_s: float
    source_time_s: float | None
    width_px: int
    height_px: int
    detector: str
    inference_latency_ms: float
    detections: tuple[Detection, ...]
    tracks: tuple[Track, ...]
    selected_track_id: int | None = None


@runtime_checkable
class CameraSource(Protocol):
    sensor_id: str

    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    async def read_frame(self) -> CameraFrame | None:
        """Next frame, or ``None`` if none arrived within the source's timeout."""
        ...


@runtime_checkable
class InferenceBackend(Protocol):
    """A detector. Swappable without touching the tracker, pipeline or UI."""

    name: str

    def infer(self, image: Image) -> list[Detection]:
        """Blocking; the pipeline calls it from a worker thread."""
        ...


def describe(result: PerceptionResult) -> dict[str, Any]:
    return result.model_dump(mode="json")
