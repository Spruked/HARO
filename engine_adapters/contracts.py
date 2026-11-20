"""Pure Checkpoint 2 contracts.

These types validate state and decisions only. They perform no I/O and are not
connected to the fabric or runtime yet.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping
import uuid

TICKET_STATES = frozenset({"received", "queued", "admitted", "loading", "executing",
                           "completed", "failed", "refused", "cancelled", "timed_out", "abandoned"})
LEASE_STATES = frozenset({"held", "release_pending", "released", "suspect", "release_unverified", "release_attested"})
MEMORY_MODELS = frozenset({"engine_managed_residency", "standing_service", "transient_per_request"})
CANCELLATION_LEVELS = frozenset({"requested", "transport_closed", "engine_acknowledged",
                                 "computation_stopped", "resources_released"})
CANCELLATION_STATUSES = frozenset({"verified", "unverified", "unsupported"})

TICKET_TRANSITIONS = {
    "received": {"queued", "refused", "cancelled"}, "queued": {"admitted", "refused", "cancelled", "timed_out"},
    "admitted": {"loading", "executing", "cancelled", "failed"}, "loading": {"executing", "failed", "cancelled"},
    "executing": {"completed", "failed", "cancelled", "timed_out", "abandoned"},
    "completed": set(), "failed": set(), "refused": set(), "cancelled": set(), "timed_out": set(), "abandoned": set(),
}
LEASE_TRANSITIONS = {
    "held": {"release_pending", "released", "suspect"},
    "release_pending": {"released", "release_unverified", "release_attested", "suspect"},
    "suspect": {"release_pending", "release_attested"},
    "release_unverified": {"release_attested", "released"},
    "release_attested": {"released"}, "released": set(),
}


def _nonempty(value: str, name: str) -> None:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be non-empty")


@dataclass(frozen=True)
class ProvenanceRecord:
    kind: str
    source: str
    observed_at: str
    fingerprint: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _nonempty(self.kind, "provenance kind")
        _nonempty(self.source, "provenance source")
        _nonempty(self.observed_at, "observed_at")


@dataclass(frozen=True)
class ObservationRecord:
    event_id: str
    provenance: ProvenanceRecord
    fingerprint: Mapping[str, str]
    population: str
    completed: bool
    status: str
    values: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _nonempty(self.event_id, "event_id")
        _nonempty(self.population, "population")
        _nonempty(self.status, "status")


@dataclass(frozen=True)
class QualityEvidenceRecord:
    evidence_id: str
    task_id: str
    evaluator: str
    score: float
    sample_count: int
    provenance: ProvenanceRecord
    fingerprint: Mapping[str, str]

    def __post_init__(self) -> None:
        for value, name in ((self.evidence_id, "evidence_id"), (self.task_id, "task_id"), (self.evaluator, "evaluator")):
            _nonempty(value, name)
        if self.sample_count < 1 or isinstance(self.sample_count, bool):
            raise ValueError("sample_count must be positive")
        if isinstance(self.score, bool) or not isinstance(self.score, (int, float)):
            raise ValueError("score must be numeric")


@dataclass(frozen=True)
class LeaseRecord:
    lease_id: str
    generation: int
    state: str = "held"

    def __post_init__(self) -> None:
        _nonempty(self.lease_id, "lease_id")
        if self.generation < 0 or isinstance(self.generation, bool):
            raise ValueError("generation must be non-negative")
        if self.state not in LEASE_STATES:
            raise ValueError(f"unknown lease state {self.state!r}")

    def transition(self, state: str) -> "LeaseRecord":
        if state not in LEASE_TRANSITIONS.get(self.state, set()):
            raise ValueError(f"illegal lease transition {self.state!r} -> {state!r}")
        return LeaseRecord(self.lease_id, self.generation, state)


@dataclass(frozen=True)
class DecisionRecord:
    decision_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    request: Mapping[str, Any] = field(default_factory=dict)
    candidates: tuple[Mapping[str, Any], ...] = ()
    reason_codes: tuple[str, ...] = ()
    evidence_ids: tuple[str, ...] = ()
    rules_hash: str | None = None
    knowledge_snapshot_hash: str | None = None
    verdicts: tuple[Mapping[str, Any], ...] = ()
    selected_plan: Mapping[str, Any] | None = None
    admission: Mapping[str, Any] | None = None
    observed_outcome: Mapping[str, Any] | None = None
    test_mode: bool = False

    def __post_init__(self) -> None:
        _nonempty(self.decision_id, "decision_id")


@dataclass(frozen=True)
class AdmissionExplanation:
    decision_id: str
    decision: str
    reasons: tuple[str, ...] = ()
    selected: str | None = None

    def text(self) -> str:
        detail = "; ".join(self.reasons) if self.reasons else "no additional reason recorded"
        selected = f" Selected {self.selected}." if self.selected else ""
        return f"Decision {self.decision} for {self.decision_id}: {detail}.{selected}"


@dataclass(frozen=True)
class TicketRecord:
    ticket_id: str
    state: str = "received"

    def __post_init__(self) -> None:
        _nonempty(self.ticket_id, "ticket_id")
        if self.state not in TICKET_STATES:
            raise ValueError(f"unknown ticket state {self.state!r}")

    def transition(self, state: str) -> "TicketRecord":
        if state not in TICKET_TRANSITIONS.get(self.state, set()):
            raise ValueError(f"illegal ticket transition {self.state!r} -> {state!r}")
        return TicketRecord(self.ticket_id, state)
