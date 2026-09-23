# TradingView Advanced Charts — installation

The NEPSE Index card on the Market Insights dashboard (`/insights/`) upgrades to
a full **TradingView Advanced Charts** terminal once the licensed library files
are installed. Until then it shows the ApexCharts area chart as a fallback — the
page is never broken by the library being absent.

Everything on **our** side is already built and tested:

- **UDF datafeed** (the API TradingView calls) lives at **`/insights/udf`**:
  - `GET /insights/udf/config`
  - `GET /insights/udf/time`
  - `GET /insights/udf/symbols?symbol=NEPSE`
  - `GET /insights/udf/search?query=...`
  - `GET /insights/udf/history?symbol=NEPSE&resolution=1D&from=...&to=...`
- Front-end bootstrap: `core_analysis/static/core_analysis/js/tv-chart.js`
- It serves daily OHLCV from `NepseMarketIndex` (indices) and
  `NepseDailyStockPrice` (stocks). Weekly/monthly bars are built by the library.

## Step 1 — get the library (free, one-time)

1. Request access at <https://www.tradingview.com/advanced-charts/>.
2. TradingView grants access to the private repo
   <https://github.com/tradingview/charting_library>.
3. Clone or download it.

## Step 2 — drop the files in (exact paths matter)

Copy the library so these two files resolve under our static dir:

```
core_analysis/static/core_analysis/charting_library/
├── charting_library.standalone.js        ← from the repo's charting_library/
├── charting_library.js  (+ bundles/chunks, keep the whole folder contents)
└── datafeeds/
    └── udf/
        └── dist/
            └── bundle.js                  ← from the repo's datafeeds/udf/dist/
```

In other words:

- Repo `charting_library/*`  →  `core_analysis/static/core_analysis/charting_library/`
- Repo `datafeeds/`          →  `core_analysis/static/core_analysis/charting_library/datafeeds/`

(If you prefer a different layout, just update `tvLibraryPath` in
`market_insights.html`'s `MI_CONFIG` — `tv-chart.js` loads
`<tvLibraryPath>charting_library.standalone.js` and
`<tvLibraryPath>datafeeds/udf/dist/bundle.js`.)

## Step 3 — collect static (production only)

```
python manage.py collectstatic
```

In local development (`DEBUG=1`) Django serves the files directly — no step
needed.

## Step 4 — verify

Reload `/insights/`. The "NEPSE Index Trend" card should become the TradingView
terminal (the hint flips to "TradingView · daily"). The dashboard's dark/light
toggle also recolours the widget.

To sanity-check the datafeed without the library:

```
curl "http://127.0.0.1:8000/insights/udf/history?symbol=NEPSE&resolution=1D&from=1735689600&to=1790000000"
```

You should get `{"s":"ok","t":[...],"o":[...],...}`.

## Notes

- Data is **end-of-day** only (`has_intraday: false`); intraday resolutions
  return `no_data` by design.
- The same datafeed already resolves every listed stock symbol (e.g. `NABIL`)
  and every sub-index (`BANKING`, `HYDRO`, …), so a per-symbol chart page is a
  small follow-up — the backend is ready.
- The library bundle is licensed by TradingView; do **not** commit it to a
  public repository. Add `core_analysis/static/core_analysis/charting_library/`
  to `.gitignore` if your repo is public.
