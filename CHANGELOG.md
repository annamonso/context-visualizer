# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] — 2026-08-06

First public release.

### Added

- **Dashboard** — a Flask + React SPA reconstructing conversation, scenario,
  cluster, fleet and trace views from ChronoLog stories. Ships an offline demo
  mode (`CHRONOLOG_OFFLINE=1`) that needs no cluster.
- **Two capture paths** — Path-A (LLM turns) via a local spool drained into
  ChronoLog by a sync worker, and Path-B (inter-agent calls) via a per-node
  collector writing to that node's keeper.
- **Hot store for live views** — ChronoLog's ~180 s keeper→grapher drain makes it
  unreadable in real time, so live views read a local hot store written at ingest
  while ChronoLog stays the durable cold path.
- **ChronoDoctor** — error detection, incident clustering by signature,
  heuristic root-cause diagnosis with remediation and blast radius, and a
  self-compacting incident memory that survives restarts.
- **Fleet Health** — per-node health heatmap backed by `(host, minute)` rollups,
  so the overview cost is independent of event volume and scales past 100 nodes.
- **Distributed tracing** — cross-host call trees with critical-path computation
  and bottleneck attribution by exclusive self-time.
- **Adapter seam** — data sources are discovered through the
  `chronolog_observability.adapters` entry-point group; blueprints declare the
  capability they need and the app factory mounts only what is servable.
- **Evaluation harness** — the E1–E6 measurement scripts used for the thesis
  evaluation, plus a runbook and the reported results.

[Unreleased]: https://github.com/annamonso/context-visualizer/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/annamonso/context-visualizer/releases/tag/v0.1.0
