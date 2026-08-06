"""Diagnosers: explain WHY an incident happens and HOW to fix it.

Two engines, same interface ``diagnose(incident) -> dict``:

  * ``HeuristicDiagnoser`` (default) — rule-based, deterministic, ZERO API cost.
    Pattern-matches the incident kind + normalised message against the failure
    modes this stack actually produces (peer timeouts, rate limits, 5xx,
    ``ofi+sockets`` transport errors, context overflow, retry loops, …) and
    returns a concrete root cause + remediation. Because it is deterministic it
    is fully unit-testable and safe to show a professor live.

  * ``LLMDiagnoser`` (opt-in, ``CHRONOLOG_DOCTOR_LLM=1``) — sends the incident
    plus a few evidence samples to Claude via the Agent SDK already vendored for
    the live harness, and returns its structured analysis. Falls back to the
    heuristic engine if the SDK or credentials are absent, so detection never
    depends on it.

The returned dict shape (stable, consumed by the UI):
    {root_cause, blast_radius, remediation, confidence, source}
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Tuple

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Heuristic rules
# ---------------------------------------------------------------------------
# Each rule: (predicate(kind, message) -> bool, root_cause, remediation, confidence)

def _has(*needles: str):
    def pred(kind: str, msg: str) -> bool:
        return any(n in msg for n in needles)
    return pred


_RULES: List[Tuple[Any, str, str, float]] = [
    (
        lambda k, m: k == "orphan_call",
        "A peer accepted an inter-agent call but never returned a result — the "
        "remote agent or its collector is hung, crashed, or unreachable, so the "
        "caller is blocked on a reply that will never arrive.",
        "Check the to_host's agent process and per-node collector (port 5650). "
        "Add a client-side timeout + retry to the call_remote_agent MCP tool so a "
        "wedged peer fails fast instead of stalling the caller. Confirm the node "
        "is still in the SLURM allocation and the 40GbE route to it is up.",
        0.7,
    ),
    (
        lambda k, m: k == "rate_limit" or "rate" in m and "limit" in m or "429" in m,
        "The provider is throttling requests (HTTP 429): the agents are issuing "
        "calls faster than the account's tokens-per-minute / requests-per-minute "
        "budget allows, which spikes when many nodes fan out at once.",
        "Add exponential backoff with jitter on 429s, cap per-node concurrency, "
        "and stagger agent start-up across the fleet. If sustained, request a "
        "higher rate limit or shard traffic across keys.",
        0.85,
    ),
    (
        _has("timed out", "timeout", "deadline"),
        "Calls are exceeding their deadline. At fleet scale this is usually a "
        "slow/overloaded peer or transport contention, not a logic bug — the "
        "callee can't answer within the window the caller allows.",
        "Inspect the slowest to_host in this incident (see the host list) and its "
        "queue depth. Raise the call timeout only if the work is legitimately "
        "long; otherwise shed load, parallelise the callee, or move the hot "
        "agent off a saturated node.",
        0.7,
    ),
    (
        _has("ofi", "sockets", "hg_", "mercury", "protonosupport", "connection refused", "transport"),
        "A ChronoLog/Mercury transport error. The classic cause is handing "
        "ChronoLog a hostname instead of a raw IPv4 (Mercury raises "
        "HG_PROTONOSUPPORT), or a keeper/visor that is down or on the wrong "
        "protocol/port.",
        "Verify CHRONOLOG_VISOR_IP is a raw IPv4 (not a hostname), that "
        "CHRONOLOG_VISOR_PROTOCOL matches the deployment (ofi+sockets by "
        "default), and that the keeper on the affected node is alive. Re-run the "
        "cluster healthcheck stage of run-live-agents.sh.",
        0.65,
    ),
    (
        _has("context", "token", "overflow", "too long", "maximum context"),
        "An agent's working context exceeded the model's window — its context "
        "graph grew without eviction until the request no longer fit.",
        "Enable context-graph eviction / summarisation for the offending "
        "session (see the Memory tab for which agent is growing unbounded), and "
        "lower the per-turn context budget so old nodes are dropped before the "
        "window fills.",
        0.75,
    ),
    (
        lambda k, m: k == "retry_storm",
        "An agent is re-issuing the same prompt repeatedly — it is stuck in a "
        "loop because a downstream call keeps failing or the model keeps "
        "returning an answer the agent rejects, with no progress between tries.",
        "Add a max-retry budget and a distinct fallback path after N identical "
        "attempts. Inspect the sample sequence_ids to see what the agent is "
        "waiting on; the real failure is usually the call it retries, not the "
        "prompt itself.",
        0.7,
    ),
    (
        lambda k, m: k in ("latency_outlier", "slow_edge"),
        "A subset of calls is far slower than the median for the same model/tool "
        "— a tail-latency problem, typically a hot node, GC/allocation pause, or "
        "transient contention rather than a correctness bug.",
        "Correlate the slow turns with the node they ran on (host list) and with "
        "the Fleet Health view's p95 column. If one node dominates, rebalance "
        "agents off it; if it's broad, the provider or network is the bottleneck.",
        0.55,
    ),
    (
        lambda k, m: k == "recovery",
        "Agents are backtracking / restoring checkpoints, meaning the forward "
        "path failed often enough to trigger rollback — wasted work and a sign "
        "of an upstream instability.",
        "Trace what precedes each restore (the failing turn or call just before "
        "the recovery event) and fix that root failure; frequent restores are a "
        "symptom, not the disease.",
        0.6,
    ),
    (
        lambda k, m: k == "http_error" or "http" in m or "500" in m or "502" in m or "503" in m,
        "The provider/API returned a server-side error (5xx) or another non-2xx "
        "status — the request reached the endpoint but it could not serve it.",
        "Retry idempotent calls with backoff; if 5xx persists it is provider-side "
        "— check provider status. For 4xx other than 429, inspect the request "
        "body in the sample (malformed tool schema or oversized payload).",
        0.6,
    ),
]

_FALLBACK = (
    "An error pattern was detected but does not match a known signature. The "
    "samples below carry the raw evidence.",
    "Open a sample's evidence (session/sequence or correlation_id) in the "
    "Workspace/Scenarios tab to see the full failing turn or call and trace it "
    "from there.",
    0.3,
)


class HeuristicDiagnoser:
    """Deterministic, zero-cost diagnosis. Pure function of the incident."""

    source = "heuristic"

    def diagnose(self, incident: Dict[str, Any]) -> Dict[str, Any]:
        kind = str(incident.get("kind") or "")
        # Use the title/sample message as the text to match against.
        msg = str(incident.get("title") or "").lower()
        for sample in incident.get("samples") or []:
            msg += " " + str(sample.get("message") or "").lower()

        root, fix, conf = _FALLBACK
        for pred, root_cause, remediation, confidence in _RULES:
            try:
                if pred(kind, msg):
                    root, fix, conf = root_cause, remediation, confidence
                    break
            except Exception:  # a rule predicate must never break diagnosis
                continue

        return {
            "root_cause": root,
            "blast_radius": _blast_radius(incident),
            "remediation": fix,
            "confidence": conf,
            "source": self.source,
        }


def _blast_radius(incident: Dict[str, Any]) -> str:
    hosts = incident.get("hosts") or []
    sessions = incident.get("sessions") or []
    scenarios = incident.get("scenarios") or []
    count = incident.get("count") or 0
    parts = [f"{count} occurrence(s)"]
    if hosts:
        parts.append(f"{len(hosts)} host(s)")
    if sessions:
        parts.append(f"{len(sessions)} agent(s)")
    if scenarios:
        parts.append(f"{len(scenarios)} scenario(s)")
    return ", ".join(parts)


class LLMDiagnoser:
    """Opt-in Claude-backed diagnosis. Falls back to the heuristic engine."""

    source = "llm"

    def __init__(self) -> None:
        self._fallback = HeuristicDiagnoser()

    def diagnose(self, incident: Dict[str, Any]) -> Dict[str, Any]:
        try:
            return self._diagnose_llm(incident)
        except Exception as exc:
            log.info("LLM diagnosis unavailable (%s); using heuristic", exc)
            out = self._fallback.diagnose(incident)
            out["source"] = "heuristic-fallback"
            return out

    def _diagnose_llm(self, incident: Dict[str, Any]) -> Dict[str, Any]:
        # Imported lazily so the module never hard-depends on the SDK.
        from claude_agent_sdk import query  # type: ignore

        import json as _json

        prompt = (
            "You are PrismaDoctor, diagnosing failures in a multi-agent system "
            "observed via ChronoLog. Given this incident (errors already "
            "clustered by signature), respond with ONLY a JSON object: "
            '{"root_cause": "...", "blast_radius": "...", "remediation": "...", '
            '"confidence": 0.0}.\n\nIncident:\n'
            + _json.dumps(incident, indent=2)[:6000]
        )
        text = ""
        for msg in query(prompt=prompt):  # type: ignore
            text += getattr(msg, "result", "") or getattr(msg, "text", "") or ""
        start, end = text.find("{"), text.rfind("}")
        data = _json.loads(text[start:end + 1])
        data.setdefault("blast_radius", _blast_radius(incident))
        data["source"] = self.source
        return data


def get_diagnoser():
    """Return the configured diagnoser (heuristic by default, LLM if opted in)."""
    if os.environ.get("CHRONOLOG_DOCTOR_LLM", "").strip().lower() in ("1", "true", "yes", "on"):
        return LLMDiagnoser()
    return HeuristicDiagnoser()


def diagnose(incident: Dict[str, Any]) -> Dict[str, Any]:
    """Convenience: diagnose one incident with the configured engine."""
    return get_diagnoser().diagnose(incident)
