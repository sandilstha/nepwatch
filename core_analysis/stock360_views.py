"""Stock 360 — all-in-one single-stock dashboard.

One symbol in, every desk's read on it out. The view renders the shell with the
quote, performance and support/resistance computed server-side; fundamentals and
floorsheet hydrate on the client from their existing JSON endpoints.

Design rules this file enforces:
  * One data build. The adjusted OHLC dataframe (the same
    ``_build_standard_dataframe`` the workbench uses) is built once per request
    and feeds BOTH the performance block and the S&R engine — so returns, the
    chart and the level engine can never disagree about the price series.
  * Adjusted vs raw is explicit. Returns / volatility / chart come from the
    ADJUSTED series (bonus/split-proof); the hero quote (last close, day change,
    volume, market cap, 52-week range) is the RAW exchange print.
  * Cached. The computed payload is cached per (symbol, latest EOD date) so the
    pandas work runs once per symbol per session, not on every page view.
  * Validated. Symbols must match the same pattern the broker desk accepts —
    junk input never reaches the dataframe builder.
"""

import logging
import math
import re
from datetime import date, timedelta

from django.core.cache import cache
from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET, require_POST

from .models import NepseDailyStockPrice, NepseMarketIndex
from .insights_views import _asset_version

logger = logging.getLogger(__name__)

_SYMBOL_RE = re.compile(r"^[A-Z0-9._-]{1,50}$")

# Approximate NEPSE trading-session counts per lookback window (~5 sessions/week
# with frequent holidays — deliberately conservative).
_WINDOWS = (("1W", 5), ("1M", 22), ("3M", 66), ("1Y", 246))

# Symbol Stock 360 / the valuation workspace open on when none is requested.
# Previously they fell through to "whatever sorted first on the latest business
# date", which landed on an arbitrary scrip with no fundamentals synced.
DEFAULT_SYMBOL = "GBIME"

_PAGE_CACHE_TTL = 30 * 60        # perf + S&R payload
_AI_CACHE_TTL = 6 * 60 * 60      # Gemini narrative — one spend per symbol/day-ish


def _f(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _rs_h(v):
    """Humanise a rupee amount using NEPSE conventions (Arba / Crore / Lakh)."""
    v = _f(v)
    a = abs(v)
    if a >= 1e9:
        return f"{v / 1e9:.2f} Ar"
    if a >= 1e7:
        return f"{v / 1e7:.2f} Cr"
    if a >= 1e5:
        return f"{v / 1e5:.2f} L"
    return f"{v:,.0f}"


def _valid_symbol(raw):
    sym = (raw or "").strip().upper()
    return sym if _SYMBOL_RE.match(sym) else ""


def _default_symbol():
    """DEFAULT_SYMBOL, or the latest-traded name if it has no local history."""
    if NepseDailyStockPrice.objects.filter(symbol=DEFAULT_SYMBOL).exists():
        return DEFAULT_SYMBOL
    return (
        NepseDailyStockPrice.objects.order_by("-business_date")
        .values_list("symbol", flat=True)
        .first()
    ) or "NABIL"


def _quote(symbol):
    """Latest raw exchange print for the hero. None = symbol has no history."""
    latest = (
        NepseDailyStockPrice.objects.filter(symbol=symbol)
        .order_by("-business_date")
        .values(
            "business_date", "close_price", "previous_close",
            "average_traded_price", "total_traded_quantity", "total_trades",
            "market_capitalization", "fifty_two_week_high", "fifty_two_week_low",
            "security_name",
        )
        .first()
    )
    if not latest:
        return None

    close = _f(latest["close_price"])
    prev = _f(latest["previous_close"])
    hi52 = _f(latest["fifty_two_week_high"])
    lo52 = _f(latest["fifty_two_week_low"])
    pos52 = None
    if hi52 and lo52 and hi52 > lo52:
        pos52 = round(max(0.0, min(1.0, (close - lo52) / (hi52 - lo52))) * 100.0, 1)

    day_pct = _pct(close, prev)
    cap_m, cap_note = _market_cap_m(symbol, latest["market_capitalization"], close)
    return {
        "as_of": latest["business_date"].isoformat() if latest["business_date"] else None,
        "security_name": latest["security_name"] or symbol,
        "close": close,
        "day_change": round(close - prev, 2) if (close and prev) else None,
        "day_change_pct": day_pct,
        "avg_price": _f(latest["average_traded_price"]),
        "volume_h": f"{int(latest['total_traded_quantity'] or 0):,}",
        "trades_h": f"{int(latest['total_trades'] or 0):,}",
        # market_capitalization is stored in MILLIONS of Rs (verified against
        # NABIL's ~270M share float), so scale to rupees before humanising.
        # Both figures come from _market_cap_m, which falls back to the last
        # reported session when the feed is sending zeros.
        "market_cap_h": _rs_h((cap_m or 0.0) * 1e6),
        "market_cap_m": cap_m,
        "market_cap_note": cap_note,
        "high_52w": hi52 or None,
        "low_52w": lo52 or None,
        "pos_52w": pos52,
    }


def _market_cap_m(symbol, latest_value, close):
    """Market cap in MILLIONS of Rs, with a fallback for the feed's zero runs.

    ``market_capitalization`` arrives as 0.00 for stretches (every row of the
    latest sessions can be zero while older rows carry real figures), which
    rendered the overview card as a dash. When the current row is empty we take
    the most recent session that actually reported one and rebase it on today's
    price:

        shares  = last_cap / last_close          (implied, both from that row)
        cap_now = shares * close = last_cap * close / last_close

    That is exact unless the share count changed in between — a bonus issue or
    right share in those few days would overstate it — so the basis travels with
    the number and the card labels it as derived rather than reported.
    """
    value = _f(latest_value)
    if value > 0:
        return round(value, 2), None

    prior = (
        NepseDailyStockPrice.objects.filter(symbol=symbol, market_capitalization__gt=0)
        .order_by("-business_date")
        .values("business_date", "market_capitalization", "close_price")
        .first()
    )
    if not prior:
        return None, None

    prior_cap = _f(prior["market_capitalization"])
    prior_close = _f(prior["close_price"])
    as_of = prior["business_date"].isoformat() if prior["business_date"] else "an earlier session"

    if close and prior_close:
        return (
            round(prior_cap * close / prior_close, 2),
            f"Derived: the exchange has reported no market cap since {as_of}. "
            f"Rebased from that session's {prior_cap:,.0f}M on the price move since. "
            "Assumes the share count is unchanged.",
        )
    return round(prior_cap, 2), f"As last reported on {as_of}; the feed has sent none since."


def _pct(now, then):
    if now is None or then in (None, 0):
        return None
    return round((now / then - 1.0) * 100.0, 2)


def _adjusted_df(symbol, end_iso):
    """Five years of adjusted OHLC (unadjusted fallback), oldest→newest.

    Five years so the market card's beta and the multi-timeframe chart (up to a
    5Y toggle) have the history they need; the shorter perf windows and the S&R
    engine simply index the tail, so the wider window costs nothing but the one
    (cached) build.
    """
    from .views import _build_standard_dataframe

    try:
        end_d = date.fromisoformat(end_iso)
    except (TypeError, ValueError):
        end_d = date.today()
    start = (end_d - timedelta(days=5 * 365 + 5)).isoformat()
    df = _build_standard_dataframe(symbol, start, end_d.isoformat(), use_unadjusted_fallback=True)
    return df if df is not None and not df.empty else None


def _performance(df):
    """Window returns and annualised volatility — ADJUSTED closes."""
    # (date, close) pairs, oldest→newest, prices coerced and zero-filtered.
    # business_date may be date, datetime or string depending on the source
    # branch inside _build_standard_dataframe, so year extraction stays duck-typed.
    pairs = []
    for d, c in zip(df["business_date"].tolist(), df["close_price_adj"].tolist()):
        v = _f(c)
        if v > 0:
            pairs.append((d, v))
    if len(pairs) < 2:
        return None
    closes = [p[1] for p in pairs]

    last = closes[-1]
    returns = {"1D": _pct(last, closes[-2])}
    for label, n in _WINDOWS:
        returns[label] = _pct(last, closes[-1 - n]) if len(closes) > n else None

    # Calendar YTD: first adjusted close of the latest year in the series.
    def _year(d):
        try:
            return int(str(d)[:4])
        except (TypeError, ValueError):
            return None

    this_year = _year(pairs[-1][0])
    ytd_base = next(
        (v for d, v in pairs if this_year is not None and _year(d) == this_year),
        None,
    )
    returns["YTD"] = _pct(last, ytd_base)

    daily = [closes[i] / closes[i - 1] - 1.0 for i in range(1, len(closes))][-252:]
    vol = None
    if len(daily) >= 20:
        mean = sum(daily) / len(daily)
        var = sum((d - mean) ** 2 for d in daily) / (len(daily) - 1)
        vol = round(math.sqrt(var) * math.sqrt(252) * 100.0, 1)

    return {"returns": returns, "vol_annual": vol}


def _avg60(df):
    """Mean of the last 60 adjusted closes (the photo's '60 Days Average')."""
    closes = [v for v in (_f(c) for c in df["close_price_adj"].tolist()) if v > 0]
    if len(closes) < 5:
        return None
    tail = closes[-60:]
    return round(sum(tail) / len(tail), 2)


def _beta5y(df, end_iso):
    """5-year beta of weekly returns vs the NEPSE Index.

    Weekly (not daily) to match how the platform's portfolio desk measures beta
    and to blunt NEPSE's frequent flat/one-print days. Returns None whenever the
    index history or overlap is too thin to be meaningful.
    """
    try:
        end_d = date.fromisoformat(end_iso)
    except (TypeError, ValueError):
        end_d = date.today()
    start = end_d - timedelta(days=5 * 365 + 5)

    idx_rows = (
        NepseMarketIndex.objects.filter(
            sector_name__iexact="NEPSE Index", business_date__range=(start, end_d)
        )
        .order_by("business_date")
        .values_list("business_date", "close_index")
    )

    def _weekly(pairs):
        """Last close of each ISO week → {(iso_year, iso_week): close}."""
        wk = {}
        for d, c in pairs:
            v = _f(c)
            if v <= 0:
                continue
            dd = d if hasattr(d, "isocalendar") else date.fromisoformat(str(d)[:10])
            iso = dd.isocalendar()
            wk[(iso[0], iso[1])] = v  # later rows overwrite → last close of week
        return wk

    stock_wk = _weekly(zip(df["business_date"].tolist(), df["close_price_adj"].tolist()))
    index_wk = _weekly(idx_rows)
    weeks = sorted(set(stock_wk) & set(index_wk))
    if len(weeks) < 30:
        return None

    sr, mr = [], []
    for i in range(1, len(weeks)):
        p0, p1 = stock_wk[weeks[i - 1]], stock_wk[weeks[i]]
        m0, m1 = index_wk[weeks[i - 1]], index_wk[weeks[i]]
        if p0 and m0:
            sr.append(p1 / p0 - 1.0)
            mr.append(m1 / m0 - 1.0)
    if len(mr) < 20:
        return None

    mbar = sum(mr) / len(mr)
    sbar = sum(sr) / len(sr)
    var_m = sum((m - mbar) ** 2 for m in mr) / (len(mr) - 1)
    if var_m == 0:
        return None
    cov = sum((sr[i] - sbar) * (mr[i] - mbar) for i in range(len(mr))) / (len(mr) - 1)
    return round(cov / var_m, 2)


def _market360(df, end_iso):
    """Price-derived cards for the redesigned overview.

    Only the fields sourceable from the price + index tables live here (60-day
    average, 5-year beta). The fundamental ratios — P/E, P/B, dividend yield,
    ROE/ROA, margins, growth — are hydrated on the client from the fundamentals
    endpoint it already calls, so nothing is duplicated or invented server-side.
    """
    if df is None:
        return {"avg_60d": None, "beta_5y": None}
    return {
        "avg_60d": _avg60(df),
        "beta_5y": _beta5y(df, end_iso),
    }


def _page_payload(symbol):
    """Quote + performance, cached per (symbol, latest EOD date).

    The adjusted dataframe is built once and drives both the returns block and
    the sparkline. The AI-narrative endpoint recomputes S&R independently on
    demand, so no support/resistance work happens on a page view.
    """
    quote = _quote(symbol)
    if not quote:
        return None

    # v7: market cap now falls back to the last reported session, so cached
    # v6 payloads still carry the empty value the feed was sending.
    key = f"stock360:v7:{symbol}:{quote['as_of']}"
    payload = cache.get(key)
    if payload is not None:
        return payload

    df = _adjusted_df(symbol, quote["as_of"])
    perf = _performance(df) if df is not None else None

    from .services.stock360_analytics import technicals

    payload = {
        "quote": quote,
        "perf": perf,
        "market": _market360(df, quote["as_of"]),
        "tech": technicals(df) if df is not None else None,
    }
    cache.set(key, payload, _PAGE_CACHE_TTL)
    return payload


def _freshness(as_of):
    """How current this symbol's price data is, as a chip the page can print.

    Two different staleness questions get one honest answer: is the platform's
    EOD store up to date at all, and has THIS symbol traded since. A stock that
    simply did not trade is not a broken pipeline, and the chip says which.
    """
    market_latest = (
        NepseDailyStockPrice.objects.order_by("-business_date")
        .values_list("business_date", flat=True)
        .first()
    )
    if not market_latest or not as_of:
        return {"tone": "neu", "label": "Freshness unknown",
                "hint": "No EOD date on file to compare against."}

    market_iso = market_latest.isoformat()
    lag_days = (market_latest - date.fromisoformat(as_of)).days
    stale_market = (date.today() - market_latest).days

    if lag_days <= 0:
        tone, label = "pos", f"Current · {market_iso}"
        hint = "This symbol's latest print is the newest session in the database."
    elif lag_days <= 3:
        tone, label = "warn", f"{lag_days} session(s) behind"
        hint = f"Last traded {as_of}; the database holds sessions through {market_iso}."
    else:
        tone, label = "neg", f"Stale · last print {as_of}"
        hint = f"No trades for {lag_days} days. The database itself is current to {market_iso}."

    if stale_market > 3:
        tone = "neg"
        label = f"Database behind · {market_iso}"
        hint = (f"The EOD store has not caught up — newest session on file is {market_iso}, "
                f"{stale_market} days ago. Run the price sync before reading these numbers.")

    return {"tone": tone, "label": label, "hint": hint,
            "as_of": as_of, "market_latest": market_iso}


def _fund_nav(symbol, close=None):
    """NAV block for a mutual fund, or None if this symbol is not one.

    Funds file no quarterly statements, so every ratio card on this page is
    blank for them by construction. What a fund publishes is its NAV, and since
    almost every Nepali scheme is closed-end the number that matters is the gap
    between NAV and the market price — the fund equivalent of P/B.

    WHICH NAV THE DISCOUNT USES. A fund can publish two NAVs: a month-end
    ("monthly") figure and, for many schemes, a far more recent weekly one. The
    discount is computed against the NEWEST available — the weekly where there
    is one — for two reasons: it is the current read, and it is the basis the
    publisher itself uses. Verified against the source on 2026-08-25: KDBY's
    published -9.18% is price 9.99 over its WEEKLY NAV of 11.00, not over its
    monthly 12.26 (which would give -18.52%). Leading with the monthly figure
    overstated the discount by nine points.

    Three figures come back, each labelled, because they answer different
    questions:
      * ``discount_pct``        — OUR latest close against the reference NAV.
        The current read, and the one the panel leads with.
      * ``nav_reference`` / ``nav_reference_label`` — which NAV that was, so the
        number is never unattributable.
      * ``source_discount_pct`` — the publisher's own figure. Kept for
        provenance, so a disagreement is explainable rather than mysterious.
    """
    from .models import CompanyProfile
    from .services import mutual_fund_nav as mfn

    sym = (symbol or "").strip().upper()
    if not CompanyProfile.objects.filter(symbol=sym, sector_name="Mutual Fund").exists():
        return None

    row = mfn.latest_for(sym)
    if row is None:
        # A fund with no NAV synced yet is still a fund — say so, rather than
        # falling through to ratio cards that can never populate.
        return {"available": False, "symbol": sym}

    nav = float(row.nav_monthly) if row.nav_monthly is not None else None
    weekly = float(row.nav_weekly) if row.nav_weekly is not None else None

    # Newest NAV wins. The weekly reading is days old where the monthly is a
    # month-end snapshot, and it is what the publisher divides by.
    if weekly:
        reference, ref_label = weekly, "weekly NAV"
        ref_when = row.nav_weekly_date.isoformat() if row.nav_weekly_date else ""
    else:
        reference, ref_label = nav, "monthly NAV"
        ref_when = row.nav_period

    discount = None
    if reference and close:
        # Negative = trading BELOW NAV (a discount), matching the source's sign.
        discount = round((float(close) - reference) / reference * 100.0, 2)

    return {
        "available": True,
        "symbol": sym,
        "fund_name": row.fund_name,
        "fund_type": mfn.FUND_TYPES.get(row.fund_type, ""),
        "nav": nav,
        "nav_period": row.nav_period,
        "nav_weekly": weekly,
        "nav_weekly_date": row.nav_weekly_date,
        "nav_reference": reference,
        "nav_reference_label": ref_label,
        "nav_reference_when": ref_when,
        # Matured schemes stop reporting; their last reading can be years old
        # and the panel must not present it as current.
        "is_matured": row.fund_type == "1",
        "nav_daily": float(row.nav_daily) if row.nav_daily is not None else None,
        "nav_daily_date": row.nav_daily_date,
        "refund_nav": float(row.refund_nav) if row.refund_nav is not None else None,
        "fund_size": float(row.fund_size) if row.fund_size is not None else None,
        "maturity_date": row.maturity_date,
        "maturity_period": row.maturity_period,
        "discount_pct": discount,
        "discount_tone": "neu" if discount is None else ("pos" if discount >= 0 else "neg"),
        "source_discount_pct": (float(row.premium_discount_pct)
                                if row.premium_discount_pct is not None else None),
        "source_close": float(row.market_close) if row.market_close is not None else None,
        "synced_at": row.synced_at,
    }


@require_GET
def stock360_sop_view(request):
    """Short methodology SOP for Stock 360 — which table feeds which panel.

    Static page, no symbol: it documents the data lineage, not one company.
    Named ``stock360_sop`` to keep it distinct from ``stock360_sop_api``, which
    is a different thing entirely (the SOP Confluence *signal* for one symbol).
    """
    return render(
        request,
        "core_analysis/stock360_sop.html",
        {"asset_version": _asset_version()},
    )


def _bond_groups(qs):
    """Group a BondValuation queryset by issuer, biggest issuers first."""
    from collections import OrderedDict

    groups = OrderedDict()
    for b in qs:
        key = b.issuer_id or "—"
        g = groups.setdefault(key, {
            "symbol": b.issuer_id,
            "name": b.issuer.security_name if b.issuer else "Issuer not resolved",
            "sector": b.sector,
            "rows": [],
            "size": 0.0,
            "notes": set(),
        })
        g["rows"].append(b)
        g["size"] += float(b.issue_size or 0)
        if b.issuer_note:
            g["notes"].add(b.issuer_note)
    ordered = sorted(groups.values(), key=lambda g: -g["size"])
    for g in ordered:
        g["size_bn"] = g["size"] / 1e9
        g["notes"] = sorted(g["notes"])
    return ordered


@require_GET
def bond_desk_view(request):
    """Every listed debenture, grouped by issuing company.

    Filters: ``?issuer=NABIL`` (one company), ``?sector=Finance``,
    ``?call=Undervalued``. Groups are ordered by outstanding size so the big
    bank issuers come first; within a group bonds run nearest-maturity first.
    """
    from .models import BondValuation

    issuer = _valid_symbol(request.GET.get("issuer"))
    sector = (request.GET.get("sector") or "").strip()
    call = (request.GET.get("call") or "").strip()

    qs = BondValuation.objects.select_related("issuer").order_by("maturity_date", "symbol")
    if issuer:
        qs = qs.filter(issuer_id=issuer)
    if sector:
        qs = qs.filter(sector=sector)
    if call:
        qs = qs.filter(valuation=call)

    all_bonds = BondValuation.objects.all()
    as_of = all_bonds.order_by("-valuation_date").values_list("valuation_date", flat=True).first()
    return render(request, "core_analysis/bond_desk.html", {
        "groups": _bond_groups(qs),
        "count": qs.count(),
        "total": all_bonds.count(),
        "as_of": as_of,
        "issuer": issuer or "",
        "sector": sector,
        "call": call,
        "sectors": sorted(set(all_bonds.values_list("sector", flat=True))),
        "calls": ["Undervalued", "Fairly valued", "Overvalued", "Not traded"],
        "asset_version": _asset_version(),
    })


@require_GET
def stock360_view(request, symbol=None):
    """Render Stock 360 for one symbol (falls back to the latest-traded name)."""
    sym = _valid_symbol(symbol or request.GET.get("symbol"))
    if not sym:
        sym = _default_symbol()

    payload = _page_payload(sym) or {}
    context = {
        "symbol": sym,
        "quote": payload.get("quote"),
        "perf": payload.get("perf"),
        "market": payload.get("market") or {},
        "tech": payload.get("tech"),
        "freshness": _freshness((payload.get("quote") or {}).get("as_of")),
        # None for ordinary equities; the NAV panel renders only for funds.
        "fund_nav": _fund_nav(sym, (payload.get("quote") or {}).get("close")),
        "asset_version": _asset_version(),
    }
    return render(request, "core_analysis/stock360.html", context)


@require_GET
def stock360_funda_api(request):
    """Latest-quarter fundamentals for one symbol, sourced from funda.aurasrp.com.np.

    Returns the same ``ks`` / ``trend`` shape the local ``/fundamentals/api/``
    produces, so the Stock 360 overview cards consume it unchanged. Never raises:
    an unreachable source or unknown symbol comes back as ``{"ok": false}`` and
    the client falls back to local fundamentals.
    """
    from .services.funda_financials import latest_keystats, stored_keystats

    sym = _valid_symbol(request.GET.get("symbol"))
    if not sym:
        return JsonResponse({"ok": False, "error": "Invalid symbol."}, status=200)

    # Prefer the snapshot the user synced into our DB; fall back to a live read
    # (cached, not persisted) so cards still populate for un-synced symbols.
    result = stored_keystats(sym) or latest_keystats(sym)
    if not result:
        return JsonResponse({"ok": False, "symbol": sym}, status=200)
    return JsonResponse({**result, "symbol": sym}, status=200)


@require_POST
def stock360_funda_sync(request):
    """Pull one symbol's latest published report from funda.aurasrp.com.np into
    our DB. Staff-only and POST-only: it triggers an outbound fetch and a DB
    write, so it must not be reachable from a GET a third-party page can forge,
    and it matches every other write on the sync dashboard."""
    from .services.funda_financials import sync_symbol

    user = getattr(request, "user", None)
    if not user or not user.is_authenticated or not user.is_staff:
        return JsonResponse(
            {"ok": False, "error": "Staff sign-in required."}, status=403)

    sym = _valid_symbol(request.POST.get("symbol") or request.GET.get("symbol"))
    if not sym:
        return JsonResponse({"ok": False, "error": "Invalid symbol."}, status=200)

    return JsonResponse(sync_symbol(sym), status=200)


@require_GET
def stock360_funda_sector(request):
    """Sector helper for the Workbench sync panel.

    Without ``?sector=``: the distinct sector names in FinancialStatement (the
    dropdown options). With ``?sector=``: the symbols funda covers for it — the
    client then syncs those one at a time with a progress counter, so no single
    long-running request is needed.
    """
    from .models import FinancialStatement
    from .services.funda_financials import sector_symbols

    sector = (request.GET.get("sector") or "").strip()
    if not sector:
        sectors = sorted(
            set(
                FinancialStatement.objects.order_by()
                .values_list("sector", flat=True)
                .distinct()
            )
        )
        return JsonResponse({"ok": True, "sectors": sectors}, status=200)

    symbols = sector_symbols(sector)
    if symbols is None:
        return JsonResponse({"ok": False, "error": "Could not reach the source."}, status=200)
    if not symbols:
        return JsonResponse({"ok": False, "error": f"Source lists no companies for {sector}."}, status=200)

    from .models import CompanyProfile

    # The source's per-sector list drops companies it has filed under a blank
    # sector (SAPIL, 2026-09) even though it serves their statements. Add any
    # active ordinary share our own profile puts in this sector, so it can be
    # picked; a symbol the source truly lacks just fails its own sync with a
    # clear message instead of being invisible here.
    local = set(
        CompanyProfile.objects.filter(sector_name=sector, status__iexact="Active")
        .exclude(security_name__iregex=r"promot|mutual fund|debenture|bond")
        .values_list("symbol", flat=True)
    )
    symbols = sorted(set(symbols) | local)

    # Names come along so the panel's company filter is searchable by company as
    # well as ticker. Falls back to the symbol where no profile row exists.
    names = dict(
        CompanyProfile.objects.filter(symbol__in=symbols)
        .values_list("symbol", "security_name")
    )
    # What we already hold, so a re-run can skip companies instead of re-fetching
    # them, and the panel can show which are done and at which quarter.
    from .models import FundaFundamentalSnapshot

    done = {
        r["symbol"]: {"period": r["period"], "synced_at": r["synced_at"].isoformat()}
        for r in FundaFundamentalSnapshot.objects.filter(symbol__in=symbols).values(
            "symbol", "period", "synced_at"
        )
    }
    companies = [{
        "symbol": s,
        "name": names.get(s) or s,
        "synced": s in done,
        "period": (done.get(s) or {}).get("period"),
        "synced_at": (done.get(s) or {}).get("synced_at"),
    } for s in symbols]
    return JsonResponse(
        {"ok": True, "sector": sector, "symbols": symbols, "companies": companies},
        status=200,
    )


@require_GET
def stock360_keyfin_api(request):
    """Sector-aware key financials for one symbol, across every reported quarter."""
    from .services.key_financials import key_financials

    sym = _valid_symbol(request.GET.get("symbol"))
    if not sym:
        return JsonResponse({"ok": False, "error": "Invalid symbol."}, status=200)

    result = key_financials(sym)
    if not result:
        return JsonResponse({"ok": False, "symbol": sym,
                             "error": f"No financial statements stored for {sym}."}, status=200)
    return JsonResponse(result, status=200)


@require_GET
def stock360_funda_recent(request):
    """Recently synced fundamentals (for the Workbench panel's history table).

    Unfiltered it shows only the newest few — the panel is a "did my sync land?"
    check, not an audit log, and a sector run would otherwise push 50-110 rows
    into the page.

    FILTERING IS SERVER-SIDE, which is the whole point: the table holds one row
    per synced company, so filtering the five rows the page happens to be
    holding would search almost nothing. ``q`` matches symbol or company name,
    ``sector`` and ``period`` match exactly. When any filter is supplied the
    default limit opens up, because the reader is now searching rather than
    glancing.
    """
    from datetime import timedelta

    from django.db.models import Q
    from django.utils import timezone

    from .models import FundaFundamentalSnapshot

    # One filter per column of the history table, so each control sits under the
    # header it acts on. `q` stays supported as a symbol-or-company catch-all.
    q = (request.GET.get("q") or "").strip()[:60]
    symbol = (request.GET.get("symbol") or "").strip()[:40]
    company = (request.GET.get("company") or "").strip()[:80]
    sector = (request.GET.get("sector") or "").strip()[:100]
    period = (request.GET.get("period") or "").strip()[:40]
    fs_min = (request.GET.get("fs_min") or "").strip()[:10]
    within = (request.GET.get("within") or "").strip()[:10]   # hours

    filtered = bool(q or symbol or company or sector or period or fs_min or within)

    try:
        limit = int(request.GET.get("limit", 50 if filtered else 5))
    except (TypeError, ValueError):
        limit = 50 if filtered else 5
    limit = max(1, min(200, limit))

    qs = FundaFundamentalSnapshot.objects.all()
    if q:
        qs = qs.filter(Q(symbol__icontains=q) | Q(security_name__icontains=q))
    if symbol:
        qs = qs.filter(symbol__icontains=symbol)
    if company:
        qs = qs.filter(security_name__icontains=company)
    if sector:
        qs = qs.filter(sector=sector)
    if period:
        qs = qs.filter(period=period)
    if fs_min:
        try:
            qs = qs.filter(fs_written__gte=int(fs_min))
        except (TypeError, ValueError):
            pass          # a half-typed number must not blank the table
    if within:
        try:
            qs = qs.filter(synced_at__gte=timezone.now() - timedelta(hours=float(within)))
        except (TypeError, ValueError):
            pass

    total = qs.count()
    rows = list(
        qs.order_by("-synced_at").values(
            "symbol", "security_name", "sector", "period", "fs_written", "synced_at"
        )[:limit]
    )
    for r in rows:
        r["synced_at"] = r["synced_at"].isoformat() if r["synced_at"] else None

    # Dropdown options come from what has actually been synced, so the filter
    # can never offer a sector or period that would return nothing.
    base = FundaFundamentalSnapshot.objects.order_by()
    sectors = sorted(s for s in base.values_list("sector", flat=True).distinct() if s)
    periods = sorted((p for p in base.values_list("period", flat=True).distinct() if p),
                     reverse=True)

    return JsonResponse({
        "ok": True,
        "results": rows,
        "total": total,                       # matches BEFORE the limit is applied
        "returned": len(rows),
        "grand_total": base.count(),          # everything ever synced
        "filtered": filtered,
        "limit": limit,
        "sectors": sectors,
        "periods": periods,
    }, status=200)


@require_GET
def stock_valuation_view(request, symbol=None):
    """Valuation & sector-model workspace for one symbol.

    Split out of Stock 360: the intrinsic model, the peer table and the
    Morningstar model together ran longer than the rest of that page, and the
    model's peer list had to be collapsed to fit. Here it has the room, so
    nothing is hidden. Both pages call the same endpoints, so there is exactly
    one valuation renderer.
    """
    sym = _valid_symbol(symbol or request.GET.get("symbol"))
    if not sym:
        sym = _default_symbol()
    quote = _quote(sym) or {}
    return render(request, "core_analysis/stock_valuation.html", {
        "symbol": sym,
        "security_name": quote.get("security_name") or sym,
        "close": quote.get("close"),
        "asset_version": _asset_version(),
    })


@require_GET
def stock360_valuation_api(request):
    """Intrinsic P/B verdict + sector peer rank for one symbol.

    Both halves come from our own tables: the fundamental snapshot for ROE /
    book value / EPS / DPS, and the daily price table for the current close and
    the peer cohort's prices. ``r`` and ``g`` are overridable (percent) so the
    panel behaves like a model the reader can push on, not a fixed verdict —
    the defaults are the ones the service documents.
    """
    from .services.stock360_analytics import valuation

    sym = _valid_symbol(request.GET.get("symbol"))
    if not sym:
        return JsonResponse({"ok": False, "error": "Invalid symbol."}, status=200)

    def _rate(name):
        raw = request.GET.get(name)
        if raw in (None, ""):
            return None
        try:
            v = float(raw) / 100.0
        except (TypeError, ValueError):
            return None
        return v if -1.0 < v < 1.0 else None

    quote = _quote(sym)
    price = (quote or {}).get("close")

    # Custom r/g are not cached — they are a what-if the reader is driving.
    r, g = _rate("r"), _rate("g")
    if r is None and g is None:
        key = f"stock360:val:v1:{sym}:{(quote or {}).get('as_of')}"
        cached = cache.get(key)
        if cached is not None:
            return JsonResponse(cached, status=200)

    try:
        data = valuation(sym, price=price, cost_of_equity=r, growth=g)
    except Exception:  # pragma: no cover - defensive
        logger.exception("Stock 360 valuation failed for %s", sym)
        return JsonResponse({"ok": False, "error": "Could not build the valuation."}, status=200)

    payload = {"ok": True, "symbol": sym, "price": price, **data}
    if r is None and g is None:
        cache.set(f"stock360:val:v1:{sym}:{(quote or {}).get('as_of')}", payload, _PAGE_CACHE_TTL)
    return JsonResponse(payload, status=200)


@require_GET
def stock360_dividends_api(request):
    """Per-fiscal-year dividend history for one symbol, from stored statements."""
    from .services.stock360_analytics import dividends

    sym = _valid_symbol(request.GET.get("symbol"))
    if not sym:
        return JsonResponse({"ok": False, "error": "Invalid symbol."}, status=200)

    try:
        return JsonResponse({"ok": True, "symbol": sym, **dividends(sym)}, status=200)
    except Exception:  # pragma: no cover - defensive
        logger.exception("Stock 360 dividends failed for %s", sym)
        return JsonResponse({"ok": False, "error": "Could not read dividend history."}, status=200)


@require_GET
def stock360_flow_series_api(request):
    """Cumulative net-buy per broker across the sessions in the chosen window.

    The floorsheet block's window selector drives this; ``range`` accepts the
    same keys the broker desk uses (1w / 1m / 3m / 6m / 1y).
    """
    from .services import broker_flow as bf

    sym = _valid_symbol(request.GET.get("symbol"))
    if not sym:
        return JsonResponse({"ok": False, "error": "Invalid symbol."}, status=200)

    range_key = (request.GET.get("range") or "1m").strip().lower()
    if range_key not in bf.NAMED_RANGES:
        return JsonResponse({"ok": False, "error": "Unknown timeframe."}, status=200)

    try:
        top_n = max(3, min(12, int(request.GET.get("top", 6))))
    except (TypeError, ValueError):
        top_n = 6

    try:
        data = bf.net_buy_series(sym, range_key=range_key, top_n=top_n)
    except Exception:  # pragma: no cover - defensive
        logger.exception("Stock 360 net-buy series failed for %s", sym)
        return JsonResponse({"ok": False, "error": "Could not build the flow series."}, status=200)

    if not data:
        return JsonResponse(
            {"ok": False, "symbol": sym,
             "error": f"No floorsheet trades for {sym} in that window."}, status=200)
    return JsonResponse(data, status=200)


@require_GET
def stock360_sop_api(request):
    """SOP Combined (Confluence) signal for one symbol.

    Runs the SAME engine the Strategy Simulator's "SOP Combined" tab runs, with
    that tab's own defaults — every indicator, a 3-of-N confluence floor, strict
    regime filter — so the call shown here and the call shown on the desk can
    never disagree for the same symbol.

    Two readings come back and both are shown:
      * ``pure``   — what the indicators alone say (BUY / SELL), regime ignored.
      * ``action`` — the tradeable instruction after the NEPSE 200-SMA regime
        filter (BUY / HOLD / SELL / WAIT).
    They diverge exactly when the setup is good but the market is not, which is
    the point of keeping both.

    The backtest is real pandas work, so the result is cached per
    (symbol, latest EOD) like the rest of the page.
    """
    from .services.sop_strategy import run_sop_combined_simulation, market_regime_series
    from .views import _build_standard_dataframe

    sym = _valid_symbol(request.GET.get("symbol"))
    if not sym:
        return JsonResponse({"ok": False, "error": "Invalid symbol."}, status=200)

    quote = _quote(sym)
    if not quote:
        return JsonResponse({"ok": False, "error": f"No price data for {sym}."}, status=200)

    key = f"stock360:sop:v1:{sym}:{quote['as_of']}"
    cached = cache.get(key)
    if cached is not None:
        return JsonResponse(cached, status=200)

    try:
        df = _adjusted_df(sym, quote["as_of"])
        if df is None or df.empty:
            return JsonResponse({"ok": False, "error": f"No price history for {sym}."}, status=200)

        # The regime filter reads the NEPSE Index, not the stock — same series
        # the desk builds, from the whole index history rather than the 5-year
        # window, so the 200-SMA is warm at the start of the test.
        idx = _build_standard_dataframe("NEPSE INDEX", "2000-01-01", quote["as_of"])
        regime_frame = market_regime_series(idx) if idx is not None and not idx.empty else None

        metrics, _trades, _equity = run_sop_combined_simulation(
            df, regime_df=regime_frame, use_regime_filter=regime_frame is not None,
        )
    except Exception:  # pragma: no cover - defensive
        logger.exception("Stock 360 SOP signal failed for %s", sym)
        return JsonResponse({"ok": False, "error": "Could not run the SOP model."}, status=200)

    if not isinstance(metrics, dict) or metrics.get("error"):
        return JsonResponse(
            {"ok": False, "error": (metrics or {}).get("error") or "SOP model returned nothing."},
            status=200,
        )

    signal = metrics.get("signal") or {}
    payload = {
        "ok": True,
        "symbol": sym,
        "signal": signal,
        "setup": {
            "indicators": metrics.get("combined_indicators") or [],
            "min_agree": metrics.get("min_agree"),
            "n_indicators": metrics.get("n_indicators"),
            "regime_mode": metrics.get("regime_mode"),
        },
        # Backtest context for the same rule over the window on screen — a live
        # call means little without knowing how the rule has actually fared.
        "backtest": {
            "window": f"{len(df)} sessions",
            "trades": metrics.get("total_trades"),
            "win_rate": metrics.get("win_rate"),
            "profit_factor": metrics.get("profit_factor"),
            "strategy_return": metrics.get("total_return"),
            "buyhold_return": metrics.get("buyhold_return"),
            "excess": metrics.get("excess_vs_buyhold"),
            "beats_buyhold": metrics.get("beats_buyhold"),
            "sharpe": metrics.get("sharpe"),
            "max_drawdown": metrics.get("max_drawdown"),
            "time_in_market": metrics.get("time_in_market"),
        },
    }
    cache.set(key, payload, _PAGE_CACHE_TTL)
    return JsonResponse(payload, status=200)


@require_GET
def stock360_ai_api(request):
    """On-demand Gemini narrative for the S&R structure of one symbol.

    Signed-in users only (the call costs tokens), and the finished narrative is
    cached per (symbol, latest EOD date) — repeat clicks and repeat users get
    the cached read instead of a fresh spend. Never raises: every failure comes
    back as a JSON ``error`` the panel shows inline.
    """
    user = getattr(request, "user", None)
    if not user or not user.is_authenticated:
        return JsonResponse({"error": "Sign in to generate the AI narrative."}, status=200)

    sym = _valid_symbol(request.GET.get("symbol"))
    if not sym:
        return JsonResponse({"error": "Invalid symbol."}, status=200)

    quote = _quote(sym)
    if not quote:
        return JsonResponse({"error": f"No price data for {sym}."}, status=200)

    key = f"stock360:ai:{sym}:{quote['as_of']}"
    cached = cache.get(key)
    if cached is not None:
        return JsonResponse({**cached, "cached": True}, status=200)

    try:
        from .views import _recent_bars_summary
        from .services.support_resistance import run_support_resistance_analysis
        from .services.gemini_analysis import generate_sr_ai_analysis

        df = _adjusted_df(sym, quote["as_of"])
        if df is None:
            return JsonResponse({"error": f"No price data for {sym}."}, status=200)

        metrics, _rows = run_support_resistance_analysis(df, symbol=sym)
        if not metrics or metrics.get("error"):
            return JsonResponse({"error": (metrics or {}).get("error") or "No analysis available."}, status=200)

        result = generate_sr_ai_analysis(metrics, None, None, _recent_bars_summary(df))
        if result.get("analysis_html"):
            cache.set(key, result, _AI_CACHE_TTL)
        return JsonResponse(result, status=200)
    except Exception:  # pragma: no cover - narrative is best-effort
        logger.exception("Stock 360 AI narrative failed for %s", sym)
        return JsonResponse({"error": "Could not generate the AI narrative right now."}, status=200)
