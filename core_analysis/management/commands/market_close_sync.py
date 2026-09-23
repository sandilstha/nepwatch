"""Post-close sync for the Raw Inventory Manager, driven by a scheduler.

Data lands in two waves after NEPSE closes at 15:00 Nepal time: prices settle
almost at once, the floorsheet dribbles in for another hour. The intended
schedule is therefore

    15:15  manage.py market_close_sync            prices + floorsheet
    15:30  manage.py market_close_sync --verify   re-sync only what is incomplete
    16:00  manage.py market_close_sync --verify   final sweep

No pass ever re-runs a sync blindly — including the 15:15 one. Every run first
measures what actually landed for the day and fetches only the gaps, so a
manual sync from the Workbench that already brought the data in means the
scheduled passes do nothing. ``--force`` overrides that. What gets measured:

  * Prices        — are there rows for the day, and is the count in line with
                    the previous trading day (a half-written sync shows up as a
                    thin day, not an empty one).
  * Floorsheet    — every trade is one floorsheet row, and the price feed
                    independently reports ``total_trades`` per stock. Their sum
                    is therefore the expected floorsheet row count for the day.
                    Coverage below the threshold means trades are still missing.

Design notes:
  * Nepal time, not server time. settings.TIME_ZONE is UTC, so "15:15" must be
    resolved against Asia/Kathmandu or the job fires 5h45m off.
  * NEPSE is shut Saturday and Sunday — those days exit immediately.
  * It never raises. A scheduled task that exits non-zero produces a daily
    error balloon; failures are logged and summarised instead.
  * Every underlying sync upserts, so re-running is safe — which is what makes
    the three-pass schedule work.
"""
import os
from datetime import date, timedelta, timezone as dt_timezone

from django.core.management import call_command
from django.core.management.base import BaseCommand
from django.db.models import Max, Sum
from django.utils import timezone

# NEPSE trades on Nepal time; UTC+05:45 has no DST, so a fixed offset is exact.
NPT = dt_timezone(timedelta(hours=5, minutes=45))
# Saturday is the only day NEPSE never trades. Do NOT add Friday: measured over
# the last 260 sessions the weekday split is Sun 36 / Mon 52 / Tue 54 / Wed 52 /
# Thu 50 / Fri 16 / Sat 0 — Friday sessions are irregular but real, and skipping
# them would lose ~16 trading days a year. Sundays and Fridays that turn out to
# be holidays are handled by the no-session check below, not by this set.
# Schedule change, Sep 2026: NEPSE now trades Mon-Fri (it was Sun-Thu, which is
# where the Sunday sessions in the tally above come from). Sunday joins Saturday
# as closed; Friday is a full session, not the irregular one it used to be.
CLOSED_WEEKDAYS = {5, 6}          # Saturday, Sunday (Mon=0)

# Floorsheet is judged complete at this share of the expected trade count. Not
# 100%: the sync legitimately drops a handful of malformed rows each day
# (reported as skipped_invalid), and chasing them would re-sync forever.
FLOOR_COVERAGE_OK = 0.995
# Prices are judged complete at this share of the previous trading day's row
# count — catches a sync that died halfway, which an "any rows?" test misses.
PRICE_ROWS_OK = 0.90


class Command(BaseCommand):
    help = "Post-close price + floorsheet sync with verification; run from a scheduler."

    def add_arguments(self, p):
        p.add_argument("--verify", action="store_true",
                       help="Kept for the scheduled passes; every run verifies first anyway.")
        p.add_argument("--floorsheet-only", action="store_true")
        p.add_argument("--price-only", action="store_true")
        p.add_argument("--date", default="",
                       help="Trading day (YYYY-MM-DD). Defaults to today, Nepal time.")
        p.add_argument("--force", action="store_true",
                       help="Re-sync even if the day is already complete, and run on "
                            "Saturday/Sunday when NEPSE is closed.")
        # Off by default: a separate, slower upstream feed. Worth enabling —
        # StockPriceAdjustment feeds most desks and goes stale silently.
        p.add_argument("--with-adjustments", action="store_true",
                       help="Also sync split/bonus-adjusted prices (sync_and_calculate).")
        # NEPSE REVISES closes after the session. Measured 2026-08-19: 43 of 347
        # closes for the prior session (12.4%) differed from what we stored, and
        # in all 43 cases the revised value matched today's `previous_close`
        # field exactly - so ours were the stale ones, not upstream's.
        #
        # The completeness check compares ROW COUNTS, which a revision never
        # changes, so revisions were invisible and stale closes accumulated
        # (100 across the last 10 sessions). Re-pull a trailing window each run.
        p.add_argument("--revision-window", type=int, default=5,
                       help="Also re-sync the N prior sessions to pick up "
                            "exchange revisions to already-stored closes (0 = off).")

    # ── status ──────────────────────────────────────────────────────────────
    def _status(self, day):
        """What actually landed for ``day``, and whether it looks complete."""
        from core_analysis.models import (NepseDailyStockPrice as P,
                                          NepseFloorsheet as F)

        price_rows = P.objects.filter(business_date=day).count()

        prev = (P.objects.filter(business_date__lt=day)
                .aggregate(m=Max("business_date"))["m"])
        prev_rows = P.objects.filter(business_date=prev).count() if prev else 0

        # Expected floorsheet rows = trades reported by the price feed.
        expected = (P.objects.filter(business_date=day)
                    .aggregate(s=Sum("total_trades"))["s"]) or 0
        floor_rows = F.objects.filter(business_date=day).count()
        coverage = (floor_rows / expected) if expected else 0.0

        price_ok = price_rows > 0 and (not prev_rows or price_rows >= prev_rows * PRICE_ROWS_OK)
        # Without prices there is no expected count, so the floorsheet cannot be
        # judged complete yet — treat it as not-ok so the next pass retries.
        floor_ok = bool(expected) and coverage >= FLOOR_COVERAGE_OK

        return {
            "price_rows": price_rows, "prev_rows": prev_rows, "price_ok": price_ok,
            "expected": expected, "floor_rows": floor_rows,
            "coverage": coverage, "floor_ok": floor_ok,
        }

    # ── main ────────────────────────────────────────────────────────────────
    def handle(self, *a, **o):
        now_npt = timezone.now().astimezone(NPT)
        day = date.fromisoformat(o["date"].strip()) if o["date"].strip() else now_npt.date()
        stamp = now_npt.strftime("%Y-%m-%d %H:%M NPT")

        if not o["force"] and now_npt.weekday() in CLOSED_WEEKDAYS:
            self.stdout.write(f"[{stamp}] NEPSE closed ({now_npt.strftime('%A')}) — nothing to do.")
            return

        do_price = not o["floorsheet_only"]
        do_floor = not o["price_only"]

        # Always look before fetching. If the day's data is already complete —
        # because a manual sync from the Workbench got there first, or an
        # earlier pass succeeded — there is nothing to do, and re-pulling it
        # would only burn a few minutes hammering the upstream feed for rows we
        # already hold. --force is the escape hatch for a deliberate refetch.
        s = self._status(day)
        self.stdout.write(
            f"[{stamp}] check {day}: prices {s['price_rows']} rows "
            f"(prev day {s['prev_rows']}) -> {'OK' if s['price_ok'] else 'INCOMPLETE'} | "
            f"floorsheet {s['floor_rows']}/{s['expected']} "
            f"({s['coverage'] * 100:.2f}%) -> {'OK' if s['floor_ok'] else 'INCOMPLETE'}"
        )
        if not o["force"]:
            do_price = do_price and not s["price_ok"]
            do_floor = do_floor and not s["floor_ok"]
        if not do_price and not do_floor:
            self.stdout.write(self.style.SUCCESS(
                f"[{stamp}] {day}: already up to date — nothing to sync."))
            return

        results = []
        if do_price:
            results.append(self._run("prices", "sync_nepse_data",
                                     source="both", from_date=day))
            window = max(0, int(o.get("revision_window") or 0))
            if window:
                results.append(self._resync_revisions(day, window))
            if o["with_adjustments"]:
                results.append(self._run("adjusted", "sync_and_calculate",
                                         source="adjustments", from_date=day))
        if do_floor:
            results.append(self._run("floorsheet", "sync_floorsheet",
                                     from_date=day, to_date=day))

        # Warm the caches the desks read, now that fresh data is in. Runs after
        # the syncs so it caches the NEW session, and only when something was
        # actually synced. Two separate problems this solves:
        #
        #  * contributors are fetched off-thread (the upstream call takes ~7s),
        #    so the first Market Insights build after any restart has none and
        #    the panels sit in a "loading" state until it lands;
        #  * the floorsheet window aggregate costs 13-18s to build cold and is
        #    shared by EVERY tab on the broker desk, so whoever opens it first
        #    after a restart pays that on a single-process server.
        #
        # Best-effort by design: a warming failure must never mark the sync bad.
        self._warm_caches()
        # Forward-test record: snapshot today's A/D scores AFTER the day's data
        # landed. Every historical split has been consumed by feature selection,
        # so the only clean test left is scores logged BEFORE their outcomes
        # exist. Append-only, one file per session, never rewritten.
        self._snapshot_ad_scores()

        after = self._status(day)
        ok = [n for n, good, _ in results if good]
        bad = [(n, e) for n, good, e in results if not good]

        line = (f"[{stamp}] {day}: ran " + (", ".join(ok) or "nothing") +
                f" | prices {after['price_rows']} rows, floorsheet "
                f"{after['floor_rows']}/{after['expected']} ({after['coverage'] * 100:.2f}%)")
        if bad:
            line += " | FAILED: " + ", ".join(f"{n} ({e})" for n, e in bad)
            self.stdout.write(self.style.WARNING(line))
        elif after["price_rows"] == 0:
            # The syncs ran cleanly and the feed returned nothing at all: this is
            # a public holiday, not a failure. Nepal has a lot of them, and many
            # calendar Sundays/Fridays are closed. Without this branch every pass
            # would log INCOMPLETE and retry all afternoon on every holiday.
            self.stdout.write(self.style.SUCCESS(
                f"[{stamp}] {day}: no trading session (holiday) — nothing to sync."))
        elif not (after["price_ok"] and after["floor_ok"]):
            # Ran cleanly but data is still short — the next pass will retry.
            self.stdout.write(self.style.WARNING(line + " | still incomplete, next pass will retry"))
        else:
            self.stdout.write(self.style.SUCCESS(line))

    def _run(self, label, command, **kwargs):
        """Call one sync, swallowing failures so a scheduled run never exits non-zero."""
        try:
            call_command(command, **kwargs)
            return (label, True, None)
        except Exception as exc:   # pragma: no cover - upstream is best-effort
            self.stderr.write(self.style.ERROR(f"  {label} sync failed: {exc}"))
            return (label, False, str(exc)[:120])

    def _resync_revisions(self, day, window):
        """Re-pull the prior ``window`` sessions so exchange revisions land.

        Returns the same (label, ok, err) tuple as ``_run`` so it slots into the
        results summary. Cheap: ~350 rows per session, and the sync upserts, so
        unchanged rows are simply rewritten with identical values.
        """
        from core_analysis.models import NepseDailyStockPrice as P

        prior = list(
            P.objects.filter(business_date__lt=day)
            .order_by("-business_date")
            .values_list("business_date", flat=True).distinct()[:window]
        )
        if not prior:
            return ("revisions", True, None)
        start, end = min(prior), max(prior)
        before = {
            (r["symbol"], r["business_date"]): r["close_price"]
            for r in P.objects.filter(business_date__gte=start, business_date__lte=end)
            .values("symbol", "business_date", "close_price")
        }
        # Pass date OBJECTS, not ISO strings: sync_nepse_data calls .isoformat()
        # on these itself, so a string here raises AttributeError inside the sync.
        # update_existing is the whole point: the default path uses
        # ignore_conflicts, so an already-stored row can never be corrected.
        label, ok, err = self._run(
            "revisions", "sync_nepse_data", source="stocks",
            from_date=start, to_date=end, update_existing=True)
        if not ok:
            return (label, ok, err)
        after = {
            (r["symbol"], r["business_date"]): r["close_price"]
            for r in P.objects.filter(business_date__gte=start, business_date__lte=end)
            .values("symbol", "business_date", "close_price")
        }
        changed = [k for k, v in after.items()
                   if k in before and before[k] is not None and v is not None
                   and abs(float(v) - float(before[k])) > 0.01]
        if changed:
            sample = ", ".join(f"{s} {d}" for s, d in sorted(changed)[:5])
            self.stdout.write(self.style.WARNING(
                f"    revisions: {len(changed)} close(s) corrected over "
                f"{start}..{end} ({sample}{' …' if len(changed) > 5 else ''})"))
        else:
            self.stdout.write(f"    revisions: none over {start}..{end}")
        return (label, True, None)

    def _snapshot_ad_scores(self):
        """Append-only daily snapshot of the A/D scan for a true walk-forward test.

        Writes logs/ad_forward/<as_of>.jsonl with one row per scored scrip. The
        file is keyed on the scan's own as_of date and NEVER overwritten: a
        forward test is only clean if the record provably predates the outcome,
        so an existing file is left untouched even on --force re-runs.

        Reads the scan over HTTP from the running server (same reason as
        _warm_caches: LocMem cache is per-process, and the server has just been
        warmed, so this is a cache hit there). Best-effort — a failure logs a
        warning and never marks the sync bad.
        """
        import json
        import urllib.request
        from pathlib import Path

        base = os.environ.get("WARM_BASE_URL", "http://127.0.0.1:8501").rstrip("/")
        try:
            req = urllib.request.Request(
                base + "/floorsheet/api/accumulation/?range=1m",
                headers={"User-Agent": "market_close_sync/forward-log"})
            with urllib.request.urlopen(req, timeout=300) as r:
                scan = json.loads(r.read())
        except Exception as exc:
            self.stdout.write(self.style.WARNING(f"    forward-log: scan fetch failed ({exc})"))
            return
        if not scan.get("ok") or not scan.get("as_of"):
            self.stdout.write(self.style.WARNING(
                f"    forward-log: scan not usable ({scan.get('reason', 'no as_of')})"))
            return

        out_dir = Path("logs") / "ad_forward"
        out_dir.mkdir(parents=True, exist_ok=True)
        out = out_dir / f"{scan['as_of']}.jsonl"
        if out.exists():
            self.stdout.write(f"    forward-log: {out.name} already exists — kept as-is")
            return

        fields = ("symbol", "sector", "score", "percentile", "band",
                  "absorb_pct", "top1_pct", "sell_hhi", "group_n",
                  "buyers_n", "sellers_n", "price_chg_pct", "volume")
        try:
            with out.open("w", encoding="utf-8") as f:
                f.write(json.dumps({
                    "_meta": {"as_of": scan["as_of"], "sessions": scan.get("sessions"),
                              "universe": scan.get("universe"), "range": "1m",
                              "payload_version": scan.get("payload_version"),
                              "written_by": "market_close_sync"}}) + "\n")
                for row in scan.get("rows") or []:
                    f.write(json.dumps({k: row.get(k) for k in fields}) + "\n")
            self.stdout.write(
                f"    forward-log: wrote {out.name} ({len(scan.get('rows') or [])} scrips)")
        except Exception as exc:
            self.stdout.write(self.style.WARNING(f"    forward-log write failed: {exc}"))

    def _warm_caches(self):
        """Pre-build the expensive caches so no human pays the cold build.

        Warming happens over HTTP against the RUNNING SERVER, not in-process.
        That is not a style choice: CACHES uses LocMemCache, which is per-process
        (see the note in settings.py). Calling the services directly from this
        management command would fill THIS process's cache and then throw it away
        on exit, leaving the web server exactly as cold as before. Requesting the
        endpoints makes the server build and cache them in its own process.

        Set REDIS_URL to share one cache across processes and this could then be
        done in-process instead.

        Entirely best-effort: the desk is merely slow if warming fails, so a
        failure here must never mark the sync itself bad.
        """
        import json
        import time
        import urllib.request

        base = os.environ.get("WARM_BASE_URL", "http://127.0.0.1:8501").rstrip("/")

        def _get(path, timeout=300):
            # Deliberately NOT requesting gzip: the point is to make the server
            # BUILD and cache the payload, and asking for gzip only means this
            # side has to decompress before it can read the result back.
            req = urllib.request.Request(
                base + path, headers={"User-Agent": "market_close_sync/warm"})
            t0 = time.time()
            with urllib.request.urlopen(req, timeout=timeout) as r:
                body = r.read()
            return time.time() - t0, body

        try:
            _get("/insights/api/?force=1")
        except Exception as exc:
            self.stdout.write(self.style.WARNING(
                f"    warm: server unreachable at {base} ({exc}) — skipped"))
            return

        # Contributors are fetched OFF-THREAD, so the forced build above only
        # kicks the refresh off. Wait for it (upstream takes ~7s), then rebuild
        # so the cached payload actually contains them.
        time.sleep(9)
        try:
            took, body = _get("/insights/api/?force=1")
            c = (json.loads(body).get("contributors") or {})
            n = len(c.get("positive") or [])
            self.stdout.write(
                f"    warmed insights in {took:.1f}s (contributors: {n} up-movers"
                + (", still pending" if c.get("pending") else "") + ")")
        except Exception as exc:
            self.stdout.write(self.style.WARNING(f"    insights warm failed: {exc}"))

        # Floorsheet window aggregates + the A/D scan built on them. This is the
        # 13-18s cold build shared by every tab on the broker desk.
        for rng in ("1w", "1m", "3m"):
            try:
                took, body = _get(f"/floorsheet/api/accumulation/?range={rng}")
                d = json.loads(body)
                self.stdout.write(
                    f"    warmed A/D {rng}: {len(d.get('rows') or [])} scored in {took:.1f}s"
                    if d.get("ok") else
                    f"    A/D {rng} not built: {d.get('reason', 'unknown')}")
            except Exception as exc:
                self.stdout.write(self.style.WARNING(f"    A/D {rng} warm failed: {exc}"))
