"""
canslim.py — normalised earnings series for the CAN SLIM screen (C and A).

This module exists because FinancialStatement is an EAV table fed from several
upstream report formats, and two properties of that data will silently produce
wrong growth numbers if they are not handled explicitly.

FINDING 1 — EPS IS STORED UNDER TWO SPELLINGS, AND THEY DO NOT OVERLAP.

    EPS (Annualized)   158 tickers
    EPS (Anualized)    197 tickers      <- upstream typo
    tickers using both:  0

  The misspelling is not a stray row: it is the ONLY spelling for 197 tickers.
  Querying the correct spelling alone returns a clean-looking result covering
  under half the market, with no error to signal the loss. Both are matched.

FINDING 2 — QUARTERLY FIGURES ARE YEAR-TO-DATE, NOT DISCRETE.

  Net profit accumulates through the year (checked on 120 tickers: 120 of 120
  monotonic). NABIL 2024/25 reads 2.06bn / 3.24bn / 5.05bn / 7.13bn — that is
  Q1, H1, 9M, FY, not four quarters. Treating Q4 as "the fourth quarter" would
  overstate it by roughly 3.4x. Discrete quarters are obtained by differencing.

  EPS rows are different again: they are already ANNUALISED run-rates, so they
  must NOT be differenced — but they may be compared like-for-like across years
  at the same quarter.

Both facts mean the two metrics need opposite handling, which is why the raw
table is never read directly by the screen.
"""
from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

# Both spellings, plus the long form some issuers use.
_EPS_RE = re.compile(r"^\s*(eps\b|earnings?\s+per\s+share)", re.I)
# Net-profit variants seen in the data. Deliberately excludes the pre-tax and
# pre-bonus lines, which are a different quantity.
_PROFIT_RE = re.compile(
    r"^\s*(net\s+income|net\s+profit(/loss)?"
    r"(\s+as\s+per\s+profit\s+or\s+loss|\s+after\s+tax)?)\s*(\(rs\.?\s*000\))?\s*$", re.I)
# Never treat these as earnings even though they match loosely.
_EXCLUDE_RE = re.compile(r"variance|before\s+(bonus|tax)|forecast|estimate", re.I)

QUARTERS = (1, 2, 3, 4)


def _fy_sort_key(fy: str) -> tuple:
    """'2024/25' -> (2024, 25) so fiscal years order correctly as strings would not."""
    m = re.match(r"(\d{4})\s*/\s*(\d{2,4})", fy or "")
    return (int(m.group(1)), int(m.group(2))) if m else (0, 0)


# When several item names report the same cell, this ladder decides which one
# is believed. Audited on 90 tickers: 18.4% of profit cells carry MULTIPLE
# matching rows, and 38 disagreed by >5% — ALBSL's 'Net Profit/Loss' rows are
# literal 0 placeholders while 'Net Income (Rs. 000)' holds the real losses.
# "First row from the DB wins" was therefore both wrong and NONDETERMINISTIC
# (the query has no ORDER BY). Lower rank = more trusted; names outside the
# ladder rank last.
_ITEM_PRIORITY = (
    "net income (rs. 000)",                    # most complete series (334 tickers)
    "net profit/loss as per profit or loss",
    "net profit",
    "net profit after tax",
    "eps (annualized)",
    "eps (anualized)",
)


def _item_rank(name: str) -> int:
    n = (name or "").strip().lower()
    for i, known in enumerate(_ITEM_PRIORITY):
        if n == known:
            return i
    return len(_ITEM_PRIORITY)


def _pick(rows: list[dict], pattern: re.Pattern,
          informative_min: float = 100.0) -> dict[tuple[str, int], float]:
    """{(fiscal_year, quarter): amount}: ONE source series per ticker.

    Three designs failed before this one, each on real data:

      * First-match-per-cell (v1) depended on database row order — 18.4% of
        cells had disagreeing candidates, so results were nondeterministic.
      * A global name-priority ladder (v2) broke on NLO: its top-priority
        'Net Income (Rs. 000)' rows are corrupt for that ticker (0, 2, 3, 6…)
        while 'NET PROFIT' holds the real series — yet for ALBSL the corruption
        runs the OTHER way ('Net Profit/Loss' is all zeros, Net Income is
        real). No fixed ladder can be right for both.
      * Mixing sources per-cell also mixes their units/quality across a growth
        calculation: NLO briefly showed "annual growth +112,254%".

    So: group candidates by item name, judge each SERIES on how many
    informative cells it carries (|amount| >= informative_min filters the 0/2/3
    placeholder junk), and use the best series ALONE. Priority rank only breaks
    exact ties (e.g. NABIL, where two names carry identical values). Cells the
    chosen series lacks stay missing — a gap is honest, a spliced-in foreign
    value is not.
    """
    by_name: dict[str, dict[tuple[str, int], float]] = {}
    for r in rows:
        name = r.get("item_name") or ""
        if _EXCLUDE_RE.search(name) or not pattern.search(name):
            continue
        try:
            amt = float(r.get("amount"))
        except (TypeError, ValueError):
            continue
        key = (r.get("fiscal_year_ad") or "", int(r.get("quarter") or 0))
        by_name.setdefault(name.strip(), {}).setdefault(key, amt)
    if not by_name:
        return {}
    def quality(item):
        name, cells = item
        informative = sum(1 for v in cells.values() if abs(v) >= informative_min)
        return (-informative, _item_rank(name), name)
    best = sorted(by_name.items(), key=quality)[0][1]
    return dict(best)


def earnings_series(ticker: str) -> dict[str, Any]:
    """Normalised EPS and net-profit history for one ticker.

    Returns::

        {
          "ticker": str,
          "eps":     {(fy, q): annualised eps},        # NOT differenced
          "profit_ytd":      {(fy, q): cumulative},    # as stored
          "profit_quarter":  {(fy, q): discrete},      # differenced
          "annual":  {fy: full-year profit},           # = Q4 cumulative
          "years":   [fy, ...] oldest first,
        }
    """
    from core_analysis.models import FinancialStatement

    ticker = (ticker or "").strip().upper()
    if not ticker:
        return {"ticker": "", "eps": {}, "profit_ytd": {}, "profit_quarter": {},
                "annual": {}, "years": []}
    rows = list(
        FinancialStatement.objects
        .filter(ticker=ticker, quarter__in=QUARTERS)
        .values("fiscal_year_ad", "quarter", "item_name", "amount")
    )
    eps = _pick(rows, _EPS_RE, informative_min=0.01)
    ytd = _pick(rows, _PROFIT_RE)

    # YTD -> discrete. Q1 is already discrete; each later quarter is the step up
    # from the previous one. A missing intermediate quarter makes the step
    # meaningless, so it is skipped rather than computed across the gap.
    discrete: dict[tuple[str, int], float] = {}
    for (fy, q), v in ytd.items():
        if q == 1:
            discrete[(fy, q)] = v
            continue
        prev = ytd.get((fy, q - 1))
        if prev is not None:
            discrete[(fy, q)] = v - prev

    annual = {fy: v for (fy, q), v in ytd.items() if q == 4}
    years = sorted({fy for fy, _ in ytd} | {fy for fy, _ in eps}, key=_fy_sort_key)
    return {"ticker": ticker, "eps": eps, "profit_ytd": ytd,
            "profit_quarter": discrete, "annual": annual, "years": years}


def _pct(new: float, old: float) -> float | None:
    """Growth %, guarding the sign trap.

    A swing from a LOSS to a profit is not a percentage growth — with a negative
    base the arithmetic returns a negative number for an improvement. Those
    cases return None and are reported as 'n/a (loss base)' instead of a figure
    that reads backwards.
    """
    if old is None or new is None or old == 0 or old < 0:
        return None
    return (new - old) / abs(old) * 100.0



def _growth_status(new: float | None, old: float | None) -> tuple[str, str]:
    """Classify a period-on-period change without inventing a percentage.

    A loss base has no meaningful percentage: a swing from -505m to +639m is a
    recovery, but the arithmetic reports -226%, which reads as collapse. Those
    cases are labelled, never numbered.
    """
    if new is None or old is None:
        return "no_data", "N/A — no comparable period"
    if old < 0 and new >= 0:
        return "recovery", "Recovery — loss to profit"
    if old < 0 and new < 0:
        return "loss_both", "N/A — loss base (still loss-making)"
    if old == 0:
        return "no_base", "N/A — zero base"
    return "measured", ""


def current_earnings(ticker: str) -> dict[str, Any]:
    """C — latest reported quarter vs the SAME quarter a year earlier.

    Same-quarter YoY, never quarter-on-quarter: Nepali earnings are strongly
    seasonal and sequential comparison would read seasonality as growth.
    """
    s = earnings_series(ticker)
    if not s["years"]:
        return {"ok": False, "reason": "no financial statements on file"}

    # Latest reported period across BOTH series — anchoring on EPS alone used
    # the wrong quarter whenever one series lagged the other.
    latest = max((k for k in list(s["eps"]) + list(s["profit_quarter"])),
                 key=lambda k: (_fy_sort_key(k[0]), k[1]), default=None)
    if latest is None:
        return {"ok": False, "reason": "no EPS or profit rows"}

    fy, q = latest
    y0, y1 = _fy_sort_key(fy)
    # Keep the stored width of the second component ('2078/2079' vs '2025/26').
    _w = len((fy or "").split("/")[-1].strip())
    prior_fy = f"{y0 - 1}/{str(y1 - 1).zfill(_w)}"

    eps_now, eps_prev = s["eps"].get((fy, q)), s["eps"].get((prior_fy, q))
    pr_now, pr_prev = s["profit_quarter"].get((fy, q)), s["profit_quarter"].get((prior_fy, q))

    # PRIMARY metric is discrete quarterly profit — that is what O'Neil's "C"
    # means. Annualised EPS is a full-year run-rate and can move the opposite
    # way in the same period (NABIL 2025/26 Q4: EPS +10.9% while the quarter
    # itself fell 45%), so it is carried as CONTEXT ONLY and never scored.
    growth = _pct(pr_now, pr_prev)
    status, label = _growth_status(pr_now, pr_prev)
    return {
        "ok": True, "period": f"{fy} Q{q}", "prior_period": f"{prior_fy} Q{q}",
        # primary
        "profit_quarter": pr_now, "profit_quarter_prior": pr_prev,
        "growth_pct": growth, "status": status, "label": label,
        # context only
        "eps": eps_now, "eps_prior": eps_prev,
        "eps_growth_pct": _pct(eps_now, eps_prev),
        "eps_note": "annualised run-rate — context only, not scored",
    }


def annual_earnings(ticker: str, years: int = 5) -> dict[str, Any]:
    """A — full-year profit history and its growth, newest last.

    Full-year figures come from the Q4 CUMULATIVE row, which is the year total.
    """
    s = earnings_series(ticker)
    hist = sorted(s["annual"].items(), key=lambda kv: _fy_sort_key(kv[0]))[-years:]
    if len(hist) < 2:
        return {"ok": False, "reason": f"only {len(hist)} full year(s) on file",
                "history": [{"fy": f, "profit": v} for f, v in hist]}

    growths = []
    for i in range(1, len(hist)):
        # Only adjacent fiscal years: a missing year would otherwise report a
        # two-year change as one year's growth.
        if _fy_sort_key(hist[i][0])[0] - _fy_sort_key(hist[i - 1][0])[0] != 1:
            continue
        g = _pct(hist[i][1], hist[i - 1][1])
        st, lb = _growth_status(hist[i][1], hist[i - 1][1])
        growths.append({"fy": hist[i][0], "growth_pct": g, "status": st, "label": lb})
    valid = [g["growth_pct"] for g in growths if g["growth_pct"] is not None]
    return {
        "ok": True,
        "history": [{"fy": f, "profit": v} for f, v in hist],
        "growth": growths,
        "avg_growth_pct": (sum(valid) / len(valid)) if valid else None,
        "years_used": len(hist),
        # Consistency matters as much as the average: three good years and one
        # collapse is a different business from four steady ones.
        "positive_years": sum(1 for g in valid if g > 0),
        "measured_years": len(valid),
    }


# ---------------------------------------------------------------------------
# L / S / N / M — price-side factors, and the scorer
#
# I (Institutional Sponsorship) is deliberately absent. NEPSE has no mandatory
# fund-holdings disclosure, so the factor is not measurable in this market. The
# floorsheet broker flow is the tempting substitute and is NOT used: that signal
# failed its own out-of-sample test (spread +1.72%, t=1.02), and dressing an
# unvalidated measure as "institutional sponsorship" would launder it behind a
# famous name. It is reported as unavailable, and — per the scoring rule below —
# an unavailable factor is EXCLUDED from the denominator, never scored as zero.
# ---------------------------------------------------------------------------

import statistics as _stats
from datetime import timedelta as _td

RS_LOOKBACK = 126          # ~6 months of sessions, the usual RS horizon
VOL_RECENT, VOL_BASE = 25, 100
NEW_HIGH_NEAR_PCT = 15.0   # "within 15% of the 52-week high" counts as N
# Fewest stock-specific factors (of C/A/N/S/L) that must be measurable before
# a score is reported at all. See the note in score_stock().
MIN_MEASURED_FACTORS = 3


def _closes(symbol: str, days: int = 400):
    """Recent daily rows for one symbol: (date, close, volume, 52w high, 52w low)."""
    from datetime import date

    from core_analysis.models import NepseDailyStockPrice as P

    return list(P.objects.filter(symbol=symbol.upper(),
                                 business_date__gte=date.today() - _td(days=days))
                .order_by("business_date")
                .values_list("business_date", "close_price",
                             "total_traded_quantity", "fifty_two_week_high",
                             "fifty_two_week_low"))


# NEPSE's daily circuit is 10%, so a one-day RAW drop beyond this is almost
# certainly a bonus/rights ex-date, not trading. 16 of 283 scored stocks had
# one inside the L lookback — HPPL's "-19.7% day" put a top-10 stock's L at
# 3/10 for a corporate action, not weakness.
_GAP_LIMIT = -0.12


def _spliced_growth(closes: list[float]) -> tuple[float | None, bool]:
    """(cumulative return, corp_action_seen) with ex-date gaps zeroed out.

    The adjusted-price table would be the right source, but upstream stopped
    publishing it, so this is the honest fallback: any single-day return below
    _GAP_LIMIT is treated as 0 and the row is FLAGGED approximate. A labelled
    approximation beats an exact-looking number that charges a bonus ex-date
    against the stock's strength.
    """
    if len(closes) < 2:
        return None, False
    growth, gapped = 1.0, False
    for i in range(1, len(closes)):
        prev, cur = closes[i - 1], closes[i]
        if not prev or not cur:
            continue
        r = cur / prev - 1.0
        if r < _GAP_LIMIT:
            gapped = True
            continue
        growth *= 1.0 + r
    return growth - 1.0, gapped


def leader(symbol: str) -> dict[str, Any]:
    """L — relative strength against NEPSE over ~6 months.

    Excess return over the index across the same window. The rule is "buy
    leaders", which is a statement about performance RELATIVE to the market,
    so the index leg must come from the same sessions, not a fixed benchmark.
    """
    from datetime import date

    from core_analysis.models import NepseMarketIndex as I

    rows = _closes(symbol)
    if len(rows) < RS_LOOKBACK + 5:
        return {"ok": False, "reason": f"needs {RS_LOOKBACK} sessions of history"}
    closes = [float(r[1] or 0) for r in rows[-RS_LOOKBACK:]]
    stock_ret, gapped = _spliced_growth(closes)
    if stock_ret is None:
        return {"ok": False, "reason": "no valid base price"}

    idx = list(I.objects.filter(sector_name__iexact="NEPSE Index",
                                business_date__gte=date.today() - _td(days=400))
               .order_by("business_date").values_list("close_index", flat=True))
    if len(idx) < RS_LOOKBACK + 5:
        return {"ok": False, "reason": "insufficient index history"}
    i_now, i_then = float(idx[-1] or 0), float(idx[-RS_LOOKBACK] or 0)
    if not i_then:
        return {"ok": False, "reason": "no valid index base"}
    index_ret = i_now / i_then - 1.0
    return {"ok": True, "stock_return_pct": stock_ret * 100,
            "index_return_pct": index_ret * 100,
            "excess_pct": (stock_ret - index_ret) * 100,
            "lookback_sessions": RS_LOOKBACK,
            "corp_action_in_window": gapped,
            "note": ("a >12% one-day gap (bonus/rights ex-date) was excluded "
                     "from the return — figures are approximate" if gapped else "")}


def supply_demand(symbol: str) -> dict[str, Any]:
    """S — is price rising on EXPANDING volume?

    Volume alone is not the factor: a stock can be heavily traded all the way
    down. The volume ratio is paired with the direction of the same window, so
    heavy trading only counts when price rose on it.
    """
    rows = _closes(symbol)
    if len(rows) < VOL_BASE + 5:
        return {"ok": False, "reason": f"needs {VOL_BASE} sessions of history"}
    vols = [float(r[2] or 0) for r in rows]
    recent, base = _stats.mean(vols[-VOL_RECENT:]), _stats.mean(vols[-VOL_BASE:])
    if not base:
        return {"ok": False, "reason": "no volume history"}
    px_ret, gapped = _spliced_growth([float(r[1] or 0) for r in rows[-VOL_RECENT:]])
    px_chg = px_ret * 100 if px_ret is not None else None
    ratio = recent / base
    return {"ok": True, "volume_ratio": ratio,
            "recent_avg_volume": recent, "base_avg_volume": base,
            "price_change_pct": px_chg,
            "rising_on_volume": bool(px_chg is not None and px_chg > 0 and ratio > 1.0),
            "corp_action_in_window": gapped,
            "window": f"{VOL_RECENT}d vs {VOL_BASE}d"}


def new_high(symbol: str) -> dict[str, Any]:
    """N — position in the 52-week range (the measurable half of N).

    The original N also covers new products, new management and new industry
    conditions. None of that exists in any table here, so this reports ONLY the
    price component and labels itself as partial rather than implying the rest.
    """
    rows = _closes(symbol)
    if not rows:
        return {"ok": False, "reason": "no price history"}
    last = rows[-1]
    close = float(last[1] or 0)
    hi = float(last[3] or 0) or max((float(r[1] or 0) for r in rows[-250:]), default=0)
    lows = [float(r[1]) for r in rows[-250:] if r[1]]
    lo = float(last[4] or 0) or (min(lows) if lows else 0)
    if not hi:
        return {"ok": False, "reason": "no 52-week high on record"}
    # The stored 52-week fields lag by up to a session, so a fresh high can sit
    # ABOVE the stored high (MEPDL read 126% of its range) and a fresh low
    # slightly below the stored low (-1%). Clamp to [0, 100]: above the stored
    # high IS a new high, not a number that breaks the ladder.
    hi = max(hi, close)
    lo = min(lo, close) if lo else close
    below = max(0.0, (hi - close) / hi * 100)
    span = (hi - lo) or 1.0
    pos = min(100.0, max(0.0, (close - lo) / span * 100))
    return {"ok": True, "close": close, "week52_high": hi, "week52_low": lo,
            "pct_below_high": below,
            "position_in_range_pct": pos,
            "at_new_high": close >= hi * 0.999,
            "near_high": below <= NEW_HIGH_NEAR_PCT,
            "covers": "price only — new product/management not in this data"}


def market_direction() -> dict[str, Any]:
    """M — NEPSE trend from the index vs its own 50/150-session averages.

    Market-wide, so it is identical for every stock and computed once.
    """
    from datetime import date

    from core_analysis.models import NepseMarketIndex as I

    idx = list(I.objects.filter(sector_name__iexact="NEPSE Index",
                                business_date__gte=date.today() - _td(days=500))
               .order_by("business_date").values_list("business_date", "close_index"))
    closes = [float(c or 0) for _, c in idx if c]
    if len(closes) < 160:
        return {"ok": False, "reason": "insufficient index history"}
    last = closes[-1]
    ma50, ma150 = _stats.mean(closes[-50:]), _stats.mean(closes[-150:])
    rising50 = ma50 > _stats.mean(closes[-70:-20])
    if last > ma50 > ma150 and rising50:
        stage = "uptrend"
        note = "Index above a rising 50-session average, and above the 150."
    elif last < ma50 < ma150:
        stage = "downtrend"
        note = "Index below both averages — the classic stand-aside condition."
    else:
        stage = "mixed"
        note = "Averages not aligned — no clear market direction."
    return {"ok": True, "stage": stage, "note": note, "index": last,
            "ma50": ma50, "ma150": ma150, "ma50_rising": rising50,
            "as_of": str(idx[-1][0])}


# --- scoring ---------------------------------------------------------------
# Each factor scores 0-10 from an explicit threshold ladder. Thresholds follow
# the published CAN SLIM rules where one exists (C above 25%, price near a new
# high) and are market-relative choices where none does. They are NOT fitted to
# NEPSE returns, and nothing here has been backtested on this market, so the
# result is a screen and must be labelled as one.

def _ladder(value, steps):
    """steps = [(threshold, score), ...] descending; first match wins."""
    if value is None:
        return None
    for thr, sc in steps:
        if value >= thr:
            return sc
    return 0


def score_stock(symbol: str) -> dict[str, Any]:
    """Full CAN SLIM read for one symbol.

    UNAVAILABLE FACTORS ARE EXCLUDED FROM THE DENOMINATOR, never scored zero.
    Zero is a claim ("this stock fails I"); absent is the truth ("this market
    does not publish I"). Scoring the unmeasurable as zero would also penalise
    every NEPSE stock identically and silently cap every score at 6/7.
    """
    symbol = (symbol or "").strip().upper()
    out: dict[str, Any] = {"symbol": symbol, "factors": {}}

    c = current_earnings(symbol)
    c_score = None
    if c.get("ok"):
        if c.get("status") == "recovery":
            # A real improvement, but not a growth rate — scored below a
            # measured strong quarter and above a measured weak one.
            c_score = 6
        elif c.get("status") == "loss_both":
            # Still loss-making. The DISPLAY stays "N/A — loss base" (no fake
            # percentage), but the SCORE must not vanish: excluding it from the
            # denominator made two straight losing quarters score BETTER than a
            # measured -30% quarter. Persistent losses are the worst C outcome.
            c_score = 1
        elif c.get("growth_pct") is not None:
            c_score = _ladder(c["growth_pct"], [(100, 10), (50, 9), (25, 8),
                                                (10, 6), (0, 4), (-25, 2)])
    out["factors"]["C"] = {"name": "Current Earnings", "score": c_score,
                           "available": c_score is not None, "detail": c}

    a = annual_earnings(symbol)
    a_score = None
    # 3+ measured year-transitions required. With 2 or fewer, one rebound year
    # IS the average — TAMOR showed "+4451% (2/2)" off a tiny base and outranked
    # companies with four steady years. Below the floor A is unavailable, which
    # also counts against the 3-factor minimum.
    if (a.get("ok") and a.get("avg_growth_pct") is not None
            and a.get("measured_years", 0) >= 3):
        base = _ladder(a["avg_growth_pct"], [(50, 10), (25, 8), (10, 6), (0, 4)]) or 0
        # Consistency gate: an average carried by a single rebound year is not
        # growth. SHIVM averages +35.7% while being positive in 1 of 4 years.
        if a.get("measured_years"):
            ratio = a["positive_years"] / a["measured_years"]
            base = base * (0.4 + 0.6 * ratio)
        a_score = round(base, 1)
    out["factors"]["A"] = {"name": "Annual Earnings", "score": a_score,
                           "available": a_score is not None, "detail": a}

    n = new_high(symbol)
    n_score = _ladder(n.get("position_in_range_pct"),
                      [(95, 10), (85, 8), (70, 6), (50, 4), (25, 2)]) if n.get("ok") else None
    out["factors"]["N"] = {"name": "New High (price only)", "score": n_score,
                           "available": n_score is not None, "detail": n}

    s = supply_demand(symbol)
    s_score = None
    if s.get("ok"):
        s_score = _ladder(s["volume_ratio"], [(2.0, 10), (1.5, 8), (1.2, 7), (1.0, 5)])
        if s_score is not None and not s["rising_on_volume"]:
            s_score = max(0, s_score - 4)   # volume without price is churn
    out["factors"]["S"] = {"name": "Supply & Demand", "score": s_score,
                           "available": s_score is not None, "detail": s}

    lead = leader(symbol)
    l_score = _ladder(lead.get("excess_pct"),
                      [(50, 10), (25, 9), (10, 7), (0, 5), (-25, 3)]) if lead.get("ok") else None
    out["factors"]["L"] = {"name": "Leader vs Market", "score": l_score,
                           "available": l_score is not None, "detail": lead}

    out["factors"]["I"] = {
        "name": "Institutional Sponsorship", "score": None, "available": False,
        "detail": {"ok": False,
                   "reason": "NEPSE publishes no fund-holdings disclosure, so this "
                             "factor is not measurable in this market",
                   "why_not_proxied": "broker floorsheet flow was considered and "
                                      "rejected: it failed its own out-of-sample "
                                      "test (spread +1.72%, t=1.02)"}}

    m = market_direction()
    m_score = ({"uptrend": 10, "mixed": 5, "downtrend": 1}.get(m.get("stage"))
               if m.get("ok") else None)
    out["factors"]["M"] = {"name": "Market Direction", "score": m_score,
                           "available": m_score is not None, "detail": m}

    # M IS A GATE, NOT A ROW. It is identical for every stock on any given day,
    # so folding it into each stock's average shifts all scores by the same
    # amount and compresses the spread that the screen exists to show. Today it
    # scores 1/10 for all 283 equities — that is a condition on the whole
    # market, reported once above the table, not an attribute of any stock.
    STOCK_FACTORS = ("C", "A", "N", "S", "L")
    scored = {k: out["factors"][k]["score"] for k in STOCK_FACTORS
              if out["factors"][k]["score"] is not None}
    out["measured_factors"] = sorted(scored)
    out["unavailable_factors"] = sorted(k for k in STOCK_FACTORS
                                        if out["factors"][k]["score"] is None) + ["I"]
    # A FLOOR ON HOW MUCH MUST BE MEASURED.
    #
    # Excluding unavailable factors from the denominator is correct, but on its
    # own it lets a single lucky factor stand in for the whole score: new
    # listings (MEPDL, SAPIL) have no earnings history, under 126 sessions of
    # price, and therefore only N — which scored 10, producing a "perfect" 100.0
    # and the top two ranks in the market.
    #
    # Absent is not zero, but absent is not excellent either. Below the floor
    # the honest output is no score at all.
    if len(scored) >= MIN_MEASURED_FACTORS:
        out["score"] = round(sum(scored.values()) / (len(scored) * 10) * 100, 1)
        out["score_basis"] = (f"{len(scored)} of 5 stock factors measured "
                              f"(M is a market gate, I unavailable)")
        out["confidence"] = {5: "full", 4: "partial", 3: "thin"}.get(len(scored), "thin")
    else:
        out["score"] = None
        out["confidence"] = "insufficient"
        out["score_basis"] = (
            f"only {len(scored)} of 5 stock factors measurable — below the "
            f"{MIN_MEASURED_FACTORS}-factor floor, so no score is given "
            f"(typically a new listing with no earnings history)")
    out["market_gate"] = {"stage": m.get("stage"), "score": m_score,
                          "note": m.get("note"), "detail": m}
    out["caveat"] = ("Screen, not a forecast. Thresholds follow the published "
                     "CAN SLIM rules and are NOT fitted to NEPSE; nothing here "
                     "has been backtested on this market.")
    return out


# ---------------------------------------------------------------------------
# Market-wide screen
# ---------------------------------------------------------------------------

CACHE_TTL = 900
# BUMP when the payload shape or any threshold changes, or cached entries from
# the previous shape keep being served for CACHE_TTL.
# v2: deterministic source priority, ex-date splicing in L/S, N clamp,
# loss_both scored 1, A floor of 3 measured years.
SCAN_VERSION = 2


def _universe() -> list[str]:
    """Ordinary equities that actually traded recently.

    Reuses the A/D Radar's non-equity exclusion so both desks agree on what
    counts as a stock — debentures, promoter shares, preference shares and
    closed-end funds have no meaningful earnings growth or 52-week breakout.
    """
    from datetime import date

    from core_analysis.models import NepseDailyStockPrice as P

    from core_analysis.services.accumulation import _non_equity_symbols

    live = set(P.objects.filter(business_date__gte=date.today() - _td(days=60))
               .values_list("symbol", flat=True).distinct())
    return sorted(live - _non_equity_symbols())


def scan_market(sector: str = "All", limit: int = 0) -> dict[str, Any]:
    """Score every ordinary equity and rank them cross-sectionally.

    The rank is the point. A CAN SLIM score of 46 means nothing alone; "12th of
    283" is the statement the method actually makes, because C/A/N/S/L are all
    comparative judgements ("strong growth", "near its high", "a leader").

    Sector selection filters the OUTPUT only — ranks are always computed against
    the whole market, so picking a small sector cannot redefine what "leader"
    means. Same rule as the A/D Radar, learned there the hard way.
    """
    from django.core.cache import cache

    ck = f"canslim_scan_v{SCAN_VERSION}"
    payload = cache.get(ck)
    if payload is None:
        gate = market_direction()
        rows = []
        skipped = {"no_fundamentals": 0, "too_few_factors": 0}
        for sym in _universe():
            try:
                r = score_stock(sym)
            except Exception:            # pragma: no cover - never fail the scan
                logger.exception("CAN SLIM scoring failed for %s", sym)
                continue
            if r.get("score") is None:
                # Distinguish "we could not measure enough" from "nothing at
                # all", so the universe note can say which.
                key = ("too_few_factors" if r.get("confidence") == "insufficient"
                       else "no_fundamentals")
                skipped[key] = skipped.get(key, 0) + 1
                continue
            f = r["factors"]
            c, a = f["C"]["detail"], f["A"]["detail"]
            n, s, l = f["N"]["detail"], f["S"]["detail"], f["L"]["detail"]
            rows.append({
                "symbol": sym,
                "score": r["score"],
                "measured": len(r["measured_factors"]),
                "confidence": r.get("confidence"),
                "C": f["C"]["score"], "A": f["A"]["score"], "N": f["N"]["score"],
                "S": f["S"]["score"], "L": f["L"]["score"],
                # the numbers behind each score, so a row can be audited on sight
                "c_growth_pct": c.get("growth_pct") if c.get("ok") else None,
                "c_status": c.get("status") if c.get("ok") else None,
                "c_period": c.get("period") if c.get("ok") else None,
                "a_avg_pct": a.get("avg_growth_pct") if a.get("ok") else None,
                "a_positive": a.get("positive_years") if a.get("ok") else None,
                "a_measured": a.get("measured_years") if a.get("ok") else None,
                "pct_below_high": n.get("pct_below_high") if n.get("ok") else None,
                "volume_ratio": s.get("volume_ratio") if s.get("ok") else None,
                "rising_on_volume": s.get("rising_on_volume") if s.get("ok") else None,
                "excess_pct": l.get("excess_pct") if l.get("ok") else None,
                # ex-date inside the price windows: L/S were computed on spliced
                # returns and must be presented as approximate.
                "corp_action": bool(l.get("corp_action_in_window")
                                    or s.get("corp_action_in_window")),
            })
        rows.sort(key=lambda r: (-r["score"], r["symbol"]))
        n_rows = len(rows)
        for i, r in enumerate(rows):
            r["rank"] = i + 1
            r["percentile"] = round(100.0 * (n_rows - i) / n_rows, 1) if n_rows else None

        payload = {
            "ok": bool(rows),
            "scan_version": SCAN_VERSION,
            "as_of": gate.get("as_of"),
            "universe": n_rows,
            "rows": rows,
            "skipped": skipped,
            # M sits here, ONCE, not in any row's score.
            "market_gate": gate,
            "unavailable": {
                "I": "NEPSE publishes no fund-holdings disclosure, so "
                     "Institutional Sponsorship cannot be measured in this market. "
                     "Broker floorsheet flow was considered as a proxy and rejected: "
                     "it failed its own out-of-sample test (spread +1.72%, t=1.02).",
                "N_partial": "N covers the PRICE component only. New product, new "
                             "management and new industry conditions are not in any "
                             "data source here.",
            },
            "caveat": ("Screen, not a forecast. Thresholds follow the published "
                       "CAN SLIM rules and are NOT fitted to NEPSE; nothing here "
                       "has been backtested on this market. Ranks are relative to "
                       "the scored universe on this date."),
        }
        cache.set(ck, payload, CACHE_TTL)

    sector = (sector or "All").strip()
    out = dict(payload)
    if sector not in ("", "All"):
        from core_analysis.models import CompanyProfile
        secmap = {c["symbol"]: c["sector_name"] for c in
                  CompanyProfile.objects.values("symbol", "sector_name")}
        out["rows"] = [dict(r, sector=secmap.get(r["symbol"]))
                       for r in payload["rows"] if secmap.get(r["symbol"]) == sector]
        out["sector"] = sector
        out["universe_note"] = (
            f"{len(out['rows'])} {sector} scrip(s) shown, ranked against all "
            f"{payload['universe']} scored equities — the sector filter narrows "
            f"the list, it does not change any rank.")
    else:
        from core_analysis.models import CompanyProfile
        secmap = {c["symbol"]: c["sector_name"] for c in
                  CompanyProfile.objects.values("symbol", "sector_name")}
        out["rows"] = [dict(r, sector=secmap.get(r["symbol"])) for r in payload["rows"]]
        out["sector"] = "All"
        out["universe_note"] = (
            f"{payload['universe']} ordinary equities scored. "
            f"{payload['skipped'].get('too_few_factors', 0)} excluded for having fewer "
            f"than {MIN_MEASURED_FACTORS} measurable factors (mostly new listings), "
            f"{payload['skipped'].get('no_fundamentals', 0)} for no usable earnings history.")
    if limit:
        out["rows"] = out["rows"][:limit]
    return out
