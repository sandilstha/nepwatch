"""
api_views.py — read-only Data API over every database table.

Each model is exposed as a DRF ReadOnlyModelViewSet (list + retrieve only —
the data is owned by the sync pipeline, never mutated through the API). Records
are paginated (?page=, ?page_size=) and orderable (?ordering=); the price/index
viewsets add explicit query-param filters for the columns callers actually slice
on — symbol, sector and a business_date range — implemented by hand so no extra
dependency (django-filter) is required.

Routes (wired in api_urls.py under /api/v1/):
    GET /api/v1/companies/                 list companies        (?sector=, ?status=, ?search=)
    GET /api/v1/companies/<symbol>/        one company
    GET /api/v1/price-adjustments/         adjusted price rows   (?symbol=, ?date_from=, ?date_to=)
    GET /api/v1/daily-prices/              raw daily price rows  (?symbol=, ?date_from=, ?date_to=)
    GET /api/v1/indices/                   index rows            (?sector=, ?date_from=, ?date_to=)
    GET /api/v1/floorsheet/                trade-level rows      (?symbol=, ?sector=, ?buyer=, ?seller=, ?date_from=, ?date_to=)
    GET /api/v1/floorsheet/<id>/           one trade
    GET /api/v1/financials/                financial-stmt rows   (?ticker=, ?fiscal_year=, ?quarter=, ?fs_type=, ?sector=, ?data_source=, ?item_code=, ?search=)
    GET /api/v1/financials/<id>/           one statement line
"""
from __future__ import annotations

import re
from datetime import date

from rest_framework import viewsets
from rest_framework.exceptions import ValidationError
from rest_framework.filters import OrderingFilter, SearchFilter
from rest_framework.pagination import PageNumberPagination

from core_analysis.models import (
    CompanyProfile,
    FinancialStatement,
    NepseDailyStockPrice,
    NepseFloorsheet,
    NepseMarketIndex,
    StockPriceAdjustment,
)
from core_analysis.serializers import (
    CompanyProfileSerializer,
    FinancialStatementSerializer,
    NepseDailyStockPriceSerializer,
    NepseFloorsheetSerializer,
    NepseMarketIndexSerializer,
    StockPriceAdjustmentSerializer,
)


class StandardPagination(PageNumberPagination):
    """Page-number pagination with a caller-tunable, hard-capped page size.

    The price/index tables hold years of daily rows, so an unbounded list would
    be enormous. Default 100 per page; callers may raise it with ?page_size= up
    to a 1000 ceiling so a runaway value can't pull the whole table into memory.
    """

    page_size = 100
    page_size_query_param = "page_size"
    max_page_size = 1000


def _parse_date(raw, param_name):
    """Parse an ISO YYYY-MM-DD query param, raising a clean 400 on bad input."""
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        raise ValidationError({param_name: "Expected a date in YYYY-MM-DD format."})


class _DateRangeFilterMixin:
    """Apply ?date_from= / ?date_to= (inclusive) on the model's date field."""

    date_field = "business_date"

    def filter_date_range(self, queryset):
        params = self.request.query_params
        date_from = _parse_date(params.get("date_from"), "date_from")
        date_to = _parse_date(params.get("date_to"), "date_to")
        if date_from and date_to and date_from > date_to:
            raise ValidationError({"date_from": "Must not be after date_to."})
        if date_from:
            queryset = queryset.filter(**{f"{self.date_field}__gte": date_from})
        if date_to:
            queryset = queryset.filter(**{f"{self.date_field}__lte": date_to})
        return queryset


class CompanyProfileViewSet(viewsets.ReadOnlyModelViewSet):
    """Listed companies. Filter with ?sector= / ?status=, free-text ?search=."""

    serializer_class = CompanyProfileSerializer
    pagination_class = StandardPagination
    filter_backends = [SearchFilter, OrderingFilter]
    search_fields = ["symbol", "security_name"]
    ordering_fields = ["symbol", "security_name", "sector_name", "status"]
    ordering = ["symbol"]
    lookup_field = "symbol"
    # Symbols can contain characters the default \w+ path converter rejects on
    # lookup; allow anything non-slash so /companies/<symbol>/ always resolves.
    lookup_value_regex = "[^/]+"

    def get_queryset(self):
        qs = CompanyProfile.objects.all()
        params = self.request.query_params
        sector = (params.get("sector") or "").strip()
        status = (params.get("status") or "").strip()
        if sector:
            qs = qs.filter(sector_name__iexact=sector)
        if status:
            qs = qs.filter(status__iexact=status)
        return qs


class StockPriceAdjustmentViewSet(_DateRangeFilterMixin, viewsets.ReadOnlyModelViewSet):
    """Corporate-action-adjusted daily prices. ?symbol=, ?date_from=, ?date_to=."""

    serializer_class = StockPriceAdjustmentSerializer
    pagination_class = StandardPagination
    filter_backends = [OrderingFilter]
    ordering_fields = ["business_date", "company"]
    ordering = ["-business_date"]

    def get_queryset(self):
        qs = StockPriceAdjustment.objects.select_related("company")
        symbol = (self.request.query_params.get("symbol") or "").strip().upper()
        if symbol:
            qs = qs.filter(company_id=symbol)
        return self.filter_date_range(qs)


class NepseDailyStockPriceViewSet(_DateRangeFilterMixin, viewsets.ReadOnlyModelViewSet):
    """Raw daily transaction rows. ?symbol=, ?date_from=, ?date_to=."""

    serializer_class = NepseDailyStockPriceSerializer
    pagination_class = StandardPagination
    filter_backends = [OrderingFilter]
    ordering_fields = ["business_date", "symbol", "total_traded_value"]
    ordering = ["-business_date"]

    def get_queryset(self):
        qs = NepseDailyStockPrice.objects.all()
        symbol = (self.request.query_params.get("symbol") or "").strip().upper()
        if symbol:
            qs = qs.filter(symbol=symbol)
        return self.filter_date_range(qs)


class NepseMarketIndexViewSet(_DateRangeFilterMixin, viewsets.ReadOnlyModelViewSet):
    """Sector / macro index rows. ?sector=, ?date_from=, ?date_to=."""

    serializer_class = NepseMarketIndexSerializer
    pagination_class = StandardPagination
    filter_backends = [OrderingFilter]
    ordering_fields = ["business_date", "sector_name"]
    ordering = ["-business_date"]

    def get_queryset(self):
        qs = NepseMarketIndex.objects.all()
        sector = (self.request.query_params.get("sector") or "").strip().upper()
        if sector:
            qs = qs.filter(sector_name__iexact=sector)
        return self.filter_date_range(qs)


def _parse_int(raw, param_name):
    """Parse an integer query param, raising a clean 400 on bad input."""
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        raise ValidationError({param_name: "Expected an integer."})


class NepseFloorsheetViewSet(_DateRangeFilterMixin, viewsets.ReadOnlyModelViewSet):
    """Trade-level floorsheet rows. ?symbol=, ?sector=, ?buyer=, ?seller=, ?date_from=, ?date_to=.

    This is a very large table (one row per executed trade), so list requests must
    provide an indexed entity filter (?symbol / ?sector / ?buyer / ?seller) or a
    bounded date_from + date_to window. Pagination is still hard-capped per page.
    """

    serializer_class = NepseFloorsheetSerializer
    pagination_class = StandardPagination
    filter_backends = [OrderingFilter]
    ordering_fields = ["business_date", "stock_symbol", "amount", "quantity", "trade_time"]
    ordering = ["-business_date", "stock_symbol"]

    def list(self, request, *args, **kwargs):
        """Require an indexed slice instead of counting the entire trade table."""
        params = request.query_params
        entity_filters = ("symbol", "sector", "buyer", "seller")
        if not any((params.get(name) or "").strip() for name in entity_filters):
            date_from = _parse_date(params.get("date_from"), "date_from")
            date_to = _parse_date(params.get("date_to"), "date_to")
            if not date_from or not date_to:
                raise ValidationError({
                    "filters": (
                        "Provide symbol, sector, buyer or seller; otherwise provide both "
                        "date_from and date_to."
                    )
                })
            if date_from > date_to:
                raise ValidationError({"date_from": "Must not be after date_to."})
            if (date_to - date_from).days > 31:
                raise ValidationError({
                    "date_to": "Unscoped floorsheet date windows cannot exceed 32 days."
                })
        return super().list(request, *args, **kwargs)

    def get_queryset(self):
        qs = NepseFloorsheet.objects.all()
        params = self.request.query_params
        symbol = (params.get("symbol") or "").strip().upper()
        sector = (params.get("sector") or "").strip()
        buyer = _parse_int(params.get("buyer"), "buyer")
        seller = _parse_int(params.get("seller"), "seller")
        if symbol and not re.fullmatch(r"[A-Z0-9._-]{1,50}", symbol):
            raise ValidationError({"symbol": "Expected a valid 1-50 character ticker."})
        if len(sector) > 100:
            raise ValidationError({"sector": "Must be 100 characters or fewer."})
        if buyer is not None and buyer <= 0:
            raise ValidationError({"buyer": "Expected a positive broker number."})
        if seller is not None and seller <= 0:
            raise ValidationError({"seller": "Expected a positive broker number."})
        if symbol:
            qs = qs.filter(stock_symbol=symbol)
        if sector:
            qs = qs.filter(sector__iexact=sector)
        if buyer is not None:
            qs = qs.filter(buyer=buyer)
        if seller is not None:
            qs = qs.filter(seller=seller)
        return self.filter_date_range(qs)


class FinancialStatementViewSet(viewsets.ReadOnlyModelViewSet):
    """Company financial-statement line items (fundamentals_financialstatdbs).

    Filters: ?ticker=, ?fiscal_year= (e.g. 2024/25), ?quarter= (0 annual / 1–4),
    ?fs_type= (BS / PL / CF…), ?sector=, ?data_source=, ?item_code=, plus free-text
    ?search= over item name / code / ticker.

    This is a large table (~1M rows), so a ?ticker filter — ideally narrowed
    further by ?fiscal_year / ?fs_type — is strongly recommended; pagination
    otherwise walks the whole table page by page.
    """

    serializer_class = FinancialStatementSerializer
    pagination_class = StandardPagination
    filter_backends = [SearchFilter, OrderingFilter]
    search_fields = ["item_name", "item_code", "ticker"]
    ordering_fields = [
        "ticker", "fiscal_year_ad", "quarter", "sorting_code", "fs_type", "amount", "created_at",
    ]
    ordering = ["ticker", "fiscal_year_ad", "quarter", "sorting_code"]

    def get_queryset(self):
        qs = FinancialStatement.objects.all()
        params = self.request.query_params
        ticker = (params.get("ticker") or "").strip().upper()
        sector = (params.get("sector") or "").strip()
        # Accept either the friendly ?fiscal_year= or the raw column name.
        fiscal_year = (params.get("fiscal_year") or params.get("fiscal_year_ad") or "").strip()
        fs_type = (params.get("fs_type") or "").strip()
        data_source = (params.get("data_source") or "").strip()
        item_code = (params.get("item_code") or "").strip()
        quarter = _parse_int(params.get("quarter"), "quarter")
        if ticker:
            qs = qs.filter(ticker=ticker)
        if sector:
            qs = qs.filter(sector__iexact=sector)
        if fiscal_year:
            qs = qs.filter(fiscal_year_ad=fiscal_year)
        if fs_type:
            qs = qs.filter(fs_type__iexact=fs_type)
        if data_source:
            qs = qs.filter(data_source__iexact=data_source)
        if item_code:
            qs = qs.filter(item_code=item_code)
        if quarter is not None:
            qs = qs.filter(quarter=quarter)
        return qs
