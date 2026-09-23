"""Pull whole-market capitalisation totals from the upstream NEPSE relay.

The per-stock ``market_capitalization`` column intermittently arrives as 0 for
the newest session(s), which zeroes any total summed from it. This endpoint
publishes the exchange's own totals — including the sensitive and float variants
that cannot be derived per-stock at all — so it is the authoritative source.

Usage:
    python manage.py sync_market_cap                 # recent pages only
    python manage.py sync_market_cap --all           # full history
    python manage.py sync_market_cap --base-url http://host:8000
"""
import os

import requests
from django.core.management.base import BaseCommand
from django.db import connection

from core_analysis.models import NepseMarketCapDaily

DEFAULT_API_BASE_URL = os.environ.get("NEPSE_API_BASE_URL", "http://192.168.1.100:8000")
_PATH = "/api/nepse-data/api/market-cap/"
_TIMEOUT = 30


def _d(v):
    if v in (None, "", "null"):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _i(v):
    if v in (None, "", "null"):
        return None
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


class Command(BaseCommand):
    help = "Sync whole-market capitalisation totals from the NEPSE relay."

    def add_arguments(self, p):
        p.add_argument("--base-url", default=DEFAULT_API_BASE_URL)
        p.add_argument(
            "--all", action="store_true",
            help="Walk every page. Without it, only the first --pages are fetched.",
        )
        p.add_argument("--pages", type=int, default=3,
                       help="Pages to fetch when not using --all (default 3).")
        p.add_argument("--limit", type=int, default=200, help="Rows per page.")

    def handle(self, *a, **o):
        base = o["base_url"].rstrip("/")
        url = f"{base}{_PATH}?limit={o['limit']}"
        session = requests.Session()
        session.headers.update({"Accept": "application/json", "User-Agent": "nepse-analytics/1.0"})

        rows, pages = [], 0
        while url:
            try:
                r = session.get(url, timeout=_TIMEOUT)
                r.raise_for_status()
                payload = r.json()
            except Exception as exc:
                self.stderr.write(self.style.ERROR(f"[FAIL] {url}: {exc}"))
                return
            batch = payload.get("results", payload if isinstance(payload, list) else [])
            rows.extend(batch)
            pages += 1
            url = payload.get("next") if isinstance(payload, dict) else None
            if not o["all"] and pages >= o["pages"]:
                break

        objs, seen = [], set()
        for r in rows:
            d = (r.get("business_date") or "").strip()
            # The feed can repeat a date across pages; the model's unique
            # constraint would reject the batch, so collapse to the first.
            if not d or d in seen:
                continue
            seen.add(d)
            objs.append(NepseMarketCapDaily(
                business_date=d,
                market_capitalization=_d(r.get("market_capitalization")),
                sensitive_market_capitalization=_d(r.get("sensitive_market_capitalization")),
                float_market_capitalization=_d(r.get("float_market_capitalization")),
                sensitive_float_market_capitalization=_d(r.get("sensitive_float_market_capitalization")),
                total_turnover=_d(r.get("total_turnover")),
                total_traded_shares=_i(r.get("total_traded_shares")),
                total_transactions=_i(r.get("total_transactions")),
                total_scrips_traded=_i(r.get("total_scrips_traded")),
            ))

        if not objs:
            self.stdout.write(self.style.WARNING("No rows returned."))
            return

        fields = [
            "market_capitalization", "sensitive_market_capitalization",
            "float_market_capitalization", "sensitive_float_market_capitalization",
            "total_turnover", "total_traded_shares", "total_transactions",
            "total_scrips_traded",
        ]
        # MySQL's ON DUPLICATE KEY UPDATE has no conflict target, and Django
        # rejects unique_fields on backends without native support.
        kw = {"update_conflicts": True, "update_fields": fields}
        if connection.features.supports_update_conflicts_with_target:
            kw["unique_fields"] = ["business_date"]

        NepseMarketCapDaily.objects.bulk_create(objs, batch_size=500, **kw)

        newest = NepseMarketCapDaily.objects.order_by("-business_date").first()
        self.stdout.write(self.style.SUCCESS(
            f"[OK] {len(objs)} sessions upserted from {pages} page(s). "
            f"Newest: {newest.business_date} = {newest.market_capitalization}"
        ))
