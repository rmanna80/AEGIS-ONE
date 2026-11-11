"""Detector pre/post-processing and metrics, with synthetic network outputs."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

pytest.importorskip("onnxruntime")

import numpy as np  # noqa: E402

from aegis.perception.metrics import DetectionScore, TrackingScore, match_boxes  # noqa: E402
from aegis.perception.yolo_onnx import (  # noqa: E402
    OnnxYoloBackend,
    decode,
    iou_matrix,
    letterbox,
    nms,
)

LABELS = {0: "person", 1: "bicycle", 2: "car"}
MODEL = Path(__file__).resolve().parents[2] / "models" / "yolo11n.onnx"


def raw_output(rows: list[tuple[float, float, float, float, int, float]]) -> np.ndarray:
    """Build a (4 + 3, N) tensor from (cx, cy, w, h, class_id, score) rows."""
    out = np.zeros((7, len(rows)), dtype=np.float32)
    for i, (cx, cy, w, h, class_id, score) in enumerate(rows):
        out[:4, i] = (cx, cy, w, h)
        out[4 + class_id, i] = score
    return out


def test_iou_matrix_known_values() -> None:
    a = np.array([[0, 0, 10, 10]], dtype=np.float32)
    b = np.array([[0, 0, 10, 10], [5, 0, 15, 10], [20, 20, 30, 30]], dtype=np.float32)
    assert iou_matrix(a, b)[0] == pytest.approx([1.0, 1 / 3, 0.0])
    assert iou_matrix(a, np.zeros((0, 4), dtype=np.float32)).shape == (1, 0)


def test_nms_keeps_the_best_of_overlapping_boxes() -> None:
    boxes = np.array([[0, 0, 10, 10], [1, 0, 11, 10], [50, 50, 60, 60]], dtype=np.float32)
    scores = np.array([0.6, 0.9, 0.5], dtype=np.float32)
    assert nms(boxes, scores, iou_threshold=0.5) == [1, 2]


def test_letterbox_geometry_for_a_wide_image() -> None:
    image = np.zeros((480, 1280, 3), dtype=np.uint8)
    tensor, scale, pad = letterbox(image, 640)
    assert tensor.shape == (1, 3, 640, 640) and tensor.dtype == np.float32
    assert scale == pytest.approx(0.5)
    assert pad == (0.0, 200.0)  # 240 rows of image centred in 640
    assert tensor[0, :, 0, 0] == pytest.approx(114 / 255)  # padding colour
    assert tensor[0, :, 320, 320] == pytest.approx(0.0)  # image content


def test_decode_maps_boxes_back_to_original_image_pixels() -> None:
    # A 1280x480 image letterboxed to 640: scale 0.5, 200 px top padding.
    # An object at (400..600, 100..300) in the image is at cx=250, cy=300, 100x100 in the tensor.
    output = raw_output([(250, 300, 100, 100, 2, 0.8)])
    (d,) = decode(output, 0.5, (0.0, 200.0), (1280, 480), LABELS, 0.25, 0.5)
    assert d.label == "car" and d.class_id == 2
    assert d.confidence == pytest.approx(0.8)
    assert d.bbox_xyxy_px == pytest.approx((400, 100, 600, 300))


def test_decode_applies_threshold_nms_class_filter_and_clipping() -> None:
    output = raw_output(
        [
            (100, 100, 50, 50, 0, 0.9),
            (102, 100, 50, 50, 0, 0.6),  # duplicate of the first -> suppressed
            (102, 100, 50, 50, 2, 0.7),  # same place, other class -> kept (class-wise NMS)
            (300, 300, 40, 40, 0, 0.1),  # below threshold
            (635, 100, 50, 50, 1, 0.5),  # extends past the right edge -> clipped
        ]
    )
    detections = decode(output, 1.0, (0.0, 0.0), (640, 640), LABELS, 0.25, 0.5)
    assert [(d.label, round(d.confidence, 2)) for d in detections] == [
        ("person", 0.9),
        ("car", 0.7),
        ("bicycle", 0.5),
    ]
    assert detections[2].bbox_xyxy_px[2] == 640.0

    only_people = decode(
        output, 1.0, (0.0, 0.0), (640, 640), LABELS, 0.25, 0.5, allowed_class_ids=frozenset({0})
    )
    assert [d.label for d in only_people] == ["person"]
    assert decode(output, 1.0, (0.0, 0.0), (640, 640), LABELS, 0.95, 0.5) == []


def test_detection_score_precision_and_recall() -> None:
    score = DetectionScore()
    truth = [
        (0, (0.0, 0.0, 10.0, 10.0)),
        (0, (50.0, 50.0, 60.0, 60.0)),
        (2, (80.0, 0.0, 90.0, 10.0)),
    ]
    predicted = [
        (0, (1.0, 0.0, 11.0, 10.0)),  # hit
        (2, (50.0, 50.0, 60.0, 60.0)),  # right place, wrong class -> false positive
        (0, (200.0, 200.0, 210.0, 210.0)),  # nothing there -> false positive
    ]
    score.add_frame(predicted, truth)
    assert (score.true_positives, score.false_positives, score.false_negatives) == (1, 2, 2)
    assert score.precision == pytest.approx(1 / 3)
    assert score.recall == pytest.approx(1 / 3)
    assert match_boxes([], truth) == []


def test_tracking_score_counts_id_switches_and_misses() -> None:
    score = TrackingScore()
    box_a, box_b = (0.0, 0.0, 10.0, 10.0), (50.0, 50.0, 60.0, 60.0)
    score.add_frame([(1, box_a), (2, box_b)], [(100, box_a), (200, box_b)])
    score.add_frame([(1, box_a)], [(100, box_a), (200, box_b)])  # object 200 missed
    score.add_frame([(1, box_a), (7, box_b)], [(100, box_a), (200, box_b)])  # 200 now id 7
    assert score.id_switches == 1
    assert score.misses == 1
    assert score.lost_track_rate == pytest.approx(1 / 6)
    assert score.mota == pytest.approx(1 - 2 / 6)


@pytest.mark.skipif(not MODEL.is_file(), reason="run scripts/get_detector_model.sh first")
def test_real_model_loads_and_handles_an_empty_scene() -> None:
    backend = OnnxYoloBackend(MODEL, confidence_threshold=0.5)
    assert backend.labels[0] == "person" and len(backend.labels) == 80
    assert backend.infer(np.full((480, 640, 3), 114, dtype=np.uint8)) == []
    with pytest.raises(ValueError, match="not in the model"):
        OnnxYoloBackend(MODEL, allowed_labels=["dragon"])


@pytest.mark.skipif(
    not MODEL.is_file() or not os.environ.get("AEGIS_COCO8_DIR"),
    reason="needs the model and AEGIS_COCO8_DIR (see scripts/eval_detector.py)",
)
def test_real_model_finds_labelled_objects_in_a_real_photo() -> None:
    import cv2

    root = Path(os.environ["AEGIS_COCO8_DIR"])
    image = cv2.imread(str(root / "images" / "val" / "000000000049.jpg"))
    labels = {d.label for d in OnnxYoloBackend(MODEL).infer(image)}
    assert {"horse", "person"} <= labels
