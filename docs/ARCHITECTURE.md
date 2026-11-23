# Finance Free: Architecture and Phased Roadmap

Status: **draft for review** (2026-11-21). No engine code is written until this plan is approved.

This is a personal research and paper-trading platform. It produces recommendations with reasoning; the owner places any real trades manually. There is no broker or billing connection in scope, but the execution layer sits behind an interface so one could be added later.

---

## 1. Assumptions and open decisions

| Topic | Decision used in this plan | Source |
|---|---|---|
| Market | US stocks and ETFs only. Market is an abstraction (calendar, currency, sessions, costs) so TASE can be added later. | Owner answer |
| Data budget | Owner is willing to pay when a purchase is needed to reach the goals. Start on free sources, every provider behind a plug-in interface, and buy each paid source only at the phase where it becomes necessary (see section 5.3 for the spend timeline). | Owner answer |
| Strategies | Generic plug-in system. Track A / Track B are tags, not hard-coded paths. Any strategy that satisfies the interface can be added. | Owner answer |
| Owner skill | Firmware engineer, reads and changes code. Plan favors explicit, typed, testable code over frameworks with hidden magic. | Owner answer |
| Hosting | Small always-on cloud VM (see section 8). | Owner answer |
| Paper-trading duration | Open; decided by results. The plan defines minimum statistical evidence instead of a fixed time (section 6, promotion pipeline). | Owner answer |
| Starting capital | $100,000 virtual, configurable. | Owner answer |
| Account type | **Guess:** cash account (no margin), so no PDT rule but T+1 settled-cash rules apply. Margin mode is a config switch that turns on PDT and buying-power rules. | Not answered |
| Shorting | **Guess:** long-only at first. Short support added later with borrow-availability and borrow-fee modeling. | Not answered |

Please correct any of the three guesses; each one changes the simulator and risk rules, not the architecture.

### An important consequence of manual execution

Because you place trades yourself, the simulator must model **your** execution, not a robot's:

- Track A: a signal computed after Friday's close is assumed filled at the **next session's open** (or a configurable later time), plus slippage. Never at the bar's own close.
- Track B (intraday): manual execution of setups that last minutes is realistically not achievable. Track B will still be researched and paper-traded automatically, but its results should be read as "what an automated system could do", not "what you can copy by hand". I'd treat Track B as a research track until it shows a clear edge after costs, which most intraday strategies do not (section 9).

---

## 2. High-level architecture

```
                 ┌──────────────────────── Data plane ────────────────────────┐
 Providers ───►  │ Ingestion adapters ─► Validation ─► Point-in-time stores    │
 (Tiingo, Alpaca,│  (one per provider)   (gaps, splits,  • Bars (Parquet/DuckDB)│
  SEC EDGAR,     │                        outliers)     • Events/fundamentals   │
  FRED, ...)     │                                       (Postgres, as-of time) │
                 └───────────────────────────────┬────────────────────────────┘
                                                 │  as-of queries only
                 ┌───────────────────────────────▼────────────────────────────┐
                 │ Signal layer: features + external signals, each with       │
                 │ a SignalSource id and a running predictive-value score     │
                 └───────────────────────────────┬────────────────────────────┘
                                                 │
                 ┌───────────────────────────────▼────────────────────────────┐
                 │ Strategies (plug-ins)  ─► Target positions + rationale      │
                 │ System Conviction model ─► its own portfolio                │
                 └───────────────────────────────┬────────────────────────────┘
                                                 │
                 ┌───────────────────────────────▼────────────────────────────┐
                 │ Portfolio / meta-allocator ─► Risk engine (veto + resize)   │
                 └───────────────────────────────┬────────────────────────────┘
                                                 │  orders
                 ┌───────────────────────────────▼────────────────────────────┐
                 │ Execution interface (Broker ABC)                           │
                 │  • SimBroker (backtest + paper, same code)                 │
                 │  • RecommendationBroker (emits trade tickets for you)      │
                 │  • future: AlpacaBroker / IBKRBroker                       │
                 └───────────────────────────────┬────────────────────────────┘
                                                 │ fills
                 ┌───────────────────────────────▼────────────────────────────┐
                 │ Ledger + audit log (append-only) ─► Metrics ─► Dashboard   │
                 │                                             ─► Alerts       │
                 └────────────────────────────────────────────────────────────┘
```

Key design rule: **the backtester and the live paper loop run the same strategy, risk and broker code.** Only the clock and the data feed differ (historical replay vs. live). This is the single most effective defense against "it worked in the backtest" surprises, and it is why I recommend a custom event-driven engine over vectorbt/backtrader.

### 2.1 Why a custom event-driven engine

| Option | Pros | Cons |
|---|---|---|
| vectorbt | Very fast parameter sweeps | Vectorized logic is easy to leak future data into; different code path from live |
| backtrader | Mature, event-driven | Largely unmaintained, awkward to extend with custom risk/ledger |
| LEAN (QuantConnect) | Full-featured, live-ready | C# core, heavy, opinionated data formats |
| **Custom event-driven (Python)** | Same code backtest/paper/live, full control of fills, costs, audit | We build and test it ourselves |

For research sweeps we can still use vectorized pandas/polars code for quick screening, but nothing gets a "validated" label without passing through the event-driven engine.

### 2.2 Core interfaces (sketch)

```python
class DataProvider(Protocol):
    name: str
    def bars(self, symbols, start, end, freq) -> pl.DataFrame: ...   # adjusted + raw
    def capabilities(self) -> ProviderCaps: ...                      # freq, history, delisted?, rate limits

class Market(Protocol):                      # US now, TASE later
    calendar: TradingCalendar                # sessions, holidays, half days
    currency: str
    cost_model: CostModel                    # commission, fees, spread, slippage
    rules: list[MarketRule]                  # settlement, PDT, short-sale rules

class Strategy(Protocol):
    id: str
    tags: set[str]                           # e.g. {"track:A", "trend"}
    params: StrategyParams                   # typed, hashed, logged per run
    def universe(self, asof) -> list[Symbol]: ...
    def on_bar(self, ctx: Context) -> list[Intent]: ...  # ctx only exposes data <= asof

@dataclass
class Intent:                                # what a strategy wants, before risk
    symbol: Symbol
    direction: Literal["long", "short", "flat"]
    confidence: float                        # 0..1
    entry: Price | None
    stop: Price
    take_profit: Price | None
    rationale: Rationale                     # signals used, risk, invalidation condition

class RiskRule(Protocol):
    def check(self, intent, portfolio, market_state) -> RiskDecision: ...  # approve / resize / reject + reason

class Broker(Protocol):
    def submit(self, order) -> OrderId: ...
    def cancel(self, order_id) -> None: ...
    def fills(self) -> Iterator[Fill]: ...
```

`Context` is the enforcement point for look-ahead prevention: a strategy cannot get a reference to data later than the current simulated time, because the only accessor takes the clock's `asof`.

### 2.3 Technology stack

| Concern | Choice | Reason |
|---|---|---|
| Language | Python 3.12, typed (mypy strict on core) | Ecosystem; types help a firmware-minded reader |
| Packaging | uv, ruff, pytest, hypothesis | Fast, reproducible; property tests for risk rules |
| Dataframes | pandas + NumPy (polars later if speed becomes a bottleneck) | Widest library support; daily data is small |
| Bar storage | Parquet files + DuckDB | Zero-ops, fast for research, trivial backups |
| Operational state | PostgreSQL 16 | Orders, fills, recommendations, signals, audit log |
| TimescaleDB | Deferred | Only needed once minute data volume grows (Track B) |
| Redis | Deferred | Only needed for real-time intraday state (Track B) |
| Scheduler | APScheduler inside the engine service | One process, no extra infra |
| API | FastAPI | Dashboard backend, health checks |
| Dashboard | Static HTML/CSS/JS prototype in `dashboard/` (owner's design), fed by the FastAPI backend in Phase 3 | No build step; `js/api.js` switches from mock data to the live API |
| Alerts | Telegram bot (free); email optional | Simple, push to phone |
| ML (later) | scikit-learn, LightGBM | Strong tabular baselines, interpretable feature importance |
| Deployment | Docker Compose on one VM | Matches "small always-on" |
| CI | GitHub Actions: lint, type check, tests | Free for this size |

---

## 3. Strategy framework and tracks

- A strategy is a Python class plus a YAML config. Registering it is adding one entry to the config; nothing else in the system changes.
- Tracks are **portfolios with their own capital, risk budget and metrics**: `track_a`, `track_b`, `system_conviction`. Strategies are assigned to a portfolio by config.
- Every run stores: strategy id, git commit, parameter hash, data snapshot id, random seeds. Any result can be reproduced exactly.
- Meta-allocation (phase 6) shifts capital between strategies inside a portfolio based on out-of-sample and live performance, with shrinkage toward equal weight so one lucky month cannot take over.

Initial candidates (chosen because of the evidence behind them, see section 9):

| Track | Strategy | Evidence |
|---|---|---|
| A | Trend filter / time-series momentum on a diversified ETF set (e.g. SPY, QQQ, IWM, EFA, EEM, TLT, IEF, GLD, DBC, VNQ) | Moskowitz, Ooi & Pedersen 2012; Hurst, Ooi & Pedersen 2017; Faber 2007 |
| A | Cross-sectional momentum (ETF rotation, later single stocks) | Jegadeesh & Titman 1993; Asness, Moskowitz & Pedersen 2013 |
| A | Volatility targeting overlay | Moreira & Muir 2017; Harvey et al. 2018 |
| A | Factor tilts: quality, low volatility, value | Fama & French 1993/2015; Novy-Marx 2013; Frazzini & Pedersen 2014; Asness, Frazzini & Pedersen 2019 |
| B | Opening range breakout on liquid "stocks in play" / index ETFs | Zarattini, Barbon & Aziz 2024 (working paper, treat with caution) |
| B | Intraday momentum (first half-hour predicts last half-hour) | Gao, Han, Li & Zhou 2018 |
| B | VWAP trend / reversion, gap plays | Mostly practitioner evidence; weak academic support |

---

## 4. Market intelligence layer

Every external input is a **SignalSource** with an id, a timestamp of *when we would have known it*, and a value. A nightly job scores each source:

- **Information coefficient (IC):** rank correlation between the signal and forward returns at several horizons (1d, 5d, 21d), rolling 1 year.
- **Decay:** how fast IC falls with horizon; tells us whether the signal is tradable by hand.
- **Stability:** IC t-stat and hit rate over rolling windows.
- **Incremental value:** does it add anything once price momentum and volatility are known? (regression / LightGBM with and without the feature)

Source weights in the System Conviction model come from these scores, shrunk toward zero until enough history exists. A source with no proven value gets ~zero weight automatically; this is how "learn which sources to trust" is implemented without hand-tuning.

Social sentiment specifically: evidence that retail forum sentiment predicts returns after costs is weak and mostly concentrated in small, illiquid names where it often signals *overpricing* (e.g. Barber, Huang, Odean & Schwarz 2022 on Robinhood herding). I'll implement it as a hype/attention detector and risk flag first, and as a return signal only if its IC proves it.

---

## 5. Data providers

Prices are approximate, from my knowledge as of 2026, and change often. Verify before subscribing.

### 5.1 Recommended starting set (all free)

| Data | Provider | Cost | Limits |
|---|---|---|---|
| Daily OHLCV (US) | **Tiingo** free tier | $0 | Rate/symbol caps on free plan; good adjusted data; personal use |
| Daily + minute bars, news | **Alpaca** market data (free account, no funding needed) | $0 | Free real-time is IEX-only (~2–3% of volume); full SIP history with 15-min delay |
| Fundamentals, insider (Form 4), 13F | **SEC EDGAR** APIs | $0 | Raw XBRL, needs cleaning; must set a User-Agent; 10 req/s |
| Macro, rates, VIX close | **FRED** | $0 | Daily/monthly; use ALFRED vintages for point-in-time macro |
| Short interest | **FINRA** | $0 | Twice-monthly, published with a lag |
| Earnings calendar, analyst recs | **Finnhub** free tier | $0 | 60 calls/min; some endpoints premium |
| Reddit | Reddit API | $0 (non-commercial) | ~100 queries/min; history limited |

`yfinance` is useful for quick checks but it is an unofficial scraper that breaks and is against Yahoo's terms; it will not be a production source.

### 5.2 Paid upgrades, in the order I'd buy them

| Need | Provider | Approx. cost | Why |
|---|---|---|---|
| Survivorship-free US stock history (delisted names, historical index membership) | **Norgate Data** | ~$300–630/yr | Required before any single-stock Track A result is trusted |
| Intraday history + real-time SIP | **Polygon.io (now Massive)** | ~$30–200/mo by tier | Clean minute/second bars, trades, quotes for Track B |
| Tick-level, usage-based | **Databento** | pay per GB | Highest quality for intraday cost modeling |
| Cleaned fundamentals | **Financial Modeling Prep** or **Sharadar** (Nasdaq Data Link) | ~$20–100/mo | Point-in-time fundamentals with less work than raw EDGAR |
| News + analyst actions | **Benzinga** (via Alpaca or direct) | free tier via Alpaca; direct is enterprise pricing | Upgrades/downgrades, PT changes |
| Options flow / put-call | **Polygon options**, **ORATS**, **CBOE DataShop** | ~$30–200+/mo | Expensive; phase 6 at the earliest |
| X/Twitter | X API | ~$200/mo (Basic) and up | Poor value for this project; skip unless evidence justifies it |
| StockTwits | API largely closed to new developers | n/a | Likely not available |

Recommendation: stay at $0 through phase 3 (ETF-based Track A doesn't suffer from survivorship bias as long as the ETF list is fixed in advance and includes ones that existed at the time). The first paid item should be Norgate (if single stocks) or Polygon (if Track B).

### 5.3 When money is actually needed

| When | Item | Approx. cost | Needed or optional | Why |
|---|---|---|---|---|
| Phase 3 (paper trading goes always-on) | Cloud VM + backups | ~$5–30/mo | **Needed** | The always-on deployment itself |
| Phase 2, only if Track A moves from ETFs to single stocks | Norgate Data (survivorship-free US stocks) | ~$300–630/yr | **Needed for single stocks** | Without delisted names, single-stock backtests overstate returns; results would not be trustworthy |
| Phase 4 | Clean point-in-time fundamentals (FMP or Sharadar) | ~$20–100/mo | Optional | Free SEC EDGAR works but takes more cleaning work |
| Phase 5 (Track B starts) | Polygon/Massive intraday + real-time SIP | ~$30–200/mo | **Needed for Track B** | Free real-time data is IEX-only, too thin to model spreads and slippage realistically |
| Phase 6 | LLM API for news/forum sentiment scoring | ~$5–30/mo at this volume | Optional | Only if sentiment shows predictive value in the scoreboard |
| Phase 6+ | Options flow, X/Twitter API | ~$100–200+/mo each | Not recommended yet | Weak evidence of value relative to cost; revisit only if other signals prove out |

Rule: nothing is bought ahead of the phase that uses it, and every optional purchase must first show that the free alternative is the bottleneck. I'll flag each purchase in the thread when we reach that phase.

---

## 6. Bias and overfitting prevention

| Bias | How it's prevented |
|---|---|
| **Look-ahead** | Every record stores `event_time` and `known_time`; the `Context` only returns rows with `known_time <= asof`. Signals computed on bar *t* fill no earlier than bar *t+1* open. Fundamentals use filing date, not period end. Unit tests replay a strategy with future data deliberately corrupted and assert the output does not change. |
| **Survivorship** | Universe is defined point-in-time (historical index membership, delisted names included) once paid data is in; until then, single-stock backtests are labeled "survivorship-biased, not valid". ETF universe is fixed in config. |
| **Data snooping / multiple testing** | Every backtest run is logged in a trial registry. Reported Sharpe is the **Deflated Sharpe Ratio** (Bailey & López de Prado 2014), which penalizes the number of trials. Probability of Backtest Overfitting via CSCV (Bailey, Borwein, López de Prado & Zhu 2017). New-factor bar set at t-stat > 3 (Harvey, Liu & Zhu 2016). |
| **Overfitting** | Few parameters per strategy, chosen from literature defaults; parameter-stability heatmaps (a good strategy works on a plateau, not a spike). Walk-forward optimization with anchored and rolling windows. |
| **Train/test leakage in ML** | Purged and embargoed K-fold cross-validation (López de Prado 2018). |
| **Hold-out abuse** | A final hold-out period (e.g. last 3 years) is locked and evaluated **once** per strategy version; the registry records that it was opened. |
| **Cost blindness** | Every result reported both gross and net of commissions, spread, slippage and (when relevant) borrow fees. A strategy must survive 2x the base cost assumption. |
| **Corporate actions** | Store raw and adjusted prices; sim trades raw prices and applies splits/dividends as events, so position sizes and cash are realistic. |

### Promotion pipeline (backtest → paper → "worth acting on")

1. **Backtest** in-sample with literature-default parameters.
2. **Walk-forward** out-of-sample; net-of-cost; Deflated Sharpe > 0 with 95% confidence; PBO < 0.5.
3. **Locked hold-out**, opened once.
4. **Paper trading**: runs until enough independent bets exist to separate skill from luck. For a monthly-rebalanced Track A strategy that is realistically years, so the rule is "live paper results are consistent with the backtest distribution" (no significant degradation) rather than "proved alpha". Intraday strategies accumulate trades faster; ~200+ trades is a reasonable minimum.
5. **Your decision.** The system only reports status; it never claims a strategy is ready for real money.

---

## 7. Risk management

Risk is a chain of independent rules; each one can approve, shrink or reject an intent, and every decision is logged with its reason.

| Rule | Default (configurable) |
|---|---|
| Position size | Volatility-based: `shares = (equity × risk_per_trade) / (k × ATR20)`; optional fractional Kelly (≤ 0.25 Kelly) capped by the same limit |
| Max risk per trade | 1% of portfolio equity (stop distance × shares) |
| Max position weight | 10% single stock, 25% single ETF |
| Sector / strategy exposure | 30% per GICS sector; per-strategy capital from allocator |
| Correlation | Reject a new position if its 60d correlation with an existing one is > 0.8 and combined weight exceeds the single-name cap |
| Max daily loss | 2% (Track A), 3% (Track B): stop new entries for the day |
| Drawdown circuit breaker | 15% from peak per portfolio: flatten to cash, require manual reset |
| Kill switch | One flag (file, API and dashboard button) checked before every order; halts everything |
| Account rules | Cash account: trade only settled cash (T+1); margin mode: PDT rule and buying power |
| Untested code | A strategy without a passed validation record cannot be attached to a portfolio (enforced at startup) |

Risk rules get the most thorough tests in the codebase, including property-based tests ("no sequence of intents can exceed the per-trade cap").

---

## 8. Hosting: small always-on cloud

### What runs

One VM running Docker Compose (`docker/compose.yaml`) with two containers that share a `state/` folder:

1. `paper`: scheduler, data ingestion, strategies, risk, sim broker (`ff daemon`)
2. `web`: FastAPI backend serving the static dashboard (`ff serve`), bound to localhost; reach it over Tailscale

State is one SQLite file (`state/paper.sqlite`) with nightly backups, instead of the Postgres container first planned: there is one writer and one user, and backups are a file copy. Moving to Postgres later only means swapping the small `PaperDB` layer.

### Sizing

- MVP / Track A (daily data): **2 vCPU, 4 GB RAM, 40–80 GB SSD** is plenty.
- With Track B minute data for a few hundred symbols: **4 vCPU, 8 GB RAM, 160 GB SSD**.
- Heavy research (parameter sweeps, ML training) is better run on your own machine or a temporary larger VM, not the always-on box.

### Provider options (approx. monthly cost)

| Provider | 2 vCPU / 4 GB | 4 vCPU / 8 GB | Notes |
|---|---|---|---|
| **Hetzner Cloud** (EU or US regions) | ~€4–8 | ~€8–16 | Best price; my recommendation |
| DigitalOcean | ~$24 | ~$48 | Good docs, managed Postgres available (extra cost) |
| AWS Lightsail | ~$24 | ~$44 | AWS ecosystem; fixed price bundles |
| Oracle Cloud Free Tier (ARM) | $0 | $0 (up to 4 OCPU / 24 GB) | Free but capacity is hard to get and accounts can be reclaimed; not for something you rely on |

### What else is required

- **Domain** (optional, ~$10/yr) if you want HTTPS via Caddy; otherwise **Tailscale** (free) gives private access to the dashboard from your phone and laptop with no open ports. I recommend Tailscale: nothing exposed to the internet.
- **Secrets:** a `.env` file on the VM, `chmod 600`, never committed; optionally encrypted in the repo with `sops` + `age`. Only API keys for data providers and the Telegram bot.
- **Backups:** nightly `pg_dump` + Parquet sync to object storage (Backblaze B2 or Hetzner Storage Box, ~$1–5/mo at this size), 30-day retention, and a monthly restore test.
- **Monitoring:** container healthchecks, a daily "I'm alive + summary" Telegram message, and an alert if a scheduled job fails or data is stale.
- **Updates:** unattended security upgrades on the VM; deploy by `git pull && docker compose up -d --build` (later via GitHub Actions over SSH or Tailscale).
- **Time:** VM in UTC, all timestamps stored in UTC, exchange calendars via `exchange_calendars` / `pandas_market_calendars`.

Total realistic running cost for the MVP: **about $5–30/month**, plus data subscriptions if you add any.

---

## 9. Honest return expectations

- **Most active strategies don't beat the index after costs.** Over 15 years, roughly 90% of US large-cap active funds underperform the S&P 500 (S&P SPIVA scorecards). Published anomalies lose about a third of their returns out of sample and about half after publication (McLean & Pontiff 2016).
- **Track A, realistic target:** similar long-run return to SPY, with materially smaller drawdowns (e.g. -20% to -25% instead of -50% in 2008-like crashes). Trend following's main proven value is crisis protection and diversification, not beating equities in bull markets (Hurst, Ooi & Pedersen 2017). Expect to *underperform* SPY in strong, steady bull runs like 2013–2021 and in V-shaped recoveries. A net Sharpe of 0.6–0.9 for a diversified trend/vol-target portfolio would be a good outcome; anything above ~1.5 in a backtest should be treated as a likely bug or overfit.
- **Track B, realistic target:** most likely flat to negative after costs. Large studies of real day traders are grim: in Taiwan fewer than 1% were predictably profitable year after year (Barber, Lee, Liu & Odean 2014; Barber, Lee, Liu, Odean & Zhang 2020); in Brazil 97% of those who persisted over 300 days lost money (Chague, De-Losso & Giovannetti 2019). A few published intraday strategies (ORB, intraday momentum) show positive backtests, but they are recent, few, and sensitive to costs and execution. Treat Track B as research.
- **System Conviction / ML:** financial returns have very low signal-to-noise; ML mostly adds value through better risk/sizing and combining weak signals, not by finding big alpha. Gu, Kelly & Xiu 2020 found real gains, but in a large cross-section with institutional data and costs not fully modeled. Expect modest improvements over simple rules, if any.
- **What success looks like for this project:** a disciplined, well-diversified Track A process that matches the market with less pain, a clear record of which signals actually carry information, and the ability to say "no" to ideas with evidence. That is a genuinely valuable outcome even if nothing beats SPY.

---

## 10. Phased roadmap

Each phase ends with runnable code, tests, and a short report.

### Phase 0: Foundations
- Repo layout, uv project, ruff/mypy/pytest, GitHub Actions CI, Docker Compose skeleton, config system (YAML + env), structured JSON logging.
- **Exit:** `make test` and CI green; `docker compose up` starts an empty engine.

### Phase 1: MVP backtest (daily bars, Track A, one strategy vs SPY)
- `DataProvider` interface + Tiingo adapter (Alpaca as second adapter), local Parquet cache, data validation (gaps, splits, bad ticks).
- US `Market`: NYSE calendar, cost model (commission $0 + spread + slippage in bps, configurable), T+1 settlement.
- Event-driven backtester: clock, `Context` with as-of enforcement, `SimBroker` with next-open fills, market/limit orders, partial fills by volume participation cap.
- One strategy: **time-series trend on a 10-ETF basket** (e.g. 10-month SMA filter / 12-month TSMOM, literature defaults), volatility-scaled weights.
- Risk module v1: ATR sizing, per-trade cap, position caps, daily loss stop, drawdown breaker, kill switch.
- Metrics: total/annualized return, volatility, Sharpe, Sortino, Calmar, max drawdown + duration, win rate, profit factor, avg win/loss, exposure, turnover; always alongside SPY and a 60/40 benchmark.
- Output: CLI command producing an HTML report (equity curve vs SPY, drawdowns, trade list with rationale).
- **Exit:** reproducible backtest 2005–today, unit tests for strategy logic, risk rules and look-ahead guard.

### Phase 2: Validation and more Track A strategies
- Walk-forward engine, trial registry, Deflated Sharpe, PBO, locked hold-out, cost-stress test.
- Add cross-sectional ETF momentum, volatility targeting overlay, dual momentum; portfolio of strategies with equal risk.
- **Exit:** validation report per strategy; promotion status recorded.

### Phase 3: Always-on paper trading + dashboard
- Live daily loop: after-close ingestion, signal generation, risk, recommendations ("trade tickets": symbol, action, size, entry, stop, target, rationale, invalidation), simulated next-open fills.
- SQLite ledger (Postgres was the first plan; see section 8) + append-only journal of every decision, risk verdict, order and fill.
- Dashboard: positions, equity curves vs SPY/QQQ, open recommendations, trade journal, risk status, kill switch. The UI design is agreed and lives in `dashboard/`; Phase 3 implements the endpoints listed in `dashboard/README.md`.
- Telegram alerts. Deploy to the VM with backups and monitoring.
- **Exit:** system runs unattended for 2 weeks with daily Telegram summaries.

### Phase 4: Market intelligence v1 and System Conviction
- SignalSource framework + IC/decay scoring + scoreboard page. **Built** (`src/ff/signals/`).
- Sources: macro (FRED VIX, 10y-3m, Baa spread; FOMC calendar, CPI/jobs dates with a free FRED key), SEC insider buys with the routine-trader filter (Cohen, Malloy & Pomorski 2012), earnings reaction around 8-K Item 2.02 as the free stand-in for earnings surprises (Brandt et al. 2008), and price momentum as the baseline. **Built.**
- Still open: analyst revisions (needs Finnhub key; free tier has little history), short interest (FINRA files), SUE from XBRL EPS.
- System Conviction portfolio: rules-based combiner, source weights learned point-in-time from each source's scored record and shrunk toward zero. **Built**; `config/validate_conviction.yaml` validates it with the Phase 2 pipeline.

### Phase 5: Track B intraday research
- Minute data (likely first paid subscription), intraday sim: spread from quotes, latency, volume-limited fills, end-of-day flattening, halts.
- ORB and intraday-momentum strategies; TimescaleDB/Redis only if needed.

### Phase 6: Meta-allocation, sentiment, ML
- Capital allocation across strategies from live + OOS performance, with shrinkage.
- News and Reddit sentiment (LLM/FinBERT scoring), hype/pump detection as risk flags.
- LightGBM conviction model with purged CV; compared head-to-head against the rules combiner.
- Options flow if budget allows.

### Phase 7: Israel (TASE)
- `Market` implementation for TASE: calendar, ILS, costs, data provider (TASE Data Hub or EODHD). FX conversion for a combined USD view.

---

## 11. Proposed repo layout

```
finance-free/
  docs/                  architecture, ADRs, strategy notes
  config/                default.yaml, strategies/*.yaml, risk.yaml
  src/ff/
    core/                types, clock, events, config, logging
    data/                providers/, store/, validation
    markets/             us/, (tase/)
    signals/             sources/, scoring
    strategies/          base.py, track_a/, track_b/
    risk/                rules/, engine.py
    portfolio/           allocator, ledger
    execution/           broker.py, sim.py, recommendations.py
    backtest/            engine.py, walkforward.py, registry.py
    analytics/           metrics.py, reports/
    app/                 scheduler, api, dashboard
  tests/                 unit/, property/, integration/
  docker/                Dockerfile, compose.yaml
```

---

## 12. References

- Asness, Frazzini & Pedersen (2019). Quality Minus Junk. *Review of Accounting Studies*.
- Asness, Moskowitz & Pedersen (2013). Value and Momentum Everywhere. *Journal of Finance*.
- Bailey & López de Prado (2014). The Deflated Sharpe Ratio. *Journal of Portfolio Management*.
- Bailey, Borwein, López de Prado & Zhu (2017). The Probability of Backtest Overfitting. *Journal of Computational Finance*.
- Barber, Huang, Odean & Schwarz (2022). Attention-Induced Trading and Returns: Evidence from Robinhood Users. *Journal of Finance*.
- Barber, Lee, Liu & Odean (2014). The Cross-Section of Speculator Skill: Evidence from Day Trading. *Journal of Financial Markets*.
- Barber, Lee, Liu, Odean & Zhang (2020). Learning, Fast or Slow. *Review of Asset Pricing Studies*.
- Bernard & Thomas (1989). Post-Earnings-Announcement Drift. *Journal of Accounting Research*.
- Chague, De-Losso & Giovannetti (2019). Day Trading for a Living? Working paper (SSRN).
- Cohen, Malloy & Pomorski (2012). Decoding Inside Information. *Journal of Finance*.
- Faber (2007). A Quantitative Approach to Tactical Asset Allocation. *Journal of Wealth Management*.
- Fama & French (1993; 2015). Common risk factors...; A five-factor asset pricing model. *JFE*.
- Frazzini & Pedersen (2014). Betting Against Beta. *JFE*.
- Gao, Han, Li & Zhou (2018). Market Intraday Momentum. *JFE*.
- Gu, Kelly & Xiu (2020). Empirical Asset Pricing via Machine Learning. *Review of Financial Studies*.
- Harvey, Liu & Zhu (2016). ...and the Cross-Section of Expected Returns. *Review of Financial Studies*.
- Harvey, Hoyle, Korgaonkar, Rattray, Sargaison & Van Hemert (2018). The Impact of Volatility Targeting. *Journal of Portfolio Management*.
- Hurst, Ooi & Pedersen (2017). A Century of Evidence on Trend-Following Investing. *Journal of Portfolio Management*.
- Jegadeesh & Titman (1993). Returns to Buying Winners and Selling Losers. *Journal of Finance*.
- López de Prado (2018). *Advances in Financial Machine Learning*. Wiley.
- McLean & Pontiff (2016). Does Academic Research Destroy Stock Return Predictability? *Journal of Finance*.
- Moreira & Muir (2017). Volatility-Managed Portfolios. *Journal of Finance*.
- Moskowitz, Ooi & Pedersen (2012). Time Series Momentum. *JFE*.
- Novy-Marx (2013). The Other Side of Value: The Gross Profitability Premium. *JFE*.
- S&P Dow Jones Indices. SPIVA U.S. Scorecard (annual).
- Zarattini, Barbon & Aziz (2024). A Profitable Day Trading Strategy for the U.S. Equity Market. Working paper (SSRN).
