"""
fundamental_views.py — Fundamental Analysis Desk.

Reads the company financial-statement line items harvested by the separate
``fundamentals`` app (mapped read-only as ``FinancialStatement``) and serves
them as a per-company desk: headline ratios, the three statements
(Key Statistics / Income Statement / Balance Sheet) for a chosen fiscal
period, and a multi-year trend of the marquee metrics.

Two endpoints:
  * fundamental_analysis_view — renders the desk shell. The symbol list for the
    picker is embedded so the page is usable without a round-trip.
  * fundamental_data_api      — JSON the page fetches on load / on symbol or
    period change.

Amounts are passed through raw with a ``fmt`` hint per field/row so the client
owns presentation (the ``%`` units are stored as fractions, ``Rs. 000`` values
as thousands of rupees, everything else as a plain number).
"""
from __future__ import annotations

import logging
import statistics

from django.db.models import Count, Max
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_GET

from core_analysis.models import CompanyProfile, FinancialStatement, NepseDailyStockPrice
from core_analysis.insights_views import _asset_version

logger = logging.getLogger(__name__)

# Statement types present in the source table, in display order, with labels.
STATEMENT_TYPES = (
    ("BS", "Balance Sheet"),
    ("IS", "Income Statement"),
    ("KS", "Key Statistics"),
)

# IMPORTANT: KS item codes are NOT numbered consistently across sectors — e.g.
# bank 508 = ROE but microfinance 508 = EPS. The DESCRIPTIVE name is stable,
# so every metric lookup keys on this classifier, never the bare number.
def _ks_key(item_code):
    """Map a KS item_code to a canonical metric key by its descriptive name."""
    c = (item_code or "").lower()
    if "return_on_equity" in c:
        return "roe"
    if "return_on_asset" in c:
        return "roa"
    if "book_value_per_share" in c:
        return "bvps"
    if "market_value_per_share" in c:
        return "price"
    if "dividend_per_share" in c:
        return "dps"
    if "reported_pe" in c:
        return "pe"
    if "eps" in c:
        return "eps"
    if "total_revenue" in c:
        return "revenue"
    if "net_income" in c:
        return "net_income"
    if "non_performing" in c or "npl_to_total" in c:
        return "npl"
    if "capital_fund_to_rwa" in c:
        return "car"
    if "margin_mrq" in c:
        return "gross_margin"
    if "outstanding_shares" in c:
        return "shares"
    return None


# Headline ratio cards, by canonical metric key, in display order. fmt drives
# the client formatter: pct (fraction → %), rs000 (thousands of Rs), num (plain).
HEADLINE_METRICS = (
    ("price", "Market Price", "num"),
    ("eps", "EPS (Annualized)", "num"),
    ("pe", "P/E (Annualized)", "num"),
    ("bvps", "Book Value / Share", "num"),
    ("roe", "ROE (TTM)", "pct"),
    ("roa", "ROA (TTM)", "pct"),
    # "dps" dropped from the headline strip: the Dividend card below already
    # carries it with the bonus/cash split and dates.
    ("net_income", "Net Income", "rs000"),
    ("revenue", "Total Revenue", "rs000"),
    ("gross_margin", "Gross Margin (MRQ)", "pct"),
)

# Marquee metrics charted across fiscal years (annual = Q4 rows).
TREND_METRICS = (
    ("revenue", "Total Revenue", "rs000"),
    ("net_income", "Net Income", "rs000"),
    ("eps", "EPS", "num"),
    ("roe", "ROE", "pct"),
)


def _fmt_for(unit: str) -> str:
    """Map a source ``unit`` string to a client formatter hint."""
    u = (unit or "").strip().lower()
    if u == "%":
        return "pct"
    if u.startswith("rs"):
        return "rs000"
    return "num"


def _sort_code_key(sc):
    """Order sorting_codes numerically when they are numeric.

    The source mixes 3- and 5-digit codes ('100' … '10105'), so a plain string
    sort puts '10000' before '999'. Sub-items are decimals off their section code
    ('320.1' … '320.3' under '320'), so the parse must be float, not int — as
    ``isdigit()`` they read as non-numeric and used to sort into the trailing
    alphabetical bucket, which stranded every sub-item (Retained Earnings, Loans
    to Customers, …) at the bottom of the statement instead of under its section.
    Genuinely non-numeric codes still sort last, alphabetically.
    """
    sc = (sc or "").strip()
    try:
        return (0, float(sc), "")
    except ValueError:
        return (1, 0.0, sc)


def _fundamental_tickers():
    """Active companies that have fundamentals, with names — for the search box.

    Restricted to CompanyProfile.status == Active so the picker never offers
    delisted / suspended scrips (the API still serves any ticker typed in by
    hand). Tickers with fundamentals but no active profile are omitted.
    """
    fund_tickers = set(
        FinancialStatement.objects.order_by().values_list("ticker", flat=True).distinct()
    )
    active = CompanyProfile.objects.filter(
        status__iexact="Active", symbol__in=fund_tickers
    ).values_list("symbol", "security_name", "sector_name")
    companies = [
        {
            "symbol": symbol,
            "name": name or "",
            "sector": (sector or "").strip() or "Unclassified",
        }
        for symbol, name, sector in active
    ]
    return sorted(
        companies,
        key=lambda company: (
            company["sector"] == "Unclassified",
            company["sector"].casefold(),
            company["symbol"].casefold(),
        ),
    )


@require_GET
def fundamental_analysis_view(request, symbol=None):
    """Fundamental Analysis desk — CROSS-SECTIONAL views only.

    The per-company blocks (ratio cards, multi-year trend, statement matrix,
    Morningstar model) live on Stock 360 and stay there: a request WITH a symbol
    still redirects, so every existing link and bookmark resolves exactly as
    before. Duplicating the single-stock view here is what the earlier merge
    removed, and it is not coming back.

    What this page owns is the question Stock 360 structurally cannot answer,
    because it renders one company at a time:

      * Industry Analysis — every company in a sector, side by side, for the
        same reporting period.
      * Smart Stock Score — the CAN SLIM screen ranked across the whole market.

    Both are comparisons, so both need the market as the unit, not a symbol.
    """
    sym = (symbol or request.GET.get("symbol") or "").strip().upper()
    if sym:
        return redirect(f"/stock/{sym}/#fund")
    from core_analysis.services import industry as ind
    from core_analysis.stock360_views import _bond_groups
    from core_analysis.models import BondValuation

    # Bonds tab: every listed debenture grouped by issuer, from the valuation
    # sheet loaded by `load_bond_valuations`. Filtering happens client-side.
    bond_rows = list(BondValuation.objects.select_related("issuer").order_by("maturity_date", "symbol"))
    bonds = None
    if bond_rows:
        valued = [r for r in bond_rows if r.ytm_pct is not None and r.issue_size]
        wsize = sum(float(r.issue_size) for r in valued)
        bonds = {
            "groups": _bond_groups(bond_rows),
            "count": len(bond_rows),
            "issuers": len({r.issuer_id for r in bond_rows}),
            "total_size_bn": sum(float(r.issue_size or 0) for r in bond_rows) / 1e9,
            "avg_ytm": round(sum(float(r.ytm_pct) * float(r.issue_size) for r in valued) / wsize, 2) if wsize else None,
            "benchmark": bond_rows[0].benchmark_pct,
            "as_of": bond_rows[0].valuation_date,
            "undervalued": sum(1 for r in bond_rows if r.valuation_tone == "pos"),
            "overvalued": sum(1 for r in bond_rows if r.valuation_tone == "neg"),
            "calls": ["Undervalued", "Fairly valued", "Overvalued", "Not traded"],
        }
    return render(request, "core_analysis/fundamental_analysis.html", {
        "asset_version": _asset_version(),
        "sectors": ind.sectors(),
        "bonds": bonds,
    })


@require_GET
def industry_matrix_api(request):
    """Sector-wide statement matrix: line items x companies for one period."""
    from core_analysis.services import industry as ind

    sector = (request.GET.get("sector") or "").strip()[:60]
    fs_type = (request.GET.get("fs_type") or "BS").strip().upper()[:4]
    fy = (request.GET.get("fiscal_year") or "").strip()[:12]
    try:
        q = int(request.GET.get("quarter") or 0)
    except (TypeError, ValueError):
        q = 0
    if not sector:
        return JsonResponse({"ok": False, "reason": "Pick a sector."}, status=200)
    try:
        return JsonResponse(ind.matrix(sector, fs_type, fy, q if q in (1, 2, 3, 4) else 0))
    except Exception:  # pragma: no cover - never 500 the desk
        logger.exception("industry matrix failed for %s/%s", sector, fs_type)
        return JsonResponse({"ok": False, "reason": "Could not build this statement."},
                            status=200)


@require_GET
def industry_periods_api(request):
    """Reporting periods available for a sector + statement type."""
    from core_analysis.services import industry as ind

    sector = (request.GET.get("sector") or "").strip()[:60]
    fs_type = (request.GET.get("fs_type") or "BS").strip().upper()[:4]
    if not sector:
        return JsonResponse({"ok": False, "periods": []}, status=200)
    return JsonResponse({"ok": True, "periods": ind.periods(sector, fs_type)})


@require_GET
def fundamental_sop_view(request):
    """Short methodology SOP for the Fundamental Analysis Morningstar tab."""
    return render(
        request,
        "core_analysis/fundamental_sop.html",
        {"asset_version": _asset_version()},
    )


def _statement_rows(qs):
    """Order a queryset of line items for tabular display.

    A row is flagged ``header`` when its name reads as a section total
    (all-uppercase in the source), so the client can emphasise it.
    """
    rows = []
    # Sorted in Python via _sort_code_key: the DB's string collation would put
    # 5-digit sorting codes ('10000') before 3-digit ones ('999').
    for r in sorted(qs, key=lambda r: (_sort_code_key(r.sorting_code), r.item_code or "")):
        name = r.item_name or ""
        rows.append(
            {
                "code": r.item_code,
                "name": name,
                "amount": float(r.amount) if r.amount is not None else None,
                "unit": r.unit or "",
                "fmt": _fmt_for(r.unit),
                "header": name.isupper() and len(name) > 1,
            }
        )
    return rows


@require_GET
def fundamental_data_api(request):
    """JSON feed for one company's fundamentals.

    Query params:
      symbol  — ticker (required; a missing/unknown ticker returns 404)
      fy      — fiscal year label, e.g. "2024/25" (defaults to latest)
      quarter — 1–4 (defaults to the latest available within fy)
    """
    sym = (request.GET.get("symbol") or "").strip().upper()

    base = FinancialStatement.objects.filter(ticker=sym) if sym else FinancialStatement.objects.none()
    if sym and not base.exists():
        return JsonResponse(
            {"ok": False, "error": f"No fundamentals available for {sym}."},
            status=404,
        )

    # Available periods, newest first. (Quarters run 1–4; annual figures ride
    # the Q4 rows — there is no separate quarter-0 annual row in the source.)
    periods = list(
        base.order_by()
        .values("fiscal_year_ad", "quarter")
        .annotate(n=Count("id"))
        .order_by("-fiscal_year_ad", "-quarter")
    )
    period_list = [
        {"fy": p["fiscal_year_ad"], "quarter": p["quarter"]} for p in periods
    ]
    if not period_list:
        return JsonResponse({"ok": False, "error": "No data."}, status=404)

    # Resolve the requested period (default: most recent).
    req_fy = (request.GET.get("fy") or "").strip()
    req_q = request.GET.get("quarter")
    selected = None
    if req_fy:
        try:
            req_q_int = int(req_q) if req_q not in (None, "") else None
        except (TypeError, ValueError):
            req_q_int = None
        for p in period_list:
            if p["fy"] == req_fy and (req_q_int is None or p["quarter"] == req_q_int):
                selected = p
                break
    if selected is None:
        selected = period_list[0]

    period_qs = base.filter(
        fiscal_year_ad=selected["fy"], quarter=selected["quarter"]
    )

    statements = []
    rows_by_type = {}
    for code, label in STATEMENT_TYPES:
        rows = _statement_rows(period_qs.filter(fs_type=code))
        if code == "KS":
            # Hand-entered life-insurance "Other Indicators" (li_ks_535+) sit
            # under the feed's own KS rows; empty for every other sector.
            from core_analysis.services import life_indicators as li
            rows.extend(li.statement_rows(sym, selected["fy"], selected["quarter"]))
        rows_by_type[code] = {r["code"]: r["amount"] for r in rows}
        statements.append({"type": code, "label": label, "rows": rows})

    # Index the selected period's KS rows by canonical metric key (name-based,
    # so it's correct for every sector's distinct numbering).
    ks_rows = next((s["rows"] for s in statements if s["type"] == "KS"), [])
    ks_by_key = {}
    for r in ks_rows:
        key = _ks_key(r["code"])
        if key and key not in ks_by_key:
            ks_by_key[key] = r["amount"]
    headline = [
        {
            "label": label,
            "value": ks_by_key.get(key),
            "fmt": fmt,
        }
        for key, label, fmt in HEADLINE_METRICS
        if ks_by_key.get(key) is not None
    ]

    # Multi-year trend: annual (Q4) KS rows for the marquee metrics.
    annual_qs = base.filter(fs_type="KS", quarter=4).order_by("fiscal_year_ad")
    trend_index = {}
    for r in annual_qs.values("fiscal_year_ad", "item_code", "amount"):
        key = _ks_key(r["item_code"])
        if not key:
            continue
        amt = float(r["amount"]) if r["amount"] is not None else None
        trend_index.setdefault(r["fiscal_year_ad"], {})[key] = amt
    trend_years = sorted(trend_index.keys())
    trend = []
    for key, label, fmt in TREND_METRICS:
        points = [
            {"fy": fy, "value": trend_index[fy].get(key)}
            for fy in trend_years
            if trend_index[fy].get(key) is not None
        ]
        if points:
            trend.append({"label": label, "fmt": fmt, "points": points})

    # BFI dividend-sustainability inputs. Only emitted when the selected period's
    # Income Statement carries a Distributable Profit line — i.e. the banks,
    # finance and insurance sectors whose IS runs the NRB/Beema-mandated
    # distributable-profit waterfall. The item_code prefix is sector-specific
    # (cb_/db_/fi_/inv_/li_/nli_…) but always ends in "_distributable_profit",
    # so a suffix match covers every BFI sub-sector with one branch. The client
    # turns these into DPS-coverage and reserve-haircut grades.
    is_rows = rows_by_type.get("IS", {})
    distributable = next(
        (amt for code, amt in is_rows.items() if code.endswith("_distributable_profit")),
        None,
    )
    bfi = None
    if distributable is not None:
        reg_reserve = next(
            (amt for code, amt in is_rows.items() if "transferred_to_regulatory_reserve" in code),
            None,
        )
        bfi = {
            "distributable_profit": distributable,        # Rs '000
            "regulatory_reserve_transfer": reg_reserve,   # Rs '000 (negative = moved into reserve)
            "net_income": ks_by_key.get("net_income"),    # Rs '000
            "dps": ks_by_key.get("dps"),                  # Rs / share
            "shares": ks_by_key.get("shares"),            # '000 shares
            "eps": ks_by_key.get("eps"),                  # Rs / share
        }

    profile = (
        CompanyProfile.objects.filter(symbol=sym)
        .values("symbol", "security_name", "sector_name")
        .first()
    )

    # Morningstar-style research: sector-relative fair value + percentile ranks.
    # Wrapped so a research failure never breaks the statement view.
    try:
        sector = base.order_by().values_list("sector", flat=True).first()
        morningstar = _morningstar_research(
            sym, sector, selected["fy"], selected["quarter"], ks_by_key
        )
    except Exception:  # pragma: no cover - research overlay is best-effort
        logger.exception("Morningstar research failed for %s", sym)
        morningstar = None

    from core_analysis.services.margin import margin_status

    return JsonResponse(
        {
            "ok": True,
            "symbol": sym,
            "profile": profile
            or {"symbol": sym, "security_name": "", "sector_name": ""},
            "margin": margin_status(sym),
            "selected": {"fy": selected["fy"], "quarter": selected["quarter"]},
            "periods": period_list,
            "headline": headline,
            # Canonical metric key → amount (sector-agnostic; percent metrics are
            # fractions). The Morningstar tab reads this instead of raw, sector-
            # specific KS item codes.
            "ks": ks_by_key,
            "statements": statements,
            "trend": trend,
            "bfi": bfi,
            "morningstar": morningstar,
        }
    )


@require_GET
def fundamental_matrix_api(request):
    """Multi-period statement matrix: one line item per row, one fiscal period
    per column (newest first) — the spreadsheet-style "Company Financials" view.

    Query params:
      symbol       — ticker (required)
      statement    — BS | IS | KS (default BS)
      data_version — source label (default: the company's only / first source)
      periods      — number of period columns to return (default 12)
    """
    sym = (request.GET.get("symbol") or "").strip().upper()
    fs_type = (request.GET.get("statement") or "BS").strip().upper()
    if fs_type not in {"BS", "IS", "KS"}:
        fs_type = "BS"
    try:
        limit = int(request.GET.get("periods") or 12)
    except (TypeError, ValueError):
        limit = 12
    limit = max(4, min(40, limit))

    base = FinancialStatement.objects.filter(ticker=sym, fs_type=fs_type)
    if not sym or not base.exists():
        return JsonResponse(
            {"ok": False, "error": f"No {fs_type} data for {sym or '—'}."},
            status=404,
        )

    # Deterministic: without an ORDER BY MySQL may hand back the sources in
    # either order, and the same URL would then render different numbers.
    versions = list(
        FinancialStatement.objects.filter(ticker=sym)
        .order_by("data_source")
        .values_list("data_source", flat=True)
        .distinct()
    )
    data_version = (request.GET.get("data_version") or "").strip()
    if data_version not in versions:
        data_version = versions[0] if versions else ""
    if data_version:
        base = base.filter(data_source=data_version)

    raw = base.values(
        "fiscal_year_ad", "quarter", "item_code", "item_name", "sorting_code", "unit", "amount"
    )

    # Columns: newest fiscal periods first, capped at `limit`.
    periods = sorted(
        {(r["fiscal_year_ad"], r["quarter"]) for r in raw},
        key=lambda p: (p[0], p[1]),
        reverse=True,
    )[:limit]
    period_set = set(periods)
    columns = [{"key": f"{fy}|{q}", "fy": fy, "quarter": q} for fy, q in periods]

    # Rows: one entry per line item, ordered by the source's sorting_code.
    items = {}
    for r in raw:
        if (r["fiscal_year_ad"], r["quarter"]) not in period_set:
            continue
        code = r["item_code"]
        it = items.get(code)
        if it is None:
            name = r["item_name"] or ""
            it = {
                "code": code,
                "name": name,
                "unit": r["unit"] or "",
                "fmt": _fmt_for(r["unit"]),
                "header": name.isupper() and len(name) > 1,
                "_sort": r["sorting_code"] or "",
                "values": {},
            }
            items[code] = it
        it["values"][f"{r['fiscal_year_ad']}|{r['quarter']}"] = (
            float(r["amount"]) if r["amount"] is not None else None
        )

    rows = sorted(items.values(), key=lambda x: (_sort_code_key(x["_sort"]), x["code"]))
    for r in rows:
        r.pop("_sort", None)

    return JsonResponse(
        {
            "ok": True,
            "symbol": sym,
            "statement": fs_type,
            "data_version": data_version,
            "data_versions": versions,
            "columns": columns,
            "rows": rows,
        }
    )


# ── Growth & Value scoring model (Morningstar-style), sector-wide ────────────
# All weights/thresholds live here so the model is tuned in one place (per the
# SOP). It is KS-only and keyed on canonical metric names (see _ks_key), so it
# works for every sector — bank-specific inputs (NPL, capital adequacy) are
# simply absent for non-financials and the weights renormalise over what's present.

# Growth Score: year-on-year growth of these canonical metrics → weight.
GV_GROWTH_WEIGHTS = (
    ("revenue", 0.25),
    ("net_income", 0.35),
    ("eps", 0.20),
    ("bvps", 0.20),
)

# Value Score sub-metrics: (key, weight, mode, a, b)
#   mode "inv" → inverse_score(value, best=a, worst=b)   [lower is better]
#   mode "dir" → direct_score(value_pct, best=a)          [higher is better, %]
GV_VALUE_SPECS = (
    ("pe",         0.22, "inv", 8, 25),
    ("pb",         0.22, "inv", 0.8, 3),
    ("div_yield",  0.15, "dir", 8, None),
    ("earn_yield", 0.15, "dir", 12, None),
    ("roe",        0.16, "dir", 18, None),
    ("roa",        0.10, "dir", 5, None),
    ("npl",        0.10, "inv", 1, 5),     # bank-only
    ("car",        0.05, "dir", 15, None), # bank-only
)
GV_STRONG = 50.0   # growth & value both ≥ this → score 3
GV_WATCH = 40.0    # growth or value ≥ this → score 2
GV_LARGE_CAP_SHARE = 0.55  # Large = top tickers up to 55% of cumulative market cap
GV_MID_CAP_SHARE = 0.85    # Mid = next 30% (cumulative 55%–85%); Small = last 15%
MILLION = 1_000_000.0


def _gv_clamp(v, lo=0.0, hi=100.0):
    return max(lo, min(hi, v))


def _gv_norm_growth(frac):
    """25% YoY growth → 100; linear; clamped. ``frac`` is a fraction (0.12)."""
    if frac is None:
        return None
    return _gv_clamp(frac * 400.0)


def _gv_inv(v, best, worst):
    if v is None:
        return None
    if v <= best:
        return 100.0
    if v >= worst:
        return 0.0
    return _gv_clamp((worst - v) / (worst - best) * 100.0)


def _gv_dir(v_pct, best):
    if v_pct is None:
        return None
    return _gv_clamp(v_pct / best * 100.0)


def _gv_weighted(pairs):
    """Weighted mean of (score, weight), renormalised over present scores."""
    num = sum(s * w for s, w in pairs if s is not None)
    den = sum(w for s, w in pairs if s is not None)
    return (num / den) if den else None


def _prev_fy(fy):
    """'2025/26' → '2024/25' (one fiscal year earlier)."""
    parts = (fy or "").split("/")
    if len(parts) != 2:
        return None
    try:
        a, b = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    # Keep the second component's width: '2078/2079' → '2077/2078', '2025/26' → '2024/25'.
    return f"{a - 1}/{b - 1:0{len(parts[1])}d}"


def _gv_value_inputs(d):
    """Comparable value metrics for one ticker from its canonical key→amount
    dict. Percent-style metrics (stored as fractions) become whole percents."""
    price = d.get("price")
    bvps = d.get("bvps")
    eps = d.get("eps")
    dps = d.get("dps")
    return {
        "pe": d.get("pe") if (d.get("pe") and d.get("pe") > 0) else None,
        "pb": (price / bvps) if (price is not None and bvps and bvps > 0) else None,
        "div_yield": (dps / price * 100) if (dps is not None and price and price > 0) else None,
        "earn_yield": (eps / price * 100) if (eps is not None and price and price > 0) else None,
        "roe": (d.get("roe") * 100) if d.get("roe") is not None else None,
        "roa": (d.get("roa") * 100) if d.get("roa") is not None else None,
        "npl": (d.get("npl") * 100) if d.get("npl") is not None else None,
        "car": (d.get("car") * 100) if d.get("car") is not None else None,
    }


def _gv_growth_score(cur, prev):
    pairs = []
    for key, w in GV_GROWTH_WEIGHTS:
        c, p = cur.get(key), prev.get(key)
        g = (c / p - 1) if (c is not None and p is not None and p > 0) else None
        pairs.append((_gv_norm_growth(g), w))
    return _gv_weighted(pairs)


def _gv_value_score(d):
    vin = _gv_value_inputs(d)
    pairs = []
    for key, w, mode, a, b in GV_VALUE_SPECS:
        v = vin.get(key)
        s = _gv_inv(v, a, b) if mode == "inv" else _gv_dir(v, a)
        pairs.append((s, w))
    return _gv_weighted(pairs)


def _gv_final(g, v):
    if g is None or v is None:
        return None, "Insufficient data"
    if g >= GV_STRONG and v >= GV_STRONG:
        return 3, "Strong / Attractive"
    if g >= GV_WATCH or v >= GV_WATCH:
        return 2, "Average / Watchlist"
    return 1, "Weak / Avoid"


def _positive_float(value):
    if value is None:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


def _latest_market_caps(symbols):
    """Latest EOD market capitalisation by symbol, converted to rupees.

    Resolved per symbol (each scrip's own most recent session), not from one
    global latest date — a stock suspended on the sector's most recent day
    would otherwise silently lose its fallback cap and drop to Unclassified.
    """
    symbols = [s for s in symbols if s]
    if not symbols:
        return {}
    latest_by_symbol = dict(
        NepseDailyStockPrice.objects.filter(symbol__in=symbols)
        .values_list("symbol")
        .annotate(latest=Max("business_date"))
    )
    if not latest_by_symbol:
        return {}
    rows = (
        NepseDailyStockPrice.objects.filter(
            symbol__in=list(latest_by_symbol),
            business_date__in=set(latest_by_symbol.values()),
        )
        .values("symbol", "business_date", "market_capitalization")
    )
    return {
        r["symbol"]: float(r["market_capitalization"]) * MILLION
        for r in rows
        if r["market_capitalization"] is not None
        and r["business_date"] == latest_by_symbol.get(r["symbol"])
    }


def _gv_market_cap(d, latest_cap=None):
    """Period KS cap first, latest EOD cap second. Returns (cap_rs, source).

    Both sources are normalised to RUPEES: KS ``shares`` is stored in '000, so
    price × shares × 1000 = Rs, matching the EOD column (Rs millions × MILLION).
    Without the ×1000 the two sources differ by three orders of magnitude, which
    pinned every EOD-fallback ticker into the Large segment and displayed
    period-KS caps 1000× too small.
    """
    price = _positive_float(d.get("price"))
    shares = _positive_float(d.get("shares"))
    if price is not None and shares is not None:
        return price * shares * 1000.0, "period_ks"
    cap = _positive_float(latest_cap)
    if cap is not None:
        return cap, "latest_eod"
    return None, "missing"


def _gv_cap_segments(market_caps):
    """Large/Mid/Small by cumulative market-cap share, not equal-count buckets."""
    segments = {ticker: "Unclassified" for ticker in market_caps}
    ranked = sorted(
        ((ticker, cap) for ticker, cap in market_caps.items() if cap and cap > 0),
        key=lambda item: item[1],
        reverse=True,
    )
    total = sum(cap for _, cap in ranked)
    if not total:
        return segments

    cumulative = 0.0
    for ticker, cap in ranked:
        # Classify on the cumulative share BEFORE adding this ticker: judged on
        # the share including itself, a dominant name that alone exceeds the 55%
        # band would fall through to Mid/Small — the sector's biggest company
        # must always be Large (concentrated NEPSE sectors hit this for real).
        share_before = cumulative / total
        cumulative += cap
        if share_before < GV_LARGE_CAP_SHARE:
            segments[ticker] = "Large"
        elif share_before < GV_MID_CAP_SHARE:
            segments[ticker] = "Mid"
        else:
            segments[ticker] = "Small"
    return segments


# The source table carries per-sector aggregate rows (industry median /
# numeric average / weighted average) alongside real companies — their tickers
# end in one of these. They must never appear as a "company" in the model.
_AGGREGATE_TICKER_SUFFIXES = ("_MEDIAN", "_N_AVG", "_W_AVG")


def _is_aggregate_ticker(ticker):
    return bool(ticker) and ticker.upper().endswith(_AGGREGATE_TICKER_SUFFIXES)


def _sector_model(sector, fy, quarter):
    """Growth/Value scores for every company in a sector for one period."""
    prev = _prev_fy(fy)
    rows = FinancialStatement.objects.filter(
        sector=sector, fs_type="KS", quarter=quarter,
        fiscal_year_ad__in=[fy, prev],
    ).values("ticker", "fiscal_year_ad", "item_code", "amount")

    cur_by, prev_by = {}, {}
    for r in rows:
        if _is_aggregate_ticker(r["ticker"]):
            continue
        key = _ks_key(r["item_code"])
        if not key:
            continue
        amt = float(r["amount"]) if r["amount"] is not None else None
        bucket = cur_by if r["fiscal_year_ad"] == fy else prev_by
        bucket.setdefault(r["ticker"], {})[key] = amt

    names = dict(
        CompanyProfile.objects.filter(symbol__in=list(cur_by.keys()))
        .values_list("symbol", "security_name")
    )

    latest_caps = _latest_market_caps(cur_by.keys())
    cap_by_ticker, cap_source_by_ticker = {}, {}
    for t, d in cur_by.items():
        market_cap, source = _gv_market_cap(d, latest_caps.get(t))
        cap_by_ticker[t] = market_cap
        cap_source_by_ticker[t] = source
    segments = _gv_cap_segments(cap_by_ticker)

    results = []
    for t, d in cur_by.items():
        g = _gv_growth_score(d, prev_by.get(t, {}))
        v = _gv_value_score(d)
        score, decision = _gv_final(g, v)
        market_cap = cap_by_ticker.get(t)
        results.append({
            "ticker": t,
            "name": names.get(t, ""),
            "growth": round(g, 2) if g is not None else None,
            "value": round(v, 2) if v is not None else None,
            "score": score,
            "decision": decision,
            "segment": segments.get(t, "Unclassified"),
            "market_cap": round(market_cap, 2) if market_cap is not None else None,
            "market_cap_source": cap_source_by_ticker.get(t, "missing"),
        })

    results.sort(key=lambda r: (-(r["score"] or 0), -(r["growth"] or 0)))
    return results


@require_GET
def fundamental_model_api(request):
    """Sector-wide Growth & Value scoring model (latest period for the sector).

    Pick the sector directly with ?sector=… ; ?symbol=… is accepted as a
    fallback to default to that company's sector. Also returns the full list of
    sectors so the front-end can offer a sector picker.
    """
    sectors = sorted(
        s for s in FinancialStatement.objects.order_by()
        .values_list("sector", flat=True).distinct() if s
    )

    sector = (request.GET.get("sector") or "").strip()
    sym = (request.GET.get("symbol") or "").strip().upper()
    if sector not in sectors:
        if sym:
            sector = (
                FinancialStatement.objects.filter(ticker=sym)
                .values_list("sector", flat=True).first()
            )
        sector = sector if sector in sectors else (sectors[0] if sectors else None)

    if not sector:
        return JsonResponse({"ok": False, "error": "No sectors available."}, status=404)

    # Coverage-aware: the newest period is usually half-filed, and scoring a
    # sector on its 2 early filers collapses the whole Large/Mid/Small model.
    from core_analysis.services import industry as ind
    chosen = ind.best_period(sector, "KS")
    if not chosen:
        return JsonResponse({"ok": False, "error": f"No data for {sector}."}, status=404)
    fy, quarter = chosen

    try:
        results = _sector_model(sector, fy, quarter)
    except Exception:  # pragma: no cover - defensive
        logger.exception("Growth/Value model failed for %s", sector)
        results = []

    summary = {
        "total": len(results),
        "strong": sum(1 for r in results if r["score"] == 3),
        "watch": sum(1 for r in results if r["score"] == 2),
        "weak": sum(1 for r in results if r["score"] == 1),
    }
    segment_summary = {
        "Large": sum(1 for r in results if r["segment"] == "Large"),
        "Mid": sum(1 for r in results if r["segment"] == "Mid"),
        "Small": sum(1 for r in results if r["segment"] == "Small"),
        "Unclassified": sum(1 for r in results if r["segment"] == "Unclassified"),
    }
    return JsonResponse({
        "ok": True,
        "symbol": sym,
        "sector": sector,
        "sectors": sectors,
        "selected": {"fy": fy, "quarter": quarter},
        "summary": summary,
        "segment_summary": segment_summary,
        "size_method": "market_cap_cumulative_55_30_15",
        "results": results,
    })


# ── Morningstar-style research: P/E fair value + sector percentile ranks ───────
# Feeds the Morningstar tab's "Valuation vs fair value" and "Sector percentile
# rank" panels for the selected company/period. Sector-relative, and — like the
# rest of this module — keyed on canonical KS metric names so it works for every
# sector, not just commercial banks.

# (key, label, client-fmt, higher-is-better) for the percentile-rank panel.
_MS_RANK_SPECS = (
    ("pe", "Price / Earnings", "num", False),
    ("pb", "Price / Book", "num", False),
    ("roe", "Return on Equity", "pct", True),
    ("div_yield", "Dividend Yield", "pct", True),
    ("earn_yield", "Earnings Yield", "pct", True),
)


def _ms_value_metrics(d):
    """Comparable valuation metrics from a canonical key→amount dict. Percent
    metrics stay as FRACTIONS (roe/div_yield/earn_yield) to match the client's
    'pct' formatter, which multiplies by 100."""
    price = _positive_float(d.get("price"))
    bvps = _positive_float(d.get("bvps"))
    eps = d.get("eps")
    dps = d.get("dps")
    pe = d.get("pe")
    return {
        "pe": pe if (pe is not None and pe > 0) else None,
        "pb": (price / bvps) if (price is not None and bvps) else None,
        "roe": d.get("roe"),
        "div_yield": (dps / price) if (dps is not None and price) else None,
        "earn_yield": (eps / price) if (eps is not None and price) else None,
    }


# Below this many same-period peers, the percentile/fair-value comparison isn't
# meaningful, so we fall back to each peer's latest reported quarter. This is what
# lets a freshly-synced company (whose sector peers haven't filed the same quarter
# yet) still get a populated Morningstar panel.
_MS_MIN_SAME_PERIOD_PEERS = 5


def _latest_ks_by_ticker(sector):
    """Canonical KS metrics for every ticker in ``sector`` at ITS OWN latest
    reported (fiscal_year, quarter). Used as the Morningstar peer set when the
    selected period is too sparse to compare against."""
    # Latest (fy, quarter) per ticker. fiscal_year_ad strings ("2024/25") sort
    # correctly alongside the integer quarter, so a tuple max is exact.
    latest = {}
    for p in (
        FinancialStatement.objects.filter(sector=sector, fs_type="KS")
        .values("ticker", "fiscal_year_ad", "quarter")
        .distinct()
    ):
        t = p["ticker"]
        key = (p["fiscal_year_ad"], p["quarter"])
        if t not in latest or key > latest[t]:
            latest[t] = key

    peer_by = {}
    for r in (
        FinancialStatement.objects.filter(sector=sector, fs_type="KS")
        .values("ticker", "fiscal_year_ad", "quarter", "item_code", "amount")
    ):
        t = r["ticker"]
        if _is_aggregate_ticker(t):
            continue
        if (r["fiscal_year_ad"], r["quarter"]) != latest.get(t):
            continue
        key = _ks_key(r["item_code"])
        if not key:
            continue
        peer_by.setdefault(t, {})[key] = float(r["amount"]) if r["amount"] is not None else None
    return peer_by


def _morningstar_research(sym, sector, fy, quarter, ks_by_key):
    """Fair value (sector-median P/E × EPS) + percentile ranks vs same-sector
    peers for one company/period. Returns None when the sector is unknown or no
    comparable metric is available."""
    if not sector:
        return None
    rows = FinancialStatement.objects.filter(
        sector=sector, fs_type="KS", quarter=quarter, fiscal_year_ad=fy,
    ).values("ticker", "item_code", "amount")

    peer_by = {}
    for r in rows:
        if _is_aggregate_ticker(r["ticker"]):
            continue
        key = _ks_key(r["item_code"])
        if not key:
            continue
        amt = float(r["amount"]) if r["amount"] is not None else None
        peer_by.setdefault(r["ticker"], {})[key] = amt

    # Sparse selected period (e.g. a just-synced company ahead of its peers'
    # filings) → compare against each peer's latest reported quarter instead.
    if len(peer_by) < _MS_MIN_SAME_PERIOD_PEERS:
        fallback = _latest_ks_by_ticker(sector)
        # Keep the company's own selected-period metrics; peers use their latest.
        fallback[sym] = ks_by_key
        if len(fallback) > len(peer_by):
            peer_by = fallback

    metrics_by_ticker = {t: _ms_value_metrics(d) for t, d in peer_by.items()}
    own = _ms_value_metrics(ks_by_key)

    # Fair value estimate = sector-median P/E applied to the company's EPS.
    sector_pes = [m["pe"] for m in metrics_by_ticker.values() if m["pe"] is not None]
    sector_pe = round(statistics.median(sector_pes), 2) if sector_pes else None
    price = _positive_float(ks_by_key.get("price"))
    eps = ks_by_key.get("eps")
    fair_value = None
    if sector_pe is not None and eps is not None and eps > 0:
        estimate = sector_pe * eps
        ratio = (price / estimate) if (price and estimate > 0) else None
        verdict = "Fairly valued"
        if ratio is not None:
            verdict = ("Undervalued" if ratio < 0.9
                       else "Overvalued" if ratio > 1.1 else "Fairly valued")
        fair_value = {
            "price": round(price, 2) if price is not None else None,
            "estimate": round(estimate, 2),
            "ratio": round(ratio, 2) if ratio is not None else None,
            "sector_pe": sector_pe,
            "verdict": verdict,
        }

    # Percentile rank per metric: share of OTHER peers this company beats (0–100),
    # direction-aware (lower P/E is better, higher ROE is better).
    ranks = []
    for key, label, fmt, higher_better in _MS_RANK_SPECS:
        val = own.get(key)
        if val is None:
            continue
        all_vals = [m.get(key) for m in metrics_by_ticker.values() if m.get(key) is not None]
        others = [m.get(key) for t, m in metrics_by_ticker.items()
                  if t != sym and m.get(key) is not None]
        if not all_vals:
            continue
        percentile = None
        if others:
            beat = sum(1 for p in others if (val > p if higher_better else val < p))
            percentile = round(100.0 * beat / len(others))
        ranks.append({
            "label": label,
            "value": round(val, 4),
            "fmt": fmt,
            "percentile": percentile,
            "median": round(statistics.median(all_vals), 4),
        })

    if fair_value is None and not ranks:
        return None
    return {
        "sector": sector,
        "peer_count": len(metrics_by_ticker),
        "fair_value": fair_value,
        "ranks": ranks,
    }


# ── Morning Star sector scan API ───────────────────────────────────────────────

@require_GET
def morningstar_scan_api(request):
    """Morning Star Growth/Value/Quality scan for one sector (JSON).

    Backed by services/morningstar.py — the final 13-sector parameter framework
    (docs/morningstar_q4_parameters.md). Percentile-ranked within sector,
    weights renormalised over present factors, quality as gates/flags.
    """
    from core_analysis.services import morningstar as ms

    sectors = ms.available_sectors()
    sector = (request.GET.get("sector") or "").strip()
    if sector not in sectors:
        sector = sectors[0] if sectors else None
    if not sector:
        return JsonResponse({"ok": False, "error": "No sectors available."}, status=404)

    try:
        payload = ms.sector_scan(sector)
    except Exception:  # pragma: no cover — defensive
        logger.exception("Morning Star scan failed for %s", sector)
        payload = {"ok": False, "error": "Scan failed — see server logs."}
    payload["sectors"] = sectors
    return JsonResponse(payload, status=200)


@require_GET
def morningstar_sop_view(request):
    """Methodology SOP for the Morning Star tab. Static content — every
    parameter, weight, gate and formula is documented in the template itself."""
    return render(request, "core_analysis/morningstar_sop.html",
                  {"asset_version": _asset_version()})
