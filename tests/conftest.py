"""Shared fixtures: small JPEGs with the EXIF and DJI XMP metadata real drone images carry."""

from collections.abc import Callable
from pathlib import Path

import pytest
from PIL import ExifTags, Image

type MakeJpeg = Callable[..., Path]

DJI_XMP = (
    '<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
    '<rdf:Description rdf:about="DJI Meta Data" xmlns:drone-dji="http://www.dji.com/drone-dji/1.0/"'
    ' drone-dji:AbsoluteAltitude="+{absolute}" drone-dji:RelativeAltitude="+{relative}"'
    ' drone-dji:GimbalRollDegree="+0.00" drone-dji:GimbalYawDegree="{yaw:+.2f}" drone-dji:GimbalPitchDegree="-89.90">'
    "</rdf:Description></rdf:RDF></x:xmpmeta>"
)


def _dms(value: float) -> tuple[float, float, float]:
    degrees = int(value)
    minutes = int((value - degrees) * 60)
    return (float(degrees), float(minutes), (value - degrees - minutes / 60) * 3600)


@pytest.fixture
def make_jpeg() -> MakeJpeg:
    """Return a function that writes a tiny JPEG with drone-like metadata. lat=None writes no GPS."""

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
            xmp = DJI_XMP.format(absolute=gps_altitude, relative=relative_altitude, yaw=yaw).encode()
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", size, (90, 120, 60)).save(path, exif=exif, xmp=xmp)
        return path

    return make
