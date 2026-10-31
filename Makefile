.PHONY: setup camera

# Install this package into the workspace environment, the .venv at the repository root.
# --inexact because a plain uv sync from here uninstalls what only the root package needs, such as pycolmap.
# xattr because iCloud Drive hides everything in a directory named .venv, and Python 3.13 skips hidden .pth files.
# It excludes .venv from iCloud, and chflags clears what was already hidden; chflags alone is undone within a second.
setup:
	uv sync --no-dev --inexact
	@if [ "$$(uname)" = Darwin ]; then \
		xattr -w 'com.apple.fileprovider.ignore#P' 1 ../../.venv; \
		chflags -R nohidden ../../.venv; \
	fi

# Measure the camera of a video mode for the acquisition software, from clips of that mode, e.g.
#   make camera VIDEOS=../../../my-field-survey-data/unisee-restaurant-survey-1
# PYTHONPATH because macOS can flag the .pth files in .venv as hidden, and Python 3.13 then skips them.
camera:
	PYTHONPATH=src uv run photogram-data-prep camera $(VIDEOS)
