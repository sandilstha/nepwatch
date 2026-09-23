# -*- coding: utf-8 -*-
"""Aggregations behind the six mutual-fund screens.

Everything here reads the local tables that ``mutual_fund_api`` fills; nothing
here talks to the network. That split matters: the feed rate-limits hard, so a
page load must never depend on it being reachable.

UNITS. One rule, applied everywhere: **the database holds rupees.** The upstream
balance sheet reports in thousands and is scaled on the way in, so a fund's
equity is 584,586,920 here where the source prints 584,587. Screens that want to
match the published tables divide by 1,000 at the point of display and say so —
they do not push the ambiguity back into storage. See ``mutual_fund_api`` note 1.
"""
from __future__ import annotations

from collections import defaultdict
from decimal import Decimal

from django.db.models import Sum

from core_analysis.models import (MutualFundHolding, MutualFundPortfolio,
                                  MutualFundProfile)
from core_analysis.services.mutual_fund_api import (SECTOR_LABEL, SECTOR_ORDER,
                                                    sector_code)
from core_analysis.services.mutual_fund_portfolio import (available_periods,
                                                          canonical_period,
                                                          month_index)

ZERO = Decimal(0)


def _f(value):
    return float(value) if value is not None else None


def _pct(part, whole):
    """Percentage, or None when the denominator is missing or zero.

    Returning None rather than 0.0 keeps "we don't know" distinct from "it is
    genuinely nothing" — the two read identically in a table otherwise.
    """
    if not whole:
        return None
    return round(float(part) / float(whole) * 100.0, 2)


def resolve_period(period, periods=None):
    """The requested period if we hold it, else the newest one we do."""
    periods = periods if periods is not None else available_periods()
    if not periods:
        return "", []
    canon = canonical_period(period) if period else ""
    return (canon if canon in periods else periods[0]), periods


# ------------------------------------------------------------------ prices

def latest_prices(symbols=None):
    """{symbol: (close, business_date)} from the most recent session.

    Uses ``nepse_daily_stock_prices``, NOT ``nepse_todayprice`` — the latter
    stops at 2026-05-21 and would silently price the whole desk three months
    stale. Verified 2026-08-27.
    """
    from django.db import connection
    sql = ("SELECT symbol, close_price, business_date FROM nepse_daily_stock_prices "
           "WHERE business_date = (SELECT MAX(business_date) FROM nepse_daily_stock_prices)")
    params = []
    if symbols:
        symbols = [s for s in symbols if s]
        if not symbols:
            return {}
        sql += " AND symbol IN (%s)" % ",".join(["%s"] * len(symbols))
        params = symbols
    with connection.cursor() as cursor:
        cursor.execute(sql, params)
        return {row[0]: (row[1], row[2]) for row in cursor.fetchall()}


def price_date():
    from django.db import connection
    with connection.cursor() as cursor:
        cursor.execute("SELECT MAX(business_date) FROM nepse_daily_stock_prices")
        row = cursor.fetchone()
    return row[0] if row else None


# ------------------------------------------------------------------ 1. fund list

def fund_list():
    """Every fund: scheme facts, both NAVs, market price, premium/discount.

    The premium/discount is quoted against the DAILY NAV, which is what the
    published list does — it is the newest reading, and a month-end figure can
    be four weeks stale. See [[mutual-fund-nav]] for why that distinction has
    already caused a nine-point error once.
    """
    profiles = list(MutualFundProfile.objects.all())
    prices = latest_prices([p.symbol for p in profiles])
    as_of = price_date()

    rows = []
    for profile in profiles:
        close, when = prices.get(profile.symbol, (None, None))
        nav = profile.daily_nav or profile.monthly_nav
        premium = premium_rs = None
        if close and nav:
            # TWO figures, because the published column conflates them. That
            # column is headed "Pre(+) or Dis(-)%" but carries RUPEES: C30MF
            # shows -0.78, which is 9.75 - 10.53, not -7.41%. Both are given
            # here, each labelled for what it actually is.
            premium_rs = round(float(close) - float(nav), 2)
            premium = round(premium_rs / float(nav) * 100.0, 2)
        rows.append({
            "symbol": profile.symbol,
            "fund_name": profile.fund_name,
            "amc": profile.amc,
            "fund_size": _f(profile.fund_size),
            "maturity_date": profile.maturity_date,
            "maturity_period": profile.maturity_period,
            "daily_nav": _f(profile.daily_nav),
            "daily_nav_date": profile.daily_nav_date,
            "monthly_nav": _f(profile.monthly_nav),
            "monthly_nav_period": profile.monthly_nav_period,
            "ltp": _f(close),
            "as_of": when,
            "premium_pct": premium,
            "premium_rs": premium_rs,
        })
    rows.sort(key=lambda r: r["symbol"])
    return {"ok": True, "funds": rows, "count": len(rows), "as_of": as_of}


# ------------------------------------------------------------------ 2. assets allocation

def assets_allocation(period=None):
    """Equity / fixed income / cash per fund for one month, plus an industry row.

    PERCENTAGES DIVIDE BY NET ASSETS, not by the sum of the three buckets. That
    is the fund's own convention and the reason a published row can total just
    over or under 100%: liabilities and fee accruals net out of NAV but are not
    one of the buckets. Reproducing it exactly is the point — C30MF Ashad 2083
    comes back 73.94 / 5.35 / 22.10, matching the published table.
    """
    canon, periods = resolve_period(period)
    if not canon:
        return {"ok": False, "error": "No fund portfolios synced yet.",
                "period": "", "periods": [], "funds": []}

    portfolios = MutualFundPortfolio.objects.filter(period=canon)
    rows, totals = [], defaultdict(Decimal)
    for portfolio in portfolios:
        equity = portfolio.equity_value or ZERO
        fixed = portfolio.fixed_income_value or ZERO
        cash = portfolio.cash_value or ZERO
        base = portfolio.net_assets or (equity + fixed + cash)
        rows.append({
            "symbol": portfolio.symbol,
            "fund_name": portfolio.fund_name,
            "monthly_nav": _f(portfolio.nav_monthly),
            "equity_value": float(equity),
            "equity_pct": _pct(equity, base),
            "fixed_income_value": float(fixed),
            "fixed_income_pct": _pct(fixed, base),
            "cash_value": float(cash),
            "cash_pct": _pct(cash, base),
            "net_assets": _f(portfolio.net_assets),
        })
        totals["equity"] += equity
        totals["fixed"] += fixed
        totals["cash"] += cash
        totals["base"] += base

    rows.sort(key=lambda r: (r["monthly_nav"] is None, -(r["monthly_nav"] or 0)))
    industry = {
        "symbol": "MF Inds",
        "fund_name": "",
        "monthly_nav": None,
        "equity_value": float(totals["equity"]),
        "equity_pct": _pct(totals["equity"], totals["base"]),
        "fixed_income_value": float(totals["fixed"]),
        "fixed_income_pct": _pct(totals["fixed"], totals["base"]),
        "cash_value": float(totals["cash"]),
        "cash_pct": _pct(totals["cash"], totals["base"]),
        "net_assets": float(totals["base"]),
    }
    return {"ok": True, "period": canon, "periods": periods,
            "funds": rows, "industry": industry, "count": len(rows)}


def _register_sectors(scripts):
    """{script: sector_name} from our own CompanyProfile register.

    The platform already has one answer for "what sector is NABIL", and a
    holdings feed should not be allowed to create a second. This matters in
    practice: the feed files CIT, HIDCL and NRN under "Organized Fund", which
    reads as a mutual fund and is actually the Investment sector. Scripts the
    register does not know fall back to whatever the feed said.
    """
    from core_analysis.models import CompanyProfile
    scripts = {s for s in scripts if s}
    if not scripts:
        return {}
    return dict(CompanyProfile.objects.filter(symbol__in=scripts)
                .values_list("symbol", "sector_name"))


# ------------------------------------------------------------------ 3. sector allocation

def sector_allocation(period=None, basis="market"):
    """Each fund's equity book split across the thirteen sector columns.

    ``basis`` picks which valuation the split is struck on:
      * ``market`` — current market value, i.e. what the book is worth now;
      * ``book``   — the published cost, i.e. what the manager paid.
    The two answer different questions and the published table offers both, so
    a sector that has since re-rated does not masquerade as a bigger bet than
    the manager actually made.

    Percentages are of the fund's OWN equity book, never of its total assets:
    otherwise two funds holding identical stocks would look different purely
    because one held more cash.
    """
    canon, periods = resolve_period(period)
    if not canon:
        return {"ok": False, "error": "No fund portfolios synced yet.",
                "period": "", "periods": [], "funds": [],
                "sectors": SECTOR_ORDER, "sector_labels": SECTOR_LABEL}

    field = "book_value" if basis == "book" else "market_value"
    holdings = list(MutualFundHolding.objects
                    .filter(portfolio__period=canon)
                    .values_list("portfolio__symbol", "script", "sector", field,
                                 "weight_percent"))

    resolved = _register_sectors({h[1] for h in holdings})

    # WHY THE SECTOR SPLIT IS COMPUTED AND NOT TAKEN FROM THE FUND'S OWN
    # PUBLISHED WEIGHTS. Each holding carries a `weight_percent` the fund
    # published, and using it looks more faithful. Measured against the
    # published sector table for HLICF Ashad 2083 it is three times WORSE:
    # total absolute error across the thirteen columns is 0.060 from the stated
    # weights against 0.018 from the cost values. The weights are rounded to two
    # decimals per line before publication, so re-aggregating them compounds
    # rounding that the underlying values do not carry — which is also why the
    # published table itself evidently computes from values. The stated weight
    # is still exact for the ONE line it describes, so it is shown per holding
    # on the financials screen, where no aggregation happens.
    by_fund = defaultdict(lambda: defaultdict(Decimal))
    invested = defaultdict(Decimal)

    for symbol, script, sector, value, weight in holdings:
        value = value or ZERO
        code = sector_code(resolved.get(script) or sector)
        by_fund[symbol][code] += value
        invested[symbol] += value

    profiles = {p.symbol: p for p in MutualFundProfile.objects.all()}
    prices = latest_prices(list(by_fund))

    rows, totals, grand = [], defaultdict(Decimal), ZERO
    for symbol, buckets in by_fund.items():
        total = invested[symbol]
        profile = profiles.get(symbol)
        close, _ = prices.get(symbol, (None, None))
        shares = {code: _pct(buckets.get(code, ZERO), total) for code in SECTOR_ORDER}

        rows.append({
            "symbol": symbol,
            "daily_nav": _f(profile.daily_nav) if profile else None,
            "monthly_nav": _f(profile.monthly_nav) if profile else None,
            "ltp": _f(close),
            "invested": float(total),
            "sectors": shares,
        })
        for code, value in buckets.items():
            totals[code] += value
        grand += total

    rows.sort(key=lambda r: -r["invested"])
    industry = {
        "symbol": "MF Inds", "daily_nav": None, "monthly_nav": None, "ltp": None,
        "invested": float(grand),
        "sectors": {code: _pct(totals.get(code, ZERO), grand) for code in SECTOR_ORDER},
    }
    return {"ok": True, "period": canon, "periods": periods, "basis": basis,
            "funds": rows, "industry": industry,
            "sectors": SECTOR_ORDER, "sector_labels": SECTOR_LABEL,
            "count": len(rows)}


# ------------------------------------------------------------------ 4. holdings by company

def company_holdings(script, limit_months=9):
    """Which funds hold one stock, month by month — the reverse of a portfolio.

    This is the question no per-fund report can answer and the interesting one
    to ask of a stock: who owns it institutionally, and are they adding or
    trimming. The month-on-month change is computed against the PREVIOUS column
    shown, so a fund that first appears has no change rather than +100%, which
    would read as a position doubling.
    """
    script = (script or "").strip().upper()
    periods = available_periods()[:limit_months]
    if not script or not periods:
        return {"ok": False, "error": "No fund portfolios synced yet." if not periods
                else "No script given.", "script": script, "periods": periods}

    rows = (MutualFundHolding.objects
            .filter(script=script, portfolio__period__in=periods)
            .values_list("portfolio__symbol", "portfolio__period", "kitta",
                         "market_value", "company_name"))

    kitta = defaultdict(dict)
    names, fund_names = {}, {}
    for symbol, period, qty, value, company in rows:
        kitta[symbol][period] = qty or ZERO
        if company:
            names[script] = company

    profiles = {p.symbol: p for p in MutualFundProfile.objects.all()}
    for symbol in kitta:
        profile = profiles.get(symbol)
        fund_names[symbol] = profile.fund_name if profile else ""

    # A gap has two meanings and they must not look alike:
    #   * the fund FILED that month and simply did not hold the stock -> 0, a
    #     real position of nothing, and the month-on-month change is -100%;
    #   * the fund filed NOTHING that month -> blank, because we do not know.
    # Collapsing both to a dash hides an exit; collapsing both to zero invents
    # one. This is the set of months each fund actually filed.
    filed = defaultdict(set)
    for symbol, period in (MutualFundPortfolio.objects
                           .filter(period__in=periods)
                           .values_list("symbol", "period")):
        filed[symbol].add(period)

    funds = []
    for symbol, by_period in kitta.items():
        cells = []
        previous = None
        # Oldest first while walking, so each change compares to the column to
        # its right in the rendered (newest-first) table.
        for period in reversed(periods):
            qty = by_period.get(period)
            if qty is None and period in filed.get(symbol, ()):
                qty = ZERO       # filed, held none: an exit, not an unknown
            change = None
            if qty is not None and previous:
                change = round((float(qty) - float(previous)) / float(previous) * 100.0, 2)
            cells.append({"period": period,
                          "kitta": _f(qty),
                          "change_pct": change})
            if qty is not None:
                previous = qty
        cells.reverse()
        funds.append({"symbol": symbol, "fund_name": fund_names.get(symbol, ""),
                      "cells": cells,
                      "latest": next((c["kitta"] for c in cells
                                      if c["kitta"] is not None), None)})

    funds.sort(key=lambda f: (f["latest"] is None, -(f["latest"] or 0)))

    totals = []
    for index, period in enumerate(periods):
        # None when NO fund has a value for the month — the filed/None
        # machinery above keeps "did not file" distinct from "held none", and
        # a total of 0 for an unknown month would undo that.
        vals = [f["cells"][index]["kitta"] for f in funds]
        known = [v for v in vals if v is not None]
        totals.append(sum(known) if known else None)

    return {"ok": True, "script": script, "company_name": names.get(script, ""),
            "periods": periods, "funds": funds, "totals": totals,
            "holder_count": len(funds)}


def held_scripts():
    """Every script any fund holds, for the company picker."""
    pairs = (MutualFundHolding.objects.order_by()
             .values_list("script", "company_name").distinct())
    best = {}
    for script, company in pairs:
        if script and (script not in best or (company and not best[script])):
            best[script] = company or ""
    return [{"script": s, "company_name": best[s]} for s in sorted(best)]


# ------------------------------------------------------------------ 5. fund financials

def fund_financials(symbol, period=None, sector=None):
    """One fund, one month: the scheme header, the balance-sheet panel, and
    every script it holds with cost, market value and unrealised P/L.

    MARKET PRICE IS DERIVED, not fed. The upstream holdings rows carry quantity,
    published (cost) amount and current market value — there is no price field.
    Market price is therefore market value / kitta, which reconciles exactly
    with the valuation P/L. The published screen shows a market price that
    matches neither its own market value nor the live quote, so it is not
    reproducible and is not reproduced.
    """
    symbol = (symbol or "").strip().upper()
    canon, periods = resolve_period(period)
    if not symbol or not canon:
        return {"ok": False, "error": "No fund portfolios synced yet." if not canon
                else "No fund given.", "periods": periods}

    # The months THIS fund filed, newest first. One query, not one per period.
    own_periods = sorted(
        MutualFundPortfolio.objects.filter(symbol=symbol)
        .values_list("period", flat=True),
        key=month_index, reverse=True)
    if not own_periods:
        return {"ok": False, "error": f"No portfolio stored for {symbol}.",
                "symbol": symbol, "period": "", "periods": []}

    # Fall back to the fund's own newest month when it did not file the one
    # asked for. Switching to a fund that files late must not dead-end on an
    # error page just because the global newest month is not one of its own.
    if canon not in own_periods:
        canon = own_periods[0]

    portfolio = MutualFundPortfolio.objects.filter(symbol=symbol, period=canon).first()

    profile = MutualFundProfile.objects.filter(symbol=symbol).first()
    close, as_of = latest_prices([symbol]).get(symbol, (None, None))

    whole = list(portfolio.holdings.all())     # evaluated once, reused below
    holdings = []
    for holding in whole:
        qty = float(holding.kitta or 0)
        cost = float(holding.book_value or 0)
        market = float(holding.market_value or 0)
        code = sector_code(holding.sector)
        if sector and sector != "ALL" and code != sector:
            continue
        holdings.append({
            "script": holding.script,
            "company_name": holding.company_name,
            "sector": code,
            "sector_label": SECTOR_LABEL.get(code, code),
            "kitta": qty,
            "cost_value": cost,
            "cost_price": round(cost / qty, 2) if qty else None,
            "market_price": round(market / qty, 2) if qty else None,
            "market_value": market,
            "valuation_pl": round(market - cost, 2),
            # The fund's OWN stated weight for this line — exact here, because
            # nothing is aggregated. None where the month was filed without it.
            "weight_pct": _f(holding.weight_percent),
        })
    holdings.sort(key=lambda h: -h["cost_value"])

    invested = sum(h["market_value"] for h in holdings)

    # The sector picker filters the rows, so "what share of the book is this?"
    # has to be measured against the WHOLE book, not against the filtered set —
    # otherwise every selection reads 100%. Recomputed unfiltered for that reason.
    whole_cost = sum(float(h.book_value or 0) for h in whole)
    shown_cost = sum(h["cost_value"] for h in holdings)
    sector_share = round(shown_cost / whole_cost * 100.0, 2) if whole_cost else None

    sectors_present = sorted(
        {sector_code(h.sector) for h in whole},
        key=lambda c: SECTOR_ORDER.index(c) if c in SECTOR_ORDER else 99)

    return {
        "ok": True,
        "symbol": symbol,
        "period": canon,
        "periods": own_periods,
        "fund_name": (profile.fund_name if profile else "") or portfolio.fund_name,
        "amc": profile.amc if profile else "",
        "fund_size": _f(profile.fund_size) if profile else None,
        "maturity_date": profile.maturity_date if profile else None,
        "daily_nav": _f(profile.daily_nav) if profile else None,
        "daily_nav_date": profile.daily_nav_date if profile else None,
        "monthly_nav": _f(portfolio.nav_monthly),
        "monthly_nav_period": canon,
        "ltp": _f(close),
        "as_of": as_of,
        "sector_share_pct": sector_share,
        "statement": _statement_panel(symbol, canon),
        "balance_sheet": {
            "equity": _f(portfolio.equity_value),
            "fixed_income": _f(portfolio.fixed_income_value),
            "cash": _f(portfolio.cash_value),
            "other": _f(portfolio.other_value),
            "net_assets": _f(portfolio.net_assets),
        },
        "holdings": holdings,
        "invested": invested,
        "invested_cost": shown_cost,
        "sectors_present": sectors_present,
        "sector_labels": SECTOR_LABEL,
        "holding_count": len(holdings),
    }


# The order each statement actually reads: investments, then current assets,
# then liabilities and accruals, then the NAV it all resolves to. The feed
# returns lines alphabetically, which drops CURRENT LIABILITIES into the middle
# of the asset lines and makes the panel unreadable as a statement.
#
# The two maps are SEPARATE because "Fund Supervisor Fee" appears in both
# statements. A shared map gave the income-statement copy the balance sheet's
# position and floated it above INCOME.
def _order(names):
    return {name: index for index, name in enumerate(names)}

_ITEM_ORDER = {
    "BS": _order([
        "INVESTMENTS",
        "Listed Securities", "Registered Equities", "IPO Investment",
        "Bank Fixed Deposits", "Corporate Debentures",
        "Government Bonds", "Other Government Securities", "Other Investments",
        "CURRENT ASSETS", "Bank Balance", "Other Current Assets",
        "CURRENT LIABILITIES",
        "Fund Management and Depository Fee", "Fund Supervisor Fee",
        "Net Asset Value (NAV) Gross", "Net Asset Value (NAV)",
        "Number of Units Outstanding", "NAV per Unit",
    ]),
    "IS": _order([
        "INCOME STATEMENT",
        "INCOME", "Realised Income", "Unrealised Income",
        "EXPENSES", "Fund Management and Depositary Fee", "Fund Supervisor Fee",
        "Audit Fee", "Notice Publication Fee", "Pre-operating Expenses",
        "Other Expenses",
        "Net Income",
    ]),
}


def _statement_panel(symbol, period):
    """The fund's own Balance Sheet and Income Statement lines, verbatim.

    Rendered exactly as filed, in filing order and at the source's own scale —
    money in thousands, per-unit and count lines in their own units. This panel
    exists so a reader can hold our page next to the fund's report and see the
    same figures; normalising them here would defeat that.
    """
    from core_analysis.models import MutualFundStatementItem

    rows = MutualFundStatementItem.objects.filter(symbol=symbol, period=period)
    # Lines that are per-unit or a count rather than money in thousands. Flagged
    # so the template can label the panel's units without lying about these two.
    per_unit = {"NAV per Unit", "Number of Units Outstanding"}

    out = {"BS": [], "IS": []}
    def rank(row):
        return (_ITEM_ORDER.get(row.fs_type, {}).get(row.item_name, 500),
                row.position, row.item_name)

    for row in sorted(rows, key=rank):
        bucket = out.get(row.fs_type)
        if bucket is None:
            continue
        bucket.append({
            "name": row.item_name,
            "amount": _f(row.amount),
            "scaled": row.item_name not in per_unit,
            # An ALL-CAPS line is a SUBTOTAL, not a bare heading: INVESTMENTS,
            # CURRENT ASSETS and CURRENT LIABILITIES all carry real figures
            # (767,514.07 / 56,878.96 / 10,314.62 for C30MF Jestha 2083).
            # Rendering them as headings dropped those three values off the
            # panel entirely, so they are flagged for emphasis only.
            "subtotal": row.item_name.isupper(),
        })
    return out


def fund_symbols():
    """Funds we hold a portfolio for, for the fund picker."""
    symbols = (MutualFundPortfolio.objects.order_by()
               .values_list("symbol", flat=True).distinct())
    profiles = dict(MutualFundProfile.objects.values_list("symbol", "fund_name"))
    return [{"symbol": s, "fund_name": profiles.get(s, "")} for s in sorted(symbols)]


# ------------------------------------------------------------------ 6. landing

def overview():
    """The headline counters on the desk's landing screen."""
    from django.db import connection

    from core_analysis.models import CompanyProfile

    as_of = price_date()
    # Counted from OUR register, not from the holdings feed: the register knows
    # 60 listed schemes where the feed carries 44, and "total funds" means every
    # fund on the board, not just the ones this one source happens to cover.
    listed = CompanyProfile.objects.filter(sector_name="Mutual Fund")
    total = listed.count()
    active = listed.filter(status="Active").count()

    traded = turnover = 0
    if as_of:
        with connection.cursor() as cursor:
            # TURNOVER IS VALUE, NOT QUANTITY. Summing quantity gave 1,523,722
            # against a published 16,800,852 — out by an order of magnitude,
            # and quietly plausible, which is what made it worth checking.
            cursor.execute(
                "SELECT COUNT(*), COALESCE(SUM(p.total_traded_value), 0) "
                "FROM nepse_daily_stock_prices p "
                "JOIN nepse_company_profiles c ON c.symbol = p.symbol "
                "WHERE p.business_date = %s AND c.sector_name = 'Mutual Fund' "
                "AND p.total_traded_quantity > 0", [as_of])
            traded, turnover = cursor.fetchone()

    periods = available_periods()
    return {
        "total_funds": total,
        "active_funds": active,
        "funds_traded": traded,
        "total_turnover": float(turnover or 0),
        "price_date": as_of,
        "periods": periods,
        "latest_period": periods[0] if periods else "",
        "covered_funds": MutualFundProfile.objects.count(),
        "fund_months": MutualFundPortfolio.objects.count(),
        "holdings": MutualFundHolding.objects.count(),
    }
