"""Telemetry hub: one loop that samples the vehicle, runs the safety monitors,
records the flight and fans snapshots out to WebSocket subscribers."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable
from typing import Any

from aegis.contracts import Alert, VehicleState
from aegis.interfaces import MissionExecutor, VehicleTelemetrySource
from aegis.missions import Geofence, Mission, MissionPhase, MissionStatus
from aegis.recording import FlightRecorder
from aegis.safety import AlertTracker, Finding, SafetyLimits, evaluate

log = logging.getLogger(__name__)

_ACTIVE_PHASES = {MissionPhase.UPLOADING, MissionPhase.ACTIVE, MissionPhase.RETURNING}


class TelemetryHub:
    def __init__(
        self,
        source: VehicleTelemetrySource,
        executor: MissionExecutor | None = None,
        recorder: FlightRecorder | None = None,
        limits: SafetyLimits | None = None,
        rate_hz: float = 10.0,
    ) -> None:
        self.source = source
        self.executor = executor
        self.recorder = recorder
        self.limits = limits or SafetyLimits()
        self.period_s = 1.0 / rate_hz
        self.alerts = AlertTracker()
        self.geofence: Geofence | None = None  # fence of the mission being flown
        self.active_mission: Mission | None = None  # the mission last accepted by the vehicle
        # Additional advisory checks contributed by optional subsystems (perception...).
        self.extra_checks: list[Callable[[], list[Finding]]] = []

        self.state: VehicleState | None = None
        self.mission = MissionStatus()
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self._task: asyncio.Task[None] | None = None
        self._recording_open = False
        self.ticks = 0

    async def start(self) -> None:
        await self.source.start()
        self._task = asyncio.create_task(self._loop(), name="telemetry-hub")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        await self.source.stop()
        if self.recorder is not None:
            self.recorder.close()

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=5)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        self._subscribers.discard(queue)

    def snapshot(self) -> dict[str, Any]:
        playback = None
        if hasattr(self.source, "playback_time_s"):
            playback = {
                "time_s": self.source.playback_time_s,
                "duration_s": getattr(self.source, "duration_s", None),
                "speed": getattr(self.source, "speed", 1.0),
            }
        return {
            "type": "snapshot",
            "server_time_unix_s": time.time(),
            "state": self.state.model_dump(mode="json") if self.state else None,
            "mission": self.mission.model_dump(mode="json"),
            "alerts": [a.model_dump(mode="json") for a in self.alerts.active],
            "playback": playback,
            "autopilot_messages": [
                m.model_dump(mode="json") for m in list(getattr(self.source, "messages", ()))[-15:]
            ],
        }

    async def _loop(self) -> None:
        next_tick = time.monotonic()
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Telemetry hub tick failed")
            next_tick += self.period_s
            await asyncio.sleep(max(0.0, next_tick - time.monotonic()))

    async def tick(self) -> None:
        """One sample / evaluate / record / broadcast cycle (public for tests)."""
        state = await self.source.get_state()
        now = time.time()
        self.state = state
        self.ticks += 1

        if self.executor is not None:
            mission = await self.executor.mission_status()
            if mission != self.mission:
                self.mission = mission
                if self.recorder is not None and self._recording_open:
                    self.recorder.record_event("mission", mission.model_dump(mode="json"))
        elif hasattr(self.source, "replayed_mission_status"):
            self.mission = self.source.replayed_mission_status()
        fence = self.geofence if self.mission.phase in _ACTIVE_PHASES else None

        findings = evaluate(state, now, self.limits, fence)
        for check in self.extra_checks:
            findings.extend(check())
        changed: list[Alert] = self.alerts.update(state, findings, now)
        for alert in changed:
            log.warning(
                "ALERT %s [%s/%s] %s%s",
                alert.code,
                alert.severity.value,
                alert.provenance.value,
                alert.message,
                "" if alert.active else " (cleared)",
            )

        if self.recorder is not None and state.connected:
            if not self._recording_open:
                source = getattr(self.source, "source", state.source)
                self.recorder.open(state.vehicle_id, state.provenance, source)
                self._recording_open = True
                log.info("Recording flight to %s", self.recorder.path)
            self.recorder.record_state(state)
        if self.recorder is not None and self._recording_open:
            # Alerts are recorded even while the link is down - that is when they matter.
            for alert in changed:
                self.recorder.record_event("alert", alert.model_dump(mode="json"))

        message = self.snapshot()
        for queue in list(self._subscribers):
            if queue.full():  # slow client: drop the oldest snapshot, keep the newest
                with contextlib.suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
            queue.put_nowait(message)
