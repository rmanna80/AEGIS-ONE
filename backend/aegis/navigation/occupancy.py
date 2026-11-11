"""3D occupancy grid (log-odds) built from depth frames.

Dense voxel grid in ENU. Each depth ray lowers the occupancy belief of the
voxels it passes through and raises it at its end point, so the map converges
with repeated observation and transient errors wash out. Cells never observed
stay at 0 ("unknown").
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
from numpy.typing import NDArray

from aegis.navigation.depth import DepthFrame, Pose, body_to_enu_matrix, pixel_rays_body
from aegis.navigation.geometry import SceneGeometry

MAP_FORMAT_VERSION = 1


@dataclass(frozen=True)
class GridSpec:
    origin_enu_m: tuple[float, float, float]  # minimum corner
    size_cells: tuple[int, int, int]  # east, north, up
    resolution_m: float = 0.5

    @classmethod
    def around(
        cls,
        bounds: tuple[float, float, float, float],
        ceiling_m: float,
        resolution_m: float = 0.5,
        margin_m: float = 5.0,
    ) -> GridSpec:
        """Grid covering an ENU bounding box (min_e, min_n, max_e, max_n) up to a ceiling."""
        min_e, min_n, max_e, max_n = bounds
        origin = (min_e - margin_m, min_n - margin_m, 0.0)
        size = (
            math.ceil((max_e - min_e + 2 * margin_m) / resolution_m),
            math.ceil((max_n - min_n + 2 * margin_m) / resolution_m),
            math.ceil(ceiling_m / resolution_m),
        )
        return cls(origin, size, resolution_m)


class OccupancyGrid:
    L_HIT = 0.85
    L_MISS = -0.4
    L_MIN, L_MAX = -2.0, 3.5
    OCCUPIED_THRESHOLD = 0.7  # log-odds above this counts as occupied (p > 0.67)
    FREE_THRESHOLD = -0.3
    GROUND_FILTER_M = 0.4  # returns this close to z = 0 are ground, not obstacles

    def __init__(self, spec: GridSpec) -> None:
        self.spec = spec
        self.log_odds = np.zeros(spec.size_cells, dtype=np.float32)
        self.frames_integrated = 0
        self.revision = 0  # bumped on every change; lets clients skip unchanged maps

    # ---------------------------------------------------------------- coordinates

    def to_index(self, points: NDArray[np.float64]) -> tuple[NDArray[np.int64], NDArray[np.bool_]]:
        """Voxel indices for ``(N, 3)`` ENU points and a mask of those inside the grid."""
        idx = np.floor((points - np.array(self.spec.origin_enu_m)) / self.spec.resolution_m).astype(
            np.int64
        )
        valid = np.all((idx >= 0) & (idx < np.array(self.spec.size_cells)), axis=1)
        return idx, valid

    def centres(self, indices: NDArray[np.int64]) -> NDArray[np.float64]:
        origin = np.array(self.spec.origin_enu_m, dtype=np.float64)
        result: NDArray[np.float64] = origin + (indices + 0.5) * self.spec.resolution_m
        return result

    # ------------------------------------------------------------------ integrate

    def integrate(self, frame: DepthFrame, pose: Pose, max_rays: int = 2500) -> None:
        """Fuse one depth frame taken from ``pose`` into the map."""
        stride = max(1, math.ceil(math.sqrt(frame.width * frame.height / max_rays)))
        depth = frame.depth_m[stride // 2 :: stride, stride // 2 :: stride].astype(np.float64)
        rays = pixel_rays_body(frame.width, frame.height, frame.horizontal_fov_rad)[
            stride // 2 :: stride, stride // 2 :: stride
        ].reshape(-1, 3)
        depth = depth.reshape(-1)
        scale = np.linalg.norm(rays, axis=1)
        directions = (rays / scale[:, None]) @ body_to_enu_matrix(pose.quaternion_wxyz).T
        ranges = depth * scale
        origin = np.array(pose.position_enu_m)

        returned = np.isfinite(ranges) & (ranges > 0.05) & (ranges <= frame.max_range_m)
        # Rays with no return clear space out to the sensor's range; NaN rays are ignored.
        usable = returned | np.isposinf(ranges)
        free_to = np.where(returned, ranges - self.spec.resolution_m, frame.max_range_m)

        step = self.spec.resolution_m * 0.9
        distances = np.arange(step, frame.max_range_m, step)
        along = distances[None, :] <= free_to[:, None]
        along &= usable[:, None]
        ray_idx, step_idx = np.nonzero(along)
        free_points = origin + directions[ray_idx] * distances[step_idx][:, None]
        self._apply(free_points, self.L_MISS)

        hits = origin + directions[returned] * ranges[returned][:, None]
        hits = hits[hits[:, 2] > self.GROUND_FILTER_M]
        self._apply(hits, self.L_HIT)

        self.frames_integrated += 1
        self.revision += 1

    def _apply(self, points: NDArray[np.float64], delta: float) -> None:
        if len(points) == 0:
            return
        idx, valid = self.to_index(points)
        idx = np.unique(idx[valid], axis=0)  # at most one update per voxel per frame
        if len(idx) == 0:
            return
        i, j, k = idx[:, 0], idx[:, 1], idx[:, 2]
        self.log_odds[i, j, k] = np.clip(self.log_odds[i, j, k] + delta, self.L_MIN, self.L_MAX)

    # -------------------------------------------------------------------- queries

    @property
    def occupied_mask(self) -> NDArray[np.bool_]:
        return self.log_odds > self.OCCUPIED_THRESHOLD

    def occupied_indices(self) -> NDArray[np.int64]:
        return np.argwhere(self.occupied_mask)

    def occupied_centres(self) -> NDArray[np.float64]:
        return self.centres(self.occupied_indices())

    def observed_fraction(self) -> float:
        return float(np.count_nonzero(self.log_odds) / self.log_odds.size)

    def slice_2d(self, altitude_m: float, vertical_margin_m: float) -> NDArray[np.bool_]:
        """East x North mask: occupied anywhere within ``altitude +/- margin``."""
        res = self.spec.resolution_m
        z0 = self.spec.origin_enu_m[2]
        low = max(0, math.floor((altitude_m - vertical_margin_m - z0) / res))
        high = min(self.spec.size_cells[2], math.ceil((altitude_m + vertical_margin_m - z0) / res))
        if high <= low:
            return np.zeros(self.spec.size_cells[:2], dtype=bool)
        return np.any(self.occupied_mask[:, :, low:high], axis=2)

    def nearest_obstacle_m(
        self, point: tuple[float, float, float], search_m: float = 20.0
    ) -> float | None:
        """Distance from a point to the nearest occupied voxel centre within ``search_m``."""
        centre = np.array(point)
        idx, _ = self.to_index(centre[None, :])
        reach = math.ceil(search_m / self.spec.resolution_m)
        lo = np.maximum(idx[0] - reach, 0)
        hi = np.minimum(idx[0] + reach + 1, np.array(self.spec.size_cells))
        if np.any(hi <= lo):
            return None
        local = np.argwhere(self.occupied_mask[lo[0] : hi[0], lo[1] : hi[1], lo[2] : hi[2]])
        if len(local) == 0:
            return None
        distances = np.linalg.norm(self.centres(local + lo) - centre, axis=1)
        nearest = float(distances.min())
        return nearest if nearest <= search_m else None

    # ------------------------------------------------------------- prior / truth

    def fill_from_scene(self, scene: SceneGeometry) -> None:
        """Mark every voxel whose centre lies inside known geometry as occupied (prior map)."""
        res = self.spec.resolution_m
        origin = np.array(self.spec.origin_enu_m)
        size = np.array(self.spec.size_cells)
        # Only the voxels inside each obstacle's bounding box need testing.
        extents = [
            (cx, cy, math.hypot(hx, hy), height) for cx, cy, hx, hy, height, _ in scene.boxes
        ] + [(cx, cy, radius, height) for cx, cy, radius, height in scene.cylinders]
        for cx, cy, reach, height in extents:
            lo = np.floor((np.array([cx - reach, cy - reach, 0.0]) - origin) / res).astype(int)
            hi = np.ceil((np.array([cx + reach, cy + reach, height]) - origin) / res).astype(int)
            lo, hi = np.maximum(lo, 0), np.minimum(hi + 1, size)
            if np.any(hi <= lo):
                continue
            ii, jj, kk = np.meshgrid(
                np.arange(lo[0], hi[0]),
                np.arange(lo[1], hi[1]),
                np.arange(lo[2], hi[2]),
                indexing="ij",
            )
            idx = np.stack([ii.ravel(), jj.ravel(), kk.ravel()], axis=1)
            inside = idx[scene.occupied(self.centres(idx))]
            self.log_odds[inside[:, 0], inside[:, 1], inside[:, 2]] = self.L_MAX
        self.revision += 1

    def compare_with(self, truth: OccupancyGrid) -> dict[str, Any]:
        """Score this map's occupied voxels against a ground-truth grid of the same shape.

        A reconstructed surface voxel can legitimately sit one cell away from the
        true one (range noise, voxel quantisation), so matches are counted with a
        one-voxel tolerance. Only truth voxels this map has observed count toward
        recall; unobserved space is reported separately as coverage.
        """
        from scipy.ndimage import binary_dilation, binary_erosion

        if truth.spec != self.spec:
            raise ValueError("grids have different geometry")
        mine, true = self.occupied_mask, truth.occupied_mask
        observed = self.log_odds != 0.0
        # Only obstacle surfaces are visible to a depth sensor; interiors can never be mapped.
        surface = true & ~binary_erosion(true, border_value=1)
        near_true = binary_dilation(true)
        near_mine = binary_dilation(mine)
        true_positive = int(np.count_nonzero(mine & near_true))
        false_positive = int(np.count_nonzero(mine & ~near_true))
        seen_surface = surface & binary_dilation(observed)
        recalled = int(np.count_nonzero(seen_surface & near_mine))
        seen = int(np.count_nonzero(seen_surface))
        return {
            "resolution_m": self.spec.resolution_m,
            "tolerance_voxels": 1,
            "mapped_occupied_voxels": int(np.count_nonzero(mine)),
            "true_surface_voxels": int(np.count_nonzero(surface)),
            "true_surface_voxels_observed": seen,
            "surface_coverage": round(seen / max(1, int(np.count_nonzero(surface))), 4),
            "precision": round(true_positive / max(1, true_positive + false_positive), 4),
            "recall_of_observed_surface": round(recalled / max(1, seen), 4),
            "false_positive_voxels": false_positive,
            "observed_fraction_of_volume": round(self.observed_fraction(), 4),
        }

    # ------------------------------------------------------------------ save/load

    def save(self, path: Path, metadata: dict[str, Any] | None = None) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        np.savez_compressed(
            path,
            format_version=MAP_FORMAT_VERSION,
            log_odds=self.log_odds,
            origin_enu_m=np.array(self.spec.origin_enu_m),
            resolution_m=self.spec.resolution_m,
            frames_integrated=self.frames_integrated,
            metadata=np.array(repr(metadata or {})),
        )

    @classmethod
    def load(cls, path: Path) -> OccupancyGrid:
        with np.load(path, allow_pickle=False) as data:
            if int(data["format_version"]) != MAP_FORMAT_VERSION:
                raise ValueError(f"{path}: unsupported map format {int(data['format_version'])}")
            log_odds = data["log_odds"]
            origin = tuple(float(v) for v in data["origin_enu_m"])
            nx, ny, nz = (int(v) for v in log_odds.shape)
            spec = GridSpec(
                (origin[0], origin[1], origin[2]), (nx, ny, nz), float(data["resolution_m"])
            )
            grid = cls(spec)
            grid.log_odds = log_odds.astype(np.float32)
            grid.frames_integrated = int(data["frames_integrated"])
        return grid
