"""
live_index.py — intraday live NEPSE index / sub-index quotes.

Pulls the internal live-index feed and caches it briefly. Returns None on any
failure so callers fall back to the end-of-day database.

Endpoint: http://<host>/api/live-index/  (env: NEPSE_LIVE_INDEX_URL)

Intraday quirk: `closing_index` is "0.0000" until the 3 PM close, so the feed's
own `abs_change` / `percentage_change` are computed off zero and are unusable
intraday. The current value is mirrored into open/high/low_index instead. The
parsing + correct change recovery lives in market_insights._live_index_metrics.
"""
from __future__ import annotations

import os

import requests
from django.core.cache import cache

LIVE_INDEX_URL = os.environ.get(
    "NEPSE_LIVE_INDEX_URL", "http://192.168.1.100:3000/api/live-index/"
)
CACHE_KEY = "nepse_live_index_rows"
CACHE_TTL = 12
FAIL_SENTINEL = "FAIL"
TIMEOUT = 5
MAX_PAGES = 5


def fetch_live_index_rows():
    """Return a list of raw live-index dicts, or None if the feed is unavailable."""
    cached = cache.get(CACHE_KEY)
    if cached is not None:
        return None if cached == FAIL_SENTINEL else cached

    rows = []
    url = LIVE_INDEX_URL
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
                rows.extend(data.get("results") or data.get("content") or [])
                url = data.get("next")
            pages += 1
    except (requests.RequestException, ValueError):
        cache.set(CACHE_KEY, FAIL_SENTINEL, CACHE_TTL)
        return None

    cache.set(CACHE_KEY, rows, CACHE_TTL)
    return rows
