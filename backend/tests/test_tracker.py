"""Tracker behaviour on synthetic scenes with known ground-truth identities."""

from __future__ import annotations

import pytest

pytest.importorskip("scipy")

import numpy as np  # noqa: E402

from aegis.perception.metrics import TrackingScore  # noqa: E402
from aegis.perception.tracker import MultiObjectTracker, TrackerConfig  # noqa: E402
from aegis.perception.types import BBox, Detection, TrackState  # noqa: E402


def det(box: BBox, label: str = "person", class_id: int = 0, conf: float = 0.9) -> Detection:
    return Detection(label=label, class_id=class_id, confidence=conf, bbox_xyxy_px=box)


def box_at(cx: float, cy: float, w: float = 40.0, h: float = 80.0) -> BBox:
    return (cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2)


def test_track_is_confirmed_only_after_min_hits() -> None:
    tracker = MultiObjectTracker(TrackerConfig(min_hits=3))
    assert tracker.update([det(box_at(100, 100))]) == []
    assert tracker.update([det(box_at(102, 100))]) == []
    tracks = tracker.update([det(box_at(104, 100))])
    assert len(tracks) == 1 and tracks[0].state is TrackState.CONFIRMED
    assert tracks[0].track_id == 1


def test_single_frame_false_positive_never_becomes_a_track() -> None:
    tracker = MultiObjectTracker()
    tracker.update([det(box_at(300, 300))])
    for _ in range(10):
        assert tracker.update([]) == []


def test_velocity_estimate_is_image_space_and_converges() -> None:
    tracker = MultiObjectTracker()
    tracks = []
    for i in range(30):
        tracks = tracker.update([det(box_at(100 + 4 * i, 200 - 2 * i))], fps=30.0)
    vx, vy = tracks[0].velocity_px_s
    assert vx == pytest.approx(120.0, rel=0.1)  # 4 px/frame * 30 fps
    assert vy == pytest.approx(-60.0, rel=0.1)
    assert len(tracks[0].history_px) == 30


def test_identity_survives_a_temporary_occlusion() -> None:
    """Simulation test 6 from the brief: tracking under temporary occlusion."""
    tracker = MultiObjectTracker(TrackerConfig(max_coast_frames=30))
    track_id = None
    for i in range(20):
        tracks = tracker.update([det(box_at(100 + 5 * i, 300))])
        if tracks:
            track_id = tracks[0].track_id
    assert track_id is not None

    for _ in range(15):  # hidden behind an obstacle, still moving
        tracks = tracker.update([])
        assert len(tracks) == 1 and tracks[0].state is TrackState.COASTING
        assert tracks[0].track_id == track_id

    tracks = tracker.update([det(box_at(100 + 5 * 35, 300))])  # reappears where expected
    assert [t.track_id for t in tracks] == [track_id]
    assert tracks[0].state is TrackState.CONFIRMED and tracks[0].frames_since_update == 0


def test_track_is_dropped_after_max_coast_frames() -> None:
    tracker = MultiObjectTracker(TrackerConfig(max_coast_frames=5))
    for i in range(5):
        tracker.update([det(box_at(100 + i, 100))])
    for _ in range(5):
        assert len(tracker.update([])) == 1
    assert tracker.update([]) == []
    # A new object in the same place gets a new identity.
    tracks = []
    for _ in range(3):
        tracks = tracker.update([det(box_at(105, 100))])
    assert tracks[0].track_id == 2


def test_detections_only_match_tracks_of_the_same_class() -> None:
    tracker = MultiObjectTracker(TrackerConfig(min_hits=1))
    first = tracker.update([det(box_at(100, 100), "person", 0)])
    second = tracker.update([det(box_at(100, 100), "car", 2)])
    ids = {t.track_id: t.label for t in second}
    assert first[0].track_id == 1
    assert ids == {1: "person", 2: "car"}


def test_two_nearby_objects_keep_separate_identities() -> None:
    tracker = MultiObjectTracker()
    seen: list[dict[int, float]] = []
    for i in range(40):
        a = det(box_at(100 + 3 * i, 200))
        b = det(box_at(100 + 3 * i, 300))  # walking in parallel, 100 px apart
        seen.append(
            {
                t.track_id: (t.bbox_xyxy_px[1] + t.bbox_xyxy_px[3]) / 2
                for t in tracker.update([b, a])
            }
        )
    final = seen[-1]
    assert len(final) == 2
    assert all(len(s) <= 2 for s in seen)
    top_id = min(final, key=lambda k: final[k])
    assert all(s[top_id] < 250 for s in seen if top_id in s)  # never swapped rows


def simulate(seed: int, miss_rate: float, noise_px: float, frames: int = 300) -> TrackingScore:
    """Three objects on straight paths; detections have jitter and random misses."""
    rng = np.random.default_rng(seed)
    starts = np.array([[80.0, 120.0], [600.0, 260.0], [200.0, 420.0]])
    velocities = np.array([[2.5, 0.3], [-2.0, 0.0], [1.0, -0.6]])
    tracker = MultiObjectTracker()
    score = TrackingScore()
    for f in range(frames):
        truth, detections = [], []
        for obj in range(3):
            cx, cy = starts[obj] + velocities[obj] * f
            truth.append((obj, box_at(cx, cy)))
            if rng.random() >= miss_rate:
                jx, jy = rng.normal(0.0, noise_px, 2)
                detections.append(det(box_at(cx + jx, cy + jy)))
        tracks = tracker.update(detections)
        if f >= 10:  # skip the confirmation warm-up
            score.add_frame([(t.track_id, t.bbox_xyxy_px) for t in tracks], truth)
    return score


def test_no_id_switches_with_clean_detections() -> None:
    score = simulate(seed=1, miss_rate=0.0, noise_px=0.0)
    assert score.id_switches == 0
    assert score.misses == 0 and score.false_positives == 0
    assert score.mota == pytest.approx(1.0)


def test_robust_to_jitter_and_missed_detections() -> None:
    """20 % missed detections and 3 px jitter: identities must hold through the gaps."""
    for seed in range(5):
        score = simulate(seed=seed, miss_rate=0.2, noise_px=3.0)
        assert score.id_switches == 0, f"seed {seed}"
        assert score.lost_track_rate < 0.02, f"seed {seed}: {score.lost_track_rate:.3f}"
