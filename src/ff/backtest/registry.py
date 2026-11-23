"""Trial registry: an append-only record of every parameter set ever backtested.

The Deflated Sharpe Ratio needs the honest number of trials, including the ones
that looked bad and were forgotten. Every validation run appends here, and the
trial count for a strategy kind is read back from the full history.

It also records when the locked hold-out period is opened, so a hold-out can't be
quietly reused to tune parameters.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import pandas as pd


def params_key(kind: str, params: dict[str, Any]) -> str:
    blob = json.dumps({"kind": kind, "params": params}, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:12]


class TrialRegistry:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)

    def _read(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        return [json.loads(line) for line in self.path.read_text().splitlines() if line.strip()]

    def _append(self, rec: dict[str, Any]) -> None:
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, default=str) + "\n")

    def record_trial(self, kind: str, params: dict[str, Any], **fields: Any) -> None:
        self._append(
            {
                "event": "trial",
                "ts": pd.Timestamp.now().isoformat(),
                "kind": kind,
                "key": params_key(kind, params),
                "params": params,
                **fields,
            }
        )

    def n_trials(self, kind: str, also: Iterable[str] = ()) -> int:
        """Distinct parameter sets ever tried for this strategy kind (plus ``also`` kinds)."""
        kinds = {kind, *also}
        return len(
            {
                (r["kind"], r["key"])
                for r in self._read()
                if r["event"] == "trial" and r["kind"] in kinds
            }
        )

    def trial_sharpes(self, kind: str, also: Iterable[str] = ()) -> list[float]:
        """Latest per-period Sharpe of each distinct parameter set for these kinds."""
        kinds = {kind, *also}
        latest: dict[tuple[str, str], float] = {}
        for r in self._read():
            if r["event"] == "trial" and r["kind"] in kinds and r.get("sharpe_daily") is not None:
                latest[(r["kind"], r["key"])] = float(r["sharpe_daily"])
        return list(latest.values())

    def holdout_openings(self, kind: str) -> list[dict[str, Any]]:
        return [r for r in self._read() if r["event"] == "holdout_opened" and r["kind"] == kind]

    def record_holdout_opened(self, kind: str, params: dict[str, Any], holdout_start: str) -> None:
        self._append(
            {
                "event": "holdout_opened",
                "ts": pd.Timestamp.now().isoformat(),
                "kind": kind,
                "key": params_key(kind, params),
                "params": params,
                "holdout_start": holdout_start,
            }
        )
