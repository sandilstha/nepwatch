"""
accumulation.py — stock-first Accumulation / Distribution radar.

The rest of the broker desk is broker-first: pick a broker, see what it traded.
This module asks the inverse, which is the question that actually matters for
positioning: *for this scrip, is someone building a position or unloading one?*

It is built on the same cached window aggregates as the rest of the desk
(``broker_analytics._window_aggregate``), so it adds no new queries.

THE MODEL IS BACKTESTED AND IT DOES NOT PREDICT. Read that before anything else
here: out-of-sample the score has no demonstrated ability to forecast returns
(60-session Q5-Q1 spread +1.72%, t=+1.02, against a ~0.6% round trip). It is a
DESCRIPTIVE flow lens — it measures who absorbed and how concentrated each side
of the book was, which the floorsheet records directly and no price/volume
screen can see. It is not a buy list.

The scoring below is exactly what was tested — 3.5 years of floorsheet
(2023-01-01 → 2026-08-19, 825 sessions, 9.83M broker-day-stock cells),
non-overlapping windows, returns adjusted for bonus/rights, measured as excess
over the cross-sectional median of the same date. Three features were selected
IN-SAMPLE on 2023-24, with date-clustered standard errors:

    absorb      top-5 net buyers' net qty ÷ window volume    +57 bps/SD  t=+4.22
    sell_hhi    concentration of the selling side            -34 bps/SD  t=-5.55
    top1_share  share of net buying done by ONE broker       -47 bps/SD  t=-2.23

    score = z(absorb) − z(top1_share) − z(sell_hhi)

Those t-statistics are IN-SAMPLE and are kept only to document what the score
is made of. Held out on 2025-01-01 onward the edge collapses: `absorb` falls
from t=5.73 to t=0.70, and `top1_share` never had univariate power in either
period and flips sign on 2023-24 alone. The composite is retained because it is
a 


 description of flow structure, NOT because it was shown to work.

The three z-scores are combined with EQUAL WEIGHTS (+1, -1, -1). That is a
deliberate robustness choice, not a fitted result: regression weights on this
sample would be roughly (+1.00, -0.87, -0.41) and score better in-sample
(+10.88% vs +8.03% at 120 sessions), but they are fitted to the same 3.5 years
they are measured on. Equal weights cannot overfit the coefficient estimates.
The ordering of the three features is what the evidence supports; their exact
relative sizes are not, so they are not asserted.

Two findings shaped the design and both are load-bearing:

  * HORIZON. In-sample the edge grew with the horizon (+1.26% over 20 sessions,
    +8.03% over 120), which is why this is framed as a 3-6 MONTH positioning
    lens rather than a weekly signal. Out-of-sample neither horizon is
    significant, and the 120-session test cannot even be run honestly: with
    non-overlapping windows only 2 anchors survive after 2025-01-01, too few for
    a clustered t-statistic. Treat the horizon claim as untested, not proven.

  * SEVERAL BROKERS, NOT ONE — RETRACTED AS A FINDING. In-sample, absorption
    concentrated in one broker scored as a negative (top1_share, t=-2.23), but
    that significance was a suppression effect in the multivariate fit: the
    feature has no univariate power in either sub-period and takes the OPPOSITE
    sign on 2023-24 alone. "Dispersed buying beats concentrated" must not be
    repeated as fact. The term stays in the score because dropping a component
    would change what the tested composite is, not because it was validated.
    It says nothing about anyone's INTENT either — a broker with many retail
    clients and a deliberately-split order look identical in this data.

Explicitly REJECTED by the backtest, and therefore not scored (they are still
reported as context, because they describe what happened even when they do not
predict): multi-session persistence of the buying group, volume expansion,
buy-side HHI, and group size on its own.

SECOND REJECTION ROUND (2026-08-20). Three further additions were proposed and
tested on the full 9.83M broker-day-stock cells over 825 sessions, out-of-sample
from 2025-01-01, non-overlapping forward windows, bonus-adjusted returns and
date-clustered errors. NONE survived, so NONE is scored:

    60-session horizon, out-of-sample, 1,245 observations
    base Q5-Q1 (unchanged)                +1.72%   t=+1.02
    + persistence (top quintile 20/40/60) +2.79%   t=+1.46
    + spike filter (no single day >50%)   +1.78%   t=+1.06
    + self-trade filter (<10% self-traded)+1.72%   t=+1.01
    + all three combined                  +2.76%   t=+1.44

Persistence looked the most promising on the headline number and is the clearest
failure underneath it. If it were real, more persistence would mean more return.
The dose-response is non-monotonic and its strongest tier is NEGATIVE:

    persist=1  -1.99%  t=-1.50
    persist=2  +1.09%  t=+0.57
    persist=3  -0.19%  t=-0.11   <- strong at 20 AND 40 AND 60 sessions

Its +2.79% came from a weak comparison group, not from persistent names
performing. Spike showed no gradient either (-0.26% / -0.37% across buckets).

In-sample (2023-24) every one of them "cleared cost" — base +5.64% t=2.40,
persistence dose-response a clean 3.71 -> 6.91 -> 6.14, spike 25-50% +10.09%.
All of it vanished out-of-sample. That is the same selection artifact that
produced the retracted +8.03%, which is why the in-sample column is kept here
only as the contrast that makes the decay visible.

Two design consequences, both load-bearing:
  * The 20/40/60 windows NEST — the last 20 sessions sit inside the 40 and the
    60 — so "strong at all three" is one reading counted three times, not three
    confirmations. Measured score correlations: 20d-40d r=+0.71, 40d-60d r=+0.82.
  * There is no validated "confidence" to report. Persistence, spike shape and
    self-trade share DESCRIBE how the flow happened and are worth surfacing as
    context, but none of them predicts, so none may be presented as reliability.

Self-trades are exactly identifiable (floorsheet buyer == seller, ~3.7% of
volume market-wide, median 2.4% per symbol, 47 of 411 symbols above 10%). They
are reported as context and deliberately NOT removed from the `absorb`
denominator: the scoring above was validated on gross volume, and changing the
denominator would mean the backtest no longer describes what is on screen.

Known blind spot, stated here so it is never mistaken for completeness: shares
transferred off-market (DP/BOD transfers, pledges, private deals) never print on
the floorsheet. A campaign conducted that way is invisible to this module.
"""
from __future__ import annotations

import hashlib
import logging
import math
import re

from django.core.cache import cache

from core_analysis.services import broker_analytics as ba

logger = logging.getLogger(__name__)

CACHE_TTL = 300
# BUMP THIS whenever the payload shape or the scoring changes — otherwise cached
# entries from the previous shape keep being served for CACHE_TTL. v2 added
# excluded_reasons + window_warning and made `rows` the complete scored set;
# v3 hardened the non-equity exclusion, which changes the scored universe and
# therefore every z-score in it.
# v5 replaced the in-sample headline with the out-of-sample result. The BACKTEST
# block is embedded in every cached payload, so the version MUST move with it or
# stale caches keep serving the retracted figures.
# v7: price_chg_pct now prefers bonus/rights-ADJUSTED closes (a raw close made a
# bonus ex-date look like a crash and put the scrip in Quiet Accumulation);
# deterministic tie-break in the ranking; identity check in _symbol_flow.
# v8: BACKTEST gained `rejected_round2` (persistence / spike / self-trade all
# failed out-of-sample). The block is embedded in every cached payload, so the
# version MUST move with it or stale caches keep serving the old evidence text.
PAYLOAD_VERSION = 8

TOP_K = 5              # size of the "accumulating group" used by `absorb`

# Liquidity floor in RUPEES TRADED PER SESSION, not shares.
#
# A share-count floor is not scale-free: 800 shares/session means Rs 160,000/day
# for a Rs 200 scrip but Rs 4,244,000/day for a Rs 5,305 one — a 26x difference
# in what "liquid enough" means, which systematically under-samples expensive
# stocks. That matters here because the backtested edge is LARGEST in the most
# expensive price tercile, so the old floor was strictest exactly where the
# signal is best.
#
# Turnover also makes a separate price floor unnecessary. The old Rs 50 minimum
# was vestigial anyway: the cheapest ordinary equity that clears this floor
# trades near Rs 190, and non-equity instruments are excluded by name/sector.
# Re-backtested at this floor: 120-session spread +8.03% (t=3.49, p=0.0005)
# versus +7.65% under the old rule — slightly better, and 12 more equities.
MIN_TURNOVER_PER_SESSION = 500_000.0   # Rs/day
MIN_UNIVERSE = 12      # fewer scored names than this and z-scores are meaningless
# Accumulation is a campaign, so a window has to be long enough to contain one.
# Refuse below MIN_SESSIONS outright rather than return a confident-looking
# one-day reading, and flag windows shorter than the shortest one ever tested
# (the sweep covered 15/25/50/75 sessions). This guard lives HERE, not in the
# view, so every caller inherits it.
MIN_SESSIONS = 5
BACKTESTED_MIN_SESSIONS = 15

# The score is only valid on ORDINARY EQUITIES, and the universe must match the
# one the model was validated on (36,077 of 36,647 backtest observations were
# equities; on equities alone the 120-session spread is +6.30%, t=2.24).
#
# Everything below is excluded for a reason, learned the hard way — the first
# production run returned debentures and closed-end funds in BOTH tails:
#   * debentures  — bonds. "Accumulation" is not a meaningful concept, and they
#                   trade through one or two brokers, so every concentration
#                   measure pins at its extreme and swamps the ranking.
#   * mutual funds— closed-end units trade around Rs 10, so the price floor
#                   removes them anyway; named here so the intent is explicit.
#   * promoter    — transfer-restricted, locked-in shares. The flow is not free
#     shares        float, and they carry their PARENT's sector (CZBILP shows as
#                   "Commercial Banks"), so a sector filter alone misses them.
EXCLUDED_SECTORS = {"non-convertible debentures", "mutual fund", "preference shares"}

# Sector labels alone are not enough to exclude non-equity listings, for two
# measured reasons:
#   * newly listed scrips trade BEFORE the profile sync fills their sector in,
#     so a new fund would slip into the cross-section during that window;
#   * some funds carry a blank sector permanently (SAEF2, SEF2, RSY2 all show
#     "-" in CompanyProfile).
# NEPSE does, however, spell the instrument type out in the security NAME, and
# that is the reliable tell: 286 of 289 promoter shares are caught by name,
# including the 17 whose parent company is delisted (ACEDPO, KISTPO, SNMAPO …)
# and which the "base symbol must be listed" test therefore misses entirely.
#
# WORD BOUNDARIES ARE REQUIRED, not cosmetic: a bare "yojana" substring matches
# "Pariyojana" and would wrongly exclude RURU (Ru Ru Jalbidhyut Pariyojana), a
# real hydropower company. The misspellings are NEPSE's own.
_NON_EQUITY_NAME_RE = re.compile(
    r"\b(promoter|promotor|promoer|prmoter|debenture|preference|pref\.)", re.I)
_FUND_NAME_RE = re.compile(r"\b(mutual\s+fund|fund|yojana|kosh|scheme)\b", re.I)
_NON_EQUITY_TTL = 3600

# Percentile bands. The backtest measured quintiles, so the bands are quintiles:
# top 20% = the Q5 that returned +4.96% excess over 120 sessions, bottom 20% =
# the Q1 that returned -1.03%. Anything else is genuinely "no signal", and is
# labelled that way rather than given a directional lean it has not earned.
BANDS = (
    (80.0, "accumulation", "Accumulation"),
    (60.0, "mild_accumulation", "Mild Accumulation"),
    (40.0, "neutral", "Neutral"),
    (20.0, "mild_distribution", "Mild Distribution"),
    (0.0, "distribution", "Distribution"),
)

# Surfaced with the payload so the UI can state the evidence next to the score
# instead of presenting it as an oracle.
BACKTEST = {
    # HEADLINE IS THE OUT-OF-SAMPLE RESULT, not the in-sample one.
    #
    # An earlier version of this block published the full-sample figures
    # (+8.03%, t=3.49) as though they were evidence of a forward-looking edge.
    # They were not: every feature had been SELECTED on the same 3.5 years it
    # was measured on. Holding out 2025-01 onward and re-testing collapses the
    # result — the model is not shown to predict, and the page must say so.
    "validated": False,
    "window_sessions": 25,
    "horizon_sessions": 120,
    "q5_excess_pct": 2.19,
    "q1_excess_pct": 0.56,
    "spread_pct": 1.64,
    "t_stat": 0.82,
    "p_value": 0.412,
    "n": 745,
    "hit_rate_pct": None,
    "sample": ("out-of-sample 2025-01-01 → 2026-08-11 · 745 independent "
               "observations · features selected on 2023-24 only"),
    "in_sample": {"spread_pct": 11.15, "t_stat": 3.34, "p_value": 0.001,
                  "period": "2023-02 → 2024-12"},
    # Deliberately does NOT repeat "not predictive" — the pill above the grid and
    # the "Significant? no" cell already say it, and a third restatement of the
    # same point is the fastest way to get all three ignored. This line carries
    # only what the numbers cannot: what the tab is actually good for.
    "note": ("Use it to see who is absorbing and how concentrated each side of the "
             "book is — the floorsheet measures that directly, and no price/volume "
             "screen can. Hit rate is around 51%, so no single reading is a call."),
    # Every horizon, sampled so that FORWARD windows never overlap (anchors are
    # spaced at least one horizon apart per symbol). An earlier version spaced
    # anchors by the 25-session formation window instead, which left consecutive
    # 120-session forward returns sharing 95 of their sessions and overstated
    # significance. Spreads here are larger and t-statistics smaller — fewer,
    # genuinely independent observations.
    # Out-of-sample by horizon. Only the 20-session row clears significance, and
    # its +0.80% does not clear the ~0.6% round trip — so there is no horizon at
    # which this is both statistically and economically supported.
    "horizons": [
        {"sessions": 20, "n": 4525, "spread_pct": 0.80, "t_stat": 2.05, "p_value": 0.040},
        {"sessions": 60, "n": 1500, "spread_pct": 1.74, "t_stat": 1.51, "p_value": 0.131},
        {"sessions": 120, "n": 745, "spread_pct": 1.64, "t_stat": 0.82, "p_value": 0.412},
    ],
    # Walk-forward: train on everything before year Y, test on Y. 2024 carried
    # the entire result; the two most recent years show nothing.
    "walk_forward_120": [{"year": 2024, "spread_pct": 13.16, "t_stat": 2.27, "n": 488},
                         {"year": 2025, "spread_pct": 1.05, "t_stat": 0.38, "n": 505},
                         {"year": 2026, "spread_pct": 2.78, "t_stat": 0.94, "n": 255}],
    # Why it decayed: `absorb` was carrying the model and stopped working.
    # `top1_share` never had univariate power in either period — its in-sample
    # multivariate significance was a suppression effect, and on 2023-24 alone
    # it takes the OPPOSITE sign. The "dispersed buying beats concentrated"
    # finding is NOT robust and should not be repeated as fact.
    "feature_stability_120": [
        {"feature": "absorb", "r_2023_24": 0.187, "t_2023_24": 5.73,
         "r_2025_26": 0.026, "t_2025_26": 0.70},
        {"feature": "top1_share", "r_2023_24": 0.018, "t_2023_24": 0.53,
         "r_2025_26": -0.004, "t_2025_26": -0.12},
        {"feature": "sell_hhi", "r_2023_24": -0.051, "t_2023_24": -1.55,
         "r_2025_26": -0.029, "t_2025_26": -0.80},
    ],
    # Observations lost because no forward price exists (delisted, suspended, or
    # simply too close to the end of the sample). Reported because dropping them
    # silently would bias results if attrition favoured one end of the score.
    # It does not: at the 120-session horizon 18.3% are lost, spread evenly
    # across score quintiles (Q1 18.6% … Q5 18.7%).
    "attrition_120_pct": 18.3,
    "attrition_is_uniform": True,
    # Round 2 (2026-08-20): persistence, spike-concentration and self-trade
    # filtering were each tested as overlays on the UNCHANGED score. None
    # reached significance out-of-sample, so none is scored — they are surfaced
    # as descriptive context only. Kept in the payload so the UI can say what
    # was tried and rejected instead of silently dropping it.
    "rejected_round2": {
        "tested_on": "825 sessions, 9.83M broker-day-stock cells, OOS from 2025-01-01",
        "horizon_sessions": 60,
        "n": 1245,
        "base": {"spread_pct": 1.72, "t_stat": 1.02},
        "candidates": [
            {"name": "persistence (top quintile at 20/40/60)",
             "spread_pct": 2.79, "t_stat": 1.46, "kept": False,
             "why": "dose-response non-monotonic and NEGATIVE at the strongest "
                    "tier (-1.99 / +1.09 / -0.19); the headline came from a weak "
                    "comparison group, not from persistent names performing"},
            {"name": "spike filter (no single day >50% of the accumulation)",
             "spread_pct": 1.78, "t_stat": 1.06, "kept": False,
             "why": "no gradient across spike buckets (-0.26% / -0.37%)"},
            {"name": "self-trade filter (<10% buyer==seller volume)",
             "spread_pct": 1.72, "t_stat": 1.01, "kept": False,
             "why": "no effect on the spread; self-trading is real (~3.7% of "
                    "volume) but does not separate winners from losers"},
            {"name": "all three combined",
             "spread_pct": 2.76, "t_stat": 1.44, "kept": False,
             "why": "no better than persistence alone"},
        ],
        # The windows nest, so agreement across them is one reading repeated.
        "window_overlap_r": {"20d_40d": 0.71, "40d_60d": 0.82, "20d_60d": 0.55},
        "note": ("In-sample every one of these cleared the cost hurdle — the same "
                 "selection artifact that produced the retracted +8.03%."),
    },
}


def _stdev(values):
    n = len(values)
    if n < 2:
        return None
    mean = sum(values) / n
    var = sum((v - mean) ** 2 for v in values) / (n - 1)
    return math.sqrt(var)


def _zmap(values):
    """{key: z-score} for a {key: value} map; empty when the spread is degenerate."""
    if len(values) < 2:
        return {}
    vals = list(values.values())
    mean = sum(vals) / len(vals)
    sd = _stdev(vals)
    if not sd:
        return {}
    return {k: (v - mean) / sd for k, v in values.items()}


def _window_closes(symbols, as_of=None):
    """{symbol: last close on or before ``as_of``} — the price floor needs a level.

    Anchored to the END OF THE WINDOW, not to today. Using today's price would
    apply a present-day filter to a historical scan: a scrip that traded at
    Rs 500 during a 2025 window but sits at Rs 40 now would be silently dropped
    from that window's cross-section, which is a look-ahead filter.
    """
    from core_analysis.models import NepseDailyStockPrice
    from datetime import timedelta

    if not symbols:
        return {}
    try:
        qs = NepseDailyStockPrice.objects.all()
        if as_of:
            qs = qs.filter(business_date__lte=as_of)
        anchor = (qs.order_by("-business_date")
                  .values_list("business_date", flat=True).first())
        if not anchor:
            return {}
        rows = (NepseDailyStockPrice.objects
                .filter(symbol__in=list(symbols),
                        business_date__lte=anchor,
                        business_date__gte=anchor - timedelta(days=30))
                .order_by("symbol", "-business_date")
                .values_list("symbol", "close_price"))
        out = {}
        for sym, close in rows:          # first row per symbol = closest to anchor
            if sym not in out and close:
                out[sym] = float(close)
        return out
    except Exception:  # pragma: no cover - price overlay is best-effort
        logger.exception("window close load failed")
        return {}


def _is_promoter_share(symbol, listed):
    """Symbol-shape fallback for promoter scrips (CZBILP, NABILPO).

    A ``PO`` suffix is decisive on its own — all 146 PO-suffixed listings on
    NEPSE are promoter shares. A bare ``P`` is NOT: NADEP (Microfinance), RRHP
    (Hydro Power) and SBPP (Manufacturing) are ordinary equities ending in P, so
    that case additionally requires the stripped base to be a listed company.

    This is only the fallback; ``_non_equity_symbols`` does the real work by
    reading the security name.
    """
    if symbol.endswith("PO"):
        return True
    return symbol.endswith("P") and symbol[:-1] in listed


def _non_equity_symbols():
    """Every listed symbol that is NOT an ordinary equity, keyed off the name.

    Promoter shares, debentures, preference shares and mutual funds are excluded
    from the A/D cross-section entirely — they trade through one or two brokers,
    so their concentration measures pin at the extremes and distort every other
    scrip's z-score. Resolved once an hour from ``CompanyProfile`` because the
    per-session sector label is not trustworthy for new listings.
    """
    # Keyed to PAYLOAD_VERSION so an edit to the exclusion regexes never serves
    # the previous exclusion set for up to an hour after a deploy.
    ck = f"ad_non_equity_v{PAYLOAD_VERSION}"
    cached = cache.get(ck)
    if cached is not None:
        return cached
    out = set()
    try:
        from core_analysis.models import CompanyProfile

        for sym, name, sector in CompanyProfile.objects.values_list(
                "symbol", "security_name", "sector_name"):
            if not sym:
                continue
            nm = name or ""
            if ((sector or "").strip().lower() in EXCLUDED_SECTORS
                    or _NON_EQUITY_NAME_RE.search(nm)
                    or _FUND_NAME_RE.search(nm)
                    or sym.endswith("PO")):
                out.add(sym)
    except Exception:  # pragma: no cover - reference table optional
        logger.exception("non-equity symbol load failed")
        return set()
    cache.set(ck, out, _NON_EQUITY_TTL)
    return out


def _band(pct):
    for floor, key, label in BANDS:
        if pct >= floor:
            return key, label
    return "distribution", "Distribution"


def _symbol_flow(buy_cells, sell_cells):
    """Per-broker net for one symbol → the raw ingredients of the score.

    ``buy_cells`` / ``sell_cells`` are ``{broker: (qty, amount)}`` straight from
    the window aggregate. Returns None when the name cannot be scored (no volume,
    or one side entirely absent — a scrip nobody sold has no distribution signal
    to measure and would divide by zero).

    PRECISE DEFINITIONS — the two ratios are easy to misread, so state them
    exactly. For each broker, ``net = shares bought − shares sold`` in the
    window. Summed over ALL brokers that is identically zero, so neither ratio
    divides by it:

      ``sum_pos`` = Σ of the POSITIVE nets (what the net buyers accumulated).
      ``sum_neg`` = Σ of |negative nets| (what the net sellers released).
                    These two are equal by construction — every share bought net
                    by one broker was sold net by another — and the code CHECKS
                    the identity below, refusing to score a symbol where it
                    breaks (which would mean the floorsheet aggregate itself has
                    a hole, e.g. rows with a missing broker code on one side).

      ``volume``  = GROSS shares traded in the window = Σ of every broker's buy
                    quantity (identical to the sell-side total, since each trade
                    has both). It counts a broker's self-crosses, which cancel
                    out of the nets but not out of this denominator.

      ``absorb``     = (Σ of the TOP-5 positive nets) ÷ volume
                       — a NET numerator over a GROSS denominator. It reads as
                       "what share of everything traded ended up in the five
                       largest net buyers' hands".
      ``top1_share`` = (largest single positive net) ÷ sum_pos
                       — concentration WITHIN the net-buying side only.
      ``sell_hhi``   = Herfindahl of the |negative nets| as shares of sum_neg,
                       ×10,000. Concentration within the net-selling side only.
    """
    # `volume` is gross traded quantity and INCLUDES a broker's self-crosses.
    # Those cancel out of `nets` (same broker both sides) but still inflate this
    # denominator, which deflates `absorb`. Left deliberately: the backtest was
    # run on exactly this definition, so "fixing" it here would ship a scoring
    # rule that was never validated. Documented as a known bias in the SOP.
    volume = sum(q for q, _a in buy_cells.values())
    if volume <= 0:
        return None

    nets = {}
    for b, (q, _a) in buy_cells.items():
        nets[b] = nets.get(b, 0.0) + q
    for b, (q, _a) in sell_cells.items():
        nets[b] = nets.get(b, 0.0) - q

    pos = sorted((v for v in nets.values() if v > 0), reverse=True)
    neg = sorted((-v for v in nets.values() if v < 0), reverse=True)
    if not pos or not neg:
        return None
    sum_pos, sum_neg = sum(pos), sum(neg)

    # The identity the docstring promises. A mismatch beyond float noise means
    # the aggregate itself is broken for this symbol (a side missing broker
    # codes, a partial sync) — scoring it anyway would distort top1_share and
    # sell_hhi in opposite directions, so refuse and say why in the log.
    if abs(sum_pos - sum_neg) > 1e-6 * max(sum_pos, sum_neg, 1.0):
        logger.warning(
            "A/D identity broken: sum_pos=%.1f sum_neg=%.1f — aggregate has a "
            "hole for this symbol; refusing to score it.", sum_pos, sum_neg)
        return None

    # The accumulating GROUP: the smallest set of net buyers whose nets sum to
    # half of `sum_pos`. Descriptive only — group size did not predict on its
    # own — but it is what shows whether net buying was broad or concentrated.
    running, group_n = 0.0, 0
    for v in pos:
        running += v
        group_n += 1
        if running >= sum_pos * 0.5:
            break

    top_buyers = sorted(((v, b) for b, v in nets.items() if v > 0), reverse=True)
    top_sellers = sorted(((-v, b) for b, v in nets.items() if v < 0), reverse=True)

    return {
        "volume": volume,
        "absorb": sum(pos[:TOP_K]) / volume,
        "top1_share": pos[0] / sum_pos,
        "sell_hhi": sum((v / sum_neg) ** 2 for v in neg) * 10000.0,
        "buy_hhi": sum((v / sum_pos) ** 2 for v in pos) * 10000.0,
        "net_top5": sum(pos[:TOP_K]),
        "group_n": group_n,
        "sellers_n": len(neg),
        "buyers_n": len(pos),
        "top_buyers": [{"broker": b, "net": round(v)} for v, b in top_buyers[:TOP_K]],
        "top_sellers": [{"broker": b, "net": round(v)} for v, b in top_sellers[:TOP_K]],
    }


def _scan_market(range_key="1m", start=None, end=None,
                 min_turnover=MIN_TURNOVER_PER_SESSION):
    """Score EVERY ordinary equity in the window — the full cross-section.

    Sector selection is deliberately NOT applied here, for one usability reason
    and one correctness reason:

      * Small sectors would die. Tradings has 2 listed companies and Hotels &
        Tourism has 8, so filtering first left fewer names than MIN_UNIVERSE and
        the tab reported "too few to rank against each other" instead of simply
        showing that sector's scrips.

      * More importantly, it would silently redefine the score. The backtest
        ranked each date across the WHOLE market; z-scoring inside one sector
        measures "accumulated relative to other hydropower stocks", which is a
        different quantity with different bands. The numbers on screen would no
        longer be the numbers that were validated.

    So the cross-section is always the market. ``accumulation_scan`` filters the
    resulting rows for display, which leaves every score and band intact.
    """
    # Same freshness tokens as the broker aggregates, so a floorsheet sync
    # invalidates this scan too instead of serving a stale A/D for 5 minutes.
    ck = (f"ad_market_v{PAYLOAD_VERSION}_{range_key}_{start}_{end}_{min_turnover:.0f}"
          f"_{ba.get_latest_trading_date()}_{ba._range_generation()}")
    cached = cache.get(ck)
    if cached is not None:
        return cached

    agg = ba._window_aggregate(range_key, start, end)
    if not agg:
        return {"ok": False, "reason": "Floorsheet aggregate unavailable.", "rows": []}

    dates = agg.get("dates") or []
    sessions = max(1, len(dates))
    if len(dates) < MIN_SESSIONS:
        return {"ok": False, "rows": [], "days": len(dates),
                "reason": (f"A {len(dates)}-session window is too short to show a campaign. "
                           f"Accumulation is measured over many sessions — pick at least "
                           f"{MIN_SESSIONS}, ideally a month.")}
    buy, sell, secmap = agg["buy"], agg["sell"], agg["sector"]

    traded = set(buy) | set(sell)
    window_end = max(dates) if dates else None
    prices = ba._window_close_changes(traded, dates)
    names = ba._company_names()
    closes = _window_closes(traded, window_end)
    listed = set(names) or traded
    non_equity = _non_equity_symbols()

    raw = {}
    skipped = {"instrument": 0, "price": 0, "volume": 0, "one_sided": 0}
    # Why each excluded symbol was dropped, so the detail view can say
    # "excluded: promoter share" instead of the misleading "did not trade enough".
    reasons = {}
    for sym in traded:
        sec = secmap.get(sym)
        # Non-equity instruments never enter the cross-section: they would
        # distort every z-score, not merely occupy rows in the table. Three
        # independent tests, because no single one is complete — the session's
        # sector label, the security name, and the symbol shape.
        if (sec or "").strip().lower() in EXCLUDED_SECTORS:
            skipped["instrument"] += 1
            reasons[sym] = f"excluded as a non-equity listing ({sec})"
            continue
        if sym in non_equity:
            skipped["instrument"] += 1
            reasons[sym] = ("excluded as a non-equity listing (promoter share, debenture, "
                            "preference share or fund)")
            continue
        if _is_promoter_share(sym, listed):
            skipped["instrument"] += 1
            reasons[sym] = "excluded as a promoter share (transfer-restricted, not free float)"
            continue
        close = closes.get(sym)
        if not close:
            # No price at all is a different condition from "cheap", and saying
            # "below the floor" for it would be simply untrue.
            skipped["price"] += 1
            reasons[sym] = "no closing price available in this window"
            continue
        flow = _symbol_flow(buy.get(sym, {}), sell.get(sym, {}))
        if not flow:
            # Counted, not just recorded — otherwise the exclusion tally printed
            # under the table does not add up to the market, which is the same
            # quiet-truncation problem in a smaller form.
            skipped["one_sided"] += 1
            # Covers both unscoreable cases _symbol_flow refuses: genuinely
            # one-sided flow, and (rare, logged) a broken buy/sell identity.
            reasons[sym] = ("flow could not be scored — brokers on one side only, "
                            "or an inconsistent aggregate for this scrip")
            continue
        # Known approximation: the whole window's volume is valued at the
        # window-END close, so a scrip that moved sharply in-window has its
        # turnover mis-stated by roughly that move, and a borderline name can
        # flicker across the floor between windows. Accepted: the floor is a
        # liquidity heuristic, not a scored quantity, and a per-day repricing
        # would add a query per symbol for a decision that is almost always
        # nowhere near the boundary.
        turnover_day = close * flow["volume"] / sessions
        if turnover_day < min_turnover:
            skipped["volume"] += 1
            reasons[sym] = (f"turned over Rs {turnover_day:,.0f}/session, below the "
                            f"Rs {min_turnover:,.0f} floor")
            continue
        flow["turnover_day"] = turnover_day
        raw[sym] = flow

    if len(raw) < MIN_UNIVERSE:
        return {"ok": False, "rows": [], "days": sessions,
                "reason": (f"Only {len(raw)} scrips meet the liquidity floor in this "
                           f"window — too few to rank against each other.")}

    z_absorb = _zmap({s: f["absorb"] for s, f in raw.items()})
    z_top1 = _zmap({s: f["top1_share"] for s, f in raw.items()})
    z_sell = _zmap({s: f["sell_hhi"] for s, f in raw.items()})
    if not (z_absorb and z_top1 and z_sell):
        return {"ok": False, "rows": [], "days": sessions,
                "reason": "Flow is degenerate in this window (no cross-sectional spread)."}

    scored = {s: z_absorb[s] - z_top1[s] - z_sell[s] for s in raw}
    # Tie-break on symbol, NOT dict order. `raw` is built from a set, whose
    # iteration order is hash-randomised per process — with tied scores (77 of
    # 268 in a typical window at 2dp) a tie straddling the 80th-percentile edge
    # would band "Accumulation" on one server process and "Mild" on another.
    order = sorted(scored, key=lambda s: (scored[s], s))
    n = len(order)
    pct_of = {s: 100.0 * i / (n - 1) for i, s in enumerate(order)} if n > 1 else {}

    rows = []
    for sym, flow in raw.items():
        pct = pct_of.get(sym, 50.0)
        key, label = _band(pct)
        rows.append({
            "symbol": sym,
            "name": names.get(sym) or sym,
            "sector": secmap.get(sym) or "—",
            "score": round(scored[sym], 2),
            "percentile": round(pct, 1),
            "band": key,
            "band_label": label,
            # what drove it
            "absorb_pct": round(100.0 * flow["absorb"], 1),
            "top1_pct": round(100.0 * flow["top1_share"], 1),
            "sell_hhi": round(flow["sell_hhi"]),
            "buy_hhi": round(flow["buy_hhi"]),
            "group_n": flow["group_n"],
            "buyers_n": flow["buyers_n"],
            "sellers_n": flow["sellers_n"],
            "net_qty": round(flow["net_top5"]),
            "volume": round(flow["volume"]),
            "price_chg_pct": (round(prices[sym], 2) if sym in prices else None),
            "top_buyers": flow["top_buyers"],
            "top_sellers": flow["top_sellers"],
        })

    rows.sort(key=lambda r: r["score"], reverse=True)

    payload = {
        "ok": True,
        "days": sessions,
        # max(), not dates[-1] — the aggregate's date list is not guaranteed
        # sorted, and the unsorted read reported a window ending a month early.
        "as_of": max(dates) if dates else None,
        "universe": n,
        "sessions": sessions,
        # EVERY scored scrip in the market. Band lists and any sector filtering
        # are derived from this by `accumulation_scan`.
        "rows": rows,
        "excluded_reasons": reasons,
        # Stated openly: a screen that silently drops most of the market reads as
        # "nothing is accumulating" when it really means "we did not look".
        "excluded": skipped,
        # A window shorter than anything in the sweep still computes, but the
        # backtested numbers on screen were not measured on it — say so.
        "window_warning": (
            (f"This {sessions}-session window is shorter than the {BACKTESTED_MIN_SESSIONS}"
             f"-session minimum the model was tested on — the backtest figures below "
             f"do not apply to it.") if sessions < BACKTESTED_MIN_SESSIONS else None),
        "universe_note": (
            f"Scored {n} ordinary equities of {len(traded)} that traded. Excluded "
            f"{skipped['instrument']} non-equity listings (debentures, funds, promoter "
            f"shares), {skipped['price']} without a closing price, {skipped['volume']} "
            f"turning over less than Rs {min_turnover:,.0f} a session, and "
            f"{skipped['one_sided']} with brokers on only one side."),
        "backtest": BACKTEST,
    }
    cache.set(ck, payload, CACHE_TTL)
    return payload


def accumulation_scan(range_key="1m", sector="All", start=None, end=None,
                      min_turnover=MIN_TURNOVER_PER_SESSION, limit=60):
    """Market cross-section, optionally narrowed to one sector for display.

    Scoring always happens across the whole market (see ``_scan_market``); the
    sector selection filters the OUTPUT. So picking "Tradings" shows how its two
    scrips rank against the entire market — which is both the useful reading and
    the one the backtest validated — instead of failing for want of peers.
    """
    base = _scan_market(range_key=range_key, start=start, end=end,
                        min_turnover=min_turnover)
    if not base.get("ok"):
        return base

    sector = (sector or "All").strip()
    rows = base["rows"]
    scoped = rows if sector in ("", "All") else [r for r in rows if r["sector"] == sector]

    accum = [r for r in scoped if r["band"] == "accumulation"]
    distrib = [r for r in scoped if r["band"] == "distribution"]

    out = dict(base)
    out.update({
        "sector": sector,
        "rows": scoped,
        "accumulation": accum[:limit],
        "distribution": distrib[-limit:][::-1],
        "counts": {"accumulation": len(accum), "distribution": len(distrib),
                   "scored": len(scoped)},
        # The KPI tiles read `counts` while the tables read the capped lists.
        # When a quintile is larger than `limit` those two disagree, so say it
        # out loud rather than let the table look complete.
        "truncated": {
            "accumulation": max(0, len(accum) - limit),
            "distribution": max(0, len(distrib) - limit),
            "limit": limit,
        },
        # CANDIDATES, not confirmed accumulation: scrips in the top quintile
        # whose price did not rise over the window. That co-occurrence is what
        # the classic pattern looks like, but it is consistent with plenty of
        # innocent explanations too, and nothing here confirms a campaign.
        #
        # `is not None` matters: `(None or 0) <= 0` is True, so a scrip whose
        # price change could not be computed would be presented as "absorbing
        # without markup" when the markup is simply unknown. Unknown is not flat.
        "quiet_accumulation": sorted(
            [r for r in accum
             if r["price_chg_pct"] is not None and r["price_chg_pct"] <= 0],
            key=lambda r: r["score"], reverse=True)[:12],
    })
    if sector not in ("", "All"):
        # Deliberately not "showing N": the active view may render only the
        # accumulation or distribution slice of these, so a "showing" count
        # would contradict the rows on screen.
        out["universe_note"] = (
            f"{len(scoped)} {sector} scrip{'' if len(scoped) == 1 else 's'} scored, "
            f"ranked against all {base['universe']} ordinary equities in the market this "
            f"window — the sector filter narrows the list, it does not change any score.")
        if not scoped:
            out["reason"] = (f"No {sector} scrip cleared the liquidity floor in this "
                             f"window, so none could be scored.")
    return out


def accumulation_detail(symbol, range_key="1m", start=None, end=None):
    """One scrip's A/D reading, with the brokers on each side.

    Scored inside the full market cross-section (not in isolation) so the
    percentile means the same thing as it does on the scan.
    """
    symbol = (symbol or "").strip().upper()
    if not symbol:
        return {"ok": False, "reason": "No symbol supplied."}

    # The full market, never a sector slice — the percentile has to mean the
    # same thing in the drawer as it does in the table.
    scan = _scan_market(range_key=range_key, start=start, end=end)
    if not scan.get("ok"):
        return {"ok": False, "reason": scan.get("reason", "Scan unavailable.")}

    row = next((r for r in scan["rows"] if r["symbol"] == symbol), None)
    if not row:
        # Say WHY. "Did not trade enough" was wrong for promoter shares and
        # debentures, which are excluded by instrument type regardless of volume.
        why = (scan.get("excluded_reasons") or {}).get(symbol)
        return {"ok": False, "symbol": symbol,
                "reason": (f"{symbol} is not scored: {why}." if why
                           else f"{symbol} did not trade in this window.")}

    return {
        "ok": True,
        "symbol": symbol,
        "days": scan["days"],
        "as_of": scan["as_of"],
        "universe": scan["universe"],
        "row": row,
        "backtest": BACKTEST,
        "reading": _reading(row),
    }


def _reading(row):
    """Plain-language description of the flow structure behind the score.

    States what the numbers ARE, not what anyone intended by them. The data
    shows how buying and selling were distributed across brokers; it cannot
    show motive, and a broker with many retail clients is indistinguishable
    here from a deliberately split order.
    """
    bits = []
    if row["band"] in ("accumulation", "mild_accumulation"):
        bits.append(
            f"The 5 largest net buyers ended the window holding a net {row['absorb_pct']}% "
            f"of all shares traded in it."
        )
        # No claim attached to group size: `top1_share` failed out-of-sample and
        # flips sign between periods, so "dispersed beats concentrated" is not a
        # finding this data supports. State the structure, stop there.
        if row["group_n"] > 1:
            bits.append(
                f"Half of that net buying came through {row['group_n']} brokers."
            )
        else:
            bits.append("Almost all of that net buying came through a single broker.")
        if row["price_chg_pct"] is not None and row["price_chg_pct"] <= 0:
            bits.append(
                f"Price moved {row['price_chg_pct']}% over the same window, so the "
                f"absorption did not come with a mark-up."
            )
    elif row["band"] in ("distribution", "mild_distribution"):
        bits.append(
            f"Net selling is concentrated (sell-side HHI {row['sell_hhi']:,} across "
            f"{row['sellers_n']} net sellers) while net buying is dispersed."
        )
        if row["price_chg_pct"] is not None and row["price_chg_pct"] > 0:
            bits.append(
                f"Price rose {row['price_chg_pct']}% over the window while that supply "
                f"was being released."
            )
    else:
        bits.append("Net flow is balanced — neither side dominates this scrip's book.")
    bits.append("This describes observed flow structure only; it is not evidence of "
                "anyone's intent, and not a forecast.")
    return " ".join(bits)

