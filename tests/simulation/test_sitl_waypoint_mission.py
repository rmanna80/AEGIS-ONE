"""Simulation tests against a live PX4 SITL instance.

Skipped unless ``AEGIS_SITL_TESTS=1``. Start PX4 first (see README), then::

    AEGIS_SITL_TESTS=1 pytest tests/simulation -v -s

Covers simulation tests 1 and 2 from the project brief: take-off/landing and
waypoint-mission completion, plus the simulator-attestation gate.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import time
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from aegis.adapters.px4_sitl import PX4SITLAdapter
from aegis.contracts import Provenance
from aegis.missions import Mission, MissionPhase, point_in_polygon

pytestmark = [
    pytest.mark.sitl,
    pytest.mark.skipif(os.environ.get("AEGIS_SITL_TESTS") != "1", reason="needs PX4 SITL"),
]

SCENARIO = Path(__file__).resolve().parents[2] / "simulation" / "scenarios" / "open_field.json"


@pytest.fixture
async def adapter() -> AsyncIterator[PX4SITLAdapter]:
    px4 = PX4SITLAdapter(connection_url=os.environ.get("AEGIS_PX4_URL", "udpin://0.0.0.0:14540"))
    await px4.start()
    deadline = time.monotonic() + 60.0
    while time.monotonic() < deadline:
        state = await px4.get_state()
        ready = state.connected and px4.attested and state.origin and state.health
        if ready and state.health and state.health.armable and not state.armed:
            break
        await asyncio.sleep(0.5)
    else:
        pytest.fail("PX4 SITL did not become ready (connected, attested, armable) in 60 s")
    yield px4
    await px4.stop()


async def test_attested_simulated_telemetry(adapter: PX4SITLAdapter) -> None:
    state = await adapter.get_state()
    assert state.provenance is Provenance.SIMULATED
    assert state.source.startswith("px4_sitl:") and "unattested" not in state.source
    assert state.position_enu_m is not None
    assert math.hypot(state.position_enu_m.x, state.position_enu_m.y) < 5.0  # parked at home
    assert state.vehicle_time_s is not None and state.vehicle_time_s > 0.0


async def test_demo_mission_takeoff_waypoints_return_and_land(adapter: PX4SITLAdapter) -> None:
    mission = Mission.model_validate(json.loads(SCENARIO.read_text())["demo_mission"])
    fence = mission.constraints.geofence

    result = await adapter.submit_mission(mission)
    assert result.accepted, result.status

    max_altitude = 0.0
    closest = [math.inf] * len(mission.waypoints)
    deadline = time.monotonic() + 420.0
    status = await adapter.mission_status()
    while time.monotonic() < deadline:
        state = await adapter.get_state()
        status = await adapter.mission_status()
        assert state.position_enu_m is not None
        east, north = state.position_enu_m.x, state.position_enu_m.y
        assert point_in_polygon((east, north), fence.polygon_enu_m), "left the geofence"
        max_altitude = max(max_altitude, state.relative_altitude_m or 0.0)
        for i, wp in enumerate(mission.waypoints):
            closest[i] = min(closest[i], math.hypot(east - wp.east_m, north - wp.north_m))
        if status.phase in (MissionPhase.COMPLETED, MissionPhase.FAILED, MissionPhase.STALLED):
            break
        await asyncio.sleep(0.2)

    assert status.phase is MissionPhase.COMPLETED, status
    assert status.current_waypoint == len(mission.waypoints)
    # Every waypoint was actually overflown (5 Hz sampling at 5 m/s => allow slack).
    assert all(d < 5.0 for d in closest), closest
    assert 10.0 < max_altitude <= fence.max_altitude_rel_m + 1.0

    final = await adapter.get_state()
    assert final.armed is False and final.in_air is False
    assert final.position_enu_m is not None
    assert math.hypot(final.position_enu_m.x, final.position_enu_m.y) < 3.0  # landed at home
