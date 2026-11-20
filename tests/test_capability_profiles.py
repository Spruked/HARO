import unittest

from engine_adapters.contract import CAP_TEXT_GENERATION, WORK_ANALYSIS, WORK_CONVERSATION, WORK_REASONING
from engine_adapters.fabric import Binding, CapabilityRequest
from engine_adapters.profiles import CapabilityProfile, MetricSummary


class CapabilityProfileTests(unittest.TestCase):
    def test_unmeasured_metrics_are_null_and_owner_approval_is_explicit(self):
        profile = CapabilityProfile("adapter", "protocol", "model", approved_work_types=(WORK_REASONING,))
        self.assertIsNone(profile.metric("latency").p95)
        self.assertTrue(profile.approves(WORK_REASONING))
        self.assertFalse(profile.approves(WORK_CONVERSATION))

    def test_metric_provenance_is_validated(self):
        with self.assertRaises(ValueError):
            MetricSummary(value=1.0, source="guessed")

        for source in ("measured", "engine_reported"):
            with self.assertRaises(ValueError):
                MetricSummary(source=source, sample_count=1)
            with self.assertRaises(ValueError):
                MetricSummary(source=source, value=1.0, sample_count=0)
        with self.assertRaises(ValueError):
            MetricSummary(source="unmeasured", value=1.0)
        with self.assertRaises(ValueError):
            MetricSummary(source="measured", value=1.0, p50=3.0, p95=2.0, sample_count=1)
        with self.assertRaises(ValueError):
            MetricSummary(source="unmeasured", sample_count=1)

    def test_binding_requires_named_work_approval(self):
        with self.assertRaisesRegex(ValueError, "adapter/model"):
            Binding(CAP_TEXT_GENERATION, "adapter", "model").validate()

    def test_work_type_is_validated_and_binding_declares_approval(self):
        Binding(CAP_TEXT_GENERATION, "adapter", "model", approved_work_types=(WORK_ANALYSIS,)).validate()
        with self.assertRaises(ValueError):
            CapabilityRequest(CAP_TEXT_GENERATION, {"prompt": "x"}, "client", work_type="invented").validate()


if __name__ == "__main__":
    unittest.main()
