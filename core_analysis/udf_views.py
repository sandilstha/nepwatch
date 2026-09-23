"""
udf_views.py — TradingView UDF (Universal Data Feed) backend.

Implements the small REST contract TradingView's Advanced Charts library calls
through its `Datafeeds.UDFCompatibleDatafeed`:

    GET /insights/udf/config     -> datafeed capabilities
    GET /insights/udf/time       -> server time (unix seconds)
    GET /insights/udf/symbols    -> symbol info for one ticker (resolveSymbol)
    GET /insights/udf/search     -> ticker search
    GET /insights/udf/history    -> OHLCV bars

Bars are served straight from the locally-synced NEPSE tables:
  * indices / sub-indices  -> NepseMarketIndex (open/high/low/close_index, turnover_volume)
  * individual stocks      -> NepseDailyStockPrice (open/high/low/close_price, total_traded_quantity)

Data is end-of-day only, so `has_intraday` is false and the library builds the
weekly / monthly resolutions itself from the daily bars we return.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

from django.core.cache import cache
from django.http import HttpResponse, JsonResponse
from django.views.decorators.http import require_GET

from core_analysis.models import (
    CompanyProfile,
    NepseDailyStockPrice,
    NepseMarketIndex,
)
from core_analysis.services.nepse_contributors import fetch_contributors
from core_analysis.services.nepse_subindices import fetch_subindices

EXCHANGE = "NEPSE"
TIMEZONE = "Asia/Kathmandu"
SUPPORTED_RESOLUTIONS = ["1D", "1W", "1M"]

# Friendly ticker -> NepseMarketIndex.sector_name. The raw sector_name is also
# accepted directly (so "NEPSE INDEX" resolves as well as "NEPSE").
INDEX_TICKERS = {
    "NEPSE": "NEPSE INDEX",
    "SENSITIVE": "SENSITIVE INDEX",
    "FLOAT": "FLOAT INDEX",
    "SENFLOAT": "SENSITIVE FLOAT INDEX",
    "BANKING": "BANKING SUBINDEX",
    "DEVBANK": "DEVELOPMENT BANK INDEX",
    "FINANCE": "FINANCE INDEX",
    "HOTEL": "HOTELS AND TOURISM INDEX",
    "HYDRO": "HYDROPOWER INDEX",
    "INVEST": "INVESTMENT INDEX",
    "LIFEINSU": "LIFE INSURANCE",
    "MANUFAC": "MANUFACTURING AND PROCESSING",
    "MICROFIN": "MICROFINANCE INDEX",
    "MUTUAL": "MUTUAL FUND",
    "NONLIFE": "NON LIFE INSURANCE",
    "OTHERS": "OTHERS INDEX",
    "TRADING": "TRADING INDEX",
}
_SECTOR_TO_TICKER = {v: k for k, v in INDEX_TICKERS.items()}

# DB sector_name -> key in the live NepseSubIndices feed (used to append/refresh
# today's bar so the chart's last candle matches the live/official value rather
# than the last end-of-day row synced into the database).
SUBINDEX_KEYS = {
    "NEPSE INDEX": "NepseIndex",
    "SENSITIVE INDEX": "SensitiveIndex",
    "FLOAT INDEX": "FloatIndex",
    "SENSITIVE FLOAT INDEX": "SenseFloatIndex",
    "BANKING SUBINDEX": "Banking SubIndex",
    "DEVELOPMENT BANK INDEX": "Development Bank Index",
    "FINANCE INDEX": "Finance Index",
    "HOTELS AND TOURISM INDEX": "Hotels And Tourism Index",
    "HYDROPOWER INDEX": "HydroPower Index",
    "INVESTMENT INDEX": "Investment Index",
    "LIFE INSURANCE": "Life Insurance",
    "MANUFACTURING AND PROCESSING": "Manufacturing And Processing",
    "MICROFINANCE INDEX": "Microfinance Index",
    "MUTUAL FUND": "Mutual Fund",
    "NON LIFE INSURANCE": "Non Life Insurance",
    "OTHERS INDEX": "Others Index",
    "TRADING INDEX": "Trading Index",
}


def _f(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# How far the contributors feed's prev_close may sit from the stored previous
# close before the feed is judged to be on a different session. Both come from
# the exchange at two decimals, so a genuine match is exact; the tolerance only
# absorbs float/Decimal rounding. A rolled feed is off by a whole day's move.
HEADLINE_PREV_CLOSE_TOLERANCE = 0.0002   # 0.02%, about half a point at 2,600


def _headline_on_session(feed_prev_close, stored_prev_close):
    """True unless the headline feed demonstrably references another session."""
    if feed_prev_close is None or stored_prev_close is None or stored_prev_close <= 0:
        return True   # nothing to check against: keep the historical behaviour
    return abs(feed_prev_close - stored_prev_close) <= stored_prev_close * HEADLINE_PREV_CLOSE_TOLERANCE


def _live_index_bar(key, prev_close=None):
    """Today's live OHLCV bar for a DB index sector_name, from NepseSubIndices.

    Returns (date, open, high, low, close, volume) or None. Intraday (before the
    close is published) it uses the open/high/low snapshot for the close.

    `prev_close` is the last stored session's close. For NEPSE the headline value
    from fetch_contributors() is only trusted when that feed's own prev_close
    matches it. After the market shuts, the feed rolls forward a session: on
    2026-09-14 it reported value=2610.83 with prev_close=2585.04, where 2585.04
    was the day's true close. It had re-applied the day's +1% on top of the close,
    and since the code also stretched `high` up to meet it, the candle closed
    exactly at a high the market never printed.
    """
    sub_key = SUBINDEX_KEYS.get(key)
    if not sub_key:
        return None
    data = fetch_subindices()
    row = data.get(sub_key) if data else None
    if not row:
        return None
    try:
        bar_date = date.fromisoformat(row.get("businessDate"))
    except (TypeError, ValueError):
        return None

    close = _f(row.get("closingIndex")) or 0.0
    if close <= 0:
        close = _f(row.get("highIndex")) or _f(row.get("openIndex")) or _f(row.get("lowIndex"))
    if key == "NEPSE INDEX":
        contributors = fetch_contributors() or {}
        headline = contributors.get("index") or {}
        value = _f(headline.get("value"))
        if value and _headline_on_session(_f(headline.get("prev_close")), _f(prev_close)):
            close = value
    if not close or close <= 0:
        return None
    open_ = _f(row.get("openIndex")) or close
    high = max(_f(row.get("highIndex")) or close, close)
    low = min(_f(row.get("lowIndex")) or close, close)
    volume = _f(row.get("turnoverVolume")) or 0.0
    return (bar_date, open_, high, low, close, volume)


# ── helpers ────────────────────────────────────────────────────────────────

def _clean_symbol(raw):
    """Normalise a requested symbol: strip the optional 'NEPSE:' prefix, upper."""
    sym = (raw or "").strip().upper()
    if ":" in sym:
        sym = sym.split(":", 1)[1]
    return sym


def _company_symbols():
    cached = cache.get("udf_company_symbols")
    if cached is None:
        cached = set(CompanyProfile.objects.values_list("symbol", flat=True))
        cache.set("udf_company_symbols", cached, 300)
    return cached


def _resolve(raw):
    """Return ('index', sector_name) | ('stock', symbol) | (None, None)."""
    sym = _clean_symbol(raw)
    if not sym:
        return None, None
    if sym in INDEX_TICKERS:
        return "index", INDEX_TICKERS[sym]
    if sym in _SECTOR_TO_TICKER:  # raw sector_name supplied directly
        return "index", sym
    if sym in _company_symbols():
        return "stock", sym
    return None, None


def _day_to_ts(d):
    return int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp())


def _symbol_info(kind, key, ticker):
    if kind == "index":
        description = key.title()
    else:
        name = (
            CompanyProfile.objects.filter(symbol=key)
            .values_list("security_name", flat=True)
            .first()
        )
        description = name or key
    return {
        "name": ticker,
        "ticker": ticker,
        "full_name": f"{EXCHANGE}:{ticker}",
        "description": description,
        "type": "index" if kind == "index" else "stock",
        "session": "24x7",
        "exchange": EXCHANGE,
        "listed_exchange": EXCHANGE,
        "timezone": TIMEZONE,
        "minmov": 1,
        "pricescale": 100,
        "has_intraday": False,
        "has_weekly_and_monthly": False,
        "supported_resolutions": SUPPORTED_RESOLUTIONS,
        "volume_precision": 0,
        "data_status": "endofday",
        "currency_code": "NPR",
    }


def _bars(kind, key, from_date, to_date, countback):
    if kind == "index":
        qs = NepseMarketIndex.objects.filter(sector_name=key)
        fields = ("business_date", "open_index", "high_index", "low_index", "close_index", "turnover_volume")
    else:
        qs = NepseDailyStockPrice.objects.filter(symbol=key)
        fields = ("business_date", "open_price", "high_price", "low_price", "close_price", "total_traded_quantity")

    if to_date:
        qs = qs.filter(business_date__lte=to_date)

    # `countback` (when present) takes precedence over `from` per the UDF spec.
    if countback:
        rows = list(qs.order_by("-business_date").values_list(*fields)[:countback])
        rows.reverse()
        return rows

    if from_date:
        qs = qs.filter(business_date__gte=from_date)
    return list(qs.order_by("business_date").values_list(*fields))


def _append_live_index_bar(kind, key, rows, to_date):
    """Refresh/append today's live index bar using the same rule as UDF history."""
    if kind != "index":
        return rows

    # The stored previous close lets the live builder reject a headline feed that
    # has already rolled on to the next session (see _live_index_bar).
    live = _live_index_bar(key, prev_close=rows[-1][4] if rows else None)
    if not live or (to_date is not None and live[0] > to_date):
        return rows

    rows = list(rows)
    if rows and rows[-1][0] == live[0]:
        # Today's bar is already stored from the post-close index sync. That EOD
        # row is the authoritative close, so keep it — do NOT overwrite it with
        # the live snapshot. The live headline (fetch_contributors) can lag a
        # session behind the official close once the market has shut: it reported
        # value=2584.79 with prev_close=2608.33 while the synced bar's true close
        # was 2608.33, producing a bogus candle whose close fell below its own low.
        return rows
    if not rows or live[0] > rows[-1][0]:
        rows.append(live)        # intraday: today's bar isn't synced yet, show live
    return rows


def _chart_bars(kind, key, from_date, to_date, countback):
    """Rows exactly as chart consumers should see them.

    This wraps the stored daily rows with the live index refresh used by the UDF
    history endpoint. Indicator endpoints use it too so oscillator values are
    calculated against the same final bar shown by the candle chart.
    """
    return _append_live_index_bar(kind, key, _bars(kind, key, from_date, to_date, countback), to_date)


# ── endpoints ──────────────────────────────────────────────────────────────

@require_GET
def udf_config(request):
    return JsonResponse({
        "supports_search": True,
        "supports_group_request": False,
        "supports_marks": False,
        "supports_timescale_marks": False,
        "supports_time": True,
        "exchanges": [
            {"value": "", "name": "All Exchanges", "desc": ""},
            {"value": EXCHANGE, "name": EXCHANGE, "desc": "Nepal Stock Exchange"},
        ],
        "symbols_types": [
            {"name": "All", "value": ""},
            {"name": "Index", "value": "index"},
            {"name": "Stock", "value": "stock"},
        ],
        "supported_resolutions": SUPPORTED_RESOLUTIONS,
    })


@require_GET
def udf_time(request):
    now = int(datetime.now(timezone.utc).timestamp())
    return HttpResponse(str(now), content_type="text/plain")


@require_GET
def udf_symbols(request):
    kind, key = _resolve(request.GET.get("symbol", ""))
    if not kind:
        return JsonResponse({"s": "error", "errmsg": "unknown_symbol"}, status=404)
    ticker = _clean_symbol(request.GET.get("symbol", ""))
    return JsonResponse(_symbol_info(kind, key, ticker))


@require_GET
def udf_search(request):
    query = (request.GET.get("query") or "").strip().upper()
    type_filter = (request.GET.get("type") or "").strip().lower()
    try:
        limit = min(int(request.GET.get("limit", 30)), 50)
    except (TypeError, ValueError):
        limit = 30

    results = []

    # Indices first.
    if type_filter in ("", "index"):
        for ticker, sector in INDEX_TICKERS.items():
            if not query or query in ticker or query in sector:
                results.append({
                    "symbol": ticker,
                    "full_name": f"{EXCHANGE}:{ticker}",
                    "description": sector.title(),
                    "exchange": EXCHANGE,
                    "type": "index",
                })

    # Then matching stocks.
    if type_filter in ("", "stock"):
        company_qs = CompanyProfile.objects.all()
        if query:
            company_qs = company_qs.filter(symbol__icontains=query)
        for symbol, name in company_qs.values_list("symbol", "security_name")[: limit * 2]:
            results.append({
                "symbol": symbol,
                "full_name": f"{EXCHANGE}:{symbol}",
                "description": name or symbol,
                "exchange": EXCHANGE,
                "type": "stock",
            })

    return JsonResponse(results[:limit], safe=False)


@require_GET
def udf_history(request):
    kind, key = _resolve(request.GET.get("symbol", ""))
    if not kind:
        return JsonResponse({"s": "error", "errmsg": "unknown_symbol"})

    resolution = (request.GET.get("resolution") or "1D").strip()
    # Intraday is not available (end-of-day data only).
    if resolution.isdigit() or resolution.upper().endswith("S"):
        return JsonResponse({"s": "no_data"})

    def _int(name):
        try:
            return int(request.GET.get(name))
        except (TypeError, ValueError):
            return None

    from_ts, to_ts, countback = _int("from"), _int("to"), _int("countback")
    from_date = datetime.fromtimestamp(from_ts, timezone.utc).date() if from_ts else None
    to_date = datetime.fromtimestamp(to_ts, timezone.utc).date() if to_ts else None

    rows = _chart_bars(kind, key, from_date, to_date, countback)

    if not rows:
        return JsonResponse({"s": "no_data"})

    t, o, h, l, c, v = [], [], [], [], [], []
    for business_date, op, hi, lo, cl, vol in rows:
        t.append(_day_to_ts(business_date))
        o.append(float(op))
        h.append(float(hi))
        l.append(float(lo))
        c.append(float(cl))
        v.append(float(vol or 0))

    return JsonResponse({"s": "ok", "t": t, "o": o, "h": h, "l": l, "c": c, "v": v})
