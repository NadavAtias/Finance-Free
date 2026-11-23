/* Small SVG chart kit. Each function takes a container and plain data,
 * so it can be fed straight from an API response. Colors are CSS variables,
 * so light/dark themes swap without re-rendering. */
FF.charts = (function () {
  'use strict';
  const { esc, tip, fmt } = FF.ui;
  const NS = 'http://www.w3.org/2000/svg';

  function niceTicks(min, max, count = 5) {
    const span = max - min || 1;
    const step0 = span / count;
    const mag = Math.pow(10, Math.floor(Math.log10(step0)));
    const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => span / s <= count) || 10 * mag;
    const lo = Math.floor(min / step) * step;
    const hi = Math.ceil(max / step) * step;
    const out = [];
    for (let v = lo; v <= hi + step / 2; v += step) out.push(+v.toFixed(10));
    return out;
  }

  function observe(el, draw) {
    if (el._ro) el._ro.disconnect();
    let w = 0;
    el._ro = new ResizeObserver(() => {
      const nw = Math.round(el.clientWidth);
      if (nw && nw !== w) { w = nw; draw(nw); }
    });
    el._ro.observe(el);
  }

  /**
   * Multi-series line chart with crosshair + tooltip and direct end labels.
   * opts: { dates, series:[{id,name,color,dash,bench,values}], height, yFormat, tipFormat, endLabels, zeroLine }
   */
  function line(el, opts) {
    el.classList.add('chart');
    const yFormat = opts.yFormat || ((v) => fmt.pct(v, 0));
    const tipFormat = opts.tipFormat || ((v) => fmt.pctSigned(v, 2));
    const H = opts.height || 300;

    observe(el, (W) => {
      const series = opts.series.filter((s) => s.values && s.values.length);
      const n = opts.dates.length;
      const labels = opts.endLabels !== false && W > 560;
      const m = { t: 10, r: labels ? 118 : 12, b: 24, l: 44 };
      const iw = W - m.l - m.r, ih = H - m.t - m.b;
      let min = Infinity, max = -Infinity;
      series.forEach((s) => s.values.forEach((v) => { if (v < min) min = v; if (v > max) max = v; }));
      if (!isFinite(min)) { min = 0; max = 1; }
      if (opts.zeroLine !== false) { min = Math.min(min, 0); max = Math.max(max, 0); }
      const ticks = niceTicks(min, max, 5);
      const y0 = ticks[0], y1 = ticks[ticks.length - 1];
      const x = (i) => m.l + (n <= 1 ? 0 : (i / (n - 1)) * iw);
      const y = (v) => m.t + ih - ((v - y0) / (y1 - y0 || 1)) * ih;

      // x ticks: first trading day of evenly spaced months
      const xt = [];
      const monthStarts = [];
      opts.dates.forEach((d, i) => { if (i === 0 || d.slice(0, 7) !== opts.dates[i - 1].slice(0, 7)) monthStarts.push(i); });
      const every = Math.max(1, Math.ceil(monthStarts.length / Math.max(2, Math.floor(iw / 90))));
      monthStarts.forEach((i, k) => { if (k % every === 0) xt.push(i); });

      const first = !el._drawn;
      let s = `<svg width="${W}" height="${H}" viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(opts.label || 'Line chart')}">`;
      ticks.forEach((t) => {
        s += `<line class="${Math.abs(t) < 1e-9 ? 'zero' : 'gridline'}" x1="${m.l}" x2="${m.l + iw}" y1="${y(t)}" y2="${y(t)}"/>`;
        s += `<text x="${m.l - 8}" y="${y(t) + 3.5}" text-anchor="end">${esc(yFormat(t))}</text>`;
      });
      xt.forEach((i) => { s += `<text x="${x(i)}" y="${H - 6}" text-anchor="middle">${esc(fmt.dateShort(opts.dates[i]))}</text>`; });

      // area (single-series underwater charts)
      series.forEach((ser) => {
        if (!ser.area) return;
        let d = `M${x(0)},${y(0)}`;
        ser.values.forEach((v, i) => { d += `L${x(i).toFixed(1)},${y(v).toFixed(1)}`; });
        d += `L${x(n - 1)},${y(0)}Z`;
        s += `<path class="${first ? 'area--draw' : ''}" d="${d}" style="fill:${ser.color};opacity:.14"/>`;
      });
      // lines: benchmarks first so portfolios sit on top
      [...series].sort((a, b) => (b.bench ? 1 : 0) - (a.bench ? 1 : 0)).forEach((ser) => {
        let d = '';
        ser.values.forEach((v, i) => { d += (i ? 'L' : 'M') + x(i).toFixed(1) + ',' + y(v).toFixed(1); });
        const anim = first && !ser.dash;
        s += `<path class="series ${ser.bench ? 'series--bench' : ''} ${anim ? 'series--draw' : ''}" d="${d}" style="stroke:${ser.color}" ${anim ? 'pathLength="1"' : ''} ${ser.dash ? `stroke-dasharray="${ser.dash}"` : ''}/>`;
      });

      // direct end labels with simple collision avoidance
      if (labels) {
        const ends = series.map((ser) => ({ ser, v: ser.values[n - 1], y: y(ser.values[n - 1]) })).sort((a, b) => a.y - b.y);
        for (let i = 1; i < ends.length; i++) if (ends[i].y - ends[i - 1].y < 13) ends[i].y = ends[i - 1].y + 13;
        const over = ends.length ? ends[ends.length - 1].y - (m.t + ih) : 0;
        if (over > 0) ends.forEach((e) => { e.y -= over; });
        ends.forEach((e) => {
          s += `<line x1="${x(n - 1) + 3}" x2="${x(n - 1) + 9}" y1="${y(e.v)}" y2="${e.y}" style="stroke:${e.ser.color}" stroke-width="1"/>`;
          s += `<text class="end-label" x="${x(n - 1) + 12}" y="${e.y + 3.5}">${esc(e.ser.short || e.ser.name)} <tspan style="font-weight:500;fill:var(--text-3)">${esc(tipFormat(e.v))}</tspan></text>`;
        });
      }

      s += `<g class="hover" style="display:none"><line class="crosshair" y1="${m.t}" y2="${m.t + ih}"/>`;
      series.forEach((ser) => { s += `<circle class="hover-dot" r="4" style="fill:${ser.color}" data-id="${esc(ser.id)}"/>`; });
      s += `</g><rect class="hit" x="${m.l}" y="${m.t}" width="${iw}" height="${ih}" fill="transparent"/></svg>`;
      el.innerHTML = s;
      el._drawn = true;

      const svg = el.querySelector('svg');
      const g = svg.querySelector('.hover');
      const cross = g.querySelector('line');
      const dots = Array.from(g.querySelectorAll('circle'));
      const hit = svg.querySelector('.hit');
      function move(evt) {
        const p = evt.touches ? evt.touches[0] : evt;
        const r = svg.getBoundingClientRect();
        const i = Math.max(0, Math.min(n - 1, Math.round(((p.clientX - r.left - m.l) / iw) * (n - 1))));
        g.style.display = '';
        cross.setAttribute('x1', x(i)); cross.setAttribute('x2', x(i));
        dots.forEach((c, k) => { c.setAttribute('cx', x(i)); c.setAttribute('cy', y(series[k].values[i])); });
        const rows = series.map((ser) => ({ ser, v: ser.values[i] })).sort((a, b) => b.v - a.v)
          .map((r2) => `<div class="tooltip__r"><span><i class="key ${r2.ser.dash ? 'key--dash' : ''}" style="color:${r2.ser.color}"></i>${esc(r2.ser.name)}</span><b>${esc(tipFormat(r2.v))}</b></div>`).join('');
        tip.show(`<div class="tooltip__h">${esc(fmt.date(opts.dates[i]))}</div>${rows}`, p.clientX, p.clientY);
      }
      function leave() { g.style.display = 'none'; tip.hide(); }
      hit.addEventListener('mousemove', move);
      hit.addEventListener('touchmove', (e) => { move(e); e.preventDefault(); }, { passive: false });
      hit.addEventListener('mouseleave', leave);
      hit.addEventListener('touchend', leave);
    });
  }

  /** Inline sparkline string (no interaction; the table row carries the numbers). */
  function sparkline(values, { w = 90, h = 22, color = 'var(--text-2)', ref } = {}) {
    if (!values || values.length < 2) return '<span class="muted">—</span>';
    let min = Math.min(...values), max = Math.max(...values);
    if (ref != null) { min = Math.min(min, ref); max = Math.max(max, ref); }
    const x = (i) => 1 + (i / (values.length - 1)) * (w - 2);
    const y = (v) => h - 2 - ((v - min) / (max - min || 1)) * (h - 4);
    const d = values.map((v, i) => (i ? 'L' : 'M') + x(i).toFixed(1) + ',' + y(v).toFixed(1)).join('');
    const refLine = ref != null ? `<line x1="0" x2="${w}" y1="${y(ref)}" y2="${y(ref)}" stroke-dasharray="2 2" style="stroke:var(--axis)"/>` : '';
    return `<svg width="${w}" height="${h}" viewBox="0 0 ${w} ${h}" aria-hidden="true" style="display:inline-block;vertical-align:middle">${refLine}<path class="spark--draw" pathLength="1" d="${d}" fill="none" stroke-width="1.5" style="stroke:${color}"/><circle cx="${x(values.length - 1)}" cy="${y(values[values.length - 1])}" r="2" style="fill:${color}"/></svg>`;
  }

  /**
   * Semicircle gauge. The limit sits at 80% of the arc so headroom past it is visible.
   * opts: { value, limit, label, format, sub }
   */
  function gauge(el, opts) {
    const ratio = Math.abs(opts.value) / opts.limit;
    const [lvl, lvlLabel] = FF.ui.level(ratio);
    const scaleMax = opts.limit / 0.8;
    const frac = Math.min(1, Math.abs(opts.value) / scaleMax);
    const cx = 100, cy = 92, r = 72;
    const pt = (f) => [cx - r * Math.cos(Math.PI * f), cy - r * Math.sin(Math.PI * f)];
    const arc = (f0, f1) => { const [x0, y0] = pt(f0), [x1, y1] = pt(f1); return `M${x0},${y0} A${r},${r} 0 0 1 ${x1},${y1}`; };
    const [lx0, ly0] = [cx - (r - 13) * Math.cos(Math.PI * 0.8), cy - (r - 13) * Math.sin(Math.PI * 0.8)];
    const [lx1, ly1] = [cx - (r + 13) * Math.cos(Math.PI * 0.8), cy - (r + 13) * Math.sin(Math.PI * 0.8)];
    el.innerHTML = `<div class="gauge">
      <svg viewBox="0 0 200 104" role="img" aria-label="${esc(opts.label)}: ${esc(opts.format(opts.value))} of ${esc(opts.format(opts.limit))} limit">
        <path class="track" d="${arc(0, 1)}" fill="none" stroke-width="14" stroke-linecap="round"/>
        ${frac > 0.003 ? `<path class="gauge__arc" pathLength="1" d="${arc(0, Math.max(frac, 0.01))}" fill="none" stroke-width="14" stroke-linecap="round" style="stroke:var(--${lvl})"/>` : ''}
        <line class="limit-tick" x1="${lx0}" y1="${ly0}" x2="${lx1}" y2="${ly1}"/>
        <text x="${lx1 + 4}" y="${ly1 - 2}" style="font:10px var(--font);fill:var(--text-2)">limit</text>
      </svg>
      <div class="gauge__v">${esc(opts.format(opts.value))}</div>
      <div class="gauge__l">of ${esc(opts.format(opts.limit))} ${esc(opts.limitLabel || 'limit')}</div>
      <div class="gauge__status"><span class="badge badge--${lvl}"><span class="dot dot--${lvl}"></span>${lvlLabel} · ${Math.round(ratio * 100)}% of limit</span></div>
      ${opts.sub ? `<div class="gauge__l" style="margin-top:8px">${opts.sub}</div>` : ''}
    </div>`;
  }

  /** Horizontal bar list with an optional dashed limit marker. */
  function bars(el, rows, { limit, scaleMax, format = (v) => fmt.pct(v), label = (r) => r.key } = {}) {
    const max = scaleMax || Math.max(limit || 0, ...rows.map((r) => r.pct)) * 1.1;
    el.innerHTML = `<div class="bars">${rows.map((r) => {
      const ratio = limit ? r.pct / limit : 0;
      const cls = limit ? (ratio >= 1 ? 'over' : ratio >= 0.85 ? 'near' : '') : '';
      return `<div class="bar-row" data-tip="${esc(label(r))}: ${esc(fmt.usd(r.value))} · ${esc(format(r.pct))}${limit ? ' of your money, limit ' + esc(format(limit)) : ''}">
        <span class="bar-row__l">${esc(label(r))}</span>
        <span class="bar-row__track"><span class="bar-row__fill ${cls}" style="width:${(r.pct / max) * 100}%"></span>${limit ? `<span class="bar-row__limit" style="left:${(limit / max) * 100}%"></span>` : ''}</span>
        <span class="bar-row__v">${esc(format(r.pct))}</span></div>`;
    }).join('')}</div>`;
    el.querySelectorAll('[data-tip]').forEach((row) => {
      row.addEventListener('mousemove', (e) => tip.show(esc(row.dataset.tip), e.clientX, e.clientY));
      row.addEventListener('mouseleave', tip.hide);
    });
  }

  /** Correlation heatmap, diverging blue (−1) · gray (0) · red (+1). */
  function heatmap(el, { tickers, matrix }) {
    const n = tickers.length;
    const cs = getComputedStyle(document.documentElement);
    const hex = (v) => { const h = cs.getPropertyValue(v).trim().replace('#', ''); return [0, 2, 4].map((i) => parseInt(h.slice(i, i + 2), 16)); };
    const neg = hex('--div-neg'), mid = hex('--div-mid'), pos = hex('--div-pos');
    const mix = (a, b, t) => a.map((x, i) => Math.round(x + (b[i] - x) * t));
    const color = (v) => (v >= 0 ? mix(mid, pos, Math.min(1, v)) : mix(mid, neg, Math.min(1, -v)));
    const lum = (c) => (0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]) / 255;
    let s = `<div class="heat" style="grid-template-columns:44px repeat(${n}, minmax(0,1fr))"><span></span>`;
    tickers.forEach((t) => { s += `<span class="heat__top">${esc(t)}</span>`; });
    tickers.forEach((a, i) => {
      s += `<span class="heat__lab">${esc(a)}</span>`;
      tickers.forEach((b, j) => {
        const v = matrix[i][j];
        const c = color(v);
        const ink = lum(c) > 0.55 ? '#0b0b0b' : '#ffffff';
        s += i === j ? `<span class="heat__cell heat__cell--diag" data-tip="${esc(a)}">1</span>` : `<span class="heat__cell" style="background:rgb(${c});color:${ink}" data-tip="${esc(a)} × ${esc(b)}: ${v.toFixed(2)}">${v.toFixed(1).replace('-', '−')}</span>`;
      });
    });
    s += '</div><div class="heat-scale"><span>−1</span><span class="heat-scale__bar"></span><span>+1</span><span style="margin-left:6px">0.7 or more counts as "too alike"</span></div>';
    el.innerHTML = s;
    el.querySelectorAll('[data-tip]').forEach((c) => {
      c.addEventListener('mousemove', (e) => tip.show(esc(c.dataset.tip), e.clientX, e.clientY));
      c.addEventListener('mouseleave', tip.hide);
    });
  }

  return { line, sparkline, gauge, bars, heatmap };
})();
