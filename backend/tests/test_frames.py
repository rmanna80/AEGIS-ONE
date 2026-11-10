import math

import pytest

from aegis import frames

ZURICH = (47.397742, 8.545594, 489.4)  # PX4 SITL default home


def test_ecef_known_point_equator_prime_meridian() -> None:
    x, y, z = frames.geodetic_to_ecef(0.0, 0.0, 0.0)
    assert (x, y, z) == pytest.approx((6378137.0, 0.0, 0.0), abs=1e-6)


def test_ecef_known_point_north_pole() -> None:
    x, y, z = frames.geodetic_to_ecef(90.0, 0.0, 0.0)
    assert x == pytest.approx(0.0, abs=1e-6)
    assert z == pytest.approx(6356752.314245, abs=1e-4)


@pytest.mark.parametrize(
    "lat,lon,alt",
    [ZURICH, (-33.86, 151.21, 20.0), (35.0, -79.77, 110.0), (0.0, 179.9, 0.0), (89.9, 10.0, 5.0)],
)
def test_geodetic_ecef_round_trip(lat: float, lon: float, alt: float) -> None:
    lat2, lon2, alt2 = frames.ecef_to_geodetic(*frames.geodetic_to_ecef(lat, lon, alt))
    assert lat2 == pytest.approx(lat, abs=1e-9)
    assert lon2 == pytest.approx(lon, abs=1e-9)
    assert alt2 == pytest.approx(alt, abs=1e-3)


def test_origin_maps_to_zero_enu() -> None:
    assert frames.geodetic_to_enu(*ZURICH, *ZURICH) == pytest.approx((0, 0, 0), abs=1e-9)


def test_enu_axes_point_east_north_up() -> None:
    lat, lon, alt = ZURICH
    e, n, u = frames.geodetic_to_enu(lat + 1e-4, lon, alt, *ZURICH)
    assert n > 10.0 and abs(e) < 1e-3  # moving north in latitude
    e, n, u = frames.geodetic_to_enu(lat, lon + 1e-4, alt, *ZURICH)
    assert e > 5.0 and abs(n) < 1e-3  # moving east in longitude
    e, n, u = frames.geodetic_to_enu(lat, lon, alt + 10.0, *ZURICH)
    assert u == pytest.approx(10.0, abs=1e-6)


def test_one_degree_of_latitude_is_about_111_km() -> None:
    _, n, _ = frames.geodetic_to_enu(1.0, 0.0, 0.0, 0.0, 0.0, 0.0)
    assert n == pytest.approx(110_574.0, rel=1e-3)


@pytest.mark.parametrize("enu", [(35.0, -30.0, 15.0), (-80.0, 80.0, 0.0), (500.0, 500.0, 120.0)])
def test_enu_geodetic_round_trip(enu: tuple[float, float, float]) -> None:
    back = frames.geodetic_to_enu(*frames.enu_to_geodetic(*enu, *ZURICH), *ZURICH)
    assert back == pytest.approx(enu, abs=1e-4)


def test_enu_ned_swap_and_inverse() -> None:
    assert frames.enu_to_ned(1.0, 2.0, 3.0) == (2.0, 1.0, -3.0)
    assert frames.ned_to_enu(*frames.enu_to_ned(1.0, 2.0, 3.0)) == (1.0, 2.0, 3.0)


def _yaw_quaternion(yaw_deg: float) -> tuple[float, float, float, float]:
    half = math.radians(yaw_deg) / 2.0
    return (math.cos(half), 0.0, 0.0, math.sin(half))


def test_quaternion_identity_is_level_facing_north() -> None:
    assert frames.quaternion_to_euler_deg(1.0, 0.0, 0.0, 0.0) == pytest.approx((0, 0, 0))


def test_yaw_90_faces_east() -> None:
    q = _yaw_quaternion(90.0)
    assert frames.quaternion_to_euler_deg(*q) == pytest.approx((0.0, 0.0, 90.0), abs=1e-9)
    # Body-forward (x) expressed in NED must point east (NED y).
    assert frames.rotate_body_to_ned(q, (1.0, 0.0, 0.0)) == pytest.approx((0, 1, 0), abs=1e-12)


def test_pitch_up_tilts_forward_axis_upwards() -> None:
    half = math.radians(30.0) / 2.0
    q = (math.cos(half), 0.0, math.sin(half), 0.0)  # +30 deg about body y
    roll, pitch, yaw = frames.quaternion_to_euler_deg(*q)
    assert (roll, pitch, yaw) == pytest.approx((0.0, 30.0, 0.0), abs=1e-9)
    north, east, down = frames.rotate_body_to_ned(q, (1.0, 0.0, 0.0))
    assert down < 0.0  # nose up means the forward axis points above the horizon


def test_gimbal_lock_does_not_raise() -> None:
    half = math.radians(90.0) / 2.0
    _, pitch, _ = frames.quaternion_to_euler_deg(math.cos(half), 0.0, math.sin(half), 0.0)
    assert pitch == pytest.approx(90.0, abs=1e-6)


@pytest.mark.parametrize("yaw,heading", [(0.0, 0.0), (-90.0, 270.0), (180.0, 180.0), (360.0, 0.0)])
def test_wrap_heading(yaw: float, heading: float) -> None:
    assert frames.wrap_heading_deg(yaw) == pytest.approx(heading)
