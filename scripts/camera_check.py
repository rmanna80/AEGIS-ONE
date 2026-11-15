#!/usr/bin/env python3
"""Check a USB webcam and measure AEGIS's detector on its live frames.

    .venv/bin/python scripts/camera_check.py                  # first USB camera found
    .venv/bin/python scripts/camera_check.py --device /dev/video36 --seconds 30

Run it on the computer the webcam is plugged into (the Raspberry Pi). Stop the
backend first if it is using the camera: only one program can open it at a time.

Two parts:

  1. Camera only: what format it agreed to, how many frames per second it really
     delivers, and the delay from the driver to AEGIS when that can be measured.
  2. Camera + detector (YOLO11n, ONNX Runtime, CPU): frames processed per second,
     detection time per frame, frames the detector had no time for, and how many
     objects of each class were detected.

Nothing is sent anywhere and no image is saved. --snapshot saves one picture locally,
so you can check what the camera sees; leave it off if the camera faces people.

Results are saved under data/generated/camera_checks/. The camera is a physical
sensor on a bench, so these are real hardware measurements of the camera and this
computer, not of anything mounted on a vehicle.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import statistics
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))

from aegis.device import (  # noqa: E402
    decode_throttled,  # noqa: E402
    read_model,
    read_temperature_c,
    read_throttled,
)
from aegis.perception.cameras import VideoFileCameraAdapter  # noqa: E402
from aegis.perception.pipeline import PerceptionPipeline  # noqa: E402
from aegis.perception.usb_camera import UsbCameraAdapter, find_usb_cameras  # noqa: E402


def percentile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(q * len(ordered)))]


async def camera_only(camera: Any, seconds: float) -> dict[str, Any]:
    intervals: list[float] = []
    last: float | None = None
    started = time.monotonic()
    frames = 0
    while time.monotonic() - started < seconds:
        frame = await camera.read_frame()
        if frame is None:
            continue
        frames += 1
        now = time.monotonic()
        if last is not None:
            intervals.append((now - last) * 1000.0)
        last = now
    elapsed = time.monotonic() - started
    result: dict[str, Any] = {
        "seconds": round(elapsed, 1),
        "frames_received": frames,
        "frames_per_second": round(frames / elapsed, 2) if elapsed else None,
    }
    if intervals:
        result["frame_interval_ms"] = {
            "median": round(statistics.median(intervals), 1),
            "p95": round(percentile(intervals, 0.95), 1),
            "max": round(max(intervals), 1),
        }
    return result


async def with_detector(camera: Any, model: Path, seconds: float, threads: int) -> dict[str, Any]:
    from aegis.perception.yolo_onnx import OnnxYoloBackend

    detector = OnnxYoloBackend(model, threads=threads)
    pipeline = PerceptionPipeline(camera, detector)  # no recording
    labels: Counter[str] = Counter()
    latencies: list[float] = []
    started = time.monotonic()
    temperature_before = read_temperature_c()
    while time.monotonic() - started < seconds:
        frame = await camera.read_frame()
        if frame is None:
            continue
        result = await pipeline.process(frame)
        latencies.append(result.inference_latency_ms)
        labels.update(d.label for d in result.detections)
    elapsed = time.monotonic() - started
    out: dict[str, Any] = {
        "detector": detector.name,
        "threads": threads,
        "seconds": round(elapsed, 1),
        "frames_processed": len(latencies),
        "frames_processed_per_second": round(len(latencies) / elapsed, 2) if elapsed else None,
        "detections_by_class": dict(labels.most_common()),
        "temperature_c_before": temperature_before,
        "temperature_c_after": read_temperature_c(),
    }
    if latencies:
        out["detection_time_ms"] = {
            "median": round(statistics.median(latencies), 1),
            "p95": round(percentile(latencies, 0.95), 1),
            "max": round(max(latencies), 1),
        }
    return out


async def run(args: argparse.Namespace) -> dict[str, Any]:
    camera: Any
    if args.video:  # a dry run of this script on a file; not a camera measurement
        camera = VideoFileCameraAdapter(Path(args.video))
    else:
        spec = f"usb:{args.device}" if args.device else "usb"
        camera = UsbCameraAdapter(spec, args.width, args.height, args.fps)
    await camera.start()
    try:
        report: dict[str, Any] = {"camera_only": await camera_only(camera, args.seconds / 3)}
        if args.snapshot:
            frame = await camera.read_frame()
            if frame is not None:
                import cv2

                path = REPO_ROOT / "data/generated/camera_checks/snapshot.jpg"
                path.parent.mkdir(parents=True, exist_ok=True)
                cv2.imwrite(str(path), frame.image)
                report["snapshot"] = str(path.relative_to(REPO_ROOT))
        model = Path(args.model)
        if args.model_found:
            report["camera_and_detector"] = await with_detector(
                camera, model, args.seconds * 2 / 3, args.threads
            )
        else:
            report["camera_and_detector"] = {
                "available": False,
                "reason": f"no detector model at {model} (bash scripts/get_model.sh)",
            }
        details = getattr(camera, "details", None)
        report["camera"] = details() if callable(details) else {"source": camera.source}
        report["provenance"] = camera.provenance.value
        return report
    finally:
        await camera.stop()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--device", help="e.g. /dev/video36 (default: first USB camera)")
    parser.add_argument("--seconds", type=float, default=30.0)
    parser.add_argument("--width", type=int, default=640)
    parser.add_argument("--height", type=int, default=480)
    parser.add_argument("--fps", type=float, default=30.0)
    parser.add_argument("--threads", type=int, default=max(1, min(4, (os.cpu_count() or 2))))
    parser.add_argument("--model", default=str(REPO_ROOT / "models/yolo11n.onnx"))
    parser.add_argument("--snapshot", action="store_true", help="save one picture locally")
    parser.add_argument("--video", help=argparse.SUPPRESS)
    args = parser.parse_args()
    args.model_found = Path(args.model).is_file()

    cameras = find_usb_cameras()
    if not args.video:
        print(
            "USB cameras found:",
            ", ".join(f"{c['device']} ({c['name']})" for c in cameras) or "none",
        )
        if not cameras and not args.device:
            print("No USB camera. Plug it in, then check: v4l2-ctl --list-devices")
            return 2
    try:
        report = asyncio.run(run(args))
    except RuntimeError as exc:
        print(f"Could not use the camera: {exc}")
        return 2

    throttled = read_throttled()
    document = {
        "schema": "aegis.camera-check/1",
        "measured_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "dry_run_on_a_video_file": bool(args.video),
        "host": {
            "model": read_model(),
            "machine": platform.machine(),
            "cpu_count": os.cpu_count(),
            "python": platform.python_version(),
        },
        "usb_cameras_found": cameras,
        "throttling_flags_after": decode_throttled(throttled) if throttled is not None else None,
        **report,
    }
    folder = REPO_ROOT / "data/generated/camera_checks"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"camera_{time.strftime('%Y%m%dT%H%M%S')}.json"
    path.write_text(json.dumps(document, indent=2))

    cam, det = report["camera_only"], report["camera_and_detector"]
    info = report["camera"]
    print(
        f"\nData: {report['provenance']}"
        + ("  (DRY RUN on a video file, not a camera measurement)" if args.video else "")
    )
    if "negotiated" in info:
        print(f"Camera agreed to: {info['negotiated']}  (asked for {info['requested']})")
    print(
        f"Camera alone: {cam['frames_per_second']} frames/s over {cam['seconds']} s",
        f"(interval median {cam.get('frame_interval_ms', {}).get('median')} ms,"
        f" max {cam.get('frame_interval_ms', {}).get('max')} ms)",
    )
    delay = info.get("driver_to_aegis_delay")
    if delay:
        print(
            "Driver to AEGIS delay:",
            f"median {delay['median_ms']} ms, p95 {delay['p95_ms']} ms"
            if delay["available"]
            else f"not available ({delay['reason']})",
        )
    if det.get("frames_processed"):
        t = det["detection_time_ms"]
        print(
            f"With the detector: {det['frames_processed_per_second']} frames/s processed;"
            f" detection {t['median']} ms median, {t['p95']} ms p95, {t['max']} ms max"
        )
        if "frames_dropped_before_processing" in info:
            print(
                f"Frames the detector had no time for: {info['frames_dropped_before_processing']}"
                f" of {info['frames_delivered']}"
            )
        print("Detected:", det["detections_by_class"] or "nothing")
        print(
            f"Temperature: {det['temperature_c_before']} -> {det['temperature_c_after']} degC;"
            f" throttling: {document['throttling_flags_after'] or 'none'}"
        )
    else:
        print("Detector:", det.get("reason", "no frames processed"))
    print(f"\nsaved: {path.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
