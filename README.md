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

## What is read

| Manifest | From |
|---|---|
| `geometry` `[lon, lat, alt]` | EXIF GPS latitude / longitude; altitude per `--altitude`. `null` without a fix (DJI 0/0 counts as none) |
| `yaw`, `pitch`, `roll` | DJI XMP `GimbalYawDegree` / `GimbalPitchDegree` / `GimbalRollDegree` (camera, not airframe) |
| `time_utc` | EXIF `DateTimeOriginal` + `--timezone` |
| `cameras` | EXIF make, model, focal length, image size; sensor width from a small table, else from the 35 mm equivalent focal length |
| `h_acc_m`, `v_acc_m` | not written: consumer DJI images carry no accuracy, so the pipeline default applies |

Notes:

- **Altitude.** The default is `relative_takeoff`: DJI's barometric height above take-off, consistent within
  a flight. EXIF `GPSAltitude` on DJI cameras has no documented datum and differs between models and firmware,
  so `--altitude absolute` makes you state it.
- **Orientation** is written as `ned_yaw_pitch_roll_deg`: yaw clockwise from north, pitch 0 = horizontal and
  −90 = nadir, roll positive right side down, all in degrees. This is the convention proposed in the design
  notes, and DJI gimbal angles already follow it.
- **Sensor width** derived from the 35 mm equivalent is wrong for cropped modes (e.g. 16:9 on a 4:3 sensor),
  which is why known models are looked up first.

## Not yet

- Video + DJI `.SRT` telemetry (per-frame position, `frame_index`).
- GPX / flight logs, lever arm and boresight corrections, accuracy conversion (CEP / 2-sigma → 1-sigma).

## Test data

Tests and examples use the **Brighton Beach** dataset from OpenDroneMap,
<https://github.com/OpenDroneMap/drone_dataset_brighton_beach>: 18 images from a DJI Phantom 3 (FC300S),
Copyright (c) 2017 Piero Toffanin, BSD 2-Clause License. The data is not committed: fetch it with `make data`
from the photogrammetry repository root, then prepare it (the dataset's `LICENSE` travels with it):

```
uv run photogram-data-prep images ../../data/drone_dataset_brighton_beach/images -o ../../data/brighton_beach \
    --timezone America/Chicago --attribution ../../data/drone_dataset_brighton_beach/LICENSE
```

## Development

`uv sync` at the root installs it. In this directory: `uv run pytest`, `uv run ruff check src tests`, `uv run ty check src tests`.
Ruff settings come from the photogrammetry root `ruff.toml`; copy it when the package moves out. The Brighton
Beach test is skipped when `../../data` has no copy of the dataset.
