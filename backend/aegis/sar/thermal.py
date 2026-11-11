"""Thermal camera model (software sensor, SIMULATED data only).

A downward-looking radiometric camera. By default it sits on an ideal two-axis
stabilised mount: it stays pointed straight down while the vehicle pitches and
rolls, and turns with the vehicle's heading (``stabilised=False`` fixes it to the
body instead). A multirotor leans well over 20 degrees at cruise in PX4's built-in
simulator, which would swing a body-fixed camera far off the ground below it. The
mount is ideal: no lag, no travel limits. For every pixel a ray is cast
from the vehicle pose to the ground plane (z = 0 in the scenario's ENU frame);
obstacles in the scenario block the view. The pixel takes the temperature of
whatever the ray reaches: a heat source, an obstacle surface or the ground.
Optical blur and sensor noise (NETD) are then applied.

What this is not: a physical thermal model. There is no emissivity, atmosphere,
sun loading, time of day or body-heat transfer; temperatures are the surface
values written in the scenario file. It exists so detection, geolocation, the
review workflow and coverage planning can be built and measured end to end.
"""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

import numpy as np
from numpy.typing import NDArray
from scipy.ndimage import gaussian_filter

from aegis import frames
from aegis.contracts import Provenance, VehicleState
from aegis.navigation.depth import Pose, body_to_enu_matrix, pose_from_state
from aegis.navigation.geometry import SceneGeometry

Image = NDArray[np.float32]


@dataclass(frozen=True)
class ThermalFrame:
    temperature_c: Image  # H x W surface temperature, degrees Celsius
    horizontal_fov_rad: float
    frame_id: int
    sensor_id: str
    provenance: Provenance
    source: str
    captured_at_unix_s: float
    pose: Pose  # camera position and orientation the frame was taken from
    vehicle_time_s: float | None = None

    @property
    def width(self) -> int:
        return int(self.temperature_c.shape[1])

    @property
    def height(self) -> int:
        return int(self.temperature_c.shape[0])


class ThermalSource(Protocol):
    sensor_id: str
    source: str

    async def read_frame(self) -> ThermalFrame | None: ...


def nadir_rays_body(width: int, height: int, horizontal_fov_rad: float) -> NDArray[np.float64]:
    """Unit-depth pixel rays in body FRD for a camera looking along body +z (down).

    Image x (columns, left to right) is body +y (right); image y (rows, top to
    bottom) is body -x, so the top of the image is ahead of the vehicle.
    """
    focal = (width / 2.0) / math.tan(horizontal_fov_rad / 2.0)
    u = (np.arange(width) + 0.5 - width / 2.0) / focal
    v = (np.arange(height) + 0.5 - height / 2.0) / focal
    uu, vv = np.meshgrid(u, v)
    return np.stack([-vv, uu, np.ones_like(uu)], axis=-1)


def ground_points(
    rays_body: NDArray[np.float64], pose: Pose
) -> tuple[NDArray[np.float64], NDArray[np.bool_]]:
    """Where each ray meets the ground plane z = 0 (ENU), and which rays do.

    Returns (points, valid): points has the rays' shape; rows where ``valid`` is
    False (ray level or pointing up, or vehicle not above the ground) are NaN.
    """
    rotation = body_to_enu_matrix(pose.quaternion_wxyz)
    directions = rays_body @ rotation.T
    origin = np.array(pose.position_enu_m, dtype=np.float64)
    down = directions[..., 2]
    valid = (down < -1e-6) & (origin[2] > 0.0)
    scale = np.where(valid, -origin[2] / np.where(valid, down, -1.0), np.nan)
    points = origin + directions * scale[..., None]
    return points, valid


class HeatSources:
    """The scenario's warm objects, as footprints on the ground with a surface temperature."""

    def __init__(self, scenario: dict[str, Any]) -> None:
        thermal = scenario.get("thermal", {})
        self.ambient_c = float(thermal.get("ambient_c", 12.0))
        self.ground_variation_c = float(thermal.get("ground_variation_c", 0.8))
        self.obstacle_offset_c = float(thermal.get("obstacle_offset_c", 1.5))
        self.sources: list[dict[str, Any]] = list(scenario.get("heat_sources", []))

    def ground_temperature(self, east: NDArray[np.float64], north: NDArray[np.float64]) -> Image:
        """Ambient plus a fixed, smooth spatial pattern (the same place always reads the same)."""
        pattern = (
            np.sin(east * 0.11 + 0.7) * np.cos(north * 0.07 - 0.3)
            + 0.5 * np.sin(east * 0.31 + north * 0.23)
        ) / 1.5
        return (self.ambient_c + self.ground_variation_c * pattern).astype(np.float32)

    def apply(self, east: NDArray[np.float64], north: NDArray[np.float64], image: Image) -> None:
        """Overwrite pixels whose ground point lies inside a heat source's footprint."""
        for source in self.sources:
            dx, dy = east - source["east_m"], north - source["north_m"]
            if "radius_m" in source:
                inside = dx * dx + dy * dy <= source["radius_m"] ** 2
            else:
                length, width = source["size_m"]
                yaw = math.radians(source.get("yaw_deg", 0.0))
                along = dx * math.cos(yaw) + dy * math.sin(yaw)
                across = -dx * math.sin(yaw) + dy * math.cos(yaw)
                inside = (np.abs(along) <= length / 2) & (np.abs(across) <= width / 2)
            image[inside] = source["surface_c"]


class ThermalCameraModel:
    """Software thermal camera driven by the vehicle's telemetry pose."""

    provenance = Provenance.SIMULATED

    def __init__(
        self,
        scenario: dict[str, Any],
        get_state: Callable[[], Awaitable[VehicleState]] | None,
        width: int = 160,
        height: int = 120,
        horizontal_fov_deg: float = 57.0,
        netd_c: float = 0.08,
        blur_px: float = 0.6,
        rate_hz: float = 4.0,
        seed: int | None = None,
        stabilised: bool = True,
        vehicle_id: str | None = None,
    ) -> None:
        self.scene = SceneGeometry(scenario)
        self.heat = HeatSources(scenario)
        self.stabilised = stabilised
        self._get_state = get_state
        self.hfov = math.radians(horizontal_fov_deg)
        self.netd_c, self.blur_px = netd_c, blur_px
        self.period_s = 1.0 / rate_hz
        self.sensor_id = "thermal-model"
        self.source = "model:thermal-stabilised" if stabilised else "model:thermal-fixed"
        if vehicle_id is not None:
            self.source += f"@{vehicle_id}"
        self._rays = nadir_rays_body(width, height, self.hfov)
        self._rng = np.random.default_rng(seed)
        self._frame_id = 0
        self._next = 0.0

    def camera_pose(self, vehicle: Pose) -> Pose:
        """Camera pose for a vehicle pose: heading only when stabilised, else the body's."""
        if not self.stabilised:
            return vehicle
        yaw = math.radians(frames.quaternion_to_euler_deg(*vehicle.quaternion_wxyz)[2])
        return Pose(vehicle.position_enu_m, (math.cos(yaw / 2), 0.0, 0.0, math.sin(yaw / 2)))

    def render(self, pose: Pose) -> Image:
        """Temperature image for a pose (public: also used by tests)."""
        points, valid = ground_points(self._rays, pose)
        shape = self._rays.shape[:2]
        image = np.full(shape, self.heat.ambient_c, dtype=np.float32)
        if not valid.any():
            return image
        east, north = points[..., 0], points[..., 1]
        image[valid] = self.heat.ground_temperature(east[valid], north[valid])
        warm = image.copy()
        self.heat.apply(np.where(valid, east, np.inf), np.where(valid, north, np.inf), warm)
        image = warm
        # Occlusion: a ray that meets an obstacle before the ground shows the obstacle.
        origin = np.array(pose.position_enu_m, dtype=np.float64)
        flat_points = points.reshape(-1, 3)
        flat_valid = valid.reshape(-1)
        vectors = flat_points[flat_valid] - origin
        distance = np.linalg.norm(vectors, axis=1)
        if distance.size and (len(self.scene.boxes) or len(self.scene.cylinders)):
            reach = float(distance.max()) + 1.0
            hit = self.scene.raycast(origin, vectors / distance[:, None], reach)
            blocked = np.zeros(flat_valid.shape, dtype=bool)
            # The ground itself is a hit at `distance`; anything clearly nearer is an obstacle.
            blocked[flat_valid] = hit < distance - 0.3
            image.reshape(-1)[blocked] = self.heat.ambient_c + self.heat.obstacle_offset_c
        if self.blur_px > 0.0:
            image = gaussian_filter(image, self.blur_px).astype(np.float32)
        if self.netd_c > 0.0:
            image = image + self._rng.normal(0.0, self.netd_c, image.shape).astype(np.float32)
        return image

    async def read_frame(self) -> ThermalFrame | None:
        if self._get_state is None:
            return None
        delay = self._next - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)
        self._next = time.monotonic() + self.period_s
        state = await self._get_state()
        pose = pose_from_state(state) if state.connected else None
        if pose is None:
            return None
        pose = self.camera_pose(pose)
        image = await asyncio.to_thread(self.render, pose)
        self._frame_id += 1
        return ThermalFrame(
            temperature_c=image,
            horizontal_fov_rad=self.hfov,
            frame_id=self._frame_id,
            sensor_id=self.sensor_id,
            provenance=self.provenance,
            source=self.source,
            captured_at_unix_s=time.time(),
            pose=pose,
            vehicle_time_s=state.vehicle_time_s,
        )
