"""External service adapter: candidate code stays byte-for-byte unchanged.

Tests host-side scheduling of calls to already-running shared model services.
Does not claim HARO owns their model residency or controls their GPU allocation.
"""
import argparse
import asyncio
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import sys
import time
import urllib.request
import wave

ROOT = Path(__file__).resolve().parent


def http(url, data=None):
    request = urllib.request.Request(url, data=None if data is None else json.dumps(data).encode(),
                                     headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=120) as response:
        return json.load(response)


def service_call(kind, config, out):
    started = time.perf_counter()
    if kind == "llm":
        data = http("http://127.0.0.1:16520/completion", {
            "prompt": config["prompt"], "n_predict": config["max_tokens"],
            "temperature": config["temperature"], "seed": config["seed"],
            "stream": False, "cache_prompt": False})
        if not data.get("content", "").strip():
            raise RuntimeError("Language model returned empty content")
        result = {"text": data["content"], "timings": data.get("timings")}
    else:
        from gradio_client import Client
        client = Client("http://127.0.0.1:8031", verbose=False)
        audio, status = client.predict(text="Hello. The HARO test lab is ready.",
                                      lang_disp="English", spk_disp="Ryan", instruct="",
                                      api_name="/run_instruct")
        (out / "tts-response.json").write_text(json.dumps({"audio": audio, "status": status}, indent=2), encoding="utf-8")
        if not audio:
            raise RuntimeError("TTS returned no audio: " + str(status))
        target = out / "qwen-tts.wav"
        shutil.copyfile(audio, target)
        with wave.open(str(target), "rb") as wav:
            frames, rate = wav.getnframes(), wav.getframerate()
            if frames <= 0 or rate <= 0:
                raise RuntimeError("Empty generated audio")
            result = {"audio_file": str(target), "duration_s": frames / rate,
                      "sample_rate": rate, "channels": wav.getnchannels(), "service_status": status}
    return {**result, "request_s": time.perf_counter() - started,
            "execution": "external_shared_service", "attributed_device_vram_mib": None}


async def trial(args):
    out = Path(args.output).resolve()
    out.mkdir(parents=True, exist_ok=True)
    config = json.loads((ROOT / "test-config/common-workload.json").read_text(encoding="utf-8-sig"))
    result = {"candidate": args.candidate, "adapter": "external host dispatch; no candidate source edits",
              "scope": "existing services; no HARO GPU residency/admission validation", "results": {}}
    source = ROOT / "candidates" / ("haro-" + args.candidate) / "haro.py"
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    module_spec = importlib.util.spec_from_file_location("haro_trial", source)
    module = importlib.util.module_from_spec(module_spec)
    sys.modules[module_spec.name] = module
    module_spec.loader.exec_module(module)

    class ServiceBackend(module.CPUBackend):
        execution = "external_shared_service"

        async def execute(self, job):
            return await asyncio.to_thread(service_call, job.payload["service"], config, out)

    scheduler = module.Scheduler([ServiceBackend(128, 1)], str(out / "audit.jsonl"))
    for kind in (["llm", "tts"] if args.tts else ["llm"]):
        job = module.Job(kind, payload={"service": kind}, authorized=True,
                         memory_mib=64, timeout_s=180, target_s=180)
        began = time.perf_counter()
        try:
            result["results"].update(await scheduler.run([job]))
        except Exception as exc:
            result["results"][kind] = {"status": "error", "error": repr(exc)}
        result["results"][kind]["wall_s"] = time.perf_counter() - began
        print(args.candidate, kind, result["results"][kind]["status"], flush=True)
    result["source_sha256"] = before
    result["source_unchanged"] = before == hashlib.sha256(source.read_bytes()).hexdigest()
    (out / "service-trial.json").write_text(json.dumps(result, indent=2), encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", choices=["claude", "grok"], required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--tts", action="store_true")
    asyncio.run(trial(parser.parse_args()))
