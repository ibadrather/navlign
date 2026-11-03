"""End-to-end on the OpenDroneMap Helenenschacht survey (Daniel Dobosi, GNU GPL v3): Autel EVO II Pro RTK images.

Skipped unless it was fetched with `make download-helenenschacht` in this repository.
"""

import json
from pathlib import Path

import pytest

from navlign import AltitudeMode, VerticalDatum, prepare_images

RAW = Path(__file__).resolve().parents[1] / "data" / "odm_data_helenenschacht"

pytestmark = pytest.mark.skipif(
    not (RAW / "images").is_dir(), reason="run `make download-helenenschacht` to fetch Helenenschacht"
)


def test_helenenschacht(tmp_path: Path) -> None:
    result = prepare_images(
        RAW / "images",
        tmp_path,
        altitude=AltitudeMode.ABSOLUTE,
        vertical_datum=VerticalDatum.ELLIPSOID,
        timezone="Europe/Vienna",
        attribution=[RAW / "README.md", RAW / "LICENSE"],
    )

    manifest = json.loads(result.manifest_path.read_text())
    assert (result.frames, result.located) == (176, 176)
    assert manifest["photogrammetry"]["altitude_reference"] == "ellipsoid"
    assert manifest["photogrammetry"]["cameras"] == {
        "autel_robotics_xt705_5472x3648_10.57mm": {
            "width": 5472,
            "height": 3648,
            "focal_length_mm": 10.57,
            "sensor_width_mm": 13.2,
            "shutter": "rolling",
        },
    }
    first = manifest["features"][0]
    # The same fix and ellipsoidal height as row 2 of RTK_Data/101FTASK_Timestamp.mrk.
    assert first["geometry"]["coordinates"] == pytest.approx([16.475927163, 47.643687321, 512.998], abs=1e-6)
    assert first["properties"] == {
        "image": "MAX_0002.JPG",
        "camera": "autel_robotics_xt705_5472x3648_10.57mm",
        # 12:17:49 CEST; the MRK has GPS week second 296288.24, 10:17:50 UTC.
        "time_utc": "2022-05-25T10:17:49Z",
        "yaw": -149.39,
        "pitch": -79.97,
        "roll": -0.01,
        "h_acc_m": 0.01439,
        "v_acc_m": 0.02969,
    }
    assert all(feature["properties"]["h_acc_m"] < 0.1 for feature in manifest["features"])
    assert "Autel Evo II Pro RTK" in (tmp_path / "README.md").read_text()
    assert "GNU GENERAL PUBLIC LICENSE" in (tmp_path / "LICENSE").read_text()
