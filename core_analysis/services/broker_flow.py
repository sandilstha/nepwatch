"""Broker-to-broker share flow for a single stock, with intraday time filtering.

The floorsheet stores one row per executed trade, each carrying the selling and
the buying broker. Aggregating those rows by ``(seller, buyer)`` gives the actual
flow of shares between desks — who accumulated from whom — which is what the
flow map draws: sellers down the left, buyers down the right, ribbon width in
proportion to the value transferred.

Time filtering is the point of the study: ``trade_time`` is populated on every
row, so any window of the session (open drive, mid-day, closing auction) can be
isolated and compared. Queries are always pinned to one ``(stock_symbol,
business_date)`` pair — which the table is indexed on — so a full-session
aggregation for one stock runs in milliseconds even though the table holds tens
of millions of rows.
"""

import logging
from datetime import date as date_cls, time as time_cls

from django.core.cache import cache
from django.db.models import Sum, Count, Min, Max

logger = logging.getLogger(__name__)

_CACHE_TTL = 10 * 60

# Brokers outside the top N per side are pooled into one "Other" node, as the
# reference visual does. 8 matches the categorical palette exactly: every named
# desk gets a hue that is validated distinct from its neighbours, and no 9th
# colour has to be invented. Callers can raise it via ?top= if they would rather
# have more nodes than guaranteed-distinct colours.
_DEFAULT_TOP_N = 8
OTHER_KEY = "Other"

# Width of one playback frame / volume bar, in minutes. 5 gives roughly 50
# steps across a NEPSE session — fine enough to see a block trade land, without
# the payload or the animation becoming unwieldy. The client reads this back off
# the response rather than assuming it, so the timeline's drag-to-select maths
# can never drift out of sync with the server.
BUCKET_MINUTES = 5

# Multi-day timeframes for the map. Spans are calendar days back from the
# symbol's own latest session (not the market's), then handed to the floorsheet
# as a business_date range — non-trading days simply contribute no rows, so
# holidays need no special casing. "fy" resolves from Shrawan 1 instead of a span.
RANGE_DAYS = {"today": 1, "1w": 7, "1m": 30, "3m": 90, "6m": 182, "1y": 365}
NAMED_RANGES = set(RANGE_DAYS) | {"fy"}

# A year of a liquid stock is a few hundred thousand floorsheet rows for ONE
# symbol, which the (stock_symbol, business_date) index handles. This caps a
# hand-typed custom window so it can't turn into a table scan.
MAX_RANGE_DAYS = 400


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _parse_time(raw):
    """'HH:MM' or 'HH:MM:SS' -> time, else None."""
    raw = (raw or "").strip()
    if not raw:
        return None
    for fmt in ("%H:%M:%S", "%H:%M"):
        try:
            from datetime import datetime

            return datetime.strptime(raw, fmt).time()
        except ValueError:
            continue
    return None


def _parse_date(raw):
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return date_cls.fromisoformat(raw)
    except ValueError:
        return None


def latest_session(symbol=None):
    """Newest business_date on the floorsheet (optionally for one symbol)."""
    from core_analysis.models import NepseFloorsheet as FSH

    qs = FSH.objects.all()
    if symbol:
        qs = qs.filter(stock_symbol=symbol)
    return qs.order_by("-business_date").values_list("business_date", flat=True).first()


def resolve_range(symbol=None, range_key="today", start=None, end=None):
    """Resolve a timeframe selection to an inclusive ``(d_from, d_to)`` pair.

    Anchored on the SYMBOL's newest session rather than the market's, so a stock
    that has not traded for a few days still shows its own most recent activity
    instead of an empty window.
    """
    from datetime import timedelta

    rk = (range_key or "today").strip().lower()

    if rk == "custom":
        d_from, d_to = _parse_date(start), _parse_date(end)
        if not d_from or not d_to:
            rk = "today"
        else:
            if d_from > d_to:
                d_from, d_to = d_to, d_from
            if (d_to - d_from).days + 1 > MAX_RANGE_DAYS:
                d_from = d_to - timedelta(days=MAX_RANGE_DAYS - 1)
            return d_from, d_to, "custom"

    anchor = latest_session(symbol)
    if not anchor:
        return None, None, rk
    if isinstance(anchor, str):
        anchor = _parse_date(anchor)

    if rk == "today":
        return anchor, anchor, "today"
    if rk == "fy":
        from core_analysis.services.broker_analytics import _fiscal_year_start

        return _fiscal_year_start(anchor), anchor, "fy"
    if rk not in RANGE_DAYS:
        return anchor, anchor, "today"
    return anchor - timedelta(days=RANGE_DAYS[rk] - 1), anchor, rk


def _node_list(pair_rows, side, top_n):
    """Per-broker totals for one side, top N by value with the rest pooled."""
    agg = {}
    for r in pair_rows:
        key = r[side]
        if key is None:
            continue
        slot = agg.setdefault(key, {"quantity": 0, "amount": 0.0, "trades": 0})
        slot["quantity"] += int(r["quantity"] or 0)
        slot["amount"] += _f(r["amount"])
        slot["trades"] += int(r["trades"] or 0)

    ordered = sorted(agg.items(), key=lambda kv: kv[1]["amount"], reverse=True)
    keep = {k for k, _ in ordered[:top_n]}
    return keep, agg


def _rank_by_volume(agg, limit=10):
    """Top brokers on one side ranked by SHARES (not value), never pooled.

    The diagram's nodes rank by turnover and fold the tail into "Other"; this is a
    separate leaderboard, so it keeps real broker numbers all the way down.
    """
    ordered = sorted(agg.items(), key=lambda kv: kv[1]["quantity"], reverse=True)
    total = sum(v["quantity"] for v in agg.values()) or 0
    out = []
    for rank, (key, v) in enumerate(ordered[:limit], 1):
        qty = int(v["quantity"] or 0)
        out.append({
            "rank": rank,
            "key": key,
            "quantity": qty,
            "amount": round(v["amount"], 2),
            "trades": int(v["trades"] or 0),
            "avg_rate": round(v["amount"] / qty, 2) if qty else None,
            "pct": round(100.0 * qty / total, 2) if total else None,
        })
    return out


def broker_flow(symbol, business_date=None, t_from=None, t_to=None, top_n=_DEFAULT_TOP_N,
                range_key=None, start=None, end=None):
    """Seller -> buyer flows for ``symbol`` over a date range and time window.

    Returns nodes for both sides (with volume-weighted average price, the number
    the reference visual prints next to each broker) and the links between them.
    ``None`` when the symbol did not trade in the window.

    Two independent filters compose here. The DATE range picks the sessions;
    the intraday time window then applies *within each* of them — so "the first
    30 minutes, every day for the last month" is a single query, which is the
    whole point of keeping them separate.

    ``business_date`` remains supported for a single explicit session; a
    ``range_key`` (or custom start/end) takes precedence when given.
    """
    from core_analysis.models import NepseFloorsheet as FSH

    sym = (symbol or "").strip().upper()
    if not sym:
        return None

    if range_key or start or end:
        d_from, d_to, rk = resolve_range(sym, range_key or "custom", start, end)
    else:
        day = business_date or latest_session(sym)
        if isinstance(day, str):
            day = _parse_date(day) or latest_session(sym)
        d_from = d_to = day
        rk = "today"
    if not d_from or not d_to:
        return None

    tf, tt = t_from, t_to
    cache_key = f"bflow:v4:{sym}:{d_from}:{d_to}:{tf}:{tt}:{top_n}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached or None

    base = FSH.objects.filter(
        stock_symbol=sym, business_date__gte=d_from, business_date__lte=d_to
    )

    # Bounds are reported unfiltered so the UI can size its time slider against
    # the real trading window, and its date inputs against the real sessions
    # present, rather than against the current selection.
    bounds = base.aggregate(
        first=Min("trade_time"), last=Max("trade_time"),
        d_first=Min("business_date"), d_last=Max("business_date"),
    )
    if bounds["first"] is None:
        cache.set(cache_key, {}, _CACHE_TTL)
        return None

    sessions = base.values_list("business_date", flat=True).distinct().count()

    scoped = base
    if tf:
        scoped = scoped.filter(trade_time__gte=tf)
    if tt:
        scoped = scoped.filter(trade_time__lte=tt)

    pair_rows = list(
        scoped.values("seller", "buyer").annotate(
            quantity=Sum("quantity"), amount=Sum("amount"), trades=Count("id")
        )
    )
    if not pair_rows:
        empty = {
            "ok": True, "symbol": sym, "date": bounds["d_last"].isoformat(),
            "range": {"key": rk, "from": d_from.isoformat(), "to": d_to.isoformat(),
                      "first": bounds["d_first"].isoformat(),
                      "last": bounds["d_last"].isoformat(), "sessions": sessions},
            "session": {"first": bounds["first"].strftime("%H:%M:%S"),
                        "last": bounds["last"].strftime("%H:%M:%S")},
            "window": {"from": tf or None, "to": tt or None},
            "totals": {"quantity": 0, "amount": 0.0, "trades": 0},
            "sellers": [], "buyers": [], "links": [], "pairs": [],
            "top_sellers": [], "top_buyers": [], "empty": True,
        }
        cache.set(cache_key, empty, _CACHE_TTL)
        return empty

    sell_keep, sell_agg = _node_list(pair_rows, "seller", top_n)
    buy_keep, buy_agg = _node_list(pair_rows, "buyer", top_n)

    # Collapse both endpoints of every pair to their display node, so a link into
    # "Other" merges with the other pooled links instead of being dropped.
    links = {}
    for r in pair_rows:
        s = r["seller"] if r["seller"] in sell_keep else OTHER_KEY
        b = r["buyer"] if r["buyer"] in buy_keep else OTHER_KEY
        slot = links.setdefault((s, b), {"quantity": 0, "amount": 0.0, "trades": 0})
        slot["quantity"] += int(r["quantity"] or 0)
        slot["amount"] += _f(r["amount"])
        slot["trades"] += int(r["trades"] or 0)

    def _nodes(agg, keep):
        out, other = [], {"quantity": 0, "amount": 0.0, "trades": 0}
        for key, v in agg.items():
            target = None
            if key in keep:
                target = {"key": key, **v}
            else:
                other["quantity"] += v["quantity"]
                other["amount"] += v["amount"]
                other["trades"] += v["trades"]
            if target:
                out.append(target)
        if other["trades"]:
            out.append({"key": OTHER_KEY, **other})
        for n in out:
            # Volume-weighted average price — the figure printed beside each broker.
            n["avg_rate"] = round(n["amount"] / n["quantity"], 2) if n["quantity"] else None
            n["amount"] = round(n["amount"], 2)
        out.sort(key=lambda n: (n["key"] == OTHER_KEY, -n["amount"]))
        return out

    link_rows = []
    for (s, b), v in links.items():
        link_rows.append({
            "seller": s, "buyer": b,
            "quantity": v["quantity"],
            "amount": round(v["amount"], 2),
            "trades": v["trades"],
            "avg_rate": round(v["amount"] / v["quantity"], 2) if v["quantity"] else None,
        })
    link_rows.sort(key=lambda r: r["amount"], reverse=True)

    # The ribbons pool everything past the top-N into "Other" so the diagram stays
    # readable, but the table underneath is the audit trail — it gets the real
    # seller/buyer numbers on every row, uncollapsed.
    pair_detail = [
        {
            "seller": r["seller"], "buyer": r["buyer"],
            "quantity": int(r["quantity"] or 0),
            "amount": round(_f(r["amount"]), 2),
            "trades": int(r["trades"] or 0),
            "avg_rate": (round(_f(r["amount"]) / int(r["quantity"]), 2)
                         if r["quantity"] else None),
        }
        for r in pair_rows
    ]
    pair_detail.sort(key=lambda r: r["amount"], reverse=True)

    total_qty = sum(r["quantity"] or 0 for r in pair_rows)
    total_amt = sum(_f(r["amount"]) for r in pair_rows)
    total_trd = sum(r["trades"] or 0 for r in pair_rows)

    result = {
        "ok": True,
        "symbol": sym,
        "date": bounds["d_last"].isoformat(),
        "range": {"key": rk, "from": d_from.isoformat(), "to": d_to.isoformat(),
                  "first": bounds["d_first"].isoformat(),
                  "last": bounds["d_last"].isoformat(), "sessions": sessions},
        "session": {"first": bounds["first"].strftime("%H:%M:%S"),
                    "last": bounds["last"].strftime("%H:%M:%S")},
        "window": {"from": tf or None, "to": tt or None},
        "totals": {"quantity": total_qty, "amount": round(total_amt, 2), "trades": total_trd},
        "sellers": _nodes(sell_agg, sell_keep),
        "buyers": _nodes(buy_agg, buy_keep),
        "links": link_rows,
        "pairs": pair_detail,
        "top_sellers": _rank_by_volume(sell_agg),
        "top_buyers": _rank_by_volume(buy_agg),
    }
    cache.set(cache_key, result, _CACHE_TTL)
    return result


def _scoped(symbol, d_from, d_to, t_from=None, t_to=None):
    """Floorsheet rows for one symbol across a date range and intraday window."""
    from core_analysis.models import NepseFloorsheet as FSH

    qs = FSH.objects.filter(
        stock_symbol=symbol, business_date__gte=d_from, business_date__lte=d_to
    )
    if t_from:
        qs = qs.filter(trade_time__gte=t_from)
    if t_to:
        qs = qs.filter(trade_time__lte=t_to)
    return qs


def flow_frames(symbol, business_date=None, bucket_minutes=BUCKET_MINUTES, top_n=_DEFAULT_TOP_N,
                t_from=None, t_to=None, range_key=None, start=None, end=None):
    """Per-bucket flows for the play animation, sent in ONE response.

    The bucket is chosen by span: a single session animates in ``bucket_minutes``
    steps, while a multi-session range animates **one step per trading day**.
    Slicing a month into 5-minute buckets would be wrong — it would interleave
    different days into the same bucket, so 10:05 on Sunday would be summed with
    10:05 on Thursday and the "animation" would show no progression at all.

    The node set is resolved across the whole SELECTED window (not per bucket), so
    the columns keep a stable layout while the ribbons build up — otherwise
    brokers would jump between frames and the animation would be unreadable.

    The map's time window applies here too: frames that ignored it would
    accumulate past the windowed total (a >100% progress readout) and could
    reference brokers absent from the windowed node set, whose ribbons the
    renderer would then drop.
    """
    sym = (symbol or "").strip().upper()
    if not sym:
        return []

    if range_key or start or end:
        d_from, d_to, _ = resolve_range(sym, range_key or "custom", start, end)
    else:
        d_from = d_to = business_date or latest_session(sym)
    if not d_from or not d_to:
        return []

    qs = _scoped(sym, d_from, d_to, t_from, t_to)

    # Group in the database, not in Python: a year of a liquid stock is hundreds
    # of thousands of trades, and only the per-bucket aggregate is ever needed.
    by_day = d_from != d_to
    if by_day:
        rows = list(
            qs.values("business_date", "seller", "buyer").annotate(
                quantity=Sum("quantity"), amount=Sum("amount"), trades=Count("id")
            )
        )
    else:
        rows = list(qs.values("seller", "buyer", "trade_time", "quantity", "amount"))
    if not rows:
        return []

    # Stable display nodes, decided on the whole window's value.
    whole = {}
    for r in rows:
        k = (r["seller"], r["buyer"])
        slot = whole.setdefault(k, {"quantity": 0, "amount": 0.0, "trades": 0})
        slot["quantity"] += int(r["quantity"] or 0)
        slot["amount"] += _f(r["amount"])
        slot["trades"] += int(r.get("trades") or 1)
    pair_rows = [{"seller": s, "buyer": b, **v} for (s, b), v in whole.items()]
    sell_keep, _ = _node_list(pair_rows, "seller", top_n)
    buy_keep, _ = _node_list(pair_rows, "buyer", top_n)

    buckets = {}
    for r in rows:
        if by_day:
            bucket = r["business_date"]
        else:
            t = r["trade_time"]
            if t is None:
                continue
            bucket = (t.hour * 60 + t.minute) // bucket_minutes * bucket_minutes
        s = r["seller"] if r["seller"] in sell_keep else OTHER_KEY
        b = r["buyer"] if r["buyer"] in buy_keep else OTHER_KEY
        cell = buckets.setdefault(bucket, {}).setdefault(
            (s, b), {"quantity": 0, "amount": 0.0, "trades": 0}
        )
        cell["quantity"] += int(r["quantity"] or 0)
        cell["amount"] += _f(r["amount"])
        cell["trades"] += int(r.get("trades") or 1)

    frames = []
    for bucket in sorted(buckets):
        links = [
            {"seller": s, "buyer": b, "quantity": v["quantity"],
             "amount": round(v["amount"], 2), "trades": v["trades"]}
            for (s, b), v in buckets[bucket].items()
        ]
        if by_day:
            label = {"start": bucket.isoformat(), "end": bucket.isoformat()}
        else:
            e = bucket + bucket_minutes
            label = {"start": f"{bucket // 60:02d}:{bucket % 60:02d}",
                     "end": f"{e // 60:02d}:{e % 60:02d}"}
        frames.append({
            **label,
            "links": links,
            "quantity": sum(l["quantity"] for l in links),
            "amount": round(sum(l["amount"] for l in links), 2),
            "trades": sum(l["trades"] for l in links),
        })
    return frames


def flow_timeline(symbol, business_date=None, bucket_minutes=BUCKET_MINUTES,
                  range_key=None, start=None, end=None):
    """Traded value per bucket — powers the timeline strip under the map.

    Buckets by 5 minutes for a single session and by trading day for a range, so
    the strip always reads as a progression. Lets you see WHERE the volume sat
    before choosing a window to study, instead of picking times blind.
    """
    sym = (symbol or "").strip().upper()
    if not sym:
        return []

    if range_key or start or end:
        d_from, d_to, _ = resolve_range(sym, range_key or "custom", start, end)
    else:
        d_from = d_to = business_date or latest_session(sym)
    if not d_from or not d_to:
        return []

    qs = _scoped(sym, d_from, d_to)
    by_day = d_from != d_to

    if by_day:
        rows = qs.values("business_date").annotate(
            quantity=Sum("quantity"), amount=Sum("amount"), trades=Count("id")
        )
        return [
            {"start": r["business_date"].isoformat(), "quantity": int(r["quantity"] or 0),
             "amount": round(_f(r["amount"]), 2), "trades": int(r["trades"] or 0)}
            for r in sorted(rows, key=lambda r: r["business_date"])
        ]

    buckets = {}
    for t, q, a in qs.values_list("trade_time", "quantity", "amount"):
        if t is None:
            continue
        minute = (t.hour * 60 + t.minute) // bucket_minutes * bucket_minutes
        slot = buckets.setdefault(minute, {"quantity": 0, "amount": 0.0, "trades": 0})
        slot["quantity"] += int(q or 0)
        slot["amount"] += _f(a)
        slot["trades"] += 1
    out = []
    for minute in sorted(buckets):
        v = buckets[minute]
        out.append({
            "start": f"{minute // 60:02d}:{minute % 60:02d}",
            "quantity": v["quantity"],
            "amount": round(v["amount"], 2),
            "trades": v["trades"],
        })
    return out


def net_buy_series(symbol, range_key="1m", start=None, end=None, top_n=6):
    """Cumulative net-buy (bought minus sold, in shares) per broker, per session.

    The read that turns a floorsheet into a conviction story: a broker who
    accumulates a little every day is doing something different from one who
    printed a single block. Returns the top ``top_n`` brokers by absolute net
    position over the window, each with its running total across the sessions.

    Two GROUP BY queries pinned to ONE ``stock_symbol`` — the buy side and the
    sell side — merged in Python. The market-wide broker analytics must build
    per-day and merge because a wide GROUP BY across every symbol is orders of
    magnitude slower; a single symbol rides the ``(stock_symbol, business_date)``
    index, so the range query is fine here.
    """
    from core_analysis.models import NepseFloorsheet as FSH

    sym = (symbol or "").strip().upper()
    if not sym:
        return None

    d_from, d_to, rk = resolve_range(sym, range_key, start, end)
    if not d_from or not d_to:
        return None

    cache_key = f"bnetseries:v1:{sym}:{d_from}:{d_to}:{top_n}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached or None

    base = FSH.objects.filter(
        stock_symbol=sym, business_date__gte=d_from, business_date__lte=d_to
    )

    net = {}       # broker -> {date -> net quantity}
    dates = set()
    for side, field, sign in (("buy", "buyer", 1), ("sell", "seller", -1)):
        rows = base.values("business_date", field).annotate(quantity=Sum("quantity"))
        for r in rows:
            broker = r[field]
            d = r["business_date"]
            if broker is None or d is None:
                continue
            dates.add(d)
            net.setdefault(broker, {})
            net[broker][d] = net[broker].get(d, 0) + sign * int(r["quantity"] or 0)

    if not dates:
        cache.set(cache_key, {}, _CACHE_TTL)
        return None

    ordered = sorted(dates)
    totals = {b: sum(v.values()) for b, v in net.items()}
    ranked = sorted(totals, key=lambda b: abs(totals[b]), reverse=True)[:top_n]

    series = []
    for broker in ranked:
        per_day = net[broker]
        running, points = 0, []
        for d in ordered:
            running += per_day.get(d, 0)
            points.append(running)
        series.append({
            "key": broker,
            "net": totals[broker],
            "side": "buy" if totals[broker] > 0 else ("sell" if totals[broker] < 0 else "flat"),
            "points": points,
        })

    result = {
        "ok": True,
        "symbol": sym,
        "range": {"key": rk, "from": d_from.isoformat(), "to": d_to.isoformat()},
        "dates": [d.isoformat() for d in ordered],
        "sessions": len(ordered),
        "series": series,
        "accumulators": sum(1 for b in totals.values() if b > 0),
        "distributors": sum(1 for b in totals.values() if b < 0),
        "brokers": len(totals),
    }
    cache.set(cache_key, result, _CACHE_TTL)
    return result
