"""Engine-agnostic adapter contract for HARO.

Python 3.11+, standard library only.

HARO's scheduler and fabric depend only on this module. Every inference engine
(local or remote, owned or externally managed) is reached through an adapter
that implements ``EngineAdapter`` and declares, rather than implies, what it
can do:

* which capabilities it serves (text generation, chat, speech synthesis, ...)
* where execution happens (local device or remote provider)
* who controls model lifecycle (HARO-owned process, engine-managed residency
  through an API, or none)
* whether execution can be cancelled and streamed
* how its memory figures were obtained (engine-reported, device delta, or not
  measurable)

Unsupported operations raise ``UnsupportedOperation``. Adapters never fall
back to another engine, another device, or remote execution on their own.
"""
from __future__ import annotations

import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol, runtime_checkable

# --- Capabilities ----------------------------------------------------------
# Capability names describe the work, never the engine that performs it.
CAP_TEXT_GENERATION = "text_generation"
CAP_CHAT = "chat"
CAP_EMBEDDING = "embedding"
CAP_SPEECH_SYNTHESIS = "speech_synthesis"
CAP_SPEECH_RECOGNITION = "speech_recognition"
KNOWN_CAPABILITIES = frozenset({
    CAP_TEXT_GENERATION, CAP_CHAT, CAP_EMBEDDING, CAP_SPEECH_SYNTHESIS, CAP_SPEECH_RECOGNITION,
})

WORK_REASONING = "reasoning"
WORK_ANALYSIS = "analysis"
WORK_PLANNING = "planning"
WORK_CONVERSATION = "conversation"
WORK_TRANSCRIPTION = "transcription"
WORK_VOICE_OUTPUT = "voice_output"
WORK_RETRIEVAL_EMBEDDING = "retrieval_embedding"
KNOWN_WORK_TYPES = frozenset({
    WORK_REASONING, WORK_ANALYSIS, WORK_PLANNING, WORK_CONVERSATION,
    WORK_TRANSCRIPTION, WORK_VOICE_OUTPUT, WORK_RETRIEVAL_EMBEDDING,
})
KNOWN_LATENCY_CLASSES = frozenset({"interactive", "user_task", "background"})
KNOWN_PRIVACY_CLASSES = frozenset({"local_only", "remote_allowed"})

# --- Execution location ----------------------------------------------------
LOCATION_LOCAL = "local"
LOCATION_REMOTE = "remote"

# --- Lifecycle control -----------------------------------------------------
# owned_process:   HARO starts and stops the engine process itself.
# engine_managed:  the engine exposes load/unload (residency) through its API.
# none:            HARO can only call the engine; residency is outside its control.
LIFECYCLE_OWNED_PROCESS = "owned_process"
LIFECYCLE_ENGINE_MANAGED = "engine_managed"
LIFECYCLE_NONE = "none"

# --- Residency -------------------------------------------------------------
RESIDENCY_RESIDENT = "resident"   # loaded and immediately executable
RESIDENCY_COLD = "cold"           # installed / available, not loaded
RESIDENCY_REMOTE = "remote"       # executed by a remote provider; no local residency
RESIDENCY_UNKNOWN = "unknown"     # engine does not report residency

# --- Measurement provenance ------------------------------------------------
MEASURE_ENGINE_REPORTED = "engine_reported"   # the engine reported this figure
MEASURE_PROCESS_REPORTED = "process_reported" # device driver attributed it to a PID
MEASURE_DEVICE_DELTA = "device_delta"         # whole-device before/after difference
MEASURE_NOT_MEASURABLE = "not_measurable"     # no trustworthy source exists
MEASURE_NOT_APPLICABLE = "not_applicable"     # e.g. remote execution: no local VRAM

# --- Result status ---------------------------------------------------------
STATUS_OK = "ok"
STATUS_ERROR = "error"
STATUS_CANCELLED = "cancelled"
STATUS_TIMEOUT = "timeout"
STATUS_REFUSED = "refused"


class AdapterError(RuntimeError):
    """Base class for adapter failures. Messages must never contain secrets."""


class UnsupportedOperation(AdapterError):
    """The adapter explicitly does not support the requested operation."""


class EngineUnavailable(AdapterError):
    """The engine could not be reached or is not healthy."""


class CancelledByHost(AdapterError):
    """HARO cancelled the operation."""


@dataclass(frozen=True)
class Binding:
    """Deployment configuration: which adapter and model provides a capability."""

    capability: str
    adapter_id: str
    model: str
    resident_mib: int | None = None
    transient_mib: int | None = None
    keep_alive_s: float | None = None
    approved_work_types: tuple[str, ...] = ()
    memory_gating: str = "required"

    def validate(self) -> None:
        if self.capability not in KNOWN_CAPABILITIES:
            raise ValueError(f"binding has unknown capability {self.capability!r}")
        if not self.approved_work_types:
            raise ValueError(f"binding {self.adapter_id}/{self.model}: approved_work_types must be declared")
        if self.memory_gating not in {"required", "tracked_unmeasured"}:
            raise ValueError(f"binding {self.adapter_id}/{self.model}: unsupported memory_gating {self.memory_gating!r}")
        unknown_work = set(self.approved_work_types) - KNOWN_WORK_TYPES
        if unknown_work:
            raise ValueError(f"binding {self.adapter_id}/{self.model}: unknown work types {sorted(unknown_work)}")
        for name in ("resident_mib", "transient_mib"):
            value = getattr(self, name)
            if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
                raise ValueError(f"binding {self.adapter_id}/{self.model}: {name} must be a non-negative integer")


def new_request_id() -> str:
    return uuid.uuid4().hex


class CancelToken:
    """Host-owned cancellation signal shared between HARO and one execution."""

    def __init__(self) -> None:
        self._event = threading.Event()
        self.reason: str | None = None
        self.cancelled_at: float | None = None

    def cancel(self, reason: str = "cancelled by host") -> None:
        if not self._event.is_set():
            self.reason = reason
            self.cancelled_at = time.monotonic()
            self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def wait(self, timeout: float | None = None) -> bool:
        return self._event.wait(timeout)


@dataclass(frozen=True)
class AdapterDescriptor:
    """What an adapter declares about itself. HARO schedules from this, not from names."""

    adapter_id: str
    protocol: str                      # wire protocol, e.g. "ollama-native", "openai-chat"
    location: str                      # LOCATION_LOCAL | LOCATION_REMOTE
    lifecycle: str                     # LIFECYCLE_*
    capabilities: tuple[str, ...]
    supports_cancel: bool
    supports_streaming: bool
    reports_device_memory: bool        # can supply per-model memory figures
    device_id: str | None = None       # local accelerator identity when known (UUID preferred)
    endpoint: str | None = None        # base URL without credentials
    # True when executing against a non-resident model makes the engine load it
    # implicitly. HARO must then admit the load before dispatching the request.
    implicit_load_on_execute: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ModelState:
    model: str
    residency: str
    device_memory_mib: int | None = None
    memory_measurement: str = MEASURE_NOT_MEASURABLE
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class HealthReport:
    healthy: bool
    latency_s: float | None
    detail: str
    checked_at_monotonic: float = field(default_factory=time.monotonic)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class LifecycleResult:
    """Outcome of load / unload. Memory figures carry their measurement provenance."""

    model: str
    operation: str                     # "load" | "unload"
    status: str
    duration_s: float
    residency_after: str
    device_memory_mib: int | None = None
    memory_measurement: str = MEASURE_NOT_MEASURABLE
    error: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class InferenceRequest:
    """One unit of work. ``inputs`` is capability-specific and engine-neutral.

    chat:               {"messages": [{"role": ..., "content": ...}, ...]}
    text_generation:    {"prompt": str}
    embedding:          {"input": str | list[str]}
    speech_synthesis:   {"text": str, "voice": str, "format": "wav", "output_path": str}
    speech_recognition: {"audio_path": str, "language": str | None}
    """

    capability: str
    model: str
    inputs: dict[str, Any]
    work_type: str
    request_id: str = field(default_factory=new_request_id)
    max_output_tokens: int | None = None
    temperature: float | None = None
    seed: int | None = None
    timeout_s: float = 120.0
    stream: bool = True
    options: dict[str, Any] = field(default_factory=dict)

    def validate(self) -> None:
        if self.capability not in KNOWN_CAPABILITIES:
            raise ValueError(f"unknown capability: {self.capability!r}")
        if self.work_type not in KNOWN_WORK_TYPES:
            raise ValueError(f"unknown work_type: {self.work_type!r}")
        if not self.model:
            raise ValueError("model is required")
        if not isinstance(self.inputs, dict):
            raise ValueError("inputs must be an object")
        if not (0 < self.timeout_s <= 3600):
            raise ValueError("timeout_s must be within (0, 3600]")
        if self.max_output_tokens is not None and self.max_output_tokens <= 0:
            raise ValueError("max_output_tokens must be positive")


@dataclass
class InferenceResult:
    request_id: str
    adapter_id: str
    capability: str
    model: str
    status: str
    output: dict[str, Any] = field(default_factory=dict)
    # Wall-clock timings measured by HARO (seconds), e.g. first_output_s, total_s.
    timings: dict[str, Any] = field(default_factory=dict)
    # Figures the engine itself reported (token counts, engine timings), unaltered.
    engine_metrics: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    cancel_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@runtime_checkable
class EngineAdapter(Protocol):
    """Contract every engine adapter implements.

    Methods are synchronous and blocking; the fabric runs them in worker threads
    so that a slow engine never blocks the host's event loop or UI.
    """

    def describe(self) -> AdapterDescriptor: ...

    def health(self) -> HealthReport: ...

    def list_models(self) -> list[ModelState]: ...

    def load(self, model: str, *, keep_alive_s: float | None = None,
             cancel: CancelToken | None = None) -> LifecycleResult: ...

    def unload(self, model: str) -> LifecycleResult: ...

    def execute(self, request: InferenceRequest, cancel: CancelToken) -> InferenceResult: ...

    def close(self) -> None: ...
