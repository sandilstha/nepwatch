"""
broker_analytics.py — "Dalal Street X" style broker analytics built on top of
the trade-level floorsheet feed.

The floorsheet lives in the local ``floorsheet_raw`` table (model
``NepseFloorsheet``), populated by the ``sync_floorsheet`` management command.
Each row is one executed trade with the fields:

    stock_symbol, buyer, seller, quantity, rate, amount, business_date,
    sector, trade_time

Strategy — never scan the whole table. For one trading day we read just that
day's rows (filtered on ``business_date``) and collapse them into a compact
per-day *aggregate*:

    {
      'date':   '2026-06-17',
      'buy':    { symbol: { broker: [qty, amount] } },   # broker == buyer
      'sell':   { symbol: { broker: [qty, amount] } },   # broker == seller
      'sector': { symbol: 'Commercial Banks' },
    }

Every tab (Broker Favorites, Broker Flow Radar, Stock Wise Details, Hotstocks,
Net Holding, Broker Concentration) is derived from this structure. Multi-day
ranges (1W / 1M / 3M)
are just the element-wise sum of several cached day aggregates, so once a day is
built it is reused everywhere. Day aggregates are cached long (immutable history)
except the latest/most-recent day, which gets a short TTL because it still moves
intraday.
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timedelta

from django.core.cache import cache

logger = logging.getLogger(__name__)

# Floorsheet is read from the local floorsheet_raw table (populated by the
# `sync_floorsheet` management command). Per-day aggregates are still cached so
# repeated dashboard hits don't re-run the aggregation over a day of trade rows.

# Cache TTLs.
DAY_TTL_PAST = 60 * 60 * 24 * 7     # finished sessions are immutable for a week
DAY_TTL_LATEST = int(os.environ.get("NEPSE_FLOORSHEET_LATEST_TTL", "300"))
STALE_DAY_TTL = int(os.environ.get("NEPSE_FLOORSHEET_STALE_TTL", "3600"))
LATEST_DATE_TTL = int(os.environ.get("NEPSE_FLOORSHEET_LATEST_DATE_TTL", "90"))
RANGE_TTL = int(os.environ.get("NEPSE_FLOORSHEET_RANGE_TTL", "300"))
META_TTL = 300
FAIL_SENTINEL = "FAIL"
FAIL_TTL = 30
DAY_BUILD_LOCK_TTL = int(os.environ.get("NEPSE_FLOORSHEET_BUILD_LOCK_TTL", "180"))
LOCK_WAIT_SECONDS = float(os.environ.get("NEPSE_FLOORSHEET_LOCK_WAIT_SECONDS", "20"))
LOCK_WAIT_STEP = 0.25

# Rolling preset windows, expressed as calendar spans anchored at the latest
# trading day. They are *resolved* to the actual NEPSE sessions inside the span
# (read from the EOD table), so Saturdays and exchange holidays are excluded
# automatically — "1w" lands on the ~6 Sun–Fri sessions of a NEPSE trading week.
RANGE_DAYS = {"today": 1, "1w": 7, "1m": 30, "3m": 90, "1y": 365}
# "fy" (Nepali fiscal year, period-to-date) is resolved separately from a start
# date, not a fixed span — see _fiscal_year_start / _trading_dates.
NAMED_RANGES = set(RANGE_DAYS) | {"fy"}

# Nepali fiscal year begins on Shrawan 1 ≈ 16 July (Gregorian). The exact day
# drifts ±1 with the Bikram Sambat calendar, but because the window is snapped to
# real trading sessions a one-day proxy error practically never changes the set.
FY_START_MONTH = 7
FY_START_DAY = 16
TOP_N = 10
_MISSING = object()


# ─────────────────────────────────────────────────────────────────────────────
# Local DB read + per-day aggregate
# ─────────────────────────────────────────────────────────────────────────────
def get_latest_trading_date():
    """Most recent date that has floorsheet rows ('YYYY-MM-DD'), or None."""
    cached = cache.get("fs_latest_date")
    if cached is not None:
        return None if cached == FAIL_SENTINEL else cached
    try:
        from core_analysis.models import NepseFloorsheet

        latest = (
            NepseFloorsheet.objects.order_by("-business_date")
            .values_list("business_date", flat=True)
            .first()
        )
        latest = latest.isoformat() if latest else None
    except Exception:  # pragma: no cover - DB optional for this overlay
        cache.set("fs_latest_date", FAIL_SENTINEL, FAIL_TTL)
        return None
    if latest:
        cache.set("fs_latest_date", latest, LATEST_DATE_TTL)
    return latest


def _day_has_rows(date_str):
    """Cheap check whether a calendar date has any floorsheet rows in the DB."""
    key = f"fs_count_{date_str}"
    cached = cache.get(key)
    if cached is not None:
        return cached
    try:
        from core_analysis.models import NepseFloorsheet

        has = NepseFloorsheet.objects.filter(business_date=date_str).exists()
    except Exception:  # pragma: no cover
        return False
    cache.set(key, has, DAY_TTL_PAST if not _is_latest(date_str) else DAY_TTL_LATEST)
    return has


def _is_latest(date_str):
    return date_str == cache.get("fs_latest_date") or date_str == get_latest_trading_date()


def _aggregate_day(date_str):
    """Build one session's (buy, sell, sector) aggregate with DB-side GROUP BY.

    A NEPSE session is ~50k+ trade rows; pulling them all into Python and summing
    row-by-row is slow and memory-hungry. Instead we let MySQL collapse each side
    with two grouped queries (covered by the ``(business_date, buyer)`` /
    ``(business_date, seller)`` indexes), so only one row per (symbol, broker)
    crosses the wire:

        buy[sym][broker]  = [qty, amount]   (broker == buyer)
        sell[sym][broker] = [qty, amount]   (broker == seller)
        sector[sym]       = sector label

    Day-scoped (not range-scoped) on purpose: a single day's GROUP BY rides the
    index, while a wide ``business_date BETWEEN`` GROUP BY filesorts millions of
    rows. Multi-day windows merge these cached per-day results instead. Broker
    keys stay ints (matching the IntegerField columns and ``_typed_broker``).
    """
    from django.db.models import Sum

    from core_analysis.models import NepseFloorsheet

    base = NepseFloorsheet.objects.filter(business_date=date_str, quantity__gt=0)
    buy, sell, sector = {}, {}, {}

    for r in (
        base.filter(buyer__isnull=False)
        .values("stock_symbol", "buyer")
        .annotate(q=Sum("quantity"), a=Sum("amount"))
    ):
        sym = r["stock_symbol"]
        if sym:
            buy.setdefault(sym, {})[r["buyer"]] = [
                float(r["q"] or 0), max(0.0, float(r["a"] or 0))
            ]

    for r in (
        base.filter(seller__isnull=False)
        .values("stock_symbol", "seller")
        .annotate(q=Sum("quantity"), a=Sum("amount"))
    ):
        sym = r["stock_symbol"]
        if sym:
            sell.setdefault(sym, {})[r["seller"]] = [
                float(r["q"] or 0), max(0.0, float(r["a"] or 0))
            ]

    for r in (
        base.exclude(sector__isnull=True)
        .exclude(sector="")
        .values("stock_symbol", "sector")
        .distinct()
    ):
        if r["stock_symbol"]:
            sector.setdefault(r["stock_symbol"], r["sector"])

    return buy, sell, sector


def _cached_day_value(key, stale_key):
    cached = cache.get(key)
    if cached is None:
        return _MISSING
    if cached == FAIL_SENTINEL:
        stale = cache.get(stale_key)
        return stale if stale is not None else None
    return cached


def _wait_for_day_build(key, stale_key):
    deadline = time.monotonic() + LOCK_WAIT_SECONDS
    while time.monotonic() < deadline:
        time.sleep(LOCK_WAIT_STEP)
        cached = _cached_day_value(key, stale_key)
        if cached is not _MISSING:
            return cached
    stale = cache.get(stale_key)
    return stale if stale is not None else _MISSING


def get_day_aggregate(date_str):
    """Compact per-day aggregate (see module docstring). Cached. None on failure."""
    key = f"fs_agg_{date_str}"
    stale_key = f"fs_agg_stale_{date_str}"
    cached = _cached_day_value(key, stale_key)
    if cached is not _MISSING:
        return cached

    lock_key = f"{key}_lock"
    if not cache.add(lock_key, "1", DAY_BUILD_LOCK_TTL):
        cached = _wait_for_day_build(key, stale_key)
        return None if cached is _MISSING else cached

    try:
        try:
            cached = _cached_day_value(key, stale_key)
            if cached is not _MISSING:
                return cached
            buy, sell, sector = _aggregate_day(date_str)
        except Exception:  # pragma: no cover - DB read failure
            cache.set(key, FAIL_SENTINEL, FAIL_TTL)
            stale = cache.get(stale_key)
            return stale if stale is not None else None

        agg = {"date": date_str, "buy": buy, "sell": sell, "sector": sector}
        is_latest = _is_latest(date_str)
        ttl = DAY_TTL_LATEST if is_latest else DAY_TTL_PAST
        cache.set(key, agg, ttl)
        cache.set(stale_key, agg, STALE_DAY_TTL)
        if is_latest:
            cache.set("fs_meta", _build_meta(date_str, agg), META_TTL)
        return agg
    finally:
        cache.delete(lock_key)


def _fiscal_year_start(latest_date):
    """Nepali fiscal-year start (≈ Shrawan 1) on or before ``latest_date``."""
    fy_anchor = latest_date.replace(month=FY_START_MONTH, day=FY_START_DAY)
    if latest_date < fy_anchor:
        fy_anchor = fy_anchor.replace(year=fy_anchor.year - 1)
    return fy_anchor


def _trading_dates(range_key):
    """List of trading-day strings for a range, newest first (incl. latest)."""
    latest = get_latest_trading_date()
    if not latest:
        return []
    if range_key == "today":
        return [latest]
    latest_d = datetime.strptime(latest, "%Y-%m-%d").date()
    if range_key == "fy":
        # Fiscal year to date: every session from Shrawan 1 through the latest.
        return _custom_trading_dates(_fiscal_year_start(latest_d), latest_d)
    span = RANGE_DAYS.get(range_key, 1)
    start = latest_d
    local_dates = _local_trading_dates(start, span)
    if local_dates:
        return local_dates
    dates = []
    for i in range(span):
        d = start - timedelta(days=i)
        ds = d.isoformat()
        if ds == latest or _day_has_rows(ds):
            dates.append(ds)
    return dates


def _local_trading_dates(latest_date, span):
    """Trading dates from the local EOD table; avoids 90 cheap upstream probes."""
    key = f"fs_local_dates_{latest_date.isoformat()}_{span}"
    cached = cache.get(key)
    if cached is not None:
        return cached
    try:
        from core_analysis.models import NepseDailyStockPrice

        start_date = latest_date - timedelta(days=span - 1)
        qs = (
            NepseDailyStockPrice.objects.filter(
                business_date__gte=start_date,
                business_date__lte=latest_date,
            )
            .order_by("-business_date")
            .values_list("business_date", flat=True)
            .distinct()
        )
        dates = [d.isoformat() for d in qs]
    except Exception:  # pragma: no cover - DB optional for this overlay
        dates = []
    cache.set(key, dates, META_TTL)
    return dates


def _range_generation():
    """Monotonic token folded into every range-cache key.

    ``refresh_after_sync`` bumps it when fresh rows land, which invalidates the
    rolling AND custom roll-ups in one step — custom keys embed arbitrary
    start/end dates, so they can't be enumerated for deletion the way the named
    ranges could.
    """
    gen = cache.get("fs_range_gen")
    return gen if gen is not None else 0


def get_range_aggregate(range_key):
    """Sum of every day aggregate in the range. Cached. None if nothing built."""
    range_key = range_key if range_key in NAMED_RANGES else "today"
    key = f"fs_range_{range_key}_{get_latest_trading_date()}_{_range_generation()}"
    cached = cache.get(key)
    if cached is not None:
        return cached or None

    dates = _trading_dates(range_key)
    if not dates:
        cache.set(key, {}, FAIL_TTL)
        return None

    buy, sell, sector = _window_sides(dates)
    if not buy and not sell:
        cache.set(key, {}, FAIL_TTL)
        return None

    merged = {
        "range": range_key,
        "dates": dates,
        "buy": buy,
        "sell": sell,
        "sector": sector,
    }
    cache.set(key, merged, RANGE_TTL)
    return merged


def _merge_into(dst, src):
    """Accumulate one side ({symbol: {broker: [qty, amt]}}) into dst."""
    for sym, brokers in src.items():
        d = dst.setdefault(sym, {})
        for broker, (qty, amt) in brokers.items():
            cell = d.get(broker)
            if cell:
                cell[0] += qty
                cell[1] += amt
            else:
                d[broker] = [qty, amt]


def _window_sides(dates):
    """(buy, sell, sector) for a list of trading-day strings.

    Built by merging the *per-day* aggregates, not a single range-wide query. A
    per-day GROUP BY is bounded and rides the ``(business_date, buyer)`` index
    (~0.4s/session), whereas one ``business_date BETWEEN`` GROUP BY over a wide
    window filesorts millions of rows (measured ~340s for a year vs ~80s cold /
    ~1s warm here). Crucially each finished session is immutable and cached for a
    week, so every other window that touches the same day reuses it — only the
    moving latest day is ever rebuilt.
    """
    if not dates:
        return {}, {}, {}
    buy, sell, sector = {}, {}, {}
    for ds in dates:
        agg = get_day_aggregate(ds)
        if not agg:
            continue
        _merge_into(buy, agg["buy"])
        _merge_into(sell, agg["sell"])
        for sym, sec in agg["sector"].items():
            sector.setdefault(sym, sec)
    return buy, sell, sector


# Hard cap on a custom window so a runaway start/end can't fan out to thousands
# of day-aggregate builds.
CUSTOM_RANGE_MAX_DAYS = int(os.environ.get("NEPSE_FLOORSHEET_CUSTOM_MAX_DAYS", "366"))


def _valid_date(raw):
    """Return a date object if ``raw`` is a valid ISO 'YYYY-MM-DD', else None."""
    raw = (str(raw).strip() if raw is not None else "")
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError:
        return None


def _custom_trading_dates(start, end):
    """Trading-day strings within [start, end] inclusive, from the local EOD table.

    Uses NepseDailyStockPrice (one row per stock per session) as the cheap,
    authoritative calendar of trading days — far lighter than scanning the
    multi-million-row floorsheet for distinct dates.
    """
    key = f"fs_custom_dates_{start}_{end}"
    cached = cache.get(key)
    if cached is not None:
        return cached
    dates = []
    try:
        from core_analysis.models import NepseDailyStockPrice

        qs = (
            NepseDailyStockPrice.objects.filter(
                business_date__gte=start, business_date__lte=end
            )
            .order_by("-business_date")
            .values_list("business_date", flat=True)
            .distinct()
        )
        dates = [d.isoformat() for d in qs]
    except Exception:  # pragma: no cover - DB optional for this overlay
        dates = []
    cache.set(key, dates, META_TTL)
    return dates


def get_custom_range_aggregate(start, end):
    """Sum of every day aggregate in an explicit [start, end] window (inclusive).

    ``start`` / ``end`` are ``date`` objects. Returns the same {buy, sell,
    sector, dates} shape as :func:`get_range_aggregate`, or None when the window
    holds no trading days / no data.
    """
    if start > end:
        start, end = end, start
    span = (end - start).days + 1
    if span > CUSTOM_RANGE_MAX_DAYS:
        start = end - timedelta(days=CUSTOM_RANGE_MAX_DAYS - 1)

    key = f"fs_custom_{start}_{end}_{get_latest_trading_date()}_{_range_generation()}"
    cached = cache.get(key)
    if cached is not None:
        return cached or None

    dates = _custom_trading_dates(start, end)
    if not dates:
        cache.set(key, {}, FAIL_TTL)
        return None

    buy, sell, sector = _window_sides(dates)
    if not buy and not sell:
        cache.set(key, {}, FAIL_TTL)
        return None

    merged = {"range": "custom", "dates": dates, "buy": buy, "sell": sell, "sector": sector}
    cache.set(key, merged, RANGE_TTL)
    return merged


def _window_aggregate(range_key, start=None, end=None):
    """Resolve the aggregate for a tab's date selection.

    ``range_key`` == 'custom' with valid ``start`` / ``end`` (YYYY-MM-DD strings)
    selects that explicit window; anything else uses the rolling preset windows
    ('today', '1w', '1m', '3m') anchored at the latest trading day. An invalid
    custom window falls back to 'today' so a tab never breaks on bad input.
    """
    if range_key == "custom":
        s, e = _valid_date(start), _valid_date(end)
        if s and e:
            return get_custom_range_aggregate(s, e)
        return get_range_aggregate("today")
    return get_range_aggregate(range_key)


# ─────────────────────────────────────────────────────────────────────────────
# Derived helpers
# ─────────────────────────────────────────────────────────────────────────────
def _metric_index(view):
    """0 = quantity (shares traded), 1 = amount (turnover)."""
    return 1 if view == "turnover" else 0


def _side_total(side_map, sym, metric_index=0):
    """Quantity or turnover total for one side, robust to a missing counterparty."""
    return sum(cell[metric_index] for cell in side_map.get(sym, {}).values())


def _row(broker_or_sym, qty, amt, pct):
    avg = (amt / qty) if qty else 0.0
    return {
        "key": broker_or_sym,
        "quantity": round(qty),
        "amount": round(amt, 2),
        "avg_price": round(avg, 2),
        "pct": round(pct, 2),
    }


def _company_names():
    """{symbol: security_name} for nice dropdown labels (best-effort)."""
    cached = cache.get("fs_company_names")
    if cached is not None:
        return cached
    names = dict(_company_meta()["names"])
    cache.set("fs_company_names", names, META_TTL)
    return names


def _company_meta():
    """Company symbols/sectors from local DB, used as instant dropdown fallback."""
    cached = cache.get("fs_company_meta_v2")
    if cached is not None:
        return cached
    # ``symbols`` (the dropdown list) holds ACTIVE companies only — delisted /
    # suspended tickers and their promoter-share twins would otherwise flood the
    # picker. ``names`` keeps every symbol so historical floorsheet rows for
    # since-delisted names still resolve to a company name.
    symbols, sectors, names = [], set(), {}
    try:
        from core_analysis.models import CompanyProfile

        rows = CompanyProfile.objects.values_list(
            "symbol", "security_name", "sector_name", "status"
        )
        for symbol, name, sector, status in rows:
            if not symbol:
                continue
            names[symbol] = name or symbol
            if (status or "").strip().lower() != "active":
                continue
            symbols.append({"symbol": symbol, "name": name or symbol})
            if sector:
                sectors.add(sector)
    except Exception:  # pragma: no cover - DB optional for this overlay
        symbols, names = [], {}
    symbols.sort(key=lambda row: row["symbol"])
    out = {"symbols": symbols, "sectors": sorted(sectors), "names": names}
    cache.set("fs_company_meta_v2", out, META_TTL)
    return out


def broker_names():
    """Map of broker number (str) -> firm name, from the nepse_brokers table.

    Keyed by string so it lines up with the string broker codes the frontend
    sends and the dropdown uses. Cached for META_TTL; empty dict if the table is
    unseeded (run ``manage.py load_brokers``) so callers degrade to bare numbers.
    """
    cached = cache.get("fs_broker_names")
    if cached is not None:
        return cached
    names = {}
    try:
        from core_analysis.models import Broker

        for number, name in Broker.objects.values_list("broker_number", "name"):
            if number is not None and name:
                names[str(number)] = name
    except Exception:  # pragma: no cover - DB optional for this overlay
        names = {}
    cache.set("fs_broker_names", names, META_TTL)
    return names


def _fallback_brokers():
    cached = cache.get("fs_default_brokers")
    if cached is not None:
        return cached
    raw = os.environ.get("NEPSE_BROKER_CHOICES", "").strip()
    if raw:
        brokers = [part.strip() for part in raw.split(",") if part.strip()]
    else:
        max_broker = int(os.environ.get("NEPSE_BROKER_MAX", "100"))
        brokers = list(range(1, max_broker + 1))
    cache.set("fs_default_brokers", brokers, META_TTL)
    return brokers


def _broker_sort_key(broker):
    try:
        return (0, int(broker))
    except (TypeError, ValueError):
        return (1, str(broker))


def _build_meta(latest, agg=None):
    brokers, symbols, sectors = set(), set(), set()
    if agg:
        for side in (agg["buy"], agg["sell"]):
            for sym, brks in side.items():
                symbols.add(sym)
                brokers.update(brks.keys())
        sectors.update(v for v in agg["sector"].values() if v)

    company_meta = _company_meta()
    names = company_meta["names"]
    bnames = broker_names()
    if not symbols:
        return {
            "ok": bool(latest),
            "latest_date": latest,
            "brokers": _fallback_brokers(),
            "broker_names": bnames,
            "symbols": company_meta["symbols"],
            "sectors": company_meta["sectors"],
            "exact": False,
        }

    return {
        "ok": True,
        "latest_date": latest,
        "brokers": sorted(brokers, key=_broker_sort_key),
        "broker_names": bnames,
        "symbols": [{"symbol": s, "name": names.get(s, s)} for s in sorted(symbols)],
        "sectors": sorted(sectors),
        "exact": True,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Cache maintenance (called by sync_floorsheet after new rows land)
# ─────────────────────────────────────────────────────────────────────────────
def refresh_after_sync(dates):
    """Invalidate and warm caches for freshly-synced sessions.

    The floorsheet sync calls this once new trade rows have landed so the broker
    dashboard reflects them immediately instead of serving a stale day aggregate
    until its TTL lapses. For each synced day we drop its cached aggregate /
    row-count and rebuild it (a single ~0.4s DB-side GROUP BY), then clear the
    derived latest-date, meta and rolling-range caches that depend on it. Warming
    here is what keeps the heavy 1Y / FY windows fast: every finished session is
    pre-built, so a range request just merges immutable, already-cached days.

    Best-effort — any cache backend hiccup is swallowed so it can never fail the
    sync itself.
    """
    try:
        date_strs = sorted({str(d) for d in (dates or [])})
        if not date_strs:
            return
        cache.delete("fs_latest_date")
        cache.delete("fs_meta")
        for ds in date_strs:
            cache.delete(f"fs_agg_{ds}")
            cache.delete(f"fs_agg_stale_{ds}")
            cache.delete(f"fs_count_{ds}")
        get_latest_trading_date()  # re-prime fs_latest_date before the rebuilds
        # Bump the range-key generation instead of deleting per-range keys: the
        # rolling AND custom roll-ups all fold this token into their cache keys,
        # so one bump invalidates every window at once (custom windows embed
        # arbitrary start/end dates and can't be enumerated for deletion).
        try:
            cache.incr("fs_range_gen")
        except ValueError:
            cache.add("fs_range_gen", 1, None)
        for ds in date_strs:
            get_day_aggregate(ds)  # rebuild + recache (also refreshes meta for latest)
    except Exception:  # pragma: no cover - cache maintenance must never break sync
        logger.exception("refresh_after_sync failed")


# ─────────────────────────────────────────────────────────────────────────────
# Tab builders
# ─────────────────────────────────────────────────────────────────────────────
def meta():
    """Dropdown data + last-updated stamp for the dashboard shell."""
    cached = cache.get("fs_meta")
    if cached:
        return cached
    latest = get_latest_trading_date()
    agg = None
    if latest:
        cached_agg = _cached_day_value(f"fs_agg_{latest}", f"fs_agg_stale_{latest}")
        if cached_agg is not _MISSING:
            agg = cached_agg
    out = _build_meta(latest, agg)
    cache.set("fs_meta", out, META_TTL if agg else min(META_TTL, 60))
    return out


def meta_cached():
    """Non-blocking meta read for the initial page render (no upstream fetch).

    Returns whatever ``meta()`` last cached, or a local fallback. The page paints
    instantly from this; the frontend refreshes exact metadata in the background.
    """
    cached = cache.get("fs_meta")
    if cached:
        return cached
    latest = cache.get("fs_latest_date")
    if latest == FAIL_SENTINEL:
        latest = None
    return _build_meta(latest, None)


def _typed_broker(broker):
    """Match the dropdown's string broker against int keys in the aggregate."""
    try:
        return int(broker)
    except (TypeError, ValueError):
        return broker


def broker_favorites(brokers, range_key="today", view="shares", start=None, end=None):
    """Top buy / top sell stocks for one or more brokers (Broker Analysis tab).

    ``brokers`` may be a single value or a list/iterable of broker numbers; when
    several are given their flow is pooled (qty/amount summed per stock) so the
    tables show the combined favourites of the selected desk. Pass
    ``range_key='custom'`` with ``start`` / ``end`` (YYYY-MM-DD) to scope the
    result to an explicit date window.
    """
    agg = _window_aggregate(range_key, start, end)
    if not agg:
        return {"ok": False, "buy": [], "sell": []}
    if isinstance(brokers, (list, tuple, set)):
        sel = {_typed_broker(b) for b in brokers}
    else:
        sel = {_typed_broker(brokers)}
    sel.discard(None)
    if not sel:
        return {"ok": True, "buy": [], "sell": []}
    mi = _metric_index(view)

    def side(side_map):
        items = []
        total = 0.0
        for sym, broker_cells in side_map.items():
            q = a = 0.0
            for b in sel:
                cell = broker_cells.get(b)
                if cell:
                    q += cell[0]
                    a += cell[1]
            if q or a:
                items.append((sym, q, a))
                total += a if mi else q
        rows = [
            _row(sym, q, a, (100.0 * (a if mi else q) / total) if total else 0.0)
            for sym, q, a in items
        ]
        rows.sort(key=lambda r: r["amount"] if mi else r["quantity"], reverse=True)
        return rows[:TOP_N], {
            "quantity": round(sum(q for _sym, q, _a in items)),
            "amount": round(sum(a for _sym, _q, a in items), 2),
            "stocks": len(items),
        }

    buy_rows, buy_summary = side(agg["buy"])
    sell_rows, sell_summary = side(agg["sell"])
    touched = {
        sym
        for side_map in (agg["buy"], agg["sell"])
        for sym, broker_cells in side_map.items()
        if any(broker_cells.get(b) for b in sel)
    }
    return {
        "ok": True,
        "buy": buy_rows,
        "sell": sell_rows,
        "summary": {
            "buy_quantity": buy_summary["quantity"],
            "sell_quantity": sell_summary["quantity"],
            "buy_amount": buy_summary["amount"],
            "sell_amount": sell_summary["amount"],
            "buy_stocks": buy_summary["stocks"],
            "sell_stocks": sell_summary["stocks"],
            "stocks_touched": len(touched),
        },
    }


def stock_wise(symbol, range_key="today", view="shares", start=None, end=None):
    """Top buy / sell / holding brokers for one stock (Stock Wise Details tab)."""
    # Normalise: aggregates key on the exact upper-case NEPSE symbols, so a
    # lower-case / padded query must not silently return empty tables.
    symbol = (symbol or "").strip().upper()
    agg = _window_aggregate(range_key, start, end)
    if not agg or not symbol:
        return {"ok": False, "buy": [], "sell": [], "holdings": []}
    mi = _metric_index(view)
    def side(side_map):
        denom = _side_total(side_map, symbol, mi)
        rows = [
            _row(broker, q, a, (100.0 * (a if mi else q) / denom) if denom else 0.0)
            for broker, (q, a) in side_map.get(symbol, {}).items()
        ]
        rows.sort(key=lambda r: r["amount"] if mi else r["quantity"], reverse=True)
        return rows[:TOP_N]

    # Holdings = positive net position per broker (buy qty - sell qty), with
    # net amount and avg buy/sell, matching the Dalal Street X stock-wise table.
    buy_b = agg["buy"].get(symbol, {})
    sell_b = agg["sell"].get(symbol, {})
    holdings = []
    for broker in set(buy_b) | set(sell_b):
        bq, ba = buy_b.get(broker, [0, 0])
        sq, sa = sell_b.get(broker, [0, 0])
        net = bq - sq
        if net <= 0:
            continue
        holdings.append(
            {
                "key": broker,
                "quantity": round(net),
                "amount": round(ba - sa, 2),
                "avg_buy": round(ba / bq, 2) if bq else 0.0,
                "avg_sell": round(sa / sq, 2) if sq else 0.0,
            }
        )
    holdings.sort(key=lambda r: r["quantity"], reverse=True)

    return {
        "ok": True,
        "symbol": symbol,
        "buy": side(agg["buy"]),
        "sell": side(agg["sell"]),
        "holdings": holdings[:TOP_N],
    }


def net_holding(brokers, range_key="today", exclude_mf=False, sector="All", start=None, end=None):
    """Per-stock net position for one or more brokers (Net Holding treemap).

    When several brokers are given their flow is pooled (qty summed per stock),
    matching the combined-desk behaviour of the Broker Analysis tab.
    """
    agg = _window_aggregate(range_key, start, end)
    if not agg:
        return {"ok": False, "items": []}
    if isinstance(brokers, (list, tuple, set)):
        sel = {_typed_broker(b) for b in brokers}
    else:
        sel = {_typed_broker(brokers)}
    sel.discard(None)
    if not sel:
        return {"ok": True, "items": []}
    items = []
    symbols = set(agg["buy"]) | set(agg["sell"])
    for sym in symbols:
        if sector and sector != "All" and agg["sector"].get(sym) != sector:
            continue
        if exclude_mf and _is_mutual_fund(sym, agg["sector"].get(sym)):
            continue
        bq = sum(agg["buy"].get(sym, {}).get(b, [0, 0])[0] for b in sel)
        sq = sum(agg["sell"].get(sym, {}).get(b, [0, 0])[0] for b in sel)
        net = bq - sq
        if net == 0:
            continue
        items.append(
            {"symbol": sym, "net": round(net), "size": abs(round(net)),
             "side": "buy" if net > 0 else "sell",
             "buy": round(bq), "sell": round(sq)}
        )
    items.sort(key=lambda x: x["size"], reverse=True)
    return {"ok": True, "items": items}


def _is_mutual_fund(symbol, sector=None):
    """True if a symbol is a NEPSE mutual fund.

    The floorsheet/company tables carry an authoritative ``sector`` of
    'Mutual Fund' for every close-ended fund, so we key on that first (it
    catches names a symbol-suffix heuristic misses entirely — CSY, GSY, KSY,
    LUK, KEF, …). The suffix heuristic only survives as a fallback for the rare
    row with a blank sector.
    """
    if sector:
        return sector.strip().lower() == "mutual fund"
    return symbol.endswith("MF") or (len(symbol) > 2 and symbol[-1].isdigit() and "MF" in symbol)


def broker_concentration(range_key="today", sector="All", start=None, end=None):
    """Per-stock top-3 broker concentration on each side (Broker Concentration)."""
    agg = _window_aggregate(range_key, start, end)
    if not agg:
        return {"ok": False, "rows": []}
    rows = []
    symbols = set(agg["buy"]) | set(agg["sell"])
    for sym in symbols:
        if sector and sector != "All" and agg["sector"].get(sym) != sector:
            continue
        buy_total = _side_total(agg["buy"], sym)
        sell_total = _side_total(agg["sell"], sym)
        market_total = max(buy_total, sell_total)
        if market_total <= 0:
            continue

        def top3(side_map, side_total):
            brokers = sorted(
                side_map.get(sym, {}).items(), key=lambda kv: kv[1][0], reverse=True
            )[:3]
            out = [
                {"broker": b, "pct": round(100.0 * q / side_total, 2)}
                for b, (q, _a) in brokers
                if side_total > 0
            ]
            return out, round(sum(x["pct"] for x in out), 2)

        buy_top, buy_sum = top3(agg["buy"], buy_total)
        sell_top, sell_sum = top3(agg["sell"], sell_total)
        rows.append(
            {
                "symbol": sym,
                "total": round(market_total),
                "buy": buy_top,
                "buy_sum": buy_sum,
                "sell": sell_top,
                "sell_sum": sell_sum,
            }
        )
    rows.sort(key=lambda r: r["total"], reverse=True)
    return {"ok": True, "rows": rows}


def hotstocks(range_key="today", view="shares", sector="All", start=None, end=None):
    """Most-active stocks with their dominant brokers (Hotstocks tab)."""
    agg = _window_aggregate(range_key, start, end)
    if not agg:
        return {"ok": False, "rows": []}
    rows = []
    symbols = set(agg["buy"]) | set(agg["sell"])
    for sym in symbols:
        if sector and sector != "All" and agg["sector"].get(sym) != sector:
            continue
        buy_qty = _side_total(agg["buy"], sym)
        sell_qty = _side_total(agg["sell"], sym)
        if buy_qty >= sell_qty:
            qty = buy_qty
            amt = _side_total(agg["buy"], sym, 1)
        else:
            qty = sell_qty
            amt = _side_total(agg["sell"], sym, 1)
        if qty <= 0:
            continue

        def lead(side_map, side_total):
            brokers = side_map.get(sym, {})
            if not brokers:
                return None
            b, (q, _a) = max(brokers.items(), key=lambda kv: kv[1][0])
            return {
                "broker": b,
                "pct": round(100.0 * q / side_total, 2) if side_total else 0,
            }

        rows.append(
            {
                "symbol": sym,
                "sector": agg["sector"].get(sym, ""),
                "quantity": round(qty),
                "amount": round(amt, 2),
                "avg_price": round(amt / qty, 2) if qty else 0.0,
                "buyers": len(agg["buy"].get(sym, {})),
                "sellers": len(agg["sell"].get(sym, {})),
                "top_buy": lead(agg["buy"], buy_qty),
                "top_sell": lead(agg["sell"], sell_qty),
            }
        )
    rows.sort(key=lambda r: r["amount"] if view == "turnover" else r["quantity"],
              reverse=True)
    return {"ok": True, "rows": rows[:50]}


def broker_flow_radar(range_key="today", start=None, end=None):
    """Broker-wide flow ranking for the full market.

    This extends a basic "Top Brokers" table with actionable but factual flow
    reads: gross activity, net direction, two-sided matching, and bias.
    """
    agg = _window_aggregate(range_key, start, end)
    if not agg:
        return {"ok": False, "rows": []}

    bnames = broker_names()
    brokers = {}

    def cell_for(broker):
        return brokers.setdefault(
            broker,
            {
                "broker": broker,
                "broker_name": "",
                "buy_qty": 0.0,
                "sell_qty": 0.0,
                "buy_amount": 0.0,
                "sell_amount": 0.0,
            },
        )

    for side_name, side_map in (("buy", agg["buy"]), ("sell", agg["sell"])):
        for _sym, broker_cells in side_map.items():
            for broker, (qty, amt) in broker_cells.items():
                row = cell_for(broker)
                if side_name == "buy":
                    row["buy_qty"] += qty
                    row["buy_amount"] += amt
                else:
                    row["sell_qty"] += qty
                    row["sell_amount"] += amt

    rows = []
    for row in brokers.values():
        buy_amt = row["buy_amount"]
        sell_amt = row["sell_amount"]
        total_amt = buy_amt + sell_amt
        if total_amt <= 0:
            continue
        diff = buy_amt - sell_amt
        bias_pct = 100.0 * diff / total_amt
        if bias_pct >= 10:
            stance = "Accumulating"
        elif bias_pct <= -10:
            stance = "Distributing"
        else:
            stance = "Balanced"
        rows.append({
            "broker": row["broker"],
            "broker_name": bnames.get(str(row["broker"]), row["broker_name"]),
            "buy_quantity": round(row["buy_qty"]),
            "sell_quantity": round(row["sell_qty"]),
            "buy_amount": round(buy_amt, 2),
            "sell_amount": round(sell_amt, 2),
            "total_amount": round(total_amt, 2),
            "difference": round(diff, 2),
            "matching_amount": round(min(buy_amt, sell_amt), 2),
            "matching_pct": round(200.0 * min(buy_amt, sell_amt) / total_amt, 2),
            "bias_pct": round(bias_pct, 2),
            "stance": stance,
        })

    rows.sort(key=lambda r: r["total_amount"], reverse=True)
    return {"ok": True, "days": len(agg.get("dates") or []), "rows": rows}


# HHI concentration bands (0-10000 scale). Aligned with the DOJ/FTC merger
# guidelines so the label is a recognised market-structure read, not invented.
HHI_MODERATE = 1500
HHI_HIGH = 2500


def broker_persistence(brokers, range_key="1w", sector="All", exclude_mf=False,
                       start=None, end=None):
    """Multi-day persistence + concentration for a desk (Broker Flow headline).

    Walks every trading day in the window and tracks the selected broker(s)'
    pooled net position (buy qty − sell qty) per stock, then surfaces, per stock:

    * ``streak``  — consecutive most-recent sessions the desk stayed on one side
      (its current conviction run; 0 if flat on the latest session),
    * ``side``    — buy / sell / flat for that current run,
    * ``cum_net`` — net shares accumulated/distributed across the whole window,
    * ``buy_days`` / ``sell_days`` / ``active_days`` — how the window split,
    * ``hhi``     — Herfindahl–Hirschman concentration of *all-broker* trading in
      the stock over the window (0 = fragmented … 10000 = a single broker), with
      a ``risk`` band (low / moderate / high),
    * ``dominant``— the single most-active broker in the stock and its share %.

    streak / cum_net are broker-specific — only NEPSE's broker-tagged floorsheet
    makes them computable. HHI is a market-structure read on the same stocks.
    Both are factual roll-ups: no predictive "smart money"/"success" labelling.
    """
    agg = _window_aggregate(range_key, start, end)
    if not agg:
        return {"ok": False, "rows": []}
    if isinstance(brokers, (list, tuple, set)):
        sel = {_typed_broker(b) for b in brokers}
    else:
        sel = {_typed_broker(brokers)}
    sel.discard(None)
    if not sel:
        return {"ok": True, "rows": []}

    # Newest-first sessions so the streak walk starts at the latest day.
    dates = sorted(agg.get("dates") or [], reverse=True)
    n_days = len(dates)

    # Per-day pooled net for the desk: {symbol: {day_index: net_qty}}, day 0 = latest.
    per_day = {}
    for di, ds in enumerate(dates):
        day = get_day_aggregate(ds)
        if not day:
            continue
        buy_m, sell_m = day.get("buy", {}), day.get("sell", {})
        for sym in set(buy_m) | set(sell_m):
            bq = sum(buy_m.get(sym, {}).get(b, (0, 0))[0] for b in sel)
            sq = sum(sell_m.get(sym, {}).get(b, (0, 0))[0] for b in sel)
            net = bq - sq
            if net:
                per_day.setdefault(sym, {})[di] = net

    rows = []
    for sym, day_nets in per_day.items():
        if sector and sector != "All" and agg["sector"].get(sym) != sector:
            continue
        if exclude_mf and _is_mutual_fund(sym, agg["sector"].get(sym)):
            continue

        # Streak: consecutive most-recent sessions on one side (break on a flat
        # or opposite session). day index 0 == latest trading day.
        streak, run_side = 0, None
        for di in range(n_days):
            net = day_nets.get(di, 0)
            if not net:
                break
            s = 1 if net > 0 else -1
            if run_side is None:
                run_side = s
            elif s != run_side:
                break
            streak += 1

        # HHI over every broker trading this stock in the window (buy side; buy
        # qty == sell qty in aggregate). share_i in 0..1 → squared → ×10000.
        buy_cells = agg["buy"].get(sym, {})
        sell_cells = agg["sell"].get(sym, {})
        buy_q = sum(q for q, _a in buy_cells.values())
        sell_q = sum(q for q, _a in sell_cells.values())
        cells = buy_cells if buy_q >= sell_q else sell_cells
        total_q = max(buy_q, sell_q)
        hhi, dominant = 0.0, None
        if total_q > 0:
            top_b, top_q = None, 0.0
            for b, (q, _a) in cells.items():
                share = q / total_q
                hhi += share * share
                if q > top_q:
                    top_q, top_b = q, b
            hhi *= 10000.0
            dominant = {"broker": top_b, "pct": round(100.0 * top_q / total_q, 2)}
        risk = "high" if hhi >= HHI_HIGH else "moderate" if hhi >= HHI_MODERATE else "low"

        rows.append({
            "symbol": sym,
            "side": "buy" if run_side == 1 else "sell" if run_side == -1 else "flat",
            "streak": streak,
            "cum_net": round(sum(day_nets.values())),
            "active_days": len(day_nets),
            "buy_days": sum(1 for v in day_nets.values() if v > 0),
            "sell_days": sum(1 for v in day_nets.values() if v < 0),
            "hhi": round(hhi),
            "risk": risk,
            "dominant": dominant,
        })

    # Strongest current conviction first, then by size of net position.
    rows.sort(key=lambda r: (r["streak"], abs(r["cum_net"])), reverse=True)
    return {"ok": True, "days": n_days, "rows": rows[:40]}


def _window_close_changes(symbols, dates):
    """% price change over the window for each symbol (first vs last close in it).

    Prefers the bonus/rights-ADJUSTED series (StockPriceAdjustment.close_price_adj)
    and falls back to raw NepseDailyStockPrice closes for symbols the adjusted
    table does not cover in the window. The raw close books a bonus ex-date as a
    price crash — which put scrips whose adjusted price actually ROSE into the
    A/D Radar's "Quiet Accumulation" list and into the divergence signal as
    'accum_weak'. Returns {symbol: pct_change}; symbols without two priced
    sessions are simply omitted.
    """
    if not symbols or not dates:
        return {}
    series = {}
    adjusted = set()
    try:
        from core_analysis.models import NepseDailyStockPrice, StockPriceAdjustment

        d0 = datetime.strptime(min(dates), "%Y-%m-%d").date()
        d1 = datetime.strptime(max(dates), "%Y-%m-%d").date()
        # Adjusted series first. Iterate INSIDE the try — querysets are lazy, so
        # the DB only executes here; an error during iteration must also degrade
        # (divergence simply drops out) instead of failing the whole payload.
        adj_qs = (
            StockPriceAdjustment.objects.filter(
                company__symbol__in=list(symbols),
                business_date__gte=d0, business_date__lte=d1,
                close_price_adj__isnull=False,
            )
            .values_list("company__symbol", "business_date", "close_price_adj")
        )
        for sym, bd, cp in adj_qs:
            if cp:
                series.setdefault(sym, []).append((bd, float(cp)))
        # A single adjusted row cannot make a change; treat it as no coverage so
        # the raw fallback still produces a figure for that symbol. Partial
        # adjusted entries are DROPPED, not appended to — mixing one adjusted
        # close with raw closes would put two different price scales in the same
        # series whenever a corp action sits in the window, which is exactly the
        # distortion this function exists to avoid.
        adjusted = {s for s, arr in series.items() if len(arr) >= 2}
        raw_needed = [s for s in symbols if s not in adjusted]
        for s in raw_needed:
            series.pop(s, None)
        if raw_needed:
            qs = (
                NepseDailyStockPrice.objects.filter(
                    symbol__in=raw_needed, business_date__gte=d0, business_date__lte=d1
                )
                .values_list("symbol", "business_date", "close_price")
            )
            for sym, bd, cp in qs:
                series.setdefault(sym, []).append((bd, float(cp)))
    except Exception:  # pragma: no cover - DB optional for this overlay
        return {}
    out = {}
    for sym, arr in series.items():
        if len(arr) < 2:
            continue
        arr.sort()
        first, last = arr[0][1], arr[-1][1]
        if first:
            out[sym] = 100.0 * (last - first) / first
    return out


def broker_signals(brokers, range_key="1m", sector="All", exclude_mf=False, start=None, end=None):
    """Four research-desk signals for a broker selection, one window pass.

    All derive from the broker-tagged floorsheet (+ local closes for divergence);
    every field is a factual roll-up, never a prediction:

    * ``divergence`` — desk net flow disagrees with price: net-buying while price
      fell ('accum_weak') or net-selling into a rising price ('distrib_strong').
    * ``breadth``    — market-wide count of distinct brokers net-buying vs
      net-selling each stock (consensus; complements the HHI concentration read).
    * ``two_sided``  — stocks the *selected desk* both bought and sold heavily
      (churn %: 100 = perfectly balanced two-siding / market-making).
    * ``sectors``    — the desk's net flow rolled up by sector (rotation).
    """
    agg = _window_aggregate(range_key, start, end)
    if not agg:
        return {"ok": False, "divergence": [], "breadth": [], "two_sided": [], "sectors": []}
    if isinstance(brokers, (list, tuple, set)):
        sel = {_typed_broker(b) for b in brokers}
    else:
        sel = {_typed_broker(brokers)}
    sel.discard(None)
    if not sel:
        return {"ok": True, "divergence": [], "breadth": [], "two_sided": [], "sectors": []}

    buy, sell, secmap = agg["buy"], agg["sell"], agg["sector"]

    def passes(sym):
        if sector and sector != "All" and secmap.get(sym) != sector:
            return False
        if exclude_mf and _is_mutual_fund(sym, secmap.get(sym)):
            return False
        return True

    symbols = [s for s in (set(buy) | set(sell)) if passes(s)]

    # Desk net per stock (drives divergence, two-sided, sector rotation).
    desk = {}
    for sym in symbols:
        bq = sum(buy.get(sym, {}).get(b, (0, 0))[0] for b in sel)
        sq = sum(sell.get(sym, {}).get(b, (0, 0))[0] for b in sel)
        if bq or sq:
            desk[sym] = (bq, sq)

    # Breadth: distinct net-buying vs net-selling brokers per stock (all brokers).
    breadth = []
    for sym in symbols:
        bb, sb = buy.get(sym, {}), sell.get(sym, {})
        nb = ns = 0
        for b in set(bb) | set(sb):
            net = bb.get(b, (0, 0))[0] - sb.get(b, (0, 0))[0]
            if net > 0:
                nb += 1
            elif net < 0:
                ns += 1
        if nb or ns:
            breadth.append({
                "symbol": sym, "buyers": nb, "sellers": ns, "net": nb - ns,
                "total": round(sum(q for q, _a in bb.values())),
            })
    breadth.sort(key=lambda r: r["total"], reverse=True)

    # Two-sided: desk both bought and sold the name. churn = 2·min/(buy+sell)·100
    # (100 = perfectly balanced). Require churn ≥ 33 (at least a 1:2 split) so a
    # token opposite leg isn't flagged, then rank by genuinely two-sided volume
    # (the smaller side) — not by churn, else tiny balanced trades dominate.
    two_sided = []
    for sym, (bq, sq) in desk.items():
        if bq > 0 and sq > 0:
            churn = round(200.0 * min(bq, sq) / (bq + sq), 1)
            if churn >= 33:
                two_sided.append({
                    "symbol": sym, "buy": round(bq), "sell": round(sq),
                    "net": round(bq - sq), "churn": churn, "two_sided": round(min(bq, sq)),
                })
    two_sided.sort(key=lambda r: r["two_sided"], reverse=True)

    # Sector rotation: desk net qty by sector.
    sec_net = {}
    for sym, (bq, sq) in desk.items():
        sec_net[secmap.get(sym) or "—"] = sec_net.get(secmap.get(sym) or "—", 0) + (bq - sq)
    sectors = [{"sector": k, "net": round(v)} for k, v in sec_net.items() if v]
    sectors.sort(key=lambda r: abs(r["net"]), reverse=True)

    # Divergence: desk net vs window price change.
    price_chg = _window_close_changes(list(desk.keys()), agg.get("dates") or [])
    divergence = []
    for sym, (bq, sq) in desk.items():
        net = bq - sq
        pc = price_chg.get(sym)
        if not net or pc is None:
            continue
        if net > 0 and pc < 0:
            typ = "accum_weak"
        elif net < 0 and pc > 0:
            typ = "distrib_strong"
        else:
            continue
        divergence.append({"symbol": sym, "net": round(net), "price_chg": round(pc, 2), "type": typ})
    divergence.sort(key=lambda r: abs(r["net"]), reverse=True)

    return {
        "ok": True,
        "days": len(agg.get("dates") or []),
        "divergence": divergence[:15],
        "breadth": breadth[:15],
        "two_sided": two_sided[:15],
        "sectors": sectors[:15],
    }


