"""Vendor-neutral accelerator telemetry with an NVIDIA (nvidia-smi) provider.

HARO asks "what devices and how much memory?" through ``DeviceTelemetry``; it
never branches on vendor. Additional providers (AMD, Intel, Apple, NPUs) plug
in by implementing the same two methods.

All figures are what the driver reported. Whole-device figures include every
process on the device, so a before/after difference is labelled a device delta,
not an attribution to one model.
"""
from __future__ import annotations

import csv
import subprocess
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Callable, Protocol

MAX_REASONABLE_MIB = 20 * 1024 * 1024


@dataclass(frozen=True)
class DeviceSnapshot:
    device_id: str          # stable identity (UUID for NVIDIA)
    index: str
    name: str
    vendor: str
    total_mib: int | None
    used_mib: int | None
    free_mib: int | None
    driver: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


class DeviceTelemetry(Protocol):
    vendor: str

    def devices(self) -> list[DeviceSnapshot]: ...

    def process_memory_mib(self) -> dict[int, int] | None:
        """Per-PID device memory if the driver attributes it, else None."""
        ...


Runner = Callable[[list[str], float], str]


def _run_nvidia_smi(args: list[str], timeout: float) -> str:
    result = subprocess.run(["nvidia-smi", *args], capture_output=True, text=True,
                            timeout=timeout, check=True)
    return result.stdout


def _as_mib(value: str) -> int | None:
    value = value.strip()
    if not value.isdecimal():
        return None          # "[N/A]", "[Not Supported]" on some Windows/WDDM setups
    number = int(value)
    return number if 0 <= number <= MAX_REASONABLE_MIB else None


class NvidiaSmiTelemetry:
    vendor = "nvidia"
    QUERY = "index,uuid,name,memory.total,memory.used,memory.free,driver_version"

    def __init__(self, runner: Runner | None = None, timeout_s: float = 4.0):
        self._run = runner or _run_nvidia_smi
        self.timeout_s = timeout_s

    def devices(self) -> list[DeviceSnapshot]:
        out = self._run([f"--query-gpu={self.QUERY}", "--format=csv,noheader,nounits"], self.timeout_s)
        snapshots = []
        for row in csv.reader(line for line in out.splitlines() if line.strip()):
            if len(row) != 7:
                raise ValueError(f"unexpected nvidia-smi row with {len(row)} fields")
            index, uuid, name, total, used, free, driver = (x.strip() for x in row)
            snapshots.append(DeviceSnapshot(uuid, index, name, self.vendor, _as_mib(total),
                                            _as_mib(used), _as_mib(free), driver))
        return snapshots

    def process_memory_mib(self) -> dict[int, int] | None:
        try:
            out = self._run(["--query-compute-apps=pid,used_memory", "--format=csv,noheader,nounits"],
                            self.timeout_s)
        except (OSError, subprocess.SubprocessError):
            return None
        usage: dict[int, int] = {}
        for row in csv.reader(line for line in out.splitlines() if line.strip()):
            if len(row) != 2:
                return None
            pid, mib = row[0].strip(), _as_mib(row[1])
            if not pid.isdecimal() or mib is None:
                return None    # driver does not attribute memory per process
            usage[int(pid)] = usage.get(int(pid), 0) + mib
        return usage


def find_device(telemetry: DeviceTelemetry, device_id: str) -> DeviceSnapshot | None:
    for snap in telemetry.devices():
        if device_id in (snap.device_id, snap.index):
            return snap
    return None


@dataclass
class DeviceSampleSeries:
    device_id: str
    interval_s: float
    samples: list[tuple[float, int]] = field(default_factory=list)   # (monotonic_s, used_mib)
    errors: list[str] = field(default_factory=list)

    def summary(self) -> dict:
        used = [u for _, u in self.samples]
        return {
            "device_id": self.device_id,
            "sample_count": len(used),
            "interval_s": self.interval_s,
            "min_used_mib": min(used) if used else None,
            "max_used_mib": max(used) if used else None,
            "first_used_mib": used[0] if used else None,
            "last_used_mib": used[-1] if used else None,
            "error_count": len(self.errors),
            "errors": self.errors[:5],
            "kind": "whole_device",
            "note": "Whole-device memory; includes other processes; sampling can miss short peaks.",
        }


class DeviceSampler:
    """Background sampler of one device's used memory, for before/peak/after figures."""

    def __init__(self, telemetry: DeviceTelemetry, device_id: str, interval_s: float = 0.25):
        if interval_s < 0.05:
            raise ValueError("interval_s must be >= 0.05")
        self.telemetry = telemetry
        self.series = DeviceSampleSeries(device_id, interval_s)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def sample_once(self) -> int | None:
        try:
            snap = find_device(self.telemetry, self.series.device_id)
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            self.series.errors.append(f"{type(exc).__name__}: {exc}")
            return None
        if snap is None or snap.used_mib is None:
            self.series.errors.append("device absent or used memory not reported")
            return None
        self.series.samples.append((time.monotonic(), snap.used_mib))
        return snap.used_mib

    def __enter__(self) -> "DeviceSampler":
        self.sample_once()

        def loop() -> None:
            while not self._stop.wait(self.series.interval_s):
                self.sample_once()

        self._thread = threading.Thread(target=loop, name="haro-device-sampler", daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self.sample_once()
