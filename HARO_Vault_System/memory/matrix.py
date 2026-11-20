"""Small persistent Vault Logic memory-matrix boundary.

This is the Vault Logic matrix, deliberately separate from A.I.M.S. storage.
"""

import json
from pathlib import Path
from typing import Any


class MemoryMatrix:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self._write({})

    def _read(self) -> dict[str, Any]:
        return json.loads(self.path.read_text(encoding="utf-8"))

    def _write(self, data: dict[str, Any]) -> None:
        self.path.write_text(json.dumps(data, sort_keys=True, indent=2) + "\n", encoding="utf-8")

    def record(self, key: str, value: Any) -> None:
        data = self._read()
        data[key] = value
        self._write(data)

    def read(self, key: str) -> Any:
        return self._read().get(key)
