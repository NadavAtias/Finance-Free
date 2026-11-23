/* Count-up for headline numbers. Watches the page for new KPI values and rolls
   them up from zero once, keeping the exact formatted text at the end. Does
   nothing when the system asks for reduced motion. */
(function () {
  if (window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;
  const SEL = '.kpi-hero__v, .kpi__v, .gauge__v, .stat__value';
  const NUM = /[-+−]?\d[\d,]*(?:\.\d+)?/;
  const seen = new WeakSet();
  const last = new Map(); // top-bar stats re-render every minute: only move when the value changes

  function animate(el) {
    if (seen.has(el)) return;
    seen.add(el);
    // only plain text nodes, so links, badges and tooltips stay untouched
    const node = Array.from(el.childNodes).find((n) => n.nodeType === 3 && NUM.test(n.nodeValue));
    if (!node) return;
    const text = node.nodeValue;
    const m = text.match(NUM);
    const raw = m[0];
    const target = parseFloat(raw.replace(/[,+]/g, '').replace('−', '-'));
    if (!isFinite(target) || /[A-Za-z]/.test(text.slice(0, m.index))) return; // dates and labels stay as they are
    let from = 0;
    if (el.classList.contains('stat__value')) {
      const key = (el.previousElementSibling || {}).textContent || '';
      const prev = last.get(key);
      last.set(key, target);
      if (prev === target) return; // unchanged values stay still on the minute refresh
      if (prev !== undefined) from = prev;
    }
    if (target === from) return;
    const decimals = (raw.split('.')[1] || '').length;
    const commas = raw.includes(',');
    const sign = /^[+]/.test(raw) ? '+' : '';
    const minus = raw.startsWith('−') ? '−' : '-';
    const pre = text.slice(0, m.index);
    const post = text.slice(m.index + raw.length);
    const show = (v) => {
      let s = Math.abs(v).toFixed(decimals);
      if (commas) { const [a, b] = s.split('.'); s = a.replace(/\B(?=(\d{3})+(?!\d))/g, ',') + (b ? '.' + b : ''); }
      return pre + (v < 0 ? minus : sign) + s + post;
    };
    el.classList.add('count-up');
    const dur = 900;
    const t0 = performance.now();
    function frame(t) {
      const k = Math.min(1, (t - t0) / dur);
      const e = 1 - Math.pow(1 - k, 3);
      node.nodeValue = k < 1 ? show(from + (target - from) * e) : text;
      if (k < 1) requestAnimationFrame(frame);
    }
    requestAnimationFrame(frame);
  }

  function scan(root) {
    if (root.nodeType !== 1) return;
    if (root.matches(SEL)) animate(root);
    root.querySelectorAll(SEL).forEach(animate);
  }

  const mo = new MutationObserver((list) => list.forEach((r) => r.addedNodes.forEach(scan)));
  function start() {
    const app = document.getElementById('app');
    if (!app) return;
    scan(app);
    mo.observe(app, { childList: true, subtree: true });
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start); else start();
})();
