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
MEMORY_MODELS = frozenset({"undeclared", "engine_managed_residency", "standing_service", "transient_per_request"})
PROVENANCE_KINDS = frozenset({"owner_declared", "engine_reported", "device_measured", "externally_evaluated", "client_reported"})
FINGERPRINT_FIELDS = frozenset({"device_id", "driver_version", "runtime_version", "model_digest", "quantization", "context_size", "binding_config_hash"})
CANCELLATION_LEVELS = frozenset({"requested", "transport_closed", "engine_acknowledged",
                                 "computation_stopped", "resources_released"})
CANCELLATION_STATUSES = frozenset({"verified", "unverified", "unsupported"})

TICKET_TRANSITIONS = {
    "received": {"queued", "refused", "cancelled", "abandoned"}, "queued": {"admitted", "refused", "cancelled", "timed_out", "abandoned"},
    "admitted": {"loading", "executing", "cancelled", "failed", "abandoned"}, "loading": {"executing", "failed", "cancelled", "abandoned"},
    "executing": {"completed", "failed", "cancelled", "timed_out", "abandoned"},
    "completed": set(), "failed": set(), "refused": set(), "cancelled": set(), "timed_out": set(), "abandoned": set(),
}
LEASE_TRANSITIONS = {
    "held": {"release_pending", "suspect"},
    "release_pending": {"release_unverified", "release_attested", "suspect"},
    "suspect": {"release_pending", "release_attested"},
    "release_unverified": {"release_attested", "released"},
    "release_attested": {"released"}, "released": set(),
}
RELEASE_BASES = frozenset({"engine_idle", "telemetry_returned", "cancellation_verified", "completed_normally"})


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
        if self.kind not in PROVENANCE_KINDS:
            raise ValueError(f"unsupported provenance kind {self.kind!r}")
        _nonempty(self.source, "provenance source")
        _nonempty(self.observed_at, "observed_at")
        if not self.fingerprint or not FINGERPRINT_FIELDS.issubset(self.fingerprint):
            raise ValueError("provenance fingerprint is missing required identity fields")


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
        if not self.fingerprint or not FINGERPRINT_FIELDS.issubset(self.fingerprint):
            raise ValueError("observation fingerprint is missing required identity fields")


@dataclass(frozen=True)
class QualityEvidenceRecord:
    evidence_id: str
    task_id: str
    evaluator: str
    score: float
    sample_count: int
    provenance: ProvenanceRecord
    fingerprint: Mapping[str, str]
    metric: str
    scale: str

    def __post_init__(self) -> None:
        for value, name in ((self.evidence_id, "evidence_id"), (self.task_id, "task_id"), (self.evaluator, "evaluator")):
            _nonempty(value, name)
        if self.sample_count < 1 or isinstance(self.sample_count, bool):
            raise ValueError("sample_count must be positive")
        if isinstance(self.score, bool) or not isinstance(self.score, (int, float)):
            raise ValueError("score must be numeric")
        _nonempty(self.metric, "metric")
        _nonempty(self.scale, "scale")


@dataclass(frozen=True)
class LeaseRecord:
    lease_id: str
    generation: int
    state: str = "held"
    release_basis: str | None = None
    owner_attestation: str | None = None

    def __post_init__(self) -> None:
        _nonempty(self.lease_id, "lease_id")
        if self.generation < 0 or isinstance(self.generation, bool):
            raise ValueError("generation must be non-negative")
        if self.state not in LEASE_STATES:
            raise ValueError(f"unknown lease state {self.state!r}")
        if self.release_basis is not None and self.release_basis not in RELEASE_BASES:
            raise ValueError("release_basis must be an accepted physical release basis")
        if self.state == "release_attested" and not self.owner_attestation:
            raise ValueError("release_attested requires owner_attestation")

    def transition(self, state: str, *, release_basis: str | None = None,
                   owner_attestation: str | None = None) -> "LeaseRecord":
        if state not in LEASE_TRANSITIONS.get(self.state, set()):
            raise ValueError(f"illegal lease transition {self.state!r} -> {state!r}")
        if state == "released" and release_basis not in RELEASE_BASES:
            raise ValueError("released requires a physical release_basis")
        if state == "release_attested" and not owner_attestation:
            raise ValueError("release_attested requires owner_attestation")
        return LeaseRecord(self.lease_id, self.generation, state, release_basis, owner_attestation)


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
    principal: str | None = None
    resource_snapshot: Mapping[str, Any] | None = None
    reservations: Mapping[str, Any] | None = None
    ordering_reasons: tuple[str, ...] = ()
    lifecycle_transitions: tuple[Mapping[str, Any], ...] = ()
    predicted_vs_observed: Mapping[str, Any] | None = None
    evidence_maturity: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _nonempty(self.decision_id, "decision_id")
        if not self.test_mode and (not self.rules_hash or not self.verdicts):
            raise ValueError("production decision records require rules_hash and at least one verdict")


@dataclass(frozen=True)
class AdmissionExplanation:
    decision_id: str
    decision: str
    required_memory_mib: int | None = None
    memory_source: str | None = None
    live_available_mib: int | None = None
    reserved_mib: int = 0
    safety_margin_mib: int = 0
    vault_verdict: str | None = None
    matched_rules: tuple[str, ...] = ()
    rules_hash: str | None = None
    invariants_checked: tuple[str, ...] = ()
    alternatives: tuple[str, ...] = ()

    def text(self) -> str:
        facts = [f"required memory {self.required_memory_mib} MiB ({self.memory_source})",
                 f"live available {self.live_available_mib} MiB", f"reserved {self.reserved_mib} MiB",
                 f"safety margin {self.safety_margin_mib} MiB", f"Vault verdict {self.vault_verdict}",
                 f"matched rules {', '.join(self.matched_rules) or 'none'}",
                 f"invariants {', '.join(self.invariants_checked) or 'none'}"]
        if self.alternatives:
            facts.append(f"alternatives {', '.join(self.alternatives)}")
        return f"Decision {self.decision} for {self.decision_id}: " + "; ".join(facts) + "."


@dataclass(frozen=True)
class TicketRecord:
    ticket_id: str
    state: str = "received"
    cancel_requested: bool = False

    def __post_init__(self) -> None:
        _nonempty(self.ticket_id, "ticket_id")
        if self.state not in TICKET_STATES:
            raise ValueError(f"unknown ticket state {self.state!r}")

    def transition(self, state: str) -> "TicketRecord":
        if state not in TICKET_TRANSITIONS.get(self.state, set()):
            raise ValueError(f"illegal ticket transition {self.state!r} -> {state!r}")
        return TicketRecord(self.ticket_id, state, self.cancel_requested)
