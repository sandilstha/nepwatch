# Drop the TradingView Advanced Charts library here

This folder must contain the licensed TradingView charting library files so the
NEPSE Index card on `/insights/` becomes a TradingView terminal.

Expected after install:

```
charting_library/
├── charting_library.standalone.js
├── charting_library.js   (+ chunks/bundles from the repo)
└── datafeeds/udf/dist/bundle.js
```

See `TRADINGVIEW_SETUP.md` at the project root for full instructions
(how to request access and copy the files).

Until these files exist, `tv-chart.js` silently falls back to the ApexCharts
area chart — nothing breaks.
