import time
import unittest

from engine_adapters.contract import AdapterDescriptor, CAP_TEXT_GENERATION, LIFECYCLE_NONE, LOCATION_LOCAL, LOCATION_REMOTE, WORK_ANALYSIS, WORK_CONVERSATION
from engine_adapters.fabric import Binding
from engine_adapters.profiles import CapabilityProfile, MetricSummary
from engine_adapters.resolver import CapabilityResolver, ResolutionRequest


class Adapter:
    def __init__(self, adapter_id, location=LOCATION_LOCAL, device_id=None):
        self.adapter_id, self.location, self.device_id = adapter_id, location, device_id

    def describe(self):
        return AdapterDescriptor(self.adapter_id, "test", self.location, LIFECYCLE_NONE, (CAP_TEXT_GENERATION,), False, True, False, self.device_id)


class ResolverTests(unittest.TestCase):
    def setUp(self):
        self.adapters = {"local": Adapter("local", device_id="gpu-1"), "remote": Adapter("remote", LOCATION_REMOTE)}

    def test_filters_work_type_privacy_memory_and_health_with_reasons(self):
        bindings = [
            Binding(CAP_TEXT_GENERATION, "local", "small", resident_mib=400, transient_mib=100, approved_work_types=(WORK_ANALYSIS,)),
            Binding(CAP_TEXT_GENERATION, "remote", "cloud", approved_work_types=(WORK_CONVERSATION,)),
        ]
        resolver = CapabilityResolver(self.adapters, bindings, live_available_mib=lambda _: 450, health={"remote": True})
        result = resolver.resolve(ResolutionRequest(CAP_TEXT_GENERATION, WORK_CONVERSATION, privacy="local_only"))
        self.assertEqual(len(result.eligible), 0)
        self.assertTrue(any("work type" in reason for reason in result.candidates[0].reasons))
        self.assertTrue(any("remote" in reason for reason in result.candidates[1].reasons))

    def test_measured_profile_ranks_before_unmeasured_and_cooldown_excludes(self):
        bindings = [
            Binding(CAP_TEXT_GENERATION, "local", "fast", resident_mib=100, transient_mib=50),
            Binding(CAP_TEXT_GENERATION, "local", "unknown", resident_mib=100, transient_mib=50),
        ]
        profiles = {
            ("local", "fast"): CapabilityProfile("local", "test", "fast", approved_work_types=(WORK_CONVERSATION,), quality_tier="high", metrics={"first_output_latency": MetricSummary(p95=0.2, sample_count=3, source="measured")}),
            ("local", "unknown"): CapabilityProfile("local", "test", "unknown", approved_work_types=(WORK_CONVERSATION,), cooldown_until=time.time() + 60),
        }
        result = CapabilityResolver(self.adapters, bindings, profiles, live_available_mib=lambda _: 1000).resolve(ResolutionRequest(CAP_TEXT_GENERATION))
        self.assertEqual(result.eligible[0].model, "fast")
        self.assertFalse(result.candidates[-1].eligible)
        self.assertIn("cooldown", result.candidates[-1].reasons[0])


if __name__ == "__main__":
    unittest.main()
