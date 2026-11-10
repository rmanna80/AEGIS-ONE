"""Waypoint missions: models, geofence geometry and pre-flight validation.

Missions are authored in the local **ENU** frame relative to the vehicle's home
position (east/north in metres, altitude in metres above home). Adapters convert
to WGS84 at upload time with :mod:`aegis.frames`.
"""

from __future__ import annotations

import math
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

Point2 = tuple[float, float]  # (east_m, north_m)


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Waypoint(_Model):
    east_m: float
    north_m: float
    altitude_rel_m: float = Field(gt=0.0, description="Metres above home.")
    acceptance_radius_m: float = Field(default=2.0, gt=0.0, le=50.0)
    speed_m_s: float | None = Field(default=None, gt=0.0, le=20.0)
    hold_s: float = Field(default=0.0, ge=0.0, le=600.0)


class Geofence(_Model):
    """Inclusion fence: a simple polygon in ENU plus an altitude ceiling."""

    polygon_enu_m: list[Point2] = Field(min_length=3, max_length=64)
    max_altitude_rel_m: float = Field(gt=0.0, le=120.0)


class MissionConstraints(_Model):
    geofence: Geofence
    min_altitude_rel_m: float = Field(default=2.0, ge=0.0)
    cruise_speed_m_s: float = Field(default=5.0, gt=0.0, le=20.0)
    max_leg_length_m: float = Field(default=500.0, gt=0.0)
    leg_timeout_s: float = Field(default=120.0, gt=0.0)
    return_to_home_on_complete: bool = True


class Mission(_Model):
    mission_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    name: str = Field(min_length=1, max_length=120)
    waypoints: list[Waypoint] = Field(min_length=1, max_length=100)
    constraints: MissionConstraints


class MissionPhase(StrEnum):
    IDLE = "idle"
    UPLOADING = "uploading"
    ACTIVE = "active"
    RETURNING = "returning"
    COMPLETED = "completed"
    CANCELLED = "cancelled"  # operator intervened (hold / RTL / land)
    STALLED = "stalled"  # a waypoint was not reached within its leg timeout
    FAILED = "failed"
    REJECTED = "rejected"  # failed validation or authorisation; nothing was sent


class MissionStatus(_Model):
    mission_id: str | None = None
    name: str | None = None
    phase: MissionPhase = MissionPhase.IDLE
    current_waypoint: int = 0  # number of waypoints reached so far
    total_waypoints: int = 0
    message: str = ""
    issues: list[str] = Field(default_factory=list)
    updated_at_unix_s: float = 0.0


class MissionResult(_Model):
    accepted: bool
    status: MissionStatus


# --------------------------------------------------------------------------- geometry


def point_in_polygon(point: Point2, polygon: list[Point2]) -> bool:
    """Ray-casting test. Points exactly on an edge count as inside."""
    x, y = point
    inside = False
    n = len(polygon)
    for i in range(n):
        x1, y1 = polygon[i]
        x2, y2 = polygon[(i + 1) % n]
        if _on_segment(point, (x1, y1), (x2, y2)):
            return True
        if (y1 > y) != (y2 > y):
            x_cross = x1 + (y - y1) * (x2 - x1) / (y2 - y1)
            if x < x_cross:
                inside = not inside
    return inside


def _cross(o: Point2, a: Point2, b: Point2) -> float:
    return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])


def _on_segment(p: Point2, a: Point2, b: Point2, eps: float = 1e-9) -> bool:
    if abs(_cross(a, b, p)) > eps * max(1.0, math.dist(a, b)):
        return False
    return (
        min(a[0], b[0]) - eps <= p[0] <= max(a[0], b[0]) + eps
        and min(a[1], b[1]) - eps <= p[1] <= max(a[1], b[1]) + eps
    )


def segments_properly_intersect(p1: Point2, p2: Point2, q1: Point2, q2: Point2) -> bool:
    """True when the open segments cross at a single interior point."""
    d1, d2 = _cross(q1, q2, p1), _cross(q1, q2, p2)
    d3, d4 = _cross(p1, p2, q1), _cross(p1, p2, q2)
    return (d1 * d2 < 0.0) and (d3 * d4 < 0.0)


def segment_inside_polygon(a: Point2, b: Point2, polygon: list[Point2]) -> bool:
    """Whether the whole straight leg a->b stays inside a simple polygon."""
    if not (point_in_polygon(a, polygon) and point_in_polygon(b, polygon)):
        return False
    n = len(polygon)
    for i in range(n):
        if segments_properly_intersect(a, b, polygon[i], polygon[(i + 1) % n]):
            return False
    # Concave fences: both ends inside and no proper crossing can still leave the
    # polygon through a vertex; the midpoint check catches the common cases.
    mid = ((a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0)
    return point_in_polygon(mid, polygon)


def distance_to_polygon_edge_m(point: Point2, polygon: list[Point2]) -> float:
    """Shortest distance from a point to the polygon boundary."""
    best = math.inf
    n = len(polygon)
    for i in range(n):
        a, b = polygon[i], polygon[(i + 1) % n]
        abx, aby = b[0] - a[0], b[1] - a[1]
        length_sq = abx * abx + aby * aby
        t = 0.0
        if length_sq > 0.0:
            t = ((point[0] - a[0]) * abx + (point[1] - a[1]) * aby) / length_sq
            t = max(0.0, min(1.0, t))
        best = min(best, math.dist(point, (a[0] + t * abx, a[1] + t * aby)))
    return best


# ------------------------------------------------------------------------- validation


def validate_mission(mission: Mission, start_enu_m: Point2 = (0.0, 0.0)) -> list[str]:
    """Return human-readable problems. An empty list means the mission may be uploaded.

    ``start_enu_m`` is where the vehicle is now (home is the ENU origin), so the
    first leg and the return-to-home leg are checked too.
    """
    issues: list[str] = []
    c = mission.constraints
    fence = c.geofence.polygon_enu_m

    if not point_in_polygon((0.0, 0.0), fence):
        issues.append("Home position (ENU origin) is outside the geofence.")
    if not point_in_polygon(start_enu_m, fence):
        issues.append("Vehicle's current position is outside the geofence.")
    if c.min_altitude_rel_m >= c.geofence.max_altitude_rel_m:
        issues.append("Minimum altitude is not below the geofence ceiling.")

    previous = start_enu_m
    for i, wp in enumerate(mission.waypoints, start=1):
        here = (wp.east_m, wp.north_m)
        if wp.altitude_rel_m < c.min_altitude_rel_m:
            issues.append(
                f"Waypoint {i}: altitude {wp.altitude_rel_m:.1f} m is below the "
                f"minimum {c.min_altitude_rel_m:.1f} m."
            )
        if wp.altitude_rel_m > c.geofence.max_altitude_rel_m:
            issues.append(
                f"Waypoint {i}: altitude {wp.altitude_rel_m:.1f} m exceeds the "
                f"geofence ceiling {c.geofence.max_altitude_rel_m:.1f} m."
            )
        if not point_in_polygon(here, fence):
            issues.append(f"Waypoint {i} is outside the geofence.")
        elif not segment_inside_polygon(previous, here, fence):
            issues.append(f"The leg into waypoint {i} leaves the geofence.")
        leg = math.dist(previous, here)
        if leg > c.max_leg_length_m:
            issues.append(
                f"The leg into waypoint {i} is {leg:.0f} m, longer than the "
                f"{c.max_leg_length_m:.0f} m limit."
            )
        previous = here

    if c.return_to_home_on_complete and not segment_inside_polygon(previous, (0.0, 0.0), fence):
        issues.append("The return-to-home leg leaves the geofence.")
    return issues
