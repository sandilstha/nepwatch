"""life_indicators.py — hand-entered life-insurance "Other Indicators".

The fundamentals feed stops at ``li_ks_534``; these nine lines continue the
same numbering so they slot under the feed's rows in every KS view. See the
``LifeInsuranceIndicator`` model docstring for why they are a separate table.
"""
from __future__ import annotations

import re
from decimal import Decimal

from django.core.cache import cache

from core_analysis.models import LifeInsuranceIndicator

LIFE = "Life Insurance"
NON_LIFE = "Non Life Insurance"
SECTOR = LIFE          # kept for older imports
_REV_KEY = "life_indicators_rev"

# Per sector: (model field, item number, sort code, item name, unit, kind).
#   item number continues the feed's own numbering for that sector (life ends
#   at li_ks_534, non-life at nli_ks_541); sort code is that number x10 so a
#   rate's low/high rows (5371, 5372) stay between their neighbours instead of
#   sorting after every 3-digit feed code.
#   kind: "rs"    = full rupees as printed -> shown in the feed's "Rs. 000";
#         "count" = plain number; "ratio" = as-is;
#         "rate"  = text range ("Rs. 55- Rs. 85 Per Thousand") -> low/high rows.
LIFE_SPEC = [
    ("policies_issued",          "535", "5350", "Total No of Policies Issued During the Year", "Nos.",      "count"),
    ("gross_claim_outstanding",  "536", "5360", "Gross Claim Outstanding (Amount)",            "Rs. 000",   "rs"),
    ("declared_bonus_rate",      "537", "5370", "Declared Bonus Rate",                         "Rs/000 SA", "rate"),
    ("interim_bonus_rate",       "538", "5380", "Interim Bonus Rate",                          "Rs/000 SA", "rate"),
    ("policyholders_loan",       "539", "5390", "Policyholders Loan",                          "Rs. 000",   "rs"),
    ("investment_at_cost",       "540", "5400", "Investment in Cost Value",                    "Rs. 000",   "rs"),
    ("life_insurance_fund",      "541", "5410", "Life Insurance Fund (Amount)",                "Rs. 000",   "rs"),
    ("unearned_premium_reserve", "542", "5420", "Unearned Premium Reserve for Term Policies",  "Rs. 000",   "rs"),
    ("solvency_margin_ratio",    "543", "5430", "Solvency Margin Ratio",                       "x",         "ratio"),
]
# Non-life: the feed already carries issued/renewed policy counts, gross
# written premium, claims paid/outstanding counts and LT/ST investments; these
# are the report lines it does not. Extend here when a report shows more.
NON_LIFE_SPEC = [
    ("gross_claim_outstanding",  "545", "5450", "Gross Claim Outstanding (Amount)",            "Rs. 000",   "rs"),
    ("investment_at_cost",       "546", "5460", "Investment in Cost Value",                    "Rs. 000",   "rs"),
    ("unearned_premium_reserve", "547", "5470", "Unearned Premium Reserve (Amount)",           "Rs. 000",   "rs"),
    ("solvency_margin_ratio",    "548", "5480", "Solvency Margin Ratio",                       "x",         "ratio"),
]
SPECS = {LIFE: LIFE_SPEC, NON_LIFE: NON_LIFE_SPEC}
# item_code prefix the feed uses for each sector, so ours sort under its rows
PREFIX = {LIFE: "li_ks_", NON_LIFE: "nli_ks_"}
SPEC = LIFE_SPEC       # kept for older imports
FIELDS = sorted({f for spec in SPECS.values() for f, *_ in spec})


def revision() -> int:
    """Bumped on every save/delete so cached KS matrices rebuild."""
    return int(cache.get(_REV_KEY) or 0)


def bump_revision() -> None:
    cache.set(_REV_KEY, revision() + 1, None)


def _code(prefix: str, sc: str, name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    return f"{prefix}{sc}_{slug}"


def _rate_bounds(text: str):
    """'Rs. 55- Rs. 85 Per Thousand' -> (55.0, 85.0); one figure -> (v, v)."""
    nums = [float(n) for n in re.findall(r"\d+(?:\.\d+)?", text or "")]
    if not nums:
        return None, None
    return min(nums), max(nums)


def rows_for(entry: LifeInsuranceIndicator) -> list[dict]:
    """One entry -> KS-shaped rows (item_code / item_name / sorting_code /
    amount / unit) in the feed's conventions."""
    out = []
    spec = SPECS.get(entry.sector, LIFE_SPEC)
    prefix = PREFIX.get(entry.sector, "li_ks_")
    for field, num, sc, name, unit, kind in spec:
        raw = getattr(entry, field)
        if kind == "rate":
            lo, hi = _rate_bounds(raw)
            if lo is None:
                continue
            out.append({"item_code": _code(prefix, num + "a", name + " low"), "item_name": f"{name} — Low",
                        "sorting_code": sc[:-1] + "1", "amount": lo, "unit": unit})
            out.append({"item_code": _code(prefix, num + "b", name + " high"), "item_name": f"{name} — High",
                        "sorting_code": sc[:-1] + "2", "amount": hi, "unit": unit})
            continue
        if raw is None or raw == "":
            continue
        amount = float(raw)
        if kind == "rs":
            amount = amount / 1000.0
        out.append({"item_code": _code(prefix, num, name), "item_name": name,
                    "sorting_code": sc, "amount": amount, "unit": unit})
    return out


def matrix_rows(sector: str, fiscal_year: str, quarter: int) -> list[dict]:
    """Rows to append to the Industry Analysis KS matrix for one period."""
    if sector not in SPECS:
        return []
    rows = []
    for e in LifeInsuranceIndicator.objects.filter(sector=sector, fiscal_year_ad=fiscal_year, quarter=quarter):
        for r in rows_for(e):
            rows.append({**r, "ticker": e.ticker})
    return rows


def statement_rows(ticker: str, fiscal_year: str, quarter: int) -> list[dict]:
    """Rows in the company statement API's shape for one ticker-period."""
    e = LifeInsuranceIndicator.objects.filter(
        ticker=ticker, fiscal_year_ad=fiscal_year, quarter=quarter).first()
    if not e:
        return []
    return [{"code": r["item_code"], "name": r["item_name"], "amount": r["amount"],
             "unit": r["unit"], "fmt": "int" if r["unit"] == "Nos." else "num",
             "header": False, "manual": True} for r in rows_for(e)]


def tickers_by_sector() -> dict[str, list[str]]:
    """{sector: [tickers the feed files under it]} — the entry form's dropdowns."""
    from core_analysis.models import FinancialStatement as F
    out = {}
    for sec in SPECS:
        out[sec] = sorted(set(F.objects.filter(sector=sec).order_by()
                              .values_list("ticker", flat=True).distinct()))
    return out


def life_tickers() -> list[str]:
    return tickers_by_sector().get(LIFE, [])


def form_spec() -> list[dict]:
    """Field descriptors for the template, tagged with the sectors they apply to."""
    seen = {}
    for sec, spec in SPECS.items():
        for f, _num, _sc, name, unit, kind in spec:
            d = seen.setdefault(f, {"field": f, "label": name, "unit": unit, "kind": kind, "sectors": []})
            d["sectors"].append(sec)
    return [{**d, "sectors_attr": "|".join(d["sectors"])} for d in seen.values()]


def parse_form(post, sector: str = LIFE) -> tuple[dict, list[str]]:
    """Form fields -> model kwargs for one sector's layout. Returns (values, errors).
    Fields outside that layout are set to None so a re-save clears stale values."""
    values, errors = {f: None for f in FIELDS}, []
    for f in FIELDS:
        if f in ("declared_bonus_rate", "interim_bonus_rate"):
            values[f] = ""
    for field, _num, _sc, name, _unit, kind in SPECS.get(sector, LIFE_SPEC):
        raw = (post.get(field) or "").strip()
        if kind == "rate":
            values[field] = raw[:80]
            continue
        if not raw:
            values[field] = None
            continue
        cleaned = raw.replace(",", "").replace("Rs.", "").replace("Rs", "").strip()
        try:
            values[field] = int(Decimal(cleaned)) if kind == "count" else Decimal(cleaned)
        except Exception:
            errors.append(f"{name}: '{raw}' is not a number.")
    return values, errors
