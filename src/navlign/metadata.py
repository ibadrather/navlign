"""Read everything a DJI video carries and write it as two files that share their timestamps.

- ``<video>.nav.geojson``: a point per telemetry entry with where the aircraft was and how it moved, and the
  attitude of the aircraft and gimbal at the start of the recording;
- ``<video>.camera.json``: the exposure per telemetry entry, the container, its streams, the device, and a row per
  video frame.

Both give each telemetry entry its ``index``, ``start_s`` and ``end_s`` (seconds of video) and ``time_utc``; each frame
names the entry it falls in, and each feature its first and last frame.

Nothing is decoded. ffprobe describes the container, the streams and the frames; the telemetry is the .SRT next to the
video or its subtitle stream, as for ``video``; the user data box is read directly (``boxes.py``); each data stream is
listed packet by packet with ffmpeg's framemd5 muxer, which tells filler (every packet the same) from data. FFmpeg must
be installed.
"""

import bisect
import itertools
import json
import math
import re
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

from navlign.boxes import read_udta
from navlign.images import Json, PrepError
from navlign.video import Entry, find_srt, read_telemetry

# ISO 6709, as in the location tag: +53.1149+8.8318 or +53.1149+008.8318+064.800/
ISO6709 = re.compile(r"([+-]\d+(?:\.\d+)?)([+-]\d+(?:\.\d+)?)([+-]\d+(?:\.\d+)?)?")
EARTH_RADIUS_M = 6_371_008.8

# What each telemetry value means and its unit. Values not listed keep their own name and go to the nav file.
FIELDS: dict[str, tuple[str, str]] = {
    "latitude": ("latitude", "°"),
    "longitude": ("longitude", "°"),
    "satellites": ("GPS satellites", ""),
    "rel_alt": ("height above take-off", "m"),
    "altitude": ("height above take-off", "m"),
    "abs_alt": ("altitude, datum not stated", "m"),
    "home_distance_m": ("distance from home point", "m"),
    "speed_ms": ("horizontal speed", "m/s"),
    "vertical_speed_ms": ("vertical speed", "m/s"),
    "gb_yaw": ("gimbal yaw", "°"),
    "gb_pitch": ("gimbal pitch", "°"),
    "gb_roll": ("gimbal roll", "°"),
    "SrtCnt": ("subtitle number", ""),
    "DiffTime": ("time since the last subtitle", "ms"),
    "f_number": ("aperture", "f/"),
    "fnum": ("aperture x 100", ""),
    "shutter": ("shutter speed, as 1 / exposure", "1/s"),
    "iso": ("ISO", ""),
    "ev": ("exposure bias", "EV"),
    "ct": ("colour temperature", "K"),
    "zoom": ("digital zoom", "x"),
    "dzoom_ratio": ("digital zoom x 10000", ""),
    "focal_len": ("focal length, 35 mm equivalent", "mm"),
}
CAMERA_FIELDS = {"f_number", "fnum", "shutter", "iso", "ev", "ct", "zoom", "dzoom_ratio", "focal_len"}
# The height that goes into the coordinates, in order of preference, and what it is measured from.
HEIGHTS = {"rel_alt": "relative_takeoff", "altitude": "relative_takeoff", "abs_alt": "absolute_datum_unknown"}
TIME_REFERENCE = "creation_time of the container (start of the recording, UTC) plus the seconds of video"


@dataclass(frozen=True)
class Frame:
    """A video frame in presentation order: its time from the first frame, its packet and whether it is a key frame."""

    time_s: float
    duration_s: float
    size_bytes: int
    offset: int
    key: bool


@dataclass(frozen=True)
class DataStream:
    """A data stream of the video, listed packet by packet: ``distinct`` 1 means filler."""

    index: int
    handler: str
    packets: int
    size_bytes: int
    distinct: int


@dataclass(frozen=True)
class VideoMetadata:
    """All a video carries: the container, its streams, user data, data streams, frames and telemetry."""

    path: Path
    container: Json
    streams: list[Json]
    udta: dict[str, Json]
    data_streams: list[DataStream]
    frames: list[Frame]
    telemetry_source: str | None
    telemetry_error: str | None
    entries: list[Entry]

    @property
    def tags(self) -> dict[str, str]:
        """The container tags."""
        tags = self.container.get("tags", {})
        return tags if isinstance(tags, dict) else {}

    @property
    def start_utc(self) -> datetime | None:
        """When the recording started: DJI writes it in UTC as the creation time of the container."""
        try:
            return datetime.fromisoformat(self.tags["creation_time"]).astimezone(UTC)
        except (KeyError, ValueError):
            return None

    @property
    def device(self) -> dict[str, str]:
        """The comment tag split up: DJI writes DE=None,SN=<serial>, Type=Normal, HQ=Normal, Mode=P."""
        comment = self.tags.get("comment", "")
        pairs = (part.split("=", 1) for part in comment.split(",") if "=" in part)
        return {key.strip(): value.strip() for key, value in pairs}

    @property
    def location(self) -> tuple[float, float] | None:
        """Latitude and longitude from the location tag: where the recording started."""
        if match := ISO6709.match(self.tags.get("location", "")):
            return float(match[1]), float(match[2])
        return None

    @property
    def fields(self) -> list[str]:
        """The telemetry values, in the order they first appear."""
        return list(dict.fromkeys(key for entry in self.entries for key in entry.fields))

    @property
    def nav_fields(self) -> list[str]:
        """The telemetry values about the aircraft: position, height, speed, attitude."""
        return [field for field in self.fields if field not in CAMERA_FIELDS]

    @property
    def camera_fields(self) -> list[str]:
        """The telemetry values about the camera: exposure, zoom, focal length."""
        return [field for field in self.fields if field in CAMERA_FIELDS]

    @property
    def height_field(self) -> str | None:
        """The telemetry value used as the height of each point."""
        return next((field for field in HEIGHTS if field in self.fields), None)

    def time_utc(self, seconds: float) -> str | None:
        """The UTC time ``seconds`` into the video, to the millisecond."""
        if (start := self.start_utc) is None:
            return None
        time = start + timedelta(seconds=seconds)
        return time.isoformat(timespec="milliseconds").replace("+00:00", "Z")

    def frame_entries(self) -> list[int | None]:
        """For each frame, the index of the telemetry entry whose interval holds it."""
        starts = [entry.start_s for entry in self.entries]
        found: list[int | None] = []
        for frame in self.frames:
            i = bisect.bisect_right(starts, frame.time_s) - 1
            found.append(i if i >= 0 and frame.time_s < self.entries[i].end_s else None)
        return found

    def nav_geojson(self, camera_file: str | None = None) -> Json:
        """A FeatureCollection with a point per telemetry entry and the attitude at the start in ``navigation``."""
        spans = _spans(self.frame_entries(), len(self.entries))
        features: list[Json] = []
        points: list[list[float]] = []
        for index, entry in enumerate(self.entries):
            geometry: Json | None = None
            if (position := _position(entry)) is not None:
                coordinates = [position[1], position[0]]
                if self.height_field and (height := entry.fields.get(self.height_field)) is not None:
                    coordinates.append(height)
                points.append(coordinates)
                geometry = {"type": "Point", "coordinates": coordinates}
            properties = self._timestamps(index, entry)
            if span := spans[index]:
                properties["first_frame"], properties["last_frame"] = span
            properties |= {field: entry.fields[field] for field in self.nav_fields if field in entry.fields}
            properties["text"] = entry.text
            features.append({"type": "Feature", "geometry": geometry, "properties": properties})

        collection: Json = {"type": "FeatureCollection"}
        if points and len({len(point) for point in points}) == 1:
            columns = list(zip(*points, strict=True))
            collection["bbox"] = [min(column) for column in columns] + [max(column) for column in columns]
        navigation: Json = {
            "source": self.telemetry_source,
            "entries": len(self.entries),
            "height": self.height_field,
            "altitude_reference": HEIGHTS.get(self.height_field or ""),
            "fields": _describe(self.nav_fields),
            "location_tag": dict(zip(("latitude", "longitude"), self.location, strict=True)) if self.location else None,
            "at_start": {kind: atom for kind, atom in self.udta.items() if atom["group"] == "nav"},
        }
        if self.telemetry_error:
            navigation["error"] = self.telemetry_error
        collection["video"] = self._video(camera_file=camera_file)
        collection["navigation"] = navigation
        collection["features"] = features
        return collection

    def camera_json(self, nav_file: str | None = None) -> Json:
        """The exposure per telemetry entry, the container, streams and device, and a row per frame."""
        entries = [
            self._timestamps(index, entry)
            | {field: entry.fields[field] for field in self.camera_fields if field in entry.fields}
            for index, entry in enumerate(self.entries)
        ]
        frames = [
            {
                "frame": number,
                "time_s": round(frame.time_s, 6),
                "time_utc": self.time_utc(frame.time_s),
                "duration_s": round(frame.duration_s, 6),
                "key": frame.key,
                "size_bytes": frame.size_bytes,
                "offset": frame.offset,
                "entry": entry,
            }
            for number, (frame, entry) in enumerate(zip(self.frames, self.frame_entries(), strict=True))
        ]
        return {
            "video": self._video(nav_file=nav_file),
            "device": self.device,
            "user_data": {kind: atom for kind, atom in self.udta.items() if atom["group"] == "camera"},
            "format": self.container,
            "streams": self.streams,
            "data_streams": [
                {
                    "index": stream.index,
                    "handler": stream.handler,
                    "packets": stream.packets,
                    "size_bytes": stream.size_bytes,
                    "distinct_packets": stream.distinct,
                }
                for stream in self.data_streams
            ],
            "fields": _describe(self.camera_fields),
            "entries": entries,
            "frames": frames,
        }

    def _video(self, *, nav_file: str | None = None, camera_file: str | None = None) -> Json:
        """What both files say about the video, and the name of the other file."""
        video: Json = {
            "name": self.path.name,
            "path": str(self.path.resolve()),
            "size_bytes": self.path.stat().st_size,
            "start_utc": self.time_utc(0),
            "time_reference": TIME_REFERENCE,
            "duration_s": float(str(self.container["duration"])) if "duration" in self.container else None,
            "frames": len(self.frames),
        }
        if nav_file:
            video["nav_file"] = nav_file
        if camera_file:
            video["camera_file"] = camera_file
        return video

    def _timestamps(self, index: int, entry: Entry) -> Json:
        """The keys a telemetry entry has in both files."""
        stamps: Json = {
            "index": index,
            "start_s": round(entry.start_s, 3),
            "end_s": round(entry.end_s, 3),
            "time_utc": self.time_utc(entry.start_s),
        }
        if entry.clock:
            stamps["clock"] = f"{entry.clock}.{entry.clock_ms:03d}"
        return stamps


def read_metadata(video: Path, *, srt: Path | None = None) -> VideoMetadata:
    """Everything ``video`` carries. Telemetry from ``srt``, the .SRT next to the video or its subtitle stream.

    A video without telemetry is read all the same, with the reason in ``telemetry_error``, unless ``srt`` was given.
    """
    command = ["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", str(video)]
    info = json.loads(_run(command, video))
    container = _printable({key: value for key, value in info.get("format", {}).items() if key != "filename"})
    streams = [_stream(stream) for stream in info.get("streams", [])]
    data_streams = [_data_stream(video, stream) for stream in streams if stream.get("codec_type") == "data"]
    frames = _frames(video) if any(stream.get("codec_type") == "video" for stream in streams) else []

    source = srt or find_srt(video)
    try:
        entries, error = read_telemetry(video, source), None
    except PrepError as failure:
        if srt is not None:
            raise
        entries, source, error = [], None, str(failure)
    name = source.name if source else ("subtitle stream" if entries else None)
    return VideoMetadata(video, container, streams, read_udta(video), data_streams, frames, name, error, entries)


def write_metadata(metadata: VideoMetadata, output_dir: Path, *, overwrite: bool = False) -> tuple[Path, Path]:
    """Write ``<video>.nav.geojson`` and ``<video>.camera.json`` to ``output_dir``."""
    nav_path, camera_path = output_paths(metadata.path, output_dir)
    if not overwrite and (existing := [path for path in (nav_path, camera_path) if path.exists()]):
        raise PrepError(f"{existing[0]} already exists; pass --overwrite to replace it")
    output_dir.mkdir(parents=True, exist_ok=True)
    for path, content in (
        (nav_path, metadata.nav_geojson(camera_file=camera_path.name)),
        (camera_path, metadata.camera_json(nav_file=nav_path.name)),
    ):
        path.write_text(json.dumps(content, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return nav_path, camera_path


def output_paths(video: Path, output_dir: Path) -> tuple[Path, Path]:
    """Where ``write_metadata`` puts the nav and camera files of ``video``."""
    return output_dir / f"{video.stem}.nav.geojson", output_dir / f"{video.stem}.camera.json"


def decimals(values: list[float]) -> int:
    """The fewest decimals that write all ``values`` exactly, up to 9."""
    return next((places for places in range(10) if all(abs(round(v, places) - v) < 1e-9 for v in values)), 9)


def track_length_m(entries: list[Entry]) -> float:
    """The length of the path through the positions of ``entries``, on a sphere."""
    positions = [position for entry in entries if (position := _position(entry)) is not None]
    length = 0.0
    for (lat0, lon0), (lat1, lon1) in itertools.pairwise(positions):
        phi0, phi1 = math.radians(lat0), math.radians(lat1)
        a = math.sin((phi1 - phi0) / 2) ** 2
        a += math.cos(phi0) * math.cos(phi1) * math.sin(math.radians(lon1 - lon0) / 2) ** 2
        length += 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))
    return length


def framemd5_packets(text: str) -> tuple[int, int, int]:
    """The number of packets, their total size and the number of distinct ones, from framemd5 output."""
    rows = [line.split(",") for line in text.splitlines() if line.strip() and not line.startswith("#")]
    return len(rows), sum(int(row[4]) for row in rows), len({(row[4].strip(), row[5].strip()) for row in rows})


def _describe(fields: list[str]) -> Json:
    """What each of ``fields`` means and its unit."""
    return {field: dict(zip(("meaning", "unit"), FIELDS.get(field, (field, "")), strict=True)) for field in fields}


def _spans(frame_entries: list[int | None], count: int) -> list[tuple[int, int] | None]:
    """For each telemetry entry, its first and last frame."""
    spans: list[tuple[int, int] | None] = [None] * count
    for frame, entry in enumerate(frame_entries):
        if entry is not None:
            span = spans[entry]
            spans[entry] = (span[0] if span else frame, frame)
    return spans


def _position(entry: Entry) -> tuple[float, float] | None:
    """Latitude and longitude of ``entry``; DJI writes 0/0 when there was no fix."""
    lat, lon = entry.fields.get("latitude"), entry.fields.get("longitude")
    if lat is None or lon is None or (lat, lon) == (0, 0):
        return None
    return lat, lon


def _run(command: list[str], video: Path) -> str:
    """The output of an FFmpeg ``command`` on ``video``."""
    try:
        return subprocess.run(command, capture_output=True, check=True, text=True).stdout
    except FileNotFoundError as error:
        raise PrepError(f"{command[0]} not found; install FFmpeg") from error
    except subprocess.CalledProcessError as error:
        raise PrepError(f"cannot read {video}: {error.stderr.strip()}") from error


def _stream(stream: Json) -> Json:
    """A stream as ffprobe describes it, with only the disposition flags that are set."""
    described = {key: value for key, value in stream.items() if key != "disposition"}
    disposition = stream.get("disposition", {})
    described["disposition"] = [flag for flag, on in disposition.items() if on] if isinstance(disposition, dict) else []
    return _printable(described)


def _frames(video: Path) -> list[Frame]:
    """The frames of the first video stream in presentation order, timed from the first."""
    command = ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries"]
    command += ["packet=pts_time,duration_time,size,pos,flags", "-of", "json", str(video)]
    try:
        packets = json.loads(_run(command, video))["packets"]
        frames = sorted(
            (
                float(packet["pts_time"]),
                float(packet.get("duration_time", 0)),
                int(packet["size"]),
                int(packet.get("pos", -1)),
                "K" in packet.get("flags", ""),
            )
            for packet in packets
        )
    except (KeyError, TypeError, ValueError) as error:
        raise PrepError(f"{video.name}: no frame times in the video stream") from error
    if not frames:
        return []
    first = frames[0][0]
    return [Frame(time - first, duration, size, offset, key) for time, duration, size, offset, key in frames]


def _data_stream(video: Path, stream: Json) -> DataStream:
    """List the packets of a data stream: their number, total size and how many differ."""
    index = stream["index"]
    command = ["ffmpeg", "-v", "error", "-nostdin", "-i", str(video), "-map", f"0:{index}", "-c", "copy"]
    packets, size, distinct = framemd5_packets(_run([*command, "-f", "framemd5", "-"], video))
    tags = stream.get("tags", {})
    handler = tags.get("handler_name", "") if isinstance(tags, dict) else ""
    return DataStream(int(str(index)), str(handler), packets, size, distinct)


def _printable[T](value: T) -> T:
    """``value`` with control characters dropped from its strings: DJI handler names start with a length byte."""
    if isinstance(value, str):
        return cast("T", "".join(character for character in value if character.isprintable()))
    if isinstance(value, dict):
        return cast("T", {key: _printable(item) for key, item in value.items()})
    if isinstance(value, list):
        return cast("T", [_printable(item) for item in value])
    return value
