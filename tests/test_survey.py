import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from numpy.typing import NDArray
from photogram_data_prep import Level, PrepError, check_survey, prepare_survey, prepare_video, scan_clip
from photogram_data_prep.camera import MIN_PAIRS, measure_camera
from photogram_data_prep.scan import MIN_TEXTURE, Sample, overlap_of, overlaps_between
from photogram_data_prep.survey import _integrate, _smooth, choose_frames
from PIL import Image

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs FFmpeg")

FPS = 10
WIDTH, HEIGHT = 320, 180
HEIGHT_M = 10.0
PX_PER_M = 30  # 2 m/s at 10 fps is 6 whole pixels a frame, so the clip needs no resampling
FOCAL_PX = PX_PER_M * HEIGHT_M
# As the Mini 4K writes it: lon and lat rounded to 4 decimals, then the satellite count.
LINE = (
    "F/2.8, SS 175.23, ISO 100, EV 0, DZOOM {zoom:.3f}, GPS ({lon}, {lat}, 23), D 35.36m, H {height:.2f}m, "
    "H.S {speed:.2f}m/s, V.S -0.00m/s "
)
CAMERA: dict[str, Any] = {"focal_length_mm": 5.785, "sensor_width_mm": 6.17}  # FOCAL_PX over 320 px


def ground(width: int, *, seed: int = 1) -> NDArray[np.uint8]:
    """Gray ground with detail at many scales, ``width`` pixels long."""
    random = np.random.default_rng(seed)
    image = np.zeros((HEIGHT, width))
    for cell in (4, 8, 16):
        coarse = random.random((HEIGHT // cell + 2, width // cell + 2))
        repeated = np.kron(coarse, np.ones((cell, cell)))
        image += repeated[:HEIGHT, :width] * 80
    return np.clip(image, 0, 255).astype(np.uint8)


def make_clip(
    path: Path,
    speeds: list[float],
    *,
    height: float = HEIGHT_M,
    flat: bool = False,
    size: tuple[int, int] = (WIDTH, HEIGHT),
    lon: float = 8.8354,
    lat: float = 53.1084,
    zoom: float = 1.0,
    created: str = "2026-10-30T13:20:06.000000Z",
) -> Path:
    """A clip flown along the ground at ``speeds``, one a second, with a telemetry entry per second.

    The top left corner of each frame shows the number of the frame as a brightness of 4 x its number. flat is open
    water: no texture at all.
    """
    path.parent.mkdir(exist_ok=True)
    blocks = [
        f"{n + 1}\n00:00:{n:02d},000 --> 00:00:{n + 1:02d},000\n"
        + LINE.format(lon=lon, lat=lat, height=height, speed=speed, zoom=zoom)
        + "\n"
        for n, speed in enumerate(speeds)
    ]
    subtitles = path.with_name(f"{path.stem}.subtitles")
    subtitles.write_text("\n".join(blocks))
    shifts = np.cumsum([0.0, *(speed / FPS * PX_PER_M for speed in speeds for _ in range(FPS))]).round().astype(int)
    width = size[0]
    landscape = np.full((HEIGHT, int(shifts[-1]) + width + 1), 90, np.uint8) if flat else ground(shifts[-1] + width + 1)
    frames = bytearray()
    for number in range(len(speeds) * FPS):
        frame = landscape[: size[1], shifts[number] : shifts[number] + width].copy()
        frame[:8, :8] = min(number * 4, 255)
        frames += frame.tobytes()
    command = ["ffmpeg", "-v", "error", "-f", "rawvideo", "-pix_fmt", "gray", "-s", f"{width}x{size[1]}"]
    command += ["-r", str(FPS), "-i", "-", "-f", "srt", "-i", str(subtitles), "-map", "0:v", "-map", "1:s"]
    command += ["-c:s", "mov_text", "-crf", "1", "-g", "10", "-metadata", f"creation_time={created}"]
    command += ["-pix_fmt", "yuv420p", str(path)]
    subprocess.run(command, input=bytes(frames), check=True)
    subtitles.unlink()
    return path


def shift(x: float, y: float = 0.0) -> NDArray[np.float64]:
    """The motion of a pure translation."""
    return np.array([[1.0, 0.0, x], [0.0, 1.0, y], [0.0, 0.0, 1.0]])


def turn(degrees: float, width: int, height: int) -> NDArray[np.float64]:
    """The motion of a rotation about the centre of an image."""
    cos, sin = np.cos(np.radians(degrees)), np.sin(np.radians(degrees))
    centre = np.array([width / 2, height / 2])
    matrix = np.eye(3)
    matrix[:2, :2] = [[cos, -sin], [sin, cos]]
    matrix[:2, 2] = centre - matrix[:2, :2] @ centre
    return matrix


def samples_of(steps: list[float | None], *, texture: float = 1.0) -> list[Sample]:
    """Samples moving along x by each of ``steps`` pixels; None could not be measured."""
    return [
        Sample(index, texture, 100.0, shift(step) if step is not None else None, step)
        for index, step in enumerate([None, *steps])
    ]


def load(dataset: Path) -> dict:
    return json.loads((dataset / "manifest.geojson").read_text())


def test_integrates_speed_between_moments() -> None:
    assert _integrate([0.0, 1.0, 3.0], [0.0, 2.0, 2.0]) == [0.0, 1.0, 5.0]


def test_overlap_of_a_shift_along_each_side() -> None:
    assert overlap_of(shift(0.0, 50.0), 200, 100) == pytest.approx(0.5, abs=0.02)
    assert overlap_of(shift(50.0), 200, 100) == pytest.approx(0.75, abs=0.02)


def test_turning_on_the_spot_costs_overlap_without_moving() -> None:
    assert overlap_of(turn(45, 200, 100), 200, 100) < 0.75


def test_chooses_a_sample_each_time_the_overlap_is_used_up() -> None:
    # A 100 px wide image at 0.75: a frame every 25 px at most, so every second step of 10, then the last sample.
    samples = samples_of([10.0] * 10)

    assert choose_frames(samples, 0.75, 100, 100) == [0, 2, 4, 6, 8, 10]


def test_a_hover_gives_one_frame() -> None:
    assert choose_frames(samples_of([0.0] * 50), 0.8, 100, 100) == [0]


def test_jitter_that_goes_nowhere_adds_no_frames() -> None:
    assert choose_frames(samples_of([3.0, -3.0] * 30), 0.8, 100, 100) == [0]


def test_a_big_step_is_kept_even_with_no_overlap_left_to_spend() -> None:
    assert choose_frames(samples_of([30.0, 30.0]), 0.9, 100, 100) == [0, 1, 2]


def test_a_soft_frame_gives_way_to_an_earlier_one() -> None:
    samples = samples_of([10.0] * 4)
    soft = samples[2]
    samples[2] = Sample(soft.frame, soft.texture, 50.0, soft.motion, soft.surface_px)

    # 25 px runs out at sample 3; sample 2 was the one to keep, but it is soft.
    assert choose_frames(samples, 0.75, 100, 100)[1] == 1


def test_no_texture_leaves_no_frames_and_starts_again_after() -> None:
    samples = samples_of([10.0] * 8)
    water = range(3, 6)
    samples = [
        Sample(s.frame, MIN_TEXTURE / 2 if s.frame in water else s.texture, s.sharpness, s.motion, s.surface_px)
        for s in samples
    ]

    kept = choose_frames(samples, 0.75, 100, 100)

    assert not set(kept) & set(water)
    assert 6 in kept


def test_lost_tracking_starts_a_new_stretch() -> None:
    kept = choose_frames(samples_of([1.0, 1.0, None, 1.0, 1.0]), 0.5, 100, 100)

    assert kept == [0, 3]


def test_overlap_is_not_measured_across_a_gap() -> None:
    samples = samples_of([10.0, None, 10.0])

    assert overlaps_between(samples, [0, 1, 3], 100, 100) == [pytest.approx(0.9, abs=0.02)]


def test_smoothing_recovers_a_rounded_line_and_stays_centred_at_the_ends() -> None:
    rounded = [float(round(value * 0.3)) for value in range(40)]

    smoothed = _smooth(rounded, 5)

    assert smoothed[0] == rounded[0]
    assert smoothed[-1] == rounded[-1]
    assert all(abs(value - i * 0.3) < 0.35 for i, value in enumerate(smoothed) if 5 <= i < 35)


@needs_ffmpeg
def test_scan_measures_the_motion_of_the_ground(tmp_path: Path) -> None:
    scan = scan_clip(make_clip(tmp_path / "raw" / "DJI_0001.MP4", [2.0] * 3))

    # 10 fps sampled at 5 Hz is every second frame, starting on an odd one: never the keyframes every 10 frames.
    assert [sample.frame for sample in scan.samples[:3]] == [1, 3, 5]
    moved = [sample.motion[0, 2] for sample in scan.samples[1:] if sample.motion is not None]
    assert np.median(moved) == pytest.approx(-12, abs=0.3)


@needs_ffmpeg
def test_measures_the_focal_length_from_speed_height_and_motion(tmp_path: Path) -> None:
    camera = measure_camera([scan_clip(make_clip(tmp_path / "raw" / "DJI_0001.MP4", [2.0] * 8))])

    assert camera.focal_px == pytest.approx(FOCAL_PX, rel=0.03)
    assert camera.flight_height_m == pytest.approx(HEIGHT_M)
    assert camera.footprint_m(HEIGHT_M)[1] == pytest.approx(HEIGHT_M * HEIGHT / FOCAL_PX, rel=0.03)
    # 1 px is 3.3 cm on the ground and the shutter is 1/175 s.
    assert camera.speed_limit_ms(HEIGHT_M) == pytest.approx(HEIGHT_M / FOCAL_PX * 175.23, rel=0.03)


@needs_ffmpeg
def test_the_acquisition_profile_gives_the_footprint_on_the_ground(tmp_path: Path) -> None:
    camera = measure_camera([scan_clip(make_clip(tmp_path / "raw" / "DJI_0001.MP4", [2.0] * 8))])
    profile: Any = camera.acquisition_profile()

    assert (profile["image_w_px"], profile["image_h_px"]) == (WIDTH, HEIGHT)
    across_m, along_m = camera.footprint_m(50.0)
    assert 50.0 * profile["sensor_w_mm"] / profile["focal_mm"] == pytest.approx(across_m, rel=0.01)
    assert 50.0 * profile["sensor_h_mm"] / profile["focal_mm"] == pytest.approx(along_m, rel=0.01)


@needs_ffmpeg
def test_a_hover_cannot_measure_a_camera(tmp_path: Path) -> None:
    scan = scan_clip(make_clip(tmp_path / "raw" / "DJI_0001.MP4", [0.0] * 3))

    with pytest.raises(PrepError, match="steady flight"):
        measure_camera([scan])
    assert MIN_PAIRS > 0


@needs_ffmpeg
def test_a_directory_of_clips_becomes_an_image_dataset(tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    make_clip(raw / "DJI_0002.MP4", [2.0] * 6, created="2026-10-30T13:30:00.000000Z")
    make_clip(raw / "DJI_0001.MP4", [2.0] * 6)
    (raw / "notes.txt").write_text("not a video")
    dataset = tmp_path / "dataset"

    result = prepare_survey([raw], dataset, overlap=0.5)

    manifest = load(dataset)
    block = manifest["photogrammetry"]
    assert block["source"] == "images"
    assert block["altitude_reference"] == "relative_takeoff"
    assert block["frame_selection"] == {"method": "overlap", "overlap": 0.5, "measure": "image motion"}
    camera = next(iter(block["cameras"].values()))
    assert camera["focal_length_mm"] / camera["sensor_width_mm"] * WIDTH == pytest.approx(FOCAL_PX, rel=0.03)

    # 160 px of a 320 px wide image at 12 px a sample: every 13 samples, or 26 frames, then the end of the line.
    frames = [
        f["properties"]["video_frame"] for f in manifest["features"] if f["properties"]["video"] == "DJI_0001.MP4"
    ]
    assert frames[:3] == [1, 27, 53]
    assert result.frames == result.located == len(manifest["features"])
    assert [clip.name for clip in result.clips] == ["DJI_0001.MP4", "DJI_0002.MP4"]

    names = [f["properties"]["image"] for f in manifest["features"]]
    assert names == sorted(path.name for path in (dataset / "images").iterdir())
    assert not list(dataset.glob("video.*"))
    assert result.image_bytes == sum(path.stat().st_size for path in (dataset / "images").iterdir())


@needs_ffmpeg
def test_writes_a_report_with_the_checks_and_the_camera(tmp_path: Path) -> None:
    prepare_survey([make_clip(tmp_path / "raw" / "DJI_0001.MP4", [2.0] * 6)], tmp_path / "dataset", overlap=0.5)

    report = json.loads((tmp_path / "dataset" / "prep_report.json").read_text())

    assert report["verdict"] in {"pass", "warn"}
    assert {check["name"] for check in report["checks"]} >= {"front overlap", "coverage", "camera"}
    assert report["camera"]["acquisition_profile"]["image_w_px"] == WIDTH


@needs_ffmpeg
def test_check_writes_nothing_and_agrees_with_the_survey(tmp_path: Path) -> None:
    clip = make_clip(tmp_path / "raw" / "DJI_0001.MP4", [2.0] * 6)

    checked = check_survey([clip], overlap=0.5)
    written = prepare_survey([clip], tmp_path / "dataset", overlap=0.5)

    assert list((tmp_path / "raw").iterdir()) == [clip]
    assert checked.frames == written.frames
    assert [check.message for check in checked.checks] == [check.message for check in written.checks]


@needs_ffmpeg
def test_open_water_keeps_no_frames_and_is_no_fault(tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    make_clip(raw / "DJI_0001.MP4", [2.0] * 6)
    make_clip(raw / "DJI_0002.MP4", [2.0] * 3, flat=True)

    result = prepare_survey([raw], tmp_path / "dataset", overlap=0.5)

    assert [clip.kept > 0 for clip in result.clips] == [True, False]
    assert result.clips[1].featureless_s > 2
    assert next(check for check in result.checks if check.name == "featureless").level is Level.PASS


@needs_ffmpeg
def test_all_water_is_refused_and_leaves_an_existing_dataset_alone(tmp_path: Path) -> None:
    dataset = tmp_path / "dataset"
    prepare_survey([make_clip(tmp_path / "raw" / "DJI_0001.MP4", [2.0] * 3)], dataset, overlap=0.5, **CAMERA)
    before = sorted(path.name for path in (dataset / "images").iterdir())
    water = make_clip(tmp_path / "raw" / "DJI_0002.MP4", [2.0] * 3, flat=True)

    with pytest.raises(PrepError, match="no frame has texture"):
        prepare_survey([water], dataset, overwrite=True, **CAMERA)

    assert sorted(path.name for path in (dataset / "images").iterdir()) == before
    assert check_survey([water], **CAMERA).verdict is Level.FAIL


@needs_ffmpeg
def test_the_image_is_the_frame_the_manifest_names(tmp_path: Path) -> None:
    prepare_survey(
        [make_clip(tmp_path / "raw" / "DJI_0001.MP4", [2.0] * 3)], tmp_path / "dataset", overlap=0.5, **CAMERA
    )

    for feature in load(tmp_path / "dataset")["features"]:
        with Image.open(tmp_path / "dataset" / "images" / feature["properties"]["image"]) as image:
            brightness = float(np.mean(np.asarray(image.convert("L"))[2:6, 2:6]))
        assert brightness / 4 == pytest.approx(feature["properties"]["video_frame"], abs=1)


@needs_ffmpeg
def test_position_height_and_time_of_a_frame(tmp_path: Path) -> None:
    clip = make_clip(tmp_path / "raw" / "DJI_0001.MP4", [2.0] * 3, height=50.2)

    prepare_survey([clip], tmp_path / "dataset", overlap=0.9, **CAMERA)

    feature = load(tmp_path / "dataset")["features"][1]
    assert feature["geometry"]["coordinates"] == [8.8354, 53.1084, 50.2]
    assert feature["properties"]["video"] == "DJI_0001.MP4"
    frame = feature["properties"]["video_frame"]
    assert feature["properties"]["time_utc"] == f"2026-10-30T13:20:0{6 + frame // FPS}.{frame % FPS}00Z"


@needs_ffmpeg
def test_no_fix_leaves_the_frame_without_a_position(tmp_path: Path) -> None:
    clip = make_clip(tmp_path / "raw" / "DJI_0001.MP4", [2.0] * 3, lon=0.0, lat=0.0)

    result = prepare_survey([clip], tmp_path / "dataset", **CAMERA)

    assert result.located == 0
    assert result.frames > 0
    assert next(check for check in result.checks if check.name == "telemetry").level is Level.WARN


@needs_ffmpeg
def test_video_reads_the_embedded_stream_too(tmp_path: Path) -> None:
    clip = make_clip(tmp_path / "raw" / "DJI_0001.MP4", [2.0] * 3)

    prepare_video(clip, tmp_path / "dataset", **CAMERA)

    assert load(tmp_path / "dataset")["features"][0]["geometry"]["coordinates"] == [8.8354, 53.1084, 10.0]


@needs_ffmpeg
def test_a_clip_of_another_size_is_refused_before_writing(tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    make_clip(raw / "DJI_0001.MP4", [2.0] * 2)
    make_clip(raw / "DJI_0002.MP4", [2.0] * 2, size=(160, 90))

    with pytest.raises(PrepError, match="frame size"):
        prepare_survey([raw], tmp_path / "dataset", **CAMERA)
    assert not (tmp_path / "dataset").exists()


@needs_ffmpeg
def test_two_clips_of_the_same_name_are_refused(tmp_path: Path) -> None:
    first = make_clip(tmp_path / "one" / "DJI_0001.MP4", [2.0] * 2)
    second = make_clip(tmp_path / "two" / "DJI_0001.MP4", [2.0] * 2)

    with pytest.raises(PrepError, match="same name"):
        prepare_survey([first, second], tmp_path / "dataset", **CAMERA)


@needs_ffmpeg
def test_telemetry_without_speed_is_refused(tmp_path: Path) -> None:
    clip = make_clip(tmp_path / "raw" / "DJI_0001.MP4", [2.0] * 2)
    clip.with_suffix(".SRT").write_text("1\n00:00:00,000 --> 00:00:01,000\n[latitude: 45.5] [longitude: 9.0]\n")

    with pytest.raises(PrepError, match=r"no H\.S speed"):
        prepare_survey([clip], tmp_path / "dataset", **CAMERA)


@needs_ffmpeg
def test_replaces_a_dataset_only_when_asked(tmp_path: Path) -> None:
    clip = make_clip(tmp_path / "raw" / "DJI_0001.MP4", [2.0] * 2)
    dataset = tmp_path / "dataset"
    prepare_video(clip, dataset, **CAMERA)

    with pytest.raises(PrepError, match="--overwrite"):
        prepare_survey([clip], dataset, **CAMERA)

    prepare_survey([clip], dataset, overwrite=True, **CAMERA)
    assert not (dataset / "video.mp4").exists()  # the pipeline wants images/ or a video, not both


def test_a_camera_is_given_in_full_or_not_at_all(tmp_path: Path) -> None:
    with pytest.raises(PrepError, match="together"):
        prepare_survey([tmp_path], tmp_path / "dataset", focal_length_mm=4.25)


@pytest.mark.parametrize("overlap", [0.0, 1.0, -0.2])
def test_overlap_must_be_a_fraction(tmp_path: Path, overlap: float) -> None:
    with pytest.raises(PrepError, match="overlap"):
        prepare_survey([tmp_path], tmp_path / "dataset", overlap=overlap, **CAMERA)


def test_no_videos(tmp_path: Path) -> None:
    with pytest.raises(PrepError, match="no videos"):
        prepare_survey([tmp_path], tmp_path / "dataset", **CAMERA)
