"""Adapter for engines speaking the OpenAI-compatible audio protocol.

* speech_synthesis:   POST {base}/audio/speech        (JSON in, audio bytes out)
* speech_recognition: POST {base}/audio/transcriptions (multipart in, JSON out)

Lifecycle is not controllable through this protocol, so load/unload are
unsupported and memory is not attributable. Voice identity is a request input
(``inputs["voice"]``), never an adapter default.
"""
from __future__ import annotations

import io
import time
import wave
from pathlib import Path
from typing import Any

from . import transport
from .base import bearer_headers, guarded_execute, timed_health
from .contract import (
    CAP_SPEECH_RECOGNITION, CAP_SPEECH_SYNTHESIS, LIFECYCLE_NONE, LOCATION_LOCAL, LOCATION_REMOTE,
    MEASURE_NOT_MEASURABLE, RESIDENCY_UNKNOWN, AdapterDescriptor, AdapterError, CancelToken,
    HealthReport, InferenceRequest, InferenceResult, LifecycleResult, ModelState, UnsupportedOperation,
)

AUDIO_TYPES = {".wav": "audio/wav", ".mp3": "audio/mpeg", ".flac": "audio/flac",
               ".ogg": "audio/ogg", ".m4a": "audio/mp4", ".webm": "audio/webm"}


def describe_wav(data: bytes) -> dict[str, Any]:
    """Validate WAV bytes and return their real properties. Raises on empty or invalid audio."""
    try:
        with wave.open(io.BytesIO(data), "rb") as wav:
            frames, rate = wav.getnframes(), wav.getframerate()
            info = {"sample_rate": rate, "channels": wav.getnchannels(),
                    "sample_width_bytes": wav.getsampwidth(), "frames": frames}
    except (wave.Error, EOFError) as exc:
        raise AdapterError(f"engine returned invalid WAV audio: {exc}") from exc
    if frames <= 0 or rate <= 0:
        raise AdapterError("engine returned WAV audio with no frames")
    info["duration_s"] = round(frames / rate, 4)
    return info


class OpenAIAudioAdapter:
    protocol = "openai-audio"

    def __init__(self, adapter_id: str, base_url: str, *, location: str = LOCATION_LOCAL,
                 capabilities: tuple[str, ...] = (CAP_SPEECH_SYNTHESIS, CAP_SPEECH_RECOGNITION),
                 api_key_env: str | None = None, health_path: str = "/models",
                 device_id: str | None = None):
        transport.split_base_url(base_url)
        unsupported = set(capabilities) - {CAP_SPEECH_SYNTHESIS, CAP_SPEECH_RECOGNITION}
        if unsupported:
            raise ValueError(f"capabilities not served by this protocol: {sorted(unsupported)}")
        self.adapter_id = adapter_id
        self.base_url = base_url.rstrip("/")
        self.location = location
        self.capabilities = tuple(capabilities)
        self.api_key_env = api_key_env
        self.health_path = health_path
        self.device_id = device_id if location == LOCATION_LOCAL else None

    def describe(self) -> AdapterDescriptor:
        return AdapterDescriptor(
            adapter_id=self.adapter_id, protocol=self.protocol, location=self.location,
            lifecycle=LIFECYCLE_NONE, capabilities=self.capabilities, supports_cancel=True,
            supports_streaming=False, reports_device_memory=False, device_id=self.device_id,
            endpoint=self.base_url)

    def health(self) -> HealthReport:
        try:
            headers = bearer_headers(self.api_key_env)
        except AdapterError as exc:
            return HealthReport(False, None, str(exc))
        return timed_health(self.base_url, self.health_path, headers=headers)

    def list_models(self) -> list[ModelState]:
        resp = transport.request("GET", self.base_url, "/models", headers=bearer_headers(self.api_key_env),
                                 timeout_s=10)
        return [ModelState(str(m.get("id")), RESIDENCY_UNKNOWN, None, MEASURE_NOT_MEASURABLE)
                for m in resp.json().get("data", []) if isinstance(m, dict) and m.get("id")]

    def load(self, model: str, *, keep_alive_s: float | None = None,
             cancel: CancelToken | None = None) -> LifecycleResult:
        raise UnsupportedOperation(f"{self.adapter_id}: model lifecycle is not controllable via {self.protocol}")

    def unload(self, model: str) -> LifecycleResult:
        raise UnsupportedOperation(f"{self.adapter_id}: model lifecycle is not controllable via {self.protocol}")

    def execute(self, request: InferenceRequest, cancel: CancelToken) -> InferenceResult:
        def synthesize(result: InferenceResult) -> None:
            text, voice = request.inputs.get("text"), request.inputs.get("voice")
            fmt = request.inputs.get("format", "wav")
            output_path = request.inputs.get("output_path")
            if not isinstance(text, str) or not text.strip():
                raise ValueError("speech_synthesis requires non-empty text")
            if not isinstance(voice, str) or not voice:
                raise ValueError("speech_synthesis requires an explicit voice")
            if not output_path:
                raise ValueError("speech_synthesis requires output_path")
            payload = {"model": request.model, "voice": voice, "input": text, "response_format": fmt,
                       **request.options}
            resp = transport.request("POST", self.base_url, "/audio/speech", json_body=payload,
                                     headers=bearer_headers(self.api_key_env),
                                     timeout_s=request.timeout_s, cancel=cancel)
            if not resp.body:
                raise AdapterError("engine returned empty audio")
            if resp.headers.get("content-type", "").startswith("application/json"):
                raise AdapterError(f"engine returned JSON instead of audio: {resp.body[:300]!r}")
            audio = describe_wav(resp.body) if fmt == "wav" else {}
            path = Path(output_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(resp.body)
            result.output = {"audio_path": str(path), "bytes": len(resp.body), "format": fmt,
                             "voice": voice, **audio}
            if "duration_s" in audio:
                result.engine_metrics["real_time_factor"] = None  # filled below from HARO timing

        def transcribe(result: InferenceResult) -> None:
            audio_path = request.inputs.get("audio_path")
            if not audio_path or not Path(audio_path).is_file():
                raise ValueError("speech_recognition requires an existing audio_path")
            path = Path(audio_path)
            fields = {"model": request.model, "response_format": "json"}
            if request.inputs.get("language"):
                fields["language"] = str(request.inputs["language"])
            fields.update({k: str(v) for k, v in request.options.items()})
            body, ctype = transport.multipart(fields, {"file": (
                path.name, path.read_bytes(), AUDIO_TYPES.get(path.suffix.lower(), "application/octet-stream"))})
            resp = transport.request("POST", self.base_url, "/audio/transcriptions", body=body,
                                     headers={"Content-Type": ctype, **bearer_headers(self.api_key_env)},
                                     timeout_s=request.timeout_s, cancel=cancel)
            data = resp.json()
            if not isinstance(data, dict) or not isinstance(data.get("text"), str):
                raise AdapterError("transcription response has no text field")
            result.output = {"text": data["text"], "language": data.get("language")}
            result.engine_metrics = {k: data.get(k) for k in ("duration", "language") if k in data}

        def body(result: InferenceResult, started: float) -> None:
            if request.capability == CAP_SPEECH_SYNTHESIS:
                synthesize(result)
                elapsed = time.monotonic() - started
                if result.output.get("duration_s"):
                    result.engine_metrics["real_time_factor"] = round(elapsed / result.output["duration_s"], 4)
            else:
                transcribe(result)

        return guarded_execute(self.adapter_id, self.capabilities, request, cancel, body)

    def close(self) -> None:
        return None
