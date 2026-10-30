"""Prepare a dataset (manifest.geojson + video) from a DJI video and the telemetry recorded with it.

The telemetry is the .SRT next to the video or, failing that, the subtitle stream inside it. Each video frame gets
the SRT entry whose subtitle interval contains the frame's presentation time. Frame times come from ffprobe, which
must be installed (FFmpeg).
"""

import bisect
import json
import math
import re
import shutil
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from photogram_data_prep.images import (
    FULL_FRAME_DIAGONAL_MM,
    AltitudeMode,
    Json,
    PrepError,
    PrepResult,
    Shutter,
    VerticalDatum,
)

TIMING = re.compile(r"(\d+):(\d\d):(\d\d)[,.](\d{3})\s*-->\s*(\d+):(\d\d):(\d\d)[,.](\d{3})")
# Values like latitude: 45.499044 or rel_alt: 1.300 abs_alt: 132.778 inside the [...] groups.
FIELD = re.compile(r"([A-Za-z_]\w*)\s*:\s*([-+]?\d+(?:\.\d+)?)")
CLOCK = re.compile(r"(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)(?:[,.](\d{3}))?")
# The one line the Mini 4K writes: GPS (lon, lat, satellites), D home distance, H height above take-off, H.S speed.
FLIGHT_LINE = re.compile(r"GPS \(([-\d.]+), ([-\d.]+), [-\d.]+\), D [\d.]+m, H ([-\d.]+)m, H\.S ([\d.]+)m/s")


@dataclass(frozen=True)
class Entry:
    """One SRT subtitle: its interval in seconds from the start of the video and the values it carries."""

    start_s: float
    end_s: float
    fields: dict[str, float]
    clock: str | None
    clock_ms: int


def prepare_video(
    video: Path,
    dataset_dir: Path,
    *,
    srt: Path | None = None,
    altitude: AltitudeMode = AltitudeMode.RELATIVE,
    vertical_datum: VerticalDatum | None = None,
    timezone: str | None = None,
    focal_length_mm: float | None = None,
    sensor_width_mm: float | None = None,
    shutter: Shutter = Shutter.ROLLING,
    attribution: Sequence[Path] = (),
    overwrite: bool = False,
) -> PrepResult:
    """Write dataset_dir/manifest.geojson with one feature per frame and copy the video to dataset_dir.

    srt defaults to the .SRT next to the video, then to the subtitle stream inside it. timezone is the IANA zone of
    the drone clock; without it no time_utc is written. Without focal_length_mm and sensor_width_mm the camera is the
    35 mm equivalent from the SRT. Everything is read before anything is written.
    """
    if altitude is AltitudeMode.RELATIVE and vertical_datum is not None:
        raise PrepError("a vertical datum only applies to absolute altitudes")
    if altitude is AltitudeMode.ABSOLUTE and vertical_datum is None:
        raise PrepError("absolute altitudes need their vertical datum (ellipsoid or egm96)")
    if (focal_length_mm is None) != (sensor_width_mm is None):
        raise PrepError("pass --focal-length-mm and --sensor-width-mm together")
    altitude_reference = str(vertical_datum) if vertical_datum else "relative_takeoff"
    try:
        zone = ZoneInfo(timezone) if timezone else None
    except (ZoneInfoNotFoundError, ValueError) as error:
        raise PrepError(f"unknown time zone {timezone!r}") from error

    entries = read_telemetry(video, srt)
    width, height, times, _ = probe(video)

    # Camera
    if focal_length_mm is None or sensor_width_mm is None:
        # Mini 2 / Mini SE write tenths of a millimetre (240), later models millimetres (24.00).
        focals = {
            value / 10 if value >= 100 else value for entry in entries if (value := entry.fields.get("focal_len"))
        }
        if len(focals) != 1:
            found = "no" if not focals else "a changing"
            raise PrepError(f"{video.name}: {found} focal length; pass --focal-length-mm and --sensor-width-mm")
        focal_length_mm = focals.pop()
        sensor_width_mm = FULL_FRAME_DIAGONAL_MM * width / math.hypot(width, height)
    camera_id = f"dji_video_{width}x{height}_{focal_length_mm:g}mm"
    camera: Json = {
        "width": width,
        "height": height,
        "focal_length_mm": focal_length_mm,
        "sensor_width_mm": round(sensor_width_mm, 3),
        "shutter": str(shutter),
    }

    starts = [entry.start_s for entry in entries]
    features: list[Json] = []
    for frame_index, time_s in enumerate(times):
        properties: Json = {"frame_index": frame_index, "camera": camera_id}
        geometry: Json | None = None
        i = bisect.bisect_right(starts, time_s) - 1
        if i >= 0 and time_s < entries[i].end_s:
            entry = entries[i]
            lat, lon = entry.fields.get("latitude"), entry.fields.get("longitude")
            # DJI writes 0/0 when there was no fix.
            if lat is not None and lon is not None and (lat, lon) != (0, 0):
                if altitude is AltitudeMode.RELATIVE:
                    alt = entry.fields.get("rel_alt", entry.fields.get("altitude"))
                else:
                    alt = entry.fields.get("abs_alt")
                if alt is None:
                    raise PrepError(f"{video.name}: has a position but no {altitude} altitude")
                geometry = {"type": "Point", "coordinates": [round(lon, 9), round(lat, 9), round(alt, 3)]}
            if zone and entry.clock:
                local = datetime.strptime(entry.clock, "%Y-%m-%d %H:%M:%S").replace(tzinfo=zone)
                utc = (local + timedelta(milliseconds=entry.clock_ms)).astimezone(UTC)
                properties["time_utc"] = utc.isoformat(timespec="milliseconds").replace("+00:00", "Z")
            for key, field in (("yaw", "gb_yaw"), ("pitch", "gb_pitch"), ("roll", "gb_roll")):
                if (angle := entry.fields.get(field)) is not None:
                    properties[key] = angle
        features.append({"type": "Feature", "geometry": geometry, "properties": properties})

    manifest_path = dataset_dir / "manifest.geojson"
    video_path = dataset_dir / f"video{video.suffix.lower()}"
    if (manifest_path.exists() or video_path.exists()) and not overwrite:
        raise PrepError(f"{dataset_dir} already holds a dataset; pass --overwrite to replace it")
    dataset_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(video, video_path)
    for path in attribution:
        shutil.copy2(path, dataset_dir / path.name)

    manifest = {
        "type": "FeatureCollection",
        "photogrammetry": {
            "schema_version": "0.1.0",
            "source": video_path.name,
            "altitude_reference": altitude_reference,
            # Gimbal attitude in degrees: yaw clockwise from north, pitch -90 = nadir, roll right side down.
            "orientation_convention": "ned_yaw_pitch_roll_deg",
            "cameras": {camera_id: camera},
        },
        "features": features,
    }
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return PrepResult(
        manifest_path=manifest_path,
        frames=len(features),
        located=sum(feature["geometry"] is not None for feature in features),
        altitude_reference=altitude_reference,
        cameras=[camera_id],
    )


def read_telemetry(video: Path, srt: Path | None = None) -> list[Entry]:
    """The telemetry of ``video``: from ``srt``, the .SRT next to it, or the subtitle stream inside it."""
    srt = srt or next((path for path in (video.with_suffix(".SRT"), video.with_suffix(".srt")) if path.is_file()), None)
    if srt is not None:
        try:
            return _parse_srt(srt.read_text(encoding="utf-8-sig", errors="replace"), srt)
        except OSError as error:
            raise PrepError(f"cannot read {srt}: {error}") from error
    command = ["ffmpeg", "-v", "error", "-nostdin", "-i", str(video), "-map", "0:s:0", "-f", "srt", "-"]
    try:
        completed = subprocess.run(command, capture_output=True, check=True, text=True, errors="replace")
    except FileNotFoundError as error:
        raise PrepError("ffmpeg not found; install FFmpeg") from error
    except subprocess.CalledProcessError as error:
        raise PrepError(f"no .SRT next to {video} and no subtitle stream in it; pass --srt") from error
    return _parse_srt(completed.stdout, video)


def _parse_srt(text: str, path: Path) -> list[Entry]:
    """The entries of a DJI subtitle file, in time order."""
    entries: list[Entry] = []
    for block in re.split(r"\n\s*\n", text.replace("\r\n", "\n")):
        if not (timing := TIMING.search(block)):
            continue
        h0, m0, s0, ms0, h1, m1, s1, ms1 = (int(part) for part in timing.groups())
        body = re.sub(r"<[^>]+>", " ", block[timing.end() :])
        clock = CLOCK.search(body)
        fields = {key: float(value) for key, value in FIELD.findall(CLOCK.sub(" ", body))}
        if flight := FLIGHT_LINE.search(body):
            fields["longitude"], fields["latitude"], fields["rel_alt"], fields["speed_ms"] = map(float, flight.groups())
        entries.append(
            Entry(
                start_s=h0 * 3600 + m0 * 60 + s0 + ms0 / 1000,
                end_s=h1 * 3600 + m1 * 60 + s1 + ms1 / 1000,
                fields=fields,
                clock=clock[1] if clock else None,
                clock_ms=int(clock[2] or 0) if clock else 0,
            )
        )
    if not entries:
        raise PrepError(f"no subtitle entries in {path}")
    return sorted(entries, key=lambda entry: entry.start_s)


def probe(video: Path) -> tuple[int, int, list[float], str | None]:
    """Width, height, frame presentation times in seconds from the first frame and the creation time, via ffprobe."""
    command = ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries"]
    command += ["stream=width,height:packet=pts_time:format_tags=creation_time", "-of", "json", str(video)]
    try:
        completed = subprocess.run(command, capture_output=True, check=True, text=True)
    except FileNotFoundError as error:
        raise PrepError("ffprobe not found; install FFmpeg") from error
    except subprocess.CalledProcessError as error:
        raise PrepError(f"cannot read {video}: {error.stderr.strip()}") from error
    try:
        info = json.loads(completed.stdout)
        stream = info["streams"][0]
        times = sorted(float(packet["pts_time"]) for packet in info["packets"])
        created = info.get("format", {}).get("tags", {}).get("creation_time")
        return int(stream["width"]), int(stream["height"]), [time - times[0] for time in times], created
    except (KeyError, IndexError, TypeError, ValueError) as error:
        raise PrepError(f"{video.name}: no video stream with frame times") from error
