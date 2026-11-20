"""Live validation of HARO's adapters and fabric against real engines.

Run on the machine that hosts the engines. Every figure in the report is
labelled with its source; nothing is estimated or filled in. Steps whose
engine is unreachable are reported as not run, with the reason.

What it touches:
  * Engine-managed models (Ollama): loads and unloads ONLY the model named by
    --ollama-model. If that model is already resident, someone else may be using
    it, so the lifecycle step is NOT run unless --allow-disturb-resident is given
    (then it is unloaded, exercised and restored).
  * HARO-owned llama-server: started on --llama-port only, stopped afterwards.
    A separately managed llama-server is only called, never started or stopped.
  * Speech service and remote provider: called only. The remote provider is
    contacted only with --allow-remote.

Example (PowerShell, from the HARO repository root):
  python -m engine_adapters.live_validation --config deployment\\test-machine.json `
      --voice <voice-id> --ollama-model <installed-model> --allow-remote
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .config import build_adapter, load_config, make_telemetry
from .contract import (
    CAP_CHAT, CAP_SPEECH_RECOGNITION, CAP_SPEECH_SYNTHESIS, STATUS_CANCELLED, STATUS_OK, CancelToken,
    InferenceRequest, RESIDENCY_RESIDENT,
)
from .device_telemetry import DeviceSampler, DeviceTelemetry
from .contract import Binding
from .fabric import PRIORITY_INTERACTIVE, CapabilityRequest, Fabric
from .llama_cpp import LlamaServerAdapter
from .ollama import OllamaAdapter
from .openai_audio import OpenAIAudioAdapter
from .openai_compatible import OpenAICompatibleAdapter

TEST_SENTENCE = "This is a test of the speech adapter."
CHAT_PROMPT = "In one sentence, why does a voice assistant need reliable resource accounting?"
LONG_PROMPT = "Write a detailed, numbered list of 60 distinct facts about computer memory."


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def git_commit(root: Path) -> str | None:
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True,
                              timeout=5, check=True).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None


class Recorder:
    def __init__(self, output: Path):
        self.output = output
        self.report: dict[str, Any] = {"schema": "haro-live-validation/1", "steps": []}

    def step(self, name: str, fn: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        started = time.monotonic()
        entry: dict[str, Any] = {"step": name, "source": "live"}
        try:
            entry.update(fn())
            entry.setdefault("status", "passed")
        except _Skip as exc:
            entry.update(status="not_run", reason=str(exc))
        except Exception as exc:  # report and continue: one engine must not hide the others
            entry.update(status="failed", error=f"{type(exc).__name__}: {exc}")
        entry["wall_s"] = round(time.monotonic() - started, 4)
        self.report["steps"].append(entry)
        self.save()
        print(f"[{entry['status']:>8}] {name}" + (f" - {entry.get('reason') or entry.get('error')}"
                                                   if entry["status"] != "passed" else ""), flush=True)
        return entry

    def save(self) -> None:
        self.output.mkdir(parents=True, exist_ok=True)
        (self.output / "report.json").write_text(json.dumps(self.report, indent=2, default=str), encoding="utf-8")


class _Skip(Exception):
    pass


def _sampled(telemetry: DeviceTelemetry | None, device_id: str | None, fn: Callable[[], Any]):
    if telemetry is None or device_id is None:
        return fn(), None
    with DeviceSampler(telemetry, device_id, 0.1) as sampler:
        value = fn()
    return value, sampler.series.summary()


def _delta(summary: dict | None) -> dict[str, Any] | None:
    if not summary or summary["first_used_mib"] is None:
        return None
    return {"peak_minus_before_mib": summary["max_used_mib"] - summary["first_used_mib"],
            "after_minus_before_mib": summary["last_used_mib"] - summary["first_used_mib"],
            "measurement": "device_delta", "summary": summary}


def _text_brief(text: str | None) -> dict[str, Any]:
    text = text or ""
    return {"chars": len(text), "preview": text[:200]}


def _word_overlap(expected: str, actual: str) -> float:
    norm = lambda s: {w.strip(".,!?").lower() for w in s.split() if w.strip(".,!?")}
    want = norm(expected)
    return round(len(want & norm(actual)) / len(want), 3) if want else 0.0


def cancel_probe(adapter, request: InferenceRequest, after_s: float) -> dict[str, Any]:
    """Start a long request, cancel it, and measure how fast control returns."""
    cancel = CancelToken()
    timer = threading.Timer(after_s, cancel.cancel, args=("live cancellation probe",))
    started = time.monotonic()
    timer.start()
    result = adapter.execute(request, cancel)
    timer.cancel()
    returned = time.monotonic()
    cancelled_at = cancel.cancelled_at
    return {"result_status": result.status,
            "cancel_issued_after_s": round(cancelled_at - started, 4) if cancelled_at else None,
            "control_returned_after_cancel_s": round(returned - cancelled_at, 4) if cancelled_at else None,
            "completed_before_cancel": cancelled_at is None,
            "engine_stopped_work": "unverified (client disconnect observed only)"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", required=True)
    parser.add_argument("--output", help="results folder (default: HARO-Test-Lab/results/<device>/adapters-<stamp>)")
    parser.add_argument("--device-index", default="0", help="accelerator index for device measurements")
    parser.add_argument("--voice", help="voice identifier for speech synthesis (required for the speech steps)")
    parser.add_argument("--ollama-model", help="installed model to load, run, cancel and unload")
    parser.add_argument("--allow-remote", action="store_true", help="permit calls to remote providers")
    parser.add_argument("--allow-disturb-resident", action="store_true",
                        help="permit the lifecycle step to unload a model that is already resident")
    parser.add_argument("--llama-server", help="llama-server executable for the HARO-owned process test")
    parser.add_argument("--llama-model", help="GGUF file for the HARO-owned process test")
    parser.add_argument("--llama-port", type=int, default=17667)
    parser.add_argument("--llama-gpu-layers", type=int, default=999)
    parser.add_argument("--llama-ctx", type=int, default=4096)
    parser.add_argument("--max-tokens", type=int, default=64)
    args = parser.parse_args(argv)

    root = Path(__file__).resolve().parents[1]
    config = load_config(args.config)
    try:
        telemetry = make_telemetry(config.get("telemetry"))
        devices = telemetry.devices() if telemetry else []
    except Exception as exc:
        telemetry, devices = None, []
        print(f"device telemetry unavailable: {type(exc).__name__}: {exc}")
    device = next((d for d in devices if d.index == str(args.device_index)), None)
    device_id = device.device_id if device else None
    folder_name = (device.name.replace("NVIDIA GeForce ", "").replace(" ", "-") if device else "no-device-telemetry")
    output = Path(args.output) if args.output else root / "HARO-Test-Lab" / "results" / folder_name / f"adapters-{utc_stamp()}"
    rec = Recorder(output)
    rec.report["environment"] = {
        "started_utc": utc_stamp(), "python": sys.version.split()[0], "platform": platform.platform(),
        "haro_commit": git_commit(root), "config": str(Path(args.config).resolve()),
        "devices": [d.to_dict() for d in devices], "measurement_device_id": device_id,
        "allow_remote": args.allow_remote,
    }
    adapters = {}
    for spec in config.get("adapters", []):
        spec = dict(spec)
        if spec.get("device") is not None and telemetry is None:
            spec.pop("device")
        adapters[spec["id"]] = build_adapter(spec, telemetry)
    rec.report["adapters"] = {k: a.describe().to_dict() for k, a in adapters.items()}

    by_type = lambda cls: [a for a in adapters.values() if isinstance(a, cls)]
    remote_ids = {k for k, a in adapters.items() if a.describe().location == "remote"}

    # 1. Health and inventory for every adapter (remote only when allowed).
    for adapter_id, adapter in adapters.items():
        def inventory(adapter=adapter, adapter_id=adapter_id):
            if adapter_id in remote_ids and not args.allow_remote:
                raise _Skip("remote provider not contacted without --allow-remote")
            health = adapter.health()
            out = {"health": health.to_dict()}
            if not health.healthy:
                out["status"] = "failed"
                return out
            out["models"] = [m.to_dict() for m in adapter.list_models()]
            return out
        rec.step(f"inventory:{adapter_id}", inventory)

    # 2. Speech round trip: synthesize a known sentence, transcribe it back.
    audio_dir = output / "audio"
    speech = by_type(OpenAIAudioAdapter)
    synth_binding = next((b for b in config.get("bindings", []) if b["capability"] == CAP_SPEECH_SYNTHESIS), None)
    stt_binding = next((b for b in config.get("bindings", []) if b["capability"] == CAP_SPEECH_RECOGNITION), None)
    synthesized: dict[str, Any] = {}

    def speech_round_trip():
        if not speech or not synth_binding:
            raise _Skip("no speech adapter or synthesis binding configured")
        if not args.voice:
            raise _Skip("--voice not given; voice identity is never defaulted")
        adapter = adapters[synth_binding["adapter"]]
        runs = []
        for i in range(2):
            path = audio_dir / f"synthesis-{i + 1}.wav"
            req = InferenceRequest(CAP_SPEECH_SYNTHESIS, synth_binding["model"],
                                   {"text": TEST_SENTENCE, "voice": args.voice, "format": "wav", "output_path": str(path)},
                                   work_type="voice_output",
                                   timeout_s=120, stream=False)
            result, dev = _sampled(telemetry, device_id, lambda: adapter.execute(req, CancelToken()))
            runs.append({"run": i + 1, "status": result.status, "error": result.error, "output": result.output,
                         "timings": result.timings, "engine_metrics": result.engine_metrics, "device": _delta(dev)})
        out: dict[str, Any] = {"synthesis": runs}
        ok = [r for r in runs if r["status"] == STATUS_OK]
        if not ok:
            out["status"] = "failed"
            return out
        synthesized["path"] = ok[-1]["output"]["audio_path"]
        if stt_binding:
            stt = adapters[stt_binding["adapter"]]
            req = InferenceRequest(CAP_SPEECH_RECOGNITION, stt_binding["model"], {"audio_path": synthesized["path"]},
                                   work_type="transcription",
                                   timeout_s=120, stream=False)
            result, dev = _sampled(telemetry, device_id, lambda: stt.execute(req, CancelToken()))
            out["transcription"] = {"status": result.status, "error": result.error, "text": result.output.get("text"),
                                    "word_overlap_with_input": _word_overlap(TEST_SENTENCE, result.output.get("text") or ""),
                                    "timings": result.timings, "device": _delta(dev)}
            if result.status != STATUS_OK:
                out["status"] = "failed"
        return out
    rec.step("speech:round_trip", speech_round_trip)

    # 3. Engine-managed residency: load, run, cancel, recover, unload.
    measured: dict[str, Any] = {}

    def ollama_lifecycle():
        engines = by_type(OllamaAdapter)
        if not engines:
            raise _Skip("no engine-managed adapter configured")
        if not args.ollama_model:
            raise _Skip("--ollama-model not given; inventory step lists installed models")
        adapter = engines[0]
        if not adapter.health().healthy:
            raise _Skip("engine-managed adapter is not healthy")
        model = args.ollama_model
        was_resident = any(m.residency == RESIDENCY_RESIDENT and m.model.split(":")[0] == model.split(":")[0]
                           for m in adapter.list_models())
        if was_resident and not args.allow_disturb_resident:
            raise _Skip(f"{model} is already resident and may be in use by another client; "
                        "rerun with --allow-disturb-resident to exercise its lifecycle")
        out: dict[str, Any] = {"model": model, "resident_before_run": was_resident}
        if was_resident:
            unloaded, dev = _sampled(telemetry, device_id, lambda: adapter.unload(model))
            out["pre_unload"] = {**unloaded.to_dict(), "device": _delta(dev)}
        loaded, dev = _sampled(telemetry, device_id, lambda: adapter.load(model, keep_alive_s=-1))
        out["load"] = {**loaded.to_dict(), "device": _delta(dev)}
        if loaded.status != STATUS_OK:
            out["status"] = "failed"
            return out
        measured["ollama_resident_mib"] = loaded.device_memory_mib
        chat = InferenceRequest(CAP_CHAT, model, {"messages": [{"role": "user", "content": CHAT_PROMPT}]}, "conversation",
                                max_output_tokens=args.max_tokens, temperature=0, seed=7, timeout_s=120)
        result, dev = _sampled(telemetry, device_id, lambda: adapter.execute(chat, CancelToken()))
        out["chat"] = {"status": result.status, "error": result.error, "text": _text_brief(result.output.get("text")),
                       "timings": result.timings, "engine_metrics": result.engine_metrics, "device": _delta(dev)}
        if dev and dev["first_used_mib"] is not None:
            measured["ollama_transient_peak_mib"] = max(0, dev["max_used_mib"] - dev["first_used_mib"])
        long = InferenceRequest(CAP_CHAT, model, {"messages": [{"role": "user", "content": LONG_PROMPT}]}, "conversation",
                                max_output_tokens=600, temperature=0, seed=7, timeout_s=120)
        out["cancellation"] = cancel_probe(adapter, long, 1.5)
        recovery = adapter.execute(chat, CancelToken())
        out["recovery_after_cancel"] = {"status": recovery.status, "error": recovery.error,
                                        "text": _text_brief(recovery.output.get("text"))}
        unloaded, dev = _sampled(telemetry, device_id, lambda: adapter.unload(model))
        out["unload"] = {**unloaded.to_dict(), "device": _delta(dev)}
        if was_resident:
            restored = adapter.load(model)
            out["restored_residency"] = restored.status
        if (result.status != STATUS_OK or recovery.status != STATUS_OK or unloaded.status != STATUS_OK
                or out["cancellation"]["result_status"] != STATUS_CANCELLED):
            out["status"] = "failed"
        return out
    rec.step("engine_managed:lifecycle", ollama_lifecycle)

    # 4. Separately managed llama-server: call only.
    for adapter in by_type(LlamaServerAdapter):
        def external_call(adapter=adapter):
            if adapter.owned:
                raise _Skip("owned adapters are exercised in the owned-process step")
            if not adapter.health().healthy:
                raise _Skip(f"{adapter.base_url} not healthy")
            models = adapter.list_models()
            if not models:
                raise _Skip("server lists no model")
            req = InferenceRequest(CAP_CHAT, models[0].model, {"messages": [{"role": "user", "content": CHAT_PROMPT}]}, "conversation",
                                   max_output_tokens=args.max_tokens, temperature=0, seed=7, timeout_s=120)
            result, dev = _sampled(telemetry, device_id, lambda: adapter.execute(req, CancelToken()))
            return {"status": "passed" if result.status == STATUS_OK else "failed", "model": models[0].model,
                    "result_status": result.status, "error": result.error,
                    "text": _text_brief(result.output.get("text")), "timings": result.timings,
                    "engine_metrics": result.engine_metrics, "device": _delta(dev)}
        rec.step(f"external_server:{adapter.adapter_id}", external_call)

    # 5. HARO-owned llama-server process.
    def owned_process():
        if not (args.llama_server and args.llama_model):
            raise _Skip("--llama-server and --llama-model not given")
        adapter = LlamaServerAdapter("haro-owned-llama-server", server_path=args.llama_server,
                                     models={"validation-model": args.llama_model}, port=args.llama_port,
                                     gpu_layers=args.llama_gpu_layers, ctx_size=args.llama_ctx,
                                     device_id=device_id, telemetry=telemetry,
                                     device_env=({"CUDA_DEVICE_ORDER": "PCI_BUS_ID", "CUDA_VISIBLE_DEVICES": device_id}
                                                 if device_id and telemetry and telemetry.vendor == "nvidia" else {}),
                                     log_dir=str(output / "logs"))
        out: dict[str, Any] = {}
        try:
            loaded = adapter.load("validation-model")
            out["load"] = loaded.to_dict()
            if loaded.status != STATUS_OK:
                out["status"] = "failed"
                return out
            req = InferenceRequest(CAP_CHAT, "validation-model", {"messages": [{"role": "user", "content": CHAT_PROMPT}]}, "conversation",
                                   max_output_tokens=args.max_tokens, temperature=0, seed=7, timeout_s=120)
            result, dev = _sampled(telemetry, device_id, lambda: adapter.execute(req, CancelToken()))
            out["chat"] = {"status": result.status, "error": result.error, "text": _text_brief(result.output.get("text")),
                           "timings": result.timings, "engine_metrics": result.engine_metrics, "device": _delta(dev)}
            long = InferenceRequest(CAP_CHAT, "validation-model", {"messages": [{"role": "user", "content": LONG_PROMPT}]}, "conversation",
                                    max_output_tokens=600, temperature=0, seed=7, timeout_s=120)
            out["cancellation"] = cancel_probe(adapter, long, 1.5)
            out["health_after_cancel"] = adapter.health().to_dict()
        finally:
            unloaded = adapter.unload("validation-model")
            out["unload"] = unloaded.to_dict()
            pid = (out.get("load") or {}).get("details", {}).get("pid")
            out["process_gone_after_unload"] = _pid_gone(pid) if pid else None
        if out.get("chat", {}).get("status") != STATUS_OK or not out.get("process_gone_after_unload"):
            out["status"] = "failed"
        return out
    rec.step("owned_process:lifecycle", owned_process)

    # 6. Remote provider: one explicitly permitted call.
    for adapter_id in sorted(remote_ids):
        def remote_call(adapter_id=adapter_id):
            if not args.allow_remote:
                raise _Skip("--allow-remote not given")
            binding = next((b for b in config.get("bindings", []) if b["adapter"] == adapter_id
                            and b["capability"] == CAP_CHAT), None)
            if binding is None:
                raise _Skip("no chat binding for this remote provider")
            req = InferenceRequest(CAP_CHAT, binding["model"], {"messages": [{"role": "user", "content": CHAT_PROMPT}]}, "conversation",
                                   max_output_tokens=max(args.max_tokens, 256), temperature=0, timeout_s=120)
            result = adapters[adapter_id].execute(req, CancelToken())
            return {"status": "passed" if result.status == STATUS_OK else "failed", "model": binding["model"],
                    "result_status": result.status, "error": result.error,
                    "text": _text_brief(result.output.get("text")),
                    "reasoning_chars": len(result.output.get("reasoning") or ""),
                    "timings": result.timings, "engine_metrics": result.engine_metrics,
                    "local_device_memory": "not_applicable (remote execution)"}
        rec.step(f"remote:{adapter_id}", remote_call)

    # 7. One full voice turn through the fabric, with A.I.M.S. and Vault Logic, then restart.
    def fabric_voice_turn():
        if not synthesized.get("path") or not stt_binding:
            raise _Skip("speech round trip did not produce audio")
        chat_binding = None
        if args.ollama_model and measured.get("ollama_resident_mib") is not None and device_id:
            transient = measured.get("ollama_transient_peak_mib")
            if transient is None:
                raise _Skip("no measured per-request figure for the local chat model")
            engine = by_type(OllamaAdapter)[0]
            chat_binding = Binding(CAP_CHAT, engine.adapter_id, args.ollama_model,
                                   resident_mib=measured["ollama_resident_mib"], transient_mib=transient,
                                   approved_work_types=("conversation",))
        elif args.allow_remote:
            spec = next((b for b in config.get("bindings", []) if b["capability"] == CAP_CHAT
                         and b["adapter"] in remote_ids), None)
            if spec:
                chat_binding = Binding(CAP_CHAT, spec["adapter"], spec["model"],
                                       approved_work_types=("conversation",))
        if chat_binding is None:
            raise _Skip("no measured local chat binding and remote not allowed")
        sys.path.insert(0, str(root))
        from haro_integration import HaroSubsystems
        memory_root = output / "haro-aims"
        subsystems = HaroSubsystems(memory_root)
        bindings = [chat_binding, Binding(CAP_SPEECH_RECOGNITION, stt_binding["adapter"], stt_binding["model"],
                                          approved_work_types=("transcription",)),
                    Binding(CAP_SPEECH_SYNTHESIS, synth_binding["adapter"], synth_binding["model"],
                            approved_work_types=("voice_output",))]
        fabric = Fabric(adapters, bindings, telemetry=telemetry, subsystems=subsystems,
                        safety_margin_mib=int(config.get("fabric", {}).get("safety_margin_mib", 256)))
        turn: dict[str, Any] = {"chat_binding": f"{chat_binding.adapter_id}/{chat_binding.model}"}
        try:
            heard = fabric.run(CapabilityRequest(CAP_SPEECH_RECOGNITION, {"audio_path": synthesized["path"]},
                                                 "live-validation", "transcription", priority=PRIORITY_INTERACTIVE, stream=False))
            turn["hear"] = {"status": heard.status, "error": heard.error, "timings": heard.timings}
            text = (heard.result or {}).get("output", {}).get("text") or TEST_SENTENCE
            reply = fabric.run(CapabilityRequest(CAP_CHAT, {"messages": [{"role": "user", "content": text}]},
                                                 "live-validation", "conversation", priority=PRIORITY_INTERACTIVE,
                                                 allow_remote=args.allow_remote, max_output_tokens=args.max_tokens))
            turn["reason"] = {"status": reply.status, "error": reply.error, "timings": reply.timings,
                              "decisions": reply.decisions, "device": reply.device}
            answer = (reply.result or {}).get("output", {}).get("text") or ""
            spoken = fabric.run(CapabilityRequest(CAP_SPEECH_SYNTHESIS, {
                "text": answer[:400] or TEST_SENTENCE, "voice": args.voice, "format": "wav",
                "output_path": str(audio_dir / "fabric-turn-reply.wav")}, "live-validation", "voice_output",
                priority=PRIORITY_INTERACTIVE, stream=False))
            turn["speak"] = {"status": spoken.status, "error": spoken.error, "timings": spoken.timings}
        finally:
            fabric.close()
            before = len(subsystems.memory.long_term.entries)
            subsystems.close()
        reopened = HaroSubsystems(memory_root)
        after = len(reopened.memory.long_term.entries)
        chain_ok, broken = reopened.memory.long_term.verify_chain()
        integrity = {"chain_valid": chain_ok, "broken_sequences": broken}
        reopened.close()
        turn["aims_entries_before_restart"] = before
        turn["aims_entries_after_restart"] = after
        turn["aims_integrity_after_restart"] = integrity
        if (any(turn[k]["status"] != STATUS_OK for k in ("hear", "reason", "speak")) or before != after
                or not chain_ok):
            turn["status"] = "failed"
        return turn
    rec.step("fabric:voice_turn", fabric_voice_turn)

    for adapter in adapters.values():
        adapter.close()
    rec.report["finished_utc"] = utc_stamp()
    rec.report["summary"] = {s["step"]: s["status"] for s in rec.report["steps"]}
    rec.save()
    print(f"\nreport: {output / 'report.json'}")
    return 0 if all(s["status"] != "failed" for s in rec.report["steps"]) else 1


def _pid_gone(pid: int) -> bool:
    if os.name == "nt":
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True)
        return str(pid) not in out.stdout
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return False


if __name__ == "__main__":
    sys.exit(main())
