"""
load_margin_eligible — seed / refresh the ``margin_eligible_companies`` table
from the bundled CSV (``core_analysis/data/margin_eligible_companies.csv``).

Idempotent and sync-style: symbols in the CSV are upserted and marked eligible;
symbols previously eligible but ABSENT from the CSV are soft de-listed
(``is_eligible = False``) rather than deleted, so the record and its history
survive. This is the future-proof update path — edit the CSV (or the admin) and
re-run; nothing in the app logic needs to change.

    python manage.py load_margin_eligible
    python manage.py load_margin_eligible --path /custom/list.csv
    python manage.py load_margin_eligible --keep-missing   # don't de-list drops
    python manage.py load_margin_eligible --source "SEBON circular 2082-03"
"""
from __future__ import annotations

import csv
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from core_analysis.models import MarginEligibleCompany

DEFAULT_CSV = Path(__file__).resolve().parents[2] / "data" / "margin_eligible_companies.csv"


def _clean(value):
    v = (value or "").strip()
    # The source uses "#N/A" for an unknown sector — treat as blank.
    return "" if v.upper() in ("", "#N/A", "N/A", "-") else v


class Command(BaseCommand):
    help = "Load NEPSE margin-eligible companies from the bundled CSV."

    def add_arguments(self, parser):
        parser.add_argument("--path", default=str(DEFAULT_CSV),
                             help="Path to the CSV (defaults to the bundled file).")
        parser.add_argument("--source", default="bundled CSV",
                             help="Value stored in each row's `source` column.")
        parser.add_argument("--keep-missing", action="store_true",
                             help="Do NOT de-list symbols that are absent from the CSV.")

    def handle(self, *args, **options):
        path = Path(options["path"])
        if not path.exists():
            raise CommandError(f"CSV not found: {path}")
        source = options["source"]

        created = updated = relisted = 0
        seen_symbols = set()

        with path.open(newline="", encoding="utf-8-sig") as fh:
            reader = csv.DictReader(fh)
            # Tolerate either "Company Name" or "Company Name list" header.
            name_key = next((k for k in (reader.fieldnames or [])
                             if k and k.strip().lower().startswith("company name")), None)
            with transaction.atomic():
                for row in reader:
                    symbol = _clean(row.get("Symbol")).upper()
                    if not symbol:
                        continue
                    name = _clean(row.get(name_key)) if name_key else ""
                    sector = _clean(row.get("Sector"))
                    seen_symbols.add(symbol)

                    obj, was_created = MarginEligibleCompany.objects.get_or_create(
                        symbol=symbol,
                        defaults={"company_name": name, "sector": sector,
                                  "is_eligible": True, "source": source},
                    )
                    if was_created:
                        created += 1
                        continue
                    # Existing row: refresh name/sector, ensure eligible.
                    if not obj.is_eligible:
                        relisted += 1
                    obj.company_name = name or obj.company_name
                    obj.sector = sector or obj.sector
                    obj.is_eligible = True
                    obj.source = source
                    obj.save(update_fields=["company_name", "sector", "is_eligible",
                                            "source", "updated_at"])
                    updated += 1

                delisted = 0
                if not options["keep_missing"]:
                    delisted = (
                        MarginEligibleCompany.objects
                        .filter(is_eligible=True)
                        .exclude(symbol__in=seen_symbols)
                        .update(is_eligible=False)
                    )

        # Bust the cached eligible-symbol set so search reflects the change now.
        from django.core.cache import cache
        cache.delete("margin_eligible_map:v1")

        total = MarginEligibleCompany.objects.filter(is_eligible=True).count()
        self.stdout.write(self.style.SUCCESS(
            f"Margin list loaded: {created} created, {updated} updated, "
            f"{relisted} re-listed, {delisted} de-listed. "
            f"{total} currently eligible."
        ))
