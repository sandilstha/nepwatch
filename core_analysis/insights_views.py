"""
insights_views.py — view layer for the Market Insights dashboard.

Two endpoints:
  * market_insights_view  — renders the dashboard shell with the first payload
                            embedded (fast first paint, no initial fetch).
  * market_insights_api   — JSON endpoint the page polls to auto-refresh.

Both fail gracefully: a DB / service error never 500s the page — the shell
renders with an error banner and the poller surfaces a "stale data" state.
"""
from __future__ import annotations

import logging
import os

from django.conf import settings
from django.contrib.staticfiles import finders
from django.core.cache import cache
from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET

from core_analysis.services.market_insights import (
    build_payload,
    sector_turnover_window,
    subindex_comparison,
)

logger = logging.getLogger(__name__)

DEFAULT_REFRESH_SECONDS = 30

# A forced rebuild bypasses the payload cache and re-runs every DB query, so
# rate-limit it: at most one forced rebuild per this window across all clients,
# regardless of how often "?force=1" is hit (manual-refresh spam / scripted GETs).
FORCE_COOLDOWN_KEY = "market_insights_force_cooldown"
FORCE_COOLDOWN_SECONDS = 60   # a forced rebuild is ~3s of external fetches + table scans

# Static assets fingerprinted for cache-busting: the page appends ?v=<version>
# to these so the browser fetches fresh copies whenever a file changes.
_ASSET_FILES = (
    "core_analysis/css/insights.css",
    "core_analysis/css/tokens.css",
    "core_analysis/css/responsive.css",
    "core_analysis/js/insights.js",
    "core_analysis/js/ohlc-chart.js",
    "core_analysis/js/tv-chart.js",
    "core_analysis/css/floorsheet.css",
    "core_analysis/js/floorsheet-brokers.js",
    "core_analysis/css/portfolio.css",
    "core_analysis/js/portfolio.js",
    "core_analysis/css/stock360.css",
    "core_analysis/js/stock360.js",
    "core_analysis/js/stock360-grid.js",
    # Shared by Stock 360 and the Fundamentals desk; left out of this list they
    # kept an unchanged ?v= and a styling change never reached the browser.
    "core_analysis/css/fundamentals-desk.css",
    "core_analysis/js/fundamentals.js",
    "core_analysis/js/fundamentals-panel.js",
    "core_analysis/css/nepse-data.css",
    "core_analysis/js/nepse-data.js",
)


def _asset_version():
    """Latest mtime across the dashboard's static assets (cache-bust token)."""
    latest = 0
    for rel in _ASSET_FILES:
        path = finders.find(rel)
        try:
            if path:
                latest = max(latest, int(os.path.getmtime(path)))
        except OSError:
            pass
    return latest or 1


_TV_LIBRARY_REL = "core_analysis/charting_library/charting_library.standalone.js"
_tv_installed_cache = None


def _tv_library_installed():
    """True if the licensed TradingView Advanced Charts bundle is present.

    Result is memoised — the file only appears at deploy time (see
    TRADINGVIEW_SETUP.md), so there's no need to stat it on every request.
    """
    global _tv_installed_cache
    if _tv_installed_cache is None:
        _tv_installed_cache = bool(finders.find(_TV_LIBRARY_REL))
    return _tv_installed_cache


def _refresh_seconds():
    """Initial auto-refresh interval in seconds; 0 means Manual.

    0 must survive the clamp. The old `max(5, ...)` silently turned Manual into
    a 5-second poll — the one setting that exists to STOP polling became the
    most aggressive one available. Anything above 0 is still floored at 5 so a
    stray `1` cannot hammer the API.
    """
    value = getattr(settings, "INSIGHTS_REFRESH_SECONDS", DEFAULT_REFRESH_SECONDS)
    try:
        value = int(value)
    except (TypeError, ValueError):
        return DEFAULT_REFRESH_SECONDS
    return 0 if value <= 0 else max(5, value)


def _empty_payload(error):
    return {
        "as_of": None,
        "has_data": False,
        "overview": {},
        "breadth": {},
        "gainers": [],
        "losers": [],
        "most_active": [],
        "sectors": [],
        "heatmap": [],
        "history": [],
        "stock_count": 0,
        "error": error,
    }


@require_GET
def market_insights_view(request):
    """Render the dashboard shell instantly.

    The payload is embedded ONLY if it is already cached — the render path never
    blocks on the external NEPSE feeds. On a cold cache the shell ships with an
    empty payload and the browser fetches the live data from /insights/api/ on
    demand (insights.js triggers an immediate fetch when the embedded payload
    carries no data). This keeps time-to-first-byte independent of how slow or
    reachable the upstream feeds are.
    """
    try:
        payload = build_payload(cache_only=True)
    except Exception:  # pragma: no cover - defensive
        logger.exception("Market Insights cached read failed")
        payload = None

    if payload is None:
        # Nothing cached yet: render the shell, let the client fetch on demand.
        payload = _empty_payload(None)
        error = None
    else:
        error = None if payload.get("has_data") else "No market data has been synced yet."

    context = {
        # json_script safely escapes </script>, <, >, & — never use |safe with
        # raw json.dumps here, that allows a stock name to break out of the tag.
        "bootstrap_payload": payload,
        "refresh_seconds": _refresh_seconds(),
        "load_error": error,
        "asset_version": _asset_version(),
        # Only emit the TradingView loader when the licensed library is actually
        # installed — otherwise it fires two guaranteed 404s on every page load.
        "tv_enabled": _tv_library_installed(),
    }
    return render(request, "core_analysis/market_insights.html", context)


@require_GET
def subindex_comparison_api(request):
    """JSON multi-series feed for the sub-index comparison chart.

    Accepts ?days=<sessions> (clamped server-side) and returns aligned daily
    closes for the NEPSE index plus every sub-index. Independent of the main
    dashboard payload so the heavy historical series isn't re-sent on every
    15s auto-refresh — the client fetches it once on load and on range change.
    """
    try:
        days = int(request.GET.get("days") or 0)
    except (TypeError, ValueError):
        days = 0
    try:
        data = subindex_comparison(days) if days else subindex_comparison()
        data["ok"] = True
        return JsonResponse(data)
    except Exception:  # pragma: no cover - defensive
        logger.exception("Sub-index comparison build failed")
        return JsonResponse(
            {"ok": False, "error": "Unable to load sub-index data right now."}, status=500
        )


@require_GET
def sector_turnover_api(request):
    """JSON feed for the Sector Turnover card's period selector.

    ?period=1D|1W|1M|1Q|6M|1Y, or period=custom with ?from=YYYY-MM-DD&to=YYYY-MM-DD.
    Served separately from the polled dashboard payload so switching the window
    never forces a full snapshot rebuild.
    """
    try:
        data = sector_turnover_window(
            request.GET.get("period"),
            request.GET.get("from"),
            request.GET.get("to"),
        )
        return JsonResponse(data)
    except Exception:  # pragma: no cover - defensive
        logger.exception("Sector turnover window build failed")
        return JsonResponse(
            {"ok": False, "error": "Unable to load sector turnover right now."}, status=500
        )


@require_GET
def market_insights_api(request):
    """JSON snapshot used by the front-end auto-refresh poller."""
    try:
        force = request.GET.get("force") == "1"
        fast = request.GET.get("fast") == "1"
        # Throttle forced rebuilds so "?force=1" can't be looped to bypass the
        # cache and hammer the DB; serve the cached payload once one fires.
        if force:
            if cache.get(FORCE_COOLDOWN_KEY):
                force = False
            else:
                cache.set(FORCE_COOLDOWN_KEY, 1, FORCE_COOLDOWN_SECONDS)
        payload = build_payload(force=force, fast=fast)
        payload["ok"] = True
        return JsonResponse(payload)
    except Exception:  # pragma: no cover - defensive
        logger.exception("Market Insights API build failed")
        return JsonResponse(
            {"ok": False, "error": "Unable to load market data right now."}, status=500
        )
