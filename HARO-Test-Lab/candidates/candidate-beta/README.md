# Hardware-Adaptive Runtime Orchestrator (HARO)
## Correctness & Resource Accounting Pass

Python 3.11+; **standard library only**.

### Invariants

- Backend names are unique (`gpuN` vs `mock-gpuN`).
- Discovery is injectable; automated tests never depend on live host GPUs.
- NVIDIA telemetry is never called synchronously from the scheduling loop (rate-limited + `asyncio.to_thread`).
- VRAM accounting: `effective_capacity = max(0, live_free − outstanding_reservations)`. Live free is refreshed asynchronously; on failure the previous capacity is retained (never invented).
- Host RSS (`peak_rss_mib`), device VRAM (`peak_vram_mib`), and simulated capacity are distinct fields.
- Execution capability is explicit metadata (`cpu` | `gpu_sim` | `gpu_real`), never inferred from backend name.
- Simulated GPU work consumes an accounted shared CPU execution budget.
- Simulated execution is never reported as genuine GPU acceleration (`verified_execution=False`).
- CUDA device mapping prefers UUID and sets `CUDA_DEVICE_ORDER=PCI_BUS_ID`.

### Quick start

```bash
chmod +x haro.py
python -m unittest -v
python haro.py discover
python haro.py demo --memory-mib 128 --mock-gpu-mib 8192
python haro.py adaptive --mock-gpu-mib 10240
```

### Capacity claims (honest)

Accounting accepts capacity *values* up to the safety bound (~20 TiB). This is an integer-range check, not a validation of physical multi-terabyte hardware, topology, NUMA, or NVLink.

### Backend notes

| Backend            | capability | verified_execution | Work performed      |
|--------------------|------------|--------------------|---------------------|
| CPUBackend         | cpu        | True               | Host worker         |
| GPUBackend         | gpu_sim    | False              | Host worker + VRAM accounting |
| MockGPUBackend     | gpu_sim    | False              | Host worker + fixed logical capacity |

A future real inference adapter must subclass `GPUBackend`, override `execute()`, measure real VRAM, and set `capability="gpu_real"` and `verified_execution=True`.

No real inference engines, models, or Orbot changes are included in this pass.
