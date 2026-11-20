"""Matched sequential streaming trials through direct and external HARO adapters."""
import asyncio
import hashlib
import importlib.util
import json
from pathlib import Path
import statistics
import sys
import threading
import time
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parent


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


async def main(url):
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    folder = ROOT / "results" / ("comparison-" + stamp)
    folder.mkdir(parents=True)
    config = json.loads((ROOT / "test-config/common-workload.json").read_text(encoding="utf-8-sig"))
    probe = load("comparison_probe", ROOT / "inference-engine/inference_probe.py")
    models = probe.http_json(url + "/v1/models")
    gpu = probe.available_gpus()[0]
    modules = {name: load("compare_" + name, ROOT / "candidates" / ("candidate-" + name) / "haro.py") for name in ("alpha", "beta")}
    sources = {name: hashlib.sha256((ROOT / "candidates" / ("candidate-" + name) / "haro.py").read_bytes()).hexdigest() for name in modules}
    rows = []
    # Rotate order to reduce systematic first/last-run bias. No service reconfiguration.
    for repetition, order in enumerate((("direct", "alpha", "beta"), ("beta", "direct", "alpha"), ("alpha", "beta", "direct")), 1):
        for name in order:
            samples, errors = [], []
            stop = threading.Event()
            worker = threading.Thread(target=probe.sample_vram, args=(stop, samples, errors, config["interval"]), daemon=True)
            before = probe.available_gpus()[0]["used_mib"]
            worker.start()
            started = time.perf_counter()
            try:
                async def completion():
                    return await asyncio.to_thread(probe.request_completion, url, config["prompt"], config["max_tokens"], config["timeout"])
                if name == "direct":
                    output = await completion()
                    status = "ok"
                    scheduled = None
                else:
                    module = modules[name]

                    class Adapter(module.CPUBackend):
                        execution = "external_shared_service"

                        async def execute(self, job):
                            return await completion()

                    scheduler = module.Scheduler([Adapter(128, 1)], str(folder / f"{name}-{repetition}.audit.jsonl"))
                    job = module.Job("inference", authorized=True, memory_mib=64, timeout_s=config["timeout"], target_s=config["timeout"])
                    scheduled = (await scheduler.run([job]))["inference"]
                    status = scheduled["status"]
                    output = scheduled.get("output", {})
                elapsed = time.perf_counter() - started
                if status == "ok" and not output.get("text", "").strip():
                    status = "invalid_empty_output"
                row = dict(candidate=name, repetition=repetition, status=status, wall_s=elapsed,
                           inference=output, scheduler_result=scheduled)
            except Exception as exc:
                row = dict(candidate=name, repetition=repetition, status="error", error=repr(exc))
            finally:
                after = probe.available_gpus()[0]["used_mib"]
                stop.set()
                worker.join(timeout=5)
            row.update(pre_device_used_mib=before, post_device_used_mib=after,
                       sampled_device_vram=probe.aggregate(samples, gpu["uuid"]), telemetry_errors=errors)
            row["device_peak_mib"] = max(before, after, row["sampled_device_vram"]["max_used_mib"] or 0)
            rows.append(row)
            (folder / "runs.json").write_text(json.dumps(rows, indent=2), encoding="utf-8")
            print(name, repetition, row["status"], round(row.get("wall_s", 0), 3), flush=True)
    summary = {}
    for name in ("direct", "alpha", "beta"):
        group = [r for r in rows if r["candidate"] == name and r["status"] == "ok"]
        summary[name] = dict(passed=len(group), total=3,
            median_wall_s=statistics.median(r["wall_s"] for r in group) if group else None,
            median_first_text_s=statistics.median(r["inference"]["time_to_first_text_s"] for r in group) if group else None,
            median_engine_tokens_s=statistics.median(r["inference"]["server_timings"]["predicted_per_second"] for r in group) if group else None,
            max_device_used_mib=max((r["device_peak_mib"] for r in group), default=None))
    result = dict(gpu=gpu, models=models, workload=config, summary=summary, runs=rows,
                  source_hashes=sources, source_unchanged=all(sources[n] == hashlib.sha256((ROOT / "candidates" / ("haro-" + n) / "haro.py").read_bytes()).hexdigest() for n in sources),
                  all_text_equal=len({r["inference"]["text"] for r in rows if r["status"] == "ok"}) == 1,
                  limitations="Single-job host dispatch to existing services; no GPU admission, memory isolation, model loading or efficiency claim. Device-wide VRAM includes other processes; sampling may miss peaks. Voice services/watchdogs were not contacted or reconfigured.")
    (folder / "comparison.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))
    print(folder)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True, help="Explicitly authorized test service endpoint")
    asyncio.run(main(parser.parse_args().url))
