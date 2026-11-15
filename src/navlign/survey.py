"""Prepare a dataset (manifest.geojson + images/) from the DJI video clips of one survey, frames chosen by overlap.

A survey is many gigabytes of video in which a frame every few metres is enough, so this keeps only those frames, as
JPEGs, and copies no video: the pipeline gets an image dataset a small fraction of the size of the clips.

Frames are chosen by how far the image really moved, measured from the pixels (see ``scan.py``), so no camera model
goes into the choice and the overlap holds over trees, in turns, in a hover and across a change of height. Stretches
with no texture, like open water, yield no frames. Position, height and attitude come from the flight log when one is
found that fits the clip (see ``nav.py``), interpolated to the time of the frame, and all of that log is written next to
the manifest as trajectory.geojson. Without one, position is the GPS of the video telemetry, averaged over a few seconds
as the Mini 4K rounds it to about 11 m of latitude. The camera is measured from the footage (see ``camera.py``), and
the result is checked (see ``checks.py``), so one command reads the videos, picks the frames and says whether to trust
them.
"""

import json
import shutil
import subprocess
import tempfile
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

from navlign.camera import SENSOR_WIDTH_MM, Camera, measure_camera
from navlign.checks import Check, Level, run_checks, verdict
from navlign.images import Json, PrepError, PrepResult, Shutter
from navlign.nav import (
    MARGIN_S,
    TRAJECTORY_NAME,
    Alignment,
    Track,
    align,
    find_tracks,
    nav_places,
    write_trajectory,
)
from navlign.scan import MIN_TEXTURE, ClipScan, Sample, at, overlap_of, scan_clip

VIDEO_SUFFIXES = frozenset({".mp4", ".mov"})
JPEG_QUALITY = 2  # ffmpeg -q:v, 1 (best) to 31
SMOOTHING_S = 5.0  # half window of the GPS mean: long enough to span a rounding step at survey speed
PICK_WINDOW = 3  # samples before the overlap runs out among which to avoid a soft frame
SOFT_MARGIN = 0.95  # the latest of those is kept unless it is this much softer than the sharpest
REPORT_NAME = "prep_report.json"
VIDEO_NAV = "video telemetry"  # where the positions of a clip are from when no flight log fits it


@dataclass(frozen=True)
class ClipResult:
    """What came out of one video."""

    name: str
    duration_s: float
    flown_m: float
    frames: int
    kept: int
    featureless_s: float
    nav: str = VIDEO_NAV  # the file the positions are from
    time_scale: float = 1.0  # real seconds per second of video, fitted to that file
    clock_offset_s: float = 0.0  # how long after its creation time the clip began
    nav_frames: int = 0  # kept frames with a position from that file


@dataclass(frozen=True)
class SurveyResult(PrepResult):
    """What was written, per clip, the size of the images against the size of the videos, and what was checked."""

    clips: list[ClipResult]
    video_bytes: int
    image_bytes: int
    camera: Camera
    checks: list[Check]
    trajectory_path: Path | None = None  # every point of the flight logs used, when there were any
    nav_found: list[Path] = field(default_factory=list)  # navigation files covering the time of the clips
    nav_used: list[Path] = field(default_factory=list)  # those a clip was fitted to, in time order

    @property
    def verdict(self) -> Level:
        """The worst of the checks."""
        return verdict(self.checks)


@dataclass(frozen=True)
class SurveyCheck:
    """What a run would do: nothing written yet."""

    clips: list[ClipResult]
    camera: Camera
    checks: list[Check]
    frames: int
    nav_found: list[Path] = field(default_factory=list)  # navigation files covering the time of the clips
    nav_used: list[Path] = field(default_factory=list)  # those a clip was fitted to, in time order

    @property
    def verdict(self) -> Level:
        """The worst of the checks."""
        return verdict(self.checks)


@dataclass(frozen=True)
class _Clip:
    """One video with the frames to keep and their features."""

    scan: ClipScan
    kept: list[int]  # indices into scan.samples
    features: list[Json]
    result: ClipResult

    @property
    def frames(self) -> list[int]:
        """The kept frames, as frame numbers of the video."""
        return [self.scan.samples[index].frame for index in self.kept]


def check_survey(
    sources: Sequence[Path],
    *,
    focal_length_mm: float | None = None,
    sensor_width_mm: float | None = None,
    overlap: float = 0.85,
    nav: Sequence[Path] | None = None,
) -> SurveyCheck:
    """Say what prepare_survey would keep and whether to trust it, writing nothing."""
    clips, camera, _, checks, tracks, found = _plan(
        sources, focal_length_mm, sensor_width_mm, overlap, Shutter.ROLLING, nav
    )
    return SurveyCheck(
        [clip.result for clip in clips],
        camera,
        checks,
        sum(len(clip.kept) for clip in clips),
        nav_found=[track.path for track in found],
        nav_used=[track.path for track in tracks],
    )


def prepare_survey(
    sources: Sequence[Path],
    dataset_dir: Path,
    *,
    focal_length_mm: float | None = None,
    sensor_width_mm: float | None = None,
    overlap: float = 0.85,
    shutter: Shutter = Shutter.ROLLING,
    attribution: Sequence[Path] = (),
    overwrite: bool = False,
    nav: Sequence[Path] | None = None,
) -> SurveyResult:
    """Write dataset_dir/manifest.geojson, the frames of ``sources`` that keep ``overlap`` to dataset_dir/images/,
    dataset_dir/prep_report.json and, when a flight log fits the clips, dataset_dir/trajectory.geojson.

    sources are videos, or directories of them taken in file name order. overlap is the share of a frame that the next
    one still shows, measured on the pixels, between 0 and 1. The camera is measured from the footage unless
    focal_length_mm and sensor_width_mm are given, together. nav are files or folders with flight logs and Swathwise
    missions; by default the folders of the videos and those in NAVLIGN_NAV are searched. Everything is read before
    anything is written.
    """
    clips, camera, cameras, checks, tracks, found = _plan(
        sources, focal_length_mm, sensor_width_mm, overlap, shutter, nav
    )
    if not any(clip.kept for clip in clips):
        raise PrepError("no frame has texture to work with: nothing to write")

    manifest_path = dataset_dir / "manifest.geojson"
    images_dir = dataset_dir / "images"
    if (manifest_path.exists() or images_dir.exists()) and not overwrite:
        raise PrepError(f"{dataset_dir} already holds a dataset; pass --overwrite to replace it")
    manifest_path.unlink(missing_ok=True)
    shutil.rmtree(images_dir, ignore_errors=True)
    for old_video in dataset_dir.glob("video.*"):
        old_video.unlink()
    trajectory_path = dataset_dir / TRAJECTORY_NAME
    trajectory_path.unlink(missing_ok=True)
    images_dir.mkdir(parents=True)
    for clip in clips:
        if clip.kept:
            _extract_frames(clip, images_dir)
    for path in attribution:
        shutil.copy2(path, dataset_dir / path.name)

    features = [feature for clip in clips for feature in clip.features]
    header: Json = {
        "schema_version": "0.1.0",
        "source": "images",
        "altitude_reference": "relative_takeoff",
        "orientation_convention": "ned_yaw_pitch_roll_deg",
        "cameras": cameras,
        "frame_selection": {"method": "overlap", "overlap": overlap, "measure": "image motion"},
    }
    if tracks:
        write_trajectory(trajectory_path, tracks)
        header["navigation"] = {"sources": [track.path.name for track in tracks], "trajectory": TRAJECTORY_NAME}
    manifest = {"type": "FeatureCollection", "photogrammetry": header, "features": features}
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    result = SurveyResult(
        manifest_path=manifest_path,
        frames=len(features),
        located=sum(feature["geometry"] is not None for feature in features),
        altitude_reference="relative_takeoff",
        cameras=list(cameras),
        clips=[clip.result for clip in clips],
        video_bytes=sum(clip.scan.video.stat().st_size for clip in clips),
        image_bytes=sum(path.stat().st_size for path in images_dir.iterdir()),
        camera=camera,
        checks=checks,
        trajectory_path=trajectory_path if tracks else None,
        nav_found=[track.path for track in found],
        nav_used=[track.path for track in tracks],
    )
    (dataset_dir / REPORT_NAME).write_text(json.dumps(_report(result), indent=2) + "\n", encoding="utf-8")
    return result


def list_videos(sources: Sequence[Path]) -> list[Path]:
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


def choose_frames(samples: Sequence[Sample], overlap: float, width: int, height: int) -> list[int]:
    """The samples to keep: the first of each stretch of usable ones, then one each time ``overlap`` runs out.

    The image motion is chained from the last frame kept, so a hover adds nothing. A stretch ends where the frames
    have no texture or the motion could not be measured; its last sample is kept if the view moved at all, to cover
    the end of the line. Where the overlap runs out, a soft frame gives way to a slightly earlier one.
    """
    kept: list[int] = []
    index = 0
    while index < len(samples):
        if samples[index].texture < MIN_TEXTURE:
            index += 1
            continue
        kept.append(index)
        chained = np.eye(3)
        following = index + 1
        chosen: int | None = None
        while following < len(samples) and chosen is None:
            sample = samples[following]
            motion = sample.motion
            if motion is None or sample.texture < MIN_TEXTURE:
                break
            chained = motion @ chained
            if overlap_of(chained, width, height) < overlap:
                chosen = _pick(samples, index + 1, following)
            else:
                following += 1
        if chosen is not None:
            index = chosen
            continue
        # The end of a stretch: half a step of movement is worth one more frame.
        if following - 1 > index and overlap_of(chained, width, height) < (1 + overlap) / 2:
            kept.append(following - 1)
        index = following
    return kept


def _pick(samples: Sequence[Sample], first: int, limit: int) -> int:
    """The sample to keep when ``limit`` is the first one past the overlap: the latest one that is not soft."""
    window = range(max(first, limit - PICK_WINDOW), limit)
    if not window:
        return limit
    sharpest = max(samples[index].sharpness for index in window)
    return next(index for index in reversed(window) if samples[index].sharpness >= SOFT_MARGIN * sharpest)


def _plan(
    sources: Sequence[Path],
    focal_length_mm: float | None,
    sensor_width_mm: float | None,
    overlap: float,
    shutter: Shutter,
    nav: Sequence[Path] | None,
) -> tuple[list[_Clip], Camera, dict[str, Json], list[Check], list[Track], list[Track]]:
    """Read every clip, fit it to the flight log, measure the camera, choose the frames and check them. Writes
    nothing. The tracks returned are those a clip was fitted to, in time order, then all that cover the clips."""
    if not 0 < overlap < 1:
        raise PrepError(f"overlap must be between 0 and 1, got {overlap}")
    if (focal_length_mm is None) != (sensor_width_mm is None):
        raise PrepError("pass --focal-length-mm and --sensor-width-mm together")
    if (focal_length_mm is not None and focal_length_mm <= 0) or (sensor_width_mm is not None and sensor_width_mm <= 0):
        raise PrepError("focal length and sensor width must be positive")
    videos = list_videos(sources)

    scans: list[ClipScan] = []
    for video in videos:
        scan = scan_clip(video)
        if any("speed_ms" not in entry.fields for entry in scan.entries):
            raise PrepError(f"{video.name}: telemetry has no H.S speed; only the Mini 4K format can be read")
        if scans and (scan.width, scan.height) != (scans[0].width, scans[0].height):
            # One focal length cannot describe two video modes: each crops the sensor differently.
            raise PrepError(f"{video.name}: frame size differs from the other clips; prepare them separately")
        scans.append(scan)
    found, alignments = _align_clips(scans, nav_places(videos, nav))
    # From here on a clip that fits a track runs on real time: its frame times, its telemetry and its start.
    scans = [_on_real_time(scan, fit) if fit else scan for scan, fit in zip(scans, alignments, strict=True)]

    width, height = scans[0].width, scans[0].height
    used_mm = (focal_length_mm, sensor_width_mm) if focal_length_mm and sensor_width_mm else None
    given_focal_px = used_mm[0] / used_mm[1] * width if used_mm else None
    measured = _camera_of(scans, given_focal_px)
    focal_mm, sensor_mm = used_mm or (round(measured.focal_mm, 3), SENSOR_WIDTH_MM)
    camera_id = f"dji_video_{width}x{height}_{focal_mm:g}mm"
    cameras: dict[str, Json] = {
        camera_id: {
            "width": width,
            "height": height,
            "focal_length_mm": focal_mm,
            "sensor_width_mm": sensor_mm,
            "shutter": str(shutter),
        }
    }

    clips = [_read_clip(scan, camera_id, overlap, fit) for scan, fit in zip(scans, alignments, strict=True)]
    checks = run_checks(
        [(clip.scan, clip.kept) for clip in clips],
        [feature for clip in clips for feature in clip.features],
        measured,
        overlap,
        given_focal_px,
    )
    checks.append(_navigation_check([clip.result for clip in clips], found))
    used = {fit.track.path: fit.track for fit in alignments if fit}
    return clips, measured, cameras, checks, sorted(used.values(), key=lambda track: track.times[0]), found


def _align_clips(scans: Sequence[ClipScan], places: Sequence[Path]) -> tuple[list[Track], list[Alignment | None]]:
    """The tracks in ``places`` from the time of the clips, and for each clip its fit to the finest track it fits."""
    starts = [datetime.fromisoformat(scan.created).timestamp() if scan.created else None for scan in scans]
    spans = [(start, start + scan.times[-1]) for scan, start in zip(scans, starts, strict=True) if start is not None]
    if not spans:
        return [], [None] * len(scans)
    tracks = find_tracks(places, min(span[0] for span in spans) - MARGIN_S, max(span[1] for span in spans) + MARGIN_S)
    fits: list[Alignment | None] = []
    for scan, start in zip(scans, starts, strict=True):
        candidates = (align(scan.entries, start, track) for track in tracks) if start is not None else ()
        fits.append(next((fit for fit in candidates if fit), None))
    return tracks, fits


def _on_real_time(scan: ClipScan, fit: Alignment) -> ClipScan:
    """``scan`` with its frame times and telemetry in real seconds, starting when the track says the clip began."""
    assert scan.created is not None
    created = datetime.fromisoformat(scan.created) + timedelta(seconds=fit.offset_s)
    entries = [
        replace(entry, start_s=entry.start_s * fit.scale, end_s=entry.end_s * fit.scale) for entry in scan.entries
    ]
    return replace(scan, times=[time * fit.scale for time in scan.times], entries=entries, created=created.isoformat())


def _navigation_check(clips: Sequence[ClipResult], tracks: Sequence[Track]) -> Check:
    """Where the positions are from: warns when a flight log was there and a clip, or some frames, did not fit it."""
    if not tracks:
        return Check("navigation", Level.PASS, "no flight log found: positions from the video telemetry")
    missed = [clip.name for clip in clips if clip.nav == VIDEO_NAV]
    if missed:
        return Check("navigation", Level.WARN, f"no flight log fits {', '.join(missed)}: video telemetry used")
    frames, on_track = sum(clip.kept for clip in clips), sum(clip.nav_frames for clip in clips)
    sources = ", ".join(dict.fromkeys(clip.nav for clip in clips))
    scales = sorted({round(clip.time_scale, 2) for clip in clips})
    message = f"{on_track} of {frames} frames on {sources}; video runs at {', '.join(map(str, scales))} of real time"
    return Check("navigation", Level.WARN if on_track < frames else Level.PASS, message)


def _camera_of(scans: Sequence[ClipScan], given_focal_px: float | None) -> Camera:
    """The camera measured from ``scans``; a camera that was given stands in when the footage cannot say."""
    try:
        return measure_camera(scans)
    except PrepError:
        if given_focal_px is None:
            raise
        heights = [entry.fields["rel_alt"] for scan in scans for entry in scan.entries]
        return Camera(scans[0].width, scans[0].height, given_focal_px, float(np.median(heights)), 0, None)


def _read_clip(scan: ClipScan, camera_id: str, overlap: float, fit: Alignment | None = None) -> _Clip:
    """Choose the frames of one video and make their features, from the track of ``fit`` where it has points."""
    entries = scan.entries
    moments = [entry.start_s for entry in entries]
    video = scan.video
    kept = choose_frames(scan.samples, overlap, scan.width, scan.height)

    # DJI writes 0/0 when there was no fix.
    fixes = [entry for entry in entries if (entry.fields["latitude"], entry.fields["longitude"]) != (0, 0)]
    fix_moments = [entry.start_s for entry in fixes]
    half_window = round(SMOOTHING_S / max(entries[1].start_s - entries[0].start_s, 0.001)) if len(entries) > 1 else 0
    lons = _smooth([entry.fields["longitude"] for entry in fixes], half_window)
    lats = _smooth([entry.fields["latitude"] for entry in fixes], half_window)
    start = datetime.fromisoformat(scan.created) if scan.created else None

    features: list[Json] = []
    on_track = 0
    for index in kept:
        frame = scan.samples[index].frame
        time = scan.times[frame]
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
        if fit and start is not None and fit.track.covers(moment := start.timestamp() + time):
            values = fit.track.at(moment)
            geometry = {"type": "Point", "coordinates": [values.pop("lon"), values.pop("lat"), values.pop("height")]}
            properties |= values  # gimbal yaw, pitch, roll; speeds; aircraft attitude; satellites
            properties["nav_source"] = fit.track.path.name
            on_track += 1
        elif fixes:
            height = at(moments, [entry.fields["rel_alt"] for entry in entries], time)
            lon, lat = at(fix_moments, lons, time), at(fix_moments, lats, time)
            geometry = {"type": "Point", "coordinates": [round(lon, 9), round(lat, 9), round(height, 3)]}
        features.append({"type": "Feature", "geometry": geometry, "properties": properties})

    flown = _integrate(moments, [entry.fields["speed_ms"] for entry in entries])
    featureless = sum(sample.texture < MIN_TEXTURE for sample in scan.samples) * scan.sample_interval_s
    result = ClipResult(video.name, scan.times[-1], flown[-1], len(scan.times), len(kept), featureless)
    if fit:
        result = replace(
            result, nav=fit.track.path.name, time_scale=fit.scale, clock_offset_s=fit.offset_s, nav_frames=on_track
        )
    return _Clip(scan, kept, features, result)


def _integrate(moments: list[float], speeds: list[float]) -> list[float]:
    """The distance flown by each moment, with the speed linear between moments."""
    flown = [0.0]
    for i in range(1, len(moments)):
        flown.append(flown[-1] + (speeds[i - 1] + speeds[i]) / 2 * (moments[i] - moments[i - 1]))
    return flown


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
    video = clip.scan.video
    terms = "+".join(f"eq(n,{frame})" for frame in clip.frames)
    with tempfile.TemporaryDirectory(dir=images_dir.parent) as scratch:
        scratch_dir = Path(scratch)
        command = ["ffmpeg", "-v", "error", "-nostdin", "-i", str(video), "-vf", f"select='{terms}'"]
        command += ["-fps_mode", "passthrough", "-q:v", str(JPEG_QUALITY), str(scratch_dir / "%06d.jpg")]
        try:
            subprocess.run(command, capture_output=True, text=True, check=True)
        except FileNotFoundError as error:
            raise PrepError("ffmpeg not found; install FFmpeg") from error
        except subprocess.CalledProcessError as error:
            raise PrepError(f"ffmpeg failed on {video}: {error.stderr.strip()}") from error
        decoded = sorted(scratch_dir.iterdir())
        if len(decoded) != len(clip.frames):
            raise PrepError(f"ffmpeg wrote {len(decoded)} of {len(clip.frames)} frames of {video.name}")
        for path, feature in zip(decoded, clip.features, strict=True):
            properties = feature["properties"]
            assert isinstance(properties, dict)
            path.rename(images_dir / properties["image"])


def _report(result: SurveyResult) -> Json:
    """prep_report.json: the verdict, each check, the clips and the measured camera."""
    camera = result.camera
    return {
        "verdict": str(result.verdict),
        "checks": [
            {"name": check.name, "level": str(check.level), "message": check.message} for check in result.checks
        ],
        "frames": result.frames,
        "clips": [asdict(clip) for clip in result.clips],
        "camera": {
            "focal_px": round(camera.focal_px, 1),
            "fov_deg": [round(angle, 1) for angle in camera.fov_deg],
            "flight_height_m": round(camera.flight_height_m, 1),
            "acquisition_profile": camera.acquisition_profile(),
        },
    }
