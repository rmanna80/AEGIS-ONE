"""Depth frames pushed over the TCP frame stream (the Gazebo depth camera bridge)."""

from __future__ import annotations

import math
import time
from typing import Any

import numpy as np

from aegis.contracts import Provenance
from aegis.navigation.depth import DepthFrame
from aegis.perception.cameras import ACCEPTED_PROVENANCE, StreamCameraAdapter


class DepthStreamAdapter(StreamCameraAdapter):
    """Same wire format as the camera stream, with ``encoding`` "depth32f" (float32
    metres, planar depth) or "depth16u" (uint16 millimetres). ``horizontal_fov_rad``
    and ``max_range_m`` may be given in the header; otherwise the configured values apply."""

    def __init__(self, host: str, port: int, horizontal_fov_deg: float, max_range_m: float) -> None:
        super().__init__(host, port)
        self.sensor_id = "depth-stream"
        self.source = f"stream:{port}"
        self._hfov = math.radians(horizontal_fov_deg)
        self._max_range = max_range_m
        self._latest_depth: DepthFrame | None = None

    def _accept(self, header: dict[str, Any], payload: bytes) -> None:
        if not isinstance(header, dict):
            raise ValueError("header is not a JSON object")
        try:
            provenance = Provenance(str(header.get("provenance")))
        except ValueError as exc:
            raise ValueError(f"unknown provenance {header.get('provenance')!r}") from exc
        if provenance not in ACCEPTED_PROVENANCE:
            raise ValueError(f"provenance {provenance.value!r} is not accepted in this release")
        try:
            width, height = int(header["width"]), int(header["height"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("header needs integer width and height") from exc
        if not (0 < width <= 4096 and 0 < height <= 4096):
            raise ValueError(f"implausible frame size {width}x{height}")
        encoding = header.get("encoding")
        dtype: np.dtype[Any]
        if encoding == "depth32f":
            dtype, scale = np.dtype("<f4"), 1.0
        elif encoding == "depth16u":
            dtype, scale = np.dtype("<u2"), 0.001
        else:
            raise ValueError(f"unsupported depth encoding {encoding!r}")
        if len(payload) != width * height * dtype.itemsize:
            raise ValueError(
                f"payload is {len(payload)} bytes, expected {width * height * dtype.itemsize}"
            )
        depth = (
            np.frombuffer(payload, dtype=dtype).reshape(height, width).astype(np.float32) * scale
        )
        max_range = float(header.get("max_range_m") or self._max_range)
        if encoding == "depth16u":
            depth[depth == 0.0] = np.inf  # 0 mm means "no return"
        depth[depth >= max_range] = np.inf
        self._frame_id += 1
        self.sensor_id = str(header.get("sensor_id", "depth-stream"))[:64]
        self._latest_depth = DepthFrame(
            depth_m=depth,
            horizontal_fov_rad=float(header.get("horizontal_fov_rad") or self._hfov),
            max_range_m=max_range,
            frame_id=self._frame_id,
            sensor_id=self.sensor_id,
            provenance=provenance,
            source=f"stream:{str(header.get('source', 'unknown'))[:64]}",
            captured_at_unix_s=time.time(),
            pose=None,  # paired with the latest telemetry pose by the navigation service
        )
        self._new_frame.set()

    async def read_frame(self) -> DepthFrame | None:  # type: ignore[override]
        await super().read_frame()
        frame, self._latest_depth = self._latest_depth, None
        return frame
