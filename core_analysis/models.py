from django.conf import settings
from django.db import models
from django.utils import timezone

class Broker(models.Model):
    """
    Table: nepse_brokers
    Reference list of NEPSE stock brokers (and stock dealers), keyed by the
    broker number that appears as ``buyer`` / ``seller`` in the floorsheet feed.
    Used to resolve a broker number to a human-readable name across the broker
    analytics dashboard (Floor sheet page).
    """
    broker_number = models.IntegerField(
        primary_key=True, help_text="NEPSE broker code; matches floorsheet buyer/seller"
    )
    name = models.CharField(max_length=255)
    contact_person = models.CharField(max_length=255, null=True, blank=True)
    contact_number = models.CharField(max_length=255, null=True, blank=True)
    status = models.CharField(max_length=20, default="ACTIVE", db_index=True)
    tms_link = models.CharField(max_length=255, null=True, blank=True)
    is_dealer = models.BooleanField(
        default=False, help_text="True if the firm also operates as a NEPSE stock dealer"
    )

    class Meta:
        db_table = 'nepse_brokers'
        ordering = ['broker_number']

    def __str__(self):
        return f"{self.broker_number} - {self.name}"


class CompanyProfile(models.Model):
    """
    Table: nepse_company_profiles
    Stores the unique list of companies from the /api/listed-companies/companies/ endpoint.
    """
    symbol = models.CharField(max_length=20, primary_key=True, db_index=True, help_text="Maps to script_ticker")
    security_name = models.CharField(max_length=255, help_text="Maps to company_name")
    sector_name = models.CharField(max_length=100, null=True, blank=True, help_text="Maps to sector")
    status = models.CharField(max_length=50, default="Active")

    class Meta:
        db_table = 'nepse_company_profiles'
        ordering = ['symbol']

    def __str__(self):
        return f"{self.symbol} - {self.security_name}"


class StockPriceAdjustment(models.Model):
    """
    Table: nepse_todayprice_adj
    Stores the daily historical pricing rows from the /api/stock-adjustments/stock-price-adj/ endpoint.
    """
    external_id = models.IntegerField(unique=True, help_text="The raw ID from the source API")
    business_date = models.DateField(db_index=True)
    
    # Foreign Key relationship mapping straight to the CompanyProfile table via its unique symbol
    # Django will treat 'company' as the object, but MySQL will name the actual column 'symbol'
    company = models.ForeignKey(CompanyProfile, on_delete=models.CASCADE, to_field='symbol', db_column='symbol')
    security_id = models.IntegerField()
    
    # Raw Market Prices
    open_price = models.DecimalField(max_digits=12, decimal_places=2)
    high_price = models.DecimalField(max_digits=12, decimal_places=2)
    low_price = models.DecimalField(max_digits=12, decimal_places=2)
    close_price = models.DecimalField(max_digits=12, decimal_places=2)
    
    # Corporate Action Adjusted Prices
    open_price_adj = models.DecimalField(max_digits=12, decimal_places=2)
    high_price_adj = models.DecimalField(max_digits=12, decimal_places=2)
    low_price_adj = models.DecimalField(max_digits=12, decimal_places=2)
    close_price_adj = models.DecimalField(max_digits=12, decimal_places=2)
    adjustment_factor = models.DecimalField(max_digits=14, decimal_places=10)
    average_traded_price_adj = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)

    class Meta:
        db_table = 'nepse_todayprice_adj'
        ordering = ['-business_date', 'company'] # Changed from symbol to company
        
        # Composite unique constraint using the linked company relationship field
        unique_together = ('business_date', 'company')

    def __str__(self):
        return f"{self.company_id} - {self.business_date}"
    

class NepseDailyStockPrice(models.Model):
    """
    Table: nepse_daily_stock_prices
    Stores the raw daily transaction data rows from /api/stock-prices/
    """
    api_id = models.IntegerField(unique=True, help_text="Maps to JSON 'id'")
    business_date = models.DateField(db_index=True)
    security_id = models.CharField(max_length=20)
    symbol = models.CharField(max_length=20, db_index=True)
    security_name = models.CharField(max_length=255)
    
    # Pricing Matrix
    open_price = models.DecimalField(max_digits=12, decimal_places=2)
    high_price = models.DecimalField(max_digits=12, decimal_places=2)
    low_price = models.DecimalField(max_digits=12, decimal_places=2)
    close_price = models.DecimalField(max_digits=12, decimal_places=2)
    previous_close = models.DecimalField(max_digits=12, decimal_places=2)
    average_traded_price = models.DecimalField(max_digits=12, decimal_places=2)
    
    # Volumetric Data
    total_traded_quantity = models.BigIntegerField()
    total_traded_value = models.DecimalField(max_digits=16, decimal_places=2)
    total_trades = models.IntegerField()
    market_capitalization = models.DecimalField(max_digits=16, decimal_places=2)
    
    # 52 Week Ranges
    fifty_two_week_high = models.DecimalField(max_digits=12, decimal_places=2)
    fifty_two_week_low = models.DecimalField(max_digits=12, decimal_places=2)
    
    last_updated_time = models.DateTimeField()

    class Meta:
        db_table = 'nepse_daily_stock_prices'
        ordering = ['-business_date', 'symbol']
        unique_together = ('business_date', 'symbol')

    def __str__(self):
        return f"{self.symbol} - {self.business_date}"


class NepseMarketIndex(models.Model):
    """
    Table: nepse_market_indices
    Stores historical daily sector and macro index data rows from /api/indices/
    """
    api_id = models.IntegerField(unique=True, help_text="Maps to JSON 'id'")
    business_date = models.DateField(db_index=True, help_text="Maps to JSON 'date'")
    sector_name = models.CharField(max_length=100, db_index=True, help_text="Maps to JSON 'sector'")
    
    # Index Coordinates
    open_index = models.DecimalField(max_digits=12, decimal_places=2, help_text="Maps to JSON 'open'")
    high_index = models.DecimalField(max_digits=12, decimal_places=2, help_text="Maps to JSON 'high'")
    low_index = models.DecimalField(max_digits=12, decimal_places=2, help_text="Maps to JSON 'low'")
    close_index = models.DecimalField(max_digits=12, decimal_places=2, help_text="Maps to JSON 'close'")
    
    # Variations
    absolute_change = models.DecimalField(max_digits=12, decimal_places=2)
    percentage_change = models.DecimalField(max_digits=6, decimal_places=4)
    
    # Volumetric Fields
    turnover_values = models.DecimalField(max_digits=18, decimal_places=2)
    turnover_volume = models.BigIntegerField()
    total_transaction = models.IntegerField()
    
    # 52 Week Ranges
    number_52_weeks_high = models.DecimalField(max_digits=12, decimal_places=2)
    number_52_weeks_low = models.DecimalField(max_digits=12, decimal_places=2)
    
    created_at = models.DateTimeField()

    class Meta:
        db_table = 'nepse_market_indices'
        ordering = ['-business_date', 'sector_name']
        unique_together = ('business_date', 'sector_name')

    def __str__(self):
        return f"{self.sector_name} - {self.business_date}"


class NepseFloorsheet(models.Model):
    """
    Table: floorsheet_raw
    Stores trade-level floorsheet rows (one row per executed trade) from
    /api/nepse-data/api/floorsheet/. This is a high-volume table — tens of
    millions of rows — so it is synced day-by-day filtered on calculation_date.

    The upstream JSON 'id' is used directly as this table's primary key (it is a
    stable, globally-unique trade id), so there is no separate surrogate key and
    no `api_id` column. Most trade-economics columns are nullable to mirror the
    raw feed, which occasionally omits them.
    """
    # Source 'id' is the primary key (not auto-generated locally).
    id = models.BigIntegerField(primary_key=True, help_text="Maps to JSON 'id'")
    contract_no = models.CharField(
        max_length=255, null=True, blank=True, db_index=True, help_text="Maps to JSON 'contract_no'"
    )
    stock_symbol = models.CharField(max_length=50, db_index=True)

    # Counterparties (broker numbers).
    buyer = models.IntegerField(null=True, blank=True, db_index=True)
    seller = models.IntegerField(null=True, blank=True, db_index=True)

    # Trade economics.
    quantity = models.IntegerField(null=True, blank=True)
    rate = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    amount = models.DecimalField(max_digits=15, decimal_places=2, null=True, blank=True)

    sector = models.CharField(max_length=100, null=True, blank=True, db_index=True)

    # DB column is `calculation_date`; kept as `business_date` in Python for
    # parity with the other NEPSE models and the analytics layer.
    business_date = models.DateField(
        db_column='calculation_date', db_index=True, help_text="Maps to JSON 'calculation_date'"
    )

    # Execution clock time (HH:MM:SS.ffffff), nullable for malformed rows.
    trade_time = models.TimeField(
        db_column='time', null=True, blank=True, help_text="Maps to JSON 'time'"
    )

    class Meta:
        db_table = 'floorsheet_raw'
        ordering = ['-business_date', 'stock_symbol']
        indexes = [
            models.Index(fields=['stock_symbol', 'business_date']),
            models.Index(fields=['business_date', 'buyer']),
            models.Index(fields=['business_date', 'seller']),
            models.Index(fields=['business_date', 'sector']),
        ]

    def __str__(self):
        return f"{self.contract_no} - {self.stock_symbol} - {self.business_date}"


class FinancialStatement(models.Model):
    """
    Table: fundamentals_financialstatdbs (read-only mapping).

    Company financial-statement line items (one row per
    ticker × fiscal year × quarter × statement type × item), as harvested by the
    separate ``fundamentals`` app that owns this table. We map it here only to
    *read* fundamentals alongside the price/floorsheet data — hence
    ``managed = False`` so Django never creates, alters or drops it, and the two
    foreign-key columns are mapped as plain integer ids (their parent tables,
    ``nepali_datetime_fiscalyear`` and ``fundamentals_accountdictionary``, are not
    modelled in this project).
    """
    id = models.BigAutoField(primary_key=True)

    # Identity / classification.
    sector = models.CharField(max_length=100, db_index=True)
    fiscal_year_ad = models.CharField(
        max_length=10, db_index=True, help_text="Gregorian fiscal year label, e.g. '2024/25'"
    )
    quarter = models.PositiveSmallIntegerField(db_index=True, help_text="0 = annual / 1–4 = quarter")
    data_source = models.CharField(max_length=20, db_index=True)
    ticker = models.CharField(max_length=20, db_index=True)
    fs_type = models.CharField(
        max_length=10, db_index=True, help_text="Statement type, e.g. BS / PL / CF"
    )

    # Line item.
    item_name = models.CharField(max_length=255)
    item_code = models.CharField(max_length=80, db_index=True)
    sorting_code = models.CharField(max_length=20, db_index=True)
    unit = models.CharField(max_length=10)
    amount = models.DecimalField(max_digits=20, decimal_places=4)
    remarks = models.CharField(max_length=255, blank=True, default="")

    created_at = models.DateTimeField()

    # Foreign-key columns from the source schema, kept as raw ids (their parent
    # tables live in other apps and aren't modelled here).
    fiscal_year_bs = models.BigIntegerField(
        db_column="fiscal_year_bs_id",
        help_text="FK id -> nepali_datetime_fiscalyear.id (not modelled here)",
    )
    item = models.BigIntegerField(
        db_column="item_id",
        help_text="FK id -> fundamentals_accountdictionary.id (not modelled here)",
    )

    class Meta:
        db_table = "fundamentals_financialstatdbs"
        managed = False  # owned by the `fundamentals` app; never migrate it here
        ordering = ["ticker", "fiscal_year_ad", "quarter", "sorting_code"]
        # Mirrors the source table's natural key (financialstatdbs_unique_row).
        unique_together = (
            ("sector", "fiscal_year_ad", "quarter", "data_source", "ticker", "fs_type", "item_code"),
        )

    def __str__(self):
        return f"{self.ticker} {self.fiscal_year_ad} Q{self.quarter} {self.fs_type} {self.item_code}"


class Portfolio(models.Model):
    """
    Table: portfolio_portfolio
    A logged-in user's private holdings portfolio — typically imported from a
    Meroshare "My Shares" CSV. One user may keep several named portfolios. Risk /
    valuation analytics are derived on the fly from the linked NEPSE EOD prices,
    so only the positions live here.
    """
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="portfolios"
    )
    PERSONAL = "personal"
    SPOUSE = "spouse"
    PARENTS = "parents"
    CHILDREN = "children"
    JOINT = "joint"
    CLIENT = "client"
    CUSTOM = "custom"
    TYPE_CHOICES = (
        (PERSONAL, "Personal"),
        (SPOUSE, "Spouse"),
        (PARENTS, "Parents"),
        (CHILDREN, "Children"),
        (JOINT, "Joint"),
        (CLIENT, "Client"),
        (CUSTOM, "Custom"),
    )
    name = models.CharField(max_length=120, default="My Portfolio")
    portfolio_type = models.CharField(
        max_length=20, choices=TYPE_CHOICES, default=PERSONAL, db_index=True
    )
    is_default = models.BooleanField(default=False, db_index=True)
    is_archived = models.BooleanField(default=False, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "portfolio_portfolio"
        ordering = ["is_archived", "-is_default", "-updated_at"]
        unique_together = (("user", "name"),)
        indexes = [
            models.Index(fields=["user", "is_archived", "is_default"]),
        ]

    def __str__(self):
        return f"{self.name} (user {self.user_id})"

    def save(self, *args, **kwargs):
        if self.is_archived:
            self.is_default = False
            update_fields = kwargs.get("update_fields")
            if update_fields is not None:
                kwargs["update_fields"] = set(update_fields) | {"is_default"}
        super().save(*args, **kwargs)
        if self.is_default:
            Portfolio.objects.filter(user_id=self.user_id, is_default=True).exclude(pk=self.pk).update(
                is_default=False
            )


class Holding(models.Model):
    """
    Table: portfolio_holding
    One scrip position inside a portfolio. ``quantity`` is the demat balance; the
    two price columns are the *snapshot* the CSV was exported with (kept for
    reference only). Live valuation always re-prices against the latest
    ``NepseDailyStockPrice`` close so every holding is marked to the same session.
    """
    portfolio = models.ForeignKey(
        Portfolio, on_delete=models.CASCADE, related_name="holdings"
    )
    symbol = models.CharField(max_length=20, db_index=True)
    quantity = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    # Import-time snapshot prices (informational; not used for live valuation).
    last_close = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    ltp = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "portfolio_holding"
        ordering = ["symbol"]
        unique_together = (("portfolio", "symbol"),)

    def __str__(self):
        return f"{self.symbol} x{self.quantity}"


class HoldingCost(models.Model):
    """
    Table: portfolio_holding_cost
    Cost basis (WACC) for one scrip, imported from the broker "My WACC" report
    (Sani Securities / any TMS — CSV, Excel or PDF). Kept in its OWN table rather
    than on ``Holding`` so re-importing the Meroshare "My Shares" CSV — which
    replaces every ``Holding`` — never wipes the cost basis. Joined to holdings
    by ``symbol`` at valuation time to derive book value & paper P/L.
    """
    portfolio = models.ForeignKey(
        Portfolio, on_delete=models.CASCADE, related_name="costs"
    )
    symbol = models.CharField(max_length=20, db_index=True)
    # Weighted-average cost per share, and the report's own quantity / total cost
    # (informational; live book value is wacc_rate × the current demat balance).
    wacc_rate = models.DecimalField(max_digits=14, decimal_places=4, null=True, blank=True)
    quantity = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    total_cost = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
    modified = models.CharField(max_length=32, blank=True, default="")  # report's "Last Modification Date"
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "portfolio_holding_cost"
        ordering = ["symbol"]
        unique_together = (("portfolio", "symbol"),)

    def __str__(self):
        return f"{self.symbol} @ {self.wacc_rate}"


class PortfolioSnapshot(models.Model):
    """
    Table: portfolio_snapshot
    What one portfolio was actually worth at the close of one session.

    Why this exists: ``Holding`` keeps only the CURRENT quantity, and its
    ``updated_at`` is ``auto_now`` — so it is overwritten on every sync and the
    portfolio has no memory of itself. Every metric in ``portfolio_analytics``
    therefore reconstructs history by applying TODAY's weights backwards over
    past prices, which answers "how would this basket have behaved" rather than
    "how did my portfolio behave". A name bought last week inherits its full
    two-year drawdown. Risk numbers survive that simplification; performance
    numbers (equity curve, Sharpe, tracking error) do not, which is why they
    were never built.

    One row per portfolio per session, written by the EOD price sync. ``weights``
    stores the per-symbol {symbol: {"qty", "price", "value"}} breakdown, so a
    later reconstruction can recompute true historical weights without needing
    a row per holding per day.
    """
    portfolio = models.ForeignKey(
        Portfolio, on_delete=models.CASCADE, related_name="snapshots"
    )
    # The NEPSE trading session this values against — NOT the write time, so a
    # late or re-run sync lands on the session it priced, and re-running is an
    # idempotent overwrite rather than a duplicate.
    business_date = models.DateField(db_index=True)
    total_value = models.DecimalField(max_digits=20, decimal_places=2, default=0)
    total_cost = models.DecimalField(max_digits=20, decimal_places=2, null=True, blank=True)
    holdings_count = models.PositiveIntegerField(default=0)
    # {symbol: {"qty": float, "price": float, "value": float}}
    weights = models.JSONField(default=dict, blank=True)
    # "sync" when written by the EOD hook, "ledger" when reconstructed from an
    # imported broker statement — reconstructed history is an inference and a
    # chart should be able to say so.
    SOURCE_SYNC = "sync"
    SOURCE_LEDGER = "ledger"
    SOURCE_CHOICES = ((SOURCE_SYNC, "EOD sync"), (SOURCE_LEDGER, "Ledger reconstruction"))
    source = models.CharField(
        max_length=12, choices=SOURCE_CHOICES, default=SOURCE_SYNC, db_index=True
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "portfolio_snapshot"
        ordering = ["-business_date"]
        unique_together = (("portfolio", "business_date"),)
        indexes = [
            models.Index(fields=["portfolio", "business_date"]),
        ]

    def __str__(self):
        return f"{self.portfolio_id} @ {self.business_date}: {self.total_value}"


class BrokerLedgerImport(models.Model):
    """One uploaded broker account-statement file and its import audit trail."""

    portfolio = models.ForeignKey(
        Portfolio, on_delete=models.CASCADE, related_name="ledger_imports"
    )
    source_name = models.CharField(max_length=255)
    file_sha256 = models.CharField(max_length=64, db_index=True)
    account_name = models.CharField(max_length=160, blank=True, default="")
    account_code = models.CharField(max_length=80, blank=True, default="")
    report_from_ad = models.DateField(null=True, blank=True)
    report_to_ad = models.DateField(null=True, blank=True)
    source_fiscal_year = models.CharField(max_length=20, blank=True, default="")
    imported_rows = models.PositiveIntegerField(default=0)
    duplicate_rows = models.PositiveIntegerField(default=0)
    warning_count = models.PositiveIntegerField(default=0)
    imported_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "portfolio_ledger_import"
        ordering = ["-imported_at", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["portfolio", "file_sha256"],
                name="uniq_pf_ledger_file",
            ),
        ]

    def __str__(self):
        return f"{self.portfolio}: {self.source_name}"


class BrokerLedgerTransaction(models.Model):
    """A normalized cash-ledger row from a private broker account statement."""

    OPENING = "opening"
    DEPOSIT = "deposit"
    BUY = "buy"
    SELL = "sell"
    PAYMENT = "payment"
    DIVIDEND = "dividend"
    CHARGE = "charge"
    TAX = "tax"
    ADJUSTMENT = "adjustment"
    OTHER = "other"
    TYPE_CHOICES = (
        (OPENING, "Opening balance"),
        (DEPOSIT, "Deposit / receipt"),
        (BUY, "Purchase bill"),
        (SELL, "Sale bill"),
        (PAYMENT, "Payment / withdrawal"),
        (DIVIDEND, "Dividend"),
        (CHARGE, "Charge"),
        (TAX, "Tax"),
        (ADJUSTMENT, "Adjustment"),
        (OTHER, "Other"),
    )
    CR = "CR"
    DR = "DR"
    BALANCE_SIDE_CHOICES = ((CR, "Credit"), (DR, "Debit"))

    portfolio = models.ForeignKey(
        Portfolio, on_delete=models.CASCADE, related_name="ledger_transactions"
    )
    import_batch = models.ForeignKey(
        BrokerLedgerImport, on_delete=models.CASCADE, related_name="transactions"
    )
    source_row_no = models.PositiveIntegerField(null=True, blank=True)
    date_ad = models.DateField(null=True, blank=True, db_index=True)
    date_bs = models.CharField(max_length=10, blank=True, default="")
    fiscal_year = models.CharField(max_length=10, blank=True, default="", db_index=True)
    voucher_no = models.CharField(max_length=80, blank=True, default="")
    transaction_type = models.CharField(
        max_length=16, choices=TYPE_CHOICES, default=OTHER, db_index=True
    )
    particulars = models.TextField(blank=True, default="")
    reference_no = models.CharField(max_length=100, blank=True, default="")
    branch = models.CharField(max_length=40, blank=True, default="")
    debit = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    credit = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    balance = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    balance_side = models.CharField(
        max_length=2, choices=BALANCE_SIDE_CHOICES, blank=True, default=""
    )
    derived_deductions = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    fingerprint = models.CharField(max_length=64)
    sequence_gap = models.BooleanField(default=False)
    balance_mismatch = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "portfolio_ledger_transaction"
        ordering = ["date_ad", "source_row_no", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["portfolio", "fingerprint"],
                name="uniq_pf_ledger_tx",
            ),
            models.CheckConstraint(
                condition=models.Q(debit__gte=0), name="ledger_debit_nonneg"
            ),
            models.CheckConstraint(
                condition=models.Q(credit__gte=0), name="ledger_credit_nonneg"
            ),
            models.CheckConstraint(
                condition=models.Q(derived_deductions__gte=0),
                name="ledger_deduct_nonneg",
            ),
        ]
        indexes = [
            models.Index(fields=["portfolio", "date_ad"], name="pf_ledger_date_idx"),
            models.Index(
                fields=["portfolio", "fiscal_year", "transaction_type"],
                name="pf_ledger_fy_type_idx",
            ),
        ]

    def __str__(self):
        return f"{self.date_ad or 'Opening'} {self.get_transaction_type_display()}"


class BrokerTrade(models.Model):
    """One symbol/quantity/price leg parsed from a broker purchase or sale bill."""

    BUY = "buy"
    SELL = "sell"
    SIDE_CHOICES = ((BUY, "Buy"), (SELL, "Sell"))

    transaction = models.ForeignKey(
        BrokerLedgerTransaction, on_delete=models.CASCADE, related_name="trades"
    )
    symbol = models.CharField(max_length=20, db_index=True)
    side = models.CharField(max_length=4, choices=SIDE_CHOICES)
    quantity = models.DecimalField(max_digits=18, decimal_places=2)
    price = models.DecimalField(max_digits=14, decimal_places=4)
    gross_amount = models.DecimalField(max_digits=18, decimal_places=2)
    allocated_deductions = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    net_amount = models.DecimalField(max_digits=18, decimal_places=2)

    class Meta:
        db_table = "portfolio_broker_trade"
        ordering = ["transaction_id", "id"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(quantity__gt=0), name="broker_trade_qty_pos"
            ),
            models.CheckConstraint(
                condition=models.Q(price__gte=0), name="broker_trade_price_nonneg"
            ),
            models.CheckConstraint(
                condition=models.Q(gross_amount__gte=0), name="broker_trade_gross_nonneg"
            ),
            models.CheckConstraint(
                condition=models.Q(allocated_deductions__gte=0),
                name="broker_trade_deduct_nonneg",
            ),
        ]

    def __str__(self):
        return f"{self.side.upper()} {self.symbol} x{self.quantity}"


class PageVisit(models.Model):
    """
    Table: site_page_visit
    One row per page view, written by ``VisitTrackingMiddleware`` on every HTML
    page load. This is the self-hosted alternative to Google Analytics — it works
    on an offline / air-gapped LAN because nothing leaves the server. The /stats/
    dashboard rolls these rows up into visit / unique-visitor / top-page counts.

    Only real page navigations are stored (GET, text/html, HTTP 200); static
    files, the admin, JSON API polls and AJAX requests are filtered out by the
    middleware so the table isn't flooded by the dashboards' auto-refresh.
    """
    path = models.CharField(max_length=300, db_index=True)
    method = models.CharField(max_length=8, default="GET")
    status_code = models.PositiveSmallIntegerField(default=200)
    # Client IP is the unique-visitor key on a LAN (each device has one). Nullable
    # because a misconfigured proxy can hide it.
    ip_address = models.GenericIPAddressField(null=True, blank=True, db_index=True)
    session_key = models.CharField(max_length=40, blank=True, default="", db_index=True)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="page_visits",
    )
    user_agent = models.CharField(max_length=400, blank=True, default="")
    referer = models.CharField(max_length=400, blank=True, default="")
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        db_table = "site_page_visit"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["created_at", "path"]),
        ]

    def __str__(self):
        return f"{self.path} @ {self.created_at:%Y-%m-%d %H:%M} ({self.ip_address or '?'})"


class AccountApproval(models.Model):
    """Admin review state for a self-service portfolio account request."""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    STATUS_CHOICES = (
        (PENDING, "Pending"),
        (APPROVED, "Approved"),
        (REJECTED, "Rejected"),
    )

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="account_approval"
    )
    contact_email = models.EmailField(unique=True)
    status = models.CharField(
        max_length=20, choices=STATUS_CHOICES, default=PENDING, db_index=True
    )
    review_note = models.CharField(max_length=255, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="reviewed_account_approvals",
    )

    class Meta:
        db_table = "account_approval_request"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.contact_email} ({self.get_status_display()})"

    @property
    def is_pending(self):
        return self.status == self.PENDING

    # Called by the admin actions and the approvals API. These were missing,
    # so every approve/reject raised AttributeError (the tests caught it).
    def _review(self, status, reviewer=None, note=""):
        self.status = status
        self.reviewed_by = reviewer
        self.reviewed_at = timezone.now()
        if note:
            self.review_note = note[:255]
        self.save(update_fields=["status", "reviewed_by", "reviewed_at", "review_note"])

    def approve(self, reviewer=None, note=""):
        self._review(self.APPROVED, reviewer, note)
        if not self.user.is_active:
            self.user.is_active = True
            self.user.save(update_fields=["is_active"])

    def reject(self, reviewer=None, note=""):
        self._review(self.REJECTED, reviewer, note)
        if self.user.is_active:
            self.user.is_active = False
            self.user.save(update_fields=["is_active"])


class MarginEligibleCompany(models.Model):
    """
    Table: margin_eligible_companies

    The regulator-/broker-published list of securities eligible for margin
    lending. Owned by THIS app (managed), so it is maintained entirely from the
    Django admin — add, remove or update entries without any code change.

    Design notes (future-proofing):
      * ``symbol`` is the natural key (unique); a company keeps ONE row across
        list revisions.
      * Removal is a soft toggle (``is_eligible = False``) rather than a delete,
        so history/audit survives and re-listing is a one-click flip.
      * Margin specifics that vary per revision (loan-to-value %, risk group,
        haircut) each get a typed, nullable column — populate when known,
        leave null otherwise.
      * ``metadata`` (JSON) absorbs any attribute added by a future list format
        without a migration, so the app logic never has to change to store more.
    """
    symbol = models.CharField(
        max_length=20, unique=True, db_index=True,
        help_text="Trading symbol / ticker, e.g. NABIL (the natural key).",
    )
    company_name = models.CharField(max_length=255, blank=True, default="")
    sector = models.CharField(max_length=100, blank=True, default="", db_index=True)

    # Soft add/remove: flip instead of deleting so the list stays auditable.
    is_eligible = models.BooleanField(
        default=True, db_index=True,
        help_text="Untick to de-list without losing the record.",
    )

    # Margin specifics — all nullable; fill in as the published list provides them.
    margin_rate = models.DecimalField(
        max_digits=6, decimal_places=2, null=True, blank=True,
        help_text="Max loan-to-value the broker lends against this scrip, in %% (e.g. 50.00).",
    )
    risk_category = models.CharField(
        max_length=50, blank=True, default="",
        help_text="Optional risk group / class from the source list (e.g. 'Group A').",
    )
    source = models.CharField(
        max_length=100, blank=True, default="",
        help_text="Where this entry came from (regulator circular, broker sheet, upload…).",
    )
    effective_date = models.DateField(
        null=True, blank=True,
        help_text="Date this eligibility takes effect, if the source states one.",
    )
    notes = models.TextField(blank=True, default="")

    # Escape hatch for any future field the list may carry — no migration needed.
    metadata = models.JSONField(default=dict, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "margin_eligible_companies"
        ordering = ["symbol"]
        verbose_name = "Margin-eligible company"
        verbose_name_plural = "Margin-eligible companies"
        indexes = [
            models.Index(fields=["is_eligible", "symbol"]),
        ]

    def __str__(self):
        state = "eligible" if self.is_eligible else "de-listed"
        return f"{self.symbol} ({state})"

    def approve(self, reviewer=None, note=""):
        self.status = self.APPROVED
        self.reviewed_by = reviewer
        self.review_note = note or ""
        self.reviewed_at = timezone.now()
        self.user.is_active = True
        self.user.save(update_fields=["is_active"])
        self.save(update_fields=["status", "reviewed_by", "review_note", "reviewed_at"])

    def reject(self, reviewer=None, note=""):
        self.status = self.REJECTED
        self.reviewed_by = reviewer
        self.review_note = note or ""
        self.reviewed_at = timezone.now()
        self.user.is_active = False
        self.user.save(update_fields=["is_active"])
        self.save(update_fields=["status", "reviewed_by", "review_note", "reviewed_at"])


class FundaFundamentalSnapshot(models.Model):
    """Latest fundamentals synced on demand from funda.aurasrp.com.np.

    We OWN this table (unlike the read-only ``FinancialStatement`` mapping, whose
    external FK columns make it unsafe to write). A row is the newest published
    quarter for one symbol — its KeyStats mapped to the canonical keys the Stock
    360 cards read, plus a small revenue/net-income trend and the raw statements
    for reference. One row per symbol: a re-sync overwrites in place.
    """
    symbol = models.CharField(max_length=20, unique=True, db_index=True)
    security_name = models.CharField(max_length=255, blank=True, default="")
    sector = models.CharField(max_length=100, blank=True, default="")

    period = models.CharField(max_length=20, help_text="e.g. '2024/25 Q4'")
    fiscal_year_ad = models.CharField(max_length=10, blank=True, default="")
    quarter = models.PositiveSmallIntegerField(default=0)

    ks = models.JSONField(default=dict, help_text="Canonical keystats: eps, pe, roe, …")
    trend = models.JSONField(default=list, help_text="Annual revenue / net-income points")
    raw = models.JSONField(default=dict, blank=True, help_text="Full statements payload")

    source = models.CharField(max_length=100, default="funda.aurasrp.com.np")
    fs_written = models.PositiveSmallIntegerField(
        default=0, help_text="KeyStats line items pushed into FinancialStatement on last sync"
    )
    synced_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["symbol"]

    def __str__(self):
        return f"{self.symbol} {self.period} (synced {self.synced_at:%Y-%m-%d})"


class NepseMarketCapDaily(models.Model):
    """
    Table: nepse_market_cap_daily

    Whole-market capitalisation per session, straight from the upstream
    ``/api/nepse-data/api/market-cap/`` feed.

    This exists because the per-stock ``NepseDailyStockPrice.market_capitalization``
    column is unreliable — the feed intermittently returns 0 for every scrip on
    the most recent session(s), which silently zeroes any total summed from it.
    This endpoint reports the exchange's own published totals and has values for
    exactly the dates the per-stock column is missing, so it is the authoritative
    source for market-cap figures and the per-stock column is only used for the
    per-scrip breakdown.

    It also carries the sensitive / float variants, which cannot be derived from
    the per-stock table at all.
    """
    business_date = models.DateField(unique=True, db_index=True)

    market_capitalization = models.DecimalField(max_digits=22, decimal_places=2, null=True, blank=True)
    sensitive_market_capitalization = models.DecimalField(max_digits=22, decimal_places=2, null=True, blank=True)
    float_market_capitalization = models.DecimalField(max_digits=22, decimal_places=2, null=True, blank=True)
    sensitive_float_market_capitalization = models.DecimalField(max_digits=22, decimal_places=2, null=True, blank=True)

    total_turnover = models.DecimalField(max_digits=22, decimal_places=2, null=True, blank=True)
    total_traded_shares = models.BigIntegerField(null=True, blank=True)
    total_transactions = models.BigIntegerField(null=True, blank=True)
    total_scrips_traded = models.IntegerField(null=True, blank=True)

    synced_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "nepse_market_cap_daily"
        ordering = ["-business_date"]
        verbose_name_plural = "Nepse market cap (daily)"

    def __str__(self):
        return f"{self.business_date} — {self.market_capitalization}"


class ProposedDividend(models.Model):
    """
    Table: proposed_dividends

    Board-proposed dividends (bonus + cash) per company per fiscal year, scraped
    from ShareSansar's /proposed-dividend DataTables feed.

    "Proposed" means announced by the board but not necessarily approved at the
    AGM or distributed yet — that is exactly why the dates are nullable: a fresh
    announcement has no book-closure, distribution or bonus-listing date, and
    they fill in over the following weeks. Re-syncing updates the same row
    (keyed on the upstream ``source_id``) as those dates land.

    ``ltp`` / ``price_as_of`` are the upstream's own price snapshot at scrape
    time — kept for provenance only. Never use them for analytics: the local
    NepseDailyStockPrice table is the price source and is split/bonus adjusted.
    """
    source_id = models.IntegerField(unique=True, help_text="ShareSansar row id")

    symbol = models.CharField(max_length=20, db_index=True)
    company_name = models.CharField(max_length=255, blank=True, default="")
    fiscal_year = models.CharField(max_length=20, db_index=True, help_text="BS, e.g. 2081/2082")

    # Percent of paid-up capital. Cash covers the tax-on-bonus portion too, which
    # is how the exchange publishes it.
    bonus_percent = models.DecimalField(max_digits=10, decimal_places=4, null=True, blank=True)
    cash_percent = models.DecimalField(max_digits=10, decimal_places=4, null=True, blank=True)
    total_percent = models.DecimalField(max_digits=10, decimal_places=4, null=True, blank=True)

    announcement_date = models.DateField(null=True, blank=True, db_index=True)
    bookclose_date = models.DateField(null=True, blank=True)
    # Upstream ships book closure as "2025-08-11 [Closed]" — the bracketed state
    # is split out here so the date stays a real date.
    bookclose_status = models.CharField(max_length=40, blank=True, default="")
    distribution_date = models.DateField(null=True, blank=True)
    bonus_listing_date = models.DateField(null=True, blank=True)

    ltp = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    price_as_of = models.DateField(null=True, blank=True)

    synced_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "proposed_dividends"
        ordering = ["-announcement_date", "symbol"]
        indexes = [
            models.Index(fields=["symbol", "fiscal_year"]),
            models.Index(fields=["fiscal_year", "-announcement_date"]),
        ]

    def __str__(self):
        return f"{self.symbol} {self.fiscal_year} — {self.total_percent}%"


class MutualFundNav(models.Model):
    """
    Table: mutual_fund_nav

    Net asset value per unit for NEPSE-listed mutual funds, scraped from
    ShareSansar's /mutual-fund-navs DataTables feed.

    WHY THIS TABLE EXISTS AT ALL. Mutual funds do not file the quarterly
    Balance Sheet / Income Statement / Key Statistics that ``FinancialStatement``
    harvests — a fund has no revenue or ROE — so every fund shows blank on the
    fundamentals desks and always will. What a fund publishes instead is its NAV,
    and because almost every Nepali scheme is CLOSED-END, the number that
    actually matters is the gap between NAV and the market price: a fund trading
    at 9.35 against a NAV of 9.96 is on an 8% discount. ``premium_discount_pct``
    is that figure, and it is the fund equivalent of P/B.

    One row per (symbol, NAV period). The feed only ever exposes each fund's
    LATEST reading, so history is not backfillable — it accumulates from the
    first sync onward, one new row per fund per Nepali month.

    ``nav_period`` is a Nepali-calendar month label as published, e.g.
    "Asadh 2083". It is stored as text on purpose: it is the source's own
    reporting key, and converting it to a Gregorian date would invent a
    precision (which day?) the publisher never stated.
    """
    symbol = models.CharField(max_length=20, db_index=True)
    nav_period = models.CharField(
        max_length=32, help_text="Nepali month as published, e.g. 'Asadh 2083'")

    fund_name = models.CharField(max_length=255, blank=True, default="")
    # Upstream row id. Kept for provenance/debugging only — NOT the unique key,
    # because one fund yields a new row every month under the same id.
    source_id = models.IntegerField(null=True, blank=True)

    # 0 = closed-end, 1 = matured, 2 = open-end. Stored as the source sends it;
    # see FUND_TYPES in services/mutual_fund_nav.py for the labels.
    fund_type = models.CharField(max_length=2, blank=True, default="")
    fund_size = models.DecimalField(max_digits=20, decimal_places=2, null=True, blank=True)
    maturity_date = models.DateField(null=True, blank=True)
    maturity_period = models.CharField(max_length=40, blank=True, default="")

    nav_monthly = models.DecimalField(max_digits=12, decimal_places=4, null=True, blank=True)
    # Open-end schemes publish daily/weekly as well; closed-end ones mostly do not.
    nav_weekly = models.DecimalField(max_digits=12, decimal_places=4, null=True, blank=True)
    nav_weekly_date = models.DateField(null=True, blank=True)
    nav_daily = models.DecimalField(max_digits=12, decimal_places=4, null=True, blank=True)
    nav_daily_date = models.DateField(null=True, blank=True)
    # Only open-end funds quote a refund/redemption NAV.
    refund_nav = models.DecimalField(max_digits=12, decimal_places=4, null=True, blank=True)

    # The source's own price snapshot, kept ONLY so premium_discount_pct can be
    # audited against the inputs that produced it. Never use it for analytics:
    # nepse_daily_stock_prices is the price source and is split/bonus adjusted.
    market_close = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    market_close_date = models.DateField(null=True, blank=True)
    # Negative = trading BELOW NAV (a discount), which is the normal state for
    # Nepali closed-end funds. Null when the fund no longer trades.
    premium_discount_pct = models.DecimalField(max_digits=10, decimal_places=4, null=True, blank=True)

    synced_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "mutual_fund_nav"
        # The feed exposes one reading per fund, so re-syncing inside the same
        # Nepali month must update that month's row, not append a duplicate.
        unique_together = (("symbol", "nav_period"),)
        indexes = [
            models.Index(fields=["symbol", "-synced_at"]),
        ]

    def __str__(self):
        return f"{self.symbol} {self.nav_period} — NAV {self.nav_monthly}"


class MutualFundPortfolio(models.Model):
    """
    Table: mutual_fund_portfolio

    One fund's portfolio for one Nepali month: the asset-class split, plus the
    NAV/price context needed to read it. The per-script detail hangs off this
    row as ``MutualFundHolding``.

    WHY A SUMMARY ROW AND NOT JUST HOLDINGS. Equity is recomputed from the
    holdings (their market values sum to it), but fixed income and cash have no
    per-script detail to sum — a fund reports "Fixed Deposit: 4,105,151" as one
    line. Those buckets therefore have to be stored, not derived, or the asset
    allocation could never add up to 100%.

    ``period`` is a Nepali month, stored CANONICALLY (see
    ``services/mutual_fund_portfolio.canonical_period``). The sources disagree
    on spelling — ShareSansar's NAV feed says "Asadh 2083" where the portfolio
    reports say "Ashad 2083" — so both are normalised on the way in, or the NAV
    and the allocation for one fund-month would never join.
    """
    symbol = models.CharField(max_length=20, db_index=True)
    period = models.CharField(
        max_length=32, db_index=True,
        help_text="Canonical Nepali month, e.g. 'Ashadh 2083'")

    fund_name = models.CharField(max_length=255, blank=True, default="")

    # NAV/price context as reported with the portfolio. Nullable: a report may
    # carry the holdings without restating the NAV, which the NAV feed already has.
    nav_monthly = models.DecimalField(max_digits=12, decimal_places=4, null=True, blank=True)
    nav_daily = models.DecimalField(max_digits=12, decimal_places=4, null=True, blank=True)
    ltp = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)

    # Asset-class buckets, in rupees. equity_value is the sum of the holdings
    # below and is stored denormalised so allocation queries need no join.
    equity_value = models.DecimalField(max_digits=20, decimal_places=2, default=0)
    fixed_income_value = models.DecimalField(max_digits=20, decimal_places=2, default=0)
    cash_value = models.DecimalField(max_digits=20, decimal_places=2, default=0)
    # Anything the report lists that is none of the three above. Kept separate
    # rather than folded into cash so the percentages stay honest.
    other_value = models.DecimalField(max_digits=20, decimal_places=2, default=0)

    # Net assets as the fund reports them, when the report states it. This is
    # the DENOMINATOR the published allocation tables use, and it is not the sum
    # of the buckets above: liabilities net out, which is why a fund's published
    # equity/fixed-income/cash percentages routinely add up to slightly over
    # 100%. Null means we fall back to the bucket sum, which sums to exactly
    # 100% — self-consistent, but not the same convention.
    net_assets = models.DecimalField(max_digits=20, decimal_places=2, null=True, blank=True)

    source_name = models.CharField(max_length=255, blank=True, default="",
                                   help_text="Uploaded file this came from")
    imported_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "mutual_fund_portfolio"
        unique_together = (("symbol", "period"),)
        indexes = [models.Index(fields=["period", "symbol"])]

    def __str__(self):
        return f"{self.symbol} {self.period}"

    @property
    def total_value(self):
        return (self.equity_value + self.fixed_income_value
                + self.cash_value + self.other_value)


class MutualFundHolding(models.Model):
    """
    Table: mutual_fund_holding

    One line of a fund's equity portfolio for one month — the ``Script /
    Company Name / Sector / Kitta / Book Value / Market Value`` detail.

    ``sector`` is stored as the report gave it, but allocation is aggregated on
    the sector resolved from CompanyProfile where the script is known: the
    platform already has one answer for "what sector is NABIL", and a report's
    own spelling should not create a second.
    """
    portfolio = models.ForeignKey(
        MutualFundPortfolio, on_delete=models.CASCADE, related_name="holdings")

    script = models.CharField(max_length=20, db_index=True)
    company_name = models.CharField(max_length=255, blank=True, default="")
    sector = models.CharField(max_length=80, blank=True, default="",
                              help_text="As printed in the report")

    kitta = models.DecimalField(max_digits=20, decimal_places=2, null=True, blank=True)
    book_value = models.DecimalField(max_digits=20, decimal_places=2, null=True, blank=True)
    market_value = models.DecimalField(max_digits=20, decimal_places=2, null=True, blank=True)

    # The fund's OWN published weight for this line, as a percentage of its
    # equity book. Verified against the source: it tracks the BOOK (cost) share,
    # not the market one — C30MF/SALICO Ashad 2083 reports 8.61 where cost gives
    # 8.6059 and market gives 8.4199 — and the set sums to ~99.99 rather than
    # exactly 100 because each line is rounded to 2dp before publication.
    #
    # Stored rather than always recomputed so a fund's stated weight can be
    # shown as the fund stated it. Null for any month published without it.
    weight_percent = models.DecimalField(max_digits=8, decimal_places=4,
                                         null=True, blank=True)

    class Meta:
        db_table = "mutual_fund_holding"
        # One line per script per fund-month; a re-import replaces the set.
        unique_together = (("portfolio", "script"),)
        indexes = [models.Index(fields=["script"])]

    def __str__(self):
        return f"{self.portfolio.symbol} {self.portfolio.period} — {self.script}"


class MutualFundProfile(models.Model):
    """
    Table: mutual_fund_profile

    Scheme-level facts for one fund: who runs it, how big it is, when it
    matures, and the two NAVs it publishes. Sourced from the internal
    ``/api/mutual-fund/funds/`` feed (see ``services/mutual_fund_api``).

    WHY THIS IS SEPARATE FROM ``MutualFundNav``. That table is a time series
    scraped from ShareSansar — one row per reading, and history matters. This is
    a single current-state row per fund, and it carries things a NAV feed has no
    concept of: fund size, maturity date, the asset management company. Merging
    them would force every scheme fact to be re-stated on every NAV reading.

    ``daily_nav`` is the fund's most recent published NAV and ``monthly_nav``
    its month-end one. They are frequently equal — the feed restates the daily
    figure as the monthly at period close — and that is not an error.
    """
    symbol = models.CharField(max_length=20, unique=True, db_index=True)
    fund_name = models.CharField(max_length=255, blank=True, default="")
    amc = models.CharField(max_length=255, blank=True, default="",
                           help_text="Asset management company")
    amc_member = models.CharField(max_length=255, blank=True, default="")

    fund_size = models.DecimalField(max_digits=20, decimal_places=2, null=True, blank=True)
    maturity_date = models.DateField(null=True, blank=True)
    maturity_period = models.CharField(max_length=64, blank=True, default="")

    daily_nav = models.DecimalField(max_digits=12, decimal_places=4, null=True, blank=True)
    daily_nav_date = models.DateField(null=True, blank=True)
    monthly_nav = models.DecimalField(max_digits=12, decimal_places=4, null=True, blank=True)
    # Nepali month text ("Ashadh 2083"), canonicalised on the way in.
    monthly_nav_period = models.CharField(max_length=32, blank=True, default="")

    synced_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "mutual_fund_profile"
        ordering = ["symbol"]

    def __str__(self):
        return f"{self.symbol} — {self.fund_name}"


class MutualFundStatementItem(models.Model):
    """
    Table: mutual_fund_statement_item

    One line of a fund's Balance Sheet or Income Statement for one month, stored
    VERBATIM as the fund published it.

    WHY VERBATIM, AND WHY SEPARATE FROM ``MutualFundPortfolio``. That model
    holds four normalised buckets in rupees, which is what allocation maths
    needs. This holds every line the fund actually filed, at the scale it filed
    it, which is what a financials screen needs — a reader comparing our page
    with the fund's own report must see the same numbers in the same units.
    Deriving one from the other is lossy in both directions, so both are kept.

    ``amount`` IS NOT NORMALISED. The source reports money in thousands of
    rupees ("Invested in Shares 725,961.84" means Rs 725.96m) while per-unit and
    count lines — "NAV per Unit", "Number of Units Outstanding" — are in their
    own natural units. Mixing scales in one column is only safe because nothing
    aggregates across this table: it is read back a line at a time for display.
    Anything that does arithmetic on these figures must use
    ``MutualFundPortfolio``'s rupee columns instead.
    """
    symbol = models.CharField(max_length=20, db_index=True)
    period = models.CharField(max_length=32, db_index=True,
                              help_text="Canonical Nepali month")
    # "BS" (Balance Sheet) or "IS" (Income Statement), as the feed labels them.
    fs_type = models.CharField(max_length=4)
    item_name = models.CharField(max_length=120)
    amount = models.DecimalField(max_digits=22, decimal_places=4, null=True, blank=True)

    # The statement date this line came from. A fund can file twice for one
    # month (Provisional, then Published); the newest wins on import.
    nav_date = models.DateField(null=True, blank=True)
    # Publication order, so the panel can render the fund's own line sequence
    # rather than an alphabetical one that reads as a jumble.
    position = models.PositiveSmallIntegerField(default=0)

    class Meta:
        db_table = "mutual_fund_statement_item"
        unique_together = (("symbol", "period", "fs_type", "item_name"),)
        indexes = [models.Index(fields=["symbol", "period", "fs_type"])]
        ordering = ["fs_type", "position", "item_name"]

    def __str__(self):
        return f"{self.symbol} {self.period} {self.fs_type} — {self.item_name}"


class LifeInsuranceIndicator(models.Model):
    """
    Table: life_insurance_indicators — the "Other Indicators" block of an
    insurer's quarterly report (life AND non-life; the name predates the
    non-life extension), entered BY HAND.

    WHY A SEPARATE TABLE: the fundamentals feed (funda.aurasrp) publishes only
    eight of the seventeen "Other Indicators" lines — it stops at
    ``li_ks_534_short_term_investments`` for every life insurer — and the nine
    below appear nowhere but the company's own PDF. They cannot go into
    ``FinancialStatement`` either: that table is unmanaged, owned by another
    app, and keyed on an account-dictionary id we do not model.

    Read paths merge these rows into the KS statement at read time (Industry
    Analysis matrix, company statement API) as items ``li_ks_535``–``543`` so
    they sit under the feed's own rows without a second UI.

    Amounts are stored in FULL RUPEES exactly as printed on the report; the
    merge converts to the feed's "Rs. 000" unit so columns compare.
    """
    # Which "Other Indicators" layout the row follows. Life and non-life
    # reports print different tables; the field set per sector lives in
    # services/life_indicators.SPECS and unused columns simply stay NULL.
    sector = models.CharField(max_length=40, default="Life Insurance", db_index=True)
    ticker = models.CharField(max_length=20, db_index=True)
    fiscal_year_ad = models.CharField(max_length=10, db_index=True, help_text="e.g. 2025/26")
    quarter = models.PositiveSmallIntegerField(help_text="1-4")

    policies_issued = models.BigIntegerField(null=True, blank=True,
                                             help_text="Total no. of policies issued during the year")
    gross_claim_outstanding = models.DecimalField(max_digits=20, decimal_places=2, null=True, blank=True,
                                                  help_text="Rs, as printed")
    declared_bonus_rate = models.CharField(max_length=80, blank=True, default="",
                                           help_text="As printed, e.g. 'Rs. 55 - Rs. 85 Per Thousand'")
    interim_bonus_rate = models.CharField(max_length=80, blank=True, default="")
    policyholders_loan = models.DecimalField(max_digits=20, decimal_places=2, null=True, blank=True)
    investment_at_cost = models.DecimalField(max_digits=20, decimal_places=2, null=True, blank=True)
    life_insurance_fund = models.DecimalField(max_digits=20, decimal_places=2, null=True, blank=True)
    unearned_premium_reserve = models.DecimalField(max_digits=20, decimal_places=2, null=True, blank=True)
    solvency_margin_ratio = models.DecimalField(max_digits=10, decimal_places=4, null=True, blank=True)

    source_note = models.CharField(max_length=255, blank=True, default="",
                                   help_text="Where the figures came from, e.g. 'Q4 2082/83 report p.7'")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "life_insurance_indicators"
        unique_together = (("ticker", "fiscal_year_ad", "quarter"),)
        ordering = ["-fiscal_year_ad", "-quarter", "ticker"]

    def __str__(self):
        return f"{self.ticker} {self.fiscal_year_ad} Q{self.quarter} — other indicators"


class BondValuation(models.Model):
    """
    Table: bond_valuations

    One row per listed debenture / bond, holding the latest valuation sheet
    (price, YTM, spread over benchmark, fair value, duration). Loaded from
    ``core_analysis/data/bond_valuations.csv`` by ``load_bond_valuations``;
    re-running the loader refreshes the same row (keyed on the bond symbol).

    ``issuer`` links the bond to the equity that issued it, so a company's
    Stock 360 page can list its own debentures. The loader resolves it from the
    security name; merged banks resolve to the surviving entity (Sunrise ->
    LSL, BOK -> GBIME, NCC -> KBL, ...) with the reason kept in ``issuer_note``.
    """
    symbol = models.CharField(max_length=30, unique=True, db_index=True,
                              help_text="Bond ticker, e.g. NBLD87")
    security_name = models.CharField(max_length=255)
    sector = models.CharField(max_length=100, blank=True, default="", db_index=True)
    issuer = models.ForeignKey(
        CompanyProfile, on_delete=models.SET_NULL, null=True, blank=True,
        db_column="issuer_symbol", related_name="bonds",
        help_text="Equity symbol of the issuing company.",
    )
    issuer_note = models.CharField(max_length=255, blank=True, default="")

    coupon_pct = models.DecimalField(max_digits=7, decimal_places=3, null=True, blank=True)
    maturity_date = models.DateField(null=True, blank=True, db_index=True)
    years_to_maturity = models.DecimalField(max_digits=7, decimal_places=2, null=True, blank=True)
    issue_size = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True,
                                     help_text="Total face value of the issue, Rs")

    price = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    price_quality = models.CharField(max_length=60, blank=True, default="",
                                     help_text="Adequate / Stale - N days / Thin / No trade")
    ytm_pct = models.DecimalField(max_digits=8, decimal_places=3, null=True, blank=True)
    benchmark_pct = models.DecimalField(max_digits=8, decimal_places=3, null=True, blank=True)
    spread_bp = models.IntegerField(null=True, blank=True)
    fair_value = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    variance = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    variance_pct = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)
    valuation = models.CharField(max_length=30, blank=True, default="", db_index=True,
                                 help_text="Undervalued / Fairly valued / Overvalued / Not traded")
    macaulay_duration = models.DecimalField(max_digits=7, decimal_places=2, null=True, blank=True)
    modified_duration = models.DecimalField(max_digits=7, decimal_places=2, null=True, blank=True)

    valuation_date = models.DateField(null=True, blank=True, db_index=True)
    source = models.CharField(max_length=100, blank=True, default="")
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "bond_valuations"
        ordering = ["maturity_date", "symbol"]
        indexes = [models.Index(fields=["issuer", "maturity_date"])]

    def __str__(self):
        return f"{self.symbol} — {self.security_name}"

    @property
    def issue_size_bn(self):
        return float(self.issue_size) / 1e9 if self.issue_size else None

    @property
    def valuation_tone(self):
        v = (self.valuation or "").lower()
        if v.startswith("under"):
            return "pos"
        if v.startswith("over"):
            return "neg"
        return "neu"


class MarketDepthSnapshot(models.Model):
    """
    Table: market_depth_snapshots

    One TMS top-5 order-book capture for one script, as served by the feed host
    (``/api/tms-market-depth/``). Synced by ``sync_market_depth``.

    The feed polls scripts in rotation rather than snapshotting the whole market
    at once, so there is NO batch id: a "replay frame" is simply the sequence of
    captures for one symbol ordered by ``captured_at``. Frame-to-frame
    comparisons are therefore always per symbol (services/market_depth.py).

    ``depth`` keeps the raw book verbatim: {"bids": [{level, price, qty,
    splits}...], "asks": [...]}. The flattened columns are query helpers only —
    they are derived from ``depth`` at sync time and never edited by hand.
    """
    source_id = models.BigIntegerField(unique=True, help_text="Row id on the feed host")
    business_date = models.DateField(db_index=True)
    captured_at = models.DateTimeField(db_index=True, help_text="Capture wall-clock time (feed date + time)")
    symbol = models.CharField(max_length=20, db_index=True)
    board = models.CharField(max_length=10, blank=True, default="1")

    total_bids = models.BigIntegerField(default=0)
    total_asks = models.BigIntegerField(default=0)
    depth = models.JSONField(default=dict, help_text='{"bids": [...], "asks": [...]} raw top-5 book')

    # Derived from `depth` for fast filtering / charting.
    best_bid = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    best_ask = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    bid_qty_top5 = models.BigIntegerField(default=0)
    ask_qty_top5 = models.BigIntegerField(default=0)
    bid_splits_top5 = models.IntegerField(default=0)
    ask_splits_top5 = models.IntegerField(default=0)

    aon_side = models.CharField(max_length=10, blank=True, default="")
    aon_status = models.CharField(max_length=30, blank=True, default="")
    synced_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "market_depth_snapshots"
        ordering = ["symbol", "captured_at"]
        indexes = [
            models.Index(fields=["symbol", "captured_at"]),
            models.Index(fields=["business_date", "symbol"]),
        ]

    def __str__(self):
        return f"{self.symbol} @ {self.captured_at:%Y-%m-%d %H:%M:%S}"
