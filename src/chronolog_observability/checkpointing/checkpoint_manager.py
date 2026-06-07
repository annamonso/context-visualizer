"""Checkpoint system for DTProvenance — semantic restore points with rollback scoring."""

import json
import os
import threading
import uuid
from dataclasses import dataclass, asdict, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Optional, Dict, List


class CheckpointType(Enum):
    """Checkpoint classification types."""
    AUTO_END_TURN = "auto_end_turn"
    AUTO_TOOL_BOUNDARY = "auto_tool_boundary"
    AUTO_DELEGATION = "auto_delegation"
    MANUAL = "manual"


@dataclass
class Checkpoint:
    """A single checkpoint (restore point) in an agent's session."""
    checkpoint_id: str
    session_id: str
    sequence_id: int
    name: str
    checkpoint_type: CheckpointType
    timestamp: str  # ISO format
    score: float
    token_count: int = 0
    latency_ms: float = 0.0
    metadata: Dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        """Convert to dict, with checkpoint_type as string."""
        d = asdict(self)
        d["checkpoint_type"] = self.checkpoint_type.value
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Checkpoint":
        """Construct from dict, converting checkpoint_type string to enum."""
        d = d.copy()
        if isinstance(d.get("checkpoint_type"), str):
            d["checkpoint_type"] = CheckpointType(d["checkpoint_type"])
        if "metadata" not in d:
            d["metadata"] = {}
        return cls(**d)


@dataclass
class RollbackPlan:
    """A plan for rolling back a session and affected peers."""
    plan_id: str
    trigger_session_id: str
    trigger_sequence_id: int
    error_description: str
    failed_messages: List = field(default_factory=list)
    sessions: Dict = field(default_factory=dict)  # {session_id: {"checkpoint": Checkpoint, "score": float}}
    timestamp: str = ""  # ISO format

    def to_dict(self) -> dict:
        """Convert to dict, converting Checkpoint objects in sessions."""
        return {
            "plan_id": self.plan_id,
            "trigger_session_id": self.trigger_session_id,
            "trigger_sequence_id": self.trigger_sequence_id,
            "error_description": self.error_description,
            "failed_messages": self.failed_messages,
            "sessions": {
                sid: {
                    "checkpoint": info["checkpoint"].to_dict() if isinstance(info["checkpoint"], Checkpoint) else info["checkpoint"],
                    "score": info["score"],
                }
                for sid, info in self.sessions.items()
            },
            "timestamp": self.timestamp,
        }


class CheckpointManager:
    """Manages semantic checkpoints with rollback planning and replay injection."""

    def __init__(self):
        """Initialize checkpoint manager with persistent store."""
        self._lock = threading.Lock()
        self._checkpoints: Dict[str, List[Checkpoint]] = {}  # {session_id: [Checkpoint]}
        self._pending_replays: Dict[str, RollbackPlan] = {}  # {session_id: RollbackPlan}
        self._recent_plans: Dict[str, RollbackPlan] = {}  # {plan_id: RollbackPlan}
        self._store_path = self._get_store_path()
        self._load()

    def _get_store_path(self) -> Path:
        """Get checkpoint store path from env or default."""
        env_path = os.environ.get("DTP_CHECKPOINT_STORE")
        if env_path:
            return Path(env_path)
        return Path.home() / ".dt_provenance" / "checkpoints.json"

    def _load(self) -> None:
        """Load checkpoints from persistent store."""
        if not self._store_path.exists():
            return
        try:
            with open(self._store_path, "r") as f:
                data = json.load(f)
            for session_id, cp_list in data.items():
                self._checkpoints[session_id] = [
                    Checkpoint.from_dict(cp) for cp in cp_list
                ]
        except Exception:
            pass

    def _save(self) -> None:
        """Atomically persist checkpoints to disk."""
        try:
            self._store_path.parent.mkdir(parents=True, exist_ok=True)
            data = {
                sid: [cp.to_dict() for cp in cps]
                for sid, cps in self._checkpoints.items()
            }
            # Write to temp file then rename (atomic on POSIX)
            tmp_path = self._store_path.with_suffix(".json.tmp")
            with open(tmp_path, "w") as f:
                json.dump(data, f)
            tmp_path.replace(self._store_path)
        except Exception:
            pass

    def _compute_score(self, cp: Checkpoint) -> float:
        """Compute checkpoint quality score (0-20 range)."""
        # Type base scores
        type_base = {
            CheckpointType.MANUAL: 10.0,
            CheckpointType.AUTO_END_TURN: 8.0,
            CheckpointType.AUTO_DELEGATION: 6.0,
            CheckpointType.AUTO_TOOL_BOUNDARY: 5.0,
        }.get(cp.checkpoint_type, 5.0)

        # Token component: more tokens = lower score (0-3 points)
        token_component = 3.0 * (1.0 - min(cp.token_count / 100_000, 1.0))

        # Latency component: slower = lower score (0-2 points)
        latency_component = 2.0 * (1.0 - min(cp.latency_ms / 60_000, 1.0))

        score = type_base + token_component + latency_component
        return round(score, 3)

    def create_checkpoint(
        self,
        session_id: str,
        sequence_id: int,
        name: str,
        checkpoint_type: CheckpointType = CheckpointType.MANUAL,
        token_count: int = 0,
        latency_ms: float = 0.0,
        metadata: Optional[dict] = None,
    ) -> Checkpoint:
        """Create and store a checkpoint."""
        if metadata is None:
            metadata = {}

        checkpoint = Checkpoint(
            checkpoint_id=str(uuid.uuid4()),
            session_id=session_id,
            sequence_id=sequence_id,
            name=name,
            checkpoint_type=checkpoint_type,
            timestamp=datetime.utcnow().isoformat() + "Z",
            score=0.0,  # Placeholder, will compute below
            token_count=token_count,
            latency_ms=latency_ms,
            metadata=metadata,
        )

        # Compute score
        checkpoint.score = self._compute_score(checkpoint)

        with self._lock:
            # Avoid duplicates: don't add if same session+sequence+type already exists
            bucket = self._checkpoints.setdefault(session_id, [])
            for existing in bucket:
                if (existing.sequence_id == sequence_id and
                    existing.checkpoint_type == checkpoint_type):
                    # Duplicate detected; return existing
                    return existing

            bucket.append(checkpoint)
            bucket.sort(key=lambda cp: cp.sequence_id)
            self._save()

        return checkpoint

    def detect_and_create(
        self,
        session_id: str,
        sequence_id: int,
        response_body: str,
        provider: str,
        token_count: int = 0,
        latency_ms: float = 0.0,
    ) -> Optional[Checkpoint]:
        """Detect natural breakpoints in LLM response and create checkpoint if found."""
        cp_type = self._detect_checkpoint_type(response_body, provider)
        if cp_type is None:
            return None

        name = f"{cp_type.value}@{sequence_id}"
        return self.create_checkpoint(
            session_id, sequence_id, name, cp_type, token_count, latency_ms
        )

    def _detect_checkpoint_type(self, response_body: str, provider: str) -> Optional[CheckpointType]:
        """Detect checkpoint type from LLM response."""
        try:
            body = json.loads(response_body)
        except Exception:
            return None

        if provider == "anthropic":
            stop_reason = body.get("stop_reason", "")
            content = body.get("content", [])

            # Check for tool_use blocks
            has_tool_use = any(
                isinstance(block, dict) and block.get("type") == "tool_use"
                for block in content
            )

            if stop_reason == "end_turn" and not has_tool_use:
                return CheckpointType.AUTO_END_TURN
            elif stop_reason == "tool_use":
                return CheckpointType.AUTO_TOOL_BOUNDARY

        elif provider in ("openai", "ollama"):
            choices = body.get("choices", [])
            if choices and isinstance(choices[0], dict):
                finish_reason = choices[0].get("finish_reason", "")
                message = choices[0].get("message", {})

                # Check for tool_calls
                has_tool_calls = bool(message.get("tool_calls"))

                if finish_reason == "stop" and not has_tool_calls:
                    return CheckpointType.AUTO_END_TURN
                elif finish_reason == "tool_calls":
                    return CheckpointType.AUTO_TOOL_BOUNDARY

        return None

    def get_checkpoints(self, session_id: str) -> List[Checkpoint]:
        """Get all checkpoints for a session, sorted by sequence_id."""
        with self._lock:
            cps = self._checkpoints.get(session_id, [])
            return sorted(cps, key=lambda cp: cp.sequence_id)

    def find_best_restore_point(
        self, session_id: str, before_sequence_id: int
    ) -> Optional[Checkpoint]:
        """Find highest-scored checkpoint before a given sequence_id."""
        candidates = [
            cp for cp in self.get_checkpoints(session_id)
            if cp.sequence_id < before_sequence_id
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda cp: cp.score)

    def score_restore_point(
        self, session_id: str, sequence_id: int, token_count: int = 0, latency_ms: float = 0.0
    ) -> float:
        """Get score for a restore point (existing or synthesized)."""
        # Check if checkpoint exists
        for cp in self.get_checkpoints(session_id):
            if cp.sequence_id == sequence_id:
                return cp.score

        # Synthesize a MANUAL checkpoint for scoring
        dummy = Checkpoint(
            checkpoint_id="",
            session_id=session_id,
            sequence_id=sequence_id,
            name="",
            checkpoint_type=CheckpointType.MANUAL,
            timestamp="",
            score=0.0,
            token_count=token_count,
            latency_ms=latency_ms,
        )
        return self._compute_score(dummy)

    def build_rollback_plan(
        self,
        trigger_session_id: str,
        trigger_sequence_id: int,
        error_description: str,
        failed_messages: Optional[List] = None,
    ) -> RollbackPlan:
        """Build a rollback plan for a trigger session and affected peers."""
        # Fetch failed_messages if not provided
        if failed_messages is None:
            failed_messages = self._fetch_failed_messages(trigger_session_id, trigger_sequence_id)

        # Find best restore point for trigger session
        trigger_restore = self.find_best_restore_point(trigger_session_id, trigger_sequence_id)

        # Find affected peer sessions and their best restore points
        sessions_dict = {}
        if trigger_restore is not None:
            sessions_dict[trigger_session_id] = {
                "checkpoint": trigger_restore,
                "score": trigger_restore.score,
            }

        # Find peer sessions affected by recovery events
        try:
            from ..adapters.registry import registry as _reg
            from ..adapters.base import Capability as _Cap
            _cc = _reg.source_for(_Cap.RECOVERY)
            all_sessions = _cc.get_sessions()
            session_list = []
            for _, result in all_sessions.items():
                if isinstance(result, list):
                    for s in result:
                        if isinstance(s, dict):
                            sid = s.get("session_id", "")
                            if sid:
                                session_list.append(sid)

            for peer_sid in session_list:
                    if peer_sid == trigger_session_id:
                        continue

                    try:
                        events = _cc.get_recovery_events(peer_sid)
                    except Exception:
                        continue

                    # Check for unacknowledged events from trigger session
                    unack_from_trigger = [
                        e for e in events
                        if e.get("source_session_id") == trigger_session_id and not e.get("acknowledged")
                    ]

                    if unack_from_trigger:
                        # Find earliest event timestamp
                        earliest_ts = min(
                            (e.get("timestamp", "") for e in unack_from_trigger),
                            default=""
                        )
                        if earliest_ts:
                            best_cp = self._find_best_before_timestamp(peer_sid, earliest_ts)
                            if best_cp is not None:
                                sessions_dict[peer_sid] = {
                                    "checkpoint": best_cp,
                                    "score": best_cp.score,
                                }
        except Exception:
            pass

        plan = RollbackPlan(
            plan_id=str(uuid.uuid4()),
            trigger_session_id=trigger_session_id,
            trigger_sequence_id=trigger_sequence_id,
            error_description=error_description,
            failed_messages=failed_messages,
            sessions=sessions_dict,
            timestamp=datetime.utcnow().isoformat() + "Z",
        )

        # Store in recent plans (bounded to 100 entries)
        with self._lock:
            self._recent_plans[plan.plan_id] = plan
            if len(self._recent_plans) > 100:
                # Evict oldest by timestamp
                oldest_id = min(
                    self._recent_plans.items(),
                    key=lambda x: x[1].timestamp,
                )[0]
                del self._recent_plans[oldest_id]

        return plan

    def _fetch_failed_messages(self, session_id: str, sequence_id: int) -> List:
        """Fetch failed interaction messages from CTE."""
        try:
            from ..adapters.registry import registry as _reg
            from ..adapters.base import Capability as _Cap
            _cc = _reg.source_for(_Cap.RECOVERY)
            result = _cc.get_interaction(session_id, sequence_id)
            for cid, data in result.items():
                if isinstance(data, str):
                    try:
                        data = json.loads(data)
                    except Exception:
                        continue
                if isinstance(data, dict):
                    req = data.get("request", {})
                    if isinstance(req, str):
                        try:
                            req = json.loads(req)
                        except Exception:
                            pass
                    if isinstance(req, dict):
                        return req.get("body", {}).get("messages", [])
        except Exception:
            pass
        return []

    def _find_best_before_timestamp(self, session_id: str, iso_timestamp: str) -> Optional[Checkpoint]:
        """Find best-scored checkpoint before a given timestamp."""
        candidates = [
            cp for cp in self.get_checkpoints(session_id)
            if cp.timestamp < iso_timestamp
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda cp: cp.score)

    def register_replay(self, session_id: str, plan: RollbackPlan) -> None:
        """Register a rollback plan to be injected in the next request."""
        with self._lock:
            self._pending_replays[session_id] = plan

    def consume_replay(self, session_id: str) -> Optional[RollbackPlan]:
        """Pop and return the pending replay for a session (one-shot)."""
        with self._lock:
            return self._pending_replays.pop(session_id, None)

    def has_pending_replay(self, session_id: str) -> bool:
        """Check if a session has a pending replay."""
        with self._lock:
            return session_id in self._pending_replays

    def build_replay_injection(self, plan: RollbackPlan) -> str:
        """Build system prompt preamble for replay injection."""
        lines = [
            "[DTProvenance Rollback — Replay with Modification]",
            f"Your session has been rolled back to checkpoint before sequence {plan.trigger_sequence_id}.",
            f"Error that triggered rollback: {plan.error_description}",
            "",
            "The following messages are from the failed interaction that caused this error.",
            "Review them carefully to understand what went wrong and avoid repeating the same mistake:",
            "",
            "[Failed Interaction — Messages]",
        ]

        for msg in plan.failed_messages:
            role = "unknown"
            content = ""
            if isinstance(msg, dict):
                role = msg.get("role", "unknown")
                content_raw = msg.get("content", "")
                if isinstance(content_raw, str):
                    content = content_raw
                elif isinstance(content_raw, list):
                    # Extract text from content list
                    text_parts = []
                    for item in content_raw:
                        if isinstance(item, dict) and item.get("type") == "text":
                            text_parts.append(item.get("text", ""))
                    content = " ".join(text_parts)

            # Truncate to 500 chars per message
            if len(content) > 500:
                content = content[:497] + "..."

            lines.append(f"{role}: {content}")

        lines += [
            "[End Failed Interaction]",
            "",
            "Adjust your approach based on the above. Do not reproduce the pattern that caused the error.",
        ]

        return "\n".join(lines)


# Module-level singleton
manager = CheckpointManager()
