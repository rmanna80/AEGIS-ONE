"""Known scene geometry from a scenario file: ground-truth occupancy and ray casting.

Used for three things: the prior obstacle map for route checking, the simulated
depth sensor, and the ground truth that reconstructed maps are scored against.
All coordinates are ENU metres (x east, y north, z up).
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np
from numpy.typing import NDArray

Array = NDArray[np.float64]


class SceneGeometry:
    """Boxes (yaw-rotated, resting on the ground), vertical cylinders and the ground plane."""

    def __init__(self, scenario: dict[str, Any]) -> None:
        boxes: list[tuple[float, float, float, float, float, float]] = []  # cx, cy, hx, hy, h, yaw
        cylinders: list[tuple[float, float, float, float]] = []  # cx, cy, r, h
        for obstacle in scenario.get("obstacles", []):
            if obstacle["shape"] == "box":
                sx, sy, sz = obstacle["size_enu_m"]
                yaw = math.radians(obstacle.get("yaw_deg", 0.0))
                boxes.append((obstacle["east_m"], obstacle["north_m"], sx / 2, sy / 2, sz, yaw))
            elif obstacle["shape"] == "cylinder":
                cylinders.append(
                    (
                        obstacle["east_m"],
                        obstacle["north_m"],
                        obstacle["radius_m"],
                        obstacle["height_m"],
                    )
                )
            else:
                raise ValueError(f"Unsupported obstacle shape: {obstacle['shape']}")
        # Staged people and vehicles are physical objects too.
        for prop in scenario.get("props", []):
            length, width, height = prop["size_m"]
            yaw = math.radians(prop.get("yaw_deg", 0.0))
            boxes.append((prop["east_m"], prop["north_m"], length / 2, width / 2, height, yaw))
        self.boxes = np.array(boxes, dtype=np.float64).reshape(-1, 6)
        self.cylinders = np.array(cylinders, dtype=np.float64).reshape(-1, 4)

    # ------------------------------------------------------------------ occupancy

    def occupied(self, points: Array) -> NDArray[np.bool_]:
        """Whether each ``(N, 3)`` point is inside an obstacle (the ground is not an obstacle)."""
        points = np.atleast_2d(points)
        inside = np.zeros(len(points), dtype=bool)
        for cx, cy, hx, hy, height, yaw in self.boxes:
            dx, dy = points[:, 0] - cx, points[:, 1] - cy
            c, s = math.cos(yaw), math.sin(yaw)
            local_x, local_y = dx * c + dy * s, -dx * s + dy * c
            inside |= (
                (np.abs(local_x) <= hx)
                & (np.abs(local_y) <= hy)
                & (points[:, 2] >= 0.0)
                & (points[:, 2] <= height)
            )
        for cx, cy, radius, height in self.cylinders:
            inside |= (
                (np.hypot(points[:, 0] - cx, points[:, 1] - cy) <= radius)
                & (points[:, 2] >= 0.0)
                & (points[:, 2] <= height)
            )
        return inside

    # ---------------------------------------------------------------- ray casting

    def raycast(self, origin: Array, directions: Array, max_range: float) -> Array:
        """Distance along each unit ray to the first surface; ``inf`` beyond ``max_range``."""
        directions = np.atleast_2d(directions)
        best = np.full(len(directions), np.inf)

        # Ground plane z = 0.
        dz = directions[:, 2]
        with np.errstate(divide="ignore", invalid="ignore"):
            t_ground = np.where(dz < -1e-9, -origin[2] / dz, np.inf)
        best = np.minimum(best, np.where(t_ground > 0.0, t_ground, np.inf))

        for cx, cy, hx, hy, height, yaw in self.boxes:
            c, s = math.cos(yaw), math.sin(yaw)
            ox, oy = origin[0] - cx, origin[1] - cy
            local_o = np.array([ox * c + oy * s, -ox * s + oy * c, origin[2]])
            local_d = np.stack(
                [
                    directions[:, 0] * c + directions[:, 1] * s,
                    -directions[:, 0] * s + directions[:, 1] * c,
                    directions[:, 2],
                ],
                axis=1,
            )
            lower = np.array([-hx, -hy, 0.0])
            upper = np.array([hx, hy, height])
            with np.errstate(divide="ignore", invalid="ignore"):
                inv = 1.0 / local_d
                t1 = (lower - local_o) * inv
                t2 = (upper - local_o) * inv
            # A ray parallel to a slab misses unless its origin lies inside the slab.
            parallel = np.abs(local_d) < 1e-12
            outside = parallel & ((local_o < lower) | (local_o > upper))
            t_near = np.where(parallel, -np.inf, np.minimum(t1, t2)).max(axis=1)
            t_far = np.where(parallel, np.inf, np.maximum(t1, t2)).min(axis=1)
            hit = (t_far >= np.maximum(t_near, 0.0)) & ~outside.any(axis=1)
            t = np.where(t_near > 0.0, t_near, t_far)  # origin inside the box -> exit distance
            best = np.minimum(best, np.where(hit, t, np.inf))

        for cx, cy, radius, height in self.cylinders:
            ox, oy = origin[0] - cx, origin[1] - cy
            a = directions[:, 0] ** 2 + directions[:, 1] ** 2
            b = 2.0 * (ox * directions[:, 0] + oy * directions[:, 1])
            c0 = ox * ox + oy * oy - radius * radius
            disc = b * b - 4.0 * a * c0
            with np.errstate(divide="ignore", invalid="ignore"):
                root = np.sqrt(np.maximum(disc, 0.0))
                t_side = np.where((disc >= 0.0) & (a > 1e-12), (-b - root) / (2.0 * a), np.inf)
            with np.errstate(invalid="ignore"):
                z_side = origin[2] + t_side * directions[:, 2]
            side_ok = (t_side > 0.0) & (z_side >= 0.0) & (z_side <= height)
            best = np.minimum(best, np.where(side_ok, t_side, np.inf))
            # Top cap.
            with np.errstate(divide="ignore", invalid="ignore"):
                t_cap = np.where(np.abs(dz) > 1e-9, (height - origin[2]) / dz, np.inf)
            with np.errstate(invalid="ignore"):
                px = ox + t_cap * directions[:, 0]
                py = oy + t_cap * directions[:, 1]
            cap_ok = (t_cap > 0.0) & (px * px + py * py <= radius * radius)
            best = np.minimum(best, np.where(cap_ok, t_cap, np.inf))

        return np.where(best <= max_range, best, np.inf)
