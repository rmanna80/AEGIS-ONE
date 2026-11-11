"""Fleet: shared frame, route conflicts, separation monitor, per-vehicle API."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from aegis.api import create_app
from aegis.config import Settings
from aegis.contracts import Severity, Vec3
from aegis.fleet import (
    Fleet,
    SeparationLimits,
    _segment_distance_2d,
    route_conflicts,
    route_legs,
)
from aegis.hub import TelemetryHub
from aegis.missions import MissionPhase, Waypoint
from conftest import make_mission
from test_api import AUTH, TOKEN, FakeVehicle

LIMITS = SeparationLimits()


def vehicle(
    vehicle_id: str, east: float, north: float, up: float, in_air: bool = True
) -> FakeVehicle:
    fake = FakeVehicle()
    fake.vehicle_id = vehicle_id
    fake.overrides = {
        "vehicle_id": vehicle_id,
        "position_enu_m": Vec3(x=east, y=north, z=up),
        "in_air": in_air,
    }
    return fake


async def make_fleet(*vehicles: FakeVehicle, **kwargs: Any) -> Fleet:
    hubs = [TelemetryHub(v, v) for v in vehicles]
    for hub in hubs:
        await hub.tick()
    return Fleet(hubs, **kwargs)


def route(points: list[tuple[float, float, float]], mission_id: str = "m") -> Any:
    mission = make_mission()
    waypoints = [Waypoint(east_m=e, north_m=n, altitude_rel_m=u) for e, n, u in points]
    return mission.model_copy(update={"mission_id": mission_id, "waypoints": waypoints})


# ------------------------------------------------------------------------- geometry


def test_segment_distance() -> None:
    assert _segment_distance_2d((0, 0), (10, 0), (5, -5), (5, 5)) == 0.0  # crossing
    assert _segment_distance_2d((0, 0), (10, 0), (0, 4), (10, 4)) == pytest.approx(4.0)
    assert _segment_distance_2d((0, 0), (10, 0), (13, 4), (20, 4)) == pytest.approx(5.0)
    assert _segment_distance_2d((0, 0), (0, 0), (3, 4), (3, 4)) == pytest.approx(5.0)


def test_route_conflicts_need_both_nearness_and_similar_height() -> None:
    a = route_legs(route([(0, 0, 20), (40, 0, 20)]), None)
    crossing_same_height = route_legs(route([(20, -20, 21), (20, 20, 21)]), None)
    crossing_higher = route_legs(route([(20, -20, 26), (20, 20, 26)]), None)
    parallel_far = route_legs(route([(0, 15, 20), (40, 15, 20)]), None)
    parallel_near = route_legs(route([(0, 6, 20), (40, 6, 20)]), None)
    assert route_conflicts(a, crossing_same_height, LIMITS) == (0.0, 1.0)
    assert route_conflicts(a, crossing_higher, LIMITS) is None
    assert route_conflicts(a, parallel_far, LIMITS) is None
    assert route_conflicts(a, parallel_near, LIMITS) == (pytest.approx(6.0), 0.0)
    # A climbing leg occupies its whole height band.
    climbing = route_legs(route([(20, -20, 10), (20, 20, 40)]), None)
    assert route_conflicts(a, climbing, LIMITS) == (0.0, 0.0)


def test_route_legs_include_the_climb_out_from_the_start() -> None:
    legs = route_legs(route([(10, 0, 20), (30, 0, 20)]), (0.0, -12.0, 0.0))
    assert legs[0] == ((0.0, -12.0, 20.0), (10.0, 0.0, 20.0))
    assert len(legs) == 2


# ----------------------------------------------------------------------- separation


async def test_separation_counts_only_airborne_vehicles() -> None:
    fleet = await make_fleet(vehicle("a", 0, 0, 20), vehicle("b", 3, 4, 20, in_air=False))
    assert fleet.separations() == [] and fleet.findings() == []
    fleet = await make_fleet(vehicle("a", 0, 0, 20), vehicle("b", 3, 4, 22))
    [pair] = fleet.separations()
    assert (pair.a, pair.b, pair.horizontal_m, pair.vertical_m) == ("a", "b", 5.0, 2.0)


async def test_alert_levels_follow_the_limits() -> None:
    close = await make_fleet(vehicle("a", 0, 0, 20), vehicle("b", 5, 0, 21))
    assert [(c, s) for c, s, _ in close.findings()] == [
        ("fleet.separation_lost", Severity.CRITICAL)
    ]
    near = await make_fleet(vehicle("a", 0, 0, 20), vehicle("b", 10, 0, 21))
    assert [(c, s) for c, s, _ in near.findings()] == [("fleet.separation_low", Severity.WARNING)]
    stacked = await make_fleet(vehicle("a", 0, 0, 20), vehicle("b", 1, 0, 27))
    assert stacked.findings() == []  # directly above, but 7 m of height
    apart = await make_fleet(vehicle("a", 0, 0, 20), vehicle("b", 40, 0, 20))
    assert apart.findings() == []


async def test_too_close_holds_both_once_and_rearms_after_they_separate() -> None:
    a, b = vehicle("a", 0, 0, 20), vehicle("b", 4, 0, 21)
    fleet = await make_fleet(a, b)
    await fleet.check_separation()
    await fleet.check_separation()
    assert len(a.commands) == 1 and a.commands[0].startswith("hold:Too close to b")
    assert len(b.commands) == 1 and b.commands[0].startswith("hold:Too close to a")
    assert fleet.holds_commanded == 1
    assert a.status.phase is MissionPhase.BLOCKED
    assert fleet.min_separation_seen is not None and fleet.min_separation_seen.horizontal_m == 4.0

    b.overrides["position_enu_m"] = Vec3(x=9.0, y=0.0, z=21.0)  # apart, but still "near"
    await fleet.hubs[1].tick()
    await fleet.check_separation()
    b.overrides["position_enu_m"] = Vec3(x=4.0, y=0.0, z=21.0)
    await fleet.hubs[1].tick()
    await fleet.check_separation()
    assert fleet.holds_commanded == 1  # not re-armed until clearly separated

    b.overrides["position_enu_m"] = Vec3(x=30.0, y=0.0, z=21.0)
    await fleet.hubs[1].tick()
    await fleet.check_separation()
    b.overrides["position_enu_m"] = Vec3(x=4.0, y=0.0, z=21.0)
    await fleet.hubs[1].tick()
    await fleet.check_separation()
    assert fleet.holds_commanded == 2


async def test_auto_hold_can_be_turned_off_and_alerts_remain() -> None:
    a, b = vehicle("a", 0, 0, 20), vehicle("b", 4, 0, 21)
    fleet = await make_fleet(a, b, auto_hold=False)
    await fleet.check_separation()
    assert a.commands == [] and b.commands == []
    assert fleet.findings()[0][0] == "fleet.separation_lost"


async def test_vehicles_get_distinct_return_altitudes() -> None:
    a, b, c = vehicle("a", 0, 0, 0), vehicle("b", 0, -12, 0), vehicle("c", 0, -24, 0)
    for fake in (a, b, c):
        fake.return_altitude_m = None  # type: ignore[attr-defined]
    await make_fleet(a, b, c)
    assert [v.return_altitude_m for v in (a, b, c)] == [30.0, 36.0, 42.0]  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------- routes


async def test_route_issues_only_against_missions_being_flown() -> None:
    a, b = vehicle("a", 0, 0, 0, in_air=False), vehicle("b", 0, -12, 0, in_air=False)
    fleet = await make_fleet(a, b)
    mine = route([(20, -20, 20), (20, 20, 20)])
    assert fleet.route_issues("b", mine) == []  # nobody else is flying
    fleet.hubs[0].active_mission = route([(0, 0, 20), (40, 0, 20)])
    fleet.hubs[0].mission = fleet.hubs[0].mission.model_copy(update={"phase": MissionPhase.ACTIVE})
    [issue] = fleet.route_issues("b", mine)
    assert "route a is flying" in issue and "0 m" in issue
    assert fleet.route_issues("a", mine) == []  # a vehicle does not conflict with itself
    fleet.hubs[0].mission = fleet.hubs[0].mission.model_copy(
        update={"phase": MissionPhase.COMPLETED}
    )
    assert fleet.route_issues("b", mine) == []


# ------------------------------------------------------------------------------- API


def fleet_client(tmp_path: Path, *fakes: FakeVehicle) -> tuple[TestClient, Fleet]:
    hubs = [TelemetryHub(v, v, rate_hz=50.0) for v in fakes]
    fleet = Fleet(hubs)
    settings = Settings(
        operator_token=TOKEN, record_flights=False, data_dir=tmp_path, frontend_dist=tmp_path / "x"
    )
    return TestClient(create_app(settings, hubs[0], fleet=fleet)), fleet


def test_fleet_api_commands_one_vehicle_or_all(tmp_path: Path) -> None:
    a, b = vehicle("a", 0, 0, 0, in_air=False), vehicle("b", 0, -12, 0, in_air=False)
    client, fleet = fleet_client(tmp_path, a, b)
    with client:
        status = client.get("/api/fleet").json()
        assert status["enabled"] and status["size"] == 2
        assert [v["vehicle_id"] for v in status["vehicles"]] == ["a", "b"]

        mission = route([(10, 10, 15), (20, 10, 15)], "for-b").model_dump()
        assert client.post("/api/fleet/b/missions", json=mission).status_code == 401
        assert (
            client.post("/api/fleet/nobody/missions", json=mission, headers=AUTH).status_code == 404
        )
        result = client.post("/api/fleet/b/missions", json=mission, headers=AUTH).json()
        assert result["accepted"] and b.commands == ["submit_mission"] and a.commands == []
        assert fleet.hubs[1].active_mission is not None

        # The same route for the other vehicle is refused before anything is sent to it.
        refused = client.post(
            "/api/missions", json={**mission, "mission_id": "for-a"}, headers=AUTH
        )
        assert refused.json()["accepted"] is False
        assert "route b is flying" in refused.json()["status"]["issues"][0]
        assert a.commands == []

        assert client.post("/api/fleet/b/return", headers=AUTH).json()["vehicle_id"] == "b"
        assert client.post("/api/fleet/b/explode", headers=AUTH).status_code == 404
        assert b.commands == ["submit_mission", "return_to_home"]

        assert client.post("/api/fleet/hold-all").status_code == 401
        held = client.post("/api/fleet/hold-all", headers=AUTH).json()
        assert held["results"] == {"a": "sent", "b": "sent"}
        assert a.commands == ["hold"] and b.commands[-1] == "hold"

        snapshot = fleet.hubs[0].snapshot()
        assert [v["vehicle_id"] for v in snapshot["fleet"]] == ["b"]


def test_single_vehicle_setup_reports_no_fleet(tmp_path: Path) -> None:
    client, _ = fleet_client(tmp_path, vehicle("only", 0, 0, 0, in_air=False))
    with client:
        status = client.get("/api/fleet").json()
        assert status["enabled"] is False and status["size"] == 1
        assert "fleet" not in client.app.state.fleet.primary.snapshot()  # type: ignore[attr-defined]
