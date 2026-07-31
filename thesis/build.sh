#!/bin/bash
# Build the thesis PDF. Requires the one-time setup:
#   conda create -y -n texbuild -c conda-forge tectonic
# Tectonic fetches any missing LaTeX packages on first run and caches them.
set -e
cd "$(dirname "$0")"
export TECTONIC_CACHE_DIR="${TECTONIC_CACHE_DIR:-/mnt/common/$USER/.tectonic-cache}"
conda run -n texbuild tectonic -X compile main.tex --keep-logs
echo
echo "built: $(pwd)/main.pdf"
grep -c "Overfull" main.log | xargs echo "overfull boxes:"
grep -c "undefined" main.log | xargs echo "undefined refs/cites:"
