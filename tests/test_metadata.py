import json
import shutil
import struct
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from navlign.boxes import read_udta
from navlign.cli import app
from navlign.metadata import framemd5_packets, read_metadata, write_metadata
from navlign.video import read_telemetry

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs FFmpeg")

# As the Mini 4K writes it, once a second.
LINE = (
    "F/2.8, SS {shutter}, ISO 100, EV 0, DZOOM 1.000, GPS (8.8318, 53.11{n}9, 23), D 57.24m, H 64.80m, "
    "H.S 2.00m/s, V.S -0.10m/s "
)


def write_srt(path: Path, seconds: int) -> Path:
    blocks = [
        f"{n + 1}\n00:00:0{n},000 --> 00:00:0{n + 1},000\n{LINE.format(shutter=100 + n, n=n)}\n" for n in range(seconds)
    ]
    path.write_text("\n".join(blocks))
    return path


def box(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", 8 + len(payload)) + kind + payload


def text_atom(kind: str, text: str) -> bytes:
    data = text.encode()
    return box(kind.encode("latin-1"), struct.pack(">HH", len(data) + 1, 0x7FFF) + data + b"\0")


def test_mini_4k_line_keeps_every_value(tmp_path: Path) -> None:
    srt = write_srt(tmp_path / "DJI_0001.SRT", 1)

    (entry,) = read_telemetry(tmp_path / "DJI_0001.MP4", srt)

    assert entry.fields == {
        "longitude": 8.8318,
        "latitude": 53.1109,
        "satellites": 23,
        "rel_alt": 64.8,
        "speed_ms": 2.0,
        "f_number": 2.8,
        "shutter": 100,
        "iso": 100,
        "ev": 0,
        "zoom": 1.0,
        "home_distance_m": 57.24,
        "vertical_speed_ms": -0.1,
    }
    assert entry.text.startswith("F/2.8, SS 100, ISO 100")


def test_framemd5_tells_filler_from_data() -> None:
    header = "#format: frame checksums\n#stream#, dts, pts, duration, size, hash\n"
    filler = header + "0, 0, 0, 0, 250, aa\n0, 1, 1, 0, 250, aa\n0, 2, 2, 0, 250, aa\n"
    data = header + "0, 0, 0, 0, 250, aa\n0, 1, 1, 0, 120, bb\n"

    assert framemd5_packets(filler) == (3, 750, 1)
    assert framemd5_packets(data) == (2, 370, 2)


def test_reads_dji_user_data(tmp_path: Path) -> None:
    xtra_value = "v01.43.0097\0".encode("utf-16-le")
    xtra_entry = struct.pack(">I", 11) + b"WM/Category" + struct.pack(">I", 1)
    xtra_entry += struct.pack(">IH", 6 + len(xtra_value), 8) + xtra_value
    udta = b"".join(
        [
            text_atom("©fyw", "+88.70"),
            text_atom("©gpt", "-90.00"),
            text_atom("©xyz", "+53.1149+8.8318"),
            box("©mdl".encode("latin-1"), b"FC7703" + b"\0" * 18),
            box("©res".encode("latin-1"), struct.pack(">HHI", 3840, 2160, 24) + b"\0" * 8),
            box(b"Xtra", struct.pack(">I", 4 + len(xtra_entry)) + xtra_entry),
            box("©uid".encode("latin-1"), bytes.fromhex("1ad1cb19006800ad")),
        ]
    )
    path = tmp_path / "clip.mp4"
    path.write_bytes(box(b"ftyp", b"avc1" * 2) + box(b"mdat", b"\0" * 16) + box(b"moov", box(b"udta", udta)))

    atoms = read_udta(path)

    assert atoms["©fyw"] | {"hex": ""} == {
        "meaning": "aircraft yaw at the start",
        "group": "nav",
        "unit": "°",
        "value": 88.7,
        "hex": "",
    }
    assert atoms["©gpt"]["value"] == pytest.approx(-90.0)
    assert atoms["©xyz"]["value"] == "+53.1149+8.8318"
    assert atoms["©mdl"]["value"] == "FC7703"
    assert atoms["©res"]["value"] == {"width": 3840, "height": 2160, "frame_rate": 24}
    assert atoms["Xtra"]["value"] == {"WM/Category": ["v01.43.0097"]}
    assert atoms["©uid"] == {"meaning": "not known", "group": "camera", "hex": "1ad1cb19006800ad"}


@pytest.fixture
def clip(tmp_path: Path) -> Path:
    """A 3 s clip at 10 fps with the telemetry as a subtitle stream and the tags DJI writes."""
    srt = write_srt(tmp_path / "telemetry.srt", 3)
    path = tmp_path / "raw" / "DJI_0173.MP4"
    path.parent.mkdir()
    command = ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=gray:s=64x36:r=10:d=3", "-i", str(srt)]
    command += ["-map", "0", "-map", "1", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:s", "mov_text"]
    command += ["-metadata", "creation_time=2026-11-01T12:24:49.000000Z", "-metadata", "location=+53.1149+8.8318"]
    command += ["-metadata", "comment=DE=None,SN=9J2FN7D0BL09SL, Type=Normal, HQ=Normal, Mode=P", str(path)]
    subprocess.run(command, check=True)
    return path


@needs_ffmpeg
def test_writes_nav_and_camera_files_with_common_timestamps(tmp_path: Path, clip: Path) -> None:
    metadata = read_metadata(clip)
    nav_path, camera_path = write_metadata(metadata, tmp_path / "out")

    nav = json.loads(nav_path.read_text())
    camera = json.loads(camera_path.read_text())
    assert (nav_path.name, camera_path.name) == ("DJI_0173.nav.geojson", "DJI_0173.camera.json")
    assert nav["navigation"]["source"] == "subtitle stream"
    assert nav["navigation"]["altitude_reference"] == "relative_takeoff"
    assert nav["video"]["camera_file"] == camera_path.name
    assert camera["video"]["nav_file"] == nav_path.name

    first = nav["features"][1]
    assert first["geometry"] == {"type": "Point", "coordinates": [8.8318, 53.1119, 64.8]}
    assert first["properties"] | {"text": ""} == {
        "index": 1,
        "start_s": 1.0,
        "end_s": 2.0,
        "time_utc": "2026-11-01T12:24:50.000Z",
        "first_frame": 10,
        "last_frame": 19,
        "longitude": 8.8318,
        "latitude": 53.1119,
        "satellites": 23,
        "rel_alt": 64.8,
        "speed_ms": 2.0,
        "home_distance_m": 57.24,
        "vertical_speed_ms": -0.1,
        "text": "",
    }
    assert nav["bbox"] == [8.8318, 53.1109, 64.8, 8.8318, 53.1129, 64.8]

    assert camera["device"]["SN"] == "9J2FN7D0BL09SL"
    assert camera["entries"][1] == {
        "index": 1,
        "start_s": 1.0,
        "end_s": 2.0,
        "time_utc": "2026-11-01T12:24:50.000Z",
        "f_number": 2.8,
        "shutter": 101,
        "iso": 100,
        "ev": 0,
        "zoom": 1.0,
    }
    frames = camera["frames"]
    assert len(frames) == 30
    assert frames[15]["entry"] == 1
    assert frames[15]["time_utc"] == "2026-11-01T12:24:50.500Z"
    assert frames[0]["key"] is True
    assert [stream["codec_type"] for stream in camera["streams"]] == ["video", "subtitle"]


@needs_ffmpeg
def test_cli_prints_and_refuses_to_overwrite(clip: Path) -> None:
    runner = CliRunner()

    result = runner.invoke(app, ["inspect", str(clip), "--rows", "2"], env={"COLUMNS": "160"})

    assert result.exit_code == 0, result.output
    assert "9J2FN7D0BL09SL" in result.output
    assert "Navigation entries (3)" in result.output
    assert (clip.parent / "DJI_0173.nav.geojson").is_file()
    assert (clip.parent / "DJI_0173.camera.json").is_file()
    again = runner.invoke(app, ["inspect", str(clip)])
    assert again.exit_code == 1
    assert "--overwrite" in again.output


@needs_ffmpeg
def test_video_without_telemetry_is_read_all_the_same(tmp_path: Path) -> None:
    path = tmp_path / "plain.mp4"
    command = ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=gray:s=64x36:r=10:d=1", "-pix_fmt", "yuv420p"]
    subprocess.run([*command, str(path)], check=True)

    metadata = read_metadata(path)

    assert metadata.entries == []
    assert metadata.telemetry_error
    assert len(metadata.frames) == 10
    assert metadata.nav_geojson()["features"] == []
