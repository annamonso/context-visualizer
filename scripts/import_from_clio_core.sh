#!/usr/bin/env bash
# Copy reusable code from the old context-visualizer into this scaffold so you
# start from real bodies, then refactor per MIGRATION.md. Idempotent; copies
# into *_imported/ staging dirs so it never clobbers the scaffold seam files.
set -euo pipefail

SRC="${1:-/mnt/common/amonsorodriguez/clio-core/context-visualizer}"
HERE="$(cd "$(dirname "$0")/.." && pwd)"
PKG="$HERE/src/chronolog_observability"

if [[ ! -d "$SRC/context_visualizer" ]]; then
  echo "error: $SRC/context_visualizer not found (pass old repo path as \$1)" >&2
  exit 1
fi

RSYNC=(rsync -a --exclude='__pycache__' --exclude='*.pyc')

echo ">> backend (chronolog/)"
"${RSYNC[@]}" "$SRC/context_visualizer/chronolog/" "$PKG/backend/_imported/"

echo ">> shape adapters"
"${RSYNC[@]}" "$SRC/context_visualizer/adapters/" "$PKG/adapters/shape/_imported/"

echo ">> api blueprints"
"${RSYNC[@]}" "$SRC/context_visualizer/api/" "$PKG/api/_imported/"

echo ">> capture-related glue"
"${RSYNC[@]}" "$SRC/context_visualizer/inter_agent/" "$PKG/capture/_imported/inter_agent/" || true
cp -n "$SRC/context_visualizer/llm_demo_store.py" "$PKG/capture/_imported/" 2>/dev/null || true
# Note: chimaera_client.py is intentionally NOT imported — this plugin is
# ChronoLog-only and does not depend on the chimaera runtime.

echo ">> static + templates"
"${RSYNC[@]}" "$SRC/context_visualizer/static/" "$PKG/static/" || true
"${RSYNC[@]}" "$SRC/context_visualizer/templates/" "$PKG/templates/" || true

echo ">> frontend (no node_modules)"
"${RSYNC[@]}" --exclude='node_modules' --exclude='dist' "$SRC/frontend/" "$HERE/frontend/" || true

echo ">> docs + demo scripts"
"${RSYNC[@]}" "$SRC/docs/" "$HERE/docs/_imported/" || true
"${RSYNC[@]}" --exclude='__pycache__' "$SRC/scripts/" "$HERE/scripts/_imported/" || true

echo
echo "Done. Imported code is under *_imported/ staging dirs."
echo "Now refactor it into the seam per MIGRATION.md (start with backend/)."
