"""
nepse_top_movers.py — official NEPSE top gainers / losers / most-active scrips.

Endpoints (env-overridable):
  gainers -> http://<host>/TopGainers
  losers  -> http://<host>/TopLosers
  active  -> http://<host>/TopTenTradeScrips

NOTE: the upstream service was unreachable when this was written, so the exact
field names could not be confirmed. The mapper therefore tries several common
camelCase / snake_case variants and the response envelope is detected flexibly.
Each kind caches briefly and returns None on failure, so callers fall back to
the locally-computed lists. Tighten `_map_mover` once a real sample is seen.
"""
from __future__ import annotations

import os

import requests
from django.core.cache import cache

BASE = os.environ.get("NEPSE_TOP_MOVERS_BASE", "http://192.168.1.100:8001")
URLS = {
    "gainers": os.environ.get("NEPSE_TOP_GAINERS_URL", BASE + "/TopGainers"),
    "losers": os.environ.get("NEPSE_TOP_LOSERS_URL", BASE + "/TopLosers"),
    "active": os.environ.get("NEPSE_TOP_ACTIVE_URL", BASE + "/TopTenTradeScrips"),
}
CACHE_TTL = 12
FAIL_SENTINEL = "FAIL"
FAIL_TTL = 30  # > payload TTL, so a down feed isn't retried on every rebuild
TIMEOUT = 3


def _g(row, *keys):
    if not isinstance(row, dict):
        return None
    for key in keys:
        if row.get(key) is not None:
            return row[key]
    return None


def _f(value):
    try:
        return float(str(value).replace(",", "").replace("%", ""))
    except (TypeError, ValueError):
        return None


def _records(payload):
    """Pull the list of records out of whatever envelope the endpoint uses."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("data", "results", "content", "gainers", "losers", "scrips", "items", "topGainers", "topLosers"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
        values = list(payload.values())
        # dict keyed by symbol -> record. Guard against error/meta envelopes
        # (e.g. {"error": {...}}) by requiring the values to actually look like
        # mover records (carry a symbol-ish field), not just be dicts.
        if values and all(isinstance(v, dict) for v in values) and all(
            _g(v, "symbol", "Symbol", "stockSymbol", "scrip") is not None for v in values
        ):
            return values
    return []


def _map_mover(row):
    symbol = _g(row, "symbol", "Symbol", "stockSymbol", "scrip")
    if not symbol:
        return None
    ltp = _f(_g(row, "ltp", "lastTradedPrice", "closingPrice", "closePrice",
                "last_updated_price", "lastUpdatedPrice", "close"))
    pct = _f(_g(row, "percentageChange", "percentage_change", "percentChange", "pChange"))
    change = _f(_g(row, "pointChange", "point_change", "change", "absChange", "schange"))
    volume = _f(_g(row, "totalTradedQuantity", "shareTraded", "sharesTraded", "turnoverVolume",
                   "total_traded_quantity", "quantity", "tradedShares"))
    turnover = _f(_g(row, "totalTradedValue", "turnoverValue", "amount", "total_traded_value", "turnover"))
    # TopTenTradeScrips reports shares traded + price but no turnover — approximate
    # it (volume x price). Prefer the average traded price (volume x avg == exact
    # turnover); fall back to LTP only when no average is provided.
    if turnover is None and volume is not None:
        price = _f(_g(row, "averageTradedPrice", "average_traded_price", "avgPrice", "averagePrice")) or ltp
        if price is not None:
            turnover = volume * price
    return {
        "symbol": symbol,
        "name": _g(row, "securityName", "security_name", "companyName", "name"),
        "ltp": round(ltp, 2) if ltp is not None else None,
        "change": round(change, 2) if change is not None else None,
        "pct": round(pct, 2) if pct is not None else None,
        "volume": int(volume) if volume is not None else 0,
        "turnover": turnover or 0.0,
    }


def _fetch(kind, limit):
    cache_key = "nepse_top_" + kind
    cached = cache.get(cache_key)
    if cached is not None:
        return None if cached == FAIL_SENTINEL else cached

    try:
        resp = requests.get(URLS[kind], timeout=TIMEOUT, headers={"Accept": "application/json"})
        resp.raise_for_status()
        rows = [m for m in (_map_mover(r) for r in _records(resp.json())) if m]
        if not rows:
            cache.set(cache_key, FAIL_SENTINEL, FAIL_TTL)
            return None
    except (requests.RequestException, ValueError):
        cache.set(cache_key, FAIL_SENTINEL, FAIL_TTL)
        return None

    rows = rows[:limit]
    cache.set(cache_key, rows, CACHE_TTL)
    return rows


def fetch_top_gainers(limit=5):
    return _fetch("gainers", limit)


def fetch_top_losers(limit=5):
    return _fetch("losers", limit)


def fetch_top_active(limit=5):
    return _fetch("active", limit)
