#!/usr/bin/env python3
"""Unit tests for the hardware-adaptive runtime orchestrator."""
import asyncio
import unittest
from haro import (
    CPUBackend,
    GPUBackend,
    Job,
    MockGPUBackend,
    Scheduler,
    build_backends,
    discover,
    MAX_REASONABLE_MIB,
)
import time

NO_GPU = {"cpu_count": 4, "available_ram_mib": 4096, "gpus": []}


def gpu_entry(i, free, uuid=None):
    return {"index": str(i), "uuid": uuid or f"GPU-{i}", "name": "Fake", "total_mib": free,
            "free_mib": free, "driver": "1"}


def report_with(*frees):
    return {**NO_GPU, "gpus": [gpu_entry(i, f) for i, f in enumerate(frees)]}


def max_overlap(events, backends=None):
    live = peak = 0
    for e in events:
        if backends and e.get("backend") not in backends:
            continue
        if e["event"] == "admitted":
            live += 1
            peak = max(peak, live)
        elif e["event"] == "completed":
            live -= 1
    return peak


class Tests(unittest.IsolatedAsyncioTestCase):
    def job(self, id="x", **kwargs):
        return Job(id, authorized=True, **kwargs)

    async def test_cpu(self):
        result = await Scheduler([CPUBackend(128)]).run([self.job()])
        self.assertEqual(result["x"]["status"], "ok")
        self.assertEqual(len(result["x"]["output"]["digest"]), 64)

    async def test_unauthorized(self):
        result = await Scheduler([CPUBackend(128)]).run([Job("x")])
        self.assertEqual(result["x"]["reason"], "authorization_required")

    async def test_oversized(self):
        result = await Scheduler([CPUBackend(32)]).run([self.job(memory_mib=64)])
        self.assertEqual(result["x"]["status"], "rejected")

    async def test_priority_and_budget(self):
        s = Scheduler([CPUBackend(64, 2)])
        await s.run([self.job("low"), self.job("high", priority=9)])
        admitted = [e for e in s.events if e["event"] == "admitted"]
        self.assertEqual(admitted[0]["job_id"], "high")
        self.assertTrue(all(e["used_mib"] <= 64 for e in admitted))

    async def test_concurrency(self):
        s = Scheduler([CPUBackend(128, 2)])
        await s.run(
            [
                self.job("a", kind="sleep", payload={"seconds": 0.05}),
                self.job("b", kind="sleep", payload={"seconds": 0.05}),
            ]
        )
        self.assertEqual([e["event"] for e in s.events[:2]], ["admitted", "admitted"])

    async def test_failure_releases(self):
        s = Scheduler([CPUBackend(64)])
        r = await s.run([self.job("a", kind="fail"), self.job("b")])
        self.assertEqual(r["a"]["status"], "error")
        self.assertEqual(r["b"]["status"], "ok")

    async def test_timeout(self):
        s = Scheduler([CPUBackend(64)])
        r = await s.run(
            [
                self.job("a", kind="sleep", payload={"seconds": 1}, timeout_s=0.02),
                self.job("b"),
            ]
        )
        self.assertEqual(r["a"]["status"], "error")
        self.assertEqual(r["b"]["status"], "ok")

    async def test_backend_restriction(self):
        r = await Scheduler([CPUBackend(128)]).run(
            [self.job(allowed_backends=("cuda",))]
        )
        self.assertEqual(r["x"]["status"], "rejected")

    async def test_duplicates(self):
        with self.assertRaises(ValueError):
            await Scheduler([CPUBackend(128)]).run([self.job(), self.job()])

    async def test_cancellation(self):
        s = Scheduler([CPUBackend(128)])
        task = asyncio.create_task(
            s.run([self.job(kind="sleep", payload={"seconds": 5})])
        )
        await asyncio.sleep(0.05)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(s.busy)
        self.assertEqual((await s.run([self.job()]))["x"]["status"], "ok")

    async def test_target(self):
        r = await Scheduler([CPUBackend(128)]).run([self.job(target_s=0.00001)])
        self.assertFalse(r["x"]["target_met"])

    def test_discover(self):
        d = discover()
        self.assertIn("gpus", d)
        self.assertIn("total_vram_mib", d)
        self.assertIn("free_vram_mib", d)
        self.assertIn("available_ram_mib", d)

    # --- New robustness / GPU adaptation tests ---

    async def test_gpu_preference(self):
        """When both CPU and GPU are present and job allows GPU, prefer GPU."""
        backends = [CPUBackend(256, 1), MockGPUBackend("gpu0", 8192, 2)]
        s = Scheduler(backends)
        r = await s.run([self.job(memory_mib=64)])
        self.assertEqual(r["x"]["status"], "ok")
        self.assertEqual(r["x"]["backend"], "gpu0")

    async def test_gpu_fallback_to_cpu(self):
        """If GPU capacity is insufficient, fall back to CPU when allowed."""
        backends = [CPUBackend(256, 1), MockGPUBackend("gpu0", 32, 1)]
        s = Scheduler(backends)
        r = await s.run([self.job(memory_mib=64)])
        self.assertEqual(r["x"]["status"], "ok")
        self.assertEqual(r["x"]["backend"], "cpu")

    async def test_large_vram_capacity(self):
        """Support multi-terabyte theoretical capacity (10 000 GB = ~10 TiB)."""
        huge = 10_000 * 1024  # 10 000 GiB in MiB
        self.assertLessEqual(huge, MAX_REASONABLE_MIB)
        backends = [CPUBackend(128, 1), MockGPUBackend("gpu0", huge, 4)]
        s = Scheduler(backends)
        # A modest job should still prefer the huge GPU.
        r = await s.run([self.job(memory_mib=512)])
        self.assertEqual(r["x"]["status"], "ok")
        self.assertEqual(r["x"]["backend"], "gpu0")

    async def test_extreme_request_rejected(self):
        """Requests beyond safety bound are rejected."""
        s = Scheduler([MockGPUBackend("gpu0", MAX_REASONABLE_MIB, 1)])
        r = await s.run([self.job(memory_mib=MAX_REASONABLE_MIB + 1)])
        self.assertEqual(r["x"]["status"], "rejected")
        self.assertEqual(r["x"]["reason"], "invalid_resource_request")

    async def test_build_backends_with_mock(self):
        backends = build_backends(report=NO_GPU, force_mock_gpu_mib=4096, concurrency=2)
        names = {b.name for b in backends}
        self.assertIn("cpu", names)
        self.assertIn("gpu0", names)
        gpu = next(b for b in backends if b.name == "gpu0")
        self.assertEqual(gpu.capacity_mib, 4096)

    async def test_multi_gpu_mock(self):
        """Multiple GPU backends + CPU; large job lands on GPU with room."""
        backends = [
            CPUBackend(128, 1),
            MockGPUBackend("gpu0", 4096, 1),
            MockGPUBackend("gpu1", 16384, 2),
        ]
        s = Scheduler(backends)
        r = await s.run([self.job(memory_mib=8000)])
        self.assertEqual(r["x"]["status"], "ok")
        self.assertEqual(r["x"]["backend"], "gpu1")

    async def test_zero_gpu_still_works(self):
        """Pure CPU environment (no mock) remains fully functional."""
        backends = build_backends(report=NO_GPU, force_mock_gpu_mib=None)
        self.assertTrue(any(b.name == "cpu" for b in backends))
        s = Scheduler(backends)
        r = await s.run([self.job()])
        self.assertEqual(r["x"]["status"], "ok")
        self.assertEqual(r["x"]["backend"], "cpu")

    async def test_gpu_backend_refresh_noop_on_mock(self):
        g = MockGPUBackend("gpu0", 8192)
        before = g.capacity_mib
        g.refresh_capacity()
        self.assertEqual(g.capacity_mib, before)

    async def test_allowed_backends_cpu_only(self):
        backends = [CPUBackend(256), MockGPUBackend("gpu0", 8192)]
        s = Scheduler(backends)
        r = await s.run([self.job(allowed_backends=("cpu",))])
        self.assertEqual(r["x"]["backend"], "cpu")


    # --- Correctness & resource-accounting pass ---

    def test_discover_injected_multi_gpu(self):
        out = "GPU-a, Fake, 24576, 20000, 550, 0\nGPU-b, Fake, 81920, 80000, 550, 1\n"
        d = discover(runner=lambda args, timeout: out)
        self.assertEqual([g["uuid"] for g in d["gpus"]], ["GPU-a", "GPU-b"])
        self.assertEqual(d["free_vram_mib"], 100000)
        self.assertTrue(d["gpu_telemetry_ok"])
        self.assertTrue(all(g["execution_capability"] == "host_fixture" for g in d["gpus"]))

    def test_discover_telemetry_unavailable(self):
        def boom(args, timeout):
            raise FileNotFoundError("nvidia-smi")
        d = discover(runner=boom)
        self.assertEqual(d["gpus"], [])
        self.assertFalse(d["gpu_telemetry_ok"])
        self.assertTrue(d["warnings"])

    def test_build_no_gpu(self):
        self.assertEqual([b.name for b in build_backends(report=NO_GPU)], ["cpu"])

    async def test_one_real_gpu(self):
        bs = build_backends(report=report_with(20000))
        gpu = next(b for b in bs if b.name == "gpu0")
        self.assertFalse(gpu.simulated)
        gpu._telemetry = lambda dev: 20000  # no host dependency
        r = await Scheduler(bs).run([self.job(memory_mib=64)])
        self.assertEqual(r["x"]["backend"], "gpu0")
        self.assertEqual(r["x"]["reserved_resource"], "device_vram")
        self.assertFalse(r["x"]["hardware_inference"])
        self.assertEqual(r["x"]["execution"], "host_fixture")

    def test_multi_real_gpu_build(self):
        names = [b.name for b in build_backends(report=report_with(8000, 16000))]
        self.assertEqual(names, ["cpu", "gpu0", "gpu1"])

    async def test_mixed_mock_and_real_unique(self):
        bs = build_backends(report=report_with(20000), force_mock_gpu_mib=4096)
        names = [b.name for b in bs]
        self.assertEqual(len(names), len(set(names)))
        mock = next(b for b in bs if isinstance(b, MockGPUBackend))
        self.assertEqual(mock.name, "gpu1")
        self.assertTrue(mock.simulated)
        Scheduler(bs)  # must not raise

    def test_duplicate_names_still_rejected(self):
        with self.assertRaises(ValueError):
            Scheduler([MockGPUBackend("gpu0"), MockGPUBackend("gpu0")])

    def test_refresh_changes_physical_not_logical(self):
        g = GPUBackend("gpu0", 1000, uuid="GPU-x", telemetry=lambda d: 300)
        g.refresh_capacity()
        self.assertEqual(g.capacity_mib, 1000)
        self.assertEqual(g.observed_free_mib, 300)
        self.assertTrue(g.telemetry_ok)

    async def test_unavailable_telemetry_keeps_last_observation(self):
        def boom(d):
            raise OSError("gone")
        g = GPUBackend("gpu0", 1000, uuid="GPU-x", telemetry=boom)
        s = Scheduler([CPUBackend(128), g])
        r = await s.run([self.job(memory_mib=64)])
        self.assertEqual(r["x"]["status"], "ok")
        self.assertEqual(g.observed_free_mib, 1000)
        self.assertFalse(g.telemetry_ok)
        self.assertTrue(any(e["event"] == "telemetry_unavailable" for e in s.events))

    async def test_refresh_is_nonblocking_and_bounded(self):
        def slow(d):
            time.sleep(0.4)
            return 1000
        g = GPUBackend("gpu0", 1000, uuid="GPU-x", telemetry=slow)
        s = Scheduler([CPUBackend(128), g], refresh_timeout_s=0.05)
        ticks = 0

        async def ticker():
            nonlocal ticks
            while True:
                ticks += 1
                await asyncio.sleep(0.01)
        t = asyncio.create_task(ticker())
        r = await s.run([self.job(memory_mib=64)])
        t.cancel()
        self.assertEqual(r["x"]["status"], "ok")
        self.assertTrue(any(e["event"] == "telemetry_timeout" for e in s.events))
        self.assertGreater(ticks, 2)  # event loop kept running during the probe

    def test_ledger_no_double_count_with_attribution(self):
        g = GPUBackend("gpu0", 2000, uuid="GPU-x", telemetry=lambda d: 1400)
        s = Scheduler([g])
        g.refresh_capacity()
        s._reserve("gpu0", "a", 600)
        # Unattributed: assume a's 600 not yet allocated -> conservative.
        self.assertEqual(s.headroom("gpu0"), (800, 800))
        # Attributed: telemetry already reflects a's 600 -> not subtracted twice.
        s._ledger["gpu0"]["a"]["allocated_mib"] = 600
        self.assertEqual(s.headroom("gpu0"), (1400, 1400))

    async def test_active_reservations_during_refresh(self):
        class Attributed(GPUBackend):
            def attributed_usage(self):
                return {"a": 600}
        g = Attributed("gpu0", 2000, uuid="GPU-x", telemetry=lambda d: 1400)
        s = Scheduler([g], refresh_interval_s=0.02, host_concurrency=2)
        g.max_concurrency = 2
        r = await s.run([
            self.job("a", kind="sleep", payload={"seconds": 0.4}, memory_mib=600, priority=1),
            self.job("b", memory_mib=1000),
        ])
        self.assertEqual(r["a"]["status"], "ok")
        self.assertEqual(r["b"]["status"], "ok")
        order = [(e["event"], e["job_id"]) for e in s.events if e["event"] in ("admitted", "completed")]
        # b admitted while a still running (after attribution), not serialized behind it.
        self.assertLess(order.index(("admitted", "b")), order.index(("completed", "a")))
        self.assertEqual(s.reserved_mib("gpu0"), 0)

    async def test_physical_shortage_reason(self):
        g = GPUBackend("gpu0", 2000, uuid="GPU-x", telemetry=lambda d: 100)
        r = await Scheduler([g]).run([self.job(memory_mib=500, allowed_backends=("gpu",))])
        self.assertEqual(r["x"]["reason"], "insufficient_physical_availability")

    async def test_host_slots_shared_with_mock_gpu(self):
        s = Scheduler([CPUBackend(256, 1), MockGPUBackend("gpu0", 8192, 2)])
        await s.run([self.job(str(i), kind="sleep", payload={"seconds": 0.05}) for i in range(3)])
        self.assertEqual(max_overlap(s.events), 1)

    async def test_capability_not_name(self):
        impostor = CPUBackend(128)
        impostor.name = "gpu_lookalike"
        r = await Scheduler([impostor]).run([self.job(allowed_backends=("gpu",))])
        self.assertEqual(r["x"]["status"], "rejected")
        r = await Scheduler([MockGPUBackend("accel0")]).run([self.job(allowed_backends=("gpu",))])
        self.assertEqual(r["x"]["backend"], "accel0")

    async def test_host_rss_not_used_for_vram_estimate(self):
        s = Scheduler([CPUBackend(256), MockGPUBackend("gpu0", 8192)])
        r = await s.run([self.job("a", memory_mib=1), self.job("b", memory_mib=1,
                                                              allowed_backends=("cpu",))])
        self.assertIsNone(r["a"]["estimate_exceeded"])  # VRAM not measured
        self.assertIsNotNone(r["a"]["host_peak_rss_mib"])
        self.assertEqual(s.estimate(self.job(memory_mib=1), "gpu0"), 1)
        self.assertGreater(s.estimate(self.job(memory_mib=1), "cpu"), 1)

    async def test_audit_distinguishes_simulated(self):
        s = Scheduler([MockGPUBackend("gpu0")])
        await s.run([self.job()])
        done = next(e for e in s.events if e["event"] == "completed")
        self.assertTrue(done["simulated"])
        self.assertFalse(done["hardware_inference"])
        self.assertEqual(done["reserved_resource"], "simulated_vram")
        self.assertIn("no GPU inference", done["output"]["backend_note"])

    def test_cuda_identity(self):
        env = GPUBackend("gpu0", 1000, uuid="GPU-abc", device_index="1")._device_env()
        self.assertEqual(env["CUDA_VISIBLE_DEVICES"], "GPU-abc")
        self.assertEqual(env["CUDA_DEVICE_ORDER"], "PCI_BUS_ID")
        self.assertEqual(MockGPUBackend()._device_env()["CUDA_VISIBLE_DEVICES"], "")

    async def test_release_after_failure_timeout_cancel(self):
        g = MockGPUBackend("gpu0", 8192, 2)
        s = Scheduler([CPUBackend(256, 2), g])
        await s.run([self.job("f", kind="fail"),
                     self.job("t", kind="sleep", payload={"seconds": 1}, timeout_s=0.02)])
        self.assertEqual((s.reserved_mib("gpu0"), s.reserved_mib("cpu"), s._host_slots_used), (0, 0, 0))
        task = asyncio.create_task(s.run([self.job("c", kind="sleep", payload={"seconds": 5})]))
        await asyncio.sleep(0.05)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual((s.reserved_mib("gpu0"), s._host_slots_used), (0, 0))


if __name__ == "__main__":
    unittest.main(verbosity=2)
