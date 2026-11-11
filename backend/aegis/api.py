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
    Response,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from aegis import __version__
from aegis.adapters.px4_sitl import NotAttestedError, PX4SITLAdapter
from aegis.config import OperatingMode, Settings
from aegis.contracts import Alert, Severity
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

if TYPE_CHECKING:
    from aegis.navigation.service import NavigationService
    from aegis.perception.pipeline import PerceptionPipeline
    from aegis.perception.playback import RecordedPerceptionPlayer

    Perception = PerceptionPipeline | RecordedPerceptionPlayer

log = logging.getLogger("aegis.api")


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


def create_app(
    settings: Settings | None = None,
    hub: TelemetryHub | None = None,
    perception: Perception | None = None,
    navigation: NavigationService | None = None,
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
    token = settings.operator_token or secrets.token_urlsafe(24)
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

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        log.info("AEGIS ONE %s starting in %s mode", __version__, settings.mode.value)
        if hub.executor is not None and settings.operator_token is None:
            log.warning("AEGIS_OPERATOR_TOKEN not set; generated for this run: %s", token)
        nonlocal perception, perception_off_reason, navigation, navigation_off_reason
        await hub.start()
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
            yield
        finally:
            # Always runs, including when startup fails part-way, so the MAVSDK
            # server child process is never left behind.
            if navigation is not None:
                await navigation.stop()
            if perception is not None:
                await perception.stop()
            await hub.stop()

    app = FastAPI(title="AEGIS ONE", version=__version__, lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    def require_operator(
        x_aegis_operator_token: Annotated[str | None, Header()] = None,
    ) -> MissionExecutor:
        """Commands need (a) simulation mode and (b) the operator token."""
        if hub.executor is None:
            raise HTTPException(403, f"Commands are disabled in {settings.mode.value} mode.")
        supplied = (x_aegis_operator_token or "").encode()
        if not hmac.compare_digest(supplied, token.encode()):
            raise HTTPException(401, "Missing or invalid operator token.")
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

    # ---------------------------------------------------------- operator commands

    @app.post("/api/missions", response_model=MissionResult)
    async def submit(mission: Mission, executor: MissionExecutor = operator) -> MissionResult:
        log.info("Operator submitted mission %s (%s)", mission.mission_id, mission.name)
        if navigation is not None:
            start = vehicle_start()
            if not validate_mission(mission, start_enu_m=start[:2]):
                blocked = navigation.preflight_issues()
                blocked += await asyncio.to_thread(navigation.checker.check_mission, mission, start)
                if blocked:  # refused here: nothing is sent to the vehicle
                    log.warning("Mission %s rejected: %s", mission.mission_id, blocked)
                    return MissionResult(
                        accepted=False,
                        status=MissionStatus(
                            mission_id=mission.mission_id,
                            name=mission.name,
                            phase=MissionPhase.REJECTED,
                            total_waypoints=len(mission.waypoints),
                            message="Mission rejected by the pre-flight navigation checks.",
                            issues=blocked,
                            updated_at_unix_s=time.time(),
                        ),
                    )
        result = await executor.submit_mission(mission)
        if result.accepted:
            hub.geofence = mission.constraints.geofence
            hub.active_mission = mission
        return result

    async def _intervene(name: str, executor: MissionExecutor) -> dict[str, str]:
        log.info("Operator commanded %s", name)
        try:
            await getattr(executor, name)()
        except NotAttestedError as exc:
            raise HTTPException(409, str(exc)) from exc
        except Exception as exc:
            raise HTTPException(502, f"Autopilot refused {name}: {exc}") from exc
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
        app.mount("/", StaticFiles(directory=settings.frontend_dist, html=True), name="ui")

    return app


def _load_scenario(path: Path) -> dict[str, Any]:
    """Load a scenario file and fail fast if its demo mission is malformed."""
    scenario: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    mission = Mission.model_validate(scenario["demo_mission"])
    issues = validate_mission(mission)
    if issues:
        raise ValueError(f"{path}: demo mission is invalid: {issues}")
    return scenario
