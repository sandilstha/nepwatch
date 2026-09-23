"""
portfolio_analytics.py — valuation + risk roll-ups for a user's Portfolio.

Everything is derived from the positions in a ``Portfolio`` plus the local NEPSE
end-of-day tables (``NepseDailyStockPrice`` for prices, ``NepseMarketIndex`` for
the benchmark, ``CompanyProfile`` for sector/name). Position risk works without
cost data; optional WACC and broker-ledger imports add unrealised P/L, realised
P/L and broker cash while leaving the market-risk calculations unchanged.

NEPSE realities deliberately shaped the design:
  * Holdings are marked to each scrip's most-recent close (not a single session),
    because illiquid names don't trade every day.
  * Volatility / beta are best-effort and degrade to None on thin history; they
    are close-to-close estimates and inherit NEPSE's stale-price / circuit-band
    quirks, so they're presented as estimates, not guarantees.
  * Beta (and the factor model built on it) is estimated on WEEKLY returns, not
    daily: NEPSE names trade thinly and non-synchronously, so daily-return betas
    are biased toward zero (stale closes miss the index move). Sampling the last
    close of each Sun–Fri week absorbs the lag. Volatility & VaR stay daily —
    they don't suffer the cross-correlation bias and daily gives 5× the sample.
  * Portfolio beta is the weight-weighted sum of holding betas. Portfolio VaR
    is calculated from the current-weight portfolio return series, preserving
    observed cross-name diversification.
"""
from __future__ import annotations

import logging
import math
from datetime import timedelta

from django.core.cache import cache

logger = logging.getLogger(__name__)

NEPSE_INDEX_NAME = "NEPSE Index"  # matched case-insensitively (data has mixed casing)
RISK_LOOKBACK_DAYS = 370          # ~1 trading year of sessions for vol/beta/VaR
TRADING_DAYS_YEAR = 246           # NEPSE trades Sun–Fri (~246 sessions/yr)
WEEKS_YEAR = 52                   # annualisation factor for weekly-return stats
MIN_RETURNS = 20                  # need at least this many daily returns for vol
MIN_WEEKLY_RETURNS = 26           # ~6 months of weekly observations for beta
MIN_VAR_POINTS = 30               # need at least this many sessions for VaR
VAR_HORIZON_DAYS = 10             # second VaR horizon (empirical + parametric)
# Per-holding VaR horizons for the Portfolio Summary desk, in NEPSE sessions.
VAR_1W_SESSIONS = 5               # ~1 trading week
VAR_1M_SESSIONS = 20             # ~1 trading month
Z95, Z99 = 1.645, 2.326           # normal quantiles for parametric VaR
# HHI bands (0–10000), aligned with the broker-analytics concentration read.
HHI_MODERATE = 1500
HHI_HIGH = 2500
# Liquidity: one calendar year of observations and configurable participation.
# The UI exposes the three policy scenarios below; 20% remains the default.
LIQ_LOOKBACK_DAYS = 365           # trailing calendar year / ~246 sessions
PARTICIPATION_RATES = (0.10, 0.20, 0.25)
PARTICIPATION_RATE = 0.20
LIQUIDATION_TARGETS = (25.0, 50.0, 75.0, 100.0)
DTL_LIQUID, DTL_MODERATE = 1.0, 5.0   # days-to-liquidate tier thresholds
LIQUIDITY_RISK_LABELS = {
    "liquid": "Low",
    "moderate": "Moderate",
    "illiquid": "High",
    "untradeable": "Very High",
}
# Annual risk-free rate used by every risk-adjusted performance measure
# (Sharpe / Treynor / M² / Jensen's alpha). 6% ≈ the 91-day T-bill; Stock 360
# carries the same figure as DEFAULT_RISK_FREE. If one moves, move both — a
# mismatch would make the same book score differently on two pages.
RISK_FREE_ANNUAL = 0.06

CACHE_TTL = 180
PAYLOAD_VERSION = 9   # 7: performance+correlation · 8: pl_snapshot · 9: reliability grading

# Default investment-policy limits monitored on every portfolio. "warn" raises a
# watch, "breach" a violation. Sensible institutional defaults tuned for a
# concentrated NEPSE retail book; a future per-user RiskLimit model can override.
LIMITS = {
    "single_name": {"warn": 12.0, "breach": 15.0},    # max one-stock weight %
    "top5": {"warn": 45.0, "breach": 55.0},           # max top-5 weight %
    "sector": {"warn": 30.0, "breach": 40.0},         # max one-sector weight %
    "illiquid": {"warn": 15.0, "breach": 25.0},       # max % in >5-day-to-exit names
    "untradeable": {"warn": 3.0, "breach": 8.0},      # max % with no ADV
    "var_1d_95": {"warn": 3.0, "breach": 5.0},         # max 1-day 95% VaR %
    "diversification": {"warn": 10.0, "breach": 6.0},  # min effective holdings
    "beta": {"soft": (0.6, 1.4), "hard": (0.4, 1.6)},  # acceptable beta band
}


def _f(value, default=0.0):
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return parsed if math.isfinite(parsed) else default


def normalize_participation_rate(value):
    """Return an allowed ADV participation fraction (10%, 20%, or 25%)."""
    rate = _f(value, PARTICIPATION_RATE)
    if rate > 1:
        rate /= 100.0
    return min(PARTICIPATION_RATES, key=lambda allowed: abs(allowed - rate))


def normalize_liquidation_target(value):
    """Clamp a user liquidation target to a meaningful portfolio percentage."""
    return min(100.0, max(0.1, _f(value, 100.0)))


# ─────────────────────────────────────────────────────────────────────────────
# Reference data (latest prices, sectors, names)
# ─────────────────────────────────────────────────────────────────────────────
def _latest_session():
    from core_analysis.models import NepseDailyStockPrice

    return (
        NepseDailyStockPrice.objects.order_by("-business_date")
        .values_list("business_date", flat=True)
        .first()
    )


def _latest_prices(symbols):
    """{symbol: (close, business_date, market_cap, previous_close)} at each symbol's MOST RECENT
    session — illiquid scrips may not have a row on the very latest day."""
    from core_analysis.models import NepseDailyStockPrice

    if not symbols:
        return {}
    latest = _latest_session()
    if not latest:
        return {}
    start = latest - timedelta(days=20)
    rows = (
        NepseDailyStockPrice.objects.filter(
            symbol__in=symbols, business_date__gte=start
        )
        .order_by("symbol", "-business_date")
        .values_list(
            "symbol", "business_date", "close_price", "market_capitalization",
            "previous_close",
        )
    )
    out = {}
    for sym, bd, close, mcap, previous_close in rows:
        if sym not in out:
            out[sym] = (_f(close), bd, _f(mcap), _f(previous_close))
    return out


def _company_meta(symbols):
    """{symbol: (security_name, sector_name)} from CompanyProfile (best-effort)."""
    from core_analysis.models import CompanyProfile

    meta = {}
    try:
        for sym, name, sector in CompanyProfile.objects.filter(
            symbol__in=symbols
        ).values_list("symbol", "security_name", "sector_name"):
            meta[sym] = (name or sym, sector or "Uncategorized")
    except Exception:  # pragma: no cover - reference table optional
        meta = {}
    return meta


def _liquidity(symbols):
    """Average daily volume / turnover per symbol → ``({sym: {...}}, sessions)``.

    ADV is the whole-market traded quantity for the scrip averaged over the
    *market* sessions in the window (``total_traded_quantity`` from the EOD
    table), NOT over only the days it traded — so a name that prints on 3 of 22
    sessions is correctly scored as thin, not falsely liquid.
    """
    from core_analysis.models import NepseDailyStockPrice

    out = {}
    if not symbols:
        return out, 0
    try:
        latest = _latest_session()
        if not latest:
            return out, 0
        start = latest - timedelta(days=LIQ_LOOKBACK_DAYS)
        # True market-session count over the window, taken from the whole EOD
        # table (NOT just the held symbols' trade days) — otherwise a book of
        # thin names would divide by too few sessions and look falsely liquid.
        sessions = (
            NepseDailyStockPrice.objects.filter(business_date__gte=start)
            .values("business_date").distinct().count()
        )
        rows = NepseDailyStockPrice.objects.filter(
            symbol__in=symbols, business_date__gte=start
        ).values_list("symbol", "business_date", "total_traded_quantity", "total_traded_value")
        agg = {}
        for sym, bd, q, v in rows:
            a = agg.setdefault(sym, [0.0, 0.0])
            a[0] += _f(q)
            a[1] += _f(v)
        sessions = sessions or 1
        for sym, (q, v) in agg.items():
            out[sym] = {"adv_qty": q / sessions, "adv_turnover": v / sessions}
        return out, sessions
    except Exception:  # pragma: no cover - liquidity overlay is best-effort
        logger.exception("liquidity load failed")
        return out, 0


# ─────────────────────────────────────────────────────────────────────────────
# Risk stats (per-holding volatility + beta to NEPSE)
# ─────────────────────────────────────────────────────────────────────────────
def _returns(series):
    """Close-to-close simple returns from a date-sorted [(date, close), …]."""
    out = {}
    prev = None
    for bd, close in series:
        if prev is not None and prev[1]:
            out[bd] = (close - prev[1]) / prev[1]
        prev = (bd, close)
    return out


def _weekly_returns(series):
    """Weekly simple returns from a date-sorted [(date, close), …].

    Closes collapse to the LAST trading close of each ISO week (NEPSE's Sun–Fri
    week ends Friday), then returns are computed week-over-week, keyed by
    ``(iso_year, iso_week)``. Weekly sampling is what makes NEPSE betas usable:
    a scrip that prints late (or not at all) on a given day still catches up
    within the week, so the stale-close bias of daily betas mostly cancels.
    A week with no trade simply has no key — the next observed week's return
    spans the gap, matching how thin names actually reprice.
    """
    weekly = {}
    for bd, close in series:   # date-ascending → last write wins = week's close
        weekly[bd.isocalendar()[:2]] = close
    out = {}
    prev = None
    for wk in sorted(weekly):
        close = weekly[wk]
        if prev is not None and prev[1]:
            out[wk] = (close - prev[1]) / prev[1]
        prev = (wk, close)
    return out


def _stdev(values):
    n = len(values)
    if n < 2:
        return None
    mean = sum(values) / n
    var = sum((v - mean) ** 2 for v in values) / (n - 1)
    return math.sqrt(var)


def _load_returns(symbols):
    """Daily + weekly simple returns over the risk window.

    Returns ``(stock_ret, index_ret, stock_wret, index_wret)`` where the first
    pair is daily (``{symbol: {date: ret}}`` / ``{date: ret}``) and feeds
    volatility + historical-simulation VaR, and the second pair is weekly
    (keyed by ``(iso_year, iso_week)``) and feeds beta + the factor model.
    Best-effort — returns empties on any failure so risk is just omitted,
    never fatal.
    """
    from core_analysis.models import NepseDailyStockPrice, NepseMarketIndex

    stock_ret, index_ret, stock_wret, index_wret = {}, {}, {}, {}
    if not symbols:
        return stock_ret, index_ret, stock_wret, index_wret
    try:
        latest = _latest_session()
        if not latest:
            return stock_ret, index_ret, stock_wret, index_wret
        start = latest - timedelta(days=RISK_LOOKBACK_DAYS)

        idx_rows = NepseMarketIndex.objects.filter(
            sector_name__iexact=NEPSE_INDEX_NAME, business_date__gte=start
        ).values_list("business_date", "close_index")
        idx_map = {}
        for bd, c in idx_rows:
            idx_map[bd] = _f(c)
        idx_series = sorted(idx_map.items())
        index_ret = _returns(idx_series)
        index_wret = _weekly_returns(idx_series)

        series = {}
        rows = (
            NepseDailyStockPrice.objects.filter(
                symbol__in=symbols, business_date__gte=start
            )
            .order_by("symbol", "business_date")
            .values_list("symbol", "business_date", "close_price")
        )
        for sym, bd, close in rows:
            series.setdefault(sym, []).append((bd, _f(close)))
        for sym, ser in series.items():
            stock_ret[sym] = _returns(ser)
            stock_wret[sym] = _weekly_returns(ser)
    except Exception:  # pragma: no cover - risk overlay is best-effort
        logger.exception("portfolio return load failed")
    return stock_ret, index_ret, stock_wret, index_wret


def _beta_resid(stock_ret, index_ret, min_n=MIN_WEEKLY_RETURNS):
    """OLS (beta, residual std) of a stock vs the index over shared periods.

    Called with WEEKLY return maps (keys are ``(iso_year, iso_week)``), so beta
    is a weekly-return beta and the residual std is the stock-specific
    (idiosyncratic) WEEKLY volatility left after the market move is regressed
    out — the raw material for the factor risk decomposition. Returns
    ``(None, None)`` on thin overlap.
    """
    common = [d for d in stock_ret if d in index_ret]
    if len(common) < min_n:
        return None, None
    xs = [index_ret[d] for d in common]
    ys = [stock_ret[d] for d in common]
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / (n - 1)
    var = sum((x - mx) ** 2 for x in xs) / (n - 1)
    if not var:
        return None, None
    beta = cov / var
    alpha = my - beta * mx
    resid = [ys[k] - (alpha + beta * xs[k]) for k in range(n)]
    return beta, _stdev(resid)


def _beta_precision(stock_ret, index_ret, min_n=MIN_WEEKLY_RETURNS):
    """(beta, standard error, n) for one holding — how much to trust the beta.

    A weekly beta on ~52 observations is not a precise number, and displaying it
    to two decimals implies otherwise. Measured on real books: UNHPL came out at
    0.50 with a standard error of 0.24 (95% CI 0.03-0.97) while being 48% of that
    portfolio, and NIBLGF at -0.20 with t = -0.73, i.e. indistinguishable from
    zero. The desk needs to say which betas are estimates and which are noise.
    """
    common = [d for d in stock_ret if d in index_ret]
    n = len(common)
    if n < min_n:
        return None, None, n
    xs = [index_ret[d] for d in common]
    ys = [stock_ret[d] for d in common]
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if not sxx or n < 3:
        return None, None, n
    beta = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
    alpha = my - beta * mx
    resid = [y - (alpha + beta * x) for x, y in zip(xs, ys)]
    s2 = sum(e * e for e in resid) / (n - 2)
    return beta, math.sqrt(s2 / sxx), n


def _parametric_var(vol_annual_pct, sessions):
    """95% parametric VaR as a positive loss *fraction* over ``sessions`` sessions.

    ``vol_annual_pct`` is the annualised volatility % produced by
    ``_per_symbol_stats``; recover the daily sigma and √-time scale it. Returns
    None when volatility is unavailable (thin history) so callers show "—".
    """
    if not vol_annual_pct:
        return None
    daily_sigma = (vol_annual_pct / 100.0) / math.sqrt(TRADING_DAYS_YEAR)
    return Z95 * daily_sigma * math.sqrt(sessions)


def _nepse_index_level():
    """Latest and all-history peak NEPSE closes for beta stress scenarios."""
    from core_analysis.models import NepseMarketIndex

    qs = NepseMarketIndex.objects.filter(sector_name__iexact=NEPSE_INDEX_NAME)
    row = (
        qs.order_by("-business_date")
        .values_list("business_date", "close_index")
        .first()
    )
    if not row:
        return {"value": None, "date": None, "highest": None, "highest_date": None}
    highest = (
        qs.order_by("-close_index", "business_date")
        .values_list("business_date", "close_index")
        .first()
    )
    return {
        "value": round(_f(row[1]), 2),
        "date": row[0].isoformat(),
        "highest": round(_f(highest[1]), 2) if highest else None,
        "highest_date": highest[0].isoformat() if highest else None,
    }


def _pl_snapshot(rows, top_n=4):
    """Winners vs losers on unrealised P/L, plus the best/worst by percentage.

    Needs a WACC cost basis, so it is empty until the user imports the broker's
    "My WACC" report — position risk elsewhere on the desk works without it.

    Ranked by PERCENT, not rupees, deliberately: the rupee leader is usually
    just the biggest position, which says more about sizing than about the
    holding. Totals stay in rupees because that is the money at stake.
    """
    costed = [r for r in rows if r.get("pl") is not None and r.get("cost_value") is not None]
    if not costed:
        return {"ok": False, "reason": "Import the WACC report to see unrealised P/L."}

    winners = [r for r in costed if r["pl"] > 0]
    losers = [r for r in costed if r["pl"] < 0]
    flat = [r for r in costed if r["pl"] == 0]

    def _side(group):
        pl = sum(r["pl"] for r in group)
        cost = sum(r["cost_value"] for r in group)
        return {
            "count": len(group),
            "pl": round(pl, 2),
            "pl_pct": round(100.0 * pl / cost, 2) if cost else None,
        }

    def _card(r):
        return {
            "symbol": r["symbol"],
            "price": r.get("price"),
            "day_change": r.get("day_change"),
            "day_change_pct": r.get("day_change_pct"),
            "pl": r.get("pl"),
            "pl_pct": r.get("pl_pct"),
            "quantity": r.get("quantity"),
            "weight": r.get("weight"),
        }

    ranked = sorted(costed, key=lambda r: (r.get("pl_pct") is None, -(r.get("pl_pct") or 0)))
    return {
        "ok": True,
        "total": len(costed),
        "uncosted": len(rows) - len(costed),
        "winners": _side(winners),
        "losers": _side(losers),
        "flat_count": len(flat),
        "net_pl": round(sum(r["pl"] for r in costed), 2),
        "top_gainers": [_card(r) for r in ranked if (r.get("pl_pct") or 0) > 0][:top_n],
        "top_losers": [_card(r) for r in reversed(ranked) if (r.get("pl_pct") or 0) < 0][:top_n],
    }


def _cost_summary(rows):
    """Book value & paper P/L over the holdings that carry a WACC cost basis.

    Computed on the *costed* subset only (market value vs book value of the same
    names) so a partial WACC import never distorts the paper P/L. ``has_cost`` is
    False until the user imports the 'My WACC' report.
    """
    costed = [r for r in rows if r.get("cost_value") is not None]
    if not costed:
        return {"has_cost": False, "covered_count": 0, "book_value": None,
                "costed_market_value": None, "paper_pl": None, "paper_pl_pct": None}
    book = round(sum(r["cost_value"] for r in costed), 2)
    mkt = round(sum(r["value"] for r in costed), 2)
    pl = round(mkt - book, 2)
    return {
        "has_cost": True,
        "covered_count": len(costed),
        "book_value": book,
        "costed_market_value": mkt,
        "paper_pl": pl,
        "paper_pl_pct": round(100.0 * pl / book, 2) if book else None,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Data-quality grading
# ─────────────────────────────────────────────────────────────────────────────
# Thresholds for how much of a holding's history has to be real prints before
# its risk numbers can be believed. A NEPSE scrip that does not trade carries
# its last close forward, which the return series reads as a 0% move — so an
# illiquid name looks CALM rather than unpriced. Measured on a real 51-name
# book: KAHL traded 29 of 226 sessions and its annualised volatility came out
# at 33.3%, against 90.8% when computed only over sessions it actually traded.
TRADED_OK = 0.90        # >= 90% of sessions priced -> treat as reliable
TRADED_WEAK = 0.60      # 60-90% -> estimate; below that -> unreliable
BETA_T_OK = 2.0         # |beta / se| below this is statistically indistinct from 0
STALE_WEIGHT_WARN = 5.0     # % of book in stale names before vol/VaR is flagged
STALE_WEIGHT_BAD = 20.0

GRADE_LABELS = {"green": "Reliable", "amber": "Estimate", "red": "High model uncertainty"}


def _holding_quality(rows, stock_ret, stock_wret, index_wret, session_dates):
    """Attach a per-holding data-quality grade, and return the book-level roll-up.

    Grades describe INPUT quality, not risk: a red holding is one whose numbers
    cannot be trusted, which is different from a holding that is risky.
    """
    sessions = len(set(session_dates or []))
    stale_weight = 0.0
    noisy_beta_weight = 0.0
    no_beta_weight = 0.0
    worst = "green"
    for r in rows:
        sym = r["symbol"]
        traded = len(stock_ret.get(sym, {}))
        frac = (traded / sessions) if sessions else 0.0
        beta, se, nwk = _beta_precision(stock_wret.get(sym, {}), index_wret)
        tstat = (beta / se) if (beta is not None and se) else None

        reasons = []
        grade = "green"
        if frac < TRADED_WEAK:
            grade = "red"
            reasons.append(f"priced on only {traded} of {sessions} sessions — "
                           f"volatility and VaR are understated")
        elif frac < TRADED_OK:
            grade = "amber"
            reasons.append(f"missing {sessions - traded} of {sessions} sessions")
        if beta is None:
            grade = "red" if grade != "red" else grade
            reasons.append(f"no beta ({nwk} shared weeks, needs {MIN_WEEKLY_RETURNS})")
        elif tstat is not None and abs(tstat) < BETA_T_OK:
            grade = "amber" if grade == "green" else grade
            reasons.append(f"beta {beta:.2f} ± {se:.2f} is not statistically "
                           f"distinguishable from zero (t={tstat:.1f})")

        r["quality"] = grade
        r["quality_label"] = GRADE_LABELS[grade]
        r["quality_reasons"] = reasons
        r["traded_sessions"] = traded
        r["traded_pct"] = round(100.0 * frac, 1) if sessions else None
        r["beta_se"] = round(se, 3) if se is not None else None
        r["beta_t"] = round(tstat, 2) if tstat is not None else None
        r["beta_ci"] = ([round(beta - 1.96 * se, 2), round(beta + 1.96 * se, 2)]
                        if (beta is not None and se) else None)

        w = r.get("weight", 0.0)
        if frac < TRADED_OK:
            stale_weight += w
        if beta is None:
            no_beta_weight += w
        elif tstat is not None and abs(tstat) < BETA_T_OK:
            noisy_beta_weight += w
        if grade == "red" or (grade == "amber" and worst == "green"):
            worst = grade if grade == "red" else "amber"

    return {
        "sessions": sessions,
        "stale_weight_pct": round(stale_weight, 1),
        "noisy_beta_weight_pct": round(noisy_beta_weight, 1),
        "no_beta_weight_pct": round(no_beta_weight, 1),
        "worst_holding_grade": worst,
        "stale_names": [r["symbol"] for r in rows if r["quality"] == "red"],
    }


def _metric_reliability(q, cost_has, perf_ok):
    """Grade each METRIC GROUP by the quality of the data feeding it.

    The point of this block is that the desk currently presents a 2-decimal VaR
    computed partly from prices that never moved because nothing traded. Every
    number here is still shown; this says how much weight to put on it.
    """
    stale, noisy = q["stale_weight_pct"], q["noisy_beta_weight_pct"] + q["no_beta_weight_pct"]

    def g(bad, warn):
        return "red" if bad else ("amber" if warn else "green")

    items = [
        {"key": "vol_var", "label": "Volatility · VaR · Expected Shortfall",
         "grade": g(stale >= STALE_WEIGHT_BAD, stale >= STALE_WEIGHT_WARN),
         "why": (f"{stale:.1f}% of the book trades on fewer than {TRADED_OK*100:.0f}% of "
                 f"sessions. A scrip that does not trade repeats its last close, which the "
                 f"return series reads as a 0% move, so risk is biased DOWNWARD."
                 if stale else
                 "Every holding is priced on essentially all sessions, so no stale-price "
                 "bias in the return series.")},
        {"key": "beta_stress", "label": "Beta · Stress scenarios",
         "grade": g(q["no_beta_weight_pct"] >= 25 or noisy >= 40, noisy >= 10),
         "why": (f"{noisy:.1f}% of the book has a beta that is statistically weak or absent. "
                 f"Beta is also estimated on ~52 weekly points, so a two-decimal figure "
                 f"carries a confidence interval that is often ±0.2 or wider."
                 if noisy else
                 "Beta is available and statistically distinguishable from zero across "
                 "essentially the whole book — though still a ~52-point estimate.")},
        {"key": "stress_linearity", "label": "Stress = beta × shock",
         "grade": "amber",
         "why": ("A linear approximation. In a genuine crash, betas rise, correlations "
                 "converge and liquidity vanishes, so realised losses are typically worse "
                 "than beta × shock. Treat it as a scenario, not a prediction.")},
        {"key": "performance", "label": "Sharpe · Treynor · Alpha · M² · Info ratio",
         "grade": "amber" if perf_ok else "red",
         "why": ("Computed by applying TODAY'S weights to past prices. It answers 'how "
                 "would this book have scored', not 'what did you actually earn' — that "
                 "needs a holdings history, which is not recorded yet."
                 if perf_ok else "Not enough shared history to compute.")},
        {"key": "correlation", "label": "Correlation · Diversification ratio",
         "grade": "amber",
         "why": ("Measured on ~52 weekly points in one regime. Same-sector residuals are "
                 "empirically correlated (+0.15 to +0.46 on these books), which the "
                 "single-factor model assumes away — so diversification is, if anything, "
                 "flattered.")},
        {"key": "factors", "label": "Factor decomposition",
         "grade": "amber",
         "why": ("A SINGLE-market-factor split, not a full risk model: it carries no "
                 "explicit interest-rate, sector, size or liquidity factor, and assumes "
                 "residuals are uncorrelated — which measurement shows they are not "
                 "within a sector.")},
        {"key": "liquidity", "label": "Days-to-liquidate · Liquidity stress",
         "grade": "amber",
         "why": ("Modelled capacity from past average volume, not executable liquidity. "
                 "Volume tends to disappear precisely when selling is urgent, so DTL "
                 "understates exit time in exactly the conditions that matter.")},
        {"key": "concentration", "label": "Weights · HHI · Effective holdings",
         "grade": "green",
         "why": ("Arithmetic on current positions and prices — no statistical estimation, "
                 "so no model uncertainty. Note HHI counts names, not economic exposure: "
                 "ten banks score as diversified.")},
        {"key": "pl", "label": "Book value · Unrealised P/L",
         "grade": "green" if cost_has else "red",
         "why": ("Arithmetic against the imported WACC cost basis."
                 if cost_has else "No WACC cost basis imported, so P/L cannot be computed.")},
    ]
    order = {"red": 0, "amber": 1, "green": 2}
    items.sort(key=lambda i: order[i["grade"]])
    worst = items[0]["grade"] if items else "green"
    return {
        "items": items,
        "labels": GRADE_LABELS,
        "overall": worst,
        "counts": {k: sum(1 for i in items if i["grade"] == k)
                   for k in ("green", "amber", "red")},
        "note": ("Grades describe the QUALITY OF THE INPUTS, not how risky the portfolio "
                 "is. A red grade means the number should not be read to two decimals — "
                 "not that the holding is dangerous."),
    }


def _per_symbol_stats(symbols, stock_ret, index_ret, stock_wret, index_wret):
    """{symbol: {'vol': annualised %|None, 'beta': float|None, 'resid': weekly std|None}}.

    Volatility comes from DAILY returns (5× the sample, no cross-correlation
    bias); beta + residual come from WEEKLY returns (thin-trading-robust, per
    the module docstring). ``resid`` is the weekly idiosyncratic std, consumed
    by ``_factor_decomposition`` which annualises with ``WEEKS_YEAR``.
    """
    stats = {s: {"vol": None, "beta": None, "resid": None} for s in symbols}
    for sym in symbols:
        vals = list(stock_ret.get(sym, {}).values())
        sd = _stdev(vals) if len(vals) >= MIN_RETURNS else None
        beta, resid = _beta_resid(stock_wret.get(sym, {}), index_wret)
        stats[sym] = {
            "vol": round(sd * math.sqrt(TRADING_DAYS_YEAR) * 100.0, 1) if sd else None,
            "beta": round(beta, 2) if beta is not None else None,
            "resid": resid,                       # weekly residual std (unrounded)
        }
    return stats


# ─────────────────────────────────────────────────────────────────────────────
# Value at Risk + stress testing
# ─────────────────────────────────────────────────────────────────────────────
def _percentile(sorted_vals, q):
    """Linear-interpolated quantile of an already-sorted list (q in 0..1)."""
    n = len(sorted_vals)
    if n == 0:
        return None
    if n == 1:
        return sorted_vals[0]
    pos = q * (n - 1)
    lo = int(math.floor(pos))
    hi = min(lo + 1, n - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (pos - lo)


def _portfolio_returns(weight_frac, stock_ret, session_dates=None):
    """Current-weights historical return series: rₜ = Σ wᵢ·rᵢ,ₜ.

    A held name with no trade on date t contributes 0 (NEPSE illiquidity =
    no-move assumption). When benchmark session dates are supplied, the book is
    valued on every NEPSE market session. Returns ``{date: portfolio_return}``.
    """
    dates = set(session_dates or [])
    for sym in weight_frac:
        dates.update(stock_ret.get(sym, {}).keys())
    out = {}
    for d in dates:
        out[d] = sum(w * stock_ret.get(sym, {}).get(d, 0.0) for sym, w in weight_frac.items())
    return out


def _max_drawdown(ordered_returns):
    """Worst peak-to-trough on the current-holdings equity curve (≤ 0)."""
    eq = peak = 1.0
    mdd = 0.0
    for r in ordered_returns:
        eq *= (1.0 + r)
        peak = max(peak, eq)
        if peak:
            mdd = min(mdd, (eq - peak) / peak)
    return mdd


def _worst_window(ordered_returns, k):
    """Worst compounded return over any k consecutive sessions (≤ 0), or None."""
    n = len(ordered_returns)
    if n < k:
        return None
    worst = None
    for i in range(n - k + 1):
        prod = 1.0
        for r in ordered_returns[i:i + k]:
            prod *= (1.0 + r)
        ret = prod - 1.0
        worst = ret if worst is None else min(worst, ret)
    return worst


def _compounded_windows(ordered_returns, sessions):
    """Return overlapping compounded historical returns for a horizon."""
    if sessions <= 1:
        return list(ordered_returns)
    out = []
    for start in range(len(ordered_returns) - sessions + 1):
        wealth = 1.0
        for value in ordered_returns[start:start + sessions]:
            wealth *= 1.0 + value
        out.append(wealth - 1.0)
    return out


def _tail_metrics(returns, confidence):
    """Return positive historical VaR and Expected Shortfall fractions."""
    values = sorted(returns)
    threshold = _percentile(values, 1.0 - confidence)
    if threshold is None:
        return 0.0, 0.0
    var = max(0.0, -threshold)
    tail = [value for value in returns if value <= threshold]
    expected_shortfall = max(var, -(sum(tail) / len(tail))) if tail else var
    return var, max(0.0, expected_shortfall)


def _beta_stress_scenarios(port_beta, total_value, index_info):
    """Build standard market shocks and explicit NEPSE target scenarios."""
    if port_beta is None:
        return []
    current = _f((index_info or {}).get("value"))
    current_date = (index_info or {}).get("date")
    scenarios = []

    def add(label, shock, kind, target=None, reference=None):
        impact = port_beta * shock
        impact_rs = total_value * impact / 100.0
        projected = total_value + impact_rs
        scenarios.append({
            "label": label,
            "kind": kind,
            "shock": round(shock, 2),
            "target_index": round(target, 2) if target is not None else (
                round(current * (1.0 + shock / 100.0), 2) if current else None
            ),
            "impact_pct": round(impact, 2),
            "gain_loss_pct": round(impact, 2),
            "impact_rs": round(impact_rs, 2),
            "portfolio_value": round(projected, 2),
            # The risk engine stresses invested holdings only. Broker cash is
            # reported in accounting Total Equity and is intentionally not shocked.
            "nav": round(projected, 2),
            "reference": reference,
        })

    if current:
        reference = f"Latest NEPSE close on {current_date}" if current_date else "Latest NEPSE close"
        add("Current NEPSE", 0.0, "current", current, reference)
    for shock in (-20, -10, -5, 5, 10, 20):
        add(f"NEPSE {shock:+d}%", float(shock), "shock")
    if current:
        highest = _f((index_info or {}).get("highest"))
        highest_date = (index_info or {}).get("highest_date")
        near_3200 = highest and abs(highest - 3200.0) / 3200.0 <= 0.01
        if near_3200:
            reference = f"ATH close {highest:,.2f}"
            if highest_date:
                reference += f" on {highest_date}"
            add(
                "NEPSE 3,200 / ATH",
                (3200.0 / current - 1.0) * 100.0,
                "target_near_high",
                3200.0,
                reference,
            )
        else:
            add("NEPSE at 3,200", (3200.0 / current - 1.0) * 100.0, "target", 3200.0)
        if highest and not near_3200:
            add(
                "NEPSE at all-time high",
                (highest / current - 1.0) * 100.0,
                "historical_high",
                highest,
                f"ATH close date {highest_date}" if highest_date else None,
            )
    return scenarios


def _risk_block(weight_frac, total_value, port_beta, stock_ret, index_info=None,
                session_dates=None):
    """Historical/parametric VaR, Expected Shortfall, and beta stress scenarios.

    Historical simulation is primary (NEPSE returns are fat-tailed and circuit
    bands truncate them, so normal VaR is unreliable). Ten-session historical
    risk uses overlapping compounded windows; parametric risk uses square-root
    scaling. All currency risk figures are positive losses.
    """
    if total_value <= 0:
        return {"ok": False, "reason": "Portfolio has no marked value."}
    port = _portfolio_returns(weight_frac, stock_ret, session_dates)
    if len(port) < MIN_VAR_POINTS:
        return {"ok": False,
                "reason": f"Not enough price history for VaR (need {MIN_VAR_POINTS}+ sessions)."}

    items = sorted(port.items())                 # by date, ascending
    rets = [r for _d, r in items]
    rets10 = _compounded_windows(rets, VAR_HORIZON_DAYS)
    sigma = _stdev(rets) or 0.0
    v95, cvar95 = _tail_metrics(rets, 0.95)
    v99, cvar99 = _tail_metrics(rets, 0.99)
    v95_10, cvar95_10 = _tail_metrics(rets10, 0.95)
    v99_10, cvar99_10 = _tail_metrics(rets10, 0.99)
    worst = min(items, key=lambda kv: kv[1])
    mdd = _max_drawdown(rets)
    w5 = _worst_window(rets, 5)

    def rs(p):
        return round(p * total_value, 2)

    p95_1 = Z95 * sigma
    p99_1 = Z99 * sigma
    p95_10 = p95_1 * math.sqrt(VAR_HORIZON_DAYS)
    p99_10 = p99_1 * math.sqrt(VAR_HORIZON_DAYS)
    scenarios = _beta_stress_scenarios(port_beta, total_value, index_info or {})

    return {
        "ok": True,
        "sessions": len(rets),
        "ann_vol_pct": round(sigma * math.sqrt(TRADING_DAYS_YEAR) * 100.0, 1) if sigma else None,
        "var": {
            "hist_95_1d_pct": round(v95 * 100, 2), "hist_95_1d_rs": rs(v95),
            "hist_99_1d_pct": round(v99 * 100, 2), "hist_99_1d_rs": rs(v99),
            "hist_95_10d_pct": round(v95_10 * 100, 2), "hist_95_10d_rs": rs(v95_10),
            "hist_99_10d_pct": round(v99_10 * 100, 2), "hist_99_10d_rs": rs(v99_10),
            "cvar_95_1d_pct": round(cvar95 * 100, 2), "cvar_95_1d_rs": rs(cvar95),
            "cvar_99_1d_pct": round(cvar99 * 100, 2), "cvar_99_1d_rs": rs(cvar99),
            "cvar_95_10d_pct": round(cvar95_10 * 100, 2), "cvar_95_10d_rs": rs(cvar95_10),
            "cvar_99_10d_pct": round(cvar99_10 * 100, 2), "cvar_99_10d_rs": rs(cvar99_10),
            "param_95_1d_pct": round(p95_1 * 100, 2), "param_95_1d_rs": rs(p95_1),
            "param_99_1d_pct": round(p99_1 * 100, 2), "param_99_1d_rs": rs(p99_1),
            "param_95_10d_pct": round(p95_10 * 100, 2), "param_95_10d_rs": rs(p95_10),
            "param_99_10d_pct": round(p99_10 * 100, 2), "param_99_10d_rs": rs(p99_10),
            # Diversified parametric VaR at the summary horizons — Z·σ_p·√h on the
            # *portfolio* daily sigma (correlation already baked into the return
            # series), so it is lower than the sum of per-holding VaRs.
            "param_95_1w_pct": round(Z95 * sigma * math.sqrt(VAR_1W_SESSIONS) * 100, 2),
            "param_95_1w_rs": rs(Z95 * sigma * math.sqrt(VAR_1W_SESSIONS)),
            "param_95_1m_pct": round(Z95 * sigma * math.sqrt(VAR_1M_SESSIONS) * 100, 2),
            "param_95_1m_rs": rs(Z95 * sigma * math.sqrt(VAR_1M_SESSIONS)),
        },
        "worst_day": {"date": worst[0].isoformat(), "pct": round(worst[1] * 100, 2),
                      "rs": rs(worst[1])},
        "max_drawdown_pct": round(mdd * 100, 2), "max_drawdown_rs": rs(mdd),
        "worst_5d_pct": round(w5 * 100, 2) if w5 is not None else None,
        "worst_5d_rs": rs(w5) if w5 is not None else None,
        "beta_used": round(port_beta, 2) if port_beta is not None else None,
        "stress_reason": None if port_beta is not None else "Portfolio beta unavailable.",
        "scenarios": scenarios,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Factor risk decomposition (single-factor: NEPSE market + stock-specific)
# ─────────────────────────────────────────────────────────────────────────────
def _corr(xs, ys):
    """Pearson correlation of two equal-length sequences, or None if undefined."""
    n = len(xs)
    if n < 2:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    sy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if not sx or not sy:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / (sx * sy)


def _annualise(rets, periods_year=WEEKS_YEAR):
    """(geometric annual return, annualised stdev) from a period return list."""
    n = len(rets)
    if n < 2:
        return None, None
    growth = 1.0
    for r in rets:
        growth *= (1.0 + r)
    if growth <= 0:
        return None, None                       # total wipe-out: CAGR undefined
    ann_ret = growth ** (periods_year / n) - 1.0
    sd = _stdev(rets)
    return ann_ret, (sd * math.sqrt(periods_year) if sd is not None else None)


def _performance_block(weight_frac, stock_wret, index_wret, port_beta):
    """Risk-adjusted performance vs the NEPSE index: Sharpe, Treynor, M²,
    Jensen's alpha, tracking error and information ratio.

    WEEKLY throughout, deliberately. Beta is estimated on weekly returns (see the
    module docstring), and Treynor/alpha divide by that beta — pairing it with a
    daily sigma would mix two different sampling frequencies and silently
    misstate every ratio. Volatility and VaR stay daily elsewhere; this block is
    self-contained and internally consistent.

    IMPORTANT — what these numbers are: the return series is the CURRENT weights
    applied to past prices (rₜ = Σ wᵢ·rᵢ,ₜ), the same series the VaR block uses.
    So this answers "how would today's book have scored over the window", not
    "what did the user actually earn". Realised, money-weighted performance needs
    a holdings history (PortfolioSnapshot), which is not populated yet.
    """
    if not weight_frac or not index_wret:
        return {"ok": False, "reason": "No benchmark history available."}

    port = _portfolio_returns(weight_frac, stock_wret)
    weeks = sorted(w for w in port if w in index_wret)
    if len(weeks) < MIN_WEEKLY_RETURNS:
        return {"ok": False,
                "reason": (f"Not enough shared history (need {MIN_WEEKLY_RETURNS}+ "
                           f"weeks, have {len(weeks)}).")}

    pr = [port[w] for w in weeks]
    br = [index_wret[w] for w in weeks]

    p_ret, p_vol = _annualise(pr)
    b_ret, b_vol = _annualise(br)
    if p_ret is None or p_vol is None or b_ret is None:
        return {"ok": False, "reason": "Return series could not be annualised."}

    rf = RISK_FREE_ANNUAL
    excess = p_ret - rf

    sharpe = (excess / p_vol) if p_vol else None
    treynor = (excess / port_beta) if port_beta else None
    # M²: lever/de-lever the book to the benchmark's volatility, then compare.
    m2 = (excess * (b_vol / p_vol) + rf) if (p_vol and b_vol) else None
    m2_alpha = (m2 - b_ret) if m2 is not None else None
    jensen = p_ret - (rf + port_beta * (b_ret - rf)) if port_beta is not None else None

    active = [p - b for p, b in zip(pr, br)]
    te_sd = _stdev(active)
    te = te_sd * math.sqrt(WEEKS_YEAR) if te_sd is not None else None
    active_ret = p_ret - b_ret
    info_ratio = (active_ret / te) if te else None

    pct = lambda v: round(v * 100.0, 2) if v is not None else None
    num = lambda v: round(v, 3) if v is not None else None
    return {
        "ok": True,
        "basis": "weekly",
        "weeks": len(weeks),
        "risk_free_pct": round(rf * 100.0, 2),
        "portfolio_return_pct": pct(p_ret),
        "portfolio_vol_pct": pct(p_vol),
        "benchmark_return_pct": pct(b_ret),
        "benchmark_vol_pct": pct(b_vol),
        "beta_used": port_beta,
        "sharpe": num(sharpe),
        "treynor": num(treynor),
        "m2_pct": pct(m2),
        "m2_alpha_pct": pct(m2_alpha),
        "jensen_alpha_pct": pct(jensen),
        "tracking_error_pct": pct(te),
        "active_return_pct": pct(active_ret),
        "information_ratio": num(info_ratio),
        # Sharpe and Treynor invert when excess return is negative: a riskier
        # book then scores a *less* negative ratio. Flag it rather than let the
        # ranking be read the wrong way round.
        "excess_negative": excess < 0,
        "note": ("Current weights applied to the last "
                 f"{len(weeks)} weeks of prices — not realised performance."),
    }


def _correlation_block(rows, stock_wret, port_vol_pct=None):
    """Pairwise correlation across holdings + the diversification ratio.

    Effective-holdings counts names; this measures whether they actually move
    apart. Ten NEPSE banks score well on count and badly here, which is the
    distinction that matters for real diversification.

    Diversification ratio follows the CFA curriculum's definition — portfolio
    volatility divided by the AVERAGE single-holding volatility, so LOWER is
    better and 100% means diversification bought nothing.
    """
    syms = [r["symbol"] for r in rows if r["symbol"] in stock_wret]
    if len(syms) < 2:
        return {"ok": False, "reason": "Need at least two priced holdings."}

    pairs, seen = [], {}
    for i, a in enumerate(syms):
        for b in syms[i + 1:]:
            common = sorted(set(stock_wret[a]) & set(stock_wret[b]))
            if len(common) < MIN_WEEKLY_RETURNS:
                continue
            c = _corr([stock_wret[a][w] for w in common],
                      [stock_wret[b][w] for w in common])
            if c is None:
                continue
            pairs.append({"a": a, "b": b, "corr": round(c, 3), "weeks": len(common)})
            seen.setdefault(a, []).append(c)
            seen.setdefault(b, []).append(c)

    if not pairs:
        return {"ok": False, "reason": "Not enough overlapping history between holdings."}

    corrs = [p["corr"] for p in pairs]
    avg = sum(corrs) / len(corrs)
    ranked = sorted(pairs, key=lambda p: p["corr"], reverse=True)

    # Guide's ratio: portfolio vol ÷ average individual vol. The per-holding
    # `vol` column on rows is DAILY-annualised, while port_vol_pct arrives from
    # the weekly performance block — dividing one by the other would compare two
    # sampling frequencies. Re-derive each holding's vol from the same weekly
    # series so numerator and denominator agree.
    vols = []
    for sym in syms:
        sd = _stdev(list(stock_wret[sym].values()))
        if sd:
            vols.append(sd * math.sqrt(WEEKS_YEAR) * 100.0)
    avg_vol = (sum(vols) / len(vols)) if vols else None
    div_ratio = (round(100.0 * port_vol_pct / avg_vol, 1)
                 if (port_vol_pct and avg_vol) else None)

    return {
        "ok": True,
        "pairs_measured": len(pairs),
        "avg_correlation": round(avg, 3),
        "max_correlation": ranked[0],
        "min_correlation": ranked[-1],
        "most_correlated": ranked[:5],
        "least_correlated": ranked[-5:][::-1],
        "avg_holding_vol_pct": round(avg_vol, 2) if avg_vol else None,
        "portfolio_vol_pct": round(port_vol_pct, 2) if port_vol_pct else None,
        "diversification_ratio_pct": div_ratio,
        # Same bands the curriculum uses when judging whether a correlation is
        # high enough to negate the benefit of holding both names.
        "band": ("high" if avg >= 0.75 else "moderate" if avg >= 0.50 else "low"),
    }


def _factor_decomposition(rows, stats, index_wret):
    """Split portfolio risk into systematic (market) vs idiosyncratic, and
    attribute it by sector and by name — the institutional "where does my risk
    come from" view.

    Runs entirely on WEEKLY quantities (beta, residual std and market sigma all
    come from weekly returns) and annualises with ``WEEKS_YEAR``, so the model
    is internally consistent with the weekly-beta estimation.

    Variance model (single NEPSE factor, residuals assumed uncorrelated):
        σ²_p = β_p,eff²·σ²_m + Σ wᵢ²·residᵢ²
    Each holding's risk contribution = wᵢ·βᵢ·β_p,eff·σ²_m (systematic) +
    wᵢ²·residᵢ² (idiosyncratic); contributions sum *exactly* to σ²_p, so the
    sector/name splits are exhaustive. Names lacking return history sit outside
    the covered weight rather than distorting the result.
    """
    idx_vals = list(index_wret.values())
    sigma_m = _stdev(idx_vals) if len(idx_vals) >= MIN_WEEKLY_RETURNS else None
    if not sigma_m:
        return {"ok": False, "reason": "No market history for the factor model."}
    sm2 = sigma_m * sigma_m

    covered = []
    for r in rows:
        st = stats.get(r["symbol"], {})
        b, resid = st.get("beta"), st.get("resid")
        if b is not None and resid is not None:
            covered.append((r, b, resid, r["weight"] / 100.0))
    if not covered:
        return {"ok": False, "reason": "Not enough return history to decompose risk."}

    beta_p = sum(w * b for _r, b, _e, w in covered)
    sys_var = beta_p * beta_p * sm2
    idio_var = sum((w * w) * (e * e) for _r, _b, e, w in covered)
    total_var = sys_var + idio_var
    if total_var <= 0:
        return {"ok": False, "reason": "Degenerate risk (no variance)."}

    sectors, names = {}, []
    for r, b, e, w in covered:
        rc = (w * b * beta_p * sm2) + (w * w * e * e)   # exhaustive contribution
        sectors[r["sector"]] = sectors.get(r["sector"], 0.0) + rc
        names.append((r["symbol"], rc))

    def ann(v):
        return round(math.sqrt(max(v, 0.0) * WEEKS_YEAR) * 100.0, 1)

    sec_rows = sorted(
        ({"sector": s, "pct": round(100.0 * rc / total_var, 1)} for s, rc in sectors.items()),
        key=lambda x: x["pct"], reverse=True,
    )
    name_rows = sorted(
        ({"symbol": s, "pct": round(100.0 * rc / total_var, 1)} for s, rc in names),
        key=lambda x: x["pct"], reverse=True,
    )
    return {
        "ok": True,
        "total_vol_pct": ann(total_var),
        "systematic_vol_pct": ann(sys_var),
        "idiosyncratic_vol_pct": ann(idio_var),
        "systematic_pct": round(100.0 * sys_var / total_var, 1),
        "idiosyncratic_pct": round(100.0 * idio_var / total_var, 1),
        "beta": round(beta_p, 2),
        "covered_weight_pct": round(sum(w for _r, _b, _e, w in covered) * 100.0, 1),
        "sectors": sec_rows[:8],
        "name_contributors": name_rows,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Compliance: investment-policy limit monitoring
# ─────────────────────────────────────────────────────────────────────────────
def _check_max(key, label, current, limit_fmt, detail=""):
    """A 'must stay below' limit (concentration / exposure / VaR)."""
    lim = LIMITS[key]
    status = "breach" if current >= lim["breach"] else "warn" if current >= lim["warn"] else "ok"
    return {"key": key, "label": label, "status": status,
            "current": limit_fmt.format(current), "limit": "≤ " + limit_fmt.format(lim["breach"]),
            "detail": detail}


def build_compliance(rows, sectors, concentration, risk, port_beta, total):
    """Evaluate the portfolio against the default investment-policy limits.

    Returns ``{summary, checks}`` where each check is ok / warn / breach with its
    current value, the limit, and the offending names where relevant. Pure
    roll-up of metrics already computed — no extra queries.
    """
    checks = []
    if not rows or not total:
        return {"summary": {"ok": 0, "warn": 0, "breach": 0}, "checks": []}

    # Single-name concentration.
    over = [r for r in rows if r["weight"] >= LIMITS["single_name"]["warn"]]
    top = concentration.get("top_symbol")
    checks.append(_check_max(
        "single_name", "Single-stock concentration", concentration.get("top_weight", 0.0),
        "{:.1f}%",
        detail=(", ".join(f"{r['symbol']} {r['weight']:.1f}%" for r in over[:4]) if over
                else (f"largest: {top}" if top else "")),
    ))

    # Top-5 concentration.
    top5 = sum(r["weight"] for r in sorted(rows, key=lambda r: r["weight"], reverse=True)[:5])
    checks.append(_check_max("top5", "Top-5 holdings concentration", top5, "{:.1f}%",
                             detail=f"{min(5, len(rows))} largest positions"))

    # Sector concentration.
    if sectors:
        worst = max(sectors, key=lambda s: s["weight"])
        checks.append(_check_max("sector", "Sector concentration", worst["weight"], "{:.1f}%",
                                 detail=f"{worst['sector']} ({worst['weight']:.1f}%)"))

    # Illiquid + untradeable exposure.
    illiq_val = sum(r["value"] for r in rows if r["dtl"] is None or r["dtl"] > DTL_MODERATE)
    untr_val = sum(r["value"] for r in rows if r["dtl"] is None)
    illiq_names = [r for r in rows if (r["dtl"] is None or r["dtl"] > DTL_MODERATE)]
    checks.append(_check_max(
        "illiquid", "Illiquid exposure (>5 days to exit)", 100.0 * illiq_val / total, "{:.1f}%",
        detail=(", ".join(r["symbol"] for r in illiq_names[:5]) if illiq_names else "none"),
    ))
    untr_names = [r["symbol"] for r in rows if r["dtl"] is None]
    checks.append(_check_max("untradeable", "Untradeable exposure (no volume)",
                             100.0 * untr_val / total, "{:.1f}%",
                             detail=(", ".join(untr_names[:6]) + (" +%d more" % (len(untr_names) - 6)
                                     if len(untr_names) > 6 else "")) if untr_names else "none"))

    # 1-day 95% VaR.
    if risk and risk.get("ok"):
        checks.append(_check_max("var_1d_95", "1-day Value at Risk (95%)",
                                 risk["var"]["hist_95_1d_pct"], "{:.2f}%",
                                 detail=f"≈ Rs {risk['var']['hist_95_1d_rs']:,.0f} loss"))

    # Diversification (a minimum, not a maximum).
    eff = concentration.get("effective_holdings", 0.0)
    dlim = LIMITS["diversification"]
    dstatus = "breach" if eff < dlim["breach"] else "warn" if eff < dlim["warn"] else "ok"
    checks.append({"key": "diversification", "label": "Diversification (effective holdings)",
                   "status": dstatus, "current": f"{eff:.1f}", "limit": f"≥ {dlim['breach']:.0f}",
                   "detail": f"{len(rows)} positions"})

    # Portfolio beta band.
    soft, hard = LIMITS["beta"]["soft"], LIMITS["beta"]["hard"]
    if port_beta is None:
        bstatus, bcur, bdetail = "ok", "—", "insufficient history"
    else:
        bstatus = ("breach" if port_beta < hard[0] or port_beta > hard[1]
                   else "warn" if port_beta < soft[0] or port_beta > soft[1] else "ok")
        bcur = f"{port_beta:.2f}"
        bdetail = "more volatile than market" if port_beta > 1 else "less volatile than market"
    checks.append({"key": "beta", "label": "Market beta within band", "status": bstatus,
                   "current": bcur, "limit": f"{hard[0]:.1f}–{hard[1]:.1f}", "detail": bdetail})

    summary = {
        "ok": sum(1 for c in checks if c["status"] == "ok"),
        "warn": sum(1 for c in checks if c["status"] == "warn"),
        "breach": sum(1 for c in checks if c["status"] == "breach"),
    }
    # Worst-first so violations surface at the top.
    order = {"breach": 0, "warn": 1, "ok": 2}
    checks.sort(key=lambda c: order[c["status"]])
    return {"summary": summary, "checks": checks}


# ─────────────────────────────────────────────────────────────────────────────
# Main payload
# ─────────────────────────────────────────────────────────────────────────────
def _days_to_liquidate_value(rows, total_value, target_pct, participation_rate):
    """Continuous days needed to sell a target share of portfolio market value."""
    marked_total = sum(_f(row.get("value")) for row in rows)
    if total_value <= 0 or marked_total <= 0:
        return None
    target_value = marked_total * normalize_liquidation_target(target_pct) / 100.0
    capacities = []
    maximum_tradeable = 0.0
    for row in rows:
        capacity = _f(row.get("adv_qty")) * _f(row.get("price")) * participation_rate
        if capacity > 0 and row["value"] > 0:
            capacities.append((row["value"], capacity))
            maximum_tradeable += row["value"]
    if maximum_tradeable + 0.005 < target_value:
        return None
    low = 0.0
    high = max(value / capacity for value, capacity in capacities)
    for _ in range(60):
        mid = (low + high) / 2.0
        sold = sum(min(value, capacity * mid) for value, capacity in capacities)
        if sold >= target_value:
            high = mid
        else:
            low = mid
    return high


def _liquidity_risk(days):
    if days is None:
        return "untradeable"
    if days <= DTL_LIQUID:
        return "liquid"
    if days <= DTL_MODERATE:
        return "moderate"
    return "illiquid"


def _liquidity_risk_label(tier):
    return LIQUIDITY_RISK_LABELS.get(tier, "Very High")


def _liquidation_scenarios(rows, total_value, custom_target):
    """Return milestone DTL rows for every supported participation limit."""
    targets = list(LIQUIDATION_TARGETS)
    custom = normalize_liquidation_target(custom_target)
    if all(abs(custom - target) > 1e-9 for target in targets):
        targets.append(custom)
    output = {}
    for rate in PARTICIPATION_RATES:
        rate_rows = []
        for target in targets:
            days = _days_to_liquidate_value(rows, total_value, target, rate)
            tier = _liquidity_risk(days)
            rate_rows.append({
                "target_pct": round(target, 1),
                "days": round(days, 2) if days is not None else None,
                "risk": tier,
                "risk_label": _liquidity_risk_label(tier),
                "custom": abs(target - custom) < 1e-9 and target not in LIQUIDATION_TARGETS,
            })
        output[str(round(rate * 100))] = rate_rows
    return output


def _return_attribution(rows, stock_ret, session_dates=None):
    """Static current-weight arithmetic return attribution over one year."""
    contributions = {}
    observations = set(session_dates or [])
    for row in rows:
        symbol_returns = stock_ret.get(row["symbol"], {})
        observations.update(symbol_returns)
        # Compound the daily returns (an arithmetic sum over ~250 sessions
        # drifts far from the geometric return every other figure uses).
        if symbol_returns:
            growth = 1.0
            for r_ in symbol_returns.values():
                growth *= (1.0 + r_)
            contributions[row["symbol"]] = (row["weight"] / 100.0) * (growth - 1.0) * 100.0
        else:
            contributions[row["symbol"]] = None
    total = sum(value for value in contributions.values() if value is not None)
    return contributions, round(total, 2), len(observations)


def build_portfolio_payload(portfolio, participation_rate=PARTICIPATION_RATE,
                            liquidation_target=100.0, fiscal_year=None):
    """Full valuation + risk roll-up for one portfolio (cached briefly)."""
    participation_rate = normalize_participation_rate(participation_rate)
    liquidation_target = normalize_liquidation_target(liquidation_target)
    holdings = list(portfolio.holdings.all())
    latest = _latest_session()
    # Full-resolution timestamp (microseconds) so two imports within the same
    # second don't collide on a 1-second-truncated key and serve stale data.
    ck = (
        f"pf_payload_v{PAYLOAD_VERSION}_{portfolio.id}_{portfolio.updated_at.timestamp()}_{latest}_"
        f"{participation_rate:.2f}_{liquidation_target:.1f}_{fiscal_year or 'latest'}"
    )
    cached = cache.get(ck)
    if cached is not None:
        return cached

    symbols = [h.symbol for h in holdings]
    costs = {c.symbol: c for c in portfolio.costs.all()}  # WACC cost basis by symbol
    prices = _latest_prices(symbols)
    meta = _company_meta(symbols)
    stock_ret, index_ret, stock_wret, index_wret = _load_returns(symbols)
    stats = _per_symbol_stats(symbols, stock_ret, index_ret, stock_wret, index_wret)
    liq, liq_sessions = _liquidity(symbols)

    rows, total = [], 0.0
    for h in holdings:
        live = prices.get(h.symbol)
        if live:
            price, priced_on, mcap, previous_close = live
            price_source = "eod"
        else:
            # Fall back to the imported snapshot price when the scrip isn't in the
            # local EOD table (newly listed / delisted / symbol typo).
            previous_close = _f(h.last_close)
            price = _f(h.ltp) or previous_close
            priced_on, mcap = None, 0.0
            price_source = "snapshot"
        price = max(0.0, _f(price))
        previous_close = max(0.0, _f(previous_close))
        qty = max(0.0, _f(h.quantity))
        value = qty * price
        total += value
        if previous_close > 0:
            day_change = price - previous_close
            day_change_pct = day_change / previous_close * 100.0
            day_pl = day_change * qty
        else:
            day_change = day_change_pct = day_pl = None
        name, sector = meta.get(h.symbol, (h.symbol, "Uncategorized"))
        st = stats.get(h.symbol, {})

        # Per-position DTL at the selected participation limit.
        adv = (liq.get(h.symbol) or {}).get("adv_qty", 0.0)
        if adv > 0 and qty > 0:
            dtl = qty / (participation_rate * adv)
            tier = ("liquid" if dtl <= DTL_LIQUID
                    else "moderate" if dtl <= DTL_MODERATE else "illiquid")
        else:
            dtl, tier = None, "untradeable"

        # Per-holding parametric VaR (95%) at the 1-week / 1-month horizons, as a
        # signed loss (negative) both in % and in ₨ on this position's value.
        v1w = _parametric_var(st.get("vol"), VAR_1W_SESSIONS)
        v1m = _parametric_var(st.get("vol"), VAR_1M_SESSIONS)

        # Cost basis (WACC), matched by symbol from the imported "My WACC" report.
        # Book value marks the CURRENT balance at its average cost; paper P/L is
        # market value minus that. None until the user imports the WACC report.
        cost = costs.get(h.symbol)
        wacc = max(0.0, _f(cost.wacc_rate)) if (cost and cost.wacc_rate is not None) else None
        cost_value = round(wacc * qty, 2) if wacc is not None else None
        pl = round(value - cost_value, 2) if cost_value is not None else None

        rows.append({
            "symbol": h.symbol,
            "name": name,
            "sector": sector,
            "quantity": qty,
            "price": round(price, 2),
            "previous_close": round(previous_close, 2) if previous_close > 0 else None,
            "day_change": round(day_change, 2) if day_change is not None else None,
            "day_change_pct": round(day_change_pct, 2) if day_change_pct is not None else None,
            "day_pl": round(day_pl, 2) if day_pl is not None else None,
            "value": round(value, 2),
            "price_source": price_source,
            "priced_on": priced_on.isoformat() if priced_on else None,
            "market_cap": round(mcap, 2),
            "vol": st.get("vol"),
            "beta": st.get("beta"),
            "wacc": round(wacc, 2) if wacc is not None else None,
            "cost_value": cost_value,
            "pl": pl,
            # Unrealised P/L as a % of what was paid — the ranking key for the
            # winners/losers snapshot. None without a WACC cost basis.
            "pl_pct": (round(100.0 * pl / cost_value, 2)
                       if (pl is not None and cost_value) else None),
            "var_1w_pct": round(-v1w * 100, 2) if v1w is not None else None,
            "loss_1w": round(-v1w * value, 2) if v1w is not None else None,
            "var_1m_pct": round(-v1m * 100, 2) if v1m is not None else None,
            "loss_1m": round(-v1m * value, 2) if v1m is not None else None,
            "adv_qty": round(adv, 2),
            "dtl": round(dtl, 2) if dtl is not None else None,
            "liq_tier": tier,
            "liquidity_risk": _liquidity_risk_label(tier),
        })

    # Weights + concentration.
    for r in rows:
        r["weight"] = round(100.0 * r["value"] / total, 2) if total else 0.0
    rows.sort(key=lambda r: r["value"], reverse=True)

    hhi = sum((r["weight"] / 100.0) ** 2 for r in rows) * 10000.0 if total else 0.0
    eff_n = (10000.0 / hhi) if hhi else 0.0
    top = rows[0] if rows else None
    risk_band = "high" if hhi >= HHI_HIGH else "moderate" if hhi >= HHI_MODERATE else "low"

    # Portfolio beta = Σ wᵢ·βᵢ over holdings that have a beta (re-based to their
    # own weight sum so a few missing betas don't understate it).
    bw = sum(r["weight"] for r in rows if r["beta"] is not None)
    port_beta = (
        round(sum(r["weight"] * r["beta"] for r in rows if r["beta"] is not None) / bw, 2)
        if bw else None
    )
    beta_coverage_pct = round(bw, 1)

    # Sector exposure.
    sec = {}
    for r in rows:
        s = sec.setdefault(r["sector"], {"sector": r["sector"], "value": 0.0, "count": 0,
                                         "cost_value": 0.0, "pl": 0.0, "costed_count": 0})
        s["value"] += r["value"]
        s["count"] += 1
        # Unrealised P/L rolled up per sector, over the costed subset only so a
        # partial WACC import can't drag a sector's return toward zero.
        if r.get("pl") is not None and r.get("cost_value"):
            s["cost_value"] += r["cost_value"]
            s["pl"] += r["pl"]
            s["costed_count"] += 1
    sectors = sorted(sec.values(), key=lambda s: s["value"], reverse=True)
    for s in sectors:
        s["value"] = round(s["value"], 2)
        s["weight"] = round(100.0 * s["value"] / total, 2) if total else 0.0
        has_cost = s["costed_count"] > 0 and s["cost_value"] > 0
        s["cost_value"] = round(s["cost_value"], 2) if has_cost else None
        s["pl"] = round(s["pl"], 2) if has_cost else None
        s["pl_pct"] = (round(100.0 * s["pl"] / s["cost_value"], 2) if has_cost else None)

    # Liquidity: how much of the book can be unwound in 1 / 5 sessions, and the
    # least-liquid names. Fraction of a position sellable in D days = min(1, D/dtl).
    def _liq_pct(days):
        if not total:
            return 0.0
        sellable = 0.0
        for r in rows:
            d = r["dtl"]
            if d is None:
                frac = 0.0            # no ADV → can't be unwound
            elif d <= 0:
                frac = 1.0            # rounds to ~0 days → fully liquidatable
            else:
                frac = min(1.0, days / d)
            sellable += r["value"] * frac
        return round(100.0 * sellable / total, 1)

    priced = [r for r in rows if r["dtl"] is not None]
    wsum = sum(r["value"] for r in priced)
    wavg_days = round(sum(r["value"] * r["dtl"] for r in priced) / wsum, 1) if wsum else None
    least_liquid = sorted(
        rows, key=lambda r: r["dtl"] if r["dtl"] is not None else float("inf"), reverse=True
    )[:6]
    liquidity = {
        "ok": bool(rows),
        "participation_pct": round(participation_rate * 100),
        "participation_options": [round(rate * 100) for rate in PARTICIPATION_RATES],
        "risk_labels": LIQUIDITY_RISK_LABELS,
        "custom_target_pct": round(liquidation_target, 1),
        "lookback_sessions": liq_sessions,
        "lookback_calendar_days": LIQ_LOOKBACK_DAYS,
        "liquidation_scenarios": _liquidation_scenarios(
            rows, total, liquidation_target
        ),
        "liquidatable_1d_pct": _liq_pct(1),
        "liquidatable_5d_pct": _liq_pct(5),
        "wavg_days": wavg_days,
        "illiquid_count": sum(1 for r in rows if r["dtl"] is None or r["dtl"] > DTL_MODERATE),
        "untradeable_count": sum(1 for r in rows if r["dtl"] is None),
        "least_liquid": [
            {"symbol": r["symbol"], "dtl": r["dtl"], "tier": r["liq_tier"],
             "risk_label": r["liquidity_risk"], "adv_qty": r["adv_qty"], "weight": r["weight"]}
            for r in least_liquid
        ],
    }

    index_info = _nepse_index_level()

    # Value at Risk + stress testing (historical simulation on current weights).
    try:
        weight_frac = {r["symbol"]: r["weight"] / 100.0 for r in rows}
        risk = _risk_block(
            weight_frac, total, port_beta, stock_ret, index_info, index_ret.keys()
        )
    except Exception:  # pragma: no cover - never let the risk overlay break valuation
        logger.exception("portfolio VaR/stress failed")
        risk = {"ok": False, "reason": "Risk engine error."}

    # Risk-adjusted performance vs NEPSE, and how correlated the book really is.
    # Both are best-effort overlays: a failure here must never cost the user
    # their valuation, so each degrades to an ok:False block.
    try:
        performance = _performance_block(weight_frac, stock_wret, index_wret, port_beta)
    except Exception:  # pragma: no cover - defensive
        logger.exception("portfolio performance ratios failed")
        performance = {"ok": False, "reason": "Performance engine error."}

    try:
        correlation = _correlation_block(
            rows, stock_wret,
            port_vol_pct=(performance.get("portfolio_vol_pct")
                          if performance.get("ok") else None),
        )
    except Exception:  # pragma: no cover - defensive
        logger.exception("portfolio correlation failed")
        correlation = {"ok": False, "reason": "Correlation engine error."}

    try:
        factors = _factor_decomposition(rows, stats, index_wret)
    except Exception:  # pragma: no cover
        logger.exception("portfolio factor decomposition failed")
        factors = {"ok": False, "reason": "Factor engine error."}

    return_contrib, attributed_return, return_observations = _return_attribution(
        rows, stock_ret, index_ret.keys()
    )
    risk_contrib = {
        item["symbol"]: item["pct"]
        for item in factors.get("name_contributors", [])
    } if factors.get("ok") else {}
    top_holdings = []
    cumulative = 0.0
    for row in rows[:10]:
        cumulative += row["weight"]
        status = (
            "high" if row["weight"] >= LIMITS["single_name"]["breach"]
            else "moderate" if row["weight"] >= LIMITS["single_name"]["warn"]
            else "low"
        )
        top_holdings.append({
            "symbol": row["symbol"],
            "name": row["name"],
            "weight": row["weight"],
            "return_contribution_pct": (
                round(return_contrib[row["symbol"]], 2)
                if return_contrib.get(row["symbol"]) is not None else None
            ),
            "risk_contribution_pct": (
                round(risk_contrib[row["symbol"]], 2)
                if row["symbol"] in risk_contrib else None
            ),
            "cumulative_weight": round(cumulative, 2),
            "concentration_risk": status,
        })

    try:
        compliance = build_compliance(rows, sectors, {
            "top_weight": top["weight"] if top else 0.0,
            "top_symbol": top["symbol"] if top else None,
            "effective_holdings": round(eff_n, 1),
        }, risk, port_beta, total)
    except Exception:  # pragma: no cover
        logger.exception("portfolio compliance failed")
        compliance = {"summary": {"ok": 0, "warn": 0, "breach": 0}, "checks": []}

    # Data-quality grading. Runs last because it annotates `rows` in place and
    # needs the finished weights.
    try:
        quality = _holding_quality(rows, stock_ret, stock_wret, index_wret, index_ret.keys())
        reliability = _metric_reliability(quality, _cost_summary(rows).get("has_cost", False),
                                          bool(performance.get("ok")))
    except Exception:  # pragma: no cover - grading must never break the desk
        logger.exception("portfolio data-quality grading failed")
        quality, reliability = {}, {"items": [], "overall": "amber", "labels": GRADE_LABELS}

    cost_summary = _cost_summary(rows)
    from core_analysis.services.portfolio_ledger import ledger_payload

    accounting = ledger_payload(portfolio, fiscal_year=fiscal_year)
    cash_balance = accounting.get("cash_balance")
    unrealized_pl = cost_summary.get("paper_pl")
    accounting.update({
        "unrealized_pl": unrealized_pl,
        "total_pl": (
            round((accounting.get("all_time_performance") or 0.0) + unrealized_pl, 2)
            if unrealized_pl is not None else None
        ),
        "total_equity": round(total + (cash_balance or 0.0), 2),
    })

    payload = {
        "ok": True,
        "as_of": latest.isoformat() if latest else None,
        "total_value": round(total, 2),
        "holdings_count": len(rows),
        "rows": rows,
        "sectors": sectors,
        "concentration": {
            "hhi": round(hhi),
            "risk": risk_band,
            "effective_holdings": round(eff_n, 1),
            "top_weight": top["weight"] if top else 0.0,
            "top_symbol": top["symbol"] if top else None,
            "top10_weight": round(sum(row["weight"] for row in rows[:10]), 2),
            "top_holdings": top_holdings,
            "attributed_return_pct": attributed_return,
            "return_observations": return_observations,
        },
        "portfolio_beta": port_beta,
        "beta_coverage_pct": beta_coverage_pct,
        "nepse_index": index_info,
        "cost": cost_summary,
        "pl_snapshot": _pl_snapshot(rows),
        "accounting": accounting,
        "snapshot_count": sum(1 for r in rows if r["price_source"] == "snapshot"),
        "risk": risk,
        "performance": performance,
        "correlation": correlation,
        "liquidity": liquidity,
        "compliance": compliance,
        "factors": factors,
        "data_quality": quality,
        "reliability": reliability,
    }
    cache.set(ck, payload, CACHE_TTL)
    return payload
