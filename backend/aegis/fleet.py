"""Several simulated vehicles under one operator.

The fleet keeps one telemetry hub per vehicle and adds what only matters when
there is more than one aircraft:

* a shared local frame, so every position and mission means the same place;
* a pre-flight check that a new mission does not cross a route another vehicle
  is already flying at a similar height;
* a separation monitor that warns when two airborne vehicles get close and, when
  they get very close, asks PX4 to Hold both of them once and leaves the rest to
  the operator.

Vehicles never coordinate on their own and are never steered toward anything:
each flies only missions the operator submitted, and the single automatic action
is to stop.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
from dataclasses import dataclass
from typing import Any

from aegis.contracts import Severity
from aegis.hub import TelemetryHub
from aegis.missions import Mission, MissionPhase
from aegis.safety import Finding

log = logging.getLogger(__name__)

Point3 = tuple[float, float, float]
_FLYING = {MissionPhase.UPLOADING, MissionPhase.ACTIVE}


@dataclass(frozen=True)
class SeparationLimits:
    # Two vehicles count as close only when BOTH distances are under the limit.
    warn_horizontal_m: float = 12.0
    warn_vertical_m: float = 6.0
    hold_horizontal_m: float = 6.0
    hold_vertical_m: float = 4.0
    # Routes: legs nearer than this horizontally must differ in height by `route_vertical_m`.
    route_horizontal_m: float = 10.0
    route_vertical_m: float = 5.0


@dataclass(frozen=True)
class PairSeparation:
    a: str
    b: str
    horizontal_m: float
    vertical_m: float


def _segment_distance_2d(
    p1: tuple[float, float],
    p2: tuple[float, float],
    q1: tuple[float, float],
    q2: tuple[float, float],
) -> float:
    """Shortest horizontal distance between two line segments."""

    def point_to_segment(
        p: tuple[float, float], a: tuple[float, float], b: tuple[float, float]
    ) -> float:
        ax, ay = b[0] - a[0], b[1] - a[1]
        length2 = ax * ax + ay * ay
        if length2 == 0.0:
            return math.dist(p, a)
        t = max(0.0, min(1.0, ((p[0] - a[0]) * ax + (p[1] - a[1]) * ay) / length2))
        return math.dist(p, (a[0] + t * ax, a[1] + t * ay))

    def side(o: tuple[float, float], a: tuple[float, float], b: tuple[float, float]) -> float:
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    d1, d2, d3, d4 = side(q1, q2, p1), side(q1, q2, p2), side(p1, p2, q1), side(p1, p2, q2)
    if d1 * d2 < 0.0 and d3 * d4 < 0.0:
        return 0.0  # they cross
    return min(
        point_to_segment(p1, q1, q2),
        point_to_segment(p2, q1, q2),
        point_to_segment(q1, p1, p2),
        point_to_segment(q2, p1, p2),
    )


def route_legs(mission: Mission, start: Point3 | None) -> list[tuple[Point3, Point3]]:
    """A mission's legs as 3D segments, with the climb-out from ``start`` when known."""
    points: list[Point3] = [(w.east_m, w.north_m, w.altitude_rel_m) for w in mission.waypoints]
    if start is not None:
        points.insert(0, (start[0], start[1], points[0][2]))
    return list(zip(points[:-1], points[1:], strict=True))


def route_conflicts(
    legs_a: list[tuple[Point3, Point3]],
    legs_b: list[tuple[Point3, Point3]],
    limits: SeparationLimits,
) -> tuple[float, float] | None:
    """The closest approach (horizontal, vertical) of two routes if it breaks the limits.

    Timing is ignored: two legs that pass near each other count as a conflict whenever
    they are flown. That refuses some routes that would have been safe; it never
    accepts one on the hope that the vehicles miss each other in time.
    """
    worst: tuple[float, float] | None = None
    for a1, a2 in legs_a:
        for b1, b2 in legs_b:
            horizontal = _segment_distance_2d(a1[:2], a2[:2], b1[:2], b2[:2])
            if horizontal >= limits.route_horizontal_m:
                continue
            # Height bands of the two legs; their gap is the vertical separation.
            lo_a, hi_a = min(a1[2], a2[2]), max(a1[2], a2[2])
            lo_b, hi_b = min(b1[2], b2[2]), max(b1[2], b2[2])
            vertical = max(0.0, lo_a - hi_b, lo_b - hi_a)
            if vertical < limits.route_vertical_m and (worst is None or horizontal < worst[0]):
                worst = (horizontal, vertical)
    return worst


class Fleet:
    def __init__(
        self,
        hubs: list[TelemetryHub],
        limits: SeparationLimits | None = None,
        auto_hold: bool = True,
        return_altitude_base_m: float = 30.0,
        return_altitude_step_m: float = 6.0,
    ) -> None:
        if not hubs:
            raise ValueError("A fleet needs at least one vehicle.")
        self.hubs = hubs
        self.primary = hubs[0]
        self.limits = limits or SeparationLimits()
        self.auto_hold = auto_hold
        self.holds_commanded = 0
        self.min_separation_seen: PairSeparation | None = None
        self._held_pairs: set[tuple[str, str]] = set()
        self._task: asyncio.Task[None] | None = None
        for index, hub in enumerate(hubs):
            if hasattr(hub.source, "return_altitude_m"):
                hub.source.return_altitude_m = (
                    return_altitude_base_m + index * return_altitude_step_m
                )

    # ------------------------------------------------------------------ lifecycle

    @property
    def ids(self) -> list[str]:
        return [hub.source.vehicle_id for hub in self.hubs]

    def hub(self, vehicle_id: str) -> TelemetryHub | None:
        return next((h for h in self.hubs if h.source.vehicle_id == vehicle_id), None)

    async def start(self) -> None:
        """Start the vehicles after the first (the application starts that one) and the monitor."""
        for hub in self.hubs[1:]:
            await hub.start()
        self._task = asyncio.create_task(self._monitor(), name="fleet-monitor")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
            self._task = None
        for hub in self.hubs[1:]:
            await hub.stop()

    async def _monitor(self) -> None:
        while True:
            try:
                self._share_frame()
                await self.check_separation()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Fleet monitor step failed")
            await asyncio.sleep(0.2)

    def _share_frame(self) -> None:
        """Give every vehicle the first vehicle's home as the origin of its local frame."""
        state = self.primary.state
        if state is None or state.origin is None:
            return
        for hub in self.hubs[1:]:
            if hasattr(hub.source, "frame_origin") and hub.source.frame_origin is None:
                hub.source.frame_origin = state.origin.geodetic
                log.info(
                    "%s now reports positions in %s's home frame",
                    hub.source.vehicle_id,
                    self.primary.source.vehicle_id,
                )

    # ----------------------------------------------------------------- separation

    def separations(self) -> list[PairSeparation]:
        """Distances between every pair of vehicles that are both in the air."""
        airborne = [
            (hub.source.vehicle_id, hub.state.position_enu_m)
            for hub in self.hubs
            if hub.state is not None
            and hub.state.connected
            and hub.state.in_air
            and hub.state.position_enu_m is not None
        ]
        pairs = []
        for i, (id_a, a) in enumerate(airborne):
            for id_b, b in airborne[i + 1 :]:
                pairs.append(
                    PairSeparation(id_a, id_b, math.hypot(a.x - b.x, a.y - b.y), abs(a.z - b.z))
                )
        return pairs

    def _too_close(self, pair: PairSeparation, horizontal_m: float, vertical_m: float) -> bool:
        return pair.horizontal_m < horizontal_m and pair.vertical_m < vertical_m

    async def check_separation(self) -> None:
        """One monitor step (public for tests): track the closest approach, Hold when too close."""
        limits = self.limits
        for pair in self.separations():
            seen = self.min_separation_seen
            if seen is None or math.hypot(pair.horizontal_m, pair.vertical_m) < math.hypot(
                seen.horizontal_m, seen.vertical_m
            ):
                self.min_separation_seen = pair
            key = (pair.a, pair.b)
            if self._too_close(pair, limits.hold_horizontal_m, limits.hold_vertical_m):
                if self.auto_hold and key not in self._held_pairs:
                    self._held_pairs.add(key)
                    await self._hold_pair(pair)
            elif not self._too_close(pair, limits.warn_horizontal_m, limits.warn_vertical_m):
                self._held_pairs.discard(key)  # they have separated: re-arm for a later encounter

    async def _hold_pair(self, pair: PairSeparation) -> None:
        self.holds_commanded += 1
        log.warning(
            "Separation: %s and %s are %.1f m apart (%.1f m vertically); commanding Hold for both",
            pair.a,
            pair.b,
            pair.horizontal_m,
            pair.vertical_m,
        )
        for vehicle_id, other in ((pair.a, pair.b), (pair.b, pair.a)):
            hub = self.hub(vehicle_id)
            if hub is None or hub.executor is None:
                continue
            try:
                await hub.executor.hold(
                    reason=f"Too close to {other} ({pair.horizontal_m:.0f} m). "
                    "Holding; the operator must resolve it."
                )
            except Exception:
                log.exception("Could not command Hold for %s", vehicle_id)

    def findings(self) -> list[Finding]:
        found: list[Finding] = []
        limits = self.limits
        for pair in self.separations():
            text = (
                f"{pair.a} and {pair.b} are {pair.horizontal_m:.0f} m apart "
                f"({pair.vertical_m:.0f} m vertically)."
            )
            if self._too_close(pair, limits.hold_horizontal_m, limits.hold_vertical_m):
                found.append(("fleet.separation_lost", Severity.CRITICAL, text))
            elif self._too_close(pair, limits.warn_horizontal_m, limits.warn_vertical_m):
                found.append(("fleet.separation_low", Severity.WARNING, text))
        return found

    # --------------------------------------------------------------------- routes

    def route_issues(self, vehicle_id: str, mission: Mission) -> list[str]:
        """Why ``mission`` cannot be flown by ``vehicle_id`` alongside the others' routes."""
        own = self.hub(vehicle_id)
        start: Point3 | None = None
        if own is not None and own.state is not None and own.state.position_enu_m is not None:
            p = own.state.position_enu_m
            start = (p.x, p.y, max(p.z, 0.0))
        legs = route_legs(mission, start)
        issues = []
        for hub in self.hubs:
            other_id = hub.source.vehicle_id
            if other_id == vehicle_id or hub.active_mission is None:
                continue
            if hub.mission.phase not in _FLYING:
                continue
            clash = route_conflicts(legs, route_legs(hub.active_mission, None), self.limits)
            if clash is not None:
                issues.append(
                    f"The route comes within {clash[0]:.0f} m of the route {other_id} is flying "
                    f"with only {clash[1]:.0f} m of height between them (needs "
                    f"{self.limits.route_horizontal_m:.0f} m apart or "
                    f"{self.limits.route_vertical_m:.0f} m of height)."
                )
        return issues

    # --------------------------------------------------------------------- status

    def status(self) -> dict[str, Any]:
        seen = self.min_separation_seen
        return {
            "enabled": len(self.hubs) > 1,
            "size": len(self.hubs),
            "auto_hold": self.auto_hold,
            "holds_commanded": self.holds_commanded,
            "limits": {
                "warn_horizontal_m": self.limits.warn_horizontal_m,
                "warn_vertical_m": self.limits.warn_vertical_m,
                "hold_horizontal_m": self.limits.hold_horizontal_m,
                "hold_vertical_m": self.limits.hold_vertical_m,
            },
            "separations": [
                {
                    "a": p.a,
                    "b": p.b,
                    "horizontal_m": round(p.horizontal_m, 1),
                    "vertical_m": round(p.vertical_m, 1),
                }
                for p in self.separations()
            ],
            "closest_approach": None
            if seen is None
            else {
                "a": seen.a,
                "b": seen.b,
                "horizontal_m": round(seen.horizontal_m, 1),
                "vertical_m": round(seen.vertical_m, 1),
            },
            "vehicles": [self.vehicle_summary(hub) for hub in self.hubs],
        }

    def vehicle_summary(self, hub: TelemetryHub) -> dict[str, Any]:
        source = hub.source
        return {
            "vehicle_id": source.vehicle_id,
            "provenance": source.provenance.value,
            "source": getattr(source, "source", None),
            "simulator_attested": getattr(source, "attested", None),
            "return_altitude_m": getattr(source, "return_altitude_m", None),
            "state": hub.state.model_dump(mode="json") if hub.state else None,
            "mission": hub.mission.model_dump(mode="json"),
            "alerts": [a.model_dump(mode="json") for a in hub.alerts.active],
        }

    def snapshot_extra(self) -> dict[str, Any]:
        """Compact per-vehicle data for the live telemetry stream (vehicles after the first)."""
        return {"fleet": [self.vehicle_summary(hub) for hub in self.hubs[1:]]}
