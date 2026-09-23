"""
nepse_market_summary.py — official daily market totals (turnover / trades / …).

Endpoint: http://<host>/MarketSummaryHistory   (env: NEPSE_MARKET_SUMMARY_URL)

Returns a list of daily summaries (most recent first), each with the OFFICIAL
totals — totalTurnover, totalTradedShares, totalTransactions, tradedScrips —
matching the exchange (the live-price stock sum runs slightly low). Cached
briefly; None on failure so callers fall back to the computed sums.
"""
from __future__ import annotations

import os

import requests
from django.core.cache import cache

MARKET_SUMMARY_URL = os.environ.get(
    "NEPSE_MARKET_SUMMARY_URL", "http://192.168.1.100:8001/MarketSummaryHistory"
)
CACHE_KEY = "nepse_market_summary"
CACHE_TTL = 12
FAIL_SENTINEL = "FAIL"
FAIL_TTL = 30  # > payload TTL, so a down feed isn't retried on every rebuild
TIMEOUT = 3


def fetch_market_summary():
    """Return the list of daily summary dicts (most recent first), or None."""
    cached = cache.get(CACHE_KEY)
    if cached is not None:
        return None if cached == FAIL_SENTINEL else cached

    try:
        resp = requests.get(MARKET_SUMMARY_URL, timeout=TIMEOUT, headers={"Accept": "application/json"})
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, dict):
            data = data.get("results") or data.get("content") or data.get("data") or []
        if not isinstance(data, list) or not data:
            cache.set(CACHE_KEY, FAIL_SENTINEL, FAIL_TTL)
            return None
    except (requests.RequestException, ValueError):
        cache.set(CACHE_KEY, FAIL_SENTINEL, FAIL_TTL)
        return None

    cache.set(CACHE_KEY, data, CACHE_TTL)
    return data
