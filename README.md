# HARO Real Inference Experiment — one llama.cpp engine

> **Canonical subsystem source map:** Before working on A.I.M.S. or Vault Logic, read [DOCTRINE_SOURCE_MAP.md](DOCTRINE_SOURCE_MAP.md). The independent reference repositories are **Spruked/AIMS** (private) and **Spruked/Vault-Logic-System-Template** (public), **not Spruked/Orbot**. Both complete HARO subsystem deployments include the verified frozen v1.0 doctrine and checksum locally. **Reuse the system, not the components.**

This experiment runs **one actual llama.cpp GGUF inference engine** and samples **real NVIDIA device VRAM** via `nvidia-smi`. The same script works on either Windows PC and does **not** edit HARO or Orbot. Python 3.11+ standard library only. No model is downloaded or installed.

## Best first test: owned, isolated server

Use an existing `llama-server.exe` and an existing GGUF model. In PowerShell, from this folder:

```powershell
python .\inference_probe.py --server-path "C:\path\to\llama-server.exe" --model "C:\path\to\model.gguf" --gpu-index 0 --output new-pc-result.json
```

Run **the same command with local executable/model paths** on the old PC, using `--output old-pc-result.json`. Keep the **same GGUF model, prompt, max tokens, context size and GPU-layer count** to make results comparable. The model must fit the smaller 6 GB device for a meaningful same-model comparison. This does not change existing Windows services or Orbot processes. The script starts its own server on **localhost:17666**, and terminates **only that process** when done. It refuses to start if it detects a ready server at that address.

For a different port, pass `--port 17667`. For a smaller GPU, test partial offload with `--gpu-layers 12` (for example); this changes the amount of work on the GPU and must be reported, not assumed equivalent to full offload.

## Alternative: connect to existing local llama-server (read-only)

```powershell
python .\inference_probe.py --url http://127.0.0.1:16520 --gpu-index 0 --output existing-server-result.json
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

**Important:** `nvidia-smi` GPU memory totals cover the **whole card**. They do **not** prove which PID or model used memory. Other GPU users (desktop graphics, Orbot, other LLMs) can change the reading. Sampling can miss short-lived peaks. A Windows WDDM driver may report unavailable GPU memory fields; this script fails transparently rather than inventing measurements. `--gpu-index` chooses the telemetry device, while owned-server GPU selection uses its UUID via `CUDA_VISIBLE_DEVICES`.

## Preflight

1. Verify `nvidia-smi` runs in PowerShell and lists the NVIDIA GPU.
2. Confirm a working `llama-server` binary and locally present GGUF model.
3. Keep the GPU as idle as practical for the initial measurement; **do not stop services unless you decide to**.
4. Use two distinct results filenames; share both JSON outputs for comparison.

This is a **real inference baseline**, not yet HARO orchestration. After it works, a bounded adapter can wrap this same server endpoint and compare direct vs scheduled access. No claim is made that either PC was tested here; actual GPU testing happens on those machines.
