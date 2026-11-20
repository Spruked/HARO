"""Adapter for llama.cpp's ``llama-server`` (native /health + OpenAI-compatible /v1).

Two lifecycle modes, declared in the descriptor:

* owned_process: HARO starts ``llama-server`` for one model at a time, measures
  device memory around load and unload, and stops only the process it started.
  It refuses to start when the port already answers, and never swaps models
  silently: loading a second model while one is resident is an error.
* none (external): HARO only calls a separately managed server. Load/unload
  are unsupported and memory is not attributable.
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Mapping, Sequence

from . import transport
from .base import timed_health
from .contract import (
    CAP_CHAT, CAP_TEXT_GENERATION, LIFECYCLE_NONE, LIFECYCLE_OWNED_PROCESS, LOCATION_LOCAL,
    MEASURE_DEVICE_DELTA, MEASURE_NOT_MEASURABLE, MEASURE_PROCESS_REPORTED, RESIDENCY_COLD,
    RESIDENCY_RESIDENT, RESIDENCY_UNKNOWN, STATUS_CANCELLED, STATUS_ERROR, STATUS_OK, STATUS_REFUSED,
    AdapterDescriptor, AdapterError, CancelToken, HealthReport, InferenceRequest, InferenceResult,
    LifecycleResult, ModelState, UnsupportedOperation,
)
from .device_telemetry import DeviceSampler, DeviceTelemetry
from .openai_compatible import OpenAICompatibleAdapter


def _server_ready(base_url: str) -> bool:
    try:
        resp = transport.request("GET", base_url, "/health", timeout_s=2)
        return resp.json().get("status") == "ok"
    except (AdapterError, TimeoutError):
        return False


class LlamaServerAdapter:
    protocol = "llama.cpp-server"

    def __init__(self, adapter_id: str, *, base_url: str | None = None,
                 server_path: str | None = None, models: Mapping[str, str] | None = None,
                 host: str = "127.0.0.1", port: int | None = None, gpu_layers: int | None = None,
                 ctx_size: int | None = None, extra_args: Sequence[str] = (),
                 device_id: str | None = None, telemetry: DeviceTelemetry | None = None,
                 device_env: Mapping[str, str] | None = None, log_dir: str | None = None,
                 startup_timeout_s: float = 180.0, capabilities: tuple[str, ...] = (CAP_CHAT, CAP_TEXT_GENERATION)):
        self.adapter_id = adapter_id
        self.owned = server_path is not None
        if self.owned:
            if base_url is not None:
                raise ValueError("give server_path (owned) or base_url (external), not both")
            if not models:
                raise ValueError("owned mode requires a models mapping: model name -> GGUF path")
            if port is None:
                raise ValueError("owned mode requires an explicit port")
            if host not in ("127.0.0.1", "localhost", "::1"):
                raise ValueError("owned servers bind to loopback only")
            self.base_url = f"http://{host}:{port}"
        else:
            if base_url is None:
                raise ValueError("external mode requires base_url")
            transport.split_base_url(base_url)
            self.base_url = base_url.rstrip("/")
        self.server_path = server_path
        self.models = dict(models or {})
        self.port = port
        self.host = host
        self.gpu_layers = gpu_layers
        self.ctx_size = ctx_size
        self.extra_args = tuple(extra_args)
        self.device_id = device_id
        self.telemetry = telemetry
        self.device_env = dict(device_env or {})
        self.log_dir = Path(log_dir) if log_dir else Path(tempfile.gettempdir()) / "haro-llama-server"
        self.startup_timeout_s = startup_timeout_s
        self.capabilities = tuple(capabilities)
        self._process: subprocess.Popen | None = None
        self._loaded_model: str | None = None
        self._log_path: Path | None = None
        self._chat = OpenAICompatibleAdapter(adapter_id, self.base_url + "/v1", location=LOCATION_LOCAL,
                                             capabilities=self.capabilities, device_id=device_id)

    # --- declaration -----------------------------------------------------
    def describe(self) -> AdapterDescriptor:
        return AdapterDescriptor(
            adapter_id=self.adapter_id, protocol=self.protocol, location=LOCATION_LOCAL,
            lifecycle=LIFECYCLE_OWNED_PROCESS if self.owned else LIFECYCLE_NONE,
            capabilities=self.capabilities, supports_cancel=True, supports_streaming=True,
            reports_device_memory=self.owned and self.telemetry is not None and self.device_id is not None,
            device_id=self.device_id, endpoint=self.base_url, implicit_load_on_execute=False)

    def health(self) -> HealthReport:
        if self.owned and self._process is not None and self._process.poll() is not None:
            return HealthReport(False, None, f"owned server exited with code {self._process.returncode}")
        if self.owned and self._process is None:
            return HealthReport(False, None, "owned server not started (no model loaded)")
        return timed_health(self.base_url, "/health", ok=lambda r: r.json().get("status") == "ok")

    def list_models(self) -> list[ModelState]:
        if self.owned:
            self._reap()
            return [ModelState(name, RESIDENCY_RESIDENT if name == self._loaded_model else RESIDENCY_COLD,
                               None, MEASURE_NOT_MEASURABLE, {"path": path})
                    for name, path in self.models.items()]
        listed = self._chat.list_models()
        for state in listed:
            # A separately managed llama-server lists the model it is serving.
            state.residency = RESIDENCY_RESIDENT if state.model else RESIDENCY_UNKNOWN
        return listed

    # --- owned-process lifecycle ----------------------------------------
    def _reap(self) -> None:
        if self._process is not None and self._process.poll() is not None:
            self._process = None
            self._loaded_model = None

    def _log_tail(self, limit: int = 2000) -> str:
        if self._log_path and self._log_path.exists():
            return self._log_path.read_text(encoding="utf-8", errors="replace")[-limit:]
        return ""

    def _memory_for_pid(self, pid: int) -> int | None:
        if self.telemetry is None:
            return None
        usage = self.telemetry.process_memory_mib()
        return None if usage is None else usage.get(pid)

    def load(self, model: str, *, keep_alive_s: float | None = None,
             cancel: CancelToken | None = None) -> LifecycleResult:
        if not self.owned:
            raise UnsupportedOperation(f"{self.adapter_id}: external llama-server lifecycle is not HARO-owned")
        started = time.monotonic()
        self._reap()
        if self._loaded_model == model:
            return LifecycleResult(model, "load", STATUS_OK, 0.0, RESIDENCY_RESIDENT,
                                   details={"note": "already resident in HARO-owned process"})
        if self._loaded_model is not None:
            raise AdapterError(f"{self.adapter_id}: {self._loaded_model!r} is resident; unload it before loading {model!r}")
        if model not in self.models:
            raise AdapterError(f"{self.adapter_id}: no GGUF path configured for model {model!r}")
        exe, gguf = Path(self.server_path), Path(self.models[model])
        if not exe.is_file():
            raise AdapterError(f"llama-server executable not found: {exe}")
        if not gguf.is_file():
            raise AdapterError(f"model file not found: {gguf}")
        if _server_ready(self.base_url):
            raise AdapterError(f"{self.base_url} already serves a ready server that HARO does not own; refusing to start")

        command = [str(exe.resolve()), "-m", str(gguf.resolve()), "--host", self.host,
                   "--port", str(self.port), "--alias", model]
        if self.gpu_layers is not None:
            command += ["-ngl", str(self.gpu_layers)]
        if self.ctx_size is not None:
            command += ["-c", str(self.ctx_size)]
        command += list(self.extra_args)
        env = {**os.environ, **self.device_env}
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self._log_path = self.log_dir / f"{self.adapter_id}-{int(time.time())}.log"

        sampler = (DeviceSampler(self.telemetry, self.device_id)
                   if self.telemetry is not None and self.device_id is not None else None)
        if sampler:
            sampler.__enter__()
        log = self._log_path.open("w", encoding="utf-8")
        try:
            self._process = subprocess.Popen(command, env=env, stdout=log, stderr=subprocess.STDOUT)
            while not _server_ready(self.base_url):
                if self._process.poll() is not None:
                    raise AdapterError(f"llama-server exited with code {self._process.returncode}: {self._log_tail(800)}")
                if cancel is not None and cancel.cancelled:
                    raise _LoadCancelled(cancel.reason or "cancelled")
                if time.monotonic() - started > self.startup_timeout_s:
                    raise AdapterError(f"llama-server not healthy within {self.startup_timeout_s}s")
                time.sleep(0.25)
            self._loaded_model = model
        except (AdapterError, _LoadCancelled) as exc:
            self._stop_process()
            if sampler:
                sampler.__exit__(None, None, None)
            status = STATUS_CANCELLED if isinstance(exc, _LoadCancelled) else STATUS_ERROR
            return LifecycleResult(model, "load", status, round(time.monotonic() - started, 4), RESIDENCY_COLD,
                                   error=f"{type(exc).__name__}: {exc}",
                                   details={"device": sampler.series.summary() if sampler else None,
                                            "log_path": str(self._log_path)})
        finally:
            log.close()
        if sampler:
            sampler.__exit__(None, None, None)
        memory, measurement = self._memory_for_pid(self._process.pid), MEASURE_PROCESS_REPORTED
        device = sampler.series.summary() if sampler else None
        if memory is None and device and device["first_used_mib"] is not None and device["last_used_mib"] is not None:
            memory, measurement = max(0, device["last_used_mib"] - device["first_used_mib"]), MEASURE_DEVICE_DELTA
        if memory is None:
            measurement = MEASURE_NOT_MEASURABLE
        return LifecycleResult(model, "load", STATUS_OK, round(time.monotonic() - started, 4), RESIDENCY_RESIDENT,
                               memory, measurement,
                               details={"pid": self._process.pid, "device": device, "command": command,
                                        "log_path": str(self._log_path)})

    def _stop_process(self) -> int | None:
        proc, self._process, self._loaded_model = self._process, None, None
        if proc is None:
            return None
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=15)
        return proc.returncode

    def unload(self, model: str) -> LifecycleResult:
        if not self.owned:
            raise UnsupportedOperation(f"{self.adapter_id}: external llama-server lifecycle is not HARO-owned")
        started = time.monotonic()
        self._reap()
        if self._loaded_model != model:
            return LifecycleResult(model, "unload", STATUS_OK, 0.0, RESIDENCY_COLD,
                                   details={"note": "model was not resident in a HARO-owned process"})
        sampler = (DeviceSampler(self.telemetry, self.device_id)
                   if self.telemetry is not None and self.device_id is not None else None)
        if sampler:
            sampler.__enter__()
        pid = self._process.pid if self._process else None
        returncode = self._stop_process()
        if sampler:
            time.sleep(0.5)   # let the driver report the release
            sampler.__exit__(None, None, None)
        device = sampler.series.summary() if sampler else None
        released, measurement = None, MEASURE_NOT_MEASURABLE
        if device and device["first_used_mib"] is not None and device["last_used_mib"] is not None:
            released, measurement = max(0, device["first_used_mib"] - device["last_used_mib"]), MEASURE_DEVICE_DELTA
        return LifecycleResult(model, "unload", STATUS_OK, round(time.monotonic() - started, 4), RESIDENCY_COLD,
                               released, measurement,
                               details={"pid": pid, "exit_code": returncode, "device": device,
                                        "note": "device_memory_mib is the whole-device decrease observed"})

    # --- execution -------------------------------------------------------
    def execute(self, request: InferenceRequest, cancel: CancelToken) -> InferenceResult:
        if self.owned:
            self._reap()
            if self._loaded_model != request.model:
                return InferenceResult(request.request_id, self.adapter_id, request.capability, request.model,
                                         STATUS_REFUSED, error=f"model {request.model!r} is not resident; HARO must load it first")
        return self._chat.execute(request, cancel)

    def close(self) -> None:
        if self.owned:
            self._stop_process()


class _LoadCancelled(Exception):
    pass
