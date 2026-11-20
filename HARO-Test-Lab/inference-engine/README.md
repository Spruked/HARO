# HARO Real Inference Experiment — one llama.cpp engine

This experiment runs **one actual llama.cpp GGUF inference engine** and samples **real NVIDIA device VRAM** via `nvidia-smi`. The probe can run on separately configured test hosts without modifying unrelated applications or services. Python 3.11+ standard library only. No model is downloaded or installed.

## Best first test: owned, isolated server

Use an existing `llama-server.exe` and an existing GGUF model. In PowerShell, from this folder:

```powershell
python .\inference_probe.py --server-path "C:\path\to\llama-server.exe" --model "C:\path\to\model.gguf" --gpu-index 0 --output host-a-result.json
```

Repeat on a second test host using local paths and `--output host-b-result.json`. Match model bytes, prompt, token settings, context and GPU offload; ensure the model fits the smallest tested device. Do not stop or change externally managed processes. The script starts its own server on **localhost:17666**, and terminates **only that process** when done. It refuses to start if it detects a ready server at that address.

For a different port, pass `--port 17667`. For a smaller GPU, test partial offload with `--gpu-layers 12` (for example); this changes the amount of work on the GPU and must be reported, not assumed equivalent to full offload.

## Alternative: connect to existing local llama-server (read-only)

```powershell
python .\inference_probe.py --url http://127.0.0.1:<configured-port> --gpu-index 0 --output existing-server-result.json
```

This does not launch, stop or reconfigure the existing server. It **does send one inference request** to it. Use this mode only if that is acceptable for the service. Existing-server mode cannot measure model loading, and its VRAM delta is **not** the model's total residency.

## Measurements

The JSON report contains:

- NVIDIA GPU identity and total/free/used VRAM at discovery
- device-wide pre-server memory, device-wide peak memory, end-of-request memory
- `peak_device_delta_from_pre_server_mib` (an *estimate* of the memory increment if no other GPU processes change)
- server startup/readiness time in owned-server mode
- first generated text latency, full request latency, generated response, and engine-reported timings when present
- telemetry errors and sample count

**Important:** `nvidia-smi` GPU memory totals cover the **whole card**. They do **not** prove which PID or model used memory. Other GPU users (desktop graphics, other inference processes, and unrelated applications) can change the reading. Sampling can miss short-lived peaks. A Windows WDDM driver may report unavailable GPU memory fields; this script fails transparently rather than inventing measurements. `--gpu-index` chooses the telemetry device, while owned-server GPU selection uses its UUID via `CUDA_VISIBLE_DEVICES`.

## Preflight

1. Verify `nvidia-smi` runs in PowerShell and lists the NVIDIA GPU.
2. Confirm a working `llama-server` binary and locally present GGUF model.
3. Keep the GPU as idle as practical for the initial measurement; **do not stop services unless you decide to**.
4. Use two distinct results filenames; share both JSON outputs for comparison.

This is a **real inference baseline**, not yet HARO orchestration. After it works, a bounded adapter can wrap this same server endpoint and compare direct vs scheduled access. No claim is made that either PC was tested here; actual GPU testing happens on those machines.
