"""Telemetry anomaly detection (advisory).

Explainable consistency checks on what the vehicle reports. Each one states the
physical or procedural rule that was broken. They exist to notice sensor faults,
navigation spoofing or drift, replayed or frozen data, and control inputs that did
not come from this operator.

The monitor only raises alerts. It is never given the mission executor, so it
cannot command the vehicle and cannot override the flight controller's own
failsafes: deciding what to do about an alert stays with the operator.
"""

from __future__ import annotations

import math
import time
from collections import deque
from dataclasses import dataclass

from aegis.contracts import Severity, VehicleState
from aegis.hub import TelemetryHub
from aegis.missions import Mission, MissionPhase
from aegis.safety import Finding


@dataclass(frozen=True)
class AnomalyLimits:
    jump_window_s: float = 1.0
    # Clean recorded flights peak at 2.9 m (1 s) and 5.8 m (10 s); see docs/testing.
    jump_residual_m: float = 5.0  # position moved this much more than velocity explains, in 1 s
    drift_window_s: float = 10.0
    drift_residual_m: float = 10.0  # the same over 10 s (slow spoofing or estimator drift)
    clock_reverse_s: float = 0.5
    frozen_after_s: float = 2.0
    battery_rise_fraction: float = 0.05
    battery_drop_fraction: float = 0.2
    battery_window_s: float = 5.0
    off_route_m: float = 15.0
    command_grace_s: float = 6.0
    latch_s: float = 15.0  # an alert stays up this long after its last detection


@dataclass(frozen=True)
class _Sample:
    t: float  # seconds on the clock used for physics (vehicle clock when it has one)
    wall: float
    east: float
    north: float
    up: float
    v_east: float
    v_north: float
    v_up: float


# Modes PX4 enters by itself during a mission (completion, its own failsafes).
_EXPECTED_AFTER_MISSION = {"MISSION", "RETURN_TO_LAUNCH", "LAND", "TAKEOFF"}

DETECTORS = {
    "nav.position_jump": "Position moved further in one second than the reported velocity "
    "allows (GNSS glitch or spoofing).",
    "nav.position_drift": "Position and integrated velocity disagree over ten seconds "
    "(slow spoofing or estimator drift).",
    "telemetry.clock_reversed": "The autopilot's clock went backwards (replayed data or a "
    "reboot in flight).",
    "telemetry.frozen": "Telemetry keeps arriving but nothing in it changes while airborne.",
    "battery.implausible": "Battery level rose in flight or fell faster than a battery can.",
    "mission.off_route": "The vehicle is far from the route of the mission it is flying.",
    "control.unexpected_mode": "The flight mode left the mission without a command from "
    "this operator or a normal end of mission.",
    "control.unexpected_arm": "The vehicle armed without a mission from this operator.",
}


def _distance_to_route(point: tuple[float, float], route: list[tuple[float, float]]) -> float:
    best = math.inf
    for a, b in zip(route[:-1], route[1:], strict=True):
        ax, ay = b[0] - a[0], b[1] - a[1]
        length2 = ax * ax + ay * ay
        t = 0.0
        if length2 > 0.0:
            t = max(0.0, min(1.0, ((point[0] - a[0]) * ax + (point[1] - a[1]) * ay) / length2))
        best = min(best, math.dist(point, (a[0] + t * ax, a[1] + t * ay)))
    return best


class AnomalyMonitor:
    def __init__(self, hub: TelemetryHub, limits: AnomalyLimits | None = None) -> None:
        self.hub = hub
        self.limits = limits or AnomalyLimits()
        self._samples: deque[_Sample] = deque()
        self._battery: deque[tuple[float, float]] = deque()
        self._last_state: VehicleState | None = None
        self._last_change_wall = 0.0
        self._prev_mode: str | None = None
        self._prev_phase = MissionPhase.IDLE
        self._prev_armed: bool | None = None
        self._route: list[tuple[float, float]] | None = None
        self._route_for: str | None = None
        self._latched: dict[str, tuple[float, Severity, str]] = {}
        self.detections: dict[str, int] = dict.fromkeys(DETECTORS, 0)

    # ------------------------------------------------------------------ plumbing

    def _raise(self, code: str, severity: Severity, message: str, now: float) -> None:
        if code not in self._latched or now - self._latched[code][0] > 1.0:
            self.detections[code] += 1  # count detections, not every tick of one
        self._latched[code] = (now, severity, message)

    def _commanded_recently(self, now: float, grace_s: float) -> bool:
        """Whether AEGIS (the operator, or its own Hold) commanded this vehicle just now."""
        last = getattr(self.hub.source, "last_command_at", None)
        return last is not None and now - last <= grace_s

    def findings(self) -> list[Finding]:
        """Hub check: examine the newest state, return the alerts currently up."""
        state = self.hub.state
        now = time.monotonic()
        if state is not None and state is not self._last_state:
            self.observe(state, self.hub.mission.phase, self.hub.active_mission, now)
        return [
            (code, severity, message)
            for code, (at, severity, message) in self._latched.items()
            if now - at <= self.limits.latch_s
        ]

    # ----------------------------------------------------------------- detectors

    def observe(
        self, state: VehicleState, phase: MissionPhase, mission: Mission | None, now: float
    ) -> None:
        """Run every detector on one new state (public for tests and offline analysis)."""
        previous = self._last_state
        self._last_state = state
        if not state.connected:
            self._samples.clear()
            self._battery.clear()
            self._prev_mode, self._prev_armed = None, None
            return
        limits = self.limits

        # --- clock
        clock = state.vehicle_time_s
        if (
            previous is not None
            and previous.connected
            and clock is not None
            and previous.vehicle_time_s is not None
            and clock < previous.vehicle_time_s - limits.clock_reverse_s
        ):
            self._raise(
                "telemetry.clock_reversed",
                Severity.CRITICAL,
                f"The autopilot clock went back {previous.vehicle_time_s - clock:.1f} s. "
                "The data may be replayed, or the autopilot restarted.",
                now,
            )
            self._samples.clear()

        # A change of local frame (a fleet adopting a shared origin) moves every position
        # at once; that is bookkeeping, not motion.
        if previous is not None and self._frame_moved(previous, state):
            self._samples.clear()

        # --- frozen
        if previous is not None and self._same_content(previous, state):
            if state.in_air and now - self._last_change_wall > limits.frozen_after_s:
                self._raise(
                    "telemetry.frozen",
                    Severity.CRITICAL,
                    f"Telemetry has not changed for {now - self._last_change_wall:.0f} s while "
                    "airborne. What is shown may not be where the vehicle is.",
                    now,
                )
        else:
            self._last_change_wall = now

        # --- position against velocity
        p, v = state.position_enu_m, state.velocity_ned_m_s
        if p is not None and v is not None:
            t = clock if clock is not None else state.received_at_unix_s
            if self._samples and t <= self._samples[-1].t:
                pass  # no new time step (stalled clock): nothing to integrate
            else:
                # Height above home comes straight from the autopilot. The ENU height is
                # not used: PX4 keeps correcting the home altitude in flight, which
                # would step it by up to a metre every couple of seconds.
                up = state.relative_altitude_m if state.relative_altitude_m is not None else p.z
                self._samples.append(_Sample(t, now, p.x, p.y, up, v.y, v.x, -v.z))
            while self._samples and t - self._samples[0].t > limits.drift_window_s + 1.0:
                self._samples.popleft()
            jump = self._residual(limits.jump_window_s)
            if jump is not None and jump > limits.jump_residual_m:
                self._raise(
                    "nav.position_jump",
                    Severity.CRITICAL,
                    f"Position moved {jump:.0f} m more in {limits.jump_window_s:.0f} s than the "
                    "reported velocity allows. Possible GNSS glitch or spoofing.",
                    now,
                )
            else:
                drift = self._residual(limits.drift_window_s)
                if drift is not None and drift > limits.drift_residual_m:
                    self._raise(
                        "nav.position_drift",
                        Severity.WARNING,
                        f"Position and velocity disagree by {drift:.0f} m over "
                        f"{limits.drift_window_s:.0f} s. Possible slow spoofing or "
                        "estimator drift.",
                        now,
                    )

        # --- battery
        if state.battery is not None and state.battery.remaining_fraction is not None:
            level = state.battery.remaining_fraction
            self._battery.append((now, level))
            while self._battery and now - self._battery[0][0] > limits.battery_window_s:
                self._battery.popleft()
            oldest = self._battery[0][1]
            if (
                state.in_air
                and level - min(b for _, b in self._battery) > limits.battery_rise_fraction
            ):
                self._raise(
                    "battery.implausible",
                    Severity.WARNING,
                    "The battery level rose in flight. The reading cannot be trusted.",
                    now,
                )
            elif oldest - level > limits.battery_drop_fraction:
                self._raise(
                    "battery.implausible",
                    Severity.WARNING,
                    f"The battery fell {100 * (oldest - level):.0f}% in "
                    f"{limits.battery_window_s:.0f} s. A cell fault or a bad reading.",
                    now,
                )

        # --- route
        if phase is MissionPhase.ACTIVE and mission is not None and p is not None:
            if self._route_for != mission.mission_id:
                self._route_for = mission.mission_id
                self._route = [(p.x, p.y)] + [(w.east_m, w.north_m) for w in mission.waypoints]
            if self._route is not None and state.flight_mode == "MISSION":
                off = _distance_to_route((p.x, p.y), self._route)
                if off > limits.off_route_m:
                    self._raise(
                        "mission.off_route",
                        Severity.WARNING,
                        f"The vehicle is {off:.0f} m from its mission route.",
                        now,
                    )
        elif phase not in (MissionPhase.ACTIVE, MissionPhase.UPLOADING):
            self._route_for = None

        # --- control
        mode = state.flight_mode
        left_mission = self._prev_mode == "MISSION" and self._prev_phase is MissionPhase.ACTIVE
        left_return = self._prev_mode == "RETURN_TO_LAUNCH" and bool(state.in_air)
        if (
            (left_mission or left_return)
            and mode is not None
            and mode not in _EXPECTED_AFTER_MISSION
            and not self._commanded_recently(now, limits.command_grace_s)
        ):
            self._raise(
                "control.unexpected_mode",
                Severity.CRITICAL,
                f"The flight mode changed from {self._prev_mode} to {mode} without a command "
                "from this operator. Something else may be commanding the vehicle.",
                now,
            )
        if (
            self._prev_armed is False
            and state.armed is True
            and not self._commanded_recently(now, 2 * limits.command_grace_s)
        ):
            self._raise(
                "control.unexpected_arm",
                Severity.WARNING,
                "The vehicle armed without a mission from this operator.",
                now,
            )
        self._prev_mode, self._prev_phase, self._prev_armed = mode, phase, state.armed

    @staticmethod
    def _frame_moved(a: VehicleState, b: VehicleState) -> bool:
        """Whether the local frame's origin moved horizontally (more than about 1 m)."""
        if a.origin is None or b.origin is None:
            return a.origin is not b.origin
        ga, gb = a.origin.geodetic, b.origin.geodetic
        return (
            abs(ga.latitude_deg - gb.latitude_deg) > 1e-5
            or abs(ga.longitude_deg - gb.longitude_deg) > 1e-5
        )

    @staticmethod
    def _same_content(a: VehicleState, b: VehicleState) -> bool:
        return (
            a.position_enu_m == b.position_enu_m
            and a.attitude == b.attitude
            and a.velocity_ned_m_s == b.velocity_ned_m_s
            and a.vehicle_time_s == b.vehicle_time_s
        )

    def _residual(self, window_s: float) -> float | None:
        """|displacement - integral of velocity| over the last ``window_s``; None if too short."""
        samples = self._samples
        if len(samples) < 3:
            return None
        end = samples[-1]
        start_index = None
        for i in range(len(samples) - 1, -1, -1):
            if end.t - samples[i].t >= window_s:
                start_index = i
                break
        if start_index is None:
            return None
        window = list(samples)[start_index:]
        if max(b.t - a.t for a, b in zip(window[:-1], window[1:], strict=True)) > 0.5 * window_s:
            return None  # a gap in the data: the integral would be meaningless
        east = north = up = 0.0
        for a, b in zip(window[:-1], window[1:], strict=True):
            dt = b.t - a.t
            east += 0.5 * (a.v_east + b.v_east) * dt
            north += 0.5 * (a.v_north + b.v_north) * dt
            up += 0.5 * (a.v_up + b.v_up) * dt
        start = window[0]
        return math.sqrt(
            (end.east - start.east - east) ** 2
            + (end.north - start.north - north) ** 2
            + (end.up - start.up - up) ** 2
        )

    def status(self) -> dict[str, object]:
        now = time.monotonic()
        return {
            "enabled": True,
            "advisory_only": True,
            "detectors": DETECTORS,
            "detections": self.detections,
            "active": [
                c for c, (at, _, _) in self._latched.items() if now - at <= self.limits.latch_s
            ],
        }
