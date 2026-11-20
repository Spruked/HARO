"""Deterministic Vault Logic evaluation for the HARO host deployment.

The template remains a complete Vault Logic system.  This module supplies a
small host-facing evaluator whose rules are data-driven and whose result never
authorizes execution.  HARO still owns live resource admission and scheduling.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Iterable, Mapping


DEFAULT_RULES_PATH = Path(__file__).parents[1] / "vaults" / "haro" / "rules.json"


def load_haro_rules(path: Path = DEFAULT_RULES_PATH) -> list[dict[str, Any]]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    rules = payload.get("rules")
    if not isinstance(rules, list) or not rules:
        raise ValueError("HARO rule file must contain a non-empty rules list")
    return rules


def _matches(rule: Mapping[str, Any], context: Mapping[str, Any]) -> bool:
    conditions = rule.get("when", {})
    if not isinstance(conditions, Mapping):
        raise ValueError("rule.when must be an object")
    for key, expected in conditions.items():
        actual = context.get(key)
        if key == "requested_memory_mib_lte_available":
            if "requested_memory_mib" not in context or "available_memory_mib" not in context:
                return False
            if context["requested_memory_mib"] > context["available_memory_mib"]:
                return False
        elif key == "capability_present":
            if expected and context.get("capability") in (None, ""):
                return False
        elif actual != expected:
            return False
    return True


def evaluate_haro_context(
    context: Mapping[str, Any], rules: Iterable[Mapping[str, Any]] | None = None
) -> dict[str, Any]:
    """Evaluate a HARO operational context using active Vault Logic rules."""
    started = time.perf_counter()
    active_rules = list(rules) if rules is not None else load_haro_rules()
    if context.get("capability") in (None, ""):
        return {
            "decision": "deny",
            "allowed": False,
            "reason": "HARO operation has no declared capability",
            "matched_rules": [],
            "telemetry": {"evaluation_ms": round((time.perf_counter() - started) * 1000, 3), "rule_count": len(active_rules)},
            "execution_authority": "HARO host; Vault Logic does not execute or admit resources",
        }
    matched = [rule for rule in active_rules if _matches(rule, context)]
    highest_priority = max((int(rule.get("priority", 0)) for rule in matched), default=0)
    selected = [rule for rule in matched if int(rule.get("priority", 0)) == highest_priority]
    actions = {rule.get("action") for rule in selected}
    if len(actions) > 1:
        decision = "conflict"
        reason = "multiple applicable rules returned different actions"
    elif matched:
        decision = str(selected[0].get("action", "deny"))
        reason = str(selected[0].get("reason", "rule matched"))
    else:
        decision = "deny"
        reason = "no applicable HARO rule; evaluation is fail-closed"
    trace = [
        {"rule_id": rule.get("id"), "domain": rule.get("domain"), "action": rule.get("action")}
        for rule in matched
    ]
    return {
        "decision": decision,
        "allowed": decision == "allow",
        "reason": reason,
        "matched_rules": trace,
        "telemetry": {"evaluation_ms": round((time.perf_counter() - started) * 1000, 3), "rule_count": len(active_rules)},
        "execution_authority": "HARO host; Vault Logic does not execute or admit resources",
    }


def evaluate_condition(input_data):
    """Compatibility entry point routed through the real HARO evaluator."""
    return evaluate_haro_context(input_data)


def query_memory(key):
    """Legacy compatibility helper; memory belongs to A.I.M.S., not this engine."""
    return False if not key else None


def check_drift(current, previous):
    """Legacy compatibility helper; no fabricated drift score is returned."""
    if current == previous:
        return 0.0
    return None
