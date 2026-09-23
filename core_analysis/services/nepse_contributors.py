"""
nepse_contributors.py — official NEPSE index + index point-contributors.

Sources the HATHLYTICS contributors page (http://<host>/contributors/). That
platform's JSON APIs are auth-gated (401), but the contributors page renders
publicly and carries:
  * the OFFICIAL NEPSE index summary (matches nepalstock.com — unlike the live
    192.168.1.100 feed, which freezes on the last live tick), and
  * each stock's point contribution to the index move (index attribution), and
  * the sector-level positive / negative contributors from the same page's
    `view=sector` mode.

We parse that page (its markup is clean, class-based), cache briefly, and return
None on any failure so callers fall back gracefully.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from html import unescape
import os
import re

import requests
from django.core.cache import cache

CONTRIBUTORS_URL = os.environ.get(
    "NEPSE_CONTRIBUTORS_URL", "http://192.168.1.35:8000/contributors/"
)
CACHE_KEY = "nepse_contributors"
CACHE_TTL = 45
FAIL_SENTINEL = "FAIL"
TIMEOUT = 10
TOP_N = 8


def _num(value):
    """Plain number: strip commas / %, drop sign markers."""
    if value is None:
        return None
    try:
        return float(str(value).replace(",", "").replace("%", "").replace("+", ""))
    except ValueError:
        return None


def _signed(value):
    """Signed number: strip commas / %, keep +/-."""
    if value is None:
        return None
    try:
        return float(str(value).replace(",", "").replace("%", ""))
    except ValueError:
        return None


def _first(pattern, html):
    m = re.search(pattern, html)
    return m.group(1) if m else None


def _clean_text(value):
    text = re.sub(r"<[^>]+>", "", value or "")
    return re.sub(r"\s+", " ", unescape(text)).strip()


def _sector_view_url():
    sep = "&" if "?" in CONTRIBUTORS_URL else "?"
    return f"{CONTRIBUTORS_URL}{sep}view=sector"


def _parse_sector_movers(html):
    movers = {"positive": [], "negative": []}

    for m in re.finditer(
        r'<div class="pill (up|down)">\s*<span>(.*?)</span>\s*'
        r'<span class="pill-pts">([+\-]?[\d\.]+)</span>\s*</div>',
        html,
        re.S,
    ):
        row = {
            "sector": _clean_text(m.group(2)),
            "points": _signed(m.group(3)),
        }
        if row["sector"] and row["points"] is not None:
            movers["positive" if m.group(1) == "up" else "negative"].append(row)

    # Fallback for the sector table if the compact pill stream changes.
    if not movers["positive"] and not movers["negative"]:
        for chunk in re.split(r'class="contrib-item"', html)[1:]:
            name = re.search(r'class="sym-link"[^>]*>(.*?)<', chunk, re.S)
            pts = re.search(r'bar-value">([+\-]?[\d\.]+)<', chunk)
            if not (name and pts):
                continue
            points = _signed(pts.group(1))
            row = {"sector": _clean_text(name.group(1)), "points": points}
            if row["sector"] and points is not None:
                (movers["positive"] if points > 0 else movers["negative"]).append(row)

    movers["positive"].sort(key=lambda r: r["points"] or 0, reverse=True)
    movers["negative"].sort(key=lambda r: r["points"] or 0)
    movers["positive"] = movers["positive"][:TOP_N]
    movers["negative"] = movers["negative"][:TOP_N]
    return movers


def _parse(html):
    value = _num(_first(r'nepse-index-val">([\d,\.]+)<', html))
    change = _signed(_first(r'nepse-change[^"]*">([+\-]?[\d,\.]+)<', html))
    prev_close = _num(_first(r'Previous Close</div>\s*<div class="mini-tile-value[^"]*">([\d,\.]+)<', html))

    def _tile(label):
        return _num(_first(label + r'</div>\s*<div class="mini-tile-value[^"]*">([\d,]+)<', html))

    pct = (change / prev_close * 100.0) if (change is not None and prev_close) else None

    positive, negative = [], []
    for chunk in re.split(r'class="contrib-item"', html)[1:]:
        sym = re.search(r'sym-link[^>]*>([A-Za-z0-9]+)<', chunk)
        pts = re.search(r'bar-value">([+\-]?[\d\.]+)<', chunk)
        if not (sym and pts):
            continue
        ltp = re.search(r'ltp-cell">([\d,\.]+)<', chunk)
        pct_m = re.search(r'class="pct[^"]*">([+\-]?[\d\.]+)%', chunk)
        points = _signed(pts.group(1))
        row = {
            "symbol": sym.group(1),
            "points": points,
            "ltp": _num(ltp.group(1)) if ltp else None,
            "pct": _signed(pct_m.group(1)) if pct_m else None,
        }
        (positive if (points or 0) >= 0 else negative).append(row)

    positive.sort(key=lambda r: r["points"] or 0, reverse=True)
    negative.sort(key=lambda r: r["points"] or 0)

    return {
        "index": {
            "value": value,
            "change": change,
            "pct": round(pct, 2) if pct is not None else None,
            "prev_close": prev_close,
            "positive_scripts": _tile("Positive Scripts"),
            "negative_scripts": _tile("Negative Scripts"),
            "flat_scripts": _tile("Flat Scripts"),
        },
        "positive": positive[:TOP_N],
        "negative": negative[:TOP_N],
        "sectors": {"positive": [], "negative": []},
    }


def fetch_contributors():
    """Return index, stock contributors and sector contributors from HATHLYTICS."""
    cached = cache.get(CACHE_KEY)
    if cached is not None:
        return None if cached == FAIL_SENTINEL else cached

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            f_stock = pool.submit(requests.get, CONTRIBUTORS_URL, timeout=TIMEOUT)
            f_sector = pool.submit(requests.get, _sector_view_url(), timeout=TIMEOUT)

            stock_resp = f_stock.result()
            stock_resp.raise_for_status()
            data = _parse(stock_resp.text)

            try:
                sector_resp = f_sector.result()
                sector_resp.raise_for_status()
                data["sectors"] = _parse_sector_movers(sector_resp.text)
            except (requests.RequestException, ValueError):
                data["sectors"] = {"positive": [], "negative": []}

        if data["index"]["value"] is None and not data["positive"] and not data["negative"]:
            cache.set(CACHE_KEY, FAIL_SENTINEL, CACHE_TTL)
            return None
    except (requests.RequestException, ValueError):
        cache.set(CACHE_KEY, FAIL_SENTINEL, CACHE_TTL)
        return None

    cache.set(CACHE_KEY, data, CACHE_TTL)
    return data
