"""End-to-end on the OpenDroneMap DJI video dataset (no licence given; local testing only).

Skipped unless it was fetched with `make download-data` in this repository and FFmpeg is installed.
"""

import json
import shutil
from pathlib import Path

import pytest

from navlign import prepare_video

RAW = Path(__file__).resolve().parents[1] / "data" / "drone_dataset_dji_video"

pytestmark = [
    pytest.mark.skipif(not (RAW / "DJI_0317.MP4").is_file(), reason="run `make download-data` to fetch the video"),
    pytest.mark.skipif(shutil.which("ffprobe") is None, reason="needs FFmpeg"),
]


def test_dji_video(tmp_path: Path) -> None:
    result = prepare_video(RAW / "DJI_0317.MP4", tmp_path, timezone="Europe/Rome")

    manifest = json.loads(result.manifest_path.read_text())
    # 2792 frames but 2791 SRT entries whose clock ends slightly early: the last two frames have no telemetry.
    assert (result.frames, result.located) == (2792, 2790)
    assert list(manifest["photogrammetry"]["cameras"]) == ["dji_video_960x540_24mm"]
    first = manifest["features"][0]
    assert first["geometry"]["coordinates"] == [9.040211, 45.499044, 29.6]
    assert first["properties"] == {
        "frame_index": 0,
        "camera": "dji_video_960x540_24mm",
        "time_utc": "2023-01-11T16:01:55.343Z",
    }
