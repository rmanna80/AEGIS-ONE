"""Evaluation metrics for detection and tracking (pure functions)."""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from scipy.optimize import linear_sum_assignment

from aegis.perception.types import BBox
from aegis.perception.yolo_onnx import iou_matrix


def match_boxes(
    predicted: list[tuple[int, BBox]],
    truth: list[tuple[int, BBox]],
    iou_threshold: float = 0.5,
) -> list[tuple[int, int]]:
    """Optimal one-to-one matching of ``(class_id, box)`` lists; classes must agree."""
    if not predicted or not truth:
        return []
    iou = iou_matrix(
        np.array([b for _, b in predicted], dtype=np.float32),
        np.array([b for _, b in truth], dtype=np.float32),
    )
    same = np.array([[p == t for t, _ in truth] for p, _ in predicted])
    iou = np.where(same, iou, 0.0)
    rows, cols = linear_sum_assignment(-iou)
    return [(int(r), int(c)) for r, c in zip(rows, cols, strict=True) if iou[r, c] >= iou_threshold]


@dataclass
class DetectionScore:
    true_positives: int = 0
    false_positives: int = 0
    false_negatives: int = 0

    def add_frame(
        self,
        predicted: list[tuple[int, BBox]],
        truth: list[tuple[int, BBox]],
        iou_threshold: float = 0.5,
    ) -> None:
        matched = len(match_boxes(predicted, truth, iou_threshold))
        self.true_positives += matched
        self.false_positives += len(predicted) - matched
        self.false_negatives += len(truth) - matched

    @property
    def precision(self) -> float:
        total = self.true_positives + self.false_positives
        return self.true_positives / total if total else 0.0

    @property
    def recall(self) -> float:
        total = self.true_positives + self.false_negatives
        return self.true_positives / total if total else 0.0


@dataclass
class TrackingScore:
    """CLEAR-MOT style bookkeeping against ground-truth identities.

    * **ID switch**: a ground-truth object is matched to a different track id
      than the last time it was matched.
    * **Miss**: a visible ground-truth object has no matching track in a frame.
    * **Lost-track rate**: misses / ground-truth object-frames.
    """

    id_switches: int = 0
    misses: int = 0
    false_positives: int = 0
    matches: int = 0
    truth_frames: int = 0
    _last_track_for: dict[int, int] = field(default_factory=dict)

    def add_frame(
        self,
        tracks: list[tuple[int, BBox]],  # (track_id, box)
        truth: list[tuple[int, BBox]],  # (object_id, box) for objects visible this frame
        iou_threshold: float = 0.5,
    ) -> None:
        pairs = match_boxes([(0, b) for _, b in tracks], [(0, b) for _, b in truth], iou_threshold)
        self.truth_frames += len(truth)
        self.matches += len(pairs)
        self.misses += len(truth) - len(pairs)
        self.false_positives += len(tracks) - len(pairs)
        for t, g in pairs:
            track_id, object_id = tracks[t][0], truth[g][0]
            previous = self._last_track_for.get(object_id)
            if previous is not None and previous != track_id:
                self.id_switches += 1
            self._last_track_for[object_id] = track_id

    @property
    def lost_track_rate(self) -> float:
        return self.misses / self.truth_frames if self.truth_frames else 0.0

    @property
    def mota(self) -> float:
        if not self.truth_frames:
            return 0.0
        errors = self.misses + self.false_positives + self.id_switches
        return 1.0 - errors / self.truth_frames
