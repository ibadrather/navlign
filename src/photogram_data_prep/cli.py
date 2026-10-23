"""Command line interface."""

from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from photogram_data_prep.images import AltitudeMode, PrepError, PrepResult, Shutter, VerticalDatum, prepare_images
from photogram_data_prep.video import prepare_video

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
