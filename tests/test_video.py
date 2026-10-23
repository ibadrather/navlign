import json
import shutil
import subprocess
from pathlib import Path

import pytest
from photogram_data_prep import AltitudeMode, PrepError, VerticalDatum, prepare_video

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs FFmpeg")

# Mini 2 style: one entry per frame, position and relative altitude in [...] groups.
ENTRY = (
    "{n}\n00:00:00,{start:03d} --> 00:00:00,{end:03d}\n"
    '<font size="36">SrtCnt : {n}, DiffTime : 100ms\n2023-01-11 17:01:55,{start:03d},000\n'
    "[iso : 100] [focal_len : 240] [latitude: {lat}] [longitude: {lon}] [altitude: 29.600000] {extra}</font>\n"
)


def write_srt(path: Path, entries: int, *, lat: float = 45.499044, lon: float = 9.040211, extra: str = "") -> Path:
    blocks = [
        ENTRY.format(n=n + 1, start=n * 100, end=n * 100 + 100, lat=lat, lon=lon, extra=extra) for n in range(entries)
    ]
    path.write_text("\n".join(blocks))
    return path


@pytest.fixture
def video(tmp_path: Path) -> Path:
    """A 64x36 video of 5 frames at 10 fps."""
    path = tmp_path / "raw" / "DJI_0001.MP4"
    path.parent.mkdir()
    command = ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=gray:s=64x36:r=10", "-frames:v", "5"]
    subprocess.run([*command, "-pix_fmt", "yuv420p", str(path)], check=True)
    return path


def load(dataset: Path) -> dict:
    return json.loads((dataset / "manifest.geojson").read_text())


@needs_ffmpeg
def test_writes_manifest_and_copies_video(tmp_path: Path, video: Path) -> None:
    write_srt(video.with_suffix(".SRT"), 4)
    dataset = tmp_path / "dataset"

    result = prepare_video(video, dataset, timezone="Europe/Rome")

    manifest = load(dataset)
    block = manifest["photogrammetry"]
    assert block["source"] == "video.mp4"
    assert block["altitude_reference"] == "relative_takeoff"
    # 35 mm equivalent: 16:9 frame with a 43.27 mm diagonal.
    assert block["cameras"] == {
        "dji_video_64x36_24mm": {
            "width": 64,
            "height": 36,
            "focal_length_mm": 24.0,
            "sensor_width_mm": 37.71,
            "shutter": "rolling",
        },
    }
    features = manifest["features"]
    assert [f["properties"]["frame_index"] for f in features] == [0, 1, 2, 3, 4]
    assert features[1]["geometry"]["coordinates"] == [9.040211, 45.499044, 29.6]
    assert features[1]["properties"]["time_utc"] == "2023-01-11T16:01:55.100Z"
    assert features[4]["geometry"] is None  # past the last SRT entry
    assert (result.frames, result.located) == (5, 4)
    assert (dataset / "video.mp4").read_bytes() == video.read_bytes()


@needs_ffmpeg
def test_absolute_altitude_and_gimbal(tmp_path: Path, video: Path) -> None:
    srt = write_srt(
        tmp_path / "telemetry.srt",
        5,
        extra="[rel_alt: 1.300 abs_alt: 132.778] [gb_yaw: -12.5 gb_pitch: -90.0 gb_roll: 0.0]",
    )

    prepare_video(
        video, tmp_path / "dataset", srt=srt, altitude=AltitudeMode.ABSOLUTE, vertical_datum=VerticalDatum.ELLIPSOID
    )

    manifest = load(tmp_path / "dataset")
    assert manifest["photogrammetry"]["altitude_reference"] == "ellipsoid"
    first = manifest["features"][0]
    assert first["geometry"]["coordinates"][2] == pytest.approx(132.778)
    assert (first["properties"]["yaw"], first["properties"]["pitch"]) == (-12.5, -90.0)


@needs_ffmpeg
def test_camera_override(tmp_path: Path, video: Path) -> None:
    write_srt(video.with_suffix(".SRT"), 5)

    prepare_video(video, tmp_path / "dataset", focal_length_mm=4.49, sensor_width_mm=6.17)

    cameras = load(tmp_path / "dataset")["photogrammetry"]["cameras"]
    assert cameras["dji_video_64x36_4.49mm"]["sensor_width_mm"] == pytest.approx(6.17)


@needs_ffmpeg
def test_zero_zero_is_no_fix(tmp_path: Path, video: Path) -> None:
    write_srt(video.with_suffix(".SRT"), 5, lat=0.0, lon=0.0)

    result = prepare_video(video, tmp_path / "dataset")

    assert result.located == 0


@needs_ffmpeg
def test_absolute_altitude_missing_fails_before_writing(tmp_path: Path, video: Path) -> None:
    write_srt(video.with_suffix(".SRT"), 5)
    dataset = tmp_path / "dataset"

    with pytest.raises(PrepError, match="absolute altitude"):
        prepare_video(video, dataset, altitude=AltitudeMode.ABSOLUTE, vertical_datum=VerticalDatum.EGM96)
    assert not dataset.exists()


@needs_ffmpeg
def test_refuses_to_overwrite_unless_asked(tmp_path: Path, video: Path) -> None:
    write_srt(video.with_suffix(".SRT"), 5)
    dataset = tmp_path / "dataset"
    prepare_video(video, dataset)

    with pytest.raises(PrepError, match="--overwrite"):
        prepare_video(video, dataset)

    prepare_video(video, dataset, overwrite=True)


def test_missing_srt(tmp_path: Path) -> None:
    with pytest.raises(PrepError, match=r"no \.SRT"):
        prepare_video(tmp_path / "DJI_0001.MP4", tmp_path / "dataset")


def test_srt_without_entries(tmp_path: Path) -> None:
    (tmp_path / "DJI_0001.SRT").write_text("not a subtitle file")

    with pytest.raises(PrepError, match="no subtitle entries"):
        prepare_video(tmp_path / "DJI_0001.MP4", tmp_path / "dataset")


def test_camera_override_needs_both(tmp_path: Path) -> None:
    with pytest.raises(PrepError, match="together"):
        prepare_video(tmp_path / "DJI_0001.MP4", tmp_path / "dataset", focal_length_mm=4.49)
