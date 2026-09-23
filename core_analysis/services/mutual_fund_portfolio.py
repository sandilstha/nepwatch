"""mutual_fund_portfolio.py — fund holdings in, allocation views out.

Mutual funds publish a monthly portfolio report: a list of the shares they hold
(script, kitta, book value, market value) plus one-line totals for the money that
is not in shares (debentures, fixed deposits, cash at bank). There is no public
feed for this — ShareSansar carries NAV only — so the report is IMPORTED, and
everything else on the desk is computed from it:

    holdings + buckets ──┬─> assets allocation   (equity / fixed income / cash %)
                         └─> sector allocation   (% of the equity book per sector)

Storing the raw holdings rather than the finished percentages is the whole point:
a new cut of the same data (top holdings, overlap between funds, a fund's stake in
one company) needs no new import, only a new query.

ONE TRAP WORTH KNOWING. Nepali month names have no agreed transliteration. The
NAV feed says "Asadh 2083"; portfolio reports say "Ashad 2083"; other sources say
"Ashadh". Left alone, the same fund-month lands under three different keys and
never joins. ``canonical_period`` folds every spelling onto one, and every write
path in this module goes through it.
"""
from __future__ import annotations

import logging
import re
from decimal import Decimal

from django.db import transaction

from core_analysis.models import (
    MutualFundHolding,
    MutualFundPortfolio,
)

logger = logging.getLogger(__name__)

# Canonical Nepali month names, in calendar order, each with the spellings seen
# in the wild. Order matters: ``month_index`` uses it to sort periods, which is
# the only way a Nepali month string can be ordered chronologically.
NEPALI_MONTHS = [
    ("Baishakh", ("baishakh", "baisakh", "baishak", "baisak", "vaishakh", "baishaakh")),
    ("Jestha",   ("jestha", "jeth", "jyestha", "jestha")),
    ("Ashadh",   ("ashadh", "asadh", "ashad", "asar", "ashar", "aashadh")),
    ("Shrawan",  ("shrawan", "sawan", "srawan", "shrawn", "saun")),
    ("Bhadra",   ("bhadra", "bhadau", "bhado", "bhadow")),
    ("Ashwin",   ("ashwin", "asoj", "aswin", "ashoj")),
    ("Kartik",   ("kartik", "kaartik", "karthik")),
    ("Mangsir",  ("mangsir", "mangshir", "marga")),
    ("Poush",    ("poush", "push", "pous", "paush")),
    ("Magh",     ("magh", "mag")),
    ("Falgun",   ("falgun", "fagun", "phalgun", "phagun")),
    ("Chaitra",  ("chaitra", "chait", "chaita")),
]
_MONTH_LOOKUP = {alias: name for name, aliases in NEPALI_MONTHS for alias in aliases}
_MONTH_ORDER = {name: i for i, (name, _) in enumerate(NEPALI_MONTHS)}

# Report lines that are NOT equity holdings. A fund reports these as single
# totals, so they become buckets on the portfolio row instead of holdings.
# Matched on WORD BOUNDARIES: a plain substring test diverted any script or
# company name merely containing "fd" / "bond" / "saving" into a bucket, where
# it silently stopped being a holding.
_FIXED_INCOME_RE = re.compile(
    r"\b(debentures?|bonds?|fixed[ -]deposits?|fds?|treasury|"
    r"government securit\w*|corporate debt|term deposits?)\b", re.I)
_CASH_RE = re.compile(
    r"\b(cash|bank balance|bank deposits?|savings?|current account)\b", re.I)

# Column header aliases. Reports vary in wording and case; anything not matched
# here is ignored rather than guessed at.
_COLUMN_ALIASES = {
    "script": ("script", "scrip", "symbol", "ticker", "stock", "security"),
    "company_name": ("company name", "company", "name", "security name"),
    "sector": ("sector", "industry"),
    "kitta": ("kitta", "quantity", "qty", "units", "no of shares", "no. of shares", "shares"),
    "book_value": ("book value", "cost", "cost value", "purchase value", "book"),
    "market_value": ("market value", "market", "value", "current value", "mkt value"),
}

MAX_HOLDING_ROWS = 5_000
_SCRIPT_RE = re.compile(r"^[A-Z0-9._-]{1,20}$")


# --------------------------------------------------------------------------- #
# Periods
# --------------------------------------------------------------------------- #

def canonical_period(raw):
    """'ashad 2083' / 'Asadh 2083' / 'ASHADH-2083' -> 'Ashadh 2083'.

    Returns "" when the month cannot be recognised — callers treat that as a
    refusal to import rather than inventing a period, because a mis-keyed month
    silently splits one month's data into two.
    """
    text = " ".join(str(raw or "").replace("-", " ").replace("/", " ").split())
    if not text:
        return ""
    year = re.search(r"(20\d{2}|21\d{2})", text)
    if not year:
        return ""
    for token in text.lower().split():
        name = _MONTH_LOOKUP.get(re.sub(r"[^a-z]", "", token))
        if name:
            return f"{name} {year.group(1)}"
    return ""


def month_index(period):
    """Sortable (year, month) for a canonical period — Nepali months do not
    sort as text, so every ordering in this module goes through here."""
    parts = str(period or "").split()
    if len(parts) != 2:
        return (0, 0)
    return (int(parts[1]) if parts[1].isdigit() else 0, _MONTH_ORDER.get(parts[0], 0))


def available_periods():
    """Every period we hold, newest first."""
    periods = set(
        MutualFundPortfolio.objects.order_by()
        .values_list("period", flat=True).distinct()
    )
    return sorted(periods, key=month_index, reverse=True)


# --------------------------------------------------------------------------- #
# Import
# --------------------------------------------------------------------------- #

def _num(value):
    """Decimal from a report cell, or None. Handles '1,398,136', '(1,234)'
    negatives, stray currency words and blank dashes."""
    if value is None:
        return None
    text = str(value).strip().replace(",", "").replace("Rs.", "").replace("Rs", "")
    if not text or text in ("-", "--", "N/A", "NA"):
        return None
    negative = text.startswith("(") and text.endswith(")")
    text = text.strip("()").strip()
    try:
        number = Decimal(text)
    except Exception:
        return None
    return -number if negative else number


def _map_columns(header_cells):
    """Header row -> {field: column index}. First match wins per field."""
    mapping = {}
    for index, cell in enumerate(header_cells):
        head = " ".join(str(cell or "").split()).lower().strip(" :#")
        if not head:
            continue
        for field, aliases in _COLUMN_ALIASES.items():
            if field in mapping:
                continue
            # Exact match first, then containment — "market value" must not be
            # captured by the looser "value" alias belonging to the same field.
            if head in aliases or any(a == head for a in aliases):
                mapping[field] = index
                break
        else:
            for field, aliases in _COLUMN_ALIASES.items():
                if field not in mapping and any(a in head for a in aliases):
                    mapping[field] = index
                    break
    return mapping


def _bucket_for(label):
    """Which asset bucket a non-equity report line belongs to, or None."""
    text = str(label or "")
    if _FIXED_INCOME_RE.search(text):
        return "fixed_income"
    if _CASH_RE.search(text):
        return "cash"
    return None


def parse_holdings_table(table):
    """Raw table (list of cell-lists) -> (holdings, buckets, skipped).

    ``holdings`` are the equity lines; ``buckets`` accumulates the fixed-income
    and cash totals found as one-line entries; ``skipped`` counts everything
    ignored (header, blanks, totals, unparseable rows) so an import can report
    honestly instead of silently dropping half a file.
    """
    holdings, skipped, seen = [], 0, set()
    buckets = {"fixed_income": Decimal(0), "cash": Decimal(0), "other": Decimal(0)}
    columns = None

    for raw in table:
        cells = [" ".join(str(c or "").split()) for c in (raw or ())]
        if not cells or not any(cells):
            continue

        if columns is None:
            mapped = _map_columns(cells)
            # A header is only a header if it names the two fields that make a
            # holding meaningful; otherwise it is a title or a stray line.
            if "script" in mapped and ("market_value" in mapped or "kitta" in mapped):
                columns = mapped
            else:
                skipped += 1
            continue

        def cell(field):
            index = columns.get(field)
            return cells[index] if index is not None and index < len(cells) else None

        label = (cell("script") or "").strip()
        value = _num(cell("market_value"))

        # Non-equity lines: "Fixed Deposit", "Cash at Bank" etc. carry a value
        # but no real script, so they become buckets, not holdings.
        bucket = _bucket_for(label)
        if bucket is None and not _SCRIPT_RE.match(label.upper()):
            # The name column only counts for rows with no real ticker — a
            # listed "... Savings ..." company must not fall into the cash bucket.
            bucket = _bucket_for(cell("company_name"))
        if bucket and value is not None:
            buckets[bucket] += value
            continue

        script = label.upper()
        if (not script or script.startswith("TOTAL") or script.startswith("GRAND")
                or not _SCRIPT_RE.match(script) or script in seen):
            skipped += 1
            continue

        seen.add(script)
        holdings.append({
            "script": script,
            "company_name": (cell("company_name") or "")[:255],
            "sector": (cell("sector") or "")[:80],
            "kitta": _num(cell("kitta")),
            "book_value": _num(cell("book_value")),
            "market_value": value,
        })
        if len(holdings) > MAX_HOLDING_ROWS:
            raise ValueError("Report contains too many holdings.")

    return holdings, buckets, skipped


@transaction.atomic
def import_month(symbol, period, table, *, fund_name="", nav_monthly=None,
                 nav_daily=None, ltp=None, fixed_income=None, cash=None,
                 net_assets=None, source_name=""):
    """Import one fund's portfolio for one month. Re-importing replaces it.

    ``fixed_income`` / ``cash`` override whatever the table's bucket lines said —
    pass them when the report states the totals somewhere the table parser
    cannot see (a summary block above the holdings, say).

    ``net_assets`` is the fund's own reported net asset figure. Pass it when the
    report states it: published allocation tables use it as the denominator, and
    it is NOT the sum of the buckets, so percentages computed against it can add
    up to a little over 100%. Omitted, the buckets are used instead.

    Raises ValueError on an unusable symbol, period or table, so a bad upload
    fails loudly instead of writing a half-empty month.
    """
    sym = (symbol or "").strip().upper()
    if not sym or not _SCRIPT_RE.match(sym):
        raise ValueError(f"Unusable fund symbol: {symbol!r}")

    canon = canonical_period(period)
    if not canon:
        raise ValueError(
            f"Unrecognised period {period!r} — expected a Nepali month and year, "
            f"e.g. 'Ashadh 2083'."
        )

    holdings, buckets, skipped = parse_holdings_table(table)
    if not holdings:
        raise ValueError(
            "No holdings found. Expected a header row naming at least a script "
            "column and a market value or kitta column."
        )

    equity = sum((h["market_value"] or Decimal(0)) for h in holdings)

    portfolio, _ = MutualFundPortfolio.objects.update_or_create(
        symbol=sym, period=canon,
        defaults={
            "fund_name": (fund_name or "")[:255],
            "nav_monthly": nav_monthly,
            "nav_daily": nav_daily,
            "ltp": ltp,
            "equity_value": equity,
            "fixed_income_value": (Decimal(str(fixed_income))
                                   if fixed_income is not None else buckets["fixed_income"]),
            "cash_value": (Decimal(str(cash))
                           if cash is not None else buckets["cash"]),
            "other_value": buckets["other"],
            "net_assets": net_assets,
            "source_name": (source_name or "")[:255],
        },
    )

    # Replace rather than merge: a re-import is a correction, and a script that
    # was sold out of the fund must not survive as a stale row.
    portfolio.holdings.all().delete()
    MutualFundHolding.objects.bulk_create(
        [MutualFundHolding(portfolio=portfolio, **h) for h in holdings],
        batch_size=500,
    )

    return {
        "symbol": sym,
        "period": canon,
        "holdings": len(holdings),
        "skipped": skipped,
        "equity_value": float(equity),
        "fixed_income_value": float(portfolio.fixed_income_value),
        "cash_value": float(portfolio.cash_value),
        "basis": "net_assets" if portfolio.net_assets else "bucket_sum",
    }
