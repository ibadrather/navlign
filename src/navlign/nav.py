"""Find the navigation recorded outside the video, pick the best for each clip and put it on the clock of the clip.

A Mini 4K clip carries one telemetry line a second with the position rounded to 4 decimals (about 11 m). The flight
log of DJI Fly, which Swathwise keeps in the mission folder, has ten frames a second with the position as logged, the
speeds, and the attitude of the aircraft and the gimbal. These files are read, the finest first:

- ``frames-N.csv``, ``FlightRecord_*.csv``: the flight log as ``dji-log --csv`` writes it, about 10 Hz;
- ``flown-N_high_res.geojson``: the same frames as GeoJSON, read when there is no ``frames-N.csv`` next to it;
- ``flown-N.geojson``: the fixes Swathwise received live, about one every 1.3 s, positions to 7 decimals.

A clip does not say exactly when it started (``creation_time`` is in whole seconds and up to a few seconds early), and
a second of video is not always a second: the 24 fps mode of the Mini 4K runs at 0.8 of real time. So each clip is
fitted to the track as ``time = creation_time + offset + scale * video seconds``: roughly by position, which is
unambiguous, then finely by the speed profile, which is sharp (the telemetry speed has 0.01 m/s steps).
"""

import csv
import json
import math
import os
import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from operator import itemgetter
from pathlib import Path

import numpy as np
from numpy.typing import NDArray

from navlign.images import Json, PrepError
from navlign.video import Entry

type Array = NDArray[np.float64]

NAV_ENV = "NAVLIGN_NAV"  # more places to look for navigation, separated like PATH
TRAJECTORY_NAME = "trajectory.geojson"
FRAMES_NAME = re.compile(r"(frames-\d+|FlightRecord_.*(?<!\.polls))\.csv")
HIGH_RES_NAME = re.compile(r"flown-(\d+)_high_res\.geojson")
FLOWN_NAME = re.compile(r"flown-\d+\.geojson")
METRES_PER_DEGREE = 111_195.0
MAX_GAP_S = 2.0  # a track is not trusted across a hole longer than this
MARGIN_S = 60.0  # how far from its creation time a clip is looked for in a track
SCALES = (0.76, 1.04)  # video seconds to real seconds: 0.8 for the Mini 4K at 24 fps, 1 otherwise
MIN_ENTRIES = 10  # telemetry entries inside the track that a fit needs
MAX_RMS_M = 10.0  # 4-decimal rounding alone leaves about 4 m between telemetry and track
MIN_SPEED_CORRELATION = 0.9

# Name here, column of the DJI flight log, property of a Swathwise fix. Speeds are north, east, down; the attitude
# is the gimbal's (yaw clockwise from north, pitch -90 = nadir), then the aircraft's.
COLUMNS: tuple[tuple[str, str, str | None], ...] = (
    ("lon", "OSD.longitude", None),
    ("lat", "OSD.latitude", None),
    ("height", "OSD.height", "height_m"),
    ("speed_north_ms", "OSD.xSpeed", "speed_north_ms"),
    ("speed_east_ms", "OSD.ySpeed", "speed_east_ms"),
    ("speed_down_ms", "OSD.zSpeed", None),
    ("yaw", "GIMBAL.yaw", None),
    ("pitch", "GIMBAL.pitch", None),
    ("roll", "GIMBAL.roll", None),
    ("aircraft_yaw", "OSD.yaw", "heading_deg"),
    ("aircraft_pitch", "OSD.pitch", None),
    ("aircraft_roll", "OSD.roll", None),
    ("satellites", "OSD.gpsNum", "satellites"),
)
REQUIRED = ("lon", "lat", "height", "speed_north_ms", "speed_east_ms")
YAWS = frozenset({"yaw", "aircraft_yaw"})
DECIMALS = {"lon": 9, "lat": 9, "height": 3, "satellites": 0}


@dataclass(frozen=True)
class Track:
    """Navigation from one file: times in Unix seconds, rising, and a column of values for each name it has."""

    path: Path
    times: Array
    columns: dict[str, Array]
    recording: NDArray[np.bool_] | None  # whether the camera was recording video
    mission: tuple[float, float] | None  # when the mission started and ended, from mission.json next to the file

    @property
    def interval_s(self) -> float:
        """The usual time between two points."""
        return float(np.median(np.diff(self.times)))

    def covered(self, times: Array) -> NDArray[np.bool_]:
        """Which of ``times`` lie between two points that are close enough to interpolate."""
        after = np.clip(np.searchsorted(self.times, times), 1, len(self.times) - 1)
        within = (times >= self.times[0]) & (times <= self.times[-1])
        return within & (self.times[after] - self.times[after - 1] <= MAX_GAP_S)

    def covers(self, time: float) -> bool:
        """Whether ``time`` lies between two points that are close enough to interpolate."""
        return bool(self.covered(np.array([time]))[0])

    def at(self, time: float) -> dict[str, float | int]:
        """Every column at ``time``, linear between the two points around it; yaws come back within ±180°."""
        values = {name: float(np.interp(time, self.times, column)) for name, column in self.columns.items()}
        for name in YAWS & values.keys():
            values[name] = (values[name] + 180) % 360 - 180
        rounded = {name: round(value, DECIMALS.get(name, 2)) for name, value in values.items()}
        return {name: int(value) if name == "satellites" else value for name, value in rounded.items()}


@dataclass(frozen=True)
class Alignment:
    """A clip on the clock of a track: time = creation_time + offset_s + scale * video seconds."""

    track: Track
    scale: float
    offset_s: float
    rms_m: float  # between the telemetry positions of the clip and the track
    speed_correlation: float | None  # None when the speed was too steady to use


def nav_places(videos: Sequence[Path], nav: Sequence[Path] | None) -> list[Path]:
    """Where to look: ``nav`` when given, else the folders of the videos and those named in NAVLIGN_NAV."""
    if nav is not None:
        return list(nav)
    places = list(dict.fromkeys(video.resolve().parent for video in videos))
    return places + [Path(place) for place in os.environ.get(NAV_ENV, "").split(os.pathsep) if place]


def find_tracks(places: Sequence[Path], start: float, end: float) -> list[Track]:
    """The tracks in ``places`` (files, or folders searched all the way down) with points between start and end,
    the finest first."""
    files: dict[Path, None] = {}
    for place in places:
        if not place.exists():
            raise PrepError(f"no navigation at {place}")
        for path in [place] if place.is_file() else sorted(place.rglob("*")):
            high_res = HIGH_RES_NAME.fullmatch(path.name)  # the same frames as frames-N.csv, which reads faster
            if high_res and path.with_name(f"frames-{high_res[1]}.csv").exists():
                continue
            if high_res or FRAMES_NAME.fullmatch(path.name) or FLOWN_NAME.fullmatch(path.name):
                files[path.resolve()] = None
    tracks: list[Track] = []
    for path in files:
        try:
            first, last = _span(path)
            if first > end or last < start:
                continue
            track = _read_track(path)
        except (OSError, ValueError, KeyError, TypeError, IndexError):
            continue  # not one of ours after all, or cut short
        if track is not None:
            tracks.append(track)
    return sorted(tracks, key=lambda track: track.interval_s)


def align(entries: Sequence[Entry], created: float, track: Track) -> Alignment | None:
    """Fit the telemetry of a clip created at ``created`` to ``track``; None when they do not agree."""
    fixes = [entry for entry in entries if (entry.fields["latitude"], entry.fields["longitude"]) != (0, 0)]
    if len(fixes) < MIN_ENTRIES:
        return None
    moments = np.array([entry.start_s for entry in fixes])
    lon = np.array([entry.fields["longitude"] for entry in fixes])
    lat = np.array([entry.fields["latitude"] for entry in fixes])
    speed = np.array([entry.fields["speed_ms"] for entry in fixes])
    east_m = METRES_PER_DEGREE * math.cos(math.radians(float(lat.mean())))
    track_speed = np.hypot(track.columns["speed_north_ms"], track.columns["speed_east_ms"])

    def fit(scales: np.ndarray, offsets: np.ndarray, *, use_speed: bool) -> tuple[float, float]:
        """The scale and offset of the grid that fit best: least distance, or with use_speed the most alike speed."""
        best = (math.inf, 1.0, 0.0)
        for scale in scales:
            times = created + offsets[:, None] + scale * moments[None, :]
            inside = track.covered(times)
            enough = inside.sum(axis=1) >= max(MIN_ENTRIES, len(moments) // 2)
            if use_speed:
                there = np.where(inside, np.interp(times, track.times, track_speed), np.nan)
                cost = np.array([-_correlation(row, speed) for row in there])
            else:
                east = (np.interp(times, track.times, track.columns["lon"]) - lon) * east_m
                north = (np.interp(times, track.times, track.columns["lat"]) - lat) * METRES_PER_DEGREE
                squares = np.where(inside, east**2 + north**2, 0.0).sum(axis=1)
                cost = np.sqrt(squares / np.maximum(inside.sum(axis=1), 1))
            cost = np.where(enough & np.isfinite(cost), cost, math.inf)
            if cost.min() < best[0]:
                best = (float(cost.min()), float(scale), float(offsets[cost.argmin()]))
        return best[1], best[2]

    def measure(scale: float, offset: float) -> tuple[float, float]:
        """The distance between telemetry and track in metres (rms) and how alike their speeds are."""
        times = created + offset + scale * moments
        inside = track.covered(times)
        if inside.sum() < MIN_ENTRIES:
            return math.inf, -1.0
        east = (np.interp(times, track.times, track.columns["lon"]) - lon) * east_m
        north = (np.interp(times, track.times, track.columns["lat"]) - lat) * METRES_PER_DEGREE
        there = np.interp(times, track.times, track_speed)
        return float(np.sqrt(np.mean((east**2 + north**2)[inside]))), _correlation(there[inside], speed[inside])

    scales, offsets = np.arange(SCALES[0], SCALES[1] + 0.001, 0.02), np.arange(-MARGIN_S, MARGIN_S + 0.1, 0.5)
    rough = fit(scales, offsets, use_speed=False)
    # The speed profile is the sharper of the two, when the speed changed at all; else the position has to do. A short
    # clip moves too little for its position to tell the scale, so every scale is tried again around the rough start.
    every_scale = np.arange(SCALES[0], SCALES[1] + 0.001, 0.004)
    for use_speed in (True, False):
        scale, offset = fit(every_scale, rough[1] + np.arange(-60, 61) * 0.05, use_speed=use_speed)
        scales, offsets = scale + np.arange(-10, 11) * 0.0004, offset + np.arange(-40, 41) * 0.01
        scale, offset = fit(scales, offsets, use_speed=use_speed)
        rms, correlation = measure(scale, offset)
        if rms <= MAX_RMS_M and (not use_speed or correlation >= MIN_SPEED_CORRELATION):
            used = round(correlation, 4) if use_speed else None
            return Alignment(track, round(scale, 4), round(offset, 2), round(rms, 2), used)
    return None


def write_trajectory(path: Path, tracks: Sequence[Track]) -> int:
    """Write every point of ``tracks`` as a GeoJSON point, one a line, within the mission when its times are known.

    Returns the number of points. Heights are above take-off and angles as in the manifest.
    """
    lines: list[str] = []
    for track in tracks:
        first, last = track.mission or (-math.inf, math.inf)
        for i in np.flatnonzero((track.times >= first) & (track.times <= last)):
            values = track.at(float(track.times[i]))
            properties: Json = {"time_utc": _iso(float(track.times[i])), "source": track.path.name}
            properties |= {name: value for name, value in values.items() if name not in ("lon", "lat", "height")}
            if track.recording is not None:
                properties["recording"] = bool(track.recording[i])
            point = {"type": "Point", "coordinates": [values["lon"], values["lat"], values["height"]]}
            lines.append(json.dumps({"type": "Feature", "geometry": point, "properties": properties}))
    header = {
        "altitude_reference": "relative_takeoff",
        "orientation_convention": "ned_yaw_pitch_roll_deg",
        "sources": [track.path.name for track in tracks],
        "points": len(lines),
    }
    text = '{"type": "FeatureCollection", "navlign": ' + json.dumps(header) + ', "features": [\n'
    path.write_text(text + ",\n".join(lines) + "\n]}\n", encoding="utf-8")
    return len(lines)


def _correlation(a: Array, b: Array) -> float:
    """How alike two series are, -1 to 1, over the values both have; -1 when that cannot be said."""
    both = np.isfinite(a) & np.isfinite(b)
    if both.sum() < MIN_ENTRIES or a[both].std() == 0 or b[both].std() == 0:
        return -1.0
    return float(np.corrcoef(a[both], b[both])[0, 1])


def _iso(time: float) -> str:
    return datetime.fromtimestamp(time, UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _timestamp(text: str) -> float:
    return datetime.fromisoformat(text).timestamp()


def _span(path: Path) -> tuple[float, float]:
    """The first and last time in a file, without reading all of it."""
    with path.open("rb") as file:
        head = file.read(65536).decode("utf-8", errors="replace").splitlines()
        if path.suffix == ".csv":
            file.seek(max(0, path.stat().st_size - 65536))
            tail = file.read().decode("utf-8", errors="replace").splitlines()
            return _timestamp(head[1].split(",", 1)[0]), _timestamp(tail[-1].split(",", 1)[0])
    summary = json.loads(head[1].rstrip(","))["properties"]
    return _timestamp(summary["started"]), _timestamp(summary["ended"])


def _rows(path: Path) -> Iterator[tuple[dict[str, object], list[float] | None]]:
    """Each point of a file as a mapping, with its coordinates when the file is GeoJSON."""
    if path.suffix == ".csv":
        with path.open(newline="", encoding="utf-8") as file:
            for row in csv.DictReader(file):
                yield row, None
        return
    with path.open(encoding="utf-8") as file:
        for line in file:
            if '"Point"' in line:  # one feature a line; the summary and the lines of the track are not points
                feature = json.loads(line.rstrip().rstrip(","))
                yield feature["properties"], feature["geometry"]["coordinates"]


def _read_track(path: Path) -> Track | None:
    """The track in ``path``: its points with a position, in time order. None when there are too few."""
    points: list[tuple[float, list[float], bool | None]] = []
    for row, coordinates in _rows(path):
        if "CUSTOM.dateTime" in row:  # a frame of the flight log
            values = {name: row.get(column) for name, column, _ in COLUMNS}
            time, recording = row.get("CUSTOM.dateTime"), row.get("CAMERA.isVideo")
        else:  # a fix as Swathwise received it
            values = {name: row.get(key) for name, _, key in COLUMNS if key}
            values["lon"], values["lat"] = coordinates[:2] if coordinates else (None, None)
            time, recording = row.get("time"), row.get("recording")
        try:
            given = (values.get(name) for name, *_ in COLUMNS)
            numbers = [math.nan if value in (None, "") else float(str(value)) for value in given]
            moment = _timestamp(str(time))
        except ValueError:
            continue
        lon, lat = numbers[0], numbers[1]
        # Before the GPS has a fix the log writes 0/0, and before the clock is set, 1970.
        if moment < 1e9 or math.isnan(lon) or math.isnan(lat) or (lon, lat) == (0, 0):
            continue
        points.append((moment, numbers, None if recording in (None, "") else str(recording).lower() == "true"))
    points.sort(key=itemgetter(0))
    points = [point for i, point in enumerate(points) if i == 0 or point[0] > points[i - 1][0]]
    if len(points) < MIN_ENTRIES:
        return None
    table = np.array([numbers for _, numbers, _ in points])
    columns = {name: table[:, i] for i, (name, *_) in enumerate(COLUMNS) if not np.isnan(table[:, i]).any()}
    if any(name not in columns for name in REQUIRED):
        return None
    for name in YAWS & columns.keys():
        columns[name] = np.unwrap(columns[name], period=360)  # so that 179° to -179° is 2° and not 358°
    flags = [recording for *_, recording in points]
    return Track(
        path=path,
        times=np.array([moment for moment, *_ in points]),
        columns=columns,
        recording=None if None in flags else np.array(flags, dtype=bool),
        mission=_mission_span(path.parent),
    )


def _mission_span(folder: Path) -> tuple[float, float] | None:
    """When the mission in ``folder`` started and ended, from Swathwise's mission.json."""
    try:
        mission = json.loads((folder / "mission.json").read_text(encoding="utf-8"))
        return _timestamp(mission["started"]), _timestamp(mission["ended"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
