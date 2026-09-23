"""
market_insights.py — aggregation layer for the Market Insights dashboard.

Builds the entire dashboard payload (market overview, breadth, top gainers /
losers, most-active scrips, sector performance, heatmap tiles and the NEPSE
index trend history) from the locally-synced NEPSE tables — never from any
external source.

Everything below is derived from a SINGLE daily-stock query (the latest
available trading day) plus a couple of small index queries, then cached so
repeated dashboard polls do not re-hit the database on every request.
"""
from __future__ import annotations

import logging
import math
import threading

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone as dt_timezone

from django.conf import settings
from django.core.cache import cache
from django.core.management import call_command
from django.db import connections
from django.db.models import Max, Count, Q, F, Sum

from core_analysis.models import (
    CompanyProfile,
    NepseDailyStockPrice,
    NepseMarketIndex,
)
from core_analysis.services.live_price import fetch_live_rows
from core_analysis.services.nepse_subindices import fetch_subindices
from core_analysis.services.nepse_market_summary import fetch_market_summary
from core_analysis.services.nepse_contributors import fetch_contributors
from core_analysis.services.nepse_top_movers import (
    fetch_top_gainers,
    fetch_top_losers,
    fetch_top_active,
)

logger = logging.getLogger(__name__)

CACHE_KEY = "market_insights_payload"
FAST_CACHE_KEY = "market_insights_payload_fast_eod"
CACHE_TTL = 15  # seconds — short so the live feed actually flows through to polls

# Last successful payload, kept for an hour. Served to callers when a rebuild is
# already in flight (stampede control) so a cold cache never fans out into N×7
# duplicate external requests.
CACHE_LAST_GOOD_KEY = "market_insights_payload_last_good"
CACHE_LAST_GOOD_TTL = 3600
# Only one cold rebuild runs at a time; the lock self-expires so a crashed build
# can't wedge the dashboard.
BUILD_LOCK_KEY = "market_insights_build_lock"
BUILD_LOCK_TTL = 20
CONTRIB_REFRESH_LOCK_KEY = "market_insights_contrib_refresh_lock"
CONTRIB_REFRESH_LOCK_TTL = 30

NEPSE_INDEX_NAME = "NEPSE INDEX"
SUBINDEX_NEPSE_KEY = "NepseIndex"  # NEPSE headline key in the NepseSubIndices feed
HISTORY_DAYS = 180
# Size of the heatmap pool sent to the client. The dashboard shows the top 60
# by turnover when "All sectors" is selected, but ships a larger pool so the
# sector filter can drill into a sector's full constituent set.
HEATMAP_POOL = 300
TABLE_LIMIT = 5

# Sector sub-indices surfaced in the Sector Performance widget. Deliberately
# excludes the headline NEPSE index and the float / sensitive aggregate indices
# (which are not sectors).
SECTOR_INDEX_NAMES = (
    "BANKING SUBINDEX",
    "DEVELOPMENT BANK INDEX",
    "FINANCE INDEX",
    "HOTELS AND TOURISM INDEX",
    "HYDROPOWER INDEX",
    "INVESTMENT INDEX",
    "LIFE INSURANCE",
    "MANUFACTURING AND PROCESSING",
    "MICROFINANCE INDEX",
    "MUTUAL FUND",
    "NON LIFE INSURANCE",
    "OTHERS INDEX",
    "TRADING INDEX",
)

SECTOR_LABELS = {
    "BANKING SUBINDEX": "Banking",
    "DEVELOPMENT BANK INDEX": "Dev. Bank",
    "FINANCE INDEX": "Finance",
    "HOTELS AND TOURISM INDEX": "Hotels & Tourism",
    "HYDROPOWER INDEX": "Hydropower",
    "INVESTMENT INDEX": "Investment",
    "LIFE INSURANCE": "Life Insurance",
    "MANUFACTURING AND PROCESSING": "Manufacturing",
    "MICROFINANCE INDEX": "Microfinance",
    "MUTUAL FUND": "Mutual Fund",
    "NON LIFE INSURANCE": "Non-Life Insurance",
    "OTHERS INDEX": "Others",
    "TRADING INDEX": "Trading",
}

# Full index set for the sub-index comparison chart, in display order: the NEPSE
# headline first, then every sector sub-index. The non-sector broad-market gauges
# (Sensitive / Float / Sensitive Float) are intentionally omitted. Names are
# NepseMarketIndex.sector_name values (matched case-insensitively by the DB, like
# the other index lookups in this module).
COMPARISON_INDICES = (
    ("NEPSE INDEX", "NEPSE"),
    ("BANKING SUBINDEX", "Banking"),
    ("DEVELOPMENT BANK INDEX", "Dev. Bank"),
    ("FINANCE INDEX", "Finance"),
    ("HOTELS AND TOURISM INDEX", "Hotels & Tourism"),
    ("HYDROPOWER INDEX", "Hydropower"),
    ("INVESTMENT INDEX", "Investment"),
    ("LIFE INSURANCE", "Life Insurance"),
    ("MANUFACTURING AND PROCESSING", "Manufacturing"),
    ("MICROFINANCE INDEX", "Microfinance"),
    ("MUTUAL FUND", "Mutual Fund"),
    ("NON LIFE INSURANCE", "Non-Life Insurance"),
    ("OTHERS INDEX", "Others"),
    ("TRADING INDEX", "Trading"),
)

# Default / maximum window (trading sessions) for the comparison chart. The cap
# keeps the multi-series payload light — 14 indices × this many daily closes.
COMPARISON_SESSIONS = 250
COMPARISON_MAX_SESSIONS = 800
COMPARISON_CACHE_KEY = "market_insights_subindex_compare"
COMPARISON_CACHE_TTL = 300  # EOD series only change once a day; cache generously.


# ── helpers ────────────────────────────────────────────────────────────────

def _f(value):
    """Coerce Decimal/str/None to a finite float, else None."""
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _round(value, ndigits=2):
    return round(value, ndigits) if value is not None else None


# ── core data pulls ────────────────────────────────────────────────────────

def _latest_stock_rows():
    """Return (latest_business_date, [raw rows]) for the most recent day."""
    latest = NepseDailyStockPrice.objects.aggregate(d=Max("business_date"))["d"]
    if latest is None:
        return None, []
    rows = list(
        NepseDailyStockPrice.objects.filter(business_date=latest).values(
            "symbol",
            "security_name",
            "open_price",
            "high_price",
            "low_price",
            "close_price",
            "previous_close",
            "total_traded_quantity",
            "total_traded_value",
            "total_trades",
            "market_capitalization",
        )
    )
    return latest, rows


def _sector_map():
    """symbol -> sector_name, for tagging heatmap tiles."""
    pairs = CompanyProfile.objects.exclude(sector_name__isnull=True).exclude(
        sector_name__exact=""
    ).values_list("symbol", "sector_name")
    return dict(pairs)


def _live_get(row, *keys):
    """First non-null value among the given keys (tolerates camelCase + snake_case)."""
    for key in keys:
        if key in row and row[key] is not None:
            return row[key]
    return None


def _live_close(row):
    """Effective close for a live quote.

    The official closePrice is null intraday and only fills in after the 3 PM
    close, so it falls back to the average traded price, then the last traded
    price.
    """
    close = _f(_live_get(row, "closePrice", "close_price"))
    if close is None:
        close = _f(_live_get(row, "averageTradedPrice", "average_traded_price"))
    if close is None:
        close = _f(_live_get(row, "lastUpdatedPrice", "last_updated_price"))
    return close


def _enrich_live(rows, sector_map):
    """Map raw live-feed quotes (camelCase or snake_case) into display dicts."""
    enriched = []
    for r in rows:
        close = _live_close(r)
        if close is None:
            continue
        prev = _f(_live_get(r, "previousDayClosePrice", "previous_day_close_price"))
        change = pct = None
        if prev is not None and prev > 0:
            change = close - prev
            pct = (change / prev) * 100.0
        symbol = _live_get(r, "symbol")
        enriched.append({
            "symbol": symbol,
            "name": _live_get(r, "securityName", "security_name"),
            # Fallback when a scrip's sector hasn't synced yet (usually a new
            # listing). "Uncategorized" — NOT "Other" — so it doesn't collide
            # with NEPSE's real "Others" sector; it self-corrects once the
            # sector backfills.
            "sector": sector_map.get(symbol) or "Uncategorized",
            "ltp": _round(close),
            "prev": _round(prev),
            "open": _round(_f(_live_get(r, "openPrice", "open_price"))),
            "high": _round(_f(_live_get(r, "highPrice", "high_price"))),
            "low": _round(_f(_live_get(r, "lowPrice", "low_price"))),
            "change": _round(change),
            "pct": _round(pct),
            "volume": int(_live_get(r, "totalTradedQuantity", "total_traded_quantity") or 0),
            "turnover": _f(_live_get(r, "totalTradedValue", "total_traded_value")) or 0.0,
            "trades": int(_live_get(r, "totalTrades", "total_trades") or 0),
            "market_cap": _f(_live_get(r, "marketCapitalization", "market_capitalization")) or 0.0,
        })
    return enriched


def _enrich(rows, sector_map):
    """Turn raw daily rows into display dicts with computed daily change."""
    enriched = []
    for r in rows:
        close = _f(r["close_price"])
        prev = _f(r["previous_close"])
        if close is None:
            continue
        change = pct = None
        if prev is not None and prev > 0:
            change = close - prev
            pct = (change / prev) * 100.0
        enriched.append({
            "symbol": r["symbol"],
            "name": r["security_name"],
            "sector": sector_map.get(r["symbol"]) or "Uncategorized",
            "ltp": _round(close),
            "prev": _round(prev),
            "open": _round(_f(r["open_price"])),
            "high": _round(_f(r["high_price"])),
            "low": _round(_f(r["low_price"])),
            "change": _round(change),
            "pct": _round(pct),
            "volume": int(r["total_traded_quantity"] or 0),
            "turnover": _f(r["total_traded_value"]) or 0.0,
            "trades": int(r["total_trades"] or 0),
            "market_cap": _f(r["market_capitalization"]) or 0.0,
        })
    return enriched


# ── NepseSubIndices feed (authoritative index source) ──────────────────────

def _subindex_metrics(row):
    """Value / change for one NepseSubIndices row. `closingIndex` is populated
    and `absChange`/`percentageChange` are correct here; if the close hasn't been
    published yet intraday (0), fall back to the live snapshot and recover the
    previous close from absChange (= closingIndex - prev)."""
    if not row:
        return {"value": None, "change": None, "pct": None, "high": None, "low": None,
                "turnover": 0.0, "date": None}
    close = _f(row.get("closingIndex")) or 0.0
    high = _f(row.get("highIndex"))
    low = _f(row.get("lowIndex"))
    open_v = _f(row.get("openIndex"))
    abs_change = _f(row.get("absChange"))
    pct_given = _f(row.get("percentageChange"))

    if close > 0:
        value, change, pct = close, abs_change, pct_given
        prev = (close - abs_change) if abs_change is not None else None
        # Guard against a bogus -100%-style value left over from a zero close.
        if (pct is None or abs(pct) >= 99.9) and prev and prev > 0:
            change = value - prev
            pct = (change / prev) * 100.0
    else:
        # Intraday (closingIndex not yet published, comes through as 0). The feed
        # still computes its own absChange/percentageChange AGAINST that 0 close,
        # so absChange == (0 − prevClose) == −prevClose and percentageChange
        # == −100%. That is NOT the move of the intraday value, so `value − absChange`
        # would give `value + prevClose` (roughly double the index) and a bogus
        # −50% headline. Recover the previous close as −absChange instead, then
        # derive the real intraday change from the live value.
        value = high or open_v or low
        prev = (-abs_change) if abs_change is not None else None
        if prev is not None and prev <= 0:
            prev = None  # absChange missing/zero before the first trade — no baseline
        change = (value - prev) if (value is not None and prev is not None) else None
        pct = (change / prev) * 100.0 if (change is not None and prev) else None

    return {
        "value": value,
        "change": change,
        "pct": pct,
        "high": high,
        "low": low,
        "turnover": _f(row.get("turnoverValue")) or 0.0,
        "date": row.get("businessDate"),
    }


def _contributors_index_metrics(row, fallback=None):
    """Headline NEPSE metrics parsed from the contributors page.

    The contributors page carries the exchange-matching live headline during the
    session. Keep high/low/date from the sub-index feed when available because
    the contributor summary only exposes value, change and previous close.
    """
    if not row:
        return None

    fallback = fallback or {}
    value = _f(row.get("value"))
    if value is None:
        return None

    change = _f(row.get("change"))
    prev = _f(row.get("prev_close"))
    if change is None and prev is not None:
        change = value - prev
    if prev is None and change is not None:
        prev = value - change

    pct = _f(row.get("pct"))
    if pct is None and change is not None and prev and prev > 0:
        pct = (change / prev) * 100.0

    high = _f(row.get("high")) if row.get("high") is not None else fallback.get("high")
    low = _f(row.get("low")) if row.get("low") is not None else fallback.get("low")
    if value is not None:
        high = max(high, value) if high is not None else value
        low = min(low, value) if low is not None else value

    return {
        "value": value,
        "prev": prev,
        "change": change,
        "pct": pct,
        "high": high,
        "low": low,
        "turnover": _f(row.get("turnover")) if row.get("turnover") is not None else fallback.get("turnover", 0.0),
        "date": (
            row.get("date")
            or row.get("businessDate")
            or row.get("business_date")
            or fallback.get("date")
        ),
    }


def _sectors_from_subindices(subidx):
    """Sector performance from the NepseSubIndices feed (same labels as _sectors)."""
    sectors = []
    for name, row in (subidx or {}).items():
        key = str(name).strip().upper()
        if key not in SECTOR_INDEX_NAMES:
            continue
        m = _subindex_metrics(row)
        sectors.append({
            "sector": SECTOR_LABELS.get(key, name),
            "raw": key,
            "index": _round(m["value"]),
            "change": _round(m["change"]),
            "pct": _round(m["pct"]),
            "turnover": m["turnover"],
        })
    sectors.sort(key=lambda s: (s["pct"] is None, -(s["pct"] or 0.0)))
    return sectors


# ── widget builders ────────────────────────────────────────────────────────

def _market_cap_totals():
    """NEPSE-wide market capitalisation for the latest EOD date and its change
    versus the prior trading day.

    Prefers the exchange's own published totals (``nepse_market_cap_daily``).
    The per-stock ``market_capitalization`` column cannot be relied on: the feed
    intermittently returns 0 for every scrip on the newest session(s), which
    silently summed to "Rs 0" on the dashboard. Falls back to the summed column
    only when no published total exists.
    """
    from core_analysis.models import NepseMarketCapDaily

    official = list(
        NepseMarketCapDaily.objects
        .exclude(market_capitalization__isnull=True)
        .exclude(market_capitalization=0)
        .order_by("-business_date")
        .values_list("business_date", "market_capitalization")[:2]
    )
    if official:
        # Already denominated in rupees, unlike the per-stock column (millions).
        latest = _f(official[0][1])
        prev = _f(official[1][1]) if len(official) > 1 else None
        change = pct = None
        if latest is not None and prev not in (None, 0):
            change = latest - prev
            pct = change / prev * 100.0
        return {
            "market_cap": round(latest, 2) if latest is not None else None,
            "market_cap_change": round(change, 2) if change is not None else None,
            "market_cap_pct": round(pct, 2) if pct is not None else None,
        }

    dates = list(
        NepseDailyStockPrice.objects.order_by("-business_date")
        .values_list("business_date", flat=True)
        .distinct()[:2]
    )
    if not dates:
        return {"market_cap": None, "market_cap_change": None, "market_cap_pct": None}

    def _cap(day):
        agg = NepseDailyStockPrice.objects.filter(business_date=day).aggregate(
            total=Sum("market_capitalization")
        )
        return _f(agg["total"])

    latest = _cap(dates[0])
    prev = _cap(dates[1]) if len(dates) > 1 else None
    change = pct = None
    if latest is not None and prev not in (None, 0):
        change = latest - prev
        pct = change / prev * 100.0
    # The market_capitalization column is stored in millions of rupees; convert
    # to raw rupees so the front-end money formatter scales it correctly.
    MILLION = 1_000_000
    return {
        "market_cap": round(latest * MILLION, 2) if latest is not None else None,
        "market_cap_change": round(change * MILLION, 2) if change is not None else None,
        "market_cap_pct": round(pct, 2) if pct is not None else None,
    }


def _turnover_totals():
    """NEPSE-wide traded turnover for the latest EOD date and its change versus
    the prior trading day, summed across every stock."""
    dates = list(
        NepseDailyStockPrice.objects.order_by("-business_date")
        .values_list("business_date", flat=True)
        .distinct()[:2]
    )
    if not dates:
        return {"turnover_change": None, "turnover_pct": None}

    def _tv(day):
        agg = NepseDailyStockPrice.objects.filter(business_date=day).aggregate(
            total=Sum("total_traded_value")
        )
        return _f(agg["total"])

    latest = _tv(dates[0])
    prev = _tv(dates[1]) if len(dates) > 1 else None
    change = pct = None
    if latest is not None and prev not in (None, 0):
        change = latest - prev
        pct = change / prev * 100.0
    return {
        "turnover_change": round(change, 2) if change is not None else None,
        "turnover_pct": round(pct, 2) if pct is not None else None,
    }


def _overview(enriched, nepse_live=None, market_summary=None):
    # Market totals: prefer the official MarketSummaryHistory row; otherwise sum
    # the live-price feed (which runs slightly low vs the exchange).
    ms = market_summary or {}
    ms_turnover = _f(ms.get("totalTurnover"))
    ms_volume = _f(ms.get("totalTradedShares"))
    ms_trades = ms.get("totalTransactions")
    ms_scrips = ms.get("tradedScrips")
    totals = {
        "turnover": round(ms_turnover, 2) if ms_turnover is not None else round(sum(s["turnover"] for s in enriched), 2),
        "volume": int(ms_volume) if ms_volume is not None else sum(s["volume"] for s in enriched),
        "trades": int(ms_trades) if ms_trades is not None else sum(s["trades"] for s in enriched),
        "scrips_traded": int(ms_scrips) if ms_scrips is not None else len(enriched),
    }

    # Latest NEPSE index EOD row — source of the 52-week range (the live feed
    # omits it) and the EOD fallback for the headline values.
    idx_row = (
        NepseMarketIndex.objects.filter(sector_name=NEPSE_INDEX_NAME)
        .order_by("-business_date")
        .first()
    )

    if nepse_live and nepse_live.get("value") is not None:
        nepse = {
            "nepse_index": _round(nepse_live["value"]),
            "nepse_change": _round(nepse_live["change"]),
            "nepse_pct": _round(nepse_live["pct"]),
            "nepse_high": _round(nepse_live["high"]),
            "nepse_low": _round(nepse_live["low"]),
            "nepse_date": nepse_live["date"],
        }
    else:
        nepse = {
            "nepse_index": _round(_f(idx_row.close_index)) if idx_row else None,
            "nepse_change": _round(_f(idx_row.absolute_change)) if idx_row else None,
            "nepse_pct": _round(_f(idx_row.percentage_change)) if idx_row else None,
            "nepse_high": _round(_f(idx_row.high_index)) if idx_row else None,
            "nepse_low": _round(_f(idx_row.low_index)) if idx_row else None,
            "nepse_date": idx_row.business_date.isoformat() if idx_row else None,
        }

    # 52-week range always comes from the index row; previous close is the
    # current index value minus its absolute change.
    nepse["nepse_52w_high"] = _round(_f(idx_row.number_52_weeks_high)) if idx_row else None
    nepse["nepse_52w_low"] = _round(_f(idx_row.number_52_weeks_low)) if idx_row else None
    _idx, _chg = nepse.get("nepse_index"), nepse.get("nepse_change")
    nepse["nepse_prev_close"] = _round(_idx - _chg) if (_idx is not None and _chg is not None) else None

    nepse.update(totals)
    nepse.update(_market_cap_totals())
    nepse.update(_turnover_totals())
    return nepse


def _breadth(enriched):
    advancing = declining = unchanged = 0
    for s in enriched:
        if s["change"] is None:
            continue
        if s["change"] > 0:
            advancing += 1
        elif s["change"] < 0:
            declining += 1
        else:
            unchanged += 1
    total = advancing + declining + unchanged
    return {
        "advancing": advancing,
        "declining": declining,
        "unchanged": unchanged,
        "total": total,
    }


# Reference points shown under the Fear & Greed gauge, mirroring the CNN-style
# "previous close / 1 week / 1 month / 1 year ago" history. Offsets are in
# calendar days back from the latest trading day; each resolves to the nearest
# trading session on or before that date.
GREED_HISTORY_OFFSETS = (
    ("Previous close", 1),
    ("1 week ago", 7),
    ("1 month ago", 30),
    ("1 year ago", 365),
)


def _greed_history():
    """Historical Fear & Greed reference points from end-of-day data.

    For each offset we emit the raw breadth (advancers / decliners / unchanged)
    and the NEPSE index daily % change for that session. The front-end then runs
    the SAME computeGreed() formula on these as it does for the live gauge, so a
    historical score can never drift from the live one's methodology.
    """
    # The longest offset is one year (~250 sessions): bound the date scan
    # instead of loading the whole column back to 1997 on every build.
    nepse_dates = list(
        NepseMarketIndex.objects.filter(sector_name=NEPSE_INDEX_NAME)
        .order_by("-business_date")
        .values_list("business_date", flat=True)[:420]
    )
    if not nepse_dates:
        return []

    latest = nepse_dates[0]
    # End-of-day data: identical for every build until the next session lands.
    ck = f"mi_greed_history:v2:{latest.isoformat()}"
    cached = cache.get(ck)
    if cached is not None:
        return cached
    out = []
    for label, days in GREED_HISTORY_OFFSETS:
        target = latest - timedelta(days=days)
        day = next((d for d in nepse_dates if d <= target), None)
        if day is None:
            continue

        agg = NepseDailyStockPrice.objects.filter(business_date=day).aggregate(
            advancing=Count("pk", filter=Q(close_price__gt=F("previous_close"))),
            declining=Count("pk", filter=Q(close_price__lt=F("previous_close"))),
            unchanged=Count("pk", filter=Q(close_price=F("previous_close"))),
        )
        total = (agg["advancing"] or 0) + (agg["declining"] or 0) + (agg["unchanged"] or 0)
        if not total:
            continue

        idx = (
            NepseMarketIndex.objects.filter(sector_name=NEPSE_INDEX_NAME, business_date=day)
            .first()
        )
        out.append({
            "label": label,
            "date": day.isoformat(),
            "breadth": {
                "advancing": agg["advancing"] or 0,
                "declining": agg["declining"] or 0,
                "unchanged": agg["unchanged"] or 0,
                "total": total,
            },
            "nepse_pct": _round(_f(idx.percentage_change)) if idx else None,
        })
    cache.set(ck, out, 3600)
    return out


def _gainers(enriched, limit=TABLE_LIMIT):
    ranked = [s for s in enriched if s["pct"] is not None]
    ranked.sort(key=lambda s: s["pct"], reverse=True)
    return _slim(ranked[:limit])


def _losers(enriched, limit=TABLE_LIMIT):
    ranked = [s for s in enriched if s["pct"] is not None]
    ranked.sort(key=lambda s: s["pct"])
    return _slim(ranked[:limit])


def _most_active(enriched, limit=TABLE_LIMIT):
    ranked = sorted(enriched, key=lambda s: s["turnover"], reverse=True)
    return _slim(ranked[:limit])


def _slim(rows):
    """Trim the table rows to just the columns the widgets render."""
    return [
        {
            "symbol": s["symbol"],
            "name": s["name"],
            "ltp": s["ltp"],
            "change": s["change"],
            "pct": s["pct"],
            "volume": s["volume"],
            "turnover": s["turnover"],
        }
        for s in rows
    ]


def _sectors():
    latest = (
        NepseMarketIndex.objects.filter(sector_name__in=SECTOR_INDEX_NAMES)
        .aggregate(d=Max("business_date"))["d"]
    )
    if latest is None:
        return []
    rows = NepseMarketIndex.objects.filter(
        business_date=latest, sector_name__in=SECTOR_INDEX_NAMES
    )
    sectors = [
        {
            "sector": SECTOR_LABELS.get(r.sector_name, r.sector_name.title()),
            "raw": r.sector_name,
            "index": _round(_f(r.close_index)),
            "change": _round(_f(r.absolute_change)),
            "pct": _round(_f(r.percentage_change)),
            "turnover": _f(r.turnover_values) or 0.0,
        }
        for r in rows
    ]
    sectors.sort(key=lambda s: (s["pct"] is None, -(s["pct"] or 0.0)))
    return sectors


def _heatmap(enriched, limit=HEATMAP_POOL):
    """Most liquid scrips, tagged with their (clean) company sector + performance.

    Sorted by turnover so the client can show the top slice for "All sectors"
    and the full constituent list when a single sector is filtered.
    """
    tiles = [s for s in enriched if s["pct"] is not None and s["turnover"] > 0]
    tiles.sort(key=lambda s: s["turnover"], reverse=True)
    return [
        {
            "symbol": s["symbol"],
            "sector": s["sector"] or "Uncategorized",
            "pct": s["pct"],
            "change": s["change"],
            "ltp": s["ltp"],
            "turnover": s["turnover"],
        }
        for s in tiles[:limit]
    ]


def _contributors_block(contrib, pending=False):
    """Shape the raw contributors feed into the payload's contributors dict.

    Single source of truth so the live build and the post-close EOD build emit
    the Top Contributors widget identically. Empty-but-valid when the feed is
    down, so the widget degrades gracefully instead of erroring.

    ``pending`` carries the distinction the UI could not previously make. The
    feed is fetched OFF-THREAD (so the dashboard never waits 7s on it), which
    means the first build after any restart legitimately has no contributors
    yet. Rendering that as "No data available" was wrong and read as a broken
    feed; with pending=True the panel can say it is still loading and poll
    again. An empty block with pending=False means the feed really did return
    nothing.
    """
    if not contrib:
        return {"positive": [], "negative": [],
                "sectors": {"positive": [], "negative": []},
                "pending": bool(pending)}
    return {
        "positive": contrib.get("positive", []),
        "negative": contrib.get("negative", []),
        "sectors": contrib.get("sectors", {"positive": [], "negative": []}),
        "pending": False,
    }


def _cached_contributors():
    """Return the current contributors cache without making a network request."""
    cached = cache.get("nepse_contributors")
    if cached is None or cached == "FAIL":
        return None
    return cached


def _refresh_contributors_async():
    """Refresh contributors off-thread so page/API responses never wait on it."""
    if not cache.add(CONTRIB_REFRESH_LOCK_KEY, 1, CONTRIB_REFRESH_LOCK_TTL):
        return

    def _run():
        try:
            fetch_contributors()
        except Exception:
            logger.exception("Background contributors refresh failed")
        finally:
            cache.delete(CONTRIB_REFRESH_LOCK_KEY)
            connections.close_all()

    threading.Thread(target=_run, name="nepse-contributors-refresh", daemon=True).start()


def _nepse_history(days=HISTORY_DAYS):
    qs = (
        NepseMarketIndex.objects.filter(sector_name=NEPSE_INDEX_NAME)
        .order_by("-business_date")
        .values("business_date", "close_index", "turnover_values")[:days]
    )
    rows = list(qs)[::-1]  # back to chronological order
    return [
        {
            "date": r["business_date"].isoformat(),
            "close": _f(r["close_index"]),
            "turnover": _f(r["turnover_values"]) or 0.0,
        }
        for r in rows
    ]


def subindex_comparison(days=COMPARISON_SESSIONS):
    """Aligned daily-close series for every NEPSE (sub)index, for the comparison chart.

    Returns ``{"start": iso, "sessions": n, "series": [{name, label, points}]}``
    where ``points`` is ``[[iso_date, close], ...]`` over the most recent ``days``
    trading sessions. Closes are sent raw — the client normalises each line to
    % change from its first visible point so all 17 lines share a 0%-baseline.

    Result is cached per window: the underlying rows are end-of-day, so they only
    change once a day, but several clients may request the same range at once.
    """
    days = max(5, min(int(days or COMPARISON_SESSIONS), COMPARISON_MAX_SESSIONS))
    cache_key = "%s:%d" % (COMPARISON_CACHE_KEY, days)
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    # Window = the most recent `days` distinct NEPSE business dates. Anchoring on
    # the NEPSE index (the longest, densest series) gives every sub-index the same
    # date span even when a sector has gaps or a shorter history.
    recent_dates = list(
        NepseMarketIndex.objects.filter(sector_name=NEPSE_INDEX_NAME)
        .order_by("-business_date")
        .values_list("business_date", flat=True)[:days]
    )
    if not recent_dates:
        return {"start": None, "sessions": 0, "series": []}
    start = recent_dates[-1]

    # Filter by date only: NepseMarketIndex holds exactly the index sector_names,
    # so this returns every (sub)index without depending on case-folded `__in`.
    rows = (
        NepseMarketIndex.objects.filter(business_date__gte=start)
        .order_by("business_date")
        .values_list("sector_name", "business_date", "close_index")
    )
    order = {name.upper(): i for i, (name, _label) in enumerate(COMPARISON_INDICES)}
    buckets = {name: [] for name, _label in COMPARISON_INDICES}
    for sector_name, bdate, close in rows:
        key = (sector_name or "").upper()
        if key not in order:
            continue  # an index not surfaced in the comparison set
        value = _f(close)
        if value is None:
            continue
        buckets[COMPARISON_INDICES[order[key]][0]].append([bdate.isoformat(), _round(value)])

    series = [
        {"name": name, "label": label, "points": buckets[name]}
        for name, label in COMPARISON_INDICES
        if buckets[name]
    ]
    result = {"start": start.isoformat(), "sessions": len(recent_dates), "series": series}
    cache.set(cache_key, result, COMPARISON_CACHE_TTL)
    return result


# ── post-close EOD switch ───────────────────────────────────────────────────
# NEPSE continuous trading settles by 3:00 PM Nepal time. After the close the
# intraday live feeds just echo their final tick, so the dashboard switches to
# the settled end-of-day data in SQL — refreshing it once via a background sync.

# Nepal Standard Time is a fixed UTC+5:45 offset all year (no DST), so the local
# trading clock needs no tz database.
NPT = dt_timezone(timedelta(hours=5, minutes=45))
MARKET_CLOSE_HOUR = getattr(settings, "NEPSE_MARKET_CLOSE_HOUR", 15)
MARKET_CLOSE_MINUTE = getattr(settings, "NEPSE_MARKET_CLOSE_MINUTE", 0)
# Master switch — set False in settings to revert to purely feed-driven sourcing.
POST_CLOSE_EOD_ENABLED = getattr(settings, "NEPSE_POST_CLOSE_EOD", True)

# Post-close one-shot sync guards. The "done" flag is keyed by the Nepal trading
# date so the sync fires exactly once per session; the "running" key is a
# self-expiring single-flight lock (atomic on the Redis cache in production).
EOD_SYNC_DONE_KEY = "market_insights_eod_sync_done"
EOD_SYNC_DONE_TTL = 12 * 3600
EOD_SYNC_RUNNING_KEY = "market_insights_eod_sync_running"
EOD_SYNC_RUNNING_TTL = 1800


def _nepse_now():
    """Current wall-clock time in Nepal (UTC+5:45)."""
    return datetime.now(dt_timezone.utc).astimezone(NPT)


def _after_market_close(now=None):
    """True once the NEPSE session has settled for the day (>= 3:00 PM NPT)."""
    if not POST_CLOSE_EOD_ENABLED:
        return False
    now = now or _nepse_now()
    return (now.hour, now.minute) >= (MARKET_CLOSE_HOUR, MARKET_CLOSE_MINUTE)


def _maybe_trigger_eod_sync(force=False):
    """Refresh the local SQL price/index tables once per trading day, off-thread.

    Pulls the just-settled session's stock prices and indices into MySQL exactly
    once (idempotent upserts make a repeat run harmless). It runs in a daemon
    thread so the request never blocks: the dashboard keeps serving the last
    settled snapshot and picks up the fresh rows on the next poll, because the
    payload cache is invalidated when the sync finishes.
    """
    now = _nepse_now()
    # NEPSE is closed Saturday (5) and Sunday (6) — nothing new to pull.
    if not force and now.weekday() in (5, 6):
        return
    done_key = "%s:%s" % (EOD_SYNC_DONE_KEY, now.date().isoformat())
    if not force and cache.get(done_key):
        return
    # Single-flight: at most one sync thread across the fleet.
    if not cache.add(EOD_SYNC_RUNNING_KEY, 1, EOD_SYNC_RUNNING_TTL):
        return
    cache.set(done_key, 1, EOD_SYNC_DONE_TTL)

    trading_day = now.date()

    def _run():
        try:
            # Stocks + indices for the settled day feed every front-page widget
            # (heatmap, breadth, overview, history). Scoped to the current Nepal
            # trading day so this is a light delta, not a full historical resync.
            call_command("sync_nepse_data", source="both", from_date=trading_day)
        except Exception:
            logger.exception("Post-close EOD sync failed; will retry on a later poll")
            cache.delete(done_key)  # allow a later poll to re-attempt today
        finally:
            cache.delete(EOD_SYNC_RUNNING_KEY)
            invalidate_cache()       # serve the fresh SQL rows on the next poll
            connections.close_all()  # this thread owns its DB connections; release them

    threading.Thread(target=_run, name="nepse-eod-sync", daemon=True).start()


# ── public API ─────────────────────────────────────────────────────────────

def _build_eod_payload():
    """Build a fast DB-only payload for the first cold browser fetch."""
    sector_map = _sector_map()
    eod_date, rows = _latest_stock_rows()
    enriched = _enrich(rows, sector_map)
    as_of = eod_date.isoformat() if eod_date else None

    return {
        "as_of": as_of,
        "live": False,
        "index_source": "eod",
        "source": "eod",
        "live_time": None,
        "has_data": bool(enriched),
        "overview": _overview(enriched, None, None),
        "breadth": _breadth(enriched),
        "greed_history": _greed_history(),
        "gainers": _gainers(enriched),
        "losers": _losers(enriched),
        "most_active": _most_active(enriched),
        "sectors": _sectors(),
        "heatmap": _heatmap(enriched),
        "heatmap_as_of": as_of,
        "history": _nepse_history(),
        # Placeholder: the caller overwrites this with the real block (and the
        # correct pending flag) right after. pending=True so a payload that
        # somehow escapes without that step still reads as "loading", never
        # as "the feed returned nothing".
        "contributors": _contributors_block(None, pending=True),
        "stock_count": len(enriched),
    }


def build_payload(force=False, cache_only=False, fast=False):
    """Assemble (and cache) the full Market Insights dashboard payload.

    Stock-level widgets use the intraday live feed when it is reachable, and
    fall back to the end-of-day database otherwise. The NEPSE headline prefers
    the official contributors summary, sector indices prefer NepseSubIndices,
    and both fall back to the end-of-day database when live services are down.

    cache_only=True returns the cached payload if present, else None — without
    ever touching the external feeds. The page-render view uses this so the HTML
    shell is returned instantly; the browser then fetches the live payload from
    /insights/api/ on demand (see insights_views.market_insights_view).
    """
    if not force:
        cached = cache.get(CACHE_KEY)
        if cached is not None:
            return cached

    # Render path: do not block HTML generation on the external feeds.
    if cache_only:
        return None

    # ── Post-close: serve settled end-of-day data from SQL ────────────────────
    # After 3 PM NPT the intraday live feeds are frozen on their last tick, so
    # the authoritative figures are the settled close now in MySQL. Kick off a
    # one-shot background sync (once per trading day) and build the payload
    # straight from the database instead of the pre-close live snapshot.
    if _after_market_close():
        _maybe_trigger_eod_sync(force=force)
        payload = _build_eod_payload()
        # Contributors have no SQL equivalent, but their upstream page can stall.
        # Use the last cached value and refresh it off-thread so the API returns
        # the settled SQL dashboard immediately.
        cached_contrib = _cached_contributors()
        payload["contributors"] = _contributors_block(
            cached_contrib, pending=cached_contrib is None)
        if force or cached_contrib is None:
            _refresh_contributors_async()
        cache.set(CACHE_KEY, payload, CACHE_TTL)
        cache.set(CACHE_LAST_GOOD_KEY, payload, CACHE_LAST_GOOD_TTL)
        return payload

    if fast and not force:
        cached_fast = cache.get(FAST_CACHE_KEY)
        if cached_fast is not None:
            return cached_fast
        last_good = cache.get(CACHE_LAST_GOOD_KEY)
        payload = last_good if last_good is not None else _build_eod_payload()
        cache.set(FAST_CACHE_KEY, payload, CACHE_TTL)
        return payload

    # Stampede control: only one cold rebuild runs at a time. Other callers
    # polling just after the 15s cache expired are served the last known-good
    # payload instead of each firing its own seven external requests.
    got_lock = cache.add(BUILD_LOCK_KEY, 1, BUILD_LOCK_TTL)
    if not got_lock and not force:
        last_good = cache.get(CACHE_LAST_GOOD_KEY)
        if last_good is not None:
            return last_good
    try:
        return _build_payload_locked(fast=fast)
    finally:
        # Release only OUR lock — a forced build that ran alongside another
        # builder must not clear that builder's lock from under it.
        if got_lock:
            cache.delete(BUILD_LOCK_KEY)


def _build_payload_locked(fast=False):

    # Fetch every external feed CONCURRENTLY. Done sequentially, a single slow or
    # down service serialises into a multi-second stall (timeouts add up); in
    # parallel the cold-build cost is just the slowest single feed.
    with ThreadPoolExecutor(max_workers=6) as pool:
        f_live = pool.submit(fetch_live_rows)
        f_subidx = pool.submit(fetch_subindices)
        f_summary = pool.submit(fetch_market_summary)
        f_gainers = pool.submit(fetch_top_gainers, TABLE_LIMIT)
        f_losers = pool.submit(fetch_top_losers, TABLE_LIMIT)
        f_active = pool.submit(fetch_top_active, TABLE_LIMIT)
        live_rows = f_live.result()
        subidx = f_subidx.result()
        summary = f_summary.result()
        top_gainers = f_gainers.result()
        top_losers = f_losers.result()
        top_active = f_active.result()

    # Contributors is deliberately NOT in that pool. Its upstream page is by far
    # the slowest feed — measured at 2.7s of a 2.8s build — and because the pool
    # is joined before the function continues, one slow feed sets the latency for
    # the whole dashboard. Waiting on it inside the pool with a timeout does not
    # help either: leaving a ThreadPoolExecutor block joins every worker anyway.
    #
    # So read the last cached value and refresh off-thread, exactly as the
    # after-close path above already does. The cost is that a cold cache renders
    # one build without contributors (the NEPSE headline falls back to the market
    # summary) and self-heals as soon as the background refresh lands.
    contrib = _cached_contributors()
    if contrib is None:
        _refresh_contributors_async()

    sector_map = _sector_map()

    # Latest end-of-day stock set, reused for the heatmap (always EOD) and as the
    # fallback when the live feed is missing/stale. Loaded lazily and memoised in
    # these two vars so we never hit the DB for it twice in one build.
    eod_date = None
    eod_enriched = None

    def _eod():
        nonlocal eod_date, eod_enriched
        if eod_enriched is None:
            eod_date, _rows = _latest_stock_rows()
            eod_enriched = _enrich(_rows, sector_map)
        return eod_enriched

    if live_rows:
        enriched = _enrich_live(live_rows, sector_map)

    if live_rows and enriched:
        is_live = True
        as_of = _live_get(live_rows[0], "businessDate", "business_date")
        live_feed_date = as_of  # the live feed's own date, for staleness check below
        live_time = max(
            (_live_get(r, "lastUpdatedTime", "last_updated_time") or "") for r in live_rows
        ) or None
    else:
        enriched = _eod()
        is_live = False
        as_of = eod_date.isoformat() if eod_date else None
        live_feed_date = None
        live_time = None

    # Set when the live per-scrip feed turns out to be stale (older than the
    # official trading day): the stock-level widgets are then rebuilt from the
    # end-of-day database below so they don't show prior-session prices.
    live_stale = False

    # Sector performance comes from the NepseSubIndices feed. The NEPSE headline
    # prefers the contributors page below because that source tracks the live
    # exchange headline during the session, while NepseSubIndices has been
    # observed freezing closingIndex on a prior tick/session.
    headline_from_contributors = False
    if subidx:
        nepse_headline = _subindex_metrics(subidx.get(SUBINDEX_NEPSE_KEY))
        sectors = _sectors_from_subindices(subidx)
    else:
        nepse_headline = None
        sectors = _sectors()

    contrib_headline = _contributors_index_metrics((contrib or {}).get("index"), nepse_headline)
    if contrib_headline:
        nepse_headline = contrib_headline
        headline_from_contributors = True

    if nepse_headline and nepse_headline.get("value") is not None:
        index_source = "official"
        official_date = nepse_headline.get("date")
        # Label the dashboard with the official trading day. The live-price feed
        # can lag (it has served a stale prior-day date); trust the authoritative
        # index feed's date for the headline so totals and "as of" agree.
        if official_date:
            as_of = official_date
            # If the live-price feed's own date trails the official trading day,
            # it's serving stale prior-session quotes — don't badge the dashboard
            # "LIVE" off it. The headline (index/turnover) is already sourced from
            # the fresh official feeds; only the per-scrip live view is stale.
            if is_live and live_feed_date and str(live_feed_date)[:10] < official_date[:10]:
                is_live = False
                live_time = None
                live_stale = True
    else:
        nepse_headline = None  # let _overview fall back to the DB
        index_source = "eod"

    # Official daily totals (turnover / trades / shares) come from the
    # MarketSummaryHistory feed. Match the row to the AUTHORITATIVE trading day
    # reported by the official headline date, NOT the live-price feed — that
    # feed has been observed serving a stale prior-day date, which would
    # otherwise select a previous day's turnover for the headline. When the
    # contributor page supplies the headline, use the latest summary row because
    # that page does not expose its own business date.
    ms_row = None
    if summary:
        summary_day = str(summary[0].get("businessDate") or "")[:10]
        official_day = summary_day if headline_from_contributors and summary_day else (
            ((nepse_headline or {}).get("date") or as_of or "")[:10]
        )
        ms_row = next(
            (r for r in summary if str(r.get("businessDate") or "")[:10] == official_day),
            summary[0],
        )
        if headline_from_contributors and ms_row.get("businessDate"):
            as_of = ms_row.get("businessDate")
            if nepse_headline is not None:
                nepse_headline["date"] = ms_row.get("businessDate")
            if is_live and live_feed_date and str(live_feed_date)[:10] < str(as_of)[:10]:
                is_live = False
                live_time = None
                live_stale = True

    # The live per-scrip feed was stale (older than the official trading day).
    # The headline above is already sourced from the fresh official feeds, but the
    # stock-level widgets (heatmap, breadth, gainers/losers/most-active) are still
    # built from the stale live quotes. Rebuild them from the end-of-day database
    # so they don't show prior-session prices under a current-day "as of" badge.
    if live_stale and _eod():
        enriched = _eod()

    # ── Single-session consistency ───────────────────────────────────────────
    # When the per-scrip data is end-of-day (the live feed was stale or absent),
    # keep the WHOLE overview on that same settled session: source the headline
    # index from the EOD database too, so the index, day range, breadth, gainers
    # and totals all describe ONE session — matching nepalstock's last settled
    # close — instead of a live index sitting over prior-session widgets. When a
    # genuine live session is available (is_live stays True), the live headline /
    # breadth / movers are kept, so the live view auto-restores once the upstream
    # feeds are fresh again.
    # Only when there is NO fresh official/contributor headline. A stale
    # per-scrip feed alone must not throw away the authoritative index,
    # turnover and date that were just reconciled above (the stock-level
    # widgets were already switched to EOD by the live_stale branch).
    eod_overview = not is_live and not headline_from_contributors and index_source != "official"
    if eod_overview:
        _eod()  # ensure eod_date + eod_enriched are loaded
        enriched = eod_enriched if eod_enriched is not None else enriched
        nepse_headline = None            # _overview falls back to the EOD index row
        index_source = "eod"
        headline_from_contributors = False
        if eod_date:
            as_of = eod_date.isoformat()

    # Reconcile sector turnover to the OFFICIAL total: the 13 index sub-sectors
    # only cover indexed scrips, so the remainder (debentures, preference /
    # promoter shares) is shown as a "Non-indexed" row. This makes sector turnover
    # match the headline Total Turnover instead of reading low.
    if sectors and ms_row:
        official_turnover = _f(ms_row.get("totalTurnover"))
        sector_turnover = sum((s.get("turnover") or 0.0) for s in sectors)
        if official_turnover and official_turnover - sector_turnover > 0:
            sectors = sectors + [{
                "sector": "Non-indexed", "raw": "NON_INDEXED",
                "index": None, "change": None, "pct": None,
                "turnover": round(official_turnover - sector_turnover, 2),
            }]

    # Official top gainers / losers / most-active scrips (fetched above); fall
    # back to the lists computed from the per-scrip feed when unavailable. In the
    # EOD-consistency mode the live movers feeds are skipped so these lists also
    # describe the same settled session as the rest of the overview.
    gainers = (None if eod_overview else top_gainers) or _gainers(enriched)
    losers = (None if eod_overview else top_losers) or _losers(enriched)
    most_active = (None if eod_overview else top_active) or _most_active(enriched)

    # Heatmap is ALWAYS end-of-day, never the intraday live feed. Its change % is
    # then the settled close-vs-previous-close move, matching nepalstock's EOD
    # figures. The live feed can serve a prior session's price under a current-day
    # badge (the AKJCL-style mismatch); sourcing the heatmap from the uploaded EOD
    # prices avoids that. `eod_enriched` is reused when the rest of the dashboard
    # already fell back to EOD, so this adds no extra query in that case.
    heatmap_tiles = _heatmap(_eod())
    heatmap_as_of = eod_date.isoformat() if eod_date else None

    # Market breadth: when the headline is the LIVE contributors session, the live
    # advance/decline counts ride along on that same feed (positive_scripts /
    # negative_scripts / flat_scripts). Use them so the breadth widget and the
    # Fear & Greed meter describe the SAME session as the headline index, instead
    # of the prior-session EOD breadth (which is all `enriched` holds when the live
    # per-scrip feed is stale). Falls back to EOD breadth when those counts are
    # absent.
    breadth = None
    if headline_from_contributors:
        ci = (contrib or {}).get("index") or {}
        adv = _f(ci.get("positive_scripts"))
        dec = _f(ci.get("negative_scripts"))
        unch = _f(ci.get("flat_scripts"))
        if adv is not None and dec is not None:
            a, d, u = int(adv), int(dec), int(unch or 0)
            breadth = {"advancing": a, "declining": d, "unchanged": u, "total": a + d + u}
    if breadth is None:
        breadth = _breadth(enriched)

    payload = {
        "as_of": as_of,
        "live": is_live,
        "index_source": index_source,
        "source": "live" if is_live else "eod",
        "live_time": live_time,
        "has_data": bool(enriched),
        "overview": _overview(enriched, nepse_headline, ms_row),
        "breadth": breadth,
        "greed_history": _greed_history(),
        "gainers": gainers,
        "losers": losers,
        "most_active": most_active,
        "sectors": sectors,
        "heatmap": heatmap_tiles,
        "heatmap_as_of": heatmap_as_of,
        "history": _nepse_history(),
        "contributors": _contributors_block(contrib, pending=contrib is None),
        "stock_count": len(enriched),
    }
    cache.set(CACHE_KEY, payload, CACHE_TTL)
    cache.set(CACHE_LAST_GOOD_KEY, payload, CACHE_LAST_GOOD_TTL)
    return payload


def invalidate_cache():
    cache.delete(CACHE_KEY)
    cache.delete(FAST_CACHE_KEY)


# ── Sector turnover over a window ──────────────────────────────────────────
#
# The Sector Turnover card defaults to "today", but the desk wants to see where
# money flowed over a week / month / quarter / half-year, or across an arbitrary
# date range. No new table is needed: NepseMarketIndex already stores one row per
# (business_date, sector index) with that day's turnover_values, so a window is a
# plain SUM over the sector sub-index rows in the range. Macro gauges (NEPSE /
# Sensitive / Float) are excluded by SECTOR_INDEX_NAMES, so nothing double-counts.

# period key -> (label, trading sessions counted back from the latest EOD date)
SECTOR_TURNOVER_PERIODS = {
    "1D": ("Today", 1),
    "1W": ("This week", 5),
    "1M": ("1 month", 22),
    "1Q": ("1 quarter", 66),
    "6M": ("6 months", 132),
    "1Y": ("1 year", 250),
}
SECTOR_TURNOVER_DEFAULT = "1D"
SECTOR_TURNOVER_MAX_SESSIONS = 1500
SECTOR_TURNOVER_CACHE_TTL = 300


def _sector_sessions(limit=None):
    """Distinct business dates that have sector sub-index rows, newest first."""
    qs = (
        NepseMarketIndex.objects.filter(sector_name__in=SECTOR_INDEX_NAMES)
        .order_by("-business_date")
        .values_list("business_date", flat=True)
        .distinct()
    )
    return list(qs[:limit] if limit else qs[:SECTOR_TURNOVER_MAX_SESSIONS])


def _sector_turnover_rows(start, end):
    """{sector_name: turnover} summed over [start, end] inclusive."""
    if not start or not end:
        return {}
    rows = (
        NepseMarketIndex.objects.filter(
            sector_name__in=SECTOR_INDEX_NAMES,
            business_date__gte=start,
            business_date__lte=end,
        )
        .values("sector_name")
        .annotate(total=Sum("turnover_values"))
    )
    # The DB collation matches sector_name case-insensitively, so older history
    # can come back in a different casing than SECTOR_INDEX_NAMES. Key on the
    # upper-cased name (and merge casings) so lookups never silently miss.
    totals = {}
    for r in rows:
        key = (r["sector_name"] or "").upper()
        totals[key] = totals.get(key, 0.0) + (_f(r["total"]) or 0.0)
    return totals


def _parse_date(value):
    try:
        return datetime.strptime((value or "").strip(), "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def sector_turnover_window(period=SECTOR_TURNOVER_DEFAULT, date_from=None, date_to=None):
    """Sector turnover aggregated over a preset window or a custom date range.

    Returns the per-sector totals for the window, each sector's share of it, and
    the change versus the immediately preceding window of equal session length
    (that comparison is what shows whether money is rotating in or out).
    """
    period = (period or SECTOR_TURNOVER_DEFAULT).upper()
    custom_from, custom_to = _parse_date(date_from), _parse_date(date_to)
    is_custom = period == "CUSTOM" and custom_from and custom_to
    if is_custom and custom_from > custom_to:
        custom_from, custom_to = custom_to, custom_from
    if not is_custom and period not in SECTOR_TURNOVER_PERIODS:
        period = SECTOR_TURNOVER_DEFAULT

    cache_key = "market_insights_sector_turnover:%s:%s:%s" % (
        "CUSTOM" if is_custom else period, custom_from or "", custom_to or "")
    cached = cache.get(cache_key)
    if cached:
        return cached

    sessions = _sector_sessions()
    if not sessions:
        return {"ok": True, "period": period, "sectors": [], "sessions": 0,
                "total": 0.0, "from": None, "to": None, "label": "No data"}

    if is_custom:
        window = [d for d in sessions if custom_from <= d <= custom_to]
        label = "Custom range"
        # Previous window = the same number of sessions immediately before it.
        prior = [d for d in sessions if window and d < window[-1]][:len(window)]
    else:
        label, count = SECTOR_TURNOVER_PERIODS[period]
        window = sessions[:count]
        prior = sessions[count:count * 2]

    if not window:
        return {"ok": True, "period": period, "sectors": [], "sessions": 0,
                "total": 0.0, "from": None, "to": None, "label": label}

    start, end = window[-1], window[0]
    current = _sector_turnover_rows(start, end)
    previous = _sector_turnover_rows(prior[-1], prior[0]) if prior else {}

    total = sum(current.values())
    sectors = []
    for name in SECTOR_INDEX_NAMES:
        value = current.get(name, 0.0)
        if value <= 0:
            continue
        prev = previous.get(name)
        sectors.append({
            "sector": SECTOR_LABELS.get(name, name.title()),
            "raw": name,
            "turnover": round(value, 2),
            "share": _round(value / total * 100.0) if total else None,
            "avg_per_session": round(value / len(window), 2),
            "prev_turnover": _round(prev),
            # None when there is no comparable prior window (start of history).
            "change_pct": _round((value - prev) / prev * 100.0) if prev else None,
        })
    sectors.sort(key=lambda s: s["turnover"], reverse=True)

    data = {
        "ok": True,
        "period": "CUSTOM" if is_custom else period,
        "label": label,
        "sessions": len(window),
        "prior_sessions": len(prior),
        "from": start.isoformat(),
        "to": end.isoformat(),
        "total": round(total, 2),
        "sectors": sectors,
    }
    cache.set(cache_key, data, SECTOR_TURNOVER_CACHE_TTL)
    return data
