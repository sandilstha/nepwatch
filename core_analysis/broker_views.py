"""
broker_views.py — page + JSON endpoints for the Dalal Street X broker dashboard
(rendered on the Floor sheet page). All heavy lifting lives in
``services.broker_analytics``; these views are thin, fail soft (never 500 the
page), and return JSON the frontend renders per tab.
"""
from __future__ import annotations

import logging
import re
from datetime import date
from functools import wraps

from django.http import JsonResponse
from django.shortcuts import render
from django.contrib.auth.decorators import login_required
from django.views.decorators.http import require_GET, require_POST

from core_analysis.services import broker_analytics as ba

logger = logging.getLogger(__name__)

MAX_SELECTED_BROKERS = 200
_SYMBOL_RE = re.compile(r"^[A-Z0-9._-]{1,50}$")


class QueryValidationError(ValueError):
    """A malformed dashboard query that should produce a clean HTTP 400."""


def _validated_query(view):
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        try:
            return view(request, *args, **kwargs)
        except QueryValidationError as exc:
            return JsonResponse({"ok": False, "error": str(exc)}, status=400)

    return wrapped


def _asset_version():
    # Reuse the Market Insights cache-bust helper so all pages move together.
    from core_analysis.insights_views import _asset_version as v

    return v()


def floorsheet_view(request):
    """Render the broker analytics dashboard shell (Floor sheet page)."""
    return render(
        request,
        "core_analysis/floorsheet.html",
        {"asset_version": _asset_version(), "meta": ba.meta_cached()},
    )


def floorsheet_sop_view(request):
    """Methodology SOP for the Floorsheet (Dalal Street X) broker desk.

    One anchored section per tab / metric; the whole-page (?) icon in the tab bar
    deep-links here. Static content — every formula, data source and assumption is
    documented in the template itself, so it never touches the analytics engine.
    """
    return render(
        request,
        "core_analysis/floorsheet_sop.html",
        {"asset_version": _asset_version()},
    )


def accumulation_sop_view(request):
    """Methodology SOP for the A/D Radar tab.

    Its own page rather than a section of the Floorsheet SOP: A/D Radar is the
    only metric on the desk that is backtested rather than purely descriptive,
    so it has to document what it was measured to be worth, what was rejected,
    and where it fails. Static content — it never touches the analytics engine.
    """
    return render(
        request,
        "core_analysis/accumulation_sop.html",
        {"asset_version": _asset_version()},
    )


def _window(request):
    """Extract the shared date-range selection (range + optional custom dates).

    Returns a kwargs dict (``range_key`` always; ``start`` / ``end`` only when a
    'custom' range supplies them) to splat into any analytics builder.
    """
    range_key = (request.GET.get("range") or "today").strip().lower()
    if range_key not in ba.NAMED_RANGES | {"custom"}:
        raise QueryValidationError("Unknown date range.")
    kw = {"range_key": range_key}
    if range_key == "custom":
        start_raw = request.GET.get("start_date") or request.GET.get("start")
        end_raw = request.GET.get("end_date") or request.GET.get("end")
        try:
            start_date = date.fromisoformat((start_raw or "").strip())
            end_date = date.fromisoformat((end_raw or "").strip())
        except ValueError:
            raise QueryValidationError("Custom dates must use YYYY-MM-DD format.")
        if start_date > end_date:
            raise QueryValidationError("Custom start date must not be after the end date.")
        if (end_date - start_date).days + 1 > ba.CUSTOM_RANGE_MAX_DAYS:
            raise QueryValidationError(
                f"Custom date range cannot exceed {ba.CUSTOM_RANGE_MAX_DAYS} days."
            )
        kw["start"] = start_date.isoformat()
        kw["end"] = end_date.isoformat()
    return kw


def _brokers(request):
    raw = request.GET.get("brokers") or request.GET.get("broker") or ""
    parts = [part.strip() for part in raw.split(",") if part.strip()]
    if len(parts) > MAX_SELECTED_BROKERS:
        raise QueryValidationError(
            f"Select no more than {MAX_SELECTED_BROKERS} brokers."
        )
    brokers, seen = [], set()
    for part in parts:
        if len(part) > 6 or not part.isascii() or not part.isdecimal():
            raise QueryValidationError("Broker numbers must be positive integers.")
        broker = int(part)
        if broker <= 0:
            raise QueryValidationError("Broker numbers must be positive integers.")
        if broker not in seen:
            seen.add(broker)
            brokers.append(broker)
    return brokers


def _view_mode(request):
    value = (request.GET.get("view") or "shares").strip().lower()
    if value not in {"shares", "turnover"}:
        raise QueryValidationError("View must be either 'shares' or 'turnover'.")
    return value


def _sector(request):
    value = (request.GET.get("sector") or "All").strip() or "All"
    if len(value) > 100:
        raise QueryValidationError("Sector is too long.")
    return value


def _symbol(request):
    value = (request.GET.get("symbol") or "").strip().upper()
    if not value or not _SYMBOL_RE.fullmatch(value):
        raise QueryValidationError("Symbol must be 1-50 valid ticker characters.")
    return value


def _boolean(request, name):
    value = (request.GET.get(name) or "0").strip().lower()
    if value in {"1", "true", "yes"}:
        return True
    if value in {"0", "false", "no", ""}:
        return False
    raise QueryValidationError(f"{name} must be true or false.")


def _safe(builder, *args, **kwargs):
    try:
        data = builder(*args, **kwargs)
        data.setdefault("ok", True)
        return JsonResponse(data)
    except Exception:  # pragma: no cover - defensive
        logger.exception("Broker analytics endpoint failed: %s", builder.__name__)
        return JsonResponse(
            {"ok": False, "error": "Unable to load broker data right now."}, status=500
        )


@require_GET
def broker_meta_api(request):
    return _safe(ba.meta)


@require_GET
@_validated_query
def broker_favorites_api(request):
    return _safe(
        ba.broker_favorites,
        _brokers(request),
        view=_view_mode(request),
        **_window(request),
    )


@require_GET
@_validated_query
def broker_persistence_api(request):
    # Same broker multi-select contract as favorites. Persistence is inherently a
    # multi-session lens, so it owns its own lookback (default 1 month, floored at
    # 1 week) rather than collapsing to one day when the tab is on "Current Day".
    rk = (request.GET.get("lookback") or "1m").strip().lower()
    if rk not in {"1w", "1m", "3m"}:
        raise QueryValidationError("Lookback must be 1w, 1m or 3m.")
    return _safe(
        ba.broker_persistence,
        _brokers(request),
        range_key=rk,
        sector=_sector(request),
        exclude_mf=_boolean(request, "exclude_mf"),
    )


@require_GET
@_validated_query
def broker_signals_api(request):
    # Bundles the four research-desk signals (divergence / breadth / two-sided /
    # sector rotation). Uses the shared date-window contract like the other desks
    # (range preset or custom start/end).
    return _safe(
        ba.broker_signals,
        _brokers(request),
        sector=_sector(request),
        exclude_mf=_boolean(request, "exclude_mf"),
        **_window(request),
    )


@require_GET
@_validated_query
def stock_wise_api(request):
    return _safe(
        ba.stock_wise,
        _symbol(request),
        view=_view_mode(request),
        **_window(request),
    )


@require_GET
@_validated_query
def broker_flow_map_api(request):
    """Seller -> buyer share flows for one stock, filtered to a time window.

    Params: ``symbol`` (required), ``date`` (defaults to the stock's newest
    session), ``from``/``to`` as HH:MM[:SS], ``top`` nodes per side, and
    ``timeline=1`` to include the per-bucket volume strip.
    """
    from .services import broker_flow as bf

    symbol = _symbol(request)
    day = bf._parse_date(request.GET.get("date"))
    t_from = bf._parse_time(request.GET.get("from"))
    t_to = bf._parse_time(request.GET.get("to"))

    # Timeframe selection. Absent (or "today") keeps the original single-session
    # behaviour, so an existing ?date= link still resolves to exactly one day.
    range_key = (request.GET.get("range") or "").strip().lower()
    if range_key and range_key not in bf.NAMED_RANGES | {"custom"}:
        raise QueryValidationError("Unknown timeframe.")
    start = end = None
    if range_key == "custom":
        start = bf._parse_date(request.GET.get("start_date") or request.GET.get("start"))
        end = bf._parse_date(request.GET.get("end_date") or request.GET.get("end"))
        if not start or not end:
            raise QueryValidationError("Custom dates must use YYYY-MM-DD format.")
        if (max(start, end) - min(start, end)).days + 1 > bf.MAX_RANGE_DAYS:
            raise QueryValidationError(
                f"Custom range cannot exceed {bf.MAX_RANGE_DAYS} days."
            )
        start, end = start.isoformat(), end.isoformat()

    try:
        top_n = max(3, min(20, int(request.GET.get("top", bf._DEFAULT_TOP_N))))
    except (TypeError, ValueError):
        top_n = bf._DEFAULT_TOP_N

    rng = {"range_key": range_key or None, "start": start, "end": end}

    try:
        data = bf.broker_flow(symbol, day, t_from, t_to, top_n, **rng)
    except Exception:  # pragma: no cover - defensive
        logger.exception("Broker flow map failed for %s", symbol)
        return JsonResponse({"ok": False, "error": "Unable to build the flow map."}, status=500)

    if not data:
        scope = "in that period" if range_key and range_key != "today" else "on that session"
        return JsonResponse(
            {"ok": False, "error": f"No floorsheet trades for {symbol} {scope}."},
            status=200,
        )

    # Bucket width travels with the payload so the timeline's drag-to-select
    # arithmetic follows the server instead of hardcoding a duplicate constant.
    data["bucket_minutes"] = bf.BUCKET_MINUTES
    # Multi-session ranges bucket per trading day, so the client labels and
    # drag-selects in dates rather than clock times.
    data["bucket_unit"] = "date" if data["range"]["from"] != data["range"]["to"] else "time"

    if _boolean(request, "timeline"):
        try:
            data["timeline"] = bf.flow_timeline(
                symbol, day or data["date"], **rng
            )
        except Exception:  # pragma: no cover - strip is best-effort
            logger.exception("Flow timeline failed for %s", symbol)
            data["timeline"] = []

    # Playback frames ship with the same response so the animation never has to
    # re-query per step.
    if _boolean(request, "frames"):
        try:
            data["frames"] = bf.flow_frames(
                symbol, day or data["date"], top_n=top_n, t_from=t_from, t_to=t_to, **rng
            )
        except Exception:  # pragma: no cover - playback is best-effort
            logger.exception("Flow frames failed for %s", symbol)
            data["frames"] = []

    return JsonResponse(data)


@require_GET
@_validated_query
def net_holding_api(request):
    return _safe(
        ba.net_holding,
        _brokers(request),
        exclude_mf=_boolean(request, "exclude_mf"),
        sector=_sector(request),
        **_window(request),
    )


@require_GET
@_validated_query
def broker_concentration_api(request):
    return _safe(
        ba.broker_concentration,
        sector=_sector(request),
        **_window(request),
    )


@require_GET
@_validated_query
def hotstocks_api(request):
    return _safe(
        ba.hotstocks,
        view=_view_mode(request),
        sector=_sector(request),
        **_window(request),
    )


@login_required(login_url="login")
@require_POST
def accumulation_ask_api(request):
    """Natural-language question answered against the CURRENT A/D scan.

    POST, not GET, for three reasons: the question is user input that does not
    belong in a URL or an access log, each call spends real API credit so it
    must not be triggered by a prefetch or a shared link, and it is not
    idempotent against the per-user daily quota.

    Login-gated for the same spend reason — an anonymous endpoint that costs
    money per hit is a bill waiting to happen.
    """
    import json as _json

    from core_analysis.services import desk_assistant as da

    try:
        body = _json.loads(request.body or b"{}")
    except ValueError:
        return JsonResponse({"ok": False, "error": "Malformed request."}, status=400)

    question = (body.get("question") or "").strip()
    if not question:
        return JsonResponse({"ok": False, "error": "Ask a question first."}, status=400)

    uid = request.user.id
    allowed, remaining = da.check_quota(uid)
    if not allowed:
        return JsonResponse(
            {"ok": False, "error": (
                f"Daily limit reached ({da.DAILY_QUESTION_CAP} questions). "
                f"It resets at midnight.")}, status=429)

    # Same validation as the GET endpoints: these values key a market-wide
    # scan cache, so unbounded input means unbounded distinct scans.
    range_key = ((body.get("range") or "1m").strip() or "1m").lower()
    if range_key not in ba.NAMED_RANGES | {"custom"}:
        return JsonResponse({"ok": False, "error": "Unknown date range."}, status=400)
    sector = (body.get("sector") or "All").strip()[:60] or "All"
    start = end = None
    if range_key == "custom":
        try:
            sd = date.fromisoformat(str(body.get("start") or "").strip())
            ed = date.fromisoformat(str(body.get("end") or "").strip())
        except ValueError:
            return JsonResponse({"ok": False, "error": "Custom dates must use YYYY-MM-DD format."}, status=400)
        if sd > ed or (ed - sd).days + 1 > ba.CUSTOM_RANGE_MAX_DAYS:
            return JsonResponse({"ok": False, "error": "Invalid custom date range."}, status=400)
        start, end = sd.isoformat(), ed.isoformat()

    try:
        out = da.ask(question, range_key=range_key, sector=sector, start=start, end=end)
    except Exception:  # pragma: no cover - defensive, never 500 the panel
        logger.exception("desk assistant failed")
        return JsonResponse({"ok": False, "error": "The assistant is unavailable."}, status=200)

    # Charge only for answers actually produced, so a provider outage or a
    # too-short window does not silently eat someone's daily allowance.
    if out.get("ok"):
        da.consume_quota(uid)
        _, remaining = da.check_quota(uid)
    out["quota_remaining"] = remaining
    return JsonResponse(out)


@require_GET
@_validated_query
def broker_flow_radar_api(request):
    return _safe(ba.broker_flow_radar, **_window(request))


@require_GET
@_validated_query
def accumulation_api(request):
    """Stock-first Accumulation / Distribution radar.

    Without ``symbol`` this returns the market-wide ranking; with one it returns
    that scrip's reading, still scored inside the full cross-section so the
    percentile means the same thing on both views.

    The window defaults to 1 month rather than the shared 'today' default: a
    single session cannot show accumulation. Only the *unset* default is
    upgraded — an explicitly requested short window is passed through and the
    service refuses it with an explanation, rather than silently returning a
    month of data under a label the user did not choose.
    """
    from core_analysis.services import accumulation as ad

    window = _window(request)
    if not request.GET.get("range"):
        window["range_key"] = "1m"

    symbol = (request.GET.get("symbol") or "").strip().upper()
    if symbol:
        if not _SYMBOL_RE.fullmatch(symbol):
            raise QueryValidationError("Symbol must be 1-50 valid ticker characters.")
        return _safe(ad.accumulation_detail, symbol, **window)

    def _scan(**kw):
        # `excluded_reasons` maps every excluded symbol to its reason — a few
        # hundred entries the scan view never renders. It exists for the detail
        # lookup, which reads it server-side, so drop it from the wire payload.
        data = dict(ad.accumulation_scan(**kw))
        data.pop("excluded_reasons", None)
        return data

    return _safe(_scan, sector=_sector(request), **window)


def market_depth_sop_view(request):
    """Methodology SOP for the Market Depth tab.

    Its own page like the A/D Radar SOP: the tab is the first stage of a staged
    design (raw features, no score), so the page has to say what each number is,
    what the capture gap hides, and what is deliberately not built yet. Static.
    """
    return render(
        request,
        "core_analysis/market_depth_sop.html",
        {"asset_version": _asset_version()},
    )


# ── Market Depth (TMS top-5 replay) ─────────────────────────────────────────
# Phase 2A-1 of the market-depth design: raw per-frame features and a day
# overview. No score yet. Data comes from market_depth_snapshots, synced by
# `manage.py sync_market_depth` from the feed host.

def _depth_day(request):
    from datetime import date as _date
    raw = (request.GET.get("date") or "").strip()
    if not raw:
        return None
    try:
        return _date.fromisoformat(raw)
    except ValueError:
        raise QueryValidationError("date must be YYYY-MM-DD.")


@require_GET
@_validated_query
def depth_dates_api(request):
    from core_analysis.services import market_depth as md
    return JsonResponse({"ok": True, "dates": md.available_dates()})


@require_GET
@_validated_query
def depth_overview_api(request):
    from datetime import date as _date
    from core_analysis.services import market_depth as md
    day = _depth_day(request)
    if day is None:
        dates = md.available_dates(1)
        if not dates:
            return JsonResponse({"ok": True, "date": None, "symbols": 0, "snapshots": 0, "rows": []})
        day = _date.fromisoformat(dates[0])
    return _safe(md.overview, day)


@require_GET
@_validated_query
def depth_frames_api(request):
    from core_analysis.services import market_depth as md
    day = _depth_day(request)
    symbol = (request.GET.get("symbol") or "").strip().upper()
    if day is None or not symbol:
        raise QueryValidationError("date and symbol are required.")
    if not _SYMBOL_RE.fullmatch(symbol):
        raise QueryValidationError("Symbol must be 1-50 valid ticker characters.")
    fr = md.frames(symbol, day)
    return JsonResponse({
        "ok": True, "symbol": symbol, "date": day.isoformat(),
        "count": len(fr), "events": sum(1 for f in fr if f["event"]),
        "frames": fr,
    })
