"""Insider buying from SEC Form 4 filings.

Open-market purchases (transaction code ``P``) by a company's officers and
directors. Insiders sell for many reasons (taxes, diversification, planned
10b5-1 sales) but buy with their own money for one: they expect the stock to
rise. Evidence: Lakonishok & Lee (2001); Cohen, Malloy & Pomorski (2012) show
the value is in *opportunistic* trades and that *routine* ones (same calendar
month every year) carry none, so routine buyers are filtered out here
(classified on purchases only, a simplification of their rule). Clusters of
several insiders and buys by the CEO or CFO score higher.

Honest prior: the effect is strongest in small, less-followed companies; in
large caps purchases are rare and the edge smaller. Sales are ignored.

Data, all free from SEC EDGAR:
- history: the quarterly "Insider Transactions Data Sets" (Forms 3/4/5 in
  tab-separated files, from 2006), cached per quarter;
- the current quarter: each company's filing index plus the Form 4 XML.

Look-ahead: a Form 4 is dated by its filing date (filings are accepted until
10 pm ET), so it is first used at the close of the next session.
"""

from __future__ import annotations

import io
import logging
import re
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import numpy as np
import pandas as pd

from ff.signals.base import COLUMNS, BuildContext, SignalSource, SourceInfo, next_sessions
from ff.signals.http import FetchError, sec_http
from ff.signals.sources.edgar import company_filings, ticker_ciks

log = logging.getLogger(__name__)
TX_COLUMNS = [
    "accession",
    "filing_date",
    "issuer_cik",
    "owner_cik",
    "owner_name",
    "is_director",
    "is_officer",
    "title",
    "trans_date",
    "shares",
    "price",
]
MIN_VALUE = 10_000.0
CLUSTER_DAYS = 30
LIVE_LOOKBACK_DAYS = 200
BULK_URL = "https://www.sec.gov/files/structureddata/data/form-345-data-sets/{y}q{q}_form345.zip"


def _date(s: pd.Series) -> pd.Series:
    d = pd.to_datetime(s, format="%d-%b-%Y", errors="coerce")
    miss = d.isna() & s.notna()
    if miss.any():
        d[miss] = pd.to_datetime(s[miss], errors="coerce", format="mixed")
    return d


def parse_bulk_zip(blob: bytes) -> pd.DataFrame:
    """Open-market purchases from one quarterly Form 3/4/5 data set."""
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        names = {Path(n).name.upper(): n for n in z.namelist()}

        def tsv(name: str, cols: list[str]) -> pd.DataFrame:
            with z.open(names[name]) as fh:
                df = pd.read_csv(
                    fh,
                    sep="\t",
                    dtype=str,
                    quoting=3,
                    on_bad_lines="skip",
                    encoding_errors="replace",
                )
            df.columns = [c.upper() for c in df.columns]
            return df[[c for c in cols if c in df.columns]]

        tx = tsv(
            "NONDERIV_TRANS.TSV",
            [
                "ACCESSION_NUMBER",
                "TRANS_DATE",
                "TRANS_CODE",
                "TRANS_SHARES",
                "TRANS_PRICEPERSHARE",
                "TRANS_ACQUIRED_DISP_CD",
            ],
        )
        tx = tx[(tx["TRANS_CODE"] == "P") & (tx["TRANS_ACQUIRED_DISP_CD"].fillna("A") == "A")]
        sub = tsv(
            "SUBMISSION.TSV", ["ACCESSION_NUMBER", "FILING_DATE", "DOCUMENT_TYPE", "ISSUERCIK"]
        )
        own = tsv(
            "REPORTINGOWNER.TSV",
            [
                "ACCESSION_NUMBER",
                "RPTOWNERCIK",
                "RPTOWNERNAME",
                "RPTOWNER_RELATIONSHIP",
                "RPTOWNER_TITLE",
            ],
        )
    own = own.drop_duplicates("ACCESSION_NUMBER")
    df = tx.merge(sub, on="ACCESSION_NUMBER").merge(own, on="ACCESSION_NUMBER", how="left")
    df = df[df["DOCUMENT_TYPE"].isin(["4", "4/A"])]
    rel = df["RPTOWNER_RELATIONSHIP"].fillna("").str.lower()
    out = pd.DataFrame(
        {
            "accession": df["ACCESSION_NUMBER"],
            "filing_date": _date(df["FILING_DATE"]),
            "issuer_cik": pd.to_numeric(df["ISSUERCIK"], errors="coerce"),
            "owner_cik": pd.to_numeric(df["RPTOWNERCIK"], errors="coerce"),
            "owner_name": df["RPTOWNERNAME"].fillna(""),
            "is_director": rel.str.contains("director"),
            "is_officer": rel.str.contains("officer"),
            "title": df.get("RPTOWNER_TITLE", pd.Series("", index=df.index)).fillna(""),
            "trans_date": _date(df["TRANS_DATE"]),
            "shares": pd.to_numeric(df["TRANS_SHARES"], errors="coerce"),
            "price": pd.to_numeric(df["TRANS_PRICEPERSHARE"], errors="coerce"),
        }
    )
    return out.dropna(subset=["filing_date", "issuer_cik"]).reset_index(drop=True)


def _txt(el: ET.Element | None, path: str) -> str:
    if el is None:
        return ""
    node = el.find(path)
    if node is None:
        return ""
    v = node.find("value")
    return ((v.text if v is not None else node.text) or "").strip()


def parse_form4_xml(xml: bytes, accession: str, filing_date: pd.Timestamp) -> pd.DataFrame:
    root = ET.fromstring(xml)
    issuer = _txt(root, "issuer/issuerCik")
    owner = root.find("reportingOwner")
    rel = owner.find("reportingOwnerRelationship") if owner is not None else None

    def flag(tag: str) -> bool:
        return _txt(rel, tag).lower() in ("1", "true")

    rows = []
    for t in root.findall("nonDerivativeTable/nonDerivativeTransaction"):
        if _txt(t, "transactionCoding/transactionCode") != "P":
            continue
        if _txt(t, "transactionAmounts/transactionAcquiredDisposedCode") not in ("A", ""):
            continue
        rows.append(
            (
                accession,
                filing_date,
                float(issuer or "nan"),
                float(_txt(owner, "reportingOwnerId/rptOwnerCik") or "nan"),
                _txt(owner, "reportingOwnerId/rptOwnerName"),
                flag("isDirector"),
                flag("isOfficer"),
                _txt(rel, "officerTitle"),
                pd.to_datetime(_txt(t, "transactionDate"), errors="coerce"),
                pd.to_numeric(_txt(t, "transactionAmounts/transactionShares"), errors="coerce"),
                pd.to_numeric(
                    _txt(t, "transactionAmounts/transactionPricePerShare"), errors="coerce"
                ),
            )
        )
    return pd.DataFrame(rows, columns=TX_COLUMNS)


def _quarters(start: pd.Timestamp, end: pd.Timestamp) -> list[tuple[int, int]]:
    out = []
    p = pd.Period(max(start, pd.Timestamp("2006-01-01")), "Q")
    last = pd.Period(end, "Q") - 1  # only finished quarters are published
    while p <= last:
        out.append((p.year, p.quarter))
        p += 1
    return out


def classify_routine(tx: pd.DataFrame) -> pd.Series:
    """True for purchases by insiders who bought in the same calendar month in
    each of the three prior years (Cohen, Malloy & Pomorski 2012)."""
    ym = set(zip(tx["owner_cik"], tx["trans_date"].dt.year, tx["trans_date"].dt.month, strict=True))
    return pd.Series(
        [
            all((o, y - k, m) in ym for k in (1, 2, 3))
            for o, y, m in zip(
                tx["owner_cik"], tx["trans_date"].dt.year, tx["trans_date"].dt.month, strict=True
            )
        ],
        index=tx.index,
    )


def score_purchases(
    tx: pd.DataFrame, cik_to_symbol: dict[int, str], sessions: pd.DatetimeIndex, source_id: str
) -> pd.DataFrame:
    """One record per company and filing date with qualifying buys."""
    if tx.empty:
        return pd.DataFrame(columns=COLUMNS)
    tx = tx.dropna(subset=["trans_date", "shares", "price"]).copy()
    tx["value_usd"] = tx["shares"] * tx["price"]
    tx = tx[(tx["is_director"] | tx["is_officer"]) & (tx["value_usd"] > 0)]
    tx["routine"] = classify_routine(tx)
    tx = tx[~tx["routine"]]
    tx["symbol"] = tx["issuer_cik"].astype("Int64").map(cik_to_symbol)
    tx = tx.dropna(subset=["symbol"])
    tx["top"] = tx["title"].str.contains(
        r"\b(?:CEO|CFO|Chief Executive|Chief Financial)\b", case=False, regex=True
    )
    rows = []
    for sym, g in tx.groupby("symbol"):
        g = g.sort_values("filing_date")
        for fd in g["filing_date"].drop_duplicates():
            win = g[
                (g["filing_date"] <= fd) & (g["filing_date"] > fd - pd.Timedelta(days=CLUSTER_DAYS))
            ]
            today = g[g["filing_date"] == fd]
            if today["value_usd"].sum() < MIN_VALUE:
                continue
            n = win["owner_cik"].nunique()
            usd = float(win["value_usd"].sum())
            top = bool(win["top"].any())
            v = min(1.0, 0.4 + 0.2 * (n - 1) + 0.2 * top + 0.2 * (usd >= 1e6))
            who = ", ".join(sorted(set(win["owner_name"].str.title())))[:120]
            rows.append(
                (
                    source_id,
                    sym,
                    fd,
                    fd,
                    v,
                    f"{n} insider{'s' if n > 1 else ''} bought ${usd / 1e6:,.2f}M "
                    f"in {CLUSTER_DAYS} days{' (incl. CEO/CFO)' if top else ''}: {who}; "
                    f"filed {fd:%d %b %Y}",
                )
            )
    df = pd.DataFrame(rows, columns=COLUMNS)
    if df.empty:
        return df
    df["known_date"] = next_sessions(sessions, df["event_date"])
    df["value"] = df["value"].astype(np.float64)
    return df.dropna(subset=["known_date"])


class InsiderBuying(SignalSource):
    info = SourceInfo(
        id="insider_buying",
        name="Insider buying",
        role="pick",
        horizon=63,
        ttl=126,
        note="Officers and directors buying their own company's stock (routine buyers removed)",
        evidence="Lakonishok & Lee 2001; Cohen, Malloy & Pomorski 2012",
    )

    def _bulk(self, ctx: BuildContext) -> tuple[pd.DataFrame, pd.Timestamp | None, list[str]]:
        http = sec_http()
        parts, covered, problems = [], None, []
        for y, q in _quarters(ctx.start - pd.Timedelta(days=3 * 366 + 31), ctx.end):
            cache = ctx.cache_dir / "form345" / f"{y}q{q}.parquet"
            if not cache.exists():
                try:
                    blob = http.get(BULK_URL.format(y=y, q=q))
                except FetchError as exc:
                    problems.append(f"{y}Q{q}: {exc}")
                    continue  # not published yet, or offline: the live path fills in
                df = parse_bulk_zip(blob)
                cache.parent.mkdir(parents=True, exist_ok=True)
                df.to_parquet(cache)
                log.info("insider data %sQ%s: %d purchases", y, q, len(df))
            parts.append(pd.read_parquet(cache))
            covered = pd.Period(f"{y}Q{q}", "Q").end_time.normalize()
        tx = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame(columns=TX_COLUMNS)
        return tx, covered, problems

    def _live(self, ctx: BuildContext, ciks: dict[str, int], since: pd.Timestamp) -> pd.DataFrame:
        http = sec_http()
        parts = []
        for sym in ctx.symbols:
            cik = ciks.get(sym.upper())
            if cik is None:
                continue
            f = company_filings(cik, ctx.cache_dir, ctx.refresh, since=since)
            f = f[(f["form"].isin(["4", "4/A"])) & (f["filingDate"] > since)]
            for _, r in f.iterrows():
                acc = str(r["accessionNumber"])
                doc = re.sub(r"^xsl[^/]*/", "", str(r["primaryDocument"]))
                url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc.replace('-', '')}/{doc}"
                try:
                    xml = http.cached(
                        url,
                        ctx.cache_dir / "edgar" / "form4" / f"{acc}.xml",
                        max_age_hours=None,
                        refresh=ctx.refresh,
                    )
                    parts.append(parse_form4_xml(xml, acc, r["filingDate"]))
                except (FetchError, ET.ParseError) as exc:
                    log.warning("form 4 %s skipped: %s", acc, exc)
        return pd.concat(parts, ignore_index=True) if parts else pd.DataFrame(columns=TX_COLUMNS)

    def build(self, ctx: BuildContext) -> pd.DataFrame:
        try:
            ciks = ticker_ciks(ctx.cache_dir, ctx.refresh)
            tx, covered, problems = self._bulk(ctx)
            since = (
                covered if covered is not None else ctx.end - pd.Timedelta(days=LIVE_LOOKBACK_DAYS)
            )
            since = max(since, ctx.end - pd.Timedelta(days=LIVE_LOOKBACK_DAYS))
            live = self._live(ctx, ciks, since)
        except FetchError as exc:
            self.status = {"ok": False, "updated": None, "problem": str(exc)}
            log.warning("insider source unavailable: %s", exc)
            return pd.DataFrame(columns=COLUMNS)
        tx = pd.concat([tx, live], ignore_index=True).drop_duplicates(
            ["accession", "owner_cik", "trans_date", "shares", "price"]
        )
        self.status = {
            "ok": not problems or len(problems) <= 2,
            "updated": f"{pd.Timestamp.now().date()}",
            "problem": "; ".join(problems[-3:]),
        }
        return score_purchases(
            tx, {v: k for k, v in ciks.items() if k in set(ctx.symbols)}, ctx.sessions, self.info.id
        )
