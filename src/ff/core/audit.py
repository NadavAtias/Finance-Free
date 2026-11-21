"""Append-only audit log: every decision, risk verdict, order and fill.

The MVP writes JSON Lines to a file per run. Phase 3 adds a Postgres sink with
the same record shape.
"""

from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from enum import Enum
from pathlib import Path
from typing import Any

import pandas as pd


def _default(obj: Any) -> Any:
    if isinstance(obj, pd.Timestamp):
        return obj.isoformat()
    if isinstance(obj, Enum):
        return obj.value
    if is_dataclass(obj) and not isinstance(obj, type):
        return asdict(obj)
    return str(obj)


class AuditLog:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path
        self.records: list[dict[str, Any]] = []
        self._fh = path.open("a", encoding="utf-8") if path else None

    def write(self, kind: str, date: pd.Timestamp, **payload: Any) -> None:
        rec = {"kind": kind, "date": date, **payload}
        self.records.append(rec)
        if self._fh:
            self._fh.write(json.dumps(rec, default=_default) + "\n")

    def close(self) -> None:
        if self._fh:
            self._fh.close()
            self._fh = None

    def of_kind(self, kind: str) -> list[dict[str, Any]]:
        return [r for r in self.records if r["kind"] == kind]
