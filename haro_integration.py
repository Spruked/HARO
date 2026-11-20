"""Explicit HARO boundary between the host, A.I.M.S., and Vault Logic.

This adapter records real host events and decisions but does not schedule,
authorize, execute, or measure inference. Those remain HARO runtime duties.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from AIMS.memory_core import AIMSMemorySystem, EntryType
from HARO_Vault_System.engine.evaluator import evaluate_haro_context


class HaroSubsystems:
    def __init__(self, memory_root: Path, vault_rules: Path | None = None):
        self.memory = AIMSMemorySystem(Path(memory_root), matrix_id="haro_operational_memory")
        self.vault_rules = vault_rules

    def record_event(self, event: Mapping[str, Any], *, provenance: Mapping[str, Any]) -> dict[str, Any]:
        entry = self.memory.commit(
            EntryType.EXPERIENCE,
            content={"host": "HARO", "event": dict(event)},
            metadata={"provenance": dict(provenance), "subsystem": "haro"},
            writer_id="haro.integration",
        )
        return entry.to_dict()

    def evaluate_and_record(self, context: Mapping[str, Any], *, provenance: Mapping[str, Any]) -> dict[str, Any]:
        rules = None
        if self.vault_rules is not None:
            from HARO_Vault_System.engine.evaluator import load_haro_rules
            rules = load_haro_rules(self.vault_rules)
        evaluation = evaluate_haro_context(context, rules)
        entry = self.memory.commit(
            EntryType.DECISION,
            content={"host": "HARO", "vault_evaluation": evaluation, "context": dict(context)},
            metadata={"provenance": dict(provenance), "subsystem": "vault_logic"},
            writer_id="haro.integration",
        )
        return {"evaluation": evaluation, "evidence_entry_id": entry.entry_id}

    def retrieve_experience(self, query: str, limit: int = 20) -> dict[str, Any]:
        """Retrieve validated A.I.M.S. atoms, not raw long-term events.

        ``record_event`` commits evidence only. A caller must explicitly derive
        and reinforce an A Posteriori atom through A.I.M.S. before it becomes
        active and searchable; raw evidence remains available in the ledger.
        """
        return self.memory.retrieve(query, mode="COLLECTIVE", limit=limit)

    def close(self) -> None:
        self.memory.close()
