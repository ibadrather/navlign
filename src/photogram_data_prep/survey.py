"""Prepare a dataset (manifest.geojson + images/) from the DJI video clips of one survey, frames chosen by overlap.

A survey is many gigabytes of 4K video in which a frame every few metres is enough, so this keeps only those frames,
as JPEGs, and copies no video: the pipeline gets an image dataset a small fraction of the size of the clips.

Frames are chosen by the distance flown, the integral of the telemetry speed, not by GPS: the Mini 4K rounds GPS to
4 decimals, about 11 m of latitude, but its speed is good to 0.1 m/s. The GPS is averaged over a few seconds for the
same reason. See ``docs/learnings/mini-4k-video.md``.
"""

import bisect
import json
import shutil
import subprocess
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from photogram_data_prep.images import Json, PrepError, PrepResult, Shutter
from photogram_data_prep.video import probe, read_telemetry

VIDEO_SUFFIXES = frozenset({".mp4", ".mov"})
JPEG_QUALITY = 2  # ffmpeg -q:v, 1 (best) to 31
SMOOTHING_S = 5.0  # half window of the GPS mean: long enough to span a rounding step at survey speed
MIN_HEIGHT_M = 1.0  # keeps the footprint positive on the ground
MIN_MOVE_M = 1.0  # the last frame of a clip is kept only if the drone moved this far since the last one kept


@dataclass(frozen=True)
class ClipResult:
    """What came out of one video."""

    name: str
    duration_s: float
    flown_m: float
    frames: int
    kept: int


@dataclass(frozen=True)
class SurveyResult(PrepResult):
    """What was written, per clip, and the size of the images against the size of the videos."""

    clips: list[ClipResult]
    video_bytes: int
    image_bytes: int


@dataclass(frozen=True)
class _Clip:
    """One video with the frames to keep and their features."""

    video: Path
    frames: list[int]
    features: list[Json]
    result: ClipResult


def prepare_survey(
    sources: Sequence[Path],
    dataset_dir: Path,
    *,
    focal_length_mm: float,
    sensor_width_mm: float,
    overlap: float = 0.8,
    shutter: Shutter = Shutter.ROLLING,
    attribution: Sequence[Path] = (),
    overwrite: bool = False,
) -> SurveyResult:
    """Write dataset_dir/manifest.geojson and the frames of ``sources`` that keep ``overlap`` to dataset_dir/images/.

    sources are videos, or directories of them taken in file name order. overlap is along the flight direction,
    between 0 and 1, on the ground below the take-off point, over the short side of the image: anything taller than
    the ground overlaps less. Clips are thinned one by one, as nothing says what the drone did between them. The
    video does not record the camera, so focal_length_mm and sensor_width_mm are required: the sensor area the video
    uses, not the nominal sensor. Everything is read before anything is written.
    """
    if not 0 < overlap < 1:
        raise PrepError(f"overlap must be between 0 and 1, got {overlap}")
    if focal_length_mm <= 0 or sensor_width_mm <= 0:
        raise PrepError("focal length and sensor width must be positive")
    videos = _list_videos(sources)

    clips: list[_Clip] = []
    cameras: dict[str, Json] = {}
    for video in videos:
        width, height, times, created = probe(video)
        camera_id = f"dji_video_{width}x{height}_{focal_length_mm:g}mm"
        cameras.setdefault(
            camera_id,
            {
                "width": width,
                "height": height,
                "focal_length_mm": focal_length_mm,
                "sensor_width_mm": sensor_width_mm,
                "shutter": str(shutter),
            },
        )
        if len(cameras) > 1:
            # One focal length cannot describe two video modes: each crops the sensor differently.
            raise PrepError(f"{video.name}: frame size differs from the other clips; prepare them separately")
        footprint_ratio = min(sensor_width_mm, sensor_width_mm * height / width) / focal_length_mm
        clips.append(_read_clip(video, times, created, camera_id, footprint_ratio, overlap))

    manifest_path = dataset_dir / "manifest.geojson"
    images_dir = dataset_dir / "images"
    if (manifest_path.exists() or images_dir.exists()) and not overwrite:
        raise PrepError(f"{dataset_dir} already holds a dataset; pass --overwrite to replace it")
    manifest_path.unlink(missing_ok=True)
    shutil.rmtree(images_dir, ignore_errors=True)
    for old_video in dataset_dir.glob("video.*"):
        old_video.unlink()
    images_dir.mkdir(parents=True)
    for clip in clips:
        _extract_frames(clip, images_dir)
    for path in attribution:
        shutil.copy2(path, dataset_dir / path.name)

    features = [feature for clip in clips for feature in clip.features]
    manifest = {
        "type": "FeatureCollection",
        "photogrammetry": {
            "schema_version": "0.1.0",
            "source": "images",
            "altitude_reference": "relative_takeoff",
            "orientation_convention": "ned_yaw_pitch_roll_deg",
            "cameras": cameras,
            "frame_selection": {"method": "overlap", "overlap": overlap, "measure": "telemetry speed"},
        },
        "features": features,
    }
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return SurveyResult(
        manifest_path=manifest_path,
        frames=len(features),
        located=sum(feature["geometry"] is not None for feature in features),
        altitude_reference="relative_takeoff",
        cameras=list(cameras),
        clips=[clip.result for clip in clips],
        video_bytes=sum(video.stat().st_size for video in videos),
        image_bytes=sum(path.stat().st_size for path in images_dir.iterdir()),
    )


def _list_videos(sources: Sequence[Path]) -> list[Path]:
    """The videos named by ``sources``; a directory stands for the videos directly in it, in file name order."""
    videos: list[Path] = []
    for source in sources:
        if source.is_dir():
            videos += sorted(
                path for path in source.iterdir() if path.suffix.lower() in VIDEO_SUFFIXES and path.name[0] != "."
            )
        else:
            videos.append(source)
    if not videos:
        raise PrepError(f"no videos in {', '.join(map(str, sources))}")
    names = [video.stem for video in videos]
    if repeated := sorted({name for name in names if names.count(name) > 1}):
        raise PrepError(f"two videos have the same name, which would mix up their frames: {', '.join(repeated)}")
    return videos


def _read_clip(
    video: Path, times: list[float], created: str | None, camera_id: str, footprint_ratio: float, overlap: float
) -> _Clip:
    """Choose the frames of one video and make their features."""
    entries = read_telemetry(video)
    if any("speed_ms" not in entry.fields for entry in entries):
        raise PrepError(f"{video.name}: telemetry has no H.S speed; only the Mini 4K format can be thinned")
    # An entry holds for its whole interval, so the last one runs on to its end: that is where the clip stops.
    moments = [*(entry.start_s for entry in entries), entries[-1].end_s]
    speeds = [*(entry.fields["speed_ms"] for entry in entries), entries[-1].fields["speed_ms"]]
    heights = [*(entry.fields["rel_alt"] for entry in entries), entries[-1].fields["rel_alt"]]
    flown = _integrate(moments, speeds)

    flown_at = [_at(moments, flown, time) for time in times]
    footprints = [max(_at(moments, heights, time), MIN_HEIGHT_M) * footprint_ratio for time in times]
    frames = _choose_frames(flown_at, footprints, overlap)

    # DJI writes 0/0 when there was no fix.
    fixes = [entry for entry in entries if (entry.fields["latitude"], entry.fields["longitude"]) != (0, 0)]
    fix_moments = [entry.start_s for entry in fixes]
    half_window = round(SMOOTHING_S / max(moments[1] - moments[0], 0.001))
    lons = _smooth([entry.fields["longitude"] for entry in fixes], half_window)
    lats = _smooth([entry.fields["latitude"] for entry in fixes], half_window)
    start = datetime.fromisoformat(created) if created else None

    features: list[Json] = []
    for frame in frames:
        time = times[frame]
        properties: Json = {
            "image": f"{video.stem}_{frame:06d}.jpg",
            "camera": camera_id,
            "video": video.name,
            "video_frame": frame,
        }
        if start is not None:
            taken = (start + timedelta(seconds=time)).isoformat(timespec="milliseconds")
            properties["time_utc"] = taken.replace("+00:00", "Z")
        geometry: Json | None = None
        if fixes:
            lon, lat, height = _at(fix_moments, lons, time), _at(fix_moments, lats, time), _at(moments, heights, time)
            geometry = {"type": "Point", "coordinates": [round(lon, 9), round(lat, 9), round(height, 3)]}
        features.append({"type": "Feature", "geometry": geometry, "properties": properties})
    result = ClipResult(video.name, times[-1], flown_at[-1], len(times), len(frames))
    return _Clip(video, frames, features, result)


def _integrate(moments: list[float], speeds: list[float]) -> list[float]:
    """The distance flown by each moment, with the speed linear between moments."""
    flown = [0.0]
    for i in range(1, len(moments)):
        flown.append(flown[-1] + (speeds[i - 1] + speeds[i]) / 2 * (moments[i] - moments[i - 1]))
    return flown


def _at(moments: list[float], values: list[float], time: float) -> float:
    """``values`` at ``time``, linear between ``moments`` and held beyond them."""
    i = bisect.bisect_right(moments, time)
    if i == 0:
        return values[0]
    if i == len(moments):
        return values[-1]
    weight = (time - moments[i - 1]) / (moments[i] - moments[i - 1])
    return values[i - 1] + weight * (values[i] - values[i - 1])


def _choose_frames(flown: list[float], footprints: list[float], overlap: float) -> list[int]:
    """Keep the first frame, then each frame that overlaps the last one kept by ``overlap`` or less.

    The clip's last frame is kept too, to cover the end of the line, if the drone moved since the last one kept.
    """
    frames = [0]
    for frame in range(1, len(flown)):
        if flown[frame] - flown[frames[-1]] >= (1 - overlap) * footprints[frame]:
            frames.append(frame)
    last = len(flown) - 1
    if frames[-1] != last and flown[last] - flown[frames[-1]] >= MIN_MOVE_M:
        frames.append(last)
    return frames


def _smooth(values: list[float], half_window: int) -> list[float]:
    """A centred mean; the window narrows towards the ends so that it stays centred."""
    smoothed: list[float] = []
    for i in range(len(values)):
        half = min(i, len(values) - 1 - i, half_window)
        smoothed.append(sum(values[i - half : i + half + 1]) / (2 * half + 1))
    return smoothed


def _extract_frames(clip: _Clip, images_dir: Path) -> None:
    """Decode the kept frames of ``clip`` in one pass over the video into ``images_dir``.

    ffmpeg numbers its output files 1, 2, ... in decode order, so the k-th file is the k-th kept frame.
    """
    terms = "+".join(f"eq(n,{frame})" for frame in clip.frames)
    with tempfile.TemporaryDirectory(dir=images_dir.parent) as scratch:
        scratch_dir = Path(scratch)
        command = ["ffmpeg", "-v", "error", "-nostdin", "-i", str(clip.video), "-vf", f"select='{terms}'"]
        command += ["-fps_mode", "passthrough", "-q:v", str(JPEG_QUALITY), str(scratch_dir / "%06d.jpg")]
        try:
            subprocess.run(command, capture_output=True, text=True, check=True)
        except FileNotFoundError as error:
            raise PrepError("ffmpeg not found; install FFmpeg") from error
        except subprocess.CalledProcessError as error:
            raise PrepError(f"ffmpeg failed on {clip.video}: {error.stderr.strip()}") from error
        decoded = sorted(scratch_dir.iterdir())
        if len(decoded) != len(clip.frames):
            raise PrepError(f"ffmpeg wrote {len(decoded)} of {len(clip.frames)} frames of {clip.video.name}")
        for path, feature in zip(decoded, clip.features, strict=True):
            properties = feature["properties"]
            assert isinstance(properties, dict)
            path.rename(images_dir / properties["image"])
