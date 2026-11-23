"""Signal store, source scoring and the System Conviction strategy."""

from __future__ import annotations

import io
import json
import zipfile

import numpy as np
import pandas as pd
import pytest

from ff.backtest.engine import BacktestEngine
from ff.core.audit import AuditLog
from ff.core.config import RiskConfig
from ff.data.providers.synthetic import SyntheticProvider
from ff.data.view import MarketData
from ff.markets.us import USMarket
from ff.risk.engine import RiskEngine
from ff.signals.base import COLUMNS, MARKET, BuildContext, SignalSource, SourceInfo
from ff.signals.build import SignalsConfig, build_store
from ff.signals.sources.earnings import EarningsReaction, earnings_dates
from ff.signals.sources.edgar import parse_company_tickers, parse_submissions
from ff.signals.sources.insider import (
    classify_routine,
    parse_bulk_zip,
    parse_form4_xml,
    score_purchases,
)
from ff.signals.sources.macro import MacroRegime, parse_fredgraph
from ff.signals.sources.momentum import PriceMomentum
from ff.strategies.base import build_strategy

STOCKS = [f"S{i:02d}" for i in range(24)]
START, END = pd.Timestamp("2010-01-01"), pd.Timestamp("2016-12-31")


@pytest.fixture(scope="module")
def bars() -> dict[str, pd.DataFrame]:
    p = SyntheticProvider(seed=11)
    return {s: p.daily_bars(s, START, END) for s in ["SPY", *STOCKS]}


class Fixed(SignalSource):
    def __init__(self, info: SourceInfo, recs: pd.DataFrame) -> None:
        super().__init__()
        self.info, self.recs = info, recs

    def build(self, ctx: BuildContext) -> pd.DataFrame:
        self.status = {"ok": True, "updated": None, "problem": ""}
        return self.recs


def _info(id: str, role: str = "pick", horizon: int = 21, ttl: int = 21) -> SourceInfo:
    return SourceInfo(id, id, role, horizon, ttl, "test", "test")


def oracle_records(
    bars: dict[str, pd.DataFrame], noise: float, seed: int, src: str, every: int = 10
) -> pd.DataFrame:
    """Looks 21 sessions ahead (a cheat by design, so the scorer has something real
    to find). ``noise`` = share of records with a random sign."""
    rng = np.random.default_rng(seed)
    idx = bars["SPY"].index
    rows = []
    for k in range(0, len(idx) - 25, every):
        d = idx[k]
        for s in STOCKS:
            c = bars[s]["close"]
            fwd = c.iloc[k + 22] / bars[s]["open"].iloc[k + 1] - 1
            v = np.sign(fwd) if rng.random() > noise else rng.choice([-1.0, 1.0])
            if rng.random() < 0.3:
                rows.append((src, s, d, d, float(v), f"{src} says {v:+.0f}"))
    return pd.DataFrame(rows, columns=COLUMNS)


def _store(bars: dict[str, pd.DataFrame], sources: dict[str, SignalSource]):
    cfg = SignalsConfig(list(sources), STOCKS, history_start=str(START.date()))
    return build_store(cfg, MarketData(bars), "unused", END, refresh=False, sources=sources)


# --- the look-ahead guard ------------------------------------------------------------
def test_view_returns_only_known_records(bars: dict[str, pd.DataFrame]) -> None:
    store = _store(bars, {"o": Fixed(_info("o"), oracle_records(bars, 0.0, 1, "o"))})
    asof = bars["SPY"].index[700]
    v = store.view(asof)
    assert (v.known()["known_date"] <= asof).all()
    act = v.active("o")
    assert (act["known_date"] <= asof).all() and (act["age"] < 21).all()
    assert len(v.known()) < len(store.records)


def test_scorer_finds_real_signal_and_ignores_noise(bars: dict[str, pd.DataFrame]) -> None:
    store = _store(
        bars,
        {
            "good": Fixed(_info("good"), oracle_records(bars, 0.6, 1, "good")),
            "junk": Fixed(_info("junk"), oracle_records(bars, 1.0, 2, "junk")),
        },
    )
    sc = store.view(bars["SPY"].index[-1]).scores()
    assert sc["good"].status == "active" and sc["good"].weight > 0
    assert sc["good"].hit is not None and sc["good"].hit > 0.6
    assert sc["junk"].weight < sc["good"].weight / 3
    early = store.view(bars["SPY"].index[150]).scores()
    assert early["good"].status == "insufficient" and early["good"].weight == 0


def test_scores_use_only_resolved_outcomes(bars: dict[str, pd.DataFrame]) -> None:
    recs = oracle_records(bars, 0.5, 3, "good")
    clean = _store(bars, {"good": Fixed(_info("good"), recs)})
    cut = bars["SPY"].index[1000]
    bad = {}
    rng = np.random.default_rng(0)
    for s, df in bars.items():
        df = df.copy()
        fut = df.index > cut
        for col in ("open", "high", "low", "close"):
            df.loc[fut, col] *= rng.uniform(0.3, 3.0, fut.sum())
        bad[s] = df
    corrupt = _store(bad, {"good": Fixed(_info("good"), recs)})
    for i in (400, 700, 1000):
        a = clean.scorer.scores_asof(i, exact=True)["good"]  # type: ignore[union-attr]
        b = corrupt.scorer.scores_asof(i, exact=True)["good"]  # type: ignore[union-attr]
        assert a.as_dict() == b.as_dict()


# --- System Conviction ---------------------------------------------------------------
def _run(
    bars: dict[str, pd.DataFrame],
    recs: pd.DataFrame,
    risk_cfg: RiskConfig,
    market: USMarket,
    **params: object,
) -> tuple[pd.Series, list[dict[str, object]]]:
    store = _store(bars, {"good": Fixed(_info("good"), recs)})
    audit = AuditLog()
    strat = build_strategy("system_conviction", "conv", STOCKS, dict(params))
    eng = BacktestEngine(
        strat, MarketData(bars), market, RiskEngine(risk_cfg, audit), 100_000, audit, signals=store
    )
    res = eng.run(START + pd.Timedelta(days=400), END)
    return res.equity, audit.of_kind("decision")


def test_conviction_trades_with_stops_and_rationale(
    bars: dict[str, pd.DataFrame], risk_cfg: RiskConfig, market: USMarket
) -> None:
    eq, dec = _run(bars, oracle_records(bars, 0.5, 4, "good"), risk_cfg, market)
    buys = [d for d in dec if float(d["target_weight"]) > 0]  # type: ignore[arg-type]
    assert buys, "expected some conviction buys"
    assert all(d["stop"] is not None for d in buys)
    assert "System Conviction buy" in str(buys[0]["rationale"])
    assert "Invalidated if" in str(buys[0]["rationale"])
    assert eq.iloc[-1] > 0


def test_noise_source_gets_little_or_no_trading(
    bars: dict[str, pd.DataFrame], risk_cfg: RiskConfig, market: USMarket
) -> None:
    def n_buys(noise: float) -> int:
        _, dec = _run(bars, oracle_records(bars, noise, 5, "good"), risk_cfg, market)
        return len([d for d in dec if float(d["target_weight"]) > 0])  # type: ignore[arg-type]

    assert n_buys(1.0) < n_buys(0.5) / 3


def test_conviction_ignores_the_future(
    bars: dict[str, pd.DataFrame], risk_cfg: RiskConfig, market: USMarket
) -> None:
    recs = oracle_records(bars, 0.5, 6, "good")
    eq_a, dec_a = _run(bars, recs, risk_cfg, market)
    cut = bars["SPY"].index[1300]
    rng = np.random.default_rng(1)
    bad = {}
    for s, df in bars.items():
        df = df.copy()
        fut = df.index > cut
        for col in ("open", "high", "low", "close"):
            df.loc[fut, col] *= rng.uniform(0.3, 3.0, fut.sum())
        bad[s] = df
    recs_bad = recs.copy()
    fut = recs_bad["known_date"] > cut
    recs_bad.loc[fut, "value"] = -recs_bad.loc[fut, "value"]
    eq_b, dec_b = _run(bad, recs_bad, risk_cfg, market)
    before = [d for d in dec_a if d["date"] <= cut]
    assert before
    assert before == [d for d in dec_b if d["date"] <= cut]
    pd.testing.assert_series_equal(eq_a[:cut], eq_b[:cut])


# --- built-in sources on synthetic data ----------------------------------------------
def _ctx(bars: dict[str, pd.DataFrame]) -> BuildContext:
    data = MarketData(bars)
    return BuildContext(
        data,
        data.sessions("SPY"),
        STOCKS,
        START,
        END,
        cache_dir=None,  # type: ignore[arg-type]
        refresh=False,
    )


def test_momentum_records_are_ranked_and_month_end(bars: dict[str, pd.DataFrame]) -> None:
    df = PriceMomentum().build(_ctx(bars))
    assert len(df) and df["value"].between(-1, 1).all()
    assert (df["known_date"] == df["event_date"]).all()
    one = df[df["known_date"] == df["known_date"].iloc[0]]
    assert one["value"].max() == 1 and one["value"].min() == -1


def test_macro_regime_known_after_release(bars: dict[str, pd.DataFrame]) -> None:
    days = pd.bdate_range("2009-01-01", END)
    raw = {
        "VIXCLS": pd.Series(np.linspace(15, 40, len(days)), index=days),
        "T10Y3M": pd.Series(np.linspace(2, -1, len(days)), index=days),
        "BAA10Y": pd.Series(np.linspace(2, 3, len(days)), index=days),
    }
    df = MacroRegime().from_series(raw, _ctx(bars))
    assert (df["symbol"] == MARKET).all()
    assert (df["known_date"] > df["event_date"]).all()
    assert df["value"].iloc[-1] < df["value"].iloc[10]  # rising fear, inverting curve


def test_earnings_reaction_window(bars: dict[str, pd.DataFrame]) -> None:
    d = bars["SPY"].index[600]
    df = EarningsReaction().from_dates({"S01": pd.Series([d])}, _ctx(bars))
    assert len(df) == 1
    s = bars["SPY"].index
    assert df["known_date"].iloc[0] == s[601]  # close of the day after the report
    c, spy = bars["S01"]["close"], bars["SPY"]["close"]
    ear = (c.iloc[601] / c.iloc[599] - 1) - (spy.iloc[601] / spy.iloc[599] - 1)
    assert df["value"].iloc[0] == pytest.approx(np.clip(ear / 0.06, -1, 1))


# --- parsers for the free data formats -----------------------------------------------
def test_parse_fredgraph_both_headers() -> None:
    a = parse_fredgraph("observation_date,T10Y3M\n2024-01-02,1.5\n2024-01-03,.\n2024-01-04,1.4\n")
    b = parse_fredgraph("DATE,VIXCLS\n2024-01-02,13.2\n")
    assert list(a.values) == [1.5, 1.4] and b.iloc[0] == 13.2


def test_parse_edgar_tickers_and_filings() -> None:
    m = parse_company_tickers(
        json.dumps(
            {
                "0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple"},
                "1": {"cik_str": 1067983, "ticker": "BRK.B", "title": "Berkshire"},
            }
        ).encode()
    )
    assert m["AAPL"] == 320193 and m["BRK-B"] == 1067983
    recent = {
        "accessionNumber": ["a1", "a2", "a3"],
        "form": ["8-K", "4", "8-K"],
        "filingDate": ["2024-05-02", "2024-05-03", "2024-05-10"],
        "acceptanceDateTime": ["", "", ""],
        "items": ["2.02,9.01", "", "2.02"],
        "primaryDocument": ["x.htm", "xslF345X05/f4.xml", "y.htm"],
    }
    older = {
        "accessionNumber": ["a0"],
        "form": ["8-K"],
        "filingDate": ["2024-02-01"],
        "items": ["2.02"],
        "primaryDocument": ["z.htm"],
    }
    f = parse_submissions({"filings": {"recent": recent}}, [older])
    assert len(f) == 4
    dates = earnings_dates(f)
    assert list(dates) == [pd.Timestamp("2024-02-01"), pd.Timestamp("2024-05-02")]


FORM4 = b"""<?xml version="1.0"?>
<ownershipDocument>
  <issuer><issuerCik>0000320193</issuerCik>
    <issuerTradingSymbol>AAPL</issuerTradingSymbol></issuer>
  <reportingOwner>
    <reportingOwnerId><rptOwnerCik>0001214156</rptOwnerCik>
      <rptOwnerName>DOE JANE</rptOwnerName></reportingOwnerId>
    <reportingOwnerRelationship><isDirector>0</isDirector><isOfficer>1</isOfficer>
      <officerTitle>Chief Executive Officer</officerTitle></reportingOwnerRelationship>
  </reportingOwner>
  <nonDerivativeTable>
    <nonDerivativeTransaction>
      <transactionDate><value>2024-05-01</value></transactionDate>
      <transactionCoding><transactionFormType>4</transactionFormType><transactionCode>P</transactionCode></transactionCoding>
      <transactionAmounts><transactionShares><value>10000</value></transactionShares>
        <transactionPricePerShare><value>170.5</value></transactionPricePerShare>
        <transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode></transactionAmounts>
    </nonDerivativeTransaction>
    <nonDerivativeTransaction>
      <transactionDate><value>2024-05-01</value></transactionDate>
      <transactionCoding><transactionCode>S</transactionCode></transactionCoding>
      <transactionAmounts><transactionShares><value>500</value></transactionShares>
        <transactionPricePerShare><value>171</value></transactionPricePerShare>
        <transactionAcquiredDisposedCode><value>D</value></transactionAcquiredDisposedCode></transactionAmounts>
    </nonDerivativeTransaction>
  </nonDerivativeTable>
</ownershipDocument>"""


def test_parse_form4_keeps_open_market_buys_only() -> None:
    df = parse_form4_xml(FORM4, "0001-24-000001", pd.Timestamp("2024-05-02"))
    assert len(df) == 1
    r = df.iloc[0]
    assert r["issuer_cik"] == 320193 and r["is_officer"] and r["shares"] == 10000
    assert "Chief Executive" in r["title"]


def test_parse_bulk_zip() -> None:
    sub = (
        "ACCESSION_NUMBER\tFILING_DATE\tPERIOD_OF_REPORT\tDOCUMENT_TYPE\tISSUERCIK\n"
        "acc1\t02-MAY-2024\t01-MAY-2024\t4\t0000320193\n"
        "acc2\t03-MAY-2024\t01-MAY-2024\t4\t0000320193\n"
    )
    own = (
        "ACCESSION_NUMBER\tRPTOWNERCIK\tRPTOWNERNAME\tRPTOWNER_RELATIONSHIP\tRPTOWNER_TITLE\n"
        "acc1\t1214156\tDOE JANE\tOfficer\tCFO\nacc2\t99\tBIG FUND\tTenPercentOwner\t\n"
    )
    tx = (
        "ACCESSION_NUMBER\tNONDERIV_TRANS_SK\tTRANS_DATE\tTRANS_CODE\tTRANS_SHARES\t"
        "TRANS_PRICEPERSHARE\tTRANS_ACQUIRED_DISP_CD\n"
        "acc1\t1\t01-MAY-2024\tP\t1000\t170\tA\nacc1\t2\t01-MAY-2024\tS\t10\t170\tD\n"
        "acc2\t3\t01-MAY-2024\tP\t5\t170\tA\n"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("SUBMISSION.tsv", sub)
        z.writestr("REPORTINGOWNER.tsv", own)
        z.writestr("NONDERIV_TRANS.tsv", tx)
    df = parse_bulk_zip(buf.getvalue())
    assert len(df) == 2
    assert df.iloc[0]["filing_date"] == pd.Timestamp("2024-05-02")
    assert bool(df.iloc[0]["is_officer"]) and not bool(df.iloc[1]["is_officer"])


def test_insider_scoring_filters_routine_and_clusters() -> None:
    def row(owner: int, fd: str, td: str, title: str = "", usd: float = 50_000) -> dict:
        return {
            "accession": f"{owner}{fd}",
            "filing_date": pd.Timestamp(fd),
            "issuer_cik": 1,
            "owner_cik": owner,
            "owner_name": f"P{owner}",
            "is_director": True,
            "is_officer": bool(title),
            "title": title,
            "trans_date": pd.Timestamp(td),
            "shares": usd / 10,
            "price": 10.0,
        }

    rows = [row(7, f"{y}-03-05", f"{y}-03-03") for y in (2020, 2021, 2022, 2023)]  # routine
    rows += [row(1, "2023-06-01", "2023-05-31", "CEO"), row(2, "2023-06-10", "2023-06-08")]
    tx = pd.DataFrame(rows)
    routine = classify_routine(tx)
    assert routine.tolist() == [False, False, False, True, False, False]
    sessions = pd.bdate_range("2020-01-01", "2024-01-01")
    out = score_purchases(tx, {1: "XYZ"}, sessions, "insider_buying")
    assert pd.Timestamp("2023-03-05") not in set(out["event_date"])
    last = out.sort_values("event_date").iloc[-1]
    assert last["known_date"] > last["event_date"]
    assert last["value"] == pytest.approx(0.4 + 0.2 + 0.2)  # 2 insiders, CEO included
    assert "2 insiders" in last["detail"]
