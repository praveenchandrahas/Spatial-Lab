#!/usr/bin/env bash
# Runs Spatial-Lab's tests (no sound card, PipeWire or browser needed). Needs python3 with numpy, scipy, aiohttp, PyYAML and pytest;
# ffmpeg is optional (the upload test is skipped without it). Called by scripts/test-all.sh in the main repository.
cd "$(dirname "$0")/.." || exit 1
python3 -c "import numpy, scipy, aiohttp, yaml, pytest" 2>/dev/null || { echo "need python3 with numpy, scipy, aiohttp, PyYAML and pytest (sudo apt install python3-numpy python3-scipy python3-aiohttp python3-yaml python3-pytest)"; exit 1; }
exec python3 -m pytest -q tests "$@"
