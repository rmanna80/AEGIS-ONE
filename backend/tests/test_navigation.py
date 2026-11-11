"""Geometry, depth sensing, occupancy mapping, planning and the navigation service."""

from __future__ import annotations

import asyncio
import json
import math
import struct
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("scipy")

import numpy as np  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from aegis.api import create_app  # noqa: E402
from aegis.config import Settings  # noqa: E402
from aegis.contracts import Attitude, Provenance, Severity, Vec3  # noqa: E402
from aegis.hub import TelemetryHub  # noqa: E402
from aegis.missions import Mission, MissionPhase, MissionStatus, validate_mission  # noqa: E402
from aegis.navigation.depth import (  # noqa: E402
    DepthFrame,
    Pose,
    RaycastDepthCamera,
    body_to_enu_matrix,
    pixel_rays_body,
)
from aegis.navigation.depth_stream import DepthStreamAdapter  # noqa: E402
from aegis.navigation.geometry import SceneGeometry  # noqa: E402
from aegis.navigation.occupancy import OccupancyGrid  # noqa: E402
from aegis.navigation.planner import (  # noqa: E402
    PlanningError,
    RouteChecker,
    astar,
    inflate,
    line_is_free,
    simplify,
)
from aegis.navigation.service import NavigationService, grid_spec_for  # noqa: E402
from conftest import make_state  # noqa: E402
from test_api import AUTH, TOKEN, FakeVehicle  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]
NORTH = (1.0, 0.0, 0.0, 0.0)  # level, nose pointing north


def yaw_quaternion(deg: float) -> tuple[float, float, float, float]:
    half = math.radians(deg) / 2.0
    return (math.cos(half), 0.0, 0.0, math.sin(half))


def scenario(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(
        (REPO_ROOT / "simulation" / "scenarios" / f"{name}.json").read_text()
    )
    return data


# One 10 x 2 x 6 m wall across the path 20 m north of home, plus a pole to the east.
WALL_SCENE: dict[str, Any] = {
    "scenario_id": "test_wall",
    "obstacles": [
        {
            "id": "wall",
            "shape": "box",
            "east_m": 0.0,
            "north_m": 20.0,
            "size_enu_m": [10.0, 2.0, 6.0],
            "yaw_deg": 0.0,
        },
        {
            "id": "pole",
            "shape": "cylinder",
            "east_m": 15.0,
            "north_m": 5.0,
            "radius_m": 1.0,
            "height_m": 8.0,
        },
    ],
    "demo_mission": {
        "mission_id": "through-wall",
        "name": "Straight through the wall",
        "waypoints": [{"east_m": 0.0, "north_m": 40.0, "altitude_rel_m": 4.0}],
        "constraints": {
            "geofence": {
                "polygon_enu_m": [[-30.0, -10.0], [30.0, -10.0], [30.0, 50.0], [-30.0, 50.0]],
                "max_altitude_rel_m": 40.0,
            },
            "min_altitude_rel_m": 2.0,
            "return_to_home_on_complete": False,
        },
    },
}
WALL = SceneGeometry(WALL_SCENE)
WALL_MISSION = Mission.model_validate(WALL_SCENE["demo_mission"])


def truth_grid(scene_dict: dict[str, Any] = WALL_SCENE) -> OccupancyGrid:
    grid = OccupancyGrid(grid_spec_for(scene_dict, 0.5))
    grid.fill_from_scene(SceneGeometry(scene_dict))
    return grid


# ------------------------------------------------------------------------ geometry


def test_scene_occupancy() -> None:
    points = np.array(
        [
            [0.0, 20.0, 3.0],
            [0.0, 20.0, 7.0],
            [6.0, 20.0, 3.0],
            [15.0, 5.0, 4.0],
            [15.0, 6.5, 4.0],
            [0.0, 0.0, 0.0],
        ]
    )
    assert WALL.occupied(points).tolist() == [True, False, False, True, False, False]


def test_raycast_distances() -> None:
    origin = np.array([0.0, 0.0, 3.0])
    directions = np.array(
        [
            [0.0, 1.0, 0.0],  # north: wall face at north 19
            [0.0, -1.0, 0.0],  # south: nothing
            [0.0, 0.0, -1.0],  # straight down: ground 3 m below
            [1.0, 0.0, 0.0],  # east: nothing at north 0
        ]
    )
    assert WALL.raycast(origin, directions, 50.0) == pytest.approx([19.0, np.inf, 3.0, np.inf])
    assert np.isinf(WALL.raycast(origin, directions[:1], 10.0))[0]  # beyond sensor range

    toward_pole = np.array([[1.0, 0.0, 0.0]])
    assert WALL.raycast(np.array([0.0, 5.0, 4.0]), toward_pole, 50.0)[0] == pytest.approx(14.0)
    over_pole = WALL.raycast(np.array([0.0, 5.0, 9.0]), toward_pole, 50.0)
    assert np.isinf(over_pole)[0]


def test_raycast_respects_box_yaw() -> None:
    turned = SceneGeometry(
        {
            "obstacles": [
                {
                    "id": "b",
                    "shape": "box",
                    "east_m": 0.0,
                    "north_m": 20.0,
                    "size_enu_m": [10.0, 2.0, 6.0],
                    "yaw_deg": 90.0,
                }
            ]
        }
    )
    # Rotated 90 deg the box is 2 m wide (east) and 10 m deep (north): its face is at north 15.
    hit = turned.raycast(np.array([0.0, 0.0, 3.0]), np.array([[0.0, 1.0, 0.0]]), 50.0)
    assert hit[0] == pytest.approx(15.0)
    miss = turned.raycast(np.array([3.0, 0.0, 3.0]), np.array([[0.0, 1.0, 0.0]]), 50.0)
    assert np.isinf(miss)[0]


# --------------------------------------------------------------------------- depth


def test_body_to_enu_axes() -> None:
    level_north = body_to_enu_matrix(NORTH)
    assert level_north @ np.array([1.0, 0.0, 0.0]) == pytest.approx(
        [0.0, 1.0, 0.0]
    )  # forward = north
    assert level_north @ np.array([0.0, 1.0, 0.0]) == pytest.approx([1.0, 0.0, 0.0])  # right = east
    assert level_north @ np.array([0.0, 0.0, 1.0]) == pytest.approx([0.0, 0.0, -1.0])  # down
    facing_east = body_to_enu_matrix(yaw_quaternion(90.0))
    assert facing_east @ np.array([1.0, 0.0, 0.0]) == pytest.approx([1.0, 0.0, 0.0], abs=1e-12)


def test_pixel_rays_cover_the_field_of_view() -> None:
    rays = pixel_rays_body(100, 50, math.radians(90.0))
    assert rays.shape == (50, 100, 3) and np.all(rays[..., 0] == 1.0)
    assert rays[25, 99, 1] == pytest.approx(0.99)  # right edge: tan(45 deg) minus half a pixel
    assert rays[25, 0, 1] == pytest.approx(-0.99)
    assert rays[0, 50, 2] < 0 < rays[49, 50, 2]  # top row looks up, bottom row looks down


def camera(scene: SceneGeometry = WALL, **kwargs: Any) -> RaycastDepthCamera:
    async def no_state() -> Any:
        raise AssertionError("not used")

    return RaycastDepthCamera(scene, no_state, range_noise_fraction=0.0, **kwargs)


def test_simulated_depth_camera_reports_planar_depth() -> None:
    cam = camera(width=64, height=48, max_range_m=30.0)
    depth = cam.render(Pose((0.0, 0.0, 3.0), NORTH))
    assert depth.shape == (48, 64) and depth.dtype == np.float32
    # The whole wall face is 19 m ahead: planar depth is 19 m at the centre AND off-axis.
    assert depth[23, 32] == pytest.approx(19.0, abs=0.01)
    assert depth[23, 26] == pytest.approx(19.0, abs=0.01)
    assert np.isinf(depth[23, 2])  # far left: past the wall's edge, nothing in range
    assert depth[47, 32] < 6.0  # bottom row: the ground just ahead

    short = camera(width=64, height=48, max_range_m=10.0).render(Pose((0.0, 0.0, 3.0), NORTH))
    assert np.isinf(short[23, 32])  # wall is beyond a 10 m sensor


# ----------------------------------------------------------------------- occupancy


def survey(grid: OccupancyGrid, cam: RaycastDepthCamera, poses: list[Pose]) -> None:
    for i, pose in enumerate(poses):
        frame = DepthFrame(
            cam.render(pose),
            cam.hfov,
            cam.max_range_m,
            i,
            "t",
            Provenance.SIMULATED,
            "t",
            0.0,
            pose,
        )
        grid.integrate(frame, pose)


def orbit_poses(
    centre: tuple[float, float], radius: float, altitudes: tuple[float, ...]
) -> list[Pose]:
    """Poses on a circle around a point, each looking at it."""
    poses = []
    for altitude in altitudes:
        for deg in range(0, 360, 15):
            east = centre[0] + radius * math.sin(math.radians(deg))
            north = centre[1] + radius * math.cos(math.radians(deg))
            heading = (deg + 180.0) % 360.0  # face the centre
            poses.append(Pose((east, north, altitude), yaw_quaternion(heading)))
    return poses


def test_map_of_an_orbited_obstacle_matches_ground_truth() -> None:
    """Simulation test: reconstructed map vs. simulator ground truth (Demo 4 in miniature)."""
    truth = truth_grid()
    grid = OccupancyGrid(truth.spec)
    survey(grid, camera(), orbit_poses((0.0, 20.0), 14.0, (2.0, 4.5, 8.0)))
    score = grid.compare_with(truth)
    assert score["precision"] >= 0.97, score
    assert score["recall_of_observed_surface"] >= 0.9, score
    assert score["surface_coverage"] >= 0.5, score  # the pole was never orbited
    assert grid.frames_integrated == 72


def test_free_space_observations_clear_a_stale_obstacle() -> None:
    truth = truth_grid()
    grid = OccupancyGrid(truth.spec)
    ghost, _ = grid.to_index(np.array([[0.0, 8.0, 3.0]]))
    grid.log_odds[tuple(ghost[0])] = grid.L_MAX  # something that is no longer there
    assert grid.nearest_obstacle_m((0.0, 0.0, 3.0)) == pytest.approx(8.0, abs=0.5)
    survey(grid, camera(), [Pose((0.0, 0.0, 3.0), NORTH)] * 12)
    assert not grid.occupied_mask[tuple(ghost[0])]


def test_ground_returns_are_not_obstacles() -> None:
    grid = OccupancyGrid(truth_grid().spec)
    looking_down = (
        math.cos(math.radians(-30)),
        0.0,
        math.sin(math.radians(-30)),
        0.0,
    )  # pitch -60 deg
    survey(grid, camera(max_range_m=19.0), [Pose((0.0, -5.0, 6.0), looking_down)] * 5)
    assert not grid.occupied_mask.any()
    assert grid.observed_fraction() > 0.0


def test_map_save_and_load_round_trip(tmp_path: Path) -> None:
    grid = OccupancyGrid(truth_grid().spec)
    survey(grid, camera(), orbit_poses((0.0, 20.0), 14.0, (4.0,)))
    path = tmp_path / "maps" / "map.npz"
    grid.save(path, {"scenario": "test_wall"})
    loaded = OccupancyGrid.load(path)
    assert loaded.spec == grid.spec and loaded.frames_integrated == grid.frames_integrated
    assert np.array_equal(loaded.log_odds, grid.log_odds)


def test_slice_and_nearest_obstacle() -> None:
    truth = truth_grid()
    assert truth.slice_2d(3.0, 1.0).any()
    assert not truth.slice_2d(20.0, 1.0).any()  # nothing is that tall
    assert truth.nearest_obstacle_m((0.0, 15.0, 3.0)) == pytest.approx(4.0, abs=0.5)
    assert truth.nearest_obstacle_m((-25.0, -5.0, 30.0), search_m=10.0) is None


# ------------------------------------------------------------------------- planner


def test_astar_goes_around_a_wall_without_cutting_corners() -> None:
    blocked = np.zeros((20, 20), dtype=bool)
    blocked[10, 3:20] = True  # wall with a gap at the low end
    path = astar(blocked, (2, 15), (18, 15))
    assert path is not None and path[0] == (2, 15) and path[-1] == (18, 15)
    assert not any(blocked[c] for c in path)
    assert any(c[1] <= 2 for c in path)  # went through the gap
    for (x0, y0), (x1, y1) in zip(path, path[1:], strict=False):
        assert max(abs(x1 - x0), abs(y1 - y0)) == 1
        if x0 != x1 and y0 != y1:  # a diagonal step never squeezes past a blocked corner
            assert not blocked[x1, y0] and not blocked[x0, y1]

    corners = simplify(path, blocked)
    assert len(corners) < len(path) and corners[0] == path[0] and corners[-1] == path[-1]
    assert all(line_is_free(blocked, a, b) for a, b in zip(corners, corners[1:], strict=False))


def test_astar_reports_when_there_is_no_route() -> None:
    blocked = np.zeros((20, 20), dtype=bool)
    blocked[10, :] = True
    assert astar(blocked, (2, 15), (18, 15)) is None
    assert astar(blocked, (10, 5), (18, 15)) is None  # start inside an obstacle


def test_inflate_grows_obstacles_by_the_clearance() -> None:
    mask = np.zeros((21, 21), dtype=bool)
    mask[10, 10] = True
    grown = inflate(mask, 3)
    assert grown[10, 13] and grown[13, 10] and not grown[10, 14] and not grown[13, 13]


def test_route_through_an_obstacle_is_reported() -> None:
    checker = RouteChecker([truth_grid()], clearance_m=3.0)
    issues = checker.check_mission(WALL_MISSION, (0.0, 0.0, 0.0))
    assert len(issues) == 1 and "leg into waypoint 1" in issues[0] and "north 1" in issues[0]

    over = WALL_MISSION.model_copy(deep=True)
    over.waypoints[0].altitude_rel_m = 15.0  # well above the 6 m wall
    assert checker.check_mission(over, (0.0, 0.0, 0.0)) == []


def test_return_leg_is_checked_at_return_altitude() -> None:
    tall = json.loads(json.dumps(WALL_SCENE))
    tall["obstacles"][0]["size_enu_m"] = [10.0, 2.0, 38.0]  # taller than PX4's 30 m return altitude
    mission = Mission.model_validate(tall["demo_mission"])
    mission.waypoints[0].east_m = 12.0  # RTL flies a straight line home from here
    mission.constraints.return_to_home_on_complete = True
    mission.waypoints[0].north_m = 40.0
    checker = RouteChecker([truth_grid(tall)], clearance_m=3.0)
    issues = checker.check_mission(mission, (0.0, 0.0, 0.0))
    assert any("return leg at 30 m" in i for i in issues)


def test_planner_inserts_a_detour_that_is_clear_and_inside_the_geofence() -> None:
    checker = RouteChecker([truth_grid()], clearance_m=3.0)
    planned = checker.plan_mission(WALL_MISSION, (0.0, 0.0, 0.0))
    assert len(planned.waypoints) > len(WALL_MISSION.waypoints)
    assert planned.waypoints[-1] == WALL_MISSION.waypoints[-1]  # the goal is unchanged
    assert checker.check_mission(planned, (0.0, 0.0, 0.0)) == []
    assert validate_mission(planned) == []
    # The detour really goes around the 10 m wide wall (half-width 5 m + 3 m clearance).
    assert max(abs(wp.east_m) for wp in planned.waypoints) >= 8.0
    # A mission that is already clear is returned unchanged.
    assert checker.plan_mission(planned, (0.0, 0.0, 0.0)).waypoints == planned.waypoints


def test_planner_refuses_a_goal_inside_an_obstacle() -> None:
    checker = RouteChecker([truth_grid()], clearance_m=3.0)
    inside = WALL_MISSION.model_copy(deep=True)
    inside.waypoints[0].north_m = 20.0
    with pytest.raises(PlanningError, match="within 3 m of an obstacle"):
        checker.plan_mission(inside, (0.0, 0.0, 0.0))


@pytest.mark.parametrize("name", ["open_field", "urban"])
def test_shipped_demo_missions_are_clear_of_known_obstacles(name: str) -> None:
    data = scenario(name)
    checker = RouteChecker([truth_grid(data)], clearance_m=3.0)
    mission = Mission.model_validate(data["demo_mission"])
    assert checker.check_mission(mission, (0.0, 0.0, 0.0)) == []


# ------------------------------------------------------------------------- service


def airborne(east: float, north: float, altitude: float, **extra: Any) -> Any:
    return make_state(
        position_enu_m=Vec3(x=east, y=north, z=altitude),
        relative_altitude_m=altitude,
        attitude=Attitude(quaternion_wxyz=NORTH, roll_deg=0.0, pitch_deg=0.0, yaw_deg=0.0),
        **extra,
    )


def make_service(**kwargs: Any) -> tuple[NavigationService, FakeVehicle, TelemetryHub]:
    vehicle = FakeVehicle()
    hub = TelemetryHub(vehicle, vehicle)
    service = NavigationService(hub, WALL_SCENE, depth=None, **kwargs)
    return service, vehicle, hub


def fly(hub: TelemetryHub, vehicle: FakeVehicle, state: Any) -> None:
    hub.state = state
    hub.active_mission = WALL_MISSION
    vehicle.status = MissionStatus(
        mission_id="through-wall", phase=MissionPhase.ACTIVE, total_waypoints=1
    )
    hub.mission = vehicle.status


async def test_blocked_route_triggers_one_automatic_hold() -> None:
    """Simulation test: an obstacle sensed on the route stops the vehicle; it is never steered."""
    service, vehicle, hub = make_service(use_prior_map=False)  # the wall is unknown in advance
    fly(hub, vehicle, airborne(0.0, 2.0, 4.0))
    await service.monitor_once()
    assert service.findings() == [] and vehicle.commands == []  # nothing mapped yet

    cam = camera()
    survey(service.live, cam, [Pose((0.0, 2.0, 4.0), NORTH)] * 6)  # the depth sensor sees the wall
    await service.monitor_once()
    codes = {code: severity for code, severity, _ in service.findings()}
    assert codes.get("route.blocked") is Severity.CRITICAL
    assert len(vehicle.commands) == 1 and vehicle.commands[0].startswith("hold:Route blocked")
    assert vehicle.status.phase is MissionPhase.BLOCKED

    hub.mission = MissionStatus(
        mission_id="through-wall", phase=MissionPhase.ACTIVE, total_waypoints=1
    )
    await service.monitor_once()
    assert len(vehicle.commands) == 1  # not commanded again for the same mission

    detour = service.checker.plan_mission(WALL_MISSION, service.vehicle_point())
    assert service.checker.check_mission(detour, service.vehicle_point()) == []


async def test_far_obstacles_and_idle_vehicles_do_not_trigger_a_hold() -> None:
    service, vehicle, hub = make_service(use_prior_map=True)
    hub.state = airborne(0.0, 2.0, 4.0)  # no mission active
    await service.monitor_once()
    assert vehicle.commands == [] and service.findings() == []

    service, vehicle, hub = make_service(use_prior_map=True, auto_hold=False)
    fly(hub, vehicle, airborne(0.0, 2.0, 4.0))
    await service.monitor_once()
    assert {c for c, _, _ in service.findings()} == {"route.blocked"}
    assert vehicle.commands == []  # automatic hold switched off: alert only


async def test_obstacle_proximity_alerts() -> None:
    service, vehicle, hub = make_service()
    survey(service.live, camera(), orbit_poses((0.0, 20.0), 14.0, (4.0,)))
    hub.state = airborne(0.0, 16.5, 4.0)  # 2.5 m from the wall face
    await service.monitor_once()
    assert [(c, s) for c, s, _ in service.findings()] == [("obstacle.near", Severity.WARNING)]
    hub.state = airborne(0.0, 18.0, 4.0)
    await service.monitor_once()
    assert [(c, s) for c, s, _ in service.findings()] == [("obstacle.close", Severity.CRITICAL)]
    assert service.status()["nearest_obstacle_m"] < 1.5


async def test_lost_depth_sensing_during_a_mission_holds() -> None:
    """Fault injection: depth frames stop arriving mid-mission."""

    class DeadDepth:
        sensor_id = "dead"

        async def start(self) -> None: ...
        async def stop(self) -> None: ...

        async def read_frame(self) -> None:
            await asyncio.sleep(0.05)

    vehicle = FakeVehicle()
    hub = TelemetryHub(vehicle, vehicle)
    over = WALL_MISSION.model_copy(deep=True)
    over.waypoints[0].altitude_rel_m = 15.0
    service = NavigationService(hub, WALL_SCENE, depth=DeadDepth())
    fly(hub, vehicle, airborne(0.0, 2.0, 15.0))
    hub.active_mission = over
    assert "no depth frames have arrived" in service.preflight_issues()[0]
    await service.monitor_once()
    assert {c for c, _, _ in service.findings()} == {"depth.stale"}
    assert vehicle.commands and vehicle.commands[0].startswith("hold:Depth sensing stopped")


# ----------------------------------------------------------------------------- API


def test_api_rejects_blocked_missions_and_plans_detours(tmp_path: Path) -> None:
    scenario_file = tmp_path / "test_wall.json"
    scenario_file.write_text(json.dumps(WALL_SCENE))
    vehicle = FakeVehicle()
    vehicle.overrides = {"position_enu_m": Vec3(x=0.0, y=0.0, z=0.0), "in_air": False}
    hub = TelemetryHub(vehicle, vehicle)
    settings = Settings(
        scenario_file=scenario_file,
        operator_token=TOKEN,
        data_dir=tmp_path,
        frontend_dist=tmp_path / "no-ui",
    )
    body = WALL_MISSION.model_dump(mode="json")
    with TestClient(create_app(settings, hub)) as client:
        status = client.get("/api/navigation").json()
        assert status["enabled"] is True and status["uses_prior_map"] is True

        check = client.post("/api/missions/validate", json=body).json()
        assert check["valid"] is False and check["obstacles_checked"] is True
        assert "obstacle" in check["issues"][0]

        refused = client.post("/api/missions", json=body, headers=AUTH).json()
        assert refused["accepted"] is False and refused["status"]["phase"] == "rejected"
        assert vehicle.commands == []  # never reached the vehicle

        plan = client.post("/api/missions/plan", json={"mission": body}).json()
        assert plan["detour_waypoints_added"] >= 1
        assert plan["mission"]["mission_id"] == "through-wall-replan"
        assert client.post("/api/missions/validate", json=plan["mission"]).json()["valid"] is True
        accepted = client.post("/api/missions", json=plan["mission"], headers=AUTH).json()
        assert accepted["accepted"] is True and vehicle.commands == ["submit_mission"]
        assert hub.active_mission is not None

        impossible = json.loads(json.dumps(body))
        impossible["waypoints"][0]["north_m"] = 20.0
        response = client.post("/api/missions/plan", json={"mission": impossible})
        assert response.status_code == 422 and "obstacle" in response.json()["detail"]

        empty = client.get("/api/map").json()
        assert empty["changed"] is True and empty["voxels"] == []
        assert client.get(f"/api/map?since={empty['revision']}").json() == {
            "revision": empty["revision"],
            "changed": False,
        }
        accuracy = client.get("/api/map/accuracy").json()
        assert accuracy["scenario"] == "test_wall" and accuracy["mapped_occupied_voxels"] == 0
        saved = client.post("/api/map/save").json()
        assert Path(saved["saved"]).is_file()


# -------------------------------------------------------------------- depth stream


async def test_depth_stream_decodes_metres_and_millimetres() -> None:
    adapter = DepthStreamAdapter("127.0.0.1", 0, horizontal_fov_deg=73.0, max_range_m=19.0)
    metres = np.array([[1.5, 25.0], [np.inf, 4.0]], dtype="<f4")
    header = {
        "width": 2,
        "height": 2,
        "encoding": "depth32f",
        "provenance": "simulated",
        "source": "gazebo",
    }
    adapter._accept(header, metres.tobytes())
    frame = await adapter.read_frame()
    assert frame is not None and frame.provenance is Provenance.SIMULATED and frame.pose is None
    assert frame.depth_m[0, 0] == pytest.approx(1.5) and frame.depth_m[1, 1] == pytest.approx(4.0)
    assert np.isinf(frame.depth_m[0, 1]) and np.isinf(
        frame.depth_m[1, 0]
    )  # beyond range / no return
    assert frame.horizontal_fov_rad == pytest.approx(math.radians(73.0))

    millimetres = np.array([[1500, 0]], dtype="<u2")
    adapter._accept(
        {**header, "width": 2, "height": 1, "encoding": "depth16u"}, millimetres.tobytes()
    )
    frame = await adapter.read_frame()
    assert (
        frame is not None
        and frame.depth_m[0, 0] == pytest.approx(1.5)
        and np.isinf(frame.depth_m[0, 1])
    )

    with pytest.raises(ValueError, match="unsupported depth encoding"):
        adapter._accept({**header, "encoding": "rgb8"}, b"\x00" * 12)
    with pytest.raises(ValueError, match="expected 16"):
        adapter._accept(header, struct.pack("<f", 1.0))
    with pytest.raises(ValueError, match="not accepted"):
        adapter._accept({**header, "provenance": "physical"}, metres.tobytes())


def test_saved_map_can_be_loaded_in_a_later_session(tmp_path: Path) -> None:
    scenario_file = tmp_path / "test_wall.json"
    scenario_file.write_text(json.dumps(WALL_SCENE))
    grid = OccupancyGrid(truth_grid().spec)
    survey(grid, camera(), orbit_poses((0.0, 20.0), 14.0, (4.0,)))
    grid.save(tmp_path / "map.npz")

    vehicle = FakeVehicle()
    base = {"scenario_file": scenario_file, "data_dir": tmp_path, "frontend_dist": tmp_path / "x"}
    settings = Settings(nav_load_map=tmp_path / "map.npz", **base)
    with TestClient(create_app(settings, TelemetryHub(vehicle, vehicle))) as client:
        loaded = client.get("/api/map").json()
        assert loaded["occupied_voxels"] == int(np.count_nonzero(grid.occupied_mask)) > 0
        assert len(loaded["voxels"]) == 3 * loaded["sent_voxels"]

    other = Settings(nav_load_map=tmp_path / "map.npz", map_resolution_m=1.0, **base)
    with TestClient(create_app(other, TelemetryHub(vehicle, vehicle))) as client:
        status = client.get("/api/navigation").json()  # wrong resolution: refused, not crashed
        assert status["enabled"] is False and "different scenario or resolution" in status["reason"]
        assert client.get("/api/health").json()["status"] == "ok"


def test_mission_is_refused_before_takeoff_when_the_depth_sensor_is_silent(tmp_path: Path) -> None:
    """Regression: a mission was accepted, armed and then immediately held because the
    configured depth source had never delivered a frame."""
    scenario_file = tmp_path / "test_wall.json"
    scenario_file.write_text(json.dumps(WALL_SCENE))
    vehicle = FakeVehicle()
    vehicle.overrides = {"position_enu_m": Vec3(x=0.0, y=0.0, z=0.0), "in_air": False}
    settings = Settings(
        scenario_file=scenario_file,
        depth="stream",
        depth_stream_port=0,
        operator_token=TOKEN,
        data_dir=tmp_path,
        frontend_dist=tmp_path / "no-ui",
    )
    clear = WALL_MISSION.model_copy(deep=True)
    clear.waypoints[0].altitude_rel_m = 15.0  # over the wall: the route itself is fine
    body = clear.model_dump(mode="json")
    with TestClient(create_app(settings, TelemetryHub(vehicle, vehicle))) as client:
        check = client.post("/api/missions/validate", json=body).json()
        assert check["valid"] is False and "no depth frames have arrived" in check["issues"][0]
        refused = client.post("/api/missions", json=body, headers=AUTH).json()
        assert refused["accepted"] is False and refused["status"]["phase"] == "rejected"
        assert vehicle.commands == []
