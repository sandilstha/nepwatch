"""
load_bond_valuations — seed / refresh the ``bond_valuations`` table from the
bundled valuation sheet (``core_analysis/data/bond_valuations.csv``).

Idempotent: rows are upserted on the bond symbol, so re-running with a newer
sheet just refreshes prices / YTM / fair value. Each bond is linked to its
issuing company (CompanyProfile) by matching the security name against a
keyword table below; merged banks map to the surviving listed entity.

    python manage.py load_bond_valuations
    python manage.py load_bond_valuations --path /new/sheet.csv --as-of 2026-09-01
"""
from __future__ import annotations

import csv
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from core_analysis.models import BondValuation, CompanyProfile

DEFAULT_CSV = Path(__file__).resolve().parents[2] / "data" / "bond_valuations.csv"

# (keyword in security name, issuer equity symbol, note). Order matters:
# more specific names first ("Mahalaxmi" before "Laxmi Bank").
ISSUER_RULES = [
    ("mahalaxmi", "MLBL", ""),
    ("l.b.b.l", "LBBL", ""),
    ("laxmi bank", "LSL", "Laxmi Bank merged into Laxmi Sunrise (LSL)"),
    ("sunrise", "LSL", "Sunrise Bank merged into Laxmi Sunrise (LSL)"),
    ("nepal bank", "NBL", ""),
    ("nabil", "NABIL", ""),
    ("nbbl", "NABIL", "Nepal Bangladesh Bank merged into Nabil"),
    ("agricultural", "ADBL", ""),
    ("rbbl", "RBB", ""),
    ("prime", "PCBL", ""),
    ("century", "PCBL", "Century Commercial Bank merged into Prime"),
    ("nepal sbi", "SBI", ""),
    ("prabhu bank", "PRVU", ""),
    ("sbl debenture", "SBL", ""),
    ("nepal investment bank", "NIMB", "Nepal Investment Bank is now NIMB"),
    ("nimb", "NIMB", ""),
    ("global ime", "GBIME", ""),
    ("bok debenture", "GBIME", "Bank of Kathmandu merged into Global IME"),
    ("standard chartered", "SCB", ""),
    ("machhapuch", "MBL", ""),
    ("nmb", "NMB", ""),
    ("everest bank", "EBL", ""),
    ("sanima", "SANIMA", ""),
    ("himalayan", "HBL", ""),
    ("civil bank", "HBL", "Civil Bank merged into Himalayan"),
    ("kbl debenture", "KBL", ""),
    ("ncc debenture", "KBL", "NCC Bank merged into Kumari"),
    ("nic asia", "NICA", ""),
    ("citizens", "CZBIL", ""),
    ("muktinath", "MNBBL", ""),
    ("garima", "GBBL", ""),
    ("kamana", "KSBBL", ""),
    ("shangri", "SADBL", ""),
    ("jyoti", "JBBL", ""),
    ("shine resunga", "SHINE", ""),
    ("icfc", "ICFC", ""),
    ("goodwill", "GFCL", ""),
    ("manjushree", "MFIL", ""),
    ("nifra", "NIFRA", ""),
]

_NUM_RE = re.compile(r"[^0-9.\-]")


def _dec(value):
    """'(6.62)' -> -6.62 ; '1,016.59' -> 1016.59 ; '7.087%' -> 7.087 ; '' -> None."""
    v = (value or "").strip()
    if not v:
        return None
    neg = v.startswith("(") and v.endswith(")")
    v = _NUM_RE.sub("", v)
    if v in ("", "-", "."):
        return None
    try:
        d = Decimal(v)
    except InvalidOperation:
        return None
    return -d if neg else d


def _date(value):
    v = (value or "").strip()
    for fmt in ("%d-%b-%Y", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(v, fmt).date()
        except ValueError:
            continue
    return None


def resolve_issuer(name):
    """Return (CompanyProfile | None, note) for a bond security name."""
    low = (name or "").lower()
    for key, symbol, note in ISSUER_RULES:
        if key in low:
            profile = CompanyProfile.objects.filter(symbol=symbol).first()
            if profile is None:
                return None, f"issuer {symbol} not in company list"
            return profile, note
    return None, "issuer not resolved"


class Command(BaseCommand):
    help = "Load bond / debenture valuations from the bundled CSV."

    def add_arguments(self, parser):
        parser.add_argument("--path", default=str(DEFAULT_CSV))
        parser.add_argument("--as-of", default=None,
                            help="Valuation date (YYYY-MM-DD). Defaults to the date in the sheet footer.")
        parser.add_argument("--source", default="bond valuation sheet")

    def handle(self, *args, **options):
        path = Path(options["path"])
        if not path.exists():
            raise CommandError(f"CSV not found: {path}")

        text = path.read_text(encoding="utf-8-sig")
        as_of = _date(options["as_of"]) if options["as_of"] else None
        if as_of is None:
            m = re.search(r"Valuation date\s+(\d{2}-\w{3}-\d{4})", text)
            as_of = _date(m.group(1)) if m else None

        created = updated = unresolved = 0
        with transaction.atomic():
            for row in csv.DictReader(text.splitlines()):
                symbol = (row.get("Symbol") or "").strip().upper()
                if not symbol:
                    continue
                name = " ".join((row.get("Security Name") or "").split())
                issuer, note = resolve_issuer(name)
                if issuer is None:
                    unresolved += 1
                    self.stdout.write(self.style.WARNING(f"  no issuer for {symbol}: {name} ({note})"))
                spread = _dec(row.get("Spread (bp)"))
                fields = {
                    "security_name": name,
                    "sector": (row.get("Sector") or "").strip(),
                    "issuer": issuer,
                    "issuer_note": note,
                    "coupon_pct": _dec(row.get("Coupon")),
                    "maturity_date": _date(row.get("Maturity")),
                    "years_to_maturity": _dec(row.get("Years")),
                    "issue_size": _dec(row.get("Face Value (Rs)")),
                    "price": _dec(row.get("Price (Rs)")),
                    "price_quality": (row.get("Price Quality") or "").strip(),
                    "ytm_pct": _dec(row.get("YTM")),
                    "benchmark_pct": _dec(row.get("Benchmark")),
                    "spread_bp": int(spread) if spread is not None else None,
                    "fair_value": _dec(row.get("Fair Value (Rs)")),
                    "variance": _dec(row.get("Variance (Rs)")),
                    "variance_pct": _dec(row.get("Variance (%)")),
                    "valuation": (row.get("Valuation") or "").strip(),
                    "macaulay_duration": _dec(row.get("Macaulay")),
                    "modified_duration": _dec(row.get("Modified")),
                    "valuation_date": as_of,
                    "source": options["source"],
                    "metadata": {"sheet_no": (row.get("No.") or "").strip()},
                }
                _, was_created = BondValuation.objects.update_or_create(symbol=symbol, defaults=fields)
                created += was_created
                updated += not was_created

        self.stdout.write(self.style.SUCCESS(
            f"Bonds loaded: {created} created, {updated} updated, {unresolved} without issuer. "
            f"Valuation date {as_of}."
        ))
