/* Page renderers. Each page is a function (root) => void that lays out
 * panels; each panel owns one API call via FF.ui.panel. */
FF.pages = (function () {
  'use strict';
  const { fmt, tone, esc, $, $$, panel, badge, toast, store, ICON } = FF.ui;
  const api = FF.api;
  const charts = FF.charts;

  const SERIES_STYLE = {
    track_a: { color: 'var(--s1)', short: 'Track A' },
    track_b: { color: 'var(--s2)', short: 'Track B' },
    conviction: { color: 'var(--s3)', short: 'Conviction' },
    spy: { color: 'var(--b1)', short: 'SPY', dash: '6 4', bench: true },
    qqq: { color: 'var(--b2)', short: 'QQQ', dash: '2 3', bench: true },
    bal_60_40: { color: 'var(--b3)', short: '60/40', dash: '9 3 2 3', bench: true },
  };
  const PORTFOLIO_NAME = { track_a: 'Track A', track_b: 'Track B', conviction: 'Conviction' };
  const STRATEGY_NAME = {
    trend_200_50: 'Trend following', vol_target: 'Steady risk', dual_momentum: 'Momentum', conviction_ensemble: 'Conviction picks',
    orb_5m: 'Morning breakout', vwap_reversion: 'Bounce back', pead: 'After-earnings drift', gap_fade: 'Gap fade',
    // live backend ids
    track_a_systematic: 'Trend + risk parity', core_60_40: '60/40 core', trend_etf: 'Trend following',
    risk_parity: 'Risk parity', xs_momentum: 'Momentum rotation',
  };
  const pname = (id) => PORTFOLIO_NAME[id] || id;
  const sname = (id) => STRATEGY_NAME[id] || id;
  // One request shared by several panels; a failed request is retried on the next call.
  function shared(path) {
    let p = null;
    return () => { if (!p) p = api.get(path).catch((e) => { p = null; throw e; }); return p; };
  }
  const signed = (v, f) => `<span class="${tone(v)}">${esc(f(v))}</span>`;

  function head(root, title, sub, actions = '') {
    root.insertAdjacentHTML('beforeend', `<div class="page-head"><div><h1>${esc(title)}</h1>${sub ? `<p>${sub}</p>` : ''}</div><div class="toolbar">${actions}</div></div>`);
  }
  function block(root, cls = '') {
    const d = document.createElement('section');
    if (cls) d.className = cls;
    root.appendChild(d);
    return d;
  }
  function seg(name, options, value) {
    return `<div class="seg" role="group" data-seg="${name}">${options.map(([v, l]) => `<button type="button" data-v="${v}" aria-pressed="${v === value}">${esc(l)}</button>`).join('')}</div>`;
  }
  function onSeg(scope, name, cb) {
    const g = $(`[data-seg="${name}"]`, scope);
    if (!g) return;
    g.addEventListener('click', (e) => {
      const b = e.target.closest('button');
      if (!b) return;
      $$('button', g).forEach((x) => x.setAttribute('aria-pressed', x === b));
      cb(b.dataset.v);
    });
  }

  // ======================= 1. OVERVIEW =======================
  function overview(root) {
    head(root, 'Overview', 'How your practice portfolios are doing against the market since you started. This is practice money, and past results don\'t promise future ones.');
    const curve = block(root);
    const grid = block(root, 'grid');
    grid.style.marginTop = '14px';
    const dd = block(grid);
    const kpis = block(root, 'grid grid--4');
    kpis.style.marginTop = '14px';

    const visible = store('overview.visible') || { track_a: true, track_b: true, conviction: true, spy: true, qqq: false, bal_60_40: true };
    let range = store('overview.range') || 'all';
    let view = 'chart';

    const load = shared('/api/overview');

    panel(curve, {
      title: 'Growth over time',
      sub: 'Percent gained or lost since the start of the period you pick',
      skeleton: 'chart',
      load,
      isEmpty: (d) => !d.series.length,
      empty: { title: 'No history yet', text: 'The chart fills in after the first trading day closes.' },
      render(body, d, ctx) {
        ctx.actions.innerHTML = seg('range', [['3m', '3M'], ['6m', '6M'], ['ytd', 'YTD'], ['1y', '1Y'], ['all', 'All']], range) + seg('view', [['chart', 'Chart'], ['table', 'Table']], view);
        body.innerHTML = `<div class="chips" id="seriesChips">${d.series.map((s) => {
          const st = SERIES_STYLE[s.id];
          return `<button class="chip" type="button" data-id="${s.id}" aria-pressed="${!!visible[s.id]}" style="--c:${st.color}"><i class="key ${st.dash ? 'key--dash' : ''}" style="color:${st.color}"></i>${esc(s.name)}</button>`;
        }).join('')}</div><div id="curveChart" style="margin-top:10px"></div>`;
        const draw = () => {
          const start = rangeStart(d.dates, range);
          const dates = d.dates.slice(start);
          const series = d.series.filter((s) => visible[s.id]).map((s) => {
            const base = 1 + s.values[start];
            return Object.assign({ id: s.id, name: s.name, values: s.values.slice(start).map((v) => (1 + v) / base - 1) }, SERIES_STYLE[s.id]);
          });
          const el = $('#curveChart', body);
          if (!series.length) { el._ro && el._ro.disconnect(); el.innerHTML = FF.ui.emptyHTML({ title: 'Nothing selected', text: 'Pick at least one line above.' }); return; }
          if (view === 'table') { el._ro && el._ro.disconnect(); el.innerHTML = monthlyTable(dates, series); return; }
          charts.line(el, { dates, series, height: 320, label: 'Growth over time' });
          drawDrawdown(d, start);
        };
        $('#seriesChips', body).addEventListener('click', (e) => {
          const b = e.target.closest('.chip');
          if (!b) return;
          visible[b.dataset.id] = !visible[b.dataset.id];
          b.setAttribute('aria-pressed', visible[b.dataset.id]);
          store('overview.visible', visible);
          draw();
        });
        onSeg(ctx.actions, 'range', (v) => { range = v; store('overview.range', v); draw(); });
        onSeg(ctx.actions, 'view', (v) => { view = v; draw(); });
        draw();
      },
    });

    let ddBody = null;
    function drawDrawdown(d, start) {
      if (!ddBody) return;
      const dates = d.dates.slice(start);
      const series = d.series.filter((s) => s.kind === 'portfolio' && visible[s.id]).map((s) => {
        let peak = -Infinity;
        const vals = s.values.slice(start).map((v) => { peak = Math.max(peak, 1 + v); return (1 + v) / peak - 1; });
        return Object.assign({ id: s.id, name: s.name, values: vals }, SERIES_STYLE[s.id]);
      });
      if (!series.length) { ddBody.innerHTML = FF.ui.emptyHTML({ title: 'No portfolio selected' }); return; }
      charts.line(ddBody, { dates, series, height: 150, label: 'Drops from the high', endLabels: true });
    }
    panel(dd, {
      title: 'Drops from the high',
      sub: 'How far each portfolio has fallen from its best point. At −15% the system sells everything.',
      skeleton: 'chart',
      load,
      isEmpty: (d) => !d.series.length,
      empty: { title: 'No history yet' },
      render(body, d) {
        body.innerHTML = '<div id="ddChart"></div>';
        ddBody = $('#ddChart', body);
        drawDrawdown(d, rangeStart(d.dates, range));
      },
    });

    // KPI cards: one per portfolio + benchmarks, all from the same response.
    ['track_a', 'track_b', 'conviction'].forEach((id) => {
      panel(block(kpis, 'kpi-card'), {
        load,
        isEmpty: (d) => !d.portfolios.length,
        empty: { title: 'No numbers yet' },
        render(body, d, ctx) {
          const p = d.portfolios.find((x) => x.id === id);
          const m = p.metrics;
          ctx.el.querySelector('.panel__body').classList.add('panel__body--flush');
          body.style.padding = '0';
          body.innerHTML = `
            <div class="panel__head"><div class="title-row"><span class="swatch" style="background:${SERIES_STYLE[id].color}"></span><span class="panel__title">${esc(p.name)}</span></div><span class="muted mono">started with ${esc(fmt.usd(p.start_capital))}</span></div>
            <div class="kpi-hero"><span class="kpi-hero__v">${esc(fmt.usd(p.equity))}</span><span class="${tone(m.total_return)} mono">${esc(fmt.pctSigned(m.total_return))}</span><span class="kpi-hero__s">${esc(fmt.usdSigned(p.pnl_today))} today</span></div>
            <div class="kpi-grid">
              ${kpi('Per year', signed(m.annualized_return, (v) => fmt.pctSigned(v)), TIPS.annual)}
              ${kpi('Sharpe', esc(fmt.num(m.sharpe)), TIPS.sharpe)}
              ${kpi('Sortino', esc(fmt.num(m.sortino)), TIPS.sortino)}
              ${kpi('Calmar', esc(fmt.num(m.calmar)), TIPS.calmar)}
              ${kpi('Biggest drop', `<span class="down">${esc(fmt.pctSigned(m.max_drawdown))}</span>`, TIPS.maxdd)}
              ${kpi('Winning trades', esc(fmt.pct(m.win_rate, 0)), TIPS.win)}
              ${kpi('Profit factor', `<span class="${m.profit_factor < 1 ? 'down' : ''}">${esc(fmt.num(m.profit_factor))}</span>`, TIPS.pf)}
              ${kpi('Time invested', esc(fmt.pct(m.exposure, 0)), TIPS.exposure)}
              ${kpi('Trades', esc(fmt.int(m.trades)))}
            </div>`;
        },
      });
    });
    panel(block(kpis, 'kpi-card'), {
      load,
      isEmpty: (d) => !d.benchmarks.length,
      empty: { title: 'No market data' },
      render(body, d) {
        body.style.padding = '0';
        body.innerHTML = `<div class="panel__head"><span class="panel__title">The market</span><span class="muted">if you just bought and held</span></div>
          <div class="table-wrap" style="margin-top:8px"><table class="t"><thead><tr><th></th><th class="num">Total</th><th class="num" title="${esc(TIPS.sharpe)}">Sharpe</th><th class="num" style="white-space:normal">Worst drop</th></tr></thead><tbody>
          ${d.benchmarks.map((b) => `<tr><td><span class="title-row"><i class="key key--dash" style="color:${SERIES_STYLE[b.id].color}"></i>${esc(b.name)}</span></td>
            <td class="num ${tone(b.metrics.total_return)}">${esc(fmt.pctSigned(b.metrics.total_return))}</td>
            <td class="num">${esc(fmt.num(b.metrics.sharpe))}</td>
            <td class="num down">${esc(fmt.pctSigned(b.metrics.max_drawdown))}</td></tr>`).join('')}
          </tbody></table></div>
          <p class="hint" style="padding:10px 14px">Track A tries to earn about what the market does, with smaller drops. It isn\'t trying to win when the market is racing up.</p>`;
      },
    });
  }
  const kpi = (l, v, t) => `<div class="kpi"${t ? ` title="${esc(t)}"` : ''}><div class="kpi__l">${esc(l)}${t ? ' <span class="info-i" aria-hidden="true">i</span>' : ''}</div><div class="kpi__v">${v}</div>${t ? `<span class="sr-only">${esc(t)}</span>` : ''}</div>`;
  // Plain-English meaning of each number, shown on hover.
  const TIPS = {
    annual: 'Average gain or loss per year.',
    sharpe: 'Gain compared with how much the value bounces around. Higher is better; above 1 is good.',
    sortino: 'Like Sharpe, but only counts the drops, not the jumps up. Higher is better.',
    calmar: 'Yearly gain divided by the biggest drop. Higher is better.',
    maxdd: 'The largest fall from a high point before it recovered.',
    win: 'Share of trades that made money.',
    pf: 'Dollars won for every dollar lost. Above 1 means it made money overall.',
    exposure: 'Share of the time the money was invested rather than sitting in cash.',
  };

  function rangeStart(dates, range) {
    const last = new Date(dates[dates.length - 1] + 'T12:00:00Z');
    let from;
    if (range === 'all') return 0;
    if (range === 'ytd') from = `${last.getUTCFullYear()}-01-01`;
    else {
      const months = { '3m': 3, '6m': 6, '1y': 12 }[range];
      const d = new Date(last); d.setUTCMonth(d.getUTCMonth() - months);
      from = d.toISOString().slice(0, 10);
    }
    const i = dates.findIndex((d) => d >= from);
    return Math.max(0, i - (i > 0 ? 1 : 0));
  }

  function monthlyTable(dates, series) {
    const rows = [];
    dates.forEach((d, i) => { if (i === dates.length - 1 || d.slice(0, 7) !== dates[i + 1].slice(0, 7)) rows.push(i); });
    return `<div class="table-wrap" style="max-height:340px"><table class="t"><thead><tr><th>End of month</th>${series.map((s) => `<th class="num">${esc(s.short)}</th>`).join('')}</tr></thead><tbody>
      ${rows.reverse().map((i) => `<tr><td class="mono">${esc(dates[i])}</td>${series.map((s) => `<td class="num ${tone(s.values[i])}">${esc(fmt.pctSigned(s.values[i]))}</td>`).join('')}</tr>`).join('')}
      </tbody></table></div>`;
  }

  // ======================= 2. RECOMMENDATIONS =======================
  function recommendations(root, app) {
    head(root, 'Trade ideas', 'Buy and sell ideas from the latest run. You make the trades yourself, then mark each idea as taken or skipped so the log stays complete.',
      seg('pf', [['all', 'All'], ['track_a', 'Track A'], ['track_b', 'Track B'], ['conviction', 'Conviction']], 'all') +
      seg('dir', [['all', 'Both'], ['BUY', 'Buy'], ['SELL', 'Sell']], 'all'));
    const host = block(root, 'panel--bare');
    let pf = 'all', dir = 'all';
    const ctx = panel(host, {
      load: () => api.get('/api/recommendations'),
      isEmpty: (d) => !d.length,
      empty: { title: 'No trade ideas right now', text: 'New ideas come in after the market closes at 4 pm ET. You\'ll get an alert.' },
      render: (body, d) => drawRecs(body, d),
    });
    onSeg(root, 'pf', (v) => { pf = v; if (ctx.data) drawRecs(ctx.body, ctx.data); });
    onSeg(root, 'dir', (v) => { dir = v; if (ctx.data) drawRecs(ctx.body, ctx.data); });

    function drawRecs(body, all) {
      const list = all.filter((r) => (pf === 'all' || r.portfolio === pf) && (dir === 'all' || r.direction === dir)).sort((a, b) => b.confidence - a.confidence);
      if (!all.length) { body.innerHTML = FF.ui.emptyHTML({ title: 'You\'re all done', text: 'Every idea is marked as taken or skipped. You can find them in the Activity log.' }); return; }
      if (!list.length) { body.innerHTML = FF.ui.emptyHTML({ title: 'Nothing matches these filters' }); return; }
      const halted = app.killSwitch;
      body.innerHTML = `<div class="rec-grid">${list.map((r) => recCard(r, halted)).join('')}</div>`;
      body.querySelector('.rec-grid').addEventListener('click', async (e) => {
        const b = e.target.closest('button[data-act]');
        if (!b) return;
        const card = b.closest('.rec');
        const id = card.dataset.id;
        const rec = all.find((x) => x.id === id);
        const act = b.dataset.act;
        $$('button', card).forEach((x) => { x.disabled = true; });
        try {
          await api.post(`/api/recommendations/${encodeURIComponent(id)}/${act}`);
          if (api.mode !== 'live') FF_MOCK._journalAppend({ ts: new Date().toISOString(), type: 'manual', ticker: rec.ticker, strategy: rec.strategy, detail: `You ${act === 'taken' ? 'marked this as taken' : 'skipped this'}: ${rec.direction === 'BUY' ? 'buy' : 'sell'} ${rec.shares} at ${rec.entry.toFixed(2)}` });
          card.classList.add('leaving');
          setTimeout(() => {
            ctx.data = all.filter((x) => x.id !== id);
            drawRecs(body, ctx.data);
            app.refreshSummary();
          }, 200);
          toast(`${rec.ticker} ${act === 'taken' ? 'marked as taken' : 'skipped'}. Saved to the log.`);
        } catch (err) {
          $$('button', card).forEach((x) => { x.disabled = false; });
          toast('Couldn\'t save that. Please try again.');
        }
      });
    }
  }

  function recCard(r, halted) {
    const buy = r.direction === 'BUY';
    const stopPct = r.stop == null ? null : (r.stop - r.entry) / r.entry;
    const tpPct = r.take_profit == null ? null : (r.take_profit - r.entry) / r.entry;
    const confLabel = r.confidence >= 70 ? 'Fairly sure' : r.confidence >= 50 ? 'Somewhat sure' : 'Not very sure';
    return `<article class="panel rec" data-id="${esc(r.id)}">
      <div class="rec__head">
        <div>
          <div class="rec__tick"><span class="ticker">${esc(r.ticker)}</span><span class="dir dir--${buy ? 'buy' : 'sell'}">${esc(r.direction)}</span></div>
          <div class="rec__meta">${esc(sname(r.strategy))} · ${esc(pname(r.portfolio))} · ${esc(r.horizon)}</div>
        </div>
        <div class="conf" title="How sure the system is, from 0 to 100">
          <div class="conf__v">${r.confidence}<small>/100</small></div>
          <div class="conf__bar"><i style="width:${r.confidence}%"></i></div>
          <div class="conf__l">${confLabel}</div>
        </div>
      </div>
      <div class="levels">
        <div><span>${buy ? 'Buy' : 'Sell'} at</span><b>${esc(fmt.price(r.entry))}</b><em>&nbsp;</em></div>
        <div><span>Cut losses at</span><b>${esc(fmt.price(r.stop))}</b><em>${esc(fmt.pctSigned(stopPct))}</em></div>
        <div><span>Take profit at</span><b>${esc(fmt.price(r.take_profit))}</b><em>${esc(fmt.pctSigned(tpPct))}</em></div>
        <div><span>Amount</span><b>${esc(r.shares)} shares</b><em>${esc(fmt.usd(r.position_value))}</em></div>
      </div>
      <div class="rat">
        <div><span class="k">Why</span><span>${esc(r.signals)}</span></div>
        <div><span class="k">Watch out</span><span>${esc(r.risk)}</span></div>
        <div><span class="k">Drop it if</span><span>${esc(r.invalidation)}</span></div>
      </div>
      <div class="rec__foot">
        <span class="muted" title="What you lose if the stop-loss is hit, and how much bigger the possible gain is">Could lose ${esc(fmt.usd(r.risk_usd))} (${esc(fmt.pct(r.risk_pct_equity, 2))})${r.reward_risk == null ? '' : ` · could gain ${esc(r.reward_risk.toFixed(1))}×`}</span>
        <span class="btns">
          <button class="btn" data-act="skip" ${halted ? 'disabled title="Trading is stopped"' : ''}>Skip</button>
          <button class="btn btn--primary" data-act="taken" ${halted ? 'disabled title="Trading is stopped"' : ''}>Mark as taken</button>
        </span>
      </div>
    </article>`;
  }

  // ======================= 3. POSITIONS =======================
  function positions(root) {
    head(root, 'Holdings', 'What you own right now, priced at the last close. "At risk" is what you\'d lose if every stop-loss is hit.');
    const summary = block(root, 'grid grid--4');
    const comp = block(root, 'grid grid--2');
    comp.style.marginTop = '14px';
    const tbl = block(root);
    tbl.style.marginTop = '14px';
    const req = shared('/api/positions');

    // What each strategy's money is in right now (only the live backend sends `sleeves`).
    req().then((d) => {
      (d.sleeves || []).forEach((sl) => panel(block(comp), {
        title: `${sl.name} · ${pname(sl.portfolio)}`,
        sub: sl.rules,
        load: req,
        isEmpty: () => false,
        render(body) {
          const note = sl.holdings.length
            ? `Share of this strategy's money in each holding. ${esc(fmt.pct(sl.invested_pct, 0))} invested; the rest waits in cash earning the T-bill rate.`
            : 'Everything is in cash right now: no holding passes this strategy\'s rules.';
          body.innerHTML = `<div class="kpi-grid" style="margin-bottom:12px">
              <div><div class="kpi__l">Started with</div><div class="mono">${esc(fmt.usd(sl.start_capital))}</div></div>
              <div><div class="kpi__l">Worth now</div><div class="mono">${esc(fmt.usd(sl.equity))} <span class="${tone(sl.return_pct)}">${esc(fmt.pctSigned(sl.return_pct))}</span></div></div>
              <div><div class="kpi__l">Cash</div><div class="mono">${esc(fmt.usd(sl.cash))} <span class="muted">${esc(fmt.pct(1 - sl.invested_pct, 0))}</span></div></div>
            </div><div class="sl-bars"></div>
            <p class="hint" style="margin-top:10px">${note}</p>`;
          if (sl.holdings.length) charts.bars($('.sl-bars', body), sl.holdings, { scaleMax: Math.max(0.3, ...sl.holdings.map((h) => h.pct)) * 1.1 });
        },
      }));
    }).catch(() => {});

    panel(summary, {
      load: req,
      isEmpty: () => false,
      render(body, d, ctx) {
        // render summary tiles in place of the panel wrapper
        const p = d.positions;
        const mv = p.reduce((a, x) => a + x.market_value, 0);
        const up = p.reduce((a, x) => a + x.unrealized_pnl, 0);
        const risk = p.reduce((a, x) => a + x.risk_usd, 0);
        const day = p.reduce((a, x) => a + x.day_pnl, 0);
        ctx.el.className = 'grid grid--4 grid--tiles';
        ctx.el.innerHTML = [
          ['Worth now', fmt.usd(mv), `${fmt.pct(mv / d.equity, 0)} invested · ${fmt.usd(d.equity - mv)} in cash`],
          ['Gain or loss so far', fmt.usdSigned(up), `On ${p.length} holdings, not sold yet`, tone(up)],
          ['Today', fmt.usdSigned(day), 'Change since yesterday\'s close', tone(day)],
          ['At risk', fmt.usd(risk), `${fmt.pct(risk / d.equity, 2)} of your money`],
        ].map(([l, v, s, t]) => `<div class="panel"><div class="panel__body"><div class="kpi__l">${esc(l)}</div><div class="kpi-hero__v ${t || ''}" style="margin-top:4px">${esc(v)}</div><div class="kpi-hero__s" style="margin-top:4px">${esc(s)}</div></div></div>`).join('');
      },
    });

    let sort = { key: 'market_value', dir: -1 };
    panel(tbl, {
      title: 'Holdings',
      sub: 'Tap a column name to sort. Tap a holding to see why it was bought and its trades.',
      flush: true,
      load: req,
      isEmpty: (d) => !d.positions.length,
      empty: { title: 'You don\'t own anything yet', text: 'Everything is in cash. Holdings show up here after a trade goes through.' },
      render(body, d) {
        const cols = [
          ['ticker', 'Stock'], ['portfolio', 'Portfolio'], ['strategy', 'Strategy'], ['shares', 'Shares', 1], ['entry', 'Paid', 1], ['last', 'Now', 1],
          ['market_value', 'Worth', 1], ['day_pnl', 'Today', 1], ['unrealized_pnl', 'Gain/loss', 1], ['unrealized_pct', '%', 1],
          ['risk_usd', 'At risk', 1], ['risk_pct_equity', 'At risk %', 1], ['stop', 'Stop-loss', 1], ['stop_distance_pct', 'Room to stop', 1], ['days_held', 'Days held', 1],
        ];
        const draw = () => {
          const rows = [...d.positions].sort((a, b) => (a[sort.key] > b[sort.key] ? 1 : a[sort.key] < b[sort.key] ? -1 : 0) * sort.dir);
          const tot = (k) => d.positions.reduce((a, x) => a + x[k], 0);
          body.innerHTML = `<div class="table-wrap"><table class="t"><thead><tr>${cols.map(([k, l, n]) => `<th class="sortable ${n ? 'num' : ''}" data-k="${k}" ${sort.key === k ? `aria-sort="${sort.dir > 0 ? 'ascending' : 'descending'}"` : ''}>${esc(l)}</th>`).join('')}</tr></thead>
            <tbody>${rows.map((p, i) => `<tr data-i="${i}" style="${p.why !== undefined ? 'cursor:pointer' : ''}">
              <td><div class="ticker">${esc(p.ticker)}</div><div class="tname">${esc(p.name)}</div></td>
              <td>${esc(pname(p.portfolio))}</td><td>${esc(sname(p.strategy))}</td>
              <td class="num">${esc(p.shares)}</td><td class="num">${esc(fmt.price(p.entry))}</td><td class="num">${esc(fmt.price(p.last))}</td>
              <td class="num">${esc(fmt.usd(p.market_value))}</td>
              <td class="num ${tone(p.day_pnl)}">${esc(fmt.usdSigned(p.day_pnl))}</td>
              <td class="num ${tone(p.unrealized_pnl)}">${esc(fmt.usdSigned(p.unrealized_pnl))}</td>
              <td class="num ${tone(p.unrealized_pct)}">${esc(fmt.pctSigned(p.unrealized_pct))}</td>
              <td class="num">${esc(fmt.usd(p.risk_usd))}</td>
              <td class="num">${riskCell(p.risk_pct_equity)}</td>
              <td class="num">${esc(fmt.price(p.stop))}</td>
              <td class="num">${stopCell(p.stop_distance_pct)}</td>
              <td class="num">${esc(p.days_held)}</td></tr>${open.has(keyOf(p)) ? detailRow(p, cols.length) : ''}`).join('')}</tbody>
            <tfoot><tr><td colspan="6">Total · ${d.positions.length} holdings</td>
              <td class="num">${esc(fmt.usd(tot('market_value')))}</td>
              <td class="num ${tone(tot('day_pnl'))}">${esc(fmt.usdSigned(tot('day_pnl')))}</td>
              <td class="num ${tone(tot('unrealized_pnl'))}">${esc(fmt.usdSigned(tot('unrealized_pnl')))}</td><td></td>
              <td class="num">${esc(fmt.usd(tot('risk_usd')))}</td><td class="num">${esc(fmt.pct(tot('risk_usd') / d.equity, 2))}</td><td colspan="3"></td></tr></tfoot>
            </table></div>
            ${d.flat_portfolios && d.flat_portfolios.length ? `<div class="panel__foot">${d.flat_portfolios.map(pname).join(', ')} owns nothing right now. It only trades during the day and sells everything before 4 pm ET.</div>` : ''}`;
          $$('th.sortable', body).forEach((th) => th.addEventListener('click', () => {
            sort = { key: th.dataset.k, dir: sort.key === th.dataset.k ? -sort.dir : -1 };
            draw();
          }));
          $$('tbody tr[data-i]', body).forEach((tr) => tr.addEventListener('click', () => {
            const p = rows[+tr.dataset.i];
            if (p.why === undefined) return;
            if (open.has(keyOf(p))) open.delete(keyOf(p)); else open.add(keyOf(p));
            draw();
          }));
        };
        const open = new Set();
        draw();
      },
    });
  }
  const keyOf = (p) => `${p.ticker}/${p.strategy}`;
  function detailRow(p, span) {
    const hist = (p.history || []).map((h) => `<li><span class="mono muted">${esc(fmt.date(h.ts))}</span> ${esc(h.detail)}</li>`).join('');
    return `<tr class="detail"><td colspan="${span}" style="white-space:normal;background:var(--surface-2)">
      <div class="grid grid--2" style="gap:18px;padding:6px 2px">
        <div><div class="kpi__l">Why it's held (${esc(sname(p.strategy))})</div><div style="margin-top:4px;max-width:70ch">${esc(p.why || 'No decision recorded yet.')}</div>
          <p class="hint" style="margin-top:8px">Bought ${esc(p.opened ? fmt.date(p.opened) : '—')} · stop-loss ${esc(fmt.price(p.stop))} · the rules are checked again at each month end.</p></div>
        <div><div class="kpi__l">Trades in this holding</div><ul style="margin:4px 0 0;padding-left:16px">${hist || '<li class="muted">No trades recorded.</li>'}</ul></div>
      </div></td></tr>`;
  }
  function riskCell(v) {
    const [lvl] = FF.ui.level(v / 0.01);
    return `${esc(fmt.pct(v, 2))}${lvl !== 'good' ? ` <span class="dot dot--${lvl}" title="Getting close to the 1% limit per trade"></span>` : ''}`;
  }
  function stopCell(v) {
    const near = v < 0.02;
    return `<span title="How far the price can fall before the stop-loss sells">${esc(fmt.pct(v))}</span>${near ? ' <span class="dot dot--serious" title="Less than 2% above the stop-loss"></span>' : ''}`;
  }

  // ======================= 4. STRATEGIES =======================
  function strategies(root) {
    head(root, 'Strategies', 'Each strategy is a set of trading rules. "Tested" shows how the rules did on past data; "Live" shows how they\'re doing now with practice money. A big gap means a strategy may have stopped working.');
    const host = block(root);
    panel(host, {
      title: 'Ranking',
      sub: 'Best live results first. "Money" is each strategy\'s share of your practice money, updated weekly.',
      flush: true,
      load: () => api.get('/api/strategies'),
      isEmpty: (d) => !d.length,
      empty: { title: 'No strategies yet', text: 'Add one in the code and point a config file to it.' },
      render(body, d) {
        const rows = [...d].sort((a, b) => (b.live ? b.live.sharpe : -9) - (a.live ? a.live.sharpe : -9));
        const L = (s, k, f) => (s.live ? f(s.live[k]) : '<span class="muted">—</span>');
        body.innerHTML = `<div class="table-wrap"><table class="t">
          <thead>
            <tr class="group"><th colspan="5" style="background:var(--surface)"></th><th colspan="3" class="bl">Tested on past data</th><th colspan="3" class="bl">Live, practice money</th><th class="bl" style="background:var(--surface)"></th></tr>
            <tr><th>#</th><th>Strategy</th><th>Portfolio</th><th>Status</th><th>Money</th>
              <th class="num bl">Per year</th><th class="num" title="${esc(TIPS.sharpe)}">Sharpe</th><th class="num">Biggest drop</th>
              <th class="num bl">Per year</th><th class="num" title="${esc(TIPS.sharpe)}">Sharpe</th><th class="num">Biggest drop</th>
              <th class="num bl" title="Live Sharpe minus tested Sharpe. Below −0.5 is a warning.">Live vs tested</th></tr>
          </thead>
          <tbody>${rows.map((s, i) => {
            const gap = s.live && s.live.sharpe != null && s.backtest.sharpe != null ? s.live.sharpe - s.backtest.sharpe : null;
            return `<tr>
              <td class="muted mono">${i + 1}</td>
              <td style="white-space:normal;min-width:190px;max-width:260px"><div style="font-weight:600">${esc(s.name)}</div><div class="tname" title="${esc(s.id)}">${esc(s.desc || s.id)}${s.live_trades ? ' · ' + s.live_trades + ' trades' : ''}</div></td>
              <td title="${s.since ? 'Live since ' + esc(fmt.date(s.since)) : 'Not live yet'}">${esc(pname(s.portfolio))}</td>
              <td>${badge(s.status)}</td>
              <td class="mono">${esc(fmt.pct(s.allocation, 0))}<span class="minibar"><i style="width:${Math.min(100, s.allocation / 0.4 * 100)}%"></i></span></td>
              <td class="num bl ${tone(s.backtest.cagr)}">${esc(fmt.pctSigned(s.backtest.cagr))}</td>
              <td class="num">${esc(fmt.num(s.backtest.sharpe))}</td>
              <td class="num">${esc(fmt.pct(s.backtest.max_dd))}</td>
              <td class="num bl">${L(s, 'cagr', (v) => signed(v, (x) => fmt.pctSigned(x)))}</td>
              <td class="num">${L(s, 'sharpe', (v) => `<b class="${v < 0 ? 'down' : ''}">${esc(fmt.num(v))}</b>`)}</td>
              <td class="num">${L(s, 'max_dd', (v) => esc(fmt.pct(v)))}</td>
              <td class="num bl">${gap == null ? '<span class="muted">—</span>' : `<span class="${gap < -0.5 ? 'down' : ''}">${esc(fmt.num(gap))}</span>${gap < -0.5 ? ' <span class="dot dot--serious" title="Doing much worse live than in testing"></span>' : ''}`}</td>
            </tr>`;
          }).join('')}</tbody></table></div>
          <div class="panel__foot">${badge('research')} only tested on past data · ${badge('paper')} trading with practice money · ${badge('decaying')} doing worse than tested, gets less money · ${badge('paused')} stopped until you check it</div>`;
      },
    });
    const closed = block(root);
    closed.style.marginTop = '14px';
    panel(closed, {
      title: 'Finished trades',
      sub: 'Every holding that was bought and then fully sold, newest first.',
      flush: true,
      load: () => api.get('/api/trades'),
      isEmpty: (d) => !d.length,
      empty: { title: 'No finished trades yet', text: 'A trade shows up here once a holding is fully sold.' },
      render(body, d) {
        const why = (r) => (!r ? '—' : r.startsWith('stop hit') ? 'Stop-loss hit' : 'Rules said sell');
        body.innerHTML = `<div class="table-wrap"><table class="t"><thead><tr><th>Stock</th><th>Strategy</th><th>Bought</th><th>Sold</th><th class="num">Shares</th><th class="num">Paid</th><th class="num">Sold at</th><th class="num">Gain/loss</th><th class="num">%</th><th>Why sold</th></tr></thead>
          <tbody>${d.map((t) => `<tr><td class="ticker">${esc(t.ticker)}</td><td>${esc(sname(t.sleeve))}</td>
            <td>${esc(t.opened ? fmt.date(t.opened) : '—')}</td><td>${esc(fmt.date(t.closed))}</td><td class="num">${esc(t.shares)}</td>
            <td class="num">${esc(fmt.price(t.entry))}</td><td class="num">${esc(fmt.price(t.exit))}</td>
            <td class="num ${tone(t.pnl)}">${esc(fmt.usdSigned(t.pnl))}</td><td class="num ${tone(t.pnl_pct)}">${esc(fmt.pctSigned(t.pnl_pct))}</td>
            <td class="tname">${esc(why(t.reason))}</td></tr>`).join('')}</tbody></table></div>`;
      },
    });
  }

  // ======================= 5. RISK =======================
  function risk(root) {
    head(root, 'Risk', 'How close you are to each safety limit. The system checks these before every trade, and if your money drops 15% from its high, it sells everything.');
    const gauges = block(root, 'grid grid--4');
    const row = block(root, 'grid grid--2');
    row.style.marginTop = '14px';
    const load = shared('/api/risk');

    const g = [
      ['Biggest single risk', 'Most you could lose on one holding', (d) => ({ value: d.risk_per_trade.value, limit: d.limits.risk_per_trade, format: (v) => fmt.pct(v, 2), limitLabel: 'limit per trade', sub: `${esc(d.risk_per_trade.ticker)} · all holdings together: ${esc(fmt.pct(d.open_risk.pct, 2))}` })],
      ['Today\'s loss', 'Sold and unsold, added together', (d) => ({ value: d.daily_loss.value, limit: d.limits.daily_loss, format: (v) => fmt.pct(Math.abs(v), 2), limitLabel: 'daily limit', sub: d.daily_loss.pnl >= 0 ? `No loss today. Up ${esc(fmt.usd(d.daily_loss.pnl))}` : esc(fmt.usdSigned(d.daily_loss.pnl)) })],
      ['Drop from high', 'Compared with your best day', (d) => ({ value: d.drawdown.value, limit: d.limits.circuit_breaker, format: (v) => fmt.pct(Math.abs(v), 1), limitLabel: 'sell-everything point', sub: `Best day: ${esc(fmt.usd(d.drawdown.peak))}` })],
      ['Money invested', 'Share of your money in stocks', (d) => ({ value: d.gross_exposure, limit: d.limits.gross_exposure, format: (v) => fmt.pct(v, 0), limitLabel: 'max', sub: 'No borrowed money' })],
    ];
    g.forEach(([title, sub, f]) => panel(block(gauges), { title, sub, load, isEmpty: () => false, render: (body, d) => charts.gauge(body, Object.assign({ label: title }, f(d))) }));

    let by = 'sector';
    panel(block(row), {
      title: 'Where your money is',
      sub: 'Share of your total money. The dashed line is the limit, where there is one.',
      load,
      isEmpty: (d) => !d.exposure.ticker.length,
      empty: { title: 'Nothing invested', text: 'Everything is in cash.' },
      render(body, d, ctx) {
        ctx.actions.innerHTML = seg('by', [['ticker', 'Stock'], ['sector', 'Industry'], ['strategy', 'Strategy']], by);
        const draw = () => {
          const top = Math.max(0, ...d.exposure[by].map((r) => r.pct)) * 1.1; // keep live bars inside the track
          const opts = by === 'sector' ? { limit: d.limits.sector, scaleMax: Math.max(0.4, top) } : { scaleMax: Math.max(by === 'strategy' ? 0.4 : 0.15, top) };
          charts.bars(body, d.exposure[by], Object.assign(opts, { label: (r) => (by === 'strategy' ? sname(r.key) : r.key) }));
          if (by === 'sector' && d.limits.sector != null) body.insertAdjacentHTML('beforeend', `<p class="hint" style="margin-top:12px">No more than ${fmt.pct(d.limits.sector, 0)} in any one industry. Funds count toward the industry they hold.</p>`);
        };
        onSeg(ctx.actions, 'by', (v) => { by = v; draw(); });
        draw();
      },
    });
    panel(block(row), {
      title: 'Do your holdings move together?',
      sub: '1 = always move the same way, 0 = no link, −1 = opposite ways. Last 60 trading days.',
      load,
      isEmpty: (d) => d.correlation.tickers.length < 2,
      empty: { title: 'Need two or more holdings', text: 'This fills in once you own at least two things.' },
      render: (body, d) => charts.heatmap(body, d.correlation),
    });
  }

  // ======================= 6. SIGNALS =======================
  function signals(root) {
    head(root, 'Signals', 'Where trade ideas come from, how often each source has been right, and how much the system listens to it. Sources that stop working get listened to less.');
    const tbl = block(root);
    const ev = block(root);
    ev.style.marginTop = '14px';
    panel(tbl, {
      title: 'Sources',
      sub: '"Right" means the stock moved the way the source said within 5 days',
      flush: true,
      load: () => api.get('/api/signals'),
      isEmpty: (d) => !d.length,
      empty: { title: 'No sources set up yet' },
      render(body, d) {
        const maxW = Math.max(...d.map((s) => s.trust));
        body.innerHTML = `<div class="table-wrap"><table class="t"><thead><tr>
          <th>Source</th><th>Used for</th><th>Status</th><th class="num">Right, 90 days</th><th class="num">Right, all time</th><th class="num">Calls made</th><th class="num" title="Information coefficient: how well the signal lines up with what happened next. Above 0 helps, below 0 hurts.">Strength</th><th>How much we listen</th><th>Last 12 weeks</th></tr></thead>
          <tbody>${d.map((s) => `<tr>
            <td><div style="font-weight:600">${esc(s.name)}</div><div class="tname">${esc(s.note)}</div></td>
            <td class="sec">${esc(s.role)}</td>
            <td>${badge(s.status)}</td>
            <td class="num">${s.hit_90d == null ? '—' : `<b class="${s.hit_90d < 0.5 ? 'down' : ''}">${esc(fmt.pct(s.hit_90d, 0))}</b>`}</td>
            <td class="num">${esc(s.hit_all == null ? '—' : fmt.pct(s.hit_all, 0))}</td>
            <td class="num">${esc(fmt.int(s.n))}</td>
            <td class="num ${s.ic < 0 ? 'down' : ''}">${s.ic == null ? '—' : esc(fmt.num(s.ic, 3))}</td>
            <td class="mono">${esc(fmt.pct(s.trust, 0))}<span class="minibar"><i style="width:${maxW ? (s.trust / maxW) * 100 : 0}%"></i></span></td>
            <td>${charts.sparkline(s.history, { ref: 0.5, color: 'var(--s1)' })}</td></tr>`).join('')}</tbody></table></div>
          <div class="panel__foot">In the last column, the dashed line is 50%, the same as a coin flip. Reddit is only used to warn about hype, because heavy chatter usually comes before a drop.</div>`;
      },
    });
    panel(ev, {
      title: 'Latest signals',
      sub: 'What the sources said before the last run',
      flush: true,
      load: () => api.get('/api/signals/events'),
      isEmpty: (d) => !d.length,
      empty: { title: 'No new signals in the last 2 days' },
      render(body, d) {
        body.innerHTML = `<div class="table-wrap"><table class="t"><thead><tr><th>Time (NY)</th><th>Source</th><th>Stock</th><th>What happened</th><th class="num" title="Positive leans buy, negative leans sell">Score</th></tr></thead>
          <tbody>${d.map((e) => `<tr><td class="mono">${esc(fmt.ts(e.ts))}</td><td>${esc(e.source)}</td><td class="ticker">${esc(e.ticker)}</td><td class="wrap">${esc(e.text)}</td><td class="num ${tone(e.score)}">${esc(fmt.num(e.score))}</td></tr>`).join('')}</tbody></table></div>`;
      },
    });
  }

  // ======================= 7. JOURNAL =======================
  const TYPES = [['decision', 'Idea'], ['risk', 'Safety check'], ['order', 'Order'], ['fill', 'Trade done'], ['manual', 'You'], ['system', 'System']];
  function journal(root) {
    head(root, 'Activity log', 'Everything the system did: ideas, safety checks, orders and finished trades. Times are New York time.');
    const host = block(root);
    const f = { q: '', type: 'all', strategy: 'all', from: '', to: '' };
    panel(host, {
      flush: true,
      load: () => api.get('/api/journal'),
      isEmpty: (d) => !d.length,
      empty: { title: 'Nothing logged yet', text: 'Entries show up after the first run.' },
      render(body, all) {
        const strategiesUsed = [...new Set(all.map((e) => e.strategy).filter((s) => s && s !== '—'))];
        body.innerHTML = `<div class="toolbar" style="padding:0 14px 10px">
            <input type="search" placeholder="Search stock or text" aria-label="Search the log" id="jq" style="width:200px">
            <select id="jtype" aria-label="Entry type"><option value="all">Everything</option>${TYPES.map(([v, l]) => `<option value="${v}">${l}</option>`).join('')}</select>
            <select id="jstrat" aria-label="Strategy"><option value="all">All strategies</option>${strategiesUsed.map((s) => `<option value="${esc(s)}">${esc(sname(s))}</option>`).join('')}</select>
            <label class="field field--inline"><span>From</span><input type="date" id="jfrom"></label>
            <label class="field field--inline"><span>To</span><input type="date" id="jto"></label>
            <span class="grow"></span><span class="muted" id="jcount"></span>
            <button class="btn" id="jexport"><svg viewBox="0 0 16 16"><path d="M8 2v8M5 7l3 3 3-3M3 13h10"/></svg>Download</button>
          </div><div id="jtable"></div>`;
        const draw = () => {
          const q = f.q.toLowerCase();
          const rows = all.filter((e) => {
            const day = new Date(e.ts).toLocaleDateString('en-CA', { timeZone: 'America/New_York' });
            return (f.type === 'all' || e.type === f.type) && (f.strategy === 'all' || e.strategy === f.strategy) &&
              (!f.from || day >= f.from) && (!f.to || day <= f.to) &&
              (!q || (e.ticker + ' ' + e.detail).toLowerCase().includes(q));
          });
          $('#jcount', body).textContent = `Showing ${rows.length} of ${all.length}`;
          const t = $('#jtable', body);
          if (!rows.length) { t.innerHTML = FF.ui.emptyHTML({ title: 'Nothing matches', text: 'Try removing a filter or picking more dates.' }); return; }
          t.innerHTML = `<div class="table-wrap" style="max-height:calc(100vh - 260px)"><table class="t"><thead><tr><th>Time (NY)</th><th>Type</th><th>Stock</th><th>Strategy</th><th>What happened</th><th>ID</th></tr></thead>
            <tbody>${rows.map((e) => `<tr><td class="mono">${esc(fmt.ts(e.ts))}</td><td>${typeBadge(e)}</td><td class="ticker">${esc(e.ticker)}</td><td class="sec">${esc(e.strategy === '—' ? '—' : sname(e.strategy))}</td><td class="wrap">${esc(e.detail)}</td><td class="mono muted">${esc(e.id)}</td></tr>`).join('')}</tbody></table></div>`;
          t._rows = rows;
        };
        $('#jq', body).addEventListener('input', (e) => { f.q = e.target.value; draw(); });
        $('#jtype', body).addEventListener('change', (e) => { f.type = e.target.value; draw(); });
        $('#jstrat', body).addEventListener('change', (e) => { f.strategy = e.target.value; draw(); });
        $('#jfrom', body).addEventListener('change', (e) => { f.from = e.target.value; draw(); });
        $('#jto', body).addEventListener('change', (e) => { f.to = e.target.value; draw(); });
        $('#jexport', body).addEventListener('click', () => {
          const rows = $('#jtable', body)._rows || [];
          const csv = ['id,ts,type,ticker,strategy,detail'].concat(rows.map((e) => [e.id, e.ts, e.type, e.ticker, e.strategy, '"' + String(e.detail).replace(/"/g, '""') + '"'].join(','))).join('\n');
          const a = document.createElement('a');
          a.href = URL.createObjectURL(new Blob([csv], { type: 'text/csv' }));
          a.download = 'journal.csv';
          a.click();
          URL.revokeObjectURL(a.href);
        });
        draw();
      },
    });
  }
  function typeBadge(e) {
    if (e.type === 'risk') {
      const rej = /^BLOCKED/.test(e.detail);
      return `<span class="badge badge--${rej ? 'critical' : 'good'}"><span class="dot dot--${rej ? 'critical' : 'good'}"></span>${rej ? 'Blocked' : 'Approved'}</span>`;
    }
    const m = { decision: ['info', 'Idea'], order: ['', 'Order'], fill: ['', 'Trade done'], manual: ['', 'You'], system: ['', 'System'] }[e.type] || ['', e.type];
    return `<span class="badge ${m[0] ? 'badge--' + m[0] : ''}"><span class="dot ${m[0] ? 'dot--' + m[0] : 'dot--muted'}"></span>${m[1]}</span>`;
  }

  // ======================= 8. ALERTS =======================
  function alerts(root, app) {
    head(root, 'Alerts', 'Messages from the system, and where to send them. Urgent alerts always go out, even during quiet hours.');
    const row = block(root, 'grid grid--3');
    const feed = block(row, 'span-2');
    const settings = block(row);
    let only = 'all';

    panel(feed, {
      title: 'Messages',
      flush: true,
      load: () => api.get('/api/alerts'),
      isEmpty: (d) => !d.length,
      empty: { title: 'No messages', text: 'You\'re all caught up.' },
      render(body, d, ctx) {
        ctx.actions.innerHTML = seg('only', [['all', 'All'], ['unread', 'Unread']], only) + '<button class="btn btn--ghost" id="markAll">Mark all read</button>';
        const draw = () => {
          const list = d.filter((a) => only === 'all' || !a.read);
          if (!list.length) { body.innerHTML = FF.ui.emptyHTML({ title: 'Nothing unread', text: 'You\'re all caught up.' }); return; }
          body.innerHTML = `<ul class="feed">${list.map((a) => `<li class="${a.read ? '' : 'unread'}" data-id="${a.id}">
            <span class="sev sev--${a.severity}" title="${esc(a.severity)}">${ICON[a.severity]}</span>
            <div><div class="feed__t">${esc(a.title)} <span class="sr-only">(${esc(a.severity)})</span></div><div class="feed__b">${esc(a.body)}</div></div>
            <div style="text-align:right"><div class="feed__ts">${esc(fmt.ts(a.ts).slice(5, 16))}</div>${a.read ? '' : '<button class="btn btn--ghost" data-read style="height:22px;padding:0 6px;margin-top:4px">Mark read</button>'}</div></li>`).join('')}</ul>`;
        };
        body.addEventListener('click', async (e) => {
          const b = e.target.closest('[data-read]');
          if (!b) return;
          const id = b.closest('li').dataset.id;
          await api.post(`/api/alerts/${id}/read`);
          d.find((a) => a.id === id).read = true;
          draw(); app.refreshSummary();
        });
        onSeg(ctx.actions, 'only', (v) => { only = v; draw(); });
        $('#markAll', ctx.actions).addEventListener('click', async () => {
          await Promise.all(d.filter((a) => !a.read).map((a) => api.post(`/api/alerts/${a.id}/read`)));
          d.forEach((a) => { a.read = true; });
          draw(); app.refreshSummary();
        });
        draw();
      },
    });

    const EVENTS = [
      ['recommendations', 'New trade ideas'], ['fills', 'Every finished trade'], ['stop_near', 'A holding is close to its stop-loss'], ['risk_limit', 'Close to a safety limit'],
      ['circuit_breaker', 'System sold everything'], ['kill_switch', 'Kill switch turned on or off'], ['strategy_status', 'A strategy changes status'], ['daily_summary', 'Daily summary at 4:15 pm ET'],
    ];
    const sw = (name, on, label) => `<label class="setting-row"><span>${esc(label)}</span><span class="switch"><input type="checkbox" name="${name}" ${on ? 'checked' : ''} aria-label="${esc(label)}"><span></span></span></label>`;
    panel(settings, {
      title: 'Where to send alerts',
      load: () => api.get('/api/alerts/settings'),
      isEmpty: () => false,
      render(body, s) {
        body.innerHTML = `<form class="settings" id="alertForm">
          <fieldset><legend>Telegram ${s.telegram.bot_configured ? badge('active') : '<span class="badge">Not configured</span>'}</legend>
            ${sw('telegram.enabled', s.telegram.enabled, 'Send to Telegram')}
            <label class="field"><span>Chat ID</span><input type="text" name="telegram.chat_id" value="${esc(s.telegram.chat_id)}" placeholder="e.g. 123456789" inputmode="numeric"></label>
            <p class="hint">The bot key stays on the server (<code>TELEGRAM_BOT_TOKEN</code>) and is never shown here.</p>
            <div><button type="button" class="btn" data-test="telegram">Send test message</button></div>
          </fieldset>
          <fieldset><legend>Email</legend>
            ${sw('email.enabled', s.email.enabled, 'Send by email')}
            <label class="field"><span>Address</span><input type="email" name="email.address" value="${esc(s.email.address)}" placeholder="you@example.com"></label>
            <div><button type="button" class="btn" data-test="email">Send test email</button></div>
          </fieldset>
          <fieldset><legend>Tell me when</legend>${EVENTS.map(([k, l]) => sw('events.' + k, s.events[k], l)).join('')}</fieldset>
          <fieldset><legend>When to send</legend>
            <label class="setting-row"><span>Only send if at least</span><select name="min_severity">${['info', 'warning', 'critical'].map((v) => `<option value="${v}" ${s.min_severity === v ? 'selected' : ''}>${{ info: 'Info', warning: 'Warning', critical: 'Urgent' }[v]}</option>`).join('')}</select></label>
            ${sw('quiet_hours.enabled', s.quiet_hours.enabled, 'Quiet hours')}
            <div class="setting-row"><span>Quiet from / to (NY time)</span><span class="toolbar"><input type="time" name="quiet_hours.from" value="${esc(s.quiet_hours.from)}" aria-label="Quiet hours start"><input type="time" name="quiet_hours.to" value="${esc(s.quiet_hours.to)}" aria-label="Quiet hours end"></span></div>
          </fieldset>
          <div class="toolbar"><button type="submit" class="btn btn--primary">Save settings</button><span class="muted" id="saveState"></span></div>
        </form>`;
        const form = $('#alertForm', body);
        form.addEventListener('input', () => { $('#saveState', body).textContent = 'Unsaved changes'; });
        form.addEventListener('submit', async (e) => {
          e.preventDefault();
          const out = JSON.parse(JSON.stringify(s));
          $$('input, select', form).forEach((inp) => {
            const [a, b] = inp.name.split('.');
            const v = inp.type === 'checkbox' ? inp.checked : inp.value;
            if (b) out[a][b] = v; else out[a] = v;
          });
          await api.put('/api/alerts/settings', out);
          Object.assign(s, out);
          $('#saveState', body).textContent = 'Saved';
          toast('Alert settings saved');
        });
        $$('[data-test]', form).forEach((b) => b.addEventListener('click', async () => {
          await api.post('/api/alerts/test', { channel: b.dataset.test });
          toast(`Test ${b.dataset.test === 'email' ? 'email' : 'Telegram message'} sent`);
        }));
      },
    });
  }

  return { overview, recommendations, positions, strategies, risk, signals, journal, alerts };
})();
