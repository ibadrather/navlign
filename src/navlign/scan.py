"""Measure what a video shows from its pixels, a few times a second: how the view moved, its texture, its sharpness.

Telemetry says what the drone did. Only the pixels say what the camera got: how far the image really moved over trees
or roads, where it looks at open water, and which frames are soft. This only tracks points from one sample to the
next, which is cheap; finding and matching features across the survey is the job of sfm.

Samples sit between keyframes on purpose. An H.264 keyframe measures about 50 % sharper than its neighbours, but that
is grain the encoder keeps: it matched no better than a P frame on the Mini 4K clips.
"""

import bisect
import itertools
import subprocess
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np
from cv2.typing import MatLike
from numpy.typing import NDArray

from navlign.images import PrepError
from navlign.video import Entry, probe, read_telemetry

type Matrix = NDArray[np.float64]

SAMPLE_RATE_HZ = 5.0
TRACK_WIDTH_PX = 640  # image motion is measured at this size, which is plenty and keeps a scan fast
MAX_CORNERS = 400
MIN_TRACKS = 30  # fewer points than this do not pin down a motion
MAX_TRACK_ERROR_PX = 1.0  # forward then backward, a good track comes back to where it started
FIT_TOLERANCE_PX = 1.5
SURFACE_PERCENTILE = 20  # of the points of a pair: slow points are the far surface, the ground rather than the trees
TEXTURE_CELLS = (16, 9)
TEXTURE_ENERGY = 100.0  # mean squared gradient of a cell; open water sits near 30, land in the thousands
MIN_TEXTURE = 0.25  # share of the image with texture; below it a frame is water or sky, with nothing for sfm to use
OVERLAP_GRID = (64, 36)


@dataclass(frozen=True)
class Sample:
    """One sampled frame. motion and surface_px are against the sample before; None if that could not be measured."""

    frame: int
    texture: float  # share of the image that has texture
    sharpness: float  # variance of the Laplacian; only comparable between samples close in time
    motion: Matrix | None  # 3x3 similarity taking pixels of the sample before to this one, in video pixels
    surface_px: float | None  # how far the far surface moved, in video pixels


@dataclass(frozen=True)
class ClipScan:
    """One video: its size, frame times, telemetry and samples."""

    video: Path
    width: int
    height: int
    times: list[float]
    created: str | None
    entries: list[Entry]
    samples: list[Sample]

    def at(self, field: str, time: float) -> float:
        """A telemetry field at ``time``, linear between entries, with each value at the middle of its interval."""
        moments = [(entry.start_s + entry.end_s) / 2 for entry in self.entries]
        return at(moments, [entry.fields[field] for entry in self.entries], time)

    @property
    def sample_interval_s(self) -> float:
        """Seconds between samples."""
        return (self.times[self.samples[1].frame] - self.times[self.samples[0].frame]) if len(self.samples) > 1 else 0


def scan_clip(video: Path) -> ClipScan:
    """Read the telemetry of ``video`` and measure its frames."""
    entries = read_telemetry(video)
    width, height, times, created = probe(video)
    fps = (len(times) - 1) / times[-1] if times[-1] > 0 else SAMPLE_RATE_HZ
    step = max(1, round(fps / SAMPLE_RATE_HZ))
    scale = min(1.0, TRACK_WIDTH_PX / width)
    size = (round(width * scale), round(height * scale))

    samples: list[Sample] = []
    previous: MatLike | None = None
    for frame, image in _decode(video, width, height, step):
        small = cv2.resize(image, size, interpolation=cv2.INTER_AREA)
        motion, surface_px = _track(previous, small, 1 / scale) if previous is not None else (None, None)
        samples.append(
            Sample(frame, _texture(small), float(cv2.Laplacian(image, cv2.CV_32F).var()), motion, surface_px)
        )
        previous = small
    if not samples:
        raise PrepError(f"{video.name}: no frames could be decoded")
    return ClipScan(video, width, height, times, created, entries, samples)


def overlap_of(motion: Matrix, width: int, height: int) -> float:
    """The share of an image still in view after ``motion``."""
    columns, rows = OVERLAP_GRID
    x, y = np.meshgrid((np.arange(columns) + 0.5) / columns * width, (np.arange(rows) + 0.5) / rows * height)
    moved_x = motion[0, 0] * x + motion[0, 1] * y + motion[0, 2]
    moved_y = motion[1, 0] * x + motion[1, 1] * y + motion[1, 2]
    return float(np.mean((moved_x >= 0) & (moved_x < width) & (moved_y >= 0) & (moved_y < height)))


def overlaps_between(samples: Sequence[Sample], kept: Sequence[int], width: int, height: int) -> list[float]:
    """The overlap of each pair of successive kept samples in one stretch; a gap of open water has none to measure."""
    measured: list[float] = []
    for first, second in itertools.pairwise(kept):
        chained = np.eye(3)
        for sample in samples[first + 1 : second + 1]:
            if sample.motion is None or sample.texture < MIN_TEXTURE:
                break
            chained = sample.motion @ chained
        else:
            measured.append(overlap_of(chained, width, height))
    return measured


def at(moments: Sequence[float], values: Sequence[float], time: float) -> float:
    """``values`` at ``time``, linear between ``moments`` and held beyond them."""
    i = bisect.bisect_right(moments, time)
    if i == 0:
        return values[0]
    if i == len(moments):
        return values[-1]
    weight = (time - moments[i - 1]) / (moments[i] - moments[i - 1])
    return values[i - 1] + weight * (values[i] - values[i - 1])


def _decode(video: Path, width: int, height: int, step: int) -> Iterator[tuple[int, MatLike]]:
    """Every ``step``-th frame in gray at full size, starting half a step in, to stay off keyframes."""
    first = step // 2
    command = ["ffmpeg", "-v", "error", "-nostdin", "-i", str(video), "-an", "-sn", "-dn"]
    command += ["-vf", f"select='not(mod(n-{first},{step}))',format=gray", "-fps_mode", "passthrough"]
    command += ["-f", "rawvideo", "-"]
    try:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except FileNotFoundError as error:
        raise PrepError("ffmpeg not found; install FFmpeg") from error
    assert process.stdout is not None
    size = width * height
    index = 0
    with process:
        while len(buffer := process.stdout.read(size)) == size:
            yield first + index * step, np.frombuffer(buffer, np.uint8).reshape(height, width)
            index += 1
    if process.returncode:
        raise PrepError(f"ffmpeg failed on {video}")


def _track(previous: MatLike, image: MatLike, scale: float) -> tuple[Matrix | None, float | None]:
    """The similarity from ``previous`` to ``image`` and the motion of the far surface, in full size pixels.

    Each point is tracked forward and back, which drops most bad tracks, and the fit drops points that disagree with
    the main surface. Over water little is left, and what is left can be a reflection, so it needs MIN_TRACKS points.
    """
    points = cv2.goodFeaturesToTrack(previous, MAX_CORNERS, 0.01, 12, blockSize=7)
    if points is None or len(points) < MIN_TRACKS:
        return None, None
    moved, found, _ = cv2.calcOpticalFlowPyrLK(previous, image, points, points.copy(), winSize=(21, 21), maxLevel=3)
    back, returned, _ = cv2.calcOpticalFlowPyrLK(image, previous, moved, moved.copy(), winSize=(21, 21), maxLevel=3)
    error = np.linalg.norm((back - points).reshape(-1, 2), axis=1)
    good = (found.ravel() == 1) & (returned.ravel() == 1) & (error < MAX_TRACK_ERROR_PX)
    if good.sum() < MIN_TRACKS:
        return None, None
    before, after = points[good].reshape(-1, 2), moved[good].reshape(-1, 2)
    fit, inliers = cv2.estimateAffinePartial2D(before, after, method=cv2.RANSAC, ransacReprojThreshold=FIT_TOLERANCE_PX)
    if fit is None or inliers.sum() < MIN_TRACKS:
        return None, None
    motion = np.eye(3)
    motion[:2] = fit
    motion[:2, 2] *= scale
    return motion, float(np.percentile(np.linalg.norm(after - before, axis=1), SURFACE_PERCENTILE) * scale)


def _texture(image: MatLike) -> float:
    """The share of the cells of ``image`` with texture, which is low on open water and in the sky."""
    gradient_x = cv2.Sobel(image, cv2.CV_32F, 1, 0)
    gradient_y = cv2.Sobel(image, cv2.CV_32F, 0, 1)
    energy = cv2.resize(gradient_x**2 + gradient_y**2, TEXTURE_CELLS, interpolation=cv2.INTER_AREA)
    return float(np.mean(energy > TEXTURE_ENERGY))
