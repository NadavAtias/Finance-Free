# Dashboard (static prototype)

Single-page paper-trading dashboard: plain HTML, CSS and vanilla JS, no build step. It runs on inline mock data today and is shaped to be fed by the FastAPI backend later.

```bash
python3 -m http.server 8765 --directory dashboard   # then open http://localhost:8765
```

Opening `index.html` straight from disk also works in most browsers.

## Files

| File | Role |
|---|---|
| `index.html` | Shell: left nav, top bar (equity, today's P&L, drawdown, market badge, kill switch) |
| `styles.css` | Design tokens (light + dark) and components |
| `js/mock.js` | Mock backend: returns exactly the JSON each endpoint should return |
| `js/api.js` | The only data access point. `MODE = 'mock'` or `'live'` |
| `js/ui.js` | Formatting, `panel()` (loading / empty / error / ready states), toasts, tooltip |
| `js/charts.js` | SVG line chart with crosshair, gauge, bars, heatmap, sparkline |
| `js/pages.js` | One renderer per page; each panel owns one API call |
| `js/app.js` | Router, top bar, kill switch, theme, panel-state switcher |

## Going live

Set `MODE = 'live'` (and `BASE` if the API runs on another origin) in `js/api.js`, then serve these endpoints. Shapes are defined by the matching function in `js/mock.js`.

| Method | Path | Used by |
|---|---|---|
| GET | `/api/summary` | Top bar, nav counts |
| GET | `/api/overview` | Equity curves (`dates`, `series[].values` as cumulative return), KPI tiles |
| GET | `/api/recommendations` | Trade ideas page |
| POST | `/api/recommendations/{id}/taken` · `/skip` | Card actions |
| GET | `/api/positions` | Holdings page |
| GET | `/api/strategies` | Leaderboard (`backtest` vs `live` metrics) |
| GET | `/api/risk` | Gauges, exposure, correlation matrix |
| GET | `/api/signals` · `/api/signals/events` | Signal scoreboard, recent events |
| GET | `/api/journal` | Activity log page |
| GET | `/api/alerts` · POST `/api/alerts/{id}/read` | Alert feed |
| GET / PUT | `/api/alerts/settings` · POST `/api/alerts/test` | Telegram / email settings |
| POST | `/api/kill-switch` `{engaged: bool}` | Kill switch (should create/remove the `KILL_SWITCH` file) |

In live mode, drop the two `FF_MOCK._journalAppend` calls in `pages.js` and `app.js`; the backend writes those journal entries.

## Reviewing states

The **Preview** menu at the bottom of the nav forces every box into loading, no data, or something broke. In normal view, "Latest signals" on the Signals page fails on first load to show a real error with Try again.

## Wording

On-screen text uses plain, everyday English. Technical terms (Sharpe, Sortino, Calmar, profit factor, IC) keep their names but explain themselves on hover. URLs and API fields keep their original names (`#/recommendations`, `#/positions`, `#/journal`), so the backend contract is unchanged.
