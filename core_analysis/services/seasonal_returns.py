"""
seasonal_returns.py — monthly seasonal-return model for NEPSE indices.

Answers "which calendar months are historically strong / weak for each index".
Built entirely from the local ``NepseMarketIndex`` end-of-day table (no external
call — this is the "our own DB" search the desk runs on):

  * month-end close  = the last close of each calendar month per index,
  * monthly return   = month-over-month % change of those closes,
  * seasonal average = the mean monthly return grouped by calendar month (Jan-Dec)
                       across every year on record.

The payload is fully display-ready (pre-formatted strings + colours) so the
template stays dumb, and it is cached because the underlying scan touches the
whole index history (tens of thousands of rows) and only changes once a day.
"""
from __future__ import annotations

import calendar
import logging
import math

from django.core.cache import cache

from core_analysis.services.RGG_indices import RRG_EXCLUDED_INDICES

logger = logging.getLogger(__name__)

# Broad-market gauges that aren't sector indices (Sensitive / Float / Sensitive
# Float) — excluded from every index analytic. Reuses the RRG exclusion set so
# there is one source of truth across RRG, Seasonal and the sub-index comparison.
EXCLUDED_INDICES = set(RRG_EXCLUDED_INDICES)

CACHE_TTL = 1800  # 30 min — the EOD indices only change once per session.

# NEPSE headline index key (stored mixed-case across the feed; matched iexact).
_NEPSE_INDEX_KEY = "NEPSE INDEX"

# Display order: NEPSE headline first, then the sector sub-indices. Anything not
# listed is appended alphabetically.
_INDEX_ORDER = [
    "NEPSE INDEX",
    "BANKING SUBINDEX",
    "DEVELOPMENT BANK INDEX",
    "FINANCE INDEX",
    "MICROFINANCE INDEX",
    "HOTELS AND TOURISM INDEX",
    "HYDROPOWER INDEX",
    "LIFE INSURANCE",
    "NON LIFE INSURANCE",
    "MANUFACTURING AND PROCESSING",
    "INVESTMENT INDEX",
    "OTHERS INDEX",
    "TRADING INDEX",
    "MUTUAL FUND",   # not in the requested list — kept, placed last
]

MONTH_NAMES = [calendar.month_name[m] for m in range(1, 13)]  # January…December

# Nepali (Bikram Sambat) month labels — romanised (English letters), keyed by
# Gregorian month number. Each Gregorian month is mapped to the BS month that
# BEGINS within it (Magh starts mid-Jan, Baishakh mid-Apr, …) — the usual NEPSE
# convention. Approximate, since BS months straddle two Gregorian months; the
# averages themselves stay Gregorian.
NEPALI_MONTHS = {
    1: "Magh", 2: "Falgun", 3: "Chaitra", 4: "Baishakh", 5: "Jestha", 6: "Ashadh",
    7: "Shrawan", 8: "Bhadra", 9: "Ashwin", 10: "Kartik", 11: "Mangsir", 12: "Poush",
}

# Nepali fiscal-year row order for the seasonality matrix: the FY starts on 1
# Shrawan (≈ mid-July) and ends in Ashadh (≈ mid-July next year). With our
# Gregorian→BS mapping (July = Shrawan), that is simply July→June — i.e. Shrawan,
# Bhadra, Ashwin, Kartik, Mangsir, Poush, Magh, Falgun, Chaitra, Baishakh,
# Jestha, Ashadh.
_FY_MONTH_ORDER = (7, 8, 9, 10, 11, 12, 1, 2, 3, 4, 5, 6)

_POS = "#16a34a"   # green — positive return
_NEG = "#dc2626"   # red   — negative return
_MUTED = "#94a3b8"  # no data

# A calendar month needs at least this many yearly observations before it is
# eligible to be crowned an index's Best / Worst month — one lucky year should
# not define a season.
_MIN_SAMPLE = 5
# At/above this many observations the seasonal average drops its single best and
# single worst year (a symmetric trim) so one 2008/2021-style spike can't own a
# whole calendar month. Below it, too few points to trim — use the plain mean.
_TRIM_MIN = 6

# ── Accumulation / Distribution strategy engine ─────────────────────────────
# Longest seasonal holding window we will recommend (months). A "seasonal trade"
# is a few months, not buy-and-hold, so we cap the search here.
_MAX_HOLD = 7
# A candidate window must have occurred in at least this many years before it is
# eligible — otherwise the "optimal" window is just a lucky one-off.
_MIN_WINDOW_YEARS = 5

# Short institutional tickers for the recommendation table / outlook narrative.
_SHORT_CODE = {
    "NEPSE INDEX": "NEPSE",
    "BANKING SUBINDEX": "BANKING",
    "DEVELOPMENT BANK INDEX": "DEVBANK",
    "FINANCE INDEX": "FINANCE",
    "MICROFINANCE INDEX": "MICROFINANCE",
    "HOTELS AND TOURISM INDEX": "HOTELS",
    "HYDROPOWER INDEX": "HYDROPOWER",
    "LIFE INSURANCE": "LIFEINSU",
    "NON LIFE INSURANCE": "NONLIFEINSU",
    "MANUFACTURING AND PROCESSING": "MANUFACTURE",
    "INVESTMENT INDEX": "INVESTMENT",
    "OTHERS INDEX": "OTHERS",
    "TRADING INDEX": "TRADING",
    "MUTUAL FUND": "MUTUALFUND",
}

# Broad-market gauges that are not investable sectors — kept in the per-index
# table for reference but excluded from the "top sectors" outlook ranking.
_NON_SECTOR = {"NEPSE INDEX", "MUTUAL FUND"}

_CONF_COLOR = {"High": "#16a34a", "Medium": "#d97706", "Low": "#94a3b8", "—": "#94a3b8"}


def _label(name: str) -> str:
    """Human display label for an index code: title-case, but keep 'NEPSE'."""
    words = [("NEPSE" if w.upper() == "NEPSE" else w.capitalize()) for w in name.split()]
    return " ".join(words)


def _fmt(value):
    """Signed percent string, e.g. +2.60% / -4.20% / — for None."""
    if _is_missing(value):
        return "—"
    return f"{'+' if value >= 0 else '-'}{abs(value):.2f}%"


def _color(value):
    if _is_missing(value):
        return _MUTED
    return _POS if value >= 0 else _NEG


def _cell_bg(value, cap):
    """Diverging heat tint (green +, red −) scaled by |value| / cap."""
    if _is_missing(value) or not cap:
        return "transparent"
    alpha = min(1.0, abs(value) / cap) * 0.30
    rgb = "22, 163, 74" if value >= 0 else "220, 38, 38"
    return f"rgba({rgb}, {alpha:.3f})"


def _is_missing(value):
    if value is None:
        return True
    try:
        return math.isnan(float(value))
    except (TypeError, ValueError):
        # Unparseable is missing: returning False here let odd values flow into
        # the formatters, whose comparison blew up the whole seasonal tab.
        return True


def _seasonal_avg(values):
    """Outlier-resistant seasonal mean of a calendar month's yearly returns.

    For a healthy sample (>= ``_TRIM_MIN`` years) the single best and single
    worst year are dropped before averaging, so one extreme year can't define
    the month; smaller samples fall back to the plain arithmetic mean.
    """
    vals = sorted(values)
    if not vals:
        return 0.0
    if len(vals) >= _TRIM_MIN:
        vals = vals[1:-1]   # symmetric trim: drop the min and max year
    return sum(vals) / len(vals)


def _ordered_indices(present):
    ordered = [name for name in _INDEX_ORDER if name in present]
    extras = sorted(name for name in present if name not in _INDEX_ORDER)
    return ordered + extras


def _short(name):
    return _SHORT_CODE.get(name, _label(name))


def _abbr(m):
    """Jan…Dec for calendar month 1…12."""
    return calendar.month_abbr[m]


def _prev_month(m):
    return 12 if m == 1 else m - 1


def _next_month(m):
    return 1 if m == 12 else m + 1


def _compound_window(series, s, length):
    """Year-by-year compounded % returns for a holding window that captures
    ``length`` consecutive months starting at calendar month ``s``. ``series`` is
    a {Period('M'): pct} map; any year missing a month inside its window is
    skipped (a gap you couldn't actually trade through). Chronological order is
    preserved for the split-half stability test.
    """
    out = []
    for p in sorted(q for q in series if q.month == s):
        prod, ok = 1.0, True
        for k in range(length):
            v = series.get(p + k)     # Period + int → k months ahead
            if v is None:             # gap in this year's window — skip the year
                ok = False
                break
            prod *= 1.0 + v / 100.0
        if ok:
            out.append((prod - 1.0) * 100.0)
    return out


def _select_window(series):
    """Pick the (first_gain_month, length) that maximises the trimmed-mean
    compounded return of ``series``, favouring hit-rate then a shorter hold on
    ties. Returns (s, length, score) or None when nothing has enough history.
    """
    starts_by_month = {}
    for p in series:
        starts_by_month.setdefault(p.month, []).append(p)
    best = None
    for s in range(1, 13):
        if len(starts_by_month.get(s, [])) < _MIN_WINDOW_YEARS:
            continue
        for length in range(1, _MAX_HOLD + 1):
            realized = _compound_window(series, s, length)
            if len(realized) < _MIN_WINDOW_YEARS:
                continue
            score = _seasonal_avg(realized)
            hit = sum(1 for r in realized if r > 0) / len(realized)
            key = (round(score, 4), round(hit, 4), -length)
            if best is None or key > best[0]:
                best = (key, s, length, round(score, 2))
    return None if best is None else (best[1], best[2], best[3])


def _split_half_stable(realized):
    """True when the window's realised returns averaged positive in BOTH
    chronological halves; None when too few years to split. Guards against a
    window that only worked in one era (winner's-curse from the wide search)."""
    if len(realized) < 6:
        return None
    half = len(realized) // 2
    first, second = realized[:half], realized[half:]
    return sum(first) / len(first) > 0 and sum(second) / len(second) > 0


def _confidence(hit_rate, years, stable=None):
    """Confidence label from how often the window paid and how many years back it.

    A window that failed the split-half stability test (``stable is False``) is
    downgraded one notch — the pattern only worked in one era of the history.
    """
    if years >= 8 and hit_rate >= 0.70:
        level = "High"
    elif years >= 5 and hit_rate >= 0.60:
        level = "Medium"
    else:
        level = "Low"
    if stable is False:
        level = {"High": "Medium", "Medium": "Low", "Low": "Low"}[level]
    return level


def _month_phase(frac):
    """'early' / 'mid' / 'late' from a 0..1 position within the month's sessions."""
    if frac is None:
        return None
    return "early" if frac <= 0.35 else ("mid" if frac <= 0.65 else "late")


def _empty_row(code, label, years, reason, opp_v=None):
    return {
        "code": code, "label": label, "accumulate": "—", "exit": "—",
        "acc_timing": None, "exit_timing": None,
        "hold": "—", "hold_months": 0, "expected": None,
        "expected_str": "—", "expected_color": _MUTED,
        "opp_str": _fmt(opp_v), "opp_color": _color(opp_v),
        "blend_str": "—", "blend_color": _MUTED,
        "permo_str": "—", "permo_color": _MUTED,
        "hit_str": "—", "years": years or "—",
        "confidence": "—", "conf_color": _CONF_COLOR["—"],
        "summary": reason, "rankable": False,
        "_blend": None, "_first_gain": None,
    }


def _strategy_row(idx, m_series, o_series, timing=None):
    """Display-ready recommendation for one index.

    Entry/exit are chosen on a BLENDED per-month signal — the average of the real,
    bookable month-over-month return and the Opp % (hindsight best-case intra-month
    swing) — so both the realistic drift and the tradable range steer the window.
    We then report each leg separately: Expected Return % (realistic, what you can
    book), Opp % (hindsight ceiling), and the Blend used to rank.
    """
    code, label = _short(idx), _label(idx)
    m = {p: float(v) for p, v in m_series.items()}
    o = {p: float(v) for p, v in (o_series or {}).items()}
    common = set(m) & set(o)
    if common:                        # blend only where BOTH signals exist
        m_use = {p: m[p] for p in common}
        o_use = {p: o[p] for p in common}
        blend = {p: (m[p] + o[p]) / 2.0 for p in common}
        has_opp = True
    else:                             # no opportunity history → fall back to return
        m_use, o_use, blend, has_opp = m, {}, m, False

    sel = _select_window(blend) if blend else None
    if not sel:
        return _empty_row(code, label, len(m), "Insufficient history for a seasonal call.")
    s, L, blend_score = sel

    # Expected Return is computed over the FULL return history, not just the
    # years where opp data also exists — the blend needs the intersection, the
    # bookable-return column does not, and truncating it understated "Yrs".
    real = _compound_window(m, s, L)              # realistic, year-by-year
    if len(real) < _MIN_WINDOW_YEARS:
        return _empty_row(code, label, len(real), "Insufficient history for a seasonal call.")
    expected = round(_seasonal_avg(real), 2)
    opp_realized = _compound_window(o_use, s, L) if has_opp else []
    opp_v = round(_seasonal_avg(opp_realized), 2) if opp_realized else None
    hit = sum(1 for r in real if r > 0) / len(real)   # hit-rate on the REAL trade
    stable = _split_half_stable(real)

    if expected <= 0:                 # blend may be opp-driven; a real long still needs a real edge
        return _empty_row(
            code, label, len(real),
            "No positive seasonal return — range only (see Opp %); avoid a seasonal long.",
            opp_v=opp_v)

    entry_m = _prev_month(s)                       # buy at this month's close
    exit_m = (s + L - 2) % 12 + 1                  # last captured month → sell at close
    after_m = _next_month(exit_m)                  # first month you skip
    conf = _confidence(hit, len(real), stable)
    # Week-of-month guidance: where the ENTRY month's low and the EXIT month's
    # high have historically printed (early / mid / late) — refines "Mar/Apr"
    # into "buy around mid-Mar" / "sell around early-Jul". A tendency, not a rule.
    t = timing or {}
    acc_phase = _month_phase((t.get("low") or {}).get(entry_m))
    exit_phase = _month_phase((t.get("high") or {}).get(exit_m))
    acc_timing = f"{acc_phase} {_abbr(entry_m)}" if acc_phase else None
    exit_timing = f"{exit_phase} {_abbr(exit_m)}" if exit_phase else None
    # Monthly-equivalent (geometric) return so different hold lengths compare on
    # capital-efficiency, not just absolute window size.
    permo = ((1.0 + expected / 100.0) ** (1.0 / L) - 1.0) * 100.0
    summary = (f"Accumulate {_abbr(entry_m)}/{_abbr(s)}, exit "
               f"{_abbr(exit_m)}/{_abbr(after_m)}. ~{L}-mo hold.")
    if acc_timing and exit_timing:
        summary += f" Low ≈ {acc_timing}, high ≈ {exit_timing}."
    if stable is False:
        summary += " Pattern weaker in one half of history."
    return {
        "code": code, "label": label,
        "accumulate": f"{_abbr(entry_m)}/{_abbr(s)}",
        "exit": f"{_abbr(exit_m)}/{_abbr(after_m)}",
        "acc_timing": acc_timing, "exit_timing": exit_timing,
        "hold": f"{_abbr(entry_m)}→{_abbr(exit_m)} · ~{L} mo",
        "hold_months": L,
        "expected": expected,
        "expected_str": _fmt(expected), "expected_color": _color(expected),
        "opp_str": _fmt(opp_v), "opp_color": _color(opp_v),
        "blend_str": _fmt(blend_score), "blend_color": _color(blend_score),
        "permo_str": _fmt(round(permo, 2)), "permo_color": _color(permo),
        "hit_str": f"{round(hit * 100)}%",
        "years": len(real),
        "confidence": conf, "conf_color": _CONF_COLOR[conf],
        "summary": summary,
        # kept for the outlook narrative / ranking
        "_blend": blend_score, "_first_gain": s, "rankable": True,
    }


def _future_period(month, ref):
    """The next occurrence of `month` at or after `ref` (a Period('M'))."""
    import pandas as pd
    yr = ref.year + (1 if month < ref.month else 0)
    return pd.Period(year=yr, month=month, freq="M")


def _build_strategy(order, monthly_ret, opp_ret, month_timing, current_period):
    """Rank every index by its optimal blended window and write a market outlook."""
    rows = [_strategy_row(idx, monthly_ret[idx], opp_ret.get(idx), month_timing.get(idx))
            for idx in order if idx in monthly_ret]
    # Rank by the blended score (avg of realistic return + Opp %); non-tradable
    # rows (no positive real edge / thin history) sink to the bottom.
    rows.sort(key=lambda r: (r["rankable"], r["_blend"] if r["_blend"] is not None else -1e9),
              reverse=True)

    # Investable sectors only for the "top sectors" narrative.
    non_sector_codes = {_short(n) for n in _NON_SECTOR}
    sectors = [r for r in rows if r["rankable"] and r["code"] not in non_sector_codes]
    top = sectors[:5]

    cur_name = calendar.month_name[current_period.month]
    sentences, phase, horizon = [], "Neutral", ""

    nepse = next((r for r in rows if r["code"] == "NEPSE" and r["rankable"]), None)
    if nepse:
        s = nepse["_first_gain"]
        L = nepse["hold_months"]
        entry_m, exit_m = _prev_month(s), (s + L - 2) % 12 + 1
        held = {(s + k - 1) % 12 + 1 for k in range(L)}   # captured (strong) months
        cm = current_period.month
        if cm == exit_m or cm == _next_month(exit_m):
            phase = "Distribution"
            sentences.append(
                f"Market seasonality turns bearish after {calendar.month_name[exit_m]}-end — "
                f"reduce/exit as the historically strong window closes.")
        elif cm in held:
            phase = "Hold"
            sentences.append(
                f"Seasonality is still supportive; the NEPSE strong window runs through "
                f"{calendar.month_name[exit_m]}. Hold, then distribute into strength.")
        elif (s - cm) % 12 <= 2:
            # Within ~2 months of the strong window opening → start building.
            phase = "Accumulation"
            sentences.append(
                f"After {calendar.month_name[_prev_month(entry_m)]} the seasonal backdrop "
                f"improves — accumulation opportunities open around "
                f"{calendar.month_name[entry_m]} ({_abbr(entry_m)}/{_abbr(s)}).")
        else:
            # Between the exit and the next entry — seasonally soft stretch.
            phase = "Wait"
            sentences.append(
                f"Seasonally neutral-to-soft stretch — the next NEPSE accumulation "
                f"window opens around {calendar.month_name[entry_m]} "
                f"({_abbr(entry_m)}/{_abbr(s)}); stay patient until then.")

    if top:
        # Horizon = the ENVELOPE of the top-3 windows (earliest upcoming entry →
        # latest exit) so every sector named in the sentence actually falls
        # inside the stated range.
        spans = []
        for r in top[:3]:
            fg = _future_period(r["_first_gain"], current_period)
            spans.append((fg - 1, fg + (r["hold_months"] - 1)))   # accumulate → exit
        h_start = min(a for a, _ in spans)
        h_end = max(b for _, b in spans)
        horizon = f"{_abbr(h_start.month)} {h_start.year} – {_abbr(h_end.month)} {h_end.year}"
        # Quote the BLEND — the metric the table is ranked by. Quoting the
        # realistic Expected Return here made a lower-ranked sector show a
        # higher number than the "top" ones, which read as a mis-ordering.
        lead = ", ".join(
            f"{r['code']} ({r['blend_str']} blended; accumulate {r['accumulate']}"
            f"{f' ({r_at})' if (r_at := r.get('acc_timing')) else ''}, exit {r['exit']})"
            for r in top[:3])
        sentences.append(f"Top sectors for {horizon} are {lead}.")
        if len(top) > 3:
            extra = ", ".join(f"{r['code']} ({r['blend_str']})" for r in top[3:5])
            sentences.append(f"Other attractive sectors include {extra}.")

    return {
        "ok": bool(rows),
        "generated_for": f"{cur_name} {current_period.year}",
        "phase": phase,
        "horizon": horizon,
        "rows": rows,
        "top": top,
        "outlook": " ".join(sentences) or
                   "Not enough seasonal history to form a market outlook.",
    }


def _compute():
    import pandas as pd

    from core_analysis.models import NepseMarketIndex

    rows = NepseMarketIndex.objects.values_list(
        "sector_name", "business_date", "close_index", "high_index", "low_index")
    df = pd.DataFrame.from_records(list(rows), columns=["index", "date", "close", "high", "low"])
    if df.empty:
        return {"ok": False, "reason": "No index history available."}

    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    df["high"] = pd.to_numeric(df["high"], errors="coerce")
    df["low"] = pd.to_numeric(df["low"], errors="coerce")
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    # The index name is stored in mixed case across 29 years of feed ("NEPSE Index"
    # vs "NEPSE INDEX"). MySQL's case-insensitive collation treats them as one, but
    # pandas would split them into separate series — so normalise to a single
    # upper-case key before grouping, matching _INDEX_ORDER.
    df["index"] = df["index"].astype(str).str.strip().str.upper()
    df = df.dropna(subset=["date", "close"])
    df = df[(df["index"] != "") & (df["close"] > 0)]
    # Drop the non-sector broad-market gauges (Sensitive / Float / Sensitive Float).
    df = df[~df["index"].isin(EXCLUDED_INDICES)]
    if df.empty:
        return {"ok": False, "reason": "No usable index closes."}

    rows_processed = int(len(df))
    coverage = f"{df['date'].min():%Y-%m-%d} to {df['date'].max():%Y-%m-%d}"
    # The month currently in progress (only a few sessions old) is a partial-month
    # return — kept for "Latest Monthly Return" but excluded from the seasonal
    # AVERAGE so a 1-day print doesn't skew that calendar month's history.
    current_period = df["date"].max().to_period("M")

    # Per index: month-end close → monthly return → seasonal (per-calendar-month) mean.
    avg_by_month, avg_opp_by_month, latest, best_worst = {}, {}, {}, {}
    sample_counts = {}   # idx → {calendar_month: number of yearly observations}
    monthly_ret = {}     # idx → Period('M')-indexed month% returns (strategy engine)
    opp_ret = {}         # idx → {Period('M'): intra-month opportunity %} (blend engine)
    month_timing = {}    # idx → {'low'/'high': {calendar_month: avg position 0..1}}
    for idx, g in df.groupby("index"):
        g = g.sort_values("date")
        by_period = g.groupby(g["date"].dt.to_period("M"))
        # Last close of each calendar month (period index keeps months ordered).
        monthly = by_period["close"].last()
        if len(monthly) < 2:
            continue
        # Reindex onto a gap-free monthly range so a MISSING month (holiday
        # closures, or a sub-index that launched mid-history) becomes NaN rather
        # than letting pct_change bridge the gap and mislabel a multi-month move
        # as that month's single-month return. Both the gap month and the month
        # after it then drop out — you genuinely can't form a 1-month return there.
        full_range = pd.period_range(monthly.index.min(), monthly.index.max(), freq="M")
        monthly = monthly.reindex(full_range)
        ret = monthly.pct_change().dropna() * 100.0
        if ret.empty:
            continue
        # Complete months only (drop the in-progress partial) for the strategy engine.
        monthly_ret[idx] = ret[ret.index != current_period]

        buckets = {}
        for period, r in ret.items():
            if period == current_period:
                continue  # exclude the in-progress month from the seasonal mean
            buckets.setdefault(period.month, []).append(float(r))
        if not buckets:
            continue
        counts = {m: len(v) for m, v in buckets.items()}
        means = {m: _seasonal_avg(v) for m, v in buckets.items()}
        avg_by_month[idx] = {m: round(v, 2) for m, v in means.items()}
        sample_counts[idx] = counts

        # Directional intra-month opportunity: from the earliest trading day's High
        # (if the month's high came first) down to the month low, or from that day's
        # Low (if the low came first) up to the month high — the tradable swing, not
        # just open→close. Averaged per calendar month, partial month excluded.
        opp_buckets = {}
        opp_series = {}   # per-period opportunity for the blended strategy signal
        low_pos_buckets, high_pos_buckets = {}, {}   # where in the month the low/high prints
        for period, gm in by_period:
            if period == current_period:
                continue
            gm = gm.sort_values("date")
            m_high, m_low = gm["high"].max(), gm["low"].min()
            if _is_missing(m_high) or _is_missing(m_low):
                continue
            first = gm.iloc[0]
            # ~63% of NEPSE index bars have high == low, so the month's extreme
            # is usually a TIE across many sessions and idxmax/idxmin's
            # first-occurrence rule skews every date toward the start of the
            # month. Taking the middle of the tied dates removes that bias.
            hh_dates = gm.loc[gm["high"] == m_high, "date"].tolist()
            ll_dates = gm.loc[gm["low"] == m_low, "date"].tolist()
            hh_date = hh_dates[len(hh_dates) // 2]
            ll_date = ll_dates[len(ll_dates) // 2]
            # Intra-month timing: the low/high's position among the month's
            # sessions (0→first day, 1→last day). Averaged per calendar month it
            # tells the desk WHEN in the month accumulation lows / exit highs
            # have historically printed (early / mid / late).
            n_days = len(gm)
            if n_days >= 3:
                dlist = gm["date"].tolist()
                low_pos_buckets.setdefault(period.month, []).append(
                    (dlist.index(ll_date) + 1) / n_days)
                high_pos_buckets.setdefault(period.month, []).append(
                    (dlist.index(hh_date) + 1) / n_days)
            if hh_date == ll_date:
                # One session is both the month's high and its low (a flat bar,
                # or one dominant day) — direction is ambiguous, so take it
                # from the month's actual close-to-close drift instead of
                # defaulting to "decline".
                declining = gm.iloc[-1]["close"] < first["close"]
            else:
                declining = hh_date < ll_date
            if declining:                                   # high peaked first → decline
                ref, target = first["high"], m_low
            else:                                           # low bottomed first → rise
                ref, target = first["low"], m_high
            if _is_missing(ref) or _is_missing(target) or ref <= 0:
                continue
            opp_val = (target - ref) / ref * 100.0
            opp_buckets.setdefault(period.month, []).append(opp_val)
            opp_series[period] = opp_val
        if opp_buckets:
            avg_opp_by_month[idx] = {m: round(sum(v) / len(v), 2) for m, v in opp_buckets.items()}
        if opp_series:
            opp_ret[idx] = opp_series
        if low_pos_buckets:
            month_timing[idx] = {
                "low": {m: sum(v) / len(v) for m, v in low_pos_buckets.items()},
                "high": {m: sum(v) / len(v) for m, v in high_pos_buckets.items()},
            }

        # "Latest Monthly Return" = the most recent COMPLETE month, i.e. exclude
        # the in-progress month so the figure is a full-month move, not a 1-day
        # partial print. Fall back to whatever exists if only the partial month is.
        complete = [p for p in ret.index if p != current_period]
        last_period = complete[-1] if complete else ret.index[-1]
        latest[idx] = {
            "month": last_period.month,
            "year": last_period.year,
            "value": round(float(ret.loc[last_period]), 2),
        }
        # Rank Best / Worst only over months with enough yearly observations so a
        # single-year fluke can't top the list; if nothing clears the bar (young
        # index) fall back to the full set rather than showing nothing.
        eligible = {m: v for m, v in means.items() if counts.get(m, 0) >= _MIN_SAMPLE}
        pool = eligible or means
        best_m = max(pool, key=pool.get)
        worst_m = min(pool, key=pool.get)
        best_worst[idx] = {
            "best_month": calendar.month_name[best_m], "best_value": round(pool[best_m], 2),
            "best_n": counts.get(best_m, 0),
            "worst_month": calendar.month_name[worst_m], "worst_value": round(pool[worst_m], 2),
            "worst_n": counts.get(worst_m, 0),
        }

    present = list(avg_by_month.keys())
    if not present:
        return {"ok": False, "reason": "Not enough monthly history to build seasonality."}
    order = _ordered_indices(present)

    # Latest Monthly Return — one row per index in the fixed matrix order.
    # Alongside the actual latest-month return we show that calendar month's
    # seasonal Avg Return and Opportunity, plus the actual-vs-average difference.
    def _latest_row(idx):
        lt = latest[idx]
        m, yr = lt["month"], lt["year"]
        avg_v = avg_by_month[idx].get(m)
        opp_v = (avg_opp_by_month.get(idx) or {}).get(m)
        avg_n = (sample_counts.get(idx) or {}).get(m, 0)   # years behind that Avg
        # Difference = latest completed month's actual Return vs its seasonal Avg
        # Return (positive = the month beat its historical norm).
        diff_v = (lt["value"] - avg_v) if avg_v is not None else None
        return {
            "label": _label(idx),
            "period_en": f"{calendar.month_name[m]} {yr}",
            "period_np": f"{NEPALI_MONTHS.get(m, '')} {yr}",
            "value_str": _fmt(lt["value"]), "value_color": _color(lt["value"]),
            "avg_str": _fmt(avg_v), "avg_color": _color(avg_v), "avg_n": avg_n,
            "opp_str": _fmt(opp_v), "opp_color": _color(opp_v),
            "diff_str": _fmt(diff_v), "diff_color": _color(diff_v),
            "value": lt["value"],
        }

    # Kept in the fixed index order (not sorted by value) so every table lines up.
    latest_rows = [_latest_row(idx) for idx in order]

    # Best / Worst month by index (average), in the fixed index order.
    bestworst_rows = [
        {
            "label": _label(idx),
            "best_month": best_worst[idx]["best_month"],
            "best_str": _fmt(best_worst[idx]["best_value"]),
            "best_n": best_worst[idx]["best_n"],
            "worst_month": best_worst[idx]["worst_month"],
            "worst_str": _fmt(best_worst[idx]["worst_value"]),
            "worst_n": best_worst[idx]["worst_n"],
            "latest_str": _fmt(latest[idx]["value"]),
            "latest_color": _color(latest[idx]["value"]),
        }
        for idx in order
    ]

    # Seasonality matrix (rows = months, cols = indices). Each cell carries BOTH
    # the average monthly return and the directional opportunity %, each on its own
    # heat scale, so the desk can toggle between the two metrics client-side.
    cap_ret = max(
        (abs(v) for mm in avg_by_month.values() for v in mm.values()), default=1.0,
    ) or 1.0
    cap_opp = max(
        (abs(v) for mm in avg_opp_by_month.values() for v in mm.values()), default=1.0,
    ) or 1.0
    def _cell(rv, ov):
        return {
            "ret_text": _fmt(rv), "ret_color": _color(rv), "ret_bg": _cell_bg(rv, cap_ret),
            "opp_text": _fmt(ov), "opp_color": _color(ov), "opp_bg": _cell_bg(ov, cap_opp),
        }

    def _avg(vals):
        vals = [v for v in vals if not _is_missing(v)]
        return round(sum(vals) / len(vals), 2) if vals else None

    columns = [{"key": idx, "label": _label(idx)} for idx in order]
    matrix_rows = []
    for m in _FY_MONTH_ORDER:   # Nepali fiscal-year order: Shrawan (Jul) → Ashadh (Jun)
        rets = [avg_by_month[idx].get(m) for idx in order]
        opps = [(avg_opp_by_month.get(idx) or {}).get(m) for idx in order]
        cells = [_cell(rets[i], opps[i]) for i in range(len(order))]
        matrix_rows.append({
            "month": calendar.month_name[m],
            "month_np": NEPALI_MONTHS.get(m, ""),
            "cells": cells,
            # Month-wise average across all indices (right-hand "Avg" column).
            "row_avg": _cell(_avg(rets), _avg(opps)),
        })

    # Total-average row: per index, the mean of its 12 monthly values (both metrics)
    # — the index's average monthly Return / Opportunity across the year. The final
    # "grand" cell is the overall average across every index and month.
    total_cells = [
        _cell(_avg(avg_by_month.get(idx, {}).values()),
              _avg((avg_opp_by_month.get(idx) or {}).values()))
        for idx in order
    ]
    grand = _cell(
        _avg([v for mm in avg_by_month.values() for v in mm.values()]),
        _avg([v for mm in avg_opp_by_month.values() for v in mm.values()]),
    )
    total_row = {"label": "Average", "cells": total_cells, "grand": grand}

    # Accumulation / distribution recommendations + market outlook, ranked by the
    # optimal seasonal window's blended score. Fully derived from the same
    # monthly returns above, so it refreshes automatically with new history.
    strategy = _build_strategy(order, monthly_ret, opp_ret, month_timing, current_period)

    return {
        "ok": True,
        "coverage": coverage,
        "strategy": strategy,
        "indices_count": len(present),
        "rows_processed": rows_processed,
        "rows_processed_str": f"{rows_processed:,}",
        "latest_rows": latest_rows,
        "bestworst_rows": bestworst_rows,
        "columns": columns,
        "matrix_rows": matrix_rows,
        "total_row": total_row,
    }


def build_seasonal_payload():
    """Cached seasonal-return payload for the RRG → Seasonal desk."""
    from core_analysis.models import NepseMarketIndex

    latest = (
        NepseMarketIndex.objects.order_by("-business_date")
        .values_list("business_date", flat=True)
        .first()
    )
    # v5: bump the version whenever the payload SHAPE changes so a deploy never
    # serves a stale-schema payload from a still-warm cache entry.
    ck = f"seasonal_returns_v6_{latest}"
    cached = cache.get(ck)
    if cached is not None:
        return cached
    try:
        payload = _compute()
    except Exception:  # pragma: no cover - never let seasonality break the tab
        logger.exception("seasonal returns computation failed")
        payload = {"ok": False, "reason": "Seasonal engine error."}
    # Failures get a short TTL so a transient DB hiccup doesn't pin the error
    # message on the tab for the full cache window.
    cache.set(ck, payload, CACHE_TTL if payload.get("ok") else 60)
    return payload


# ── Market-cycle (Weinstein 4-stage) chart for the NEPSE Index ───────────────
# Operationalises the "NEPSE Market Cycle Stage Classification" SOP as a chart:
# the NEPSE Index close with its 50 / 150 (30-week) / 200-day MAs, every session
# tagged with a Weinstein stage so the chart can shade the timeline by stage.
_CYCLE_WINDOW = 750          # sessions shown (~3 trading years); MAs use full history
_MC_SLOPE_LOOKBACK = 10      # bars for the 150-day MA slope
_MC_SLOPE_BAND = 0.3         # ± % over the lookback that counts as "flat"
_STAGE_META = {
    1: {"name": "Stage 1 · Base", "color": "#94a3b8"},
    2: {"name": "Stage 2 · Advance", "color": "#16a34a"},
    3: {"name": "Stage 3 · Top", "color": "#eab308"},
    4: {"name": "Stage 4 · Decline", "color": "#dc2626"},
}


def _classify_cycle_stage(close, ma50, ma150):
    """Vectorised Weinstein stage per session from price vs. the 150-day (30-week)
    baseline and the 50-day trigger. Priority Stage 2 > 4 > 3 > 1, matching the
    SOP: buy strength, avoid confirmed declines, flag tops that roll under the
    50-day while the baseline stalls, otherwise treat it as a base."""
    import numpy as np

    slope = (ma150 - ma150.shift(_MC_SLOPE_LOOKBACK)) / ma150.shift(_MC_SLOPE_LOOKBACK) * 100.0
    rising = slope > _MC_SLOPE_BAND
    falling = slope < -_MC_SLOPE_BAND
    above = close > ma150
    s2 = above & rising
    s4 = (~above) & falling
    # Topping: rolled under the 50-day but not yet decisively below the baseline.
    s3 = (close < ma50) & (close >= ma150 * 0.95)
    stage = np.select([s2, s4, s3], [2, 4, 3], default=1)
    stage = np.where(ma150.notna() & ma50.notna(), stage, 0).astype(int)
    return stage


def build_market_cycle(window=_CYCLE_WINDOW):
    """Cached NEPSE-Index market-cycle chart payload (close + MAs + stage bands).

    Self-contained: reads only the NEPSE Index history, so it never disturbs the
    seasonal engine. Returns a JSON-serialisable dict for the Seasonal desk chart.
    """
    from core_analysis.models import NepseMarketIndex

    latest = (
        NepseMarketIndex.objects.filter(sector_name__iexact=_NEPSE_INDEX_KEY)
        .order_by("-business_date")
        .values_list("business_date", flat=True)
        .first()
    )
    if latest is None:
        return {"ok": False, "reason": "No NEPSE Index history available."}
    ck = f"market_cycle_v1_{latest}_{window}"
    cached = cache.get(ck)
    if cached is not None:
        return cached
    try:
        payload = _compute_market_cycle(window)
    except Exception:  # pragma: no cover - never let the chart break the tab
        logger.exception("market cycle computation failed")
        payload = {"ok": False, "reason": "Market-cycle engine error."}
    cache.set(ck, payload, CACHE_TTL if payload.get("ok") else 60)
    return payload


def _compute_market_cycle(window):
    import pandas as pd

    from core_analysis.models import NepseMarketIndex

    rows = (
        NepseMarketIndex.objects.filter(sector_name__iexact=_NEPSE_INDEX_KEY)
        .order_by("business_date")
        .values_list("business_date", "close_index")
    )
    df = pd.DataFrame.from_records(list(rows), columns=["date", "close"])
    if df.empty:
        return {"ok": False, "reason": "No NEPSE Index history available."}
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    df = df.dropna(subset=["date", "close"])
    df = df[df["close"] > 0].drop_duplicates(subset="date").sort_values("date")
    if len(df) < 60:
        return {"ok": False, "reason": "Not enough NEPSE history for a cycle read."}

    df["ma50"] = df["close"].rolling(50).mean()
    df["ma150"] = df["close"].rolling(150).mean()   # 30-week baseline
    df["ma200"] = df["close"].rolling(200).mean()
    df["stage"] = _classify_cycle_stage(df["close"], df["ma50"], df["ma150"])

    # 50/200 golden & death crosses (computed on the full series, filtered to the
    # window below) so the marker sits on the exact crossover session.
    cross_sign = (df["ma50"] - df["ma200"])
    df["cross"] = ""
    prev = cross_sign.shift(1)
    df.loc[(prev <= 0) & (cross_sign > 0), "cross"] = "golden"
    df.loc[(prev > 0) & (cross_sign <= 0), "cross"] = "death"

    tail = df.tail(window).copy()

    def _ms(ts):
        return int(pd.Timestamp(ts).value // 1_000_000)   # epoch ms (UTC)

    def _r(v):
        return None if pd.isna(v) else round(float(v), 2)

    points = [
        {"x": _ms(r.date), "c": _r(r.close), "m50": _r(r.ma50),
         "m150": _r(r.ma150), "m200": _r(r.ma200)}
        for r in tail.itertuples(index=False)
    ]

    # Contiguous same-stage runs → xaxis bands (skip stage 0 = pre-MA warm-up).
    bands = []
    for r in tail.itertuples(index=False):
        s = int(r.stage)
        if s == 0:
            continue
        x = _ms(r.date)
        if bands and bands[-1]["s"] == s:
            bands[-1]["x2"] = x
        else:
            bands.append({"x": x, "x2": x, "s": s})

    crosses = [
        {"x": _ms(r.date), "y": _r(r.close), "t": r.cross}
        for r in tail.itertuples(index=False) if r.cross
    ]

    last = tail.iloc[-1]
    cur = int(last["stage"]) or 1
    meta = _STAGE_META.get(cur, _STAGE_META[1])
    return {
        "ok": True,
        "as_of": f"{tail['date'].max():%Y-%m-%d}",
        "coverage": f"{tail['date'].min():%Y-%m-%d} to {tail['date'].max():%Y-%m-%d}",
        "current": {"num": cur, "name": meta["name"], "color": meta["color"]},
        "points": points,
        "bands": bands,
        "crosses": crosses,
        "legend": [{"num": k, "name": v["name"], "color": v["color"]} for k, v in _STAGE_META.items()],
    }
