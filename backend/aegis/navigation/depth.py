"""Depth sensing: data contract, pinhole model and a simulated depth camera.

Camera convention (forward-facing, rigidly mounted at the vehicle origin):
optical axis = body x (forward), image u to the right = body y, image v down =
body z. A depth image stores **planar depth**: distance along the optical axis,
in metres; ``inf`` means "no return within range".
"""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

import numpy as np
from numpy.typing import NDArray

from aegis import frames
from aegis.contracts import Provenance, VehicleState
from aegis.navigation.geometry import SceneGeometry

Depth = NDArray[np.float32]


@dataclass(frozen=True)
class Pose:
    position_enu_m: tuple[float, float, float]
    quaternion_wxyz: tuple[float, float, float, float]  # body (FRD) -> NED


@dataclass(frozen=True)
class DepthFrame:
    depth_m: Depth  # H x W planar depth
    horizontal_fov_rad: float
    max_range_m: float
    frame_id: int
    sensor_id: str
    provenance: Provenance
    source: str
    captured_at_unix_s: float
    pose: Pose | None = None  # vehicle pose at capture, when the source knows it

    @property
    def width(self) -> int:
        return int(self.depth_m.shape[1])

    @property
    def height(self) -> int:
        return int(self.depth_m.shape[0])


@runtime_checkable
class DepthSource(Protocol):
    sensor_id: str

    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    async def read_frame(self) -> DepthFrame | None: ...


def pixel_rays_body(width: int, height: int, horizontal_fov_rad: float) -> NDArray[np.float64]:
    """Per-pixel ray directions in the body frame, scaled so that x (forward) = 1.

    Multiplying a ray by the pixel's planar depth gives the 3D point in body FRD.
    Returns an ``(H, W, 3)`` array.
    """
    focal = (width / 2.0) / math.tan(horizontal_fov_rad / 2.0)
    u = (np.arange(width) + 0.5 - width / 2.0) / focal
    v = (np.arange(height) + 0.5 - height / 2.0) / focal
    uu, vv = np.meshgrid(u, v)
    return np.stack([np.ones_like(uu), uu, vv], axis=-1)


def body_to_enu_matrix(q_wxyz: tuple[float, float, float, float]) -> NDArray[np.float64]:
    """3x3 matrix taking body-FRD vectors to ENU, from a body->NED quaternion."""
    columns = [
        frames.rotate_body_to_ned(q_wxyz, axis) for axis in ((1, 0, 0), (0, 1, 0), (0, 0, 1))
    ]
    ned = np.array(columns, dtype=np.float64).T  # columns are body axes expressed in NED
    ned_to_enu = np.array([[0.0, 1.0, 0.0], [1.0, 0.0, 0.0], [0.0, 0.0, -1.0]])
    return ned_to_enu @ ned


def pose_from_state(state: VehicleState) -> Pose | None:
    if state.position_enu_m is None or state.attitude is None:
        return None
    p = state.position_enu_m
    return Pose((p.x, p.y, p.z), state.attitude.quaternion_wxyz)


class RaycastDepthCamera:
    """Simulated depth camera: casts rays from the vehicle's telemetry pose against
    the scenario's known geometry, with range limit, field of view and range noise.

    This is a software sensor model, not Gazebo: it sees exactly the obstacles in
    the scenario file. It lets mapping and planning run (and be scored against
    ground truth) on any simulator, including PX4's built-in one.
    """

    provenance = Provenance.SIMULATED

    def __init__(
        self,
        scene: SceneGeometry,
        get_state: Callable[[], Awaitable[VehicleState]],
        width: int = 96,
        height: int = 72,
        horizontal_fov_deg: float = 73.0,
        max_range_m: float = 19.0,
        range_noise_fraction: float = 0.01,
        rate_hz: float = 5.0,
        seed: int | None = None,
    ) -> None:
        self.scene = scene
        self._get_state = get_state
        self.sensor_id = "raycast-depth"
        self.source = "raycast:scenario"
        self.hfov = math.radians(horizontal_fov_deg)
        self.max_range_m = max_range_m
        self.noise = range_noise_fraction
        self.period_s = 1.0 / rate_hz
        self._rays = pixel_rays_body(width, height, self.hfov)
        self._rng = np.random.default_rng(seed)
        self._frame_id = 0
        self._next_due = 0.0

    async def start(self) -> None:
        self._next_due = time.monotonic()

    async def stop(self) -> None: ...

    def render(self, pose: Pose) -> Depth:
        """Depth image for a pose (public: also used to generate ground truth in tests)."""
        rotation = body_to_enu_matrix(pose.quaternion_wxyz)
        rays = self._rays.reshape(-1, 3)
        scale = np.linalg.norm(rays, axis=1)  # range = planar depth * scale
        directions = (rays / scale[:, None]) @ rotation.T
        ranges = self.scene.raycast(np.array(pose.position_enu_m), directions, self.max_range_m)
        if self.noise > 0.0:
            ranges = ranges * (1.0 + self._rng.normal(0.0, self.noise, ranges.shape))
        depth = ranges / scale
        return depth.reshape(self._rays.shape[:2]).astype(np.float32)

    async def read_frame(self) -> DepthFrame | None:
        delay = self._next_due - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)
        self._next_due = max(self._next_due + self.period_s, time.monotonic())
        state = await self._get_state()
        pose = pose_from_state(state) if state.connected else None
        if pose is None:
            return None
        depth = await asyncio.to_thread(self.render, pose)
        self._frame_id += 1
        return DepthFrame(
            depth_m=depth,
            horizontal_fov_rad=self.hfov,
            max_range_m=self.max_range_m,
            frame_id=self._frame_id,
            sensor_id=self.sensor_id,
            provenance=self.provenance,
            source=self.source,
            captured_at_unix_s=time.time(),
            pose=pose,
        )
