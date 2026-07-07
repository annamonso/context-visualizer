# ChronoLog Observability — local offline/demo dashboard image.
#
# This image runs the dashboard in CHRONOLOG_OFFLINE=1 mode: a self-contained
# Flask app serving the built React SPA off a local JSONL spool. It needs NO
# ChronoLog cluster, NO py_chronolog_client (the C++ client is only imported for
# a live backend, which offline mode never constructs), and NO API keys.
#
# For a *live* deployment you point a differently-built image (with the ChronoLog
# client on PYTHONPATH) at a real visor; that is out of scope here.

# ---- Stage 1: build the React SPA into the Python package ----
# Vite's outDir is ../backend/static/workspace (see
# frontend/vite.config.ts), so the src tree must exist relative to frontend/.
FROM node:24-slim AS frontend
WORKDIR /app
COPY frontend/package.json frontend/package-lock.json ./frontend/
RUN cd frontend && npm ci
COPY frontend/ ./frontend/
COPY backend/ ./backend/
RUN cd frontend && npm run build

# ---- Stage 2: Python runtime ----
FROM python:3.12-slim AS runtime
WORKDIR /app

# Install the package (with the freshly built SPA baked into package-data).
COPY pyproject.toml README.md ./
COPY backend/ ./backend/
COPY --from=frontend \
     /app/backend/static/workspace \
     ./backend/static/workspace
RUN pip install --no-cache-dir .

# Demo seeders (offline, no cluster) + entrypoint.
COPY scripts/ ./scripts/
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

ENV CHRONOLOG_OFFLINE=1 \
    DTP_STATE_DIR=/data \
    DEMO_STATE_DIR=/data \
    OBSERVE_HOST=0.0.0.0 \
    OBSERVE_PORT=5000 \
    SEED_DEMO=1

EXPOSE 5000
VOLUME ["/data"]
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
