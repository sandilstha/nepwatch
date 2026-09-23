"""Sync board-proposed dividends (bonus + cash) from ShareSansar.

The upstream feed is fiscal-year scoped, so a full history is one request per
year (25 of them, back to 2057/2058). Routine runs only need the newest year or
two — proposed dividends for closed years do not change, apart from the
distribution / bonus-listing dates trickling in on the current one.

This also runs automatically at the end of ``sync_nepse_data``; the command is
here for the initial backfill and for re-syncing one year on demand.

Usage:
    python manage.py sync_proposed_dividend            # 2 newest fiscal years
    python manage.py sync_proposed_dividend --all      # every fiscal year
    python manage.py sync_proposed_dividend --years 5
    python manage.py sync_proposed_dividend --year 2081/2082
"""
from django.core.management.base import BaseCommand

from core_analysis.services import proposed_dividend


class Command(BaseCommand):
    help = "Sync proposed dividends (bonus/cash per fiscal year) from ShareSansar."

    def add_arguments(self, p):
        p.add_argument("--all", action="store_true", help="Every fiscal year on the site.")
        p.add_argument("--years", type=int, default=2,
                       help="How many recent fiscal years to sync (default 2).")
        p.add_argument("--year", default="", help="One fiscal year, e.g. 2081/2082.")

    def handle(self, *a, **o):
        def progress(label, kept, error):
            if error:
                self.stderr.write(self.style.WARNING(f"[SKIP] {label}: {error}"))
            else:
                self.stdout.write(f"  {label}: {kept} rows")

        try:
            stats = proposed_dividend.sync(
                years=o["years"], all_years=o["all"], year=o["year"], on_progress=progress,
            )
        except Exception as exc:
            self.stderr.write(self.style.ERROR(f"[FAIL] {exc}"))
            return

        if not stats["upserted"]:
            self.stdout.write(self.style.WARNING("No rows returned."))
            return

        self.stdout.write(self.style.SUCCESS(
            f"[OK] {stats['upserted']} proposed dividends upserted across "
            f"{len(stats['years'])} fiscal year(s). Table now holds {stats['total_rows']}."
        ))
