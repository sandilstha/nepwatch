"""
load_brokers — seed / refresh the ``nepse_brokers`` reference table from the
bundled CSV (``core_analysis/data/stock_brokers.csv``).

The CSV has two sections: the main broker list and a short "Stock Dealer"
section whose numbers (60, 77) overlap the broker list. We load the broker
section first, then flag the overlapping firms as dealers. Idempotent — re-runs
update existing rows in place (keyed on broker number).

    python manage.py load_brokers
    python manage.py load_brokers --path /custom/brokers.csv
"""
from __future__ import annotations

import csv
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from core_analysis.models import Broker

DEFAULT_CSV = Path(__file__).resolve().parents[2] / "data" / "stock_brokers.csv"


def _clean(value):
    v = (value or "").strip()
    return None if v in ("", "-") else v


class Command(BaseCommand):
    help = "Load NEPSE brokers/dealers from the bundled CSV into nepse_brokers."

    def add_arguments(self, parser):
        parser.add_argument(
            "--path", default=str(DEFAULT_CSV),
            help="Path to the brokers CSV (defaults to the bundled file).",
        )

    def handle(self, *args, **options):
        path = Path(options["path"])
        if not path.exists():
            raise CommandError(f"CSV not found: {path}")

        created = updated = dealers = 0
        section = "broker"  # flips to "dealer" after the blank separator row

        with path.open(newline="", encoding="utf-8-sig") as fh:
            for row in csv.reader(fh):
                if not row or not any(cell.strip() for cell in row):
                    continue
                first = row[0].strip()
                # Header rows: the broker header starts with "SN"; the dealer
                # section header has an empty SN cell and "Stock Dealer" label.
                if first.upper() == "SN":
                    section = "broker"
                    continue
                if first == "" and len(row) > 1 and "dealer" in row[1].lower():
                    section = "dealer"
                    continue

                # Columns: SN, Name, Contact Person, Contact Number, Number, Status, TMS
                if len(row) < 7:
                    continue
                name = _clean(row[1])
                number_raw = row[4].strip()
                if not name or not number_raw:
                    continue
                try:
                    number = int(number_raw)
                except ValueError:
                    continue

                if section == "dealer":
                    # Dealer firms already exist as brokers — just flag them.
                    n = Broker.objects.filter(broker_number=number).update(is_dealer=True)
                    dealers += n
                    if not n:
                        Broker.objects.create(
                            broker_number=number, name=name,
                            contact_person=_clean(row[2]), contact_number=_clean(row[3]),
                            status=(_clean(row[5]) or "ACTIVE").upper(), tms_link=_clean(row[6]),
                            is_dealer=True,
                        )
                        dealers += 1
                    continue

                _, was_created = Broker.objects.update_or_create(
                    broker_number=number,
                    defaults={
                        "name": name,
                        "contact_person": _clean(row[2]),
                        "contact_number": _clean(row[3]),
                        "status": (_clean(row[5]) or "ACTIVE").upper(),
                        "tms_link": _clean(row[6]),
                    },
                )
                created += int(was_created)
                updated += int(not was_created)

        # Drop the cached name map (and the meta blob that embeds it) so the
        # dashboard reflects the new/updated names immediately instead of
        # waiting out the META_TTL window.
        from django.core.cache import cache

        cache.delete("fs_broker_names")
        cache.delete("fs_meta")

        self.stdout.write(self.style.SUCCESS(
            f"Brokers loaded: {created} created, {updated} updated, {dealers} flagged as dealers "
            f"({Broker.objects.count()} total)."
        ))
