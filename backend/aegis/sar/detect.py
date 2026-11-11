"""Heat-source segmentation and geolocation for a nadir thermal image.

Classical image processing, no learned model: pixels clearly warmer than the
scene's typical temperature are grouped into blobs, and each blob is placed on
the ground using the vehicle pose. The ground is assumed flat at home elevation.

The ``category`` is a size-and-temperature description, not an identification.
"person-sized warm object" means exactly that: a person, an animal or anything
else of that size and warmth looks the same to this sensor.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
from scipy.ndimage import find_objects, label

from aegis.sar.thermal import ThermalFrame, ground_points, nadir_rays_body

EIGHT_CONNECTED = np.ones((3, 3), dtype=bool)


@dataclass(frozen=True)
class HeatBlob:
    east_m: float
    north_m: float
    peak_c: float
    mean_c: float
    ambient_c: float
    pixels: int
    area_m2: float
    bbox_px: tuple[int, int, int, int]  # x0, y0, x1, y1 (exclusive)
    category: str
    touches_edge: bool


def categorise(area_m2: float, peak_c: float) -> str:
    if peak_c >= 60.0:
        return "hot object"
    if area_m2 > 2.0:
        return "large warm area"
    if 0.08 <= area_m2 <= 2.0 and 22.0 <= peak_c <= 42.0:
        return "person-sized warm object"
    return "small warm object"


def detect_heat_sources(
    frame: ThermalFrame,
    min_delta_c: float = 5.0,
    min_pixels: int = 2,
    max_tilt_deg: float = 25.0,
    min_height_m: float = 5.0,
) -> list[HeatBlob]:
    """Blobs at least ``min_delta_c`` warmer than the image's median temperature.

    Returns nothing while the vehicle is low or tilted hard: the flat-ground
    geolocation is unreliable then, and a wrong position is worse than none.
    """
    image = frame.temperature_c
    height_m = frame.pose.position_enu_m[2]
    rays = nadir_rays_body(frame.width, frame.height, frame.horizontal_fov_rad)
    points, valid = ground_points(rays, frame.pose)
    centre = points[frame.height // 2, frame.width // 2]
    if height_m < min_height_m or not valid.all():
        return []
    off_nadir = math.degrees(
        math.atan2(math.hypot(*(centre[:2] - np.array(frame.pose.position_enu_m[:2]))), height_m)
    )
    if off_nadir > max_tilt_deg:
        return []
    ambient = float(np.median(image))
    labels, count = label(image > ambient + min_delta_c, structure=EIGHT_CONNECTED)
    # Ground area of one pixel, from the spacing of neighbouring ground points.
    step_x = np.linalg.norm(points[:, 1:, :2] - points[:, :-1, :2], axis=2)
    step_y = np.linalg.norm(points[1:, :, :2] - points[:-1, :, :2], axis=2)
    pixel_area = float(np.median(step_x) * np.median(step_y))
    blobs: list[HeatBlob] = []
    for index, slices in enumerate(find_objects(labels), start=1):
        if slices is None:
            continue
        mask = labels[slices] == index
        pixels = int(mask.sum())
        if pixels < min_pixels:
            continue
        temps = image[slices][mask]
        weights = (temps - ambient).astype(np.float64)
        ground = points[slices][mask][:, :2]
        east, north = (ground * weights[:, None]).sum(axis=0) / weights.sum()
        rows, cols = slices
        area = pixels * pixel_area
        peak = float(temps.max())
        blobs.append(
            HeatBlob(
                east_m=float(east),
                north_m=float(north),
                peak_c=peak,
                mean_c=float(temps.mean()),
                ambient_c=ambient,
                pixels=pixels,
                area_m2=area,
                bbox_px=(cols.start, rows.start, cols.stop, rows.stop),
                category=categorise(area, peak),
                touches_edge=rows.start == 0
                or cols.start == 0
                or rows.stop == frame.height
                or cols.stop == frame.width,
            )
        )
    return blobs
