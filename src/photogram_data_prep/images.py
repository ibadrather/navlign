"""Prepare a dataset (manifest.geojson + images/) from a directory of geotagged JPEG images.

Position comes from EXIF GPS, camera attitude from the XMP block DJI cameras write.
"""

import json
import math
import re
import shutil
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import SupportsFloat
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from PIL import ExifTags, Image

# Sensor widths of cameras whose EXIF does not give them reliably, keyed by EXIF (make, model).
SENSOR_WIDTH_MM = {("DJI", "FC300S"): 6.17}  # Phantom 3 Professional / Advanced
FULL_FRAME_DIAGONAL_MM = math.hypot(36.0, 24.0)

type Json = dict[str, object]


class PrepError(Exception):
    """The input cannot be turned into a dataset. The message says why."""


class AltitudeMode(StrEnum):
    """relative: height above take-off (DJI XMP). absolute: EXIF GPS altitude, datum must be stated."""

    RELATIVE = "relative"
    ABSOLUTE = "absolute"


class VerticalDatum(StrEnum):
    """Datum of absolute altitudes."""

    ELLIPSOID = "ellipsoid"
    EGM96 = "egm96"


class Shutter(StrEnum):
    """Sensor readout. EXIF does not record it."""

    ROLLING = "rolling"
    GLOBAL = "global"


@dataclass(frozen=True)
class PrepResult:
    """What was written."""

    manifest_path: Path
    frames: int
    located: int
    altitude_reference: str
    cameras: list[str]


def prepare_images(
    source_dir: Path,
    dataset_dir: Path,
    *,
    altitude: AltitudeMode = AltitudeMode.RELATIVE,
    vertical_datum: VerticalDatum | None = None,
    timezone: str | None = None,
    sensor_width_mm: float | None = None,
    shutter: Shutter = Shutter.ROLLING,
    attribution: Sequence[Path] = (),
    overwrite: bool = False,
) -> PrepResult:
    """Write dataset_dir/manifest.geojson and copy the JPEGs of source_dir to dataset_dir/images/.

    timezone is the IANA zone of the camera clock; without it no time_utc is written. Every image is read
    before anything is written.
    """
    if altitude is AltitudeMode.RELATIVE and vertical_datum is not None:
        raise PrepError("a vertical datum only applies to absolute altitudes")
    if altitude is AltitudeMode.ABSOLUTE and vertical_datum is None:
        raise PrepError("absolute altitudes need their vertical datum (ellipsoid or egm96)")
    altitude_reference = str(vertical_datum) if vertical_datum else "relative_takeoff"
    try:
        zone = ZoneInfo(timezone) if timezone else None
    except (ZoneInfoNotFoundError, ValueError) as error:
        raise PrepError(f"unknown time zone {timezone!r}") from error

    paths = sorted(
        path for path in source_dir.iterdir() if path.suffix.lower() in {".jpg", ".jpeg"} and path.name[0] != "."
    )
    if not paths:
        raise PrepError(f"no JPEG images in {source_dir}")

    cameras: dict[str, Json] = {}
    features: list[Json] = []
    for path in paths:
        camera_id, camera, feature = _read_image(
            path, altitude=altitude, zone=zone, sensor_width_mm=sensor_width_mm, shutter=shutter
        )
        cameras.setdefault(camera_id, camera)
        features.append(feature)

    manifest_path = dataset_dir / "manifest.geojson"
    images_dir = dataset_dir / "images"
    if (manifest_path.exists() or images_dir.exists()) and not overwrite:
        raise PrepError(f"{dataset_dir} already holds a dataset; pass --overwrite to replace it")
    manifest_path.unlink(missing_ok=True)
    shutil.rmtree(images_dir, ignore_errors=True)
    images_dir.mkdir(parents=True)
    for path in paths:
        shutil.copy2(path, images_dir / path.name)
    for path in attribution:
        shutil.copy2(path, dataset_dir / path.name)

    manifest = {
        "type": "FeatureCollection",
        "photogrammetry": {
            "schema_version": "0.1.0",
            "source": "images",
            "altitude_reference": altitude_reference,
            # Gimbal attitude in degrees: yaw clockwise from north, pitch -90 = nadir, roll right side down.
            "orientation_convention": "ned_yaw_pitch_roll_deg",
            "cameras": cameras,
        },
        "features": features,
    }
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return PrepResult(
        manifest_path=manifest_path,
        frames=len(features),
        located=sum(feature["geometry"] is not None for feature in features),
        altitude_reference=altitude_reference,
        cameras=list(cameras),
    )


def _read_image(
    path: Path,
    *,
    altitude: AltitudeMode,
    zone: ZoneInfo | None,
    sensor_width_mm: float | None,
    shutter: Shutter,
) -> tuple[str, Json, Json]:
    """Return the camera id, the camera entry and the GeoJSON feature of one image."""
    try:
        with Image.open(path) as image:
            width, height = image.size
            exif = image.getexif()
            xmp = image.info.get("xmp", b"").decode(errors="ignore")
    except OSError as error:
        raise PrepError(f"cannot read {path}: {error}") from error
    exif_ifd = exif.get_ifd(ExifTags.IFD.Exif)
    gps = exif.get_ifd(ExifTags.IFD.GPSInfo)
    dji = dict(re.findall(r'drone-dji:(\w+)="([^"]*)"', xmp))

    # Camera
    make = _text(exif.get(ExifTags.Base.Make))
    model = _text(exif.get(ExifTags.Base.Model))
    focal_mm = _float(exif_ifd.get(ExifTags.Base.FocalLength))
    if focal_mm is None:
        raise PrepError(f"{path.name}: no focal length in EXIF")
    sensor_mm = sensor_width_mm or SENSOR_WIDTH_MM.get((make, model))
    if sensor_mm is None:
        focal_35mm = _float(exif_ifd.get(ExifTags.Base.FocalLengthIn35mmFilm))
        if not focal_35mm:
            raise PrepError(f"{path.name}: sensor width of {make} {model} is unknown; pass --sensor-width-mm")
        sensor_mm = FULL_FRAME_DIAGONAL_MM * focal_mm / focal_35mm * width / math.hypot(width, height)
    name = f"{make or 'unknown'}_{model or 'unknown'}_{width}x{height}_{focal_mm:g}mm"
    camera_id = re.sub(r"[^a-z0-9.x]+", "_", name.lower())
    camera: Json = {
        "width": width,
        "height": height,
        "focal_length_mm": focal_mm,
        "sensor_width_mm": round(sensor_mm, 3),
        "shutter": str(shutter),
    }

    # Position; DJI writes 0/0 when there was no fix.
    lat = _degrees(gps.get(ExifTags.GPS.GPSLatitude), gps.get(ExifTags.GPS.GPSLatitudeRef))
    lon = _degrees(gps.get(ExifTags.GPS.GPSLongitude), gps.get(ExifTags.GPS.GPSLongitudeRef))
    geometry: Json | None = None
    if lat is not None and lon is not None and (lat, lon) != (0, 0):
        if altitude is AltitudeMode.RELATIVE:
            alt = _float(dji.get("RelativeAltitude"))
        else:
            alt = _float(gps.get(ExifTags.GPS.GPSAltitude))
            if alt is not None and gps.get(ExifTags.GPS.GPSAltitudeRef) in {1, b"\x01"}:
                alt = -alt
        if alt is None:
            raise PrepError(f"{path.name}: has a position but no {altitude} altitude; try --altitude absolute")
        geometry = {"type": "Point", "coordinates": [round(lon, 9), round(lat, 9), round(alt, 3)]}

    properties: Json = {"image": path.name, "camera": camera_id}
    taken_at = _text(exif_ifd.get(ExifTags.Base.DateTimeOriginal))
    if zone and taken_at:
        local = datetime.strptime(taken_at, "%Y:%m:%d %H:%M:%S").replace(tzinfo=zone)
        properties["time_utc"] = local.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    for key, tag in (("yaw", "GimbalYawDegree"), ("pitch", "GimbalPitchDegree"), ("roll", "GimbalRollDegree")):
        if (angle := _float(dji.get(tag))) is not None:
            properties[key] = angle

    return camera_id, camera, {"type": "Feature", "geometry": geometry, "properties": properties}


def _float(value: object) -> float | None:
    """An EXIF rational or an XMP string like "+39.80" as a float; None if absent or not finite."""
    if not isinstance(value, str | SupportsFloat):
        return None
    try:
        number = float(value)
    except ValueError:
        return None
    return number if math.isfinite(number) else None


def _degrees(dms: object, ref: object) -> float | None:
    """An EXIF (degrees, minutes, seconds) triple and its N/S/E/W ref as signed decimal degrees."""
    if not isinstance(dms, tuple):
        return None
    parts = [_float(part) for part in dms]
    if len(parts) != 3 or None in parts:
        return None
    value = sum((part or 0.0) / 60**i for i, part in enumerate(parts))
    return -value if _text(ref) in {"S", "W"} else value


def _text(value: object) -> str | None:
    """An EXIF string without the NUL padding DJI adds."""
    if isinstance(value, bytes):
        value = value.decode("ascii", "replace")
    if not isinstance(value, str):
        return None
    return value.strip("\x00 ") or None
