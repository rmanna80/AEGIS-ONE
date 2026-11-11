#!/usr/bin/env python3
"""Explain a saved occupancy map's errors against the scenario geometry.

    .venv/bin/python scripts/analyze_map.py                      # newest session map, urban
    .venv/bin/python scripts/analyze_map.py path/to/map.npz --scenario urban

It answers one question: is the map wrong by a constant offset (camera mounting,
frame/pose latency, a shifted world origin - all fixable by calibration), or is
the error unstructured (pose-estimate noise, drift)? It prints

* the scores as saved, and the scores after the best rigid shift of the map;
* where the false positives are: by height, and by distance from true geometry.

It reads files only. The map is whatever the run recorded (simulated data).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
from scipy.ndimage import distance_transform_edt

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))

from aegis.navigation.geometry import SceneGeometry  # noqa: E402
from aegis.navigation.occupancy import OccupancyGrid  # noqa: E402


def shifted(grid: OccupancyGrid, cells: tuple[int, int, int]) -> OccupancyGrid:
    """A copy of ``grid`` moved by whole voxels (space shifted in from outside is unobserved)."""
    out = OccupancyGrid(grid.spec)
    src = [slice(None)] * 3
    dst = [slice(None)] * 3
    for axis, d in enumerate(cells):
        n = grid.log_odds.shape[axis]
        if d >= 0:
            src[axis], dst[axis] = slice(0, n - d), slice(d, n)
        else:
            src[axis], dst[axis] = slice(-d, n), slice(0, n + d)
    out.log_odds[tuple(dst)] = grid.log_odds[tuple(src)]
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("map", nargs="?", help="map.npz (default: newest session map)")
    parser.add_argument("--scenario", default="urban")
    parser.add_argument("--search-m", type=float, default=3.0, help="horizontal shift search")
    args = parser.parse_args()

    if args.map:
        path = Path(args.map)
    else:
        maps = sorted((REPO_ROOT / "data/generated/sessions").glob("*/map.npz"))
        if not maps:
            sys.exit("No saved map found under data/generated/sessions.")
        path = maps[-1]
    scenario = json.loads((REPO_ROOT / f"simulation/scenarios/{args.scenario}.json").read_text())
    live = OccupancyGrid.load(path)
    truth = OccupancyGrid(live.spec)
    truth.fill_from_scene(SceneGeometry(scenario))
    res = live.spec.resolution_m
    print(f"map: {path}")
    print(f"scenario: {args.scenario}   voxel: {res} m   frames: {live.frames_integrated}")

    base = live.compare_with(truth)
    print(
        f"as saved:      precision {base['precision']:.3f}  "
        f"recall {base['recall_of_observed_surface']:.3f}  "
        f"occupied {base['mapped_occupied_voxels']}  "
        f"false positives {base['false_positive_voxels']}"
    )

    # Search whole-voxel shifts by how many occupied voxels land exactly on true geometry
    # (an index lookup per shift; smaller shifts win ties), then score the best one in full.
    reach = int(round(args.search_m / res))
    exact = truth.occupied_mask
    occupied = np.argwhere(live.occupied_mask)
    shape = np.array(live.log_odds.shape)
    best_cells, best_precision = (0, 0, 0), -1.0
    candidates = [
        (dx, dy, dz)
        for dz in range(-3, 4)
        for dx in range(-reach, reach + 1)
        for dy in range(-reach, reach + 1)
    ]
    candidates.sort(key=lambda c: c[0] ** 2 + c[1] ** 2 + c[2] ** 2)
    for dx, dy, dz in candidates:
        moved = occupied + np.array([dx, dy, dz])
        inside = moved[np.all((moved >= 0) & (moved < shape), axis=1)]
        if len(inside) < 0.9 * len(occupied):
            continue
        hits = np.count_nonzero(exact[inside[:, 0], inside[:, 1], inside[:, 2]])
        precision = hits / max(1, len(inside))
        if precision > best_precision + 1e-9:
            best_cells, best_precision = (dx, dy, dz), precision
    best = (best_precision, best_cells, shifted(live, best_cells).compare_with(truth))
    _, (dx, dy, dz), report = best
    print(
        f"best shift:    precision {report['precision']:.3f}  "
        f"recall {report['recall_of_observed_surface']:.3f}  "
        f"after moving the map east {dx * res:+.1f} m, north {dy * res:+.1f} m, "
        f"up {dz * res:+.1f} m"
    )
    gain = (
        float(report["precision"])
        + float(report["recall_of_observed_surface"])
        - float(base["precision"])
        - float(base["recall_of_observed_surface"])
    )
    if (dx, dy, dz) == (0, 0, 0) or gain < 0.05:
        print("  -> no constant offset explains the error; it is unstructured (pose noise/drift,")
        print("     or geometry in the simulator that the scenario file does not describe).")
    else:
        print("  -> a constant offset explains part of the error (mounting, latency or origin).")

    mine, true = live.occupied_mask, truth.occupied_mask
    distance_m = distance_transform_edt(~true) * res
    occupied_distance = distance_m[mine]
    print("occupied voxels by distance from true geometry:")
    edges = [0.0, 0.5, 1.0, 1.5, 2.0, 3.0, 5.0, 1e9]
    for lo, hi in zip(edges[:-1], edges[1:], strict=True):
        count = int(np.count_nonzero((occupied_distance >= lo) & (occupied_distance < hi)))
        label = f"{lo:.1f}-{hi:.1f} m" if hi < 1e8 else f">= {lo:.1f} m"
        print(f"  {label:>11}: {count:5d}  {100 * count / max(1, occupied_distance.size):5.1f}%")

    false_positive = mine & (distance_m > res)
    heights = np.nonzero(false_positive)[2]
    print("false positives by height above the ground (voxel layer):")
    for k in range(live.log_odds.shape[2]):
        count = int(np.count_nonzero(heights == k))
        if count:
            z = live.spec.origin_enu_m[2] + (k + 0.5) * res
            print(f"  z = {z:5.2f} m: {count:5d}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
