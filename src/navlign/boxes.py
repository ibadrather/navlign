"""Read the user data box (moov/udta) of an MP4: what DJI writes there and ffprobe does not show.

DJI writes the attitude of the aircraft and the gimbal and the speed at the start of the recording as text atoms,
and the camera model and serial number as plain strings. Atoms whose layout is not known are kept as hex.
"""

import struct
from collections.abc import Iterator
from pathlib import Path
from typing import BinaryIO

from navlign.images import Json

# Known DJI atoms: (what it is, unit, file it belongs to). nav: where the aircraft was and how it was pointing.
ATOMS: dict[str, tuple[str, str, str]] = {
    "©xyz": ("location at the start, ISO 6709", "", "nav"),
    "©xsp": ("speed x at the start", "m/s", "nav"),
    "©ysp": ("speed y at the start", "m/s", "nav"),
    "©zsp": ("speed z at the start", "m/s", "nav"),
    "©fpt": ("aircraft pitch at the start", "°", "nav"),
    "©fyw": ("aircraft yaw at the start", "°", "nav"),
    "©frl": ("aircraft roll at the start", "°", "nav"),
    "©gpt": ("gimbal pitch at the start", "°", "nav"),
    "©gyw": ("gimbal yaw at the start", "°", "nav"),
    "©grl": ("gimbal roll at the start", "°", "nav"),
    "©mdl": ("camera model", "", "camera"),
    "©csn": ("camera serial number", "", "camera"),
    "©res": ("video mode: width, height, frame rate", "", "camera"),
    "©mdt": ("size of the media data (mdat)", "bytes", "camera"),
    "Xtra": ("Windows media attributes", "", "camera"),
}
SKIPPED = {"meta", "free"}  # meta/ilst holds the comment, which ffprobe reads


def read_udta(path: Path) -> dict[str, Json]:
    """Each atom of moov/udta: what it is, its value where the layout is known, and its bytes as hex."""
    atoms: dict[str, Json] = {}
    with path.open("rb") as file:
        size = file.seek(0, 2)
        moov = next(((start, end) for kind, start, end in _boxes(file, 0, size) if kind == "moov"), None)
        if moov is None:
            return atoms
        udta = next(((start, end) for kind, start, end in _boxes(file, *moov) if kind == "udta"), None)
        if udta is None:
            return atoms
        for kind, start, end in _boxes(file, *udta):
            if kind in SKIPPED:
                continue
            file.seek(start)
            data = file.read(end - start)
            meaning, unit, group = ATOMS.get(kind, ("not known", "", "camera"))
            atom: Json = {"meaning": meaning, "group": group}
            if unit:
                atom["unit"] = unit
            if (value := _value(kind, data)) is not None:
                atom["value"] = value
            atom["hex"] = data.hex()
            atoms[kind] = atom
    return atoms


def _boxes(file: BinaryIO, start: int, end: int) -> Iterator[tuple[str, int, int]]:
    """The boxes between ``start`` and ``end``: type, start of the payload, end."""
    position = start
    while position + 8 <= end:
        file.seek(position)
        size, kind = struct.unpack(">I4s", file.read(8))
        header = 8
        if size == 1:
            size, header = struct.unpack(">Q", file.read(8))[0], 16
        elif size == 0:
            size = end - position
        if size < header:
            return
        yield kind.decode("latin-1"), position + header, position + size
        position += size


def _value(kind: str, data: bytes) -> object:
    """The value of an atom whose layout is known, else None."""
    if kind == "©res" and len(data) >= 8:
        width, height, rate = struct.unpack(">HHI", data[:8])
        return {"width": width, "height": height, "frame_rate": rate}
    if kind == "©mdt" and len(data) >= 8:
        return struct.unpack(">I", data[4:8])[0]
    if kind == "Xtra":
        return _xtra(data)
    # QuickTime text: length, language, text.
    if len(data) >= 4 and (length := struct.unpack(">H", data[:2])[0]) and 4 + length <= len(data):
        text = data[4 : 4 + length].rstrip(b"\0").decode("utf-8", "replace")
        if text.isprintable():
            try:
                return text if kind == "©xyz" else float(text)
            except ValueError:
                return text
    # Null padded string, as the camera model and serial number.
    text = data.rstrip(b"\0")
    if text and b"\0" not in text and text.isascii() and text.decode().isprintable():
        return text.decode()
    return None


def _xtra(data: bytes) -> dict[str, list[str]] | None:
    """Microsoft Xtra attributes: name and UTF-16 string values; None when the layout is not that."""
    attributes: dict[str, list[str]] = {}
    position = 0
    try:
        while position + 8 <= len(data):
            size, name_length = struct.unpack(">II", data[position : position + 8])
            if size == 0:
                break
            name = data[position + 8 : position + 8 + name_length].decode("ascii")
            cursor = position + 8 + name_length
            (count,) = struct.unpack(">I", data[cursor : cursor + 4])
            cursor += 4
            values: list[str] = []
            for _ in range(count):
                value_size, value_type = struct.unpack(">IH", data[cursor : cursor + 6])
                payload = data[cursor + 6 : cursor + value_size]
                values.append(payload.decode("utf-16-le").rstrip("\0") if value_type == 8 else payload.hex())
                cursor += value_size
            attributes[name] = values
            position += size
    except (struct.error, UnicodeDecodeError):
        return None
    return attributes or None
