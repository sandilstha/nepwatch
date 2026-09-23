"""
api_urls.py — router for the read-only Data API (mounted at /api/v1/).

Kept separate from the page-serving urls.py so the JSON API surface is easy to
see in one place and versioned independently (the /v1/ prefix lives in the
project urlconf include).
"""
from __future__ import annotations

from rest_framework.routers import DefaultRouter

from core_analysis.api_views import (
    CompanyProfileViewSet,
    FinancialStatementViewSet,
    NepseDailyStockPriceViewSet,
    NepseFloorsheetViewSet,
    NepseMarketIndexViewSet,
    StockPriceAdjustmentViewSet,
)

router = DefaultRouter()
router.register("companies", CompanyProfileViewSet, basename="company")
router.register("price-adjustments", StockPriceAdjustmentViewSet, basename="price-adjustment")
router.register("daily-prices", NepseDailyStockPriceViewSet, basename="daily-price")
router.register("indices", NepseMarketIndexViewSet, basename="index")
router.register("floorsheet", NepseFloorsheetViewSet, basename="floorsheet")
router.register("financials", FinancialStatementViewSet, basename="financial")

urlpatterns = router.urls
