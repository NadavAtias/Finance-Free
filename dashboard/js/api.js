/* API client. The only place the UI talks to data.
 *
 * MODE 'mock' answers from window.FF_MOCK (js/mock.js) after a short delay.
 * MODE 'live' calls the FastAPI backend at BASE with fetch().
 *
 * Demo states (set from the sidebar) force every GET into loading / empty /
 * error so those panel states can be reviewed without a backend.
 */
window.FF = window.FF || {};
FF.api = (function () {
  'use strict';
  const MODE = 'mock'; // switch to 'live' once the FastAPI app serves /api/*
  const BASE = '';     // e.g. 'http://localhost:8000' when served separately

  let demo = 'normal';
  const failOnce = new Set(['/api/signals/events']); // shows a real error + retry on first load

  function wait(ms) { return new Promise((r) => setTimeout(r, ms)); }

  function route(method, path) {
    const key = `${method} ${path}`;
    if (FF_MOCK[key]) return { fn: FF_MOCK[key], arg: null };
    const m = path.match(/^(\/api\/[a-z-]+)\/([^/]+)\/([a-z-]+)$/);
    if (m) {
      const k = `${method} ${m[1]}/:id/${m[3]}`;
      if (FF_MOCK[k]) return { fn: FF_MOCK[k], arg: decodeURIComponent(m[2]) };
    }
    return null;
  }

  async function request(method, path, body) {
    if (MODE === 'live') {
      const res = await fetch(BASE + path, {
        method,
        headers: body ? { 'Content-Type': 'application/json' } : undefined,
        body: body ? JSON.stringify(body) : undefined,
      });
      if (!res.ok) throw new Error(`${res.status} ${res.statusText} on ${path}`);
      return res.json();
    }
    if (method === 'GET') {
      if (demo === 'loading') return new Promise(() => {}); // never resolves
      await wait(250 + Math.random() * 450);
      if (demo === 'error') throw new Error(`503 Service Unavailable on ${path}`);
      if (failOnce.has(path)) { failOnce.delete(path); throw new Error(`504 Gateway Timeout on ${path} (the data source took too long to answer)`); }
    } else {
      await wait(150);
    }
    const r = route(method, path);
    if (!r) throw new Error(`404 Not Found: ${method} ${path}`);
    let data = r.fn(r.arg, body);
    data = JSON.parse(JSON.stringify(data));
    if (method === 'GET' && demo === 'empty') data = emptied(data);
    return data;
  }

  function emptied(data) {
    if (Array.isArray(data)) return [];
    if (data && Array.isArray(data.positions)) return Object.assign(data, { positions: [] });
    if (data && Array.isArray(data.series)) return Object.assign(data, { series: [], portfolios: [], benchmarks: [] });
    if (data && data.exposure) return Object.assign(data, { exposure: { ticker: [], sector: [], strategy: [] }, correlation: { tickers: [], matrix: [] } });
    return data;
  }

  return {
    get: (p) => request('GET', p),
    post: (p, b) => request('POST', p, b),
    put: (p, b) => request('PUT', p, b),
    setDemo: (s) => { demo = s; },
    getDemo: () => demo,
    mode: MODE,
  };
})();
