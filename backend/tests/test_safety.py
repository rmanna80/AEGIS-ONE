import time

from aegis.contracts import BatteryState, GpsQuality, Severity, Vec3
from aegis.missions import Geofence
from aegis.safety import AlertTracker, SafetyLimits, evaluate
from conftest import make_state

LIMITS = SafetyLimits()
HEALTHY = make_state().health
assert HEALTHY is not None
FENCE = Geofence(
    polygon_enu_m=[(-50.0, -50.0), (50.0, -50.0), (50.0, 50.0), (-50.0, 50.0)],
    max_altitude_rel_m=30.0,
)


def codes(findings: list[tuple[str, Severity, str]]) -> set[str]:
    return {code for code, _, _ in findings}


def test_healthy_state_raises_nothing() -> None:
    assert evaluate(make_state(), time.time(), LIMITS, FENCE) == []


def test_lost_link_is_critical_and_masks_other_checks() -> None:
    state = make_state(connected=False, battery=BatteryState(remaining_fraction=0.01))
    findings = evaluate(state, time.time(), LIMITS, FENCE)
    assert findings == [("link.lost", Severity.CRITICAL, "No link to the vehicle.")]


def test_stale_telemetry() -> None:
    now = time.time()
    state = make_state(received_at_unix_s=now - 5.0)
    assert codes(evaluate(state, now, LIMITS)) == {"telemetry.stale"}


def test_battery_thresholds() -> None:
    now = time.time()
    low = make_state(battery=BatteryState(remaining_fraction=0.20))
    critical = make_state(battery=BatteryState(remaining_fraction=0.10))
    assert evaluate(low, now, LIMITS)[0][:2] == ("battery.low", Severity.WARNING)
    assert evaluate(critical, now, LIMITS)[0][:2] == ("battery.critical", Severity.CRITICAL)


def test_missing_battery_data_is_not_an_alert() -> None:
    assert evaluate(make_state(battery=None), time.time(), LIMITS) == []


def test_gps_outage_and_degradation() -> None:
    now = time.time()
    outage = make_state(gps=GpsQuality(fix_type="NO_FIX", num_satellites=0))
    weak = make_state(gps=GpsQuality(fix_type="FIX_3D", num_satellites=4))
    assert codes(evaluate(outage, now, LIMITS)) == {"gps.no_fix"}
    assert codes(evaluate(weak, now, LIMITS)) == {"gps.degraded"}


def test_invalid_position_estimate_while_airborne() -> None:
    now = time.time()
    bad = HEALTHY.model_copy(update={"local_position_ok": False})
    assert codes(evaluate(make_state(health=bad), now, LIMITS)) == {"estimator.position"}
    # On the ground the same flag is a pre-arm condition, not an in-flight emergency.
    assert evaluate(make_state(health=bad, in_air=False), now, LIMITS) == []


def test_geofence_near_breach_and_ceiling() -> None:
    now = time.time()
    near = make_state(position_enu_m=Vec3(x=47.0, y=0.0, z=10.0))
    outside = make_state(position_enu_m=Vec3(x=60.0, y=0.0, z=10.0))
    high = make_state(relative_altitude_m=35.0)
    assert codes(evaluate(near, now, LIMITS, FENCE)) == {"geofence.near"}
    assert codes(evaluate(outside, now, LIMITS, FENCE)) == {"geofence.breach"}
    assert codes(evaluate(high, now, LIMITS, FENCE)) == {"geofence.ceiling"}


def test_no_geofence_checks_without_a_fence() -> None:
    outside = make_state(position_enu_m=Vec3(x=600.0, y=0.0, z=10.0))
    assert evaluate(outside, time.time(), LIMITS, None) == []


def test_tracker_raises_once_escalates_and_clears() -> None:
    tracker = AlertTracker()
    now = time.time()
    state = make_state()
    low = [("battery.low", Severity.WARNING, "Battery at 20%.")]

    raised = tracker.update(state, low, now)
    assert [a.code for a in raised] == ["battery.low"] and raised[0].active
    assert tracker.update(state, low, now + 1) == []  # no duplicate while it persists

    critical = [("battery.low", Severity.CRITICAL, "Battery at 10%.")]
    escalated = tracker.update(state, critical, now + 2)
    assert escalated[0].severity is Severity.CRITICAL

    cleared = tracker.update(state, [], now + 3)
    assert len(cleared) == 1 and cleared[0].active is False
    assert tracker.active == []
    assert len(tracker.history) == 3


def test_alerts_carry_provenance() -> None:
    tracker = AlertTracker()
    state = make_state()
    alert = tracker.update(state, [("link.lost", Severity.CRITICAL, "x")], time.time())[0]
    assert alert.provenance is state.provenance
    assert alert.vehicle_id == state.vehicle_id


def test_slow_simulation_is_a_warning_not_a_link_fault() -> None:
    now = time.time()
    slow = make_state(sim_real_time_factor=0.32)
    findings = evaluate(slow, now, LIMITS)
    assert [(code, severity) for code, severity, _ in findings] == [("sim.slow", Severity.WARNING)]
    assert "0.32x" in findings[0][2]
    assert evaluate(make_state(sim_real_time_factor=0.95), now, LIMITS) == []
    assert evaluate(make_state(sim_real_time_factor=None), now, LIMITS) == []
