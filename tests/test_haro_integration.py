import tempfile
import unittest
from pathlib import Path

from haro_integration import HaroSubsystems
from HARO_Vault_System.engine.evaluator import evaluate_haro_context


class HaroIntegrationTests(unittest.TestCase):
    def test_event_survives_restart_and_retrieval_is_receipted(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            system = HaroSubsystems(root)
            event = system.record_event(
                {"kind": "resource_observation", "gpu": "test-gpu", "free_memory_mib": 4096},
                provenance={"source": "isolated-test", "operation_id": "op-1"},
            )
            committed = system.memory.long_term.read_entry(event["entry_id"])
            atom = system.memory.derive_aposteriori("resource_observation test-gpu", [committed], initial_confidence=0.5)
            for _ in range(3):
                system.memory.vault.reinforce(atom.atom_id, weight=0.1)
            system.memory.indexes.rebuild(system.memory.vault)
            result = system.retrieve_experience("resource_observation")
            self.assertGreaterEqual(result["result_count"], 1)
            receipt_id = result["retrieval_id"]
            system.close()

            reopened = HaroSubsystems(root)
            self.assertGreaterEqual(len(reopened.memory.long_term.entries), 1)
            self.assertIsNotNone(reopened.memory.retrieval(receipt_id))
            reopened.close()

    def test_vault_allows_safe_context_and_denies_unsafe_or_missing(self):
        safe = evaluate_haro_context({"capability": "llama", "requested_memory_mib": 512, "available_memory_mib": 1024})
        self.assertTrue(safe["allowed"])
        self.assertTrue(safe["matched_rules"])

        unsafe = evaluate_haro_context({"capability": "llama", "unsafe": True})
        self.assertFalse(unsafe["allowed"])
        self.assertEqual(unsafe["decision"], "deny")

        missing = evaluate_haro_context({"requested_memory_mib": 512, "available_memory_mib": 1024})
        self.assertEqual(missing["decision"], "deny")

    def test_conflicting_rules_are_reported_not_silently_approved(self):
        result = evaluate_haro_context(
            {"capability": "llama"},
            [
                {"id": "allow", "domain": "test", "when": {"capability_present": True}, "action": "allow"},
                {"id": "deny", "domain": "test", "when": {"capability_present": True}, "action": "deny"},
            ],
        )
        self.assertEqual(result["decision"], "conflict")
        self.assertFalse(result["allowed"])


if __name__ == "__main__":
    unittest.main()
