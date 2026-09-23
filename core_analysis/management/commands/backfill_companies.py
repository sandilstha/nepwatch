"""
Safety-net backfill for the company list.

The dropdown reads from ``CompanyProfile``, which is authoritatively populated by
the listed-companies API (``sync_and_calculate --source companies``). That API can
lag the market by days/weeks after an IPO, so a newly listed company trades — and
lands in ``NepseDailyStockPrice`` — before it ever gets a profile, leaving it
invisible in the UI.

This backfill creates a provisional ``CompanyProfile`` for any symbol that has
traded *recently* but has no profile yet, using the official ``security_name``
already carried on the price feed. It deliberately ignores symbols whose last
trade is old (delisted / renamed / settled debentures) so it doesn't resurrect
dead tickers. When the listed-companies API later includes the symbol, the
company sync upserts over this row with the authoritative name/sector/status.
"""
from datetime import timedelta

from django.core.cache import cache
from django.core.management.base import BaseCommand
from django.db.models import Max

from core_analysis.models import CompanyProfile, NepseDailyStockPrice

# A symbol counts as "active" if it traded within this many days of the latest
# trade date in the DB. Wide enough to catch a fresh IPO that paused trading,
# narrow enough to exclude tickers that stopped trading months/years ago.
DEFAULT_ACTIVE_WITHIN_DAYS = 30


def backfill_missing_company_profiles(active_within_days=DEFAULT_ACTIVE_WITHIN_DAYS, dry_run=False):
    """Create CompanyProfile rows for recently-traded symbols that have none.

    Returns the sorted list of symbols that were (or would be) created.
    """
    latest = NepseDailyStockPrice.objects.aggregate(m=Max("business_date"))["m"]
    if latest is None:
        return []

    cutoff = latest - timedelta(days=active_within_days)
    recent_traded = set(
        NepseDailyStockPrice.objects.filter(business_date__gte=cutoff)
        .values_list("symbol", flat=True)
        .distinct()
    )
    existing = set(CompanyProfile.objects.values_list("symbol", flat=True))
    missing = sorted(recent_traded - existing)
    if not missing:
        return []

    new_rows = []
    for symbol in missing:
        name = (
            NepseDailyStockPrice.objects.filter(symbol=symbol)
            .order_by("-business_date")
            .values_list("security_name", flat=True)
            .first()
        )
        new_rows.append(
            CompanyProfile(symbol=symbol, security_name=name or symbol, status="Active")
        )

    if not dry_run:
        # Plain insert with ignore_conflicts: we only ever ADD missing profiles
        # here and never want to clobber an API-sourced name/sector/status.
        CompanyProfile.objects.bulk_create(new_rows, ignore_conflicts=True)
        cache.delete("nepse_symbol_lists")

    return missing


class Command(BaseCommand):
    help = (
        "Create CompanyProfile rows for symbols that have traded recently but have "
        "no profile yet (safety net so newly listed companies appear in the dropdown "
        "even before the listed-companies API catches up)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--active-within-days",
            type=int,
            default=DEFAULT_ACTIVE_WITHIN_DAYS,
            help="Only backfill symbols traded within this many days of the latest trade date.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would be created without writing.",
        )

    def handle(self, *args, **options):
        dry_run = bool(options.get("dry_run"))
        created = backfill_missing_company_profiles(
            active_within_days=options["active_within_days"],
            dry_run=dry_run,
        )
        if not created:
            self.stdout.write(self.style.SUCCESS("No missing company profiles — list is up to date."))
            return
        verb = "Would create" if dry_run else "Created"
        self.stdout.write(self.style.SUCCESS(f"{verb} {len(created)} company profile(s): {', '.join(created)}"))
