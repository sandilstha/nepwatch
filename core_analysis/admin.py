from django.contrib import admin, messages
from django.db.models import Count
from django.utils import timezone

from .models import (
    AccountApproval,
    BondValuation,
    BrokerLedgerImport,
    BrokerLedgerTransaction,
    BrokerTrade,
    Holding,
    HoldingCost,
    MarginEligibleCompany,
    Portfolio,
)


@admin.action(description="Approve selected account requests")
def approve_requests(modeladmin, request, queryset):
    count = 0
    for approval in queryset.select_related("user"):
        if approval.status != AccountApproval.APPROVED:
            approval.approve(reviewer=request.user)
            count += 1
    modeladmin.message_user(
        request,
        f"Approved {count} account request(s).",
        messages.SUCCESS,
    )


@admin.action(description="Reject selected account requests")
def reject_requests(modeladmin, request, queryset):
    count = 0
    for approval in queryset.select_related("user"):
        if approval.status != AccountApproval.REJECTED:
            approval.reject(reviewer=request.user)
            count += 1
    modeladmin.message_user(
        request,
        f"Rejected {count} account request(s).",
        messages.WARNING,
    )


@admin.register(AccountApproval)
class AccountApprovalAdmin(admin.ModelAdmin):
    list_display = (
        "user",
        "contact_email",
        "status",
        "created_at",
        "reviewed_by",
        "reviewed_at",
    )
    list_filter = ("status", "created_at", "reviewed_at")
    search_fields = ("user__username", "user__email", "contact_email")
    readonly_fields = ("created_at", "reviewed_by", "reviewed_at")
    actions = (approve_requests, reject_requests)
    fieldsets = (
        (None, {"fields": ("user", "contact_email", "status", "review_note")}),
        ("Review", {"fields": ("created_at", "reviewed_by", "reviewed_at")}),
    )

    def save_model(self, request, obj, form, change):
        old_status = None
        if change and obj.pk:
            old_status = AccountApproval.objects.only("status").get(pk=obj.pk).status

        status_changed = old_status != obj.status
        if status_changed and obj.status in (AccountApproval.APPROVED, AccountApproval.REJECTED):
            obj.reviewed_by = request.user
            obj.reviewed_at = timezone.now()
            obj.user.is_active = obj.status == AccountApproval.APPROVED
            obj.user.save(update_fields=["is_active"])
        elif status_changed and obj.status == AccountApproval.PENDING:
            obj.reviewed_by = None
            obj.reviewed_at = None
            obj.user.is_active = False
            obj.user.save(update_fields=["is_active"])

        super().save_model(request, obj, form, change)


# ── Portfolio uploads: who imported holdings / cost basis, and what ────────────
class HoldingInline(admin.TabularInline):
    """The uploaded positions (from the Meroshare 'My Shares' CSV)."""
    model = Holding
    extra = 0
    fields = ("symbol", "quantity", "last_close", "ltp", "updated_at")
    readonly_fields = ("updated_at",)


class HoldingCostInline(admin.TabularInline):
    """The uploaded cost basis (from the broker 'My WACC' report)."""
    model = HoldingCost
    extra = 0
    fields = ("symbol", "wacc_rate", "quantity", "total_cost", "modified", "updated_at")
    readonly_fields = ("updated_at",)


@admin.register(Portfolio)
class PortfolioAdmin(admin.ModelAdmin):
    """Every user's uploaded portfolio — the answer to 'who uploaded a portfolio'.

    The changelist lists one row per uploaded portfolio with its owner and how
    many holdings / cost rows it carries; drill in to see the actual positions.
    """
    list_display = (
        "name",
        "user",
        "portfolio_type",
        "is_default",
        "is_archived",
        "holdings_count",
        "costs_count",
        "ledger_count",
        "created_at",
        "updated_at",
    )
    list_filter = ("portfolio_type", "is_default", "is_archived", "created_at", "updated_at")
    search_fields = (
        "user__username",
        "user__email",
        "name",
        "holdings__symbol",
        "ledger_transactions__voucher_no",
        "ledger_transactions__reference_no",
    )
    readonly_fields = ("created_at", "updated_at")
    date_hierarchy = "updated_at"
    ordering = ("-updated_at",)
    inlines = (HoldingInline, HoldingCostInline)

    def get_queryset(self, request):
        qs = super().get_queryset(request).select_related("user")
        return qs.annotate(_holdings=Count("holdings", distinct=True),
                           _costs=Count("costs", distinct=True),
                           _ledger=Count("ledger_transactions", distinct=True))

    @admin.display(description="Holdings", ordering="_holdings")
    def holdings_count(self, obj):
        return obj._holdings

    @admin.display(description="Cost rows", ordering="_costs")
    def costs_count(self, obj):
        return obj._costs

    @admin.display(description="Ledger rows", ordering="_ledger")
    def ledger_count(self, obj):
        return obj._ledger


@admin.register(Holding)
class HoldingAdmin(admin.ModelAdmin):
    """Flat, searchable view of every uploaded position across all users."""
    list_display = ("symbol", "quantity", "portfolio", "owner", "updated_at")
    list_filter = ("updated_at",)
    search_fields = ("symbol", "portfolio__user__username", "portfolio__name")
    readonly_fields = ("updated_at",)
    ordering = ("-updated_at",)

    def get_queryset(self, request):
        return super().get_queryset(request).select_related("portfolio__user")

    @admin.display(description="User", ordering="portfolio__user__username")
    def owner(self, obj):
        return obj.portfolio.user


@admin.register(BrokerLedgerImport)
class BrokerLedgerImportAdmin(admin.ModelAdmin):
    list_display = (
        "source_name", "portfolio", "account_name", "report_from_ad", "report_to_ad",
        "imported_rows", "duplicate_rows", "warning_count", "imported_at",
    )
    list_filter = ("imported_at",)
    search_fields = ("source_name", "account_name", "account_code", "portfolio__name", "portfolio__user__username")
    readonly_fields = ("file_sha256", "imported_at")
    ordering = ("-imported_at",)


@admin.register(BrokerLedgerTransaction)
class BrokerLedgerTransactionAdmin(admin.ModelAdmin):
    list_display = (
        "date_ad", "date_bs", "fiscal_year", "transaction_type", "voucher_no",
        "debit", "credit", "balance", "portfolio",
    )
    list_filter = ("transaction_type", "fiscal_year", "balance_side", "sequence_gap", "balance_mismatch")
    search_fields = (
        "voucher_no", "reference_no", "particulars", "portfolio__name",
        "portfolio__user__username", "trades__symbol",
    )
    readonly_fields = ("fingerprint", "created_at")
    date_hierarchy = "date_ad"
    ordering = ("-date_ad", "-id")


@admin.register(BrokerTrade)
class BrokerTradeAdmin(admin.ModelAdmin):
    list_display = ("symbol", "side", "quantity", "price", "net_amount", "transaction")
    list_filter = ("side",)
    search_fields = (
        "symbol", "transaction__voucher_no", "transaction__reference_no",
        "transaction__portfolio__user__username",
    )
    ordering = ("-transaction__date_ad", "-id")


@admin.action(description="Mark selected as margin-eligible")
def mark_margin_eligible(modeladmin, request, queryset):
    n = queryset.update(is_eligible=True)
    _bust_margin_cache()
    modeladmin.message_user(request, f"{n} company(ies) marked eligible.", messages.SUCCESS)


@admin.action(description="De-list selected (mark NOT eligible)")
def mark_margin_ineligible(modeladmin, request, queryset):
    n = queryset.update(is_eligible=False)
    _bust_margin_cache()
    modeladmin.message_user(request, f"{n} company(ies) de-listed.", messages.WARNING)


def _bust_margin_cache():
    from django.core.cache import cache
    cache.delete("margin_eligible_map:v1")


@admin.register(MarginEligibleCompany)
class MarginEligibleCompanyAdmin(admin.ModelAdmin):
    """Maintain the margin-eligible list entirely from the admin — add a row,
    edit margin_rate/sector, or toggle `is_eligible` to (de)list. The search UI
    and fundamentals panel pick up changes on the next request (cache is busted
    on save/delete and by the two bulk actions)."""
    list_display = ("symbol", "company_name", "sector", "is_eligible",
                    "margin_rate", "risk_category", "updated_at")
    list_filter = ("is_eligible", "sector", "risk_category")
    list_editable = ("is_eligible", "margin_rate")
    search_fields = ("symbol", "company_name", "sector")
    ordering = ("symbol",)
    readonly_fields = ("created_at", "updated_at")
    actions = (mark_margin_eligible, mark_margin_ineligible)
    fieldsets = (
        (None, {"fields": ("symbol", "company_name", "sector", "is_eligible")}),
        ("Margin details", {"fields": ("margin_rate", "risk_category", "effective_date", "source", "notes")}),
        ("Advanced", {"classes": ("collapse",), "fields": ("metadata", "created_at", "updated_at")}),
    )

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        _bust_margin_cache()

    def delete_model(self, request, obj):
        super().delete_model(request, obj)
        _bust_margin_cache()


@admin.register(BondValuation)
class BondValuationAdmin(admin.ModelAdmin):
    """Bond valuation sheet. Bulk refresh via `manage.py load_bond_valuations`;
    fix a single issuer link or price here."""
    list_display = ("symbol", "security_name", "issuer", "sector", "coupon_pct",
                    "maturity_date", "price", "ytm_pct", "spread_bp", "valuation", "valuation_date")
    list_filter = ("sector", "valuation", "valuation_date")
    search_fields = ("symbol", "security_name", "issuer__symbol", "issuer__security_name")
    autocomplete_fields = ()
    raw_id_fields = ("issuer",)
    ordering = ("maturity_date",)
    readonly_fields = ("created_at", "updated_at")
