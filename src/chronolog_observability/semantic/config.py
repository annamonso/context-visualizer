"""Configuration loader for semantic checks.

Config file search order:
  1. DTP_SEMANTIC_CHECKS env var (explicit path)
  2. ./semantic_checks.yaml  (current working directory)
  3. ~/.dt_provenance/semantic_checks.yaml  (user default)

Example semantic_checks.yaml:

    version: 1
    model: claude-haiku-4-5-20251001
    max_tokens: 512
    checks:
      - name: no_memory_leaks
        description: "Produced code should not have memory leaks"
        trigger: per_query
        severity: error
        enabled: true
      - name: consistent_variable_names
        description: "Variable names should be consistent with those defined earlier in the session"
        trigger: per_query
        severity: warning
        enabled: true
"""

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional


@dataclass
class SemanticCheck:
    """A single semantic check definition."""

    name: str
    description: str
    # "per_query" — run after every response
    # "on_strict_error" — run only after a strict (HTTP/stream) error is detected
    trigger: str = "per_query"
    # "error" — failure emits a recovery event and is surfaced to the agent
    # "warning" — failure is recorded but does not trigger recovery
    severity: str = "error"
    enabled: bool = True


@dataclass
class SemanticChecksConfig:
    """Parsed semantic checks configuration."""

    checks: List[SemanticCheck] = field(default_factory=list)
    # LLM used to evaluate checks — default to a fast, cheap model
    model: str = "claude-haiku-4-5-20251001"
    # Maximum tokens in the evaluator's response
    max_tokens: int = 512


def find_config_path() -> Optional[Path]:
    """Return the first semantic checks config path found, or None."""
    env = os.environ.get("DTP_SEMANTIC_CHECKS")
    if env:
        return Path(env)
    cwd_path = Path.cwd() / "semantic_checks.yaml"
    if cwd_path.exists():
        return cwd_path
    home_path = Path.home() / ".dt_provenance" / "semantic_checks.yaml"
    if home_path.exists():
        return home_path
    return None


def load_config() -> SemanticChecksConfig:
    """Load semantic checks config.  Returns empty config if no file is found."""
    path = find_config_path()
    if path is None:
        return SemanticChecksConfig()
    try:
        import yaml  # pyyaml

        with open(path) as f:
            data = yaml.safe_load(f) or {}

        _valid_check_fields = SemanticCheck.__dataclass_fields__.keys()
        checks = [
            SemanticCheck(**{k: v for k, v in c.items() if k in _valid_check_fields})
            for c in data.get("checks", [])
            if isinstance(c, dict) and "name" in c and "description" in c
        ]
        return SemanticChecksConfig(
            checks=checks,
            model=data.get("model", "claude-haiku-4-5-20251001"),
            max_tokens=int(data.get("max_tokens", 512)),
        )
    except Exception:
        return SemanticChecksConfig()
