"""End-to-end on the OpenDroneMap Brighton Beach dataset (BSD 2-Clause, (c) 2017 Piero Toffanin).

Skipped unless it was fetched with `make download-data` in this repository.
"""

import json
from pathlib import Path

import pytest

from navlign import prepare_images

RAW = Path(__file__).resolve().parents[1] / "data" / "drone_dataset_brighton_beach"

pytestmark = pytest.mark.skipif(
    not (RAW / "images").is_dir(), reason="run `make download-data` to fetch Brighton Beach"
)


def test_brighton_beach(tmp_path: Path) -> None:
    result = prepare_images(RAW / "images", tmp_path, timezone="America/Chicago", attribution=[RAW / "LICENSE"])

    manifest = json.loads(result.manifest_path.read_text())
    assert (result.frames, result.located) == (18, 18)
    assert manifest["photogrammetry"]["cameras"] == {
        "dji_fc300s_4000x2250_3.61mm": {
            "width": 4000,
            "height": 2250,
            "focal_length_mm": 3.61,
            "sensor_width_mm": 6.17,
            "shutter": "rolling",
        },
    }
    first = manifest["features"][0]
    assert first["geometry"]["coordinates"] == [-91.994559889, 46.842607083, 39.8]
    assert first["properties"] == {
        "image": "DJI_0018.JPG",
        "camera": "dji_fc300s_4000x2250_3.61mm",
        "time_utc": "2016-06-23T21:31:59Z",
        "yaw": 45.0,
        "pitch": -89.9,
        "roll": 0.0,
    }
    # Lawnmower pattern: strips flown in opposite directions.
    yaws = [feature["properties"]["yaw"] for feature in manifest["features"]]
    assert sum(yaw < 0 for yaw in yaws) == 6
    assert "Piero Toffanin" in (tmp_path / "LICENSE").read_text()
