#!/usr/bin/env python3
"""Measure detector precision, recall and latency on the COCO8 sample set.

COCO8 is 8 labelled COCO images published by Ultralytics as a smoke-test
dataset. It is far too small for a benchmark: treat the numbers as a sanity
check that the whole decode path is right, not as model accuracy.

    python scripts/eval_detector.py                # downloads COCO8 to data/generated/
    python scripts/eval_detector.py --conf 0.25

Writes a JSON report to data/generated/eval/.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import time
import urllib.request
import zipfile
from pathlib import Path

import cv2
import numpy as np

from aegis.perception.metrics import DetectionScore
from aegis.perception.yolo_onnx import OnnxYoloBackend

REPO_ROOT = Path(__file__).resolve().parents[1]
COCO8_URL = "https://github.com/ultralytics/assets/releases/download/v0.0.0/coco8.zip"


def ensure_coco8(root: Path) -> Path:
    target = root / "coco8"
    if not target.is_dir():
        root.mkdir(parents=True, exist_ok=True)
        archive = root / "coco8.zip"
        print(f"Downloading {COCO8_URL}")
        urllib.request.urlretrieve(COCO8_URL, archive)
        with zipfile.ZipFile(archive) as z:
            z.extractall(root)
    return target


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--model", type=Path, default=REPO_ROOT / "models" / "yolo11n.onnx")
    parser.add_argument("--data", type=Path, default=REPO_ROOT / "data" / "generated" / "datasets")
    parser.add_argument("--conf", type=float, default=0.35)
    parser.add_argument("--iou", type=float, default=0.5, help="IoU for a true positive")
    parser.add_argument("--repeats", type=int, default=5, help="latency runs per image")
    args = parser.parse_args()

    dataset = ensure_coco8(args.data)
    backend = OnnxYoloBackend(args.model, confidence_threshold=args.conf)
    score = DetectionScore()
    latencies: list[float] = []
    images = sorted(dataset.glob("images/*/*.jpg"))
    backend.infer(cv2.imread(str(images[0])))  # warm-up, not timed

    for path in images:
        image = cv2.imread(str(path))
        height, width = image.shape[:2]
        truth = []
        label_file = dataset / "labels" / path.parent.name / f"{path.stem}.txt"
        for line in label_file.read_text().splitlines():
            class_id, cx, cy, w, h = line.split()
            x, y, bw, bh = (
                float(cx) * width,
                float(cy) * height,
                float(w) * width,
                float(h) * height,
            )
            truth.append((int(class_id), (x - bw / 2, y - bh / 2, x + bw / 2, y + bh / 2)))
        detections = []
        for _ in range(args.repeats):
            started = time.perf_counter()
            detections = backend.infer(image)
            latencies.append((time.perf_counter() - started) * 1000.0)
        score.add_frame([(d.class_id, d.bbox_xyxy_px) for d in detections], truth, args.iou)

    report = {
        "dataset": "coco8 (8 images; smoke test, not a benchmark)",
        "model": backend.name,
        "model_licence": backend.license,
        "confidence_threshold": args.conf,
        "iou_threshold": args.iou,
        "images": len(images),
        "ground_truth_objects": score.true_positives + score.false_negatives,
        "true_positives": score.true_positives,
        "false_positives": score.false_positives,
        "false_negatives": score.false_negatives,
        "precision": round(score.precision, 3),
        "recall": round(score.recall, 3),
        "latency_ms_median": round(float(np.median(latencies)), 1),
        "latency_ms_p95": round(float(np.percentile(latencies, 95)), 1),
        "fps_from_median": round(1000.0 / float(np.median(latencies)), 1),
        "execution_provider": "CPUExecutionProvider",
        "machine": f"{platform.processor() or platform.machine()}, {os.cpu_count()} logical CPUs",
        "measured_at_unix_s": time.time(),
    }
    out_dir = REPO_ROOT / "data" / "generated" / "eval"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"detector_{time.strftime('%Y%m%dT%H%M%S')}.json"
    out.write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))
    print(f"report: {out.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
