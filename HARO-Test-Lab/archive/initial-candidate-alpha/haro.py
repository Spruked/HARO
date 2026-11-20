#!/usr/bin/env python3
"""Hardware-Adaptive Runtime Orchestrator (HARO) — multi-backend, capacity-adaptive scheduler.

Python 3.11+, standard library only. Discovers host RAM + NVIDIA GPUs, builds CPU and
GPU backends dynamically, and schedules finite batches with priority + backfill.

No model inference or external deps. Every backend in this build executes the same
fixed *host* worker. GPU backends account admission against device VRAM, but no GPU
inference happens; audit records say so explicitly (execution="host_fixture",
hardware_inference=false). Real adapters plug in via the Backend protocol.

Three separate concepts are kept apart throughout:
  * logical capacity      - capacity_mib: what the scheduler may reserve (static budget)
  * physical availability - observed_free_mib: what the hardware last reported (may be None)
  * execution capability  - device_class / execution / simulated: what the backend can run
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
from typing import Any, Callable, Protocol

MIB = 1024 * 1024
# Practical upper bound on a single capacity value or request (20 TiB expressed in MiB).
# This bounds integer accounting only; it is not a statement about validated hardware.
MAX_REASONABLE_MIB = 20 * 1024 * 1024

# Execution capability labels.
EXEC_HOST_FIXTURE = "host_fixture"   # fixed worker.py running on the host CPU
EXEC_GPU_INFERENCE = "gpu_inference"  # reserved for real GPU adapters (none in this build)

NVIDIA_QUERY = "uuid,name,memory.total,memory.free,driver_version,index"


def _run_nvidia_smi(args: list[str], timeout: float) -> str:
    """Default telemetry runner. Raises OSError/SubprocessError on failure."""
    result = subprocess.run(
        ["nvidia-smi", *args], capture_output=True, text=True, timeout=timeout, check=True
    )
    return result.stdout


def _query_free_vram_mib(device_id: str, runner: Callable | None = None) -> int:
    """Return free VRAM (MiB) for one device, addressed by UUID or index."""
    out = (runner or _run_nvidia_smi)(
        [f"--id={device_id}", "--query-gpu=memory.free", "--format=csv,noheader,nounits"], 3
    )
    return int(out.strip().splitlines()[0].strip())


def discover(refresh_gpus: bool = True, runner: Callable | None = None) -> dict[str, Any]:
    """Snapshot of host resources. Safe on machines with 0..N GPUs.

    `runner(args, timeout) -> stdout` is injectable so tests never depend on the host.
    """
    report: dict[str, Any] = {
        "os": platform.system(),
        "cpu_count": os.cpu_count() or 1,
        "available_ram_mib": None,
        "gpus": [],
        "total_vram_mib": 0,
        "free_vram_mib": 0,
        "gpu_telemetry_ok": None,
        "warnings": [],
    }
    # --- Host RAM / cgroup ---
    try:
        values = dict(
            line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines()
        )
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

    # --- NVIDIA GPUs ---
    if refresh_gpus:
        try:
            out = (runner or _run_nvidia_smi)(
                [f"--query-gpu={NVIDIA_QUERY}", "--format=csv,noheader,nounits"], 5
            )
            for row in csv.reader(out.splitlines()):
                if len(row) < 5:
                    continue
                uid, name, total, free, driver = [x.strip() for x in row[:5]]
                idx = row[5].strip() if len(row) > 5 else str(len(report["gpus"]))
                total_mib = int(total)
                free_mib = int(free)
                if total_mib < 0 or free_mib < 0:
                    report["warnings"].append(f"GPU {uid}: negative memory report ignored")
                    continue
                if total_mib > MAX_REASONABLE_MIB:
                    report["warnings"].append(
                        f"GPU {uid}: total_mib={total_mib} exceeds accounting bound; clamped"
                    )
                    total_mib = MAX_REASONABLE_MIB
                free_mib = min(free_mib, total_mib)
                report["gpus"].append(
                    {
                        "index": idx,
                        "uuid": uid,
                        "name": name,
                        "total_mib": total_mib,
                        "free_mib": free_mib,
                        "driver": driver,
                        # Physical VRAM is present; no GPU execution adapter exists in this build.
                        "execution_capability": EXEC_HOST_FIXTURE,
                        "reason": "VRAM usable for admission accounting only; no GPU inference adapter",
                    }
                )
                report["total_vram_mib"] += total_mib
                report["free_vram_mib"] += free_mib
            report["gpu_telemetry_ok"] = True
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            report["gpu_telemetry_ok"] = False
            report["warnings"].append(
                f"NVIDIA telemetry unavailable ({type(exc).__name__}); "
                "no GPU backends built from discovery. Other accelerators not probed."
            )
    return report


@dataclass(frozen=True)
class Job:
    id: str
    kind: str = "hash"
    payload: dict = field(default_factory=dict)
    memory_mib: int = 64
    priority: int = 0
    target_s: float = 2.0
    timeout_s: float = 30.0
    authorized: bool = False
    # Entries match a backend name exactly, or a device_class ("gpu", "cpu").
    allowed_backends: tuple = ("gpu", "cpu")


class Backend(Protocol):
    """Backend contract.

    Required: name, capacity_mib (logical budget), max_concurrency, async execute(job).
    Capability metadata (read with getattr defaults if absent):
      device_class       "cpu" | "gpu" | other   (default "other": never matched by "gpu"/"cpu")
      execution          EXEC_HOST_FIXTURE | EXEC_GPU_INFERENCE | other
      simulated          True if capacity is not backed by real hardware
      reserved_resource  "host_ram" | "device_vram" | "simulated_vram"
      observed_free_mib  latest physical availability, or None if unknown
      has_live_telemetry True if refresh_capacity() queries hardware
    Optional: refresh_capacity() (sync; scheduler runs it off the event loop),
              attributed_usage() -> {job_id: mib} of device memory known to belong to
              HARO jobs (used to reconcile telemetry against reservations).
    """

    name: str
    capacity_mib: int
    max_concurrency: int

    async def execute(self, job: Job) -> dict: ...


async def _run_fixture(job: Job, env: dict | None = None) -> dict:
    """Run the fixed host worker. Shared by every backend in this build."""
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        str(Path(__file__).with_name("worker.py")),
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
        return json.loads(out)
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()


class CPUBackend:
    name = "cpu"
    device_class = "cpu"
    execution = EXEC_HOST_FIXTURE
    simulated = False
    reserved_resource = "host_ram"
    observed_free_mib = None
    has_live_telemetry = False

    def __init__(self, capacity_mib: int, max_concurrency: int = 1):
        if capacity_mib <= 0 or max_concurrency <= 0:
            raise ValueError("Positive capacity and concurrency required")
        if capacity_mib > MAX_REASONABLE_MIB:
            raise ValueError(f"capacity_mib {capacity_mib} exceeds safety bound")
        self.capacity_mib = capacity_mib
        self.max_concurrency = max_concurrency

    def refresh_capacity(self) -> None:
        # Static logical budget; host free RAM is not re-probed.
        pass

    async def execute(self, job: Job) -> dict:
        return await _run_fixture(job)


class GPUBackend:
    """Physical NVIDIA device with VRAM admission accounting.

    capacity_mib is the logical budget (free VRAM at discovery) and never changes after
    construction. refresh_capacity() updates observed_free_mib (physical availability).
    execute() still runs the fixed HOST worker: no GPU inference happens in this build.
    """

    device_class = "gpu"
    execution = EXEC_HOST_FIXTURE
    simulated = False
    reserved_resource = "device_vram"

    def __init__(
        self,
        name: str,
        capacity_mib: int,
        max_concurrency: int = 1,
        uuid: str | None = None,
        device_index: str | None = None,
        telemetry: Callable[[str], int] | None = None,
    ):
        if capacity_mib < 0 or max_concurrency <= 0:
            raise ValueError("Non-negative capacity and positive concurrency required")
        capacity_mib = min(capacity_mib, MAX_REASONABLE_MIB)
        self.name = name
        self.capacity_mib = capacity_mib
        self.max_concurrency = max_concurrency
        self.uuid = uuid
        self.device_index = device_index
        self._base_capacity = capacity_mib
        self._telemetry = telemetry or _query_free_vram_mib
        # Physical availability: seeded from discovery, updated by refresh_capacity().
        self.observed_free_mib: int | None = capacity_mib if self.device_id else None
        self.telemetry_ok: bool | None = None
        self.telemetry_at: float | None = None

    @property
    def device_id(self) -> str | None:
        """Stable device identity: UUID preferred over (enumeration-order-dependent) index."""
        return self.uuid or self.device_index

    @property
    def has_live_telemetry(self) -> bool:
        return self.device_id is not None

    def refresh_capacity(self) -> None:
        """Update observed_free_mib from telemetry. Never changes logical capacity_mib.

        On failure the last observation is kept and telemetry_ok is set False.
        Synchronous by design; the Scheduler calls it via asyncio.to_thread with a timeout.
        """
        if self.device_id is None:
            return
        try:
            free = int(self._telemetry(self.device_id))
            if not 0 <= free <= MAX_REASONABLE_MIB:
                raise ValueError(f"implausible free VRAM {free}")
            self.observed_free_mib = free
            self.telemetry_ok = True
        except (OSError, subprocess.SubprocessError, ValueError, IndexError):
            self.telemetry_ok = False
        self.telemetry_at = time.monotonic()

    def _device_env(self) -> dict:
        """Environment for the child process.

        CUDA's default ordering (FASTEST_FIRST) can differ from nvidia-smi's index order,
        so pin PCI_BUS_ID ordering and address the device by UUID when known.
        """
        env = {**os.environ, "HARO_BACKEND": self.name, "CUDA_DEVICE_ORDER": "PCI_BUS_ID"}
        if self.device_id is not None:
            env["CUDA_VISIBLE_DEVICES"] = self.device_id
        else:
            env.pop("CUDA_VISIBLE_DEVICES", None)
        return env

    async def execute(self, job: Job) -> dict:
        result = await _run_fixture(job, env=self._device_env())
        result["backend_note"] = (
            f"{self.name}: host fixture worker; VRAM admission accounting only; no GPU inference"
        )
        return result


class MockGPUBackend(GPUBackend):
    """Simulated GPU capacity for CI / no-GPU / extreme-size accounting tests.

    Logical capacity only: no physical device, no telemetry, no GPU execution.
    """

    simulated = True
    reserved_resource = "simulated_vram"

    def __init__(self, name: str = "gpu0", capacity_mib: int = 8192, max_concurrency: int = 2):
        super().__init__(name, capacity_mib, max_concurrency)
        self._base_capacity = capacity_mib
        self.observed_free_mib = None

    @property
    def has_live_telemetry(self) -> bool:
        return False

    def refresh_capacity(self) -> None:
        self.capacity_mib = self._base_capacity

    def _device_env(self) -> dict:
        # Simulated: deliberately hide every real GPU from the fixture process.
        return {**os.environ, "HARO_BACKEND": self.name, "CUDA_VISIBLE_DEVICES": ""}


def _meta(b) -> dict:
    """Capability metadata for audit records (defaults for third-party backends)."""
    execution = getattr(b, "execution", "unknown")
    simulated = bool(getattr(b, "simulated", False))
    return dict(
        device_class=getattr(b, "device_class", "other"),
        execution=execution,
        simulated=simulated,
        reserved_resource=getattr(b, "reserved_resource", "unknown"),
        # Decided by the scheduler from capability metadata, never from worker output.
        hardware_inference=(execution == EXEC_GPU_INFERENCE and not simulated),
    )


class Scheduler:
    """Finite-batch, non-preemptive, priority-first backfilling scheduler.

    VRAM / capacity accounting policy (conservative):
      logical_headroom  = capacity_mib - sum(reservations)
      physical_headroom = observed_free_mib - sum(max(0, reservation - attributed_allocation))
      admission requires need <= min(logical_headroom, physical_headroom)
    physical_headroom is only applied when the backend reports observed_free_mib.
    Reservations whose allocation is not attributed are assumed NOT yet allocated, so
    the policy may under-admit, but never over-admits relative to the last telemetry.
    Memory taken by unrelated processes after start appears through observed_free_mib.

    Host execution budget: every backend whose execution is host_fixture (including
    mock and accounting-only GPU backends) draws from one shared pool of host slots.
    """

    def __init__(
        self,
        backends: list,
        audit_path: str | Path | None = None,
        refresh_interval_s: float = 1.0,
        refresh_timeout_s: float = 5.0,
        host_concurrency: int | None = None,
    ):
        self.backends = {b.name: b for b in backends}
        if len(self.backends) != len(backends):
            raise ValueError("Backend names must be unique")
        self.audit_path = Path(audit_path) if audit_path else None
        self.events: list[dict] = []
        self.profiles: dict[tuple[str, str], dict] = {}
        self.busy = False
        self.refresh_interval_s = refresh_interval_s
        self.refresh_timeout_s = refresh_timeout_s
        if host_concurrency is None:
            cpu = [b for b in backends if getattr(b, "device_class", None) == "cpu"]
            host_concurrency = cpu[0].max_concurrency if cpu else (os.cpu_count() or 1)
        if host_concurrency <= 0:
            raise ValueError("host_concurrency must be positive")
        self.host_concurrency = host_concurrency
        self._ledger: dict[str, dict[str, dict]] = {n: {} for n in self.backends}
        self._host_slots_used = 0
        self._last_refresh = float("-inf")

    # ----- audit -----
    def record(self, event: str, **fields) -> None:
        row = dict(event=event, monotonic_s=time.monotonic(), **fields)
        self.events.append(row)
        if self.audit_path:
            with self.audit_path.open("a", encoding="utf8") as f:
                f.write(json.dumps(row) + "\n")

    # ----- estimates -----
    def estimate(self, job: Job, backend_name: str) -> int:
        """Caller estimate, raised (never reduced) by a measured peak of the SAME resource.

        Host RSS only informs host_ram backends; VRAM backends use device_vram_peak_mib
        when an adapter reports it (none do in this build).
        """
        profile = self.profiles.get((backend_name, job.kind), {})
        return max(job.memory_mib, profile.get("peak_mib", 0))

    def _backend_preference(self, name: str) -> int:
        # Lower is better: gpu class, then other, then cpu. Based on capability, not name.
        cls = getattr(self.backends[name], "device_class", "other")
        return {"gpu": 0, "cpu": 10}.get(cls, 5)

    @staticmethod
    def _allows(job: Job, name: str, backend) -> bool:
        cls = getattr(backend, "device_class", None)
        return any(a == name or (cls is not None and a == cls) for a in job.allowed_backends)

    # ----- ledger -----
    def reserved_mib(self, name: str) -> int:
        return sum(r["need"] for r in self._ledger[name].values())

    def _reserve(self, name: str, job_id: str, need: int) -> None:
        self._ledger[name][job_id] = dict(need=need, allocated_mib=0)

    def _release(self, name: str, job_id: str) -> None:
        self._ledger[name].pop(job_id, None)

    def headroom(self, name: str) -> tuple[int, int | None]:
        """Return (admission_headroom, physical_headroom_or_None) per the policy above."""
        b = self.backends[name]
        reservations = self._ledger[name].values()
        logical = b.capacity_mib - sum(r["need"] for r in reservations)
        observed = getattr(b, "observed_free_mib", None)
        if observed is None:
            return logical, None
        unallocated = sum(max(0, r["need"] - r["allocated_mib"]) for r in reservations)
        physical = observed - unallocated
        return min(logical, physical), physical

    async def _refresh(self, force: bool = False) -> None:
        """Bounded, nonblocking telemetry refresh (runs sync probes in worker threads)."""
        now = time.monotonic()
        if not force and now - self._last_refresh < self.refresh_interval_s:
            return
        self._last_refresh = now

        async def one(name, b):
            try:
                await asyncio.wait_for(
                    asyncio.to_thread(b.refresh_capacity), self.refresh_timeout_s
                )
                usage_fn = getattr(b, "attributed_usage", None)
                if callable(usage_fn):
                    usage = await asyncio.wait_for(
                        asyncio.to_thread(usage_fn), self.refresh_timeout_s
                    )
                    for job_id, mib in (usage or {}).items():
                        if job_id in self._ledger[name]:
                            self._ledger[name][job_id]["allocated_mib"] = max(0, int(mib))
                if getattr(b, "telemetry_ok", None) is False:
                    self.record("telemetry_unavailable", backend=name,
                                observed_free_mib=b.observed_free_mib)
            except asyncio.TimeoutError:
                self.record("telemetry_timeout", backend=name, timeout_s=self.refresh_timeout_s)
            except Exception as exc:  # telemetry must never crash admission
                self.record("telemetry_error", backend=name, error=f"{type(exc).__name__}: {exc}")

        targets = [
            (n, b) for n, b in self.backends.items()
            if getattr(b, "has_live_telemetry", False) and hasattr(b, "refresh_capacity")
        ]
        if targets:
            await asyncio.gather(*(one(n, b) for n, b in targets))

    # ----- run -----
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
        slots = {name: 0 for name in self.backends}
        running: dict[asyncio.Task, tuple] = {}
        results: dict[str, dict] = {}
        blocked_physical: set[str] = set()
        await self._refresh(force=True)

        try:
            while pending or running:
                await self._refresh()
                progress = False
                for item in list(pending):
                    _, job = item
                    if not job.authorized:
                        results[job.id] = dict(status="rejected", reason="authorization_required")
                    elif (
                        job.memory_mib <= 0
                        or job.timeout_s <= 0
                        or job.target_s <= 0
                        or job.memory_mib > MAX_REASONABLE_MIB
                    ):
                        results[job.id] = dict(status="rejected", reason="invalid_resource_request")
                    else:
                        candidates = []
                        for name, b in self.backends.items():
                            if not self._allows(job, name, b):
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
                                if slots[name] >= b.max_concurrency:
                                    continue
                                if (getattr(b, "execution", None) == EXEC_HOST_FIXTURE
                                        and self._host_slots_used >= self.host_concurrency):
                                    continue
                                admit, physical = self.headroom(name)
                                if need > admit:
                                    logical = b.capacity_mib - self.reserved_mib(name)
                                    if need <= logical and physical is not None:
                                        blocked_physical.add(job.id)
                                    continue
                                available.append((need, name, b))
                            if not available:
                                continue

                            def score(v):
                                need, name, b = v
                                mean = self.profiles.get((name, job.kind), {}).get(
                                    "mean_s", float("inf"))
                                return (self._backend_preference(name), mean, name)

                            need, name, backend = min(available, key=score)
                            self._reserve(name, job.id, need)
                            slots[name] += 1
                            host = getattr(backend, "execution", None) == EXEC_HOST_FIXTURE
                            if host:
                                self._host_slots_used += 1
                            blocked_physical.discard(job.id)
                            self.record(
                                "admitted",
                                job_id=job.id,
                                backend=name,
                                reserved_mib=need,
                                used_mib=self.reserved_mib(name),
                                capacity_mib=backend.capacity_mib,
                                observed_free_mib=getattr(backend, "observed_free_mib", None),
                                host_slots_used=self._host_slots_used,
                                reason="compatible; fits logical+physical headroom, backend and host slots",
                                queue_s=time.monotonic() - start,
                                **_meta(backend),
                            )
                            task = asyncio.create_task(backend.execute(job))
                            running[task] = (job, name, need, time.monotonic(), host)
                            pending.remove(item)
                            progress = True
                            continue
                    self.record("rejected", job_id=job.id, **results[job.id])
                    pending.remove(item)
                    progress = True

                if not running:
                    if pending and not progress:
                        for _, job in pending:
                            reason = ("insufficient_physical_availability"
                                      if job.id in blocked_physical else "no_compatible_capacity")
                            results[job.id] = dict(status="rejected", reason=reason)
                            self.record("rejected", job_id=job.id, **results[job.id])
                        pending.clear()
                    break

                # With jobs pending, wake at the refresh interval so new telemetry can admit them.
                done, _ = await asyncio.wait(
                    running,
                    timeout=self.refresh_interval_s if pending else None,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for task in done:
                    job, name, need, began, host = running.pop(task)
                    self._release(name, job.id)
                    slots[name] -= 1
                    if host:
                        self._host_slots_used -= 1
                    backend = self.backends[name]
                    meta = _meta(backend)
                    elapsed = time.monotonic() - began
                    try:
                        output = task.result()
                        host_peak = output.get("peak_rss_mib")
                        vram_peak = output.get("device_vram_peak_mib")
                        resource_peak = (host_peak if meta["reserved_resource"] == "host_ram"
                                         else vram_peak)
                        p = self.profiles.setdefault(
                            (name, job.kind),
                            dict(count=0, mean_s=0.0, peak_mib=0, host_peak_rss_mib=0),
                        )
                        p["count"] += 1
                        p["mean_s"] += (elapsed - p["mean_s"]) / p["count"]
                        # peak_mib = peak of the resource this backend reserves.
                        p["peak_mib"] = max(p["peak_mib"], resource_peak or 0)
                        p["host_peak_rss_mib"] = max(p["host_peak_rss_mib"], host_peak or 0)
                        end_to_end = time.monotonic() - start
                        result = dict(
                            status="ok",
                            backend=name,
                            output=output,
                            execution_s=elapsed,
                            end_to_end_s=end_to_end,
                            target_met=end_to_end <= job.target_s,
                            reserved_mib=need,
                            host_peak_rss_mib=host_peak,
                            device_vram_peak_mib=vram_peak,
                            # None = the reserved resource was not measured.
                            estimate_exceeded=(None if resource_peak is None
                                               else resource_peak > need),
                            **meta,
                        )
                    except Exception as exc:
                        result = dict(
                            status="error",
                            backend=name,
                            error=type(exc).__name__ + ": " + str(exc),
                            execution_s=elapsed,
                            **meta,
                        )
                    results[job.id] = result
                    self.record("completed", job_id=job.id, **result)
            return results
        finally:
            for task in list(running):
                task.cancel()
            if running:
                await asyncio.gather(*running, return_exceptions=True)
            # Release every reservation and host slot still held (cancel / error paths).
            for (job, name, need, began, host) in running.values():
                self._release(name, job.id)
                if host:
                    self._host_slots_used -= 1
            running.clear()


def build_backends(
    report: dict | None = None,
    memory_mib: int | None = None,
    concurrency: int = 1,
    force_mock_gpu_mib: int | None = None,
) -> list:
    """Construct backends from a discovery report (live discovery only if report is None)."""
    if report is None:
        report = discover()
    backends: list = []

    budget = memory_mib
    if budget is None:
        available = report.get("available_ram_mib")
        if available is None or available < 64:
            budget = 128
        else:
            budget = min(1024, max(128, available // 2))
    budget = max(64, min(budget, MAX_REASONABLE_MIB))
    cpu_conc = max(1, min(concurrency, report.get("cpu_count", 1)))
    backends.append(CPUBackend(budget, cpu_conc))

    for i, g in enumerate(report.get("gpus") or []):
        free = g.get("free_mib", 0)
        if free <= 0:
            continue
        g_conc = 1 if free < 16_000 else (2 if free < 80_000 else 4)
        g_conc = min(g_conc, concurrency)
        backends.append(
            GPUBackend(
                name=f"gpu{i}",
                capacity_mib=free,
                max_concurrency=max(1, g_conc),
                uuid=g.get("uuid"),
                device_index=str(g.get("index", i)),
            )
        )

    if force_mock_gpu_mib is not None and force_mock_gpu_mib > 0:
        # Next unused gpuN name, so mocks never collide with real devices.
        taken = {b.name for b in backends}
        n = 0
        while f"gpu{n}" in taken:
            n += 1
        backends.append(
            MockGPUBackend(
                name=f"gpu{n}",
                capacity_mib=min(force_mock_gpu_mib, MAX_REASONABLE_MIB),
                max_concurrency=max(1, concurrency),
            )
        )
    return backends


def demo_jobs() -> list[Job]:
    return [
        Job("transcription-placeholder", payload={"text": "audio fixture"},
            authorized=True, priority=10, memory_mib=64),
        Job("reasoning-placeholder", payload={"text": "text fixture"},
            authorized=True, priority=5, memory_mib=128),
        Job("synthesis-placeholder", payload={"text": "speech fixture"},
            authorized=True, memory_mib=64),
    ]


def _describe(b) -> dict:
    return dict(
        name=b.name,
        capacity_mib=b.capacity_mib,
        observed_free_mib=getattr(b, "observed_free_mib", None),
        max_concurrency=b.max_concurrency,
        **{k: v for k, v in _meta(b).items()},
    )


async def cli(args: argparse.Namespace) -> None:
    if args.command == "discover":
        print(json.dumps(discover(), indent=2))
        return

    report = discover()
    backends = build_backends(
        report,
        memory_mib=args.memory_mib,
        concurrency=args.concurrency,
        force_mock_gpu_mib=getattr(args, "mock_gpu_mib", None),
    )
    scheduler = Scheduler(backends, args.audit)
    note_exec = ("All execution is the fixed host worker (hash/sleep fixtures). "
                 "GPU backends provide VRAM admission accounting only; no GPU inference.")

    if args.command == "demo":
        results = {}
        for job in demo_jobs():
            result = await scheduler.run([job])
            results.update(result)
            if result[job.id]["status"] != "ok":
                break
        print(json.dumps({"results": results, "backends": [_describe(b) for b in backends],
                          "note": "Hash fixtures, not speech or language inference. " + note_exec},
                         indent=2))
    elif args.command == "benchmark":
        jobs = [Job(str(i), payload={"iterations": 80_000, "text": str(i)}, authorized=True)
                for i in range(6)]
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
            "host_concurrency": scheduler.host_concurrency,
            "identical_outputs": all(
                results[j.id].get("output", {}).get("digest") == o["digest"]
                for j, o in zip(jobs, outputs)),
            "backends_used": sorted({r.get("backend") for r in results.values()
                                     if r.get("status") == "ok"}),
            "note": "Single trial. Any speedup comes from host_concurrency, not GPU. " + note_exec,
        }, indent=2))
    elif args.command == "adaptive":
        large = min(report.get("free_vram_mib") or 4096, 8192)
        jobs = [
            Job("cpu-light", kind="hash", payload={"iterations": 10_000}, authorized=True,
                priority=1, memory_mib=32, allowed_backends=("cpu",)),
            Job("gpu-prefer", kind="hash", payload={"iterations": 20_000}, authorized=True,
                priority=5, memory_mib=64),
            Job("large-reservation", kind="hash", payload={"iterations": 5_000},
                authorized=True, priority=0, memory_mib=min(large // 4, 2048)),
        ]
        results = await scheduler.run(jobs)
        print(json.dumps({
            "discovery_summary": {
                "available_ram_mib": report.get("available_ram_mib"),
                "gpu_count": len(report.get("gpus") or []),
                "total_vram_mib": report.get("total_vram_mib"),
                "free_vram_mib": report.get("free_vram_mib"),
                "gpu_telemetry_ok": report.get("gpu_telemetry_ok"),
                "warnings": report.get("warnings"),
            },
            "host_concurrency": scheduler.host_concurrency,
            "backends": [_describe(b) for b in backends],
            "results": results,
            "note": "Prefers gpu-class backends when headroom allows, else CPU. " + note_exec,
        }, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["discover", "demo", "benchmark", "adaptive"],
                        help="discover | demo | benchmark | adaptive (auto-select backends)")
    parser.add_argument("--memory-mib", type=int, default=None, help="Logical CPU budget (MiB)")
    parser.add_argument("--concurrency", type=int, default=2,
                        help="Max concurrent slots per backend (CPU value also sets host slots)")
    parser.add_argument("--audit", default="audit.jsonl", help="JSONL audit log path")
    parser.add_argument("--mock-gpu-mib", type=int, default=None,
                        help="Add a SIMULATED GPU of this logical capacity (MiB); "
                             "named after any real GPUs (e.g. gpu1 if gpu0 is real)")
    args = parser.parse_args()
    if args.memory_mib is not None and (args.memory_mib <= 0 or args.memory_mib > MAX_REASONABLE_MIB):
        parser.error(f"--memory-mib must be in 1..{MAX_REASONABLE_MIB}")
    if args.mock_gpu_mib is not None and (args.mock_gpu_mib < 0 or args.mock_gpu_mib > MAX_REASONABLE_MIB):
        parser.error(f"--mock-gpu-mib must be in 0..{MAX_REASONABLE_MIB}")
    if args.concurrency <= 0:
        parser.error("--concurrency must be positive")
    asyncio.run(cli(args))


if __name__ == "__main__":
    main()
