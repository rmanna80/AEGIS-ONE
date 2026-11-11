"""Multi-object tracker: constant-velocity Kalman filter per track plus
IoU-based Hungarian data association (SORT-style).

* State per track: ``[cx, cy, w, h, vx, vy, vw, vh]`` in pixels and pixels/frame.
* A track is ``tentative`` until it has been matched ``min_hits`` times.
* An unmatched confirmed track ``coasts`` on its prediction for up to
  ``max_coast_frames`` frames, which is what carries identities through short
  occlusions. After that it is dropped.
* Detections only match tracks of the same class.

The tracker reports image-space motion only and never commands anything.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray
from scipy.optimize import linear_sum_assignment

from aegis.perception.types import BBox, Detection, Track, TrackState
from aegis.perception.yolo_onnx import iou_matrix

Array = NDArray[np.float64]


def _xyxy_to_cxcywh(b: BBox) -> Array:
    return np.array([(b[0] + b[2]) / 2, (b[1] + b[3]) / 2, b[2] - b[0], b[3] - b[1]])


def _cxcywh_to_xyxy(s: Array) -> BBox:
    w, h = max(float(s[2]), 1.0), max(float(s[3]), 1.0)
    return (float(s[0]) - w / 2, float(s[1]) - h / 2, float(s[0]) + w / 2, float(s[1]) + h / 2)


@dataclass(frozen=True)
class TrackerConfig:
    min_hits: int = 3
    max_coast_frames: int = 30
    max_tentative_misses: int = 2
    iou_threshold: float = 0.2  # minimum IoU for a detection to match a track
    history_length: int = 40
    # Noise scales, relative to box size so behaviour is resolution independent.
    position_noise: float = 0.05
    velocity_noise: float = 0.02
    measurement_noise: float = 0.05


_F = np.eye(8)
_F[:4, 4:] = np.eye(4)  # x_{k+1} = x_k + v_k  (dt = 1 frame)
_H = np.eye(4, 8)


class _KalmanTrack:
    def __init__(self, track_id: int, det: Detection, cfg: TrackerConfig) -> None:
        self.id = track_id
        self.cfg = cfg
        self.label, self.class_id, self.confidence = det.label, det.class_id, det.confidence
        z = _xyxy_to_cxcywh(det.bbox_xyxy_px)
        self.x = np.concatenate([z, np.zeros(4)])
        size = max(z[2], z[3])
        self.P = np.diag([*(4 * [(2 * cfg.measurement_noise * size) ** 2]), *(4 * [size**2])])
        self.hits = 1
        self.age = 1
        self.misses = 0
        self.confirmed = cfg.min_hits <= 1
        self.history: deque[tuple[float, float]] = deque([(z[0], z[1])], maxlen=cfg.history_length)

    def predict(self) -> None:
        size = max(self.x[2], self.x[3], 1.0)
        q_pos, q_vel = (self.cfg.position_noise * size) ** 2, (self.cfg.velocity_noise * size) ** 2
        self.x = _F @ self.x
        self.P = _F @ self.P @ _F.T + np.diag([*(4 * [q_pos]), *(4 * [q_vel])])
        self.age += 1

    def update(self, det: Detection) -> None:
        z = _xyxy_to_cxcywh(det.bbox_xyxy_px)
        size = max(z[2], z[3], 1.0)
        r = np.eye(4) * (self.cfg.measurement_noise * size) ** 2
        s = _H @ self.P @ _H.T + r
        k = self.P @ _H.T @ np.linalg.inv(s)
        self.x = self.x + k @ (z - _H @ self.x)
        self.P = (np.eye(8) - k @ _H) @ self.P
        self.hits += 1
        self.misses = 0
        self.confidence = det.confidence
        if self.hits >= self.cfg.min_hits:
            self.confirmed = True
        self.history.append((float(self.x[0]), float(self.x[1])))

    @property
    def bbox(self) -> BBox:
        return _cxcywh_to_xyxy(self.x)

    def state(self) -> TrackState:
        if not self.confirmed:
            return TrackState.TENTATIVE
        return TrackState.COASTING if self.misses > 0 else TrackState.CONFIRMED

    def snapshot(self, fps: float) -> Track:
        return Track(
            track_id=self.id,
            label=self.label,
            class_id=self.class_id,
            state=self.state(),
            bbox_xyxy_px=self.bbox,
            confidence=self.confidence,
            velocity_px_s=(float(self.x[4]) * fps, float(self.x[5]) * fps),
            age_frames=self.age,
            frames_since_update=self.misses,
            history_px=tuple(self.history),
        )


class MultiObjectTracker:
    def __init__(self, config: TrackerConfig | None = None) -> None:
        self.config = config or TrackerConfig()
        self._tracks: list[_KalmanTrack] = []
        self._next_id = 1

    def reset(self) -> None:
        self._tracks.clear()

    def update(self, detections: list[Detection], fps: float = 30.0) -> list[Track]:
        """Advance one frame. Returns confirmed and coasting tracks (not tentative ones)."""
        cfg = self.config
        for track in self._tracks:
            track.predict()

        matches, unmatched_tracks, unmatched_dets = self._associate(detections)
        for t, d in matches:
            self._tracks[t].update(detections[d])
        for t in unmatched_tracks:
            self._tracks[t].misses += 1
        for d in unmatched_dets:
            self._tracks.append(_KalmanTrack(self._next_id, detections[d], cfg))
            self._next_id += 1

        self._tracks = [
            t
            for t in self._tracks
            if t.misses <= (cfg.max_coast_frames if t.confirmed else cfg.max_tentative_misses)
        ]
        return [t.snapshot(fps) for t in self._tracks if t.confirmed]

    def _associate(
        self, detections: list[Detection]
    ) -> tuple[list[tuple[int, int]], list[int], list[int]]:
        n_t, n_d = len(self._tracks), len(detections)
        if n_t == 0 or n_d == 0:
            return [], list(range(n_t)), list(range(n_d))
        track_boxes = np.array([t.bbox for t in self._tracks], dtype=np.float32)
        det_boxes = np.array([d.bbox_xyxy_px for d in detections], dtype=np.float32)
        iou = iou_matrix(track_boxes, det_boxes)
        same_class = np.array(
            [[t.class_id == d.class_id for d in detections] for t in self._tracks]
        )
        iou = np.where(same_class, iou, 0.0)

        rows, cols = linear_sum_assignment(-iou)
        matches = [
            (int(r), int(c))
            for r, c in zip(rows, cols, strict=True)
            if iou[r, c] >= self.config.iou_threshold
        ]
        matched_t = {m[0] for m in matches}
        matched_d = {m[1] for m in matches}
        return (
            matches,
            [t for t in range(n_t) if t not in matched_t],
            [d for d in range(n_d) if d not in matched_d],
        )
