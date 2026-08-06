"""Shared helpers for the three feature demos.

All demos run fully OFFLINE (``CHRONOLOG_OFFLINE=1``) against a dedicated
``DTP_STATE_DIR`` so they need no ChronoLog cluster, no API keys, and never
touch the real ``~/.dt_provenance`` data. They seed the same spool +
inter-agent store the dashboard reads, then call the backend analysis directly
to print a narrated walk-through — and tell you how to open the same data live
in the dashboard.

The point of these demos: show, with zero infrastructure, exactly what the new
PrismaDoctor / Fleet / Tracing features do — then prove it scales by pointing
the dashboard at the same state dir on ARES.
"""

from __future__ import annotations

import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path


def bootstrap(state_dir: str) -> None:
    """Configure offline mode + state dir, then put ``src`` on the path.

    Must be called BEFORE importing ``chronolog_observability`` so the backend
    selects the offline (JSONL/spool) path.
    """
    os.environ["CHRONOLOG_OFFLINE"] = "1"
    os.environ["DTP_STATE_DIR"] = state_dir
    Path(state_dir).mkdir(parents=True, exist_ok=True)
    repo_src = Path(__file__).resolve().parents[2] / "src"
    if str(repo_src) not in sys.path:
        sys.path.insert(0, str(repo_src))


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def now() -> datetime:
    return datetime.now(timezone.utc)


# --- seeding primitives (import chronolog_observability lazily, post-bootstrap) ---

def edge(
    scenario: str,
    from_host: str,
    from_session: str,
    to_host: str,
    to_session: str,
    *,
    tool: str = "call_remote_agent",
    status: str = "ok",
    latency_ms: float = 0.0,
    when: datetime | None = None,
    parent_corr: str = "",
    correlation_id: str | None = None,
    orphan: bool = False,
) -> str:
    """Seed a start (+done unless ``orphan``) inter-agent edge. Returns its corr id."""
    from chronolog_observability.capture.inter_agent import get_store
    from chronolog_observability.capture.inter_agent.store import new_message

    store = get_store()
    corr = correlation_id or uuid.uuid4().hex
    when = when or now()
    common = dict(
        scenario_id=scenario, from_host=from_host, from_session=from_session,
        to_host=to_host, to_session=to_session, tool_name=tool,
        correlation_id=corr, parent_correlation_id=parent_corr,
    )
    s = new_message(phase="start", **common)
    s.ts = iso(when)
    store.append(s)
    if not orphan:
        d = new_message(phase="done", status=status, latency_ms=float(latency_ms), **common)
        d.ts = iso(when + timedelta(milliseconds=latency_ms))
        store.append(d)
    return corr


def turn(
    session: str,
    seq: int,
    *,
    host: str,
    scenario: str = "",
    model: str = "claude-opus-4-8",
    status_code: int = 200,
    text: str = "ok",
    latency_ms: float = 300.0,
    in_tok: int = 500,
    out_tok: int = 50,
    cost: float = 0.001,
    prompt: str = "do the work",
    when: datetime | None = None,
) -> None:
    """Seed one LLM interaction turn into the spool (Path-A)."""
    from chronolog_observability.capture.spool import get_spool

    sp = get_spool()
    when = when or now()
    sp.record_interaction(session, {
        "session_id": session, "sequence_id": seq, "scenario_id": scenario,
        "host": host, "timestamp": iso(when), "provider": "anthropic", "model": model,
        "request": {"method": "POST", "path": "/v1/messages",
                    "body": {"messages": [{"role": "user", "content": prompt}]}},
        "response": {"status_code": status_code, "text": text, "is_streaming": False},
        "metrics": {"total_latency_ms": latency_ms, "delta_input_tokens": in_tok,
                    "delta_output_tokens": out_tok, "delta_cost_usd": cost},
    }, sequence_id=seq)
    # Also record a context-graph node so the Memory tab (agent working memory)
    # shows tokens growing/evicting — it reads context nodes, not interactions.
    sp.record_context_node(session, {
        "session_id": session, "sequence_id": seq, "timestamp": iso(when),
        "op": "add", "node": f"ctx-{seq}", "summary": prompt[:60],
        "tokens": in_tok + out_tok,
        "delta_input_tokens": in_tok, "delta_output_tokens": out_tok,
        "delta_cost_usd": cost,
    }, sequence_id=seq)


def recovery(session: str, *, reason: str = "checkpoint-restore", host: str = "", when: datetime | None = None) -> None:
    from chronolog_observability.capture.spool import get_spool

    sp = get_spool()
    sp.record_recovery(session, {
        "session_id": session, "reason": reason, "host": host,
        "blob_name": uuid.uuid4().hex, "timestamp": iso(when or now()),
    })


# --- pretty printing ---

C = {
    "reset": "\033[0m", "bold": "\033[1m", "dim": "\033[2m",
    "red": "\033[31m", "green": "\033[32m", "yellow": "\033[33m",
    "blue": "\033[34m", "cyan": "\033[36m", "mag": "\033[35m",
}


def color(s: str, c: str) -> str:
    if not sys.stdout.isatty():
        return s
    return f"{C.get(c,'')}{s}{C['reset']}"


def h1(s: str) -> None:
    print("\n" + color("━" * 72, "dim"))
    print(color(s, "bold"))
    print(color("━" * 72, "dim"))


def h2(s: str) -> None:
    print("\n" + color("▶ " + s, "cyan"))


def view_hint(state_dir: str, tab: str, extra: str = "") -> None:
    h2("See it live in the dashboard")
    print("  Launch the dashboard against this demo's data:\n")
    print(color(
        f"    CHRONOLOG_OFFLINE=1 DTP_STATE_DIR={state_dir} \\\n"
        f"      PYTHONPATH=src python3 -m chronolog_observability\n", "green"))
    print(f"  Then open:  http://<host>:5000/?tab={tab}{extra}")
    print(color("  (On ARES, point the dashboard at the real visor instead of "
                "offline mode to view the same\n   features over a live cluster — "
                "the analysis code path is identical.)", "dim"))
