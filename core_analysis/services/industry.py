"""
industry.py — sector-wise statement comparison for the Fundamental Analysis desk.

Builds a MATRIX for one sector and one reporting period: line items down the
side, companies across the top. That orientation is the whole point — a single
company's balance sheet is already on Stock 360, and the question this desk
answers is the one you cannot ask there: how does every bank's loan book compare
in the same quarter?

WHAT IS NOT HERE, AND WHY: there is no Cash Flow Statement. The upstream feed
supplies exactly three statement types (BS 424,649 rows / IS 312,180 / KS
255,116) and no cash-flow rows exist under any type — a search of all 992k rows
for 'cash flow', 'operating activities', 'investing activities' and 'financing
activities' returns nothing. The UI used to render Cash Flow as a greyed-out tab to
explain the gap; that left a permanently dead control in the bar, so the tab is
gone and this endpoint still answers fs_type=CF with the reason above.
"""
from __future__ import annotations

import hashlib
import logging
from typing import Any

from django.core.cache import cache

logger = logging.getLogger(__name__)

CACHE_TTL = 1800
# BUMP when the payload shape changes.
INDUSTRY_VERSION = 2

# fs_type -> (label, blurb). Cash flow is kept in UNAVAILABLE_STATEMENTS so a
# direct request for it still gets a reason rather than a bare error.
STATEMENTS = {
    "BS": ("Balance Sheet", "What the company owns and owes at the period end."),
    "IS": ("Income Statement", "Revenue, costs and profit earned during the period."),
    "KS": ("Key Stats & Ratios", "Per-share figures, margins and regulatory ratios."),
}
UNAVAILABLE_STATEMENTS = {
    "CF": ("Cash Flow Statement",
           "Not published to this platform. The upstream feed supplies balance "
           "sheet, income statement and key stats only — no cash-flow lines "
           "exist under any statement type."),
}


def sectors() -> list[dict[str, Any]]:
    """Sectors that have filings, with company counts. Cached."""
    from django.db.models import Count

    from core_analysis.models import FinancialStatement as F

    ck = f"industry_sectors_v{INDUSTRY_VERSION}"
    got = cache.get(ck)
    if got is not None:
        return got
    rows = (F.objects.exclude(sector="").exclude(sector__isnull=True)
            .values("sector").annotate(companies=Count("ticker", distinct=True))
            .order_by("-companies"))
    out = [{"sector": r["sector"], "companies": r["companies"]} for r in rows]
    cache.set(ck, out, CACHE_TTL)
    return out


def periods(sector: str, fs_type: str = "BS", limit: int = 12) -> list[dict[str, Any]]:
    """Reporting periods available for a sector, newest first.

    Coverage is reported per period because the newest one is usually INCOMPLETE
    — at 2026-08-20 Commercial Banks show 22 tickers for 2025/26 Q3 but only 19
    for Q4, because late filers have not reported yet. Presenting Q4 as the
    sector's position without that count would understate the sector.
    """
    from django.db.models import Count

    from core_analysis.models import FinancialStatement as F

    from core_analysis.services.canslim import _fy_sort_key

    rows = (F.objects.filter(sector=sector, fs_type=fs_type)
            .values("fiscal_year_ad", "quarter")
            .annotate(companies=Count("ticker", distinct=True)))
    out = [{"fiscal_year": r["fiscal_year_ad"], "quarter": r["quarter"],
            "companies": r["companies"],
            "label": f"{r['fiscal_year_ad']} Q{r['quarter']}"} for r in rows]
    out.sort(key=lambda p: (_fy_sort_key(p["fiscal_year"]), p["quarter"]), reverse=True)
    return out[:limit]


def latest_prices(tickers) -> tuple[dict[str, float], Any]:
    """Newest stored close per ticker, plus the newest business date among them.

    Two queries rather than one row-per-symbol scan: MySQL has no DISTINCT ON,
    and the price table is the largest in the schema.
    """
    from django.db.models import Max

    from core_analysis.models import NepseDailyStockPrice as P

    tickers = list(tickers)
    if not tickers:
        return {}, None
    newest = {r["symbol"]: r["d"] for r in
              P.objects.filter(symbol__in=tickers).values("symbol").annotate(d=Max("business_date"))
              if r["d"]}
    if not newest:
        return {}, None
    out: dict[str, float] = {}
    as_of = None
    for r in (P.objects.filter(symbol__in=list(newest), business_date__in=set(newest.values()))
              .values("symbol", "business_date", "close_price")):
        if newest.get(r["symbol"]) != r["business_date"]:
            continue
        try:
            px = float(r["close_price"])
        except (TypeError, ValueError):
            continue
        if px <= 0:
            continue
        out[r["symbol"]] = px
        if as_of is None or r["business_date"] > as_of:
            as_of = r["business_date"]
    return out, as_of


def _refresh_market_price(payload: dict[str, Any]) -> None:
    """Replace the FILED share price with the latest close, in place.

    The feed files 'Market Value per Share' as it stood on the reporting date,
    which by the time anyone reads the sector is months old — measured on
    2026-09-09 the filed price was 31% away from the market for ANLB and 18% for
    GMLI. A sector valuation screen built on those numbers compares prices from
    different days, so the row is refreshed from the platform's own EOD store.

    'Reported PE (Annualized)' is refreshed with it. That is not an invention:
    the filed PE equals the filed price divided by the filed EPS for 192 of 192
    companies checked across four sectors, so re-dividing the new price by the
    same EPS keeps the source's own method and stops the two rows contradicting
    each other.

    Anything without a stored price keeps exactly what was filed. That covers
    the feed's synthetic aggregate columns (F_INDS_MEDIAN, OT_ME_N_AVG and the
    like), which are sector statistics rather than tradable companies.
    """
    tickers = payload.get("companies") or []
    prices, as_of = latest_prices(tickers)
    if not prices:
        return

    def _rows(prefix):
        return [r for r in payload["rows"]
                if (r.get("name") or r["item"]).strip().lower().startswith(prefix)]

    price_rows = _rows("market value per share")
    if not price_rows:
        return

    # EPS is needed to rebuild PE; take the first row that carries one.
    eps_rows = _rows("eps")
    eps = {}
    for t_i, t in enumerate(tickers):
        for r in eps_rows:
            v = r["values"][t_i]
            if v not in (None, 0):
                eps[t] = v
                break

    refreshed = 0
    for r in price_rows:
        for i, t in enumerate(tickers):
            px = prices.get(t)
            if px is None:
                continue          # aggregate column or untraded — leave as filed
            if r["values"][i] != px:
                refreshed += 1
            r["values"][i] = px
        r["live"] = True
        r["as_of"] = as_of.isoformat() if as_of else ""
        r["live_for"] = sum(1 for t in tickers if t in prices)

    for r in _rows("reported pe"):
        for i, t in enumerate(tickers):
            px, e = prices.get(t), eps.get(t)
            if px is None or not e:
                continue
            r["values"][i] = round(px / e, 4)
        r["live"] = True
        r["as_of"] = as_of.isoformat() if as_of else ""
        r["live_for"] = sum(1 for t in tickers if t in prices and eps.get(t))

    if refreshed and as_of:
        held = len(tickers) - len(prices)
        payload["price_as_of"] = as_of.isoformat()
        payload["note"] += (
            f" Market Value per Share and Reported PE use the close of "
            f"{as_of.isoformat()}, not the price filed with the statement"
            + (f"; {held} column(s) without a traded price keep the filed figure."
               if held else "."))


def best_period(sector: str, fs_type: str = "KS"):
    """(fiscal_year, quarter) to score a sector on: the newest period, unless it
    has under 80% of the previous period's filers (early filers only)."""
    avail = periods(sector, fs_type, limit=4)
    if not avail:
        return None
    chosen = avail[0]
    if len(avail) > 1 and chosen["companies"] < avail[1]["companies"] * 0.8:
        chosen = avail[1]
    return chosen["fiscal_year"], chosen["quarter"]


def matrix(sector: str, fs_type: str = "BS", fiscal_year: str = "",
           quarter: int = 0) -> dict[str, Any]:
    """Line items x companies for one sector and period.

    Row order follows ``sorting_code`` where the feed provides one, so the
    statement reads in its filed order (TOTAL ASSETS before its components)
    rather than alphabetically.
    """
    from core_analysis.models import FinancialStatement as F

    fs_type = (fs_type or "BS").upper()
    if fs_type in UNAVAILABLE_STATEMENTS:
        label, why = UNAVAILABLE_STATEMENTS[fs_type]
        return {"ok": False, "unavailable": True, "statement": label, "reason": why}
    if fs_type not in STATEMENTS:
        return {"ok": False, "reason": f"Unknown statement type {fs_type!r}."}

    avail = periods(sector, fs_type)
    if not avail:
        return {"ok": False, "reason": f"No {STATEMENTS[fs_type][0]} filings for {sector}."}
    if not fiscal_year or not quarter:
        # Default to the newest period that is not obviously half-reported: if
        # the latest has materially fewer filers than the one before, prefer the
        # complete one and say so.
        chosen = avail[0]
        if len(avail) > 1 and chosen["companies"] < avail[1]["companies"] * 0.8:
            chosen = avail[1]
        fiscal_year, quarter = chosen["fiscal_year"], chosen["quarter"]

    # Hashed: sector names contain spaces and fiscal years contain "/", both of
    # which are illegal in a memcached key. LocMem tolerates them, so this would
    # only surface as a hard failure the day REDIS_URL is set.
    from core_analysis.services import life_indicators as li
    # Hand-entered life-insurance lines change outside the feed's sync, so
    # their revision is part of the key for the one matrix they extend.
    li_rev = li.revision() if (sector in (li.LIFE, li.NON_LIFE) and fs_type == "KS") else 0
    # KS carries a LIVE share price, so the newest EOD date is part of the key —
    # otherwise the first cache fill of the day would serve yesterday's price
    # for the next half hour.
    px_day = ""
    if fs_type == "KS":
        from django.db.models import Max

        from core_analysis.models import NepseDailyStockPrice as P
        px_day = str(P.objects.aggregate(d=Max("business_date"))["d"] or "")
    ck = ("industry_m_v%d_%s" % (
        INDUSTRY_VERSION,
        hashlib.md5(f"{sector}|{fs_type}|{fiscal_year}|{quarter}|{li_rev}|{px_day}"
                    .encode()).hexdigest()))
    got = cache.get(ck)
    if got is not None:
        return got

    rows = list(F.objects.filter(sector=sector, fs_type=fs_type,
                                 fiscal_year_ad=fiscal_year, quarter=quarter)
                .values("ticker", "item_name", "item_code", "sorting_code",
                        "amount", "unit"))
    if not rows:
        return {"ok": False,
                "reason": f"No {STATEMENTS[fs_type][0]} rows for {sector} {fiscal_year} Q{quarter}."}
    if fs_type == "KS":
        # The feed's KS block ends at li_ks_534; the hand-entered "Other
        # Indicators" continue the numbering underneath it.
        rows.extend(li.matrix_rows(sector, fiscal_year, quarter))

    tickers = sorted({r["ticker"] for r in rows})

    def _order_key(sc: str) -> tuple:
        # sorting_code is text; parse as a number so "10" sorts after "9" AND
        # sub-items like "320.1" stay under "320" (isdigit() stranded them).
        sc = (sc or "").strip()
        try:
            return (0, float(sc), sc)
        except ValueError:
            return (1 if sc else 2, 0.0, sc.lower())

    # PASS 1 — decide which names are genuinely TWO different filed lines.
    #
    # One name can sit at more than one sorting code for two opposite reasons:
    #
    #   * The same company files BOTH lines. Every commercial bank files
    #     "Derivative Financial Instruments" at 150.1 under OTHER ASSETS and
    #     again at 230.1 under OTHER LIABILITIES; "Margin (MRQ) %" is filed at
    #     504 (gross) and 506 (net). Keying a row on the name alone merged an
    #     asset with a liability, and one margin with another. Worse, the DB
    #     returns rows unordered, so WHICH of the two survived was arbitrary and
    #     could change between cache rebuilds. These must be SPLIT.
    #
    #   * Different companies use different filing templates. In "Others",
    #     TOTAL ASSETS sits at 101, 150 or 160 depending on the sub-industry
    #     schema and each company files it once. Splitting those would shatter
    #     one comparable line into several near-empty rows. These stay MERGED.
    #
    # What separates them is whether the company sets overlap: same companies on
    # both codes means two real lines, disjoint companies means two templates.
    spread: dict[str, dict[tuple, set]] = {}
    for r in rows:
        item = (r["item_name"] or "").strip()
        if item:
            (spread.setdefault(item, {})
                   .setdefault(_order_key(r.get("sorting_code")), set())
                   .add(r["ticker"]))
    split_names: set[str] = set()
    for item, per_code in spread.items():
        if len(per_code) < 2:
            continue
        top = sorted(per_code.values(), key=len, reverse=True)[:2]
        shared = len(top[0] & top[1]) / max(1, min(len(top[0]), len(top[1])))
        if shared >= 0.5:
            split_names.add(item)

    # PASS 2 — build the cells. Key is the name, plus the filed line number for
    # the names decided above, so no company's two figures land in one cell.
    order: dict[tuple, tuple] = {}
    units: dict[tuple, str] = {}
    names: dict[tuple, str] = {}
    lines: dict[tuple, str] = {}
    cells: dict[tuple, dict[str, float]] = {}
    dropped = 0
    for r in rows:
        item = (r["item_name"] or "").strip()
        if not item:
            continue
        try:
            amt = float(r["amount"])
        except (TypeError, ValueError):
            continue
        sc = (r.get("sorting_code") or "").strip()
        okey = _order_key(sc)
        key = (item, okey) if item in split_names else (item, None)
        # A merged row sorts where its earliest instance sits.
        if key not in order or okey < order[key]:
            order[key] = okey
        names[key] = item
        if item in split_names:
            lines[key] = sc
        # keep the first non-empty unit seen for the row
        if r.get("unit") and key not in units:
            units[key] = r["unit"]
        if r["ticker"] in cells.get(key, {}):
            # Same company, same line, twice — a true source duplicate. Keep the
            # first and count it rather than let the later one silently win.
            dropped += 1
            continue
        cells.setdefault(key, {})[r["ticker"]] = amt

    items = sorted(cells, key=lambda k: (order[k], names[k]))
    payload = {
        "ok": True,
        "sector": sector,
        "statement": STATEMENTS[fs_type][0],
        "statement_note": STATEMENTS[fs_type][1],
        "fs_type": fs_type,
        "fiscal_year": fiscal_year,
        "quarter": quarter,
        "period_label": f"{fiscal_year} Q{quarter}",
        "companies": tickers,
        "periods": avail,
        # `item` carries the filed line number when one name occupies two lines,
        # so an asset and a liability sharing a name stay tellable apart; `line`
        # repeats it on its own for exports.
        "rows": [{"item": (f"{names[k]} (line {lines[k]})"
                           if lines.get(k) else names[k]),
                  "name": names[k],
                  "line": lines.get(k, ""),
                  "unit": units.get(k, ""),
                  "values": [cells[k].get(t) for t in tickers],
                  # coverage is per LINE ITEM: a ratio only a few banks report
                  # should not look like a sector-wide comparison
                  "reported_by": sum(1 for t in tickers if cells[k].get(t) is not None)}
                 for k in items],
        "line_items": len(items),
        "note": (f"{len(tickers)} companies filed this statement for "
                 f"{fiscal_year} Q{quarter}. Blank cells mean the company did not "
                 f"report that line — not zero."
                 + (f" A line number in brackets means that name is filed on two "
                    f"different lines, and each is shown separately."
                    if split_names else "")
                 + (f" {dropped} source row(s) repeated the same company on the "
                    f"same line and were ignored." if dropped else "")),
    }
    if fs_type == "KS":
        _refresh_market_price(payload)
    cache.set(ck, payload, CACHE_TTL)
    return payload
