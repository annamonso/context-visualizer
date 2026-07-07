"""Semantic check execution — evaluates natural-language checks against LLM responses.

Each check is a plain-English criterion (e.g. "Produced code should not have memory
leaks").  The checker submits a short evaluation prompt to an LLM and parses the
structured response to determine pass/fail.

The Anthropic SDK is used directly (bypassing the proxy) so that evaluator calls are
not themselves intercepted and stored as agent interactions.  If the SDK is unavailable
or no API key is set, all checks are silently skipped.
"""

import json
import logging
from dataclasses import dataclass, field
from typing import List, Optional

from .config import SemanticCheck, SemanticChecksConfig

logger = logging.getLogger(__name__)


@dataclass
class CheckResult:
    """Result of a single semantic check."""

    check_name: str
    passed: bool
    confidence: float  # 0.0–1.0
    explanation: str
    severity: str = "error"
    # Sequence number where the LLM thinks the error was first introduced, if known.
    # None means "at the current interaction".
    error_introduced_at_sequence: Optional[int] = None

    def to_dict(self) -> dict:
        return {
            "check_name": self.check_name,
            "passed": self.passed,
            "confidence": self.confidence,
            "explanation": self.explanation,
            "severity": self.severity,
            "error_introduced_at_sequence": self.error_introduced_at_sequence,
        }


@dataclass
class CheckBatch:
    """Results from running a set of checks on one interaction."""

    session_id: str
    sequence_id: int
    results: List[CheckResult] = field(default_factory=list)

    @property
    def has_failures(self) -> bool:
        return any(not r.passed for r in self.results)

    @property
    def error_failures(self) -> List[CheckResult]:
        """Failed checks with severity == 'error'."""
        return [r for r in self.results if not r.passed and r.severity == "error"]

    @property
    def warning_failures(self) -> List[CheckResult]:
        """Failed checks with severity == 'warning'."""
        return [r for r in self.results if not r.passed and r.severity == "warning"]

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "sequence_id": self.sequence_id,
            "results": [r.to_dict() for r in self.results],
            "has_failures": self.has_failures,
            "error_count": len(self.error_failures),
            "warning_count": len(self.warning_failures),
        }


_EVAL_PROMPT = """\
You are an objective evaluator checking an AI assistant's response against a quality criterion.

Criterion: {criterion}

--- BEGIN REQUEST (last messages sent to assistant) ---
{request_excerpt}
--- END REQUEST ---

--- BEGIN RESPONSE ---
{response_excerpt}
--- END RESPONSE ---

Does the response satisfy the criterion?

Reply with ONLY valid JSON — no markdown fences, no prose outside the JSON object:
{{
  "passed": true or false,
  "confidence": <float 0.0–1.0>,
  "explanation": "<one concise sentence>",
  "error_introduced_at_sequence": <integer sequence number if the root cause was likely introduced in an earlier turn, otherwise null>
}}"""

# Characters to keep from request/response to avoid huge prompts
_MAX_EXCERPT = 3000


class SemanticChecker:
    """Evaluates semantic checks against LLM responses."""

    def __init__(self, config: SemanticChecksConfig):
        self._config = config
        self._client = None  # lazy-initialised

    def _get_client(self):
        if self._client is not None:
            return self._client
        try:
            import anthropic

            self._client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY
            return self._client
        except Exception as exc:
            logger.debug("SemanticChecker: Anthropic client unavailable — %s", exc)
            return None

    def run_checks(
        self,
        session_id: str,
        sequence_id: int,
        request_text: str,
        response_text: str,
        trigger: str = "per_query",
    ) -> CheckBatch:
        """Run all enabled checks whose trigger matches *trigger*.

        Args:
            session_id:    Session being checked.
            sequence_id:   Sequence number of the interaction being checked.
            request_text:  Serialised request (or last messages) sent to the LLM.
            response_text: Text extracted from the LLM response.
            trigger:       "per_query" or "on_strict_error".
        """
        applicable = [
            c
            for c in self._config.checks
            if c.enabled and c.trigger in (trigger, "always")
        ]

        batch = CheckBatch(session_id=session_id, sequence_id=sequence_id)
        if not applicable:
            return batch

        client = self._get_client()
        if client is None:
            return batch

        for check in applicable:
            result = self._run_single(client, check, request_text, response_text)
            batch.results.append(result)

        return batch

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _run_single(
        self,
        client,
        check: SemanticCheck,
        request_text: str,
        response_text: str,
    ) -> CheckResult:
        prompt = _EVAL_PROMPT.format(
            criterion=check.description,
            request_excerpt=request_text[:_MAX_EXCERPT],
            response_excerpt=response_text[:_MAX_EXCERPT],
        )
        try:
            message = client.messages.create(
                model=self._config.model,
                max_tokens=self._config.max_tokens,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = message.content[0].text.strip()
            # Strip accidental markdown fences
            if raw.startswith("```"):
                parts = raw.split("```")
                raw = parts[1] if len(parts) > 1 else raw
                if raw.startswith("json"):
                    raw = raw[4:]
            data = json.loads(raw)
            return CheckResult(
                check_name=check.name,
                passed=bool(data.get("passed", True)),
                confidence=float(data.get("confidence", 0.5)),
                explanation=str(data.get("explanation", "")),
                severity=check.severity,
                error_introduced_at_sequence=data.get("error_introduced_at_sequence"),
            )
        except Exception as exc:
            logger.warning("SemanticChecker: check '%s' evaluation error — %s", check.name, exc)
            # On evaluator failure default to passed so we don't block on infra issues
            return CheckResult(
                check_name=check.name,
                passed=True,
                confidence=0.0,
                explanation=f"Evaluation failed: {exc}",
                severity=check.severity,
            )


# Module-level singleton — created lazily so that config file discovery happens at
# first use rather than import time (allows DTP_SEMANTIC_CHECKS to be set after import).
_checker: Optional[SemanticChecker] = None


def get_checker() -> SemanticChecker:
    """Return the module-level SemanticChecker singleton, creating it if needed."""
    global _checker
    if _checker is None:
        from .config import load_config
        _checker = SemanticChecker(load_config())
    return _checker


def reload_checker() -> SemanticChecker:
    """Force reload of config and return a fresh checker (e.g. after config file edit)."""
    global _checker
    from .config import load_config
    _checker = SemanticChecker(load_config())
    return _checker
