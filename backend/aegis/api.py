"""FastAPI application: REST endpoints plus the telemetry WebSocket.

Run with::

    uvicorn aegis.api:create_app --factory --host 127.0.0.1 --port 8000
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import secrets
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from aegis import __version__
from aegis.adapters.px4_sitl import NotAttestedError, PX4SITLAdapter
from aegis.config import OperatingMode, Settings
from aegis.contracts import Alert, Severity
from aegis.hub import TelemetryHub
from aegis.interfaces import MissionExecutor, VehicleTelemetrySource
from aegis.missions import Mission, MissionResult, MissionStatus, validate_mission
from aegis.recording import FlightRecorder, RecordedTelemetryAdapter

if TYPE_CHECKING:
    from aegis.perception.pipeline import PerceptionPipeline
    from aegis.perception.playback import RecordedPerceptionPlayer

    Perception = PerceptionPipeline | RecordedPerceptionPlayer

log = logging.getLogger("aegis.api")


class TrackSelection(BaseModel):
    track_id: int | None


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
    if perception is None:
        perception, perception_off_reason = build_perception(settings, hub, record_dir)
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

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        log.info("AEGIS ONE %s starting in %s mode", __version__, settings.mode.value)
        if hub.executor is not None and settings.operator_token is None:
            log.warning("AEGIS_OPERATOR_TOKEN not set; generated for this run: %s", token)
        nonlocal perception, perception_off_reason
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
                    hub.extra_checks.clear()
                    perception = None
            if perception is None:
                log.info("Perception disabled: %s", perception_off_reason)
            yield
        finally:
            # Always runs, including when startup fails part-way, so the MAVSDK
            # server child process is never left behind.
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

    @app.post("/api/missions/validate")
    async def validate(mission: Mission) -> dict[str, Any]:
        start = (0.0, 0.0)
        if hub.state and hub.state.position_enu_m:
            start = (hub.state.position_enu_m.x, hub.state.position_enu_m.y)
        issues = validate_mission(mission, start_enu_m=start)
        return {"valid": not issues, "issues": issues}

    @app.get("/api/missions/current", response_model=MissionStatus)
    async def current_mission() -> MissionStatus:
        return hub.mission

    # ---------------------------------------------------------- operator commands

    @app.post("/api/missions", response_model=MissionResult)
    async def submit(mission: Mission, executor: MissionExecutor = operator) -> MissionResult:
        log.info("Operator submitted mission %s (%s)", mission.mission_id, mission.name)
        result = await executor.submit_mission(mission)
        if result.accepted:
            hub.geofence = mission.constraints.geofence
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
