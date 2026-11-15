# navlign

Turns raw drone and camera data into the dataset the [photogrammetry](https://github.com/ibadrather/photogrammetry)
pipeline reads: a `manifest.geojson` plus the media. Vendor formats stay here, so the pipeline sees one format. It was
`photogram-data-prep` in the photogrammetry repo; neither imports the other, they meet only in the dataset format.

```bash
uv tool install git+https://github.com/ibadrather/navlign.git   # the navlign command
uv add git+https://github.com/ibadrather/navlign.git            # or as a library in a uv project
```

## In and out

| Command | In | Out |
|---|---|---|
| `images` | a folder of JPEGs (DJI, Autel, RTK models) | `manifest.geojson`, `images/` (a copy of each, in file name order) |
| `video` | one video and its DJI `.SRT` (or the subtitle stream inside) | `manifest.geojson`, `video.mp4` (a copy) |
| `survey` | DJI Mini 4K clips or folders of them, plus flight logs / Swathwise missions when found | `manifest.geojson`, `images/` (picked frames), `trajectory.geojson` (only with a log), `prep_report.json` |
| `check` | as `survey` | the verdict only, nothing written; exits 1 if the data will not do |
| `camera` | as `survey` | the camera, printed |
| `inspect` | one video | `<clip>.nav.geojson`, `<clip>.camera.json` |

Every dataset also gets the `--attribution` files (licence, credits). The pipeline needs only the manifest and the
media; the rest is for people and checks. A Swathwise mission end to end:

```bash
export NAVLIGN_NAV=~/…/swathwise/data/missions       # once: where flight logs are found
navlign survey <clips_dir> -o <dataset_dir>          # finds the finest log that fits each clip
```

## Usage

### Images

```
navlign images <images_dir> -o <dataset_dir> [options]
```

| Option | Default | |
|---|---|---|
| `--altitude relative\|absolute` | `relative` | Height above take-off (DJI XMP) or EXIF GPS altitude |
| `--vertical-datum ellipsoid\|egm96` | — | Required with `--altitude absolute` |
| `--timezone NAME` | — | IANA zone of the camera clock; without it no `time_utc` is written |
| `--sensor-width-mm MM` | known / derived | Override the sensor width |
| `--shutter rolling\|global` | `rolling` | Not recorded in EXIF |
| `--attribution FILE` | — | Copied into the dataset; repeatable |
| `--overwrite` | off | Replace an existing manifest and `images/` |

Library: `navlign.prepare_images(...)`, `images.py`.

### Video

```
navlign video <video> -o <dataset_dir> [options]
```

Needs `ffprobe` (FFmpeg) on the `PATH` for frame times.

| Option | Default | |
|---|---|---|
| `--srt FILE` | `<video>.SRT` / `.srt`, then the subtitle stream | DJI telemetry recorded with the video |
| `--altitude relative\|absolute` | `relative` | SRT `rel_alt` / `altitude`, or `abs_alt` |
| `--vertical-datum`, `--timezone` | | As for images (drone clock) |
| `--focal-length-mm MM` `--sensor-width-mm MM` | 35 mm equivalent | Physical camera, given together |
| `--shutter`, `--attribution`, `--overwrite` | | As for images |

Library: `navlign.prepare_video(...)`, `video.py`.

### Survey

A survey is several clips and many gigabytes, of which a frame every few metres is enough. `survey` reads the telemetry
and the pixels, measures the camera, picks the frames, writes the dataset and says whether to trust it:

```
navlign survey <video-or-dir> [...] -o <dataset_dir> [options]
navlign check  <video-or-dir> [...] [options]
navlign camera <video-or-dir> [...]
make camera VIDEOS=<video-or-dir>                  # camera, from this directory
```

Needs FFmpeg. A directory means the videos directly in it, in file name order. Takes about a tenth of the footage's
length, plus as much again to write the frames. No video is copied: the output is an image dataset.

| Option | Default | |
|---|---|---|
| `--overlap F` | `0.85` | Share of a frame the next one still shows, measured on the pixels |
| `--focal-length-mm MM` `--sensor-width-mm MM` | measured | A camera to use instead; `check` compares it with the footage |
| `--nav PATH` | the video folders and their subfolders, then `NAVLIGN_NAV` | Flight logs or Swathwise mission folders; repeatable. Optional: keep the mission folder with the videos and it is found |
| `--shutter`, `--attribution`, `--overwrite` | | As for images |

Images are named `DJI_0153_000437.jpg` (clip, frame). Library: `navlign.prepare_survey(...)` and `check_survey(...)`,
`survey.py`.

#### Navigation

The telemetry in a Mini 4K clip is one line a second, position rounded to 4 decimals (about 11 m), no attitude. The DJI
Fly flight log has 10 Hz, full position, speeds and aircraft and gimbal attitude, and
[Swathwise](https://github.com/ibadrather/swathwise) keeps it in the mission folder. `survey` searches all the way down
the folders of the videos, `--nav` and `NAVLIGN_NAV` (several, separated like `PATH`) for, finest first:

| File | |
|---|---|
| `frames-N.csv`, `FlightRecord_*.csv` | the flight log as `dji-log --csv` writes it, about 10 Hz |
| `flown-N_high_res.geojson` | the same frames as GeoJSON; read when no `frames-N.csv` is next to it |
| `flown-N.geojson` | the fixes Swathwise received live: one every 1.3 s, 7 decimals, no gimbal attitude |

Only files covering the clips' time are read. Each clip takes the finest one it fits, else its own telemetry (`nav.py`).
`survey` and `check` print a `navigation` box before reading the clips (where they looked, the files found) and end
with a summary box: clips, frames, the file each clip was fitted to, files found but not fitted, outputs and verdict.

- **Fit.** `creation_time` is whole seconds and 1 to 3.5 s early on the Mini 4K, and its 24 fps mode runs at 0.8 of
  real time. So each clip is fitted as `time = creation_time + offset + scale * video seconds`, roughly by position,
  then by speed profile (video `H.S` against log speed; 0.9996 or better on the 2026-11-01 surveys). Refused when
  positions differ over 10 m rms or speeds correlate under 0.9.
- **Per frame**, interpolated between the two log frames around it: `geometry` (height above take-off), gimbal `yaw`,
  `pitch`, `roll`, and beside the contract `speed_north_ms`, `speed_east_ms`, `speed_down_ms`, `aircraft_yaw`,
  `aircraft_pitch`, `aircraft_roll`, `satellites`, `nav_source` (the file used). `time_utc` is the fitted time. A frame
  in a log gap over 2 s keeps the video position and has no `nav_source`.
- **Real time from there on**, so camera, distance flown and motion blur are right for 24 fps clips too (without a
  log their focal length comes out 20 % short).
- **`trajectory.geojson`**: every log frame used, a Point `[lon, lat, height]` with `time_utc`, the properties above,
  `recording` and `source`, within `mission.json`'s times when present. Named in the manifest under
  `photogrammetry.navigation`; the pipeline does not need it.
- `prep_report.json` gives each clip `nav`, `time_scale`, `clock_offset_s`, `nav_frames`; the `navigation` check warns
  when a log was there and a clip did not fit.

#### Picking frames

Five times a second OpenCV tracks a few hundred points between frames (`scan.py`), giving rotation, scale and shift,
chained from the last kept frame. The next frame is kept when the share still in view falls to `--overlap`. So no
camera model or height goes in and overlap holds for whatever is in view (trees get more frames than a car park); a
hover or back-and-forth adds nothing; turns and climbs cost overlap as they should; frames with under a quarter
texture yield nothing — open water can't be mapped, so it is left out and reported, not a fault, and each stretch of
land starts with a new frame. Where overlap runs out, a frame over 5 % softer than its neighbours gives way to a
slightly earlier one (sharpness compared only within a second). Feature matching stays in sfm.

#### The camera

`camera` prints focal length, field of view, footprint, ground resolution and the speed that keeps motion blur to 1 px
at the exposure flown, then a block for the acquisition software:

```json
{"type": "camera", "sensor_w_mm": 6.17, "sensor_h_mm": 3.471, "focal_mm": 5.236, "image_w_px": 2720, "image_h_px": 1530}
```

Footprint = `height * sensor / focal`. It describes the video mode (which crops the sensor), not the spec sheet; only
the sensor/focal ratio counts. Focal length is `f = flow * H / v` (`camera.py`): image motion from the pixels against
telemetry speed and height, using the slow points of each sample pair in steady straight flight over land (≥ 1 m/s),
and the low end of those — trees move faster, water reflections slower. Treat it as ±5 %: `H` is above take-off, not
the ground. sfm refines it and on this drone ends 6 to 11 % shorter with a warning; that is expected, and re-preparing
with sfm's value changes nothing.

#### Checks

Each is a number against a threshold in `prep_report.json`; the verdict is the worst.

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
| `navigation` | A flight log was found and a clip, or some of its frames, did not fit it |

#### What the Mini 4K footage taught

From nine 2.7K and two 4K clips of 2026-10-30 and 2026-10-31:

- **Keyframes look 50 % sharper and are not better.** Laplacian variance jumps on each I frame (once a second, settling
  over three frames), but SIFT matches were no better than P frames at the same baseline (218 / 220, 259 / 278,
  141 / 153, 764 / 889 inliers): the extra is grain. Samples sit between keyframes.
- **Real blur is rare**: a handful of samples per clip 15 % softer than their neighbours. Stepping around them helps;
  hunting the sharpest frame does not.
- **2.7K shows the same view as 4K** within 5 % scatter: no different crop.
- **Two focal length measures differ by 6 to 11 %.** Bundle adjustment reaches one value from any start (2045 px at
  2.7K from 1940, 2308 and 2733), likely helped by the camera's 2 to 3° off nadir, but it moves with the site (2031,
  2045, 2131 px) and sits under image motion's, at 4K too (about 3100 px from three heights vs 2868). Plan with image
  motion footprints. A hover at two heights over one road would settle which depth is right; until then read DSM
  relief as possibly 10 % low.
- **Water lies**: over a lake the focal length came out 25 % short — reflected trees sit below the ground.
- **Keyframe-only decoding is no shortcut** (about 1.5× faster, on the noisiest frames); `-hwaccel` was slower than
  software here.

### Inspect

```
navlign inspect <video> [-o <dir>] [--srt FILE] [--rows N] [--overwrite]
```

Prints container, tags, MP4 user data, streams, frames and telemetry, and writes next to the video (or to `-o`):

```
DJI_0173.nav.geojson   # a point per telemetry entry: position, height, speeds, satellites, home distance, raw line;
                       # navigation.at_start: aircraft and gimbal attitude and speed when recording started
DJI_0173.camera.json   # exposure per entry; container, streams, device, user data; a row per frame
```

Both share `index`, `start_s`, `end_s` (video seconds) and `time_utc` per entry; each frame names its `entry`, each
point its `first_frame` and `last_frame`. `time_utc` = container `creation_time` (recording start, UTC; on the Mini 4K
to the second) + video seconds. Nothing is decoded: a 2 GB clip takes about two seconds. `--rows N` prints N entries
from start and end, `0` all. Library: `navlign.read_metadata(...)`, `write_metadata(...)`, `metadata.py`.

A Mini 4K clip holds, beyond the per-second telemetry:

- `moov/udta` (`boxes.py`): `©fpt` `©fyw` `©frl` aircraft and `©gpt` `©gyw` `©grl` gimbal pitch, yaw, roll at
  recording start, `©xsp` `©ysp` `©zsp` speeds, `©xyz` location, `©mdl` model (`FC7703`), `©csn` serial, `©res` video
  mode, `Xtra` versions. Unknown atoms are kept as hex.
- `DJI.Meta` data stream: on this drone 250 bytes of filler per packet. Newer models write attitude there (not decoded;
  `inspect` says when a stream holds more than filler).
- No attitude or IMU per entry or frame.

## What is read

Images:

| Manifest | From |
|---|---|
| `geometry` `[lon, lat, alt]` | EXIF GPS lat / lon; altitude per `--altitude`. `null` without a fix (DJI 0/0 counts as none) |
| `yaw`, `pitch`, `roll` | DJI XMP `GimbalYawDegree` / `GimbalPitchDegree` / `GimbalRollDegree` (camera, not airframe); Autel writes the same |
| `time_utc` | EXIF `DateTimeOriginal` + `--timezone` |
| `cameras` | EXIF make, model, focal length, size; sensor width from a small table, else from the 35 mm equivalent |
| `h_acc_m`, `v_acc_m` | RTK models (Phantom 4 RTK, EVO II RTK): XMP `RtkStdLat` / `RtkStdLon` (the larger), `RtkStdHgt` only with `--altitude absolute`. Otherwise the pipeline default |

Video, one feature per frame (`frame_index`, decoder presentation order):

| Manifest | From |
|---|---|
| frame ↔ SRT entry | the entry whose interval contains the frame's presentation time (ffprobe); frames outside every entry get `geometry: null` |
| `geometry` `[lon, lat, alt]` | SRT `latitude` / `longitude`; altitude per `--altitude`. 0/0 counts as no fix |
| `yaw`, `pitch`, `roll` | SRT `gb_yaw` / `gb_pitch` / `gb_roll` where written (not on the Mini 2) |
| `time_utc` | SRT clock line (`2023-01-11 17:01:55,343`) + `--timezone`, to the millisecond |
| `cameras` | ffprobe frame size; SRT `focal_len` as 35 mm equivalent, with the frame's equivalent sensor width (43.27 mm diagonal) |

Survey, one feature per kept frame (`image`, plus `video` and `video_frame` to trace it). Telemetry is the Mini 4K
line, once a second, from the subtitle stream or an `.SRT` beside the clip; `H.S`, `H`, `SS` measure camera and blur,
`DZOOM` and satellites feed the checks:

| Manifest | From |
|---|---|
| `geometry` `[lon, lat, alt]` | The flight log when one fits ([navigation](#navigation)). Else `GPS (lon, lat, satellites)` averaged over ±5 s to undo rounding, linear between entries; `H` as height above take-off. 0/0 counts as no fix |
| `time_utc` | MP4 `creation_time` (UTC, whole seconds) + presentation time, both corrected by the log fit when there is one |
| `cameras` | Measured, or `--focal-length-mm` / `--sensor-width-mm`; ffprobe frame size. Clips of different sizes are refused |
| `yaw`, `pitch`, `roll` | The gimbal's, from the flight log; not written without one |

Notes:

- **Altitude** defaults to `relative_takeoff`: DJI's barometric height above take-off, consistent within a flight. DJI
  EXIF `GPSAltitude` has no documented datum and varies by model and firmware, so `--altitude absolute` makes you
  state it.
- **Orientation** is `ned_yaw_pitch_roll_deg`: yaw clockwise from north, pitch 0 horizontal and −90 nadir, roll
  positive right side down, degrees — as proposed in the design notes, and as DJI gimbal angles already are.
- **RTK accuracy** is the receiver's for its fix, leaving out camera timing and antenna offset, so it can be
  optimistic; georef says so when residuals are much larger (σ0 ≫ 1).
- **Sensor width** from the 35 mm equivalent is wrong for cropped modes (16:9 on a 4:3 sensor), hence the model
  table first. DJI video is usually cropped and the SRT does not name the model: pass `--focal-length-mm` and
  `--sensor-width-mm` when known; bundle adjustment refines the rest.
- **SRT formats**: `[key: value]` groups of recent DJI models (Mini 2 / SE, Mini 3, Mavic 3, Air 2S). `focal_len` ≥ 100
  is read as tenths of a mm (Mini 2 writes `240`). The Phantom 4 `GPS(lon,lat,alt) BAROMETER:` format is not read.
  The Mini 4K line (`GPS (lon, lat, satellites), D .. H .. H.S ..`) is read by `video` and `survey`; `survey` reads
  only it, as it needs the speed.

## Not yet

- Interpolating positions between SRT entries in `video` (`survey` does); digital zoom (`dzoom_ratio`).
- Dropping frames where clips fly the same ground twice; heights across separate take-offs; telling a tilted gimbal
  from nadir (not in the telemetry).
- Flight logs in `images` and `video`; GPX; lever arm and boresight; accuracy conversion (CEP / 2-sigma → 1-sigma).

## Test data

Not committed; fetched into `data/`. Tests skip a dataset that is missing.

- **Brighton Beach** (OpenDroneMap, <https://github.com/OpenDroneMap/drone_dataset_brighton_beach>): 18 images, DJI
  Phantom 3 (FC300S), © 2017 Piero Toffanin, BSD 2-Clause. `make download-data`, then:

  ```
  uv run navlign images data/drone_dataset_brighton_beach/images -o data/brighton_beach \
      --timezone America/Chicago --attribution data/drone_dataset_brighton_beach/LICENSE
  ```

- **DJI video** (OpenDroneMap, <https://github.com/OpenDroneMap/drone_dataset_dji_video>): 46 s of 960x540 at 59.94 fps
  with a Mini 2 SRT, near Milan. No licence given; local testing only.

  ```
  uv run navlign video data/drone_dataset_dji_video/DJI_0317.MP4 -o data/dji_video --timezone Europe/Rome
  ```

- **Helenenschacht** (OpenDroneMap, <https://github.com/OpenDroneMap/odm_data_helenenschacht>): 176 nadir images
  (5472x3648), Autel EVO II Pro RTK, with RTK accuracies, GCPs and RINEX / MRK, Burgenland, Austria. Data by Daniel
  Dobosi via the [OpenDroneMap forum](https://community.opendronemap.org/t/gcp-projected-coordinates/11762/7), GPL v3.
  2.5 GB: `make download-helenenschacht`. Heights are ellipsoidal (EXIF `GPSAltitude` = MRK `Ellh`), clock is CEST.
  GCPs and RINEX / MRK are not read yet.

  ```
  uv run navlign images data/odm_data_helenenschacht/images -o data/helenenschacht \
      --altitude absolute --vertical-datum ellipsoid --timezone Europe/Vienna \
      --attribution data/odm_data_helenenschacht/README.md --attribution data/odm_data_helenenschacht/LICENSE
  ```

## Development

`make dev-setup` installs with dev tools into `.venv` and excludes it from iCloud Drive (which hides its `.pth` files).
Then `uv run pytest`, `uv run ruff check`, `uv run ty check`, or `make test`, `make format`, `make type-check`. Tests
that make a video skip without FFmpeg.

Every code change bumps the version in `pyproject.toml` and runs `uv lock`; CI fails a change to `src/` without a bump.
