"""Stock universe files (``config/universes/*.yaml``): ticker -> [name, sector]."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml


@lru_cache(maxsize=8)
def load_universe(path: str) -> dict[str, tuple[str, str]]:
    raw = yaml.safe_load(Path(path).read_text())
    return {str(t): (str(v[0]), str(v[1])) for t, v in raw["stocks"].items()}


def tickers(path: str) -> list[str]:
    return list(load_universe(path))
