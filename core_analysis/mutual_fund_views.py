"""mutual_fund_views.py — JSON API for the mutual fund desk.

Read endpoints are open (they serve the same public figures the fund managers
publish); the import endpoint is staff-only and POST-only, because it writes.

Every read takes ``?month=`` in any Nepali spelling — "Ashad 2083", "Asadh 2083"
and "ashadh-2083" all resolve to the same stored period. Omit it and the newest
imported month is used, so a bare call always returns something useful.
"""
from __future__ import annotations

import logging

from django.contrib.admin.views.decorators import staff_member_required
from django.contrib import messages
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_GET, require_POST

from core_analysis.services import mutual_fund_portfolio as mfp

logger = logging.getLogger(__name__)


@require_GET
def mf_dashboard(request):
    """The mutual fund desk shell.

    Three tabs, only one of which has data today: NAV & discount is populated
    from the scraped feed, while both allocation tabs stay empty until a monthly
    portfolio is imported. That is stated on the page rather than left to look
    like a bug.
    """
    from core_analysis.insights_views import _asset_version
    from core_analysis.services import mutual_fund_nav as mfn

    return render(request, "core_analysis/mutual_fund.html", {
        "asset_version": _asset_version(),
        "coverage": mfn.coverage(),
    })


@require_GET
def mf_nav_api(request):
    """Latest NAV, our close and the discount, for every fund."""
    from core_analysis.services import mutual_fund_nav as mfn

    import statistics

    rows = mfn.nav_table()
    priced = [r for r in rows if r["discount_pct"] is not None and not r["is_matured"]]
    median = None
    if priced:
        median = round(statistics.median(r["discount_pct"] for r in priced), 2)
    return JsonResponse({
        "ok": True, "funds": rows,
        "count": len(rows),
        "priced": len(priced),
        "median_discount_pct": median,
    }, status=200)


@staff_member_required
@require_POST
def mf_import_api(request):
    """Import one fund's monthly portfolio from an uploaded CSV / Excel / PDF.

    Form fields: ``file``, ``symbol``, ``month``; optional ``fund_name``,
    ``nav_monthly``, ``nav_daily``, ``ltp``, ``fixed_income``, ``cash``,
    ``net_assets``.

    ``net_assets`` is the fund's own reported net asset figure. Pass it to get
    the same percentages the published tables show — they divide by net assets,
    not by the sum of the buckets, which is why their rows add up to slightly
    over 100%. The response's ``basis`` says which denominator was used.

    The optional bucket overrides exist because a report often states its fixed
    income and cash totals in a summary block ABOVE the holdings table, where a
    table parser cannot reach them. Without them the allocation would show 100%
    equity for such a file — wrong, and wrong in a way that looks plausible.

    Reuses the hardened readers behind the portfolio importer (size caps, zip
    bomb guards, page limits) rather than opening uploads a second way.
    """
    from core_analysis.portfolio_views import (
        _table_from_excel, _table_from_pdf,
    )
    import csv
    import io

    upload = request.FILES.get("file")
    symbol = (request.POST.get("symbol") or "").strip()
    month = (request.POST.get("month") or "").strip()
    if not upload or not symbol or not month:
        return JsonResponse(
            {"ok": False, "error": "file, symbol and month are all required."},
            status=200,
        )

    name = (upload.name or "").lower()
    try:
        data = upload.read()
        if name.endswith(".csv") or name.endswith(".txt"):
            text = data.decode("utf-8-sig", errors="replace")
            table = list(csv.reader(io.StringIO(text)))
        elif name.endswith(".xlsx") or name.endswith(".xlsm"):
            table = _table_from_excel(data)
        elif name.endswith(".pdf"):
            table = _table_from_pdf(data)
        else:
            return JsonResponse(
                {"ok": False, "error": "Upload a .csv, .xlsx or .pdf file."},
                status=200,
            )
    except Exception as exc:
        logger.exception("Mutual fund portfolio import: could not read %s", upload.name)
        return JsonResponse(
            {"ok": False, "error": f"Could not read the file: {exc}"}, status=200)

    def _opt(field):
        raw = (request.POST.get(field) or "").strip()
        return mfp._num(raw) if raw else None

    try:
        result = mfp.import_month(
            symbol, month, table,
            fund_name=(request.POST.get("fund_name") or "").strip(),
            nav_monthly=_opt("nav_monthly"),
            nav_daily=_opt("nav_daily"),
            ltp=_opt("ltp"),
            fixed_income=_opt("fixed_income"),
            cash=_opt("cash"),
            net_assets=_opt("net_assets"),
            source_name=upload.name or "",
        )
    except ValueError as exc:
        # Bad symbol / unrecognised month / nothing parseable — the caller's
        # problem to fix, and specific enough to act on.
        return JsonResponse({"ok": False, "error": str(exc)}, status=200)
    except Exception as exc:
        logger.exception("Mutual fund portfolio import failed for %s", symbol)
        return JsonResponse({"ok": False, "error": f"Import failed: {exc}"}, status=200)

    return JsonResponse({"ok": True, **result}, status=200)


# ===========================================================================
# The six desk screens, backed by ``services/mutual_fund_desk``.
#
# These replace the upload-driven allocation views above: the internal
# 192.168.1.39 feed supplies per-script holdings and each fund's balance sheet,
# so nothing here depends on somebody uploading a monthly report. The old
# import endpoint stays as a fallback for a month the feed has not published.
# ===========================================================================

def _page(request, template, section, **extra):
    from core_analysis.insights_views import _asset_version
    from core_analysis.services import mutual_fund_desk as desk
    context = {"active_section": "mutualfund", "mf_screen": section,
               "overview": desk.overview(),
               "asset_version": _asset_version()}
    context.update(extra)
    return render(request, template, context)


@require_GET
def mf_home(request):
    """Landing screen: counters plus the links into the five reports."""
    return _page(request, "core_analysis/mf/home.html", "home")


@require_GET
def mf_list_page(request):
    return _page(request, "core_analysis/mf/list.html", "list")


@require_GET
def mf_assets_page(request):
    return _page(request, "core_analysis/mf/assets.html", "assets")


@require_GET
def mf_sector_page(request):
    return _page(request, "core_analysis/mf/sector.html", "sector")


@require_GET
def mf_holdings_page(request):
    from core_analysis.services import mutual_fund_desk as desk
    return _page(request, "core_analysis/mf/holdings.html", "holdings",
                 scripts=desk.held_scripts())


@require_GET
def mf_financials_page(request):
    from core_analysis.services import mutual_fund_desk as desk
    return _page(request, "core_analysis/mf/financials.html", "financials",
                 fund_symbols=desk.fund_symbols())


# ---------------------------------------------------------------- JSON

@require_GET
def mf_fund_list_api(request):
    from core_analysis.services import mutual_fund_desk as desk
    return JsonResponse(desk.fund_list())


@require_GET
def mf_assets_api(request):
    from core_analysis.services import mutual_fund_desk as desk
    return JsonResponse(desk.assets_allocation(request.GET.get("month")))


@require_GET
def mf_sector_api(request):
    from core_analysis.services import mutual_fund_desk as desk
    # "book" values the book at what the manager paid, "market" at what it is
    # worth now. Anything else falls back to market rather than erroring.
    basis = "book" if request.GET.get("basis") == "book" else "market"
    return JsonResponse(desk.sector_allocation(request.GET.get("month"), basis))


@require_GET
def mf_company_holdings_api(request):
    from core_analysis.services import mutual_fund_desk as desk
    try:
        months = max(1, min(24, int(request.GET.get("months", 9))))
    except (TypeError, ValueError):
        months = 9
    return JsonResponse(desk.company_holdings(request.GET.get("script"), months))


@require_GET
def mf_financials_api(request):
    from core_analysis.services import mutual_fund_desk as desk
    return JsonResponse(desk.fund_financials(
        request.GET.get("symbol"), request.GET.get("month"),
        request.GET.get("sector")), json_dumps_params={"default": str})


@staff_member_required
@require_POST
def mf_sync_api(request):
    """Pull the internal feed into the local tables.

    Staff-only and POST-only: it writes, and a full pull takes long enough that
    a stray GET from a link prefetcher would be a real cost.

    Answers in whichever form the caller asked for. The Raw Inventory Manager
    posts a plain form and expects to land back on the dashboard with a message,
    exactly like every other sync row; an XHR caller gets JSON. Returning JSON
    to the form left a staff user staring at a raw payload with no way back.

    NOTE ON DURATION: this blocks for roughly a minute and a half — the feed
    rate-limits and the pull honours its Retry-After. That is well inside a
    default gunicorn timeout but worth knowing before wiring it to anything
    with a shorter one.
    """
    from core_analysis.services import mutual_fund_api as api

    wants_json = (request.headers.get("X-Requested-With") == "XMLHttpRequest"
                  or "application/json" in request.headers.get("Accept", ""))
    try:
        stats = api.sync()
    except api.FeedError as exc:
        logger.warning("mutual fund holdings sync failed: %s", exc)
        if wants_json:
            return JsonResponse({"ok": False, "error": str(exc)}, status=502)
        messages.error(request, f"Mutual fund holdings sync failed: {exc}")
        return redirect("crud_dashboard")
    except Exception as exc:
        # A malformed feed row or a mid-sync DB error must not surface as a
        # bare 500 to the staff form — it has a redirect to land on.
        logger.exception("mutual fund holdings sync crashed")
        if wants_json:
            return JsonResponse({"ok": False, "error": f"Sync failed: {exc}"}, status=500)
        messages.error(request, f"Mutual fund holdings sync failed: {exc}")
        return redirect("crud_dashboard")

    if wants_json:
        return JsonResponse(stats)

    note = ""
    if stats.get("unmapped_items"):
        # A balance-sheet line we do not bucket is a silent hole in the asset
        # allocation, so it is surfaced rather than left in the log.
        note = (" Unrecognised balance-sheet line(s): "
                f"{', '.join(stats['unmapped_items'][:5])}.")
    messages.success(
        request,
        f"Mutual fund holdings synced — {stats['fund_months']} fund-month(s) and "
        f"{stats['holdings']} holding row(s) across {len(stats['periods'])} month(s) "
        f"for {stats['funds']} fund(s).{note}")
    return redirect("crud_dashboard")
