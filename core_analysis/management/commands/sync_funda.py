"""Sync latest published fundamentals from funda.aurasrp.com.np into our DB.

Usage:
    python manage.py sync_funda SAMINA RSDC FMDBL
    python manage.py sync_funda --all      # every company the source lists

Each symbol's newest reported quarter is upserted into FundaFundamentalSnapshot,
which the Stock 360 overview cards read.
"""

import time

import requests
from django.core.management.base import BaseCommand

from core_analysis.services.funda_financials import sync_symbol, _BASE, _UA


class Command(BaseCommand):
    help = "Pull latest published fundamentals from funda.aurasrp.com.np into the DB."

    def add_arguments(self, parser):
        parser.add_argument("symbols", nargs="*", help="Ticker symbols to sync, e.g. SAMINA RSDC")
        parser.add_argument("--all", action="store_true", help="Sync every symbol the source lists")
        parser.add_argument("--sleep", type=float, default=0.3, help="Seconds between requests")

    def handle(self, *args, **opts):
        symbols = [s.strip().upper() for s in opts["symbols"] if s.strip()]

        if opts["all"]:
            symbols = self._all_symbols() or symbols

        if not symbols:
            self.stderr.write("Give one or more symbols, or pass --all.")
            return

        ok = 0
        for sym in symbols:
            res = sync_symbol(sym)
            if res.get("ok"):
                ok += 1
                self.stdout.write(self.style.SUCCESS(
                    f"[OK]   {sym}: {res['period']} — {res.get('fs_note', '')}"))
            else:
                self.stdout.write(self.style.WARNING(f"[FAIL] {sym}: {res.get('error')}"))
            time.sleep(opts["sleep"])

        self.stdout.write(f"\nSynced {ok}/{len(symbols)} symbols.")

    def _all_symbols(self):
        try:
            r = requests.get(f"{_BASE}/api/companies", headers={"User-Agent": _UA}, timeout=15)
            r.raise_for_status()
            return [c["symbol"] for c in r.json().get("results", []) if c.get("symbol")]
        except Exception as exc:
            self.stderr.write(f"Could not list companies: {exc}")
            return []
