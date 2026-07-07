#!/usr/bin/env bash
# Seed the offline demo data (once), then launch the dashboard.
#
# Runs the three shipped feature demos (Doctor / Fleet / Tracing) against a
# shared DTP_STATE_DIR so the dashboard comes up fully populated instead of an
# empty shell. Seeding is skipped when the state dir already has data or when
# SEED_DEMO is unset — so a mounted volume persists across restarts.
set -euo pipefail

STATE_DIR="${DTP_STATE_DIR:-/data}"
mkdir -p "$STATE_DIR"

seed_needed=0
if [ "${SEED_DEMO:-1}" = "1" ] && [ -z "$(ls -A "$STATE_DIR" 2>/dev/null || true)" ]; then
  seed_needed=1
fi

if [ "$seed_needed" = "1" ]; then
  echo "[entrypoint] seeding offline demo data into $STATE_DIR ..."
  export DEMO_STATE_DIR="$STATE_DIR"
  python3 scripts/demos/demo_doctor.py           >/dev/null || echo "[entrypoint] WARN: doctor seed failed (non-fatal)"
  python3 scripts/demos/demo_fleet_scale.py --nodes 60 >/dev/null || echo "[entrypoint] WARN: fleet seed failed (non-fatal)"
  python3 scripts/demos/demo_tracing.py          >/dev/null || echo "[entrypoint] WARN: tracing seed failed (non-fatal)"
  echo "[entrypoint] seed complete."
else
  echo "[entrypoint] $STATE_DIR already populated (or SEED_DEMO=0) — skipping seed."
fi

echo "[entrypoint] starting dashboard on ${OBSERVE_HOST:-0.0.0.0}:${OBSERVE_PORT:-5000} (offline mode)"
exec chronolog-observe
