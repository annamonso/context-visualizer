"""Self-compacting incident memory — a markdown document that learns across runs.

Mirrors the project's own ``MEMORY.md`` discipline: one short line per incident,
and when the file grows past a threshold the oldest entries are folded into a
condensed digest so the document stays bounded while preserving the long tail as
counts. This is what lets ChronoDoctor say "this is the 14th time comp-12 has
timed out" instead of treating every run as brand new.

Format::

    # ChronoLog Incident Memory

    ## Digest (compacted)
    - [edge_error] peer timed out — seen 142x, last 2026-06-22 (hosts: ...)

    ## Recent
    - 2026-06-22T10:30Z [critical] orphan_call ×7 — hung/dropped peer (comp-12)

Compaction is pluggable: by default it is a deterministic heuristic (group the
old "Recent" lines by signature and sum counts). An LLM summariser can be passed
in for prose digests, but the heuristic keeps the feature zero-cost and testable.
"""

from __future__ import annotations

import logging
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

log = logging.getLogger(__name__)

_HEADER = "# ChronoLog Incident Memory\n"
_DIGEST_H = "## Digest (compacted)"
_RECENT_H = "## Recent"

# Compact when the Recent section exceeds this many lines; keep this many newest.
DEFAULT_COMPACT_AT = 200
DEFAULT_KEEP_RECENT = 60


def _state_dir() -> Path:
    base = os.environ.get("DTP_STATE_DIR", "") or str(Path.home() / ".dt_provenance")
    return Path(base) / "diagnostics"


def _now() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%MZ")


class IncidentMemory:
    """Append-and-compact markdown log of incidents."""

    def __init__(
        self,
        path: Optional[os.PathLike] = None,
        *,
        compact_at: int = DEFAULT_COMPACT_AT,
        keep_recent: int = DEFAULT_KEEP_RECENT,
        summariser: Optional[Callable[[List[str]], List[str]]] = None,
    ) -> None:
        self.path = Path(path) if path else _state_dir() / "incident_memory.md"
        self.compact_at = compact_at
        self.keep_recent = keep_recent
        self._summarise = summariser or _heuristic_digest
        self._lock = threading.Lock()

    # ----- public API -----

    def record(self, incidents: List[Dict[str, Any]]) -> int:
        """Append one line per incident; compact if needed. Returns lines added."""
        if not incidents:
            return 0
        lines = [self._format(inc) for inc in incidents]
        with self._lock:
            digest, recent = self._read()
            recent.extend(lines)
            if len(recent) > self.compact_at:
                old = recent[: -self.keep_recent]
                recent = recent[-self.keep_recent :]
                digest = self._merge_digest(digest, old)
            self._write(digest, recent)
        return len(lines)

    def render(self) -> str:
        """Return the full memory document as text (for the API / UI)."""
        with self._lock:
            if self.path.is_file():
                return self.path.read_text(encoding="utf-8")
        return _HEADER

    def known_signatures(self) -> Dict[str, int]:
        """Map signature/title -> historical count, from the digest section.

        Lets a scan annotate "recurring" incidents — recognising a repeat
        offender across runs is the whole point of the memory.
        """
        digest, recent = self._read()
        out: Dict[str, int] = {}
        # Compacted digest entries: "- [kind] title — seen Nx".
        for line in digest:
            m = re.search(r"\bseen (\d+)x", line)
            key = re.sub(r" — seen \d+x.*$", "", line).lstrip("- ").strip()
            if m and key:
                out[key] = out.get(key, 0) + int(m.group(1))
        # Uncompacted Recent entries: "- ts [sev] kind ×N — title (hosts)".
        # Reading these too means a repeat is recognised on the very next scan,
        # not only after the digest compacts.
        for line in recent:
            m = re.search(r"\]\s+(\S+)\s+×(\d+)\s+—\s+(.+?)\s*\(", line)
            if not m:
                continue
            kind, cnt, title = m.group(1), int(m.group(2)), m.group(3)
            key = f"[{kind}] {title}"
            out[key] = out.get(key, 0) + cnt
        return out

    # ----- internals -----

    def _format(self, inc: Dict[str, Any]) -> str:
        hosts = inc.get("hosts") or []
        host_s = ",".join(hosts[:3]) + ("…" if len(hosts) > 3 else "")
        return (
            f"- {_now()} [{inc.get('severity','?')}] {inc.get('kind','?')} "
            f"×{inc.get('count',0)} — {inc.get('title','')} ({host_s})"
        )

    def _read(self) -> tuple[List[str], List[str]]:
        if not self.path.is_file():
            return [], []
        text = self.path.read_text(encoding="utf-8")
        digest = _section(text, _DIGEST_H)
        recent = _section(text, _RECENT_H)
        return digest, recent

    def _write(self, digest: List[str], recent: List[str]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        parts = [_HEADER, ""]
        if digest:
            parts.append(_DIGEST_H)
            parts.extend(digest)
            parts.append("")
        parts.append(_RECENT_H)
        parts.extend(recent)
        parts.append("")
        self.path.write_text("\n".join(parts), encoding="utf-8")

    def _merge_digest(self, digest: List[str], old_lines: List[str]) -> List[str]:
        new_digest_lines = self._summarise(old_lines)
        merged: Dict[str, int] = {}
        order: List[str] = []
        for line in digest + new_digest_lines:
            key = re.sub(r" — seen \d+x.*$", "", line).lstrip("- ").strip()
            m = re.search(r"\bseen (\d+)x", line)
            cnt = int(m.group(1)) if m else 1
            if key not in merged:
                order.append(key)
                merged[key] = 0
            merged[key] += cnt
        return [f"- {key} — seen {merged[key]}x" for key in order]


def _section(text: str, header: str) -> List[str]:
    lines = text.splitlines()
    out: List[str] = []
    capture = False
    for ln in lines:
        if ln.strip() == header:
            capture = True
            continue
        if capture and ln.startswith("## "):
            break
        if capture and ln.strip().startswith("- "):
            out.append(ln.rstrip())
    return out


def _heuristic_digest(old_lines: List[str]) -> List[str]:
    """Group old Recent lines by (kind, title) and sum counts. Deterministic."""
    agg: Dict[str, int] = {}
    order: List[str] = []
    for ln in old_lines:
        # "- <ts> [sev] kind ×N — title (hosts)"
        m = re.search(r"\]\s+(\S+)\s+×(\d+)\s+—\s+(.+?)\s*\(", ln)
        if not m:
            continue
        kind, cnt, title = m.group(1), int(m.group(2)), m.group(3)
        key = f"[{kind}] {title}"
        if key not in agg:
            order.append(key)
            agg[key] = 0
        agg[key] += cnt
    return [f"- {key} — seen {agg[key]}x" for key in order]
