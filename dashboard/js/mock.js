/* Mock backend.
 *
 * Every function here returns exactly the JSON a FastAPI endpoint is expected
 * to return (see the endpoint table in dashboard/README.md). The UI never reads
 * this file directly: it goes through FF.api (js/api.js), so swapping to the
 * real backend is a one-line change there.
 *
 * All numbers are generated deterministically from fixed seeds, so the page
 * looks the same on every load.
 */
(function () {
  'use strict';

  // ---------- deterministic randomness ----------
  function mulberry32(seed) {
    return function () {
      seed |= 0; seed = (seed + 0x6d2b79f5) | 0;
      let t = Math.imul(seed ^ (seed >>> 15), 1 | seed);
      t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
  }
  function normal(rand) {
    let u = 0, v = 0;
    while (u === 0) u = rand();
    while (v === 0) v = rand();
    return Math.sqrt(-2 * Math.log(u)) * Math.cos(2 * Math.PI * v);
  }

  // ---------- calendar ----------
  const START = '2025-01-02';
  const END = '2026-11-20'; // last close before "today" (Sat 2026-11-21)
  const HOLIDAYS = new Set([
    '2025-01-20', '2025-02-17', '2025-04-18', '2025-05-26', '2025-06-19', '2025-07-04',
    '2025-09-01', '2025-11-27', '2025-12-25', '2026-01-01', '2026-01-19', '2026-02-16',
    '2026-04-03', '2026-05-25', '2026-06-19', '2026-07-03', '2026-09-07',
  ]);
  function tradingDays(start, end) {
    const out = [];
    const d = new Date(start + 'T12:00:00Z');
    const e = new Date(end + 'T12:00:00Z');
    while (d <= e) {
      const iso = d.toISOString().slice(0, 10);
      const wd = d.getUTCDay();
      if (wd !== 0 && wd !== 6 && !HOLIDAYS.has(iso)) out.push(iso);
      d.setUTCDate(d.getUTCDate() + 1);
    }
    return out;
  }
  const DATES = tradingDays(START, END);
  const N = DATES.length;

  // ---------- equity curves ----------
  // A common market factor drives benchmarks and (partly) the portfolios,
  // with an early-April-2025 style sell-off and recovery.
  const mRand = mulberry32(7);
  const market = DATES.map((d, i) => {
    let r = normal(mRand) * 0.0095;
    if (i >= 62 && i <= 69) r -= 0.021; // sharp sell-off
    if (i >= 70 && i <= 80) r += 0.009; // V-shaped rebound
    if (i >= 300 && i <= 318) r -= 0.004; // slow grind lower
    return r;
  });

  const SERIES_DEF = [
    { id: 'track_a', name: 'Track A · long-term', kind: 'portfolio', capital: 60000, target: 0.142, beta: 0.42, idio: 0.0030, seed: 11 },
    { id: 'track_b', name: 'Track B · intraday', kind: 'portfolio', capital: 20000, target: -0.031, beta: 0.05, idio: 0.0034, seed: 12 },
    { id: 'conviction', name: 'System Conviction', kind: 'portfolio', capital: 20000, target: 0.098, beta: 0.5, idio: 0.0032, seed: 13 },
    { id: 'spy', name: 'SPY', kind: 'benchmark', target: 0.262, beta: 1.0, idio: 0.0, seed: 21 },
    { id: 'qqq', name: 'QQQ', kind: 'benchmark', target: 0.318, beta: 1.22, idio: 0.0042, seed: 22 },
    { id: 'bal_60_40', name: '60/40', kind: 'benchmark', target: 0.158, beta: 0.58, idio: 0.0021, seed: 23 },
  ];

  function buildCurve(def) {
    const rand = mulberry32(def.seed);
    const raw = market.map((m) => def.beta * m + normal(rand) * def.idio);
    // Track A trend-following cuts exposure into the sell-off.
    if (def.id === 'track_a') for (let i = 64; i <= 72; i++) raw[i] *= 0.35;
    const sum = raw.reduce((a, b) => a + b, 0);
    const shift = (Math.log(1 + def.target) - (sum - raw[0])) / (N - 1);
    raw[0] = -shift; // first point is the inception mark: growth exactly 1.0
    let lv = 0;
    return raw.map((r) => { lv += r + shift; return Math.exp(lv); });
  }

  function metrics(growth) {
    const rets = [];
    for (let i = 1; i < growth.length; i++) rets.push(growth[i] / growth[i - 1] - 1);
    const mean = rets.reduce((a, b) => a + b, 0) / rets.length;
    const sd = Math.sqrt(rets.reduce((a, b) => a + (b - mean) ** 2, 0) / (rets.length - 1));
    const downs = rets.map((r) => Math.min(0, r));
    const dsd = Math.sqrt(downs.reduce((a, b) => a + b * b, 0) / downs.length);
    let peak = growth[0], mdd = 0;
    for (const g of growth) { peak = Math.max(peak, g); mdd = Math.min(mdd, g / peak - 1); }
    const total = growth[growth.length - 1] / growth[0] - 1;
    const years = rets.length / 252;
    const cagr = Math.pow(1 + total, 1 / years) - 1;
    return {
      total_return: total,
      annualized_return: cagr,
      sharpe: (mean / sd) * Math.sqrt(252),
      sortino: (mean / dsd) * Math.sqrt(252),
      calmar: mdd < 0 ? cagr / Math.abs(mdd) : null,
      max_drawdown: mdd,
      volatility: sd * Math.sqrt(252),
    };
  }

  const CURVES = {};
  SERIES_DEF.forEach((d) => { CURVES[d.id] = buildCurve(d); });

  const TRADE_STATS = {
    track_a: { win_rate: 0.46, profit_factor: 1.62, exposure: 0.71, trades: 58 },
    track_b: { win_rate: 0.41, profit_factor: 0.91, exposure: 0.18, trades: 412 },
    conviction: { win_rate: 0.53, profit_factor: 1.34, exposure: 0.83, trades: 37 },
  };

  const PORTFOLIOS = SERIES_DEF.filter((d) => d.kind === 'portfolio').map((d) => {
    const g = CURVES[d.id];
    return {
      id: d.id,
      name: d.name,
      start_capital: d.capital,
      equity: d.capital * g[N - 1],
      pnl_today: d.capital * (g[N - 1] - g[N - 2]),
      metrics: Object.assign(metrics(g), TRADE_STATS[d.id]),
    };
  });

  const totalEquity = PORTFOLIOS.reduce((a, p) => a + p.equity, 0);
  const totalCurve = DATES.map((_, i) =>
    SERIES_DEF.filter((d) => d.kind === 'portfolio').reduce((a, d) => a + d.capital * CURVES[d.id][i], 0));
  const peakEquity = Math.max(...totalCurve);

  // ---------- market clock ----------
  function marketStatus(now) {
    const parts = new Intl.DateTimeFormat('en-US', {
      timeZone: 'America/New_York', weekday: 'short', hour: '2-digit', minute: '2-digit', hour12: false,
      year: 'numeric', month: '2-digit', day: '2-digit',
    }).formatToParts(now).reduce((o, p) => { o[p.type] = p.value; return o; }, {});
    const iso = `${parts.year}-${parts.month}-${parts.day}`;
    const mins = (+parts.hour % 24) * 60 + +parts.minute;
    const weekend = parts.weekday === 'Sat' || parts.weekday === 'Sun';
    const holiday = HOLIDAYS.has(iso);
    let state = 'closed', label = 'Closed';
    if (!weekend && !holiday) {
      if (mins >= 570 && mins < 960) { state = 'open'; label = 'Open'; }
      else if (mins >= 240 && mins < 570) { state = 'pre'; label = 'Pre-market'; }
      else if (mins >= 960 && mins < 1200) { state = 'post'; label = 'After hours'; }
    }
    return { state, label, reason: weekend ? 'Weekend' : holiday ? 'Holiday' : '', et_time: `${parts.hour}:${parts.minute} ET` };
  }

  // ---------- positions ----------
  const POSITIONS = [
    { ticker: 'XLK', name: 'Technology Select SPDR', portfolio: 'track_a', strategy: 'trend_200_50', sector: 'Technology', shares: 46, entry: 238.10, last: 251.40, prev: 249.95, stop: 233.20, opened: '2026-08-14' },
    { ticker: 'GLD', name: 'SPDR Gold Shares', portfolio: 'track_a', strategy: 'trend_200_50', sector: 'Commodities', shares: 40, entry: 236.50, last: 249.80, prev: 250.60, stop: 236.10, opened: '2026-07-22' },
    { ticker: 'IEF', name: 'iShares 7-10Y Treasury', portfolio: 'track_a', strategy: 'vol_target', sector: 'Fixed income', shares: 110, entry: 95.20, last: 94.35, prev: 94.21, stop: 91.60, opened: '2026-09-30' },
    { ticker: 'EFA', name: 'iShares MSCI EAFE', portfolio: 'track_a', strategy: 'dual_momentum', sector: 'Intl equity', shares: 120, entry: 84.60, last: 86.95, prev: 86.40, stop: 81.90, opened: '2026-10-06' },
    { ticker: 'MSFT', name: 'Microsoft', portfolio: 'track_a', strategy: 'trend_200_50', sector: 'Technology', shares: 14, entry: 452.30, last: 441.10, prev: 445.80, stop: 428.00, opened: '2026-09-11' },
    { ticker: 'COST', name: 'Costco', portfolio: 'conviction', strategy: 'conviction_ensemble', sector: 'Consumer staples', shares: 6, entry: 912.00, last: 948.60, prev: 941.20, stop: 884.00, opened: '2026-06-03' },
    { ticker: 'AVGO', name: 'Broadcom', portfolio: 'conviction', strategy: 'conviction_ensemble', sector: 'Technology', shares: 18, entry: 312.40, last: 336.20, prev: 331.75, stop: 309.00, opened: '2026-10-21' },
    { ticker: 'JPM', name: 'JPMorgan Chase', portfolio: 'conviction', strategy: 'conviction_ensemble', sector: 'Financials', shares: 20, entry: 268.90, last: 262.15, prev: 264.40, stop: 255.50, opened: '2026-10-28' },
  ];
  const AS_OF = new Date('2026-11-20T21:05:00Z');
  function daysBetween(a, b) { return Math.round((new Date(b) - new Date(a)) / 86400000); }
  function positionsPayload() {
    return POSITIONS.map((p) => {
      const value = p.shares * p.last;
      const risk = Math.max(0, (p.last - p.stop) * p.shares);
      return Object.assign({}, p, {
        market_value: value,
        unrealized_pnl: (p.last - p.entry) * p.shares,
        unrealized_pct: p.last / p.entry - 1,
        day_pnl: (p.last - p.prev) * p.shares,
        risk_usd: risk,
        risk_pct_equity: risk / totalEquity,
        stop_distance_pct: (p.last - p.stop) / p.last,
        weight: value / totalEquity,
        days_held: daysBetween(p.opened, '2026-11-20'),
      });
    });
  }

  // ---------- recommendations ----------
  const RECS = [
    { id: 'rec_2611_01', ticker: 'XLV', direction: 'BUY', confidence: 72, portfolio: 'track_a', strategy: 'trend_200_50',
      entry: 148.20, stop: 141.10, take_profit: 162.00, shares: 70, created: '2026-11-20T21:10:00Z', horizon: 'Hold for weeks to months',
      signals: 'The 50-day average price just moved above the 200-day average, a classic uptrend sign. Healthcare is the 3rd strongest of 11 sectors over 3 months, and analysts keep raising profit forecasts.',
      risk: 'If investors rush back into tech and growth stocks, healthcare could fall behind quickly.',
      invalidation: 'It closes below 141.10, or the uptrend sign flips back.' },
    { id: 'rec_2611_02', ticker: 'MSFT', direction: 'SELL', confidence: 61, portfolio: 'track_a', strategy: 'trend_200_50',
      entry: 441.10, stop: 456.00, take_profit: 418.00, shares: 14, created: '2026-11-20T21:10:00Z', horizon: 'Sell what you own',
      signals: 'It has closed below its 50-day average 6 days in a row and is lagging other tech stocks. News about it has turned slightly negative.',
      risk: 'The long-term trend is still up. If it quickly climbs back above 456, selling now will look like a mistake.',
      invalidation: 'It closes above 456 before you sell. If so, cancel the sale.' },
    { id: 'rec_2611_03', ticker: 'IEF', direction: 'BUY', confidence: 57, portfolio: 'track_a', strategy: 'vol_target',
      entry: 94.35, stop: 91.60, take_profit: 99.00, shares: 45, created: '2026-11-20T21:12:00Z', horizon: 'Add a bit more',
      signals: 'Your portfolio is calmer than its target, so there is room to add. Bonds are moving opposite to stocks again, which helps soften drops.',
      risk: 'A surprise jump in inflation could make bonds and stocks fall at the same time.',
      invalidation: 'Bonds and stocks start moving together again.' },
    { id: 'rec_2611_04', ticker: 'AMD', direction: 'BUY', confidence: 48, portfolio: 'conviction', strategy: 'conviction_ensemble',
      entry: 164.80, stop: 152.40, take_profit: 189.00, shares: 22, created: '2026-11-20T21:15:00Z', horizon: 'Hold for 1–3 months',
      signals: 'Analysts raised their price targets about 6% in 2 weeks. Options traders are betting on a rise at more than twice the usual rate, and the trend is up.',
      risk: 'Reddit chatter is far above normal. Very hyped stocks often give back their gains.',
      invalidation: 'It closes below 152.40, or the hype fades while the price stops rising.' },
    { id: 'rec_2611_05', ticker: 'JPM', direction: 'SELL', confidence: 55, portfolio: 'conviction', strategy: 'conviction_ensemble',
      entry: 262.15, stop: 270.50, take_profit: 248.00, shares: 20, created: '2026-11-20T21:15:00Z', horizon: 'Sell what you own',
      signals: 'The system\'s overall score for JPM fell from +0.41 to +0.08 in 10 days. Interest rate trends are turning against banks, and 2 analysts cut their rating.',
      risk: 'The score is close to neutral, not negative. Selling now could miss a year-end bounce.',
      invalidation: 'The score climbs back above +0.25 by Monday\'s close.' },
    { id: 'rec_2611_06', ticker: 'QQQ', direction: 'BUY', confidence: 44, portfolio: 'track_b', strategy: 'orb_5m',
      entry: 528.60, stop: 525.90, take_profit: 534.00, shares: 18, created: '2026-11-20T21:20:00Z', horizon: 'Same day · Monday morning',
      signals: 'Only buy if the price breaks above its high from the first 5 minutes of trading, and only if it opens at least 0.3% higher on busy trading.',
      risk: 'This strategy has been losing lately even though it tested well, so the amount is cut in half.',
      invalidation: 'No breakout by 10:30 am ET, or it opens less than 0.3% up. Sell by 3:55 pm ET no matter what.' },
  ];
  function recsPayload(handled) {
    return RECS.filter((r) => !handled[r.id]).map((r) => {
      const riskPerShare = Math.abs(r.entry - r.stop);
      return Object.assign({}, r, {
        position_value: r.entry * r.shares,
        risk_usd: riskPerShare * r.shares,
        risk_pct_equity: (riskPerShare * r.shares) / totalEquity,
        reward_risk: Math.abs(r.take_profit - r.entry) / riskPerShare,
      });
    });
  }

  // ---------- strategies ----------
  const STRATEGIES = [
    { id: 'trend_200_50', name: 'Trend following', desc: 'Buys when the 50-day average is above the 200-day', portfolio: 'track_a', status: 'paper', allocation: 0.35, since: '2025-01-02', live_trades: 31,
      backtest: { cagr: 0.089, sharpe: 0.74, max_dd: -0.182, win_rate: 0.43 }, live: { cagr: 0.081, sharpe: 0.81, max_dd: -0.069, win_rate: 0.45 } },
    { id: 'vol_target', name: 'Steady risk', desc: 'Invests more when markets are calm, less when they swing', portfolio: 'track_a', status: 'paper', allocation: 0.20, since: '2025-01-02', live_trades: 14,
      backtest: { cagr: 0.071, sharpe: 0.69, max_dd: -0.148, win_rate: 0.55 }, live: { cagr: 0.062, sharpe: 0.72, max_dd: -0.051, win_rate: 0.57 } },
    { id: 'conviction_ensemble', name: 'Conviction picks', desc: 'Combines all sources into one score per stock', portfolio: 'conviction', status: 'paper', allocation: 0.18, since: '2025-03-03', live_trades: 37,
      backtest: { cagr: 0.104, sharpe: 0.71, max_dd: -0.195, win_rate: 0.52 }, live: { cagr: 0.052, sharpe: 0.49, max_dd: -0.104, win_rate: 0.53 } },
    { id: 'dual_momentum', name: 'Momentum', desc: 'Holds whatever has gone up the most lately', portfolio: 'track_a', status: 'paper', allocation: 0.15, since: '2025-01-02', live_trades: 13,
      backtest: { cagr: 0.098, sharpe: 0.66, max_dd: -0.214, win_rate: 0.58 }, live: { cagr: 0.041, sharpe: 0.38, max_dd: -0.088, win_rate: 0.46 } },
    { id: 'orb_5m', name: 'Morning breakout', desc: 'Buys when price breaks the first 5-minute high', portfolio: 'track_b', status: 'decaying', allocation: 0.12, since: '2025-02-03', live_trades: 286,
      backtest: { cagr: 0.142, sharpe: 1.21, max_dd: -0.096, win_rate: 0.44 }, live: { cagr: -0.024, sharpe: -0.21, max_dd: -0.073, win_rate: 0.40 } },
    { id: 'vwap_reversion', name: 'Bounce back', desc: 'Bets that big moves during the day snap back', portfolio: 'track_b', status: 'paused', allocation: 0.0, since: '2025-04-01', live_trades: 126,
      backtest: { cagr: 0.110, sharpe: 1.05, max_dd: -0.081, win_rate: 0.61 }, live: { cagr: -0.068, sharpe: -0.74, max_dd: -0.099, win_rate: 0.47 } },
    { id: 'pead', name: 'After-earnings drift', desc: 'Follows stocks that beat or miss earnings', portfolio: 'conviction', status: 'research', allocation: 0.0, since: null, live_trades: 0,
      backtest: { cagr: 0.063, sharpe: 0.48, max_dd: -0.160, win_rate: 0.51 }, live: null },
    { id: 'gap_fade', name: 'Gap fade', desc: 'Bets that big overnight jumps shrink back', portfolio: 'track_b', status: 'research', allocation: 0.0, since: null, live_trades: 0,
      backtest: { cagr: 0.051, sharpe: 0.41, max_dd: -0.122, win_rate: 0.56 }, live: null },
  ];

  // ---------- risk ----------
  function riskPayload() {
    const pos = positionsPayload();
    const by = (key) => {
      const m = {};
      pos.forEach((p) => { m[p[key]] = (m[p[key]] || 0) + p.market_value; });
      return Object.entries(m).map(([k, v]) => ({ key: k, value: v, pct: v / totalEquity })).sort((a, b) => b.value - a.value);
    };
    const maxRisk = pos.reduce((a, p) => Math.max(a, p.risk_pct_equity), 0);
    const openRisk = pos.reduce((a, p) => a + p.risk_usd, 0);
    const dayPnl = PORTFOLIOS.reduce((a, p) => a + p.pnl_today, 0);

    // Correlation matrix from a small factor model, so it is a valid matrix.
    const L = { // market, rates, tech, gold, intl, financials
      XLK: [0.80, -0.10, 0.50, 0.00, 0.00, 0.00], GLD: [0.10, 0.30, 0.00, 0.80, 0.10, 0.00],
      IEF: [-0.25, 0.85, 0.00, 0.20, 0.00, -0.10], EFA: [0.75, 0.00, 0.05, 0.10, 0.50, 0.05],
      MSFT: [0.70, -0.10, 0.55, 0.00, 0.00, 0.00], COST: [0.60, 0.00, 0.05, 0.00, 0.00, 0.00],
      AVGO: [0.72, -0.10, 0.52, 0.00, 0.00, 0.00], JPM: [0.68, -0.20, 0.00, 0.00, 0.05, 0.55],
    };
    const tickers = pos.map((p) => p.ticker);
    const vec = (t) => L[t];
    const norm = (v) => Math.sqrt(v.reduce((a, x) => a + x * x, 0) + 0.12); // + idiosyncratic
    const matrix = tickers.map((a) => tickers.map((b) => {
      if (a === b) return 1;
      const va = vec(a), vb = vec(b);
      return va.reduce((s, x, i) => s + x * vb[i], 0) / (norm(va) * norm(vb));
    }));

    return {
      equity: totalEquity,
      limits: { risk_per_trade: 0.01, daily_loss: 0.02, circuit_breaker: 0.15, sector: 0.30, gross_exposure: 1.0 },
      risk_per_trade: { value: maxRisk, ticker: pos.find((p) => p.risk_pct_equity === maxRisk).ticker },
      open_risk: { usd: openRisk, pct: openRisk / totalEquity },
      daily_loss: { value: Math.min(0, dayPnl) / totalEquity, pnl: dayPnl },
      drawdown: { value: totalEquity / peakEquity - 1, peak: peakEquity },
      gross_exposure: pos.reduce((a, p) => a + p.market_value, 0) / totalEquity,
      exposure: { ticker: by('ticker'), sector: by('sector'), strategy: by('strategy') },
      correlation: { tickers, matrix, window: '60 trading days' },
    };
  }

  // ---------- signals ----------
  const SIGNALS = [
    { id: 'price', name: 'Price & trend', role: 'Picking stocks', status: 'active', hit_90d: 0.56, hit_all: 0.54, n: 1840, ic: 0.041, trust: 0.34,
      history: [0.52, 0.53, 0.55, 0.54, 0.53, 0.55, 0.56, 0.54, 0.55, 0.57, 0.56, 0.56], note: 'Moving averages, momentum, breakouts' },
    { id: 'analysts', name: 'Analyst opinions', role: 'Picking stocks', status: 'active', hit_90d: 0.55, hit_all: 0.53, n: 612, ic: 0.032, trust: 0.22,
      history: [0.51, 0.52, 0.52, 0.53, 0.54, 0.53, 0.52, 0.54, 0.55, 0.55, 0.54, 0.55], note: 'Changes to profit forecasts and price targets' },
    { id: 'macro', name: 'Economy', role: 'How much to invest', status: 'active', hit_90d: 0.58, hit_all: 0.55, n: 96, ic: 0.027, trust: 0.16,
      history: [0.50, 0.52, 0.54, 0.55, 0.56, 0.55, 0.57, 0.56, 0.58, 0.57, 0.58, 0.58], note: 'Interest rates, credit, economic data' },
    { id: 'news', name: 'News mood', role: 'Picking stocks', status: 'probation', hit_90d: 0.51, hit_all: 0.52, n: 2210, ic: 0.012, trust: 0.12,
      history: [0.54, 0.53, 0.53, 0.52, 0.52, 0.51, 0.52, 0.50, 0.51, 0.51, 0.50, 0.51], note: 'Tone of headlines; less useful since summer' },
    { id: 'options', name: 'Options bets', role: 'Picking stocks', status: 'probation', hit_90d: 0.53, hit_all: 0.51, n: 344, ic: 0.009, trust: 0.10,
      history: [0.49, 0.50, 0.50, 0.51, 0.50, 0.52, 0.51, 0.52, 0.53, 0.52, 0.53, 0.53], note: 'Unusually large bets in the options market' },
    { id: 'reddit', name: 'Reddit', role: 'Warnings only', status: 'risk_flag', hit_90d: 0.47, hit_all: 0.48, n: 905, ic: -0.018, trust: 0.06,
      history: [0.49, 0.48, 0.48, 0.47, 0.49, 0.48, 0.47, 0.46, 0.48, 0.47, 0.47, 0.47], note: 'Big hype usually means a drop later' },
    { id: 'stocktwits', name: 'StockTwits', role: 'Not available', status: 'unavailable', hit_90d: null, hit_all: null, n: 0, ic: null, trust: 0,
      history: [], note: 'Their data is closed to new users' },
  ];
  const SIGNAL_EVENTS = [
    { ts: '2026-11-20T20:58:00Z', source: 'Price', ticker: 'XLV', text: '50-day average moved above the 200-day average', score: 0.62 },
    { ts: '2026-11-20T20:41:00Z', source: 'Reddit', ticker: 'AMD', text: 'Mentions far above normal (hype warning)', score: -0.30 },
    { ts: '2026-11-20T19:12:00Z', source: 'Options', ticker: 'AMD', text: 'More than twice the usual bets on a rise', score: 0.21 },
    { ts: '2026-11-20T17:30:00Z', source: 'Analysts', ticker: 'JPM', text: 'A large bank cut its rating to Neutral, target $255', score: -0.25 },
    { ts: '2026-11-20T15:05:00Z', source: 'News', ticker: 'MSFT', text: 'Slightly negative tone across 48 headlines (cloud pricing)', score: -0.18 },
    { ts: '2026-11-20T13:00:00Z', source: 'Economy', ticker: '—', text: 'Gap between 10-year and 2-year rates shrank 0.09% this week', score: -0.05 },
    { ts: '2026-11-19T21:00:00Z', source: 'Price', ticker: 'MSFT', text: 'Sixth day in a row closing below the 50-day average', score: -0.40 },
  ];
  // ---------- journal ----------
  function buildJournal() {
    const j = [];
    let seq = 1;
    const add = (ts, type, ticker, strategy, detail, extra) => j.push(Object.assign({ id: `ev_${String(seq++).padStart(5, '0')}`, ts, type, ticker, strategy, detail }, extra || {}));
    add('2026-11-20T21:20:00Z', 'decision', 'QQQ', 'orb_5m', 'Idea: buy 18 if it breaks the morning high (44/100 sure, half size because the strategy is slipping)');
    add('2026-11-20T21:15:00Z', 'decision', 'AMD', 'conviction_ensemble', 'Idea: buy 22 at 164.80 (48/100 sure)');
    add('2026-11-20T21:15:00Z', 'risk', 'AMD', 'conviction_ensemble', 'APPROVED with a hype warning (Reddit). Could lose 0.25% of your money');
    add('2026-11-20T21:15:00Z', 'decision', 'JPM', 'conviction_ensemble', 'Idea: sell all 20 shares. Score dropped to +0.08');
    add('2026-11-20T21:12:00Z', 'decision', 'IEF', 'vol_target', 'Idea: buy 45 at 94.35 to bring risk up to target');
    add('2026-11-20T21:10:00Z', 'decision', 'MSFT', 'trend_200_50', 'Idea: sell all 14 shares. 6 closes below the 50-day average');
    add('2026-11-20T21:10:00Z', 'decision', 'XLV', 'trend_200_50', 'Idea: buy 70 at 148.20 (72/100 sure)');
    add('2026-11-20T21:10:00Z', 'risk', 'XLV', 'trend_200_50', 'APPROVED. Could lose 0.45% (limit 1%). Healthcare would be 9.4% of money (limit 30%)');
    add('2026-11-20T21:05:00Z', 'system', '—', '—', 'End of day: total money ' + Math.round(totalEquity).toLocaleString('en-US') + ' USD');
    add('2026-11-20T20:55:00Z', 'fill', 'NVDA', 'orb_5m', 'Sold 30 at 186.10 before the close. Lost $69', { qty: -30, price: 186.10 });
    add('2026-11-20T20:55:00Z', 'order', 'NVDA', 'orb_5m', 'Order to sell 30 at the close (end-of-day sell-off)');
    add('2026-11-20T15:41:00Z', 'fill', 'NVDA', 'orb_5m', 'Bought 30 at 188.40, 0.02% worse than expected', { qty: 30, price: 188.40 });
    add('2026-11-20T15:41:00Z', 'order', 'NVDA', 'orb_5m', 'Buy order for 30 triggered at 188.35 (morning high broken)');
    add('2026-11-20T15:36:00Z', 'risk', 'NVDA', 'orb_5m', 'APPROVED. Could lose 0.29%. Half size because the strategy is slipping');
    add('2026-11-20T15:35:00Z', 'decision', 'NVDA', 'orb_5m', 'Morning range set at 185.20 – 188.35. Waiting for a breakout');
    add('2026-11-20T14:30:00Z', 'system', '—', '—', 'Market open. All data sources working');
    add('2026-11-19T21:30:00Z', 'manual', 'AVGO', 'conviction_ensemble', 'You marked this as taken');
    add('2026-11-19T21:32:00Z', 'manual', 'TSLA', 'conviction_ensemble', 'You skipped this: "not sure enough, already a lot in this industry"');
    add('2026-11-19T21:10:00Z', 'risk', 'TSLA', 'conviction_ensemble', 'BLOCKED: stop-loss is 11.8% away, so the trade would be too small to make sense');
    add('2026-11-19T21:10:00Z', 'decision', 'TSLA', 'conviction_ensemble', 'Idea: buy 4 at 341.00 (39/100 sure)');
    add('2026-11-19T20:55:00Z', 'fill', 'QQQ', 'orb_5m', 'Sold 18 at 527.40. Made $41.40', { qty: -18, price: 527.40 });
    add('2026-11-19T16:02:00Z', 'fill', 'QQQ', 'orb_5m', 'Bought 18 at 525.10, 0.01% worse than expected', { qty: 18, price: 525.10 });
    add('2026-11-19T16:02:00Z', 'order', 'QQQ', 'orb_5m', 'Buy order for 18 triggered at 525.05');
    add('2026-11-18T21:10:00Z', 'risk', 'XLE', 'trend_200_50', 'BLOCKED: moves too much like stocks you already own');
    add('2026-11-18T21:10:00Z', 'decision', 'XLE', 'trend_200_50', 'Idea: buy 90 at 93.40 (52/100 sure)');
    add('2026-11-18T19:44:00Z', 'system', 'SPY', 'vwap_reversion', 'Strategy is paused, so no order. Idea saved for reference only');
    add('2026-11-18T14:31:00Z', 'system', '—', '—', 'Bounce back stays paused: it has lost money for 60 days');
    add('2026-11-17T21:12:00Z', 'fill', 'EFA', 'dual_momentum', 'Bought 20 more at 86.10, no fee', { qty: 20, price: 86.10 });
    add('2026-11-17T21:11:00Z', 'order', 'EFA', 'dual_momentum', 'Order to buy 20 at the close');
    add('2026-11-17T21:10:00Z', 'risk', 'EFA', 'dual_momentum', 'APPROVED. Could lose 0.21%');
    add('2026-11-14T21:10:00Z', 'system', '—', '—', 'Weekly money split: Morning breakout 15% → 12% (slipping), Conviction picks 15% → 18%');
    add('2026-11-13T16:20:00Z', 'system', 'GLD', 'trend_200_50', 'Stop-loss moved up from 232.00 to 236.10 to lock in gains');
    add('2026-11-12T14:29:00Z', 'system', '—', '—', 'Kill switch test: on at 14:29:02, off at 14:29:40');
    return j;
  }
  const JOURNAL = buildJournal();

  // ---------- alerts ----------
  const ALERTS = [
    { id: 'al_1', ts: '2026-11-20T21:15:00Z', severity: 'warning', title: 'Hype warning on AMD', body: 'People on Reddit are talking about AMD far more than usual. The AMD trade idea includes a warning.', read: false },
    { id: 'al_2', ts: '2026-11-20T21:10:00Z', severity: 'info', title: '6 new trade ideas', body: 'XLV, MSFT, IEF, AMD, JPM, QQQ. Look them over before Monday\'s open.', read: false },
    { id: 'al_3', ts: '2026-11-20T20:55:00Z', severity: 'info', title: 'Track B sold everything for the weekend', body: 'Sold NVDA at 186.10 for a $69 loss. Track B holds nothing now.', read: false },
    { id: 'al_4', ts: '2026-11-19T21:00:00Z', severity: 'warning', title: 'MSFT is close to its stop-loss', body: 'It\'s 3% above the stop-loss at 428. The system suggests selling.', read: true },
    { id: 'al_5', ts: '2026-11-18T14:31:00Z', severity: 'critical', title: 'Bounce back is still paused', body: 'It has lost money for 60 days. Check it before turning it back on.', read: true },
    { id: 'al_6', ts: '2026-11-14T21:10:00Z', severity: 'warning', title: 'Morning breakout is slipping', body: 'It has done much worse live than in testing for 40 days, so its share of money was cut to 12%.', read: true },
    { id: 'al_7', ts: '2026-11-12T14:29:00Z', severity: 'critical', title: 'Kill switch was turned on (test)', body: 'It was on for 38 seconds. No orders were waiting.', read: true },
    { id: 'al_8', ts: '2026-11-10T13:05:00Z', severity: 'info', title: 'Backup data source used', body: 'The main data source timed out for 3 stocks, so the backup was used. Nothing is missing.', read: true },
  ];
  const ALERT_SETTINGS = {
    telegram: { enabled: true, chat_id: '', bot_configured: true },
    email: { enabled: false, address: '' },
    events: { recommendations: true, fills: false, stop_near: true, risk_limit: true, circuit_breaker: true, kill_switch: true, strategy_status: true, daily_summary: true },
    min_severity: 'warning',
    quiet_hours: { enabled: true, from: '22:00', to: '07:00' },
  };

  // ---------- endpoint table ----------
  const state = { handled: {}, killSwitch: false };

  window.FF_MOCK = {
    'GET /api/summary': () => {
      const dayPnl = PORTFOLIOS.reduce((a, p) => a + p.pnl_today, 0);
      return {
        as_of: AS_OF.toISOString(),
        equity: totalEquity,
        start_capital: 100000,
        pnl_today: dayPnl,
        pnl_today_pct: dayPnl / (totalEquity - dayPnl),
        peak_equity: peakEquity,
        drawdown_pct: totalEquity / peakEquity - 1,
        market: marketStatus(new Date()),
        kill_switch: state.killSwitch,
        unread_alerts: ALERTS.filter((a) => !a.read).length,
        open_recommendations: RECS.filter((r) => !state.handled[r.id]).length,
      };
    },
    'GET /api/overview': () => ({
      as_of: AS_OF.toISOString(),
      dates: DATES,
      series: SERIES_DEF.map((d) => ({ id: d.id, name: d.name, kind: d.kind, values: CURVES[d.id].map((g) => g - 1) })),
      portfolios: PORTFOLIOS,
      benchmarks: SERIES_DEF.filter((d) => d.kind === 'benchmark').map((d) => ({ id: d.id, name: d.name, metrics: metrics(CURVES[d.id]) })),
    }),
    'GET /api/recommendations': () => recsPayload(state.handled),
    'POST /api/recommendations/:id/taken': (id) => { state.handled[id] = 'taken'; return { ok: true }; },
    'POST /api/recommendations/:id/skip': (id) => { state.handled[id] = 'skipped'; return { ok: true }; },
    'GET /api/positions': () => ({ as_of: AS_OF.toISOString(), equity: totalEquity, positions: positionsPayload(), flat_portfolios: ['track_b'] }),
    'GET /api/strategies': () => STRATEGIES,
    'GET /api/risk': () => riskPayload(),
    'GET /api/signals': () => SIGNALS,
    'GET /api/signals/events': () => SIGNAL_EVENTS,
    'GET /api/journal': () => JOURNAL,
    'GET /api/trades': () => [],
    'GET /api/alerts': () => ALERTS,
    'POST /api/alerts/:id/read': (id) => { const a = ALERTS.find((x) => x.id === id); if (a) a.read = true; return { ok: true }; },
    'GET /api/alerts/settings': () => JSON.parse(JSON.stringify(ALERT_SETTINGS)),
    'PUT /api/alerts/settings': (_, body) => { Object.assign(ALERT_SETTINGS, body); return { ok: true }; },
    'POST /api/alerts/test': (_, body) => ({ ok: true, channel: body && body.channel }),
    'POST /api/kill-switch': (_, body) => { state.killSwitch = !!(body && body.engaged); return { engaged: state.killSwitch }; },
    _journalAppend: (entry) => JOURNAL.unshift(Object.assign({ id: `ev_m${Date.now()}` }, entry)),
  };
})();
