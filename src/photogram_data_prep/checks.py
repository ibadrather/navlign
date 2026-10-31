"""Say whether a set of chosen frames is worth handing to the pipeline, from geometry and telemetry alone.

Each check turns one thing that went wrong, or nearly did, on a real survey into a number and a threshold, so nobody has
to look through the footage. Whether the features really match is left to sfm. Open water is reported but is no
fault: there is nothing to map there, and the frames over it were already left out.
"""

import math
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

import numpy as np

from photogram_data_prep.camera import ACCURACY, Camera
from photogram_data_prep.images import Json
from photogram_data_prep.scan import MIN_TEXTURE, ClipScan, overlaps_between

MIN_FRAMES = 3  # two views define no reconstruction worth the name
OVERLAP_SLACK = 0.1  # how far under the target a stretch may fall before it counts
MIN_SIDE_OVERLAP = 0.6
MIN_LINK_OVERLAP = 0.3  # two frames that share this much of the ground can tie their lines together
SAME_LINE_M = 6.0  # frames closer than this across the track are on one line: smoothed GPS is good to about 2 m
SOFT = 0.85  # of the sharpness around a frame: blur, not just a different view
MAX_SOFT_SHARE = 0.1
MAX_BLUR_PX = 2.0
MIN_SATELLITES = 10
ZOOM_TOLERANCE = 0.001
EARTH_RADIUS_M = 6_371_000.0


class Level(StrEnum):
    """How a check came out."""

    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"


@dataclass(frozen=True)
class Check:
    """One thing looked at and what came of it."""

    name: str
    level: Level
    message: str


def verdict(checks: Sequence[Check]) -> Level:
    """The worst of ``checks``."""
    levels = {check.level for check in checks}
    return Level.FAIL if Level.FAIL in levels else Level.WARN if Level.WARN in levels else Level.PASS


def run_checks(
    clips: Sequence[tuple[ClipScan, Sequence[int]]],
    features: Sequence[Json],
    camera: Camera,
    overlap: float,
    given_focal_px: float | None,
) -> list[Check]:
    """Look at the kept samples of each clip, ``clips`` as (scan, kept), and at their ``features``."""
    return [
        _frames(features),
        _front_overlap(clips, overlap),
        _coverage(features, camera),
        _side_overlap(features, camera),
        _sharpness(clips),
        _motion_blur(clips, camera),
        _telemetry(clips, features),
        _featureless(clips),
        _camera(camera, given_focal_px),
    ]


def _frames(features: Sequence[Json]) -> Check:
    """Is there anything to reconstruct?"""
    level = Level.PASS if len(features) >= MIN_FRAMES else Level.FAIL
    return Check("frames", level, f"{len(features)} frames kept")


def _front_overlap(clips: Sequence[tuple[ClipScan, Sequence[int]]], overlap: float) -> Check:
    """Did the frames along each line keep the overlap asked for, in the pixels?"""
    measured = [
        value for scan, kept in clips for value in overlaps_between(scan.samples, kept, scan.width, scan.height)
    ]
    if not measured:
        return Check("front overlap", Level.WARN, "no two frames in a row to measure")
    level = Level.PASS if np.percentile(measured, 5) >= overlap - OVERLAP_SLACK else Level.WARN
    message = f"median {np.median(measured):.0%}, lowest {min(measured):.0%}, target {overlap:.0%}, on the pixels"
    return Check("front overlap", level, message)


def _coverage(features: Sequence[Json], camera: Camera) -> Check:
    """Do the frames form one block, or several that sfm cannot tie together?

    Two frames are linked when their footprints share enough of the ground along the short side of the image.
    """
    points = _ground_points(features)
    if len(points) < MIN_FRAMES:
        return Check("coverage", Level.WARN, "too few positions to tell")
    _, along_m = camera.footprint_m(camera.flight_height_m)
    links = _links(points, (1 - MIN_LINK_OVERLAP) * along_m)
    blocks = _blocks(len(points), links)
    biggest = max(np.bincount(blocks))
    count = len(set(blocks.tolist()))
    if count == 1:
        return Check("coverage", Level.PASS, "the frames form one block")
    message = (
        f"{count} separate blocks, the largest with {biggest / len(points):.0%} of the frames: sfm will split here"
    )
    return Check("coverage", Level.WARN, message)


def _side_overlap(features: Sequence[Json], camera: Camera) -> Check:
    """How far across the flight does each frame sit from the next line?

    The next line is the nearest frame level with this one along the track and clearly off to the side of it.
    """
    points = _ground_points(features)
    if len(points) < MIN_FRAMES:
        return Check("side overlap", Level.WARN, "too few positions to tell")
    across_m, along_m = camera.footprint_m(camera.flight_height_m)
    ground = points[:, :2]
    order = np.arange(len(ground))
    track = ground[np.minimum(order + 2, len(ground) - 1)] - ground[np.maximum(order - 2, 0)]
    track /= np.maximum(np.linalg.norm(track, axis=1, keepdims=True), 1e-9)
    offsets = ground[None, :] - ground[:, None]
    along = np.abs(np.sum(offsets * track[:, None], axis=2))
    across = np.abs(offsets[:, :, 0] * track[:, None, 1] - offsets[:, :, 1] * track[:, None, 0])
    across[(along > along_m / 2) | (across < SAME_LINE_M)] = np.inf
    nearest = across.min(axis=1)
    nearest = nearest[np.isfinite(nearest)]
    if not len(nearest):
        return Check("side overlap", Level.PASS, "one line only")
    # Lines converge in the turns at their ends, so the spacing along them is the upper end of these distances.
    side = 1 - float(np.percentile(nearest, 75)) / across_m
    level = Level.PASS if side >= MIN_SIDE_OVERLAP else Level.WARN
    return Check("side overlap", level, f"about {side:.0%} between lines, from telemetry positions")


def _sharpness(clips: Sequence[tuple[ClipScan, Sequence[int]]]) -> Check:
    """Are kept frames softer than the ones around them?"""
    soft = 0
    total = 0
    for scan, kept in clips:
        values = [sample.sharpness for sample in scan.samples]
        for index in kept:
            total += 1
            soft += values[index] < SOFT * float(np.median(values[max(0, index - 8) : index + 9]))
    if not total:
        return Check("sharpness", Level.WARN, "no frames")
    level = Level.PASS if soft / total <= MAX_SOFT_SHARE else Level.WARN
    return Check("sharpness", level, f"{soft} of {total} kept frames softer than their neighbours")


def _motion_blur(clips: Sequence[tuple[ClipScan, Sequence[int]]], camera: Camera) -> Check:
    """How far does the ground move while the shutter is open, at the speed and exposure of each kept frame?"""
    blurs = [
        scan.at("speed_ms", time) / scan.at("shutter", time) * camera.focal_px / scan.at("rel_alt", time)
        for scan, kept in clips
        if all("shutter" in entry.fields for entry in scan.entries)
        for time in (scan.times[scan.samples[index].frame] for index in kept)
    ]
    if not blurs:
        return Check("motion blur", Level.PASS, "the telemetry has no shutter speed")
    level = Level.PASS if np.percentile(blurs, 95) <= MAX_BLUR_PX else Level.WARN
    return Check("motion blur", level, f"{np.median(blurs):.1f} px typical, {max(blurs):.1f} px at most, on the ground")


def _telemetry(clips: Sequence[tuple[ClipScan, Sequence[int]]], features: Sequence[Json]) -> Check:
    """Is the position usable, and did the camera stay the same?"""
    entries = [entry for scan, _ in clips for entry in scan.entries]
    if any(abs(entry.fields.get("zoom", 1.0) - 1) > ZOOM_TOLERANCE for entry in entries):
        return Check("telemetry", Level.WARN, "digital zoom was used: the camera changes with it")
    located = sum(feature["geometry"] is not None for feature in features)
    if located < len(features):
        return Check("telemetry", Level.WARN, f"{len(features) - located} of {len(features)} frames have no position")
    satellites = [entry.fields["satellites"] for entry in entries if "satellites" in entry.fields]
    if satellites and min(satellites) < MIN_SATELLITES:
        return Check("telemetry", Level.WARN, f"as few as {min(satellites):.0f} satellites: positions may be poor")
    return Check("telemetry", Level.PASS, f"{len(entries)} s of telemetry, position on every frame")


def _featureless(clips: Sequence[tuple[ClipScan, Sequence[int]]]) -> Check:
    """How much of the flight showed nothing to map? Said for information."""
    empty = sum(
        sum(sample.texture < MIN_TEXTURE for sample in scan.samples) * scan.sample_interval_s for scan, _ in clips
    )
    total = sum(scan.times[-1] for scan, _ in clips)
    return Check("featureless", Level.PASS, f"{empty:.0f} of {total:.0f} s left out: open water or sky, nothing to map")


def _camera(camera: Camera, given_focal_px: float | None) -> Check:
    """Does the camera passed in agree with the footage?"""
    if given_focal_px is None:
        return Check("camera", Level.PASS, f"measured from the footage: {camera.focal_px:.0f} px, ±{ACCURACY:.0%}")
    if not camera.pairs:
        return Check("camera", Level.WARN, f"{given_focal_px:.0f} px as given; the footage could not confirm it")
    difference = given_focal_px / camera.focal_px - 1
    level = Level.PASS if abs(difference) <= 2 * ACCURACY else Level.WARN
    return Check("camera", level, f"given {given_focal_px:.0f} px is {difference:+.0%} from {camera.focal_px:.0f} px")


def _ground_points(features: Sequence[Json]) -> np.ndarray:
    """East and north in metres from the first frame, for the features with a position."""
    rows: list[tuple[float, float]] = []
    for feature in features:
        geometry = feature["geometry"]
        if isinstance(geometry, dict):
            coordinates = geometry["coordinates"]
            assert isinstance(coordinates, list)
            rows.append((coordinates[0], coordinates[1]))
    if not rows:
        return np.empty((0, 2))
    points = np.array(rows)
    scale = math.radians(1) * EARTH_RADIUS_M
    east = (points[:, 0] - points[0, 0]) * scale * math.cos(math.radians(points[0, 1]))
    north = (points[:, 1] - points[0, 1]) * scale
    return np.column_stack([east, north])


def _links(points: np.ndarray, reach_m: float) -> list[tuple[int, int]]:
    """The pairs of points within ``reach_m``."""
    near = np.hypot(*(points[:, None] - points[None, :]).transpose(2, 0, 1)) <= reach_m
    first, second = np.nonzero(np.triu(near, 1))
    return list(zip(first.tolist(), second.tolist(), strict=True))


def _blocks(count: int, links: Sequence[tuple[int, int]]) -> np.ndarray:
    """The block of each of ``count`` items, given the ``links`` between them."""
    parents = list(range(count))

    def root(item: int) -> int:
        while parents[item] != item:
            parents[item] = parents[parents[item]]
            item = parents[item]
        return item

    for first, second in links:
        parents[root(first)] = root(second)
    return np.array([root(item) for item in range(count)])
