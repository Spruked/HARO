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
            self.assertEqual(system.retrieve_experience("resource_observation")["result_count"], 0)
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
            self.assertGreaterEqual(reopened.retrieve_experience("resource_observation")["result_count"], 1)
            reopened.close()

    def test_vault_allows_safe_context_and_denies_unsafe_or_missing(self):
        safe = evaluate_haro_context({"capability": "llama", "requested_memory_mib": 512, "available_memory_mib": 1024})
        self.assertTrue(safe["allowed"])
        self.assertTrue(safe["matched_rules"])

        with tempfile.TemporaryDirectory() as directory:
            system = HaroSubsystems(Path(directory))
            integrated = system.evaluate_and_record(
                {"capability": "llama", "requested_memory_mib": 512, "available_memory_mib": 1024},
                provenance={"source": "isolated-test", "operation_id": "op-2"},
            )
            self.assertTrue(integrated["evaluation"]["glyph_trace"] == [])
            self.assertTrue(integrated["evaluation"]["memory_matrix_key"])
            self.assertTrue(integrated["evaluation"]["telemetry_recorded"])
            self.assertTrue((Path(directory) / "vault_logic_matrix.json").exists())
            self.assertTrue((Path(directory) / "vault_logic_telemetry.jsonl").exists())
            system.close()

    def test_every_early_denial_is_audited_and_safety_types_are_strict(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            system = HaroSubsystems(root)
            contexts = [
                {"capability": "llama", "requested_memory_mib": 6800, "available_memory_mib": 751},
                {"capability": "llama"},
                {"capability": "llama", "requested_memory_mib": 512, "available_memory_mib": 1024, "requires_model_residency": True},
                {"capability": "llama", "requested_memory_mib": 512, "available_memory_mib": 1024, "unsafe": "true"},
                {"requested_memory_mib": 512, "available_memory_mib": 1024},
            ]
            for context in contexts:
                result = evaluate_haro_context(context, memory_matrix=system.vault_memory, telemetry=system.telemetry)
                self.assertFalse(result["allowed"])
                self.assertIn("glyph_trace", result)
                self.assertTrue(result["telemetry_recorded"])
            self.assertEqual(len((root / "vault_logic_telemetry.jsonl").read_text(encoding="utf-8").splitlines()), len(contexts))
            self.assertEqual(len(system.vault_memory._read()["matrices"]["haro_evaluations"]), len(contexts))
            system.close()

        unsafe = evaluate_haro_context({"capability": "llama", "requested_memory_mib": 512, "available_memory_mib": 1024, "unsafe": True})
        self.assertFalse(unsafe["allowed"])
        self.assertEqual(unsafe["decision"], "deny")

        missing = evaluate_haro_context({"requested_memory_mib": 512, "available_memory_mib": 1024})
        self.assertEqual(missing["decision"], "deny")

        oversized = evaluate_haro_context({"capability": "llama", "requested_memory_mib": 2048, "available_memory_mib": 1024})
        self.assertEqual(oversized["decision"], "deny")

        invalid = evaluate_haro_context({"capability": "llama", "requested_memory_mib": "512", "available_memory_mib": 1024})
        self.assertEqual(invalid["decision"], "deny")

        residency = evaluate_haro_context({"capability": "llama", "requested_memory_mib": 512, "available_memory_mib": 1024, "requires_model_residency": True})
        self.assertEqual(residency["decision"], "deny")

    def test_explicit_deny_precedes_allow_and_conflicting_allows_are_unresolved(self):
        result = evaluate_haro_context(
            {"capability": "llama", "requested_memory_mib": 512, "available_memory_mib": 1024},
            [
                {"id": "allow", "domain": "test", "when": {"capability_present": True}, "action": "allow"},
                {"id": "deny", "domain": "test", "when": {"capability_present": True}, "action": "deny"},
            ],
        )
        self.assertEqual(result["decision"], "deny")
        self.assertFalse(result["allowed"])

        unresolved = evaluate_haro_context(
            {"capability": "llama", "requested_memory_mib": 512, "available_memory_mib": 1024},
            [
                {"id": "allow-a", "domain": "test", "when": {"capability_present": True}, "action": "allow"},
                {"id": "allow-b", "domain": "test", "when": {"capability_present": True}, "action": "allow"},
            ],
        )
        self.assertEqual(unresolved["decision"], "unresolved")
        self.assertFalse(unresolved["allowed"])

        malformed = evaluate_haro_context(
            {"capability": "llama", "requested_memory_mib": 512, "available_memory_mib": 1024},
            [{"id": "bad", "domain": "test", "when": {}, "action": "approve"}],
        )
        self.assertEqual(malformed["decision"], "unresolved")
        self.assertFalse(malformed["allowed"])


if __name__ == "__main__":
    unittest.main()
