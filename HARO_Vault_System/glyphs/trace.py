"""Native Vault Logic evaluation glyph tracing for HARO."""

import json
from pathlib import Path
from typing import Any, Mapping


def generate_haro_trace(result: Mapping[str, Any], context: Mapping[str, Any], glyph_map: Path | None = None) -> list[str]:
    path = glyph_map or Path(__file__).with_name("glyph_map.json")
    definitions = json.loads(Path(path).read_text(encoding="utf-8"))
    names = []
    if result.get("decision") == "deny":
        names.append("decision_escalate")
    if result.get("decision") == "unresolved":
        names.append("drift_detected")
    if context.get("memory_hit"):
        names.append("memory_recall")
    return [definitions[name]["symbol"] for name in names if name in definitions]
