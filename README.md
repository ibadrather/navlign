# photogram-data-prep

Turns raw drone and camera data into the dataset read by the
[photogrammetry](../../README.md) pipeline: a `manifest.geojson` plus the media. Vendor formats are handled
here so the pipeline only ever sees one format.

It sits in `packages/` of the photogrammetry repo for now as a uv workspace member: the pipeline depends on it
(editable) and the workspace shares one `uv.lock` at the root. It has no imports from the pipeline, so it can
be moved to its own repository as is; then run `uv lock` there and point the pipeline's
`[tool.uv.sources]` entry at the new repo.

## Usage

```
photogram-data-prep images <images_dir> -o <dataset_dir> [options]
```

| Option | Default | |
|---|---|---|
| `--altitude relative\|absolute` | `relative` | Height above take-off (DJI XMP) or EXIF GPS altitude |
| `--vertical-datum ellipsoid\|egm96` | — | Required with `--altitude absolute` |
| `--timezone NAME` | — | IANA zone of the camera clock; without it no `time_utc` is written |
| `--sensor-width-mm MM` | known / derived | Override the sensor width |
| `--shutter rolling\|global` | `rolling` | Not recorded in EXIF |
| `--attribution FILE` | — | Licence or credits to copy into the dataset; repeatable |
| `--overwrite` | off | Replace an existing manifest and `images/` |

The same is available as `photogram_data_prep.prepare_images(...)`; all of it lives in `images.py`.

Output:

```
dataset_dir/
  manifest.geojson
  images/          # a copy of each input JPEG, in file name order
  LICENSE ...      # --attribution files
```

### Video

```
photogram-data-prep video <video> -o <dataset_dir> [options]
```

Needs `ffprobe` (FFmpeg) on the `PATH` to read frame times.

| Option | Default | |
|---|---|---|
| `--srt FILE` | `<video>.SRT` / `.srt`, then the subtitle stream inside the video | DJI telemetry recorded with the video |
| `--altitude relative\|absolute` | `relative` | SRT `rel_alt` / `altitude`, or `abs_alt` |
| `--vertical-datum ellipsoid\|egm96` | — | Required with `--altitude absolute` |
| `--timezone NAME` | — | IANA zone of the drone clock; without it no `time_utc` is written |
| `--focal-length-mm MM` `--sensor-width-mm MM` | 35 mm equivalent | Physical camera, given together |
| `--shutter`, `--attribution`, `--overwrite` | | As for images |

`photogram_data_prep.prepare_video(...)`, in `video.py`. Output:

```
dataset_dir/
  manifest.geojson
  video.mp4        # a copy of the input video
  LICENSE ...      # --attribution files
```

### Survey

A survey is several clips and many gigabytes, of which a frame every few metres is enough. Point `survey` at the clips
and it reads the telemetry and the pixels, measures the camera, picks the frames, writes the dataset and says whether
to trust it:

```
photogram-data-prep survey <video-or-dir> [...] -o <dataset_dir> [options]
photogram-data-prep check  <video-or-dir> [...] [options]     # the same, writing nothing; exits 1 if the data will not do
photogram-data-prep camera <video-or-dir> [...]               # only the camera
make camera VIDEOS=<video-or-dir>                             # the same, from this directory
```

Needs FFmpeg. A directory stands for the videos directly in it, in file name order. It takes about a tenth of the
length of the footage, plus as much again to write the frames.

| Option | Default | |
|---|---|---|
| `--overlap F` | `0.85` | Share of a frame the next one still shows, measured on the pixels |
| `--focal-length-mm MM` `--sensor-width-mm MM` | measured | A camera to use instead; `check` compares it with the footage |
| `--shutter`, `--attribution`, `--overwrite` | | As for images |

`photogram_data_prep.prepare_survey(...)` in `survey.py`, `check_survey(...)` next to it. No video is copied: the output
is an image dataset, so the pipeline needs nothing else.

```
dataset_dir/
  manifest.geojson
  images/            # DJI_0153_000437.jpg: clip and frame number
  prep_report.json   # verdict, each check, each clip, the camera
  LICENSE ...        # --attribution files
```

Only the video and its telemetry go in; no mission plan or flight log is read.

#### Picking frames

Five times a second the tool tracks a few hundred points from one frame to the next, with OpenCV (`scan.py`), which gives
a rotation, a scale and a shift. These are chained from the last frame kept, and the next frame is kept when the share
of the image still in view falls to `--overlap`. So:

- no camera model and no height go into the choice, and the overlap is true of whatever the camera was looking at:
  trees get more frames than a car park;
- a hover adds nothing, and going back and forth adds nothing;
- a turn or a climb costs overlap as it should;
- images with no texture, under a quarter of the frame, yield no frames. That is open water: it cannot be mapped, so
  it is left out and reported, not counted as a fault. Each stretch of land starts again with a new frame.

Where the overlap runs out, a frame more than 5 % softer than its neighbours gives way to a slightly earlier one.
Sharpness is only compared within a second, as it also depends on what is in view.

Finding features and matching them across the survey stays in sfm: nothing here tries to do that job twice.

#### The camera

`camera` prints the focal length, field of view, footprint, ground resolution and the speed that keeps motion blur to
1 px at the exposure flown, then a block to paste into the acquisition software:

```json
{"type": "camera", "sensor_w_mm": 6.17, "sensor_h_mm": 3.471, "focal_mm": 5.236, "image_w_px": 2720, "image_h_px": 1530}
```

Footprints come out right when computed as `height * sensor / focal`. It is the video mode that is described, not the
sensor on the spec sheet: the mode crops it. The sensor width is a convention, only the ratio to the focal length counts.

The focal length comes from `f = flow * H / v`: how fast the ground moves in the image, from the pixels, against the
speed and height from the telemetry (`camera.py`). Trees are closer and move faster, and water can show reflections,
which move too slowly, so it uses the slow points of each pair of samples, in steady straight flight over land, and
the low end of those. It needs steady flight of at least 1 m/s over land.

Treat it as ±5 %: `H` is above the take-off point, not above the ground under the camera. It describes the footprint
at a telemetry height. sfm refines it, and on this drone ends 6 to 11 % shorter and says so in a warning; that warning
is expected, and preparing the dataset again with sfm's value would change nothing, as sfm reaches it from any start.

#### Checks

Each is a number against a threshold, written to `prep_report.json`. The verdict is the worst of them.

| Check | Warns when |
|---|---|
| `frames` | Fails under 3 frames |
| `front overlap` | The worst 5 % of pairs fall 10 points under the target, on the pixels |
| `coverage` | The frames fall into separate blocks by position, which sfm will not join |
| `side overlap` | Lines overlap under 60 %, from telemetry positions and the footprint |
| `sharpness` | Over 10 % of kept frames are softer than 85 % of their neighbours |
| `motion blur` | Over 5 % of kept frames blur more than 2 px, from speed, shutter and height |
| `telemetry` | Digital zoom was used, frames have no position, or under 10 satellites |
| `featureless` | Never: how much of the footage had nothing to map |
| `camera` | A camera passed in is over 10 % from the footage, or the footage could not confirm it |

#### What the Mini 4K footage taught

From nine 2.7K and two 4K clips of 2026-10-30 and 2026-10-31:

- **A keyframe looks 50 % sharper and is not better.** The variance of the Laplacian jumps on each I frame, once a
  second, and takes three frames to settle, but SIFT matches between I frames were no better than between P frames at
  the same baseline (218 / 220, 259 / 278, 141 / 153, 764 / 889 inliers). The extra is grain. Samples sit between
  keyframes, so this does not decide which frame is kept.
- **Real blur is rare**: a handful of samples per clip are 15 % softer than those around them. It helps to step around
  them; hunting the sharpest frame does not.
- **2.7K shows the same view as 4K**, within the 5 % scatter of these measurements: no sign of a different crop.
- **Two measures of the focal length differ by 6 to 11 %.** On one patch of frames bundle adjustment reaches one value
  from any start (2045 px at 2.7K from 1940, 2308 and 2733), and it does separate focal length from depth, likely thanks
  to the 2 to 3° the camera sits off nadir. But the value moves with the site (2031, 2045, 2131 px at 2.7K for one lens)
  and sits under the one from image motion, at 4K too, where three flight heights give about 3100 px and it gives 2868.
  Footprints from the image motion are the safe number for planning. Which depth is right is open: a hover at two
  heights over one road would settle it, as the slope of pixels per metre needs no take-off height. Until then read
  relief in a DSM as possibly 10 % low.
- **Water can lie.** Tracks over a lake gave a focal length 25 % too short: reflections of trees sit below the ground.
- **Decoding only keyframes is no shortcut**, about 1.5 times faster and on the noisiest frames, and `-hwaccel` was
  slower than software on this machine.

## What is read

| Manifest | From |
|---|---|
| `geometry` `[lon, lat, alt]` | EXIF GPS latitude / longitude; altitude per `--altitude`. `null` without a fix (DJI 0/0 counts as none) |
| `yaw`, `pitch`, `roll` | DJI XMP `GimbalYawDegree` / `GimbalPitchDegree` / `GimbalRollDegree` (camera, not airframe); Autel writes the same block |
| `time_utc` | EXIF `DateTimeOriginal` + `--timezone` |
| `cameras` | EXIF make, model, focal length, image size; sensor width from a small table, else from the 35 mm equivalent focal length |
| `h_acc_m`, `v_acc_m` | RTK models (Phantom 4 RTK, EVO II RTK): XMP `RtkStdLat` / `RtkStdLon` (the larger), and `RtkStdHgt` with `--altitude absolute` only. Consumer images carry none, so the pipeline default applies |

Video, one feature per frame (`frame_index`, presentation order as a decoder outputs them):

| Manifest | From |
|---|---|
| frame ↔ SRT entry | the entry whose subtitle interval contains the frame's presentation time (ffprobe); frames outside every entry get `geometry: null` |
| `geometry` `[lon, lat, alt]` | SRT `latitude` / `longitude`; altitude per `--altitude`. 0/0 counts as no fix |
| `yaw`, `pitch`, `roll` | SRT `gb_yaw` / `gb_pitch` / `gb_roll` where the model writes them (the Mini 2 does not) |
| `time_utc` | SRT clock line (`2023-01-11 17:01:55,343`) + `--timezone`, to the millisecond |
| `cameras` | ffprobe frame size; SRT `focal_len` as the 35 mm equivalent focal length, with the equivalent sensor width of the frame (43.27 mm diagonal) |

Survey, one feature per kept frame (`image`, plus `video` and `video_frame` to trace it back). The telemetry is the
Mini 4K line, one entry per second, from the subtitle stream inside the clip or an `.SRT` next to it. `H.S`, `H` and `SS`
measure the camera and the blur, `DZOOM` and the satellite count go to the checks:

| Manifest | From |
|---|---|
| `geometry` `[lon, lat, alt]` | `GPS (lon, lat, satellites)`, averaged over ±5 s to undo its rounding, between entries linear; `H` as a height above take-off. 0/0 counts as no fix |
| `time_utc` | MP4 `creation_time` (UTC, whole seconds) + the frame's presentation time |
| `cameras` | Measured from the footage, or `--focal-length-mm`, `--sensor-width-mm`; ffprobe frame size. Clips of different sizes are refused |
| `yaw`, `pitch`, `roll` | not written: this telemetry has none |

Notes:

- **Altitude.** The default is `relative_takeoff`: DJI's barometric height above take-off, consistent within
  a flight. EXIF `GPSAltitude` on DJI cameras has no documented datum and differs between models and firmware,
  so `--altitude absolute` makes you state it.
- **Orientation** is written as `ned_yaw_pitch_roll_deg`: yaw clockwise from north, pitch 0 = horizontal and
  −90 = nadir, roll positive right side down, all in degrees. This is the convention proposed in the design
  notes, and DJI gimbal angles already follow it.
- **RTK accuracy** is what the receiver reports for its fix. It leaves out camera timing and the antenna to camera
  offset, so it can be optimistic; georef says so when its residuals are much larger (σ0 ≫ 1).
- **Sensor width** derived from the 35 mm equivalent is wrong for cropped modes (e.g. 16:9 on a 4:3 sensor),
  which is why known models are looked up first. DJI video is usually such a crop, and the SRT does not name the
  model: pass `--focal-length-mm` and `--sensor-width-mm` when you know them; bundle adjustment refines the rest.
- **SRT formats.** Values are read from the `[key: value]` groups of recent DJI models (Mini 2 / SE, Mini 3,
  Mavic 3, Air 2S). `focal_len` of 100 or more is read as tenths of a millimetre (the Mini 2 writes `240`). The
  older `GPS(lon,lat,alt) BAROMETER:` format of the Phantom 4 is not read. The Mini 4K line (`GPS (lon, lat,
  satellites), D .. H .. H.S ..`) is read by both `video` and `survey`; `survey` reads no other format, because
  it needs the speed.

## Not yet

- Interpolating positions between SRT entries in `video` (`survey` does); digital zoom (`dzoom_ratio`).
- Dropping frames across clips that fly the same ground twice; heights across separate take-offs; telling a tilted
  gimbal from a nadir one, which the telemetry does not record.
- GPX / flight logs, lever arm and boresight corrections, accuracy conversion (CEP / 2-sigma → 1-sigma).

## Test data

Tests and examples use the **Brighton Beach** dataset from OpenDroneMap,
<https://github.com/OpenDroneMap/drone_dataset_brighton_beach>: 18 images from a DJI Phantom 3 (FC300S),
Copyright (c) 2017 Piero Toffanin, BSD 2-Clause License. The data is not committed: fetch it with `make download-data`
from the photogrammetry repository root, then prepare it (the dataset's `LICENSE` travels with it):

```
uv run photogram-data-prep images ../../data/drone_dataset_brighton_beach/images -o ../../data/brighton_beach \
    --timezone America/Chicago --attribution ../../data/drone_dataset_brighton_beach/LICENSE
```

The video tests use the **DJI video** dataset from OpenDroneMap, <https://github.com/OpenDroneMap/drone_dataset_dji_video>:
46 s of 960x540 video at 59.94 fps with an SRT in the DJI Mini 2 format, flown near Milan. No licence is given; local
testing only.

```
uv run photogram-data-prep video ../../data/drone_dataset_dji_video/DJI_0317.MP4 -o ../../data/dji_video \
    --timezone Europe/Rome
```

The RTK tests use the **Helenenschacht** survey from OpenDroneMap,
<https://github.com/OpenDroneMap/odm_data_helenenschacht>: 176 nadir images (5472x3648) from an Autel EVO II Pro RTK
with RTK accuracies, GCPs and the RINEX / MRK files, flown in Burgenland, Austria. Data by Daniel Dobosi, contributed
through the [OpenDroneMap community forum](https://community.opendronemap.org/t/gcp-projected-coordinates/11762/7),
GNU General Public License v3. It is 2.5 GB, so it has its own target, `make download-helenenschacht`. Its heights
are ellipsoidal (EXIF `GPSAltitude` matches the MRK `Ellh`) and the camera clock is local time (CEST):

```
uv run photogram-data-prep images ../../data/odm_data_helenenschacht/images -o ../../data/helenenschacht \
    --altitude absolute --vertical-datum ellipsoid --timezone Europe/Vienna \
    --attribution ../../data/odm_data_helenenschacht/README.md --attribution ../../data/odm_data_helenenschacht/LICENSE
```

The GCPs and the RINEX / MRK files are not read yet.

## Development

`uv sync` at the root installs it, or `make setup` in this directory, which leaves the root package's dependencies alone
and excludes `.venv` from iCloud Drive, which otherwise hides its `.pth` files from Python. In this directory: `uv run pytest`, `uv run ruff check src tests`, `uv run ty check src tests`.
Ruff settings come from the photogrammetry root `ruff.toml`; copy it when the package moves out. The Brighton
Beach, DJI video and Helenenschacht tests are skipped when `../../data` has no copy of the dataset; tests that make a video
are skipped without FFmpeg.
