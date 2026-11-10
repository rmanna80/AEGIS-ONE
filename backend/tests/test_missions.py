import pytest
from pydantic import ValidationError

from aegis.missions import (
    Geofence,
    Mission,
    Waypoint,
    distance_to_polygon_edge_m,
    point_in_polygon,
    segment_inside_polygon,
    validate_mission,
)
from conftest import make_mission

SQUARE = [(-50.0, -50.0), (50.0, -50.0), (50.0, 50.0), (-50.0, 50.0)]
# An "L": the square with its north-east quadrant removed (concave).
L_SHAPE = [(-50.0, -50.0), (50.0, -50.0), (50.0, 0.0), (0.0, 0.0), (0.0, 50.0), (-50.0, 50.0)]


def test_point_in_polygon_inside_outside_and_edge() -> None:
    assert point_in_polygon((0.0, 0.0), SQUARE)
    assert not point_in_polygon((60.0, 0.0), SQUARE)
    assert point_in_polygon((50.0, 10.0), SQUARE)  # on an edge counts as inside
    assert point_in_polygon((50.0, 50.0), SQUARE)  # on a vertex


def test_point_in_concave_polygon() -> None:
    assert point_in_polygon((-25.0, 25.0), L_SHAPE)
    assert point_in_polygon((25.0, -25.0), L_SHAPE)
    assert not point_in_polygon((25.0, 25.0), L_SHAPE)  # the removed quadrant


def test_segment_cutting_a_concave_corner_is_rejected() -> None:
    # Both endpoints are inside the L, but the straight line crosses the notch.
    assert not segment_inside_polygon((-25.0, 40.0), (40.0, -25.0), L_SHAPE)
    assert segment_inside_polygon((-25.0, 40.0), (-25.0, -40.0), L_SHAPE)


def test_distance_to_edge() -> None:
    assert distance_to_polygon_edge_m((0.0, 0.0), SQUARE) == pytest.approx(50.0)
    assert distance_to_polygon_edge_m((45.0, 0.0), SQUARE) == pytest.approx(5.0)
    assert distance_to_polygon_edge_m((60.0, 60.0), SQUARE) == pytest.approx(200.0**0.5)


def test_valid_mission_has_no_issues(mission: Mission) -> None:
    assert validate_mission(mission) == []


def test_waypoint_outside_geofence_is_reported(mission: Mission) -> None:
    mission.waypoints.append(Waypoint(east_m=90.0, north_m=0.0, altitude_rel_m=10.0))
    issues = validate_mission(mission)
    assert any("Waypoint 3 is outside the geofence" in i for i in issues)


def test_altitude_limits_are_reported(mission: Mission) -> None:
    mission.waypoints[0].altitude_rel_m = 2.0  # below the 5 m minimum
    mission.waypoints[1].altitude_rel_m = 45.0  # above the 30 m ceiling
    issues = validate_mission(mission)
    assert any("Waypoint 1" in i and "below the minimum" in i for i in issues)
    assert any("Waypoint 2" in i and "exceeds the geofence ceiling" in i for i in issues)


def test_leg_leaving_concave_fence_is_reported() -> None:
    mission = make_mission(geofence=Geofence(polygon_enu_m=L_SHAPE, max_altitude_rel_m=30.0))
    mission.waypoints = [
        Waypoint(east_m=-25.0, north_m=40.0, altitude_rel_m=10.0),
        Waypoint(east_m=40.0, north_m=-25.0, altitude_rel_m=10.0),
    ]
    issues = validate_mission(mission, start_enu_m=(-25.0, -25.0))
    assert any("leg into waypoint 2 leaves the geofence" in i for i in issues)


def test_overlong_leg_is_reported() -> None:
    mission = make_mission(max_leg_length_m=10.0)
    assert any("longer than" in i for i in validate_mission(mission))


def test_vehicle_starting_outside_fence_is_reported(mission: Mission) -> None:
    issues = validate_mission(mission, start_enu_m=(200.0, 0.0))
    assert any("current position is outside" in i for i in issues)


def test_home_outside_fence_is_reported() -> None:
    fence = Geofence(
        polygon_enu_m=[(10.0, 10.0), (50.0, 10.0), (50.0, 50.0), (10.0, 50.0)],
        max_altitude_rel_m=30.0,
    )
    issues = validate_mission(make_mission(geofence=fence), start_enu_m=(20.0, 20.0))
    assert any("Home position" in i for i in issues)


def test_schema_rejects_bad_input() -> None:
    with pytest.raises(ValidationError):
        Waypoint(east_m=0.0, north_m=0.0, altitude_rel_m=-1.0)
    with pytest.raises(ValidationError):
        Geofence(polygon_enu_m=[(0.0, 0.0), (1.0, 1.0)], max_altitude_rel_m=10.0)
    with pytest.raises(ValidationError):
        Geofence(polygon_enu_m=SQUARE, max_altitude_rel_m=500.0)  # above 120 m cap
    with pytest.raises(ValidationError):
        Mission.model_validate({"mission_id": "bad id!", "name": "x", "waypoints": []})
