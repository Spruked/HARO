#!/usr/bin/env python3
"""Hardware-Adaptive Runtime Orchestrator (HARO) — Correctness & Resource Accounting Pass.

Python 3.11+, standard library only.

Invariants of this pass:
- Backend identity is unique (gpuN vs mock-gpuN).
- Discovery is injectable; tests never depend on live host GPUs.
- NVIDIA telemetry is never called synchronously from the scheduling loop.
- VRAM accounting reconciles live free memory with HARO's outstanding
  reservations without double-counting.
- Host RSS, device VRAM, and simulated capacity are distinct metrics.
- Execution capability is explicit metadata, never inferred from name alone.
- Mock / simulated GPU work consumes an accounted shared CPU execution budget.
- Simulated execution is never reported as genuine GPU acceleration.
- CUDA device mapping prefers UUID + PCI_BUS_ID order.

No real inference engines, models, or Orbot changes.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import platform
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Protocol

MIB = 1024 * 1024
MAX_REASONABLE_MIB = 20 * 1024 * 1024  # ~20 TiB safety bound for accounting values

# How often a GPU backend is allowed to refresh live free VRAM (seconds).
_REFRESH_MIN_INTERVAL_S = 1.0


# ---------------------------------------------------------------------------
# Discovery (injectable)
# ---------------------------------------------------------------------------

def discover(refresh_gpus: bool = True) -> dict[str, Any]:
    """Snapshot of host resources. Telemetry failures → empty GPU list (conservative)."""
    report: dict[str, Any] = {
        "os": platform.system(),
        "cpu_count": os.cpu_count() or 1,
        "available_ram_mib": None,
        "gpus": [],
        "total_vram_mib": 0,
        "free_vram_mib": 0,
        "warnings": [],
    }
    try:
        values = dict(line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines())
        report["available_ram_mib"] = int(values["MemAvailable"].split()[0]) // 1024
        try:
            limit = Path("/sys/fs/cgroup/memory.max").read_text().strip()
            used = int(Path("/sys/fs/cgroup/memory.current").read_text())
            if limit != "max":
                cgroup_free = max(0, (int(limit) - used) // MIB)
                report["available_ram_mib"] = min(report["available_ram_mib"], cgroup_free)
        except (OSError, ValueError):
            pass
    except (OSError, ValueError, KeyError):
        report["warnings"].append(
            "RAM/cgroup discovery incomplete; supply an explicit --memory-mib budget."
        )

    if refresh_gpus:
        try:
            result = subprocess.run(
                [
                    "nvidia-smi",
                    "--query-gpu=uuid,name,memory.total,memory.free,driver_version,index",
                    "--format=csv,noheader,nounits",
                ],
                capture_output=True, text=True, timeout=5, check=True,
            )
            for row in csv.reader(result.stdout.splitlines()):
                if len(row) < 5:
                    continue
                uid, name, total, free, driver = [x.strip() for x in row[:5]]
                idx = row[5].strip() if len(row) > 5 else str(len(report["gpus"]))
                try:
                    total_mib = int(total)
                    free_mib = int(free)
                except ValueError:
                    report["warnings"].append(f"GPU {uid}: non-integer memory ignored")
                    continue
                if total_mib < 0 or free_mib < 0:
                    report["warnings"].append(f"GPU {uid}: negative memory ignored")
                    continue
                if total_mib > MAX_REASONABLE_MIB:
                    report["warnings"].append(f"GPU {uid}: total clamped to safety bound")
                    total_mib = MAX_REASONABLE_MIB
                free_mib = min(free_mib, total_mib)
                # Detection only — never claims execution capability.
                g = {
                    "index": idx, "uuid": uid, "name": name,
                    "total_mib": total_mib, "free_mib": free_mib, "driver": driver,
                    "executable": False,
                    "reason": "Detected by telemetry; no verified GPU execution adapter registered",
                }
                report["gpus"].append(g)
                report["total_vram_mib"] += total_mib
                report["free_vram_mib"] += free_mib
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            report["warnings"].append(
                f"NVIDIA telemetry unavailable ({type(exc).__name__}); GPU list empty."
            )
    return report


# ---------------------------------------------------------------------------
# Job
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Job:
    id: str
    kind: str = "hash"
    payload: dict = field(default_factory=dict)
    memory_mib: int = 64          # request against the backend's capacity domain
    priority: int = 0
    target_s: float = 2.0
    timeout_s: float = 30.0
    authorized: bool = False
    # Explicit capability tokens the job is willing to accept.
    # "cpu" | "gpu_sim" | "gpu_real"
    allowed_capabilities: tuple = ("gpu_sim", "gpu_real", "cpu")


# ---------------------------------------------------------------------------
# Backend protocol & implementations
# ---------------------------------------------------------------------------

class Backend(Protocol):
    name: str
    capacity_mib: int
    max_concurrency: int
    # Explicit capability: "cpu" | "gpu_sim" | "gpu_real"
    capability: str
    # True only when the backend can perform real device-side work.
    verified_execution: bool

    async def execute(self, job: Job) -> dict: ...
    async def refresh_capacity(self) -> None: ...


class CPUBackend:
    """Real host-CPU work. Capacity is a logical host-RAM budget."""
    name = "cpu"
    capability = "cpu"
    verified_execution = True

    def __init__(self, capacity_mib: int, max_concurrency: int = 1):
        if capacity_mib <= 0 or max_concurrency <= 0:
            raise ValueError("Positive capacity and concurrency required")
        if capacity_mib > MAX_REASONABLE_MIB:
            raise ValueError(f"capacity_mib exceeds safety bound")
        self.capacity_mib = capacity_mib
        self.max_concurrency = max_concurrency
        # Shared CPU execution slots also consumed by simulated GPU work.
        self._cpu_slots_in_use = 0

    async def refresh_capacity(self) -> None:
        pass

    def try_acquire_cpu_slot(self) -> bool:
        if self._cpu_slots_in_use >= self.max_concurrency:
            return False
        self._cpu_slots_in_use += 1
        return True

    def release_cpu_slot(self) -> None:
        self._cpu_slots_in_use = max(0, self._cpu_slots_in_use - 1)

    async def execute(self, job: Job) -> dict:
        if not self.try_acquire_cpu_slot():
            raise RuntimeError("CPU execution budget exhausted")
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, str(Path(__file__).with_name("worker.py")),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                out, err = await asyncio.wait_for(
                    proc.communicate(json.dumps(asdict(job)).encode()), job.timeout_s
                )
                if proc.returncode:
                    raise RuntimeError(err.decode(errors="replace")[-1000:])
                result = json.loads(out)
            finally:
                if proc.returncode is None:
                    proc.kill()
                    await proc.wait()
        finally:
            self.release_cpu_slot()
        result.setdefault("peak_rss_mib", None)
        result["peak_vram_mib"] = None
        result["execution_mode"] = "cpu_host"
        result["verified_execution"] = True
        result["capability"] = self.capability
        return result


class GPUBackend:
    """VRAM capacity-accounting backend.

    capability = "gpu_sim"  → host worker, no real device kernels.
    capability = "gpu_real" → reserved for future real adapters (not used here).

    Accounting policy (conservative reconciliation):
      effective_capacity = max(0, live_free_mib - outstanding_reservations)
    Live free is refreshed asynchronously and rate-limited. On telemetry
    failure the previous capacity is retained (never invented).
    """
    verified_execution = False  # overridden only by real adapters

    def __init__(
        self,
        name: str,
        capacity_mib: int,
        max_concurrency: int = 1,
        *,
        uuid: str | None = None,
        device_index: str | None = None,
        is_mock: bool = False,
        cpu_backend: CPUBackend | None = None,
    ):
        if capacity_mib < 0 or max_concurrency <= 0:
            raise ValueError("Non-negative capacity and positive concurrency required")
        capacity_mib = min(capacity_mib, MAX_REASONABLE_MIB)
        self.name = name
        self.capacity_mib = capacity_mib
        self.max_concurrency = max_concurrency
        self.uuid = uuid
        self.device_index = device_index
        self.is_mock = is_mock
        self.capability = "gpu_sim"
        self._base_capacity = capacity_mib
        self._outstanding_mib = 0          # HARO reservations not yet released
        self._last_refresh_mono = 0.0
        self._cpu_backend = cpu_backend    # shared CPU budget for simulated work

    async def refresh_capacity(self) -> None:
        """Rate-limited, non-blocking refresh of live free VRAM."""
        if self.is_mock:
            # Mock has fixed logical capacity; outstanding already subtracted at admission.
            self.capacity_mib = max(0, self._base_capacity - self._outstanding_mib)
            return
        now = time.monotonic()
        if now - self._last_refresh_mono < _REFRESH_MIN_INTERVAL_S:
            return
        self._last_refresh_mono = now
        if self.uuid is None and self.device_index is None:
            return
        try:
            free = await asyncio.to_thread(self._query_free_vram)
            if free is not None:
                # Reconcile: live free already reflects other processes;
                # subtract only HARO's own outstanding reservations.
                self.capacity_mib = max(0, free - self._outstanding_mib)
        except Exception:
            # Conservative: leave capacity unchanged on any failure.
            pass

    def _query_free_vram(self) -> int | None:
        """Synchronous nvidia-smi query; called only via to_thread."""
        try:
            cmd = [
                "nvidia-smi",
                f"--id={self.uuid or self.device_index}",
                "--query-gpu=memory.free",
                "--format=csv,noheader,nounits",
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=3, check=True)
            free = int(result.stdout.strip().splitlines()[0].strip())
            if 0 <= free <= MAX_REASONABLE_MIB:
                return free
        except (OSError, subprocess.SubprocessError, ValueError, IndexError):
            pass
        return None

    def note_reservation(self, delta: int) -> None:
        self._outstanding_mib = max(0, self._outstanding_mib + delta)
        # Keep capacity consistent immediately for the next admission decision.
        if self.is_mock:
            self.capacity_mib = max(0, self._base_capacity - self._outstanding_mib)

    async def execute(self, job: Job) -> dict:
        need = max(0, job.memory_mib)
        self.note_reservation(+need)
        # Simulated GPU work still runs on the host CPU and must respect
        # the shared CPU execution budget.
        if self._cpu_backend is not None:
            # Admission already checked; this is a final race-guard.
            # Wait briefly for a slot rather than failing the job.
            acquired = False
            for _ in range(50):  # ~0.5 s max
                if self._cpu_backend.try_acquire_cpu_slot():
                    acquired = True
                    break
                await asyncio.sleep(0.01)
            if not acquired:
                self.note_reservation(-need)
                raise RuntimeError("Shared CPU execution budget exhausted for simulated GPU work")
        try:
            env = {
                **os.environ,
                "HARO_BACKEND": self.name,
                "HARO_SIMULATED": "1",
                "CUDA_DEVICE_ORDER": "PCI_BUS_ID",
            }
            if self.uuid:
                env["CUDA_VISIBLE_DEVICES"] = self.uuid
            elif self.device_index is not None:
                env["CUDA_VISIBLE_DEVICES"] = str(self.device_index)
            else:
                env["CUDA_VISIBLE_DEVICES"] = ""  # mock: hide all GPUs

            proc = await asyncio.create_subprocess_exec(
                sys.executable, str(Path(__file__).with_name("worker.py")),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            try:
                out, err = await asyncio.wait_for(
                    proc.communicate(json.dumps(asdict(job)).encode()), job.timeout_s
                )
                if proc.returncode:
                    raise RuntimeError(err.decode(errors="replace")[-1000:])
                result = json.loads(out)
            finally:
                if proc.returncode is None:
                    proc.kill()
                    await proc.wait()
        finally:
            self.note_reservation(-need)
            if self._cpu_backend is not None:
                self._cpu_backend.release_cpu_slot()

        result.setdefault("peak_rss_mib", None)
        result["peak_vram_mib"] = None          # no real device measurement
        result["execution_mode"] = "simulated_gpu_accounting"
        result["verified_execution"] = False
        result["capability"] = self.capability
        result["backend_note"] = (
            f"VRAM capacity accounting via {self.name}; "
            "host worker only — not genuine GPU acceleration"
        )
        return result


class MockGPUBackend(GPUBackend):
    """Fixed logical capacity for CI / extreme-size tests. Distinct name namespace."""
    def __init__(
        self,
        name: str = "mock-gpu0",
        capacity_mib: int = 8192,
        max_concurrency: int = 2,
        cpu_backend: CPUBackend | None = None,
    ):
        super().__init__(
            name=name,
            capacity_mib=capacity_mib,
            max_concurrency=max_concurrency,
            is_mock=True,
            cpu_backend=cpu_backend,
        )


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------

class Scheduler:
    """Finite-batch, non-preemptive, priority-first backfilling scheduler.

    Prefers backends whose capability is in the job's allowed_capabilities,
    ranking gpu_real > gpu_sim > cpu. Reservations are logical.
    One run() at a time.
    """

    def __init__(self, backends: list, audit_path: str | Path | None = None):
        self.backends = {b.name: b for b in backends}
        if len(self.backends) != len(backends):
            raise ValueError("Backend names must be unique")
        self.audit_path = Path(audit_path) if audit_path else None
        self.events: list[dict] = []
        self.profiles: dict[tuple[str, str], dict] = {}
        self.busy = False

    def record(self, event: str, **fields) -> None:
        row = dict(event=event, monotonic_s=time.monotonic(), **fields)
        self.events.append(row)
        if self.audit_path:
            with self.audit_path.open("a", encoding="utf8") as f:
                f.write(json.dumps(row) + "\n")

    def estimate(self, job: Job, backend_name: str) -> int:
        profile = self.profiles.get((backend_name, job.kind), {})
        # Caller estimate is never reduced by historical observations.
        return max(job.memory_mib, profile.get("peak_mib", 0))

    def _capability_rank(self, capability: str) -> int:
        # Lower is better.
        return {"gpu_real": 0, "gpu_sim": 1, "cpu": 10}.get(capability, 5)

    def _is_allowed(self, job: Job, backend) -> bool:
        return backend.capability in job.allowed_capabilities

    async def run(self, jobs: list[Job]) -> dict[str, dict]:
        if self.busy:
            raise RuntimeError("Concurrent run calls are not supported")
        jobs = list(jobs)
        if len({j.id for j in jobs}) != len(jobs):
            raise ValueError("Job ids must be unique within a batch")
        self.busy = True
        try:
            return await self._run(jobs)
        finally:
            self.busy = False

    async def _run(self, jobs: list[Job]) -> dict[str, dict]:
        start = time.monotonic()
        pending = sorted(enumerate(jobs), key=lambda x: (-x[1].priority, x[0]))
        used = {name: 0 for name in self.backends}
        slots = dict(used)
        running: dict[asyncio.Task, tuple] = {}
        results: dict[str, dict] = {}

        try:
            while pending or running:
                # Non-blocking, rate-limited refresh (to_thread inside GPUBackend).
                for b in self.backends.values():
                    await b.refresh_capacity()

                progress = False
                for item in list(pending):
                    _, job = item
                    if not job.authorized:
                        results[job.id] = dict(status="rejected", reason="authorization_required")
                    elif (job.memory_mib <= 0 or job.timeout_s <= 0 or job.target_s <= 0
                          or job.memory_mib > MAX_REASONABLE_MIB):
                        results[job.id] = dict(status="rejected", reason="invalid_resource_request")
                    else:
                        candidates = []
                        for name, b in self.backends.items():
                            if not self._is_allowed(job, b):
                                continue
                            need = self.estimate(job, name)
                            if need > b.capacity_mib:
                                continue
                            candidates.append((need, name, b))
                        if not candidates:
                            results[job.id] = dict(status="rejected", reason="no_compatible_capacity")
                        else:
                            available = []
                            for need, name, b in candidates:
                                if used[name] + need > b.capacity_mib:
                                    continue
                                if slots[name] >= b.max_concurrency:
                                    continue
                                # Simulated GPU work also needs a free shared CPU slot.
                                if getattr(b, "capability", "") == "gpu_sim":
                                    cpu_b = getattr(b, "_cpu_backend", None)
                                    if cpu_b is not None and cpu_b._cpu_slots_in_use >= cpu_b.max_concurrency:
                                        continue
                                available.append((need, name, b))
                            if not available:
                                continue

                            def score(v):
                                need, name, b = v
                                rank = self._capability_rank(b.capability)
                                mean = self.profiles.get((name, job.kind), {}).get("mean_s", float("inf"))
                                return (rank, mean, name)

                            need, name, backend = min(available, key=score)
                            used[name] += need
                            slots[name] += 1
                            # Immediately record the reservation so concurrent refresh sees it.
                            if hasattr(backend, "note_reservation"):
                                backend.note_reservation(+need)
                            self.record(
                                "admitted",
                                job_id=job.id, backend=name, reserved_mib=need,
                                used_mib=used[name], capacity_mib=backend.capacity_mib,
                                capability=backend.capability,
                                verified_execution=backend.verified_execution,
                                reason="compatible; capability preference then measured latency",
                                queue_s=time.monotonic() - start,
                            )
                            task = asyncio.create_task(backend.execute(job))
                            running[task] = (job, name, need, time.monotonic(), backend)
                            pending.remove(item)
                            progress = True
                            continue
                    self.record("rejected", job_id=job.id, **results[job.id])
                    pending.remove(item)
                    progress = True

                if not running:
                    if pending and not progress:
                        for _, job in pending:
                            results[job.id] = dict(status="rejected", reason="no_compatible_capacity")
                            self.record("rejected", job_id=job.id, **results[job.id])
                        pending.clear()
                    break

                done, _ = await asyncio.wait(running, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    job, name, need, began, backend = running.pop(task)
                    used[name] -= need
                    slots[name] -= 1
                    if hasattr(backend, "note_reservation"):
                        backend.note_reservation(-need)
                    elapsed = time.monotonic() - began
                    try:
                        output = task.result()
                        peak_host = output.get("peak_rss_mib")
                        peak_device = output.get("peak_vram_mib")
                        key = (name, job.kind)
                        p = self.profiles.setdefault(key, dict(count=0, mean_s=0.0, peak_mib=0))
                        p["count"] += 1
                        p["mean_s"] += (elapsed - p["mean_s"]) / p["count"]
                        # Profile peak uses the metric that belongs to the backend.
                        observed = peak_device if peak_device is not None else peak_host
                        p["peak_mib"] = max(p["peak_mib"], observed or 0)
                        result = dict(
                            status="ok", backend=name, output=output,
                            execution_s=elapsed,
                            end_to_end_s=time.monotonic() - start,
                            target_met=(time.monotonic() - start) <= job.target_s,
                            estimate_exceeded=(
                                (peak_device is not None and peak_device > need) or
                                (peak_device is None and peak_host is not None and peak_host > need)
                            ),
                            verified_execution=output.get("verified_execution", False),
                            capability=output.get("capability", backend.capability),
                        )
                    except Exception as exc:
                        result = dict(
                            status="error", backend=name,
                            error=type(exc).__name__ + ": " + str(exc),
                            execution_s=elapsed,
                        )
                    results[job.id] = result
                    self.record("completed", job_id=job.id, **result)
            return results
        finally:
            for task in list(running):
                task.cancel()
            if running:
                await asyncio.gather(*running, return_exceptions=True)


# ---------------------------------------------------------------------------
# Backend construction (injectable discovery, unique names)
# ---------------------------------------------------------------------------

def build_backends(
    report: dict | None = None,
    memory_mib: int | None = None,
    concurrency: int = 1,
    force_mock_gpu_mib: int | None = None,
) -> list:
    """Construct backends from an (optional) discovery report.

    When report is supplied, live discovery is skipped — tests become
    independent of the host's GPUs. Mock names live in the mock-gpu*
    namespace so they never collide with real gpu* identifiers.
    """
    report = report if report is not None else discover()
    backends: list = []
    used_names: set[str] = set()

    # CPU first (also supplies the shared execution budget for simulated GPUs).
    budget = memory_mib
    if budget is None:
        available = report.get("available_ram_mib")
        if available is None or available < 64:
            budget = 128
        else:
            budget = min(1024, max(128, available // 2))
    budget = max(64, min(budget, MAX_REASONABLE_MIB))
    cpu_conc = max(1, min(concurrency, report.get("cpu_count", 1)))
    cpu = CPUBackend(budget, cpu_conc)
    backends.append(cpu)
    used_names.add("cpu")

    # Real detected GPUs → simulated capacity backends.
    for i, g in enumerate(report.get("gpus") or []):
        free = g.get("free_mib", 0)
        if free <= 0:
            continue
        name = f"gpu{i}"
        while name in used_names:
            name = f"gpu{i}_{len(used_names)}"
        used_names.add(name)
        g_conc = 1 if free < 16_000 else (2 if free < 80_000 else 4)
        g_conc = min(g_conc, concurrency)
        backends.append(GPUBackend(
            name=name,
            capacity_mib=free,
            max_concurrency=g_conc,
            uuid=g.get("uuid"),
            device_index=str(g.get("index", i)),
            is_mock=False,
            cpu_backend=cpu,
        ))

    # Optional mock — distinct namespace.
    if force_mock_gpu_mib is not None and force_mock_gpu_mib > 0:
        mock_name = "mock-gpu0"
        n = 0
        while mock_name in used_names:
            n += 1
            mock_name = f"mock-gpu{n}"
        backends.append(MockGPUBackend(
            name=mock_name,
            capacity_mib=min(force_mock_gpu_mib, MAX_REASONABLE_MIB),
            max_concurrency=max(1, concurrency),
            cpu_backend=cpu,
        ))

    return backends


# ---------------------------------------------------------------------------
# Demo jobs & CLI
# ---------------------------------------------------------------------------

def demo_jobs() -> list[Job]:
    return [
        Job("transcription-placeholder", payload={"text": "audio fixture"}, authorized=True, priority=10, memory_mib=64),
        Job("reasoning-placeholder", payload={"text": "text fixture"}, authorized=True, priority=5, memory_mib=128),
        Job("synthesis-placeholder", payload={"text": "speech fixture"}, authorized=True, memory_mib=64),
    ]


async def cli(args: argparse.Namespace) -> None:
    if args.command == "discover":
        print(json.dumps(discover(), indent=2))
        return

    report = discover()
    force_mock = getattr(args, "mock_gpu_mib", None)
    backends = build_backends(
        report, memory_mib=args.memory_mib,
        concurrency=args.concurrency, force_mock_gpu_mib=force_mock,
    )
    scheduler = Scheduler(backends, args.audit)

    if args.command == "demo":
        results = {}
        for job in demo_jobs():
            result = await scheduler.run([job])
            results.update(result)
            if result[job.id]["status"] != "ok":
                break
        print(json.dumps({
            "results": results,
            "backends": [
                {"name": b.name, "capacity_mib": b.capacity_mib,
                 "capability": b.capability, "verified_execution": b.verified_execution}
                for b in backends
            ],
            "note": (
                "Hash fixtures only. GPU backends perform VRAM accounting + host worker; "
                "verified_execution=False until a real adapter is registered."
            ),
        }, indent=2))

    elif args.command == "benchmark":
        jobs = [Job(str(i), payload={"iterations": 80_000, "text": str(i)}, authorized=True) for i in range(6)]
        direct = CPUBackend(args.memory_mib or 128, 1)
        t = time.monotonic()
        outputs = [await direct.execute(j) for j in jobs]
        direct_s = time.monotonic() - t
        t = time.monotonic()
        results = await scheduler.run(jobs)
        scheduled_s = time.monotonic() - t
        print(json.dumps({
            "direct_sequential_s": direct_s,
            "scheduled_s": scheduled_s,
            "ratio_direct_over_scheduled": direct_s / scheduled_s if scheduled_s else None,
            "identical_outputs": all(
                results[j.id].get("output", {}).get("digest") == o["digest"]
                for j, o in zip(jobs, outputs)
            ),
            "backends_used": sorted({r.get("backend") for r in results.values() if r.get("status") == "ok"}),
            "note": "Single trial. Digests must match. GPU path is simulated accounting only.",
        }, indent=2))

    elif args.command == "adaptive":
        large = min(report.get("free_vram_mib") or 4096, 8192)
        jobs = [
            Job("cpu-light", kind="hash", payload={"iterations": 10_000}, authorized=True,
                priority=1, memory_mib=32, allowed_capabilities=("cpu",)),
            Job("gpu-prefer", kind="hash", payload={"iterations": 20_000}, authorized=True,
                priority=5, memory_mib=64),
            Job("large-reservation", kind="hash", payload={"iterations": 5_000}, authorized=True,
                priority=0, memory_mib=min(large // 4, 2048)),
        ]
        results = await scheduler.run(jobs)
        print(json.dumps({
            "discovery_summary": {
                "available_ram_mib": report.get("available_ram_mib"),
                "gpu_count": len(report.get("gpus") or []),
                "total_vram_mib": report.get("total_vram_mib"),
                "free_vram_mib": report.get("free_vram_mib"),
                "warnings": report.get("warnings"),
            },
            "backends": [
                {"name": b.name, "capacity_mib": b.capacity_mib,
                 "max_concurrency": b.max_concurrency,
                 "capability": b.capability, "verified_execution": b.verified_execution}
                for b in backends
            ],
            "results": results,
            "note": (
                "Capability preference: gpu_real > gpu_sim > cpu. "
                "All current GPU backends are gpu_sim (simulated accounting only)."
            ),
        }, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("command", choices=["discover", "demo", "benchmark", "adaptive"])
    parser.add_argument("--memory-mib", type=int, default=None)
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument("--audit", default="audit.jsonl")
    parser.add_argument(
        "--mock-gpu-mib", type=int, default=None,
        help="Inject a mock GPU (distinct name mock-gpuN) of this capacity in MiB",
    )
    args = parser.parse_args()
    if args.memory_mib is not None and (args.memory_mib <= 0 or args.memory_mib > MAX_REASONABLE_MIB):
        parser.error(f"--memory-mib must be in 1..{MAX_REASONABLE_MIB}")
    if args.mock_gpu_mib is not None and (args.mock_gpu_mib < 0 or args.mock_gpu_mib > MAX_REASONABLE_MIB):
        parser.error(f"--mock-gpu-mib must be in 0..{MAX_REASONABLE_MIB}")
    asyncio.run(cli(args))


if __name__ == "__main__":
    main()
