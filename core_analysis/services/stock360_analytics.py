"""Stock 360 analytics — technicals, intrinsic valuation, peer rank, dividends.

Everything here reads OUR OWN tables. No new ingestion, no external call:

  * technicals   -> the adjusted OHLC dataframe Stock 360 already builds
  * valuation    -> ``FundaFundamentalSnapshot.ks`` (P/B, ROE, EPS, DPS) + the
                    latest ``NepseDailyStockPrice`` close
  * peer rank    -> the same snapshot table, filtered to the company's sector
  * dividends    -> ``FundaFundamentalSnapshot.raw`` KeyStats rows, one per
                    reported quarter, carrying ``DividendPerShare``

Design rules:
  * Refusing to compute is a feature. Every helper returns an explicit
    ``available: False`` with a human ``note`` rather than a plausible number
    built from missing inputs.
  * Range-based indicators are deliberately absent. A large share of NEPSE bars
    print high == low, which silently corrupts ATR/ADX-style measures; RSI and
    moving averages on the close are safe on this data.
  * Peer percentiles need a real cohort. Below ``_MIN_PEERS`` companies in a
    sector we say so instead of ranking against three names.
"""

import math

# Cost of equity / growth defaults for the justified-P/B model. Nepal-market
# conventions: a ~12% required return and a risk-free anchored on the long
# government bond. Both are overridable per request so the panel stays a model,
# not a verdict handed down.
DEFAULT_COST_OF_EQUITY = 0.12
DEFAULT_RISK_FREE = 0.06

_MIN_PEERS = 5            # below this a sector percentile is noise
_FAIR_BAND = 0.10         # within ±10% of intrinsic = "Fairly valued"


def _f(v, default=None):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    if math.isnan(f) or math.isinf(f):
        return default
    return f


# --------------------------------------------------------------------------- #
# Technicals
# --------------------------------------------------------------------------- #

def _sma(values, n):
    if len(values) < n:
        return None
    return round(sum(values[-n:]) / n, 2)


def _rsi(values, n=14):
    """Wilder's RSI on closing prices.

    Close-based on purpose: NEPSE prints a lot of high==low bars, so anything
    that measures the intraday range understates volatility on this feed.
    """
    if len(values) < n + 1:
        return None
    gains = losses = 0.0
    for i in range(len(values) - n, len(values)):
        ch = values[i] - values[i - 1]
        gains += max(ch, 0.0)
        losses += max(-ch, 0.0)
    avg_g, avg_l = gains / n, losses / n
    if avg_l == 0:
        return 100.0 if avg_g > 0 else 50.0
    rs = avg_g / avg_l
    return round(100.0 - (100.0 / (1.0 + rs)), 1)


def _rsi_label(rsi):
    if rsi is None:
        return None, "neu"
    if rsi >= 70:
        return "Overbought", "warn"
    if rsi >= 55:
        return "Strong", "pos"
    if rsi <= 30:
        return "Oversold", "warn"
    if rsi <= 45:
        return "Weak", "neg"
    return "Neutral", "neu"


def technicals(df):
    """Trend, momentum and participation read for one symbol.

    ``df`` is the adjusted dataframe Stock 360 already builds for the returns
    block and chart, so this costs no extra query. Returns ``None`` when the
    price history is too short for even the shortest average.
    """
    if df is None or df.empty:
        return None

    closes = [c for c in (_f(v) for v in df["close_price_adj"].tolist()) if c and c > 0]
    if len(closes) < 11:
        return None
    last = closes[-1]

    mas = {}
    for n in (10, 20, 50, 200):
        val = _sma(closes, n)
        mas[f"ma{n}"] = {
            "value": val,
            "dist_pct": round((last / val - 1.0) * 100.0, 2) if val else None,
            "above": (last >= val) if val else None,
        }

    # Trend structure: how many of the available averages price sits above, and
    # whether the stack itself is ordered 10 > 20 > 50 > 200 (a clean uptrend).
    present = [m for m in mas.values() if m["value"] is not None]
    above = sum(1 for m in present if m["above"])
    stack = [mas[f"ma{n}"]["value"] for n in (10, 20, 50, 200)]
    stack_vals = [v for v in stack if v is not None]
    aligned_up = len(stack_vals) >= 3 and all(
        stack_vals[i] >= stack_vals[i + 1] for i in range(len(stack_vals) - 1)
    )
    aligned_down = len(stack_vals) >= 3 and all(
        stack_vals[i] <= stack_vals[i + 1] for i in range(len(stack_vals) - 1)
    )

    if present and above == len(present) and aligned_up:
        trend, trend_tone = "Strong uptrend", "pos"
    elif present and above >= max(1, len(present) - 1):
        trend, trend_tone = "Uptrend", "pos"
    elif present and above == 0 and aligned_down:
        trend, trend_tone = "Strong downtrend", "neg"
    elif present and above <= 1:
        trend, trend_tone = "Downtrend", "neg"
    else:
        trend, trend_tone = "Sideways / mixed", "warn"

    rsi = _rsi(closes)
    rsi_lab, rsi_tone = _rsi_label(rsi)

    # Position inside the trailing 52-week ADJUSTED range. The hero's 52w bar
    # uses the exchange's raw print; this one stays on the adjusted series so it
    # agrees with the moving averages beside it.
    window = closes[-246:]
    hi, lo = max(window), min(window)
    pos52 = round((last - lo) / (hi - lo) * 100.0, 1) if hi > lo else None

    # 20 trading SESSIONS, not calendar days — the distinction matters on an
    # exchange with as many holidays as NEPSE.
    ret20 = round((last / closes[-21] - 1.0) * 100.0, 2) if len(closes) > 21 else None

    vol_ratio = None
    if "volume" in df.columns:
        vols = [v for v in (_f(x, 0.0) for x in df["volume"].tolist()) if v and v > 0]
        if len(vols) >= 21:
            avg20 = sum(vols[-21:-1]) / 20.0
            if avg20 > 0:
                vol_ratio = round(vols[-1] / avg20, 2)

    return {
        "last": round(last, 2),
        # The per-average table was removed from the page; the stack itself still
        # decides the trend label and the "above N of M" count below.
        "mas": mas,
        "ma_above": above,
        "ma_count": len(present),
        "trend": trend,
        "trend_tone": trend_tone,
        "rsi": rsi,
        "rsi_label": rsi_lab,
        "rsi_tone": rsi_tone,
        "pos_52w_adj": pos52,
        "ret_20s": ret20,
        "vol_ratio": vol_ratio,
        "bars": len(closes),
    }


# --------------------------------------------------------------------------- #
# Valuation + peers
# --------------------------------------------------------------------------- #

def _snapshot_pb(ks, price=None):
    """P/B from book value per share and the freshest price we hold."""
    bvps = _f((ks or {}).get("bvps"))
    px = _f(price) or _f((ks or {}).get("price"))
    if not bvps or bvps <= 0 or not px or px <= 0:
        return None
    return round(px / bvps, 2)


def _percentile(values, value, higher_is_better):
    """Share of the cohort this value beats, 0-100. ``None`` if uncomputable."""
    pool = [v for v in values if v is not None]
    if len(pool) < _MIN_PEERS or value is None:
        return None
    if higher_is_better:
        beaten = sum(1 for v in pool if value > v)
    else:
        beaten = sum(1 for v in pool if value < v)
    return round(beaten / len(pool) * 100.0, 1)


def _quartile_label(pct):
    if pct is None:
        return None, "neu"
    if pct >= 75:
        return "Top 25% — Outstanding", "pos"
    if pct >= 50:
        return "Upper half — above median", "pos"
    if pct >= 25:
        return "Lower half — below median", "warn"
    return "Bottom 25% — lagging peers", "neg"


def _median(values):
    pool = sorted(v for v in values if v is not None)
    if not pool:
        return None
    mid = len(pool) // 2
    if len(pool) % 2:
        return round(pool[mid], 2)
    return round((pool[mid - 1] + pool[mid]) / 2.0, 2)


def justified_pb(roe, growth, cost_of_equity):
    """Gordon-growth justified P/B = (ROE - g) / (r - g).

    Returns ``None`` when the inputs make the model meaningless: a non-positive
    required return, or growth at or above it (which sends the ratio to
    infinity and would print a fantasy target).
    """
    roe, g, r = _f(roe), _f(growth), _f(cost_of_equity)
    if roe is None or g is None or r is None:
        return None
    if r <= 0 or g >= r:
        return None
    jpb = round((roe - g) / (r - g), 2)
    # A non-positive ratio means the company earns at or below its growth
    # assumption — the model prices its book at nothing, which is a statement
    # about the inputs, not a target. Refuse it.
    return jpb if jpb > 0 else None


def valuation(symbol, price=None, cost_of_equity=None, growth=None):
    """Intrinsic P/B verdict + sector peer rank for one symbol, from our DB.

    ``price`` should be the latest close; without it we fall back to the price
    stamped on the fundamental snapshot, which can be a quarter stale.
    """
    from core_analysis.models import FundaFundamentalSnapshot as Snap

    sym = (symbol or "").strip().upper()
    snap = Snap.objects.filter(symbol=sym).first()
    if not snap:
        return {
            "available": False,
            # Points at the Workbench sync panel: Stock 360's own sync button was
            # removed, and an instruction naming a control that isn't there is
            # worse than no instruction.
            "note": f"No fundamentals synced for {sym}. Sync it from the Workbench "
                    "fundamentals panel, then reload — valuation needs book value and ROE.",
        }

    ks = snap.ks or {}
    roe = _f(ks.get("roe"))
    eps = _f(ks.get("eps"))
    dps = _f(ks.get("dps"))
    bvps = _f(ks.get("bvps"))
    pb = _snapshot_pb(ks, price)
    pe = _f(ks.get("pe"))

    r = _f(cost_of_equity, DEFAULT_COST_OF_EQUITY) or DEFAULT_COST_OF_EQUITY

    # Sustainable growth g = ROE x retention.
    #
    # Retention must come from an ANNUAL payout. The snapshot's ``dps`` is the
    # figure on the latest reported QUARTER, and NEPSE companies declare once a
    # year — so mid-year it reads 0.00 and would imply full retention for every
    # company on the exchange, pinning g at its ceiling and inflating every
    # target. The dividend history is the honest source.
    payout, payout_basis = None, None
    hist = dividends(sym)
    if hist.get("available"):
        last_paid = next((h for h in reversed(hist["history"]) if h["dps"] > 0), None)
        if last_paid and eps and eps > 0:
            payout = max(0.0, min(1.0, last_paid["dps"] / eps))
            payout_basis = f"FY {last_paid['fy']} DPS {last_paid['dps']:g} / EPS {eps:g}"
        elif hist.get("paid_years") == 0:
            payout, payout_basis = 0.0, "never declared a dividend"
    if payout is None and eps and eps > 0 and dps:
        payout = max(0.0, min(1.0, dps / eps))
        payout_basis = "latest reported quarter"

    if growth is not None:
        g = _f(growth)
        g_basis = "your input"
    elif roe is not None and payout is not None:
        g = roe * (1.0 - payout)
        g_basis = f"ROE x retention ({payout_basis})"
    else:
        g, g_basis = None, None

    # No silent cap. If retained earnings imply growth at or above the required
    # return, the Gordon model has nothing to say about this company — saying so
    # beats printing a target built on a clamped input.
    withheld = None
    if g is not None and g >= r:
        withheld = (f"Estimate withheld: implied growth ({g * 100:.1f}%) is at or above the "
                    f"required return ({r * 100:.1f}%), so the model has no finite answer. "
                    "Raise the cost of equity or set growth by hand to explore it.")

    jpb = justified_pb(roe, g, r)

    verdict, tone, gap = None, "neu", None
    if jpb is not None and pb:
        gap = round((pb / jpb - 1.0) * 100.0, 1)
        if pb < jpb * (1.0 - _FAIR_BAND):
            verdict, tone = "Undervalued", "pos"
        elif pb <= jpb * (1.0 + _FAIR_BAND):
            verdict, tone = "Fairly valued", "warn"
        else:
            verdict, tone = "Expensive", "neg"

    # Earnings yield cross-check: EPS/price against the risk-free rate. A stock
    # yielding less than a government bond is expensive on an absolute basis
    # whatever the peer table says.
    px = _f(price) or _f(ks.get("price"))
    earn_yield = round(eps / px * 100.0, 2) if (eps and px and px > 0) else None

    out = {
        "available": jpb is not None or pb is not None,
        "period": snap.period,
        "sector": snap.sector or "",
        "synced_at": snap.synced_at.isoformat() if snap.synced_at else None,
        "inputs": {
            "roe": round(roe * 100.0, 2) if roe is not None else None,
            "cost_of_equity": round(r * 100.0, 2),
            "growth": round(g * 100.0, 2) if g is not None else None,
            "growth_basis": g_basis,
            "payout": round(payout * 100.0, 1) if payout is not None else None,
            "payout_basis": payout_basis,
            "risk_free": round(DEFAULT_RISK_FREE * 100.0, 2),
        },
        "pb": pb,
        "pe": pe,
        "bvps": bvps,
        "eps": eps,
        "justified_pb": jpb,
        "fair_price": round(jpb * bvps, 2) if (jpb and bvps) else None,
        "gap_pct": gap,
        "verdict": verdict,
        "tone": tone,
        "earnings_yield": earn_yield,
        "risk_free_pct": round(DEFAULT_RISK_FREE * 100.0, 2),
    }
    if jpb is None:
        out["note"] = withheld or (
            "Justified P/B needs a positive ROE and a book value on file. "
            "Not enough of that pair is stored for this company.")

    # ---- sensitivity grid: how the verdict moves with ROE and growth --------
    if roe is not None and g is not None and jpb is not None:
        roe_axis = [round(roe + d, 4) for d in (-0.04, -0.02, 0.0, 0.02, 0.04)]
        g_axis = [round(min(max(g + d, 0.0), r - 0.01), 4) for d in (-0.02, -0.01, 0.0, 0.01, 0.02)]
        out["sensitivity"] = {
            "roe_axis": [round(v * 100.0, 1) for v in roe_axis],
            "g_axis": [round(v * 100.0, 1) for v in g_axis],
            "grid": [[justified_pb(rv, gv, r) for gv in g_axis] for rv in roe_axis],
        }

    out["peers"] = _peer_rank(snap, pb, pe, roe)
    return out


def _peer_rank(snap, pb, pe, roe):
    """Percentile of this company against every synced name in its sector."""
    from core_analysis.models import FundaFundamentalSnapshot as Snap
    from core_analysis.models import NepseDailyStockPrice

    sector = (snap.sector or "").strip()
    if not sector:
        return {"available": False, "note": "No sector on the fundamental snapshot, so no peer cohort."}

    rows = list(
        Snap.objects.filter(sector__iexact=sector).values("symbol", "security_name", "ks")
    )
    if len(rows) < _MIN_PEERS:
        return {
            "available": False,
            "sector": sector,
            "count": len(rows),
            "note": f"Only {len(rows)} companies synced in {sector}. "
                    f"A percentile needs at least {_MIN_PEERS} — sync more of the sector first.",
        }

    # One price read for the whole cohort: the latest close per symbol on the
    # most recent session we hold. Cheaper and fairer than the quarter-old price
    # stamped on each snapshot.
    symbols = [r["symbol"] for r in rows]
    latest_date = (
        NepseDailyStockPrice.objects.filter(symbol__in=symbols)
        .order_by("-business_date")
        .values_list("business_date", flat=True)
        .first()
    )
    prices = {}
    if latest_date:
        prices = dict(
            NepseDailyStockPrice.objects.filter(
                symbol__in=symbols, business_date=latest_date
            ).values_list("symbol", "close_price")
        )

    peers = []
    for r in rows:
        ks = r["ks"] or {}
        peers.append({
            "symbol": r["symbol"],
            "name": r["security_name"] or r["symbol"],
            "pb": _snapshot_pb(ks, prices.get(r["symbol"])),
            "pe": _f(ks.get("pe")),
            "roe": _f(ks.get("roe")),
            "eps": _f(ks.get("eps")),
        })

    pb_vals = [p["pb"] for p in peers]
    pe_vals = [p["pe"] for p in peers if p["pe"] and p["pe"] > 0]
    roe_vals = [p["roe"] for p in peers]

    metrics = [
        {"key": "pb", "label": "P/B", "value": pb, "median": _median(pb_vals),
         "percentile": _percentile(pb_vals, pb, higher_is_better=False),
         "hint": "Cheaper than this share of the sector."},
        {"key": "pe", "label": "P/E", "value": pe if (pe and pe > 0) else None,
         "median": _median(pe_vals),
         "percentile": _percentile(pe_vals, pe if (pe and pe > 0) else None, higher_is_better=False),
         "hint": "Cheaper than this share of the sector."},
        {"key": "roe", "label": "ROE %",
         "value": round(roe * 100.0, 2) if roe is not None else None,
         "median": round(_median(roe_vals) * 100.0, 2) if _median(roe_vals) is not None else None,
         "percentile": _percentile(roe_vals, roe, higher_is_better=True),
         "hint": "Earns more on equity than this share of the sector."},
    ]

    scored = [m["percentile"] for m in metrics if m["percentile"] is not None]
    overall = round(sum(scored) / len(scored), 1) if scored else None
    label, tone = _quartile_label(overall)

    # Cheapest peers by P/B, for the "who else is in this bucket" table.
    table = sorted(
        (p for p in peers if p["pb"] is not None),
        key=lambda p: p["pb"],
    )[:10]

    return {
        "available": True,
        "sector": sector,
        "count": len(rows),
        "metrics": metrics,
        "overall": overall,
        "label": label,
        "tone": tone,
        "priced_on": latest_date.isoformat() if latest_date else None,
        "table": table,
        "note": "Peer quartile (not fiscal quarter): the average of the per-metric "
                "percentiles above, against every company synced in this sector.",
    }


# --------------------------------------------------------------------------- #
# Dividends
# --------------------------------------------------------------------------- #

def proposed_dividends(symbol, limit=6):
    """Board-proposed dividends for one symbol, newest announcement first.

    Distinct from the paid history below: these are announced but not
    necessarily approved at the AGM or distributed yet, which is why the later
    dates are often empty. This is also the only source that splits bonus from
    cash — the statements feed reports a single DividendPerShare.
    """
    from django.db.models import F

    from core_analysis.models import ProposedDividend

    sym = (symbol or "").strip().upper()
    rows = ProposedDividend.objects.filter(symbol=sym).order_by(
        F("announcement_date").desc(nulls_last=True), "-fiscal_year"
    )[:limit]
    return [
        {
            "fy": r.fiscal_year,
            "bonus": _f(r.bonus_percent),
            "cash": _f(r.cash_percent),
            "total": _f(r.total_percent),
            "announced": r.announcement_date.isoformat() if r.announcement_date else None,
            "bookclose": r.bookclose_date.isoformat() if r.bookclose_date else None,
            "bookclose_status": r.bookclose_status,
            "distribution": r.distribution_date.isoformat() if r.distribution_date else None,
            "bonus_listing": r.bonus_listing_date.isoformat() if r.bonus_listing_date else None,
        }
        for r in rows
    ]


def _bs_fy_to_ad(fy_bs):
    """'2078/2079' (Bikram Sambat, the proposed-dividend feed) -> '2021/22'
    (the statements feed's spelling). The Nepali fiscal year runs mid-July to
    mid-July, so BS 2078/79 is AD 2021/22: subtract 57. Returns "" when the
    input is not a BS year pair."""
    import re
    m = re.match(r"^\s*(\d{4})\s*/\s*(\d{2,4})\s*$", str(fy_bs or ""))
    if not m:
        return ""
    start = int(m.group(1)) - 57
    return f"{start}/{(start + 1) % 100:02d}"


def _fy_start(fy_ad):
    """'2021/22' -> 2021, for ordering; 0 when unparseable."""
    head = str(fy_ad or "").split("/")[0].strip()
    return int(head) if head.isdigit() else 0


def _current_dividend(latest, proposed):
    """The ONE dividend the card headlines.

    Two sources can describe it: the statements feed (declared DPS per year)
    and the proposed-dividend feed (board proposal with the bonus/cash split
    and the dates). They are usually the SAME dividend seen twice — JBBL's
    proposed FY 2078/79 at 6.80% is its declared FY 2021/22 at 6.80 — so the
    card used to show it as two things. Merge by fiscal year:

      * proposal newer than the last declared year -> headline the proposal,
        flagged as pending AGM approval;
      * same year -> one block: the declared figure with the split and dates;
      * only one source present -> that one.
    """
    top = proposed[0] if proposed else None
    top_ad = _bs_fy_to_ad(top["fy"]) if top else ""
    latest_fy = latest["fy"] if latest and latest.get("dps", 0) > 0 else ""

    if top and (not latest_fy or _fy_start(top_ad) > _fy_start(latest_fy)):
        return {
            "status": "proposed", "fy": top_ad or top["fy"], "fy_bs": top["fy"],
            "total": top["total"], "bonus": top["bonus"], "cash": top["cash"],
            "announced": top["announced"], "bookclose": top["bookclose"],
            "bookclose_status": top["bookclose_status"],
            "distribution": top["distribution"], "bonus_listing": top["bonus_listing"],
        }
    if not latest_fy:
        return None
    same = top if top and top_ad == latest_fy else None
    return {
        "status": "declared", "fy": latest_fy, "fy_bs": same["fy"] if same else None,
        "total": latest["dps"],
        "bonus": same["bonus"] if same else None,
        "cash": same["cash"] if same else None,
        "announced": same["announced"] if same else None,
        "bookclose": same["bookclose"] if same else None,
        "bookclose_status": same["bookclose_status"] if same else "",
        "distribution": same["distribution"] if same else None,
        "bonus_listing": same["bonus_listing"] if same else None,
    }


def dividends(symbol):
    """Per-year dividend history from the stored KeyStats rows.

    The source reports ``DividendPerShare`` on each quarterly row; the fiscal
    year's declared payout is the largest value seen in that year (Q4 usually
    carries it, but a company that reports it earlier is still picked up).

    The ``proposed`` block rides along independently: a company can have a
    board-proposed dividend with no fundamentals synced at all, so the card must
    still have something to show when ``available`` is False.
    """
    from core_analysis.models import FundaFundamentalSnapshot as Snap

    sym = (symbol or "").strip().upper()
    proposed = proposed_dividends(sym)
    snap = Snap.objects.filter(symbol=sym).first()
    if not snap:
        return {
            "available": False,
            "proposed": proposed,
            "current": _current_dividend(None, proposed),
            "note": f"No fundamentals synced for {sym}.",
        }

    rows = ((snap.raw or {}).get("keyStats")
            or ((snap.raw or {}).get("statements") or {}).get("keyStats")
            or [])
    by_year = {}
    for e in rows:
        year = str(e.get("Year") or "").strip()
        dps = _f(e.get("DividendPerShare"))
        if not year or dps is None:
            continue
        by_year[year] = max(by_year.get(year, 0.0), dps)

    years = sorted(by_year)
    history = [{"fy": y, "dps": round(by_year[y], 2)} for y in years]
    paid = [h for h in history if h["dps"] > 0]

    if not history:
        return {
            "available": False,
            "proposed": proposed,
            "current": _current_dividend(None, proposed),
            "note": "No dividend line on the stored statements for this company.",
        }

    # The newest fiscal year is usually still in progress and reports 0.00 —
    # that is "not declared yet", not "paid nothing". Headline the last DECLARED
    # payout and say which year it was, so a mid-year read never looks like a
    # company that stopped paying.
    latest = next((h for h in reversed(history) if h["dps"] > 0), history[-1])
    pending = history[-1]["fy"] if history[-1]["dps"] == 0 and latest is not history[-1] else None
    # A year still in progress is not evidence of anything — leave it out of the
    # average and the consistency count rather than scoring the company down for
    # a dividend it has not had the chance to declare.
    settled = history[:-1] if pending else history
    settled_paid = [h for h in settled if h["dps"] > 0]
    recent = [h["dps"] for h in settled[-5:]]
    avg5 = round(sum(recent) / len(recent), 2) if recent else None
    # Face value is Rs 100 for NEPSE equities, so DPS is already the % of face
    # value the company declared.
    consistency = round(len(settled_paid) / len(settled) * 100.0, 0) if settled else None

    # Attach the bonus/cash split to the history year it belongs to. The
    # proposed feed is the only source that splits them, and its year is BS.
    by_ad = {_bs_fy_to_ad(p["fy"]): p for p in proposed if _bs_fy_to_ad(p["fy"])}
    for h in history:
        p = by_ad.get(h["fy"])
        h["bonus"] = p["bonus"] if p else None
        h["cash"] = p["cash"] if p else None
        h["pending"] = h["fy"] == pending
        h["missing"] = False

    # The table shows a CONTINUOUS five-year run (plus the in-progress year).
    # The source skips years — 2023/24 is absent for most companies — and a
    # skipped column read as "the company paid every year shown", so a gap is
    # rendered as an explicit n/a column instead of disappearing.
    by_fy = {h["fy"]: h for h in history}
    newest = _fy_start(settled[-1]["fy"]) if settled else _fy_start(history[-1]["fy"])
    table = []
    for start in range(newest - 4, newest + 1):
        fy = f"{start}/{(start + 1) % 100:02d}"
        table.append(by_fy.get(fy) or {"fy": fy, "dps": None, "bonus": None, "cash": None,
                                       "pending": False, "missing": True})
    if pending:
        table.append(by_fy[pending])

    return {
        "available": True,
        "proposed": proposed,
        "current": _current_dividend(latest, proposed),
        "period": snap.period,
        "history": history,
        "table": table,
        "latest": latest,
        "pending_fy": pending,
        "avg_5y": avg5,
        "years": len(settled) or len(history),
        "paid_years": len(settled_paid) if settled else len(paid),
        "consistency": consistency,
        "note": "Dividend per share on a Rs 100 face value — the figure doubles as "
                "percent of face value. Cash and bonus are not split by the source.",
    }
