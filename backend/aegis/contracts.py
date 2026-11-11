"""Typed data contracts shared by adapters, the API, the recorder and the dashboard.

Conventions (enforced by field names, not by comments elsewhere):

* Every measurement carries its **provenance** (simulated / replayed / physical).
* Units are part of the field name (``_m``, ``_m_s``, ``_deg``, ``_v``, ``_s``).
* Coordinate frames are part of the field name or type:
    - ``GeodeticPosition``: WGS84 latitude/longitude, altitude above mean sea level.
    - ``*_enu_m``: local tangent plane East-North-Up, origin = ``LocalOrigin``.
    - ``*_ned_m_s``: local tangent plane North-East-Down.
    - Attitude quaternion rotates the vehicle body frame (Forward-Right-Down)
      into the local NED frame (PX4 / MAVLink convention).
* Fields computed by AEGIS rather than reported by the vehicle are listed in
  ``VehicleState.derived_fields`` so the UI can label them as estimates.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class Provenance(StrEnum):
    """Where a measurement came from. Shown everywhere in logs and the UI."""

    SIMULATED = "simulated"
    REPLAYED = "replayed"
    PHYSICAL = "physical"


class Severity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Vec3(_Model):
    """A 3-vector. The frame and unit are given by the name of the field holding it."""

    x: float
    y: float
    z: float


class GeodeticPosition(_Model):
    """WGS84 position."""

    latitude_deg: float = Field(ge=-90.0, le=90.0)
    longitude_deg: float = Field(ge=-180.0, le=180.0)
    altitude_amsl_m: float


class LocalOrigin(_Model):
    """Origin of the local ENU/NED tangent plane (the vehicle's home position)."""

    geodetic: GeodeticPosition


class Attitude(_Model):
    """Body (FRD) -> local NED rotation."""

    quaternion_wxyz: tuple[float, float, float, float]
    roll_deg: float
    pitch_deg: float
    yaw_deg: float  # 0 = north, positive clockwise seen from above


class BatteryState(_Model):
    voltage_v: float | None = None
    remaining_fraction: float | None = Field(default=None, ge=0.0, le=1.0)


class GpsQuality(_Model):
    fix_type: str
    num_satellites: int


class HealthFlags(_Model):
    """Flight-controller self-reported health (PX4 pre-arm checks)."""

    gyrometer_calibration_ok: bool
    accelerometer_calibration_ok: bool
    magnetometer_calibration_ok: bool
    local_position_ok: bool
    global_position_ok: bool
    home_position_ok: bool
    armable: bool


class VehicleState(_Model):
    """One consolidated snapshot of a vehicle, assembled by a telemetry adapter."""

    vehicle_id: str
    provenance: Provenance
    source: str  # e.g. "px4_sitl:sih_quadx", "px4_sitl:gz_x500", "recording:<file>"
    recorded_provenance: Provenance | None = None  # original provenance when replayed

    received_at_unix_s: float  # host wall clock when the snapshot was assembled
    vehicle_time_s: float | None = None  # time since autopilot boot (sim time in SITL)
    # Simulation seconds per wall-clock second, measured by AEGIS; None when unknown
    # or not a simulation. 1.0 = real time.
    sim_real_time_factor: float | None = None

    connected: bool
    armed: bool | None = None
    in_air: bool | None = None
    flight_mode: str | None = None
    landed_state: str | None = None

    origin: LocalOrigin | None = None
    geodetic: GeodeticPosition | None = None
    relative_altitude_m: float | None = None  # above home
    position_enu_m: Vec3 | None = None  # derived from geodetic + origin
    velocity_ned_m_s: Vec3 | None = None
    attitude: Attitude | None = None

    ground_speed_m_s: float | None = None  # derived
    vertical_speed_up_m_s: float | None = None  # derived (= -velocity_ned.z)

    battery: BatteryState | None = None
    gps: GpsQuality | None = None
    health: HealthFlags | None = None

    derived_fields: tuple[str, ...] = (
        "position_enu_m",
        "ground_speed_m_s",
        "vertical_speed_up_m_s",
        "sim_real_time_factor",
    )


class Alert(_Model):
    """Advisory raised by an AEGIS monitor. Never a substitute for autopilot failsafes."""

    alert_id: str
    code: str  # stable machine-readable identifier, e.g. "telemetry.stale"
    severity: Severity
    message: str
    vehicle_id: str
    provenance: Provenance
    raised_at_unix_s: float
    active: bool = True


class AutopilotMessage(_Model):
    """A status text emitted by the flight controller (MAVLink STATUSTEXT)."""

    vehicle_id: str
    provenance: Provenance
    level: str  # MAVLink severity name as reported by MAVSDK, e.g. "INFO", "WARNING"
    text: str
    received_at_unix_s: float
