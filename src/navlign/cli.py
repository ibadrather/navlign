"""Command line interface."""

from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from navlign.camera import ACCURACY, Camera, measure_camera
from navlign.checks import Check, Level, verdict
from navlign.images import AltitudeMode, PrepError, PrepResult, Shutter, VerticalDatum, prepare_images
from navlign.metadata import output_paths, read_metadata, write_metadata
from navlign.report import print_metadata
from navlign.scan import scan_clip
from navlign.survey import ClipResult, check_survey, list_videos, prepare_survey
from navlign.video import prepare_video

app = typer.Typer(no_args_is_help=True, add_completion=False)
console = Console()


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
    overwrite: Annotated[bool, typer.Option(help="Replace an existing manifest and images directory.")] = False,
) -> None:
    """Prepare a dataset from the video clips of a survey: pick the frames, measure the camera, check the result."""
    try:
        result = prepare_survey(
            sources,
            output,
            focal_length_mm=focal_length_mm,
            sensor_width_mm=sensor_width_mm,
            overlap=overlap,
            shutter=shutter,
            attribution=attribution or (),
            overwrite=overwrite,
        )
    except PrepError as error:
        console.print(f"[red]error:[/red] {error}")
        raise typer.Exit(1) from error
    _print_clips(result.clips)
    _report(result)
    gigabytes = result.image_bytes / 1e9, result.video_bytes / 1e9
    console.print(f"{gigabytes[0]:.2f} GB of images from {gigabytes[1]:.2f} GB of video")
    _print_checks(result.checks)


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
) -> None:
    """Say what survey would keep and whether to trust it, writing nothing. Exits 1 if the data will not do."""
    try:
        result = check_survey(
            sources, focal_length_mm=focal_length_mm, sensor_width_mm=sensor_width_mm, overlap=overlap
        )
    except PrepError as error:
        console.print(f"[red]error:[/red] {error}")
        raise typer.Exit(1) from error
    _print_clips(result.clips)
    _print_checks(result.checks)
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
    for heading in ("clip", "length", "flown", "frames", "kept", "no texture"):
        table.add_column(heading, justify="left" if heading == "clip" else "right")
    for clip in clips:
        table.add_row(
            clip.name,
            f"{clip.duration_s:.0f} s",
            f"{clip.flown_m:.0f} m",
            str(clip.frames),
            str(clip.kept),
            f"{clip.featureless_s:.0f} s",
        )
    console.print(table)


def _print_checks(checks: list[Check]) -> None:
    """Print each check and the verdict."""
    colours = {Level.PASS: "green", Level.WARN: "yellow", Level.FAIL: "red"}
    table = Table(show_header=False, box=None)
    for item in checks:
        table.add_row(f"[{colours[item.level]}]{item.level}[/]", item.name, item.message)
    console.print(table)
    level = verdict(checks)
    console.print(f"verdict: [{colours[level]}]{level}[/]")


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
