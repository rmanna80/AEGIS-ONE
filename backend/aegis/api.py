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
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from aegis import __version__
from aegis.adapters.px4_sitl import NotAttestedError, PX4SITLAdapter
from aegis.config import OperatingMode, Settings
from aegis.contracts import Alert
from aegis.hub import TelemetryHub
from aegis.interfaces import MissionExecutor, VehicleTelemetrySource
from aegis.missions import Mission, MissionResult, MissionStatus, validate_mission
from aegis.recording import FlightRecorder, RecordedTelemetryAdapter

log = logging.getLogger("aegis.api")


def build_hub(settings: Settings) -> TelemetryHub:
    """Wire the adapter for the configured operating mode."""
    source: VehicleTelemetrySource
    executor: MissionExecutor | None = None
    recorder: FlightRecorder | None = None

    if settings.mode is OperatingMode.REPLAY:
        source = RecordedTelemetryAdapter(settings.replay_file, speed=settings.replay_speed)
    else:
        px4 = PX4SITLAdapter(
            vehicle_id=settings.vehicle_id,
            connection_url=settings.px4_url,
            grpc_port=settings.mavsdk_grpc_port,
        )
        source = px4
        if settings.mode is OperatingMode.SIMULATION:
            executor = px4
        if settings.record_flights:
            stamp = time.strftime("%Y%m%dT%H%M%S")
            recorder = FlightRecorder(settings.data_dir / "flights" / f"flight_{stamp}.jsonl")
    return TelemetryHub(source, executor, recorder, rate_hz=settings.broadcast_hz)


def create_app(settings: Settings | None = None, hub: TelemetryHub | None = None) -> FastAPI:
    settings = settings or Settings()
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    hub = hub or build_hub(settings)
    token = settings.operator_token or secrets.token_urlsafe(24)
    scenario = _load_scenario(settings.scenario_file)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        log.info("AEGIS ONE %s starting in %s mode", __version__, settings.mode.value)
        if hub.executor is not None and settings.operator_token is None:
            log.warning("AEGIS_OPERATOR_TOKEN not set; generated for this run: %s", token)
        await hub.start()
        try:
            yield
        finally:
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
        found = []
        for folder in (settings.data_dir / "flights", settings.replay_file.parent):
            for path in sorted(folder.glob("*.jsonl")):
                found.append({"file": path.name, "bytes": path.stat().st_size})
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
