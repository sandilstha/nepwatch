"""
sync_market_depth — pull TMS top-5 market-depth captures from the feed host
into the local ``market_depth_snapshots`` table.

The feed (``/api/tms-market-depth/``) returns one row per (symbol, capture
time) with the raw top-5 book nested under ``depth``. Rows are keyed on the
feed's own ``id`` so re-running a day is a no-op for rows already stored
(``bulk_create(ignore_conflicts=True)`` — MySQL-safe, see the upsert memory).

    python manage.py sync_market_depth                       # today
    python manage.py sync_market_depth --from-date 2026-08-05 --to-date 2026-09-09
    python manage.py sync_market_depth --symbol MNBBL --from-date 2026-09-01
"""
from __future__ import annotations

import os
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation

import requests
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from core_analysis.models import MarketDepthSnapshot

DEFAULT_API_BASE_URL = os.environ.get("NEPSE_DEPTH_API_BASE_URL", "http://192.168.1.100:3000")
DEPTH_PATH = "/api/tms-market-depth/"
PAGE_SIZE = 5000
HARD_MAX_PAGES = 400


def _dec(v):
    try:
        return Decimal(str(v)) if v not in (None, "") else None
    except InvalidOperation:
        return None


def _level_list(raw):
    out = []
    for x in raw or []:
        try:
            out.append({
                "level": int(x.get("level", len(out))),
                "price": float(x.get("price") or 0),
                "qty": int(x.get("qty") or 0),
                "splits": int(x.get("splits") or 0),
            })
        except (TypeError, ValueError):
            continue
    return out[:5]


def row_to_model(r):
    """Feed row -> unsaved MarketDepthSnapshot, or None when it cannot be parsed."""
    try:
        sid = int(r["id"])
        bdate = date.fromisoformat(r["date"])
        t = (r.get("time") or "00:00:00").split(".")[0]
        captured = datetime.combine(bdate, datetime.strptime(t, "%H:%M:%S").time())
    except (KeyError, TypeError, ValueError):
        return None
    if timezone.is_naive(captured) and timezone.is_aware(timezone.now()):
        captured = timezone.make_aware(captured, timezone.get_current_timezone())
    depth = r.get("depth") or {}
    bids = _level_list(depth.get("bids"))
    asks = _level_list(depth.get("asks"))
    return MarketDepthSnapshot(
        source_id=sid,
        business_date=bdate,
        captured_at=captured,
        symbol=(r.get("security") or "").strip().upper()[:20],
        board=str(r.get("board") or "1")[:10],
        total_bids=int(r.get("total_bids") or 0),
        total_asks=int(r.get("total_ask") or r.get("total_asks") or 0),
        depth={"bids": bids, "asks": asks},
        best_bid=_dec(bids[0]["price"]) if bids else None,
        best_ask=_dec(asks[0]["price"]) if asks else None,
        bid_qty_top5=sum(x["qty"] for x in bids),
        ask_qty_top5=sum(x["qty"] for x in asks),
        bid_splits_top5=sum(x["splits"] for x in bids),
        ask_splits_top5=sum(x["splits"] for x in asks),
        aon_side=str(r.get("aon_side") or "")[:10],
        aon_status=str(r.get("aon_status") or "")[:30],
    )


class Command(BaseCommand):
    help = "Sync TMS top-5 market depth captures into market_depth_snapshots."

    def add_arguments(self, parser):
        parser.add_argument("--from-date", type=date.fromisoformat, dest="from_date")
        parser.add_argument("--to-date", type=date.fromisoformat, dest="to_date")
        parser.add_argument("--symbol", default="", help="Only this script (feed-side filter).")
        parser.add_argument("--api-base-url", default=DEFAULT_API_BASE_URL)
        parser.add_argument("--page-size", type=int, default=PAGE_SIZE)
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options):
        d_from, d_to = options.get("from_date"), options.get("to_date")
        if d_from and d_to and d_from > d_to:
            raise CommandError("--from-date is after --to-date")
        d_to = d_to or d_from or timezone.localdate()
        d_from = d_from or d_to
        base = options["api_base_url"].rstrip("/") + DEPTH_PATH
        page_size = max(100, min(int(options["page_size"]), 10000))
        symbol = (options.get("symbol") or "").strip().upper()
        session = requests.Session()

        grand = 0
        day = d_from
        while day <= d_to:
            if day.weekday() == 5:  # Saturday: NEPSE closed
                day += timedelta(days=1)
                continue
            n = self._sync_day(session, base, day, page_size, symbol, options["dry_run"])
            grand += n
            self.stdout.write(f"  {day}: {n} rows")
            day += timedelta(days=1)
        self.stdout.write(self.style.SUCCESS(f"Market depth sync done: {grand} new rows."))

    def _sync_day(self, session, base, day, page_size, symbol, dry_run):
        params = {"date_from": day.isoformat(), "date_to": day.isoformat(), "page_size": page_size}
        if symbol:
            params["security"] = symbol
        url, pages, written = base, 0, 0
        before = MarketDepthSnapshot.objects.filter(business_date=day).count() if not dry_run else 0
        while url and pages < HARD_MAX_PAGES:
            try:
                resp = session.get(url, params=params if pages == 0 else None, timeout=120)
                resp.raise_for_status()
                payload = resp.json()
            except (requests.RequestException, ValueError) as exc:
                self.stderr.write(self.style.WARNING(f"  {day}: feed error on page {pages + 1}: {exc}"))
                break
            rows = payload.get("results") or []
            objs = [m for m in (row_to_model(r) for r in rows) if m and m.symbol]
            # The feed's date filter is trusted for this endpoint (verified), but
            # guard anyway so a bad page never lands under the wrong day.
            objs = [m for m in objs if m.business_date == day]
            if objs and not dry_run:
                MarketDepthSnapshot.objects.bulk_create(objs, batch_size=2000, ignore_conflicts=True)
            elif dry_run:
                written += len(objs)
            url = payload.get("next")
            pages += 1
        if not dry_run:
            written = MarketDepthSnapshot.objects.filter(business_date=day).count() - before
        return written
