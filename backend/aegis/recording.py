"""Flight recording and replay.

A recording is a JSON-Lines file. Each line is ``{"kind": ..., "t": ..., "data": ...}``
where ``t`` is seconds since the start of the recording:

* ``header``  - recorder version, vehicle id, original provenance and source
* ``state``   - a :class:`aegis.contracts.VehicleState`
* ``mission`` - a :class:`aegis.missions.MissionStatus` change
* ``alert``   - a :class:`aegis.contracts.Alert`

JSONL is append-only, so a crash loses at most the last line.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from pathlib import Path
from typing import IO, Any

from aegis.contracts import Provenance, VehicleState
from aegis.missions import MissionStatus

FORMAT_VERSION = 1


class FlightRecorder:
    """Appends states and events to a JSONL file."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._file: IO[str] | None = None
        self._t0 = 0.0
        self._last_state_json: str | None = None

    def open(self, vehicle_id: str, provenance: Provenance, source: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._file = self.path.open("w", encoding="utf-8")
        self._t0 = time.monotonic()
        self._write(
            "header",
            {
                "format_version": FORMAT_VERSION,
                "vehicle_id": vehicle_id,
                "provenance": provenance.value,
                "source": source,
                "started_at_unix_s": time.time(),
            },
        )

    def _write(self, kind: str, data: Any) -> None:
        if self._file is None:
            return
        record = {"kind": kind, "t": round(time.monotonic() - self._t0, 4), "data": data}
        self._file.write(json.dumps(record, separators=(",", ":")) + "\n")
        self._file.flush()

    def record_state(self, state: VehicleState) -> None:
        self._write("state", state.model_dump(mode="json"))

    def record_event(self, kind: str, data: dict[str, Any]) -> None:
        self._write(kind, data)

    def close(self) -> None:
        if self._file is not None:
            self._file.close()
            self._file = None


def load_recording(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Return ``(header, records)``. Raises ``ValueError`` on a malformed file."""
    header: dict[str, Any] | None = None
    records: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as f:
        for line_no, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
                kind, _t, data = record["kind"], float(record["t"]), record["data"]
            except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
                raise ValueError(f"{path}:{line_no}: malformed record ({exc})") from exc
            if kind == "header":
                header = data
            else:
                records.append(record)
    if header is None:
        raise ValueError(f"{path}: missing header record")
    if header.get("format_version") != FORMAT_VERSION:
        raise ValueError(f"{path}: unsupported format_version {header.get('format_version')}")
    return header, records


class RecordedTelemetryAdapter:
    """Replays a recording as a :class:`VehicleTelemetrySource`.

    Every replayed state is re-labelled ``provenance=replayed`` and keeps the
    original provenance in ``recorded_provenance`` so nothing downstream can
    mistake a replay for a live vehicle. Replay is read-only: this class does
    not implement ``MissionExecutor``.
    """

    provenance = Provenance.REPLAYED

    def __init__(self, path: Path, speed: float = 1.0, loop: bool = True) -> None:
        if speed <= 0.0:
            raise ValueError("speed must be positive")
        self.path = path
        self.speed = speed
        self.loop = loop
        header, records = load_recording(path)
        self.header = header
        self.vehicle_id = str(header["vehicle_id"])
        self._original = Provenance(header["provenance"])
        self._states = [r for r in records if r["kind"] == "state"]
        if not self._states:
            raise ValueError(f"{path}: recording contains no state records")
        self._missions = [r for r in records if r["kind"] == "mission"]
        self.duration_s = float(self._states[-1]["t"])
        self._index = 0
        self.playback_time_s = 0.0
        self.finished = False
        self._task: asyncio.Task[None] | None = None

    @property
    def source(self) -> str:
        return f"recording:{self.path.name}"

    async def start(self) -> None:
        self._task = asyncio.create_task(self._play(), name="replay")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _play(self) -> None:
        while True:
            started = time.monotonic()
            for i, record in enumerate(self._states):
                due = started + record["t"] / self.speed
                delay = due - time.monotonic()
                if delay > 0:
                    await asyncio.sleep(delay)
                self._index = i
                self.playback_time_s = record["t"]
            if not self.loop:
                self.finished = True
                return

    def replayed_mission_status(self) -> MissionStatus:
        """The mission status that was current at the playback position."""
        latest: dict[str, Any] | None = None
        for record in self._missions:
            if record["t"] > self.playback_time_s:
                break
            latest = record["data"]
        return MissionStatus.model_validate(latest) if latest else MissionStatus()

    async def get_state(self) -> VehicleState:
        data = dict(self._states[self._index]["data"])
        data["provenance"] = Provenance.REPLAYED.value
        data["recorded_provenance"] = self._original.value
        data["source"] = self.source
        data["received_at_unix_s"] = time.time()
        return VehicleState.model_validate(data)
