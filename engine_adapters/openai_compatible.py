"""Adapter for engines speaking the OpenAI-compatible chat/completions protocol.

Serves remote providers (location="remote", credential from an environment
variable) and local servers that expose the same protocol but whose lifecycle
HARO does not control. Load/unload are explicitly unsupported here.
"""
from __future__ import annotations

import json
import time
from typing import Any

from . import transport
from .base import bearer_headers, guarded_execute, timed_health
from .contract import (
    CAP_CHAT, CAP_EMBEDDING, CAP_TEXT_GENERATION, LIFECYCLE_NONE, LOCATION_LOCAL, LOCATION_REMOTE,
    MEASURE_NOT_APPLICABLE, MEASURE_NOT_MEASURABLE, RESIDENCY_REMOTE, RESIDENCY_UNKNOWN,
    AdapterDescriptor, AdapterError, CancelToken, HealthReport, InferenceRequest, InferenceResult,
    LifecycleResult, ModelState, UnsupportedOperation,
)


def parse_sse_data(line: bytes) -> Any | None:
    """Return the JSON object of one SSE ``data:`` line, ``"[DONE]"``, or None."""
    text = line.decode("utf-8", errors="replace").strip()
    if not text.startswith("data:"):
        return None
    payload = text[5:].strip()
    if payload == "[DONE]":
        return "[DONE]"
    try:
        return json.loads(payload)
    except json.JSONDecodeError:
        return None


class OpenAICompatibleAdapter:
    protocol = "openai-chat"

    def __init__(self, adapter_id: str, base_url: str, *, location: str = LOCATION_REMOTE,
                 api_key_env: str | None = None, capabilities: tuple[str, ...] = (CAP_CHAT,),
                 include_usage_in_stream: bool = False, device_id: str | None = None,
                 health_timeout_s: float = 10.0, require_tls: bool = True):
        if location not in (LOCATION_LOCAL, LOCATION_REMOTE):
            raise ValueError("location must be local or remote")
        unsupported = set(capabilities) - {CAP_CHAT, CAP_TEXT_GENERATION, CAP_EMBEDDING}
        if unsupported:
            raise ValueError(f"capabilities not served by this protocol: {sorted(unsupported)}")
        transport.split_base_url(base_url)
        if location == LOCATION_REMOTE and require_tls and not base_url.lower().startswith("https://"):
            raise ValueError("remote adapters require an https base_url")
        self.adapter_id = adapter_id
        self.base_url = base_url.rstrip("/")
        self.location = location
        self.api_key_env = api_key_env
        self.capabilities = tuple(capabilities)
        self.include_usage_in_stream = include_usage_in_stream
        self.device_id = device_id if location == LOCATION_LOCAL else None
        self.health_timeout_s = health_timeout_s

    # --- declaration -----------------------------------------------------
    def describe(self) -> AdapterDescriptor:
        return AdapterDescriptor(
            adapter_id=self.adapter_id, protocol=self.protocol, location=self.location,
            lifecycle=LIFECYCLE_NONE, capabilities=self.capabilities, supports_cancel=True,
            supports_streaming=True, reports_device_memory=False, device_id=self.device_id,
            endpoint=self.base_url)

    def health(self) -> HealthReport:
        try:
            headers = bearer_headers(self.api_key_env)
        except AdapterError as exc:
            return HealthReport(False, None, str(exc))
        return timed_health(self.base_url, "/models", headers=headers, timeout_s=self.health_timeout_s)

    def list_models(self) -> list[ModelState]:
        resp = transport.request("GET", self.base_url, "/models",
                                 headers=bearer_headers(self.api_key_env), timeout_s=self.health_timeout_s)
        residency = RESIDENCY_REMOTE if self.location == LOCATION_REMOTE else RESIDENCY_UNKNOWN
        measurement = MEASURE_NOT_APPLICABLE if self.location == LOCATION_REMOTE else MEASURE_NOT_MEASURABLE
        return [ModelState(str(m.get("id")), residency, None, measurement,
                           {"owned_by": m.get("owned_by")})
                for m in resp.json().get("data", []) if isinstance(m, dict) and m.get("id")]

    # --- lifecycle: not controllable through this protocol --------------
    def load(self, model: str, *, keep_alive_s: float | None = None,
             cancel: CancelToken | None = None) -> LifecycleResult:
        raise UnsupportedOperation(f"{self.adapter_id}: model lifecycle is not controllable via {self.protocol}")

    def unload(self, model: str) -> LifecycleResult:
        raise UnsupportedOperation(f"{self.adapter_id}: model lifecycle is not controllable via {self.protocol}")

    # --- execution -------------------------------------------------------
    def _chat_payload(self, request: InferenceRequest) -> dict[str, Any]:
        if request.capability == CAP_CHAT:
            messages = request.inputs.get("messages")
            if not isinstance(messages, list) or not messages:
                raise ValueError("chat requires a non-empty messages list")
        else:
            prompt = request.inputs.get("prompt")
            if not isinstance(prompt, str) or not prompt:
                raise ValueError("text_generation requires a prompt")
            messages = [{"role": "user", "content": prompt}]
        payload: dict[str, Any] = {"model": request.model, "messages": messages, "stream": request.stream}
        if request.max_output_tokens is not None:
            payload["max_tokens"] = request.max_output_tokens
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        if request.seed is not None:
            payload["seed"] = request.seed
        if request.stream and self.include_usage_in_stream:
            payload["stream_options"] = {"include_usage": True}
        for key, value in request.options.items():
            payload.setdefault(key, value)
        return payload

    def execute(self, request: InferenceRequest, cancel: CancelToken) -> InferenceResult:
        def body(result: InferenceResult, started: float) -> None:
            headers = bearer_headers(self.api_key_env)
            if request.capability == CAP_EMBEDDING:
                resp = transport.request("POST", self.base_url, "/embeddings",
                                         json_body={"model": request.model, "input": request.inputs.get("input")},
                                         headers=headers, timeout_s=request.timeout_s, cancel=cancel)
                data = resp.json()
                result.output = {"embeddings": [d.get("embedding") for d in data.get("data", [])]}
                result.engine_metrics = {"usage": data.get("usage")}
                return
            payload = self._chat_payload(request)
            if not request.stream:
                resp = transport.request("POST", self.base_url, "/chat/completions", json_body=payload,
                                         headers=headers, timeout_s=request.timeout_s, cancel=cancel)
                data = resp.json()
                choice = (data.get("choices") or [{}])[0]
                message = choice.get("message") or {}
                result.output = {"text": message.get("content") or "",
                                 "reasoning": message.get("reasoning_content"),
                                 "finish_reason": choice.get("finish_reason")}
                result.engine_metrics = {"usage": data.get("usage"), "timings": data.get("timings")}
                return
            text, reasoning, finish, usage, timings = [], [], None, None, None
            first = None
            for line in transport.stream_lines("POST", self.base_url, "/chat/completions", json_body=payload,
                                               headers=headers, timeout_s=request.timeout_s, cancel=cancel):
                event = parse_sse_data(line)
                if event is None:
                    continue
                if event == "[DONE]":
                    break
                if "error" in event:
                    raise AdapterError(f"engine error: {event['error']}")
                usage = event.get("usage") or usage
                timings = event.get("timings") or timings
                for choice in event.get("choices") or []:
                    delta = choice.get("delta") or {}
                    piece = delta.get("content")
                    thought = delta.get("reasoning_content")
                    if (piece or thought) and first is None:
                        first = time.monotonic()
                    if piece:
                        text.append(piece)
                    if thought:
                        reasoning.append(thought)
                    finish = choice.get("finish_reason") or finish
            if first is None:
                raise AdapterError("stream ended without generated output")
            result.output = {"text": "".join(text), "reasoning": "".join(reasoning) or None,
                             "finish_reason": finish}
            result.timings["first_output_s"] = round(first - started, 4)
            result.engine_metrics = {"usage": usage, "timings": timings}

        return guarded_execute(self.adapter_id, self.capabilities, request, cancel, body)

    def close(self) -> None:
        return None
