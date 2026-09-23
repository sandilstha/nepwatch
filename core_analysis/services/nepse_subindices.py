"""
nepse_subindices.py — clean live NEPSE index + sub-index quotes.

Endpoint: http://<host>/NepseSubIndices   (env: NEPSE_SUBINDICES_URL)

Returns a dict keyed by index name, each value carrying real OHLC plus a
populated `closingIndex` and correct `absChange` / `percentageChange` — unlike
the older live-index feed whose `closing_index` stayed 0 intraday and produced
bogus changes. This is the authoritative index source for Market Insights.
Cached briefly; returns None on any failure so callers fall back to the DB.
"""
from __future__ import annotations

import os

import requests
from django.core.cache import cache

SUBINDICES_URL = os.environ.get(
    "NEPSE_SUBINDICES_URL", "http://192.168.1.100:8001/NepseSubIndices"
)
CACHE_KEY = "nepse_subindices"
CACHE_TTL = 12
FAIL_SENTINEL = "FAIL"
FAIL_TTL = 30  # > payload TTL, so a down feed isn't retried on every rebuild
TIMEOUT = 3


def fetch_subindices():
    """Return the {index_name: row} dict, or None if the feed is unavailable."""
    cached = cache.get(CACHE_KEY)
    if cached is not None:
        return None if cached == FAIL_SENTINEL else cached

    try:
        resp = requests.get(SUBINDICES_URL, timeout=TIMEOUT, headers={"Accept": "application/json"})
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, dict) or not data:
            cache.set(CACHE_KEY, FAIL_SENTINEL, FAIL_TTL)
            return None
    except (requests.RequestException, ValueError):
        cache.set(CACHE_KEY, FAIL_SENTINEL, FAIL_TTL)
        return None

    cache.set(CACHE_KEY, data, CACHE_TTL)
    return data
