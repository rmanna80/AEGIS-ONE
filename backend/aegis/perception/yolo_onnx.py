"""ONNX Runtime backend for Ultralytics-export-compatible YOLO detection models.

Expects the standard Ultralytics detection export: input ``(1, 3, H, W)`` float
RGB in [0, 1]; output ``(1, 4 + num_classes, N)`` with ``cx, cy, w, h`` in input
pixels followed by per-class scores (YOLOv8 / YOLO11 layout, no objectness).
Runs on CPU by default, so no GPU is required.
"""

from __future__ import annotations

import ast
from pathlib import Path

import cv2
import numpy as np
import onnxruntime as ort
from numpy.typing import NDArray

from aegis.perception.types import Detection, Image

Floats = NDArray[np.float32]


def letterbox(image: Image, size: int) -> tuple[Floats, float, tuple[float, float]]:
    """Resize keeping aspect ratio and pad to ``size x size``.

    Returns the network tensor, the scale applied and the (x, y) padding, which
    :func:`decode` needs to map boxes back to the original image.
    """
    height, width = image.shape[:2]
    scale = min(size / height, size / width)
    new_w, new_h = round(width * scale), round(height * scale)
    resized = cv2.resize(image, (new_w, new_h), interpolation=cv2.INTER_LINEAR)
    pad_x, pad_y = (size - new_w) / 2.0, (size - new_h) / 2.0
    canvas = np.full((size, size, 3), 114, dtype=np.uint8)
    top, left = round(pad_y - 0.1), round(pad_x - 0.1)
    canvas[top : top + new_h, left : left + new_w] = resized
    tensor = canvas[:, :, ::-1].transpose(2, 0, 1).astype(np.float32) / 255.0
    return np.ascontiguousarray(tensor[None]), scale, (float(left), float(top))


def iou_matrix(a: Floats, b: Floats) -> Floats:
    """Pairwise IoU of two ``(n, 4)`` / ``(m, 4)`` xyxy arrays -> ``(n, m)``."""
    if len(a) == 0 or len(b) == 0:
        return np.zeros((len(a), len(b)), dtype=np.float32)
    x1 = np.maximum(a[:, None, 0], b[None, :, 0])
    y1 = np.maximum(a[:, None, 1], b[None, :, 1])
    x2 = np.minimum(a[:, None, 2], b[None, :, 2])
    y2 = np.minimum(a[:, None, 3], b[None, :, 3])
    inter = np.clip(x2 - x1, 0, None) * np.clip(y2 - y1, 0, None)
    area_a = (a[:, 2] - a[:, 0]) * (a[:, 3] - a[:, 1])
    area_b = (b[:, 2] - b[:, 0]) * (b[:, 3] - b[:, 1])
    union = area_a[:, None] + area_b[None, :] - inter
    iou: Floats = (inter / np.maximum(union, 1e-9)).astype(np.float32)
    return iou


def nms(boxes: Floats, scores: Floats, iou_threshold: float) -> list[int]:
    """Greedy non-maximum suppression; returns kept indices, best first."""
    order = np.argsort(-scores)
    keep: list[int] = []
    while order.size:
        best = int(order[0])
        keep.append(best)
        if order.size == 1:
            break
        overlaps = iou_matrix(boxes[best : best + 1], boxes[order[1:]])[0]
        order = order[1:][overlaps <= iou_threshold]
    return keep


def decode(
    output: Floats,
    scale: float,
    pad: tuple[float, float],
    image_size: tuple[int, int],
    labels: dict[int, str],
    confidence_threshold: float,
    iou_threshold: float,
    allowed_class_ids: frozenset[int] | None = None,
    max_detections: int = 100,
) -> list[Detection]:
    """Turn the raw ``(4 + nc, N)`` network output into detections in image pixels."""
    predictions = output.T  # (N, 4 + nc)
    class_scores = predictions[:, 4:]
    class_ids = class_scores.argmax(axis=1)
    confidences = class_scores[np.arange(len(class_ids)), class_ids]
    mask = confidences >= confidence_threshold
    if allowed_class_ids is not None:
        mask &= np.isin(class_ids, list(allowed_class_ids))
    if not mask.any():
        return []
    cxcywh, class_ids, confidences = predictions[mask, :4], class_ids[mask], confidences[mask]

    width, height = image_size
    half = cxcywh[:, 2:4] / 2.0
    boxes = np.concatenate([cxcywh[:, :2] - half, cxcywh[:, :2] + half], axis=1)
    boxes[:, [0, 2]] = np.clip((boxes[:, [0, 2]] - pad[0]) / scale, 0, width)
    boxes[:, [1, 3]] = np.clip((boxes[:, [1, 3]] - pad[1]) / scale, 0, height)

    detections: list[Detection] = []
    for class_id in np.unique(class_ids):  # class-wise NMS
        idx = np.flatnonzero(class_ids == class_id)
        for k in nms(boxes[idx], confidences[idx], iou_threshold):
            i = idx[k]
            x1, y1, x2, y2 = (float(v) for v in boxes[i])
            if x2 - x1 < 1.0 or y2 - y1 < 1.0:
                continue
            detections.append(
                Detection(
                    label=labels.get(int(class_id), str(int(class_id))),
                    class_id=int(class_id),
                    confidence=float(min(1.0, confidences[i])),
                    bbox_xyxy_px=(x1, y1, x2, y2),
                )
            )
    detections.sort(key=lambda d: -d.confidence)
    return detections[:max_detections]


class OnnxYoloBackend:
    def __init__(
        self,
        model_path: Path,
        confidence_threshold: float = 0.35,
        iou_threshold: float = 0.5,
        allowed_labels: list[str] | None = None,
        input_size: int = 640,
        threads: int | None = None,
    ) -> None:
        if not model_path.is_file():
            raise FileNotFoundError(f"Detector model not found: {model_path}")
        options = ort.SessionOptions()
        if threads:
            options.intra_op_num_threads = threads
        self._session = ort.InferenceSession(
            str(model_path), sess_options=options, providers=["CPUExecutionProvider"]
        )
        self._input = self._session.get_inputs()[0].name
        meta = self._session.get_modelmeta().custom_metadata_map
        self.labels: dict[int, str] = (
            {int(k): str(v) for k, v in ast.literal_eval(meta["names"]).items()}
            if "names" in meta
            else {}
        )
        shape = self._session.get_inputs()[0].shape
        fixed = shape[2] if isinstance(shape[2], int) else None
        self.input_size = fixed or input_size
        self.confidence_threshold = confidence_threshold
        self.iou_threshold = iou_threshold
        self._allowed: frozenset[int] | None = None
        if allowed_labels:
            unknown = sorted(set(allowed_labels) - set(self.labels.values()))
            if unknown:
                raise ValueError(f"Labels not in the model: {unknown}")
            self._allowed = frozenset(i for i, n in self.labels.items() if n in allowed_labels)
        self.name = f"onnx:{model_path.name}"
        self.license = meta.get("license", "unknown")

    def infer(self, image: Image) -> list[Detection]:
        tensor, scale, pad = letterbox(image, self.input_size)
        output = self._session.run(None, {self._input: tensor})[0][0]
        return decode(
            output,
            scale,
            pad,
            (image.shape[1], image.shape[0]),
            self.labels,
            self.confidence_threshold,
            self.iou_threshold,
            self._allowed,
        )
