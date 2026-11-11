"""Navigation service: keeps the occupancy map up to date from the depth source,
answers route questions, and watches the route ahead during a mission.

Automation boundary: the only thing this service ever asks the vehicle to do is
enter PX4's Hold mode (stop and hover), when the route ahead is blocked or depth
sensing has gone stale during a mission. It never steers. Continuing afterwards
is always an operator decision.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import time
from pathlib import Path
from typing import Any

import numpy as np

from aegis.contracts import Severity
from aegis.hub import TelemetryHub
from aegis.missions import Mission, MissionPhase
from aegis.navigation.depth import DepthFrame, DepthSource, pose_from_state
from aegis.navigation.geometry import SceneGeometry
from aegis.navigation.occupancy import GridSpec, OccupancyGrid
from aegis.navigation.planner import Point3, RouteChecker
from aegis.safety import Finding

log = logging.getLogger(__name__)

DEPTH_STALE_S = 2.0  # in simulation seconds
LOOKAHEAD_M = 30.0
MAX_VOXELS_SENT = 30000


def grid_spec_for(scenario: dict[str, Any], resolution_m: float) -> GridSpec:
    fence = scenario["demo_mission"]["constraints"]["geofence"]
    east = [p[0] for p in fence["polygon_enu_m"]]
    north = [p[1] for p in fence["polygon_enu_m"]]
    return GridSpec.around(
        (min(east), min(north), max(east), max(north)),
        ceiling_m=fence["max_altitude_rel_m"],
        resolution_m=resolution_m,
    )


class NavigationService:
    def __init__(
        self,
        hub: TelemetryHub,
        scenario: dict[str, Any],
        depth: DepthSource | None,
        resolution_m: float = 0.5,
        clearance_m: float = 3.0,
        use_prior_map: bool = True,
        auto_hold: bool = True,
        save_dir: Path | None = None,
    ) -> None:
        self.hub = hub
        self.scenario_id = str(scenario["scenario_id"])
        self.scene = SceneGeometry(scenario)
        self.depth = depth
        self.auto_hold = auto_hold
        self.save_dir = save_dir
        spec = grid_spec_for(scenario, resolution_m)
        self.live = OccupancyGrid(spec)
        self.truth = OccupancyGrid(spec)
        self.truth.fill_from_scene(self.scene)
        self.use_prior_map = use_prior_map
        grids = [self.live, self.truth] if use_prior_map else [self.live]
        self.checker = RouteChecker(grids, clearance_m=clearance_m)
        self._tasks: list[asyncio.Task[None]] = []
        self._last_frame_at = 0.0
        self._last_frame: DepthFrame | None = None
        self._findings: list[Finding] = []
        self._held_for: str | None = None  # mission id already put on hold by this service
        self.nearest_obstacle_m: float | None = None
        self.last_error: str | None = None

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        if self.depth is not None:
            await self.depth.start()
            self._tasks.append(asyncio.create_task(self._map_loop(), name="nav-mapping"))
        self._tasks.append(asyncio.create_task(self._monitor_loop(), name="nav-monitor"))

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._tasks.clear()
        if self.depth is not None:
            await self.depth.stop()
        if self.save_dir is not None and self.live.frames_integrated > 0:
            self.save_map(self.save_dir / "map.npz")

    # -------------------------------------------------------------------- mapping

    async def _map_loop(self) -> None:
        assert self.depth is not None
        while True:
            try:
                frame = await self.depth.read_frame()
                if frame is None:
                    await asyncio.sleep(0.05)
                    continue
                pose = frame.pose
                if pose is None and self.hub.state is not None and self.hub.state.connected:
                    # Stream sources do not know the vehicle pose; pair the frame with
                    # the latest telemetry (see docs: this ignores transport latency).
                    pose = pose_from_state(self.hub.state)
                if pose is None:
                    continue
                await asyncio.to_thread(self.live.integrate, frame, pose)
                self._last_frame = frame
                self._last_frame_at = time.monotonic()
                self.last_error = None
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = repr(exc)
                log.exception("Depth frame could not be integrated")
                await asyncio.sleep(0.5)

    def depth_age_s(self) -> float | None:
        return time.monotonic() - self._last_frame_at if self._last_frame_at else None

    # -------------------------------------------------------------------- monitor

    async def _monitor_loop(self) -> None:
        while True:
            await asyncio.sleep(0.5)
            try:
                await self.monitor_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Navigation monitor failed")

    def _route_ahead(
        self, mission: Mission, reached: int, here: Point3
    ) -> list[tuple[Point3, Point3]]:
        legs = []
        current = here
        for wp in mission.waypoints[reached:]:
            nxt = (wp.east_m, wp.north_m, wp.altitude_rel_m)
            legs.append((current, nxt))
            current = nxt
        return legs

    async def monitor_once(self) -> None:
        """Re-evaluate obstacle proximity and the route ahead (public for tests)."""
        findings: list[Finding] = []
        state = self.hub.state
        mission_status = self.hub.mission
        mission = self.hub.active_mission
        if state is None or not state.connected or state.position_enu_m is None:
            self._findings = findings
            return
        p = state.position_enu_m
        here: Point3 = (p.x, p.y, max(p.z, 0.0))

        self.nearest_obstacle_m = await asyncio.to_thread(self.live.nearest_obstacle_m, here)
        if state.in_air and self.nearest_obstacle_m is not None:
            if self.nearest_obstacle_m < self.checker.clearance_m / 2.0:
                findings.append(
                    (
                        "obstacle.close",
                        Severity.CRITICAL,
                        f"Mapped obstacle {self.nearest_obstacle_m:.1f} m from the vehicle.",
                    )
                )
            elif self.nearest_obstacle_m < self.checker.clearance_m:
                findings.append(
                    (
                        "obstacle.near",
                        Severity.WARNING,
                        f"Mapped obstacle {self.nearest_obstacle_m:.1f} m from the vehicle.",
                    )
                )

        active = mission_status.phase is MissionPhase.ACTIVE and mission is not None
        if active and mission is not None:
            hold_reason: str | None = None

            if self.depth is not None:
                rtf = state.sim_real_time_factor or 1.0
                limit = DEPTH_STALE_S / max(0.05, min(1.0, rtf))
                age = self.depth_age_s()
                if age is None or age > limit:
                    findings.append(
                        (
                            "depth.stale",
                            Severity.CRITICAL,
                            "Depth sensing has stopped; obstacles ahead can no longer be seen.",
                        )
                    )
                    hold_reason = "Depth sensing stopped during the mission; holding position."

            travelled = 0.0
            for a, b in self._route_ahead(mission, mission_status.current_waypoint, here):
                conflict = await asyncio.to_thread(
                    self.checker.first_conflict, a, b, LOOKAHEAD_M - travelled
                )
                if conflict is not None:
                    distance = travelled + conflict.distance_along_m
                    e, n, alt = conflict.point
                    findings.append(
                        (
                            "route.blocked",
                            Severity.CRITICAL,
                            f"Route blocked {distance:.0f} m ahead by an obstacle near "
                            f"east {e:.0f} m, north {n:.0f} m at {alt:.0f} m.",
                        )
                    )
                    hold_reason = (
                        f"Route blocked {distance:.0f} m ahead (obstacle near east {e:.0f} m, "
                        f"north {n:.0f} m); holding. Plan a detour or intervene."
                    )
                    break
                travelled += math.dist(a, b)
                if travelled >= LOOKAHEAD_M:
                    break

            if (
                hold_reason is not None
                and self.auto_hold
                and self.hub.executor is not None
                and self._held_for != mission.mission_id
            ):
                self._held_for = mission.mission_id
                log.warning("Automatic hold: %s", hold_reason)
                try:
                    await self.hub.executor.hold(reason=hold_reason)
                except Exception:
                    log.exception("Automatic hold could not be commanded")
        elif mission_status.phase in (MissionPhase.IDLE, MissionPhase.COMPLETED):
            self._held_for = None

        self._findings = findings

    def findings(self) -> list[Finding]:
        """Latest findings, for the telemetry hub's alert tracker."""
        return list(self._findings)

    # ----------------------------------------------------------------- operations

    def vehicle_point(self) -> Point3:
        state = self.hub.state
        if state is None or state.position_enu_m is None:
            return (0.0, 0.0, 0.0)
        p = state.position_enu_m
        return (p.x, p.y, max(p.z, 0.0))

    def save_map(self, path: Path) -> Path:
        self.live.save(path, {"scenario": self.scenario_id, "saved_at_unix_s": time.time()})
        log.info("Saved occupancy map to %s", path)
        return path

    def load_map(self, path: Path) -> None:
        loaded = OccupancyGrid.load(path)
        if loaded.spec != self.live.spec:
            raise ValueError(f"{path} was made for a different scenario or resolution")
        self.live.log_odds[...] = loaded.log_odds
        self.live.frames_integrated = loaded.frames_integrated
        self.live.revision += 1

    def accuracy(self) -> dict[str, Any]:
        """Score the reconstructed map against the scenario's ground-truth geometry."""
        report = self.live.compare_with(self.truth)
        report["scenario"] = self.scenario_id
        report["frames_integrated"] = self.live.frames_integrated
        report["ground_truth"] = "scenario geometry (obstacles and staged props)"
        return report

    def map_payload(self, since_revision: int = -1) -> dict[str, Any]:
        revision = self.live.revision
        if revision == since_revision:
            return {"revision": revision, "changed": False}
        centres = self.live.occupied_centres()
        total = len(centres)
        if total > MAX_VOXELS_SENT:
            centres = centres[:: math.ceil(total / MAX_VOXELS_SENT)]
        return {
            "revision": revision,
            "changed": True,
            "resolution_m": self.live.spec.resolution_m,
            "occupied_voxels": total,
            "sent_voxels": len(centres),
            # flat [east, north, up, east, north, up, ...] in metres
            "voxels": np.round(centres, 2).reshape(-1).tolist(),
        }

    def status(self) -> dict[str, Any]:
        frame = self._last_frame
        return {
            "enabled": True,
            "depth_source": frame.source if frame else getattr(self.depth, "source", None),
            "depth_provenance": frame.provenance.value if frame else None,
            "depth_age_s": self.depth_age_s(),
            "frames_integrated": self.live.frames_integrated,
            "occupied_voxels": int(np.count_nonzero(self.live.occupied_mask)),
            "map_revision": self.live.revision,
            "resolution_m": self.live.spec.resolution_m,
            "clearance_m": self.checker.clearance_m,
            "uses_prior_map": self.use_prior_map,
            "auto_hold": self.auto_hold,
            "nearest_obstacle_m": self.nearest_obstacle_m,
            "last_error": self.last_error,
        }
