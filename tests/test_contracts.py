import unittest

from engine_adapters.contract import AdapterDescriptor, CAP_CHAT, LIFECYCLE_NONE, LOCATION_LOCAL
from engine_adapters.contracts import (AdmissionExplanation, DecisionRecord, LeaseRecord, ObservationRecord,
                                       ProvenanceRecord, QualityEvidenceRecord, TicketRecord)


class ContractTests(unittest.TestCase):
    def setUp(self):
        self.provenance = ProvenanceRecord("device_measured", "test", "2026-11-20T00:00:00Z", {"device": "gpu"})

    def test_decision_and_explanation_are_structural(self):
        decision = DecisionRecord(request={"capability": CAP_CHAT}, reason_codes=("approved",))
        explanation = AdmissionExplanation(decision.decision_id, "allow", ("approved",), "adapter/model")
        self.assertIn("approved", explanation.text())

    def test_ticket_legal_transitions_and_rejects_illegal(self):
        ticket = TicketRecord("t").transition("queued").transition("admitted").transition("executing")
        self.assertEqual(ticket.state, "executing")
        with self.assertRaises(ValueError):
            ticket.transition("received")

    def test_lease_generation_and_legal_transitions(self):
        lease = LeaseRecord("l", 3).transition("release_pending").transition("release_unverified").transition("release_attested").transition("released")
        self.assertEqual((lease.generation, lease.state), (3, "released"))
        with self.assertRaises(ValueError):
            lease.transition("held")

    def test_adapter_cancellation_semantics_are_complete(self):
        descriptor = AdapterDescriptor("a", "test", LOCATION_LOCAL, LIFECYCLE_NONE, (CAP_CHAT,), True, True, False)
        descriptor.validate()
        with self.assertRaises(ValueError):
            AdapterDescriptor("a", "test", LOCATION_LOCAL, LIFECYCLE_NONE, (CAP_CHAT,), True, True, False,
                              cancellation_semantics=(("requested", "verified"),)).validate()

    def test_observation_and_quality_evidence_validate(self):
        observation = ObservationRecord("e", self.provenance, {"device": "gpu"}, "all_attempts", False, "failed")
        self.assertEqual(observation.event_id, "e")
        quality = QualityEvidenceRecord("q", "task", "evaluator", 0.8, 2, self.provenance, {"device": "gpu"})
        self.assertEqual(quality.sample_count, 2)

    def test_memory_model_is_separate_from_gating(self):
        from engine_adapters.contract import Binding
        binding = Binding(CAP_CHAT, "a", "m", approved_work_types=("conversation",),
                          memory_gating="tracked_unmeasured", memory_model="standing_service")
        binding.validate()
        self.assertEqual(binding.memory_model, "standing_service")


if __name__ == "__main__":
    unittest.main()
