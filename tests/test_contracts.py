import unittest

from engine_adapters.contract import AdapterDescriptor, CAP_CHAT, LIFECYCLE_NONE, LOCATION_LOCAL
from engine_adapters.contracts import (AdmissionExplanation, DecisionRecord, LeaseRecord, ObservationRecord,
                                       ProvenanceRecord, QualityEvidenceRecord, TicketRecord)


class ContractTests(unittest.TestCase):
    def setUp(self):
        self.fingerprint = {"device_id": "gpu", "driver_version": "d", "runtime_version": "r",
                            "model_digest": "m", "quantization": "q", "context_size": "c", "binding_config_hash": "h"}
        self.provenance = ProvenanceRecord("device_measured", "test", "2026-11-20T00:00:00Z", self.fingerprint)

    def test_decision_and_explanation_are_structural(self):
        decision = DecisionRecord(request={"capability": CAP_CHAT}, reason_codes=("approved",),
                                  rules_hash="rh", verdicts=({"decision": "allow"},))
        explanation = AdmissionExplanation(decision.decision_id, "allow", ("approved",), "adapter/model")
        self.assertIn("approved", explanation.text())

    def test_ticket_legal_transitions_and_rejects_illegal(self):
        ticket = TicketRecord("t").transition("queued").transition("admitted").transition("executing")
        self.assertEqual(ticket.state, "executing")
        with self.assertRaises(ValueError):
            ticket.transition("received")

    def test_lease_generation_and_legal_transitions(self):
        lease = LeaseRecord("l", 3).transition("release_pending").transition("released", release_basis="completed_normally")
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
        observation = ObservationRecord("e", self.provenance, self.fingerprint, "all_attempts", False, "failed")
        self.assertEqual(observation.event_id, "e")
        quality = QualityEvidenceRecord("q", "task", "evaluator", 0.8, 2, self.provenance, self.fingerprint, "accuracy", "0-1")
        self.assertEqual(quality.sample_count, 2)

    def test_memory_model_is_separate_from_gating(self):
        from engine_adapters.contract import Binding
        binding = Binding(CAP_CHAT, "a", "m", approved_work_types=("conversation",),
                          memory_gating="tracked_unmeasured", memory_model="standing_service")
        binding.validate()
        self.assertEqual(binding.memory_model, "standing_service")

    def test_strict_evidence_and_decision_requirements(self):
        with self.assertRaises(ValueError):
            ProvenanceRecord("invented", "test", "now", self.fingerprint)
        with self.assertRaises(ValueError):
            ObservationRecord("e", self.provenance, {"device_id": "gpu"}, "all", True, "ok")
        with self.assertRaises(ValueError):
            DecisionRecord(test_mode=False)
        DecisionRecord(test_mode=False, reason_codes=("invalid request",))
        with self.assertRaises(ValueError):
            LeaseRecord("l", 1).transition("release_pending").transition("release_unverified").transition("released")
        with self.assertRaises(ValueError):
            QualityEvidenceRecord("q", "task", "evaluator", 0.8, 2, self.provenance, self.fingerprint, "", "0-1")

    def test_remote_voice_owner_fingerprints_and_empty_values(self):
        remote = dict(self.fingerprint, device_id="not_applicable", driver_version="not_applicable", context_size="not_applicable")
        ProvenanceRecord("engine_reported", "remote", "now", remote)
        voice = dict(self.fingerprint, device_id="gpu", driver_version="unreported", runtime_version="unreported",
                     model_digest="unreported", quantization="unreported", context_size="unreported")
        ObservationRecord("voice", ProvenanceRecord("engine_reported", "voice", "now", voice), voice, "all", True, "ok")
        owner = dict(self.fingerprint, device_id="not_applicable", driver_version="not_applicable", runtime_version="not_applicable",
                     model_digest="not_applicable", quantization="not_applicable", context_size="not_applicable")
        ProvenanceRecord("owner_declared", "owner", "now", owner)
        with self.assertRaises(ValueError):
            ProvenanceRecord("owner_declared", "owner", "now", dict(owner, device_id=""))

    def test_unmeasured_explanation_is_explicit(self):
        text = AdmissionExplanation("d", "refuse").text()
        self.assertIn("required memory unmeasured (unmeasured)", text)


if __name__ == "__main__":
    unittest.main()
