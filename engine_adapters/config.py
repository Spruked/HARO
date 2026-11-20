"""Build adapters, bindings and a fabric from a deployment configuration file.

The configuration is the only place that names engines, endpoints, models and
devices. Credentials are referenced by environment-variable name only; a
configuration containing an inline secret is rejected.

Vendor-specific wiring (such as pinning an owned process to one NVIDIA device)
lives here, in the builder, so the fabric and contract stay vendor-neutral.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from .contract import Binding, EngineAdapter, LOCATION_LOCAL, LOCATION_REMOTE
from .device_telemetry import DeviceTelemetry, NvidiaSmiTelemetry
from .fabric import Fabric
from .llama_cpp import LlamaServerAdapter
from .ollama import OllamaAdapter
from .openai_audio import OpenAIAudioAdapter
from .openai_compatible import OpenAICompatibleAdapter

SECRET_KEYS = {"api_key", "token", "password", "secret", "authorization"}

ADAPTER_KEYS = {
    "ollama": {"id", "type", "base_url", "device", "capabilities", "lifecycle_timeout_s"},
    "llama_cpp": {"id", "type", "base_url", "server_path", "models", "host", "port", "gpu_layers",
                  "ctx_size", "extra_args", "device", "log_dir", "startup_timeout_s", "capabilities"},
    "openai_compatible": {"id", "type", "base_url", "location", "api_key_env", "capabilities",
                          "include_usage_in_stream", "device", "health_timeout_s"},
    "openai_audio": {"id", "type", "base_url", "location", "api_key_env", "capabilities",
                     "health_path", "device"},
}


def _reject_secrets(node: Any, path: str = "") -> None:
    if isinstance(node, Mapping):
        for key, value in node.items():
            if str(key).lower() in SECRET_KEYS:
                raise ValueError(f"inline credential at {path}{key}; reference an environment variable instead")
            _reject_secrets(value, f"{path}{key}.")
    elif isinstance(node, list):
        for i, value in enumerate(node):
            _reject_secrets(value, f"{path}{i}.")


def make_telemetry(spec: Mapping[str, Any] | None) -> DeviceTelemetry | None:
    if not spec:
        return None
    provider = spec.get("provider")
    if provider == "nvidia-smi":
        return NvidiaSmiTelemetry()
    raise ValueError(f"unsupported telemetry provider {provider!r}")


def resolve_device(ref: Any, telemetry: DeviceTelemetry | None) -> str | None:
    """Resolve a device reference: {"index": N} or {"id": "<uuid>"} -> stable device id."""
    if ref is None:
        return None
    if telemetry is None:
        raise ValueError("a device reference requires a telemetry provider")
    if isinstance(ref, Mapping) and "id" in ref:
        return str(ref["id"])
    if isinstance(ref, Mapping) and "index" in ref:
        for snap in telemetry.devices():
            if snap.index == str(ref["index"]):
                return snap.device_id
        raise ValueError(f"no device with index {ref['index']}")
    raise ValueError(f"invalid device reference {ref!r}")


def build_adapter(spec: Mapping[str, Any], telemetry: DeviceTelemetry | None) -> EngineAdapter:
    kind = spec.get("type")
    if kind not in ADAPTER_KEYS:
        raise ValueError(f"unknown adapter type {kind!r}")
    unknown = set(spec) - ADAPTER_KEYS[kind]
    if unknown:
        raise ValueError(f"adapter {spec.get('id')!r}: unknown keys {sorted(unknown)}")
    if not spec.get("id"):
        raise ValueError("every adapter needs an id")
    device_id = resolve_device(spec.get("device"), telemetry)
    caps = tuple(spec["capabilities"]) if "capabilities" in spec else None
    common = {"capabilities": caps} if caps else {}
    if kind == "ollama":
        return OllamaAdapter(spec["id"], spec["base_url"], device_id=device_id,
                             lifecycle_timeout_s=spec.get("lifecycle_timeout_s", 300.0), **common)
    if kind == "openai_compatible":
        return OpenAICompatibleAdapter(spec["id"], spec["base_url"], location=spec.get("location", LOCATION_REMOTE),
                                       api_key_env=spec.get("api_key_env"),
                                       include_usage_in_stream=spec.get("include_usage_in_stream", False),
                                       device_id=device_id, health_timeout_s=spec.get("health_timeout_s", 10.0),
                                       **common)
    if kind == "openai_audio":
        return OpenAIAudioAdapter(spec["id"], spec["base_url"], location=spec.get("location", LOCATION_LOCAL),
                                  api_key_env=spec.get("api_key_env"), health_path=spec.get("health_path", "/models"),
                                  device_id=device_id, **common)
    device_env: dict[str, str] = {}
    if spec.get("server_path") and device_id and isinstance(telemetry, NvidiaSmiTelemetry):
        # NVIDIA-specific pinning; CUDA's default order can differ from nvidia-smi's.
        device_env = {"CUDA_DEVICE_ORDER": "PCI_BUS_ID", "CUDA_VISIBLE_DEVICES": device_id}
    return LlamaServerAdapter(
        spec["id"], base_url=spec.get("base_url"), server_path=spec.get("server_path"),
        models=spec.get("models"), host=spec.get("host", "127.0.0.1"), port=spec.get("port"),
        gpu_layers=spec.get("gpu_layers"), ctx_size=spec.get("ctx_size"),
        extra_args=spec.get("extra_args", ()), device_id=device_id, telemetry=telemetry,
        device_env=device_env, log_dir=spec.get("log_dir"),
        startup_timeout_s=spec.get("startup_timeout_s", 180.0), **common)


def load_config(path: str | Path) -> dict[str, Any]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    _reject_secrets(data)
    return data


def build_fabric(config: Mapping[str, Any], *, subsystems: Any = None) -> Fabric:
    telemetry = make_telemetry(config.get("telemetry"))
    adapters = {}
    for spec in config.get("adapters", []):
        adapter = build_adapter(spec, telemetry)
        if spec["id"] in adapters:
            raise ValueError(f"duplicate adapter id {spec['id']!r}")
        adapters[spec["id"]] = adapter
    bindings = [Binding(capability=b["capability"], adapter_id=b["adapter"], model=b["model"],
                        resident_mib=b.get("resident_mib"), transient_mib=b.get("transient_mib"),
                        keep_alive_s=b.get("keep_alive_s"),
                        approved_work_types=tuple(b["approved_work_types"]))
                for b in config.get("bindings", [])]
    options = config.get("fabric", {})
    device_concurrency = {}
    for ref, limit in (options.get("device_concurrency") or {}).items():
        device_concurrency[resolve_device({"index": ref} if str(ref).isdigit() else {"id": ref}, telemetry)] = int(limit)
    return Fabric(adapters, bindings, telemetry=telemetry, subsystems=subsystems,
                  device_concurrency=device_concurrency,
                  safety_margin_mib=int(options.get("safety_margin_mib", 256)),
                  allow_eviction=bool(options.get("allow_eviction", False)),
                  test_mode=bool(options.get("test_mode", False)))
