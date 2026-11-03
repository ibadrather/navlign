"""Measure the camera of a video mode from the video and its telemetry.

A nadir camera at height H, flying at v, sees the ground move at v * f / H pixels per second, so f = flow * H / v. This
is the camera that gives the right footprint and ground resolution at a telemetry height, which is what planning a
flight needs, and it is metric: speed and height set its scale.

Tree tops are closer and move faster, so each pair of samples uses its slow points, and the survey takes the low end
of the pairs, which is the ground. Frames with open water are left out: their few tracks can be reflections, which
sit below the ground and move too slowly. H is above the take-off point, not above the ground under the camera: 2.5 m
of difference at 50 m is 5 %, hence ACCURACY.

Bundle adjustment settles 6 to 11 % shorter on the Mini 4K, from any starting value, and by an amount that moves with
the site. Which is right is open, and sfm is free to refine this value as it does.
"""

import math
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

from navlign.images import Json, PrepError
from navlign.scan import ClipScan

ACCURACY = 0.05
SENSOR_WIDTH_MM = 6.17  # only the ratio to the focal length matters: this is the 1/2.3 inch sensor of the Mini 4K
MIN_SPEED_MS = 1.0  # below this, the 0.1 m/s telemetry resolution starts to count
MAX_SPEED_CHANGE_MS = 0.25  # over the 3 s around a pair: the 1 Hz telemetry only follows steady flight
MAX_TURN_DEG = 0.3  # between two samples: turning moves the corners of the image as well
LAND_TEXTURE = 0.85  # keeps frames with water in them out
MIN_PAIRS = 20
GROUND_PERCENTILE = 10
MAX_BLUR_PX = 1.0  # motion blur the speed limit keeps to


@dataclass(frozen=True)
class Camera:
    """A video mode measured from footage."""

    width: int
    height: int
    focal_px: float
    flight_height_m: float
    pairs: int
    exposure_s: float | None  # the longest typical exposure, for the speed limit

    @property
    def focal_mm(self) -> float:
        """The focal length on a sensor of SENSOR_WIDTH_MM."""
        return self.focal_px / self.width * SENSOR_WIDTH_MM

    @property
    def sensor_height_mm(self) -> float:
        """The height of the sensor area this video mode uses."""
        return SENSOR_WIDTH_MM * self.height / self.width

    @property
    def fov_deg(self) -> tuple[float, float]:
        """Horizontal and vertical field of view."""
        horizontal, vertical = (2 * math.atan(side / 2 / self.focal_px) for side in (self.width, self.height))
        return math.degrees(horizontal), math.degrees(vertical)

    def footprint_m(self, height_m: float) -> tuple[float, float]:
        """The ground seen from ``height_m``: across the image (long side), along it (short side)."""
        return self.width / self.focal_px * height_m, self.height / self.focal_px * height_m

    def gsd_cm(self, height_m: float) -> float:
        """The ground size of one pixel from ``height_m``."""
        return height_m / self.focal_px * 100

    def speed_limit_ms(self, height_m: float) -> float | None:
        """The ground speed that blurs by MAX_BLUR_PX at the typical long exposure, if the telemetry gives it."""
        return MAX_BLUR_PX * self.gsd_cm(height_m) / 100 / self.exposure_s if self.exposure_s else None

    def acquisition_profile(self) -> Json:
        """The sensor block for mission planning: footprints come out right when computed as height * sensor / focal."""
        return {
            "type": "camera",
            "sensor_w_mm": SENSOR_WIDTH_MM,
            "sensor_h_mm": round(self.sensor_height_mm, 3),
            "focal_mm": round(self.focal_mm, 3),
            "image_w_px": self.width,
            "image_h_px": self.height,
        }


def measure_camera(scans: Sequence[ClipScan]) -> Camera:
    """The camera that fits the image motion of ``scans``, clips of one video mode."""
    if len({(scan.width, scan.height) for scan in scans}) > 1:
        raise PrepError("clips differ in frame size: each video mode has its own camera; measure them separately")
    estimates = [value for scan in scans for value in _focal_estimates(scan)]
    if len(estimates) < MIN_PAIRS:
        raise PrepError(
            f"only {len(estimates)} usable moments to measure the camera: it needs steady flight of at least "
            f"{MIN_SPEED_MS:g} m/s over land, or --focal-length-mm and --sensor-width-mm"
        )
    exposures = [1 / entry.fields["shutter"] for scan in scans for entry in scan.entries if "shutter" in entry.fields]
    return Camera(
        width=scans[0].width,
        height=scans[0].height,
        focal_px=float(np.percentile(estimates, GROUND_PERCENTILE)),
        flight_height_m=float(np.median([entry.fields["rel_alt"] for scan in scans for entry in scan.entries])),
        pairs=len(estimates),
        exposure_s=float(np.percentile(exposures, 90)) if exposures else None,
    )


def _focal_estimates(scan: ClipScan) -> list[float]:
    """f = flow * H / v for each pair of samples in steady, straight flight over land."""
    estimates: list[float] = []
    interval = scan.sample_interval_s
    for sample in scan.samples:
        if sample.surface_px is None or sample.motion is None or sample.texture < LAND_TEXTURE:
            continue
        if abs(math.degrees(math.atan2(sample.motion[1, 0], sample.motion[0, 0]))) > MAX_TURN_DEG:
            continue
        time = scan.times[sample.frame]
        speeds = [scan.at("speed_ms", time + offset) for offset in (-1.0, 0.0, 1.0)]
        if min(speeds) < MIN_SPEED_MS or max(speeds) - min(speeds) > MAX_SPEED_CHANGE_MS:
            continue
        estimates.append(sample.surface_px / interval / speeds[1] * scan.at("rel_alt", time))
    return estimates
