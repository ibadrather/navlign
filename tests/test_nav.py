import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pytest

from navlign.nav import align, find_tracks, write_trajectory
from navlign.video import Entry

START = datetime(2026, 11, 1, 12, 0, 0, tzinfo=UTC)
LON, LAT = 8.8354, 53.1084
EAST_M_PER_DEGREE = 111_195.0 * 0.6
# A line flown east: speeding up, steady, slowing down, a turn on the spot, and back up to speed. One value per 0.1 s.
SPEEDS = [
    *np.linspace(0, 5, 50),
    *[5.0] * 600,
    *np.linspace(5, 0, 50),
    *[0.0] * 50,
    *np.linspace(0, 4, 40),
    *[4.0] * 400,
]


def telemetry(clip_start_s: float, scale: float, seconds: int) -> list[Entry]:
    """What a clip that began ``clip_start_s`` into SPEEDS writes: an entry per second of video, each second of video
    being ``scale`` real seconds, with the position rounded to 4 decimals as the Mini 4K does."""
    east_m = np.concatenate([[0.0], np.cumsum(SPEEDS) / 10])
    entries = []
    for second in range(seconds):
        at = (clip_start_s + scale * second) * 10
        fields = {
            "longitude": round(LON + float(np.interp(at, np.arange(len(east_m)), east_m)) / EAST_M_PER_DEGREE, 4),
            "latitude": LAT,
            "rel_alt": 10.0,
            "speed_ms": round(float(np.interp(at, np.arange(len(SPEEDS)), SPEEDS)), 2),
        }
        entries.append(Entry(float(second), second + 1.0, fields, None, 0))
    return entries


def test_fits_the_start_and_the_time_scale_of_a_clip(tmp_path: Path, make_log: Callable[..., Path]) -> None:
    make_log(tmp_path / "frames-1.csv", START, SPEEDS, lon=LON, lat=LAT)
    (track,) = find_tracks([tmp_path], START.timestamp(), START.timestamp() + 60)
    created = START + timedelta(seconds=20)  # the clip really began 2.3 s later, and runs at 0.8 of real time

    fit = align(telemetry(22.3, 0.8, 100), created.timestamp(), track)

    assert fit is not None
    assert fit.scale == pytest.approx(0.8, abs=0.001)
    assert fit.offset_s == pytest.approx(2.3, abs=0.1)
    assert fit.rms_m < 4


def test_steady_flight_is_fitted_by_position(tmp_path: Path, make_log: Callable[..., Path]) -> None:
    make_log(tmp_path / "frames-1.csv", START, [5.0] * 1000, lon=LON, lat=LAT)
    (track,) = find_tracks([tmp_path], START.timestamp(), START.timestamp() + 60)
    east = [round(LON + 5.0 * (31 + second) / EAST_M_PER_DEGREE, 4) for second in range(60)]
    entries = [
        Entry(float(n), n + 1.0, {"longitude": lon, "latitude": LAT, "rel_alt": 10.0, "speed_ms": 5.0}, None, 0)
        for n, lon in enumerate(east)
    ]

    fit = align(entries, START.timestamp() + 30, track)

    assert fit is not None
    assert fit.speed_correlation is None
    assert fit.offset_s == pytest.approx(1.0, abs=0.5)


def test_a_clip_from_somewhere_else_does_not_fit(tmp_path: Path, make_log: Callable[..., Path]) -> None:
    make_log(tmp_path / "frames-1.csv", START, SPEEDS, lon=LON + 0.01, lat=LAT)
    (track,) = find_tracks([tmp_path], START.timestamp(), START.timestamp() + 60)

    assert align(telemetry(22.3, 0.8, 100), START.timestamp() + 20, track) is None


def test_finds_the_files_of_the_time_and_puts_the_finest_first(tmp_path: Path, make_log: Callable[..., Path]) -> None:
    mission = tmp_path / "missions" / "2026-11-01_field"
    make_log(mission / "frames-1.csv", START, SPEEDS)
    make_log(mission / "flown-1_high_res.geojson", START, SPEEDS)  # the same frames: not read twice
    make_log(mission / "flown-1.geojson", START, SPEEDS)
    make_log(mission / "flown-2_high_res.geojson", START + timedelta(minutes=3), SPEEDS)
    make_log(tmp_path / "missions" / "another-day" / "frames-1.csv", START + timedelta(days=1), SPEEDS)
    (mission / "raw").mkdir()
    (mission / "raw" / "FlightRecord_2026-11-01_[13-00-00].polls.csv").write_text("laptop_before,laptop_after\n1,2\n")

    tracks = find_tracks([tmp_path], START.timestamp(), START.timestamp() + 600)

    names = [track.path.name for track in tracks]
    assert sorted(names[:2]) == ["flown-2_high_res.geojson", "frames-1.csv"]
    assert names[2] == "flown-1.geojson"
    assert tracks[0].interval_s == pytest.approx(0.1)
    assert tracks[2].interval_s == pytest.approx(1.3)
    assert all("yaw" in track.columns for track in tracks[:2])
    assert "yaw" not in tracks[2].columns  # Swathwise's fixes have the heading of the aircraft, not the gimbal


def test_values_between_two_points_and_a_yaw_across_north(tmp_path: Path, make_log: Callable[..., Path]) -> None:
    path = make_log(tmp_path / "frames-1.csv", START, [2.0] * 20, lon=LON, lat=LAT)
    lines = path.read_text().splitlines()
    column = lines[0].split(",").index("GIMBAL.yaw")
    for number in range(1, len(lines)):  # the gimbal turns through south: 179°, then -179°
        cells = lines[number].split(",")
        cells[column] = "179.0" if number % 2 else "-179.0"
        lines[number] = ",".join(cells)
    path.write_text("\n".join(lines) + "\n")
    (track,) = find_tracks([path], START.timestamp(), START.timestamp() + 60)

    values = track.at(START.timestamp() + 0.05)

    assert abs(values["yaw"]) == pytest.approx(180.0)
    assert values["lon"] == pytest.approx(LON + 0.1 / EAST_M_PER_DEGREE, abs=1e-8)
    assert values["speed_east_ms"] == pytest.approx(2.0)
    assert track.covers(START.timestamp() + 1)
    assert not track.covers(START.timestamp() + 5)


def test_the_trajectory_is_the_log_within_the_mission(tmp_path: Path, make_log: Callable[..., Path]) -> None:
    make_log(tmp_path / "frames-1.csv", START, [2.0] * 100, lon=LON, lat=LAT)
    ends = {"started": "2026-11-01T12:00:02.000Z", "ended": "2026-11-01T12:00:05.000Z"}
    (tmp_path / "mission.json").write_text(json.dumps(ends))
    tracks = find_tracks([tmp_path], START.timestamp(), START.timestamp() + 60)

    count = write_trajectory(tmp_path / "trajectory.geojson", tracks)

    written = json.loads((tmp_path / "trajectory.geojson").read_text())
    assert count == len(written["features"]) == 31
    assert written["navlign"]["sources"] == ["frames-1.csv"]
    first = written["features"][0]
    assert first["geometry"]["coordinates"] == [pytest.approx(LON + 4 / EAST_M_PER_DEGREE, abs=1e-8), LAT, 10.0]
    assert first["properties"]["time_utc"] == "2026-11-01T12:00:02.000Z"
    assert first["properties"]["yaw"] == pytest.approx(90.5)
    assert first["properties"]["pitch"] == pytest.approx(-90.0)
    assert first["properties"]["recording"] is True
