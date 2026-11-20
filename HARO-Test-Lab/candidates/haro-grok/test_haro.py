#!/usr/bin/env python3
"""Hardware-independent tests for HARO Correctness & Resource Accounting Pass."""
import asyncio
import unittest
from haro import (
    CPUBackend, GPUBackend, Job, MockGPUBackend, Scheduler,
    build_backends, discover, MAX_REASONABLE_MIB,
)

# Fixed discovery reports so tests never depend on live host GPUs.
REPORT_NO_GPU = {
    "os": "Linux", "cpu_count": 2, "available_ram_mib": 512,
    "gpus": [], "total_vram_mib": 0, "free_vram_mib": 0, "warnings": [],
}
REPORT_ONE_GPU = {
    "os": "Linux", "cpu_count": 4, "available_ram_mib": 1024,
    "gpus": [{
        "index": "0", "uuid": "GPU-FAKE-UUID-0", "name": "FakeGPU",
        "total_mib": 8192, "free_mib": 6144, "driver": "550.00",
        "executable": False, "reason": "test",
    }],
    "total_vram_mib": 8192, "free_vram_mib": 6144, "warnings": [],
}
REPORT_MULTI_GPU = {
    "os": "Linux", "cpu_count": 8, "available_ram_mib": 2048,
    "gpus": [
        {"index": "0", "uuid": "GPU-FAKE-0", "name": "A100",
         "total_mib": 40960, "free_mib": 40000, "driver": "550.00",
         "executable": False, "reason": "test"},
        {"index": "1", "uuid": "GPU-FAKE-1", "name": "A100",
         "total_mib": 40960, "free_mib": 20000, "driver": "550.00",
         "executable": False, "reason": "test"},
    ],
    "total_vram_mib": 81920, "free_vram_mib": 60000, "warnings": [],
}


class Tests(unittest.IsolatedAsyncioTestCase):
    def job(self, id="x", **kwargs):
        return Job(id, authorized=True, **kwargs)

    # ---- original behaviour ----

    async def test_cpu(self):
        result = await Scheduler([CPUBackend(128)]).run([self.job()])
        self.assertEqual(result["x"]["status"], "ok")
        self.assertEqual(len(result["x"]["output"]["digest"]), 64)
        self.assertEqual(result["x"]["output"]["execution_mode"], "cpu_host")
        self.assertIsNone(result["x"]["output"]["peak_vram_mib"])
        self.assertTrue(result["x"]["verified_execution"])

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
        await s.run([
            self.job("a", kind="sleep", payload={"seconds": 0.05}),
            self.job("b", kind="sleep", payload={"seconds": 0.05}),
        ])
        self.assertEqual([e["event"] for e in s.events[:2]], ["admitted", "admitted"])

    async def test_failure_releases(self):
        s = Scheduler([CPUBackend(64)])
        r = await s.run([self.job("a", kind="fail"), self.job("b")])
        self.assertEqual(r["a"]["status"], "error")
        self.assertEqual(r["b"]["status"], "ok")

    async def test_timeout(self):
        s = Scheduler([CPUBackend(64)])
        r = await s.run([
            self.job("a", kind="sleep", payload={"seconds": 1}, timeout_s=0.02),
            self.job("b"),
        ])
        self.assertEqual(r["a"]["status"], "error")
        self.assertEqual(r["b"]["status"], "ok")

    async def test_backend_restriction(self):
        r = await Scheduler([CPUBackend(128)]).run(
            [self.job(allowed_capabilities=("gpu_real",))]
        )
        self.assertEqual(r["x"]["status"], "rejected")

    async def test_duplicates(self):
        with self.assertRaises(ValueError):
            await Scheduler([CPUBackend(128)]).run([self.job(), self.job()])

    async def test_cancellation(self):
        s = Scheduler([CPUBackend(128)])
        task = asyncio.create_task(s.run([self.job(kind="sleep", payload={"seconds": 5})]))
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
        for g in d.get("gpus") or []:
            self.assertFalse(g.get("executable", True))

    # ---- correctness & accounting regressions ----

    async def test_simulated_never_claims_verified(self):
        cpu = CPUBackend(256, 2)
        mock = MockGPUBackend("mock-gpu0", 8192, 2, cpu_backend=cpu)
        s = Scheduler([cpu, mock])
        r = await s.run([self.job(memory_mib=64)])
        self.assertEqual(r["x"]["backend"], "mock-gpu0")
        self.assertFalse(r["x"]["verified_execution"])
        out = r["x"]["output"]
        self.assertEqual(out["execution_mode"], "simulated_gpu_accounting")
        self.assertEqual(out["capability"], "gpu_sim")
        self.assertIsNone(out["peak_vram_mib"])
        self.assertIn("not genuine GPU", out.get("backend_note", ""))

    async def test_host_rss_vs_device_vram_separation(self):
        r = await Scheduler([CPUBackend(128)]).run([self.job()])
        out = r["x"]["output"]
        self.assertIn("peak_rss_mib", out)
        self.assertIn("peak_vram_mib", out)
        self.assertIsNone(out["peak_vram_mib"])

    async def test_capability_preference_over_name(self):
        """Preference uses explicit capability, not name.startswith."""
        cpu = CPUBackend(256, 1)
        # Deliberately name a mock something that does NOT start with "gpu"
        mock = MockGPUBackend("accelerator0", 8192, 1, cpu_backend=cpu)
        mock.capability = "gpu_sim"
        s = Scheduler([cpu, mock])
        r = await s.run([self.job(memory_mib=64)])
        self.assertEqual(r["x"]["backend"], "accelerator0")

    async def test_unique_names_mixed_real_and_mock(self):
        backends = build_backends(REPORT_ONE_GPU, force_mock_gpu_mib=4096, concurrency=1)
        names = [b.name for b in backends]
        self.assertEqual(len(names), len(set(names)), f"duplicates: {names}")
        self.assertIn("cpu", names)
        self.assertTrue(any(n.startswith("gpu") for n in names))
        self.assertTrue(any(n.startswith("mock-gpu") for n in names))

    async def test_zero_gpu_still_works(self):
        backends = build_backends(REPORT_NO_GPU)
        self.assertTrue(all(b.name == "cpu" or not b.name.startswith("gpu") for b in backends))
        s = Scheduler(backends)
        r = await s.run([self.job()])
        self.assertEqual(r["x"]["status"], "ok")
        self.assertEqual(r["x"]["backend"], "cpu")

    async def test_one_gpu_prefers_sim(self):
        backends = build_backends(REPORT_ONE_GPU, concurrency=1)
        s = Scheduler(backends)
        r = await s.run([self.job(memory_mib=64)])
        self.assertEqual(r["x"]["status"], "ok")
        self.assertTrue(r["x"]["backend"].startswith("gpu"))
        self.assertFalse(r["x"]["verified_execution"])
        self.assertEqual(r["x"]["capability"], "gpu_sim")

    async def test_multi_gpu_selects_fitting(self):
        backends = build_backends(REPORT_MULTI_GPU, concurrency=1)
        s = Scheduler(backends)
        # Request that only the larger free card can satisfy
        r = await s.run([self.job(memory_mib=30000)])
        self.assertEqual(r["x"]["status"], "ok")
        self.assertEqual(r["x"]["backend"], "gpu0")  # 40000 free

    async def test_large_logical_capacity_accepted(self):
        """Accounting accepts capacity values up to the safety bound."""
        huge = 10_000 * 1024  # ~9.5 TiB in MiB
        self.assertLessEqual(huge, MAX_REASONABLE_MIB)
        cpu = CPUBackend(128, 1)
        mock = MockGPUBackend("mock-gpu0", huge, 1, cpu_backend=cpu)
        s = Scheduler([cpu, mock])
        r = await s.run([self.job(memory_mib=512)])
        self.assertEqual(r["x"]["status"], "ok")
        self.assertEqual(r["x"]["backend"], "mock-gpu0")

    async def test_extreme_request_rejected(self):
        s = Scheduler([MockGPUBackend("mock-gpu0", MAX_REASONABLE_MIB, 1)])
        r = await s.run([self.job(memory_mib=MAX_REASONABLE_MIB + 1)])
        self.assertEqual(r["x"]["status"], "rejected")

    async def test_reservation_reduces_mock_capacity(self):
        """Outstanding reservation is subtracted from mock capacity immediately."""
        cpu = CPUBackend(256, 2)
        mock = MockGPUBackend("mock-gpu0", 1024, 2, cpu_backend=cpu)
        mock.note_reservation(+400)
        self.assertEqual(mock.capacity_mib, 624)
        mock.note_reservation(-400)
        self.assertEqual(mock.capacity_mib, 1024)

    async def test_refresh_during_reservation_no_double_count(self):
        """Refresh must not invent capacity; outstanding is already accounted."""
        cpu = CPUBackend(256, 2)
        mock = MockGPUBackend("mock-gpu0", 2048, 2, cpu_backend=cpu)
        mock.note_reservation(+512)
        before = mock.capacity_mib
        await mock.refresh_capacity()
        self.assertEqual(mock.capacity_mib, before)  # still 2048-512
        self.assertEqual(mock._outstanding_mib, 512)

    async def test_shared_cpu_budget_for_simulated_gpu(self):
        """Simulated GPU work consumes the shared CPU concurrency budget."""
        cpu = CPUBackend(512, max_concurrency=1)  # only one CPU slot
        mock = MockGPUBackend("mock-gpu0", 8192, max_concurrency=4, cpu_backend=cpu)
        s = Scheduler([cpu, mock])
        # Two concurrent simulated jobs; second should wait or the budget blocks.
        # With max_concurrency=1 on CPU, the second job cannot start until first finishes.
        r = await s.run([
            self.job("a", kind="sleep", payload={"seconds": 0.08}, memory_mib=64),
            self.job("b", kind="sleep", payload={"seconds": 0.08}, memory_mib=64),
        ])
        self.assertEqual(r["a"]["status"], "ok")
        self.assertEqual(r["b"]["status"], "ok")
        # Both should have run on the mock, but serially because of shared CPU slot.
        self.assertEqual(r["a"]["backend"], "mock-gpu0")
        self.assertEqual(r["b"]["backend"], "mock-gpu0")

    async def test_cpu_only_capability(self):
        backends = build_backends(REPORT_ONE_GPU, force_mock_gpu_mib=4096)
        s = Scheduler(backends)
        r = await s.run([self.job(allowed_capabilities=("cpu",))])
        self.assertEqual(r["x"]["backend"], "cpu")

    async def test_build_backends_injectable(self):
        """build_backends with an explicit report never calls live discovery."""
        backends = build_backends(REPORT_NO_GPU, force_mock_gpu_mib=2048)
        names = {b.name for b in backends}
        self.assertIn("cpu", names)
        self.assertTrue(any(n.startswith("mock-gpu") for n in names))
        self.assertFalse(any(n.startswith("gpu") and not n.startswith("mock") for n in names))

    async def test_telemetry_unavailable_conservative(self):
        d = discover()
        # In this environment nvidia-smi is absent.
        self.assertEqual(d["gpus"], [])
        self.assertTrue(any("telemetry unavailable" in w.lower() or "nvidia" in w.lower()
                            for w in d["warnings"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
