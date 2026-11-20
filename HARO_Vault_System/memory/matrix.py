"""Small persistent Vault Logic memory-matrix boundary.

This is the Vault Logic matrix, deliberately separate from A.I.M.S. storage.
"""

import json
import os
import tempfile
from pathlib import Path
from typing import Any


class MemoryMatrix:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self._write({"matrices": {"haro_evaluations": []}})

    def _read(self) -> dict[str, Any]:
        data = json.loads(self.path.read_text(encoding="utf-8"))
        data.setdefault("matrices", {}).setdefault("haro_evaluations", [])
        return data

    def _write(self, data: dict[str, Any]) -> None:
        fd, temporary = tempfile.mkstemp(prefix=self.path.name + ".", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(data, stream, sort_keys=True, indent=2)
                stream.write("\n")
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def record(self, key: str, value: Any) -> None:
        data = self._read()
        if key == "last_haro_evaluation":
            data["matrices"]["haro_evaluations"].append(value)
        else:
            data["matrices"][key] = value
        self._write(data)

    def read(self, key: str) -> Any:
        data = self._read()["matrices"]
        if key == "last_haro_evaluation":
            return data["haro_evaluations"][-1] if data["haro_evaluations"] else None
        return data.get(key)
