"""JSONL-backed store for synthetic LLM interactions + context-graph nodes.

This is a *demo-only* fallback for the live read path so the Workspace
tab populates without `dt_demo_server` running. Production reads from
the legacy runtime (in-memory CTE blobs) or from ChronoLog after cut-over.

Layout under ``~/.dt_provenance/demo_llm/``:

    sessions.jsonl                          one line per known session
    interactions/<session_id>.jsonl         per-session LLM interactions
    context_graphs/<session_id>.jsonl       per-session diff nodes
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import List, Optional


def _root() -> Path:
    base = os.environ.get("DTP_STATE_DIR") or os.path.expanduser("~/.dt_provenance")
    return Path(base) / "demo_llm"


class LLMDemoStore:
    """Tiny JSONL store, thread-safe, idempotent per (session, sequence_id)."""

    def __init__(self, root: Optional[os.PathLike] = None):
        self._root = Path(root) if root is not None else _root()
        (self._root / "interactions").mkdir(parents=True, exist_ok=True)
        (self._root / "context_graphs").mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    # ---- sessions -----------------------------------------------------

    def list_sessions(self) -> List[str]:
        out: List[str] = []
        for p in (self._root / "interactions").iterdir():
            if p.suffix == ".jsonl":
                out.append(p.stem)
        return sorted(out)

    def add_session(self, session_id: str, attrs: Optional[dict] = None) -> None:
        ent = {"session_id": session_id, **(attrs or {})}
        path = self._root / "sessions.jsonl"
        line = json.dumps(ent, separators=(",", ":")) + "\n"
        with self._lock:
            with open(path, "a", encoding="utf-8") as f:
                f.write(line)

    # ---- interactions -------------------------------------------------

    def append_interaction(self, session_id: str, interaction: dict) -> None:
        path = self._root / "interactions" / f"{session_id}.jsonl"
        line = json.dumps(interaction, separators=(",", ":")) + "\n"
        with self._lock:
            with open(path, "a", encoding="utf-8") as f:
                f.write(line)

    def read_interactions(self, session_id: str) -> List[dict]:
        path = self._root / "interactions" / f"{session_id}.jsonl"
        if not path.is_file():
            return []
        out: List[dict] = []
        with self._lock:
            with open(path, "r", encoding="utf-8") as f:
                for raw in f:
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        out.append(json.loads(raw))
                    except Exception:
                        continue
        out.sort(key=lambda d: int(d.get("sequence_id", 0)))
        return out

    # ---- context graphs ----------------------------------------------

    def append_context_node(self, session_id: str, node: dict) -> None:
        path = self._root / "context_graphs" / f"{session_id}.jsonl"
        line = json.dumps(node, separators=(",", ":")) + "\n"
        with self._lock:
            with open(path, "a", encoding="utf-8") as f:
                f.write(line)

    def read_context_graph(self, session_id: str) -> List[dict]:
        path = self._root / "context_graphs" / f"{session_id}.jsonl"
        if not path.is_file():
            return []
        out: List[dict] = []
        with self._lock:
            with open(path, "r", encoding="utf-8") as f:
                for raw in f:
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        out.append(json.loads(raw))
                    except Exception:
                        continue
        out.sort(key=lambda d: int(d.get("sequence_id", 0)))
        return out


_DEFAULT: Optional[LLMDemoStore] = None
_DEFAULT_LOCK = threading.Lock()


def get_store() -> LLMDemoStore:
    global _DEFAULT
    if _DEFAULT is None:
        with _DEFAULT_LOCK:
            if _DEFAULT is None:
                _DEFAULT = LLMDemoStore()
    return _DEFAULT


def reset_store() -> None:
    global _DEFAULT
    with _DEFAULT_LOCK:
        _DEFAULT = None
