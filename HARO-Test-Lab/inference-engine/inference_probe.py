#!/usr/bin/env python3
"""One real llama.cpp inference backend + measured NVIDIA device VRAM.

Python 3.11+, stdlib only. Runs an existing localhost llama-server or starts an
isolated server that this script owns. No integration with unrelated applications or externally managed runtimes.
"""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

PROMPT = "In two sentences, explain why reliable resource accounting matters for a local AI voice assistant."


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def smi_rows(fields: str, timeout: float = 4.0):
    cmd = ["nvidia-smi", f"--query-gpu={fields}", "--format=csv,noheader,nounits"]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=True)
    return [list(map(str.strip, line.split(","))) for line in p.stdout.splitlines() if line.strip()]


def available_gpus():
    result = []
    for idx, row in enumerate(smi_rows("index,uuid,name,memory.total,memory.used,memory.free")):
        if len(row) != 6:
            raise ValueError("Unexpected nvidia-smi GPU output; cannot measure reliably")
        ix, uuid, name, total, used, free = row
        def as_int(s):
            return int(s) if s.isdecimal() else None
        result.append(dict(index=int(ix), uuid=uuid, name=name,
                           total_mib=as_int(total), used_mib=as_int(used), free_mib=as_int(free)))
    return result


def http_json(url, payload=None, timeout=30):
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.load(response)


def ready(url):
    try:
        data = http_json(url.rstrip("/") + "/health", timeout=2)
        return data.get("status") == "ok"
    except (ValueError, OSError, urllib.error.URLError):
        return False


def sample_vram(stop, samples, errors, interval):
    while not stop.is_set():
        try:
            samples.append(dict(timestamp=utc_now(), monotonic_s=time.monotonic(), gpus=available_gpus()))
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            errors.append(f"{type(exc).__name__}: {exc}")
        stop.wait(interval)


def aggregate(samples, uuid):
    values = [g["used_mib"] for sample in samples for g in sample["gpus"]
              if g["uuid"] == uuid and g["used_mib"] is not None]
    return {"samples": len(values), "min_used_mib": min(values) if values else None,
            "max_used_mib": max(values) if values else None,
            "last_used_mib": values[-1] if values else None}


def request_completion(url, prompt, max_tokens, timeout):
    payload = {"prompt": prompt, "n_predict": max_tokens, "temperature": 0,
               "seed": 7, "stream": True, "cache_prompt": False}
    req = urllib.request.Request(url.rstrip("/") + "/completion",
                                 data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json", "Accept": "text/event-stream"})
    began = time.monotonic()
    first_token = None
    text_parts = []
    final_event = None
    with urllib.request.urlopen(req, timeout=timeout) as response:
        for raw in response:
            line = raw.decode("utf-8", errors="replace").strip()
            if not line.startswith("data:"):
                continue
            content = line[5:].strip()
            if content == "[DONE]":
                break
            try:
                event = json.loads(content)
            except json.JSONDecodeError:
                continue
            if "error" in event:
                raise RuntimeError(f"Inference API error: {event['error']}")
            if event.get("content"):
                if first_token is None:
                    first_token = time.monotonic()
                text_parts.append(event["content"])
            if event.get("stop") or event.get("timings"):
                final_event = event
    ended = time.monotonic()
    if first_token is None:
        raise RuntimeError("No generated text received from streaming inference")
    return {"text": "".join(text_parts), "time_to_first_text_s": round(first_token - began, 4),
            "completion_s": round(ended - began, 4), "server_timings":
            (final_event or {}).get("timings"), "completed_at": utc_now()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--server-path", help="Path to llama-server executable: start isolated process")
    mode.add_argument("--url", help="Existing local llama-server URL, e.g. http://127.0.0.1:<configured-port>")
    parser.add_argument("--model", help="GGUF model path, required with --server-path")
    parser.add_argument("--gpu-index", type=int, default=0, help="NVIDIA GPU index on this machine")
    parser.add_argument("--port", type=int, default=17666, help="Port for owned server (launch mode)")
    parser.add_argument("--gpu-layers", type=int, default=999)
    parser.add_argument("--ctx-size", type=int, default=2048)
    parser.add_argument("--prompt", default=PROMPT)
    parser.add_argument("--max-tokens", type=int, default=96)
    parser.add_argument("--timeout", type=float, default=120)
    parser.add_argument("--interval", type=float, default=0.25, help="GPU sampling period, seconds")
    parser.add_argument("--output", default="inference-result.json")
    args = parser.parse_args()
    if args.server_path and not args.model:
        parser.error("--model is required with --server-path")
    if args.interval < 0.1 or args.max_tokens < 1 or args.timeout < 5:
        parser.error("interval must be >=0.1; max-tokens >=1; timeout >=5")
    if args.url and not (args.url.startswith("http://127.0.0.1:") or args.url.startswith("http://localhost:")):
        parser.error("For safety, existing inference server must be localhost")
    try:
        gpus = available_gpus()
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        raise SystemExit(f"NVIDIA memory telemetry unavailable: {exc}. Cannot claim measured VRAM.")
    selected = next((g for g in gpus if g["index"] == args.gpu_index), None)
    if selected is None:
        raise SystemExit(f"GPU index {args.gpu_index} absent. Detected indices: {[g['index'] for g in gpus]}")
    if selected["used_mib"] is None:
        raise SystemExit("GPU memory.used is unavailable (common on some Windows WDDM configurations). No measured VRAM claim possible.")
    url = args.url or f"http://127.0.0.1:{args.port}"
    samples, errors = [], []
    stop = threading.Event()
    worker = threading.Thread(target=sample_vram, args=(stop, samples, errors, args.interval), daemon=True)
    server = None
    log_file = None
    load_start = time.monotonic()
    result = {"schema_version": "1.0", "timestamp": utc_now(), "machine": os.environ.get("COMPUTERNAME") or os.uname().nodename if hasattr(os, "uname") else os.environ.get("COMPUTERNAME", "unknown"),
              "mode": "owned_server" if args.server_path else "existing_server",
              "gpu": selected, "server_url": url, "model": args.model,
              "note": "nvidia-smi measures total device memory, NOT necessarily memory attributable to this process. Other GPU activity can affect readings."}
    try:
        worker.start()
        time.sleep(min(0.3, args.interval * 1.1))
        result["pre_server_used_mib"] = aggregate(samples, selected["uuid"])["last_used_mib"]
        if args.server_path:
            exe = Path(args.server_path)
            model = Path(args.model)
            if not exe.is_file() or not model.is_file():
                raise FileNotFoundError("Verify --server-path and --model point to existing files")
            if ready(url):
                raise RuntimeError(f"Port {args.port} already hosts a ready server; refusing to launch a second server")
            log_path = Path(args.output).with_suffix(".server.log")
            log_file = log_path.open("w", encoding="utf-8")
            env = os.environ.copy()
            env["CUDA_DEVICE_ORDER"] = "PCI_BUS_ID"
            env["CUDA_VISIBLE_DEVICES"] = selected["uuid"]
            command = [str(exe.resolve()), "-m", str(model.resolve()), "--host", "127.0.0.1", "--port", str(args.port),
                       "-ngl", str(args.gpu_layers), "-c", str(args.ctx_size)]
            server = subprocess.Popen(command, env=env, stdout=log_file, stderr=subprocess.STDOUT)
            while not ready(url):
                if server.poll() is not None:
                    raise RuntimeError(f"llama-server exited with code {server.returncode}; check {log_path}")
                if time.monotonic() - load_start > args.timeout:
                    raise TimeoutError(f"Server did not become healthy in {args.timeout} seconds")
                time.sleep(0.4)
        else:
            if not ready(url):
                raise RuntimeError(f"Local llama-server is not ready at {url}/health")
        result["server_ready_s"] = round(time.monotonic() - load_start, 4)
        result["before_inference_used_mib"] = aggregate(samples, selected["uuid"])["last_used_mib"]
        result["inference"] = request_completion(url, args.prompt, args.max_tokens, args.timeout)
        time.sleep(min(0.35, args.interval * 1.1))
        result["after_inference_used_mib"] = aggregate(samples, selected["uuid"])["last_used_mib"]
        result["status"] = "ok"
    except Exception as exc:
        result["status"] = "error"
        result["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        if server is not None:
            server.terminate()
            try:
                server.wait(timeout=8)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait()
        if log_file is not None:
            log_file.close()
        stop.set()
        worker.join(timeout=5)
        stats = aggregate(samples, selected["uuid"])
        result["vram_device_measurement"] = stats
        base = result.get("pre_server_used_mib")
        if base is not None and stats["max_used_mib"] is not None:
            result["peak_device_delta_from_pre_server_mib"] = max(0, stats["max_used_mib"] - base)
        result["telemetry_errors"] = errors[:10]
        result["telemetry_error_count"] = len(errors)
        result["sample_interval_s"] = args.interval
        result["gpu_memory_measurement_kind"] = "whole_device_nvidia_smi"
        Path(args.output).write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(json.dumps(result, indent=2))
    return 0 if result["status"] == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
