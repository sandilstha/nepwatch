"""
market_depth — frame-to-frame reading of TMS top-5 order-book captures.

Phase 2A-1 of the market-depth design: RAW FEATURES ONLY, no score. Everything
here is an observation a reader can check against the replay:

  * per-frame book (5 bid / 5 ask levels with price, qty, splits)
  * top-5 quantity on each side and a per-script rolling baseline of it
  * spike ratio  = current top-5 qty / baseline (relative, never absolute)
  * qty added / pulled versus the previous capture of the SAME symbol
  * the single price level that changed most (before/after qty and splits)
  * seconds elapsed since the previous capture — pull "speed" is meaningless
    without it, because this feed polls scripts in rotation (median ~26 s,
    p90 ~2 min for liquid names, far slower for thin ones)

Limits stated up front: there is no batch id, so a "frame" is one capture of
one symbol; LTP is not on the feed, so the mid of the best bid/ask stands in
for price; a pull that happens between two captures is invisible.
"""
from __future__ import annotations

import statistics
from datetime import date
from typing import Iterable

from django.core.cache import cache
from django.db.models import Count, Max, Min

from core_analysis.models import MarketDepthSnapshot

BASELINE_WINDOW = 20        # prior captures used for the rolling median
MIN_BASELINE_FRAMES = 5     # no baseline (so no spike, no event) before this much history
MIN_BASELINE_SHARES = 100   # a baseline below this is noise: a 5-share book gives 1000x spikes
MIN_EVENT_SHARES = 100      # ignore level changes below this many shares
EVENT_FRACTION = 0.5        # level change must be >= this fraction of baseline top-5 qty
CACHE_TTL = 10 * 60


# ---------------------------------------------------------------- catalog
def available_dates(limit=40):
    qs = (MarketDepthSnapshot.objects.values_list("business_date", flat=True)
          .distinct().order_by("-business_date")[:limit])
    return [d.isoformat() for d in qs]


def symbols_for(day: date):
    rows = (MarketDepthSnapshot.objects.filter(business_date=day)
            .values("symbol").annotate(n=Count("id"), first=Min("captured_at"), last=Max("captured_at"))
            .order_by("symbol"))
    return [{"symbol": r["symbol"], "snapshots": r["n"],
             "first": r["first"].strftime("%H:%M:%S"), "last": r["last"].strftime("%H:%M:%S")} for r in rows]


# ---------------------------------------------------------------- helpers
def _levels(depth, side):
    return [x for x in (depth or {}).get(side, []) if isinstance(x, dict)]


def _by_price(levels):
    return {round(float(x.get("price") or 0), 2): x for x in levels}


def _median(values):
    """Rolling baseline: None until there is enough history and the book is not trivially thin."""
    vals = [v for v in values if v is not None]
    if len(vals) < MIN_BASELINE_FRAMES:
        return None
    m = float(statistics.median(vals))
    return m if m >= MIN_BASELINE_SHARES else None


def _biggest_level_change(prev_levels, cur_levels):
    """Largest |qty change| at one price across prev and cur books.

    Tracked by PRICE, not by rank: an order that slides from level 5 to level 3
    because neighbours were filled has not changed, and rank comparison would
    report a phantom add and pull.
    """
    p, c = _by_price(prev_levels), _by_price(cur_levels)
    best = None
    for price in set(p) | set(c):
        q0 = int(p.get(price, {}).get("qty") or 0)
        q1 = int(c.get(price, {}).get("qty") or 0)
        d = q1 - q0
        if abs(d) < MIN_EVENT_SHARES:
            continue
        if best is None or abs(d) > abs(best["delta"]):
            best = {
                "price": price, "qty_before": q0, "qty_after": q1, "delta": d,
                "splits_before": int(p.get(price, {}).get("splits") or 0),
                "splits_after": int(c.get(price, {}).get("splits") or 0),
                "pct": round(d / q0 * 100.0, 1) if q0 else None,
                "in_book_before": price in p, "in_book_after": price in c,
            }
    return best


# ---------------------------------------------------------------- frames
def frames(symbol: str, day: date):
    """Every capture of one symbol on one day, oldest first, with features."""
    rows = list(
        MarketDepthSnapshot.objects.filter(symbol=symbol, business_date=day)
        .order_by("captured_at")
        .values("captured_at", "depth", "total_bids", "total_asks",
                "bid_qty_top5", "ask_qty_top5", "bid_splits_top5", "ask_splits_top5",
                "best_bid", "best_ask")
    )
    out = []
    hist_bid, hist_ask = [], []
    prev = None
    for r in rows:
        bids, asks = _levels(r["depth"], "bids"), _levels(r["depth"], "asks")
        bq, aq = int(r["bid_qty_top5"]), int(r["ask_qty_top5"])
        base_b = _median(hist_bid[-BASELINE_WINDOW:])
        base_a = _median(hist_ask[-BASELINE_WINDOW:])
        bb = float(r["best_bid"]) if r["best_bid"] is not None else None
        ba = float(r["best_ask"]) if r["best_ask"] is not None else None
        mid = (bb + ba) / 2 if (bb and ba) else (bb or ba)

        f = {
            "t": r["captured_at"].strftime("%H:%M:%S"),
            "ts": r["captured_at"].isoformat(),
            "bids": bids, "asks": asks,
            "best_bid": bb, "best_ask": ba, "mid": round(mid, 2) if mid else None,
            "spread_pct": round((ba - bb) / mid * 100.0, 2) if (bb and ba and mid) else None,
            "total_bids": int(r["total_bids"]), "total_asks": int(r["total_asks"]),
            "bid_qty": bq, "ask_qty": aq,
            "bid_splits": int(r["bid_splits_top5"]), "ask_splits": int(r["ask_splits_top5"]),
            "bid_qty_per_split": round(bq / r["bid_splits_top5"], 1) if r["bid_splits_top5"] else None,
            "ask_qty_per_split": round(aq / r["ask_splits_top5"], 1) if r["ask_splits_top5"] else None,
            "bid_baseline": round(base_b) if base_b else None,
            "ask_baseline": round(base_a) if base_a else None,
            "bid_spike": round(bq / base_b, 2) if base_b else None,
            "ask_spike": round(aq / base_a, 2) if base_a else None,
            "secs_since_prev": None,
            "bid_delta": None, "ask_delta": None,
            "bid_delta_pct": None, "ask_delta_pct": None,
            "bid_level_change": None, "ask_level_change": None,
            "event": None,
        }
        if prev is not None:
            secs = (r["captured_at"] - prev["captured_at"]).total_seconds()
            f["secs_since_prev"] = round(secs, 1)
            f["bid_delta"] = bq - prev["bq"]
            f["ask_delta"] = aq - prev["aq"]
            f["bid_delta_pct"] = round(f["bid_delta"] / prev["bq"] * 100.0, 1) if prev["bq"] else None
            f["ask_delta_pct"] = round(f["ask_delta"] / prev["aq"] * 100.0, 1) if prev["aq"] else None
            f["bid_level_change"] = _biggest_level_change(prev["bids"], bids)
            f["ask_level_change"] = _biggest_level_change(prev["asks"], asks)
            f["event"] = _classify(f, base_b, base_a, secs)
        out.append(f)
        hist_bid.append(bq)
        hist_ask.append(aq)
        prev = {"captured_at": r["captured_at"], "bq": bq, "aq": aq, "bids": bids, "asks": asks}
    return out


def _classify(f, base_b, base_a, secs):
    """Observation label for the frame, or None. Never an accusation."""
    cands = []
    for side, base, chg in (("BID", base_b, f["bid_level_change"]), ("ASK", base_a, f["ask_level_change"])):
        if not chg or not base:
            continue
        if abs(chg["delta"]) < EVENT_FRACTION * base:
            continue
        kind = "PULL" if chg["delta"] < 0 else "ADD"
        cands.append({
            "side": side, "kind": kind, "price": chg["price"],
            "shares": abs(chg["delta"]),
            "pct": chg["pct"],
            "vs_baseline": round(abs(chg["delta"]) / base, 2),
            "splits_before": chg["splits_before"], "splits_after": chg["splits_after"],
            "secs": secs,
            "shares_per_sec": round(abs(chg["delta"]) / secs, 1) if secs else None,
        })
    if not cands:
        return None
    return max(cands, key=lambda c: c["vs_baseline"])


def events(symbol: str, day: date):
    return [f for f in frames(symbol, day) if f["event"]]


# ---------------------------------------------------------------- overview
def overview(day: date):
    """Per-symbol summary for the day, cached: snapshots, cadence, max spikes, events."""
    key = f"mdepth:overview:v1:{day.isoformat()}"
    data = cache.get(key)
    if data is not None:
        return data
    rows = (MarketDepthSnapshot.objects.filter(business_date=day)
            .order_by("symbol", "captured_at")
            .values("symbol", "captured_at", "depth", "bid_qty_top5", "ask_qty_top5", "best_bid", "best_ask"))
    by_sym = {}
    for r in rows:
        by_sym.setdefault(r["symbol"], []).append(r)
    out = []
    for sym, rs in by_sym.items():
        hb, ha = [], []
        max_bs = max_as = 0.0
        pulls = adds = 0
        biggest = None
        prev = None
        gaps = []
        for r in rs:
            bq, aq = int(r["bid_qty_top5"]), int(r["ask_qty_top5"])
            base_b, base_a = _median(hb[-BASELINE_WINDOW:]), _median(ha[-BASELINE_WINDOW:])
            if base_b:
                max_bs = max(max_bs, bq / base_b)
            if base_a:
                max_as = max(max_as, aq / base_a)
            if prev is not None:
                secs = (r["captured_at"] - prev["captured_at"]).total_seconds()
                gaps.append(secs)
                for side, base, a, b in (("BID", base_b, _levels(prev["depth"], "bids"), _levels(r["depth"], "bids")),
                                         ("ASK", base_a, _levels(prev["depth"], "asks"), _levels(r["depth"], "asks"))):
                    chg = _biggest_level_change(a, b)
                    if not chg or not base or abs(chg["delta"]) < EVENT_FRACTION * base:
                        continue
                    if chg["delta"] < 0:
                        pulls += 1
                    else:
                        adds += 1
                    score = abs(chg["delta"]) / base
                    if biggest is None or score > biggest["vs_baseline"]:
                        biggest = {"t": r["captured_at"].strftime("%H:%M:%S"), "side": side,
                                   "kind": "PULL" if chg["delta"] < 0 else "ADD",
                                   "price": chg["price"], "shares": abs(chg["delta"]),
                                   "vs_baseline": round(score, 1), "secs": round(secs)}
            hb.append(bq)
            ha.append(aq)
            prev = r
        last = rs[-1]
        bb = float(last["best_bid"]) if last["best_bid"] is not None else None
        ba = float(last["best_ask"]) if last["best_ask"] is not None else None
        out.append({
            "symbol": sym,
            "snapshots": len(rs),
            "first": rs[0]["captured_at"].strftime("%H:%M:%S"),
            "last": last["captured_at"].strftime("%H:%M:%S"),
            "median_gap_s": round(statistics.median(gaps)) if gaps else None,
            "best_bid": bb, "best_ask": ba,
            "bid_qty": int(last["bid_qty_top5"]), "ask_qty": int(last["ask_qty_top5"]),
            "max_bid_spike": round(max_bs, 1), "max_ask_spike": round(max_as, 1),
            "pulls": pulls, "adds": adds,
            "biggest": biggest,
        })
    out.sort(key=lambda x: -(x["biggest"]["vs_baseline"] if x["biggest"] else 0))
    data = {"date": day.isoformat(), "symbols": len(out), "snapshots": sum(x["snapshots"] for x in out), "rows": out}
    cache.set(key, data, CACHE_TTL)
    return data
