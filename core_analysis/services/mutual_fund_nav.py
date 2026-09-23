"""mutual_fund_nav.py — scraper for ShareSansar's mutual-fund NAV table.

WHY: mutual funds never appear on the fundamentals desks and never will. They
do not file the quarterly Balance Sheet / Income Statement / Key Statistics that
``FinancialStatement`` harvests — a fund has no revenue, no ROE, no EPS — so
P/E and P/B are category errors for one. What a fund publishes is its NAV, and
since almost every Nepali scheme is closed-end, the useful figure is the gap
between NAV and the market price. This module fills that gap.

The page at /mutual-fund-navs renders three empty <table>s and fills them from a
DataTables server-side feed: the SAME url with ``?type=<n>`` returns JSON. We hit
that feed directly — no HTML table parsing, no browser. Same trick, and the same
HTTP 202 trap, as ``proposed_dividend.py``: the feed validates ``length`` against
the page's own lengthMenu and answers 202 with an empty payload rather than an
error, so PAGE_SIZE must stay at the maximum the site actually allows.

``type`` partitions the universe rather than filtering it, so all three are
fetched and merged on symbol:
    0 — closed-end (the bulk of them)
    1 — matured
    2 — open-end (these also publish daily/weekly NAV and a refund NAV)

HISTORY IS NOT BACKFILLABLE. The feed exposes only each fund's latest reading,
so there is no way to ask it for last year. History accumulates from the first
sync onward, one row per fund per Nepali month — which is why the sync is worth
running monthly even when nothing looks like it changed.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime
from decimal import Decimal, InvalidOperation

import requests
from django.db import connection

from core_analysis.models import MutualFundNav

logger = logging.getLogger(__name__)

BASE_URL = "https://www.sharesansar.com/mutual-fund-navs"
TIMEOUT = 30
# See the module docstring: over the site's own lengthMenu maximum the feed
# answers HTTP 202 with an empty payload instead of an error.
PAGE_SIZE = 50
THROTTLE_SECONDS = 0.6

# The feed's own ``type`` codes, with the labels the page shows for them.
FUND_TYPES = {
    "0": "Closed-end",
    "1": "Matured",
    "2": "Open-end",
}

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "X-Requested-With": "XMLHttpRequest",
    "Referer": BASE_URL,
    "Accept": "application/json, text/javascript, */*; q=0.01",
}

# Everything except the unique key, so a re-sync inside the same Nepali month
# refreshes the reading in place.
_UPDATE_FIELDS = [
    "fund_name", "source_id", "fund_type", "fund_size", "maturity_date",
    "maturity_period", "nav_monthly", "nav_weekly", "nav_weekly_date",
    "nav_daily", "nav_daily_date", "refund_nav", "market_close",
    "market_close_date", "premium_discount_pct", "synced_at",
]


def _dec(value):
    """Decimal, or None. The feed ships numbers as strings and blanks as null."""
    if value in (None, "", "-"):
        return None
    try:
        return Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, ValueError, TypeError):
        return None


def _date(value):
    """ISO date, or None. Anything unparseable is dropped rather than guessed."""
    if not value:
        return None
    try:
        return datetime.strptime(str(value).strip()[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def parse_row(row):
    """One feed row -> model kwargs, or None if it cannot be keyed.

    ``symbol`` and ``nav_period`` are the unique key, so a row missing either is
    unusable. Every other field is allowed to be absent: matured funds have no
    market price, and closed-end funds publish no daily NAV.
    """
    symbol = (row.get("symbol") or "").strip().upper()
    period = (row.get("monthly_date") or "").strip()
    if not symbol or not period:
        return None

    return {
        "symbol": symbol,
        "nav_period": period,
        "fund_name": (row.get("companyname") or "").strip()[:255],
        "source_id": row.get("companyid"),
        "fund_type": str(row.get("type") or "").strip()[:2],
        "fund_size": _dec(row.get("fund_size")),
        "maturity_date": _date(row.get("maturity_date")),
        "maturity_period": (row.get("maturity_period") or "").strip()[:40],
        "nav_monthly": _dec(row.get("monthly_nav_price")),
        "nav_weekly": _dec(row.get("weekly_nav_price")),
        "nav_weekly_date": _date(row.get("weekly_date")),
        "nav_daily": _dec(row.get("daily_nav_price")),
        "nav_daily_date": _date(row.get("daily_date")),
        "refund_nav": _dec(row.get("refund_nav")),
        "market_close": _dec(row.get("close")),
        "market_close_date": _date(row.get("published_date")),
        # The source computes this itself; we store rather than recompute so the
        # figure shown always matches the one its own page shows.
        "premium_discount_pct": _dec(row.get("prem_dis")),
    }


def _fetch_type(session, fund_type):
    """Every row for one ``type``, paging until the feed is exhausted."""
    rows, start, draw = [], 0, 1
    while True:
        params = {"type": fund_type, "draw": draw, "start": start, "length": PAGE_SIZE}
        response = session.get(BASE_URL, params=params, timeout=TIMEOUT)
        response.raise_for_status()
        payload = response.json()
        batch = payload.get("data") or []
        total = payload.get("recordsTotal") or 0

        if not batch and start < total:
            raise RuntimeError(
                f"Feed returned no rows at offset {start} of {total} "
                f"(HTTP {response.status_code}) for type={fund_type}."
            )

        rows.extend(batch)
        start += PAGE_SIZE
        draw += 1
        if start >= total or not batch:
            return rows
        time.sleep(THROTTLE_SECONDS)


def sync():
    """Pull every fund's latest NAV and upsert it.

    Returns a stats dict for the message the Raw Inventory Manager prints.
    Partial failure is reported, not swallowed: if one ``type`` is unreachable
    the other two are still stored and the failure is named.
    """
    session = requests.Session()
    session.headers.update(_HEADERS)
    # The feed sets a cookie on the page view and 202s without it.
    session.get(BASE_URL, timeout=TIMEOUT)

    merged, failed = {}, []
    for index, fund_type in enumerate(sorted(FUND_TYPES)):
        try:
            batch = _fetch_type(session, fund_type)
        except Exception as exc:
            logger.warning("Mutual fund NAV: type=%s failed: %s", fund_type, exc)
            failed.append(FUND_TYPES[fund_type])
            continue
        for row in batch:
            parsed = parse_row(row)
            # (symbol, nav_period) is the unique key; a repeat inside one batch
            # would make bulk_create reject the whole thing.
            if parsed:
                merged[(parsed["symbol"], parsed["nav_period"])] = parsed
        if index < len(FUND_TYPES) - 1:
            time.sleep(THROTTLE_SECONDS)

    objs = [MutualFundNav(**kw) for kw in merged.values()]
    if objs:
        # MySQL's ON DUPLICATE KEY UPDATE has no conflict target, and Django
        # rejects unique_fields on backends without native support.
        kw = {"update_conflicts": True, "update_fields": _UPDATE_FIELDS}
        if connection.features.supports_update_conflicts_with_target:
            kw["unique_fields"] = ["symbol", "nav_period"]
        MutualFundNav.objects.bulk_create(objs, batch_size=500, **kw)

    periods = sorted({kw["nav_period"] for kw in merged.values()})
    return {
        "upserted": len(objs),
        "funds": len({kw["symbol"] for kw in merged.values()}),
        "periods": periods,
        "failed_types": failed,
        "total_rows": MutualFundNav.objects.count(),
    }


def latest_for(symbol):
    """The newest stored NAV reading for one symbol, or None.

    Ordered by ``synced_at`` rather than by ``nav_period``: the period label is
    a Nepali month name, which does not sort chronologically as text.
    """
    sym = (symbol or "").strip().upper()
    if not sym:
        return None
    return MutualFundNav.objects.filter(symbol=sym).order_by("-synced_at", "-id").first()


def coverage():
    """Per-sector counts of which mutual funds have a stored NAV.

    Used by the Raw Inventory Manager so the fund gap is a number on screen
    rather than something you notice by an empty table.
    """
    from core_analysis.models import CompanyProfile

    profiles = dict(
        CompanyProfile.objects
        .filter(sector_name="Mutual Fund")
        .values_list("symbol", "status")
    )
    # Coverage is judged against ACTIVE funds — a delisted scheme having no
    # current NAV is correct, not a gap.
    active = {sym for sym, status in profiles.items() if status == "Active"}
    stored = set(MutualFundNav.objects.values_list("symbol", flat=True).distinct())
    return {
        "listed": len(active),
        "with_nav": len(active & stored),
        "missing": sorted(active - stored),
        # Funds the feed carries that are in NO CompanyProfile at all — usually a
        # new scheme awaiting backfill. Compared against every profiled fund, not
        # just the active ones, or every delisted fund would be listed here.
        "unprofiled": sorted(stored - set(profiles)),
    }


def nav_table():
    """Every fund's latest NAV reading, with our own close and the discount.

    The discount divides by the NEWEST NAV the fund published — the weekly one
    where it exists, else the month-end figure. That is not a detail: the source
    prints both and computes its own premium/discount against the WEEKLY value,
    so dividing by the monthly one instead overstates discounts by as much as
    nine points (verified on KDBY: -9.18% vs -18.52%).

    Prices come from our own EOD table rather than the feed's snapshot, so the
    figure moves with the market instead of with the scraper's last run.
    """
    from core_analysis.models import CompanyProfile, NepseDailyStockPrice

    latest = {}
    for row in MutualFundNav.objects.order_by("symbol", "-synced_at", "-id"):
        latest.setdefault(row.symbol, row)

    symbols = list(latest)
    # Two queries instead of one per fund: each symbol's own last traded date,
    # then the closes on those dates. Per-symbol dates are deliberate — a fund
    # that skipped the last session still shows its actual last close.
    closes = {}
    if symbols:
        from django.db.models import Max, Q

        last_dates = (NepseDailyStockPrice.objects.filter(symbol__in=symbols)
                      .values("symbol").annotate(last=Max("business_date")))
        wanted = Q()
        for row in last_dates:
            wanted |= Q(symbol=row["symbol"], business_date=row["last"])
        if last_dates:
            for p in (NepseDailyStockPrice.objects.filter(wanted)
                      .values("symbol", "close_price", "business_date")):
                closes[p["symbol"]] = p
    names = dict(
        CompanyProfile.objects.filter(symbol__in=symbols)
        .values_list("symbol", "security_name"))

    out = []
    for sym, row in latest.items():
        monthly = float(row.nav_monthly) if row.nav_monthly is not None else None
        weekly = float(row.nav_weekly) if row.nav_weekly is not None else None
        reference = weekly or monthly
        ref_label = "weekly" if weekly else "monthly"
        ref_when = (row.nav_weekly_date.isoformat() if weekly and row.nav_weekly_date
                    else row.nav_period)

        price = float(closes[sym]["close_price"]) if sym in closes else None
        discount = None
        if reference and price:
            discount = round((price - reference) / reference * 100.0, 2)

        out.append({
            "symbol": sym,
            "name": names.get(sym) or row.fund_name,
            "fund_type": FUND_TYPES.get(row.fund_type, ""),
            "is_matured": row.fund_type == "1",
            "nav": reference,
            "nav_basis": ref_label,
            "nav_when": ref_when,
            "nav_monthly": monthly,
            "nav_period": row.nav_period,
            "price": price,
            "price_date": (closes[sym]["business_date"].isoformat() if sym in closes else None),
            "discount_pct": discount,
            "fund_size": float(row.fund_size) if row.fund_size is not None else None,
            "maturity_date": row.maturity_date.isoformat() if row.maturity_date else None,
            "source_discount_pct": (float(row.premium_discount_pct)
                                    if row.premium_discount_pct is not None else None),
        })

    # Deepest discount first — the ordering the desk is actually read in.
    out.sort(key=lambda r: (r["discount_pct"] is None, r["discount_pct"]))
    return out
