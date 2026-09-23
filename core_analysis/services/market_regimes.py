"""Bull / bear market dating for the NEPSE Index.

Two independent datings of the same history, both computed from the local
``NepseMarketIndex`` end-of-day table (no external call):

  * ``ps``     — Pagan & Sossounov (2003), the standard academic procedure and
                 itself an adaptation of Bry-Boschan (1971). Monthly log closes,
                 +/-8-month local extrema, formal censoring. This is the headline
                 segmentation: few, long, unambiguous phases.
  * ``swing``  — a daily 20% swing filter (peak-to-trough / trough-to-peak) with
                 a 4-month minimum-phase censor. Finer resolution, same story.

Keeping both is deliberate. They were built independently and agree on 7 of 9
turning points within a quarter, which is the evidence that either can be
trusted; where they differ (the 2008-12 and 2021-23 troughs) the daily filter
finds the price low and the monthly procedure finds the point the market
actually turned, months later.

Two properties of this dataset shaped the implementation and must not be
"tidied away":

  * ~63% of index bars carry ``high == low``, so intraday range is unusable —
    everything here works on CLOSES only.
  * The record contains genuine multi-week exchange closures (2015 earthquake,
    2020 COVID). Phase DURATIONS that span them are calendar-true but not
    comparable to others, so sessions are reported alongside months.
"""
from __future__ import annotations

import datetime
import logging
import math

from django.core.cache import cache

logger = logging.getLogger(__name__)

CACHE_TTL = 1800  # 30 min — EOD indices change once per session.

_NEPSE_INDEX_KEY = "NEPSE INDEX"

# ── Pagan-Sossounov parameters, as published ────────────────────────────────
_PS_WINDOW = 8       # months either side for a local extreme
_PS_MIN_PHASE = 4    # months; waived when amplitude exceeds _PS_MIN_AMP
_PS_MIN_CYCLE = 16   # months, peak-to-peak / trough-to-trough
_PS_MIN_AMP = 0.20   # 20% — the conventional bull/bear amplitude
_PS_END_GUARD = 6    # months at each end where turning points are not called

# ── Daily swing parameters ──────────────────────────────────────────────────
_SWING_THRESHOLD = 0.20
_SWING_MIN_MONTHS = 4.0

BULL_COLOR = "#16a34a"
BEAR_COLOR = "#dc2626"
OPEN_COLOR = "#94a3b8"


def _series():
    """(dates, closes) for the NEPSE headline index, oldest first."""
    from core_analysis.models import NepseMarketIndex

    rows = list(
        NepseMarketIndex.objects.filter(sector_name__iexact=_NEPSE_INDEX_KEY)
        .order_by("business_date")
        .values_list("business_date", "close_index")
    )
    dates, closes = [], []
    for d, c in rows:
        try:
            c = float(c)
        except (TypeError, ValueError):
            continue
        if c > 0:
            dates.append(d)
            closes.append(c)
    return dates, closes


def _months_between(a, b):
    return round((b - a).days / 30.44, 1)


# ── Pagan-Sossounov ─────────────────────────────────────────────────────────

def _monthly(dates, closes):
    """Last close of each calendar month -> [(ym, date, close)]."""
    last = {}
    for d, c in zip(dates, closes):
        last[(d.year, d.month)] = (d, c)
    return [(k, *last[k]) for k in sorted(last)]


def _alternate(points, logp):
    """Collapse runs of same-kind turning points to the most extreme one."""
    out = []
    for t in points:
        if out and out[-1][1] == t[1]:
            better = (logp[t[0]] > logp[out[-1][0]]) if t[1] == "P" else (logp[t[0]] < logp[out[-1][0]])
            if better:
                out[-1] = t
        else:
            out.append(t)
    return out


def _ps_turning_points(month_rows):
    n = len(month_rows)
    if n < 2 * _PS_WINDOW + 4:
        return []
    logp = [math.log(r[2]) for r in month_rows]

    pts = []
    for i in range(n):
        a, b = max(0, i - _PS_WINDOW), min(n, i + _PS_WINDOW + 1)
        win = logp[a:b]
        if logp[i] == max(win):
            pts.append([i, "P"])
        elif logp[i] == min(win):
            pts.append([i, "T"])
    pts = _alternate(pts, logp)
    # No turning points are called near either end — there is not yet enough
    # data on one side to know an extreme is one.
    pts = _alternate([t for t in pts if _PS_END_GUARD <= t[0] <= n - 1 - _PS_END_GUARD], logp)

    def amp(i, j):
        return abs(math.exp(abs(logp[j] - logp[i])) - 1)

    for _ in range(200):
        changed = False
        # Rule: phases shorter than the minimum are not cycles, unless the move
        # is large enough to count on amplitude alone.
        for i in range(len(pts) - 1):
            if pts[i + 1][0] - pts[i][0] < _PS_MIN_PHASE and amp(pts[i][0], pts[i + 1][0]) <= _PS_MIN_AMP:
                # At either END of the series drop only the outer point. Dropping
                # the pair would delete the extreme anchoring the neighbouring
                # phase — doing so erased the entire 1998-2000 bull (+220%).
                if i == 0:
                    del pts[0]
                elif i + 2 >= len(pts):
                    del pts[-1]
                else:
                    del pts[i:i + 2]
                pts = _alternate(pts, logp)
                changed = True
                break
        if changed:
            continue
        # Rule: complete cycles shorter than the minimum are noise.
        for i in range(len(pts) - 2):
            if pts[i + 2][0] - pts[i][0] < _PS_MIN_CYCLE:
                del pts[i + 1:i + 3]
                pts = _alternate(pts, logp)
                changed = True
                break
        if not changed:
            break
    return pts


def _ps_phases(dates, closes):
    rows = _monthly(dates, closes)
    pts = _ps_turning_points(rows)
    out = []
    for (i0, k0), (i1, k1) in zip(pts, pts[1:]):
        if k0 == k1:
            continue
        d0, d1 = rows[i0][1], rows[i1][1]
        v0, v1 = rows[i0][2], rows[i1][2]
        out.append(_phase("BULL" if k0 == "T" else "BEAR", d0, d1, v0, v1, months=i1 - i0))
    return out


# ── Daily 20% swing filter ──────────────────────────────────────────────────

def _swing_pivots(closes, threshold):
    """Alternating peaks/troughs. Direction is seeded by the first move that
    clears the threshold in either direction, so a long opening trend cannot be
    swallowed — an earlier version left direction unset through the whole
    1997-2000 advance and silently dropped its peak."""
    n = len(closes)
    if n < 2:
        return []
    piv = []
    hi = lo = closes[0]
    hi_i = lo_i = 0
    dirn = 0
    for i in range(1, n):
        c = closes[i]
        if c > hi:
            hi, hi_i = c, i
        if c < lo:
            lo, lo_i = c, i
        if dirn == 0:
            if c <= hi * (1 - threshold):
                piv.append((hi_i, hi, "P")); dirn = -1; lo, lo_i = c, i
            elif c >= lo * (1 + threshold):
                piv.append((lo_i, lo, "T")); dirn = 1; hi, hi_i = c, i
        elif dirn > 0:
            if c <= hi * (1 - threshold):
                piv.append((hi_i, hi, "P")); dirn = -1; lo, lo_i = c, i
        else:
            if c >= lo * (1 + threshold):
                piv.append((lo_i, lo, "T")); dirn = 1; hi, hi_i = c, i
    piv.append((hi_i if dirn > 0 else lo_i, hi if dirn > 0 else lo, "P" if dirn > 0 else "T"))
    return piv


def _swing_phases(dates, closes):
    piv = _swing_pivots(closes, _SWING_THRESHOLD)
    raw = []
    for (i0, v0, k0), (i1, v1, k1) in zip(piv, piv[1:]):
        if k0 == k1:
            continue
        raw.append({"kind": "BULL" if k0 == "T" else "BEAR", "i0": i0, "i1": i1, "p0": v0, "p1": v1})

    # Censor counter-trend moves shorter than the minimum by absorbing them into
    # the surrounding trend.
    changed = True
    while changed and len(raw) > 1:
        changed = False
        for i, p in enumerate(raw):
            if _months_between(dates[p["i0"]], dates[p["i1"]]) >= _SWING_MIN_MONTHS:
                continue
            if 0 < i < len(raw) - 1:
                a, b = raw[i - 1], raw[i + 1]
                merged = {"kind": a["kind"], "i0": a["i0"], "i1": b["i1"], "p0": a["p0"], "p1": b["p1"]}
                raw = raw[:i - 1] + [merged] + raw[i + 2:]
            elif i == 0:
                b = raw[1]
                raw = [{"kind": b["kind"], "i0": p["i0"], "i1": b["i1"], "p0": p["p0"], "p1": b["p1"]}] + raw[2:]
            else:
                a = raw[-2]
                raw = raw[:-2] + [{"kind": a["kind"], "i0": a["i0"], "i1": p["i1"], "p0": a["p0"], "p1": p["p1"]}]
            changed = True
            break

    # A merged phase must still terminate at the genuine extreme of its window,
    # and its successor must start there. Merging alone leaves the boundary at
    # the absorbed phase's endpoint (which mis-dated the 2011 trough and the
    # 2021 peak by months).
    for _ in range(10):
        moved = False
        for i, p in enumerate(raw):
            seg = closes[p["i0"]:p["i1"] + 1]
            want = max(seg) if p["kind"] == "BULL" else min(seg)
            if abs(want - p["p1"]) > 1e-9:
                j = p["i0"] + seg.index(want)
                p["i1"], p["p1"] = j, want
                if i + 1 < len(raw):
                    raw[i + 1]["i0"], raw[i + 1]["p0"] = j, want
                moved = True
        if not moved:
            break

    return [
        _phase(p["kind"], dates[p["i0"]], dates[p["i1"]], p["p0"], p["p1"],
               sessions=p["i1"] - p["i0"])
        for p in raw
    ]


# ── shared ──────────────────────────────────────────────────────────────────

def _phase(kind, d0, d1, v0, v1, months=None, sessions=None):
    pct = 100.0 * (v1 - v0) / v0 if v0 else 0.0
    return {
        "kind": kind,
        "is_bull": kind == "BULL",
        "start": d0.isoformat(),
        "end": d1.isoformat(),
        "start_label": d0.strftime("%b %Y"),
        "end_label": d1.strftime("%b %Y"),
        "from_value": round(v0, 2),
        "to_value": round(v1, 2),
        "pct": round(pct, 1),
        "pct_label": f"{pct:+,.1f}%",
        "days": (d1 - d0).days,
        "months": months if months is not None else _months_between(d0, d1),
        "sessions": sessions,
        "color": BULL_COLOR if kind == "BULL" else BEAR_COLOR,
    }


def _current_state(dates, closes, phases):
    """What has happened since the last confirmed turning point.

    Reported rather than classified: if the move has not cleared the 20% rule it
    is NOT called a bear, however far it has fallen. Anything else would make
    this dataset's first sub-20% decline a bear market and break comparability
    with every phase above it.
    """
    if not phases:
        return None
    last = phases[-1]
    end = datetime.date.fromisoformat(last["end"])
    try:
        i0 = dates.index(end)
    except ValueError:
        return None

    ref = closes[i0]
    tail = closes[i0:]
    cur = tail[-1]
    lo, hi = min(tail), max(tail)
    lo_i = i0 + tail.index(lo)
    from_ref = 100.0 * (cur - ref) / ref
    drawdown = 100.0 * (lo - ref) / ref

    if last["kind"] == "BULL":
        confirmed = drawdown <= -100 * _SWING_THRESHOLD
        label = "Bear market confirmed" if confirmed else "Correction within an intact bull"
        note = (
            "The decline cleared the 20% rule."
            if confirmed else
            f"The deepest fall was {abs(drawdown):.1f}%, short of the 20% needed to call a bear market."
        )
    else:
        confirmed = 100.0 * (hi - ref) / ref >= 100 * _SWING_THRESHOLD
        label = "Bull market confirmed" if confirmed else "Rebound within an intact bear"
        note = (
            "The advance cleared the 20% rule."
            if confirmed else
            f"The best rally was {100.0 * (hi - ref) / ref:.1f}%, short of the 20% needed to call a bull market."
        )

    return {
        "since": last["end"],
        "since_label": datetime.date.fromisoformat(last["end"]).strftime("%d %b %Y"),
        "anchor_kind": last["kind"],
        "anchor_value": round(ref, 2),
        "current": round(cur, 2),
        "current_date": dates[-1].isoformat(),
        "from_ref": round(from_ref, 1),
        "from_ref_label": f"{from_ref:+,.1f}%",
        "drawdown": round(drawdown, 1),
        "extreme_value": round(lo if last["kind"] == "BULL" else hi, 2),
        "extreme_date": dates[lo_i].isoformat() if last["kind"] == "BULL" else dates[i0 + tail.index(hi)].isoformat(),
        "days": (dates[-1] - end).days,
        "sessions": len(tail) - 1,
        "confirmed": confirmed,
        "label": label,
        "note": note,
        "trigger": round(ref * (1 - _SWING_THRESHOLD) if last["kind"] == "BULL" else ref * (1 + _SWING_THRESHOLD), 0),
    }


def _current_phase(dates, closes, phases, state):
    """The phase the market is IN today, which the tables alone never say.

    A turning point is only confirmed in retrospect: the peak that ends a bull is
    identified by the 20% fall that follows it. With no such fall, the last row's
    end date is a running maximum, not a confirmed top — so the phase that
    started at the previous trough is still under way. Reporting the table's last
    row as "the last phase" would imply the market is between cycles, which is
    never true.
    """
    if not phases or not state:
        return None
    last = phases[-1]

    if state["confirmed"]:
        # The reversal cleared the rule: the new phase runs from that extreme.
        kind = "BEAR" if last["kind"] == "BULL" else "BULL"
        start = state["since"]
        start_value = state["anchor_value"]
    else:
        # Unconfirmed: the phase in the last row has not actually ended.
        kind = last["kind"]
        start = last["start"]
        start_value = last["from_value"]

    d0 = datetime.date.fromisoformat(start)
    today = dates[-1]
    cur = closes[-1]
    i0 = next((i for i, d in enumerate(dates) if d >= d0), 0)
    window = closes[i0:]
    extreme = max(window) if kind == "BULL" else min(window)
    ei = i0 + window.index(extreme)
    pct = 100.0 * (cur - start_value) / start_value if start_value else 0.0
    ext_pct = 100.0 * (extreme - start_value) / start_value if start_value else 0.0

    return {
        "kind": kind,
        "is_bull": kind == "BULL",
        "start": start,
        "start_label": d0.strftime("%b %Y"),
        "start_full": d0.strftime("%d %b %Y"),
        "start_value": round(start_value, 2),
        "as_of": today.isoformat(),
        "as_of_label": today.strftime("%d %b %Y"),
        "current": round(cur, 2),
        "pct": round(pct, 1),
        "pct_label": f"{pct:+,.1f}%",
        "months": _months_between(d0, today),
        "days": (today - d0).days,
        "sessions": len(window) - 1,
        "extreme": round(extreme, 2),
        "extreme_date": dates[ei].isoformat(),
        "extreme_label": dates[ei].strftime("%d %b %Y"),
        "extreme_pct_label": f"{ext_pct:+,.1f}%",
        "confirmed_end": state["confirmed"],
        "color": BULL_COLOR if kind == "BULL" else BEAR_COLOR,
        "headline": (
            f"NEPSE is in a {kind.lower()} market that began {d0.strftime('%d %b %Y')} — "
            f"running {_months_between(d0, today):.0f} months, {pct:+,.1f}% to date."
        ),
    }


def _summary(phases):
    bull = [p for p in phases if p["is_bull"]]
    bear = [p for p in phases if not p["is_bull"]]
    total = sum(p["days"] for p in phases) or 1

    def med(xs):
        xs = sorted(xs)
        return round(xs[len(xs) // 2], 1) if xs else None

    return {
        "bull_count": len(bull),
        "bear_count": len(bear),
        "bull_median_months": med([p["months"] for p in bull]),
        "bear_median_months": med([p["months"] for p in bear]),
        "bull_median_pct": med([p["pct"] for p in bull]),
        "bear_median_pct": med([p["pct"] for p in bear]),
        "bull_time_pct": round(100.0 * sum(p["days"] for p in bull) / total, 1),
        "bear_time_pct": round(100.0 * sum(p["days"] for p in bear) / total, 1),
    }


def _closures(dates, closes, phases, min_gap=10):
    """Multi-week exchange closures that fall inside a phase.

    Surfaced because they make a phase's calendar duration incomparable — the
    2020 bear spans 4 months but only 17 sessions.
    """
    out = []
    index = {d: i for i, d in enumerate(dates)}
    for n, p in enumerate(phases, 1):
        a = index.get(datetime.date.fromisoformat(p["start"]))
        b = index.get(datetime.date.fromisoformat(p["end"]))
        if a is None or b is None:
            continue
        for i in range(a + 1, b + 1):
            gap = (dates[i] - dates[i - 1]).days
            if gap > min_gap:
                out.append({
                    "phase": n, "kind": p["kind"],
                    "from": dates[i - 1].isoformat(), "to": dates[i].isoformat(), "days": gap,
                })
    return out


def _compute():
    dates, closes = _series()
    if len(dates) < 400:
        return {"ok": False, "reason": "Not enough index history to date market cycles."}

    ps = _ps_phases(dates, closes)
    swing = _swing_phases(dates, closes)
    if not ps and not swing:
        return {"ok": False, "reason": "No bull/bear phases could be identified."}

    headline = ps or swing
    state = _current_state(dates, closes, swing or ps)
    return {
        "ok": True,
        "as_of": dates[-1].isoformat(),
        "first_date": dates[0].isoformat(),
        "sessions": len(dates),
        "ps": ps,
        "swing": swing,
        "ps_summary": _summary(ps) if ps else None,
        "swing_summary": _summary(swing) if swing else None,
        # (key, phases, summary) triples so the template renders one table body
        # per dating instead of duplicating the markup.
        "views": [
            ("ps", ps, _summary(ps) if ps else None),
            ("swing", swing, _summary(swing) if swing else None),
        ],
        # State is measured against the DAILY dating, not the monthly headline:
        # the monthly peak is a month-end close (2,922.63) while the true peak
        # was 3,002.07, which understates the drawdown by ~2.3 points. The
        # trigger level a reader acts on must come from the real extreme.
        "state": state,
        # Which phase is running TODAY — measured on the daily dating so the
        # start date and the running total are exact, not month-end rounded.
        "current_phase": _current_phase(dates, closes, swing or ps, state),
        "closures": _closures(dates, closes, headline),
        # The head of the record before the first confirmed turning point cannot
        # be classified — said out loud rather than shown as a phase.
        "unclassified_head": {
            "from": dates[0].isoformat(),
            "to": headline[0]["start"],
        } if headline and headline[0]["start"] != dates[0].isoformat() else None,
        "params": {
            "ps_window": _PS_WINDOW, "ps_min_phase": _PS_MIN_PHASE,
            "ps_min_cycle": _PS_MIN_CYCLE, "amplitude": int(_PS_MIN_AMP * 100),
            "swing_min_months": _SWING_MIN_MONTHS,
        },
    }


def build_market_regimes():
    """Cached bull/bear dating payload for the Seasonal desk."""
    from core_analysis.models import NepseMarketIndex

    latest = (
        NepseMarketIndex.objects.order_by("-business_date")
        .values_list("business_date", flat=True)
        .first()
    )
    ck = f"market_regimes_v1_{latest}"
    cached = cache.get(ck)
    if cached is not None:
        return cached
    try:
        payload = _compute()
    except Exception:  # pragma: no cover - never let this break the tab
        logger.exception("market regime dating failed")
        payload = {"ok": False, "reason": "Market cycle engine error."}
    cache.set(ck, payload, CACHE_TTL if payload.get("ok") else 60)
    return payload
