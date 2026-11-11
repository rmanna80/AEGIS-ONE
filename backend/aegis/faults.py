"""Fault injection for testing the monitors (SIMULATION ONLY, off by default).

Faults alter AEGIS's own copy of the telemetry after it has been read from the
simulator. The vehicle and its autopilot are not touched: they keep flying on
correct data. That is deliberate. The purpose is to check that the anomaly and
safety monitors notice bad data, not to disturb a vehicle.

Every altered snapshot carries the names of the active faults in
``injected_faults``, on screen and in recordings, so injected data is never
mistaken for what the simulator reported.

The one exception to "telemetry only" is ``external_hold``, which asks the
simulated autopilot to Hold without recording it as an operator command, to
exercise the unexpected-mode detector. It is refused unless the vehicle is an
attested simulator.
"""

from __future__ import annotations

import time
from typing import Any

from pydantic import BaseModel, Field

from aegis.contracts import BatteryState, Vec3, VehicleState

KINDS = {
    "gps_jump": "Shift the reported position by a fixed offset (east_m, north_m).",
    "gps_drift": "Shift the reported position by an offset growing at rate_m_s (eastward).",
    "freeze": "Repeat the snapshot from the moment of injection.",
    "battery_drop": "Report the battery at to_fraction.",
    "clock_rewind": "Set the autopilot clock back by seconds.",
    "external_hold": "Hold commanded outside the operator's command path (simulator only).",
}


class FaultRequest(BaseModel):
    kind: str
    duration_s: float = Field(default=12.0, gt=0.0, le=120.0)
    east_m: float = 25.0
    north_m: float = 0.0
    rate_m_s: float = Field(default=2.0, gt=0.0, le=20.0)
    to_fraction: float = Field(default=0.3, ge=0.0, le=1.0)
    seconds: float = Field(default=30.0, gt=0.0, le=3600.0)


class FaultInjector:
    """A hub state filter. With no active fault it returns the state untouched."""

    def __init__(self) -> None:
        self._active: dict[str, tuple[FaultRequest, float, float]] = {}  # kind -> (req, start, end)
        self._frozen: VehicleState | None = None
        self.injected_total = 0

    def inject(self, request: FaultRequest) -> None:
        if request.kind not in KINDS or request.kind == "external_hold":
            raise ValueError(f"Unknown telemetry fault {request.kind!r}.")
        now = time.monotonic()
        self._active[request.kind] = (request, now, now + request.duration_s)
        if request.kind == "freeze":
            self._frozen = None  # captured from the next state
        self.injected_total += 1

    def clear(self) -> None:
        self._active.clear()
        self._frozen = None

    def active(self) -> list[dict[str, Any]]:
        now = time.monotonic()
        return [
            {"kind": kind, "remaining_s": round(end - now, 1)}
            for kind, (_, _, end) in self._active.items()
            if end > now
        ]

    def apply(self, state: VehicleState) -> VehicleState:
        now = time.monotonic()
        for kind in [k for k, (_, _, end) in self._active.items() if end <= now]:
            del self._active[kind]
            if kind == "freeze":
                self._frozen = None
        if not self._active or not state.connected:
            return state
        changes: dict[str, Any] = {}
        offset_east = offset_north = 0.0
        for kind, (request, start, _) in self._active.items():
            if kind == "gps_jump":
                offset_east += request.east_m
                offset_north += request.north_m
            elif kind == "gps_drift":
                offset_east += request.rate_m_s * (now - start)
            elif kind == "battery_drop" and state.battery is not None:
                changes["battery"] = BatteryState(
                    voltage_v=state.battery.voltage_v, remaining_fraction=request.to_fraction
                )
            elif kind == "clock_rewind" and state.vehicle_time_s is not None:
                changes["vehicle_time_s"] = state.vehicle_time_s - request.seconds
        if (offset_east or offset_north) and state.position_enu_m is not None:
            p = state.position_enu_m
            changes["position_enu_m"] = Vec3(x=p.x + offset_east, y=p.y + offset_north, z=p.z)
        names = tuple(sorted(self._active))
        if "freeze" in self._active:
            if self._frozen is None:
                self._frozen = state
            return self._frozen.model_copy(
                update={"received_at_unix_s": state.received_at_unix_s, "injected_faults": names}
            )
        return state.model_copy(update={**changes, "injected_faults": names})
