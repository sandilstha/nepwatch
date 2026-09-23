"""NEPSE Data — raw exchange report pages.

One generic page view and one generic JSON endpoint serve every report in
``services.nepse_reports.REPORTS``; the report's registry entry supplies the
title, the filter controls and the column spec. Adding a report is a registry
entry, not another view.

Views fail soft: a broken report returns an error payload rather than a 500 that
takes the page down.
"""
from __future__ import annotations

import json
import logging
import re

from django.http import JsonResponse, Http404
from django.shortcuts import render
from django.views.decorators.http import require_GET

from core_analysis.services import nepse_reports as nr

logger = logging.getLogger(__name__)

_SYMBOL_RE = re.compile(r"^[A-Z0-9._-]{1,50}$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _asset_version():
    from core_analysis.insights_views import _asset_version as v

    return v()


def _clean(request):
    """Whitelist the query params a report may receive.

    Values reach ORM filters, so each is shape-checked here rather than trusted;
    anything malformed is dropped so the report falls back to its default rather
    than erroring.
    """
    g = request.GET
    p = {}
    sym = (g.get("symbol") or "").strip().upper()
    if sym and _SYMBOL_RE.fullmatch(sym):
        p["symbol"] = sym
    for k in ("date", "start", "end"):
        v = (g.get(k) or "").strip()
        if v and _DATE_RE.fullmatch(v):
            p[k] = v
    idx = (g.get("sector") or g.get("index") or "").strip()
    if idx and len(idx) <= 100:
        p["sector"] = idx
    br = (g.get("broker") or "").strip()
    if br.isdigit() and len(br) <= 6:
        p["broker"] = br
    n = (g.get("n") or "").strip()
    if n.isdigit():
        p["n"] = n
    order = (g.get("order") or "").strip()
    if order in ("time", "quantity"):
        p["order"] = order
    page = (g.get("page") or "").strip()
    if page.isdigit():
        p["page"] = page
    ps = (g.get("page_size") or "").strip().lower()
    if ps.isdigit() or ps == "all":
        p["page_size"] = ps
    if (g.get("all") or "").strip().lower() in ("1", "true", "yes"):
        p["all"] = "1"
    return p


def nepse_data_view(request, slug=None):
    """Render one report page (shell only — rows arrive via the JSON API)."""
    slug = slug or nr.REPORT_ORDER[0]
    spec = nr.REPORTS.get(slug)
    if not spec:
        raise Http404("Unknown report")

    controls = spec.get("controls", [])
    return render(request, "core_analysis/nepse_data.html", {
        "asset_version": _asset_version(),
        # Drives base.html's shared workspace-header and the primary-nav
        # highlight. Without these the header falls through to its active_tab
        # switch and every NEPSE Data page announced itself as the Technical
        # Analysis Desk, with TECHNICAL ANALYSIS lit in the nav.
        "page_badge": "NEPSE",
        "page_title": spec["title"],
        "page_sub": spec.get("blurb", ""),
        "page_section": "nepsedata",
        "slug": slug,
        "spec": spec,
        "menu": nr.menu(),
        "controls": controls,
        # Serialised as real JSON, not a Python repr — the template inlines these
        # into a <script> block, where `True`/`None` would be a syntax error.
        "columns": spec["columns"],
        "controls": controls,
        # Stock Trading needs a symbol to return anything; the floor sheet is a
        # complete session that a symbol merely filters, so it must offer "all".
        "symbol_optional": slug != "stock-trading",
        "dates": nr.price_dates(180),
        "index_names": nr.index_names() if "index" in spec.get("controls", []) else [],
        "latest": {
            "price": nr.latest_price_date(),
            "index": nr.latest_index_date(),
            "floorsheet": nr.latest_floorsheet_date(),
        },
    })


@require_GET
def nepse_data_api(request, slug):
    """Rows + columns for one report as JSON."""
    if slug not in nr.REPORTS:
        return JsonResponse({"ok": False, "error": "Unknown report."}, status=404)
    try:
        out = nr.build(slug, _clean(request))
    except Exception:  # pragma: no cover - defensive
        logger.exception("NEPSE report failed: %s", slug)
        return JsonResponse(
            {"ok": False, "error": "Unable to build this report right now."}, status=500
        )
    out["ok"] = True
    out["slug"] = slug
    return JsonResponse(out)


@require_GET
def nepse_data_symbols_api(request):
    """Symbols for the report pickers, newest session first."""
    from core_analysis.services import broker_analytics as ba

    try:
        meta = ba.meta_cached()
        return JsonResponse({"ok": True, "symbols": meta.get("symbols", [])})
    except Exception:  # pragma: no cover - defensive
        logger.exception("Symbol list failed")
        return JsonResponse({"ok": True, "symbols": []})
