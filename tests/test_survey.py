import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from photogram_data_prep import PrepError, prepare_survey, prepare_video
from photogram_data_prep.survey import _choose_frames, _integrate, _smooth
from PIL import Image

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs FFmpeg")

FPS = 10
# As the Mini 4K writes it: lon and lat rounded to 4 decimals, then the satellite count.
LINE = (
    "F/2.8, SS 175.23, ISO 100, EV 0, DZOOM 1.000, GPS ({lon}, {lat}, 23), D 35.36m, H {height:.2f}m, "
    "H.S {speed:.2f}m/s, V.S -0.00m/s "
)
# Short image side over focal length on a 16:9 frame: the footprint is 0.817 of the height.
CAMERA: dict[str, Any] = {"focal_length_mm": 4.25, "sensor_width_mm": 6.17}


def make_clip(
    path: Path,
    speeds: list[float],
    *,
    height: float = 10.0,
    size: str = "64x36",
    lon: float = 8.8354,
    lat: float = 53.1084,
    created: str = "2026-10-30T13:20:06.000000Z",
) -> Path:
    """A clip with one telemetry entry per second in a subtitle stream, each frame as bright as 8 x its number."""
    path.parent.mkdir(exist_ok=True)
    blocks = [
        f"{n + 1}\n00:00:{n:02d},000 --> 00:00:{n + 1:02d},000\n"
        + LINE.format(lon=lon, lat=lat, height=height, speed=speed)
        + "\n"
        for n, speed in enumerate(speeds)
    ]
    subtitles = path.with_name(f"{path.stem}.subtitles")
    subtitles.write_text("\n".join(blocks))
    frames = len(speeds) * FPS
    source = f"color=c=black:s={size}:r={FPS},format=gray,geq=lum='min(N*8,255)',format=yuv420p"
    command = ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", source, "-f", "srt", "-i", str(subtitles)]
    command += ["-frames:v", str(frames), "-map", "0:v", "-map", "1:s", "-c:s", "mov_text", "-crf", "1"]
    command += ["-metadata", f"creation_time={created}", "-pix_fmt", "yuv420p", str(path)]
    subprocess.run(command, check=True)
    subtitles.unlink()
    return path


def load(dataset: Path) -> dict:
    return json.loads((dataset / "manifest.geojson").read_text())


def test_integrates_speed_between_moments() -> None:
    assert _integrate([0.0, 1.0, 3.0], [0.0, 2.0, 2.0]) == [0.0, 1.0, 5.0]


def test_chooses_a_frame_each_time_the_overlap_is_used_up() -> None:
    flown = [float(frame) for frame in range(11)]

    # A 10 m footprint at 0.75 overlap: a frame every 2.5 m, and the last one for the end of the line.
    assert _choose_frames(flown, [10.0] * 11, 0.75) == [0, 3, 6, 9, 10]


def test_a_hover_gives_one_frame() -> None:
    assert _choose_frames([0.0] * 50, [10.0] * 50, 0.8) == [0]


def test_a_taller_flight_keeps_fewer_frames() -> None:
    flown = [float(frame) for frame in range(101)]

    low, high = _choose_frames(flown, [10.0] * 101, 0.8), _choose_frames(flown, [40.0] * 101, 0.8)

    assert len(high) < len(low)


def test_smoothing_recovers_a_rounded_line_and_stays_centred_at_the_ends() -> None:
    rounded = [float(round(value * 0.3)) for value in range(40)]

    smoothed = _smooth(rounded, 5)

    assert smoothed[0] == rounded[0]
    assert smoothed[-1] == rounded[-1]
    assert all(abs(value - i * 0.3) < 0.35 for i, value in enumerate(smoothed) if 5 <= i < 35)


@needs_ffmpeg
def test_a_directory_of_clips_becomes_an_image_dataset(tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    make_clip(raw / "DJI_0002.MP4", [2.0] * 5, created="2026-10-30T13:30:00.000000Z")
    make_clip(raw / "DJI_0001.MP4", [2.0] * 5)
    (raw / "notes.txt").write_text("not a video")
    dataset = tmp_path / "dataset"

    result = prepare_survey([raw], dataset, overlap=0.5, **CAMERA)

    manifest = load(dataset)
    block = manifest["photogrammetry"]
    assert block["source"] == "images"
    assert block["altitude_reference"] == "relative_takeoff"
    assert block["cameras"]["dji_video_64x36_4.25mm"]["sensor_width_mm"] == pytest.approx(6.17)
    assert block["frame_selection"]["overlap"] == pytest.approx(0.5)

    # 8.17 m footprint at 0.5 overlap: a frame every 4.08 m, or 21 frames at 2 m/s and 10 fps, then the last frame.
    assert [clip.kept for clip in result.clips] == [4, 4]
    assert [f["properties"]["video_frame"] for f in manifest["features"][:4]] == [0, 21, 42, 49]
    assert result.frames == result.located == 8
    assert [clip.name for clip in result.clips] == ["DJI_0001.MP4", "DJI_0002.MP4"]

    names = [f["properties"]["image"] for f in manifest["features"]]
    assert names == sorted(path.name for path in (dataset / "images").iterdir())
    assert not list(dataset.glob("video.*"))
    assert result.image_bytes == sum(path.stat().st_size for path in (dataset / "images").iterdir())


@needs_ffmpeg
def test_the_image_is_the_frame_the_manifest_names(tmp_path: Path) -> None:
    prepare_survey(
        [make_clip(tmp_path / "raw" / "DJI_0001.MP4", [2.0] * 3)], tmp_path / "dataset", overlap=0.5, **CAMERA
    )

    for feature in load(tmp_path / "dataset")["features"]:
        with Image.open(tmp_path / "dataset" / "images" / feature["properties"]["image"]) as image:
            brightness = float(image.convert("L").resize((1, 1), Image.Resampling.BOX).tobytes()[0])
        assert brightness / 8 == pytest.approx(feature["properties"]["video_frame"], abs=0.5)


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


@needs_ffmpeg
def test_video_reads_the_embedded_stream_too(tmp_path: Path) -> None:
    clip = make_clip(tmp_path / "raw" / "DJI_0001.MP4", [2.0] * 3)

    prepare_video(clip, tmp_path / "dataset", **CAMERA)

    assert load(tmp_path / "dataset")["features"][0]["geometry"]["coordinates"] == [8.8354, 53.1084, 10.0]


@needs_ffmpeg
def test_a_clip_of_another_size_is_refused_before_writing(tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    make_clip(raw / "DJI_0001.MP4", [2.0] * 2)
    make_clip(raw / "DJI_0002.MP4", [2.0] * 2, size="32x18")

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


@pytest.mark.parametrize("overlap", [0.0, 1.0, -0.2])
def test_overlap_must_be_a_fraction(tmp_path: Path, overlap: float) -> None:
    with pytest.raises(PrepError, match="overlap"):
        prepare_survey([tmp_path], tmp_path / "dataset", overlap=overlap, **CAMERA)


def test_no_videos(tmp_path: Path) -> None:
    with pytest.raises(PrepError, match="no videos"):
        prepare_survey([tmp_path], tmp_path / "dataset", **CAMERA)
