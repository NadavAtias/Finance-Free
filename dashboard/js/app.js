/* App shell: router, top bar, kill switch, theme, panel-state demo. */
(function () {
  'use strict';
  const { fmt, tone, esc, $, $$, toast, confirm, store } = FF.ui;
  const api = FF.api;

  const app = {
    killSwitch: false,
    page: null,
    refreshSummary,
  };

  // ---------- top bar ----------
  async function refreshSummary() {
    const box = $('#topStats');
    try {
      const s = await api.get('/api/summary');
      app.killSwitch = s.kill_switch;
      setKill(s.kill_switch, false);
      const mk = { open: 'good', pre: 'warning', post: 'warning', closed: '' }[s.market.state];
      box.innerHTML = `
        <div class="stat"><span class="stat__label">Total money</span><span class="stat__value">${esc(fmt.usd(s.equity))}<small class="${tone(s.equity - s.start_capital)}">${esc(fmt.pctSigned(s.equity / s.start_capital - 1))}</small></span></div>
        <div class="stat"><span class="stat__label">Today</span><span class="stat__value ${tone(s.pnl_today)}">${esc(fmt.usdSigned(s.pnl_today))}<small>${esc(fmt.pctSigned(s.pnl_today_pct, 2))}</small></span></div>
        <div class="stat"><span class="stat__label">Down from high</span><span class="stat__value">${esc(fmt.pct(s.drawdown_pct, 1).replace('-', '−'))}<small class="muted">sells all at −15%</small></span></div>
        <div class="stat stat--market"><span class="stat__label">Market · ${esc(s.market.et_time)}</span><span class="stat__value"><span class="badge ${mk ? 'badge--' + mk : ''}"><span class="dot ${mk ? 'dot--' + mk : 'dot--muted'} ${s.market.state === 'open' ? 'dot--pulse' : ''}"></span>${esc(s.market.label)}${s.market.reason ? ' · ' + esc(s.market.reason) : ''}</span></span></div>`;
      $('#navRecCount').textContent = s.open_recommendations || '';
      $('#navAlertCount').textContent = s.unread_alerts || '';
    } catch (err) {
      box.innerHTML = `<div class="stat"><span class="stat__label">Your account</span><span class="stat__value" style="font-family:var(--font);font-weight:500;font-size:12px"><span class="badge badge--critical"><span class="dot dot--critical"></span>Couldn't load</span> <button class="btn btn--ghost" id="sumRetry" style="height:22px">Try again</button></span></div>`;
      $('#sumRetry').addEventListener('click', refreshSummary);
    }
  }

  // ---------- kill switch ----------
  function setKill(on, rerender = true) {
    app.killSwitch = on;
    const b = $('#killBtn');
    b.setAttribute('aria-checked', on);
    $('#killHint').textContent = on ? 'On · trading is stopped' : 'Trading is on';
    $('#haltBanner').hidden = !on;
    if (rerender && app.page === 'recommendations') render();
  }
  $('#killBtn').addEventListener('click', async () => {
    const engage = !app.killSwitch;
    const ok = await confirm(engage
      ? { title: 'Stop all trading?', body: 'Every strategy stops right away. No new trades will be made, and any waiting orders are cancelled. Trading stays off until you turn it back on.', ok: 'Stop trading' }
      : { title: 'Turn trading back on?', body: 'Strategies start again at their next scheduled run. Cancelled orders are not sent again.', ok: 'Turn trading on', danger: false });
    if (!ok) return;
    try {
      const r = await api.post('/api/kill-switch', { engaged: engage });
      setKill(r.engaged);
      if (api.mode !== 'live') FF_MOCK._journalAppend({ ts: new Date().toISOString(), type: 'system', ticker: '—', strategy: '—', detail: `You turned the kill switch ${r.engaged ? 'on. Trading stopped' : 'off. Trading is back on'}` });
      toast(r.engaged ? 'Trading is stopped.' : 'Trading is back on.');
    } catch (err) {
      toast('The kill switch didn\'t respond. As a backup, create a file named KILL_SWITCH on the server.');
    }
  });

  // ---------- router ----------
  const PAGES = ['overview', 'recommendations', 'positions', 'strategies', 'risk', 'signals', 'journal', 'alerts'];
  function current() {
    const p = location.hash.replace(/^#\/?/, '');
    return PAGES.includes(p) ? p : 'overview';
  }
  function render() {
    const page = current();
    app.page = page;
    $$('#navLinks a').forEach((a) => (a.dataset.page === page ? a.setAttribute('aria-current', 'page') : a.removeAttribute('aria-current')));
    const root = $('#content');
    root.querySelectorAll('.chart').forEach((c) => c._ro && c._ro.disconnect());
    FF.ui.tip.hide();
    root.innerHTML = '';
    FF.pages[page](root, app);
    document.title = `${$('#navLinks a[data-page="' + page + '"]').textContent.replace(/\d+$/, '').trim()} · Finance Free`;
    $('#app').classList.remove('nav-open');
  }
  window.addEventListener('hashchange', () => { render(); $('#content').focus({ preventScroll: true }); window.scrollTo(0, 0); });

  // ---------- nav drawer (mobile) ----------
  $('#menuBtn').addEventListener('click', () => $('#app').classList.toggle('nav-open'));
  $('#navScrim').addEventListener('click', () => $('#app').classList.remove('nav-open'));

  // ---------- theme ----------
  const themeSel = $('#themeSelect');
  themeSel.value = store('theme') || 'system';
  function applyTheme(v) {
    if (v === 'system') delete document.documentElement.dataset.theme;
    else document.documentElement.dataset.theme = v;
  }
  applyTheme(themeSel.value);
  themeSel.addEventListener('change', () => {
    try { if (themeSel.value === 'system') localStorage.removeItem('ff.theme'); else localStorage.setItem('ff.theme', themeSel.value); } catch (e) {}
    applyTheme(themeSel.value);
    render(); // heatmap colors are computed at draw time
  });
  window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => { if (themeSel.value === 'system') render(); });

  // ---------- panel-state demo ----------
  $('#demoSelect').addEventListener('change', (e) => {
    api.setDemo(e.target.value);
    refreshSummary();
    render();
  });
  $('#apiMode').textContent = api.mode === 'mock' ? 'Sample data, not real' : 'Live data';

  refreshSummary();
  render();
  setInterval(refreshSummary, 60000); // keeps the market badge current
})();
