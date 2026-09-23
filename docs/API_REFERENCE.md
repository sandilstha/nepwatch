# External APIs and data sources

Every outbound call the platform makes, where it is configured, and what stops
working if it is unreachable. Addresses beginning `192.168.` are on the office
network and only work from inside it; the rest need ordinary internet access.

---

## 1. NEPSE data API — `http://192.168.1.100:8000`

The main feed. Without it the site still runs, but no new market data arrives.

| Endpoint | Feeds |
|---|---|
| `/api/nepse-data/api/stock-prices/` | Daily OHLC prices for every scrip |
| `/api/nepse-data/api/indices/` | NEPSE index and all sub-indices |
| `/api/nepse-data/api/floorsheet/` | Trade-level floorsheet (broker analytics) |
| `/api/nepse-data/api/market-cap/` | Market capitalisation per company |
| `/api/listed-companies/companies/` | Company list and sector mapping |
| `/api/stock-adjustments/stock-price-adj/` | Bonus / rights adjustment factors |

- **Setting:** `NEPSE_API_BASE_URL` (defaults to the address above)
- **Auth:** none
- **Used by:** `sync_nepse_data`, `sync_floorsheet`, `sync_market_cap`, `sync_and_calculate`

---

## 2. NEPSE live / TMS feed — `http://192.168.1.100:3000`

| Endpoint | Feeds |
|---|---|
| `/api/tms-market-depth/` | Top-five order book snapshots (Market Depth tab) |
| `/api/live-index/` | Live index value during trading hours |
| `/api/live-price/` | Live per-scrip prices during trading hours |

- **Setting:** `NEPSE_DEPTH_API_BASE_URL`
- **Auth:** none
- **Used by:** `sync_market_depth`, live index/price services

---

## 3. NEPSE statistics service — `http://192.168.1.100:8001`

| Endpoint | Feeds |
|---|---|
| `/NepseSubIndices` | Sub-index snapshot used by the live chart bar |
| `/MarketSummaryHistory` | Turnover, traded shares, transaction counts |
| `/TopGainers`, `/TopLosers`, `/TopTenTradeScrips` | Market Insights movers tables |

- **Setting:** `NEPSE_TOP_MOVERS_BASE`
- **Auth:** none

---

## 4. Index contributors — `http://192.168.1.35:8000/contributors/`

Headline index value and per-scrip point contributions.

> **Known quirk:** after the market closes this feed rolls forward a session — it
> reports the day's true close as "previous close" and adds the day's move again.
> The chart now ignores it whenever its previous close does not match the stored
> one. See `live-index-feed-lag` handling in `core_analysis/udf_views.py`.

---

## 5. Mutual fund feed — `http://192.168.1.39:8000`

Per-scrip fund holdings and fund balance sheets (Mutual Funds desk).

- **Settings:** `NEPSE_MF_API_BASE`, `NEPSE_MF_API_USER`, `NEPSE_MF_API_PASSWORD`
- **Auth:** session login, and the feed is rate-limited — the sync paces itself

---

## 6. Fundamentals — `https://funda.aurasrp.com.np`

Company financial statements (balance sheet, income statement, key stats) that
feed Industry Analysis, Morning Star and Stock 360.

- **Auth:** none · **Internet required**

---

## 7. ShareSansar — `https://www.sharesansar.com`

| Page | Feeds |
|---|---|
| `/mutual-fund-navs` | Monthly NAV per fund |
| `/proposed-dividend` | Board-proposed bonus and cash dividends |

- **Auth:** none · **Internet required** · Scraped from the page's data feed

---

## 8. AI commentary (optional)

| Service | Endpoint | Setting |
|---|---|---|
| Google Gemini | `https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent` | `GEMINI_API_KEY`, `GEMINI_MODEL` |
| OpenRouter (fallback) | `https://openrouter.ai/api/v1/chat/completions` | `OPENROUTER_API_KEY`, `OPENROUTER_MODEL` |

Used for the written commentary on analysis pages. If the keys are missing or the
quota is exhausted, the pages still work — only the generated text is absent.

---

## Quick reachability test on a new PC

Open these in the browser on the new machine. A page or JSON response means the
feed is reachable; a timeout means it is not.

```
http://192.168.1.100:8000/
http://192.168.1.100:3000/
http://192.168.1.100:8001/NepseSubIndices
http://192.168.1.39:8000/
https://funda.aurasrp.com.np/
```

---

## Summary of settings

| Setting | Default / purpose |
|---|---|
| `NEPSE_API_BASE_URL` | `http://192.168.1.100:8000` — main data feed |
| `NEPSE_DEPTH_API_BASE_URL` | `http://192.168.1.100:3000` — market depth and live prices |
| `NEPSE_TOP_MOVERS_BASE` | `http://192.168.1.100:8001` — statistics service |
| `NEPSE_MF_API_BASE` / `_USER` / `_PASSWORD` | Mutual fund feed and its login |
| `GEMINI_API_KEY` / `GEMINI_MODEL` | AI commentary |
| `OPENROUTER_API_KEY` / `OPENROUTER_MODEL` | AI commentary fallback |

All of these live in `.env`. The three `192.168.1.*` hosts are the ones that
matter most: without them the platform has no new data.
