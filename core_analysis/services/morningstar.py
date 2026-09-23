"""
morningstar.py — Morning Star sector scoring (Growth / Value / Quality).

Implements the final 13-sector parameter framework (docs/
morningstar_q4_parameters.md): every parameter is scored as a PERCENTILE RANK
within its own sector (0-100, direction-aware), weighted per the sector spec,
with missing factors renormalised (never zeroed) and a "n of m factors"
confidence tag. The Balance Sheet / Quality pillar is a MODIFIER, not a third
weighted score: hard gates cap the star rating, soft flags subtract points,
everything else is context.

Data source is the quarterly fundamentals feed (FinancialStatement BS/IS/KS
rows) at the sector's coverage-aware best period (industry.best_period), the
prior fiscal year's same quarter for growth, and — for Life Insurance only —
the hand-entered LifeInsuranceIndicator rows (bonus rate, solvency). Mutual
funds file no statements, so their scores come from the NAV table instead.

Growth compares YTD vs prior-year YTD (Nepali quarterly figures are cumulative,
so same-quarter rows ARE the YTD comparison); QoQ is never used.
"""
from __future__ import annotations

import logging
from collections import defaultdict

from django.core.cache import cache

logger = logging.getLogger(__name__)

CACHE_TTL = 600
SCAN_VERSION = 11

# A company with no close within this many calendar days of the newest stored
# business date is treated as no longer trading and is left out of the scan.
# The gap distribution is sharply bimodal — live names trade within days, dead
# ones are months stale — so the exact figure is not delicate.
STALE_DAYS = 30

# Ordinary shares are issued at Rs 100 par in Nepal; SHL (Rs 10) and HATHY
# (Rs 50) are the known exceptions. Book value below par means accumulated
# losses have eaten into the capital shareholders paid in.
PAR_VALUE = 100.0
PAR_EXCEPTIONS = {"SHL": 10.0, "HATHY": 50.0}


def _par_value(ticker):
    return PAR_EXCEPTIONS.get(ticker, PAR_VALUE)

# Combined rating mix. Value-dominant sectors have no real growth engine.
GV_MIX_DEFAULT = (0.60, 0.40)
GV_MIX_VALUE_DOMINANT = (0.30, 0.70)
VALUE_DOMINANT_SECTORS = {"Tradings", "Investment", "Mutual Fund"}

# Style box: Growth − Value percentile spread.
STYLE_SPREAD = 15.0
# Size tiers by cumulative sector market-cap share.
SIZE_LARGE_SHARE = 0.70
SIZE_MID_SHARE = 0.90

MILLION = 1_000_000.0


# ── Row access helpers ─────────────────────────────────────────────────────────
# Item codes are prefixed per sector (cb_/db_/fin_/mf_/li_/...), so extractors
# match on the code's descriptive FRAGMENT, which is stable across sectors.

def _find(d, *frags):
    """First amount in dict {item_code: amount} whose code contains any frag."""
    if not d:
        return None
    for frag in frags:
        for code, amt in d.items():
            if frag in code and amt is not None:
                return amt
    return None


def _pos(v):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _growth(cur, prev):
    """YoY growth fraction; None when the base is missing or non-positive."""
    cur, prev = _num(cur), _num(prev)
    if cur is None or prev is None or prev <= 0:
        return None
    return cur / prev - 1.0


def _ratio(a, b):
    a, b = _num(a), _pos(b)
    if a is None or b is None:
        return None
    return a / b


# ── Per-ticker data bundle ─────────────────────────────────────────────────────

class T:
    """One ticker's statements for the current and previous fiscal year."""
    __slots__ = ("ticker", "cur", "prev", "ks", "ksp", "extra")

    def __init__(self, ticker):
        self.ticker = ticker
        self.cur = {"IS": {}, "BS": {}, "KS": {}}
        self.prev = {"IS": {}, "BS": {}, "KS": {}}
        self.extra = {}          # manual indicators (life), NAV rows (funds)

    # current / previous accessors by statement + fragment
    def c(self, fs, *frags):
        return _find(self.cur.get(fs), *frags)

    def p(self, fs, *frags):
        return _find(self.prev.get(fs), *frags)

    def g(self, fs, *frags):
        """YoY growth of one line item."""
        return _growth(self.c(fs, *frags), self.p(fs, *frags))

    # canonical KS shortcuts
    def price(self):
        """Latest traded close, falling back to the price filed with the report.

        The KS block files the share price as it stood on the reporting date.
        By the time a sector is scored that is months old — on 2026-09-09 the
        filed price was 33% above the market for ANLB and 19% for GMLI — and
        Value is ranked as a cross-section, so one stale price drags every other
        company's percentile with it. `_attach_live_prices` fills `live_price`
        for anything that trades; anything that does not keeps the filed figure.
        """
        return _pos(self.extra.get("live_price")) or _pos(self.c("KS", "market_value_per_share"))

    def eps(self):
        return _num(self.c("KS", "eps_an"))

    def pe(self):
        """P/E on the live price, or the filed one where there is no live price.

        Recomputing is not an invention: the filed PE equals the filed price
        divided by the filed EPS for every company checked across four sectors,
        so re-dividing by the same EPS keeps the source's own method. Without
        this the price row and the P/E row would disagree.
        """
        live, eps = _pos(self.extra.get("live_price")), _num(self.c("KS", "eps_an"))
        if live is not None and eps:
            return _pos(live / eps)
        return _pos(self.c("KS", "reported_pe"))

    def bvps(self):
        return _pos(self.c("KS", "book_value_per_share"))

    def pb(self):
        return _ratio(self.price(), self.bvps())

    def roe(self):
        return _num(self.c("KS", "return_on_equity"))

    def roa(self):
        return _num(self.c("KS", "return_on_asset"))

    def dps(self):
        return _num(self.c("KS", "dividend_per_share"))

    def shares(self):
        """Shares outstanding in '000. Exchange first, KS block as the fallback.

        The KS share count is simply wrong for a handful of companies: NICA
        files 17,394 ('000) against 149.2 million actually listed, an eightfold
        understatement that sized one of the largest banks in the country as
        Small. MBL, SANIMA and KSBBL are 9% to 26% out, usually a bonus the feed
        has not caught up with. The exchange publishes a capitalisation and a
        close on the same day, and their quotient is the listed count, so that
        is the authority. Par value never enters this: SHL at Rs 10 and HATHY at
        Rs 50 are handled by the same arithmetic as a Rs 100 company, because
        nothing here divides capital by a face value.
        """
        ex = _pos(self.extra.get("eod_shares"))
        if ex is not None:
            return ex / 1000.0
        return _pos(self.c("KS", "outstanding_shares"))

    def mcap(self):
        """Rupees: the live price on the exchange's own share count.

        Deliberately not the exchange's capitalisation column, which is stale by
        design (it reads zero for the newest sessions and is backfilled later).
        Taking its share count and re-pricing at today's close keeps the count
        right and the price current. Falls back to the stored capitalisation,
        then to the KS block, so a company the exchange has never priced still
        gets a tier rather than dropping off the size chart.
        """
        p, s = self.price(), self.shares()
        if p is not None and s is not None:
            return p * s * 1000.0
        return _pos(self.extra.get("eod_mcap"))


# ── Sector parameter specs ─────────────────────────────────────────────────────
# Each parameter: (key, label, weight, higher_is_better, fn(T) -> float|None).
# Growth fns return FRACTIONS (0.12 = +12%); value fns return the metric itself.

def _bank_specs():
    growth = [
        ("nii_g", "NII growth", 25, True, lambda t: t.g("IS", "net_interest_income")),
        ("loan_g", "Loan growth", 25, True, lambda t: t.g("BS", "loans_and_advances_to_customers", "_loans")),
        ("dist_g", "Distributable profit growth", 20, True, lambda t: t.g("IS", "distributable_profit")),
        ("dep_g", "Deposit growth", 15, True, lambda t: t.g("BS", "deposits_from_customers", "_deposits")),
        ("fee_g", "Fee income growth", 15, True, lambda t: t.g("IS", "net_fees_and_commission")),
    ]
    value = [
        ("pb_roe", "P/B vs ROE", 30, True,
         lambda t: _ratio(t.roe(), t.pb())),                       # ROE per unit of P/B — higher = cheaper for its quality
        ("dpsy", "Distributable PS / price", 25, True,
         lambda t: _ratio(_ratio(t.c("IS", "distributable_profit"), t.shares()), t.price())),
        ("pe", "P/E", 15, False, lambda t: t.pe()),
        ("nim", "Net interest spread", 15, True, lambda t: _num(t.c("KS", "net_interest_spread"))),
        ("npl_tr", "NPL trend (fall = better)", 15, False,
         lambda t: _delta(t.c("KS", "npl_to_total", "non_performing"), t.p("KS", "npl_to_total", "non_performing"))),
    ]
    return growth, value


def _delta(cur, prev):
    """Absolute change (percentage-point move for fraction metrics)."""
    cur, prev = _num(cur), _num(prev)
    if cur is None or prev is None:
        return None
    return cur - prev


def _finance_specs():
    growth = [
        ("nii_g", "NII growth", 35, True, lambda t: t.g("IS", "net_interest_income")),
        ("loan_g", "Loan growth", 30, True, lambda t: t.g("BS", "loans_and_advances_to_customers", "_loans", "loan_and_advances")),
        ("dist_g", "Distributable profit growth", 20, True, lambda t: t.g("IS", "distributable_profit")),
        ("dep_g", "Deposit/funding growth", 15, True, lambda t: t.g("BS", "deposits_from_customers", "_deposits")),
    ]
    value = [
        ("pb_roe", "P/B vs ROE", 30, True, lambda t: _ratio(t.roe(), t.pb())),
        ("dpsy", "Distributable PS / price", 25, True,
         lambda t: _ratio(_ratio(t.c("IS", "distributable_profit"), t.shares()), t.price())),
        ("pe", "P/E", 20, False, lambda t: t.pe()),
        ("npl_tr", "NPL trend (fall = better)", 15, False,
         lambda t: _delta(t.c("KS", "npl_to_total", "non_performing"), t.p("KS", "npl_to_total", "non_performing"))),
        ("roa", "ROA", 10, True, lambda t: t.roa()),
    ]
    return growth, value


def _micro_specs():
    growth = [
        ("loan_g", "Loan portfolio growth", 40, True, lambda t: t.g("BS", "loan_and_advances", "_loans")),
        ("nii_g", "NII growth", 35, True, lambda t: t.g("IS", "net_interest_income")),
        ("cof_impr", "Borrowing-cost improvement", 25, False,
         lambda t: _delta(t.c("KS", "cost_of_funds"), t.p("KS", "cost_of_funds"))),
    ]
    value = [
        ("pb", "P/B", 30, False, lambda t: t.pb()),
        ("dpsy", "Accumulated reserves PS / price", 30, True,
         # Microfinance BS has no retained-earnings line — accumulated
         # distributable profit sits in "Reserves and Surplus" (mf_bs_120).
         lambda t: _ratio(_ratio(t.c("BS", "reserves_and_surplus"), t.shares()), t.price())),
        ("pe", "P/E", 25, False, lambda t: t.pe()),
        ("npl_tr", "NPL trend (fall = better)", 15, False,
         lambda t: _delta(t.c("KS", "npl_to_total", "non_performing"), t.p("KS", "npl_to_total", "non_performing"))),
    ]
    return growth, value


def _life_specs():
    growth = [
        ("fyp_g", "First-year premium growth", 35, True, lambda t: t.g("KS", "first_year_premium")),
        ("ren_g", "Renewal premium growth", 20, True, lambda t: t.g("KS", "renewal_premium")),
        ("pif_g", "Policies-in-force growth", 15, True, lambda t: t.g("KS", "enforced_policy")),
        ("fund_g", "Life fund growth", 15, True,
         lambda t: t.g("BS", "gross_insurance_contract_liabilities")),
        ("inv_g", "Investment income growth", 15, True,
         lambda t: t.g("IS", "income_from_investments_and_loans")),
    ]
    value = [
        ("mc_gp", "MCap / gross premium", 35, False,
         lambda t: _ratio(t.mcap(), _pos(t.c("IS", "gross_earned_premiums")) and _pos(t.c("IS", "gross_earned_premiums")) * 1000.0)),
        ("pb", "P/B", 25, False, lambda t: t.pb()),
        ("bonus", "Declared bonus rate", 25, True, lambda t: _num(t.extra.get("bonus_rate"))),
        ("inv_y", "Investment yield", 15, True,
         lambda t: _ratio(t.c("IS", "income_from_investments_and_loans"), t.c("BS", "_investments"))),
    ]
    return growth, value


def _nonlife_specs():
    def _net_earned(t):
        return _pos(t.c("IS", "net_earned_premiums"))

    def _combined(t):
        ne = _net_earned(t)
        te = _num(t.c("IS", "total_expenses"))
        fc = _num(t.c("IS", "finance_cost")) or 0.0
        if ne is None or te is None:
            return None
        return (te - fc) / ne

    def _claim_ratio(d):
        ne = _pos(_find(d.get("IS"), "net_earned_premiums"))
        cl = _num(_find(d.get("IS"), "net_benefits_and_claims"))
        if ne is None or cl is None:
            return None
        return cl / ne

    def _claim_vs_prior(t):
        cur = _claim_ratio(t.cur)
        prior = _claim_ratio(t.prev)
        if cur is None or prior is None or prior <= 0:
            return None
        return cur / prior       # >1 = worse than its own history

    growth = [
        ("gp_g", "Gross premium growth", 35, True, lambda t: t.g("IS", "gross_earned_premiums")),
        ("pol_g", "Policy count growth", 25, True, lambda t: t.g("KS", "total_issued_policy_count")),
        ("gwp_g", "Written premium growth", 15, True, lambda t: t.g("KS", "gross_written_premium")),
        ("inv_g", "Investment income growth", 15, True,
         lambda t: t.g("IS", "income_from_investments_and_loans")),
        ("ren_g", "Renewed policy growth", 10, True, lambda t: t.g("KS", "total_renewed_policy_count")),
    ]
    value = [
        ("combined", "Combined ratio", 35, False, _combined),
        ("claim_tr", "Claim ratio vs own prior year", 25, False, _claim_vs_prior),
        ("pb", "P/B", 25, False, lambda t: t.pb()),
        ("retention", "Retention ratio", 15, True,
         lambda t: _ratio(t.c("IS", "net_earned_premiums"), t.c("IS", "gross_earned_premiums"))),
    ]
    return growth, value


def _hydro_specs():
    def _core_profit(d):
        np_ = _num(_find(d.get("IS"), "net_profit"))
        if np_ is None:
            return None
        other = sum(
            v for v in (
                _num(_find(d.get("IS"), "income_from_other_sources")),
                _num(_find(d.get("IS"), "forex_gain")),
                _num(_find(d.get("IS"), "income_from_dividend")),
            ) if v is not None
        )
        return np_ - other

    def _int_cov(t):
        op = _num(t.c("IS", "operating_profit"))
        ni = _num(t.c("IS", "net_interest_income"))
        if op is None or ni is None or ni == 0:
            return None
        return op / abs(ni)

    def _de(t):
        debt = _num(t.c("BS", "long_term_liabilities"))
        eq = sum(v for v in (
            _num(t.c("BS", "paid_up_capital")),
            _num(t.c("BS", "share_premium")),
            _num(t.c("BS", "_reserves")),
        ) if v is not None)
        if debt is None or eq <= 0:
            return None
        return debt / eq

    growth = [
        ("rev_g", "Core revenue growth (energy sales)", 35, True,
         lambda t: t.g("IS", "income_from_sale_of_energy")),
        ("core_g", "Core profit growth (ex one-offs)", 35, True,
         lambda t: _growth(_core_profit(t.cur), _core_profit(t.prev))),
        ("fc_impr", "Finance-cost improvement", 15, False,
         lambda t: _growth(abs(_num(t.c("IS", "net_interest_income")) or 0) or None,
                           abs(_num(t.p("IS", "net_interest_income")) or 0) or None)),
        ("cwip_g", "Project progress (CWIP growth)", 15, True, lambda t: t.g("BS", "work_in_progress")),
    ]
    value = [
        ("core_pe", "Core P/E", 30, False,
         lambda t: _ratio(t.mcap(), _pos(_core_profit(t.cur)) and _pos(_core_profit(t.cur)) * 1000.0)),
        ("pb", "P/B", 25, False, lambda t: t.pb()),
        ("int_cov", "Interest coverage", 20, True, _int_cov),
        ("de", "Debt / equity", 15, False, _de),
        ("roa", "ROA", 10, True, lambda t: t.roa()),
    ]
    return growth, value


def _mfg_specs():
    def _roce(t):
        op = _num(t.c("IS", "operating_profit"))
        cap = sum(v for v in (
            _num(t.c("BS", "total_equity")),
            _num(t.c("BS", "secured_loan")),
            _num(t.c("BS", "unsecured_loan")),
            _num(t.c("BS", "_borrowings")),
        ) if v is not None)
        if op is None or cap <= 0:
            return None
        return op / cap

    def _gm(d):
        gp = _num(_find(d.get("KS"), "gross_profit"))
        rev = _pos(_find(d.get("KS"), "total_revenue"))
        if gp is None or rev is None:
            return None
        return gp / rev

    growth = [
        ("rev_g", "Revenue growth", 40, True, lambda t: t.g("IS", "sales_less_return", "total_income")),
        ("np_g", "Net profit growth", 35, True, lambda t: t.g("IS", "net_profit")),
        ("gm_tr", "Gross-margin trend", 25, True, lambda t: _delta(_gm(t.cur), _gm(t.prev))),
    ]
    value = [
        ("pe", "P/E", 30, False, lambda t: t.pe()),
        ("roce", "ROCE", 30, True, _roce),
        ("pb", "P/B", 20, False, lambda t: t.pb()),
        ("margin", "Net margin vs sector", 20, True,
         lambda t: _ratio(t.c("KS", "net_income"), t.c("KS", "total_revenue"))),
    ]
    return growth, value


def _hotel_specs():
    def _op_lev(t):
        rg = t.g("IS", "total_income")
        og = t.g("IS", "gross_operating_profit")
        if rg is None or og is None:
            return None
        return og - rg

    def _ev_op(t):
        mc = t.mcap()
        op = _pos(t.c("IS", "gross_operating_profit"))
        if mc is None or op is None:
            return None
        debt = sum(v for v in (
            _num(t.c("BS", "medium_and_long_term_loans")),
            _num(t.c("BS", "short_term_loans")),
        ) if v is not None)
        cash = _num(t.c("BS", "cash_and_bank")) or 0.0
        return (mc + (debt - cash) * 1000.0) / (op * 1000.0)

    growth = [
        ("rev_g", "Revenue growth", 50, True, lambda t: t.g("IS", "total_income")),
        ("op_lev", "Operating leverage (op-profit vs revenue)", 50, True, _op_lev),
    ]
    value = [
        ("pb", "P/B", 45, False, lambda t: t.pb()),
        ("ev_op", "EV / op profit before dep.", 35, False, _ev_op),
        ("pe", "P/E", 20, False, lambda t: t.pe()),
    ]
    return growth, value


def _trading_specs():
    def _nca(t):
        ca = _num(t.c("BS", "total_current_assets"))
        cl = _num(t.c("BS", "total_current_liabilities"))
        ncl = _num(t.c("BS", "total_non_current_liabilities")) or 0.0
        if ca is None or cl is None:
            return None
        return ca - cl - ncl

    growth = [
        ("rev_g", "Sales/income growth", 60, True,
         lambda t: t.g("IS", "total_income_from_operations", "revenue_from_operations")),
        ("np_g", "Profit growth", 40, True, lambda t: t.g("IS", "net_profit")),
    ]
    value = [
        ("pb", "P/B", 40, False, lambda t: t.pb()),
        ("nca_mc", "Net current assets / market cap", 30, True,
         lambda t: _ratio(_pos(_nca(t)) and _pos(_nca(t)) * 1000.0, t.mcap())),
        ("pe", "P/E", 20, False, lambda t: t.pe()),
        ("res", "Reserves / paid-up", 10, True,
         lambda t: _ratio(t.c("BS", "reserve_and_surplus"), t.c("BS", "share_capital"))),
    ]
    return growth, value


def _investment_specs():
    growth = [
        ("inc_g", "Income growth", 60, True,
         lambda t: t.g("IS", "total_income", "total_operating_income")),
        ("di_g", "Dividend & interest income growth", 40, True,
         lambda t: _growth(
             sum(v for v in (_num(t.c("IS", "interest_income")), _num(t.c("IS", "dividend_income"))) if v is not None) or None,
             sum(v for v in (_num(t.p("IS", "interest_income")), _num(t.p("IS", "dividend_income"))) if v is not None) or None)),
    ]
    value = [
        ("pb", "P/B (NAV discount)", 60, False, lambda t: t.pb()),
        ("dy", "Dividend yield", 40, True, lambda t: _ratio(t.dps(), t.price())),
    ]
    return growth, value


def _others_specs():
    growth = [
        ("rev_g", "Revenue growth", 50, True, lambda t: t.g("KS", "total_revenue")),
        ("np_g", "Profit growth", 50, True, lambda t: t.g("KS", "net_income")),
    ]
    value = [
        ("pe", "P/E", 50, False, lambda t: t.pe()),
        ("pb", "P/B", 50, False, lambda t: t.pb()),
    ]
    return growth, value


SECTOR_SPECS = {
    "Commercial Banks": _bank_specs,
    "Development Banks": _bank_specs,
    "Finance": _finance_specs,
    "Microfinance": _micro_specs,
    "Life Insurance": _life_specs,
    "Non Life Insurance": _nonlife_specs,
    "Hydro Power": _hydro_specs,
    "Manufacturing And Processing": _mfg_specs,
    "Hotels And Tourism": _hotel_specs,
    "Tradings": _trading_specs,
    "Investment": _investment_specs,
    "Others": _others_specs,
}


# ── Quality: hard gates + soft flags per ticker ────────────────────────────────

def _quality(sector, t):
    """Returns (gates, flags): lists of human-readable strings. Gates cap stars,
    flags subtract 5 combined points each (max 3 counted)."""
    gates, flags = [], []

    eps = t.eps()
    if eps is not None and eps < 0:
        gates.append("Negative EPS")

    npl = _num(t.c("KS", "npl_to_total", "non_performing"))
    if npl is not None and npl > 0.04:
        gates.append(f"NPL {npl * 100:.1f}% > 4%")

    # Book value below par means accumulated losses have eaten into paid-up
    # capital, and percentile scoring rewards exactly that twice over: a wrecked
    # balance sheet flatters P/B on the Value side, and a near-zero base
    # flatters every growth rate. 41% of hydros sit below par; a handful of
    # finance, insurance, hotel and development-bank names do too. Gate it in
    # every sector so neither pillar can carry such a company, and so the
    # quadrant map cannot call it High Growth or High Value.
    bv = _num(t.c("KS", "book_value_per_share"))
    if bv is not None:
        par = _par_value(t.ticker)
        if bv <= 0:
            gates.append(f"Negative book value (Rs {bv:,.1f} per share)")
        elif bv < par:
            gates.append(f"Book value Rs {bv:,.1f} below Rs {par:,.0f} par")

    if sector in ("Commercial Banks", "Development Banks", "Finance", "Microfinance"):
        car = _num(t.c("KS", "capital_fund_to_rwa"))
        floor = 0.08 if sector == "Microfinance" else 0.11
        if car is not None and car < floor:
            gates.append(f"Capital fund {car * 100:.1f}% below regulatory floor")
        prov = _num(t.c("KS", "loan_loss_provision_to_total_npl"))
        prov_p = _num(t.p("KS", "loan_loss_provision_to_total_npl"))
        if prov is not None and prov_p is not None and prov < prov_p * 0.85:
            flags.append("Provision coverage falling")
        if npl is not None:
            npl_p = _num(t.p("KS", "npl_to_total", "non_performing"))
            if npl_p is not None and npl > npl_p * 1.25 and npl > 0.02:
                flags.append("NPL rising sharply")

    if sector == "Life Insurance":
        sol = _num(t.extra.get("solvency"))
        if sol is not None and sol < 1.0:
            gates.append(f"Solvency {sol:.2f} below 1.0 floor")

    if sector == "Hydro Power":
        op = _num(t.c("IS", "operating_profit"))
        ni = _num(t.c("IS", "net_interest_income"))
        if op is not None and ni not in (None, 0) and op / abs(ni) < 1.5:
            gates.append("Interest coverage < 1.5x")
        pbt = _num(t.c("IS", "profit_before_taxes"))
        other = sum(v for v in (
            _num(t.c("IS", "income_from_other_sources")),
            _num(t.c("IS", "forex_gain")),
            _num(t.c("IS", "income_from_dividend")),
        ) if v is not None)
        if pbt is not None and pbt > 0 and other > pbt * 0.30:
            gates.append("One-time/side income > 30% of PBT")

    if sector == "Manufacturing And Processing":
        nonrec = _num(t.c("IS", "non_recurring"))
        pbt = _num(t.c("IS", "profit_before_tax"))
        if nonrec is not None and pbt is not None and pbt > 0 and abs(nonrec) > pbt * 0.30:
            gates.append("Non-recurring items > 30% of PBT")

    if sector in ("Manufacturing And Processing", "Tradings", "Hotels And Tourism", "Others"):
        inv_g = t.g("BS", "inventories", "_inventory")
        rev_g = t.g("IS", "sales_less_return", "total_income_from_operations", "total_income")
        if inv_g is not None and rev_g is not None and inv_g - rev_g > 0.15:
            flags.append("Inventory growing much faster than revenue")
        rec_g = t.g("BS", "trade_and_other_receivables", "trade_receivables", "sundry_debtors", "receivables")
        if rec_g is not None and rev_g is not None and rec_g - rev_g > 0.15:
            flags.append("Receivables outrunning revenue")
        ca = _num(t.c("BS", "total_current_assets"))
        cl = _num(t.c("BS", "total_current_liabilities", "total_short_term_liabilities"))
        if ca is not None and _pos(cl) is not None and ca / cl < 1.0:
            flags.append("Current ratio below 1")

    if sector in ("Life Insurance", "Non Life Insurance"):
        fv = _num(t.c("IS", "fair_value_changes"))
        pbt = _num(t.c("IS", "profit_before_tax"))
        if fv is not None and pbt is not None and pbt > 0 and fv > pbt * 0.30:
            flags.append("Fair-value gains > 30% of PBT")

    return gates, flags


# ── Percentile scoring ─────────────────────────────────────────────────────────

def _percentiles(values_by_ticker, higher_better):
    """Direction-aware percentile rank (share of OTHER peers beaten, ties = 50%
    of the tie group). {ticker: raw} -> {ticker: 0-100}."""
    items = [(tk, v) for tk, v in values_by_ticker.items() if v is not None]
    n = len(items)
    if n == 0:
        return {}
    if n == 1:
        return {items[0][0]: 50.0}
    out = {}
    for tk, v in items:
        better = sum(1 for _, o in items if (o < v if higher_better else o > v))
        ties = sum(1 for tk2, o in items if o == v and tk2 != tk)
        out[tk] = (better + ties * 0.5) / (n - 1) * 100.0
    return out


def _pillar(tickers, specs):
    """Score one pillar. Returns ({ticker: score}, {ticker: factor detail},
    total_weight, factor_count)."""
    per_factor_pct = {}
    raw_by_factor = {}
    for key, label, w, hib, fn in specs:
        raw = {}
        for tk, t in tickers.items():
            try:
                raw[tk] = fn(t)
            except Exception:
                raw[tk] = None
        raw_by_factor[key] = raw
        per_factor_pct[key] = _percentiles(raw, hib)

    scores, details = {}, {}
    for tk in tickers:
        num = den = 0.0
        present = 0
        rows = []
        for key, label, w, hib, _fn in specs:
            pct = per_factor_pct[key].get(tk)
            raw = raw_by_factor[key].get(tk)
            rows.append({
                "label": label, "weight": w,
                "raw": round(raw, 4) if isinstance(raw, float) else raw,
                "pct": round(pct, 1) if pct is not None else None,
            })
            if pct is not None:
                num += pct * w
                den += w
                present += 1
        scores[tk] = (num / den) if den else None
        details[tk] = {"factors": rows, "present": present, "total": len(specs)}
    return scores, details


# ── Data loading ───────────────────────────────────────────────────────────────

def _load_tickers(sector, fy, quarter, prev_fy):
    from core_analysis.models import FinancialStatement as FS
    from core_analysis.fundamental_views import _is_aggregate_ticker

    rows = FS.objects.filter(
        sector=sector, quarter=quarter, fiscal_year_ad__in=[fy, prev_fy],
    ).values("ticker", "fiscal_year_ad", "fs_type", "item_code", "amount")

    tickers = {}
    for r in rows:
        tk = r["ticker"]
        if _is_aggregate_ticker(tk):
            continue
        t = tickers.get(tk)
        if t is None:
            t = tickers[tk] = T(tk)
        bucket = t.cur if r["fiscal_year_ad"] == fy else t.prev
        fs = r["fs_type"]
        if fs in bucket:
            code = (r["item_code"] or "").lower()
            bucket[fs][code] = float(r["amount"]) if r["amount"] is not None else None

    # Companies with no current-FY rows can't be scored — drop shells that only
    # have prior-year data.
    return {tk: t for tk, t in tickers.items() if any(t.cur.values())}


def _drop_inactive(tickers):
    """Remove companies that are no longer trading. Returns (dropped, detail).

    Two independent tests, because neither is sufficient on its own:

      * `CompanyProfile.status` catches names the feed has marked Delisted,
        Suspended or Inactive. It misses recent suspensions — SFCL still reads
        "Active" months after its last trade — and some dead tickers have no
        profile row at all.
      * Trading recency catches those. A company with no close within
        STALE_DAYS of the newest business date in the store is not trading,
        whatever the profile says.

    Mutates `tickers` in place.
    """
    from django.db.models import Max
    from core_analysis.models import CompanyProfile, NepseDailyStockPrice as P

    if not tickers:
        return 0, {}

    symbols = list(tickers)
    status = dict(CompanyProfile.objects.filter(symbol__in=symbols)
                  .values_list("symbol", "status"))
    newest = P.objects.aggregate(d=Max("business_date"))["d"]
    last = dict(P.objects.filter(symbol__in=symbols)
                .values("symbol").annotate(d=Max("business_date"))
                .values_list("symbol", "d"))

    detail = {}
    for tk in symbols:
        st = (status.get(tk) or "").strip()
        if st and st.lower() != "active":
            detail[tk] = st
            continue
        d = last.get(tk)
        if d is None:
            detail[tk] = "no price history"
        elif newest and (newest - d).days > STALE_DAYS:
            detail[tk] = f"no trade in {(newest - d).days} days"

    for tk in detail:
        tickers.pop(tk, None)
    return len(detail), detail


def _attach_live_prices(tickers):
    """Fill each company's `live_price` from the EOD store. Returns (as_of, n).

    Shares the accessor with the Industry Analysis tab so both desks quote the
    same price for the same company on the same day. A company with no stored
    close keeps the price it filed, and is counted out of `n`.
    """
    from core_analysis.services.industry import latest_prices

    prices, as_of = latest_prices(list(tickers))
    for tk, px in prices.items():
        t = tickers.get(tk)
        if t is not None:
            t.extra["live_price"] = px
    return (as_of.isoformat() if as_of else ""), len(prices)


def _attach_life_extras(tickers, fy, quarter):
    from core_analysis.models import LifeInsuranceIndicator
    import re as _re
    for row in LifeInsuranceIndicator.objects.filter(
        ticker__in=list(tickers), fiscal_year_ad=fy, quarter=quarter,
    ).values("ticker", "declared_bonus_rate", "solvency_margin_ratio"):
        t = tickers.get(row["ticker"])
        if not t:
            continue
        # bonus rate arrives as printed ("Rs. 55 - Rs. 85 Per Thousand") — take
        # the first number as the comparable figure.
        raw = row["declared_bonus_rate"] or ""
        m = _re.search(r"(\d+(?:\.\d+)?)", str(raw))
        if m:
            t.extra["bonus_rate"] = float(m.group(1))
        if row["solvency_margin_ratio"] is not None:
            t.extra["solvency"] = float(row["solvency_margin_ratio"])


# ── Mutual funds (NAV-based, no statements) ────────────────────────────────────

def _mutual_fund_rows():
    """Score funds on NAV total return (growth) and discount to NAV (value)."""
    from core_analysis.models import MutualFundNav

    hist = defaultdict(list)
    for r in MutualFundNav.objects.order_by("symbol", "id").values(
            "symbol", "fund_name", "nav_period", "nav_monthly", "premium_discount_pct",
            "fund_size"):
        hist[r["symbol"]].append(r)

    growth_raw, value_raw, names, fund_sizes = {}, {}, {}, {}
    for sym, rows in hist.items():
        names[sym] = rows[-1]["fund_name"] or sym
        fund_sizes[sym] = _pos(rows[-1]["fund_size"])
        navs = [(_num(r["nav_monthly"])) for r in rows if r["nav_monthly"] is not None]
        if len(navs) >= 2 and navs[0]:
            growth_raw[sym] = navs[-1] / navs[0] - 1.0
        pd = _num(rows[-1]["premium_discount_pct"])
        value_raw[sym] = pd    # more negative = deeper discount = better value

    g_pct = _percentiles(growth_raw, True)
    v_pct = _percentiles(value_raw, False)
    # Funds have no market cap in the usual sense — tier them by fund size
    # (scheme assets), which the NAV feed publishes.
    sizes = _size_segments(fund_sizes)

    gw, vw = GV_MIX_VALUE_DOMINANT
    results = []
    for sym in sorted(set(list(growth_raw) + list(value_raw))):
        g, v = g_pct.get(sym), v_pct.get(sym)
        combined = None
        if g is not None and v is not None:
            combined = g * gw + v * vw
        elif v is not None:
            combined = v
        elif g is not None:
            combined = g
        results.append({
            "ticker": sym, "name": names.get(sym, sym),
            "growth": round(g, 1) if g is not None else None,
            "value": round(v, 1) if v is not None else None,
            "combined": round(combined, 1) if combined is not None else None,
            "stars": None, "style": _style(g, v), "size": sizes.get(sym, "—"),
            "confidence": f"{(g is not None) + (v is not None)} of 2 factors",
            "gates": [], "flags": [],
            "detail": {
                "growth": {"factors": [{"label": "NAV growth over recorded history", "weight": 70,
                                        "raw": round(growth_raw.get(sym), 4) if growth_raw.get(sym) is not None else None,
                                        "pct": round(g, 1) if g is not None else None}],
                           "present": int(g is not None), "total": 1},
                "value": {"factors": [{"label": "Premium/discount to NAV (%)", "weight": 75,
                                       "raw": value_raw.get(sym),
                                       "pct": round(v, 1) if v is not None else None}],
                          "present": int(v is not None), "total": 1},
            },
        })
    for r in results:
        r["stars"] = _stars_from_combined(r["combined"], [], [])
        r.setdefault("gates", [])
        r.setdefault("flags", [])
    mf_g, mf_v = _assign_quadrants(results)
    results.sort(key=lambda r: -(r["combined"] or 0))
    return {
        "ok": True, "sector": "Mutual Fund", "period": "NAV history (all recorded months)",
        "quadrant_split": {"growth": mf_g, "value": mf_v},
        "note": ("Funds file no quarterly statements — scored on NAV total return over the "
                 "recorded history and the current premium/discount to NAV. Expense drag is "
                 "not published, so Value is the discount alone."),
        "mix": {"growth": 30, "value": 70},
        "results": results,
    }


# ── Assembly ───────────────────────────────────────────────────────────────────

def _style(g, v):
    if g is None or v is None:
        return "—"
    spread = g - v
    if spread >= STYLE_SPREAD:
        return "Growth"
    if spread <= -STYLE_SPREAD:
        return "Value"
    return "Blend"


def _stars_from_combined(combined, gates, flags):
    if combined is None:
        return None
    adj = combined - min(len(flags), 3) * 5.0
    if adj >= 80:
        stars = 5
    elif adj >= 60:
        stars = 4
    elif adj >= 40:
        stars = 3
    elif adj >= 20:
        stars = 2
    else:
        stars = 1
    if gates:
        stars = min(stars, 2)
    return stars


def _assign_quadrants(results):
    """Tag each row High/Low on Growth and Value, split at the sector median.

    A company failing a quality gate is barred from either High half however
    it scored. The gates exist because the score cannot be trusted for that
    company — a hydro trading below par posts huge growth rates off a wrecked
    base and a flattering P/B off the same wreckage — so promoting it on those
    numbers is exactly the error the gate was added to prevent.
    """
    scored = [r for r in results if r["growth"] is not None and r["value"] is not None]
    if not scored:
        for r in results:
            r["quadrant"] = "—"
        return None, None

    def _median(vals):
        s = sorted(vals)
        n = len(s)
        return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0

    g_med = _median([r["growth"] for r in scored])
    v_med = _median([r["value"] for r in scored])

    for r in results:
        g, v = r["growth"], r["value"]
        if g is None or v is None:
            r["quadrant"] = "—"
            continue
        hi_g = g >= g_med and not r["gates"]
        hi_v = v >= v_med and not r["gates"]
        r["quadrant"] = ("High Growth / High Value" if hi_g and hi_v else
                         "High Growth / Low Value" if hi_g else
                         "Low Growth / High Value" if hi_v else
                         "Low Growth / Low Value")
    return round(g_med, 1), round(v_med, 1)


def _size_segments(mcaps):
    """Large = top 70% of cumulative sector cap, Mid = next 20%, Small = rest."""
    segs = {tk: "—" for tk in mcaps}
    ranked = sorted(((tk, c) for tk, c in mcaps.items() if c and c > 0),
                    key=lambda x: x[1], reverse=True)
    total = sum(c for _, c in ranked)
    if not total:
        return segs
    cum = 0.0
    for tk, c in ranked:
        share_before = cum / total
        cum += c
        if share_before < SIZE_LARGE_SHARE:
            segs[tk] = "Large"
        elif share_before < SIZE_MID_SHARE:
            segs[tk] = "Mid"
        else:
            segs[tk] = "Small"
    return segs


def _barometer(results):
    """Morningstar-style barometer: average price change per Style x Size cell
    for 1D / 1W / 1Y, over the companies this scan scored. Simple (unweighted)
    average — every scrip counts once, so one giant name can't hide the rest."""
    from datetime import timedelta
    from core_analysis.models import NepseDailyStockPrice as P

    bucket_of = {}
    for r in results:
        if r.get("style") in ("Value", "Blend", "Growth") and r.get("size") in ("Large", "Mid", "Small"):
            bucket_of[r["ticker"]] = (r["size"], r["style"])
    if not bucket_of:
        return None

    latest = P.objects.filter(symbol__in=list(bucket_of)).order_by("-business_date")        .values_list("business_date", flat=True).first()
    if latest is None:
        return None
    since = latest - timedelta(days=400)

    hist = defaultdict(list)
    for row in P.objects.filter(symbol__in=list(bucket_of), business_date__gte=since)            .order_by("symbol", "business_date").values("symbol", "business_date", "close_price"):
        if row["close_price"] is not None:
            hist[row["symbol"]].append((row["business_date"], float(row["close_price"])))

    def change(rows, horizon):
        if len(rows) < 2:
            return None
        last_d, last_c = rows[-1]
        if last_c <= 0:
            return None
        if horizon == "1d":
            base = rows[-2][1]
        elif horizon == "1w":
            base = rows[max(0, len(rows) - 6)][1]
        else:  # 1y: nearest close at or before latest - 365d
            target = last_d - timedelta(days=365)
            base = None
            for d, c in rows:
                if d <= target:
                    base = c
                else:
                    break
            if base is None:
                base = rows[0][1]
        return (last_c / base - 1.0) * 100.0 if base and base > 0 else None

    out = {}
    for horizon in ("1d", "1w", "1y"):
        cells = {sz: {st: [] for st in ("Value", "Blend", "Growth")} for sz in ("Large", "Mid", "Small")}
        for sym, (sz, st) in bucket_of.items():
            ch = change(hist.get(sym, []), horizon)
            if ch is not None:
                cells[sz][st].append(ch)
        out[horizon] = {
            sz: {st: (round(sum(v) / len(v), 2) if v else None, len(v))
                 for st, v in row.items()}
            for sz, row in cells.items()
        }
        # flatten (avg, n) into dicts for JSON
        out[horizon] = {
            sz: {st: {"avg": val[0], "n": val[1]} for st, val in row.items()}
            for sz, row in out[horizon].items()
        }
    return out


def _latest_nonzero_caps(symbols):
    """Latest NONZERO capitalisation per symbol as {symbol: (rupees, shares)}.

    The upstream price feed sends market_capitalization = 0 for the newest
    session(s) and fills it in later, so "the latest row" is routinely zero for
    the entire market — each symbol's most recent nonzero reading is the usable
    figure. Column is in Rs millions (matches _latest_market_caps)."""
    from django.db.models import Max
    from core_analysis.models import NepseDailyStockPrice as P

    symbols = [x for x in symbols if x]
    if not symbols:
        return {}
    nz = P.objects.filter(symbol__in=symbols, market_capitalization__gt=0)
    latest_by = dict(nz.values_list("symbol").annotate(m=Max("business_date")))
    if not latest_by:
        return {}
    out = {}
    for r in nz.filter(business_date__in=set(latest_by.values())).values(
            "symbol", "business_date", "market_capitalization", "close_price"):
        if r["business_date"] != latest_by.get(r["symbol"]):
            continue
        cap = float(r["market_capitalization"]) * MILLION
        # Capitalisation and close come from the same row, so their quotient is
        # the listed share count on that day — the figure the KS block gets
        # wrong for NICA and a few others.
        px = _pos(r["close_price"])
        out[r["symbol"]] = (cap, cap / px if px else None)
    return out


def sector_scan(sector):
    """Full Morning Star scan for one sector. Cached."""
    # The scan reads the day's closing price, so the newest EOD date belongs in
    # the key — otherwise the first fill of a new session serves yesterday's
    # valuations for the whole cache window.
    from django.db.models import Max

    from core_analysis.models import NepseDailyStockPrice as P
    px_day = str(P.objects.aggregate(d=Max("business_date"))["d"] or "")
    key = f"morningstar_v{SCAN_VERSION}:{sector.replace(' ', '_')}:{px_day}"
    hit = cache.get(key)
    if hit is not None:
        return hit
    result = _sector_scan_uncached(sector)
    cache.set(key, result, CACHE_TTL)
    return result


def _sector_scan_uncached(sector):
    if sector == "Mutual Fund":
        try:
            return _mutual_fund_rows()
        except Exception:
            logger.exception("Morning Star mutual fund scan failed")
            return {"ok": False, "error": "Mutual fund NAV data unavailable."}

    spec_fn = SECTOR_SPECS.get(sector)
    if spec_fn is None:
        return {"ok": False, "error": f"No Morning Star spec for {sector}."}

    from core_analysis.services import industry as ind
    from core_analysis.fundamental_views import _prev_fy
    from core_analysis.models import CompanyProfile

    chosen = ind.best_period(sector, "KS")
    if not chosen:
        return {"ok": False, "error": f"No filed data for {sector}."}
    fy, quarter = chosen
    prev_fy = _prev_fy(fy)

    tickers = _load_tickers(sector, fy, quarter, prev_fy)
    if not tickers:
        return {"ok": False, "error": f"No companies with {fy} Q{quarter} filings in {sector}."}

    # Delisted and suspended issues still carry filed statements, so they reach
    # this point and would otherwise be scored and ranked against live peers.
    try:
        n_inactive, inactive = _drop_inactive(tickers)
    except Exception:
        logger.exception("Active-issue filter failed for %s", sector)
        n_inactive, inactive = 0, {}
    if n_inactive:
        logger.info("Morning Star %s: dropped %d inactive issue(s): %s",
                    sector, n_inactive,
                    ", ".join(f"{k} ({v})" for k, v in sorted(inactive.items())))
    if not tickers:
        return {"ok": False, "error": f"No actively traded companies in {sector}."}

    # Live share price BEFORE any scoring: P/B, P/E, dividend yield, the
    # distributable-per-price metrics and market cap all read T.price(), and
    # market cap also sets the size tier.
    try:
        price_as_of, priced_live = _attach_live_prices(tickers)
    except Exception:
        logger.exception("Live price merge failed for %s", sector)
        price_as_of, priced_live = "", 0

    if sector == "Life Insurance":
        try:
            _attach_life_extras(tickers, fy, quarter)
        except Exception:
            logger.exception("Life indicator merge failed")

    # EOD market-cap fallback for sectors whose KS block has no share count.
    try:
        eod_caps = _latest_nonzero_caps(list(tickers))
    except Exception:
        logger.exception("EOD market-cap fallback failed for %s", sector)
        eod_caps = {}
    for tk, (cap, sh) in eod_caps.items():
        if tk in tickers:
            tickers[tk].extra["eod_mcap"] = cap
            if sh:
                tickers[tk].extra["eod_shares"] = sh

    growth_specs, value_specs = spec_fn()
    g_scores, g_detail = _pillar(tickers, growth_specs)
    v_scores, v_detail = _pillar(tickers, value_specs)

    gw, vw = GV_MIX_VALUE_DOMINANT if sector in VALUE_DOMINANT_SECTORS else GV_MIX_DEFAULT

    names = dict(CompanyProfile.objects.filter(symbol__in=list(tickers))
                 .values_list("symbol", "security_name"))
    sizes = _size_segments({tk: t.mcap() for tk, t in tickers.items()})

    results = []
    for tk, t in tickers.items():
        g, v = g_scores.get(tk), v_scores.get(tk)
        combined = None
        if g is not None and v is not None:
            combined = g * gw + v * vw
        elif g is not None or v is not None:
            combined = g if g is not None else v
        gates, flags = _quality(sector, t)
        gp, gt = g_detail[tk]["present"], g_detail[tk]["total"]
        vp, vt = v_detail[tk]["present"], v_detail[tk]["total"]
        results.append({
            "ticker": tk,
            "name": names.get(tk, ""),
            "growth": round(g, 1) if g is not None else None,
            "value": round(v, 1) if v is not None else None,
            "combined": round(combined, 1) if combined is not None else None,
            "stars": _stars_from_combined(combined, gates, flags),
            "style": _style(g, v),
            "bvps": round(_bv, 1) if (_bv := _num(t.c("KS", "book_value_per_share"))) is not None else None,
            "size": sizes.get(tk, "—"),
            "confidence": f"{gp + vp} of {gt + vt} factors",
            "low_confidence": (gp + vp) < (gt + vt) * 0.6,
            "gates": gates,
            "flags": flags,
            "detail": {"growth": g_detail[tk], "value": v_detail[tk]},
        })

    g_median, v_median = _assign_quadrants(results)
    results.sort(key=lambda r: (-(r["stars"] or 0), -(r["combined"] or 0)))
    try:
        barometer = _barometer(results)
    except Exception:
        logger.exception("Barometer failed for %s", sector)
        barometer = None
    return {
        "ok": True,
        "barometer": barometer,
        "sector": sector,
        "period": f"{fy} Q{quarter} vs {prev_fy} Q{quarter}",
        "mix": {"growth": int(gw * 100), "value": int(vw * 100)},
        "price_as_of": price_as_of,
        "priced_live": priced_live,
        "priced_total": len(tickers),
        "inactive_excluded": n_inactive,
        "quadrant_split": {"growth": g_median, "value": v_median},
        "results": results,
    }


def available_sectors():
    """Sectors the scan can serve, in display order."""
    from core_analysis.models import FinancialStatement as FS
    present = set(FS.objects.order_by().values_list("sector", flat=True).distinct())
    ordered = [s for s in SECTOR_SPECS if s in present]
    ordered.append("Mutual Fund")
    return ordered
