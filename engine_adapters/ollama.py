"""Adapter for engines speaking the Ollama native HTTP protocol (/api/*).

Residency is engine-managed: HARO loads with ``keep_alive`` and unloads with
``keep_alive: 0``. Per-model accelerator memory comes from ``/api/ps``
(``size_vram``), labelled engine-reported. The engine loads a cold model on
first use, so the descriptor declares ``implicit_load_on_execute``.
"""
from __future__ import annotations

import json
import time
from typing import Any

from . import transport
from .base import guarded_execute, timed_health
from .contract import (
    CAP_CHAT, CAP_EMBEDDING, CAP_TEXT_GENERATION, LIFECYCLE_ENGINE_MANAGED, LOCATION_LOCAL,
    MEASURE_ENGINE_REPORTED, MEASURE_NOT_MEASURABLE, RESIDENCY_COLD, RESIDENCY_RESIDENT,
    STATUS_CANCELLED, STATUS_ERROR, STATUS_OK, AdapterDescriptor, AdapterError, CancelToken, CancelledByHost,
    HealthReport, InferenceRequest, InferenceResult, LifecycleResult, ModelState,
)

MIB = 1024 * 1024


def _keep_alive(seconds: float | None) -> Any:
    if seconds is None:
        return None
    if seconds < 0:
        return -1          # keep resident until explicitly unloaded
    return f"{int(seconds)}s"


def _names_match(a: str, b: str) -> bool:
    """Ollama reports 'name:latest' for a model requested as 'name'."""
    norm = lambda s: s if ":" in s else s + ":latest"
    return norm(a) == norm(b)


class OllamaAdapter:
    protocol = "ollama-native"

    def __init__(self, adapter_id: str, base_url: str, *, device_id: str | None = None,
                 capabilities: tuple[str, ...] = (CAP_CHAT, CAP_TEXT_GENERATION, CAP_EMBEDDING),
                 lifecycle_timeout_s: float = 300.0):
        transport.split_base_url(base_url)
        self.adapter_id = adapter_id
        self.base_url = base_url.rstrip("/")
        self.device_id = device_id
        self.capabilities = tuple(capabilities)
        self.lifecycle_timeout_s = lifecycle_timeout_s

    def describe(self) -> AdapterDescriptor:
        return AdapterDescriptor(
            adapter_id=self.adapter_id, protocol=self.protocol, location=LOCATION_LOCAL,
            lifecycle=LIFECYCLE_ENGINE_MANAGED, capabilities=self.capabilities, supports_cancel=True,
            supports_streaming=True, reports_device_memory=True, device_id=self.device_id,
            endpoint=self.base_url, implicit_load_on_execute=True)

    def health(self) -> HealthReport:
        return timed_health(self.base_url, "/api/version")

    def _loaded(self) -> list[dict[str, Any]]:
        resp = transport.request("GET", self.base_url, "/api/ps", timeout_s=10)
        return [m for m in resp.json().get("models", []) if isinstance(m, dict)]

    @staticmethod
    def _state_from_ps(entry: dict[str, Any]) -> ModelState:
        size = entry.get("size")
        size_vram = entry.get("size_vram")
        vram_mib = round(size_vram / MIB) if isinstance(size_vram, int) else None
        details = {
            "size_total_mib": round(size / MIB) if isinstance(size, int) else None,
            "accelerator_fraction": (round(size_vram / size, 4)
                                     if isinstance(size, int) and size and isinstance(size_vram, int) else None),
            "expires_at": entry.get("expires_at"),
            "digest": entry.get("digest"),
            "parameter_size": (entry.get("details") or {}).get("parameter_size"),
            "quantization": (entry.get("details") or {}).get("quantization_level"),
        }
        return ModelState(str(entry.get("name") or entry.get("model")), RESIDENCY_RESIDENT, vram_mib,
                          MEASURE_ENGINE_REPORTED if vram_mib is not None else MEASURE_NOT_MEASURABLE, details)

    def list_models(self) -> list[ModelState]:
        tags = transport.request("GET", self.base_url, "/api/tags", timeout_s=10).json().get("models", [])
        loaded = {str(m.get("name")): m for m in self._loaded()}
        states = []
        for tag in tags:
            name = str(tag.get("name"))
            match = next((v for k, v in loaded.items() if _names_match(k, name)), None)
            if match is not None:
                states.append(self._state_from_ps(match))
            else:
                size = tag.get("size")
                states.append(ModelState(name, RESIDENCY_COLD, None, MEASURE_NOT_MEASURABLE, {
                    "size_on_disk_mib": round(size / MIB) if isinstance(size, int) else None,
                    "digest": tag.get("digest"),
                    "parameter_size": (tag.get("details") or {}).get("parameter_size"),
                    "quantization": (tag.get("details") or {}).get("quantization_level"),
                }))
        return states

    def _resident_state(self, model: str) -> ModelState | None:
        for entry in self._loaded():
            if _names_match(str(entry.get("name")), model):
                return self._state_from_ps(entry)
        return None

    def load(self, model: str, *, keep_alive_s: float | None = None,
             cancel: CancelToken | None = None) -> LifecycleResult:
        started = time.monotonic()
        payload: dict[str, Any] = {"model": model}
        if (ka := _keep_alive(keep_alive_s)) is not None:
            payload["keep_alive"] = ka
        try:
            resp = transport.request("POST", self.base_url, "/api/generate", json_body=payload,
                                     timeout_s=self.lifecycle_timeout_s, cancel=cancel)
            data = resp.json()
            state = self._resident_state(model)
            if state is None:
                raise AdapterError("engine accepted load but model is not listed as loaded")
            return LifecycleResult(model, "load", STATUS_OK, round(time.monotonic() - started, 4),
                                   RESIDENCY_RESIDENT, state.device_memory_mib, state.memory_measurement,
                                   details={"engine_load_duration_ns": data.get("load_duration"), **state.details})
        except (AdapterError, TimeoutError) as exc:
            status = STATUS_CANCELLED if isinstance(exc, CancelledByHost) else STATUS_ERROR
            return LifecycleResult(model, "load", status, round(time.monotonic() - started, 4),
                                   RESIDENCY_COLD, error=f"{type(exc).__name__}: {exc}")

    def unload(self, model: str) -> LifecycleResult:
        started = time.monotonic()
        try:
            before = self._resident_state(model)
            transport.request("POST", self.base_url, "/api/generate",
                              json_body={"model": model, "keep_alive": 0}, timeout_s=60)
            deadline = time.monotonic() + 30
            while self._resident_state(model) is not None:
                if time.monotonic() > deadline:
                    raise AdapterError("model still listed as loaded 30s after unload request")
                time.sleep(0.2)
            return LifecycleResult(model, "unload", STATUS_OK, round(time.monotonic() - started, 4),
                                   RESIDENCY_COLD, before.device_memory_mib if before else None,
                                   before.memory_measurement if before else MEASURE_NOT_MEASURABLE,
                                   details={"note": "device_memory_mib is the engine-reported residency released"})
        except (AdapterError, TimeoutError) as exc:
            return LifecycleResult(model, "unload", STATUS_ERROR, round(time.monotonic() - started, 4),
                                   RESIDENCY_RESIDENT, error=f"{type(exc).__name__}: {exc}")

    def execute(self, request: InferenceRequest, cancel: CancelToken) -> InferenceResult:
        def body(result: InferenceResult, started: float) -> None:
            if "keep_alive" in request.options:
                raise ValueError("keep_alive is controlled by HARO lifecycle and cannot be supplied in options")
            options: dict[str, Any] = {}
            if request.max_output_tokens is not None:
                options["num_predict"] = request.max_output_tokens
            if request.temperature is not None:
                options["temperature"] = request.temperature
            if request.seed is not None:
                options["seed"] = request.seed
            options.update(request.options.get("engine_options", {}))
            extra = {k: v for k, v in request.options.items() if k in ("format", "think")}
            if request.capability == "embedding":
                data = transport.request("POST", self.base_url, "/api/embed",
                                         json_body={"model": request.model, "input": request.inputs.get("input"), **extra},
                                         timeout_s=request.timeout_s, cancel=cancel).json()
                result.output = {"embeddings": data.get("embeddings")}
                result.engine_metrics = {k: data.get(k) for k in ("load_duration", "total_duration", "prompt_eval_count")}
                return
            if request.capability == CAP_CHAT:
                messages = request.inputs.get("messages")
                if not isinstance(messages, list) or not messages:
                    raise ValueError("chat requires a non-empty messages list")
                path, payload = "/api/chat", {"model": request.model, "messages": messages}
            else:
                prompt = request.inputs.get("prompt")
                if not isinstance(prompt, str) or not prompt:
                    raise ValueError("text_generation requires a prompt")
                path, payload = "/api/generate", {"model": request.model, "prompt": prompt}
            payload.update(stream=request.stream, options=options, **extra)
            if not request.stream:
                data = transport.request("POST", self.base_url, path, json_body=payload,
                                         timeout_s=request.timeout_s, cancel=cancel).json()
                events = [data]
            else:
                events = self._stream(path, payload, request, cancel, result, started)
            final = events[-1] if events else {}
            if not final.get("done"):
                raise AdapterError("engine stream ended without a final 'done' record")
            text = "".join((e.get("message") or {}).get("content", "") if path == "/api/chat"
                           else e.get("response", "") for e in events)
            thinking = "".join((e.get("message") or {}).get("thinking", "") or e.get("thinking", "") or ""
                               for e in events)
            result.output = {"text": text, "reasoning": thinking or None, "finish_reason": final.get("done_reason")}
            result.engine_metrics = {k: final.get(k) for k in (
                "total_duration", "load_duration", "prompt_eval_count", "prompt_eval_duration",
                "eval_count", "eval_duration")}
            load_ns = final.get("load_duration")
            # A large load_duration means this request triggered an implicit model load.
            result.engine_metrics["implicit_load_ns"] = load_ns

        return guarded_execute(self.adapter_id, self.capabilities, request, cancel, body)

    def _stream(self, path, payload, request, cancel, result, started) -> list[dict]:
        events: list[dict] = []
        first = None
        for line in transport.stream_lines("POST", self.base_url, path, json_body=payload,
                                           timeout_s=request.timeout_s, cancel=cancel):
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                raise AdapterError("engine sent a malformed stream record") from exc
            if "error" in event:
                raise AdapterError(f"engine error: {event['error']}")
            piece = (event.get("message") or {}).get("content") if path == "/api/chat" else event.get("response")
            if piece and first is None:
                first = time.monotonic()
                result.timings["first_output_s"] = round(first - started, 4)
            events.append(event)
            if event.get("done"):
                break
        return events

    def close(self) -> None:
        return None
