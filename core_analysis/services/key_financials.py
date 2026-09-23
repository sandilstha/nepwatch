"""Sector-aware key financials: one company, every reported quarter.

Stock 360 shows a compact statement view — the handful of line items that
actually matter for a company's sector — pivoted so each column is a reported
quarter (newest first). Everything is read from ``FinancialStatement``, the same
table the fundamentals desk and Morningstar use, so a freshly synced quarter
shows up here immediately.

Two design points worth knowing:

* Metrics are curated PER SECTOR, keyed on ``item_name``. Item codes carry a
  sector prefix (``cb_bs_130_loans`` vs ``mf_bs_...``) and their numbering is not
  consistent across sectors, but the descriptive name is stable — so a name-based
  map works for every sector with one table.
* Sectors without a curated list (notably "Others", which pools companies from
  several different statement schemas) fall back to the statement's own top-level
  rows — items whose ``sorting_code`` has no decimal part, i.e. the totals and
  section headings rather than their sub-items.
"""

import logging

from django.core.cache import cache

logger = logging.getLogger(__name__)

_CACHE_TTL = 30 * 60

# Key statistics appended for every sector. Aliases matter here: the source
# spells EPS "Annualized" for BFIs but "Anualized" (sic) for every other sector,
# and suffixes ROE with "%" outside banking. NPL only resolves for lenders;
# anything a sector doesn't report is simply dropped.
_COMMON_KS = (
    ["EPS (Annualized)", "EPS (Anualized)"],
    ["Book Value per Share"],
    ["Return on Equity (TTM)", "Return on Equity (TTM) %"],
    ["Non Performing Loan (NPL) to Total Loan"],
)

# A curated list this thin means the company's schema doesn't match its sector's
# usual shape (the statement tables pool a few variants) — top up from the
# statement's own top-level rows so the card still says something useful.
_MIN_CURATED_ROWS = 3

# sector -> {fs_type: [item_name, ...]} in display order.
# BFI list mirrors the metrics requested for banks/finance companies.
_BFI = {
    "BS": ["SHAREHOLDERS EQUITY", "INVESTMENTS", "LOANS", "DEPOSITS", "TOTAL ASSETS"],
    "IS": [
        "Net Interest Income",
        "Net Interest Fee and Commission Income",
        "Total Operating Income",
        "Impairment Charge",
        "Operating Profit",
        "Profit/Loss for the period",
        "Distributable Profit",
    ],
}

_INSURANCE = {
    "BS": ["TOTAL EQUITY", "Investments", "Loans", "TOTAL ASSETS"],
    "IS": [
        "Gross Earned Premiums",
        "Net Earned Premiums",
        "TOTAL INCOME",
        "Net Benefits and Claims Paid",
        "TOTAL EXPENSES",
        "Profit Before Tax",
        "NET PROFIT",
        "Total Distributable Profit",
    ],
}

SECTOR_METRICS = {
    "Commercial Banks": _BFI,
    "Development Banks": _BFI,
    "Finance": _BFI,
    # The Investment sector pools two statement schemas — some companies report
    # BFI-style ("SHAREHOLDERS EQUITY"), others use "Total Equity" — hence aliases.
    "Investment": {
        "BS": [
            ["Total Equity", "SHAREHOLDERS EQUITY"],
            ["INVESTMENTS", "Investments"],
            ["LOANS", "Loans", "Loans Extended"],
            ["TOTAL ASSETS", "Total Sources of Funds"],
        ],
        "IS": [
            "Net Interest Income",
            "Total Operating Income",
            "Impairment Charge",
            "Operating Profit",
            "Profit/Loss for the period",
            "Distributable Profit",
        ],
    },
    "Microfinance": {
        "BS": [
            "Paid Up Capital",
            "Reserves and Surplus",
            "Investments",
            "Loan and Advances",
            "Deposits",
            "Borrowings",
        ],
        "IS": [
            "Net Interest Income",
            "Total Operating Income",
            "Operating Profit Before Provision",
            "Provision for Possible Losses",
            "Operating Profit",
            "Net Profit/Loss",
        ],
    },
    "Life Insurance": _INSURANCE,
    "Non Life Insurance": _INSURANCE,
    "Hydro Power": {
        "BS": [
            "Paid up Capital",
            "Reserves",
            "Net Fixed Assets",
            "Investments",
            "Long Term Liabilities",
            "Total Sources of Funds",
        ],
        "IS": [
            "Income from Sale of Energy",
            "Cost of Production",
            "Gross Profit",
            "Operating Profit",
            "Profit Before Taxes",
            "Net Profit",
        ],
    },
    "Manufacturing And Processing": {
        "BS": ["TOTAL ASSETS", "Total Equity", "Share Capital", "Reserve and Surplus", "Total Liabilities"],
        "IS": [
            "Sales Less Return",
            "Total Income",
            "Total Expenditure",
            "Operating Profit",
            "Profit Before Tax",
            "Net Profit",
        ],
    },
    "Hotels And Tourism": {
        "BS": ["Share Capital", "Reserve & Surplus", "Net Fixed Assets", "Investments", "Grand Total"],
        "IS": [
            "Total Income",
            "Total Operating Expenditure",
            "Gross Operating Profit",
            "Net Profit (Loss) Before Tax",
            "Net Profit After Tax",
        ],
    },
    "Tradings": {
        "BS": ["TOTAL ASSETS", "Total Equity", "Share Capital", "Reserve and Surplus", "Total Liabilities"],
        "IS": [
            "Revenue from Operations",
            "Gross Profit",
            "Total Income from Operations",
            "Operating Profit",
            "Profit Before Tax",
            "Net Profit",
        ],
    },
}

_STMT_TITLE = {"BS": "Balance Sheet", "IS": "Income Statement", "KS": "Key Statistics"}

# Display-only cleanup of source label quirks (typos, stray unit suffixes).
_LABEL_FIX = {
    "EPS (Anualized)": "EPS (Annualized)",
    "Return on Equity (TTM) %": "Return on Equity (TTM)",
    "Return on Asset (TTM) %": "Return on Asset (TTM)",
    "Total Comprehesive Income": "Total Comprehensive Income",
    "Profit/Loss for the period": "Profit / Loss for the Period",
}


def _label(name):
    return _LABEL_FIX.get((name or "").strip(), name)

# Fallback shows at most this many top-level rows per statement, so an unmapped
# sector stays readable instead of dumping its whole schema.
_FALLBACK_MAX = 12


# Ratio metrics are stored as fractions but their ``unit`` column is unreliable —
# Commercial Banks tag NPL as "%", Microfinance leaves it blank. Detect by name so
# a ratio never renders as a bare 0.0288.
_RATIO_HINTS = ("npl", "return on", "ratio", "margin", "growth", "spread", "yield")


def _fmt_for(unit, name=""):
    """Client formatting hint from the source unit (name breaks unit ties)."""
    u = (unit or "").strip()
    if u == "%":
        return "pct"          # stored as a fraction
    if u.lower().startswith("rs"):
        return "rs000"        # thousands of rupees
    low = (name or "").lower()
    if any(h in low for h in _RATIO_HINTS):
        return "pct"
    return "num"


def _sort_val(code):
    try:
        return float(code)
    except (TypeError, ValueError):
        return 0.0


def _is_top_level(code):
    """True for a statement's own headline rows (no decimal sub-numbering)."""
    return "." not in str(code or "")


def key_financials(symbol):
    """Curated line items for ``symbol``, pivoted by reported quarter.

    Returns ``{ok, symbol, sector, periods, groups}`` — ``periods`` newest-first,
    each group holding the rows for one statement. ``None`` when the company has
    no statements at all.
    """
    from core_analysis.models import FinancialStatement as FS

    sym = (symbol or "").strip().upper()
    if not sym:
        return None

    cache_key = f"keyfin:v1:{sym}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached or None

    rows = list(
        FS.objects.filter(ticker=sym)
        .values("fs_type", "fiscal_year_ad", "quarter", "item_code", "item_name",
                "sorting_code", "unit", "amount", "sector")
    )
    if not rows:
        cache.set(cache_key, {}, _CACHE_TTL)
        return None

    sector = rows[0]["sector"]

    # Periods, newest first. fiscal_year_ad ("2025/26") sorts correctly as a
    # string, so a (fy, quarter) tuple orders periods exactly.
    periods = sorted({(r["fiscal_year_ad"], r["quarter"]) for r in rows}, reverse=True)
    period_keys = [{"key": f"{fy}|{q}", "fy": fy, "quarter": q} for fy, q in periods]

    # Index by item_code: several codes can share an item_name (e.g. "Investments"
    # appears as both a non-current and a current asset), so values are collected
    # per code and the best-populated code wins that name.
    by_code = {}
    for r in rows:
        code = r["item_code"]
        slot = by_code.get(code)
        if slot is None:
            slot = by_code[code] = {
                "fs_type": r["fs_type"],
                "name": r["item_name"],
                "sorting_code": r["sorting_code"],
                "unit": r["unit"],
                "values": {},
            }
        amount = r["amount"]
        if amount is not None:
            slot["values"][f"{r['fiscal_year_ad']}|{r['quarter']}"] = float(amount)

    # (fs_type, lowercased name) -> the code with the most reported periods.
    best_by_name = {}
    for code, slot in by_code.items():
        key = (slot["fs_type"], slot["name"].strip().lower())
        cur = best_by_name.get(key)
        if cur is None or len(slot["values"]) > len(by_code[cur]["values"]):
            best_by_name[key] = code

    spec = SECTOR_METRICS.get(sector)

    def _rows_for(fs_type, names):
        """Resolve each metric to a row. An entry may be a list of aliases (for
        sectors that pool more than one statement schema) — first match wins."""
        out, used = [], set()
        for entry in names:
            aliases = [entry] if isinstance(entry, str) else list(entry)
            for name in aliases:
                code = best_by_name.get((fs_type, name.strip().lower()))
                if not code or code in used:
                    continue
                slot = by_code[code]
                if not slot["values"]:
                    continue
                used.add(code)
                out.append({
                    "label": _label(slot["name"]),
                    "fmt": _fmt_for(slot["unit"], slot["name"]),
                    "values": slot["values"],
                })
                break
        return out

    def _fallback_rows(fs_type):
        """Top-level rows for a sector with no curated list."""
        cands = [
            s for s in by_code.values()
            if s["fs_type"] == fs_type and _is_top_level(s["sorting_code"]) and s["values"]
        ]
        cands.sort(key=lambda s: _sort_val(s["sorting_code"]))
        seen, out = set(), []
        for s in cands:
            nm = s["name"].strip().lower()
            if nm in seen:
                continue
            seen.add(nm)
            out.append({"label": _label(s["name"]), "fmt": _fmt_for(s["unit"], s["name"]), "values": s["values"]})
            if len(out) >= _FALLBACK_MAX:
                break
        return out

    groups = []
    for fs_type in ("BS", "IS"):
        items = _rows_for(fs_type, spec.get(fs_type, [])) if spec else _fallback_rows(fs_type)
        if len(items) < _MIN_CURATED_ROWS:
            # Top up with top-level rows this company actually reports, skipping
            # any label the curated pass already placed.
            have = {r["label"].strip().lower() for r in items}
            items += [r for r in _fallback_rows(fs_type)
                      if r["label"].strip().lower() not in have]
        if items:
            groups.append({"type": fs_type, "title": _STMT_TITLE[fs_type], "rows": items})

    ks_rows = _rows_for("KS", _COMMON_KS)
    if ks_rows:
        groups.append({"type": "KS", "title": _STMT_TITLE["KS"], "rows": ks_rows})

    if not groups:
        cache.set(cache_key, {}, _CACHE_TTL)
        return None

    result = {
        "ok": True,
        "symbol": sym,
        "sector": sector,
        "curated": bool(spec),
        "periods": period_keys,
        "groups": groups,
    }
    cache.set(cache_key, result, _CACHE_TTL)
    return result
