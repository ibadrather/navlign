"""Print what ``inspect`` read from a video, with rich."""

import itertools
import math
from datetime import UTC, datetime

from rich import box
from rich.console import Console
from rich.table import Table

from navlign.metadata import FIELDS, VideoMetadata, decimals, track_length_m


def print_metadata(console: Console, metadata: VideoMetadata, rows: int) -> None:
    """Print the container, the user data, the streams, the frames and the telemetry of a video."""
    console.rule(f"[bold]{metadata.path.name}")
    _print_container(console, metadata)
    _print_user_data(console, metadata)
    _print_streams(console, metadata)
    _print_frames(console, metadata)
    if not metadata.entries:
        console.print(f"[yellow]no telemetry:[/yellow] {metadata.telemetry_error}")
        return
    _print_flight(console, metadata)
    _print_fields(console, metadata)
    _print_entries(console, metadata, metadata.nav_fields, "Navigation entries", rows)
    _print_entries(console, metadata, metadata.camera_fields, "Camera entries", rows)
    console.print(f"[dim]raw text of entry 0:[/] {metadata.entries[0].text}")
    console.print()


def _key_values(title: str) -> Table:
    """A table of names and values."""
    table = Table(title=title, title_justify="left", title_style="bold", show_header=False, box=None)
    table.add_column(style="cyan", no_wrap=True)
    table.add_column(overflow="fold")
    return table


def _print_container(console: Console, metadata: VideoMetadata) -> None:
    """The file, the container and its tags, with the start time, device and location read out."""
    container = metadata.container
    size = metadata.path.stat().st_size
    table = _key_values("Container")
    table.add_row("file", str(metadata.path.resolve()))
    table.add_row("size", f"{size / 1e9:.2f} GB ({size:,} bytes)")
    table.add_row("format", f"{container.get('format_long_name', '')} ({container.get('format_name', '')})")
    if duration := container.get("duration"):
        minutes, seconds = divmod(float(str(duration)), 60)
        table.add_row("duration", f"{float(str(duration)):.3f} s ({minutes:.0f}:{seconds:06.3f})")
    if bit_rate := container.get("bit_rate"):
        table.add_row("bit rate", f"{int(str(bit_rate)) / 1e6:.2f} Mb/s")
    for key, value in container.items():
        if key not in {"format_long_name", "format_name", "duration", "bit_rate", "size", "tags"}:
            table.add_row(key.replace("_", " "), str(value))
    for key, value in metadata.tags.items():
        table.add_row(f"tag {key}", str(value))
    if start := metadata.start_utc:
        check = ""
        try:
            born = datetime.fromtimestamp(metadata.path.stat().st_birthtime, UTC)
            check = f", the file was created {(born - start).total_seconds():+.0f} s from it"
        except AttributeError:
            pass
        table.add_row("[bold]start of recording[/]", f"{metadata.time_utc(0)}{check}")
    if location := metadata.location:
        table.add_row("[bold]location[/]", f"{location[0]:+.4f}, {location[1]:+.4f}  (lat, lon at the start)")
    for key, value in metadata.device.items():
        name = {"SN": "serial number", "Type": "type", "Mode": "mode"}.get(key, key)
        table.add_row(f"[bold]device {name}[/]", value)
    console.print(table)
    console.print()


def _print_user_data(console: Console, metadata: VideoMetadata) -> None:
    """The atoms of the user data box, the attitude at the start first."""
    if not metadata.udta:
        return
    table = Table(title="User data (moov/udta)", title_justify="left", title_style="bold", box=box.SIMPLE_HEAD)
    for heading in ("atom", "file", "meaning", "value"):
        table.add_column(heading, overflow="fold")
    atoms = sorted(metadata.udta.items(), key=lambda item: item[1]["group"] != "nav")
    for kind, atom in atoms:
        if "value" in atom:
            value = f"{atom['value']} {atom.get('unit', '')}".strip()
        else:
            raw = str(atom["hex"])
            value = f"[dim]{raw[:48]}{'…' if len(raw) > 48 else ''}[/]"
        table.add_row(f"[cyan]{kind}[/]", str(atom["group"]), str(atom["meaning"]), value)
    console.print(table)
    console.print()


def _print_streams(console: Console, metadata: VideoMetadata) -> None:
    """One row per stream, with what it holds, then the tags of each."""
    data = {stream.index: stream for stream in metadata.data_streams}
    table = Table(title="Streams", title_justify="left", title_style="bold", box=box.SIMPLE_HEAD)
    for heading in ("#", "type", "codec", "handler", "duration", "holds", "flags"):
        table.add_column(heading, justify="right" if heading == "#" else "left")
    for stream in metadata.streams:
        kind = stream.get("codec_type", "")
        holds: list[str] = []
        if kind == "video":
            holds.append(f"{stream.get('width')}x{stream.get('height')}")
            numerator, _, denominator = str(stream.get("avg_frame_rate", "0/0")).partition("/")
            if float(denominator or 0):
                holds.append(f"{float(numerator) / float(denominator):.3f} fps")
            holds += [f"{stream.get('nb_frames')} frames", str(stream.get("pix_fmt", ""))]
            holds.append(f"{stream.get('profile', '')} level {stream.get('level', '')}")
            holds.append(f"colour {stream.get('color_space', '?')}/{stream.get('color_range', '?')}")
            if bit_rate := stream.get("bit_rate"):
                holds.append(f"{int(str(bit_rate)) / 1e6:.1f} Mb/s")
        elif kind == "audio":
            holds += [f"{stream.get('sample_rate')} Hz", f"{stream.get('channels')} channels"]
        elif kind == "subtitle":
            holds.append(f"{stream.get('nb_frames')} entries")
            if metadata.telemetry_source == "subtitle stream":
                holds.append("[green]the telemetry[/]")
        elif kind == "data" and (packets := data.get(int(str(stream["index"])))):
            holds.append(f"{packets.packets} packets, {packets.size_bytes:,} bytes")
            if packets.packets and packets.distinct == 1:
                holds.append("[yellow]all identical: filler, no data[/]")
            else:
                holds.append(f"{packets.distinct} distinct, binary, not decoded")
        duration = stream.get("duration")
        disposition = stream.get("disposition", [])
        table.add_row(
            str(stream.get("index")),
            str(kind),
            str(stream.get("codec_name") or stream.get("codec_tag_string", "")),
            str(_tags(stream).get("handler_name", "")),
            f"{float(str(duration)):.3f} s" if duration else "",
            ", ".join(item for item in holds if item),
            ", ".join(disposition) if isinstance(disposition, list) else "",
        )
    console.print(table)
    tags = _key_values("Stream tags")
    for stream in metadata.streams:
        for key, value in _tags(stream).items():
            tags.add_row(f"#{stream.get('index')} {key}", str(value))
    console.print(tags)
    console.print()


def _print_frames(console: Console, metadata: VideoMetadata) -> None:
    """The video frames as a whole: how many, key frames, sizes, how they line up with the telemetry."""
    frames = metadata.frames
    if not frames:
        return
    keys = [number for number, frame in enumerate(frames) if frame.key]
    gaps = [b - a for a, b in itertools.pairwise(keys)]
    sizes = [frame.size_bytes for frame in frames]
    table = _key_values("Frames")
    table.add_row("frames", f"{len(frames)}, {frames[0].time_s:.3f} s to {frames[-1].time_s:.3f} s")
    steps = [b.time_s - a.time_s for a, b in itertools.pairwise(frames)]
    if steps:
        table.add_row("frame interval", f"{min(steps) * 1000:.3f} to {max(steps) * 1000:.3f} ms")
    table.add_row("key frames", f"{len(keys)}, every {sum(gaps) / len(gaps):.1f} frames" if gaps else str(len(keys)))
    table.add_row("frame size", f"{min(sizes):,} to {max(sizes):,} bytes, {sum(sizes) / len(sizes):,.0f} on average")
    linked = [entry for entry in metadata.frame_entries() if entry is not None]
    if metadata.entries:
        table.add_row("with telemetry", f"{len(linked)} of {len(frames)} frames fall in a telemetry entry")
    console.print(table)
    console.print()


def _print_flight(console: Console, metadata: VideoMetadata) -> None:
    """The telemetry as a whole: where it comes from, how often, and the flight it describes."""
    entries, fields = metadata.entries, metadata.fields
    table = _key_values("Telemetry")
    table.add_row("source", metadata.telemetry_source or "")
    steps = [entry.end_s - entry.start_s for entry in entries]
    span = f"{entries[0].start_s:.3f} s to {entries[-1].end_s:.3f} s"
    table.add_row("entries", f"{len(entries)}, every {sum(steps) / len(steps):.3f} s, {span}")
    if clocks := [entry for entry in entries if entry.clock]:
        table.add_row("drone clock", f"{clocks[0].clock}.{clocks[0].clock_ms:03d} to {clocks[-1].clock}")
    fixes = [entry for entry in entries if entry.fields.get("latitude") and entry.fields.get("longitude")]
    if fixes:
        lats = [entry.fields["latitude"] for entry in fixes]
        lons = [entry.fields["longitude"] for entry in fixes]
        places = min(decimals(lats), decimals(lons))
        step_m = 10**-places * 111_195
        precision = f"written to {places} decimals: steps of {step_m:.1f} m north-south, "
        precision += f"{step_m * math.cos(math.radians(lats[0])):.1f} m east-west"
        table.add_row("positions", f"{len(fixes)} of {len(entries)} with a fix, {precision}")
        table.add_row("start", f"{lats[0]}, {lons[0]}")
        table.add_row("end", f"{lats[-1]}, {lons[-1]}")
        table.add_row("extent", f"lat {min(lats)} to {max(lats)}, lon {min(lons)} to {max(lons)}")
        table.add_row("track", f"{track_length_m(entries):.0f} m through the positions")
    if "speed_ms" in fields:
        flown = sum(entry.fields.get("speed_ms", 0) * (entry.end_s - entry.start_s) for entry in entries)
        table.add_row("flown", f"{flown:.0f} m from the horizontal speed")
    if height := metadata.height_field:
        heights = [entry.fields[height] for entry in entries if height in entry.fields]
        meaning, unit = FIELDS[height]
        table.add_row("height", f"{min(heights):g} to {max(heights):g} {unit}, {meaning} ({height})")
    attitude = [field for field in fields if field.startswith("gb_")]
    table.add_row("attitude", ", ".join(attitude) if attitude else "[yellow]none per entry[/], only at the start")
    console.print(table)
    console.print()


def _print_fields(console: Console, metadata: VideoMetadata) -> None:
    """Every telemetry value: the file it goes to, what it is and the range it took."""
    table = Table(title="Telemetry values", title_justify="left", title_style="bold", box=box.SIMPLE_HEAD)
    for heading in ("field", "file", "meaning", "unit", "entries", "first", "min", "max", "last"):
        numeric = heading in {"entries", "first", "min", "max", "last"}
        table.add_column(heading, justify="right" if numeric else "left", no_wrap=heading == "field")
    for group, fields in (("nav", metadata.nav_fields), ("camera", metadata.camera_fields)):
        for field in fields:
            values = [entry.fields[field] for entry in metadata.entries if field in entry.fields]
            meaning, unit = FIELDS.get(field, ("", ""))
            ends = (values[0], min(values), max(values), values[-1])
            table.add_row(f"[cyan]{field}[/]", group, meaning, unit, str(len(values)), *map(_number, ends))
    console.print(table)
    console.print()


def _print_entries(console: Console, metadata: VideoMetadata, fields: list[str], title: str, rows: int) -> None:
    """The telemetry entries with ``fields``, the first and last ``rows`` / 2 of them, or all with 0."""
    entries = metadata.entries
    if not fields:
        return
    shown: list[int | None] = list(range(len(entries)))
    if rows and len(entries) > rows:
        shown = [*range((rows + 1) // 2), None, *range(len(entries) - rows // 2, len(entries))]
    table = Table(title=f"{title} ({len(entries)})", title_justify="left", title_style="bold", box=box.SIMPLE_HEAD)
    for heading in ("#", "start s", "time utc", *fields):
        table.add_column(heading.replace("_", " "), justify="right")
    for index in shown:
        if index is None:
            table.add_row("…")
            table.caption = f"{len(entries) - rows} more not shown; --rows 0 shows all"
            continue
        entry = entries[index]
        values = [_number(entry.fields[field]) if field in entry.fields else "" for field in fields]
        time = metadata.time_utc(entry.start_s)
        table.add_row(str(index), f"{entry.start_s:.3f}", time[11:] if time else "", *values)
    console.print(table)


def _tags(stream: dict[str, object]) -> dict[str, object]:
    """The tags of a stream."""
    tags = stream.get("tags", {})
    return tags if isinstance(tags, dict) else {}


def _number(value: float) -> str:
    """``value`` as written, without trailing zeros."""
    return f"{value:.10g}"
