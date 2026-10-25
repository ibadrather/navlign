import json
from collections.abc import Callable
from pathlib import Path

import pytest
from photogram_data_prep import AltitudeMode, PrepError, Shutter, VerticalDatum, prepare_images

type MakeJpeg = Callable[..., Path]


def load(dataset: Path) -> dict:
    return json.loads((dataset / "manifest.geojson").read_text())


@pytest.fixture
def source(tmp_path: Path, make_jpeg: MakeJpeg) -> Path:
    images = tmp_path / "raw" / "images"
    make_jpeg(images / "DJI_0002.JPG", lat=46.8427, relative_altitude=40.1, gps_altitude=198.6)
    make_jpeg(images / "DJI_0001.JPG", lat=46.8426)
    make_jpeg(images / "DJI_0003.jpg", lat=None)
    (images / "notes.txt").write_text("ignored")
    (images / ".DS_Store").write_bytes(b"")
    return images


def test_writes_manifest_and_copies_images(tmp_path: Path, source: Path) -> None:
    dataset = tmp_path / "dataset"

    result = prepare_images(source, dataset, timezone="America/Chicago")

    manifest = load(dataset)
    block = manifest["photogrammetry"]
    assert manifest["type"] == "FeatureCollection"
    assert block["source"] == "images"
    assert block["altitude_reference"] == "relative_takeoff"
    assert block["cameras"] == {
        "dji_fc300s_64x36_3.61mm": {
            "width": 64,
            "height": 36,
            "focal_length_mm": pytest.approx(3.61),
            "sensor_width_mm": 6.17,
            "shutter": "rolling",
        },
    }

    features = manifest["features"]
    assert [f["properties"]["image"] for f in features] == ["DJI_0001.JPG", "DJI_0002.JPG", "DJI_0003.jpg"]
    lon, lat, alt = features[1]["geometry"]["coordinates"]
    assert (lon, lat, alt) == (pytest.approx(-91.9945), pytest.approx(46.8427), 40.1)
    assert features[1]["properties"]["time_utc"] == "2016-06-23T21:31:59Z"
    assert features[1]["properties"]["yaw"] == pytest.approx(45.0)
    assert features[1]["properties"]["pitch"] == pytest.approx(-89.9)
    assert features[2]["geometry"] is None

    assert (result.frames, result.located) == (3, 2)
    copies = sorted((dataset / "images").iterdir())
    assert [copy.name for copy in copies] == ["DJI_0001.JPG", "DJI_0002.JPG", "DJI_0003.jpg"]
    assert copies[0].read_bytes() == (source / "DJI_0001.JPG").read_bytes()


def test_southern_and_eastern_hemispheres(tmp_path: Path, make_jpeg: MakeJpeg) -> None:
    make_jpeg(tmp_path / "images" / "a.jpg", lat=-33.5, lon=151.25)

    prepare_images(tmp_path / "images", tmp_path / "dataset")

    lon, lat, _ = load(tmp_path / "dataset")["features"][0]["geometry"]["coordinates"]
    assert (lon, lat) == (pytest.approx(151.25), pytest.approx(-33.5))


def test_zero_zero_is_no_fix(tmp_path: Path, make_jpeg: MakeJpeg) -> None:
    make_jpeg(tmp_path / "images" / "a.jpg", lat=0.0, lon=0.0)

    result = prepare_images(tmp_path / "images", tmp_path / "dataset")

    assert result.located == 0


def test_reads_autel_xmp(tmp_path: Path, make_jpeg: MakeJpeg) -> None:
    make_jpeg(
        tmp_path / "images" / "MAX_0002.JPG",
        make="Autel Robotics\x00\x00",
        model="XT705\x00\x00",
        focal_length=10.57,
        relative_altitude=49.98,
        yaw=-149.39,
        xmp_prefix="drone",
    )

    prepare_images(tmp_path / "images", tmp_path / "dataset")

    manifest = load(tmp_path / "dataset")
    properties = manifest["features"][0]["properties"]
    assert manifest["features"][0]["geometry"]["coordinates"][2] == pytest.approx(49.98)
    assert (properties["yaw"], properties["pitch"]) == (-149.39, -89.9)
    camera = manifest["photogrammetry"]["cameras"]["autel_robotics_xt705_64x36_10.57mm"]
    assert camera["sensor_width_mm"] == pytest.approx(13.2)


def test_rtk_accuracy(tmp_path: Path, make_jpeg: MakeJpeg) -> None:
    make_jpeg(tmp_path / "images" / "a.jpg", rtk_std=(0.0144, 0.0137, 0.0297))

    prepare_images(tmp_path / "images", tmp_path / "relative")
    prepare_images(
        tmp_path / "images",
        tmp_path / "absolute",
        altitude=AltitudeMode.ABSOLUTE,
        vertical_datum=VerticalDatum.ELLIPSOID,
    )

    relative = load(tmp_path / "relative")["features"][0]["properties"]
    absolute = load(tmp_path / "absolute")["features"][0]["properties"]
    assert relative["h_acc_m"] == pytest.approx(0.0144)
    # The RTK height accuracy says nothing about the barometric height.
    assert "v_acc_m" not in relative
    assert (absolute["h_acc_m"], absolute["v_acc_m"]) == (pytest.approx(0.0144), pytest.approx(0.0297))


def test_no_rtk_accuracy_without_a_fix(tmp_path: Path, make_jpeg: MakeJpeg) -> None:
    make_jpeg(tmp_path / "images" / "a.jpg", lat=None, rtk_std=(0.0144, 0.0137, 0.0297))
    make_jpeg(tmp_path / "images" / "b.jpg", rtk_std=(0.0, 0.0, 0.0))

    prepare_images(tmp_path / "images", tmp_path / "dataset")

    for feature in load(tmp_path / "dataset")["features"]:
        assert "h_acc_m" not in feature["properties"]


def test_absolute_altitude_with_datum(tmp_path: Path, source: Path) -> None:
    dataset = tmp_path / "dataset"

    prepare_images(source, dataset, altitude=AltitudeMode.ABSOLUTE, vertical_datum=VerticalDatum.EGM96)

    manifest = load(dataset)
    assert manifest["photogrammetry"]["altitude_reference"] == "egm96"
    assert manifest["features"][1]["geometry"]["coordinates"][2] == pytest.approx(198.6)


def test_absolute_altitude_needs_datum(tmp_path: Path, source: Path) -> None:
    with pytest.raises(PrepError, match="vertical datum"):
        prepare_images(source, tmp_path / "dataset", altitude=AltitudeMode.ABSOLUTE)


def test_no_time_utc_without_timezone(tmp_path: Path, source: Path) -> None:
    prepare_images(source, tmp_path / "dataset")

    assert "time_utc" not in load(tmp_path / "dataset")["features"][0]["properties"]


def test_relative_altitude_missing_fails_before_writing(tmp_path: Path, make_jpeg: MakeJpeg) -> None:
    make_jpeg(tmp_path / "images" / "a.jpg", relative_altitude=None)
    dataset = tmp_path / "dataset"

    with pytest.raises(PrepError, match="altitude absolute"):
        prepare_images(tmp_path / "images", dataset)
    assert not dataset.exists()


def test_unknown_camera_derives_sensor_width_from_35mm_equivalent(tmp_path: Path, make_jpeg: MakeJpeg) -> None:
    make_jpeg(tmp_path / "images" / "a.jpg", make="Acme", model="Cam 1", size=(40, 30))

    prepare_images(tmp_path / "images", tmp_path / "dataset", shutter=Shutter.GLOBAL)

    # 43.27 mm full-frame diagonal * 3.61 / 20 = 7.81 mm sensor diagonal; 4:3 -> width is 4/5 of it.
    cameras = load(tmp_path / "dataset")["photogrammetry"]["cameras"]
    assert cameras["acme_cam_1_40x30_3.61mm"]["sensor_width_mm"] == pytest.approx(6.248, abs=1e-3)
    assert cameras["acme_cam_1_40x30_3.61mm"]["shutter"] == "global"


def test_sensor_width_override(tmp_path: Path, source: Path) -> None:
    prepare_images(source, tmp_path / "dataset", sensor_width_mm=6.3)

    cameras = load(tmp_path / "dataset")["photogrammetry"]["cameras"]
    assert cameras["dji_fc300s_64x36_3.61mm"]["sensor_width_mm"] == pytest.approx(6.3)


def test_refuses_to_overwrite_unless_asked(tmp_path: Path, source: Path) -> None:
    dataset = tmp_path / "dataset"
    prepare_images(source, dataset)

    with pytest.raises(PrepError, match="--overwrite"):
        prepare_images(source, dataset)

    prepare_images(source, dataset, overwrite=True)
    assert (dataset / "images" / "DJI_0001.JPG").is_file()


def test_copies_attribution(tmp_path: Path, source: Path) -> None:
    licence = tmp_path / "raw" / "LICENSE"
    licence.write_text("BSD 2-Clause License")

    prepare_images(source, tmp_path / "dataset", attribution=[licence])

    assert (tmp_path / "dataset" / "LICENSE").read_text() == "BSD 2-Clause License"


def test_unreadable_file(tmp_path: Path) -> None:
    (tmp_path / "broken.jpg").write_bytes(b"not a jpeg")

    with pytest.raises(PrepError, match=r"broken\.jpg"):
        prepare_images(tmp_path, tmp_path / "dataset")


def test_empty_directory(tmp_path: Path) -> None:
    with pytest.raises(PrepError, match="no JPEG images"):
        prepare_images(tmp_path, tmp_path / "dataset")


def test_unknown_timezone(tmp_path: Path, source: Path) -> None:
    with pytest.raises(PrepError, match="time zone"):
        prepare_images(source, tmp_path / "dataset", timezone="Mars/Olympus")
