# -*- coding: utf-8 -*-
"""Mutual fund holdings and NAV statements from the internal 192.168.1.39 feed.

This host is the source the manual upload path in ``mutual_fund_portfolio`` was
built to work around. It carries what no public feed does: 36k+ per-script
holdings across 14 Nepali months, each fund's balance sheet, and its income
statement. Once this syncs, the Assets and Sector allocation tabs populate
themselves — nobody has to upload a monthly report again.

FOUR THINGS THIS FEED WILL CATCH YOU OUT ON.

1.  **The balance sheet is in THOUSANDS; the holdings are in RUPEES.** C30MF
    states ``Listed Securities 583,875.98`` against holdings whose market values
    sum to 572,116,002 — the same number to within a valuation date, three
    orders of magnitude apart. Cross-checked against the ``funds`` endpoint,
    which gives C30MF a ``fund_size`` of 801,636,519 versus a balance-sheet NAV
    of 790,615.94. Every BS figure is therefore scaled by ``BS_UNIT`` on the way
    in, so the tables hold one unit: rupees.

2.  **Auth is a Django session, not a token.** ``/api/token/`` is itself behind
    the login, so there is no bootstrap that skips the form. We POST to
    ``/accounts/login/`` with the CSRF cookie and keep the session.

3.  **``snapshot=`` filters holdings but NOT nav-items.** It works where you
    would expect on ``/holdings/``; on ``/nav-items/`` it is silently ignored
    and you get the unfiltered page back, which reads as a fund with an empty
    balance sheet rather than as an error. Group nav-items by ``nav_date``.

4.  **Two month vocabularies.** This feed says "Ashad 2083" where ShareSansar's
    NAV feed says "Asadh 2083". Both go through
    ``mutual_fund_portfolio.canonical_period`` or a fund's NAV and its
    allocation would never join. See [[mutual-fund-nav]].
"""
from __future__ import annotations

import logging
import os
import time
from collections import defaultdict
from decimal import Decimal

import requests
from django.db import transaction

from core_analysis.models import (CompanyProfile, MutualFundHolding,
                                  MutualFundPortfolio, MutualFundProfile,
                                  MutualFundStatementItem)
from core_analysis.services.mutual_fund_portfolio import canonical_period

logger = logging.getLogger(__name__)

BASE_URL = os.environ.get("NEPSE_MF_API_BASE", "http://192.168.1.39:8000").rstrip("/")
USERNAME = os.environ.get("NEPSE_MF_API_USER", "")
PASSWORD = os.environ.get("NEPSE_MF_API_PASSWORD", "")
TIMEOUT = 120
PAGE_SIZE = 1000
PAGE_PAUSE = 1.5          # seconds between pages; the host throttles hard
MAX_RETRIES = 6
MAX_THROTTLE_WAIT = 120   # cap a hostile Retry-After rather than hang the sync

# The balance sheet reports in thousands of rupees. See note 1 above.
BS_UNIT = Decimal("1000")

# Balance-sheet lines, bucketed. Anything present and unlisted lands in "other"
# rather than being dropped, so the allocation still totals what the fund holds.
EQUITY_ITEMS = ("Listed Securities", "Registered Equities", "IPO Investment")
FIXED_INCOME_ITEMS = ("Bank Fixed Deposits", "Corporate Debentures",
                      "Government Bonds", "Other Government Securities")
# CASH IS "CURRENT ASSETS", NOT "Bank Balance". Verified against the published
# allocation for C30MF Ashad 2083, which shows cash 174,725 = 22.10%: that is
# Bank Balance 110,742.70 PLUS Other Current Assets 63,981.91, which is exactly
# the CURRENT ASSETS line. Using Bank Balance alone gives 14.01% and quietly
# loses a fifth of the fund. The two components are summed rather than reading
# CURRENT ASSETS directly so a fund that omits the subtotal still adds up.
CASH_ITEMS = ("Bank Balance", "Other Current Assets")
OTHER_ITEMS = ("Other Investments",)
NET_ASSETS_ITEM = "Net Asset Value (NAV)"
NAV_PER_UNIT_ITEM = "NAV per Unit"

# Lines that are correctly NOT buckets: subtotals (which would double-count),
# scheme metadata, and fee accruals. Listed explicitly so the "unmapped item"
# warning stays meaningful — a warning that fires on every sync is one nobody
# reads, and the point of it is to catch a NEW line the feed starts publishing.
NON_BUCKET_ITEMS = frozenset({
    "CURRENT ASSETS", "CURRENT LIABILITIES", "INVESTMENTS",
    "Net Asset Value (NAV)", "Net Asset Value (NAV) Gross", "NAV per Unit",
    "Number of Units Outstanding",
    "Fund Management and Depository Fee", "Fund Supervisor Fee",
})

SOURCE_NAME = "192.168.1.39 mutual-fund API"

# ---------------------------------------------------------------------------
# SECTOR VOCABULARY
#
# The feed uses TWO vocabularies for the same sectors and mixes them within a
# single fund: "HP" and "Hydro Power", "CB" and "Commercial Banks", "MC" and
# "Microcredit". Aggregating on the raw string therefore splits one sector into
# two columns that each look half-weight. Everything is folded to the short code
# the published sector table columns use.
#
# Reinsurance folds to OTH, matching the reference table, which files Himalayan
# Reinsurance under "Others" rather than giving it a column of its own.
# ---------------------------------------------------------------------------
SECTOR_CODES = [
    ("CB",    "Commercial Banks",            ("CB", "COMMERCIALBANKS")),
    ("DB",    "Development Bank",            ("DB", "DEVELOPMENTBANK", "DEVELOPMENTBANKS")),
    ("FIN",   "Finance",                     ("FIN", "FINANCE")),
    ("HT",    "Hotels",                      ("HOT", "HT", "HOTELS", "HOTELSANDTOURISM")),
    ("HYDRO", "Hydro Power",                 ("HP", "HYDRO", "HYDROPOWER")),
    # "Organized Fund" is the feed's name for the Investment sector — CHDC,
    # CIT, HIDCL, NRN — NOT for mutual funds. Folding it into MF inflated that
    # column by the whole Investment book (HLICF: 9.31% where the published
    # split is MF 1.83 + INV 7.48).
    ("INV",   "Investment",                  ("INV", "INVESTMENT", "OF", "ORGANIZEDFUND")),
    ("LI",    "Life Insurance",              ("LI", "LIFEINSURANCE")),
    ("MF",    "Mutual Fund",                 ("MF", "MUTUALFUND")),
    ("MFG",   "Manufacturing And Processing", ("MNP", "MFG", "MANUFACTURINGANDPROCESSING")),
    ("MFI",   "Microfinance",                ("MC", "MFI", "MICROCREDIT", "MICROFINANCE")),
    ("NLI",   "Non Life Insurance",          ("NLI", "NONLIFEINSURANCE")),
    # Our register spells it "Tradings"; the feed says "Trading". Missing the
    # plural silently emptied the TRAD column into Others.
    ("TRAD",  "Trading",                     ("TRA", "TRAD", "TRADING", "TRADINGS")),
    # Reinsurance and Telecom have no column in the published table; both file
    # under Others there, so they do here.
    ("OTH",   "Others",                      ("OTH", "OTHERS", "REIN", "REINSURANCE",
                                              "TELECOM", "TELECOMMUNICATION")),
]
SECTOR_ORDER = [code for code, _, _ in SECTOR_CODES]
SECTOR_LABEL = {code: label for code, label, _ in SECTOR_CODES}
_SECTOR_LOOKUP = {}
for _code, _label, _aliases in SECTOR_CODES:
    for _alias in _aliases:
        _SECTOR_LOOKUP[_alias] = _code


def sector_code(raw):
    """'HP' / 'Hydro Power' / 'hydropower' -> 'HYDRO'. Unknown -> 'OTH'.

    Falling back to OTH rather than to the raw string is deliberate: a new
    spelling should widen an existing column, never invent one.
    """
    import re as _re
    key = _re.sub(r"[^A-Z]", "", str(raw or "").upper())
    return _SECTOR_LOOKUP.get(key, "OTH")


class FeedError(RuntimeError):
    """Raised for anything that should stop a sync rather than half-finish it."""


# ---------------------------------------------------------------- transport

def _login():
    """Authenticated session, or FeedError. Credentials come from the env only.

    Deliberately no default username/password: a fallback credential in source
    is how an internal host ends up reachable from somewhere it shouldn't be.
    """
    if not USERNAME or not PASSWORD:
        raise FeedError(
            "Set NEPSE_MF_API_USER and NEPSE_MF_API_PASSWORD in .env — this "
            "feed uses a Django session login and has no anonymous access.")
    session = requests.Session()
    login_url = f"{BASE_URL}/accounts/login/"
    try:
        session.get(login_url, timeout=TIMEOUT)
        response = session.post(
            login_url,
            data={"username": USERNAME, "password": PASSWORD,
                  "csrfmiddlewaretoken": session.cookies.get("csrftoken") or ""},
            headers={"Referer": login_url},
            timeout=TIMEOUT, allow_redirects=False)
    except requests.RequestException as exc:
        raise FeedError(f"Could not reach {BASE_URL}: {exc}") from exc

    # A failed login re-renders the form with a 200; only the cookie is proof.
    if not session.cookies.get("sessionid"):
        raise FeedError(
            f"Login rejected by {BASE_URL} (HTTP {response.status_code}). "
            "Check NEPSE_MF_API_USER / NEPSE_MF_API_PASSWORD.")
    return session


def _get(session, url, params, endpoint):
    """One GET, honouring the feed's throttle.

    The host runs a DRF throttle and answers 429 with a ``Retry-After`` in
    seconds. A full holdings pull is ~37 pages, which trips it, so this waits
    exactly as long as it is asked to rather than guessing a backoff. Treating
    the 429 as a hard failure is what makes a sync look broken when it is only
    impatient.
    """
    for attempt in range(MAX_RETRIES):
        try:
            response = session.get(url, params=params, timeout=TIMEOUT)
        except requests.RequestException as exc:
            raise FeedError(f"{endpoint}: {exc}") from exc
        if response.status_code == 429:
            wait = float(response.headers.get("Retry-After") or 30)
            wait = min(wait + 1, MAX_THROTTLE_WAIT)
            logger.info("mutual_fund_api: throttled on %s, waiting %.0fs "
                        "(attempt %d/%d)", endpoint, wait, attempt + 1, MAX_RETRIES)
            time.sleep(wait)
            continue
        if response.status_code != 200:
            raise FeedError(f"{endpoint}: HTTP {response.status_code}")
        return response
    raise FeedError(
        f"{endpoint}: still throttled after {MAX_RETRIES} attempts. The feed "
        "rate-limits hard; re-run the sync in a few minutes.")


def _fetch_all(session, endpoint, **params):
    """Every page of a DRF list endpoint, following ``next``."""
    rows, url = [], f"{BASE_URL}/api/mutual-fund/{endpoint}/"
    params = dict(params, format="json", page_size=PAGE_SIZE)
    first = True
    while url:
        if not first:
            time.sleep(PAGE_PAUSE)   # stay under the limit instead of racing it
        first = False
        payload = _get(session, url, params, endpoint).json()
        rows.extend(payload.get("results") or [])
        url = payload.get("next")
        params = None      # `next` is already a fully-formed URL
    logger.info("mutual_fund_api: %s -> %d rows", endpoint, len(rows))
    return rows


def _dec(value):
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except (ArithmeticError, ValueError):
        return None


# ---------------------------------------------------------------- profiles

def _store_profiles(funds, balance_sheets):
    """Scheme facts into ``mutual_fund_profile`` — one row per fund.

    Two fields the ``funds`` endpoint does NOT carry and that the published
    fund list shows anyway:

    * the fund's full name ("Citizens Super 30 Mutual Fund") — the feed gives
      only the ticker, so it is read from our own CompanyProfile register;
    * the monthly NAV and its period — the feed gives only the daily one, so
      the newest balance sheet's "NAV per Unit" supplies it.
    """
    names = dict(CompanyProfile.objects.filter(symbol__in=list(funds))
                 .values_list("symbol", "security_name"))

    from core_analysis.services.mutual_fund_portfolio import month_index
    newest = {}
    for (symbol, period), bs in balance_sheets.items():
        current = newest.get(symbol)
        if current is None or month_index(period) > month_index(current[0]):
            newest[symbol] = (period, bs.get("nav_per_unit"))

    for symbol, row in funds.items():
        if not symbol:
            continue
        MutualFundProfile.objects.update_or_create(
            symbol=symbol.strip().upper(),
            defaults={
                "fund_name": (names.get(symbol) or "")[:255],
                "amc": (row.get("asset_management_company") or "")[:255],
                "amc_member": (row.get("asset_management_member") or "")[:255],
                "fund_size": _dec(row.get("fund_size")),
                "maturity_date": row.get("maturity_date") or None,
                "maturity_period": (row.get("maturity_period") or "")[:64],
                "daily_nav": _dec(row.get("daily_nav")),
                "daily_nav_date": row.get("daily_nav_date") or None,
                "monthly_nav": (newest.get(symbol) or (None, None))[1],
                "monthly_nav_period": (newest.get(symbol) or ("", None))[0],
            })


# ---------------------------------------------------------------- balance sheet

def _balance_sheets(session):
    """{(symbol, canonical_period): bucket dict} from the newest snapshot in each month.

    A fund can file more than once for a month — a Provisional row and then a
    Published one. Keyed by nav_date and taking the max, the published figure
    wins, which is what the fund itself would want quoted.
    """
    items = _fetch_all(session, "nav-items")
    snapshots = _fetch_all(session, "nav-snapshots")

    # nav-items carries nav_date but not month_year; the snapshots carry both.
    period_of = {}
    for snap in snapshots:
        key = (snap.get("mutual_fund"), snap.get("nav_date"))
        period = canonical_period(snap.get("month_year"))
        if period:
            period_of[key] = period

    by_key = defaultdict(dict)
    raw = defaultdict(list)          # verbatim lines, both statements
    for row in items:
        key = (row.get("mutual_fund"), row.get("nav_date"))
        amount = _dec(row.get("amount"))
        raw[key].append((row.get("fs_type"), row.get("item_name"), amount))
        if row.get("fs_type") != "BS":
            continue
        if amount is not None:
            by_key[key][row.get("item_name")] = amount * BS_UNIT

    # Newest nav_date wins within a period.
    best = {}
    for (symbol, nav_date), bs in by_key.items():
        period = period_of.get((symbol, nav_date))
        if not period:
            continue
        current = best.get((symbol, period))
        if current is None or nav_date > current[0]:
            best[(symbol, period)] = (nav_date, bs)

    _store_statements(best, raw, period_of)

    out = {}
    for (symbol, period), (nav_date, bs) in best.items():
        total = lambda names: sum((bs.get(n) or Decimal(0)) for n in names)  # noqa: E731
        known = (set(EQUITY_ITEMS + FIXED_INCOME_ITEMS + CASH_ITEMS + OTHER_ITEMS)
                 | NON_BUCKET_ITEMS)
        nav_per_unit = bs.get(NAV_PER_UNIT_ITEM)
        out[(symbol, period)] = {
            "nav_date": nav_date,
            "equity": total(EQUITY_ITEMS),
            "fixed_income": total(FIXED_INCOME_ITEMS),
            "cash": total(CASH_ITEMS),
            "other": total(OTHER_ITEMS),
            "net_assets": bs.get(NET_ASSETS_ITEM),
            # NAV per unit is a per-unit price, so it is NOT in thousands and
            # must be scaled back out of the blanket BS_UNIT multiply above.
            "nav_per_unit": (nav_per_unit / BS_UNIT) if nav_per_unit is not None else None,
            "unmapped": sorted(k for k in bs if k not in known),
        }
    return out


def _store_statements(best, raw, period_of):
    """Verbatim BS and IS lines for the winning snapshot of each fund-month.

    Only the snapshot that won the newest-date contest is stored, so a month a
    fund filed twice does not end up with Provisional and Published figures
    interleaved under the same period.
    """
    for (symbol, period), (nav_date, _bs) in best.items():
        lines = raw.get((symbol, nav_date)) or []
        if not lines:
            continue
        with transaction.atomic():
            MutualFundStatementItem.objects.filter(
                symbol=symbol, period=period).delete()
            MutualFundStatementItem.objects.bulk_create([
                MutualFundStatementItem(
                    symbol=symbol, period=period, fs_type=(fs or "")[:4],
                    item_name=(name or "")[:120], amount=amount,
                    nav_date=nav_date, position=index)
                for index, (fs, name, amount) in enumerate(lines)
                if name
            ], ignore_conflicts=True)


def statement(symbol, period, fs_type=None):
    """One fund-month's published lines, in the order the fund filed them."""
    rows = MutualFundStatementItem.objects.filter(symbol=symbol, period=period)
    if fs_type:
        rows = rows.filter(fs_type=fs_type)
    return list(rows)


# ---------------------------------------------------------------- sync

def sync(periods=None):
    """Pull holdings + balance sheets into the local portfolio tables.

    ``periods`` limits the work to specific canonical months; the default syncs
    everything the feed holds, which is a few seconds of transfer and worth it
    because a re-run is the only way a restated month gets corrected.
    """
    session = _login()

    funds = {f.get("mutual_fund"): f for f in _fetch_all(session, "funds")}
    balance_sheets = _balance_sheets(session)
    _store_profiles(funds, balance_sheets)
    holdings = _fetch_all(session, "holdings")

    by_fund_month = defaultdict(list)
    for row in holdings:
        period = canonical_period(row.get("month_year"))
        symbol = row.get("mutual_fund")
        if period and symbol:
            by_fund_month[(symbol, period)].append(row)

    # A month with a balance sheet but no holdings is still worth storing: the
    # Assets tab only needs the buckets, and the Sector tab correctly shows the
    # fund as having no equity detail rather than omitting it entirely.
    keys = set(by_fund_month) | set(balance_sheets)
    if periods:
        wanted = {canonical_period(p) for p in periods}
        keys = {k for k in keys if k[1] in wanted}

    written = skipped = holding_rows = 0
    unmapped = set()

    # The feed's ``funds`` rows carry the AMC ("Citizens Capital"), not the
    # scheme's name ("Citizens Super 30 Mutual Fund"). The scheme name lives in
    # our own register, same place _store_profiles reads it from.
    scheme_names = dict(
        CompanyProfile.objects.filter(symbol__in={s for s, _ in keys})
        .values_list("symbol", "security_name"))

    for symbol, period in sorted(keys):
        bs = balance_sheets.get((symbol, period)) or {}
        rows = by_fund_month.get((symbol, period)) or []
        unmapped.update(bs.get("unmapped") or ())

        equity_from_holdings = sum(
            (_dec(r.get("current_market_value")) or Decimal(0)) for r in rows)

        # Prefer the fund's own balance-sheet equity. It is the figure the fund
        # published; the holdings sum is the same book repriced, and the two
        # differ by a few percent because they are struck on different days.
        equity = bs.get("equity")
        if not equity:
            equity = equity_from_holdings
        if not equity and not rows:
            skipped += 1
            continue

        with transaction.atomic():
            portfolio, _ = MutualFundPortfolio.objects.update_or_create(
                symbol=symbol, period=period,
                defaults={
                    "fund_name": (scheme_names.get(symbol)
                                  or (funds.get(symbol) or {}).get(
                                      "asset_management_company", "") or ""),
                    "nav_monthly": bs.get("nav_per_unit"),
                    "equity_value": equity or Decimal(0),
                    "fixed_income_value": bs.get("fixed_income") or Decimal(0),
                    "cash_value": bs.get("cash") or Decimal(0),
                    "other_value": bs.get("other") or Decimal(0),
                    "net_assets": bs.get("net_assets"),
                    "source_name": SOURCE_NAME,
                })
            if rows:
                # Replace rather than merge: a restated month drops scripts, and
                # a merge would leave the dropped ones behind forever.
                portfolio.holdings.all().delete()
                MutualFundHolding.objects.bulk_create([
                    MutualFundHolding(
                        portfolio=portfolio,
                        script=(r.get("symbol") or "").strip().upper(),
                        company_name=(r.get("company_name") or "")[:255],
                        sector=(r.get("sector") or "")[:80],
                        kitta=_dec(r.get("quantity")),
                        book_value=_dec(r.get("published_amount")),
                        market_value=_dec(r.get("current_market_value")),
                        weight_percent=_dec(r.get("weight_percent")),
                    )
                    for r in rows if (r.get("symbol") or "").strip()
                ], ignore_conflicts=True)
                holding_rows += len(rows)
        written += 1

    if unmapped:
        # Loud on purpose: a new balance-sheet line the feed starts publishing
        # would otherwise vanish from the allocation without anyone noticing.
        logger.warning("mutual_fund_api: unmapped balance-sheet items %s", unmapped)

    return {
        "ok": True,
        "funds": len(funds),
        "fund_months": written,
        "skipped": skipped,
        "holdings": holding_rows,
        "periods": sorted({p for _, p in keys}),
        "unmapped_items": sorted(unmapped),
    }


# ---------------------------------------------------------------- reverse lookup

def funds_holding(script, period=None):
    """Which funds hold ``script``, heaviest first — the reverse of a portfolio.

    This is the question the per-fund reports cannot answer and the one worth
    asking of a stock: institutional ownership, and whether it is rising.
    """
    script = (script or "").strip().upper()
    if not script:
        return {"ok": False, "error": "No script given."}

    from core_analysis.services.mutual_fund_portfolio import (
        available_periods, month_index)

    periods = available_periods()
    if not periods:
        return {"ok": False, "error": "No fund portfolios synced yet.",
                "script": script, "periods": []}
    canon = canonical_period(period) if period else ""
    if canon not in periods:
        canon = periods[0]

    rows = (MutualFundHolding.objects
            .filter(script=script, portfolio__period=canon)
            .select_related("portfolio"))

    holders, total_value, total_kitta = [], Decimal(0), Decimal(0)
    for holding in rows:
        value = holding.market_value or Decimal(0)
        equity = holding.portfolio.equity_value or Decimal(0)
        total_value += value
        total_kitta += holding.kitta or Decimal(0)
        holders.append({
            "symbol": holding.portfolio.symbol,
            "fund_name": holding.portfolio.fund_name,
            "kitta": float(holding.kitta or 0),
            "market_value": float(value),
            # Weight in the FUND, not in the stock: "this is 4% of the fund's
            # equity book" is what tells you the manager's conviction.
            "weight_pct": (round(float(value) / float(equity) * 100.0, 2)
                           if equity else None),
        })
    holders.sort(key=lambda h: h["market_value"], reverse=True)

    # Prior month, so the panel can say whether funds are adding or trimming.
    previous = None
    order = [p for p in periods if month_index(p) < month_index(canon)]
    if order:
        prior = order[0]
        prior_rows = MutualFundHolding.objects.filter(
            script=script, portfolio__period=prior)
        previous = {
            "period": prior,
            "holders": prior_rows.count(),
            "kitta": float(sum((h.kitta or Decimal(0)) for h in prior_rows)),
        }

    return {
        "ok": True,
        "script": script,
        "period": canon,
        "periods": periods,
        "holders": holders,
        "holder_count": len(holders),
        "total_kitta": float(total_kitta),
        "total_value": float(total_value),
        "previous": previous,
    }


def coverage():
    """What the local tables now hold — for the inventory screen."""
    from core_analysis.services.mutual_fund_portfolio import available_periods
    return {
        "periods": available_periods(),
        "fund_months": MutualFundPortfolio.objects.count(),
        "holdings": MutualFundHolding.objects.count(),
        "funds": MutualFundPortfolio.objects.values("symbol").distinct().count(),
    }
