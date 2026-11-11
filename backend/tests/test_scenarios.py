"""Every scenario file must be loadable, flyable and consistent with its Gazebo world."""

from __future__ import annotations

import importlib.util
import json
import math
import xml.dom.minidom
from pathlib import Path
from typing import Any

import pytest

from aegis.missions import Mission, validate_mission

REPO_ROOT = Path(__file__).resolve().parents[2]
SCENARIOS = sorted((REPO_ROOT / "simulation" / "scenarios").glob("*.json"))
MIN_OBSTACLE_CLEARANCE_M = 5.0


def load(path: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(path.read_text())
    return data


def generator() -> Any:
    spec = importlib.util.spec_from_file_location(
        "generate_gazebo_world", REPO_ROOT / "scripts" / "generate_gazebo_world.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def distance_to_footprint(point: tuple[float, float], obstacle: dict[str, Any]) -> float:
    """Horizontal distance from a point to an obstacle's footprint (0 if inside)."""
    dx, dy = point[0] - obstacle["east_m"], point[1] - obstacle["north_m"]
    if obstacle["shape"] == "cylinder":
        return max(0.0, math.hypot(dx, dy) - obstacle["radius_m"])
    yaw = math.radians(obstacle.get("yaw_deg", 0.0))
    local_x = dx * math.cos(yaw) + dy * math.sin(yaw)
    local_y = -dx * math.sin(yaw) + dy * math.cos(yaw)
    half_x, half_y = obstacle["size_enu_m"][0] / 2, obstacle["size_enu_m"][1] / 2
    return math.hypot(max(abs(local_x) - half_x, 0.0), max(abs(local_y) - half_y, 0.0))


def test_expected_scenarios_exist() -> None:
    assert {p.stem for p in SCENARIOS} >= {"open_field", "urban"}


@pytest.mark.parametrize("path", SCENARIOS, ids=lambda p: p.stem)
def test_demo_mission_is_valid(path: Path) -> None:
    scenario = load(path)
    assert scenario["scenario_id"] == path.stem
    mission = Mission.model_validate(scenario["demo_mission"])
    assert validate_mission(mission) == []


@pytest.mark.parametrize("path", SCENARIOS, ids=lambda p: p.stem)
def test_demo_route_keeps_clear_of_obstacles_it_does_not_overfly(path: Path) -> None:
    """Mission validation does not know about obstacles yet (Phase 3), so the shipped
    routes are checked here: wherever the route is below an obstacle's top plus a
    margin, it must stay at least 5 m away horizontally."""
    scenario = load(path)
    waypoints = scenario["demo_mission"]["waypoints"]
    first = waypoints[0]
    route = [(0.0, 0.0, first["altitude_rel_m"])] + [
        (w["east_m"], w["north_m"], w["altitude_rel_m"]) for w in waypoints
    ]
    for (e1, n1, a1), (e2, n2, a2) in zip(route, route[1:], strict=False):
        for step in range(51):
            f = step / 50
            point = (e1 + f * (e2 - e1), n1 + f * (n2 - n1))
            altitude = a1 + f * (a2 - a1)
            for obstacle in scenario["obstacles"]:
                top = (
                    obstacle["size_enu_m"][2]
                    if obstacle["shape"] == "box"
                    else obstacle["height_m"]
                )
                if altitude > top + 3.0:
                    continue
                clearance = distance_to_footprint(point, obstacle)
                assert clearance >= MIN_OBSTACLE_CLEARANCE_M, (
                    f"route passes {clearance:.1f} m from {obstacle['id']} at {altitude:.0f} m"
                )


@pytest.mark.parametrize("path", SCENARIOS, ids=lambda p: p.stem)
def test_committed_gazebo_world_matches_the_scenario(path: Path) -> None:
    scenario = load(path)
    expected = generator().build_world(scenario)
    world_file = REPO_ROOT / "simulation" / "gazebo" / "worlds" / f"{scenario['gazebo_world']}.sdf"
    assert world_file.read_text() == expected, "run scripts/generate_gazebo_world.py"

    document = xml.dom.minidom.parseString(expected)
    names = {m.getAttribute("name") for m in document.getElementsByTagName("model")}
    assert {o["id"] for o in scenario["obstacles"]} <= names
    assert {"ground_plane", "landing_pad"} <= names
    includes = document.getElementsByTagName("include")
    assert len(includes) == len(scenario.get("props", []))
    for include in includes:  # staged targets never move
        assert include.getElementsByTagName("static")[0].firstChild.data == "true"


def test_urban_scenario_stages_people_and_vehicles_in_view_of_the_route() -> None:
    scenario = load(REPO_ROOT / "simulation" / "scenarios" / "urban.json")
    kinds = {p["kind"] for p in scenario["props"]}
    assert kinds == {"person", "vehicle"}
    assert all(p["expected_labels"] for p in scenario["props"])
    # The street runs north from home; every target must be beside it, not on the flight line.
    assert all(abs(p["east_m"]) >= 2.0 for p in scenario["props"])
    # Props sit on surfaces, not inside buildings.
    for prop in scenario["props"]:
        for obstacle in scenario["obstacles"]:
            assert distance_to_footprint((prop["east_m"], prop["north_m"]), obstacle) > 0.5


def test_scenario_detector_labels_are_classes_the_default_model_knows() -> None:
    coco = {"person", "bicycle", "car", "motorcycle", "airplane", "bus", "train", "truck"}
    scenario = load(REPO_ROOT / "simulation" / "scenarios" / "urban.json")
    labels = set(scenario["detector_labels"])
    assert labels <= coco and "airplane" not in labels
    for prop in scenario["props"]:  # every staged target stays detectable after filtering
        assert set(prop["expected_labels"]) & labels
