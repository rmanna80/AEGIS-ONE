"""Advisory safety monitors.

These monitors **observe** telemetry and raise operator alerts. They never
command the vehicle and never override the flight controller: PX4's own
failsafes (geofence, low battery, data-link loss) remain the authority. Each
check is a pure function of the latest state and the clock, so behaviour is
deterministic and unit-testable.
"""

from __future__ import annotations

from dataclasses import dataclass

from aegis.contracts import Alert, Severity, VehicleState
from aegis.missions import Geofence, distance_to_polygon_edge_m, point_in_polygon


@dataclass(frozen=True)
class SafetyLimits:
    stale_after_s: float = 2.0
    battery_warning_fraction: float = 0.25
    battery_critical_fraction: float = 0.15
    geofence_margin_m: float = 5.0
    min_satellites: int = 6


Finding = tuple[str, Severity, str]  # (code, severity, message)


def evaluate(
    state: VehicleState,
    now_unix_s: float,
    limits: SafetyLimits,
    geofence: Geofence | None = None,
) -> list[Finding]:
    """Return every condition that currently holds for this state."""
    findings: list[Finding] = []

    if not state.connected:
        findings.append(("link.lost", Severity.CRITICAL, "No link to the vehicle."))
        return findings  # everything else would be judged on stale data

    age = now_unix_s - state.received_at_unix_s
    if age > limits.stale_after_s:
        findings.append(("telemetry.stale", Severity.CRITICAL, f"Telemetry is {age:.1f} s old."))
        return findings

    if state.battery is not None and state.battery.remaining_fraction is not None:
        remaining = state.battery.remaining_fraction
        if remaining <= limits.battery_critical_fraction:
            findings.append(("battery.critical", Severity.CRITICAL, f"Battery at {remaining:.0%}."))
        elif remaining <= limits.battery_warning_fraction:
            findings.append(("battery.low", Severity.WARNING, f"Battery at {remaining:.0%}."))

    if state.gps is not None:
        if state.gps.fix_type in ("NO_GPS", "NO_FIX"):
            findings.append(("gps.no_fix", Severity.CRITICAL, "No GNSS fix."))
        elif state.gps.num_satellites < limits.min_satellites:
            findings.append(
                (
                    "gps.degraded",
                    Severity.WARNING,
                    f"Only {state.gps.num_satellites} satellites in view.",
                )
            )

    if state.health is not None and state.in_air and not state.health.local_position_ok:
        findings.append(
            ("estimator.position", Severity.CRITICAL, "Local position estimate is invalid.")
        )

    if geofence is not None and state.position_enu_m is not None:
        here = (state.position_enu_m.x, state.position_enu_m.y)
        altitude = state.relative_altitude_m or 0.0
        if not point_in_polygon(here, geofence.polygon_enu_m):
            findings.append(
                ("geofence.breach", Severity.CRITICAL, "Vehicle is outside the geofence.")
            )
        elif altitude > geofence.max_altitude_rel_m:
            findings.append(
                (
                    "geofence.ceiling",
                    Severity.CRITICAL,
                    f"Altitude {altitude:.1f} m is above the "
                    f"{geofence.max_altitude_rel_m:.0f} m ceiling.",
                )
            )
        else:
            margin = distance_to_polygon_edge_m(here, geofence.polygon_enu_m)
            if margin < limits.geofence_margin_m:
                findings.append(
                    (
                        "geofence.near",
                        Severity.WARNING,
                        f"{margin:.1f} m from the geofence boundary.",
                    )
                )
    return findings


class AlertTracker:
    """Turns per-tick findings into raise / clear alert transitions."""

    def __init__(self) -> None:
        self._active: dict[str, Alert] = {}
        self._counter = 0
        self.history: list[Alert] = []

    @property
    def active(self) -> list[Alert]:
        return list(self._active.values())

    def update(
        self, state: VehicleState, findings: list[Finding], now_unix_s: float
    ) -> list[Alert]:
        """Return the alerts that changed (newly raised, escalated or cleared)."""
        changed: list[Alert] = []
        current = {code: (severity, message) for code, severity, message in findings}

        for code, (severity, message) in current.items():
            existing = self._active.get(code)
            if existing is None or existing.severity is not severity:
                self._counter += 1
                alert = Alert(
                    alert_id=f"A{self._counter:05d}",
                    code=code,
                    severity=severity,
                    message=message,
                    vehicle_id=state.vehicle_id,
                    provenance=state.provenance,
                    raised_at_unix_s=now_unix_s,
                )
                self._active[code] = alert
                self.history.append(alert)
                changed.append(alert)
            elif existing.message != message:
                self._active[code] = existing.model_copy(update={"message": message})

        for code in [c for c in self._active if c not in current]:
            cleared = self._active.pop(code).model_copy(update={"active": False})
            self.history.append(cleared)
            changed.append(cleared)

        del self.history[:-500]
        return changed
