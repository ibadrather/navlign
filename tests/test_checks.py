"""Each check has to be able to fail: a check that always passes says nothing."""

from pathlib import Path

import numpy as np

from navlign import Camera, Check, Level
from navlign.checks import run_checks, verdict
from navlign.images import Json
from navlign.scan import ClipScan, Sample
from navlign.video import Entry

WIDTH, HEIGHT = 2720, 1530
METRE_LAT = 1 / 111_195  # degrees of latitude in a metre, on the sphere the checks use
CAMERA = Camera(WIDTH, HEIGHT, 2300.0, 50.0, 500, 1 / 100)


def shift(pixels: float) -> np.ndarray:
    matrix = np.eye(3)
    matrix[1, 2] = pixels
    return matrix


def feature(north_m: float, east_m: float = 0.0) -> Json:
    """A frame at ``north_m`` and ``east_m`` from the first."""
    east_degrees = east_m * METRE_LAT / np.cos(np.radians(53.1))
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [8.8 + east_degrees, 53.1 + north_m * METRE_LAT, 50.0]},
        "properties": {"image": "x.jpg"},
    }


def line(step_m: float = 5.0, frames: int = 20) -> list[Json]:
    return [feature(step_m * index) for index in range(frames)]


def clip(
    *, speed: float = 2.0, shutter: float = 200.0, zoom: float = 1.0, satellites: float = 20.0, step_px: float = 40.0
) -> ClipScan:
    """A minute of steady flight sampled at 5 Hz."""
    fields = {
        "speed_ms": speed,
        "rel_alt": 50.0,
        "shutter": shutter,
        "zoom": zoom,
        "satellites": satellites,
        "latitude": 53.1,
        "longitude": 8.8,
    }
    entries = [Entry(float(n), n + 1.0, fields, None, 0) for n in range(60)]
    samples = [Sample(index * 6, 1.0, 100.0, shift(step_px) if index else None, step_px) for index in range(300)]
    return ClipScan(Path("DJI_0001.MP4"), WIDTH, HEIGHT, [n / 30 for n in range(1800)], None, entries, samples)


def run(
    scan: ClipScan | None = None,
    features: list[Json] | None = None,
    kept: list[int] | None = None,
    given: float | None = None,
) -> dict[str, Check]:
    scan = scan or clip()
    kept = list(range(0, 300, 6)) if kept is None else kept
    return {
        check.name: check
        for check in run_checks([(scan, kept)], line() if features is None else features, CAMERA, 0.85, given)
    }


def test_good_data_passes_every_check() -> None:
    assert {check.name: check.level for check in run().values() if check.level is not Level.PASS} == {}


def test_nothing_to_reconstruct_fails() -> None:
    checks = run(features=[], kept=[])

    assert checks["frames"].level is Level.FAIL
    assert verdict(list(checks.values())) is Level.FAIL


def test_too_little_overlap_warns() -> None:
    assert run(scan=clip(step_px=100.0))["front overlap"].level is Level.WARN


def test_two_blocks_warn_because_sfm_will_split_there() -> None:
    apart = line() + [feature(1000.0 + 5.0 * index) for index in range(20)]

    checks = run(features=apart)

    assert checks["coverage"].level is Level.WARN
    assert "2 separate blocks" in checks["coverage"].message


def test_lines_are_measured_across_the_track() -> None:
    # The footprint is 59 m across: lines 20 m apart overlap by 66 %, lines 40 m apart by 32 %.
    near = run(features=line() + [feature(5.0 * index, 20.0) for index in range(20)])
    far = run(features=line() + [feature(5.0 * index, 40.0) for index in range(20)])

    assert near["side overlap"].level is Level.PASS
    assert "66%" in near["side overlap"].message
    assert far["side overlap"].level is Level.WARN


def test_a_slow_line_is_not_its_own_neighbour() -> None:
    assert run(features=line(step_m=2.0, frames=100))["side overlap"].message == "one line only"


def test_lines_meeting_in_a_turn_do_not_narrow_the_spacing() -> None:
    # 25 of 30 frames on lines 20 m apart, the last 5 closing in to 10 m.
    closing = [feature(5.0 * index, 20.0 if index < 25 else 10.0) for index in range(30)]

    assert "66%" in run(features=line(frames=30) + closing)["side overlap"].message


def test_soft_frames_warn() -> None:
    scan = clip()
    samples = [
        Sample(s.frame, s.texture, 10.0 if index % 12 == 0 else s.sharpness, s.motion, s.surface_px)
        for index, s in enumerate(scan.samples)
    ]
    soft = ClipScan(scan.video, WIDTH, HEIGHT, scan.times, None, scan.entries, samples)

    assert run(scan=soft, kept=list(range(0, 300, 12)))["sharpness"].level is Level.WARN


def test_a_long_exposure_at_speed_warns() -> None:
    assert run(scan=clip(speed=6.0, shutter=60.0))["motion blur"].level is Level.WARN


def test_digital_zoom_warns() -> None:
    assert run(scan=clip(zoom=1.5))["telemetry"].level is Level.WARN


def test_few_satellites_warn() -> None:
    assert run(scan=clip(satellites=6.0))["telemetry"].level is Level.WARN


def test_a_camera_off_like_the_spec_sheet_one_warns() -> None:
    checks = run(given=2300.0 * 0.83)

    assert checks["camera"].level is Level.WARN
    assert "-17%" in checks["camera"].message


def test_a_camera_close_to_the_footage_passes() -> None:
    assert run(given=2300.0 * 1.05)["camera"].level is Level.PASS
