"""FastAPI application: REST endpoints plus the telemetry WebSocket.

Run with::

    uvicorn aegis.api:create_app --factory --host 127.0.0.1 --port 8000
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import secrets
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

from fastapi import (
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Request,
    Response,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from aegis import __version__
from aegis.adapters.px4_sitl import NotAttestedError, PX4SITLAdapter
from aegis.anomaly import AnomalyMonitor
from aegis.config import OperatingMode, Settings
from aegis.contracts import Alert, Severity
from aegis.faults import KINDS as FAULT_KINDS
from aegis.faults import FaultInjector, FaultRequest
from aegis.fleet import Fleet, route_conflicts, route_legs
from aegis.hub import TelemetryHub
from aegis.interfaces import MissionExecutor, VehicleTelemetrySource
from aegis.missions import (
    Mission,
    MissionPhase,
    MissionResult,
    MissionStatus,
    validate_mission,
)
from aegis.recording import FlightRecorder, RecordedTelemetryAdapter
from aegis.sar.review import ReviewRequest
from aegis.security import AuditLog, SecurityMonitor, posture
from aegis.watchdog import StallWatchdog, system_snapshot

if TYPE_CHECKING:
    from aegis.navigation.service import NavigationService
    from aegis.perception.pipeline import PerceptionPipeline
    from aegis.perception.playback import RecordedPerceptionPlayer

    Perception = PerceptionPipeline | RecordedPerceptionPlayer

log = logging.getLogger("aegis.api")


class _DashboardFiles(StaticFiles):
    """Serves the built dashboard. The page itself is never cached, so a rebuilt
    dashboard is picked up on the next load (its scripts have content-hashed names)."""

    async def get_response(self, path: str, scope: Any) -> Response:
        response = await super().get_response(path, scope)
        if response.headers.get("content-type", "").startswith("text/html"):
            response.headers["Cache-Control"] = "no-cache"
        return response


class TrackSelection(BaseModel):
    track_id: int | None


class PlanRequest(BaseModel):
    mission: Mission
    # Number of waypoints already reached; they are dropped from the planned mission.
    from_waypoint: int = 0


def build_navigation(
    settings: Settings, hub: TelemetryHub, scenario: dict[str, Any], record_dir: Path | None
) -> tuple[NavigationService | None, str]:
    """Create the navigation service, or explain why it is off. Never raises."""
    try:
        from aegis.navigation.depth import DepthSource, RaycastDepthCamera
        from aegis.navigation.depth_stream import DepthStreamAdapter
        from aegis.navigation.geometry import SceneGeometry
        from aegis.navigation.service import NavigationService
    except ImportError as exc:
        return None, f"Navigation dependencies are not installed ({exc.name}); see README."
    try:
        depth: DepthSource | None = None
        live = settings.mode is not OperatingMode.REPLAY
        if settings.depth == "raycast" and live:
            depth = RaycastDepthCamera(
                SceneGeometry(scenario),
                hub.source.get_state,
                horizontal_fov_deg=settings.depth_hfov_deg,
                max_range_m=settings.depth_max_range_m,
            )
        elif settings.depth == "stream" and live:
            depth = DepthStreamAdapter(
                settings.camera_stream_host,
                settings.depth_stream_port,
                settings.depth_hfov_deg,
                settings.depth_max_range_m,
            )
        elif settings.depth not in ("none", "raycast", "stream"):
            return None, f"Unknown AEGIS_DEPTH value {settings.depth!r}."
        service = NavigationService(
            hub,
            scenario,
            depth,
            resolution_m=settings.map_resolution_m,
            clearance_m=settings.nav_clearance_m,
            use_prior_map=settings.nav_use_prior_map,
            auto_hold=settings.nav_auto_hold,
            save_dir=record_dir,
        )
        saved = settings.nav_load_map
        if saved is None and not live:
            session = settings.resolved_replay_session()
            if session is not None and (session / "map.npz").is_file():
                saved = session / "map.npz"
        if saved is not None:
            service.load_map(saved)
        return service, ""
    except (OSError, ValueError, RuntimeError, KeyError) as exc:
        return None, str(exc)


class SarPlanRequest(BaseModel):
    altitude_rel_m: float | None = Field(default=None, gt=0.0, le=120.0)
    overlap: float | None = Field(default=None, ge=0.0, lt=0.9)


def build_sar(
    settings: Settings,
    hub: TelemetryHub,
    scenario: dict[str, Any],
    record_dir: Path | None,
    other_hubs: list[TelemetryHub] | None = None,
) -> tuple[Any, str]:
    """Create the search-and-rescue service, or explain why it is off. Never raises.

    The service receives the hub (to read state and log events) but not the mission
    executor: it cannot command the vehicle."""
    if settings.thermal == "none":
        return None, "No thermal sensor configured (set AEGIS_THERMAL=model)."
    if settings.thermal != "model":
        return None, f"Unknown AEGIS_THERMAL value {settings.thermal!r}."
    if settings.mode is OperatingMode.REPLAY:
        return None, "Thermal sensing is not replayed yet."
    try:
        from aegis.sar.service import SarService
        from aegis.sar.thermal import ThermalCameraModel
    except ImportError as exc:
        return None, f"Search dependencies are not installed ({exc.name}); see README."
    try:
        others = other_hubs or []
        camera = ThermalCameraModel(
            scenario,
            hub.source.get_state,
            horizontal_fov_deg=settings.thermal_hfov_deg,
            vehicle_id=hub.source.vehicle_id if others else None,
        )
        extra = [
            ThermalCameraModel(
                scenario,
                other.source.get_state,
                horizontal_fov_deg=settings.thermal_hfov_deg,
                vehicle_id=other.source.vehicle_id,
            )
            for other in others
        ]
        service = SarService(
            hub,
            scenario,
            camera,
            save_dir=record_dir,
            default_altitude_m=settings.sar_altitude_m,
            default_overlap=settings.sar_overlap,
            min_delta_c=settings.thermal_min_delta_c,
            extra_thermals=extra,
        )
        return service, ""
    except (OSError, ValueError, RuntimeError, KeyError) as exc:
        return None, str(exc)


def build_perception(
    settings: Settings, hub: TelemetryHub, record_dir: Path | None
) -> tuple[Perception | None, str]:
    """Create the perception pipeline (or the playback of a recorded one), or explain
    why it is off. Never raises: the simulator and dashboard must keep working
    without the AI stack."""
    if settings.mode is OperatingMode.REPLAY:
        source = hub.source
        try:
            session = settings.resolved_replay_session()
            if session is None or not isinstance(source, RecordedTelemetryAdapter):
                return None, "The recording being replayed has no camera data."
            from aegis.perception.playback import RecordedPerceptionPlayer

            started = float(source.header["started_at_unix_s"])
            return RecordedPerceptionPlayer(session, source, started), ""
        except ImportError as exc:
            return None, f"Perception dependencies are not installed ({exc.name}); see README."
        except (OSError, ValueError, RuntimeError, KeyError) as exc:
            return None, str(exc)

    if settings.camera == "none":
        return None, "No camera configured (set AEGIS_CAMERA)."
    try:
        from aegis.perception.cameras import StreamCameraAdapter, VideoFileCameraAdapter
        from aegis.perception.pipeline import PerceptionPipeline
        from aegis.perception.types import CameraSource
        from aegis.perception.yolo_onnx import OnnxYoloBackend
    except ImportError as exc:
        return None, f"Perception dependencies are not installed ({exc.name}); see README."
    try:
        camera: CameraSource
        if settings.camera == "stream":
            camera = StreamCameraAdapter(settings.camera_stream_host, settings.camera_stream_port)
        elif settings.camera.startswith("video:"):
            camera = VideoFileCameraAdapter(Path(settings.camera.removeprefix("video:")))
        else:
            return None, f"Unknown AEGIS_CAMERA value {settings.camera!r}."
        detector = OnnxYoloBackend(
            settings.detector_model,
            confidence_threshold=settings.detector_confidence,
            allowed_labels=settings.detector_labels or None,
            threads=settings.detector_threads or max(1, min(4, (os.cpu_count() or 2) // 2)),
        )
    except (OSError, ValueError, RuntimeError) as exc:
        return None, str(exc)
    return PerceptionPipeline(camera, detector, record_dir=record_dir), ""


def build_hub(settings: Settings, record_dir: Path | None = None) -> TelemetryHub:
    """Wire the adapter for the configured operating mode."""
    source: VehicleTelemetrySource
    executor: MissionExecutor | None = None
    recorder: FlightRecorder | None = None

    if settings.mode is OperatingMode.REPLAY:
        session = settings.resolved_replay_session()
        replay_file = session / "flight.jsonl" if session else settings.replay_file
        source = RecordedTelemetryAdapter(replay_file, speed=settings.replay_speed)
    else:
        px4 = PX4SITLAdapter(
            vehicle_id=settings.vehicle_id,
            connection_url=settings.px4_url,
            grpc_port=settings.mavsdk_grpc_port,
        )
        source = px4
        if settings.mode is OperatingMode.SIMULATION:
            executor = px4
        if record_dir is not None:
            recorder = FlightRecorder(record_dir / "flight.jsonl")
    return TelemetryHub(source, executor, recorder, rate_hz=settings.broadcast_hz)


def build_fleet(settings: Settings, primary: TelemetryHub, record_dir: Path | None) -> Fleet:
    """The first vehicle plus ``fleet_size - 1`` more PX4 instances (simulation only)."""
    hubs = [primary]
    live = settings.mode is not OperatingMode.REPLAY and isinstance(primary.source, PX4SITLAdapter)
    base_url, _, port_text = settings.px4_url.rpartition(":")
    base_port = int(port_text) if port_text.isdigit() else 14540
    for index in range(1, settings.fleet_size if live else 1):
        vehicle_id = f"sim-{index + 1}"
        px4 = PX4SITLAdapter(
            vehicle_id=vehicle_id,
            connection_url=f"{base_url}:{base_port + index}",
            grpc_port=settings.mavsdk_grpc_port + index,
        )
        recorder = FlightRecorder(record_dir / f"flight_{vehicle_id}.jsonl") if record_dir else None
        executor = px4 if settings.mode is OperatingMode.SIMULATION else None
        hubs.append(TelemetryHub(px4, executor, recorder, rate_hz=settings.broadcast_hz))
    return Fleet(hubs, auto_hold=settings.fleet_auto_hold)


def create_app(
    settings: Settings | None = None,
    hub: TelemetryHub | None = None,
    perception: Perception | None = None,
    navigation: NavigationService | None = None,
    fleet: Fleet | None = None,
) -> FastAPI:
    settings = settings or Settings()
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    # One directory per backend start holds the flight log, camera video and detections,
    # so a session can be replayed with everything in sync.
    record_dir: Path | None = None
    if hub is None and settings.record_flights and settings.mode is not OperatingMode.REPLAY:
        record_dir = settings.data_dir / "sessions" / time.strftime("%Y%m%dT%H%M%S")
    hub = hub or build_hub(settings, record_dir)
    fleet = fleet or build_fleet(settings, hub, record_dir)
    if len(fleet.hubs) > 1:
        hub.extra_checks.append(fleet.findings)
        hub.snapshot_extras.append(fleet.snapshot_extra)
    audit = AuditLog(record_dir / "audit.jsonl" if record_dir is not None else None)
    security = SecurityMonitor(audit)
    hub.extra_checks.append(security.findings)
    anomaly = AnomalyMonitor(hub) if settings.anomaly else None
    if anomaly is not None:
        hub.extra_checks.append(anomaly.findings)
    injector: FaultInjector | None = None
    if settings.fault_injection and settings.mode is OperatingMode.SIMULATION:
        injector = FaultInjector()
        hub.state_filters.append(injector.apply)
    token = settings.operator_token or secrets.token_urlsafe(24)
    boot_id = secrets.token_hex(8)
    scenario = _load_scenario(settings.resolved_scenario_file())
    if not settings.detector_labels and scenario.get("detector_labels"):
        # A scenario may narrow the detector to the classes that matter in it; an
        # explicit AEGIS_DETECTOR_LABELS still wins.
        settings = settings.model_copy(
            update={"detector_labels": list(scenario["detector_labels"])}
        )
    perception_off_reason = ""
    perception_check: Any = None
    if perception is None:
        perception, perception_off_reason = build_perception(settings, hub, record_dir)
    navigation_off_reason = ""
    if navigation is None:
        navigation, navigation_off_reason = build_navigation(settings, hub, scenario, record_dir)
    if navigation is not None:
        hub.extra_checks.append(navigation.findings)
    sar, sar_off_reason = build_sar(settings, hub, scenario, record_dir, fleet.hubs[1:])
    if sar is not None:
        hub.extra_checks.append(sar.findings)
    if perception is not None:
        pipeline = perception

        def perception_findings() -> list[tuple[str, Severity, str]]:
            messages = {
                "camera.no_frames": "No camera frames are arriving.",
                "inference.slow": "Object-detection latency is above 500 ms (p95).",
            }
            return [
                (code, Severity.WARNING, messages[code]) for code in pipeline.status()["faults"]
            ]

        hub.extra_checks.append(perception_findings)
        perception_check = perception_findings

    watchdog = StallWatchdog()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        log.info("AEGIS ONE %s starting in %s mode", __version__, settings.mode.value)
        if hub.executor is not None and settings.operator_token is None:
            log.warning("AEGIS_OPERATOR_TOKEN not set; generated for this run: %s", token)
        nonlocal perception, perception_off_reason, navigation, navigation_off_reason
        log.info("System at start: %s", system_snapshot() or "n/a")
        await watchdog.start()
        await hub.start()
        await fleet.start()
        if len(fleet.hubs) > 1:
            log.info("Fleet of %d vehicles: %s", len(fleet.hubs), ", ".join(fleet.ids))
        try:
            if perception is not None:
                try:
                    await perception.start()
                    log.info(
                        "Perception enabled: camera=%s detector=%s",
                        settings.camera,
                        perception.detector.name,
                    )
                except OSError as exc:
                    # e.g. the camera stream port is taken. Perception is optional:
                    # report it and keep the simulator and dashboard running.
                    perception_off_reason = f"Camera could not start: {exc}"
                    if settings.camera == "stream":
                        perception_off_reason += (
                            f" Is another AEGIS backend already running? "
                            f"(port {settings.camera_stream_port})"
                        )
                    log.error(perception_off_reason)
                    hub.extra_checks[:] = [c for c in hub.extra_checks if c is not perception_check]
                    perception = None
            if perception is None:
                log.info("Perception disabled: %s", perception_off_reason)
            if navigation is not None:
                try:
                    await navigation.start()
                    log.info("Navigation enabled: depth=%s", settings.depth)
                except OSError as exc:
                    navigation_off_reason = f"Depth source could not start: {exc}"
                    log.error(navigation_off_reason)
                    hub.extra_checks.remove(navigation.findings)
                    navigation = None
            else:
                log.info("Navigation disabled: %s", navigation_off_reason)
            if sar is not None:
                await sar.start()
                log.info("Search support enabled: thermal=%s (advisory only)", settings.thermal)
            else:
                log.info("Search support disabled: %s", sar_off_reason)
            yield
        finally:
            # Always runs, including when startup fails part-way, so the MAVSDK
            # server child process is never left behind.
            if sar is not None:
                await sar.stop()
            if navigation is not None:
                await navigation.stop()
            if perception is not None:
                await perception.stop()
            await fleet.stop()
            await hub.stop()
            await watchdog.stop()

    app = FastAPI(title="AEGIS ONE", version=__version__, lifespan=lifespan)
    app.state.sar = sar
    app.state.fleet = fleet
    app.state.audit = audit
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    def require_operator(
        request: Request,
        x_aegis_operator_token: Annotated[str | None, Header()] = None,
    ) -> MissionExecutor:
        """Commands need (a) simulation mode and (b) the operator token. Every attempt,
        allowed or not, goes into the audit log (never the token itself)."""
        if hub.executor is None:
            raise HTTPException(403, f"Commands are disabled in {settings.mode.value} mode.")
        client = request.client.host if request.client else None
        supplied = (x_aegis_operator_token or "").encode()
        if not hmac.compare_digest(supplied, token.encode()):
            security.auth_failed(client, request.url.path)
            raise HTTPException(401, "Missing or invalid operator token.")
        security.authorised(client, request.url.path)
        return hub.executor

    operator = Depends(require_operator)

    # ----------------------------------------------------------------- read-only

    @app.get("/api/health")
    async def health() -> dict[str, Any]:
        state = hub.state
        return {
            "status": "ok",
            "version": __version__,
            "mode": settings.mode.value,
            "hub_ticks": hub.ticks,
            "backend_lag_s": round(hub.loop_lag_s, 3),
            "worst_backend_lag_s": round(hub.max_loop_lag_s, 3),
            "stalls": watchdog.stalls,
            "longest_stall_s": round(watchdog.longest_stall_s, 1),
            "system": system_snapshot(),
            "vehicle_connected": bool(state and state.connected),
        }

    @app.get("/api/system")
    async def system() -> dict[str, Any]:
        source = hub.source
        info: dict[str, Any] = {
            "version": __version__,
            "mode": settings.mode.value,
            "vehicle_id": source.vehicle_id,
            "provenance": source.provenance.value,
            "source": getattr(source, "source", None),
            "commands_enabled": hub.executor is not None,
            "broadcast_hz": settings.broadcast_hz,
            # Changes on every backend start. An open dashboard reloads when it sees a new
            # one, so it never keeps showing the previous run's scenario or page.
            "boot_id": boot_id,
            "scenario_id": scenario.get("scenario_id"),
        }
        if isinstance(source, PX4SITLAdapter):
            info["simulator_attested"] = source.attested
            info["attestation_error"] = source.attestation_error
        if isinstance(source, RecordedTelemetryAdapter):
            info["replay"] = {
                "file": source.path.name,
                "duration_s": source.duration_s,
                "playback_time_s": source.playback_time_s,
                "speed": source.speed,
                "recorded_source": source.header.get("source"),
                "recorded_provenance": source.header.get("provenance"),
            }
        return info

    @app.get("/api/state")
    async def state() -> dict[str, Any]:
        return hub.snapshot()

    @app.get("/api/scenario")
    async def get_scenario() -> dict[str, Any]:
        return scenario

    @app.get("/api/alerts", response_model=list[Alert])
    async def alerts(limit: int = 100) -> list[Alert]:
        return hub.alerts.history[-max(1, min(limit, 500)) :]

    @app.get("/api/recordings")
    async def recordings() -> list[dict[str, Any]]:
        """Recorded sessions (newest first) and the bundled sample flight."""
        found: list[dict[str, Any]] = []
        for folder in sorted((settings.data_dir / "sessions").glob("*"), reverse=True):
            flight = folder / "flight.jsonl"
            if flight.is_file():
                found.append(
                    {
                        "session": folder.name,
                        "flight_bytes": flight.stat().st_size,
                        "has_camera": (folder / "video.mp4").is_file()
                        and (folder / "detections.jsonl").is_file(),
                        "replay_with": f"AEGIS_MODE=replay AEGIS_REPLAY_SESSION={folder.name}",
                    }
                )
        if settings.replay_file.is_file():
            found.append({"sample": settings.replay_file.name, "has_camera": False})
        return found

    def vehicle_start() -> tuple[float, float, float]:
        if hub.state and hub.state.position_enu_m:
            p = hub.state.position_enu_m
            return (p.x, p.y, max(p.z, 0.0))
        return (0.0, 0.0, 0.0)

    async def mission_issues(mission: Mission) -> list[str]:
        """Geofence / altitude rules plus, when navigation is on, obstacle clearance."""
        start = vehicle_start()
        issues = validate_mission(mission, start_enu_m=start[:2])
        if navigation is not None and not issues:
            issues += navigation.preflight_issues()
            issues += await asyncio.to_thread(navigation.checker.check_mission, mission, start)
        return issues

    @app.post("/api/missions/validate")
    async def validate(mission: Mission) -> dict[str, Any]:
        issues = await mission_issues(mission)
        return {
            "valid": not issues,
            "issues": issues,
            "obstacles_checked": navigation is not None,
        }

    @app.post("/api/missions/plan")
    async def plan(request: PlanRequest) -> dict[str, Any]:
        """Propose detour waypoints around known and mapped obstacles. Nothing is sent
        to the vehicle: the operator reviews the result and submits it like any mission."""
        if navigation is None:
            raise HTTPException(409, f"Navigation is not enabled. {navigation_off_reason}")
        from aegis.navigation.planner import PlanningError

        remaining = request.mission.waypoints[max(0, request.from_waypoint) :]
        if not remaining:
            raise HTTPException(422, "No waypoints left to plan.")
        mission = request.mission.model_copy(update={"waypoints": remaining})
        try:
            planned = await asyncio.to_thread(
                navigation.checker.plan_mission, mission, vehicle_start()
            )
        except PlanningError as exc:
            raise HTTPException(422, str(exc)) from exc
        added = len(planned.waypoints) - len(mission.waypoints)
        if request.from_waypoint > 0 or added > 0:
            planned = planned.model_copy(
                update={
                    "mission_id": f"{request.mission.mission_id.removesuffix('-replan')}-replan"
                }
            )
        return {"mission": planned.model_dump(mode="json"), "detour_waypoints_added": added}

    @app.get("/api/missions/current", response_model=MissionStatus)
    async def current_mission() -> MissionStatus:
        return hub.mission

    @app.get("/api/missions/active")
    async def active_mission() -> dict[str, Any]:
        """The mission last accepted by the vehicle (whoever submitted it), so every
        display can draw the route actually being flown."""
        mission = hub.active_mission
        return {"mission": mission.model_dump(mode="json") if mission is not None else None}

    # ---------------------------------------------------------- operator commands

    def rejected(mission: Mission, message: str, issues: list[str]) -> MissionResult:
        log.warning("Mission %s rejected: %s", mission.mission_id, issues)
        security.outcome("mission", "rejected before upload", mission_id=mission.mission_id)
        return MissionResult(
            accepted=False,
            status=MissionStatus(
                mission_id=mission.mission_id,
                name=mission.name,
                phase=MissionPhase.REJECTED,
                total_waypoints=len(mission.waypoints),
                message=message,
                issues=issues,
                updated_at_unix_s=time.time(),
            ),
        )

    async def submit_to(target: TelemetryHub, mission: Mission) -> MissionResult:
        """Pre-flight checks, then hand the mission to one vehicle. A refusal here means
        nothing was sent to that vehicle."""
        assert target.executor is not None
        vehicle_id = target.source.vehicle_id
        log.info(
            "Operator submitted mission %s (%s) to %s", mission.mission_id, mission.name, vehicle_id
        )
        if navigation is not None and target is hub:
            start = vehicle_start()
            if not validate_mission(mission, start_enu_m=start[:2]):
                blocked = navigation.preflight_issues()
                blocked += await asyncio.to_thread(navigation.checker.check_mission, mission, start)
                if blocked:
                    return rejected(
                        mission, "Mission rejected by the pre-flight navigation checks.", blocked
                    )
        conflicts = fleet.route_issues(vehicle_id, mission)
        if conflicts:
            return rejected(
                mission, "Mission rejected: it conflicts with another vehicle's route.", conflicts
            )
        result = await target.executor.submit_mission(mission)
        if result.accepted:
            target.geofence = mission.constraints.geofence
            target.active_mission = mission
        security.outcome(
            "mission",
            "accepted" if result.accepted else "rejected",
            vehicle_id=vehicle_id,
            mission_id=mission.mission_id,
        )
        return result

    @app.post("/api/missions", response_model=MissionResult)
    async def submit(mission: Mission, _: MissionExecutor = operator) -> MissionResult:
        return await submit_to(hub, mission)

    async def _intervene(name: str, executor: MissionExecutor) -> dict[str, str]:
        log.info("Operator commanded %s", name)
        vehicle_id = getattr(executor, "vehicle_id", None)
        try:
            await getattr(executor, name)()
        except NotAttestedError as exc:
            security.outcome(name, "refused: simulator not attested", vehicle_id=vehicle_id)
            raise HTTPException(409, str(exc)) from exc
        except Exception as exc:
            security.outcome(name, "refused by the autopilot", vehicle_id=vehicle_id)
            raise HTTPException(502, f"Autopilot refused {name}: {exc}") from exc
        security.outcome(name, "sent", vehicle_id=vehicle_id)
        return {"status": "sent", "command": name}

    @app.post("/api/vehicle/hold")
    async def hold(executor: MissionExecutor = operator) -> dict[str, str]:
        return await _intervene("hold", executor)

    @app.post("/api/vehicle/return")
    async def return_home(executor: MissionExecutor = operator) -> dict[str, str]:
        return await _intervene("return_to_home", executor)

    @app.post("/api/vehicle/land")
    async def land(executor: MissionExecutor = operator) -> dict[str, str]:
        return await _intervene("land", executor)

    # ---------------------------------------------------- security and anomalies

    @app.get("/api/security")
    async def security_status() -> dict[str, Any]:
        """Configuration posture, authentication failures, the audit log's state, the
        anomaly detectors and any faults being injected."""
        return {
            "posture": posture(
                operator_token=settings.operator_token,
                token_generated=settings.operator_token is None,
                px4_url=settings.px4_url,
                cors_origins=settings.cors_origins,
                fault_injection=injector is not None,
                mode=settings.mode.value,
            ),
            "auth_failures_recent": security.recent_failures(),
            "auth_failures_total": security.failures_total,
            "command_requests_total": security.commands_total,
            "audit": {
                "records": audit.count,
                "file": str(audit.path) if audit.path else None,
                "recent": [
                    {k: v for k, v in r.items() if k not in ("prev", "hash")}
                    for r in list(audit.recent)[-15:]
                ],
            },
            "anomaly": anomaly.status() if anomaly is not None else {"enabled": False},
            "faults": {
                "enabled": injector is not None,
                "active": injector.active() if injector is not None else [],
                "kinds": FAULT_KINDS,
            },
        }

    def require_injector() -> FaultInjector:
        if injector is None:
            raise HTTPException(
                409,
                "Fault injection is off. Start the backend with AEGIS_FAULT_INJECTION=true "
                "(simulation mode only).",
            )
        if not getattr(hub.source, "attested", False):
            raise HTTPException(409, "Fault injection needs an attested simulator.")
        return injector

    @app.post("/api/faults")
    async def inject_fault(
        request: FaultRequest, executor: MissionExecutor = operator
    ) -> dict[str, Any]:
        """Inject a fault into AEGIS's copy of the telemetry (testing, simulation only)."""
        faults = require_injector()
        if request.kind == "external_hold":
            simulate = getattr(executor, "simulate_external_hold", None)
            if simulate is None:
                raise HTTPException(409, "This vehicle adapter cannot simulate an outside command.")
            await simulate()
        else:
            try:
                faults.inject(request)
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from exc
        log.warning("FAULT INJECTED for testing: %s", request.kind)
        security.outcome("fault_injection", request.kind)
        return {"injected": request.kind, "active": faults.active()}

    @app.delete("/api/faults")
    async def clear_faults(_: MissionExecutor = operator) -> dict[str, Any]:
        require_injector().clear()
        security.outcome("fault_injection", "cleared")
        return {"active": []}

    # ---------------------------------------------------------------------- fleet

    def fleet_vehicle(vehicle_id: str) -> TelemetryHub:
        target = fleet.hub(vehicle_id)
        if target is None:
            raise HTTPException(404, f"No vehicle {vehicle_id}. Known: {', '.join(fleet.ids)}.")
        if target.executor is None:
            raise HTTPException(403, f"Commands are disabled in {settings.mode.value} mode.")
        return target

    @app.get("/api/fleet")
    async def fleet_status() -> dict[str, Any]:
        """Every vehicle's state and mission, and how far apart the airborne ones are."""
        return fleet.status()

    @app.post("/api/fleet/hold-all")
    async def fleet_hold_all(_: MissionExecutor = operator) -> dict[str, Any]:
        """Ask every connected vehicle to Hold. Reports per vehicle; one failure does
        not stop the others being commanded."""
        log.info("Operator commanded hold for the whole fleet")
        results = {}
        for target in fleet.hubs:
            vehicle_id = target.source.vehicle_id
            if target.executor is None or target.state is None or not target.state.connected:
                results[vehicle_id] = "not connected"
                continue
            try:
                await target.executor.hold()
                results[vehicle_id] = "sent"
            except Exception as exc:
                results[vehicle_id] = f"refused: {exc}"
        return {"command": "hold", "results": results}

    @app.post("/api/fleet/{vehicle_id}/missions", response_model=MissionResult)
    async def fleet_submit(
        vehicle_id: str, mission: Mission, _: MissionExecutor = operator
    ) -> MissionResult:
        return await submit_to(fleet_vehicle(vehicle_id), mission)

    @app.post("/api/fleet/{vehicle_id}/{command}")
    async def fleet_command(
        vehicle_id: str, command: str, _: MissionExecutor = operator
    ) -> dict[str, str]:
        names = {"hold": "hold", "return": "return_to_home", "land": "land"}
        if command not in names:
            raise HTTPException(404, f"Unknown command {command!r}. Use hold, return or land.")
        target = fleet_vehicle(vehicle_id)
        assert target.executor is not None
        result = await _intervene(names[command], target.executor)
        return {**result, "vehicle_id": vehicle_id}

    # ------------------------------------------------------------------ streaming

    @app.websocket("/ws/telemetry")
    async def telemetry(ws: WebSocket) -> None:
        await ws.accept()
        queue = hub.subscribe()
        try:
            await ws.send_json(hub.snapshot())
            while True:
                await ws.send_json(await queue.get())
        except (WebSocketDisconnect, asyncio.CancelledError, RuntimeError):
            pass
        finally:
            hub.unsubscribe(queue)

    # ---------------------------------------------------------------- navigation

    @app.get("/api/navigation")
    async def navigation_status() -> dict[str, Any]:
        if navigation is None:
            return {"enabled": False, "reason": navigation_off_reason}
        return navigation.status()

    @app.get("/api/map")
    async def occupancy_map(since: int = -1) -> Response:
        """Occupied voxels of the live map; ``since`` avoids resending an unchanged map."""
        if navigation is None:
            raise HTTPException(409, "Navigation is not enabled.")
        body = await asyncio.to_thread(navigation.map_json, since)
        return Response(content=body, media_type="application/json")

    @app.get("/api/map/accuracy")
    async def map_accuracy() -> dict[str, Any]:
        """Reconstructed map scored against the scenario's ground-truth geometry."""
        if navigation is None:
            raise HTTPException(409, "Navigation is not enabled.")
        return await asyncio.to_thread(navigation.accuracy)

    @app.post("/api/map/save")
    async def save_map() -> dict[str, Any]:
        if navigation is None:
            raise HTTPException(409, "Navigation is not enabled.")
        folder = record_dir or settings.data_dir / "maps"
        path = await asyncio.to_thread(navigation.save_map, folder / "map.npz")
        return {"saved": str(path), "load_with": f"AEGIS_NAV_LOAD_MAP={path}"}

    # --------------------------------------------------------- search and rescue

    def require_sar() -> Any:
        if sar is None:
            raise HTTPException(409, f"Search support is not enabled. {sar_off_reason}")
        return sar

    @app.get("/api/sar")
    async def sar_status() -> dict[str, Any]:
        if sar is None:
            return {"enabled": False, "reason": sar_off_reason}
        status: dict[str, Any] = sar.status()
        return status

    @app.get("/api/sar/thermal.jpg")
    async def sar_thermal_image() -> Response:
        """Latest thermal frame in false colour, detected warm blobs outlined."""
        jpeg = await asyncio.to_thread(require_sar().thermal_jpeg)
        if jpeg is None:
            raise HTTPException(404, "No thermal frame yet.")
        return Response(
            content=jpeg, media_type="image/jpeg", headers={"Cache-Control": "no-store"}
        )

    @app.get("/api/sar/candidates")
    async def sar_candidates() -> dict[str, Any]:
        service = require_sar()
        return {
            "candidates": [c.model_dump(mode="json") for c in service.candidates()],
            "awaiting_review": service.registry.awaiting_review(),
        }

    @app.post("/api/sar/candidates/{candidate_id}/review")
    async def sar_review(
        candidate_id: str, request: ReviewRequest, _: MissionExecutor = operator
    ) -> dict[str, Any]:
        """Record the operator's decision on a candidate. Commands nothing."""
        candidate = require_sar().review(candidate_id, request)
        if candidate is None:
            raise HTTPException(404, f"No candidate {candidate_id}.")
        security.outcome("candidate_review", request.decision.value, candidate_id=candidate_id)
        return {"candidate": candidate.model_dump(mode="json")}

    @app.post("/api/sar/plan")
    async def sar_plan(request: SarPlanRequest) -> dict[str, Any]:
        """Propose a search pattern over the scenario's search area. Nothing is sent to
        the vehicle: the operator reviews it and submits it like any mission."""
        from aegis.sar.coverage import CoveragePlanError

        service = require_sar()
        try:
            mission, summary = service.plan(
                vehicle_start()[:2], request.altitude_rel_m, request.overlap
            )
        except CoveragePlanError as exc:
            raise HTTPException(422, str(exc)) from exc
        return {
            "mission": mission.model_dump(mode="json"),
            "summary": summary,
            "issues": await mission_issues(mission),
        }

    @app.post("/api/sar/plan/fleet")
    async def sar_plan_fleet(request: SarPlanRequest) -> dict[str, Any]:
        """Propose one search pattern per connected vehicle (the area cut into strips, each
        vehicle at its own height). Nothing is sent; the operator submits each mission."""
        from aegis.sar.coverage import CoveragePlanError

        service = require_sar()
        starts = []
        for target in fleet.hubs:
            if target.state is None or target.state.position_enu_m is None:
                continue
            p = target.state.position_enu_m
            starts.append((target.source.vehicle_id, (p.x, p.y)))
        if not starts:
            raise HTTPException(409, "No vehicle has a position yet.")
        try:
            plans = service.plan_fleet(starts, request.altitude_rel_m, request.overlap)
        except CoveragePlanError as exc:
            raise HTTPException(422, str(exc)) from exc
        legs = {plan["vehicle_id"]: route_legs(plan["mission"], None) for plan in plans}
        out = []
        for plan in plans:
            vehicle_id = plan["vehicle_id"]
            owner = fleet.hub(vehicle_id)
            start = next(s for v, s in starts if v == vehicle_id)
            issues = validate_mission(plan["mission"], start_enu_m=start)
            for other_id, other_legs in legs.items():
                if other_id != vehicle_id and route_conflicts(
                    legs[vehicle_id], other_legs, fleet.limits
                ):
                    issues.append(f"The pattern conflicts with the one planned for {other_id}.")
            out.append(
                {
                    "vehicle_id": vehicle_id,
                    "mission": plan["mission"].model_dump(mode="json"),
                    "summary": plan["summary"],
                    "strip_enu_m": plan["strip_enu_m"],
                    "issues": issues,
                    "return_altitude_m": getattr(owner.source, "return_altitude_m", None)
                    if owner
                    else None,
                }
            )
        return {"plans": out}

    @app.get("/api/sar/coverage")
    async def sar_coverage() -> dict[str, Any]:
        payload: dict[str, Any] = require_sar().coverage.payload()
        return payload

    @app.get("/api/sar/accuracy")
    async def sar_accuracy() -> dict[str, Any]:
        """Candidates scored against the scenario's ground-truth heat sources."""
        result: dict[str, Any] = require_sar().accuracy()
        return result

    @app.get("/api/sar/report")
    async def sar_report() -> dict[str, Any]:
        result: dict[str, Any] = require_sar().report()
        return result

    @app.post("/api/sar/report/save")
    async def sar_report_save() -> dict[str, Any]:
        folder = record_dir or settings.data_dir / "reports"
        saved: dict[str, Any] = await asyncio.to_thread(require_sar().save_report, folder)
        return {"saved": saved}

    # ---------------------------------------------------------------- perception

    @app.get("/api/perception")
    async def perception_status() -> dict[str, Any]:
        if perception is None:
            return {"enabled": False, "reason": perception_off_reason}
        return perception.status()

    @app.get("/api/perception/latest")
    async def perception_latest() -> dict[str, Any]:
        """The most recent PerceptionResult (detections and tracks), without the image."""
        if perception is None:
            raise HTTPException(409, "Perception is not enabled.")
        latest = perception.latest
        if latest is None:
            return {"result": None}
        return {"result": latest if isinstance(latest, dict) else latest.model_dump(mode="json")}

    @app.post("/api/perception/select")
    async def select_track(selection: TrackSelection) -> dict[str, Any]:
        """Highlight a track for the operator. Display only: it commands nothing."""
        if perception is None:
            raise HTTPException(409, "Perception is not enabled.")
        if not perception.select_track(selection.track_id):
            raise HTTPException(404, f"No active track {selection.track_id}.")
        return {"selected_track_id": perception.selected_track_id}

    @app.websocket("/ws/video")
    async def video(ws: WebSocket) -> None:
        """Per frame: one JSON text message (PerceptionResult) then one binary JPEG."""
        await ws.accept()
        if perception is None:
            await ws.close(code=1008, reason="perception disabled")
            return
        queue = perception.subscribe()
        try:
            while True:
                result, jpeg = await queue.get()
                await ws.send_json(result)
                await ws.send_bytes(jpeg)
        except (WebSocketDisconnect, asyncio.CancelledError, RuntimeError):
            pass
        finally:
            perception.unsubscribe(queue)

    if settings.frontend_dist.is_dir():
        app.mount("/", _DashboardFiles(directory=settings.frontend_dist, html=True), name="ui")

    return app


def _load_scenario(path: Path) -> dict[str, Any]:
    """Load a scenario file and fail fast if its demo mission is malformed."""
    scenario: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    mission = Mission.model_validate(scenario["demo_mission"])
    issues = validate_mission(mission)
    if issues:
        raise ValueError(f"{path}: demo mission is invalid: {issues}")
    return scenario
