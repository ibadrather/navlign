"""Shared fixtures: small JPEGs with the EXIF and DJI XMP metadata real drone images carry, and flight logs."""

import csv
import json
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from PIL import ExifTags, Image

type MakeJpeg = Callable[..., Path]

DJI_XMP = (
    '<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
    '<rdf:Description rdf:about="DJI Meta Data" xmlns:{p}="http://www.dji.com/drone-dji/1.0/"'
    ' {p}:AbsoluteAltitude="+{absolute}" {p}:RelativeAltitude="+{relative}"'
    ' {p}:GimbalRollDegree="+0.00" {p}:GimbalYawDegree="{yaw:+.2f}" {p}:GimbalPitchDegree="-89.90"{rtk}>'
    "</rdf:Description></rdf:RDF></x:xmpmeta>"
)


def _dms(value: float) -> tuple[float, float, float]:
    degrees = int(value)
    minutes = int((value - degrees) * 60)
    return (float(degrees), float(minutes), (value - degrees - minutes / 60) * 3600)


@pytest.fixture
def make_jpeg() -> MakeJpeg:
    """Return a function that writes a tiny JPEG with drone-like metadata. lat=None writes no GPS.

    xmp_prefix="drone" writes the XMP as Autel does; rtk_std=(lat, lon, height) adds the RTK accuracies.
    """

    def make(
        path: Path,
        *,
        lat: float | None = 46.8426,
        lon: float = -91.9945,
        gps_altitude: float = 198.3,
        relative_altitude: float | None = 39.8,
        yaw: float = 45.0,
        make: str = "DJI\x00\x00",
        model: str = "FC300S\x00\x00",
        focal_length: float = 3.61,
        size: tuple[int, int] = (64, 36),
        xmp_prefix: str = "drone-dji",
        rtk_std: tuple[float, float, float] | None = None,
    ) -> Path:
        exif = Image.Exif()
        exif[ExifTags.Base.Make] = make
        exif[ExifTags.Base.Model] = model
        exif_ifd = exif.get_ifd(ExifTags.IFD.Exif)
        exif_ifd[ExifTags.Base.FocalLength] = focal_length
        exif_ifd[ExifTags.Base.FocalLengthIn35mmFilm] = 20
        exif_ifd[ExifTags.Base.DateTimeOriginal] = "2016:06:23 16:31:59"
        if lat is not None:
            gps = exif.get_ifd(ExifTags.IFD.GPSInfo)
            gps[ExifTags.GPS.GPSLatitudeRef] = "N" if lat >= 0 else "S"
            gps[ExifTags.GPS.GPSLatitude] = _dms(abs(lat))
            gps[ExifTags.GPS.GPSLongitudeRef] = "E" if lon >= 0 else "W"
            gps[ExifTags.GPS.GPSLongitude] = _dms(abs(lon))
            gps[ExifTags.GPS.GPSAltitudeRef] = b"\x00"
            gps[ExifTags.GPS.GPSAltitude] = gps_altitude
        xmp = b""
        if relative_altitude is not None:
            rtk = ""
            if rtk_std is not None:
                rtk = "".join(
                    f' {xmp_prefix}:{tag}="{value:.5f}"'
                    for tag, value in zip(("RtkStdLat", "RtkStdLon", "RtkStdHgt"), rtk_std, strict=True)
                )
            xmp = DJI_XMP.format(
                p=xmp_prefix, absolute=gps_altitude, relative=relative_altitude, yaw=yaw, rtk=rtk
            ).encode()
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", size, (90, 120, 60)).save(path, exif=exif, xmp=xmp)
        return path

    return make


type MakeLog = Callable[..., Path]
LOG_HZ = 10
EAST_M_PER_DEGREE = 111_195.0 * 0.6  # at 53° north


@pytest.fixture
def make_log() -> MakeLog:
    """Return a function that writes a flight flown east from (lon, lat) at ``speeds``, one every 0.1 s.

    The file name says what is written: a DJI flight log as frames-N.csv or flown-N_high_res.geojson, or the fixes of
    Swathwise as flown-N.geojson, which keeps one point in 13 and 7 decimals.
    """

    def make(
        path: Path, start: datetime, speeds: Sequence[float], *, lon: float = 8.8354, lat: float = 53.1084
    ) -> Path:
        rows: list[dict[str, object]] = []
        east_m = 0.0
        for number, speed in enumerate(speeds):
            rows.append(
                {
                    "CUSTOM.dateTime": (start + timedelta(seconds=number / LOG_HZ)).isoformat().replace("+00:00", "Z"),
                    "OSD.latitude": lat,
                    "OSD.longitude": lon + east_m / EAST_M_PER_DEGREE,
                    "OSD.height": 10.0,
                    "OSD.xSpeed": 0.0,
                    "OSD.ySpeed": speed,
                    "OSD.zSpeed": 0.0,
                    "OSD.pitch": -4.0,
                    "OSD.roll": 1.0,
                    "OSD.yaw": 92.0,
                    "OSD.gpsNum": 21,
                    "GIMBAL.pitch": -90.0,
                    "GIMBAL.roll": 0.0,
                    "GIMBAL.yaw": 90.5,
                    "CAMERA.isVideo": "true",
                }
            )
            east_m += speed / LOG_HZ
        path.parent.mkdir(parents=True, exist_ok=True)
        first, last = rows[0]["CUSTOM.dateTime"], rows[-1]["CUSTOM.dateTime"]
        if path.suffix == ".csv":
            with path.open("w", newline="") as file:
                writer = csv.DictWriter(file, list(rows[0]))
                writer.writeheader()
                writer.writerows(rows)
            return path
        if "high_res" in path.name:
            summary = {"kind": "frames", "format": "swathwise-high-res", "started": first, "ended": last}
            points = [([row["OSD.longitude"], row["OSD.latitude"]], row) for row in rows]
        else:
            summary = {"kind": "flight", "format": "swathwise-flight", "started": first, "ended": last}
            points = [
                (
                    [round(float(str(row["OSD.longitude"])), 7), row["OSD.latitude"]],
                    {
                        "time": row["CUSTOM.dateTime"],
                        "height_m": 10.0,
                        "heading_deg": 92.0,
                        "speed_north_ms": 0.0,
                        "speed_east_ms": row["OSD.ySpeed"],
                        "recording": True,
                        "satellites": 21,
                    },
                )
                for row in rows[::13]
            ]
        features = [{"type": "Feature", "geometry": None, "properties": summary}]
        features += [
            {"type": "Feature", "geometry": {"type": "Point", "coordinates": at}, "properties": properties}
            for at, properties in points
        ]
        lines = ",\n".join(json.dumps(feature, separators=(",", ":")) for feature in features)
        path.write_text('{"type":"FeatureCollection","features":[\n' + lines + "\n]}\n")
        return path

    return make
