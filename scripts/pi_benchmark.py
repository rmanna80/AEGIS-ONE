#!/usr/bin/env python3
"""Can this computer run AEGIS's processing fast enough? (Raspberry Pi or any Linux)

    .venv/bin/python scripts/pi_benchmark.py

Measures the three workloads a companion computer carries, on this machine, with no
vehicle and no simulator involved:

  * object detection (YOLO11n, ONNX Runtime, CPU) on a 640x480 frame;
  * the thermal pipeline (render one frame, find heat sources);
  * depth mapping (fuse one 320x240 depth frame into the occupancy map).

It reports the median and worst-case time per frame against what a live flight needs,
and the processor temperature before and after. Results are saved under
data/generated/benchmarks/.
"""

from __future__ import annotations

import json
import math
import platform
import statistics
import sys
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))

import numpy as np  # noqa: E402

from aegis.contracts import Provenance  # noqa: E402
from aegis.device import DeviceMonitor  # noqa: E402
from aegis.navigation.depth import DepthFrame, Pose, RaycastDepthCamera  # noqa: E402
from aegis.navigation.geometry import SceneGeometry  # noqa: E402
from aegis.navigation.occupancy import OccupancyGrid  # noqa: E402
from aegis.navigation.service import grid_spec_for  # noqa: E402
from aegis.sar.detect import detect_heat_sources  # noqa: E402
from aegis.sar.thermal import ThermalCameraModel, ThermalFrame  # noqa: E402

# What a live flight needs: the detector alert fires above 500 ms; thermal frames
# arrive at 4 Hz and depth frames are fused at up to 5 Hz.
BUDGETS_MS = {"object_detection": 500.0, "thermal_pipeline": 250.0, "depth_mapping": 200.0}


def timed(function: Any, repeats: int) -> list[float]:
    function()  # warm-up: first calls pay one-off costs
    times = []
    for _ in range(repeats):
        started = time.perf_counter()
        function()
        times.append((time.perf_counter() - started) * 1000.0)
    return times


def summarise(times: list[float]) -> dict[str, float]:
    ordered = sorted(times)
    return {
        "median_ms": round(statistics.median(ordered), 1),
        "p95_ms": round(ordered[min(len(ordered) - 1, int(0.95 * len(ordered)))], 1),
        "worst_ms": round(ordered[-1], 1),
        "frames_per_second": round(1000.0 / statistics.median(ordered), 1),
    }


def main() -> int:
    device = DeviceMonitor(REPO_ROOT / "data")
    before = device.refresh()
    print(
        f"computer: {before['model'] or platform.node()} ({before['machine']}, "
        f"{before['cpu_count']} cores), Python {platform.python_version()}"
    )
    print(
        f"temperature before: {before['temperature_c']} C   firmware flags: "
        f"{before['throttled'] if before['throttled'] is not None else 'not a Raspberry Pi'}"
    )
    results: dict[str, Any] = {}

    model = REPO_ROOT / "models" / "yolo11n.onnx"
    if model.is_file():
        from aegis.perception.yolo_onnx import OnnxYoloBackend

        detector = OnnxYoloBackend(model)
        frame = np.random.default_rng(0).integers(0, 255, (480, 640, 3), dtype=np.uint8)
        results["object_detection"] = summarise(timed(lambda: detector.infer(frame), 30))
    else:
        print("object detection: skipped (run scripts/get_detector_model.sh first)")

    scenario = json.loads((REPO_ROOT / "simulation/scenarios/sar_field.json").read_text())
    thermal = ThermalCameraModel(scenario, None, seed=0)
    pose = Pose((62.0, -12.0, 25.0), (1.0, 0.0, 0.0, 0.0))

    def thermal_step() -> None:
        image = thermal.render(pose)
        detect_heat_sources(
            ThermalFrame(image, thermal.hfov, 1, "bench", Provenance.SIMULATED, "bench", 0.0, pose)
        )

    results["thermal_pipeline"] = summarise(timed(thermal_step, 40))

    urban = json.loads((REPO_ROOT / "simulation/scenarios/urban.json").read_text())
    camera = RaycastDepthCamera(SceneGeometry(urban), None, width=320, height=240, seed=0)
    grid = OccupancyGrid(grid_spec_for(urban, 0.5))
    half = math.radians(0.0) / 2
    depth_pose = Pose((0.0, 20.0, 6.0), (math.cos(half), 0.0, 0.0, math.sin(half)))
    depth = DepthFrame(
        camera.render(depth_pose),
        camera.hfov,
        camera.max_range_m,
        1,
        "bench",
        Provenance.SIMULATED,
        "bench",
        0.0,
    )
    results["depth_mapping"] = summarise(timed(lambda: grid.integrate(depth, depth_pose), 30))

    after = device.refresh()
    print(f"\n{'workload':<18} {'median':>9} {'p95':>9} {'worst':>9} {'needs':>9}  verdict")
    verdicts = {}
    for name, summary in results.items():
        ok = summary["p95_ms"] <= BUDGETS_MS[name]
        verdicts[name] = ok
        print(
            f"{name:<18} {summary['median_ms']:>7.1f}ms {summary['p95_ms']:>7.1f}ms "
            f"{summary['worst_ms']:>7.1f}ms {BUDGETS_MS[name]:>7.0f}ms  "
            f"{'fast enough' if ok else 'TOO SLOW'}"
        )
    print(
        f"\ntemperature after: {after['temperature_c']} C   firmware flags: "
        f"{after['throttled'] if after['throttled'] is not None else 'not a Raspberry Pi'}"
    )
    for code, _, message in device.findings():
        print(f"  {code}: {message}")
    print(
        "These workloads were timed one at a time. A flight runs them together with the "
        "backend, so leave headroom."
    )

    out_dir = REPO_ROOT / "data" / "generated" / "benchmarks"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"benchmark_{time.strftime('%Y%m%dT%H%M%S')}.json"
    out.write_text(
        json.dumps(
            {
                "device_before": before,
                "device_after": after,
                "python": platform.python_version(),
                "results": results,
                "budgets_ms": BUDGETS_MS,
                "fast_enough": verdicts,
            },
            indent=2,
        )
    )
    print(f"saved: {out.relative_to(REPO_ROOT)}")
    return 0 if all(verdicts.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
