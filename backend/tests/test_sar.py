"""Search and rescue: thermal model, heat-source detection, candidates, coverage, API."""

from __future__ import annotations

import json
import math
import time
from pathlib import Path
from typing import Any

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("scipy")

from fastapi.testclient import TestClient  # noqa: E402

from aegis.api import create_app  # noqa: E402
from aegis.config import Settings  # noqa: E402
from aegis.contracts import Provenance  # noqa: E402
from aegis.hub import TelemetryHub  # noqa: E402
from aegis.missions import MissionConstraints, point_in_polygon, validate_mission  # noqa: E402
from aegis.navigation.depth import Pose  # noqa: E402
from aegis.sar.candidates import CandidateRegistry  # noqa: E402
from aegis.sar.coverage import (  # noqa: E402
    CoveragePlanError,
    CoverageTracker,
    plan_lawnmower,
    swath_width_m,
)
from aegis.sar.detect import categorise, detect_heat_sources  # noqa: E402
from aegis.sar.review import ReviewRequest, ReviewStatus  # noqa: E402
from aegis.sar.service import SarService, report_markdown  # noqa: E402
from aegis.sar.thermal import ThermalCameraModel, ThermalFrame  # noqa: E402
from test_api import AUTH, TOKEN, FakeVehicle  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]
SCENARIO: dict[str, Any] = json.loads(
    (REPO_ROOT / "simulation" / "scenarios" / "sar_field.json").read_text()
)
HFOV = math.radians(57.0)


def heading_pose(east: float, north: float, up: float, heading_deg: float = 0.0) -> Pose:
    half = math.radians(heading_deg) / 2
    return Pose((east, north, up), (math.cos(half), 0.0, 0.0, math.sin(half)))


def frame_at(camera: ThermalCameraModel, pose: Pose, frame_id: int = 1) -> ThermalFrame:
    return ThermalFrame(
        temperature_c=camera.render(pose),
        horizontal_fov_rad=camera.hfov,
        frame_id=frame_id,
        sensor_id=camera.sensor_id,
        provenance=Provenance.SIMULATED,
        source=camera.source,
        captured_at_unix_s=time.time(),
        pose=pose,
    )


def source(source_id: str) -> dict[str, Any]:
    return next(s for s in SCENARIO["heat_sources"] if s["id"] == source_id)


# --------------------------------------------------------------------------- sensor


def test_thermal_image_shows_a_person_at_the_right_place_and_temperature() -> None:
    camera = ThermalCameraModel(SCENARIO, None, netd_c=0.0, blur_px=0.0)
    person = source("person_lying")
    image = camera.render(heading_pose(person["east_m"], person["north_m"], 25.0))
    assert image.shape == (120, 160)
    centre = image[55:65, 75:85]
    assert centre.max() == pytest.approx(30.0)  # directly below: the image centre
    assert abs(float(np.median(image)) - 12.0) < 1.0  # ambient ground elsewhere
    assert image[:20, :20].max() < 14.0


def test_image_axes_follow_the_vehicle_heading() -> None:
    camera = ThermalCameraModel(SCENARIO, None, netd_c=0.0, blur_px=0.0)
    person = source("person_sitting")
    # Vehicle 8 m SOUTH of the person, heading north: the person is ahead = top of the image.
    image = camera.render(heading_pose(person["east_m"], person["north_m"] - 8.0, 25.0, 0.0))
    row, col = np.unravel_index(np.argmax(image), image.shape)
    assert row < 40 and 70 < col < 90
    # Same place, heading east: the person is to the vehicle's left = left of the image.
    image = camera.render(heading_pose(person["east_m"], person["north_m"] - 8.0, 25.0, 90.0))
    row, col = np.unravel_index(np.argmax(image), image.shape)
    assert col < 50 and 50 < row < 70


def test_stabilised_mount_looks_straight_down_while_the_vehicle_leans() -> None:
    half = math.radians(-26.0) / 2  # nose down 26 deg, heading north
    leaning = Pose((62.0, -12.0, 25.0), (math.cos(half), 0.0, math.sin(half), 0.0))
    stabilised = ThermalCameraModel(SCENARIO, None).camera_pose(leaning)
    assert stabilised.quaternion_wxyz == pytest.approx((1.0, 0.0, 0.0, 0.0))
    assert stabilised.position_enu_m == leaning.position_enu_m
    fixed = ThermalCameraModel(SCENARIO, None, stabilised=False)
    assert fixed.camera_pose(leaning) is leaning and fixed.source == "model:thermal-fixed"


def test_a_roof_hides_what_is_under_it() -> None:
    camera = ThermalCameraModel(SCENARIO, None, netd_c=0.0, blur_px=0.0)
    hidden = source("person_in_shelter")
    frame = frame_at(camera, heading_pose(hidden["east_m"], hidden["north_m"], 25.0))
    assert frame.temperature_c.max() < 16.0
    assert detect_heat_sources(frame) == []


# ------------------------------------------------------------------------ detection


def test_detection_geolocates_a_person_within_a_metre_from_an_offset_tilted_view() -> None:
    camera = ThermalCameraModel(SCENARIO, None, seed=3)
    person = source("person_lying")
    # 6 m east and 4 m south of the person, heading 40 deg, a few degrees of roll and pitch.
    yaw, pitch, roll = (math.radians(a) for a in (40.0, 4.0, -3.0))
    cy, sy, cp, sp, cr, sr = (
        math.cos(yaw / 2),
        math.sin(yaw / 2),
        math.cos(pitch / 2),
        math.sin(pitch / 2),
        math.cos(roll / 2),
        math.sin(roll / 2),
    )
    q = (
        cr * cp * cy + sr * sp * sy,
        sr * cp * cy - cr * sp * sy,
        cr * sp * cy + sr * cp * sy,
        cr * cp * sy - sr * sp * cy,
    )
    pose = Pose((person["east_m"] + 6.0, person["north_m"] - 4.0, 25.0), q)
    blobs = detect_heat_sources(frame_at(camera, pose))
    assert len(blobs) == 1
    blob = blobs[0]
    assert math.hypot(blob.east_m - person["east_m"], blob.north_m - person["north_m"]) < 1.0
    assert blob.category == "person-sized warm object"
    assert 0.4 < blob.area_m2 < 1.6  # true footprint 0.85 m2, blurred and quantised
    assert 26.0 < blob.peak_c <= 31.0


def test_no_detection_when_low_or_tilted_hard_or_nothing_warm() -> None:
    camera = ThermalCameraModel(SCENARIO, None, seed=1)
    person = source("person_lying")
    assert detect_heat_sources(frame_at(camera, heading_pose(62.0, -12.0, 3.0))) == []
    half = math.radians(40.0) / 2  # 40 deg of roll
    tilted = Pose(
        (person["east_m"], person["north_m"], 25.0), (math.cos(half), math.sin(half), 0, 0)
    )
    assert detect_heat_sources(frame_at(camera, tilted)) == []
    assert detect_heat_sources(frame_at(camera, heading_pose(0.0, -55.0, 25.0))) == []


def test_categories_describe_size_and_temperature_only() -> None:
    assert categorise(0.8, 30.0) == "person-sized warm object"
    assert categorise(0.6, 29.0) == "person-sized warm object"  # a deer reads the same
    assert categorise(5.0, 20.0) == "large warm area"
    assert categorise(0.5, 180.0) == "hot object"
    assert categorise(0.02, 30.0) == "small warm object"


# ----------------------------------------------------------------------- candidates


def blob_at(east: float, north: float, **kwargs: Any) -> Any:
    from aegis.sar.detect import HeatBlob

    values = {
        "peak_c": 30.0,
        "mean_c": 28.0,
        "ambient_c": 12.0,
        "pixels": 20,
        "area_m2": 0.8,
        "bbox_px": (10, 10, 14, 15),
        "category": "person-sized warm object",
        "touches_edge": False,
    }
    values.update(kwargs)
    return HeatBlob(east_m=east, north_m=north, **values)


def test_observations_merge_and_only_repeated_sightings_reach_the_operator() -> None:
    registry = CandidateRegistry(merge_radius_m=3.0, min_observations=3)
    registry.observe([blob_at(50.0, 10.0)], Provenance.SIMULATED, "t")
    registry.observe([blob_at(50.6, 10.4)], Provenance.SIMULATED, "t")
    assert registry.established() == []  # two sightings: not shown yet
    registry.observe([blob_at(49.7, 9.8), blob_at(90.0, -5.0)], Provenance.SIMULATED, "t")
    [candidate] = registry.established()
    assert candidate.observations == 3
    assert candidate.status is ReviewStatus.UNREVIEWED
    assert math.hypot(candidate.east_m - 50.1, candidate.north_m - 10.07) < 0.1
    assert registry.awaiting_review() == 1
    assert [e["event"] for e in registry.events] == ["candidate"]


def test_clipped_blobs_do_not_create_or_move_candidates() -> None:
    registry = CandidateRegistry(min_observations=1)
    registry.observe([blob_at(10.0, 10.0, touches_edge=True)], Provenance.SIMULATED, "t")
    assert registry.established() == []
    registry.observe([blob_at(10.0, 10.0)], Provenance.SIMULATED, "t")
    registry.observe([blob_at(12.0, 10.0, touches_edge=True)], Provenance.SIMULATED, "t")
    [candidate] = registry.established()
    assert (candidate.east_m, candidate.observations) == (10.0, 1)


def test_status_changes_only_through_an_operator_review() -> None:
    registry = CandidateRegistry(min_observations=1)
    for _ in range(50):
        registry.observe([blob_at(10.0, 10.0)], Provenance.SIMULATED, "t")
    [candidate] = registry.established()
    assert candidate.status is ReviewStatus.UNREVIEWED and candidate.reviewed_at_unix_s is None
    reviewed = registry.review(
        candidate.candidate_id, ReviewRequest(decision=ReviewStatus.CONFIRMED, note="waving")
    )
    assert reviewed is not None and reviewed.status is ReviewStatus.CONFIRMED
    assert reviewed.review_note == "waving" and reviewed.reviewed_at_unix_s is not None
    registry.observe([blob_at(10.0, 10.0)], Provenance.SIMULATED, "t")
    assert registry.established()[0].status is ReviewStatus.CONFIRMED  # sightings never reset it
    assert registry.review("C999", ReviewRequest(decision=ReviewStatus.DISMISSED)) is None


# ------------------------------------------------------------------------- coverage


def constraints() -> MissionConstraints:
    return MissionConstraints.model_validate(SCENARIO["demo_mission"]["constraints"])


def test_lawnmower_plan_is_valid_and_spaced_for_the_sensor() -> None:
    area = [(float(e), float(n)) for e, n in SCENARIO["search_area"]["polygon_enu_m"]]
    mission, summary = plan_lawnmower(area, constraints(), 25.0, HFOV, overlap=0.2)
    assert validate_mission(mission) == []
    swath = swath_width_m(25.0, HFOV)
    assert swath == pytest.approx(27.15, abs=0.05)
    assert summary["passes"] == 4 and summary["pass_spacing_m"] == 20.0
    assert summary["overlap_planned"] >= 0.2
    norths = sorted({wp.north_m for wp in mission.waypoints})
    assert norths == [-30.0, -10.0, 10.0, 30.0]  # passes run east-west, the long way
    # Outer swaths reach the area's edges; neighbours overlap.
    assert norths[0] - swath / 2 <= -40.0 and norths[-1] + swath / 2 >= 40.0
    # Boustrophedon: consecutive passes fly in opposite directions, starting near home.
    easts = [wp.east_m for wp in mission.waypoints]
    assert easts == [20.0, 140.0, 140.0, 20.0, 20.0, 140.0, 140.0, 20.0]
    assert all(point_in_polygon((wp.east_m, wp.north_m), area) for wp in mission.waypoints)


def test_lawnmower_handles_tall_and_non_rectangular_areas() -> None:
    tall = [(20.0, -60.0), (60.0, -60.0), (60.0, 60.0), (20.0, 60.0)]
    mission, summary = plan_lawnmower(tall, constraints(), 25.0, HFOV)
    assert summary["passes"] == 2
    assert {wp.north_m for wp in mission.waypoints} == {-60.0, 60.0}  # passes run north-south
    triangle = [(20.0, -40.0), (140.0, -40.0), (80.0, 40.0)]
    mission, _ = plan_lawnmower(triangle, constraints(), 25.0, HFOV)
    lengths = [
        abs(a.east_m - b.east_m)
        for a, b in zip(mission.waypoints[0::2], mission.waypoints[1::2], strict=True)
    ]
    assert lengths == sorted(lengths, reverse=True)  # passes shorten toward the apex


def test_lawnmower_refuses_bad_requests() -> None:
    area = [(20.0, -40.0), (140.0, -40.0), (140.0, 40.0), (20.0, 40.0)]
    with pytest.raises(CoveragePlanError, match="altitude"):
        plan_lawnmower(area, constraints(), 80.0, HFOV)
    with pytest.raises(CoveragePlanError, match="geofence"):
        plan_lawnmower([(100.0, 0.0), (300.0, 0.0), (300.0, 60.0)], constraints(), 25.0, HFOV)
    with pytest.raises(CoveragePlanError, match="100"):
        plan_lawnmower(area, constraints(), 5.0, math.radians(10.0), overlap=0.8)


def test_coverage_counts_only_what_the_camera_footprint_reached() -> None:
    area = [(0.0, 0.0), (100.0, 0.0), (100.0, 40.0), (0.0, 40.0)]
    tracker = CoverageTracker(area)
    assert tracker.fraction() == 0.0
    tracker.mark(heading_pose(50.0, 20.0, 25.0), HFOV, 160, 120)
    # Heading north: image width (27.1 m) lies east-west, image height (20.4 m) north-south.
    expected = (27.15 * 20.36) / (100.0 * 40.0)
    assert tracker.fraction() == pytest.approx(expected, abs=0.01)
    tracker.mark(heading_pose(50.0, 20.0, 25.0), HFOV, 160, 120)
    assert tracker.fraction() == pytest.approx(expected, abs=0.01)  # same view adds nothing
    tracker.mark(heading_pose(500.0, 500.0, 25.0), HFOV, 160, 120)
    assert tracker.fraction() == pytest.approx(expected, abs=0.01)  # outside the area
    payload = tracker.payload()
    assert payload["size_cells"] == [100, 40] and len(payload["seen_runs_by_north_row"]) == 40


# -------------------------------------------------------------- end to end, no PX4


def fly_plan(service: SarService, camera: ThermalCameraModel, speed_m_s: float = 5.0) -> None:
    """Feed the service the frames a vehicle would produce flying the planned pattern at 4 Hz."""
    mission, _ = service.plan((0.0, 0.0))
    path = [(0.0, 0.0)] + [(wp.east_m, wp.north_m) for wp in mission.waypoints]
    frame_id = 0
    for a, b in zip(path[:-1], path[1:], strict=True):
        length = math.dist(a, b)
        heading = math.degrees(math.atan2(b[0] - a[0], b[1] - a[1]))
        for step in range(int(length / (speed_m_s / 4.0)) + 1):
            t = min(1.0, step * (speed_m_s / 4.0) / length)
            pose = heading_pose(a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t, 25.0, heading)
            frame_id += 1
            service.process(frame_at(camera, pose, frame_id))


def test_search_pattern_finds_every_visible_source_and_commands_nothing() -> None:
    vehicle = FakeVehicle()
    hub = TelemetryHub(vehicle, vehicle)
    camera = ThermalCameraModel(SCENARIO, None, seed=7)
    service = SarService(hub, SCENARIO, camera)
    fly_plan(service, camera)

    accuracy = service.accuracy()
    by_id = {s["id"]: s for s in accuracy["sources"]}
    for name in (
        "person_lying",
        "person_sitting",
        "deer",
        "warm_rock",
        "vehicle_engine",
        "campfire",
    ):
        assert by_id[name]["detected"], name
        assert by_id[name]["position_error_m"] < 1.5, (name, by_id[name])
    assert not by_id["person_in_shelter"]["detected"]  # under a roof: honestly missed
    assert accuracy["candidates_without_a_source"] == []
    assert by_id["person_lying"]["category"] == "person-sized warm object"
    assert by_id["person_sitting"]["category"] == "person-sized warm object"
    assert by_id["deer"]["category"] == "person-sized warm object"  # needs a human to tell
    assert by_id["campfire"]["category"] == "hot object"
    assert by_id["warm_rock"]["category"] == "large warm area"
    assert accuracy["coverage_fraction"] > 0.97

    # Advisory only: nothing was confirmed and the vehicle received no command.
    assert all(c.status is ReviewStatus.UNREVIEWED for c in service.candidates())
    assert vehicle.commands == []
    assert not hasattr(service, "executor")
    [(code, severity, message)] = service.findings()
    assert code == "sar.review_needed" and severity.value == "info" and "6 heat sources" in message

    report = service.report()
    assert report["candidates_by_status"] == {"unreviewed": 6, "confirmed": 0, "dismissed": 0}
    assert report["provenance"] == "simulated"
    text = report_markdown(report)
    assert "**simulated**" in text and "only by the operator" in text


def test_api_review_needs_the_operator_token_and_reports_are_saved(tmp_path: Path) -> None:
    vehicle = FakeVehicle()
    hub = TelemetryHub(vehicle, vehicle)
    settings = Settings(
        operator_token=TOKEN,
        scenario="sar_field",
        thermal="model",
        record_flights=False,
        data_dir=tmp_path,
        frontend_dist=tmp_path / "none",
    )
    with TestClient(create_app(settings, hub)) as client:
        status = client.get("/api/sar").json()
        assert status["enabled"] and status["advisory_only"]
        plan = client.post("/api/sar/plan", json={}).json()
        assert plan["issues"] == [] and plan["summary"]["passes"] == 4
        assert len(plan["mission"]["waypoints"]) == 8
        assert client.post("/api/sar/plan", json={"altitude_rel_m": 100}).status_code == 422
        assert vehicle.commands == []  # planning sends nothing

        # Put one candidate in front of the operator.
        service = client.app.state.sar  # type: ignore[attr-defined]
        camera = ThermalCameraModel(SCENARIO, None, seed=2)
        person = source("person_lying")
        for i in range(4):
            pose = heading_pose(person["east_m"] + i, person["north_m"], 25.0, 90.0)
            service.process(frame_at(camera, pose, i))
        listing = client.get("/api/sar/candidates").json()
        assert listing["awaiting_review"] == 1
        candidate_id = listing["candidates"][0]["candidate_id"]
        assert client.get("/api/sar/thermal.jpg").content[:2] == b"\xff\xd8"

        body = {"decision": "confirmed", "note": "person lying down, not moving"}
        url = f"/api/sar/candidates/{candidate_id}/review"
        assert client.post(url, json=body).status_code == 401
        assert client.get("/api/sar/candidates").json()["candidates"][0]["status"] == "unreviewed"
        reviewed = client.post(url, json=body, headers=AUTH).json()["candidate"]
        assert reviewed["status"] == "confirmed"
        assert (
            client.post("/api/sar/candidates/C999/review", json=body, headers=AUTH).status_code
            == 404
        )
        assert client.post(url, json={"decision": "maybe"}, headers=AUTH).status_code == 422
        assert vehicle.commands == []  # a confirmation moves nothing

        saved = client.post("/api/sar/report/save").json()["saved"]
        report = json.loads(Path(saved["json"]).read_text())
        assert report["candidates_by_status"]["confirmed"] == 1
        assert "confirmed" in Path(saved["markdown"]).read_text()


def test_search_support_is_off_without_a_thermal_sensor_or_search_area(tmp_path: Path) -> None:
    vehicle = FakeVehicle()
    base = {"operator_token": TOKEN, "record_flights": False, "frontend_dist": tmp_path / "none"}
    with TestClient(create_app(Settings(**base), TelemetryHub(vehicle, vehicle))) as client:
        assert client.get("/api/sar").json()["enabled"] is False
        assert client.get("/api/sar/candidates").status_code == 409
    settings = Settings(**base, thermal="model", scenario="open_field")
    with TestClient(create_app(settings, TelemetryHub(vehicle, vehicle))) as client:
        status = client.get("/api/sar").json()
        assert status["enabled"] is False and "search_area" in status["reason"]
