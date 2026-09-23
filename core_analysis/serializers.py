"""
serializers.py — DRF serializers exposing every database model as JSON.

These back the read-only Data API (see api_views.py). They mirror the model
fields one-to-one; the only shaping is flattening the StockPriceAdjustment
``company`` foreign key down to its ``symbol`` so callers get a flat row without
an extra nested object or a second lookup.
"""
from __future__ import annotations

from rest_framework import serializers

from core_analysis.models import (
    CompanyProfile,
    FinancialStatement,
    NepseDailyStockPrice,
    NepseFloorsheet,
    NepseMarketIndex,
    StockPriceAdjustment,
)


class CompanyProfileSerializer(serializers.ModelSerializer):
    class Meta:
        model = CompanyProfile
        fields = ["symbol", "security_name", "sector_name", "status"]


class StockPriceAdjustmentSerializer(serializers.ModelSerializer):
    # `company` is a FK to CompanyProfile.symbol (DB column is `symbol`). Expose
    # it flat as `symbol` so a price row reads like a plain record, no nesting.
    symbol = serializers.CharField(source="company_id", read_only=True)

    class Meta:
        model = StockPriceAdjustment
        fields = [
            "id",
            "external_id",
            "business_date",
            "symbol",
            "security_id",
            "open_price",
            "high_price",
            "low_price",
            "close_price",
            "open_price_adj",
            "high_price_adj",
            "low_price_adj",
            "close_price_adj",
            "adjustment_factor",
            "average_traded_price_adj",
        ]


class NepseDailyStockPriceSerializer(serializers.ModelSerializer):
    class Meta:
        model = NepseDailyStockPrice
        fields = [
            "id",
            "api_id",
            "business_date",
            "security_id",
            "symbol",
            "security_name",
            "open_price",
            "high_price",
            "low_price",
            "close_price",
            "previous_close",
            "average_traded_price",
            "total_traded_quantity",
            "total_traded_value",
            "total_trades",
            "market_capitalization",
            "fifty_two_week_high",
            "fifty_two_week_low",
            "last_updated_time",
        ]


class NepseFloorsheetSerializer(serializers.ModelSerializer):
    class Meta:
        model = NepseFloorsheet
        fields = [
            "id",
            "contract_no",
            "business_date",
            "stock_symbol",
            "sector",
            "buyer",
            "seller",
            "quantity",
            "rate",
            "amount",
            "trade_time",
        ]


class FinancialStatementSerializer(serializers.ModelSerializer):
    # The two FK columns are stored as raw ids (their parent tables aren't
    # modelled here); expose them under their *_id names so the row reads flat.
    fiscal_year_bs_id = serializers.IntegerField(source="fiscal_year_bs", read_only=True)
    item_id = serializers.IntegerField(source="item", read_only=True)

    class Meta:
        model = FinancialStatement
        fields = [
            "id",
            "ticker",
            "sector",
            "fiscal_year_ad",
            "quarter",
            "data_source",
            "fs_type",
            "item_name",
            "item_code",
            "sorting_code",
            "unit",
            "amount",
            "remarks",
            "created_at",
            "fiscal_year_bs_id",
            "item_id",
        ]


class NepseMarketIndexSerializer(serializers.ModelSerializer):
    class Meta:
        model = NepseMarketIndex
        fields = [
            "id",
            "api_id",
            "business_date",
            "sector_name",
            "open_index",
            "high_index",
            "low_index",
            "close_index",
            "absolute_change",
            "percentage_change",
            "turnover_values",
            "turnover_volume",
            "total_transaction",
            "number_52_weeks_high",
            "number_52_weeks_low",
            "created_at",
        ]
