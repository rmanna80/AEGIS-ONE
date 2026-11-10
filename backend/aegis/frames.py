"""Coordinate-frame conversions.

Frames used by AEGIS ONE:

* **WGS84** geodetic (latitude, longitude in degrees; altitude in metres).
* **ECEF** earth-centred earth-fixed Cartesian (metres) - intermediate only.
* **ENU** local tangent plane: x = East, y = North, z = Up. Used for mission
  authoring, geofences and all visualisation.
* **NED** local tangent plane: x = North, y = East, z = Down. Used by PX4/MAVLink.
* **Body FRD**: x = Forward, y = Right, z = Down, fixed to the vehicle.

All functions are pure and dependency-free so they can run on the Pi as well.
"""

from __future__ import annotations

import math

# WGS84 ellipsoid
_A = 6378137.0
_F = 1.0 / 298.257223563
_E2 = _F * (2.0 - _F)
_B = _A * (1.0 - _F)
_EP2 = (_A * _A - _B * _B) / (_B * _B)

Vec = tuple[float, float, float]


def geodetic_to_ecef(lat_deg: float, lon_deg: float, alt_m: float) -> Vec:
    lat, lon = math.radians(lat_deg), math.radians(lon_deg)
    n = _A / math.sqrt(1.0 - _E2 * math.sin(lat) ** 2)
    x = (n + alt_m) * math.cos(lat) * math.cos(lon)
    y = (n + alt_m) * math.cos(lat) * math.sin(lon)
    z = (n * (1.0 - _E2) + alt_m) * math.sin(lat)
    return x, y, z


def ecef_to_geodetic(x: float, y: float, z: float) -> Vec:
    """Bowring's closed-form solution; sub-millimetre for near-surface points."""
    p = math.hypot(x, y)
    lon = math.atan2(y, x)
    theta = math.atan2(z * _A, p * _B)
    lat = math.atan2(
        z + _EP2 * _B * math.sin(theta) ** 3,
        p - _E2 * _A * math.cos(theta) ** 3,
    )
    n = _A / math.sqrt(1.0 - _E2 * math.sin(lat) ** 2)
    if abs(math.cos(lat)) > 1e-9:
        alt = p / math.cos(lat) - n
    else:  # at the poles
        alt = abs(z) - _B
    return math.degrees(lat), math.degrees(lon), alt


def _enu_basis(lat_deg: float, lon_deg: float) -> tuple[Vec, Vec, Vec]:
    lat, lon = math.radians(lat_deg), math.radians(lon_deg)
    sl, cl = math.sin(lat), math.cos(lat)
    so, co = math.sin(lon), math.cos(lon)
    east = (-so, co, 0.0)
    north = (-sl * co, -sl * so, cl)
    up = (cl * co, cl * so, sl)
    return east, north, up


def geodetic_to_enu(
    lat_deg: float,
    lon_deg: float,
    alt_m: float,
    origin_lat_deg: float,
    origin_lon_deg: float,
    origin_alt_m: float,
) -> Vec:
    """Position of a WGS84 point in the ENU frame anchored at the origin."""
    px, py, pz = geodetic_to_ecef(lat_deg, lon_deg, alt_m)
    ox, oy, oz = geodetic_to_ecef(origin_lat_deg, origin_lon_deg, origin_alt_m)
    d = (px - ox, py - oy, pz - oz)
    east, north, up = _enu_basis(origin_lat_deg, origin_lon_deg)
    return (
        sum(a * b for a, b in zip(east, d, strict=True)),
        sum(a * b for a, b in zip(north, d, strict=True)),
        sum(a * b for a, b in zip(up, d, strict=True)),
    )


def enu_to_geodetic(
    east_m: float,
    north_m: float,
    up_m: float,
    origin_lat_deg: float,
    origin_lon_deg: float,
    origin_alt_m: float,
) -> Vec:
    """WGS84 (lat_deg, lon_deg, alt_m) of an ENU point anchored at the origin."""
    east, north, up = _enu_basis(origin_lat_deg, origin_lon_deg)
    ox, oy, oz = geodetic_to_ecef(origin_lat_deg, origin_lon_deg, origin_alt_m)
    x = ox + east[0] * east_m + north[0] * north_m + up[0] * up_m
    y = oy + east[1] * east_m + north[1] * north_m + up[1] * up_m
    z = oz + east[2] * east_m + north[2] * north_m + up[2] * up_m
    return ecef_to_geodetic(x, y, z)


def enu_to_ned(east: float, north: float, up: float) -> Vec:
    return north, east, -up


def ned_to_enu(north: float, east: float, down: float) -> Vec:
    return east, north, -down


def quaternion_to_euler_deg(w: float, x: float, y: float, z: float) -> Vec:
    """Body(FRD)->NED quaternion to aerospace ZYX Euler angles (roll, pitch, yaw) in degrees."""
    roll = math.atan2(2.0 * (w * x + y * z), 1.0 - 2.0 * (x * x + y * y))
    sin_pitch = max(-1.0, min(1.0, 2.0 * (w * y - z * x)))
    pitch = math.asin(sin_pitch)
    yaw = math.atan2(2.0 * (w * z + x * y), 1.0 - 2.0 * (y * y + z * z))
    return math.degrees(roll), math.degrees(pitch), math.degrees(yaw)


def rotate_body_to_ned(q_wxyz: tuple[float, float, float, float], v_body: Vec) -> Vec:
    """Rotate a body-frame (FRD) vector into NED using a body->NED unit quaternion."""
    w, x, y, z = q_wxyz
    vx, vy, vz = v_body
    # v' = v + 2*w*(q_v x v) + 2*q_v x (q_v x v)
    cx, cy, cz = y * vz - z * vy, z * vx - x * vz, x * vy - y * vx
    ccx, ccy, ccz = y * cz - z * cy, z * cx - x * cz, x * cy - y * cx
    return (
        vx + 2.0 * (w * cx + ccx),
        vy + 2.0 * (w * cy + ccy),
        vz + 2.0 * (w * cz + ccz),
    )


def wrap_heading_deg(yaw_deg: float) -> float:
    """Map any yaw angle to a compass heading in [0, 360)."""
    return yaw_deg % 360.0
