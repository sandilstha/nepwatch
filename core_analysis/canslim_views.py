"""
canslim_views.py — page + JSON endpoints for the CAN SLIM screen.

Thin by design: every judgement lives in ``services.canslim``. These views only
shape the response and never compute a factor, so the screener page and the
Stock 360 card can never drift apart in what they claim.
"""
from __future__ import annotations

import logging

from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET

from core_analysis.services import canslim as cs

logger = logging.getLogger(__name__)

MAX_ROWS = 300


def _asset_version():
    from core_analysis.insights_views import _asset_version as v
    return v()


def canslim_view(request):
    """Render the screener shell; the table itself is fetched by JS."""
    return render(request, "core_analysis/canslim.html",
                  {"asset_version": _asset_version()})


@require_GET
def canslim_scan_api(request):
    """Market-wide ranked screen. Public: read-only, derived from public data."""
    sector = (request.GET.get("sector") or "All").strip()[:60]
    try:
        limit = max(0, min(MAX_ROWS, int(request.GET.get("limit", 0))))
    except (TypeError, ValueError):
        limit = 0
    try:
        data = cs.scan_market(sector=sector, limit=limit)
    except Exception:  # pragma: no cover - never 500 the page
        logger.exception("CAN SLIM scan failed")
        return JsonResponse({"ok": False, "error": "Could not build the screen."},
                            status=200)
    return JsonResponse(data)


@require_GET
def canslim_stock_api(request):
    """Seven-factor detail for one symbol, plus its rank in the market.

    The rank matters as much as the score: a CAN SLIM number means nothing in
    isolation, because every factor is a comparative judgement.
    """
    symbol = (request.GET.get("symbol") or "").strip().upper()[:20]
    if not symbol or not symbol.replace(".", "").replace("-", "").isalnum():
        return JsonResponse({"ok": False, "error": "Invalid symbol."}, status=200)
    try:
        data = cs.score_stock(symbol)
        # Rank comes from the CACHED market scan only. Running the full
        # ~300-symbol scan inline here made this anonymous endpoint a
        # thousand-query amplifier on every cache miss.
        from django.core.cache import cache
        scan = cache.get(f"canslim_scan_v{cs.SCAN_VERSION}") or {}
        row = next((r for r in scan.get("rows") or [] if r["symbol"] == symbol), None)
        data["rank"] = row.get("rank") if row else None
        data["percentile"] = row.get("percentile") if row else None
        data["universe"] = scan.get("universe")
        data["market_gate"] = scan.get("market_gate")
        data["unavailable_notes"] = scan.get("unavailable")
        data["ok"] = data.get("score") is not None
        if data["score"] is None:
            data["error"] = data.get("score_basis")
    except Exception:  # pragma: no cover
        logger.exception("CAN SLIM stock read failed for %s", symbol)
        return JsonResponse({"ok": False, "error": "Could not score this stock."},
                            status=200)
    return JsonResponse(data)


def canslim_sop_view(request):
    """Plain-language methodology page. Static: every rule is in the template."""
    return render(request, "core_analysis/canslim_sop.html",
                  {"asset_version": _asset_version()})
