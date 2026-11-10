"""PX4 software-in-the-loop adapter (MAVLink via MAVSDK).

Implements :class:`aegis.interfaces.VehicleTelemetrySource` and
:class:`aegis.interfaces.MissionExecutor` for a PX4 SITL instance, whichever
physics simulator sits behind it (Gazebo Harmonic, or PX4's built-in SIH).

Safety properties of this adapter:

* It **refuses every command** until it has read ``SYS_AUTOSTART`` from the
  autopilot and found it in the configured allow-list of simulation airframes.
  Telemetry is still shown for an unattested vehicle, but read-only.
* It never touches actuators. It only uses the autopilot's own mission, hold,
  return and land modes, so all PX4 failsafes stay in charge.
* The mission geofence is uploaded to PX4 so the flight controller enforces it;
  AEGIS only observes.

There is deliberately no physical-hardware counterpart in this release.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import time
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import mavsdk_grpc as mavsdk
from mavsdk_grpc.geofence import FenceType, GeofenceData, Point, Polygon
from mavsdk_grpc.mission import MissionItem, MissionPlan

from aegis import frames
from aegis.contracts import (
    Attitude,
    AutopilotMessage,
    BatteryState,
    GeodeticPosition,
    GpsQuality,
    HealthFlags,
    LocalOrigin,
    Provenance,
    Vec3,
    VehicleState,
)
from aegis.missions import (
    Mission,
    MissionPhase,
    MissionResult,
    MissionStatus,
    validate_mission,
)

log = logging.getLogger(__name__)

# PX4 SYS_AUTOSTART ids that only exist as simulation airframes
# (ROMFS/px4fmu_common/init.d-posix/airframes in PX4 v1.17).
DEFAULT_SIM_AIRFRAMES: dict[int, str] = {
    10040: "sih_quadx",
    4001: "gz_x500",
}

HEARTBEAT_TIMEOUT_S = 3.0
# PX4 validates an uploaded mission asynchronously; a start request sent too
# early is refused ("Switching to Mission is currently not available").
MISSION_START_ATTEMPTS = 5
MISSION_START_CONFIRM_S = 3.0


class NotAttestedError(RuntimeError):
    """Raised when a command is attempted before the simulator check has passed."""


class PX4SITLAdapter:
    provenance = Provenance.SIMULATED

    def __init__(
        self,
        vehicle_id: str = "sim-1",
        connection_url: str = "udpin://0.0.0.0:14540",
        grpc_port: int = 50051,
        sim_airframes: dict[int, str] | None = None,
    ) -> None:
        self.vehicle_id = vehicle_id
        self._url = connection_url
        self._grpc_port = grpc_port
        self._sim_airframes = sim_airframes or dict(DEFAULT_SIM_AIRFRAMES)

        self._system: Any = None
        self._tasks: list[asyncio.Task[None]] = []
        self._attested_airframe: str | None = None
        self._attestation_error: str | None = "Not connected to an autopilot yet."

        self._t: dict[str, Any] = {}  # latest raw telemetry by stream name
        self._last_rx = 0.0  # monotonic time of the last position/attitude sample
        self.messages: deque[AutopilotMessage] = deque(maxlen=50)  # PX4 status texts
        self._link_up = False

        self._status = MissionStatus()
        self._mission: Mission | None = None
        self._last_progress_at = 0.0
        self._command_lock = asyncio.Lock()

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        self._tasks.append(asyncio.create_task(self._run(), name="px4-connect"))

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._tasks.clear()
        if self._system is not None:
            # Terminate the bundled mavsdk_server child process.
            with contextlib.suppress(Exception):
                self._system._stop_mavsdk_server()
            self._system = None

    async def _run(self) -> None:
        self._system = mavsdk.System(port=self._grpc_port)
        log.info("Connecting to PX4 SITL at %s", self._url)
        await self._system.connect(system_address=self._url)
        tel = self._system.telemetry
        streams: dict[str, Callable[[], AsyncIterator[Any]]] = {
            "position": tel.position,
            "velocity": tel.velocity_ned,
            "quaternion": tel.attitude_quaternion,
            "battery": tel.battery,
            "gps_info": tel.gps_info,
            "flight_mode": tel.flight_mode,
            "armed": tel.armed,
            "in_air": tel.in_air,
            "landed_state": tel.landed_state,
            "health": tel.health,
            "home": tel.home,
        }
        for name, factory in streams.items():
            self._spawn(self._pump(name, factory), f"px4-{name}")
        self._spawn(self._watch_connection(), "px4-link")
        self._spawn(self._watch_status_text(), "px4-status-text")
        self._spawn(self._supervise_mission(), "px4-mission-supervisor")
        self._spawn(self._watch_mission_progress(), "px4-mission-progress")

    def _spawn(self, coro: Awaitable[None], name: str) -> None:
        self._tasks.append(asyncio.create_task(coro, name=name))  # type: ignore[arg-type]

    async def _pump(self, name: str, factory: Callable[[], AsyncIterator[Any]]) -> None:
        while True:
            try:
                async for sample in factory():
                    self._t[name] = sample
                    if name in ("position", "quaternion"):
                        self._last_rx = time.monotonic()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Telemetry stream %s failed; retrying", name)
            await asyncio.sleep(1.0)

    async def _watch_status_text(self) -> None:
        while True:
            try:
                async for text in self._system.telemetry.status_text():
                    message = AutopilotMessage(
                        vehicle_id=self.vehicle_id,
                        provenance=self.provenance,
                        level=text.type.name,
                        text=text.text.strip(),
                        received_at_unix_s=time.time(),
                    )
                    self.messages.append(message)
                    log.info("PX4> [%s] %s", message.level, message.text)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Status text stream failed; retrying")
            await asyncio.sleep(1.0)

    async def _watch_connection(self) -> None:
        async for state in self._system.core.connection_state():
            if state.is_connected and not self._link_up:
                self._link_up = True
                log.info("Autopilot heartbeat received")
                await self._attest_simulator()
            elif not state.is_connected and self._link_up:
                self._link_up = False
                self._attested_airframe = None
                self._attestation_error = "Link to the autopilot was lost."
                log.warning("Autopilot heartbeat lost")

    async def _attest_simulator(self) -> None:
        """Positively identify the autopilot as a simulation airframe before allowing commands."""
        try:
            autostart = await asyncio.wait_for(
                self._system.param.get_param_int("SYS_AUTOSTART"), timeout=10.0
            )
        except Exception as exc:
            self._attested_airframe = None
            self._attestation_error = f"Could not read SYS_AUTOSTART: {exc!r}"
            log.error(self._attestation_error)
            return
        name = self._sim_airframes.get(int(autostart))
        if name is None:
            self._attested_airframe = None
            self._attestation_error = (
                f"SYS_AUTOSTART={autostart} is not a known simulation airframe; "
                "commands are disabled (telemetry is read-only)."
            )
            log.error(self._attestation_error)
            return
        self._attested_airframe = name
        self._attestation_error = None
        log.info("Simulator attested: SYS_AUTOSTART=%s (%s)", autostart, name)

    # ------------------------------------------------------------------ telemetry

    @property
    def attested(self) -> bool:
        return self._attested_airframe is not None

    @property
    def attestation_error(self) -> str | None:
        return self._attestation_error

    @property
    def source(self) -> str:
        return f"px4_sitl:{self._attested_airframe or 'unattested'}"

    def _connected(self) -> bool:
        return self._link_up and (time.monotonic() - self._last_rx) < HEARTBEAT_TIMEOUT_S

    async def get_state(self) -> VehicleState:
        t = self._t
        connected = self._connected()

        origin: LocalOrigin | None = None
        if (home := t.get("home")) is not None:
            origin = LocalOrigin(
                geodetic=GeodeticPosition(
                    latitude_deg=home.latitude_deg,
                    longitude_deg=home.longitude_deg,
                    altitude_amsl_m=home.absolute_altitude_m,
                )
            )

        geodetic: GeodeticPosition | None = None
        rel_alt: float | None = None
        enu: Vec3 | None = None
        if (pos := t.get("position")) is not None and not math.isnan(pos.latitude_deg):
            geodetic = GeodeticPosition(
                latitude_deg=pos.latitude_deg,
                longitude_deg=pos.longitude_deg,
                altitude_amsl_m=pos.absolute_altitude_m,
            )
            rel_alt = pos.relative_altitude_m
            if origin is not None:
                o = origin.geodetic
                e, n, u = frames.geodetic_to_enu(
                    pos.latitude_deg,
                    pos.longitude_deg,
                    pos.absolute_altitude_m,
                    o.latitude_deg,
                    o.longitude_deg,
                    o.altitude_amsl_m,
                )
                enu = Vec3(x=e, y=n, z=u)

        velocity: Vec3 | None = None
        ground_speed: float | None = None
        vertical_up: float | None = None
        if (vel := t.get("velocity")) is not None:
            velocity = Vec3(x=vel.north_m_s, y=vel.east_m_s, z=vel.down_m_s)
            ground_speed = math.hypot(vel.north_m_s, vel.east_m_s)
            vertical_up = -vel.down_m_s

        attitude: Attitude | None = None
        vehicle_time: float | None = None
        if (q := t.get("quaternion")) is not None:
            roll, pitch, yaw = frames.quaternion_to_euler_deg(q.w, q.x, q.y, q.z)
            attitude = Attitude(
                quaternion_wxyz=(q.w, q.x, q.y, q.z), roll_deg=roll, pitch_deg=pitch, yaw_deg=yaw
            )
            if q.timestamp_us:
                vehicle_time = q.timestamp_us / 1e6

        battery: BatteryState | None = None
        if (bat := t.get("battery")) is not None:
            remaining = bat.remaining_percent
            # MAVSDK reports 0-100; guard against NaN and older 0-1 scaling.
            fraction = None
            if not math.isnan(remaining):
                fraction = remaining / 100.0 if remaining > 1.0 else remaining
                fraction = max(0.0, min(1.0, fraction))
            battery = BatteryState(
                voltage_v=None if math.isnan(bat.voltage_v) else bat.voltage_v,
                remaining_fraction=fraction,
            )

        gps: GpsQuality | None = None
        if (g := t.get("gps_info")) is not None:
            gps = GpsQuality(fix_type=g.fix_type.name, num_satellites=g.num_satellites)

        health: HealthFlags | None = None
        if (h := t.get("health")) is not None:
            health = HealthFlags(
                gyrometer_calibration_ok=h.is_gyrometer_calibration_ok,
                accelerometer_calibration_ok=h.is_accelerometer_calibration_ok,
                magnetometer_calibration_ok=h.is_magnetometer_calibration_ok,
                local_position_ok=h.is_local_position_ok,
                global_position_ok=h.is_global_position_ok,
                home_position_ok=h.is_home_position_ok,
                armable=h.is_armable,
            )

        mode = t.get("flight_mode")
        landed = t.get("landed_state")
        return VehicleState(
            vehicle_id=self.vehicle_id,
            provenance=self.provenance,
            source=self.source,
            received_at_unix_s=time.time(),
            vehicle_time_s=vehicle_time,
            connected=connected,
            armed=t.get("armed"),
            in_air=t.get("in_air"),
            flight_mode=mode.name if mode is not None else None,
            landed_state=landed.name if landed is not None else None,
            origin=origin,
            geodetic=geodetic,
            relative_altitude_m=rel_alt,
            position_enu_m=enu,
            velocity_ned_m_s=velocity,
            attitude=attitude,
            ground_speed_m_s=ground_speed,
            vertical_speed_up_m_s=vertical_up,
            battery=battery,
            gps=gps,
            health=health,
        )

    # ------------------------------------------------------------------- commands

    def _require_attested(self) -> None:
        if not self._connected():
            raise NotAttestedError("No live link to the autopilot.")
        if not self.attested:
            raise NotAttestedError(self._attestation_error or "Simulator not attested.")

    def _set_status(self, phase: MissionPhase, message: str = "", **changes: Any) -> None:
        self._status = self._status.model_copy(
            update={
                "phase": phase,
                "message": message,
                "updated_at_unix_s": time.time(),
                **changes,
            }
        )
        log.info("Mission %s -> %s %s", self._status.mission_id, phase.value, message)

    async def mission_status(self) -> MissionStatus:
        return self._status

    async def submit_mission(self, mission: Mission) -> MissionResult:
        async with self._command_lock:
            base = {
                "mission_id": mission.mission_id,
                "name": mission.name,
                "total_waypoints": len(mission.waypoints),
                "current_waypoint": 0,
                "issues": [],
            }
            if self._status.phase in (MissionPhase.UPLOADING, MissionPhase.ACTIVE):
                return MissionResult(
                    accepted=False,
                    status=self._status.model_copy(
                        update={"message": "Another mission is active; cancel it first."}
                    ),
                )
            try:
                self._require_attested()
            except NotAttestedError as exc:
                self._set_status(MissionPhase.REJECTED, str(exc), **base)
                return MissionResult(accepted=False, status=self._status)

            state = await self.get_state()
            if state.origin is None or state.position_enu_m is None:
                self._set_status(
                    MissionPhase.REJECTED, "No home position / position estimate yet.", **base
                )
                return MissionResult(accepted=False, status=self._status)

            start = (state.position_enu_m.x, state.position_enu_m.y)
            issues = validate_mission(mission, start_enu_m=start)
            if issues:
                self._set_status(
                    MissionPhase.REJECTED,
                    "Mission failed validation.",
                    **{**base, "issues": issues},
                )
                return MissionResult(accepted=False, status=self._status)

            self._set_status(MissionPhase.UPLOADING, "Uploading geofence and mission.", **base)
            try:
                await self._upload_and_start(mission, state.origin)
            except Exception as exc:
                self._set_status(MissionPhase.FAILED, f"Autopilot refused: {exc}")
                return MissionResult(accepted=False, status=self._status)

            self._mission = mission
            self._last_progress_at = time.monotonic()
            self._set_status(MissionPhase.ACTIVE, "Mission started.")
            return MissionResult(accepted=True, status=self._status)

    async def _upload_and_start(self, mission: Mission, origin: LocalOrigin) -> None:
        o = origin.geodetic
        c = mission.constraints

        fence_points = []
        for east, north in c.geofence.polygon_enu_m:
            lat, lon, _ = frames.enu_to_geodetic(
                east, north, 0.0, o.latitude_deg, o.longitude_deg, o.altitude_amsl_m
            )
            fence_points.append(Point(lat, lon))
        await self._system.geofence.upload_geofence(
            GeofenceData([Polygon(fence_points, FenceType.INCLUSION)], [])
        )

        items = []
        for wp in mission.waypoints:
            lat, lon, _ = frames.enu_to_geodetic(
                wp.east_m, wp.north_m, 0.0, o.latitude_deg, o.longitude_deg, o.altitude_amsl_m
            )
            items.append(
                MissionItem(
                    lat,
                    lon,
                    wp.altitude_rel_m,
                    wp.speed_m_s or c.cruise_speed_m_s,
                    wp.hold_s == 0.0,  # fly-through unless a hold is requested
                    float("nan"),
                    float("nan"),
                    MissionItem.CameraAction.NONE,
                    wp.hold_s if wp.hold_s > 0.0 else float("nan"),
                    float("nan"),
                    wp.acceptance_radius_m,
                    float("nan"),
                    float("nan"),
                    MissionItem.VehicleAction.NONE,
                )
            )
        await self._system.mission.set_return_to_launch_after_mission(c.return_to_home_on_complete)
        await self._system.mission.upload_mission(MissionPlan(items))

        # Do not trust the command ACK: confirm PX4 really entered Mission mode.
        for attempt in range(1, MISSION_START_ATTEMPTS + 1):
            await asyncio.sleep(1.0)
            if not self._t.get("armed"):
                await self._system.action.arm()
            await self._system.mission.start_mission()
            if await self._wait_for_flight_mode("MISSION", MISSION_START_CONFIRM_S):
                return
            log.warning("PX4 has not entered Mission mode yet (attempt %d)", attempt)
        with contextlib.suppress(Exception):
            if not self._t.get("in_air"):
                await self._system.action.disarm()
        latest = self.messages[-1].text if self.messages else "no autopilot message"
        raise RuntimeError(f"PX4 did not enter Mission mode ({latest})")

    async def _wait_for_flight_mode(self, name: str, timeout_s: float) -> bool:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            mode = self._t.get("flight_mode")
            if mode is not None and mode.name == name:
                return True
            await asyncio.sleep(0.1)
        return False

    async def _watch_mission_progress(self) -> None:
        while True:
            try:
                async for progress in self._system.mission.mission_progress():
                    if self._status.phase is not MissionPhase.ACTIVE:
                        continue
                    if progress.current != self._status.current_waypoint:
                        self._last_progress_at = time.monotonic()
                    done = progress.total > 0 and progress.current >= progress.total
                    if done:
                        rtl = bool(
                            self._mission and self._mission.constraints.return_to_home_on_complete
                        )
                        self._set_status(
                            MissionPhase.RETURNING if rtl else MissionPhase.COMPLETED,
                            "All waypoints reached; returning to home."
                            if rtl
                            else "All waypoints reached.",
                            current_waypoint=progress.current,
                        )
                    else:
                        self._set_status(
                            MissionPhase.ACTIVE,
                            f"Flying to waypoint {progress.current + 1} of {progress.total}.",
                            current_waypoint=progress.current,
                        )
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Mission progress stream failed; retrying")
            await asyncio.sleep(1.0)

    async def _supervise_mission(self) -> None:
        """Detect unreachable waypoints and the end of the return-to-home leg."""
        while True:
            await asyncio.sleep(1.0)
            phase = self._status.phase
            if phase is MissionPhase.ACTIVE and self._mission is not None:
                waited = time.monotonic() - self._last_progress_at
                if waited > self._mission.constraints.leg_timeout_s:
                    log.warning("Waypoint not reached in %.0f s; commanding hold", waited)
                    with contextlib.suppress(Exception):
                        await self._system.action.hold()
                    self._set_status(
                        MissionPhase.STALLED,
                        f"Waypoint {self._status.current_waypoint + 1} was not reached within "
                        f"{self._mission.constraints.leg_timeout_s:.0f} s; holding position.",
                    )
            elif (
                phase is MissionPhase.RETURNING
                and self._t.get("armed") is False
                and self._t.get("in_air") is False
            ):
                self._set_status(MissionPhase.COMPLETED, "Landed at home and disarmed.")

    async def _intervene(self, action: Callable[[], Awaitable[None]], message: str) -> None:
        self._require_attested()
        await action()
        if self._status.phase in (
            MissionPhase.ACTIVE,
            MissionPhase.RETURNING,
            MissionPhase.STALLED,
            MissionPhase.UPLOADING,
        ):
            self._set_status(MissionPhase.CANCELLED, message)

    async def hold(self) -> None:
        await self._intervene(self._system.action.hold, "Operator commanded hold.")

    async def return_to_home(self) -> None:
        await self._intervene(
            self._system.action.return_to_launch, "Operator commanded return to home."
        )

    async def land(self) -> None:
        await self._intervene(self._system.action.land, "Operator commanded land.")
