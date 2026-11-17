"""Local JSON workbook used by demo mode (no Google credentials needed)."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from ..schema import TABS
from .base import Append, Store, Update


class LocalStore(Store):
    def __init__(self, path: Path):
        self.path = Path(path)
        super().__init__(self.path.with_suffix(".lock"))
        self.ensure_schema()

    def ensure_schema(self) -> None:
        data = self._load() if self.path.exists() else {}
        changed = False
        for tab in TABS:
            if tab not in data:
                data[tab] = []
                changed = True
        if changed or not self.path.exists():
            self._save(data)

    def _load(self) -> dict[str, list[dict[str, Any]]]:
        return json.loads(self.path.read_text(encoding="utf-8"))

    def _save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.path)  # atomic on Windows and POSIX

    def _read(self):
        return self._load()

    def _commit(self, ops: list[Append | Update]) -> None:
        data = self._load()
        for op in ops:
            rec = {k: v for k, v in op.record.items() if not k.startswith("_")}
            if isinstance(op, Append):
                data[op.tab].append(rec)
            else:
                rows = data[op.tab]
                if op.row >= len(rows):
                    raise IndexError(f"{op.tab} row {op.row} does not exist")
                rows[op.row] = {**rows[op.row], **rec}
        self._save(data)

    def data_version(self):
        try:
            return self.path.stat().st_mtime_ns
        except OSError:
            return 0

    def describe(self) -> str:
        return f"local workbook {self.path}"
