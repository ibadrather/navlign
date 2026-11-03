.PHONY: setup dev-setup icloud format type-check test download-data download-helenenschacht camera

# Install runtime dependencies only
setup:
	uv sync --no-dev
	@$(MAKE) --no-print-directory icloud

# Install runtime + dev dependencies (ruff, ty, pytest)
dev-setup:
	uv sync --group dev
	@$(MAKE) --no-print-directory icloud

# iCloud Drive hides everything in a directory named .venv, and Python 3.13 skips hidden .pth files.
# xattr excludes .venv from iCloud, and chflags clears what was already hidden; chflags alone is undone within a second.
icloud:
	@if [ "$$(uname)" = Darwin ]; then \
		xattr -w 'com.apple.fileprovider.ignore#P' 1 .venv; \
		chflags -R nohidden .venv; \
	fi

format:
	uv run ruff format
	uv run ruff check --fix

type-check:
	uv run ty check

test:
	uv run pytest -rs

# Download the sample datasets the end-to-end tests use into data/ (OpenDroneMap): 18 geotagged DJI images
# (BSD-2) and a short DJI video with SRT telemetry
download-data:
	@for r in drone_dataset_brighton_beach drone_dataset_dji_video; do \
		[ -d data/$$r ] || { git clone -q --depth 1 https://github.com/OpenDroneMap/$$r.git data/$$r && rm -rf data/$$r/.git; }; \
	done

# Download the Helenenschacht survey into data/ (2.5 GB, not part of download-data): 176 nadir images from an
# Autel EVO II Pro RTK with RTK accuracies, GCPs and the RINEX / MRK files, flown in Burgenland, Austria.
# Data by Daniel Dobosi, published by OpenDroneMap, GNU GPL v3:
# https://github.com/OpenDroneMap/odm_data_helenenschacht
download-helenenschacht:
	@[ -d data/odm_data_helenenschacht ] || { git clone -q --depth 1 https://github.com/OpenDroneMap/odm_data_helenenschacht.git data/odm_data_helenenschacht && rm -rf data/odm_data_helenenschacht/.git; }

# Measure the camera of a video mode for the acquisition software, from clips of that mode, e.g.
#   make camera VIDEOS=../my-field-survey-data/unisee-restaurant-survey-1
# PYTHONPATH because macOS can flag the .pth files in .venv as hidden, and Python 3.13 then skips them.
camera:
	PYTHONPATH=src uv run navlign camera $(VIDEOS)
