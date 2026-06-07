"""Cost-effective rollback point analysis.

When an error is detected at sequence N it may have been *introduced* at an earlier
step (e.g. the agent made a wrong assumption at sequence 3 whose consequences only
surfaced at sequence 7).  Simply restoring the most recent high-quality checkpoint
before N might replay the same mistake.

This module:
  1. Asks an LLM to attribute the error's origin within the session history.
  2. Estimates, for every checkpoint before N, the probability that restoring to it
     would actually fix the error.
  3. Combines that probability with the checkpoint's quality score and the number of
     tokens that would need to be replayed to produce a cost-effectiveness score.
  4. Returns a ranked list of candidates so the caller can pick the best restore point.

Cost-effectiveness formula
--------------------------
    CE = (fix_probability * quality_score) / (1 + log2(1 + replay_tokens / 10_000))

Higher CE is better.  The log term dampens the penalty for long replays — replaying
10× more tokens is not 10× more costly because the work may simply need to be redone.
"""

import json
import logging
import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .config import SemanticChecksConfig
from ..checkpointing.checkpoint_manager import Checkpoint

logger = logging.getLogger(__name__)

# Tokens per turn assumed when we have no better estimate
_DEFAULT_TOKENS_PER_TURN = 2_000

# Maximum history turns to include in the attribution prompt
_MAX_HISTORY_TURNS = 10

# Characters per turn summary in the prompt
_MAX_TURN_CHARS = 400


@dataclass
class CheckpointCandidate:
    """A checkpoint considered as a rollback target, with its cost-effectiveness score."""

    checkpoint: Checkpoint
    fix_probability: float       # 0–1: estimated probability restoring here fixes the error
    replay_token_estimate: int   # tokens expected to replay from this checkpoint to error_sequence
    cost_effectiveness: float    # combined score — higher is better
    rationale: str               # human-readable explanation

    def to_dict(self) -> dict:
        return {
            "checkpoint": self.checkpoint.to_dict(),
            "fix_probability": self.fix_probability,
            "replay_token_estimate": self.replay_token_estimate,
            "cost_effectiveness": self.cost_effectiveness,
            "rationale": self.rationale,
        }


@dataclass
class RollbackAnalysis:
    """Full analysis result returned to the caller."""

    session_id: str
    error_sequence: int
    error_description: str
    candidates: List[CheckpointCandidate] = field(default_factory=list)
    analysis_summary: str = ""
    # checkpoint_id of the top-ranked candidate, or None if no candidates
    recommended_checkpoint_id: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "error_sequence": self.error_sequence,
            "error_description": self.error_description,
            "analysis_summary": self.analysis_summary,
            "recommended_checkpoint_id": self.recommended_checkpoint_id,
            "candidates": [c.to_dict() for c in self.candidates],
        }


_ATTRIBUTION_PROMPT = """\
You are a debugging assistant analysing a multi-turn conversation to identify where an error was introduced.

Error detected at sequence {error_seq}: {error_desc}

Conversation history (chronological, most recent last):
{history}

For each sequence listed above, estimate the probability (0.0–1.0) that restoring the \
conversation to just BEFORE that sequence would fix the error.  A sequence is a good \
restore point if the error was introduced AT or AFTER it, and restoring before it would \
give the agent a chance to take a different path.

Reply with ONLY valid JSON — no markdown fences:
{{
  "primary_origin_sequence": <integer — most likely sequence where error was first introduced>,
  "primary_confidence": <float 0.0–1.0>,
  "fix_probability_by_sequence": {{
    "<sequence>": <float 0.0–1.0>
  }},
  "rationale_by_sequence": {{
    "<sequence>": "<one sentence explaining why restoring here would or would not help>"
  }},
  "analysis": "<one paragraph summary of the error pattern and recommended approach>"
}}"""


class RollbackAnalyzer:
    """Analyses a session history to recommend the most cost-effective rollback point."""

    def __init__(self, config: SemanticChecksConfig):
        self._config = config
        self._client = None

    def _get_client(self):
        if self._client is not None:
            return self._client
        try:
            import anthropic
            self._client = anthropic.Anthropic()
            return self._client
        except Exception as exc:
            logger.debug("RollbackAnalyzer: Anthropic client unavailable — %s", exc)
            return None

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def analyze(
        self,
        session_id: str,
        error_sequence: int,
        error_description: str,
        checkpoints: List[Checkpoint],
        history: List[dict],
        avg_tokens_per_turn: int = _DEFAULT_TOKENS_PER_TURN,
    ) -> RollbackAnalysis:
        """Return a ranked list of checkpoint candidates for rolling back to.

        Args:
            session_id:          The session being analysed.
            error_sequence:      Sequence number at which the error was detected.
            error_description:   Human-readable description of the error.
            checkpoints:         All checkpoints recorded for the session.
            history:             List of raw interaction dicts (from CTE).
            avg_tokens_per_turn: Estimated tokens per conversation turn (used to
                                 compute replay cost when exact counts are missing).
        """
        analysis = RollbackAnalysis(
            session_id=session_id,
            error_sequence=error_sequence,
            error_description=error_description,
        )

        candidates_before_error = [
            cp for cp in checkpoints if cp.sequence_id < error_sequence
        ]
        if not candidates_before_error:
            return analysis

        # Try LLM-based attribution if we have history
        attribution: Dict = {}
        client = self._get_client()
        if client is not None and history:
            attribution = self._attribute_origin(client, error_sequence, error_description, history)

        # Score each candidate
        for cp in candidates_before_error:
            fix_prob = self._fix_probability(cp, attribution, error_sequence)
            turns_to_replay = error_sequence - cp.sequence_id

            # Use token_count from checkpoint if > 0, else estimate
            if cp.token_count > 0:
                replay_tokens = cp.token_count + turns_to_replay * avg_tokens_per_turn
            else:
                replay_tokens = turns_to_replay * avg_tokens_per_turn

            cost_eff = _cost_effectiveness(fix_prob, cp.score, replay_tokens)
            rationale = attribution.get("rationale_by_sequence", {}).get(str(cp.sequence_id), "")

            analysis.candidates.append(
                CheckpointCandidate(
                    checkpoint=cp,
                    fix_probability=round(fix_prob, 3),
                    replay_token_estimate=replay_tokens,
                    cost_effectiveness=round(cost_eff, 3),
                    rationale=rationale,
                )
            )

        # Sort descending by cost-effectiveness
        analysis.candidates.sort(key=lambda c: c.cost_effectiveness, reverse=True)

        if analysis.candidates:
            analysis.recommended_checkpoint_id = (
                analysis.candidates[0].checkpoint.checkpoint_id
            )

        analysis.analysis_summary = attribution.get("analysis", "")
        return analysis

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _attribute_origin(
        self,
        client,
        error_sequence: int,
        error_description: str,
        history: List[dict],
    ) -> Dict:
        """Call the LLM to identify where in the history the error was introduced."""
        # Build a compact history summary (last N turns)
        recent = history[-_MAX_HISTORY_TURNS:]
        history_lines = []
        for interaction in recent:
            seq = interaction.get("sequence_id", "?")
            user_text = _extract_user_text(interaction)
            asst_text = _extract_assistant_text(interaction)
            history_lines.append(
                f"[seq {seq}] user: {user_text[:_MAX_TURN_CHARS]}"
            )
            history_lines.append(
                f"[seq {seq}] assistant: {asst_text[:_MAX_TURN_CHARS]}"
            )

        history_block = "\n".join(history_lines)
        prompt = _ATTRIBUTION_PROMPT.format(
            error_seq=error_sequence,
            error_desc=error_description,
            history=history_block,
        )

        try:
            message = client.messages.create(
                model=self._config.model,
                max_tokens=1024,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = message.content[0].text.strip()
            if raw.startswith("```"):
                parts = raw.split("```")
                raw = parts[1] if len(parts) > 1 else raw
                if raw.startswith("json"):
                    raw = raw[4:]
            return json.loads(raw)
        except Exception as exc:
            logger.warning("RollbackAnalyzer: attribution failed — %s", exc)
            return {}

    def _fix_probability(
        self,
        cp: Checkpoint,
        attribution: Dict,
        error_sequence: int,
    ) -> float:
        """Estimate the probability that rolling back to *cp* would fix the error."""
        # If attribution is available, prefer its per-sequence probability
        fp_map: Dict = attribution.get("fix_probability_by_sequence", {})
        if fp_map:
            # Direct hit
            key = str(cp.sequence_id)
            if key in fp_map:
                return float(fp_map[key])

            # Otherwise interpolate: probability decreases with distance from primary origin
            primary_seq = attribution.get("primary_origin_sequence")
            primary_conf = float(attribution.get("primary_confidence", 0.5))
            if primary_seq is not None:
                if cp.sequence_id <= int(primary_seq):
                    dist = int(primary_seq) - cp.sequence_id
                    return max(0.05, primary_conf * (1.0 - dist * 0.12))
                # Checkpoint is AFTER primary origin — unlikely to help
                return 0.05

        # No attribution data: use a simple recency heuristic.
        # Checkpoints closer to the error are more likely to be the right restore point,
        # with a floor so that very early checkpoints still have a small probability.
        distance = max(1, error_sequence - cp.sequence_id)
        return max(0.05, 1.0 - (distance - 1) * 0.12)


# ------------------------------------------------------------------
# Module helpers
# ------------------------------------------------------------------

def _cost_effectiveness(fix_prob: float, quality: float, replay_tokens: int) -> float:
    """Combine fix probability, checkpoint quality, and replay cost into one score.

    Higher is better.  The log term dampens (but does not linearise) the penalty for
    large replay costs so that a checkpoint with a much higher fix probability can
    still win even if it requires more replay.
    """
    penalty = 1.0 + math.log2(1.0 + replay_tokens / 10_000)
    return (fix_prob * quality) / penalty


def _extract_user_text(interaction: dict) -> str:
    req = interaction.get("request", {})
    if isinstance(req, str):
        try:
            req = json.loads(req)
        except Exception:
            return ""
    if not isinstance(req, dict):
        return ""
    body = req.get("body", {})
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except Exception:
            return ""
    if not isinstance(body, dict):
        return ""
    messages = body.get("messages", [])
    if not messages:
        return ""
    last = messages[-1]
    if not isinstance(last, dict):
        return ""
    content = last.get("content", "")
    if isinstance(content, list):
        return " ".join(
            b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"
        )
    return str(content)


def _extract_assistant_text(interaction: dict) -> str:
    resp = interaction.get("response", {})
    if isinstance(resp, dict):
        text = resp.get("text", "")
        if text:
            return str(text)
        body = resp.get("body", "")
        if isinstance(body, str):
            return body[:_MAX_TURN_CHARS]
    return ""


# Module-level singleton
_analyzer: Optional[RollbackAnalyzer] = None


def get_analyzer() -> RollbackAnalyzer:
    """Return the module-level RollbackAnalyzer singleton."""
    global _analyzer
    if _analyzer is None:
        from .config import load_config
        _analyzer = RollbackAnalyzer(load_config())
    return _analyzer


def reload_analyzer() -> RollbackAnalyzer:
    """Force reload of config and return a fresh analyzer."""
    global _analyzer
    from .config import load_config
    _analyzer = RollbackAnalyzer(load_config())
    return _analyzer
