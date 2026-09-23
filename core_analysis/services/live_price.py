"""
live_price.py — intraday live NEPSE quotes for the Market Insights dashboard.

Pulls the internal live-price feed (per-scrip last-traded price, OHLC, volume,
turnover, trades) and caches it briefly so the dashboard can poll often without
hammering the upstream service. On any failure it returns None and the caller
falls back to the end-of-day database, so the page never breaks.

Endpoint shape (paginated DRF):
    { "results": [ { "symbol", "security_name", "business_date",
                     "open_price", "high_price", "low_price",
                     "last_updated_price", "previous_day_close_price",
                     "total_traded_quantity", "total_traded_value",
                     "total_trades", "market_capitalization",
                     "last_updated_time", ... } ], "next": <url|null> }
"""
from __future__ import annotations

import os

import requests
from django.core.cache import cache

LIVE_PRICE_URL = os.environ.get(
    "NEPSE_LIVE_PRICE_URL", "http://192.168.1.100:3000/api/live-price/"
)
CACHE_KEY = "nepse_live_price_rows"
CACHE_TTL = 12          # seconds — one upstream pull is reused by polls within this window
FAIL_SENTINEL = "FAIL"  # cached on error so a down feed doesn't slow every poll
FAIL_TTL = 30           # > the dashboard payload TTL, so a down feed isn't retried each rebuild
TIMEOUT = 4             # per-request seconds
MAX_PAGES = 15


def fetch_live_rows():
    """Return a list of raw live-quote dicts, or None if the feed is unavailable."""
    cached = cache.get(CACHE_KEY)
    if cached is not None:
        return None if cached == FAIL_SENTINEL else cached

    rows = []
    url = LIVE_PRICE_URL
    try:
        pages = 0
        while url and pages < MAX_PAGES:
            resp = requests.get(url, timeout=TIMEOUT, headers={"Accept": "application/json"})
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, list):
                rows.extend(data)
                url = None
            else:
                # DRF feed uses "results"+"next"; the camelCase feed uses "content".
                rows.extend(data.get("results") or data.get("content") or [])
                url = data.get("next")
            pages += 1
    except (requests.RequestException, ValueError):
        cache.set(CACHE_KEY, FAIL_SENTINEL, FAIL_TTL)
        return None

    cache.set(CACHE_KEY, rows, CACHE_TTL)
    return rows
