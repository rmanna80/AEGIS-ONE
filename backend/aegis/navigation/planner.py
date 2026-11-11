"""Obstacle-aware route checking and 2D path planning on occupancy grids.

Routes are checked in 3D (each sample of a leg against the map slice at that
sample's altitude) and planned in 2D at the leg's altitude band with A*. Space
the map has never observed is treated as free, which is optimistic; the live
route monitor exists to catch what that optimism misses.
"""

from __future__ import annotations

import heapq
import math
from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray
from scipy.ndimage import binary_dilation

from aegis.missions import Mission, Waypoint
from aegis.navigation.occupancy import OccupancyGrid

Mask = NDArray[np.bool_]
Cell = tuple[int, int]
Point3 = tuple[float, float, float]  # east, north, altitude above home

# PX4 flies home at RTL_RETURN_ALT above home (30 m by default) unless already higher.
RTL_RETURN_ALTITUDE_M = 30.0


class PlanningError(Exception):
    pass


def disk(radius_cells: int) -> Mask:
    r = np.arange(-radius_cells, radius_cells + 1)
    return (r[:, None] ** 2 + r[None, :] ** 2) <= radius_cells**2


def inflate(mask: Mask, radius_cells: int) -> Mask:
    """Grow obstacles by a clearance radius so a path may be planned for a point vehicle."""
    if radius_cells <= 0 or not mask.any():
        return mask
    result: Mask = binary_dilation(mask, structure=disk(radius_cells))
    return result


def line_cells(a: Cell, b: Cell) -> list[Cell]:
    """Every cell touched by the segment between two cell centres (supercover line)."""
    (x0, y0), (x1, y1) = a, b
    dx, dy = abs(x1 - x0), abs(y1 - y0)
    sx, sy = (1 if x1 > x0 else -1), (1 if y1 > y0 else -1)
    cells = [(x0, y0)]
    x, y, err = x0, y0, dx - dy
    while (x, y) != (x1, y1):
        e2 = 2 * err
        if e2 > -dy and e2 < dx:  # exact diagonal step: include both neighbours
            cells.extend([(x + sx, y), (x, y + sy)])
            x, y, err = x + sx, y + sy, err - dy + dx
        elif e2 > -dy:
            x, err = x + sx, err - dy
        else:
            y, err = y + sy, err + dx
        cells.append((x, y))
    return cells


def line_is_free(blocked: Mask, a: Cell, b: Cell) -> bool:
    nx, ny = blocked.shape
    return all(0 <= x < nx and 0 <= y < ny and not blocked[x, y] for x, y in line_cells(a, b))


def astar(blocked: Mask, start: Cell, goal: Cell) -> list[Cell] | None:
    """Shortest 8-connected path avoiding blocked cells; diagonal moves may not cut corners."""
    nx, ny = blocked.shape
    if blocked[start] or blocked[goal]:
        return None
    moves = [(1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (1, -1), (-1, 1), (-1, -1)]
    costs = [1.0, 1.0, 1.0, 1.0, math.sqrt(2), math.sqrt(2), math.sqrt(2), math.sqrt(2)]

    def heuristic(cell: Cell) -> float:  # octile distance: admissible for 8-connected grids
        dx, dy = abs(cell[0] - goal[0]), abs(cell[1] - goal[1])
        return max(dx, dy) + (math.sqrt(2) - 1.0) * min(dx, dy)

    best = {start: 0.0}
    parent: dict[Cell, Cell] = {}
    queue: list[tuple[float, float, Cell]] = [(heuristic(start), 0.0, start)]
    while queue:
        _, cost, cell = heapq.heappop(queue)
        if cell == goal:
            path = [cell]
            while cell in parent:
                cell = parent[cell]
                path.append(cell)
            return path[::-1]
        if cost > best[cell]:
            continue
        x, y = cell
        for (dx, dy), step in zip(moves, costs, strict=True):
            nxt = (x + dx, y + dy)
            if not (0 <= nxt[0] < nx and 0 <= nxt[1] < ny) or blocked[nxt]:
                continue
            if dx and dy and (blocked[x + dx, y] or blocked[x, y + dy]):
                continue
            new_cost = cost + step
            if new_cost < best.get(nxt, math.inf):
                best[nxt] = new_cost
                parent[nxt] = cell
                heapq.heappush(queue, (new_cost + heuristic(nxt), new_cost, nxt))
    return None


def simplify(path: list[Cell], blocked: Mask) -> list[Cell]:
    """Drop intermediate cells wherever a straight, obstacle-free line connects two of them."""
    if len(path) <= 2:
        return path
    out = [path[0]]
    anchor = 0
    while anchor < len(path) - 1:
        reach = len(path) - 1
        while reach > anchor + 1 and not line_is_free(blocked, path[anchor], path[reach]):
            reach -= 1
        out.append(path[reach])
        anchor = reach
    return out


@dataclass(frozen=True)
class Conflict:
    distance_along_m: float
    point: Point3


class RouteChecker:
    """Checks and plans routes against one or more occupancy grids of identical geometry
    (typically the prior map of known obstacles plus the live map from the depth sensor)."""

    def __init__(
        self,
        grids: list[OccupancyGrid],
        clearance_m: float = 3.0,
        vertical_margin_m: float = 2.0,
        planning_margin_m: float = 1.5,
    ) -> None:
        if not grids:
            raise ValueError("at least one grid is required")
        if any(g.spec != grids[0].spec for g in grids):
            raise ValueError("grids have different geometry")
        self.grids = grids
        self.spec = grids[0].spec
        self.clearance_m = clearance_m
        self.vertical_margin_m = vertical_margin_m
        self.planning_margin_m = planning_margin_m
        self._cache: dict[tuple[float, float, float], Mask] = {}
        self._cache_revision = -1

    def _revision(self) -> int:
        return sum(g.revision for g in self.grids)

    def blocked_slice(
        self, altitude_low_m: float, altitude_high_m: float | None = None, extra_m: float = 0.0
    ) -> Mask:
        """Inflated East x North mask of everything occupied in an altitude band.

        ``extra_m`` widens the clearance; planning uses it so that a detour does not
        hug the exact clearance boundary and get invalidated by the next map update.
        """
        high = altitude_low_m if altitude_high_m is None else altitude_high_m
        revision = self._revision()
        if revision != self._cache_revision:
            self._cache.clear()
            self._cache_revision = revision
        res = self.spec.resolution_m
        key = (round(altitude_low_m / res) * res, round(high / res) * res, extra_m)
        if key not in self._cache:
            centre, half = (key[0] + key[1]) / 2.0, (key[1] - key[0]) / 2.0
            mask = np.zeros(self.spec.size_cells[:2], dtype=bool)
            for grid in self.grids:
                mask |= grid.slice_2d(centre, half + self.vertical_margin_m)
            radius = math.ceil((self.clearance_m + extra_m) / res)
            self._cache[key] = inflate(mask, radius)
        return self._cache[key]

    def cell(self, east: float, north: float) -> Cell | None:
        res = self.spec.resolution_m
        i = math.floor((east - self.spec.origin_enu_m[0]) / res)
        j = math.floor((north - self.spec.origin_enu_m[1]) / res)
        if 0 <= i < self.spec.size_cells[0] and 0 <= j < self.spec.size_cells[1]:
            return i, j
        return None

    def cell_centre(self, cell: Cell) -> tuple[float, float]:
        res = self.spec.resolution_m
        return (
            self.spec.origin_enu_m[0] + (cell[0] + 0.5) * res,
            self.spec.origin_enu_m[1] + (cell[1] + 0.5) * res,
        )

    def point_blocked(self, point: Point3) -> bool:
        cell = self.cell(point[0], point[1])
        if cell is None:
            return False  # outside the mapped volume: nothing known there
        return bool(self.blocked_slice(point[2])[cell])

    def first_conflict(
        self, a: Point3, b: Point3, max_distance_m: float = math.inf
    ) -> Conflict | None:
        """First point along the straight 3D leg a -> b that violates the clearance."""
        length = math.dist(a, b)
        steps = max(1, math.ceil(min(length, max_distance_m) / (self.spec.resolution_m / 2.0)))
        for step in range(steps + 1):
            along = min(length, step * self.spec.resolution_m / 2.0)
            f = along / length if length > 0 else 0.0
            point = (a[0] + f * (b[0] - a[0]), a[1] + f * (b[1] - a[1]), a[2] + f * (b[2] - a[2]))
            if self.point_blocked(point):
                return Conflict(along, point)
        return None

    # -------------------------------------------------------------------- missions

    @staticmethod
    def flight_legs(mission: Mission, start: Point3) -> list[tuple[str, Point3, Point3]]:
        """The straight segments PX4 will fly: climb, waypoint legs and (optionally) the return."""
        waypoints = mission.waypoints
        first = waypoints[0]
        legs: list[tuple[str, Point3, Point3]] = []
        here = start
        if start[2] < first.altitude_rel_m - 0.5 and start[2] < 1.0:
            # Taking off: PX4 climbs vertically to the first waypoint's altitude.
            top = (start[0], start[1], first.altitude_rel_m)
            legs.append(("The take-off climb", here, top))
            here = top
        for i, wp in enumerate(waypoints, start=1):
            there = (wp.east_m, wp.north_m, wp.altitude_rel_m)
            legs.append((f"The leg into waypoint {i}", here, there))
            here = there
        if mission.constraints.return_to_home_on_complete:
            altitude = max(here[2], RTL_RETURN_ALTITUDE_M)
            top = (here[0], here[1], altitude)
            over_home = (0.0, 0.0, altitude)
            legs.append(("The climb to return altitude", here, top))
            legs.append((f"The return leg at {altitude:.0f} m", top, over_home))
            legs.append(("The final descent to home", over_home, (0.0, 0.0, 0.0)))
        return legs

    def check_mission(self, mission: Mission, start: Point3) -> list[str]:
        """Human-readable clearance problems; empty when the whole route is clear."""
        issues = []
        for name, a, b in self.flight_legs(mission, start):
            conflict = self.first_conflict(a, b)
            if conflict is not None:
                e, n, alt = conflict.point
                issues.append(
                    f"{name} comes within {self.clearance_m:.0f} m of an obstacle "
                    f"at east {e:.0f} m, north {n:.0f} m, {alt:.0f} m altitude."
                )
        return issues

    def plan_mission(self, mission: Mission, start: Point3) -> Mission:
        """Return a copy of the mission with detour waypoints inserted around obstacles.

        Only the waypoint legs are re-routed. Take-off, the return leg and the
        landing are flown by PX4 on fixed paths, so a conflict there is an error.
        """
        planned: list[Waypoint] = []
        here = start
        if start[2] < mission.waypoints[0].altitude_rel_m - 0.5 and start[2] < 1.0:
            here = (start[0], start[1], mission.waypoints[0].altitude_rel_m)
        for i, wp in enumerate(mission.waypoints, start=1):
            there = (wp.east_m, wp.north_m, wp.altitude_rel_m)
            if self.first_conflict(here, there) is not None:
                for east, north, altitude in self._detour(here, there, i):
                    planned.append(
                        wp.model_copy(
                            update={
                                "east_m": round(east, 2),
                                "north_m": round(north, 2),
                                "altitude_rel_m": round(altitude, 2),
                                "hold_s": 0.0,
                            }
                        )
                    )
            planned.append(wp)
            here = there
        result = mission.model_copy(update={"waypoints": planned})
        remaining = self.check_mission(result, start)
        if remaining:
            raise PlanningError(" ".join(remaining))
        return result

    def _detour(self, a: Point3, b: Point3, waypoint_number: int) -> list[Point3]:
        low, high = min(a[2], b[2]), max(a[2], b[2])
        required = self.blocked_slice(low, high)
        start, goal = self.cell(a[0], a[1]), self.cell(b[0], b[1])
        if start is None or goal is None:
            raise PlanningError(f"Waypoint {waypoint_number} is outside the mapped area.")
        if required[goal]:
            raise PlanningError(
                f"Waypoint {waypoint_number} is within {self.clearance_m:.0f} m of an obstacle."
            )
        if required[start]:
            raise PlanningError(
                f"The leg into waypoint {waypoint_number} starts within "
                f"{self.clearance_m:.0f} m of an obstacle."
            )
        # Plan with extra room, but never let the extra room swallow the endpoints.
        blocked = self.blocked_slice(low, high, extra_m=self.planning_margin_m).copy()
        for cell in (start, goal):
            if blocked[cell]:
                x, y = cell
                reach = math.ceil(self.planning_margin_m / self.spec.resolution_m) + 1
                window = (
                    slice(max(0, x - reach), x + reach + 1),
                    slice(max(0, y - reach), y + reach + 1),
                )
                blocked[window] = required[window]
        path = astar(blocked, start, goal)
        if path is None:
            raise PlanningError(f"No obstacle-free route to waypoint {waypoint_number} was found.")
        corners = simplify(path, blocked)[1:-1]  # endpoints are the existing waypoints
        points = [self.cell_centre(c) for c in corners]
        # Spread the altitude change evenly along the detour.
        chain = [(a[0], a[1]), *points, (b[0], b[1])]
        lengths = [math.dist(chain[k], chain[k + 1]) for k in range(len(chain) - 1)]
        total = sum(lengths) or 1.0
        out, travelled = [], 0.0
        for k, (east, north) in enumerate(points):
            travelled += lengths[k]
            out.append((east, north, a[2] + (b[2] - a[2]) * travelled / total))
        return out
