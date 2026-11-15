"""Command line interface."""

from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console, Group
from rich.panel import Panel
from rich.table import Table

from navlign.camera import ACCURACY, Camera, measure_camera
from navlign.checks import Check, Level
from navlign.images import AltitudeMode, PrepError, PrepResult, Shutter, VerticalDatum, prepare_images
from navlign.metadata import output_paths, read_metadata, write_metadata
from navlign.nav import NAV_ENV, nav_files, nav_kind, nav_places
from navlign.report import print_metadata
from navlign.scan import scan_clip
from navlign.survey import REPORT_NAME, VIDEO_NAV, ClipResult, check_survey, list_videos, prepare_survey
from navlign.video import prepare_video

app = typer.Typer(no_args_is_help=True, add_completion=False)
console = Console()
COLOURS = {Level.PASS: "green", Level.WARN: "yellow", Level.FAIL: "red"}
NAV_HELP = f"Flight log or mission folder; repeatable. Default: searched for in the video folders and {NAV_ENV}."


@app.callback()
def main() -> None:
    """Turn raw drone and camera data into a photogrammetry dataset (manifest.geojson + media)."""


@app.command()
def images(
    source: Annotated[Path, typer.Argument(exists=True, file_okay=False, help="Directory of JPEG images.")],
    *,
    output: Annotated[Path, typer.Option("--output", "-o", help="Dataset directory to write.")],
    altitude: Annotated[
        AltitudeMode,
        typer.Option(help="relative: height above take-off. absolute: GPS altitude (needs --vertical-datum)."),
    ] = AltitudeMode.RELATIVE,
    vertical_datum: Annotated[
        VerticalDatum | None,
        typer.Option(help="Datum of absolute altitudes; check your device, DJI models differ."),
    ] = None,
    timezone: Annotated[
        str | None,
        typer.Option(help="IANA time zone of the camera clock, e.g. America/Chicago. Enables time_utc."),
    ] = None,
    sensor_width_mm: Annotated[
        float | None,
        typer.Option(min=0.1, help="Override the sensor width when it is unknown or wrong."),
    ] = None,
    shutter: Annotated[Shutter, typer.Option(help="Sensor readout; not recorded in EXIF.")] = Shutter.ROLLING,
    attribution: Annotated[
        list[Path] | None,
        typer.Option(exists=True, dir_okay=False, help="Licence or credits file to copy along; repeatable."),
    ] = None,
    overwrite: Annotated[bool, typer.Option(help="Replace an existing manifest and images directory.")] = False,
) -> None:
    """Prepare a dataset from geotagged still images (EXIF GPS, DJI XMP attitude)."""
    try:
        result = prepare_images(
            source,
            output,
            altitude=altitude,
            vertical_datum=vertical_datum,
            timezone=timezone,
            sensor_width_mm=sensor_width_mm,
            shutter=shutter,
            attribution=attribution or (),
            overwrite=overwrite,
        )
    except PrepError as error:
        console.print(f"[red]error:[/red] {error}")
        raise typer.Exit(1) from error
    _report(result)


@app.command()
def video(
    source: Annotated[Path, typer.Argument(exists=True, dir_okay=False, help="DJI video file (MP4 / MOV).")],
    *,
    output: Annotated[Path, typer.Option("--output", "-o", help="Dataset directory to write.")],
    srt: Annotated[
        Path | None,
        typer.Option(exists=True, dir_okay=False, help="SRT telemetry; default: the .SRT next to the video."),
    ] = None,
    altitude: Annotated[
        AltitudeMode,
        typer.Option(help="relative: height above take-off. absolute: SRT abs_alt (needs --vertical-datum)."),
    ] = AltitudeMode.RELATIVE,
    vertical_datum: Annotated[
        VerticalDatum | None,
        typer.Option(help="Datum of absolute altitudes; check your device, DJI models differ."),
    ] = None,
    timezone: Annotated[
        str | None,
        typer.Option(help="IANA time zone of the drone clock, e.g. Europe/Rome. Enables time_utc."),
    ] = None,
    focal_length_mm: Annotated[
        float | None,
        typer.Option(min=0.1, help="Physical focal length; with --sensor-width-mm replaces the SRT 35 mm equivalent."),
    ] = None,
    sensor_width_mm: Annotated[
        float | None,
        typer.Option(min=0.1, help="Width of the sensor area the video uses; with --focal-length-mm."),
    ] = None,
    shutter: Annotated[Shutter, typer.Option(help="Sensor readout; not recorded in the SRT.")] = Shutter.ROLLING,
    attribution: Annotated[
        list[Path] | None,
        typer.Option(exists=True, dir_okay=False, help="Licence or credits file to copy along; repeatable."),
    ] = None,
    overwrite: Annotated[bool, typer.Option(help="Replace an existing manifest and video.")] = False,
) -> None:
    """Prepare a dataset from a DJI video and its .SRT telemetry (per-frame position)."""
    try:
        result = prepare_video(
            source,
            output,
            srt=srt,
            altitude=altitude,
            vertical_datum=vertical_datum,
            timezone=timezone,
            focal_length_mm=focal_length_mm,
            sensor_width_mm=sensor_width_mm,
            shutter=shutter,
            attribution=attribution or (),
            overwrite=overwrite,
        )
    except PrepError as error:
        console.print(f"[red]error:[/red] {error}")
        raise typer.Exit(1) from error
    _report(result)


@app.command()
def survey(
    sources: Annotated[list[Path], typer.Argument(exists=True, help="DJI videos, or directories of them.")],
    *,
    output: Annotated[Path, typer.Option("--output", "-o", help="Dataset directory to write.")],
    focal_length_mm: Annotated[
        float | None,
        typer.Option(min=0.1, help="Focal length of the video mode, with --sensor-width-mm; default: measured."),
    ] = None,
    sensor_width_mm: Annotated[
        float | None, typer.Option(min=0.1, help="Width of the sensor area the video uses.")
    ] = None,
    overlap: Annotated[
        float, typer.Option(min=0.01, max=0.99, help="Share of a frame the next one still shows, on the pixels.")
    ] = 0.85,
    shutter: Annotated[Shutter, typer.Option(help="Sensor readout; not recorded in the video.")] = Shutter.ROLLING,
    attribution: Annotated[
        list[Path] | None,
        typer.Option(exists=True, dir_okay=False, help="Licence or credits file to copy along; repeatable."),
    ] = None,
    nav: Annotated[
        list[Path] | None,
        typer.Option(exists=True, help=NAV_HELP),
    ] = None,
    overwrite: Annotated[bool, typer.Option(help="Replace an existing manifest and images directory.")] = False,
) -> None:
    """Prepare a dataset from the video clips of a survey: pick the frames, measure the camera, check the result."""
    try:
        _print_nav_sources(sources, nav)
        result = prepare_survey(
            sources,
            output,
            focal_length_mm=focal_length_mm,
            sensor_width_mm=sensor_width_mm,
            overlap=overlap,
            shutter=shutter,
            attribution=attribution or (),
            overwrite=overwrite,
            nav=nav,
        )
    except PrepError as error:
        console.print(f"[red]error:[/red] {error}")
        raise typer.Exit(1) from error
    _print_clips(result.clips)
    _print_checks(result.checks)
    written = Table(show_header=False, box=None, padding=(0, 1, 0, 0))
    written.add_column(no_wrap=True)
    written.add_column(overflow="fold")
    written.add_row("manifest", _shown(result.manifest_path))
    written.add_row(
        "images",
        f"{_shown(result.manifest_path.parent / 'images')}  ({result.image_bytes / 1e9:.2f} GB "
        f"from {result.video_bytes / 1e9:.2f} GB of video)",
    )
    if result.trajectory_path:
        written.add_row("trajectory", _shown(result.trajectory_path))
    written.add_row("report", _shown(result.manifest_path.parent / REPORT_NAME))
    _print_summary(
        "survey",
        result.clips,
        result.frames,
        result.located,
        result.nav_found,
        result.nav_used,
        result.verdict,
        [("camera", camera) for camera in result.cameras] + [("altitude", result.altitude_reference)],
        written,
        _video_folder(sources),
    )


@app.command()
def check(
    sources: Annotated[list[Path], typer.Argument(exists=True, help="DJI videos, or directories of them.")],
    *,
    focal_length_mm: Annotated[
        float | None,
        typer.Option(min=0.1, help="A camera to test against the footage, with --sensor-width-mm."),
    ] = None,
    sensor_width_mm: Annotated[
        float | None, typer.Option(min=0.1, help="Width of the sensor area the video uses.")
    ] = None,
    overlap: Annotated[
        float, typer.Option(min=0.01, max=0.99, help="Share of a frame the next one still shows, on the pixels.")
    ] = 0.85,
    nav: Annotated[
        list[Path] | None,
        typer.Option(exists=True, help=NAV_HELP),
    ] = None,
) -> None:
    """Say what survey would keep and whether to trust it, writing nothing. Exits 1 if the data will not do."""
    try:
        _print_nav_sources(sources, nav)
        result = check_survey(
            sources, focal_length_mm=focal_length_mm, sensor_width_mm=sensor_width_mm, overlap=overlap, nav=nav
        )
    except PrepError as error:
        console.print(f"[red]error:[/red] {error}")
        raise typer.Exit(1) from error
    _print_clips(result.clips)
    _print_checks(result.checks)
    _print_summary(
        "check",
        result.clips,
        result.frames,
        None,
        result.nav_found,
        result.nav_used,
        result.verdict,
        [("camera", f"{result.camera.focal_px:.0f} px focal length, {result.camera.width} x {result.camera.height}")],
        None,
        _video_folder(sources),
    )
    if result.verdict is Level.FAIL:
        raise typer.Exit(1)


@app.command()
def camera(
    sources: Annotated[list[Path], typer.Argument(exists=True, help="DJI videos of one video mode, or directories.")],
) -> None:
    """Measure the camera of a video mode from the footage, for the acquisition software."""
    try:
        measured = measure_camera([scan_clip(video) for video in list_videos(sources)])
    except PrepError as error:
        console.print(f"[red]error:[/red] {error}")
        raise typer.Exit(1) from error
    _print_camera(measured)


@app.command("inspect")
def inspect_video(
    source: Annotated[Path, typer.Argument(exists=True, dir_okay=False, help="Video file (MP4 / MOV).")],
    *,
    output: Annotated[
        Path | None,
        typer.Option("--output", "-o", file_okay=False, help="Directory to write to; default: next to the video."),
    ] = None,
    srt: Annotated[
        Path | None,
        typer.Option(exists=True, dir_okay=False, help="SRT telemetry; default: the .SRT next to the video."),
    ] = None,
    rows: Annotated[
        int, typer.Option(min=0, help="Telemetry entries to print, from the start and the end; 0 prints all.")
    ] = 10,
    overwrite: Annotated[bool, typer.Option(help="Replace existing output files.")] = False,
) -> None:
    """Print all a video carries; write <video>.nav.geojson (position, motion, attitude) and <video>.camera.json."""
    output = output or source.parent
    if not overwrite and (existing := [path for path in output_paths(source, output) if path.exists()]):
        console.print(f"[red]error:[/red] {existing[0]} already exists; pass --overwrite to replace it")
        raise typer.Exit(1)
    try:
        metadata = read_metadata(source, srt=srt)
        print_metadata(console, metadata, rows)
        nav_path, camera_path = write_metadata(metadata, output, overwrite=overwrite)
    except PrepError as error:
        console.print(f"[red]error:[/red] {error}")
        raise typer.Exit(1) from error
    console.print(f"wrote [bold]{nav_path}[/] ({len(metadata.entries)} points)")
    console.print(f"wrote [bold]{camera_path}[/] ({len(metadata.entries)} entries, {len(metadata.frames)} frames)")


def _print_camera(measured: Camera) -> None:
    """Print the camera and the block to paste into the acquisition software."""
    height_m = measured.flight_height_m
    across_m, along_m = measured.footprint_m(height_m)
    horizontal, vertical = measured.fov_deg
    table = Table(show_header=False, box=None)
    table.add_row("video mode", f"{measured.width} x {measured.height}")
    table.add_row("focal length", f"{measured.focal_px:.0f} px, ±{ACCURACY:.0%}, from {measured.pairs} samples")
    table.add_row("field of view", f"{horizontal:.1f}° x {vertical:.1f}°")
    table.add_row(f"at {height_m:.1f} m", f"{across_m:.1f} m across, {along_m:.1f} m along")
    table.add_row("per metre of height", f"{across_m / height_m:.3f} across, {along_m / height_m:.3f} along")
    table.add_row("ground resolution", f"{measured.gsd_cm(height_m):.2f} cm/px")
    if limit := measured.speed_limit_ms(height_m):
        table.add_row("speed limit", f"{limit:.1f} m/s for 1 px of blur at this exposure")
    console.print(table)
    console.print("Acquisition profile:")
    console.print_json(data=measured.acquisition_profile())


def _print_clips(clips: list[ClipResult]) -> None:
    """Print what came out of each clip."""
    table = Table(box=None)
    for heading in ("clip", "length", "flown", "frames", "kept", "no texture", "navigation"):
        table.add_column(heading, justify="left" if heading in ("clip", "navigation") else "right")
    for clip in clips:
        table.add_row(
            clip.name,
            f"{clip.duration_s:.0f} s",
            f"{clip.flown_m:.0f} m",
            str(clip.frames),
            str(clip.kept),
            f"{clip.featureless_s:.0f} s",
            clip.nav if clip.nav == VIDEO_NAV else f"{clip.nav}, x{clip.time_scale:.3f} {clip.clock_offset_s:+.2f} s",
        )
    console.print(table)


def _print_checks(checks: list[Check]) -> None:
    """Print each check."""
    table = Table(show_header=False, box=None)
    for item in checks:
        table.add_row(f"[{COLOURS[item.level]}]{item.level}[/]", item.name, item.message)
    console.print(table)


def _print_nav_sources(sources: list[Path], nav: list[Path] | None) -> None:
    """Print where navigation is looked for and the files there, before the clips are read."""
    places = nav_places(list_videos(sources), nav)
    files = nav_files(places)
    base = _video_folder(sources)
    table = Table(show_header=False, box=None, padding=(0, 1, 0, 0))
    table.add_column(no_wrap=True)
    table.add_column(overflow="fold")
    table.add_row("videos", str(base))
    for index, place in enumerate(places):
        shown = _shown(place, base)
        if nav is not None:
            origin = "--nav"
        elif place.resolve() in {video.resolve().parent for video in list_videos(sources)}:
            shown, origin = ("the video folder" if shown == "." else shown), "and its subfolders"
        else:
            origin = NAV_ENV
        table.add_row("searched" if index == 0 else "", shown, f"[dim]{origin}[/]")
    if files:
        for index, path in enumerate(files):
            table.add_row("found" if index == 0 else "", _shown(path, base), f"[dim]{nav_kind(path)}[/]")
        note = "[dim]Only files covering the time of the clips are read; each clip takes the finest that fits.[/]"
    else:
        table.add_row("found", "[yellow]no flight log or Swathwise fixes[/]", "")
        note = "[yellow]Positions will come from the video telemetry (4 decimals, about 11 m).[/]"
    console.print(Panel(Group(table, note), title="navigation", title_align="left", expand=False))


def _print_summary(
    title: str,
    clips: list[ClipResult],
    frames: int,
    located: int | None,
    nav_found: list[Path],
    nav_used: list[Path],
    level: Level,
    extra: list[tuple[str, str]],
    written: Table | None,
    base: Path,
) -> None:
    """Print a box with what came out: clips, frames, the navigation used and left aside, and the verdict."""
    table = Table(show_header=False, box=None, padding=(0, 1, 0, 0))
    table.add_column(no_wrap=True)
    table.add_column(overflow="fold")
    table.add_column(overflow="fold")
    seconds, metres = sum(clip.duration_s for clip in clips), sum(clip.flown_m for clip in clips)
    table.add_row("clips", f"{len(clips)}  ({seconds / 60:.1f} min of video, {metres / 1000:.2f} km flown)")
    kept = f"{frames} kept"
    if located is not None:
        kept += (
            f", {located} with position" if located == frames else f", [yellow]{frames - located} without position[/]"
        )
    table.add_row("frames", kept)
    for index, path in enumerate(nav_used):
        on = [clip for clip in clips if clip.nav == path.name]
        detail = f"{len(on)} of {len(clips)} clips, {sum(clip.nav_frames for clip in on)} frames"
        table.add_row("navigation" if index == 0 else "", f"[green]{_shown(path, base)}[/]", f"[dim]{detail}[/]")
    on_video = [clip.name for clip in clips if clip.nav == VIDEO_NAV]
    if on_video:
        why = "no flight log fits" if nav_found else "no flight log found"
        colour = "yellow" if nav_found else "default"
        table.add_row(
            "" if nav_used else "navigation", f"[{colour}]{VIDEO_NAV}[/]", f"[dim]{why}: {', '.join(on_video)}[/]"
        )
    for index, path in enumerate(path for path in nav_found if path not in nav_used):
        table.add_row("not used" if index == 0 else "", _shown(path, base), "[dim]covers the time, did not fit[/]")
    for name, value in extra:
        table.add_row(name, value)
    table.add_row("verdict", f"[bold {COLOURS[level]}]{level}[/]")
    body = Group(table, "", written) if written is not None else table
    console.print(Panel(body, title=title, title_align="left", border_style=COLOURS[level], expand=False))


def _shown(path: Path, base: Path | None = None) -> str:
    """``path`` relative to ``base``, else to the working directory, when it is inside it."""
    for folder in (base, Path.cwd()):
        if folder is not None and path.resolve().is_relative_to(folder.resolve()):
            return str(path.resolve().relative_to(folder.resolve())) or "."
    return str(path)


def _video_folder(sources: list[Path]) -> Path:
    """The folder of the first video, against which navigation files are shown."""
    return list_videos(sources)[0].resolve().parent


def _report(result: PrepResult) -> None:
    """Print what was written."""
    table = Table(show_header=False, box=None)
    table.add_row("manifest", str(result.manifest_path))
    table.add_row("frames", f"{result.frames} ({result.located} with position)")
    table.add_row("altitude", result.altitude_reference)
    for camera in result.cameras:
        table.add_row("camera", camera)
    console.print(table)
    if result.located < result.frames:
        console.print(f"[yellow]warning:[/yellow] {result.frames - result.located} frames have no position")
