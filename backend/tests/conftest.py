from __future__ import annotations

import time
from typing import Any

import pytest

from aegis.contracts import (
    Attitude,
    BatteryState,
    GeodeticPosition,
    GpsQuality,
    HealthFlags,
    LocalOrigin,
    Provenance,
    Vec3,
    VehicleState,
)
from aegis.missions import Geofence, Mission, MissionConstraints, Waypoint

HOME = GeodeticPosition(latitude_deg=47.397742, longitude_deg=8.545594, altitude_amsl_m=489.4)


def make_state(**overrides: Any) -> VehicleState:
    """A healthy, airborne, simulated vehicle; override fields per test."""
    base: dict[str, Any] = {
        "vehicle_id": "test-1",
        "provenance": Provenance.SIMULATED,
        "source": "test:fixture",
        "received_at_unix_s": time.time(),
        "vehicle_time_s": 12.5,
        "connected": True,
        "armed": True,
        "in_air": True,
        "flight_mode": "MISSION",
        "landed_state": "IN_AIR",
        "origin": LocalOrigin(geodetic=HOME),
        "geodetic": HOME,
        "relative_altitude_m": 10.0,
        "position_enu_m": Vec3(x=0.0, y=0.0, z=10.0),
        "velocity_ned_m_s": Vec3(x=1.0, y=0.0, z=0.0),
        "attitude": Attitude(
            quaternion_wxyz=(1.0, 0.0, 0.0, 0.0), roll_deg=0.0, pitch_deg=0.0, yaw_deg=0.0
        ),
        "ground_speed_m_s": 1.0,
        "vertical_speed_up_m_s": 0.0,
        "battery": BatteryState(voltage_v=16.0, remaining_fraction=0.9),
        "gps": GpsQuality(fix_type="FIX_3D", num_satellites=10),
        "health": HealthFlags(
            gyrometer_calibration_ok=True,
            accelerometer_calibration_ok=True,
            magnetometer_calibration_ok=True,
            local_position_ok=True,
            global_position_ok=True,
            home_position_ok=True,
            armable=True,
        ),
    }
    base.update(overrides)
    return VehicleState(**base)


def make_mission(**constraint_overrides: Any) -> Mission:
    constraints: dict[str, Any] = {
        "geofence": Geofence(
            polygon_enu_m=[(-50.0, -50.0), (50.0, -50.0), (50.0, 50.0), (-50.0, 50.0)],
            max_altitude_rel_m=30.0,
        ),
        "min_altitude_rel_m": 5.0,
    }
    constraints.update(constraint_overrides)
    return Mission(
        mission_id="test-mission",
        name="Test mission",
        waypoints=[
            Waypoint(east_m=0.0, north_m=20.0, altitude_rel_m=10.0),
            Waypoint(east_m=20.0, north_m=20.0, altitude_rel_m=10.0),
        ],
        constraints=MissionConstraints(**constraints),
    )


@pytest.fixture
def state() -> VehicleState:
    return make_state()


@pytest.fixture
def mission() -> Mission:
    return make_mission()
