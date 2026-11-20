# Hardware-Adaptive Runtime Orchestrator (HARO)

**Status:** Experimental multi-backend scheduler; resource abstraction being hardened. Not production-ready.  
Python 3.11+; **standard library only**. No installation, network, model download, or real GPU kernels required.

Runs on pure-CPU machines and on machines with NVIDIA GPUs. **No GPU inference happens in this build:** every backend runs the same fixed host worker; GPU backends provide VRAM *admission accounting* only, and audit records say so (`execution: "host_fixture"`, `hardware_inference: false`).

Capacity values up to `MAX_REASONABLE_MIB` (20 TiB expressed in MiB) are accepted by the integer accounting. That is not hardware validation: no system with that much physical GPU memory has been tested. Real-GPU behaviour is exercised with injected telemetry (fake `nvidia-smi` output), not on physical devices.

## Quick start

```bash
chmod +x haro.py          # already executable
python -m unittest -v
python haro.py discover
python haro.py demo --memory-mib 128
python haro.py demo --memory-mib 128 --mock-gpu-mib 8192
python haro.py adaptive --mock-gpu-mib 10240
python haro.py adaptive --mock-gpu-mib 10000000   # ~9.5 TiB of SIMULATED logical capacity
python haro.py benchmark --memory-mib 128 --concurrency 2
```

JSONL audit records go to `audit.jsonl` by default (`--audit` to change).

## What is implemented

- **Backend construction** from a discovery report. Discovery and telemetry are injectable (`discover(runner=...)`, `build_backends(report=...)`, `GPUBackend(telemetry=...)`), so tests never depend on the host.
- **CPUBackend** – logical host-RAM budget + concurrency limit.
- **GPUBackend** – one per discovered NVIDIA GPU. `capacity_mib` = logical budget (free VRAM at discovery, fixed). `observed_free_mib` = physical availability, refreshed off the event loop. Runs the host fixture; no GPU inference.
- **MockGPUBackend** – simulated logical capacity (`--mock-gpu-mib`); no device, no telemetry. Named after any real GPUs (`gpu1` if `gpu0` is real), so names never collide.
- **Capability metadata** on every backend: `device_class`, `execution`, `simulated`, `reserved_resource`. `"gpu"`/`"cpu"` in `allowed_backends` match `device_class`, not the name prefix.
- Priority-first, non-preemptive backfilling scheduler with stable FIFO ties.
- **GPU-class preference**: jobs allowing `"gpu"` go to a `device_class == "gpu"` backend with headroom; CPU fallback otherwise. A simulated GPU is still preferred; the audit marks it `simulated: true`.
- **Shared host execution budget**: every `host_fixture` backend (CPU, accounting-only GPU, mock) draws from one pool of host slots (`host_concurrency`, default = CPU backend concurrency). A mock GPU no longer adds hidden CPU parallelism.
- **Separated metrics**: `host_peak_rss_mib` (host RAM) and `device_vram_peak_mib` (from a real adapter; `null` here). Profiles raise estimates only from the peak of the resource the backend reserves, so host RSS never drives VRAM estimates. `estimate_exceeded` is `null` when the reserved resource was not measured.
- Per-backend concurrency, execution/queue timings, end-to-end target checks. Caller estimates are never reduced.
- **Nonblocking telemetry**: refresh runs in worker threads with a timeout (`refresh_timeout_s`) at most once per `refresh_interval_s`; failures keep the last observation and are audited (`telemetry_unavailable` / `telemetry_timeout` / `telemetry_error`).
- **CUDA identity**: child gets `CUDA_DEVICE_ORDER=PCI_BUS_ID` and `CUDA_VISIBLE_DEVICES=<GPU UUID>` (index only if no UUID). Mocks get `CUDA_VISIBLE_DEVICES=""` so they cannot touch real devices.
- Authorization flag (trusted seam) and explicit allowed-backends list (supports `"gpu"` wildcard matching).
- Fixed worker protocol, failure release, timeout cleanup, batch cancellation cleanup.
- Safety bound (`MAX_REASONABLE_MIB`) rejects pathological requests while still allowing 10 000+ GB configurations.
- CLI commands: `discover`, `demo`, `benchmark`, `adaptive`.

The scheduler only executes submitted jobs. It does not generate tasks or alter permissions.  
The authorization flag is a trusted integration seam, **not** authentication.

## Important limits (unchanged philosophy)

- Admission accounting, **not** hard memory isolation. A worker may exceed its estimate.
- `--memory-mib` / VRAM capacities are logical budgets.
- Discovery is a snapshot plus bounded GPU refreshes. Not a continuous monitor of every cgroup ancestor or non-NVIDIA accelerator.
- Host RAM used by fixtures running on GPU/mock backends is not charged to the CPU RAM budget (host *slots* are).
- `attributed_usage()` is a contract hook only; no backend in this build attributes per-job VRAM, so the ledger always takes the conservative (may under-admit) path.
- GPU path currently runs the **same fixed host worker** while accounting against VRAM. Real CUDA/ROCm/etc. inference engines plug in by subclassing `GPUBackend` (or implementing the `Backend` protocol) and replacing `execute()`.
- No model loading, VRAM peak instrumentation, or device-to-host transfer measurements yet.
- One `Scheduler` instance supports one `run()` at a time.
- Finite-batch API is deliberate.

## Backend extension contract

Implement `name`, `capacity_mib`, `max_concurrency`, and `async execute(job) -> dict`.  
Declare capability metadata: `device_class`, `execution`, `simulated`, `reserved_resource` (missing `device_class` defaults to `"other"`, which `"gpu"`/`"cpu"` never match). Set `execution = "gpu_inference"` only for an adapter that really runs on the device.  
Optionally implement `refresh_capacity()` (sync; run off-loop by the scheduler, must not change `capacity_mib` for real devices) and `attributed_usage() -> {job_id: mib}`. Real adapters should return `device_vram_peak_mib`.  
Register with `Scheduler([adapter, ...])`. Names must be unique.  
`execute` must enforce its own timeout and release all resources on cancellation.  
For real GPU work: discover a compatible engine first; account for weights, KV cache, scratch, allocator overhead, and already-resident models. Do **not** reuse the host RSS field for VRAM.

## VRAM accounting policy (conservative)

```text
logical_headroom  = capacity_mib      - Σ reservations
physical_headroom = observed_free_mib - Σ max(0, reservation - attributed_allocation)
admit if need <= min(logical_headroom, physical_headroom)   # physical term only when telemetry exists
```

Reservations with no attributed allocation are assumed not yet allocated. The policy can under-admit; it never over-admits relative to the last telemetry. Memory taken by unrelated processes shows up through `observed_free_mib`. A job that fits logically but not physically is rejected with `insufficient_physical_availability` once nothing is running. While jobs are pending, the scheduler wakes every `refresh_interval_s` so new telemetry can admit them.

## Adaptive behaviour

```text
discover → build_backends() → Scheduler
                │
                ├─ CPU always (budget from --memory-mib or half of available RAM)
                ├─ one GPUBackend per live NVIDIA GPU (logical = free VRAM at discovery)
                └─ optional MockGPUBackend (simulated; next free gpuN name)
```

Jobs default to `allowed_backends=("gpu", "cpu")`.  
Scheduler scoring: device_class preference (gpu → other → cpu) → lowest profiled mean latency → name order.

## Testing extremes

```bash
# 4 GiB mock
python haro.py adaptive --mock-gpu-mib 4096

# ~9.8 TiB simulated logical capacity (accounting only)
python haro.py adaptive --mock-gpu-mib 10240000
```

40 unit tests pass on a host with no GPU and on a host with a fake `nvidia-smi` on PATH. They cover no GPU, one GPU, multiple GPUs, mixed mock/real, unavailable and slow telemetry, active reservations during refresh, the ledger reconciliation, shared host slots, capability matching, CUDA identity, cancellation, and resource release. "Real GPU" cases use injected telemetry, not physical hardware.

## Next controlled experiments (same roadmap)

1. Attach one real inference backend with a fixed model + deterministic payloads.
2. Functional equivalence (direct vs orchestrated) + CPU fallback.
3. Instrument load/unload, first-token / first-audio, host RAM + device VRAM peaks.
4. Cold / warm / bounded-concurrent strategy comparisons.
5. Live admission refresh + model-specific profiles + resident lifecycle.
6. Integrate only after separate authorization & production review.

## Design references

- Python asyncio subprocess: https://docs.python.org/3/library/asyncio-subprocess.html
- NVIDIA telemetry: https://enterprise-support.nvidia.com/s/article/Useful-nvidia-smi-Queries-2
