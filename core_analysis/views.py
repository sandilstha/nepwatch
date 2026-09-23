import re
import os
from datetime import date
from decimal import Decimal, InvalidOperation
from functools import wraps

import numpy as np
import pandas as pd
# Workbench / sync / CRUD views require a logged-in *staff* user.
# Anonymous visitors are sent to Django's admin login.
from django.contrib.admin.views.decorators import staff_member_required
from django.contrib import messages
from django.contrib.staticfiles import finders
from django.core.cache import cache
from django.db import IntegrityError
from django.db.models import Max, OuterRef, Q, Subquery
from django.http import HttpResponse, HttpResponseBadRequest, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.template.loader import render_to_string
from django.core.management import call_command
from django.views.decorators.http import require_GET, require_POST

from core_analysis.models import CompanyProfile, NepseDailyStockPrice, NepseMarketIndex, StockPriceAdjustment
from core_analysis.services.CCI import run_cci_long_only_simulation
from core_analysis.services.IMM import run_imm_scoring_system
from core_analysis.services.msv_strategy import run_msv_long_only_simulation
from core_analysis.services.moving_average import run_ema_50_200_long_only_simulation
from core_analysis.services.RSI_SMA import run_rsi_sma_long_only_simulation
from core_analysis.services.sop_strategy import (
    run_sop_simulation, run_sop_combined_simulation, market_regime_series,
    INDICATOR_DEFAULTS as _SOP_INDICATOR_DEFAULTS,
    INDICATOR_LABELS as _SOP_INDICATOR_LABELS,
)

# Single source of truth for which SOP indicators exist — read from the engine
# so the view, the dropdown and the confluence checkboxes can never drift out of
# sync with it when an indicator is added or retired.
_SOP_INDICATOR_KEYS = tuple(_SOP_INDICATOR_DEFAULTS)
from core_analysis.services.strategy_tester import run_t3ma_macd_ribbon_simulation
from core_analysis.services.stage_analysis import calculate_stage_analysis
from core_analysis.services.RGG_Chart import run_rrg_simulation
from core_analysis.services.RGG_indices import (
    NEPSE_INDEX_LABELS, RRG_EXCLUDED_INDICES, ordered_nepse_indices, run_rrg_indices_simulation,
)
from core_analysis.services.seasonal_returns import build_seasonal_payload, build_market_cycle
from core_analysis.services.market_regimes import build_market_regimes
from core_analysis.services import proposed_dividend
from core_analysis.services import mutual_fund_nav
from core_analysis.services.advanced_market_structure import run_advanced_market_structure_analysis
from core_analysis.services.support_resistance import (
    DEFAULT_LEVEL_FAMILIES,
    build_institutional_analysis_rows,
    run_support_resistance_analysis,
)
from core_analysis.services.gemini_analysis import generate_sr_ai_analysis
from core_analysis.services.new_listing import (
    build_new_listing_snapshot,
    FULL_HISTORY_BARS,
    SNAPSHOT_TRIGGER_BARS,
)

# Only companies with this status are offered in the search/dropdown — Delisted,
# Suspended and Inactive tickers are hidden so users can't pick a dead symbol.
# MySQL's default collation is case-insensitive, so this matches any casing.
ACTIVE_COMPANY_STATUS = "Active"

MARKET_INDEX_ALIASES = {
    "NEPSE": "NEPSE INDEX",
    "BANKING": "BANKING SUBINDEX",
    "BANKING INDEX": "BANKING SUBINDEX",
    "DEV BANK": "DEVELOPMENT BANK INDEX",
    "DEVELOPMENT BANK": "DEVELOPMENT BANK INDEX",
    "FINANCE": "FINANCE INDEX",
    "FLOAT": "FLOAT INDEX",
    "HOTELS": "HOTELS AND TOURISM INDEX",
    "HOTEL": "HOTELS AND TOURISM INDEX",
    "HOTELS AND TOURISM": "HOTELS AND TOURISM INDEX",
    "HYDROPOWER": "HYDROPOWER INDEX",
    "INVESTMENT": "INVESTMENT INDEX",
    "LIFE": "LIFE INSURANCE",
    "LIFE INS": "LIFE INSURANCE",
    "MFG": "MANUFACTURING AND PROCESSING",
    "MANUFACTURING": "MANUFACTURING AND PROCESSING",
    "MICROFINANCE": "MICROFINANCE INDEX",
    "MUTUAL FUND INDEX": "MUTUAL FUND",
    "NON LIFE": "NON LIFE INSURANCE",
    "NON-LIFE": "NON LIFE INSURANCE",
    "OTHERS": "OTHERS INDEX",
    "SENSITIVE": "SENSITIVE INDEX",
    "SENSITIVE FLOAT": "SENSITIVE FLOAT INDEX",
    "TRADING": "TRADING INDEX",
}

KNOWN_MARKET_INDEX_SYMBOLS = {
    "BANKING SUBINDEX", "DEVELOPMENT BANK INDEX", "FINANCE INDEX", "FLOAT INDEX",
    "HOTELS AND TOURISM INDEX", "HYDROPOWER INDEX", "INVESTMENT INDEX", "LIFE INSURANCE",
    "MANUFACTURING AND PROCESSING", "MICROFINANCE INDEX", "MUTUAL FUND", "NEPSE INDEX",
    "NON LIFE INSURANCE", "OTHERS INDEX", "SENSITIVE FLOAT INDEX", "SENSITIVE INDEX",
    "TRADING INDEX",
}


@staff_member_required
@require_POST
def trigger_sync_and_calculate(request):
    from_date_raw = (request.POST.get("from_date") or "").strip()
    to_date_raw = (request.POST.get("to_date") or "").strip()
    try:
        from_date = date.fromisoformat(from_date_raw) if from_date_raw else None
        to_date = date.fromisoformat(to_date_raw) if to_date_raw else None
    except ValueError:
        messages.error(request, "Invalid sync date format. Use YYYY-MM-DD.")
        return redirect("crud_dashboard")
    if from_date and to_date and from_date > to_date:
        messages.error(request, "Sync start date cannot be after the end date.")
        return redirect("crud_dashboard")
    try:
        # Always include the company-profile refresh ("both" runs _sync_companies
        # then adjusted prices). Previously this fell back to "adjustments" once
        # any CompanyProfile existed, which permanently froze the company list:
        # newly listed (IPO) companies got daily prices via Sync Price but never a
        # CompanyProfile row, so they never appeared in the dropdown. The company
        # sync is an idempotent upsert, so re-running it each time is safe.
        kwargs = {
            "source": "both",
        }
        if from_date:
            kwargs["from_date"] = from_date
        if to_date:
            kwargs["to_date"] = to_date
        call_command("sync_and_calculate", **kwargs)
        cache.delete("nepse_symbol_lists")
        scope = f" ({from_date_raw or 'start'} to {to_date_raw or 'latest'})" if (from_date or to_date) else ""
        messages.success(request, f"Market Data and Signals sync_and_calculate successfully generated{scope}!")
    except Exception as e:
        messages.error(request, f"Command execution failed: {str(e)}")
    return redirect("crud_dashboard")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _dashboard_asset_version():
    """Cache-bust token for dashboard assets so changed UI files load fresh."""
    latest = 0
    for rel in (
        "core_analysis/css/dashboard.css",
        "core_analysis/css/workbench-layout.css",
        "core_analysis/js/dashboard.js",
        "core_analysis/js/workbench-ajax.js",
        "core_analysis/js/fundamentals-panel.js",
    ):
        path = finders.find(rel)
        try:
            if path:
                latest = max(latest, int(os.path.getmtime(path)))
        except OSError:
            pass
    return latest or 1


@require_GET
def stage_sop_view(request):
    """Methodology SOP for the Stage Analysis desk (Technical Analysis tab).

    Static content — every stage rule, parameter and formula is documented in
    the template itself, so it never touches the analytics engine. The desk's
    (?) help icon deep-links here.
    """
    return render(
        request,
        "core_analysis/stage_sop.html",
        {"asset_version": _dashboard_asset_version()},
    )


def seasonal_sop_view(request):
    """Methodology SOP for the Seasonal Return desk's Accumulation / Distribution
    strategy (Workbench → Seasonal Return tab).

    Static content — every window-selection rule, formula and assumption is
    documented in the template itself, so it never touches the analytics engine.
    The strategy card's help link deep-links here.
    """
    return render(
        request,
        "core_analysis/seasonal_sop.html",
        {"asset_version": _dashboard_asset_version()},
    )


def market_cycle_sop_view(request):
    """SOP for classifying NEPSE's macro market cycle stage (Weinstein 4-stage
    model, whole-index) — a manual weekly procedure linked beside the Seasonal
    Return desk. Distinct from stage_sop_view, which classifies a single symbol.

    Static content — the whole procedure, scoring and checklists live in the
    template, so it never touches the analytics engine.
    """
    return render(
        request,
        "core_analysis/market_cycle_sop.html",
        {"asset_version": _dashboard_asset_version()},
    )


def _safe_float(value, default):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_int(value, default, minimum=None):
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    if minimum is not None and parsed < minimum:
        return default
    return parsed


def _normalized_symbol_text(symbol):
    normalized = " ".join(str(symbol or "").strip().upper().split())
    if normalized.startswith("NEPSE:"):
        normalized = normalized.split(":", 1)[1].strip()
    return normalized


def _canonical_market_index_symbol(symbol):
    normalized = _normalized_symbol_text(symbol)
    return MARKET_INDEX_ALIASES.get(normalized, normalized)


def _resolve_market_index_symbol(symbol):
    normalized = _normalized_symbol_text(symbol)
    candidate = _canonical_market_index_symbol(symbol)
    if not candidate:
        return None
    if candidate != normalized and normalized:
        company_cache_key = f"active_company_symbol:v1:{normalized}"
        company_exists = cache.get(company_cache_key)
        if company_exists is None:
            company_exists = CompanyProfile.objects.filter(
                symbol__iexact=normalized,
                status=ACTIVE_COMPANY_STATUS,
            ).exists()
            cache.set(company_cache_key, company_exists, timeout=300)
        if company_exists:
            return None
    if candidate in KNOWN_MARKET_INDEX_SYMBOLS:
        return candidate

    cache_key = f"market_index_symbol:v1:{candidate}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached or None

    resolved = (
        NepseMarketIndex.objects
        .filter(sector_name__iexact=candidate)
        .values_list("sector_name", flat=True)
        .order_by("sector_name")
        .first()
    )
    resolved = str(resolved).strip().upper() if resolved else ""
    cache.set(cache_key, resolved, timeout=300)
    return resolved or None


def _get_symbol_lists():
    """
    FIX #1 — Cache symbol lists for 5 minutes so the two full-table scans
    (CompanyProfile + NepseMarketIndex) do NOT run on every page load.
    Cache is invalidated automatically on timeout; call cache.delete() in any
    view that adds/removes symbols if you need instant consistency.
    """
    CACHE_KEY = "nepse_symbol_lists"
    cached = cache.get(CACHE_KEY)
    if cached:
        return cached["companies"], cached["indices"]

    db_companies = sorted(
        CompanyProfile.objects.filter(status=ACTIVE_COMPANY_STATUS)
        .values_list("symbol", flat=True)
        .distinct()
    )
    db_indices = sorted(
        NepseMarketIndex.objects.values_list("sector_name", flat=True).distinct()
    )
    cache.set(CACHE_KEY, {"companies": db_companies, "indices": db_indices}, timeout=300)
    return db_companies, db_indices


def _build_standard_dataframe(symbol, start_date, end_date, use_unadjusted_fallback=False):
    """
    OPTIMIZED: Normalizes data from StockPriceAdjustment and NepseMarketIndex
    into an identical DataFrame with strict date filtering applied at the database level.
    Returns ONLY the data within the user-specified date range.

    When use_unadjusted_fallback=True for company symbols, missing adjusted-price
    dates are filled from NepseDailyStockPrice. Existing adjusted rows are kept.
    """
    symbol = (symbol or "").strip()
    symbol_upper = symbol.upper()

    # Accept "YYYY/MM/DD" (some datepickers emit slashes) as well as ISO
    # "YYYY-MM-DD". The ORM date fields only validate the ISO form, so
    # normalize slashes here before any query is built.
    if isinstance(start_date, str):
        start_date = start_date.strip().replace("/", "-")
    if isinstance(end_date, str):
        end_date = end_date.strip().replace("/", "-")

    empty_schema = pd.DataFrame(columns=[
        "business_date", "open_price_adj", "high_price_adj", "low_price_adj",
        "close_price_adj", "volume", "price_source",
    ])

    index_symbol = _resolve_market_index_symbol(symbol_upper)
    if index_symbol:
        # OPTIMIZED: Only select required columns and apply date filter at DB level
        qs = NepseMarketIndex.objects.filter(
            sector_name__iexact=index_symbol,
            business_date__gte=start_date,
            business_date__lte=end_date,
        ).values(
            "business_date", "open_index", "high_index", "low_index", "close_index", "turnover_volume"
        ).order_by("business_date")
        
        data = list(qs)
        if not data:
            return empty_schema
        df = pd.DataFrame(data)
        df.rename(columns={
            "open_index":  "open_price_adj",
            "high_index":  "high_price_adj",
            "low_index":   "low_price_adj",
            "close_index": "close_price_adj",
            "turnover_volume": "volume",
        }, inplace=True)
        df["price_source"] = "Market index"
    else:
        # OPTIMIZED: Only select required columns and apply date filter at DB level
        qs = StockPriceAdjustment.objects.filter(
            company_id__exact=symbol_upper,
            business_date__gte=start_date,
            business_date__lte=end_date,
        ).values(
            "business_date", "open_price_adj", "high_price_adj",
            "low_price_adj", "close_price_adj",
        ).order_by("business_date")
        
        data = list(qs)
        adjusted_df = pd.DataFrame(data)
        if not adjusted_df.empty:
            adjusted_df["price_source"] = "Adjusted"
        if use_unadjusted_fallback:
            unadjusted_qs = NepseDailyStockPrice.objects.filter(
                symbol__exact=symbol_upper,
                business_date__gte=start_date,
                business_date__lte=end_date,
            ).values(
                "business_date", "open_price", "high_price", "low_price", "close_price",
            ).order_by("business_date")
            unadjusted_df = pd.DataFrame(list(unadjusted_qs))
            if not unadjusted_df.empty:
                unadjusted_df.rename(columns={
                    "open_price": "open_price_adj",
                    "high_price": "high_price_adj",
                    "low_price": "low_price_adj",
                    "close_price": "close_price_adj",
                }, inplace=True)
                unadjusted_df["price_source"] = "Unadjusted close"
                if not adjusted_df.empty:
                    adjusted_df["_source_rank"] = 0
                    unadjusted_df["_source_rank"] = 1
                    df = pd.concat([adjusted_df, unadjusted_df], ignore_index=True)
                    df = (
                        df.sort_values(["business_date", "_source_rank"])
                        .drop_duplicates(subset=["business_date"], keep="first")
                        .drop(columns=["_source_rank"])
                    )
                else:
                    df = unadjusted_df
            else:
                df = adjusted_df
        else:
            df = adjusted_df
        if df.empty:
            return empty_schema

        volume_qs = NepseDailyStockPrice.objects.filter(
            symbol__exact=symbol_upper,
            business_date__gte=start_date,
            business_date__lte=end_date,
        ).values("business_date", "total_traded_quantity")
        volume_df = pd.DataFrame(list(volume_qs))
        if not volume_df.empty:
            volume_df.rename(columns={"total_traded_quantity": "volume"}, inplace=True)
            df = df.merge(volume_df, on="business_date", how="left")
        else:
            df["volume"] = np.nan

    # Convert data types
    df["business_date"] = pd.to_datetime(df["business_date"])
    for col in ["open_price_adj", "high_price_adj", "low_price_adj", "close_price_adj"]:
        df[col] = df[col].astype(float)
    if "volume" in df.columns:
        df["volume"] = pd.to_numeric(df["volume"], errors="coerce")
    
    # CRITICAL: Ensure dataframe only contains data within the specified date range
    # This prevents simulation functions from processing extra data
    df = df[
        (df["business_date"] >= pd.to_datetime(start_date)) & 
        (df["business_date"] <= pd.to_datetime(end_date))
    ].reset_index(drop=True)
    
    return df


def _build_index_dataframes(start_date, end_date, symbols=None):
    """Per-index OHLC frames for the RRG index desk.

    ``symbols`` (optional) limits the query to the indices actually being
    plotted; the RRG-excluded composites (SENSITIVE/FLOAT) are never loaded.
    """
    qs = NepseMarketIndex.objects.filter(
        business_date__gte=start_date,
        business_date__lte=end_date,
    ).exclude(sector_name__in=RRG_EXCLUDED_INDICES)
    if symbols:
        qs = qs.filter(sector_name__in=list(symbols))
    rows = list(
        qs.values(
            "sector_name", "business_date", "open_index", "high_index",
            "low_index", "close_index", "turnover_volume",
        ).order_by("sector_name", "business_date")
    )
    if not rows:
        return {}

    df = pd.DataFrame(rows)
    df["sector_name"] = df["sector_name"].astype(str).str.upper().str.strip()
    df.rename(columns={
        "open_index": "open_price_adj",
        "high_index": "high_price_adj",
        "low_index": "low_price_adj",
        "close_index": "close_price_adj",
        "turnover_volume": "volume",
    }, inplace=True)
    df["business_date"] = pd.to_datetime(df["business_date"])
    for col in ["open_price_adj", "high_price_adj", "low_price_adj", "close_price_adj"]:
        df[col] = df[col].astype(float)
    df["volume"] = pd.to_numeric(df["volume"], errors="coerce")

    frames = {}
    for sector_name, sector_df in df.groupby("sector_name", sort=False):
        frames[sector_name] = sector_df[[
            "business_date", "open_price_adj", "high_price_adj",
            "low_price_adj", "close_price_adj", "volume",
        ]].reset_index(drop=True)
    return frames


def _build_benchmark_sparkline(df, limit=45):
    if df.empty:
        return []
    sparkline_df = df.tail(limit).copy()
    sparkline_df["business_date"] = pd.to_datetime(sparkline_df["business_date"]).dt.strftime("%Y-%m-%d")
    return [
        {
            "business_date": row["business_date"],
            "close": round(float(row["close_price_adj"]), 2),
        }
        for _, row in sparkline_df.iterrows()
    ]


def _build_rrg_index_choices(benchmark_symbol="NEPSE INDEX"):
    # DISTINCT over the whole index table on every workbench request is
    # needless; the set of index names changes about never.
    index_names = cache.get("rrg_index_names:v1")
    if index_names is None:
        index_names = list(
            NepseMarketIndex.objects
            .values_list("sector_name", flat=True)
            .distinct()
            .order_by("sector_name")
        )
        cache.set("rrg_index_names:v1", index_names, timeout=600)
    ordered_names = ordered_nepse_indices(index_names, benchmark_symbol)
    return [
        {
            "value": name,
            "label": NEPSE_INDEX_LABELS.get(name, name.replace(" INDEX", "")),
        }
        for name in ordered_names
    ]


# ── Symbol autocomplete API (used by the JS search boxes) ────────────────────

# Public: read-only ticker/index name lookup. The public analysis desks
# (PUBLIC_WORKBENCH_TABS) need this for their search boxes, and it exposes only
# names already shown on the public Market Insights / chart pages.
@require_GET
def symbol_autocomplete_view(request):
    """
    OPTIMIZED: Lightweight JSON endpoint for the search-as-you-type dropdown.
    The template no longer renders <option> for every symbol; instead JS calls
    this endpoint and builds the dropdown on demand, so the initial HTML
    response is drastically smaller.

    Usage: GET /dashboard/symbols/?q=nabil
    Returns: {"results": [{"value": "NABIL", "label": "NABIL - Nabil Bank", "type": "company"}, ...]}
    """
    q_raw = request.GET.get("q", "").strip()
    q = q_raw.upper()
    fast_mode = request.GET.get("fast") == "1"
    indices_only = request.GET.get("indices_only") == "1"
    show_all_indices = indices_only and request.GET.get("all") == "1"
    # Avoid overly broad scans for 0/1-character queries.
    if len(q_raw) < 2 and not show_all_indices:
        return JsonResponse({"results": []})

    cache_mode = "indices" if indices_only else ("fast" if fast_mode else "full")
    cache_key = f"symbol_autocomplete:v4:{cache_mode}:{'all' if show_all_indices else 'query'}:{q_raw.lower()}"
    cached_results = cache.get(cache_key)
    if cached_results is not None:
        return JsonResponse({"results": cached_results})

    # Bug fix 5: the two subqueries were identical — .values() must be applied
    # *inside* Subquery so each one selects a different field.
    # Match by ticker prefix and company name prefix for focused results.
    # Fast mode intentionally avoids contains scans and latest-price subqueries.
    if indices_only:
        index_qs = NepseMarketIndex.objects.all()
        if q:
            index_qs = index_qs.filter(sector_name__icontains=q)
        index_rows = list(
            index_qs
            .values_list("sector_name", flat=True)
            .distinct()
            .order_by("sector_name")[:50]
        )
        if q:
            index_rows = sorted(
                index_rows,
                key=lambda value: (
                    not str(value).upper().startswith(q),
                    str(value).upper(),
                ),
            )
        results = []
        seen = set()
        for index_name in index_rows:
            index_value = (index_name or "").strip().upper()
            if not index_value or index_value in seen:
                continue
            seen.add(index_value)
            results.append({"value": index_value, "label": index_value, "type": "index"})
        cache.set(cache_key, results, timeout=300)
        return JsonResponse({"results": results})

    name_filter = Q(security_name__istartswith=q_raw)
    if not fast_mode and len(q_raw) >= 3:
        name_filter = name_filter | Q(security_name__icontains=q_raw)

    company_qs = CompanyProfile.objects.filter(
        Q(symbol__istartswith=q_raw) | name_filter
    ).filter(status=ACTIVE_COMPANY_STATUS)
    if fast_mode:
        company_rows = list(
            company_qs
            .values("symbol", "security_name")
            .order_by("symbol")[:20]
        )
    else:
        # Read the latest close/date from the raw daily-price table (kept current
        # by the daily-prices sync) rather than the adjusted-price table (synced
        # separately and often a few days behind), so the dropdown shows today's date.
        latest_stock_price_sq = (
            NepseDailyStockPrice.objects
            .filter(symbol=OuterRef("symbol"))
            .order_by("-business_date")
            .values("close_price")[:1]
        )
        latest_stock_date_sq = (
            NepseDailyStockPrice.objects
            .filter(symbol=OuterRef("symbol"))
            .order_by("-business_date")
            .values("business_date")[:1]
        )
        company_rows = list(
            company_qs
            .annotate(
                latest_close=Subquery(latest_stock_price_sq),
                latest_date=Subquery(latest_stock_date_sq),
            )
            .values("symbol", "security_name", "latest_close", "latest_date")
            .order_by("symbol")[:25]
        )
    index_rows = list(
        NepseMarketIndex.objects.filter(sector_name__icontains=q)
        .values_list("sector_name", flat=True)
        .distinct()
        .order_by("sector_name")[:10]
    )

    # Margin eligibility for every company row (one cached map, O(1) lookups).
    from core_analysis.services.margin import eligible_margin_map
    margin_map = eligible_margin_map()

    results = []
    seen = set()

    for row in company_rows:
        symbol = (row.get("symbol") or "").strip().upper()
        name = (row.get("security_name") or "").strip()
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        label = f"{symbol} - {name}" if name else symbol
        latest_close = row.get("latest_close")
        latest_date = row.get("latest_date")
        m = margin_map.get(symbol)
        results.append({
            "value": symbol,
            "label": label,
            "type": "company",
            "margin": {"eligible": True, "rate": m["rate"], "category": m["category"]}
                       if m else {"eligible": False},
            "latest_close": float(latest_close) if latest_close is not None else None,
            "latest_date": latest_date.isoformat() if latest_date else None,
        })

    for index_name in index_rows:
        index_value = (index_name or "").strip().upper()
        if not index_value or index_value in seen:
            continue
        seen.add(index_value)
        results.append({"value": index_value, "label": index_name, "type": "index"})

    cache.set(cache_key, results, timeout=300)
    return JsonResponse({"results": results})


# ── Main dashboard ────────────────────────────────────────────────────────────

# Workbench tabs open to anonymous visitors.
#
# The primary nav links "Technical Analysis" and "RRG Analytics" straight at
# workbench tabs. With this set empty they fell through to staff_member_required
# and dumped visitors on the DJANGO ADMIN login — a page that is not part of
# this site, looks nothing like it, and offers no sign-up. These two desks are
# read-only computations over public market data, so they are public.
#
# SAFETY: crud_dashboard.html renders EVERY pane, and build_dashboard_context
# always loads the 100 newest StockPriceAdjustment rows regardless of tab. The
# Raw Inventory Manager pane (sync triggers, CRUD forms, delete buttons) is
# therefore gated on user.is_staff in the template. Opening a tab here WITHOUT
# that gate would publish the whole data-admin surface. The mutating views are
# independently @staff_member_required + @require_POST, so this is defence in
# depth, not the only lock.
#
# Other computable desks that could be added: backtest, ema_backtest,
# cci_backtest, rsi_backtest, msv_backtest, imm_backtest, support_resistance,
# rrg_indices — see TAB_RESULTS_PARTIALS.
# Every read-only computation desk in TAB_RESULTS_PARTIALS. These run public
# market data through an indicator and render a result table — nothing to edit,
# nothing to sync, no per-user data. Keeping them behind the staff gate meant a
# visitor clicking "Strategy Simulator" or "Technical Analysis" in the primary
# nav landed on the Django ADMIN login.
#
# DELIBERATELY NOT PUBLIC:
#   inventory, rrg_seasonal  - the Workbench/Data pane (sync triggers, CRUD
#                              forms, delete buttons, raw adjustment rows)
#   bare /workbench/         - defaults to `inventory`, so it stays staff-only
PUBLIC_WORKBENCH_TABS = {
    "backtest", "ema_backtest", "cci_backtest", "sop_backtest", "sop_combined",
    "rsi_backtest", "msv_backtest", "imm_backtest", "stage_backtest",
    "support_resistance", "rrg_backtest", "rrg_indices",
}

# Seasonal Return lives inside the (staff-only) Workbench/Data area next to the Raw
# Inventory Manager, so its payload is built for either of those tabs — the two
# share a sub-tab bar and switching between them is client-side.
_WORKBENCH_DATA_TABS = {"inventory", "rrg_seasonal"}

# The company RRG desk and the index RRG desk share one tab (stacked, but fully
# independent). Either key means "the RRG tab", so both desks are built.
RRG_TABS = {"rrg_backtest", "rrg_indices"}


def _staff_or_public_tab(view):
    """Open the public desks to everyone; defer to the staff gate otherwise.

    The decision is per-request, keyed on ?active_tab=: a request for one of the
    PUBLIC_WORKBENCH_TABS is served to anyone, anything else (including a bare
    request with no tab) goes through ``staff_member_required`` unchanged.
    """
    @wraps(view)
    def _wrapped(request, *args, **kwargs):
        if request.GET.get("active_tab", "") in PUBLIC_WORKBENCH_TABS:
            return view(request, *args, **kwargs)
        return staff_member_required(view)(request, *args, **kwargs)

    return _wrapped


@_staff_or_public_tab
def crud_dashboard_view(request):
    """Render the analytics workbench shell (full page).

    Every heavy per-tab computation lives in build_dashboard_context() so the
    exact same logic backs both this full-page render and the AJAX calc
    endpoint (dashboard_tab_calc). The page therefore no longer needs a full
    reload to refresh a single tab's results — the form posts to the calc
    endpoint and only that tab's results partial is swapped in.
    """
    return render(request, "core_analysis/crud_dashboard.html", build_dashboard_context(request))


def build_dashboard_context(request):
    """
    OPTIMIZED: Consolidated Dashboard Controller with performance improvements.

    Performance improvements:
      1. Symbol lists are cached for 5 min (no full-table scan on every GET).
      2. Inventory queryset is only fetched when active_tab == 'inventory'.
      3. Only the active strategy tab runs its backtest computation.
      4. Date filtering is strictly enforced at the database and dataframe levels.
      5. Empty symbol lists are now passed to template (autocomplete handles search).
    """
    g = request.GET
    active_tab = g.get("active_tab", "inventory")

    # OPTIMIZED: Pass empty lists - autocomplete will handle search
    db_companies, db_indices = [], []

    # Inventory rows are always loaded (100 newest) so the Raw Inventory Manager
    # tab is never blank — including when it's reached via a client-side tab
    # switch rather than a fresh page load. Capped at 100 rows, so the cost is
    # negligible even on the strategy-tab AJAX recalcs that also hit this path.
    # Only the staff-only Inventory tab renders these rows; skip the query for
    # every other tab (and every anonymous request).
    queryset = []
    if request.user.is_staff and active_tab in _WORKBENCH_DATA_TABS:
        queryset = list(
            StockPriceAdjustment.objects
            .select_related("company")
            .order_by("-business_date")[:100]
        )

    # Initialise result vectors
    backtest_metrics = ema_backtest_metrics = cci_backtest_metrics = rsi_backtest_metrics = msv_backtest_metrics = imm_backtest_metrics = stage_backtest_metrics = rrg_backtest_metrics = None
    support_resistance_metrics = None
    advanced_market_structure_metrics = None
    institutional_analysis_rows = []
    sop_backtest_metrics = None
    sop_equity = []
    sopc_backtest_metrics = None
    sopc_equity = []
    sopc_completed_trades = []
    completed_trades = ema_completed_trades = cci_completed_trades = rsi_completed_trades = msv_completed_trades = sop_completed_trades = []
    msv_indicator_rows = []
    imm_indicator_rows = []
    imm_event_rows = []
    stage_indicator_rows = []
    # New-listing snapshots: populated when a symbol has too little history for
    # the desk's indicators, so we show listing-appropriate stats instead.
    msv_new_listing = imm_new_listing = stage_new_listing = rrg_new_listing = None
    support_resistance_rows = []
    rrg_indicator_rows = []
    rrg_chart_points = []
    rrg_indices_metrics = None
    rrg_indices_points = []
    rrg_indices_trails = []
    rrg_indices_skipped = []
    rrg_indices_benchmark_points = []
    seasonal = None
    market_cycle = None
    market_regimes = None
    imm_data_upload_date = None

    # Per-tab symbols
    selected_symbol     = g.get("backtest_symbol",     "").upper().strip()
    ema_selected_symbol = g.get("ema_backtest_symbol", "").upper().strip()
    cci_selected_symbol = g.get("cci_backtest_symbol", "").upper().strip()
    sop_selected_symbol = g.get("sop_backtest_symbol", "").upper().strip()
    sopc_selected_symbol = g.get("sopc_backtest_symbol", "").upper().strip()
    rsi_selected_symbol = g.get("rsi_backtest_symbol", "").upper().strip()
    msv_selected_symbol = g.get("msv_backtest_symbol", "").upper().strip()
    imm_selected_symbol = g.get("imm_backtest_symbol", "").upper().strip()
    stage_selected_symbol = g.get("stage_backtest_symbol", "").upper().strip()
    support_resistance_selected_symbol = g.get("support_resistance_symbol", "").upper().strip()
    rrg_selected_symbol = g.get("rrg_backtest_symbol", "").upper().strip()
    rrg_benchmark_symbol = g.get("rrg_benchmark_symbol", "NEPSE INDEX").upper().strip()
    rrg_indices_benchmark_symbol = g.get("rrg_indices_benchmark_symbol", "NEPSE INDEX").upper().strip()
    rrg_indices_choices = (
        _build_rrg_index_choices(rrg_indices_benchmark_symbol) if active_tab in RRG_TABS else []
    )
    # The AJAX calc endpoint renders ONE desk's partial; computing the other
    # desk too (a full all-indices load per company recalc) is pure waste.
    rrg_calc_only = getattr(request, "rrg_calc_only", None)
    rrg_run_company = active_tab in RRG_TABS and rrg_calc_only in (None, "rrg_backtest")
    rrg_run_indices = active_tab in RRG_TABS and rrg_calc_only in (None, "rrg_indices")
    rrg_lookback = _safe_int(g.get("rrg_lookback", 14), 14, minimum=2)
    rrg_indices_lookback = _safe_int(g.get("rrg_indices_lookback", 14), 14, minimum=2)
    rrg_indices_tail_length = _safe_int(g.get("rrg_indices_tail_length", 30), 30, minimum=1)

    if active_tab == "imm_backtest":
        # Two full-table MAX() scans just for a "data as of" label — cache them.
        imm_data_upload_date = cache.get("imm_data_upload_date:v1")
        if imm_data_upload_date is None:
            latest_adjusted_date = StockPriceAdjustment.objects.aggregate(max_date=Max("business_date"))["max_date"]
            latest_raw_date = NepseDailyStockPrice.objects.aggregate(max_date=Max("business_date"))["max_date"]
            imm_data_upload_date = max(
                [row_date for row_date in (latest_adjusted_date, latest_raw_date) if row_date],
                default=None,
            )
            cache.set("imm_data_upload_date:v1", imm_data_upload_date, timeout=300)
    rrg_indices_choice_values = [choice["value"] for choice in rrg_indices_choices]
    rrg_indices_selection_submitted = g.get("rrg_indices_selection_submitted") == "1"
    rrg_indices_selected_symbols = [
        symbol.strip().upper()
        for symbol in g.getlist("rrg_indices_selected_symbols")
        if symbol.strip()
    ]
    if not rrg_indices_selection_submitted:
        rrg_indices_selected_symbols = rrg_indices_choice_values[:]
    rrg_indices_selected_set = set(rrg_indices_selected_symbols)
    for choice in rrg_indices_choices:
        choice["selected"] = choice["value"] in rrg_indices_selected_set

    # Per-tab independent date ranges
    t3_from  = g.get("t3_from_date",  g.get("from_date", "")).strip()
    t3_to    = g.get("t3_to_date",    g.get("to_date",   "")).strip()
    ema_from = g.get("ema_from_date", g.get("from_date", "")).strip()
    ema_to   = g.get("ema_to_date",   g.get("to_date",   "")).strip()
    cci_from = g.get("cci_from_date", g.get("from_date", "")).strip()
    cci_to   = g.get("cci_to_date",   g.get("to_date",   "")).strip()
    sop_from = g.get("sop_from_date", g.get("from_date", "")).strip()
    sop_to   = g.get("sop_to_date",   g.get("to_date",   "")).strip()
    # Default is the best-performing indicator; a stale value from an old
    # bookmark (e.g. the removed "rsi"/"bollinger") falls back instead of
    # raising "Unknown indicator" from the engine.
    sop_indicator = g.get("sop_indicator", "supertrend").lower().strip()
    if sop_indicator not in _SOP_INDICATOR_KEYS:
        sop_indicator = "supertrend"
    # Checkbox: when the SOP form is the submission, absence = unchecked = off.
    # Otherwise (initial load) default the filter ON (it is rendered checked).
    if active_tab == "sop_backtest":
        sop_use_regime = "sop_use_regime" in g
    else:
        sop_use_regime = True

    # SOP Combined (confluence) tab params.
    _ALL_SOP_INDICATORS = list(_SOP_INDICATOR_KEYS)
    sopc_from = g.get("sopc_from_date", g.get("from_date", "")).strip().replace("/", "-")
    sopc_to   = g.get("sopc_to_date",   g.get("to_date",   "")).strip().replace("/", "-")
    sopc_indicators = [i for i in g.getlist("sopc_indicator") if i in _ALL_SOP_INDICATORS]
    if not sopc_indicators:
        sopc_indicators = list(_ALL_SOP_INDICATORS)
    # Confluence floor is 3: a single indicator firing is noise, and the
    # 2003-2026 index study showed min_agree=3 is the best-performing threshold
    # (Sharpe 1.30 vs 1.19 at 4). Never let the UI request fewer than 3.
    sopc_min_agree = _safe_int(g.get("sopc_min_agree", 3), 3, minimum=3)
    # Bear-regime behaviour: strict (default) / swing / off. Anything unrecognised
    # falls back to strict, which is the best-tested configuration.
    sopc_regime_mode = (g.get("sopc_regime_mode", "strict") or "strict").lower().strip()
    if sopc_regime_mode not in ("strict", "swing", "off"):
        sopc_regime_mode = "strict"
    sopc_bear_min_agree = _safe_int(g.get("sopc_bear_min_agree", sopc_min_agree + 1),
                                    sopc_min_agree + 1, minimum=1)
    if active_tab == "sop_combined":
        sopc_use_regime = "sopc_use_regime" in g
    else:
        sopc_use_regime = True
    rsi_from = g.get("rsi_from_date", g.get("from_date", "")).strip()
    rsi_to   = g.get("rsi_to_date",   g.get("to_date",   "")).strip()
    msv_from = g.get("msv_from_date", g.get("from_date", "")).strip()
    msv_to   = g.get("msv_to_date",   g.get("to_date",   "")).strip()
    imm_from = g.get("imm_from_date", g.get("from_date", "")).strip()
    imm_to   = g.get("imm_to_date",   g.get("to_date",   "")).strip()
    stage_from = g.get("stage_from_date", g.get("from_date", "")).strip()
    stage_to   = g.get("stage_to_date",   g.get("to_date",   "")).strip()
    support_resistance_from = g.get("support_resistance_from_date", g.get("from_date", "")).strip()
    support_resistance_to = g.get("support_resistance_to_date", g.get("to_date", "")).strip()
    rrg_from = g.get("rrg_from_date", g.get("from_date", "")).strip()
    rrg_to   = g.get("rrg_to_date",   g.get("to_date",   "")).strip()
    rrg_indices_from = g.get("rrg_indices_from_date", g.get("from_date", "")).strip()
    rrg_indices_to   = g.get("rrg_indices_to_date",   g.get("to_date",   "")).strip()
    stage_volume_multiplier = _safe_float(g.get("stage_volume_multiplier", 1.5), 1.5)
    stage_resistance_lookback = _safe_int(g.get("stage_resistance_lookback", 20), 20, minimum=2)
    stage_volume_lookback = _safe_int(g.get("stage_volume_lookback", 20), 20, minimum=2)
    stage_momentum_period = _safe_int(g.get("stage_momentum_period", 60), 60, minimum=2)
    stage_rsi_length = _safe_int(g.get("stage_rsi_length", 14), 14, minimum=2)
    stage_rsi_threshold = _safe_float(g.get("stage_rsi_threshold", 55.0), 55.0)
    stage_adx_length = _safe_int(g.get("stage_adx_length", 14), 14, minimum=2)
    stage_adx_threshold = _safe_float(g.get("stage_adx_threshold", 20.0), 20.0)
    stage_use_rsi = (g.get("stage_use_rsi", "").strip().lower() in {"1", "true", "yes", "on"})
    stage_use_adx = (g.get("stage_use_adx", "").strip().lower() in {"1", "true", "yes", "on"})
    support_resistance_std_period = _safe_int(g.get("support_resistance_std_period", 20), 20, minimum=2)
    support_resistance_rsi_length = _safe_int(g.get("support_resistance_rsi_length", 14), 14, minimum=2)
    support_resistance_stochastic_length = _safe_int(g.get("support_resistance_stochastic_length", 14), 14, minimum=2)
    support_resistance_fractal_window = _safe_int(g.get("support_resistance_fractal_window", 5), 5, minimum=3)
    if support_resistance_fractal_window % 2 == 0:
        support_resistance_fractal_window += 1
    support_resistance_family_options = [
        {"value": "pivots", "label": "Pivots"},
        {"value": "stochastics", "label": "Stochastics"},
        {"value": "fibonacci", "label": "Fibonacci"},
        {"value": "moving_averages", "label": "Moving Averages"},
        {"value": "highs_lows", "label": "Highs/Lows"},
        {"value": "rsi", "label": "RSI"},
        {"value": "hlc", "label": "HLC"},
        {"value": "standard_deviation", "label": "Standard Deviation"},
    ]
    support_resistance_filters_submitted = g.get("support_resistance_filters_submitted") == "1"
    support_resistance_enabled_families = [
        family.strip().lower()
        for family in g.getlist("support_resistance_families")
        if family.strip().lower() in DEFAULT_LEVEL_FAMILIES
    ]
    if not support_resistance_filters_submitted:
        support_resistance_enabled_families = list(DEFAULT_LEVEL_FAMILIES)
    support_resistance_enabled_set = set(support_resistance_enabled_families)
    for option in support_resistance_family_options:
        option["checked"] = option["value"] in support_resistance_enabled_set

    # ── TAB 2: T3MA ──────────────────────────────────────────────────────────
    if selected_symbol and active_tab == "backtest":
        if not (t3_from and t3_to):
            backtest_metrics = {"error": "Please select both From Date and To Date."}
        else:
            df = _build_standard_dataframe(selected_symbol, t3_from, t3_to)
            if df.empty:
                backtest_metrics = {"error": f"No price data found for '{selected_symbol}' in the selected date range."}
            else:
                backtest_metrics, trades_df = run_t3ma_macd_ribbon_simulation(df)
                if isinstance(backtest_metrics, dict) and "error" not in backtest_metrics and not trades_df.empty:
                    trades_df["entry_date"] = pd.to_datetime(trades_df["entry_date"]).dt.strftime("%Y-%m-%d")
                    trades_df["exit_date"]  = pd.to_datetime(trades_df["exit_date"]).dt.strftime("%Y-%m-%d")
                    completed_trades = trades_df.to_dict(orient="records")

    # ── TAB 3: EMA 50/200 ────────────────────────────────────────────────────
    if ema_selected_symbol and active_tab == "ema_backtest":
        if not (ema_from and ema_to):
            ema_backtest_metrics = {"error": "Please select both From Date and To Date."}
        else:
            df = _build_standard_dataframe(ema_selected_symbol, ema_from, ema_to)
            if df.empty:
                ema_backtest_metrics = {"error": f"No price data found for '{ema_selected_symbol}' in the selected date range."}
            else:
                ema_backtest_metrics, ema_trades_df = run_ema_50_200_long_only_simulation(
                    df,
                    take_profit_pct=_safe_float(g.get("ema_take_profit_pct", 15), 15.0),
                    stop_loss_pct=_safe_float(g.get("ema_stop_loss_pct", 7), 7.0),
                    fast_ema_period=_safe_int(g.get("ema_fast_period", 50), 50, minimum=1),
                    slow_ema_period=_safe_int(g.get("ema_slow_period", 200), 200, minimum=1),
                )
                if isinstance(ema_backtest_metrics, dict) and "error" not in ema_backtest_metrics and not ema_trades_df.empty:
                    ema_trades_df["entry_date"] = pd.to_datetime(ema_trades_df["entry_date"]).dt.strftime("%Y-%m-%d")
                    ema_trades_df["exit_date"]  = pd.to_datetime(ema_trades_df["exit_date"]).dt.strftime("%Y-%m-%d")
                    ema_completed_trades = ema_trades_df.to_dict(orient="records")

    # ── TAB 4: CCI ───────────────────────────────────────────────────────────
    if cci_selected_symbol and active_tab == "cci_backtest":
        if not (cci_from and cci_to):
            cci_backtest_metrics = {"error": "Please select both From Date and To Date."}
        else:
            df = _build_standard_dataframe(cci_selected_symbol, cci_from, cci_to)
            if df.empty:
                cci_backtest_metrics = {"error": f"No price data found for '{cci_selected_symbol}' in the selected date range."}
            else:
                cci_backtest_metrics, cci_trades_df = run_cci_long_only_simulation(
                    df,
                    cci_period=_safe_int(g.get("cci_period", 20), 20, minimum=1),
                    adx_threshold=_safe_float(g.get("cci_adx_threshold", 25), 25.0),
                    volume_avg_period=_safe_int(g.get("cci_volume_avg_period", 20), 20, minimum=1),
                    volume_multiplier=_safe_float(g.get("cci_volume_multiplier", 1.5), 1.5),
                )
                if isinstance(cci_backtest_metrics, dict) and "error" not in cci_backtest_metrics and not cci_trades_df.empty:
                    cci_trades_df["entry_date"] = pd.to_datetime(cci_trades_df["entry_date"]).dt.strftime("%Y-%m-%d")
                    cci_trades_df["exit_date"]  = pd.to_datetime(cci_trades_df["exit_date"]).dt.strftime("%Y-%m-%d")
                    cci_completed_trades = cci_trades_df.to_dict(orient="records")

    # ── TAB: SOP Multi-Indicator (5 optimized indicators + 200-SMA regime) ────
    if sop_selected_symbol and active_tab == "sop_backtest":
        if not (sop_from and sop_to):
            sop_backtest_metrics = {"error": "Please select both From Date and To Date."}
        else:
            df = _build_standard_dataframe(sop_selected_symbol, sop_from, sop_to, use_unadjusted_fallback=True)
            if df.empty:
                sop_backtest_metrics = {"error": f"No price data found for '{sop_selected_symbol}' in the selected date range."}
            else:
                # Regime frame: NEPSE Index 200-SMA, fetched with a wide history so
                # the SMA is warm at the window start (aligned by date on merge).
                regime_frame = None
                if sop_use_regime:
                    idx_df = _build_standard_dataframe("NEPSE INDEX", "2000-01-01", sop_to)
                    regime_frame = market_regime_series(idx_df)
                sop_backtest_metrics, sop_trades_df, sop_equity_df = run_sop_simulation(
                    df,
                    indicator=sop_indicator,
                    use_regime_filter=sop_use_regime,
                    regime_df=regime_frame,
                    cost_bps=_safe_float(g.get("sop_cost_bps", 80), 80.0),
                )
                if isinstance(sop_backtest_metrics, dict) and "error" not in sop_backtest_metrics:
                    if not sop_trades_df.empty:
                        sop_trades_df["entry_date"] = pd.to_datetime(sop_trades_df["entry_date"]).dt.strftime("%Y-%m-%d")
                        sop_trades_df["exit_date"]  = pd.to_datetime(sop_trades_df["exit_date"]).dt.strftime("%Y-%m-%d")
                        sop_completed_trades = sop_trades_df.to_dict(orient="records")
                    if sop_equity_df is not None and not sop_equity_df.empty:
                        eq = sop_equity_df.copy()
                        eq["x"] = (pd.to_datetime(eq["business_date"]).astype("int64") // 1_000_000)
                        sop_equity = [
                            {
                                "x": int(r.x),
                                "equity": None if pd.isna(r.equity) else float(r.equity),
                                "buyhold": None if pd.isna(r.buyhold) else float(r.buyhold),
                                "drawdown": None if pd.isna(r.drawdown_pct) else float(r.drawdown_pct),
                                "regime": str(r.regime),
                            }
                            for r in eq.itertuples(index=False)
                        ]

    # ── TAB: SOP Combined (confluence of N-of-M optimized indicators) ─────────
    if sopc_selected_symbol and active_tab == "sop_combined":
        if not (sopc_from and sopc_to):
            sopc_backtest_metrics = {"error": "Please select both From Date and To Date."}
        else:
            df = _build_standard_dataframe(sopc_selected_symbol, sopc_from, sopc_to, use_unadjusted_fallback=True)
            if df.empty:
                sopc_backtest_metrics = {"error": f"No price data found for '{sopc_selected_symbol}' in the selected date range."}
            else:
                regime_frame = None
                if sopc_use_regime:
                    idx_df = _build_standard_dataframe("NEPSE INDEX", "2000-01-01", sopc_to)
                    regime_frame = market_regime_series(idx_df)
                sopc_backtest_metrics, sopc_trades_df, sopc_equity_df = run_sop_combined_simulation(
                    df,
                    indicators=sopc_indicators,
                    regime_mode=sopc_regime_mode,
                    bear_min_agree=sopc_bear_min_agree,
                    min_agree=sopc_min_agree,
                    use_regime_filter=sopc_use_regime,
                    regime_df=regime_frame,
                    cost_bps=_safe_float(g.get("sopc_cost_bps", 80), 80.0),
                )
                if isinstance(sopc_backtest_metrics, dict) and "error" not in sopc_backtest_metrics:
                    if not sopc_trades_df.empty:
                        sopc_trades_df["entry_date"] = pd.to_datetime(sopc_trades_df["entry_date"]).dt.strftime("%Y-%m-%d")
                        sopc_trades_df["exit_date"]  = pd.to_datetime(sopc_trades_df["exit_date"]).dt.strftime("%Y-%m-%d")
                        sopc_completed_trades = sopc_trades_df.to_dict(orient="records")
                    if sopc_equity_df is not None and not sopc_equity_df.empty:
                        eqc = sopc_equity_df.copy()
                        eqc["x"] = (pd.to_datetime(eqc["business_date"]).astype("int64") // 1_000_000)
                        sopc_equity = [
                            {
                                "x": int(r.x),
                                "equity": None if pd.isna(r.equity) else float(r.equity),
                                "buyhold": None if pd.isna(r.buyhold) else float(r.buyhold),
                                "drawdown": None if pd.isna(r.drawdown_pct) else float(r.drawdown_pct),
                                "regime": str(r.regime),
                            }
                            for r in eqc.itertuples(index=False)
                        ]

    # ── TAB 5: RSI/SMA ───────────────────────────────────────────────────────
    if rsi_selected_symbol and active_tab == "rsi_backtest":
        if not (rsi_from and rsi_to):
            rsi_backtest_metrics = {"error": "Please select both From Date and To Date."}
        else:
            df = _build_standard_dataframe(rsi_selected_symbol, rsi_from, rsi_to)
            if df.empty:
                rsi_backtest_metrics = {"error": f"No price data found for '{rsi_selected_symbol}' in the selected date range."}
            else:
                rsi_backtest_metrics, rsi_trades_df = run_rsi_sma_long_only_simulation(
                    df,
                    rsi_length=_safe_int(g.get("rsi_length", 14), 14, minimum=1),
                    rsi_sma_length=_safe_int(g.get("rsi_sma_length", 9), 9, minimum=1),
                )
                if isinstance(rsi_backtest_metrics, dict) and "error" not in rsi_backtest_metrics and not rsi_trades_df.empty:
                    rsi_trades_df["entry_date"] = pd.to_datetime(rsi_trades_df["entry_date"]).dt.strftime("%Y-%m-%d")
                    rsi_trades_df["exit_date"]  = pd.to_datetime(rsi_trades_df["exit_date"]).dt.strftime("%Y-%m-%d")
                    rsi_completed_trades = rsi_trades_df.to_dict(orient="records")

    # TAB 6: MACD + Supertrend + VWAP + ATR + RVOL (MSV)
    if msv_selected_symbol and active_tab == "msv_backtest":
        if not (msv_from and msv_to):
            msv_backtest_metrics = {"error": "Please select both From Date and To Date."}
        else:
            df = _build_standard_dataframe(
                msv_selected_symbol,
                msv_from,
                msv_to,
                use_unadjusted_fallback=True,
            )
            if df.empty:
                msv_backtest_metrics = {"error": f"No price data found for '{msv_selected_symbol}' in the selected date range."}
            elif len(df) < SNAPSHOT_TRIGGER_BARS["msv"]:
                # Too few bars for MACD/Supertrend/ATR — show a New Listing
                # snapshot instead of erroring out.
                msv_new_listing = build_new_listing_snapshot(
                    df, FULL_HISTORY_BARS["msv"], symbol=msv_selected_symbol,
                    desk_label="Momentum Scan",
                )
            else:
                try:
                    msv_backtest_metrics, msv_trades_df, msv_indicator_df = run_msv_long_only_simulation(
                        df,
                        macd_fast=_safe_int(g.get("msv_macd_fast", 12), 12, minimum=1),
                        macd_slow=_safe_int(g.get("msv_macd_slow", 26), 26, minimum=2),
                        macd_signal=_safe_int(g.get("msv_macd_signal", 9), 9, minimum=1),
                        atr_length=_safe_int(g.get("msv_atr_length", 14), 14, minimum=2),
                        atr_multiplier=_safe_float(g.get("msv_atr_multiplier", 2.0), 2.0),
                        rvol_period=_safe_int(g.get("msv_rvol_period", 20), 20, minimum=2),
                        rvol_threshold=_safe_float(g.get("msv_rvol_threshold", 1.5), 1.5),
                        supertrend_length=_safe_int(g.get("msv_supertrend_length", 10), 10, minimum=2),
                        supertrend_multiplier=_safe_float(g.get("msv_supertrend_multiplier", 3.0), 3.0),
                    )
                except Exception as e:
                    msv_backtest_metrics = {"error": f"Error running MSV strategy: {str(e)}"}
                    msv_trades_df = pd.DataFrame()
                    msv_indicator_df = pd.DataFrame()
                if isinstance(msv_backtest_metrics, dict) and "error" not in msv_backtest_metrics:
                    if not msv_trades_df.empty:
                        msv_trades_df["entry_date"] = pd.to_datetime(msv_trades_df["entry_date"]).dt.strftime("%Y-%m-%d")
                        msv_trades_df["exit_date"] = pd.to_datetime(msv_trades_df["exit_date"]).dt.strftime("%Y-%m-%d")
                        msv_completed_trades = msv_trades_df.iloc[::-1].to_dict(orient="records")
                    if not msv_indicator_df.empty:
                        msv_indicator_df["business_date"] = pd.to_datetime(msv_indicator_df["business_date"]).dt.strftime("%Y-%m-%d")
                        msv_indicator_rows = msv_indicator_df.tail(150).iloc[::-1].to_dict(orient="records")

    # TAB 7: IMM institutional technical scoring
    if imm_selected_symbol and active_tab == "imm_backtest":
        if not (imm_from and imm_to):
            imm_backtest_metrics = {"error": "Please select both From Date and To Date."}
        else:
            stock_df = _build_standard_dataframe(
                imm_selected_symbol,
                imm_from,
                imm_to,
                use_unadjusted_fallback=True,
            )
            nepse_df = _build_standard_dataframe("NEPSE INDEX", imm_from, imm_to)
            if stock_df.empty:
                imm_backtest_metrics = {"error": f"No price data found for '{imm_selected_symbol}' in the selected date range."}
            elif len(stock_df) < SNAPSHOT_TRIGGER_BARS["imm"]:
                # IMM needs ~200 bars (SMA-200) before its score is meaningful;
                # below that show a New Listing snapshot, with relative strength
                # measured against NEPSE over the available window.
                imm_new_listing = build_new_listing_snapshot(
                    stock_df, FULL_HISTORY_BARS["imm"],
                    sector_df=nepse_df if not nepse_df.empty else None,
                    symbol=imm_selected_symbol, desk_label="IMM Technical Scoring",
                )
            elif nepse_df.empty:
                imm_backtest_metrics = {"error": "No NEPSE INDEX data found for the selected date range."}
            else:
                imm_backtest_metrics, imm_df = run_imm_scoring_system(
                    stock_df=stock_df,
                    nepse_index_df=nepse_df,
                    rs_lookback=_safe_int(g.get("imm_rs_lookback", 20), 20, minimum=2),
                    atr_length=_safe_int(g.get("imm_atr_length", 14), 14, minimum=2),
                    rsi_length=_safe_int(g.get("imm_rsi_length", 14), 14, minimum=2),
                    macd_fast=_safe_int(g.get("imm_macd_fast", 12), 12, minimum=1),
                    macd_slow=_safe_int(g.get("imm_macd_slow", 26), 26, minimum=2),
                    macd_signal=_safe_int(g.get("imm_macd_signal", 9), 9, minimum=1),
                    supertrend_length=_safe_int(g.get("imm_supertrend_length", 10), 10, minimum=2),
                    supertrend_multiplier=_safe_float(g.get("imm_supertrend_multiplier", 3.0), 3.0),
                )
                if isinstance(imm_backtest_metrics, dict) and "error" not in imm_backtest_metrics and not imm_df.empty:
                    imm_df["business_date"] = pd.to_datetime(imm_df["business_date"]).dt.strftime("%Y-%m-%d")
                    imm_indicator_rows = imm_df.tail(150).iloc[::-1].to_dict(orient="records")
                    imm_event_rows = (
                        imm_df[(imm_df["buy_signal"] == True) | (imm_df["sell_signal"] == True)]
                        .tail(120)
                        .iloc[::-1]
                        .to_dict(orient="records")
                    )

    # TAB 8: Stage Analysis
    if stage_selected_symbol and active_tab == "stage_backtest":
        if not (stage_from and stage_to):
            stage_backtest_metrics = {"error": "Please select both From Date and To Date."}
        else:
            df = _build_standard_dataframe(
                stage_selected_symbol,
                stage_from,
                stage_to,
                use_unadjusted_fallback=True,
            )
            if df.empty:
                stage_backtest_metrics = {"error": f"No price data found for '{stage_selected_symbol}' in the selected date range."}
            elif len(df) < SNAPSHOT_TRIGGER_BARS["stage"]:
                # Below the provisional floor (30 bars) stage classification is
                # meaningless — show a New Listing snapshot instead.
                stage_new_listing = build_new_listing_snapshot(
                    df, FULL_HISTORY_BARS["stage"], symbol=stage_selected_symbol,
                    desk_label="Stage Analysis",
                )
            else:
                # rename columns to match what calculate_stage_analysis expects
                df_calc = df.rename(columns={
                    "close_price_adj": "close",
                    "high_price_adj": "high",
                    "low_price_adj": "low",
                })
                # Bug fix 6: warn early if volume is entirely missing so users
                # know Stage 2 (which requires volume_ratio > 1.5) can never fire.
                if "volume" not in df_calc.columns or df_calc["volume"].isna().all():
                    stage_backtest_metrics = {
                        "error": (
                            f"No volume data found for '{stage_selected_symbol}'. "
                            "Stage 2 classification requires volume — ensure the symbol "
                            "has traded quantity records in the selected date range."
                        )
                    }
                else:
                    try:
                        stage_df = calculate_stage_analysis(
                            df_calc,
                            volume_multiplier=stage_volume_multiplier,
                            resistance_lookback=stage_resistance_lookback,
                            volume_lookback=stage_volume_lookback,
                            momentum_period=stage_momentum_period,
                            rsi_length=stage_rsi_length,
                            rsi_threshold=stage_rsi_threshold,
                            adx_length=stage_adx_length,
                            adx_threshold=stage_adx_threshold,
                            use_rsi_filter=stage_use_rsi,
                            use_adx_filter=stage_use_adx,
                        )
                        if not stage_df.empty:
                            stage_meta = {
                                "Stage 1": ("Basing/Neglect", "Watch"),
                                "Stage 2": ("Advancing/Mark-up", "Buy"),
                                "Stage 3": ("Distribution/Top", "Reduce"),
                                "Stage 4": ("Declining/Mark-down", "Avoid"),
                            }
                            stage_df["stage_name"] = stage_df["stage"].map(lambda s: stage_meta.get(s, ("Unknown", "Watch"))[0])
                            stage_df["stage_action"] = stage_df["stage"].map(lambda s: stage_meta.get(s, ("Unknown", "Watch"))[1])
                            stage_df["business_date"] = pd.to_datetime(stage_df["business_date"]).dt.strftime("%Y-%m-%d")
                            # Bug fix 7: scale returns_3m to percent in the indicator
                            # rows so the template can display it as e.g. "5.00" not "0.0500".
                            if "returns_3m" in stage_df.columns:
                                stage_df["returns_3m"] = stage_df["returns_3m"] * 100
                            stage_indicator_rows = stage_df.tail(150).iloc[::-1].to_dict(orient="records")
                            latest_row = stage_df.iloc[-1]
                            latest_stage = latest_row.get("stage", "Stage 1")
                            latest_name = latest_row.get("stage_name", "Basing/Neglect")
                            # Provisional read for newly listed stocks: the stage
                            # is computed on scaled (shorter) EMA baselines, so flag
                            # it clearly and report how much history backs it.
                            is_provisional = bool(latest_row.get("provisional", False))
                            history_rows = int(latest_row.get("history_rows", len(stage_df)))
                            history_weeks = history_rows // 5
                            stage_label = f"{latest_stage} - {latest_name}"
                            latest_action = latest_row.get("stage_action", "Watch")
                            if is_provisional:
                                stage_label += " (Provisional)"
                                latest_action = f"{latest_action} (provisional)"
                            stage_backtest_metrics = {
                                "latest_data_date": latest_row.get("business_date", ""),
                                "latest_price_source": latest_row.get("price_source", "Adjusted"),
                                "latest_stage": latest_stage,
                                "latest_stage_label": stage_label,
                                "latest_action": latest_action,
                                "is_provisional": is_provisional,
                                "history_weeks": history_weeks,
                                "weeks_needed": 30,
                                # returns_3m is already in percent at this point
                                "returns_3m": float(latest_row.get("returns_3m", 0)) if pd.notna(latest_row.get("returns_3m")) else 0,
                                "volume_ratio": float(latest_row.get("volume_ratio", 0)) if pd.notna(latest_row.get("volume_ratio")) else 0,
                                "volume_confirm": bool(latest_row.get("volume_confirm", False)),
                                "rsi": float(latest_row.get("rsi", 0)) if pd.notna(latest_row.get("rsi")) else 0,
                                "rsi_confirm": bool(latest_row.get("rsi_confirm", False)),
                                "adx": float(latest_row.get("adx", 0)) if pd.notna(latest_row.get("adx")) else 0,
                                "adx_confirm": bool(latest_row.get("adx_confirm", False)),
                                "stage2_score": int(latest_row.get("stage2_score", 0)) if pd.notna(latest_row.get("stage2_score")) else 0,
                                # Weinstein context: how long the current stage has
                                # run, % stretch from the 30-week MA, and the most
                                # recent stage flip in the window (e.g. "Stage 1 →
                                # Stage 2") with the date it happened.
                                "bars_in_stage": int(latest_row.get("bars_in_stage", 0)) if pd.notna(latest_row.get("bars_in_stage")) else 0,
                                "pct_from_ema30": float(latest_row.get("pct_from_ema30", 0)) if pd.notna(latest_row.get("pct_from_ema30")) else None,
                                "last_transition": next(
                                    (
                                        {"label": r["stage_transition"], "date": r["business_date"]}
                                        for r in reversed(stage_df.tail(150).to_dict(orient="records"))
                                        if r.get("stage_transition")
                                    ),
                                    None,
                                ),
                            }
                    except Exception as e:
                        stage_backtest_metrics = {"error": f"Error running Stage Analysis: {str(e)}"}

    # TAB 9: Support & Resistance
    if support_resistance_selected_symbol and active_tab == "support_resistance":
        if not (support_resistance_from and support_resistance_to):
            support_resistance_metrics = {"error": "Please select both From Date and To Date."}
        else:
            df = _build_standard_dataframe(
                support_resistance_selected_symbol,
                support_resistance_from,
                support_resistance_to,
                use_unadjusted_fallback=True,
            )
            if df.empty:
                support_resistance_metrics = {
                    "error": f"No price data found for '{support_resistance_selected_symbol}' in the selected date range."
                }
            else:
                # Run the advanced layer first so its DBSCAN volume-density
                # zones can feed the support/resistance confluence engine.
                try:
                    advanced_market_structure_metrics = run_advanced_market_structure_analysis(
                        df,
                        symbol=support_resistance_selected_symbol,
                        fractal_window=support_resistance_fractal_window,
                    )
                except Exception as e:
                    advanced_market_structure_metrics = {"error": f"Error running Advanced Market Structure analysis: {str(e)}"}
                density_zones = None
                if isinstance(advanced_market_structure_metrics, dict) and not advanced_market_structure_metrics.get("error"):
                    density_zones = advanced_market_structure_metrics.get("density_zones")
                try:
                    support_resistance_metrics, support_resistance_rows = run_support_resistance_analysis(
                        df,
                        symbol=support_resistance_selected_symbol,
                        std_period=support_resistance_std_period,
                        rsi_length=support_resistance_rsi_length,
                        stochastic_length=support_resistance_stochastic_length,
                        enabled_families=support_resistance_enabled_families,
                        density_zones=density_zones,
                        fractal_window=support_resistance_fractal_window,
                    )
                except Exception as e:
                    support_resistance_metrics = {"error": f"Error running Support & Resistance analysis: {str(e)}"}
                institutional_analysis_rows = build_institutional_analysis_rows(
                    support_resistance_metrics,
                    advanced_market_structure_metrics,
                )

    # TAB 10: RRG (Relative Rotation Graph)
    # The RRG tab stacks two independent desks (companies above, indices below),
    # so a full-page load of either tab key builds BOTH — otherwise whichever
    # desk did not match the key would render empty. They read separate GET
    # params (rrg_* vs rrg_indices_*), so neither can affect the other.
    # The AJAX calc endpoint renders one desk's partial and computes only that one.
    if rrg_selected_symbol and rrg_run_company:
        if not (rrg_from and rrg_to):
            rrg_backtest_metrics = {"error": "Please select both From Date and To Date."}
        else:
            # RRG needs lookback*2 bars (RS-Ratio MA, then RS-Momentum MA of it)
            # before it can plot a single point — see run_rrg_simulation's gate.
            rrg_required_bars = rrg_lookback * 2
            stock_df = _build_standard_dataframe(
                rrg_selected_symbol,
                rrg_from,
                rrg_to,
                use_unadjusted_fallback=True,
            )
            bench_df = _build_standard_dataframe(
                rrg_benchmark_symbol,
                rrg_from,
                rrg_to,
                use_unadjusted_fallback=True,
            )
            if stock_df.empty:
                rrg_backtest_metrics = {"error": f"No price data found for '{rrg_selected_symbol}' in the selected date range."}
            elif bench_df.empty:
                rrg_backtest_metrics = {"error": f"No price data found for '{rrg_benchmark_symbol}' in the selected date range."}
            elif int((pd.to_numeric(stock_df["close_price_adj"], errors="coerce") > 0).sum()) < rrg_required_bars:
                # Too few bars to rotate on the RRG — show a New Listing snapshot
                # with relative strength measured against the chosen benchmark.
                rrg_new_listing = build_new_listing_snapshot(
                    stock_df, rrg_required_bars,
                    sector_df=bench_df if not bench_df.empty else None,
                    symbol=rrg_selected_symbol, desk_label="RRG Analytics",
                )
            else:
                rrg_backtest_metrics, rrg_df = run_rrg_simulation(
                    stock_df=stock_df,
                    benchmark_df=bench_df,
                    lookback=rrg_lookback
                )
                if isinstance(rrg_backtest_metrics, dict) and "error" not in rrg_backtest_metrics and not rrg_df.empty:
                    rrg_df["business_date"] = pd.to_datetime(rrg_df["business_date"]).dt.strftime("%Y-%m-%d")
                    rrg_chart_points = [
                        {
                            "business_date": row["business_date"],
                            "RS_Ratio": round(float(row["RS_Ratio"]), 2),
                            "RS_Momentum": round(float(row["RS_Momentum"]), 2),
                            "Quadrant": str(row["Quadrant"]),
                        }
                        for _, row in rrg_df.tail(50).iterrows()
                    ]
                    rrg_indicator_rows = [
                        {
                            "business_date": row["business_date"],
                            "stock_close": round(float(row["stock_close"]), 2),
                            "bench_close": round(float(row["bench_close"]), 2),
                            "RS": round(float(row["RS"]), 4),
                            "RS_Ratio": round(float(row["RS_Ratio"]), 2),
                            "RS_Momentum": round(float(row["RS_Momentum"]), 2),
                            "Quadrant": str(row["Quadrant"]),
                        }
                        for _, row in rrg_df.tail(150).iloc[::-1].iterrows()
                    ]

    # TAB 11: RRG Indices
    if rrg_run_indices:
        if not (rrg_indices_from and rrg_indices_to):
            rrg_indices_metrics = {"error": "Please select both From Date and To Date."}
        elif not rrg_indices_selected_symbols:
            rrg_indices_metrics = {"error": "Select at least one NEPSE index to plot."}
        else:
            bench_df = _build_standard_dataframe(rrg_indices_benchmark_symbol, rrg_indices_from, rrg_indices_to)
            index_frames = _build_index_dataframes(
                rrg_indices_from, rrg_indices_to, symbols=rrg_indices_selected_symbols,
            )
            rrg_indices_metrics, rrg_indices_points, rrg_indices_trails, rrg_indices_skipped = run_rrg_indices_simulation(
                index_frames=index_frames,
                benchmark_df=bench_df,
                benchmark_symbol=rrg_indices_benchmark_symbol,
                lookback=rrg_indices_lookback,
                tail_length=rrg_indices_tail_length,
                selected_symbols=rrg_indices_selected_symbols,
            )
            rrg_indices_benchmark_points = _build_benchmark_sparkline(bench_df)

    # Seasonal Return — monthly seasonality of the NEPSE indices, computed from the
    # local index table (cached). Built for the Workbench/Data tabs so the Seasonal
    # pane always has data when switched to from the Raw Inventory Manager.
    if active_tab in _WORKBENCH_DATA_TABS:
        seasonal = build_seasonal_payload()
        # NEPSE market-cycle chart (Weinstein stages) shown on the Seasonal desk.
        market_cycle = build_market_cycle()
        # Long-horizon bull/bear dating of the same index. Complements the
        # Weinstein stages, which read the current trend over ~3 years; this
        # dates the full 29-year cycle history.
        market_regimes = build_market_regimes()

    # Life-insurance "Other Indicators" entry form — staff-only inventory tab.
    life_tickers, life_recent, life_spec = [], [], []
    if active_tab == "inventory":
        from core_analysis.models import LifeInsuranceIndicator
        from core_analysis.services import life_indicators as li
        by_sec = li.tickers_by_sector()
        life_tickers = [{"sector": sec, "tickers": by_sec.get(sec, [])} for sec in li.SPECS]
        life_recent = list(LifeInsuranceIndicator.objects.order_by("-updated_at")[:20])
        life_spec = li.form_spec()

    return {
        "records": queryset,
        "life_tickers": life_tickers,
        "life_recent": life_recent,
        "life_spec": life_spec,

        # OPTIMIZED: Empty symbol lists (autocomplete handles search)
        "company_choices": db_companies,
        "index_choices":   db_indices,

        # T3MA
        "backtest_metrics":  backtest_metrics,
        "completed_trades":  completed_trades,
        "selected_symbol":   selected_symbol,
        "t3_from_date":      t3_from,
        "t3_to_date":        t3_to,

        # EMA
        "ema_backtest_metrics": ema_backtest_metrics,
        "ema_completed_trades": ema_completed_trades,
        "ema_selected_symbol":  ema_selected_symbol,
        "ema_from_date":        ema_from,
        "ema_to_date":          ema_to,

        # CCI
        "cci_backtest_metrics": cci_backtest_metrics,
        "cci_completed_trades": cci_completed_trades,
        "cci_selected_symbol":  cci_selected_symbol,
        "cci_from_date":        cci_from,
        "cci_to_date":          cci_to,

        "sop_backtest_metrics": sop_backtest_metrics,
        "sop_completed_trades": sop_completed_trades,
        "sop_selected_symbol":  sop_selected_symbol,
        "sop_from_date":        sop_from,
        "sop_to_date":          sop_to,
        "sop_indicator":        sop_indicator,
        "sop_use_regime":       sop_use_regime,
        "sop_equity":           sop_equity,

        "sopc_backtest_metrics": sopc_backtest_metrics,
        "sopc_completed_trades": sopc_completed_trades,
        "sopc_selected_symbol":  sopc_selected_symbol,
        "sopc_from_date":        sopc_from,
        "sopc_to_date":          sopc_to,
        "sopc_indicators":       sopc_indicators,
        "sopc_min_agree":        sopc_min_agree,
        "sopc_regime_mode":      sopc_regime_mode,
        "sopc_bear_min_agree":   sopc_bear_min_agree,
        "sopc_use_regime":       sopc_use_regime,
        "sopc_equity":           sopc_equity,
        # Built from the engine registry, so retiring or adding an indicator
        # needs no edit here.
        "sopc_indicator_choices": [
            (k, _SOP_INDICATOR_LABELS.get(k, k)) for k in _SOP_INDICATOR_KEYS
        ],

        # RSI
        "rsi_backtest_metrics": rsi_backtest_metrics,
        "rsi_completed_trades": rsi_completed_trades,
        "rsi_selected_symbol":  rsi_selected_symbol,
        "rsi_from_date":        rsi_from,
        "rsi_to_date":          rsi_to,
        "msv_backtest_metrics": msv_backtest_metrics,
        "msv_new_listing":      msv_new_listing,
        "msv_completed_trades": msv_completed_trades,
        "msv_indicator_rows":   msv_indicator_rows,
        "msv_selected_symbol":  msv_selected_symbol,
        "msv_from_date":        msv_from,
        "msv_to_date":          msv_to,
        "imm_backtest_metrics": imm_backtest_metrics,
        "imm_new_listing":      imm_new_listing,
        "imm_indicator_rows":   imm_indicator_rows,
        "imm_event_rows":       imm_event_rows,
        "imm_selected_symbol":  imm_selected_symbol,
        "imm_from_date":        imm_from,
        "imm_to_date":          imm_to,
        "imm_data_upload_date": imm_data_upload_date,
        
        # Stage
        "stage_backtest_metrics": stage_backtest_metrics,
        "stage_new_listing":      stage_new_listing,
        "stage_indicator_rows":   stage_indicator_rows,
        "stage_selected_symbol":  stage_selected_symbol,
        "stage_from_date":        stage_from,
        "stage_to_date":          stage_to,
        "stage_volume_multiplier": g.get("stage_volume_multiplier", "1.5"),
        "stage_resistance_lookback": g.get("stage_resistance_lookback", "20"),
        "stage_volume_lookback": g.get("stage_volume_lookback", "20"),
        "stage_momentum_period": g.get("stage_momentum_period", "60"),
        "stage_rsi_length": g.get("stage_rsi_length", "14"),
        "stage_rsi_threshold": g.get("stage_rsi_threshold", "55"),
        "stage_adx_length": g.get("stage_adx_length", "14"),
        "stage_adx_threshold": g.get("stage_adx_threshold", "20"),
        "stage_use_rsi": stage_use_rsi,
        "stage_use_adx": stage_use_adx,

        # Support & Resistance
        "support_resistance_metrics": support_resistance_metrics,
        "support_resistance_rows": support_resistance_rows,
        "support_resistance_selected_symbol": support_resistance_selected_symbol,
        "support_resistance_from_date": support_resistance_from,
        "support_resistance_to_date": support_resistance_to,
        "support_resistance_std_period": g.get("support_resistance_std_period", "20"),
        "support_resistance_rsi_length": g.get("support_resistance_rsi_length", "14"),
        "support_resistance_stochastic_length": g.get("support_resistance_stochastic_length", "14"),
        "support_resistance_fractal_window": support_resistance_fractal_window,
        "support_resistance_family_options": support_resistance_family_options,
        "advanced_market_structure_metrics": advanced_market_structure_metrics,
        "institutional_analysis_rows": institutional_analysis_rows,

        # RRG
        "rrg_backtest_metrics": rrg_backtest_metrics,
        "rrg_new_listing":      rrg_new_listing,
        "rrg_indicator_rows":   rrg_indicator_rows,
        "rrg_chart_points":     rrg_chart_points,
        "rrg_selected_symbol":  rrg_selected_symbol,
        "rrg_benchmark_symbol": rrg_benchmark_symbol,
        "rrg_from_date":        rrg_from,
        "rrg_to_date":          rrg_to,
        "rrg_lookback":         str(rrg_lookback),

        # RRG Indices
        "rrg_indices_metrics": rrg_indices_metrics,
        "rrg_indices_points": rrg_indices_points,
        "rrg_indices_trails": rrg_indices_trails,
        "rrg_indices_skipped": rrg_indices_skipped,
        "rrg_indices_benchmark_points": rrg_indices_benchmark_points,
        "rrg_indices_benchmark_symbol": rrg_indices_benchmark_symbol,
        "rrg_indices_choices": rrg_indices_choices,
        "rrg_indices_selected_count": len(rrg_indices_selected_symbols),
        "rrg_indices_from_date": rrg_indices_from,
        "rrg_indices_to_date": rrg_indices_to,
        "rrg_indices_lookback": str(rrg_indices_lookback),
        "rrg_indices_tail_length": str(rrg_indices_tail_length),

        # RRG Seasonal Return
        "seasonal": seasonal,
        "market_cycle": market_cycle,
        "market_regimes": market_regimes,

        # EMA parameters
        "ema_take_profit_pct": g.get("ema_take_profit_pct", "15"),
        "ema_stop_loss_pct":   g.get("ema_stop_loss_pct", "7"),
        "ema_fast_period":     g.get("ema_fast_period", "50"),
        "ema_slow_period":     g.get("ema_slow_period", "200"),

        # CCI parameters
        "cci_period":        g.get("cci_period", "20"),
        "cci_adx_threshold": g.get("cci_adx_threshold", "25"),
        "cci_volume_avg_period": g.get("cci_volume_avg_period", "20"),
        "cci_volume_multiplier": g.get("cci_volume_multiplier", "1.5"),
        "sop_cost_bps":      g.get("sop_cost_bps", "80"),
        "sopc_cost_bps":     g.get("sopc_cost_bps", "80"),

        # RSI parameters
        "rsi_length":     g.get("rsi_length", "14"),
        "rsi_sma_length": g.get("rsi_sma_length", "9"),
        "msv_macd_fast": g.get("msv_macd_fast", "12"),
        "msv_macd_slow": g.get("msv_macd_slow", "26"),
        "msv_macd_signal": g.get("msv_macd_signal", "9"),
        "msv_atr_length": g.get("msv_atr_length", "14"),
        "msv_atr_multiplier": g.get("msv_atr_multiplier", "2.0"),
        "msv_rvol_period": g.get("msv_rvol_period", "20"),
        "msv_rvol_threshold": g.get("msv_rvol_threshold", "1.5"),
        "msv_supertrend_length": g.get("msv_supertrend_length", "10"),
        "msv_supertrend_multiplier": g.get("msv_supertrend_multiplier", "3.0"),
        "imm_rs_lookback": g.get("imm_rs_lookback", "20"),
        "imm_atr_length": g.get("imm_atr_length", "14"),
        "imm_rsi_length": g.get("imm_rsi_length", "14"),
        "imm_macd_fast": g.get("imm_macd_fast", "12"),
        "imm_macd_slow": g.get("imm_macd_slow", "26"),
        "imm_macd_signal": g.get("imm_macd_signal", "9"),
        "imm_supertrend_length": g.get("imm_supertrend_length", "10"),
        "imm_supertrend_multiplier": g.get("imm_supertrend_multiplier", "3.0"),

        "active_tab": active_tab,
        "dashboard_asset_version": _dashboard_asset_version(),
    }


# Maps the workbench's active_tab value to the results partial that the AJAX
# calc endpoint renders. Tabs absent here (e.g. "inventory") have no on-demand
# computation and are only ever shown via the full-page render.
TAB_RESULTS_PARTIALS = {
    "backtest":            "core_analysis/includes/_t3ma_results.html",
    "ema_backtest":        "core_analysis/includes/_ema_results.html",
    "cci_backtest":        "core_analysis/includes/_cci_results.html",
    "sop_backtest":        "core_analysis/includes/_sop_results.html",
    "sop_combined":        "core_analysis/includes/_sop_combined_results.html",
    "rsi_backtest":        "core_analysis/includes/_rsi_results.html",
    "msv_backtest":        "core_analysis/includes/_msv_results.html",
    "imm_backtest":        "core_analysis/includes/_imm_results.html",
    "stage_backtest":      "core_analysis/includes/_stage_results.html",
    "support_resistance":  "core_analysis/includes/_support_resistance_results.html",
    "rrg_backtest":        "core_analysis/includes/_rrg_results.html",
    "rrg_indices":         "core_analysis/includes/_rrg_indices_results.html",
}


@_staff_or_public_tab
@require_GET
def dashboard_tab_calc(request):
    """Run one workbench tab's calculation and return ONLY its results partial.

    The page's strategy forms post here (GET, same params they always used) via
    fetch(); the returned HTML fragment is swapped into that tab's results
    container without a full-page reload. Reuses build_dashboard_context() so
    the computation is byte-for-byte identical to the full-page path — the only
    difference is we render the single tab's results template instead of the
    whole dashboard shell.
    """
    active_tab = request.GET.get("active_tab", "")
    partial = TAB_RESULTS_PARTIALS.get(active_tab)
    if not partial:
        return HttpResponseBadRequest("Unknown or non-computable tab.")
    if active_tab in RRG_TABS:
        request.rrg_calc_only = active_tab   # compute just the desk being re-rendered
    context = build_dashboard_context(request)
    html = render_to_string(partial, context, request=request)
    return HttpResponse(html)


def _recent_bars_summary(df, count=40):
    """Compact, JSON-safe tail of the OHLC frame for the AI brief (oldest→newest)."""
    if df is None or df.empty:
        return []
    tail = df.tail(count)
    bars = []
    for _, row in tail.iterrows():
        bar = {
            "date": pd.to_datetime(row.get("business_date")).strftime("%Y-%m-%d"),
            "open": round(float(row["open_price_adj"]), 2) if pd.notna(row.get("open_price_adj")) else None,
            "high": round(float(row["high_price_adj"]), 2) if pd.notna(row.get("high_price_adj")) else None,
            "low": round(float(row["low_price_adj"]), 2) if pd.notna(row.get("low_price_adj")) else None,
            "close": round(float(row["close_price_adj"]), 2) if pd.notna(row.get("close_price_adj")) else None,
            "volume": int(row["volume"]) if pd.notna(row.get("volume")) else None,
        }
        bars.append(bar)
    return bars


@staff_member_required
@require_GET
def gemini_sr_analysis(request):
    """Generate the Gemini narrative for the Support & Resistance tab (JSON).

    Reuses build_dashboard_context() so the metrics fed to the model are
    byte-for-byte identical to what the tab rendered, then rebuilds the recent
    OHLC tail so the model can read the actual price path. The S/R tab's panel
    fires this automatically on load with the same query params.
    """
    context = build_dashboard_context(request)
    metrics = context.get("support_resistance_metrics")
    if not isinstance(metrics, dict) or metrics.get("error"):
        return JsonResponse(
            {"error": (metrics or {}).get("error") if isinstance(metrics, dict) else "No analysis available for this selection."},
            status=200,
        )

    symbol = context.get("support_resistance_selected_symbol", "")
    sr_from = context.get("support_resistance_from_date", "")
    sr_to = context.get("support_resistance_to_date", "")
    recent_bars = []
    if symbol and sr_from and sr_to:
        try:
            df = _build_standard_dataframe(symbol, sr_from, sr_to, use_unadjusted_fallback=True)
            recent_bars = _recent_bars_summary(df)
        except Exception:
            recent_bars = []

    result = generate_sr_ai_analysis(
        metrics,
        context.get("institutional_analysis_rows"),
        context.get("advanced_market_structure_metrics"),
        recent_bars,
    )
    return JsonResponse(result, status=200)


# ── CRUD handlers (unchanged) ─────────────────────────────────────────────────

@staff_member_required
@require_POST
def crud_operations_handler(request):
    """CREATE & UPDATE operations handler via HTML Form Actions."""
    if request.method == "POST":
        action = request.POST.get("action")

        if action == "create":
            company_symbol = (request.POST.get("company") or "").upper().strip()
            business_date_raw = (request.POST.get("business_date") or "").strip()
            external_id_raw = (request.POST.get("external_id") or "").strip()
            security_id_raw = (request.POST.get("security_id") or "").strip()
            close_price_raw = (request.POST.get("close_price_adj") or "").strip()

            try:
                business_date = date.fromisoformat(business_date_raw)
                external_id = int(external_id_raw)
                security_id = int(security_id_raw)
                close_price_adj = Decimal(close_price_raw)
            except (TypeError, ValueError, InvalidOperation):
                messages.error(request, "Inventory create failed: enter a valid ticker, date, IDs, and close price.")
                return redirect("crud_dashboard")

            if not company_symbol:
                messages.error(request, "Inventory create failed: company ticker is required.")
                return redirect("crud_dashboard")
            if close_price_adj <= 0:
                messages.error(request, "Inventory create failed: close price must be greater than zero.")
                return redirect("crud_dashboard")

            company_profile, _ = CompanyProfile.objects.get_or_create(
                symbol=company_symbol,
                defaults={"security_name": f"Manual profile reference {company_symbol}"},
            )
            try:
                StockPriceAdjustment.objects.create(
                    external_id=external_id,
                    business_date=business_date,
                    company=company_profile,
                    security_id=security_id,
                    open_price=0, high_price=0, low_price=0, close_price=0,
                    open_price_adj=0, high_price_adj=0, low_price_adj=0,
                    close_price_adj=close_price_adj,
                    adjustment_factor=Decimal("1.0"),
                )
                # Invalidate symbol cache so new company appears in dropdowns
                cache.delete("nepse_symbol_lists")
                messages.success(request, f"Inventory row added for {company_symbol} on {business_date}.")
            except IntegrityError:
                messages.error(request, "Inventory create failed: this external ID or ticker/date row already exists.")

        elif action == "update":
            record_id = request.POST.get("record_id")
            record = get_object_or_404(StockPriceAdjustment, id=record_id)
            try:
                record.close_price_adj = Decimal((request.POST.get("close_price_adj") or "").strip())
            except (InvalidOperation, ValueError):
                messages.error(request, "Inventory update failed: close price must be a valid number.")
                return redirect("crud_dashboard")
            if record.close_price_adj <= 0:
                messages.error(request, "Inventory update failed: close price must be greater than zero.")
                return redirect("crud_dashboard")
            record.save(update_fields=["close_price_adj"])
            messages.success(request, f"Close price updated for {record.company_id} on {record.business_date}.")

    return redirect("crud_dashboard")


@staff_member_required
@require_POST
def life_indicator_save(request):
    """Create or replace one life insurer's hand-entered "Other Indicators".

    One row per ticker x fiscal year x quarter; re-submitting the same period
    overwrites it, which is the correction path. Bumps the indicator revision
    so the cached Industry Analysis KS matrix rebuilds on the next read.
    """
    from core_analysis.models import LifeInsuranceIndicator
    from core_analysis.services import life_indicators as li

    sector = (request.POST.get("sector") or li.LIFE).strip()
    ticker = (request.POST.get("ticker") or "").strip().upper()
    fy = (request.POST.get("fiscal_year_ad") or "").strip()
    try:
        quarter = int(request.POST.get("quarter") or 0)
    except (TypeError, ValueError):
        quarter = 0
    if sector not in li.SPECS:
        messages.error(request, f"Insurance indicators: unknown sector {sector!r}.")
        return redirect(f"{reverse('crud_dashboard')}?active_tab=inventory")
    if not ticker or not re.match(r"^\d{4}/\d{2}$", fy) or quarter not in (1, 2, 3, 4):
        messages.error(request, "Insurance indicators: ticker, fiscal year (e.g. 2025/26) and quarter (1-4) are required.")
        return redirect(f"{reverse('crud_dashboard')}?active_tab=inventory")

    values, errors = li.parse_form(request.POST, sector)
    if errors:
        messages.error(request, "Life indicators not saved — " + " ".join(errors))
        return redirect(f"{reverse('crud_dashboard')}?active_tab=inventory")
    if all(v in (None, "") for v in values.values()):
        messages.error(request, "Life indicators not saved — every field was blank.")
        return redirect(f"{reverse('crud_dashboard')}?active_tab=inventory")

    values["source_note"] = (request.POST.get("source_note") or "").strip()[:255]
    values["sector"] = sector
    _, created = LifeInsuranceIndicator.objects.update_or_create(
        ticker=ticker, fiscal_year_ad=fy, quarter=quarter, defaults=values)
    li.bump_revision()
    messages.success(request, f"Other indicators {'added' if created else 'updated'} for {ticker} {fy} Q{quarter}.")
    return redirect(f"{reverse('crud_dashboard')}?active_tab=inventory")


@staff_member_required
@require_POST
def life_indicator_delete(request, pk):
    from core_analysis.models import LifeInsuranceIndicator
    from core_analysis.services import life_indicators as li

    row = LifeInsuranceIndicator.objects.filter(id=pk).first()
    if row is None:
        messages.error(request, f"Life indicators row #{pk} no longer exists.")
    else:
        label = f"{row.ticker} {row.fiscal_year_ad} Q{row.quarter}"
        row.delete()
        li.bump_revision()
        messages.success(request, f"Deleted life indicators for {label}.")
    return redirect(f"{reverse('crud_dashboard')}?active_tab=inventory")


@staff_member_required
@require_POST
def crud_delete_handler(request, pk):
    """DELETE operation route (POST only, CSRF-protected)."""
    record = StockPriceAdjustment.objects.filter(id=pk).first()
    if record is None:
        # A stale row (already deleted in another tab) lands back on the
        # dashboard with an explanation, not on a bare 404 page.
        messages.error(request, f"Record #{pk} no longer exists.")
        return redirect("crud_dashboard")
    label = f"{record.company_id} on {record.business_date}"
    record.delete()
    messages.success(request, f"Deleted inventory row for {label}.")
    return redirect("crud_dashboard")


@staff_member_required
@require_POST
def trigger_daily_api_sync_view(request):
    """
    On-demand daily sync engine endpoint. POST-only so the heavy external sync
    cannot be triggered by a GET (e.g. a forged <img> tag).
    """
    from_date_raw = (request.POST.get("from_date") or "").strip()
    to_date_raw = (request.POST.get("to_date") or "").strip()
    try:
        from_date = date.fromisoformat(from_date_raw) if from_date_raw else None
        to_date = date.fromisoformat(to_date_raw) if to_date_raw else None
    except ValueError:
        messages.error(request, "Invalid sync date format. Use YYYY-MM-DD.")
        return redirect("crud_dashboard")
    if from_date and to_date and from_date > to_date:
        messages.error(request, "Price sync start date cannot be after the end date.")
        return redirect("crud_dashboard")
    source = (request.POST.get("source") or "both").strip().lower()
    if source not in {"both", "stocks", "indices"}:
        source = "both"

    try:
        kwargs = {"source": source}
        if from_date:
            kwargs["from_date"] = from_date
        if to_date:
            kwargs["to_date"] = to_date
        call_command("sync_nepse_data", **kwargs)
        cache.delete("nepse_symbol_lists")
        messages.success(
            request,
            f"Price sync completed (source={source}, from={from_date_raw or 'start'}, to={to_date_raw or 'latest'}).",
        )
    except Exception as e:
        messages.error(request, f"Price sync failed: {str(e)}")

    return redirect("crud_dashboard")


@staff_member_required
@require_POST
def trigger_mutual_fund_nav_sync_view(request):
    """On-demand mutual-fund NAV scrape (ShareSansar), POST-only.

    This is the ONLY source of data for the Mutual Fund sector. Funds file no
    quarterly statements, so the fundamentals sync above will never populate
    them however often it runs — NAV is what a fund publishes instead.

    Worth running monthly: the feed exposes only each fund's latest reading, so
    history accumulates from syncs rather than being backfillable.
    """
    try:
        stats = mutual_fund_nav.sync()
    except Exception as e:
        # Leaves the local network — surface why rather than a silent no-op.
        messages.error(request, f"Mutual fund NAV sync failed: {e}")
        return redirect("crud_dashboard")

    cov = mutual_fund_nav.coverage()
    note = ""
    if stats["failed_types"]:
        note += f" Skipped: {', '.join(stats['failed_types'])}."
    if cov["missing"]:
        note += f" No NAV published for: {', '.join(cov['missing'])}."
    if cov["unprofiled"]:
        note += (f" {len(cov['unprofiled'])} fund(s) in the feed have no company "
                 f"profile yet: {', '.join(cov['unprofiled'][:6])}"
                 f"{'…' if len(cov['unprofiled']) > 6 else ''}.")
    messages.success(
        request,
        f"Mutual fund NAV synced — {stats['upserted']} row(s) upserted for "
        f"{stats['funds']} fund(s); {cov['with_nav']} of {cov['listed']} active "
        f"listed funds now have a NAV. Table holds {stats['total_rows']}.{note}",
    )
    return redirect("crud_dashboard")


@staff_member_required
@require_POST
def trigger_proposed_dividend_sync_view(request):
    """On-demand proposed-dividend scrape (ShareSansar), POST-only.

    Normally this rides along with the daily price sync, refreshing the two
    newest fiscal years. This endpoint exists for the cases that schedule does
    not cover: a first-time backfill of the whole history, or re-pulling one
    closed year after the source corrects it.
    """
    scope = (request.POST.get("scope") or "recent").strip()
    year = (request.POST.get("year") or "").strip()

    kwargs = {}
    if year:
        kwargs["year"] = year
    elif scope == "all":
        kwargs["all"] = True
    else:
        kwargs["years"] = 2

    try:
        stats = proposed_dividend.sync(
            years=kwargs.get("years", 2),
            all_years=kwargs.get("all", False),
            year=kwargs.get("year", ""),
        )
    except Exception as e:
        # The only sync here that leaves the local network — surface why.
        messages.error(request, f"Proposed dividend sync failed: {e}")
        return redirect("crud_dashboard")

    note = ""
    if stats["failed_years"]:
        note = f" Skipped: {', '.join(stats['failed_years'])}."
    messages.success(
        request,
        f"Proposed dividends synced — {stats['upserted']} row(s) upserted across "
        f"{len(stats['years'])} fiscal year(s); table now holds {stats['total_rows']}.{note}",
    )
    return redirect("crud_dashboard")


@staff_member_required
@require_POST
def trigger_floorsheet_sync_view(request):
    """
    On-demand floorsheet sync endpoint. POST-only so the heavy trade-level pull
    cannot be triggered by a GET. Walks the given date range day by day; an empty
    range falls back to the latest upstream trading day.
    """
    from_date_raw = (request.POST.get("from_date") or "").strip()
    to_date_raw = (request.POST.get("to_date") or "").strip()
    try:
        from_date = date.fromisoformat(from_date_raw) if from_date_raw else None
        to_date = date.fromisoformat(to_date_raw) if to_date_raw else None
    except ValueError:
        messages.error(request, "Invalid floorsheet sync date format. Use YYYY-MM-DD.")
        return redirect("crud_dashboard")
    if from_date and to_date and from_date > to_date:
        messages.error(request, "Floorsheet sync start date cannot be after the end date.")
        return redirect("crud_dashboard")
    if from_date and to_date and (to_date - from_date).days + 1 > 366:
        messages.error(request, "Floorsheet sync from the web dashboard is limited to 366 days.")
        return redirect("crud_dashboard")

    try:
        kwargs = {}
        if from_date:
            kwargs["from_date"] = from_date
        if to_date:
            kwargs["to_date"] = to_date
        call_command("sync_floorsheet", **kwargs)
        messages.success(
            request,
            f"Floorsheet sync completed (from={from_date_raw or 'latest'}, to={to_date_raw or 'latest'}).",
        )
    except Exception as e:
        messages.error(request, f"Floorsheet sync failed: {str(e)}")

    return redirect("crud_dashboard")
