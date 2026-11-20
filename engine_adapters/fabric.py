"""HARO capability fabric: routing, admission, residency and execution authority.

Applications ask for a *capability* (chat, speech_synthesis, ...). The fabric
chooses a configured binding (adapter + model), applies host invariants and
Vault Logic governance, admits the work against live device memory and its own
outstanding reservations in priority order, ensures residency through the
adapter's declared lifecycle, executes with cancellation, and records the
measured experience in A.I.M.S.

Host invariants enforced here (never delegated to editable rules):
  * remote execution only when the request explicitly allows it
  * no silent fallback: only the first eligible binding is used unless the
    request explicitly allows fallback
  * no admission without a memory figure for local accelerator work
  * no admission when live device telemetry is unavailable for that work
  * models are loaded/unloaded only through adapter lifecycle operations,
    and only models HARO itself loaded are ever evicted
"""
from __future__ import annotations

import heapq
import itertools
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Mapping

from .contract import (
    LIFECYCLE_ENGINE_MANAGED, LIFECYCLE_NONE, LIFECYCLE_OWNED_PROCESS, LOCATION_REMOTE,
    RESIDENCY_RESIDENT, STATUS_CANCELLED, STATUS_ERROR, STATUS_OK, STATUS_REFUSED, AdapterError, CancelToken,
    EngineAdapter, InferenceRequest, InferenceResult, KNOWN_CAPABILITIES, UnsupportedOperation,
    new_request_id,
)
from .device_telemetry import DeviceSampler, DeviceTelemetry, find_device

PRIORITY_INTERACTIVE = 0   # live voice / conversation: always first in line
PRIORITY_USER_TASK = 1     # user-requested work
PRIORITY_BACKGROUND = 2    # indexing, batch generation


@dataclass(frozen=True)
class Binding:
    """Deployment configuration: which adapter+model provides a capability."""

    capability: str
    adapter_id: str
    model: str
    # Planning figures for admission (MiB). Required for local accelerator work.
    resident_mib: int | None = None     # memory the model occupies once loaded
    transient_mib: int | None = None    # extra memory one request needs (KV cache, scratch)
    keep_alive_s: float | None = None   # residency hint passed to engine-managed adapters

    def validate(self) -> None:
        if self.capability not in KNOWN_CAPABILITIES:
            raise ValueError(f"binding has unknown capability {self.capability!r}")
        for name in ("resident_mib", "transient_mib"):
            value = getattr(self, name)
            if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
                raise ValueError(f"binding {self.adapter_id}/{self.model}: {name} must be a non-negative integer")


@dataclass
class CapabilityRequest:
    """What an application submits. It never names an engine, device or provider."""

    capability: str
    inputs: dict[str, Any]
    client_id: str
    priority: int = PRIORITY_USER_TASK
    allow_remote: bool = False
    allow_fallback: bool = False
    max_output_tokens: int | None = None
    temperature: float | None = None
    seed: int | None = None
    timeout_s: float = 120.0
    queue_timeout_s: float = 60.0
    stream: bool = True
    options: dict[str, Any] = field(default_factory=dict)
    request_id: str = field(default_factory=new_request_id)


@dataclass
class FabricOutcome:
    request_id: str
    capability: str
    client_id: str
    status: str
    binding: dict[str, Any] | None
    result: dict[str, Any] | None
    decisions: list[dict[str, Any]] = field(default_factory=list)
    timings: dict[str, float] = field(default_factory=dict)
    device: dict[str, Any] | None = None
    error: str | None = None
    evidence_entry_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class Ticket:
    """Handle for one submitted request: wait for the outcome or cancel it."""

    def __init__(self, request: CapabilityRequest, future: Future, cancel: CancelToken):
        self.request = request
        self._future = future
        self._cancel = cancel

    def cancel(self, reason: str = "cancelled by client") -> None:
        self._cancel.cancel(reason)

    def outcome(self, timeout: float | None = None) -> FabricOutcome:
        return self._future.result(timeout)

    @property
    def done(self) -> bool:
        return self._future.done()


class _DeviceGate:
    """Priority-ordered admission to one accelerator.

    Conservative headroom = live free memory - outstanding HARO reservations.
    A reservation is held from admission until the work's memory is either
    released (request finished) or visible in live telemetry (load finished).
    """

    def __init__(self, device_id: str, telemetry: DeviceTelemetry, max_concurrent: int, safety_margin_mib: int):
        self.device_id = device_id
        self.telemetry = telemetry
        self.max_concurrent = max_concurrent
        self.safety_margin_mib = safety_margin_mib
        self._cv = threading.Condition()
        self._waiting: list[tuple[int, int, str]] = []
        self._seq = itertools.count()
        self._reservations: dict[str, int] = {}
        self._active = 0

    def snapshot(self) -> dict[str, Any]:
        with self._cv:
            return {"device_id": self.device_id, "active": self._active, "max_concurrent": self.max_concurrent,
                    "reserved_mib": sum(self._reservations.values()), "waiting": len(self._waiting)}

    def live_free_mib(self) -> int | None:
        """Live free memory, or None when telemetry fails or the driver does not report it."""
        try:
            snap = find_device(self.telemetry, self.device_id)
        except Exception:   # any telemetry failure means "unknown", which callers refuse on
            return None
        return None if snap is None else snap.free_mib

    def headroom_mib(self) -> int | None:
        free = self.live_free_mib()
        if free is None:
            return None
        return free - sum(self._reservations.values()) - self.safety_margin_mib

    def acquire(self, ticket_id: str, priority: int, need_mib: int, cancel: CancelToken,
                deadline: float, make_room: Callable[[int], int]) -> dict[str, Any]:
        """Block until admitted. Returns the admission record; raises on refusal."""
        entry = (priority, next(self._seq), ticket_id)
        with self._cv:
            heapq.heappush(self._waiting, entry)
            try:
                while True:
                    if cancel.cancelled:
                        raise _Refused(f"cancelled while queued: {cancel.reason}")
                    if self._waiting[0] == entry and self._active < self.max_concurrent:
                        headroom = self.headroom_mib()
                        if headroom is None:
                            raise _Refused(f"live memory telemetry unavailable for device {self.device_id}")
                        if need_mib > headroom and need_mib > 0:
                            freed = make_room(need_mib - headroom)
                            if freed > 0:
                                continue
                        if need_mib <= headroom:
                            heapq.heappop(self._waiting)
                            self._reservations[ticket_id] = need_mib
                            self._active += 1
                            self._cv.notify_all()
                            return {"device_id": self.device_id, "need_mib": need_mib, "headroom_mib": headroom,
                                    "reserved_total_mib": sum(self._reservations.values())}
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise _Refused(f"not admitted within queue timeout (need {need_mib} MiB)")
                    self._cv.wait(min(0.5, remaining))
            except BaseException:
                if entry in self._waiting:
                    self._waiting.remove(entry)
                    heapq.heapify(self._waiting)
                    self._cv.notify_all()
                raise

    def adjust(self, ticket_id: str, need_mib: int) -> None:
        """Shrink a reservation once part of it is visible in live telemetry (e.g. a completed load)."""
        with self._cv:
            if ticket_id in self._reservations:
                self._reservations[ticket_id] = min(self._reservations[ticket_id], need_mib)
            self._cv.notify_all()

    def release(self, ticket_id: str) -> None:
        with self._cv:
            self._reservations.pop(ticket_id, None)
            self._active = max(0, self._active - 1)
            self._cv.notify_all()


class _Refused(Exception):
    pass


class Fabric:
    def __init__(self, adapters: Mapping[str, EngineAdapter], bindings: list[Binding], *,
                 telemetry: DeviceTelemetry | None = None, subsystems: Any = None,
                 device_concurrency: Mapping[str, int] | None = None, safety_margin_mib: int = 256,
                 allow_eviction: bool = False, max_workers: int = 8, sample_interval_s: float = 0.25):
        self.adapters = dict(adapters)
        self.bindings = list(bindings)
        for binding in self.bindings:
            binding.validate()
            if binding.adapter_id not in self.adapters:
                raise ValueError(f"binding refers to unknown adapter {binding.adapter_id!r}")
            if binding.capability not in self.adapters[binding.adapter_id].describe().capabilities:
                raise ValueError(f"adapter {binding.adapter_id!r} does not declare {binding.capability!r}")
        self.telemetry = telemetry
        self.subsystems = subsystems
        self.allow_eviction = allow_eviction
        self.sample_interval_s = sample_interval_s
        self._gates: dict[str, _DeviceGate] = {}
        for adapter in self.adapters.values():
            desc = adapter.describe()
            if desc.device_id and desc.device_id not in self._gates and telemetry is not None:
                limit = (device_concurrency or {}).get(desc.device_id, 1)
                self._gates[desc.device_id] = _DeviceGate(desc.device_id, telemetry, limit, safety_margin_mib)
        # Models HARO itself made resident: (adapter_id, model) -> last-used monotonic time.
        self._haro_resident: dict[tuple[str, str], float] = {}
        self._in_use: dict[tuple[str, str], int] = {}
        self._state_lock = threading.RLock()
        self._record_lock = threading.Lock()
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="haro-fabric")

    # --- public API ----------------------------------------------------------
    def submit(self, request: CapabilityRequest) -> Ticket:
        cancel = CancelToken()
        future = self._pool.submit(self._run, request, cancel)
        return Ticket(request, future, cancel)

    def run(self, request: CapabilityRequest) -> FabricOutcome:
        return self.submit(request).outcome()

    def inventory(self) -> dict[str, Any]:
        adapters = {}
        for adapter_id, adapter in self.adapters.items():
            entry: dict[str, Any] = {"descriptor": adapter.describe().to_dict()}
            entry["health"] = adapter.health().to_dict()
            try:
                entry["models"] = [m.to_dict() for m in adapter.list_models()]
            except (AdapterError, TimeoutError) as exc:
                entry["models_error"] = f"{type(exc).__name__}: {exc}"
            adapters[adapter_id] = entry
        devices = None
        if self.telemetry is not None:
            try:
                devices = [d.to_dict() for d in self.telemetry.devices()]
            except Exception as exc:  # telemetry failure is reported, never fatal
                devices = {"error": f"{type(exc).__name__}: {exc}"}
        return {"adapters": adapters, "devices": devices,
                "gates": {k: g.snapshot() for k, g in self._gates.items()},
                "haro_resident": [list(k) for k in self._haro_resident],
                "bindings": [asdict(b) for b in self.bindings]}

    def unload(self, adapter_id: str, model: str, *, reason: str = "requested") -> dict[str, Any]:
        with self._state_lock:
            if self._in_use.get((adapter_id, model)):
                raise AdapterError(f"{adapter_id}/{model} is executing; refusing to unload")
            result = self.adapters[adapter_id].unload(model).to_dict()
            if result["status"] == STATUS_OK:
                self._haro_resident.pop((adapter_id, model), None)
        self._record("model_unload", {"adapter_id": adapter_id, "model": model, "reason": reason, "lifecycle": result})
        return result

    def close(self) -> None:
        self._pool.shutdown(wait=True, cancel_futures=True)
        for adapter in self.adapters.values():
            adapter.close()

    # --- internals -------------------------------------------------------------
    def _record(self, kind: str, event: dict[str, Any]) -> str | None:
        if self.subsystems is None:
            return None
        with self._record_lock:
            entry = self.subsystems.record_event({"kind": kind, **event},
                                                 provenance={"source": "haro.fabric", "kind": kind})
        return entry.get("entry_id")

    def _govern(self, context: dict[str, Any], request_id: str) -> dict[str, Any]:
        if self.subsystems is None:
            return {"decision": "not_evaluated", "allowed": True,
                    "reason": "no Vault Logic subsystem configured for this fabric"}
        with self._record_lock:
            outcome = self.subsystems.evaluate_and_record(
                context, provenance={"source": "haro.fabric", "request_id": request_id})
        return {**outcome["evaluation"], "evidence_entry_id": outcome["evidence_entry_id"]}

    def _candidates(self, request: CapabilityRequest, outcome: FabricOutcome) -> list[Binding]:
        eligible = []
        for binding in (b for b in self.bindings if b.capability == request.capability):
            desc = self.adapters[binding.adapter_id].describe()
            if desc.location == LOCATION_REMOTE and not request.allow_remote:
                outcome.decisions.append({"binding": f"{binding.adapter_id}/{binding.model}",
                                          "skipped": "remote execution not allowed by this request"})
                continue
            eligible.append(binding)
            if not request.allow_fallback:
                break
        return eligible

    @staticmethod
    def _same_model(listed: str, wanted: str) -> bool:
        # Engines that tag models report "name:latest" for a model requested as "name".
        norm = lambda s: s if ":" in s else s + ":latest"
        return listed == wanted or norm(listed) == norm(wanted)

    def _is_resident(self, adapter: EngineAdapter, model: str) -> bool | None:
        try:
            for state in adapter.list_models():
                if self._same_model(state.model, model):
                    return state.residency == RESIDENCY_RESIDENT
            return False
        except (AdapterError, TimeoutError):
            return None

    def _make_room(self, device_id: str, needed_mib: int, outcome: FabricOutcome, exclude: tuple[str, str]) -> int:
        """Evict idle models HARO itself loaded on this device, least recently used first."""
        if not self.allow_eviction:
            return 0
        freed = 0
        with self._state_lock:
            candidates = sorted(
                (t, key) for key, t in self._haro_resident.items()
                if key != exclude and not self._in_use.get(key)
                and self.adapters[key[0]].describe().device_id == device_id)
            for _, (adapter_id, model) in candidates:
                if freed >= needed_mib:
                    break
                result = self.adapters[adapter_id].unload(model)
                outcome.decisions.append({"evicted": f"{adapter_id}/{model}", "status": result.status,
                                          "released_mib": result.device_memory_mib,
                                          "measurement": result.memory_measurement})
                if result.status == STATUS_OK:
                    self._haro_resident.pop((adapter_id, model), None)
                    freed += result.device_memory_mib or 1   # progress marker when release is unmeasured
                    self._record("model_evicted", {"adapter_id": adapter_id, "model": model,
                                                   "lifecycle": result.to_dict(), "for_request": outcome.request_id})
        return freed

    def _run(self, request: CapabilityRequest, cancel: CancelToken) -> FabricOutcome:
        started = time.monotonic()
        outcome = FabricOutcome(request.request_id, request.capability, request.client_id, STATUS_REFUSED, None, None)
        try:
            if request.capability not in KNOWN_CAPABILITIES:
                raise _Refused(f"unknown capability {request.capability!r}")
            candidates = self._candidates(request, outcome)
            if not candidates:
                raise _Refused("no eligible binding for this capability")
            last_error = None
            for binding in candidates:
                try:
                    self._attempt(binding, request, cancel, outcome, started)
                    return outcome
                except _Refused as exc:
                    last_error = str(exc)
                    outcome.decisions.append({"binding": f"{binding.adapter_id}/{binding.model}", "refused": last_error})
                    if cancel.cancelled:
                        break
            raise _Refused(last_error or "no binding admitted")
        except _Refused as exc:
            outcome.status = STATUS_REFUSED if not cancel.cancelled else STATUS_CANCELLED
            outcome.error = str(exc)
        except Exception as exc:   # never let a worker die silently
            outcome.status, outcome.error = STATUS_ERROR, f"{type(exc).__name__}: {exc}"
        finally:
            outcome.timings["total_s"] = round(time.monotonic() - started, 4)
            entry = self._record("capability_request", _operational_record(outcome))
            if entry:
                outcome.evidence_entry_ids.append(entry)
        return outcome

    def _attempt(self, binding: Binding, request: CapabilityRequest, cancel: CancelToken,
                 outcome: FabricOutcome, started: float) -> None:
        adapter = self.adapters[binding.adapter_id]
        desc = adapter.describe()
        key = (binding.adapter_id, binding.model)
        outcome.binding = {**asdict(binding), "protocol": desc.protocol, "location": desc.location,
                           "lifecycle": desc.lifecycle}
        local_device = desc.location != LOCATION_REMOTE and desc.device_id is not None
        gate = self._gates.get(desc.device_id) if local_device else None
        if local_device and gate is None:
            raise _Refused(f"device {desc.device_id} has no live telemetry; refusing accelerator work")

        resident = self._is_resident(adapter, binding.model) if desc.lifecycle != LIFECYCLE_NONE else None
        can_load = desc.lifecycle in (LIFECYCLE_OWNED_PROCESS, LIFECYCLE_ENGINE_MANAGED)
        need_load = bool(can_load and resident is False)
        if resident is None and (desc.lifecycle == LIFECYCLE_OWNED_PROCESS or desc.implicit_load_on_execute):
            # Executing could trigger an unadmitted load; refuse rather than guess.
            raise _Refused("cannot determine model residency; refusing work that could load a model unadmitted")
        need = 0
        if local_device:
            if need_load and binding.resident_mib is None:
                raise _Refused("no resident memory figure configured for a model that must be loaded")
            if binding.transient_mib is None:
                raise _Refused("no per-request memory figure configured for local accelerator work")
            need = (binding.resident_mib if need_load else 0) + binding.transient_mib

        available = gate.headroom_mib() if gate else 0
        evaluation = self._govern({
            "capability": request.capability,
            "requested_memory_mib": need,
            "available_memory_mib": available if available is not None else -1,
            "requires_model_residency": False,
            "remote_execution": desc.location == LOCATION_REMOTE,
            "client_id": request.client_id,
            "priority": request.priority,
        }, request.request_id)
        outcome.decisions.append({"vault_logic": evaluation})
        if not evaluation.get("allowed"):
            raise _Refused(f"Vault Logic decision {evaluation.get('decision')}: {evaluation.get('reason')}")

        admission = None
        ticket = request.request_id
        if gate is not None:
            deadline = time.monotonic() + request.queue_timeout_s
            admission = gate.acquire(ticket, request.priority, need, cancel, deadline,
                                     lambda deficit: self._make_room(desc.device_id, deficit, outcome, key))
            outcome.decisions.append({"admitted": admission})
        outcome.timings["queued_s"] = round(time.monotonic() - started, 4)
        with self._state_lock:
            self._in_use[key] = self._in_use.get(key, 0) + 1
        try:
            if need_load:
                lifecycle = adapter.load(binding.model, keep_alive_s=binding.keep_alive_s, cancel=cancel)
                outcome.decisions.append({"load": lifecycle.to_dict()})
                self._record("model_load", {"adapter_id": binding.adapter_id, "model": binding.model,
                                            "lifecycle": lifecycle.to_dict(), "for_request": request.request_id,
                                            "planned_resident_mib": binding.resident_mib})
                if lifecycle.status != STATUS_OK:
                    raise _Refused(f"load failed: {lifecycle.error}")
                with self._state_lock:
                    self._haro_resident[key] = time.monotonic()
                outcome.timings["load_s"] = lifecycle.duration_s
                if gate is not None:
                    # The loaded weights now appear in live telemetry; keep only the per-request reservation.
                    gate.adjust(ticket, binding.transient_mib or 0)
            inference = InferenceRequest(
                capability=request.capability, model=binding.model, inputs=request.inputs,
                request_id=request.request_id, max_output_tokens=request.max_output_tokens,
                temperature=request.temperature, seed=request.seed, timeout_s=request.timeout_s,
                stream=request.stream, options=request.options)
            sampler = DeviceSampler(self.telemetry, desc.device_id, self.sample_interval_s) if gate else None
            if sampler:
                with sampler:
                    result = adapter.execute(inference, cancel)
                outcome.device = sampler.series.summary()
            else:
                result = adapter.execute(inference, cancel)
            outcome.result = result.to_dict()
            outcome.status = result.status
            outcome.error = result.error or result.cancel_reason
            with self._state_lock:
                if key in self._haro_resident:
                    self._haro_resident[key] = time.monotonic()
        finally:
            with self._state_lock:
                self._in_use[key] -= 1
            if gate is not None:
                gate.release(ticket)


def _shape(value: Any) -> Any:
    """Describe a value without retaining its content."""
    if isinstance(value, str):
        return {"type": "text", "chars": len(value)}
    if isinstance(value, (list, tuple)):
        return {"type": "list", "items": len(value)}
    if isinstance(value, dict):
        return {"type": "object", "keys": sorted(value)}
    return value


def _operational_record(outcome: FabricOutcome) -> dict[str, Any]:
    """Experience for A.I.M.S.: what ran, where, how long, with what memory. Not what was said.

    Generated text, prompts, transcripts and audio paths stay with the requesting
    application; HARO's memory keeps their shape only.
    """
    record = outcome.to_dict()
    result = record.get("result")
    if result:
        result["output"] = {k: (v if k in ("bytes", "duration_s", "sample_rate", "channels", "format",
                                           "finish_reason", "frames", "sample_width_bytes") else _shape(v))
                            for k, v in (result.get("output") or {}).items()}
    return record
