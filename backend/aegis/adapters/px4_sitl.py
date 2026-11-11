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
from collections.abc import Awaitable, Callable
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

# Simulator attestation table.
#
# SYS_AUTOSTART alone is NOT sufficient: PX4's hardware firmware reuses some of
# the same numbers (4001 is "Generic Quadcopter" on a real Pixhawk, and
# "gz_x500" in SITL). Each entry therefore also lists *evidence parameters* that
# only exist in POSIX simulation builds of PX4:
#   SIM_BAT_ENABLE - battery simulator, selected by COMMON_SIMULATION, which
#                    Kconfig restricts to PLATFORM_POSIX
#   SIM_GZ_EN      - Gazebo bridge module; set to 1 by the gz airframes
# A parameter that does not exist cannot be read, so hardware firmware fails
# the check. ``None`` means "must exist"; a number means "must equal".
Evidence = tuple[tuple[str, int | None], ...]
_POSIX_SIM: Evidence = (("SIM_BAT_ENABLE", None),)
_GAZEBO: Evidence = (*_POSIX_SIM, ("SIM_GZ_EN", 1))
DEFAULT_SIM_AIRFRAMES: dict[int, tuple[str, Evidence]] = {
    10040: ("sih_quadx", _POSIX_SIM),
    4001: ("gz_x500", _GAZEBO),
    4002: ("gz_x500_depth", _GAZEBO),
    4010: ("gz_x500_mono_cam", _GAZEBO),
}

# Link loss is judged from telemetry freshness, in *simulation* seconds: a heavy
# simulation (Gazebo rendering cameras) can run well below real time, and a
# wall-clock timeout would then report a healthy link as lost.
LINK_TIMEOUT_S = 3.0
MIN_REAL_TIME_FACTOR = 0.05  # below this the timeout stops stretching (60 s wall)
RTF_WINDOW_S = 5.0
# PX4 validates an uploaded mission asynchronously; a start request sent too
# early is refused ("Switching to Mission is currently not available").
ATTESTATION_RETRY_S = 2.0
ATTESTATION_RECHECK_S = 15.0
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
        sim_airframes: dict[int, tuple[str, Evidence]] | None = None,
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
        self._link_was_up = False
        self._mavsdk_connected = False  # MAVSDK's own heartbeat state (3 s wall clock)
        self._restarting = False
        self.bridge_restarts = 0
        # (wall monotonic s, vehicle clock s) samples for the real-time-factor estimate
        self._clock_samples: deque[tuple[float, float]] = deque()

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
        streams = {
            "position": "position",
            "velocity": "velocity_ned",
            "quaternion": "attitude_quaternion",
            "battery": "battery",
            "gps_info": "gps_info",
            "flight_mode": "flight_mode",
            "armed": "armed",
            "in_air": "in_air",
            "landed_state": "landed_state",
            "health": "health",
            "home": "home",
        }
        for name, method in streams.items():
            self._spawn(self._pump(name, method), f"px4-{name}")
        self._spawn(self._watch_server(), "px4-mavsdk-watchdog")
        self._spawn(self._watch_connection(), "px4-link")
        self._spawn(self._watch_status_text(), "px4-status-text")
        self._spawn(self._keep_attested(), "px4-attestation")
        self._spawn(self._supervise_mission(), "px4-mission-supervisor")
        self._spawn(self._watch_mission_progress(), "px4-mission-progress")

    def _spawn(self, coro: Awaitable[None], name: str) -> None:
        self._tasks.append(asyncio.create_task(coro, name=name))  # type: ignore[arg-type]

    async def _pump(self, name: str, method: str) -> None:
        while True:
            try:
                # Looked up each time: after a MAVSDK restart the plugin objects are new.
                async for sample in getattr(self._system.telemetry, method)():
                    self._t[name] = sample
                    if name in ("position", "quaternion"):
                        self._last_rx = time.monotonic()
                    if name == "quaternion" and sample.timestamp_us:
                        self._note_vehicle_clock(self._last_rx, sample.timestamp_us / 1e6)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if not self._restarting:
                    log.warning(
                        "Telemetry stream %s failed (%s); retrying", name, type(exc).__name__
                    )
            await asyncio.sleep(1.0)

    async def _watch_server(self) -> None:
        """Restart the MAVSDK server child process if it dies.

        The vehicle is unaffected by such a crash: PX4 keeps flying its mission or
        failsafe on its own. AEGIS only loses its view and its ability to command
        until the bridge is back, so that is reported loudly.
        """
        while True:
            await asyncio.sleep(2.0)
            process = getattr(self._system, "_server_process", None)
            if process is None or process.poll() is None:
                continue
            self.bridge_restarts += 1
            self._restarting = True
            self._mavsdk_connected = False
            message = (
                f"MAVSDK bridge process exited (code {process.returncode}); restarting it. "
                "The vehicle remains under PX4 control."
            )
            log.error(message)
            self.messages.append(
                AutopilotMessage(
                    vehicle_id=self.vehicle_id,
                    provenance=self.provenance,
                    level="AEGIS_ERROR",
                    text=message,
                    received_at_unix_s=time.time(),
                )
            )
            if self._status.phase in (MissionPhase.UPLOADING, MissionPhase.ACTIVE):
                self._set_status(
                    MissionPhase.FAILED,
                    "Lost the MAVSDK bridge; mission progress is no longer tracked. "
                    "Check the vehicle and use Hold / Return / Land once the link is back.",
                )
            try:
                await self._system.connect(system_address=self._url)
            except Exception:
                log.exception("Could not restart the MAVSDK bridge; will try again")
            finally:
                self._restarting = False

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

    def _note_vehicle_clock(self, wall_s: float, vehicle_s: float) -> None:
        samples = self._clock_samples
        if samples and vehicle_s < samples[-1][1]:
            samples.clear()  # the autopilot restarted
        samples.append((wall_s, vehicle_s))
        while len(samples) > 2 and wall_s - samples[0][0] > RTF_WINDOW_S:
            samples.popleft()

    @property
    def real_time_factor(self) -> float | None:
        """Simulation seconds per wall-clock second over the last few seconds."""
        samples = self._clock_samples
        if len(samples) < 2:
            return None
        wall = samples[-1][0] - samples[0][0]
        if wall < 1.0:
            return None
        return (samples[-1][1] - samples[0][1]) / wall

    def _link_timeout_s(self) -> float:
        rtf = self.real_time_factor
        if rtf is None:
            return LINK_TIMEOUT_S
        return LINK_TIMEOUT_S / max(MIN_REAL_TIME_FACTOR, min(1.0, rtf))

    async def _watch_connection(self) -> None:
        """Track MAVSDK's own heartbeat state. MAVSDK uses a fixed 3 s wall-clock
        timeout, which flaps when the simulation runs slowly, so the link state
        shown to the operator comes from :meth:`_connected` instead. Commands,
        however, are only sent while MAVSDK itself considers the system connected
        (see :meth:`_await_heartbeat`)."""
        while True:
            try:
                async for state in self._system.core.connection_state():
                    self._mavsdk_connected = bool(state.is_connected)
            except asyncio.CancelledError:
                raise
            except Exception:
                self._mavsdk_connected = False
            await asyncio.sleep(1.0)

    async def _await_heartbeat(self, timeout_s: float = 20.0) -> None:
        """Wait until MAVSDK has a current heartbeat before sending it a command.

        MAVSDK v4.0.5's server was observed to crash when asked to upload a
        geofence while it considered the system timed out, which happens between
        heartbeats whenever the simulator runs slower than about 0.33x real time.
        """
        deadline = time.monotonic() + timeout_s
        while not self._mavsdk_connected:
            if time.monotonic() > deadline:
                raise RuntimeError(
                    "No autopilot heartbeat within "
                    f"{timeout_s:.0f} s (the simulation may be running too slowly)."
                )
            await asyncio.sleep(0.05)

    async def _keep_attested(self) -> None:
        """Attest as soon as the link is up, retry until it succeeds, then re-check.

        A single attempt is not enough: PX4 may still be booting when the first
        heartbeat arrives (the parameter read then times out), and a simulator
        can be restarted with a different airframe behind the same UDP port.
        """
        last_check = 0.0
        while True:
            await asyncio.sleep(ATTESTATION_RETRY_S)
            up = self._connected()
            if up and not self._link_was_up:
                log.info("Autopilot telemetry link up")
            elif not up and self._link_was_up:
                log.warning("Autopilot telemetry link lost")
                self._attested_airframe = None  # whatever reconnects must be identified again
                self._attestation_error = "Link to the autopilot was lost."
                self._clock_samples.clear()
            self._link_was_up = up
            if not up:
                continue
            due = time.monotonic() - last_check > ATTESTATION_RECHECK_S
            if not self.attested or due:
                await self._attest_simulator()
                last_check = time.monotonic()

    async def _read_int(self, name: str) -> int:
        value = await asyncio.wait_for(self._system.param.get_param_int(name), timeout=5.0)
        return int(value)

    def _refuse(self, error: str) -> None:
        self._attested_airframe = None
        if error != self._attestation_error:
            log.error(error)
        self._attestation_error = error

    async def _attest_simulator(self) -> None:
        """Positively identify the autopilot as a PX4 simulation before allowing commands."""
        try:
            autostart = await self._read_int("SYS_AUTOSTART")
        except Exception as exc:
            if self.attested:
                # A transient read failure must not lock the operator out of
                # hold / return / land on a vehicle that was already identified.
                log.warning("Attestation re-check could not read SYS_AUTOSTART: %r", exc)
                return
            error = f"Could not read SYS_AUTOSTART yet ({exc!r}); retrying."
            if error != self._attestation_error:
                log.warning(error)
            self._attestation_error = error
            return

        entry = self._sim_airframes.get(autostart)
        if entry is None:
            self._refuse(
                f"SYS_AUTOSTART={autostart} is not a known simulation airframe; "
                "commands are disabled (telemetry is read-only)."
            )
            return
        name, evidence = entry
        if name == self._attested_airframe:
            return  # evidence was already established for this airframe

        for param, required in evidence:
            try:
                value = await self._read_int(param)
            except Exception:
                self._refuse(
                    f"SYS_AUTOSTART={autostart} looks like '{name}', but the simulation-only "
                    f"parameter {param} could not be read. This may be real hardware; "
                    "commands are disabled."
                )
                return
            if required is not None and value != required:
                self._refuse(
                    f"SYS_AUTOSTART={autostart} looks like '{name}', but {param}={value} "
                    f"(expected {required}); commands are disabled."
                )
                return
        log.info(
            "Simulator attested: SYS_AUTOSTART=%s (%s), evidence %s",
            autostart,
            name,
            [p for p, _ in evidence],
        )
        self._attested_airframe = name
        self._attestation_error = None

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
        if self._last_rx == 0.0:
            return False
        return (time.monotonic() - self._last_rx) < self._link_timeout_s()

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
            sim_real_time_factor=self.real_time_factor,
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
            self._last_progress_at = self._mission_clock()
            self._set_status(MissionPhase.ACTIVE, "Mission started.")
            return MissionResult(accepted=True, status=self._status)

    async def _upload_and_start(self, mission: Mission, origin: LocalOrigin) -> None:
        o = origin.geodetic
        c = mission.constraints
        await self._await_heartbeat()

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
                        self._last_progress_at = self._mission_clock()
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

    def _mission_clock(self) -> float:
        """Seconds on the vehicle's clock (simulation time), so leg timeouts mean the
        same thing whether the simulator runs at 1x or 0.3x. Wall clock as fallback."""
        q = self._t.get("quaternion")
        if q is not None and q.timestamp_us:
            return float(q.timestamp_us) / 1e6
        return time.monotonic()

    async def _supervise_mission(self) -> None:
        """Detect unreachable waypoints and the end of the return-to-home leg."""
        while True:
            await asyncio.sleep(1.0)
            phase = self._status.phase
            if phase is MissionPhase.ACTIVE and self._mission is not None:
                waited = self._mission_clock() - self._last_progress_at
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
        await self._await_heartbeat(timeout_s=10.0)
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
