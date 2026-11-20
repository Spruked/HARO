"""Explainable capability eligibility and ranking.

The resolver is a pure planning layer. It does not load models, execute
requests, or replace HARO's live admission gate.
"""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Mapping

from .contract import LOCATION_REMOTE, WORK_CONVERSATION, KNOWN_CAPABILITIES, KNOWN_WORK_TYPES
from .fabric import Binding
from .profiles import CapabilityProfile


@dataclass(frozen=True)
class ResolutionRequest:
    capability: str
    work_type: str = WORK_CONVERSATION
    latency: str = "interactive"
    privacy: str = "local_only"
    allow_remote: bool = False
    allow_fallback: bool = False


@dataclass
class CandidateEvaluation:
    adapter_id: str
    model: str
    eligible: bool
    reasons: list[str] = field(default_factory=list)
    scores: dict[str, float | None] = field(default_factory=dict)
    required_memory_mib: int | None = None
    memory_source: str = "unmeasured"
    privacy_class: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Resolution:
    request: ResolutionRequest
    candidates: list[CandidateEvaluation]

    @property
    def eligible(self) -> list[CandidateEvaluation]:
        return [candidate for candidate in self.candidates if candidate.eligible]

    def to_dict(self) -> dict[str, Any]:
        return {"request": asdict(self.request), "candidates": [candidate.to_dict() for candidate in self.candidates]}


class CapabilityResolver:
    def __init__(self, adapters: Mapping[str, Any], bindings: list[Binding],
                 profiles: Mapping[tuple[str, str], CapabilityProfile] | None = None,
                 live_available_mib: Callable[[str], int | None] | None = None,
                 health: Mapping[str, bool] | None = None):
        self.adapters = adapters
        self.bindings = bindings
        self.profiles = profiles or {}
        self.live_available_mib = live_available_mib or (lambda _device_id: None)
        self.health = health or {}

    def resolve(self, request: ResolutionRequest | Mapping[str, Any]) -> Resolution:
        if isinstance(request, Mapping):
            request = ResolutionRequest(**dict(request))
        if request.capability not in KNOWN_CAPABILITIES:
            raise ValueError(f"unknown capability {request.capability!r}")
        if request.work_type not in KNOWN_WORK_TYPES:
            raise ValueError(f"unknown work_type {request.work_type!r}")
        candidates = [self._evaluate(binding, request) for binding in self.bindings if binding.capability == request.capability]
        eligible = [candidate for candidate in candidates if candidate.eligible]
        eligible.sort(key=lambda candidate: self._rank(candidate, request), reverse=True)
        ordered = eligible + [candidate for candidate in candidates if not candidate.eligible]
        return Resolution(request, ordered)

    def _evaluate(self, binding: Binding, request: ResolutionRequest) -> CandidateEvaluation:
        adapter = self.adapters[binding.adapter_id]
        descriptor = adapter.describe()
        profile = self.profiles.get((binding.adapter_id, binding.model))
        candidate = CandidateEvaluation(binding.adapter_id, binding.model, True)
        candidate.required_memory_mib = (binding.resident_mib or 0) + (binding.transient_mib or 0)
        candidate.memory_source = "deployment_config" if binding.resident_mib is not None and binding.transient_mib is not None else "unmeasured"
        candidate.privacy_class = profile.privacy_class if profile else None
        if request.work_type not in binding.approved_work_types:
            candidate.eligible = False; candidate.reasons.append("work type is not owner-approved")
        if descriptor.location == LOCATION_REMOTE and (request.privacy == "local_only" or not request.allow_remote):
            candidate.eligible = False; candidate.reasons.append("remote execution is not permitted for this request")
        if profile and profile.privacy_class and request.privacy == "local_only" and profile.privacy_class != "local_only":
            candidate.eligible = False; candidate.reasons.append("profile privacy class does not satisfy local_only")
        if self.health.get(binding.adapter_id) is False:
            candidate.eligible = False; candidate.reasons.append("adapter health is unavailable")
        if profile and profile.cooldown_until and profile.cooldown_until > time.time():
            candidate.eligible = False; candidate.reasons.append("provider is in cooldown")
        if descriptor.device_id:
            available = self.live_available_mib(descriptor.device_id)
            if available is None:
                candidate.eligible = False; candidate.reasons.append("live device memory is unavailable")
            elif candidate.required_memory_mib is None or candidate.required_memory_mib > available:
                candidate.eligible = False; candidate.reasons.append("required memory does not fit live availability")
        if descriptor.location != LOCATION_REMOTE and descriptor.device_id and candidate.memory_source == "unmeasured":
            candidate.eligible = False; candidate.reasons.append("local accelerator memory requirement is unmeasured")
        if profile:
            candidate.scores = {
                "latency": self._metric_score(profile, "first_output_latency", request.latency),
                "quality": 1.0 if profile.quality_tier else None,
                "resource": self._metric_score(profile, "resident_memory", "resource"),
                "reliability": self._metric_score(profile, "success_rate", "reliability"),
            }
        else:
            candidate.scores = {"latency": None, "quality": None, "resource": None, "reliability": None}
        return candidate

    @staticmethod
    def _metric_score(profile: CapabilityProfile, name: str, _mode: str) -> float | None:
        metric = profile.metric(name)
        if metric.p95 is None and metric.value is None:
            return None
        value = metric.p95 if metric.p95 is not None else metric.value
        return None if value is None else 1.0 / max(float(value), 0.000001)

    @staticmethod
    def _rank(candidate: CandidateEvaluation, request: ResolutionRequest) -> tuple[float, float, float, float]:
        scores = candidate.scores
        # Unmeasured candidates remain eligible but rank after measured ones.
        return tuple(scores.get(name) if scores.get(name) is not None else float("-inf")
                     for name in ("latency", "quality", "resource", "reliability"))
