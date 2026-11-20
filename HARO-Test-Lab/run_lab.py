"""Run unmodified candidate snapshots and a separate real inference baseline."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parent


def hashes(folder):
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in folder.iterdir() if p.is_file()}


def run(command, cwd, target, timeout=180):
    start = datetime.now(timezone.utc)
    try:
        proc = subprocess.run(command, cwd=cwd, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout)
        result = dict(command=command, exit_code=proc.returncode,
                      stdout=proc.stdout, stderr=proc.stderr)
    except subprocess.TimeoutExpired as exc:
        def decode(value):
            return value.decode("utf-8", "replace") if isinstance(value, bytes) else value or ""
        result = dict(command=command, exit_code=None, timed_out=True,
                      stdout=decode(exc.stdout), stderr=decode(exc.stderr))
    result["started_utc"] = start.isoformat()
    result["elapsed_s"] = (datetime.now(timezone.utc) - start).total_seconds()
    target.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(f"{target.parent.parent.name}/{target.parent.name}/{target.stem}: exit={result['exit_code']}", flush=True)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:16520")
    parser.add_argument("--gpu-index", type=int, default=0)
    args = parser.parse_args()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    config = json.loads((ROOT / "test-config/common-workload.json").read_text(encoding="utf-8-sig"))
    telemetry = subprocess.run(["nvidia-smi", "--query-gpu=index,name,memory.total,memory.used", "--format=csv,noheader,nounits"], capture_output=True, text=True)
    rows = [line.split(",") for line in telemetry.stdout.splitlines() if line.strip()]
    gpu = next((r for r in rows if int(r[0]) == args.gpu_index), None)
    if gpu is None:
        raise SystemExit("Selected GPU telemetry unavailable: " + telemetry.stderr)
    name = gpu[1].strip()
    gpu_folder = "RTX-5060-8GB" if "5060" in name else "RTX-3050-6GB" if "3050" in name else name.replace(" ", "-")
    base = ROOT / "results" / gpu_folder
    summary = dict(timestamp=stamp, gpu=name, telemetry=telemetry.stdout,
                   workload=config, candidates={}, inference_comparison="not_available: stock candidates execute host fixtures, not llama inference")
    for label, entry in (("claude", "haro.py"), ("grok", "haro.py")):
        source = ROOT / "candidates" / ("haro-" + label)
        out = base / label / stamp
        out.mkdir(parents=True)
        before = hashes(source)
        tests = run([sys.executable, "-B", "-m", "unittest", "discover", "-v"], source, out / "unit-tests.json")
        cli = {}
        for mode in ("discover", "demo", "benchmark", "adaptive"):
            command = [sys.executable, "-B", str(source / entry), mode,
                       "--memory-mib", "128", "--concurrency", "2", "--audit", str(out / (mode + ".audit.jsonl"))]
            cli[mode] = run(command, source, out / (mode + ".json"))
        after = hashes(source)
        summary["candidates"][label] = dict(source_hashes=before, files_unchanged=before == after,
                                             unit_tests=tests, cli=cli)
    out = base / "direct-baseline" / stamp
    out.mkdir(parents=True)
    command = [sys.executable, "-B", str(ROOT / "inference-engine/inference_probe.py"),
               "--url", args.url, "--gpu-index", str(args.gpu_index), "--output", str(out / "inference.json")]
    for key in ("prompt", "max_tokens", "timeout", "interval"):
        command.extend(["--" + key.replace("_", "-"), str(config[key])])
    # Existing-server mode preserves the running server's model/context/offload settings.
    summary["baseline_mode"] = "existing_server: model/context/offload are controlled by running server; loading time and total model residency not measured"
    summary["baseline"] = run(command, ROOT, out / "process.json", timeout=config["timeout"] + 30)
    if (out / "inference.json").exists():
        summary["baseline_measurement"] = json.loads((out / "inference.json").read_text(encoding="utf-8"))
    report = base / ("report-" + stamp + ".json")
    report.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print("Report: " + str(report), flush=True)


if __name__ == "__main__":
    main()
