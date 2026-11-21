from __future__ import annotations

from pathlib import Path

import pandas as pd

from ff.data.providers.synthetic import SyntheticProvider
from ff.data.store import BarStore, validate_bars
from tests.conftest import make_bars


def test_validation_flags_bad_bars() -> None:
    df = make_bars([100, 101, 102, 150, 151])
    df.loc[df.index[1], "high"] = 50  # high below close
    kinds = {i.kind for i in validate_bars("X", df)}
    assert {"inconsistent_ohlc", "jump"} <= kinds


def test_store_caches_to_parquet(tmp_path: Path) -> None:
    store = BarStore(SyntheticProvider(), tmp_path)
    s, e = pd.Timestamp("2020-01-01"), pd.Timestamp("2020-06-30")
    first = store.get("SPY", s, e)
    assert (tmp_path / "bars" / "synthetic" / "SPY.parquet").exists()
    again = store.get("SPY", s, e)
    pd.testing.assert_frame_equal(first, again, check_freq=False)
