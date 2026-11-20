"""Engine-agnostic capability quality profiles.

Profiles contain owner declarations and measured evidence summaries. Missing
figures remain ``None``; this module never invents measurements or scores.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class MetricSummary:
    value: float | None = None
    p50: float | None = None
    p95: float | None = None
    sample_count: int = 0
    window: str | None = None
    source: str = "unmeasured"

    def __post_init__(self) -> None:
        if self.sample_count < 0:
            raise ValueError("sample_count must be non-negative")
        if self.source not in {"measured", "engine_reported", "owner_declared", "unmeasured"}:
            raise ValueError(f"unsupported metric source {self.source!r}")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CapabilityProfile:
    adapter_id: str
    protocol: str
    model: str
    device_id: str | None = None
    device_name: str | None = None
    runtime_version: str | None = None
    model_digest: str | None = None
    quantization: str | None = None
    location: str = "local"
    approved_work_types: tuple[str, ...] = ()
    privacy_class: str | None = None
    language_coverage: tuple[str, ...] = ()
    quality_tier: str | None = None
    metrics: dict[str, MetricSummary] = field(default_factory=dict)
    availability: bool | None = None
    cooldown_until: float | None = None

    def metric(self, name: str) -> MetricSummary:
        return self.metrics.get(name, MetricSummary())

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["metrics"] = {name: metric.to_dict() for name, metric in self.metrics.items()}
        return result

    def approves(self, work_type: str) -> bool:
        return work_type in self.approved_work_types
