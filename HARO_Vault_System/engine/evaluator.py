"""Deterministic Vault Logic evaluation for the HARO host deployment.

The template remains a complete Vault Logic system.  This module supplies a
small host-facing evaluator whose rules are data-driven and whose result never
authorizes execution.  HARO still owns live resource admission and scheduling.
"""

from __future__ import annotations

import json
import math
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


def _validate_rules(rules: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    validated = []
    for rule in rules:
        if not isinstance(rule, Mapping) or not rule.get("id") or not rule.get("domain"):
            raise ValueError("every HARO rule requires id and domain")
        if rule.get("action") not in {"allow", "deny"}:
            raise ValueError("HARO rules support only allow or deny actions")
        if not isinstance(rule.get("priority", 0), int) or isinstance(rule.get("priority", 0), bool):
            raise ValueError("HARO rule priority must be an integer")
        if not isinstance(rule.get("when", {}), Mapping):
            raise ValueError("rule.when must be an object")
        validated.append(dict(rule))
    if not validated:
        raise ValueError("HARO rules must not be empty")
    return validated


def _deny(reason: str, started: float, rule_count: int, *, decision: str = "deny") -> dict[str, Any]:
    return {
        "decision": decision, "allowed": False, "reason": reason, "matched_rules": [],
        "telemetry": {"evaluation_ms": round((time.perf_counter() - started) * 1000, 3), "rule_count": rule_count},
        "execution_authority": "HARO host; Vault Logic does not execute or admit resources",
    }


def evaluate_haro_context(
    context: Mapping[str, Any], rules: Iterable[Mapping[str, Any]] | None = None,
    *, memory_matrix: Any = None, telemetry: Any = None,
) -> dict[str, Any]:
    """Evaluate a HARO operational context using active Vault Logic rules."""
    started = time.perf_counter()
    try:
        active_rules = _validate_rules(list(rules) if rules is not None else load_haro_rules())
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        return _deny(f"invalid HARO rule set: {error}", started, 0, decision="unresolved")
    if context.get("capability") in (None, ""):
        return _deny("HARO operation has no declared capability", started, len(active_rules))
    for key in ("requested_memory_mib", "available_memory_mib"):
        value = context.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
            return _deny(f"missing or invalid numeric resource measurement: {key}", started, len(active_rules))
    if context.get("requires_model_residency") and not isinstance(context.get("model_resident"), bool):
        return _deny("required model-residency evidence is missing or invalid", started, len(active_rules))
    if context["requested_memory_mib"] > context["available_memory_mib"]:
        return _deny("requested memory exceeds available memory", started, len(active_rules))
    matched = [rule for rule in active_rules if _matches(rule, context)]
    denied = [rule for rule in matched if rule.get("action") == "deny"]
    if denied:
        decision, reason = "deny", str(denied[0].get("reason", "explicit deny rule matched"))
    else:
        mandatory = [rule for rule in active_rules if rule.get("mandatory")]
        missing = [rule for rule in mandatory if rule not in matched]
        if missing:
            return _deny("mandatory HARO conditions were not satisfied", started, len(active_rules))
        actions = {rule.get("action") for rule in matched}
        priorities = {int(rule.get("priority", 0)) for rule in matched}
        if len(matched) > 1 and len(priorities) == 1:
            decision, reason = "unresolved", "conflicting applicable rules"
        elif matched and actions == {"allow"}:
            decision, reason = "allow", str(matched[0].get("reason", "mandatory rules matched"))
        else:
            decision, reason = "deny", "no applicable HARO rule; evaluation is fail-closed"
    trace = [
        {"rule_id": rule.get("id"), "domain": rule.get("domain"), "action": rule.get("action")}
        for rule in matched
    ]
    result = {
        "decision": decision,
        "allowed": decision == "allow",
        "reason": reason,
        "matched_rules": trace,
        "telemetry": {"evaluation_ms": round((time.perf_counter() - started) * 1000, 3), "rule_count": len(active_rules)},
        "execution_authority": "HARO host; Vault Logic does not execute or admit resources",
    }
    from HARO_Vault_System.glyphs.trace import generate_haro_trace
    result["glyph_trace"] = generate_haro_trace(result, context)
    if memory_matrix is not None:
        memory_matrix.record("last_haro_evaluation", {"decision": decision, "matched_rules": trace})
        result["memory_matrix_key"] = "last_haro_evaluation"
    if telemetry is not None:
        telemetry.record({"event": "vault_evaluation", "decision": decision, "rule_count": len(active_rules), "telemetry": result["telemetry"]})
        result["telemetry_recorded"] = True
    return result


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
