"""Flask blueprint: semantic checks and intelligent rollback analysis API.

Endpoints
---------
GET  /api/semantic-checks/config
    Return the active semantic checks configuration (path, model, checks list).
    Useful for verifying what checks are loaded without restarting the server.

POST /api/semantic-checks/reload
    Reload the configuration from disk (useful after editing semantic_checks.yaml).

POST /api/session/<sid>/semantic-checks/run
    Body: {"sequence_id": <int>, "trigger": "per_query"|"on_strict_error"}
    Fetch the specified interaction from CTE and run semantic checks against it.
    Returns results for each check and a summary has_failures flag.

POST /api/session/<sid>/rollback/analyze
    Body: {
        "error_sequence": <int>,        -- sequence where error was detected
        "error_description": "<text>",  -- human-readable error description
        "avg_tokens_per_turn": <int>    -- optional, default 2000
    }
    Uses LLM-based error attribution to rank all checkpoints for the session by
    cost-effectiveness.  Returns a ranked candidate list and a recommended
    checkpoint_id to restore to.
"""

import json
import logging

from flask import Blueprint, Response, request

from ..adapters.base import Capability
from ..adapters.registry import registry

bp = Blueprint("semantic", __name__)
logger = logging.getLogger(__name__)


def _source():
    """Active data source for interaction reads (ChronoLog on a bare host)."""
    return registry.source_for(Capability.SEMANTIC)


# ---------------------------------------------------------------------------
# Config endpoints
# ---------------------------------------------------------------------------

@bp.route("/semantic-checks/config", methods=["GET"])
def get_config():
    """Return the currently loaded semantic checks configuration."""
    from ..semantic.config import load_config, find_config_path

    config = load_config()
    config_path = find_config_path()

    return Response(
        json.dumps({
            "config_path": str(config_path) if config_path else None,
            "model": config.model,
            "max_tokens": config.max_tokens,
            "checks": [
                {
                    "name": c.name,
                    "description": c.description,
                    "trigger": c.trigger,
                    "severity": c.severity,
                    "enabled": c.enabled,
                }
                for c in config.checks
            ],
        }),
        content_type="application/json",
    )


@bp.route("/semantic-checks/reload", methods=["POST"])
def reload_config():
    """Reload semantic checks config from disk and return updated configuration."""
    from ..semantic.checker import reload_checker
    from ..semantic.rollback_analyzer import reload_analyzer
    from ..semantic.config import find_config_path

    checker = reload_checker()
    reload_analyzer()

    config_path = find_config_path()
    return Response(
        json.dumps({
            "reloaded": True,
            "config_path": str(config_path) if config_path else None,
            "check_count": len(checker._config.checks),
        }),
        content_type="application/json",
    )


# ---------------------------------------------------------------------------
# Per-session check execution
# ---------------------------------------------------------------------------

@bp.route("/session/<sid>/semantic-checks/run", methods=["POST"])
def run_checks(sid):
    """Run semantic checks against a stored interaction.

    Body parameters
    ---------------
    sequence_id : int  (required)
        Sequence number of the interaction to evaluate.
    trigger : str  (optional, default "per_query")
        Only checks whose trigger matches this value are executed.
    """
    data = request.get_json(silent=True) or {}
    sequence_id = data.get("sequence_id")
    trigger = data.get("trigger", "per_query")

    if sequence_id is None:
        return Response(
            json.dumps({"error": "sequence_id required"}),
            status=400,
            content_type="application/json",
        )

    request_text, response_text = _fetch_interaction_texts(sid, sequence_id)

    from ..semantic.checker import get_checker
    batch = get_checker().run_checks(sid, sequence_id, request_text, response_text, trigger)

    return Response(
        json.dumps(batch.to_dict()),
        content_type="application/json",
    )


# ---------------------------------------------------------------------------
# Rollback analysis
# ---------------------------------------------------------------------------

@bp.route("/session/<sid>/rollback/analyze", methods=["POST"])
def analyze_rollback(sid):
    """Rank checkpoints by cost-effectiveness for fixing a detected error.

    Body parameters
    ---------------
    error_sequence : int  (required, also accepted as trigger_sequence_id)
        Sequence number at which the error was detected.
    error_description : str  (optional)
        Human-readable description of the error to analyse.
    avg_tokens_per_turn : int  (optional, default 2000)
        Estimate of tokens per conversation turn used for replay cost calculation.
    """
    data = request.get_json(silent=True) or {}
    error_sequence = data.get("error_sequence") or data.get("trigger_sequence_id")
    error_description = data.get("error_description", "")
    avg_tokens_per_turn = int(data.get("avg_tokens_per_turn", 2000))

    if error_sequence is None:
        return Response(
            json.dumps({"error": "error_sequence required"}),
            status=400,
            content_type="application/json",
        )

    error_sequence = int(error_sequence)

    # Load checkpoints for this session
    from ..checkpointing.checkpoint_manager import manager
    checkpoints = manager.get_checkpoints(sid)

    # Fetch session history (up to _MAX_FETCH turns before error)
    history = _fetch_history(sid, error_sequence)

    from ..semantic.rollback_analyzer import get_analyzer
    analysis = get_analyzer().analyze(
        session_id=sid,
        error_sequence=error_sequence,
        error_description=error_description,
        checkpoints=checkpoints,
        history=history,
        avg_tokens_per_turn=avg_tokens_per_turn,
    )

    return Response(
        json.dumps(analysis.to_dict()),
        content_type="application/json",
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _fetch_interaction_texts(session_id: str, sequence_id: int):
    """Return (request_text, response_text) for a stored interaction."""

    request_text = ""
    response_text = ""

    try:
        result = _source().get_interaction(session_id, sequence_id)
    except Exception as exc:
        logger.warning("semantic.api: get_interaction(%s, %d) failed — %s", session_id, sequence_id, exc)
        return request_text, response_text

    for _, interaction in result.items():
        if isinstance(interaction, str):
            try:
                interaction = json.loads(interaction)
            except Exception:
                continue
        if not isinstance(interaction, dict):
            continue

        # Extract last few messages as request context
        req = interaction.get("request", {})
        if isinstance(req, str):
            try:
                req = json.loads(req)
            except Exception:
                pass
        if isinstance(req, dict):
            body = req.get("body", {})
            if isinstance(body, str):
                try:
                    body = json.loads(body)
                except Exception:
                    pass
            if isinstance(body, dict):
                messages = body.get("messages", [])
                # Keep last 3 messages for context
                request_text = json.dumps(messages[-3:], indent=2) if messages else ""

        # Extract response text
        resp = interaction.get("response", {})
        if isinstance(resp, dict):
            response_text = resp.get("text", "") or str(resp.get("body", ""))

        break  # first container is enough

    return request_text, response_text


_MAX_HISTORY_FETCH = 10


def _fetch_history(session_id: str, up_to_sequence: int) -> list:
    """Fetch up to _MAX_HISTORY_FETCH interactions ending at up_to_sequence."""

    start = max(1, up_to_sequence - _MAX_HISTORY_FETCH + 1)
    history = []

    for seq in range(start, up_to_sequence + 1):
        try:
            result = _source().get_interaction(session_id, seq)
            for _, interaction in result.items():
                if isinstance(interaction, str):
                    try:
                        interaction = json.loads(interaction)
                    except Exception:
                        continue
                if isinstance(interaction, dict):
                    history.append(interaction)
                break
        except Exception:
            continue

    return history
