/* Shared UI helpers: formatting, DOM, panel states, toasts, tooltip. */
FF.ui = (function () {
  'use strict';

  // ---------- formatting ----------
  const usd0 = new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD', maximumFractionDigits: 0 });
  const usd2 = new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD', minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const fmt = {
    usd: (v) => (v == null ? '—' : usd0.format(v)),
    usd2: (v) => (v == null ? '—' : usd2.format(v)),
    usdSigned: (v) => (v == null ? '—' : (v > 0 ? '+' : v < 0 ? '−' : '') + usd0.format(Math.abs(v))),
    price: (v) => (v == null ? '—' : v.toFixed(2)),
    pct: (v, d = 1) => (v == null ? '—' : (v * 100).toFixed(d) + '%'),
    pctSigned: (v, d = 1) => (v == null ? '—' : (v > 0 ? '+' : v < 0 ? '−' : '') + Math.abs(v * 100).toFixed(d) + '%'),
    num: (v, d = 2) => (v == null || !isFinite(v) ? '—' : (v < 0 ? '−' : '') + Math.abs(v).toFixed(d)),
    int: (v) => (v == null ? '—' : Math.round(v).toLocaleString('en-US')),
    date: (iso) => new Date(iso.length === 10 ? iso + 'T12:00:00Z' : iso).toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric', timeZone: 'America/New_York' }),
    dateShort: (iso) => new Date(iso + 'T12:00:00Z').toLocaleDateString('en-US', { month: 'short', year: '2-digit', timeZone: 'UTC' }),
    ts: (iso) => {
      const d = new Date(iso);
      const date = d.toLocaleDateString('en-CA', { timeZone: 'America/New_York' });
      const time = d.toLocaleTimeString('en-GB', { timeZone: 'America/New_York', hour: '2-digit', minute: '2-digit', second: '2-digit' });
      return `${date} ${time}`;
    },
    rel: (iso) => {
      const s = (Date.now() - new Date(iso)) / 1000;
      if (s < 3600) return Math.max(1, Math.round(s / 60)) + 'm ago';
      if (s < 86400) return Math.round(s / 3600) + 'h ago';
      return Math.round(s / 86400) + 'd ago';
    },
  };
  const tone = (v) => (v > 0 ? 'up' : v < 0 ? 'down' : '');

  // ---------- DOM ----------
  function esc(s) {
    return String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }
  function html(strings, ...vals) {
    return strings.reduce((out, s, i) => {
      if (i === 0) return s;
      const v = vals[i - 1];
      const str = v && v.__raw ? v.s : Array.isArray(v) ? v.map((x) => (x && x.__raw ? x.s : esc(x))).join('') : esc(v);
      return out + str + s;
    }, '');
  }
  const raw = (s) => ({ __raw: true, s });
  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

  // ---------- icons ----------
  const ICON = {
    empty: '<svg viewBox="0 0 24 24"><rect x="4" y="5" width="16" height="14" rx="2"/><path d="M4 10h16M9 14h6"/></svg>',
    error: '<svg viewBox="0 0 24 24"><path d="M12 3l9.5 17h-19z M12 10v4.5 M12 17.3v.2"/></svg>',
    info: '<svg viewBox="0 0 16 16"><path d="M8 7v4.5 M8 4.5v.2"/></svg>',
    warning: '<svg viewBox="0 0 16 16"><path d="M8 4v5 M8 11.5v.2"/></svg>',
    critical: '<svg viewBox="0 0 16 16"><path d="M5 5l6 6M11 5l-6 6"/></svg>',
    good: '<svg viewBox="0 0 16 16"><path d="M4 8.5l2.5 2.5L12 5.5"/></svg>',
    retry: '<svg viewBox="0 0 16 16"><path d="M13 8a5 5 0 11-1.5-3.5M13 2.5v3h-3"/></svg>',
  };

  // ---------- panel ----------
  /**
   * A panel owns one data request and its four states.
   *   panel(el, { title, sub, actions, load, isEmpty, empty, render, flush, foot })
   * render(body, data, ctx) draws into the body. ctx.reload() refetches.
   */
  function panel(el, opts) {
    el.classList.add('panel');
    el.innerHTML = `
      ${opts.title ? `<div class="panel__head"><div><div class="panel__title">${opts.title}</div>${opts.sub ? `<div class="panel__sub">${opts.sub}</div>` : ''}</div><div class="panel__actions toolbar"></div></div>` : ''}
      <div class="panel__body ${opts.flush ? 'panel__body--flush' : ''}"></div>
      ${opts.foot ? `<div class="panel__foot">${opts.foot}</div>` : ''}`;
    const body = $('.panel__body', el);
    const actions = $('.panel__actions', el);
    const ctx = { el, body, actions, reload: load, data: null };

    async function load() {
      body.innerHTML = loadingHTML(opts.skeleton);
      body.setAttribute('aria-busy', 'true');
      try {
        const data = await opts.load();
        ctx.data = data;
        body.removeAttribute('aria-busy');
        if (opts.isEmpty && opts.isEmpty(data)) {
          body.innerHTML = emptyHTML(opts.empty || {});
          return;
        }
        body.innerHTML = '';
        opts.render(body, data, ctx);
      } catch (err) {
        body.removeAttribute('aria-busy');
        body.innerHTML = errorHTML(err);
        $('[data-retry]', body).addEventListener('click', load);
      }
    }
    load();
    return ctx;
  }

  function loadingHTML(kind) {
    if (kind === 'chart') return '<div class="skel-block" aria-label="Loading"><div class="skel-line" style="width:30%"></div><div class="skel-line tall"></div><div class="skel-line" style="width:60%"></div></div>';
    return '<div class="skel-block" aria-label="Loading"><div class="skel-line" style="width:40%"></div><div class="skel-line"></div><div class="skel-line" style="width:85%"></div><div class="skel-line" style="width:70%"></div></div>';
  }
  function emptyHTML({ title = 'Nothing here yet', text = '' }) {
    return `<div class="state">${ICON.empty}<strong>${esc(title)}</strong>${text ? `<p>${esc(text)}</p>` : ''}</div>`;
  }
  function errorHTML(err) {
    return `<div class="state state--error" role="alert">${ICON.error}<strong>Couldn't load this</strong>
      <p>Something went wrong getting this data. The rest of the page still works.</p><code title="Technical details">${esc(err.message)}</code>
      <button class="btn" data-retry>${ICON.retry}Try again</button></div>`;
  }

  // ---------- status badge ----------
  const STATUS = {
    paper: ['good', 'Practice'], research: ['info', 'Testing'], decaying: ['serious', 'Slipping'], paused: ['critical', 'Paused'],
    active: ['good', 'Working'], probation: ['warning', 'On trial'], risk_flag: ['serious', 'Warnings only'], unavailable: ['', 'Not available'],
    insufficient: ['', 'Still learning'], off: ['critical', 'Ignored'],
  };
  function badge(status) {
    const [t, label] = STATUS[status] || ['', status];
    return `<span class="badge ${t ? 'badge--' + t : ''}"><span class="dot ${t ? 'dot--' + t : 'dot--muted'}"></span>${esc(label)}</span>`;
  }
  // Status from value vs limit. Returns ['good'|'warning'|'serious'|'critical', label]
  function level(ratio) {
    if (ratio >= 1) return ['critical', 'Over the limit'];
    if (ratio >= 0.85) return ['serious', 'Very close'];
    if (ratio >= 0.6) return ['warning', 'Getting close'];
    return ['good', 'Fine'];
  }

  // ---------- toast / confirm / tooltip ----------
  function toast(msg) {
    const t = document.createElement('div');
    t.className = 'toast';
    t.textContent = msg;
    $('#toasts').appendChild(t);
    setTimeout(() => t.remove(), 3200);
  }
  function confirm({ title, body, ok = 'Confirm', danger = true }) {
    const d = $('#confirmDialog');
    $('#confirmTitle').textContent = title;
    $('#confirmBody').textContent = body;
    const okBtn = $('#confirmOk');
    okBtn.textContent = ok;
    okBtn.className = 'btn ' + (danger ? 'btn--danger' : 'btn--primary');
    return new Promise((resolve) => {
      d.addEventListener('close', () => resolve(d.returnValue === 'ok'), { once: true });
      d.returnValue = '';
      d.showModal();
    });
  }
  const tip = {
    show(htmlStr, x, y) {
      const t = $('#tooltip');
      t.innerHTML = htmlStr;
      t.hidden = false;
      const r = t.getBoundingClientRect();
      let left = x + 14, top = y + 14;
      if (left + r.width > window.innerWidth - 8) left = x - r.width - 14;
      if (top + r.height > window.innerHeight - 8) top = y - r.height - 14;
      t.style.left = Math.max(8, left) + 'px';
      t.style.top = Math.max(8, top) + 'px';
    },
    hide() { $('#tooltip').hidden = true; },
  };

  function store(key, val) {
    try {
      if (val === undefined) return JSON.parse(localStorage.getItem('ff.' + key));
      localStorage.setItem('ff.' + key, JSON.stringify(val));
    } catch (e) { return null; }
  }

  return { fmt, tone, esc, html, raw, $, $$, ICON, panel, badge, level, toast, confirm, tip, store, emptyHTML };
})();
