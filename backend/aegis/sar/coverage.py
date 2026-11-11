"""Search-area coverage: a lawnmower flight plan and a record of what was actually seen."""

from __future__ import annotations

import math
from typing import Any

import numpy as np
from numpy.typing import NDArray

from aegis.missions import (
    Mission,
    MissionConstraints,
    Point2,
    Waypoint,
    point_in_polygon,
)
from aegis.navigation.depth import Pose
from aegis.sar.thermal import ground_points


class CoveragePlanError(ValueError):
    pass


def swath_width_m(altitude_m: float, horizontal_fov_rad: float) -> float:
    """Width on the ground seen across the direction of travel from ``altitude_m``."""
    return 2.0 * altitude_m * math.tan(horizontal_fov_rad / 2.0)


def _polygon_spans(polygon: list[Point2], y: float) -> list[tuple[float, float]]:
    """Intervals of x inside the polygon along the horizontal line at ``y``."""
    crossings: list[float] = []
    for i, (x1, y1) in enumerate(polygon):
        x2, y2 = polygon[(i + 1) % len(polygon)]
        if (y1 > y) != (y2 > y):
            crossings.append(x1 + (y - y1) * (x2 - x1) / (y2 - y1))
    crossings.sort()
    return list(zip(crossings[0::2], crossings[1::2], strict=False))


def _clip(polygon: list[Point2], axis: int, limit: float, keep_below: bool) -> list[Point2]:
    """Sutherland-Hodgman clip of a polygon against one axis-aligned line."""
    result: list[Point2] = []
    for i, current in enumerate(polygon):
        previous = polygon[i - 1]
        inside_c = current[axis] <= limit if keep_below else current[axis] >= limit
        inside_p = previous[axis] <= limit if keep_below else previous[axis] >= limit
        if inside_c != inside_p:
            t = (limit - previous[axis]) / (current[axis] - previous[axis])
            result.append(
                (
                    previous[0] + t * (current[0] - previous[0]),
                    previous[1] + t * (current[1] - previous[1]),
                )
            )
        if inside_c:
            result.append(current)
    return result


def split_area(area: list[Point2], parts: int) -> list[list[Point2]]:
    """Cut a search area into ``parts`` side-by-side strips of equal width along its
    longer side, one per vehicle, so the vehicles work in separate places."""
    if parts <= 1:
        return [list(area)]
    xs, ys = [p[0] for p in area], [p[1] for p in area]
    axis = 0 if (max(xs) - min(xs)) >= (max(ys) - min(ys)) else 1
    low = min(p[axis] for p in area)
    width = (max(p[axis] for p in area) - low) / parts
    strips = []
    for i in range(parts):
        strip = _clip(
            _clip(list(area), axis, low + i * width, False), axis, low + (i + 1) * width, True
        )
        if len(strip) >= 3:
            strips.append(strip)
    return strips


def plan_lawnmower(
    area: list[Point2],
    constraints: MissionConstraints,
    altitude_m: float,
    horizontal_fov_rad: float,
    overlap: float = 0.2,
    start_enu_m: Point2 = (0.0, 0.0),
    mission_id: str = "sar-search",
    name: str = "Search pattern",
) -> tuple[Mission, dict[str, Any]]:
    """Parallel passes over ``area`` so neighbouring sensor swaths overlap by ``overlap``.

    Passes run along the area's longer bounding-box side. The result is an ordinary
    mission: it still has to pass validation and be submitted by the operator.
    """
    if len(area) < 3:
        raise CoveragePlanError("The search area needs at least three corners.")
    if not 0.0 <= overlap < 0.9:
        raise CoveragePlanError("Overlap must be between 0 and 0.9.")
    fence = constraints.geofence
    if not constraints.min_altitude_rel_m <= altitude_m <= fence.max_altitude_rel_m:
        raise CoveragePlanError(
            f"Search altitude {altitude_m:g} m is outside the allowed "
            f"{constraints.min_altitude_rel_m:g}-{fence.max_altitude_rel_m:g} m."
        )
    swath = swath_width_m(altitude_m, horizontal_fov_rad)
    spacing = swath * (1.0 - overlap)
    xs, ys = [p[0] for p in area], [p[1] for p in area]
    # Work in a frame where passes run along x; swap axes when the area is taller than wide.
    swap = (max(ys) - min(ys)) > (max(xs) - min(xs))
    polygon = [(y, x) for x, y in area] if swap else list(area)
    low, high = min(p[1] for p in polygon), max(p[1] for p in polygon)
    lanes = max(1, math.ceil((high - low) / spacing))
    # Lanes are centred in equal strips, so the outer swaths reach the area's edges.
    strip = (high - low) / lanes
    passes: list[tuple[Point2, Point2]] = []
    for i in range(lanes):
        y = low + (i + 0.5) * strip
        spans = _polygon_spans(polygon, y)
        if not spans:
            continue
        x0, x1 = spans[0][0], spans[-1][1]
        passes.append(((x0, y), (x1, y)))
    if not passes:
        raise CoveragePlanError("The search area is too small to plan a pass through.")

    def to_enu(point: Point2) -> Point2:
        return (point[1], point[0]) if swap else point

    # Start from whichever end of the first or last pass is nearest the vehicle.
    ends = [passes[0][0], passes[0][1], passes[-1][0], passes[-1][1]]
    nearest = min(range(4), key=lambda i: math.dist(to_enu(ends[i]), start_enu_m))
    if nearest >= 2:
        passes.reverse()
    forward = nearest % 2 == 0
    waypoints: list[Waypoint] = []
    for a, b in passes:
        first, second = (a, b) if forward else (b, a)
        for point in (first, second):
            east, north = to_enu(point)
            waypoints.append(Waypoint(east_m=east, north_m=north, altitude_rel_m=altitude_m))
        forward = not forward
    if len(waypoints) > 100:
        raise CoveragePlanError(
            f"The pattern needs {len(waypoints)} waypoints (limit 100); raise the altitude, "
            "lower the overlap or split the area."
        )
    for wp in waypoints:
        if not point_in_polygon((wp.east_m, wp.north_m), fence.polygon_enu_m):
            raise CoveragePlanError(
                f"A pass end at ({wp.east_m:.0f}, {wp.north_m:.0f}) is outside the geofence."
            )
    mission = Mission(
        mission_id=mission_id, name=name, waypoints=waypoints, constraints=constraints
    )
    path = [start_enu_m] + [(w.east_m, w.north_m) for w in waypoints]
    length = sum(math.dist(a, b) for a, b in zip(path[:-1], path[1:], strict=True))
    summary = {
        "passes": len(passes),
        "altitude_rel_m": altitude_m,
        "swath_width_m": round(swath, 1),
        "pass_spacing_m": round(strip, 1),
        "overlap_planned": round(1.0 - strip / swath, 2),
        "path_length_m": round(length, 0),
        "estimated_search_time_s": round(length / constraints.cruise_speed_m_s, 0),
    }
    return mission, summary


class CoverageTracker:
    """Which parts of the search area the thermal camera has actually looked at."""

    def __init__(self, area: list[Point2], cell_m: float = 1.0) -> None:
        self.area = area
        self.cell_m = cell_m
        xs, ys = [p[0] for p in area], [p[1] for p in area]
        self.origin = (min(xs), min(ys))
        nx = max(1, math.ceil((max(xs) - min(xs)) / cell_m))
        ny = max(1, math.ceil((max(ys) - min(ys)) / cell_m))
        centres_x = self.origin[0] + (np.arange(nx) + 0.5) * cell_m
        centres_y = self.origin[1] + (np.arange(ny) + 0.5) * cell_m
        self._cx, self._cy = np.meshgrid(centres_x, centres_y, indexing="ij")
        self.inside: NDArray[np.bool_] = np.array(
            [[point_in_polygon((float(x), float(y)), area) for y in centres_y] for x in centres_x],
            dtype=bool,
        )
        self.seen = np.zeros((nx, ny), dtype=bool)
        self.revision = 0

    def mark(self, pose: Pose, horizontal_fov_rad: float, width: int, height: int) -> None:
        """Mark the cells inside the image's footprint on the ground as seen."""
        aspect = height / width
        half_x = math.tan(horizontal_fov_rad / 2.0)
        half_y = half_x * aspect
        corners_px = np.array(
            [
                [half_y, -half_x, 1.0],
                [half_y, half_x, 1.0],
                [-half_y, half_x, 1.0],
                [-half_y, -half_x, 1.0],
            ]
        )
        points, valid = ground_points(corners_px, pose)
        if not valid.all():
            return
        quad = points[:, :2]
        # Orientation of the footprint (signed area), so "inside" means the same side of
        # every edge whichever way the corners run.
        signed_area = sum(
            quad[i][0] * quad[(i + 1) % 4][1] - quad[(i + 1) % 4][0] * quad[i][1] for i in range(4)
        )
        sign = 1.0 if signed_area >= 0 else -1.0
        inside = np.ones(self.seen.shape, dtype=bool)
        for i in range(4):
            a, b = quad[i], quad[(i + 1) % 4]
            cross = (b[0] - a[0]) * (self._cy - a[1]) - (b[1] - a[1]) * (self._cx - a[0])
            inside &= cross * sign >= 0
        before = int(self.seen.sum())
        self.seen |= inside & self.inside
        if int(self.seen.sum()) != before:
            self.revision += 1

    def fraction(self) -> float:
        total = int(self.inside.sum())
        return float((self.seen & self.inside).sum()) / total if total else 0.0

    def payload(self) -> dict[str, Any]:
        """Coverage as run-length rows, compact enough to poll."""
        rows = []
        for j in range(self.seen.shape[1]):
            column = self.seen[:, j]
            edges = np.flatnonzero(np.diff(np.concatenate(([0], column.view(np.int8), [0]))))
            rows.append([int(v) for v in edges])
        return {
            "revision": self.revision,
            "fraction": round(self.fraction(), 4),
            "origin_enu_m": list(self.origin),
            "cell_m": self.cell_m,
            "size_cells": list(self.seen.shape),
            "seen_runs_by_north_row": rows,
            "area_enu_m": [list(p) for p in self.area],
        }
