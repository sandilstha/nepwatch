"""Raw NEPSE data reports — the tabular views the exchange itself publishes.

These are deliberately *reports*, not analytics: no derived signals, no scoring,
no opinion. Each one answers "what did the exchange record", so the numbers can
be checked against nepalstock.com line by line. Everything is served from tables
already synced locally, so nothing here calls out to the network.

One REPORTS registry drives all of them. A report declares its columns and a
builder that returns plain row dicts; the view layer, the template and the
client-side table are generic over that, so adding a tenth report is a registry
entry rather than another view/template/JS triple.

Market Depth is deliberately absent: it is a live order book that only exists
intraday on the exchange feed and is not retained in any local table, so it
cannot be reconstructed from history the way these nine can.
"""

import hashlib
import logging
from datetime import timedelta

from django.core.cache import cache
from django.db.models import Sum, Count, Avg, Min, Max, F, Q

logger = logging.getLogger(__name__)

_TTL = 5 * 60

# Column types drive alignment + client-side sorting: "num" sorts numerically and
# right-aligns, "str" sorts lexically, "date" sorts as ISO text (which is
# chronological), "pct" and "rs" are numeric with their own formatting.
# ---------------------------------------------------------------------------


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def latest_price_date():
    from core_analysis.models import NepseDailyStockPrice as P

    return P.objects.order_by("-business_date").values_list(
        "business_date", flat=True
    ).first()


def latest_index_date():
    from core_analysis.models import NepseMarketIndex as I

    return I.objects.order_by("-business_date").values_list(
        "business_date", flat=True
    ).first()


def latest_floorsheet_date():
    from core_analysis.models import NepseFloorsheet as F_

    return F_.objects.order_by("-business_date").values_list(
        "business_date", flat=True
    ).first()


def latest_mcap_date():
    """Newest session that actually carries market capitalisation.

    The upstream feed intermittently returns 0 for ``market_capitalization`` on
    the most recent session(s), which would render the whole Market Cap report
    as zeros. Anchoring to the newest session with real values keeps the report
    truthful; the UI says which date it fell back to rather than pretending the
    latest one was fine.
    """
    from core_analysis.models import NepseDailyStockPrice as P

    key = "ndr:mcapdate"
    hit = cache.get(key)
    if hit is not None:
        return hit
    d = (
        P.objects.exclude(market_capitalization=0)
        .order_by("-business_date")
        .values_list("business_date", flat=True)
        .first()
    )
    cache.set(key, d, _TTL)
    return d


def price_dates(limit=400):
    """Sessions that have EOD price rows, newest first — powers date pickers."""
    from core_analysis.models import NepseDailyStockPrice as P

    key = f"ndr:pdates:{limit}"
    hit = cache.get(key)
    if hit is not None:
        return hit
    out = [
        d.isoformat()
        for d in P.objects.order_by("-business_date")
        .values_list("business_date", flat=True)
        .distinct()[:limit]
    ]
    cache.set(key, out, _TTL)
    return out


# ── report builders ────────────────────────────────────────────────────────
# Each takes (params) and returns {"rows": [...], "meta": {...}}.


def _todays_price(p):
    """Full EOD price sheet for one session — the exchange's 'Today's Price'."""
    from core_analysis.models import NepseDailyStockPrice as P

    day = p.get("date") or latest_price_date()
    if not day:
        return {"rows": [], "meta": {}}
    # Same derivation as the Market Cap report: the feed returns 0 for this
    # column on the newest session(s), so it is repriced from the last known
    # share count rather than shown as zeros.
    shares, shares_src = _shares_outstanding(day)

    qs = P.objects.filter(business_date=day).order_by("symbol")
    rows = []
    derived = 0
    for r in qs.values(
        "symbol", "security_name", "open_price", "high_price", "low_price",
        "close_price", "previous_close", "average_traded_price",
        "total_traded_quantity", "total_traded_value", "total_trades",
        "market_capitalization", "fifty_two_week_high", "fifty_two_week_low",
    ):
        prev, close = _f(r["previous_close"]), _f(r["close_price"])
        mcap = _f(r["market_capitalization"])
        if mcap <= 0:
            sh = shares.get(r["symbol"])
            if sh and close > 0:
                mcap = sh * close
                derived += 1
        rows.append({
            "symbol": r["symbol"],
            "name": r["security_name"],
            "open": _f(r["open_price"]),
            "high": _f(r["high_price"]),
            "low": _f(r["low_price"]),
            "close": close,
            "prev_close": prev,
            # Change is derived rather than stored, so it always agrees with the
            # close/previous-close pair shown on the same row.
            "change": round(close - prev, 2),
            "pct_change": round(100.0 * (close - prev) / prev, 2) if prev else None,
            "vwap": _f(r["average_traded_price"]),
            "qty": int(r["total_traded_quantity"] or 0),
            "turnover": _f(r["total_traded_value"]),
            "trades": int(r["total_trades"] or 0),
            "mcap": round(mcap, 2) if mcap else None,
            "wk52_high": _f(r["fifty_two_week_high"]),
            "wk52_low": _f(r["fifty_two_week_low"]),
        })
    meta = {"date": str(day), "count": len(rows), "derived": derived}
    if derived:
        meta["warning"] = (
            f"Market Cap is 0 in the feed for {day}, so {derived} of {len(rows)} "
            f"values are computed as close x shares, with shares carried from "
            f"{shares_src}. Every other column is as reported."
        )
    return {"rows": rows, "meta": meta}


def _stock_trading(p):
    """One symbol's EOD history — the exchange's per-scrip 'Stock Trading'."""
    from core_analysis.models import NepseDailyStockPrice as P

    sym = (p.get("symbol") or "").strip().upper()
    if not sym:
        return {"rows": [], "meta": {"error": "Choose a symbol."}}
    qs = P.objects.filter(symbol=sym)
    if p.get("start"):
        qs = qs.filter(business_date__gte=p["start"])
    if p.get("end"):
        qs = qs.filter(business_date__lte=p["end"])
    # Zero-market-cap rows are NOT only recent here: NABIL has 105 of them going
    # back to 2022. A single current share count cannot fill those — its implied
    # shares run from 4.6m to 270.6m across the history (a 58x change from bonus
    # issues), so today's count would overstate a 2022 cap enormously.
    #
    # Instead the share count is carried forward from the nearest EARLIER session
    # that did report one, walking the history oldest-first. Rows before any
    # reported session keep a blank cap rather than a fabricated one.
    src = list(qs.order_by("business_date").values(
        "business_date", "open_price", "high_price", "low_price", "close_price",
        "previous_close", "average_traded_price", "total_traded_quantity",
        "total_traded_value", "total_trades", "market_capitalization",
    ))
    carried, derived = None, 0
    for r in src:
        close, mcap = _f(r["close_price"]), _f(r["market_capitalization"])
        if mcap > 0 and close > 0:
            carried = mcap / close
            r["_mcap"] = mcap
        elif carried and close > 0:
            r["_mcap"] = carried * close
            derived += 1
        else:
            r["_mcap"] = 0.0

    rows = []
    for r in reversed(src[-2000:]):
        prev, close = _f(r["previous_close"]), _f(r["close_price"])
        mcap = r["_mcap"]
        rows.append({
            "date": r["business_date"].isoformat(),
            "open": _f(r["open_price"]),
            "high": _f(r["high_price"]),
            "low": _f(r["low_price"]),
            "close": close,
            "prev_close": prev,
            "change": round(close - prev, 2),
            "pct_change": round(100.0 * (close - prev) / prev, 2) if prev else None,
            "vwap": _f(r["average_traded_price"]),
            "qty": int(r["total_traded_quantity"] or 0),
            "turnover": _f(r["total_traded_value"]),
            "trades": int(r["total_trades"] or 0),
            "mcap": round(mcap, 2) if mcap else None,
        })
    meta = {"symbol": sym, "count": len(rows), "derived": derived}
    if derived:
        meta["warning"] = (
            f"{derived} session(s) report no market cap in the feed. Those are "
            "computed as close x shares, using the share count from the nearest "
            "earlier session that did report one — so a bonus or rights issue "
            "between the two would understate them."
        )
    return {"rows": rows, "meta": meta}


def _datewise_indices(p):
    """Every index/sub-index for one session, or one index over time."""
    from core_analysis.models import NepseMarketIndex as I

    sector = (p.get("sector") or "").strip()
    qs = I.objects.all()
    if sector:
        # One index across dates.
        qs = qs.filter(sector_name=sector)
        if p.get("start"):
            qs = qs.filter(business_date__gte=p["start"])
        if p.get("end"):
            qs = qs.filter(business_date__lte=p["end"])
        qs = qs.order_by("-business_date")[:2000]
        label = {"sector": sector}
    else:
        day = p.get("date") or latest_index_date()
        if not day:
            return {"rows": [], "meta": {}}
        qs = qs.filter(business_date=day).order_by("sector_name")
        label = {"date": str(day)}

    rows = [{
        "date": r["business_date"].isoformat(),
        "index": r["sector_name"],
        "open": _f(r["open_index"]),
        "high": _f(r["high_index"]),
        "low": _f(r["low_index"]),
        "close": _f(r["close_index"]),
        "change": _f(r["absolute_change"]),
        "pct_change": round(_f(r["percentage_change"]), 2),
        "turnover": _f(r["turnover_values"]),
        "qty": int(r["turnover_volume"] or 0),
        "trades": int(r["total_transaction"] or 0),
    } for r in qs.values(
        "business_date", "sector_name", "open_index", "high_index", "low_index",
        "close_index", "absolute_change", "percentage_change", "turnover_values",
        "turnover_volume", "total_transaction",
    )]
    return {"rows": rows, "meta": dict(label, count=len(rows))}


# CompanyProfile sector names and NepseMarketIndex index names do not match, so
# the join has to be explicit. Sectors with no published index (debentures,
# preference shares) map to None and simply show no index column.
_SECTOR_INDEX = {
    "Commercial Banks": "BANKING SUBINDEX",
    "Development Banks": "DEVELOPMENT BANK INDEX",
    "Finance": "FINANCE INDEX",
    "Hotels And Tourism": "HOTELS AND TOURISM INDEX",
    "Hydro Power": "HYDROPOWER INDEX",
    "Investment": "INVESTMENT INDEX",
    "Life Insurance": "LIFE INSURANCE",
    "Manufacturing And Processing": "MANUFACTURING AND PROCESSING",
    "Microfinance": "MICROFINANCE INDEX",
    "Mutual Fund": "MUTUAL FUND",
    "Non Life Insurance": "NON LIFE INSURANCE",
    "Others": "OTHERS INDEX",
    "Tradings": "TRADING INDEX",
}


def _sector_summary(p):
    """Per-sector totals for one session, built from the constituent scrips.

    Deliberately aggregated from ``nepse_daily_stock_prices`` joined to the
    company sector rather than read off the sector index: the index row carries
    turnover but not the scrip counts (advancing / declining / traded) that make
    a sector summary useful.
    """
    from core_analysis.models import (
        NepseDailyStockPrice as P, CompanyProfile, NepseMarketIndex,
    )

    day = p.get("date") or latest_price_date()
    if not day:
        return {"rows": [], "meta": {}}

    sectors = dict(
        CompanyProfile.objects.exclude(sector_name="")
        .values_list("symbol", "sector_name")
    )
    # Share counts let the sector's cap MOVE be computed as shares x (close -
    # previous close) — the actual rupee weight each sector added or removed
    # from the market. Summing the feed's cap column cannot do this, and is
    # zero on recent sessions anyway.
    shares, shares_src = _shares_outstanding(day)

    agg = {}
    for r in P.objects.filter(business_date=day).values(
        "symbol", "close_price", "previous_close", "total_traded_quantity",
        "total_traded_value", "total_trades", "market_capitalization",
    ):
        sec = sectors.get(r["symbol"]) or "Unclassified"
        s = agg.setdefault(sec, {
            "turnover": 0.0, "qty": 0, "trades": 0, "mcap": 0.0, "impact": 0.0,
            "prev_mcap": 0.0, "scrips": 0, "adv": 0, "dec": 0, "unch": 0,
        })
        prev, close = _f(r["previous_close"]), _f(r["close_price"])
        reported = _f(r["market_capitalization"])
        sh = shares.get(r["symbol"])
        if not sh and close > 0 and reported > 0:
            sh = reported / close
        cap = (sh * close) if sh else reported
        prev_cap = (sh * prev) if sh else 0.0

        s["turnover"] += _f(r["total_traded_value"])
        s["qty"] += int(r["total_traded_quantity"] or 0)
        s["trades"] += int(r["total_trades"] or 0)
        s["mcap"] += cap
        s["prev_mcap"] += prev_cap
        s["impact"] += (cap - prev_cap) if sh else 0.0
        s["scrips"] += 1
        if close > prev:
            s["adv"] += 1
        elif close < prev:
            s["dec"] += 1
        else:
            s["unch"] += 1

    # Sector index close and move, joined by the explicit name map.
    idx = {}
    wanted = {v: k for k, v in _SECTOR_INDEX.items()}
    for r in NepseMarketIndex.objects.filter(
        business_date=day, sector_name__in=list(wanted)
    ).values("sector_name", "close_index", "percentage_change"):
        idx[wanted[r["sector_name"]]] = (
            _f(r["close_index"]), round(_f(r["percentage_change"]), 2)
        )

    total_to = sum(v["turnover"] for v in agg.values()) or 0.0
    total_cap = sum(v["mcap"] for v in agg.values()) or 0.0
    # Share of the day's GROSS movement, so gainers and losers do not cancel out
    # and the column stays meaningful on a flat day.
    gross_move = sum(abs(v["impact"]) for v in agg.values()) or 0.0

    rows = []
    for k, v in agg.items():
        ix = idx.get(k)
        rows.append({
            "sector": k,
            "scrips": v["scrips"],
            "advancing": v["adv"],
            "declining": v["dec"],
            "unchanged": v["unch"],
            "index_close": ix[0] if ix else None,
            "index_pct": ix[1] if ix else None,
            "mcap": round(v["mcap"], 2) if v["mcap"] else None,
            "pct_mcap": round(100.0 * v["mcap"] / total_cap, 2) if total_cap else None,
            "impact": round(v["impact"], 2),
            "impact_pct": round(100.0 * v["impact"] / v["prev_mcap"], 2) if v["prev_mcap"] else None,
            "share_of_move": round(100.0 * abs(v["impact"]) / gross_move, 2) if gross_move else None,
            "qty": v["qty"],
            "turnover": round(v["turnover"], 2),
            "pct_turnover": round(100.0 * v["turnover"] / total_to, 2) if total_to else None,
            "trades": v["trades"],
        })
    # Biggest mover first — the whole point of the page is "what moved the market".
    rows.sort(key=lambda r: abs(r["impact"] or 0), reverse=True)

    net = sum(v["impact"] for v in agg.values())
    return {"rows": rows, "meta": {
        "date": str(day), "count": len(rows),
        "net_impact": round(net, 2), "gross_move": round(gross_move, 2),
        "shares_from": str(shares_src) if shares_src else None,
        "warning": (
            "Impact is shares x (close - previous close), i.e. the rupee weight each "
            "sector added to or removed from the market. Share counts are carried "
            f"from {shares_src}, the last session the feed reported market cap."
        ) if shares_src else None,
    }}


def _market_summary(p):
    """Whole-market totals per session — turnover, volume, trades, cap, breadth."""
    from core_analysis.models import NepseDailyStockPrice as P

    end = p.get("end") or latest_price_date()
    if not end:
        return {"rows": [], "meta": {}}
    start = p.get("start")
    if not start:
        end_d = end if not isinstance(end, str) else None
        from datetime import date as _d
        end_d = end_d or _d.fromisoformat(str(end))
        start = (end_d - timedelta(days=90)).isoformat()

    qs = P.objects.filter(business_date__gte=start, business_date__lte=end)
    totals = {
        r["business_date"]: r
        for r in qs.values("business_date").annotate(
            turnover=Sum("total_traded_value"),
            qty=Sum("total_traded_quantity"),
            trades=Sum("total_trades"),
            mcap=Sum("market_capitalization"),
            scrips=Count("symbol"),
        )
    }
    # Breadth needs a row-level comparison, so it is counted separately rather
    # than folded into the aggregate above.
    breadth = {}
    for r in qs.values("business_date", "close_price", "previous_close"):
        b = breadth.setdefault(r["business_date"], [0, 0, 0])
        prev, close = _f(r["previous_close"]), _f(r["close_price"])
        if close > prev:
            b[0] += 1
        elif close < prev:
            b[1] += 1
        else:
            b[2] += 1

    # Exchange-published totals for the same window. Preferred over summing the
    # per-stock column, which is 0 on the newest session(s).
    from core_analysis.models import NepseMarketCapDaily

    official = {
        r["business_date"]: r
        for r in NepseMarketCapDaily.objects.filter(
            business_date__gte=start, business_date__lte=end
        ).values("business_date", "market_capitalization", "float_market_capitalization")
    }

    rows = []
    for d in sorted(totals, reverse=True):
        t, b = totals[d], breadth.get(d, [0, 0, 0])
        o = official.get(d) or {}
        # A zero here means the feed sent nothing, not that the market is
        # worth nothing. None renders as "—", which is the truthful cell.
        summed = round(_f(t["mcap"]), 2) or None
        rows.append({
            "date": d.isoformat(),
            "scrips": int(t["scrips"] or 0),
            "advancing": b[0],
            "declining": b[1],
            "unchanged": b[2],
            "qty": int(t["qty"] or 0),
            "turnover": round(_f(t["turnover"]), 2),
            "trades": int(t["trades"] or 0),
            "mcap": round(_f(o["market_capitalization"]), 2)
                    if o.get("market_capitalization") else summed,
            "float_mcap": round(_f(o["float_market_capitalization"]), 2)
                          if o.get("float_market_capitalization") is not None else None,
        })
    return {"rows": rows, "meta": {"from": str(start), "to": str(end), "count": len(rows)}}


def _shares_outstanding(as_of):
    """{symbol: shares} implied by the newest session that reported market cap.

    ``market_capitalization`` is stored in millions, so ``mcap / close`` yields
    shares in millions — and it is stable (NABIL resolves to 271 on every
    reported session), which is what makes it safe to carry forward.

    This exists because the feed returns 0 for the whole column on the most
    recent session(s). Repricing the last known share count against today's
    close reconstructs the figure from price action instead of showing zeros.
    """
    from core_analysis.models import NepseDailyStockPrice as P

    src = latest_mcap_date()
    if not src:
        return {}, None
    key = f"ndr:shares:{src}"
    hit = cache.get(key)
    if hit is not None:
        return hit, src
    out = {}
    for r in P.objects.filter(business_date=src).exclude(
        market_capitalization=0
    ).values("symbol", "close_price", "market_capitalization"):
        close = _f(r["close_price"])
        if close > 0:
            out[r["symbol"]] = _f(r["market_capitalization"]) / close
    cache.set(key, out, _TTL)
    return out, src


def _market_cap(p):
    """Market capitalisation by date — total, and the largest constituents.

    Where the feed reports 0 the figure is DERIVED as ``close x shares``, with
    shares carried from the last session that did report it. Derived rows are
    flagged so the table never presents an estimate as a published number.
    """
    from core_analysis.models import NepseDailyStockPrice as P

    latest = latest_price_date()
    day = p.get("date") or latest
    if not day:
        return {"rows": [], "meta": {}}
    qs = P.objects.filter(business_date=day).values(
        "symbol", "security_name", "close_price", "total_traded_quantity",
        "market_capitalization",
    )

    shares, shares_src = _shares_outstanding(day)
    total = 0.0
    tmp = []
    derived = 0
    for r in qs:
        mc = _f(r["market_capitalization"])
        r["_derived"] = False
        if mc <= 0:
            # Reprice the last known share count against this session's close.
            sh = shares.get(r["symbol"])
            close = _f(r["close_price"])
            if sh and close > 0:
                mc = sh * close
                r["_derived"] = True
                derived += 1
        total += mc
        tmp.append((mc, r))
    tmp.sort(key=lambda x: x[0], reverse=True)
    rows = [{
        "rank": i,
        "symbol": r["symbol"],
        "name": r["security_name"],
        "close": _f(r["close_price"]),
        "mcap": round(mc, 2) if mc else None,
        "pct_market": round(100.0 * mc / total, 2) if total and mc else None,
        "basis": "derived" if r["_derived"] else ("reported" if mc else "—"),
    } for i, (mc, r) in enumerate(tmp, 1)]
    meta = {"date": str(day), "count": len(rows), "total_mcap": round(total, 2),
            "derived": derived}

    # The exchange's own published total for this session, which exists even when
    # the per-stock column does not. Shown alongside so the page always has an
    # authoritative headline figure to check the derived rows against.
    official = official_mcap(day)
    if official:
        meta["official"] = official
    if derived:
        meta["warning"] = (
            f"The feed reported no per-scrip market capitalisation for {day}, so "
            f"{derived} of {len(rows)} rows are computed as close x shares, with "
            f"shares taken from {shares_src} (the last session that reported them). "
            "The Basis column marks which. A bonus or rights issue since then would "
            "make those rows understate the true figure."
        )
    elif not total:
        meta["warning"] = (
            f"No market capitalisation available for {day}, and no earlier session "
            "to derive share counts from."
        )
    return {"rows": rows, "meta": meta}


def official_mcap(day):
    """Exchange-published market-cap totals for one session, or None.

    Sourced from ``nepse_market_cap_daily`` (its own upstream endpoint) rather
    than summed from the per-stock column, which is intermittently zero.
    """
    from core_analysis.models import NepseMarketCapDaily

    r = NepseMarketCapDaily.objects.filter(business_date=day).values(
        "market_capitalization", "sensitive_market_capitalization",
        "float_market_capitalization", "total_turnover",
        "total_traded_shares", "total_transactions", "total_scrips_traded",
    ).first()
    if not r:
        return None
    return {k: (_f(v) if v is not None else None) for k, v in r.items()}


def _n_day_average(p):
    """N-day average traded price / volume per scrip.

    Averages the session VWAPs over the window (an unweighted mean of daily
    averages, which is what the exchange's own N-day sheet reports) and shows
    the traded-day count beside it, so a scrip that traded 3 of 30 days is not
    mistaken for one that traded every day.
    """
    from core_analysis.models import NepseDailyStockPrice as P

    try:
        n = max(1, min(365, int(p.get("n") or 30)))
    except (TypeError, ValueError):
        n = 30
    end = p.get("end") or latest_price_date()
    if not end:
        return {"rows": [], "meta": {}}
    from datetime import date as _d
    end_d = end if not isinstance(end, str) else _d.fromisoformat(end)

    # Resolve N *trading sessions*, not N calendar days — a 30-calendar-day
    # window silently becomes ~21 sessions and the average would not match the
    # exchange's.
    sessions = list(
        P.objects.filter(business_date__lte=end_d)
        .order_by("-business_date")
        .values_list("business_date", flat=True)
        .distinct()[:n]
    )
    if not sessions:
        return {"rows": [], "meta": {}}
    start_d = min(sessions)

    qs = (
        P.objects.filter(business_date__gte=start_d, business_date__lte=end_d)
        .values("symbol", "security_name")
        .annotate(
            avg_price=Avg("average_traded_price"),
            avg_close=Avg("close_price"),
            total_qty=Sum("total_traded_quantity"),
            total_turnover=Sum("total_traded_value"),
            days=Count("business_date"),
            hi=Max("high_price"),
            lo=Min("low_price"),
        )
    )
    rows = [{
        "symbol": r["symbol"],
        "name": r["security_name"],
        "avg_price": round(_f(r["avg_price"]), 2),
        "avg_close": round(_f(r["avg_close"]), 2),
        "high": _f(r["hi"]),
        "low": _f(r["lo"]),
        "avg_qty": int((r["total_qty"] or 0) / (r["days"] or 1)),
        "total_qty": int(r["total_qty"] or 0),
        "turnover": round(_f(r["total_turnover"]), 2),
        "days": int(r["days"] or 0),
    } for r in qs]
    rows.sort(key=lambda r: r["turnover"], reverse=True)
    return {
        "rows": rows,
        "meta": {"n": n, "sessions": len(sessions), "from": start_d.isoformat(),
                 "to": end_d.isoformat(), "count": len(rows)},
    }


def _floor_sheet(p):
    """Raw executed trades across a date range, filtered by scrip / broker.

    Paged on the SERVER, unlike the other reports. They are a few hundred rows and
    ship whole; this table holds 59.7M rows across 811 sessions, so the browser
    can never hold the result set and the pager has to fetch one page at a time.

    Ordering cost is what shapes the guard below. ``business_date`` is indexed, so
    counting is cheap (a day in 0.01s), but ORDER BY trade_time over a wide range
    sorts millions of rows: a month-wide unfiltered page costs ~3.3s. A symbol
    filter is indexed and collapses that to ~0.05s even across a year — which is
    why a wide window is allowed once a scrip or broker is named, and refused
    while it is not.
    """
    from core_analysis.models import NepseFloorsheet as FSH

    # An exact COUNT(*) is the only expensive part of a wide window (0.7s for a
    # month, 7.7s for a year) — the page itself is 0.01s. So the count is only
    # taken when it is cheap; beyond that the pager probes for a next page
    # instead, which keeps the entire 59.7M-row history browsable.
    COUNT_MAX_DAYS = 31
    latest = latest_floorsheet_date()

    end = p.get("end") or p.get("date") or latest
    start = p.get("start") or p.get("date") or end
    if not end:
        return {"rows": [], "meta": {}}
    from datetime import date as _d
    s = start if not isinstance(start, str) else _d.fromisoformat(start)
    e = end if not isinstance(end, str) else _d.fromisoformat(end)
    if s > e:
        s, e = e, s
    span = (e - s).days + 1
    single_day = s == e

    sym = (p.get("symbol") or "").strip().upper()
    broker = (p.get("broker") or "").strip()

    base = FSH.objects.filter(business_date__gte=s, business_date__lte=e)
    if sym:
        base = base.filter(stock_symbol=sym)

    # ``buyer=X OR seller=X`` cannot use either index once an ORDER BY is added:
    # measured at 60s for one broker's history, against 0.11s for a single side.
    # So the two sides are queried separately and merged. Each side is limited to
    # offset+size, which provably contains the true top offset+size of the union.
    split_broker = bool(broker) and not single_day
    qs = base if split_broker else (
        base.filter(Q(buyer=broker) | Q(seller=broker)) if broker else base
    )

    # Ordering is what makes this fast or unusable. (stock_symbol, business_date)
    # is indexed, so "-business_date, -id" is a backward index scan — 0.01s even
    # over 17M rows. Sorting by trade_time or quantity instead forces a filesort
    # of the whole match set: 21s for one symbol's history, 37s with trade_time.
    #
    # So the intraday sorts are offered only for a SINGLE session, where the set
    # is ~45k rows and either costs ~0.15s. Across multiple days the order is by
    # date; ``id`` is the tiebreaker purely because it is in the index — it does
    # NOT track trade time (measured ~53% correlation, i.e. none), which is why
    # the response says so rather than implying chronological order.
    order = (p.get("order") or "time").strip()
    if single_day:
        order_by = ("-quantity", "-id") if order == "quantity" else ("-trade_time", "-contract_no")
        order_label = "largest" if order == "quantity" else "latest"
    else:
        order_by = ("-business_date", "-id")
        order_label = "date"

    # "All" returns the entire filtered set in one response. Bounded because the
    # browser has to build a DOM row for each: a full session (~45k) is fine,
    # a year (17M) would hang the tab. Beyond the bound the request is refused
    # with a reason rather than silently truncated.
    ALL_MAX = 60_000
    want_all = str(p.get("page_size") or "").strip().lower() == "all"
    if want_all:
        page_size = ALL_MAX
    else:
        try:
            page_size = max(25, min(2000, int(p.get("page_size") or 500)))
        except (TypeError, ValueError):
            page_size = 500
    try:
        # Hard ceiling: an uncounted wide broker window would otherwise turn
        # ?page=999999999 into a deep OFFSET scan of a 60M-row table.
        page = max(1, min(int(p.get("page") or 1), 5000))
    except (TypeError, ValueError):
        page = 1

    # A symbol narrows enough to count cheaply (555k rows in 0.44s). A broker does
    # not — the buyer/seller indexes are far less selective, and counting one
    # broker's history measured 60s — so a wide broker window is left uncounted
    # and the pager probes for a next page instead.
    # "All" must know the size before building it, so it always counts.
    countable = want_all or span <= COUNT_MAX_DAYS or bool(sym)
    if countable:
        if split_broker:
            # Union size: a self-cross (broker on both sides) is one row.
            total = (qs.filter(buyer=broker).count()
                     + qs.filter(seller=broker).count()
                     - qs.filter(buyer=broker, seller=broker).count())
        else:
            total = qs.count()
        pages = max(1, (total + page_size - 1) // page_size)
        page = min(page, pages)
    else:
        total = pages = None

    if want_all:
        if total and total > ALL_MAX:
            return {"rows": [], "meta": {
                "from": s.isoformat(), "to": e.isoformat(), "span": span,
                "count": 0, "total": total, "page": 1, "pages": 1,
                "page_size": page_size, "server_paged": True, "order": order_label,
                "single_day": single_day, "has_next": False, "blocked": True,
                "warning": (
                    f"{total:,} rows is too many to draw in one table "
                    f"(the limit is {ALL_MAX:,}). Narrow the dates, pick a symbol "
                    f"or a broker, or use a page size."
                ),
            }}
        page = 1
    offset = (page - 1) * page_size

    # One extra row tells the pager whether a next page exists without counting
    # the whole range.
    fetch = page_size + 1
    _cols = ("business_date", "contract_no", "stock_symbol", "buyer", "seller",
             "quantity", "rate", "amount", "trade_time")

    if split_broker:
        # Merge the two indexed sides, then take the window. A trade where the
        # same broker is both buyer and seller appears once per side upstream, so
        # rows are de-duplicated on contract number.
        need = offset + fetch
        merged, seen = [], set()
        sort_fields = [f.lstrip("-") for f in order_by]
        for side in ("buyer", "seller"):
            for r in qs.filter(**{side: broker}).order_by(*order_by).values(*_cols, "id")[:need]:
                k = r["contract_no"]
                if k in seen:
                    continue
                seen.add(k)
                merged.append(r)
        # The per-side slice only contains the true top-N of the union if the
        # merge is sorted by the SAME key the sides were fetched with.
        def _mk(r):
            return tuple((r.get(f) is not None, r.get(f)) for f in sort_fields)
        merged.sort(key=_mk, reverse=True)
        page_rows = merged[offset:offset + fetch]
        for r in page_rows:
            r.pop("id", None)
    else:
        page_rows = list(qs.order_by(*order_by).values(*_cols)[offset:offset + fetch])

    rows = [{
        # Continuous across pages, so page 3 starts at 1001 rather than restarting.
        "sn": offset + i,
        "date": r["business_date"].isoformat(),
        "contract": r["contract_no"],
        "symbol": r["stock_symbol"],
        "buyer": r["buyer"],
        "seller": r["seller"],
        "qty": int(r["quantity"] or 0),
        "rate": _f(r["rate"]),
        "amount": _f(r["amount"]),
        "time": r["trade_time"].strftime("%H:%M:%S") if r["trade_time"] else "",
    } for i, r in enumerate(page_rows, 1)]

    has_next = len(rows) > page_size
    rows = rows[:page_size]

    meta = {
        "from": s.isoformat(), "to": e.isoformat(), "span": span,
        "count": len(rows), "total": total,
        "page": page, "pages": pages, "page_size": page_size,
        "has_next": has_next, "server_paged": True, "order": order_label,
        "single_day": single_day,
    }
    if not single_day:
        meta["warning"] = (
            f"{span} days: rows are ordered newest date first. Trade-time order and "
            "the largest-trades view apply within a single session — set From and To "
            "to the same date for those."
        )
    return {"rows": rows, "meta": meta}


def _margin_trade(p):
    """Securities eligible for margin lending, from the maintained local list."""
    from core_analysis.models import MarginEligibleCompany as M

    qs = M.objects.all()
    if (p.get("all") or "") not in ("1", "true", "yes"):
        qs = qs.filter(is_eligible=True)
    rows = [{
        "symbol": r["symbol"],
        "name": r["company_name"],
        "sector": r["sector"],
        "eligible": "Yes" if r["is_eligible"] else "No",
        "margin_rate": _f(r["margin_rate"]) if r["margin_rate"] is not None else None,
        "risk_category": r["risk_category"],
        "effective_date": r["effective_date"].isoformat() if r["effective_date"] else "",
        "source": r["source"],
    } for r in qs.order_by("symbol").values(
        "symbol", "company_name", "sector", "is_eligible", "margin_rate",
        "risk_category", "effective_date", "source",
    )]
    return {"rows": rows, "meta": {"count": len(rows)}}


# ── registry ───────────────────────────────────────────────────────────────
# slug -> title, blurb, filter controls, columns, builder.
# "controls" names the filter widgets the page renders; the generic template
# and JS read this, so no report needs bespoke markup.

REPORTS = {
    "todays-price": {
        "title": "Today's Price",
        "blurb": "Full end-of-day price sheet for one trading session.",
        "controls": ["date"],
        "builder": _todays_price,
        "columns": [
            {"key": "symbol", "label": "Symbol", "type": "str"},
            {"key": "name", "label": "Company", "type": "str"},
            {"key": "open", "label": "Open", "type": "num"},
            {"key": "high", "label": "High", "type": "num"},
            {"key": "low", "label": "Low", "type": "num"},
            {"key": "close", "label": "Close", "type": "num"},
            {"key": "prev_close", "label": "Prev Close", "type": "num"},
            {"key": "change", "label": "Change", "type": "signed"},
            {"key": "pct_change", "label": "% Change", "type": "pct"},
            {"key": "vwap", "label": "Avg Price", "type": "num"},
            {"key": "qty", "label": "Volume", "type": "int"},
            {"key": "turnover", "label": "Turnover", "type": "rs"},
            {"key": "trades", "label": "Trades", "type": "int"},
            {"key": "mcap", "label": "Market Cap (Rs m)", "type": "rs"},
            {"key": "wk52_high", "label": "52W High", "type": "num"},
            {"key": "wk52_low", "label": "52W Low", "type": "num"},
        ],
    },
    "stock-trading": {
        "title": "Stock Trading",
        "blurb": "Session-by-session trading history for a single scrip.",
        "controls": ["symbol", "daterange"],
        "builder": _stock_trading,
        "columns": [
            {"key": "date", "label": "Date", "type": "date"},
            {"key": "open", "label": "Open", "type": "num"},
            {"key": "high", "label": "High", "type": "num"},
            {"key": "low", "label": "Low", "type": "num"},
            {"key": "close", "label": "Close", "type": "num"},
            {"key": "prev_close", "label": "Prev Close", "type": "num"},
            {"key": "change", "label": "Change", "type": "signed"},
            {"key": "pct_change", "label": "% Change", "type": "pct"},
            {"key": "vwap", "label": "Avg Price", "type": "num"},
            {"key": "qty", "label": "Volume", "type": "int"},
            {"key": "turnover", "label": "Turnover", "type": "rs"},
            {"key": "trades", "label": "Trades", "type": "int"},
            {"key": "mcap", "label": "Market Cap (Rs m)", "type": "rs"},
        ],
    },
    "indices": {
        "title": "Datewise Indices",
        "blurb": "Every index and sub-index for a session — or one index across dates.",
        "controls": ["date", "index", "daterange"],
        "builder": _datewise_indices,
        "columns": [
            {"key": "date", "label": "Date", "type": "date"},
            {"key": "index", "label": "Index", "type": "str"},
            {"key": "open", "label": "Open", "type": "num"},
            {"key": "high", "label": "High", "type": "num"},
            {"key": "low", "label": "Low", "type": "num"},
            {"key": "close", "label": "Close", "type": "num"},
            {"key": "change", "label": "Change", "type": "signed"},
            {"key": "pct_change", "label": "% Change", "type": "pct"},
            {"key": "turnover", "label": "Turnover", "type": "rs"},
            {"key": "qty", "label": "Volume", "type": "int"},
            {"key": "trades", "label": "Trades", "type": "int"},
        ],
    },
    "sector-summary": {
        "title": "Sectorwise Summary",
        "blurb": "Which sectors moved the market — rupee impact, index change, "
                 "breadth and turnover, ranked by impact.",
        "controls": ["date"],
        "builder": _sector_summary,
        "columns": [
            {"key": "sector", "label": "Sector", "type": "str"},
            {"key": "impact", "label": "Impact (Rs m)", "type": "signed"},
            {"key": "impact_pct", "label": "Cap Change %", "type": "pct"},
            {"key": "share_of_move", "label": "% of Move", "type": "pct"},
            {"key": "index_close", "label": "Index", "type": "num"},
            {"key": "index_pct", "label": "Index %", "type": "pct"},
            {"key": "mcap", "label": "Market Cap (Rs m)", "type": "rs"},
            {"key": "pct_mcap", "label": "% of Cap", "type": "pct"},
            {"key": "advancing", "label": "Adv", "type": "int"},
            {"key": "declining", "label": "Dec", "type": "int"},
            {"key": "unchanged", "label": "Unch", "type": "int"},
            {"key": "scrips", "label": "Scrips", "type": "int"},
            {"key": "turnover", "label": "Turnover", "type": "rs"},
            {"key": "pct_turnover", "label": "% of Turnover", "type": "pct"},
            {"key": "trades", "label": "Trades", "type": "int"},
        ],
    },
    "market-summary": {
        "title": "Market Summary",
        "blurb": "Whole-market totals per session — turnover, volume, trades and breadth.",
        "controls": ["daterange"],
        "builder": _market_summary,
        "columns": [
            {"key": "date", "label": "Date", "type": "date"},
            {"key": "scrips", "label": "Scrips Traded", "type": "int"},
            {"key": "advancing", "label": "Advancing", "type": "int"},
            {"key": "declining", "label": "Declining", "type": "int"},
            {"key": "unchanged", "label": "Unchanged", "type": "int"},
            {"key": "qty", "label": "Volume", "type": "int"},
            {"key": "turnover", "label": "Turnover", "type": "rs"},
            {"key": "trades", "label": "Trades", "type": "int"},
            {"key": "mcap", "label": "Market Cap (Rs m)", "type": "rs"},
            {"key": "float_mcap", "label": "Float Market Cap", "type": "rs"},
        ],
    },
    "market-cap": {
        "title": "Market Capitalization By Date",
        "blurb": "Every listed scrip's market cap for one session, ranked by size.",
        "controls": ["date"],
        "builder": _market_cap,
        "columns": [
            {"key": "rank", "label": "#", "type": "int"},
            {"key": "symbol", "label": "Symbol", "type": "str"},
            {"key": "name", "label": "Company", "type": "str"},
            {"key": "close", "label": "Close", "type": "num"},
            {"key": "mcap", "label": "Market Cap (Rs m)", "type": "rs"},
            {"key": "pct_market", "label": "% of Market", "type": "pct"},
            {"key": "basis", "label": "Basis", "type": "str"},
        ],
    },
    "n-day-average": {
        "title": "N Day's Trading Average Price",
        "blurb": "Average traded price and volume per scrip over the last N sessions.",
        "controls": ["ndays", "date"],
        "builder": _n_day_average,
        "columns": [
            {"key": "symbol", "label": "Symbol", "type": "str"},
            {"key": "name", "label": "Company", "type": "str"},
            {"key": "avg_price", "label": "Avg Traded Price", "type": "num"},
            {"key": "avg_close", "label": "Avg Close", "type": "num"},
            {"key": "high", "label": "Period High", "type": "num"},
            {"key": "low", "label": "Period Low", "type": "num"},
            {"key": "avg_qty", "label": "Avg Volume/Day", "type": "int"},
            {"key": "total_qty", "label": "Total Volume", "type": "int"},
            {"key": "turnover", "label": "Turnover", "type": "rs"},
            {"key": "days", "label": "Days Traded", "type": "int"},
        ],
    },
    "floor-sheet": {
        "title": "Today's Floor Sheet",
        "blurb": "Every executed trade, with buying and selling broker. "
                 "Pick a symbol or broker to browse the full history.",
        "controls": ["daterange", "symbol", "broker", "order"],
        "builder": _floor_sheet,
        "columns": [
            {"key": "sn", "label": "SN", "type": "int"},
            {"key": "date", "label": "Date", "type": "date"},
            {"key": "contract", "label": "Contract No", "type": "str"},
            {"key": "symbol", "label": "Symbol", "type": "str"},
            {"key": "buyer", "label": "Buyer", "type": "str"},
            {"key": "seller", "label": "Seller", "type": "str"},
            {"key": "qty", "label": "Quantity", "type": "int"},
            {"key": "rate", "label": "Rate", "type": "num"},
            {"key": "amount", "label": "Amount", "type": "rs"},
            {"key": "time", "label": "Time", "type": "str"},
        ],
    },
    "margin-trade": {
        "title": "Margin Trade",
        "blurb": "Securities eligible for margin lending.",
        "controls": ["margin_all"],
        "builder": _margin_trade,
        "columns": [
            {"key": "symbol", "label": "Symbol", "type": "str"},
            {"key": "name", "label": "Company", "type": "str"},
            {"key": "sector", "label": "Sector", "type": "str"},
            {"key": "eligible", "label": "Eligible", "type": "str"},
            {"key": "margin_rate", "label": "Margin Rate %", "type": "pct"},
            {"key": "risk_category", "label": "Risk Category", "type": "str"},
            {"key": "effective_date", "label": "Effective", "type": "date"},
            {"key": "source", "label": "Source", "type": "str"},
        ],
    },
}

# Menu order mirrors the exchange's own navigation, so anyone used to
# nepalstock.com finds the same items in the same sequence.
REPORT_ORDER = [
    "floor-sheet", "indices", "stock-trading", "market-cap", "n-day-average",
    "todays-price", "sector-summary", "market-summary", "margin-trade",
]


def menu():
    """[(slug, title)] in exchange order — drives the nav dropdown."""
    return [(s, REPORTS[s]["title"]) for s in REPORT_ORDER if s in REPORTS]


def build(slug, params):
    """Run one report. Returns {rows, meta, columns} or None for a bad slug."""
    spec = REPORTS.get(slug)
    if not spec:
        return None
    # Every report is a fresh scan of one or more sessions; the same
    # slug+params served twice within the TTL should not scan twice.
    ck = "nd_report:v1:" + hashlib.md5(
        (slug + "|" + repr(sorted((params or {}).items()))).encode()).hexdigest()
    out = cache.get(ck)
    if out is None:
        out = spec["builder"](params or {})
        cache.set(ck, out, _TTL)
    out = dict(out)
    out["columns"] = spec["columns"]
    out["title"] = spec["title"]
    return out


def index_names():
    """Distinct index / sub-index names, for the Datewise Indices picker."""
    from core_analysis.models import NepseMarketIndex as I

    key = "ndr:indexnames"
    hit = cache.get(key)
    if hit is not None:
        return hit
    out = sorted(
        I.objects.values_list("sector_name", flat=True).distinct()
    )
    cache.set(key, out, _TTL)
    return out
