"""
sync_floorsheet.py — dedicated sync for the trade-level NEPSE floorsheet feed.

The floorsheet is a separate, very high-volume dataset (tens of thousands of
rows per trading day) so it gets its own command rather than riding along with
``sync_nepse_data``. We never pull the whole feed: instead we walk the requested
date range one calendar day at a time, filtering server-side on
``calculation_date`` and paging through that day's rows, mirroring the strategy
used by ``core_analysis.services.broker_analytics``.

Examples:
    # Sync a single day
    python manage.py sync_floorsheet --from-date 2026-06-17 --to-date 2026-06-17

    # Sync a range
    python manage.py sync_floorsheet --from-date 2026-06-01 --to-date 2026-06-17

    # Sync the latest available trading day (no dates -> upstream's newest date)
    python manage.py sync_floorsheet
"""
import os
import re
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

import requests
from django.core.management.base import BaseCommand, CommandError
from django.utils.dateparse import parse_time

from core_analysis.models import NepseFloorsheet


# Default upstream NEPSE API host. Overridable per-run with --api-base-url, or
# globally via the NEPSE_API_BASE_URL environment variable.
DEFAULT_API_BASE_URL = os.environ.get("NEPSE_API_BASE_URL", "http://192.168.1.100:8000")
FLOORSHEET_PATH = "/api/nepse-data/api/floorsheet/"
MAX_PAGE_SIZE = 10_000
MAX_BATCH_SIZE = 10_000
HARD_MAX_PAGES_PER_DAY = 1_000
MAX_BIGINT = 2**63 - 1
MAX_INTEGER = 2**31 - 1
MAX_RATE = Decimal("100000000")
MAX_AMOUNT = Decimal("10000000000000")
_SYMBOL_RE = re.compile(r"^[A-Z0-9._-]{1,50}$")


class Command(BaseCommand):
    help = "Sync trade-level NEPSE floorsheet rows into the local floorsheet_raw table, day by day."

    def add_arguments(self, parser):
        parser.add_argument(
            "--from-date",
            dest="from_date",
            type=date.fromisoformat,
            help="Start date (inclusive) in YYYY-MM-DD. Defaults to --to-date, or upstream's latest day.",
        )
        parser.add_argument(
            "--to-date",
            dest="to_date",
            type=date.fromisoformat,
            help="End date (inclusive) in YYYY-MM-DD. Defaults to --from-date, or upstream's latest day.",
        )
        parser.add_argument(
            "--api-base-url",
            default=DEFAULT_API_BASE_URL,
            help="Base URL for the upstream NEPSE API.",
        )
        parser.add_argument(
            "--api-token",
            default=os.environ.get("NEPSE_API_TOKEN", ""),
            help="Bearer/Token value for the upstream API. Can also be set with NEPSE_API_TOKEN.",
        )
        parser.add_argument(
            "--api-key",
            default=os.environ.get("NEPSE_API_KEY", ""),
            help="API key for the upstream API. Can also be set with NEPSE_API_KEY.",
        )
        parser.add_argument(
            "--api-cookie",
            default=os.environ.get("NEPSE_API_COOKIE", ""),
            help="Cookie header for session-auth upstream APIs. Can also be set with NEPSE_API_COOKIE.",
        )
        parser.add_argument(
            "--page-size",
            type=int,
            default=int(os.environ.get("NEPSE_FLOORSHEET_PAGE_SIZE", "5000")),
            help="Upstream rows requested per page.",
        )
        parser.add_argument(
            "--batch-size",
            type=int,
            default=2000,
            help="Bulk insert batch size.",
        )
        parser.add_argument(
            "--max-pages",
            type=int,
            default=None,
            help="Stop after this many API pages per day. Useful for smoke tests.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Download and validate data without writing to the database.",
        )

    def handle(self, *args, **options):
        from_date = options.get("from_date")
        to_date = options.get("to_date")
        api_base_url = options["api_base_url"].rstrip("/")
        api_token = (options.get("api_token") or "").strip()
        api_key = (options.get("api_key") or "").strip()
        api_cookie = (options.get("api_cookie") or "").strip()
        page_size = min(max(int(options.get("page_size") or 5000), 1), MAX_PAGE_SIZE)
        batch_size = min(max(int(options.get("batch_size") or 2000), 1), MAX_BATCH_SIZE)
        max_pages = options.get("max_pages")
        if max_pages is not None and max_pages <= 0:
            raise CommandError("--max-pages must be a positive integer.")
        dry_run = bool(options.get("dry_run"))

        # Normalise the date window. A single date works; an open end fills from
        # the other; a fully open window falls back to upstream's latest day.
        if from_date and to_date and from_date > to_date:
            raise CommandError("--from-date cannot be later than --to-date.")
        if from_date and not to_date:
            to_date = from_date
        if to_date and not from_date:
            from_date = to_date

        session = requests.Session()
        _configure_session(
            session,
            api_base_url=api_base_url,
            api_token=api_token,
            api_key=api_key,
            api_cookie=api_cookie,
        )

        if not from_date and not to_date:
            latest = _latest_trading_date(session, api_base_url)
            if not latest:
                raise CommandError(
                    "No --from-date/--to-date given and the latest trading date could not be "
                    "resolved from upstream."
                )
            from_date = to_date = latest
            self.stdout.write(self.style.WARNING(f"No date range supplied; defaulting to latest day {latest}."))

        self.stdout.write(
            self.style.WARNING(
                f"Floorsheet sync from_date={from_date}, to_date={to_date}, api={api_base_url}, "
                f"page_size={page_size}{', dry_run=True' if dry_run else ''}"
            )
        )

        grand_processed = 0
        grand_skipped_invalid = 0
        days = 0
        synced_days = []
        current = from_date
        while current <= to_date:
            processed, skipped_invalid = self._sync_one_day(
                session, api_base_url, current, page_size, batch_size, max_pages, dry_run
            )
            grand_processed += processed
            grand_skipped_invalid += skipped_invalid
            if processed:
                synced_days.append(current)
            days += 1
            current += timedelta(days=1)

        # Refresh the broker-analytics caches so the dashboard reflects the new
        # rows at once and the freshly-synced sessions are pre-warmed for the
        # rolling/fiscal-year windows. Best-effort; never fails the sync.
        if synced_days and not dry_run:
            from core_analysis.services import broker_analytics as ba

            ba.refresh_after_sync(synced_days)
            self.stdout.write(self.style.SUCCESS(f"Warmed broker caches for {len(synced_days)} day(s)."))

        self.stdout.write(
            self.style.SUCCESS(
                f"Floorsheet sync complete. days={days}, processed={grand_processed}, "
                f"skipped_invalid={grand_skipped_invalid}"
            )
        )

    def _sync_one_day(self, session, api_base_url, day, page_size, batch_size, max_pages, dry_run):
        url = _build_url(
            api_base_url,
            FLOORSHEET_PATH,
            {"format": "json", "calculation_date": day.isoformat(), "page_size": page_size},
        )
        batch = []
        seen_ids = set()
        seen_urls = set()
        pages = 0
        processed = 0
        skipped_invalid = 0

        while url:
            if url in seen_urls:
                raise CommandError(f"Floorsheet API pagination loop detected at {url}.")
            seen_urls.add(url)
            pages += 1
            payload, url = _fetch_page(session, url, allowed_origin=api_base_url)
            for item in payload.get("results", []):
                if not isinstance(item, dict):
                    skipped_invalid += 1
                    continue
                row_id = _clean_int(item.get("id"), minimum=1, maximum=MAX_BIGINT)
                business_date = _clean_date(item.get("calculation_date"))
                symbol = _clean_text(item.get("stock_symbol")).upper()
                buyer = _clean_int(item.get("buyer"), minimum=1, maximum=MAX_INTEGER)
                seller = _clean_int(item.get("seller"), minimum=1, maximum=MAX_INTEGER)
                quantity = _clean_int(item.get("quantity"), minimum=1, maximum=MAX_INTEGER)

                # The source 'id' is the primary key; symbol and date are NOT NULL
                # in the table. Everything else mirrors the feed and may be null.
                if (
                    row_id is None
                    or business_date != day
                    or not _SYMBOL_RE.fullmatch(symbol)
                    or quantity is None
                    or (buyer is None and seller is None)
                ):
                    skipped_invalid += 1
                    continue
                if row_id in seen_ids:
                    skipped_invalid += 1
                    continue

                batch.append(
                    NepseFloorsheet(
                        id=row_id,
                        contract_no=_clean_text(item.get("contract_no"), 255) or None,
                        business_date=business_date,
                        stock_symbol=symbol,
                        sector=_clean_text(item.get("sector"), 100) or None,
                        buyer=buyer,
                        seller=seller,
                        quantity=quantity,
                        rate=_clean_decimal(
                            item.get("rate"), default=None, minimum=Decimal("0"), maximum=MAX_RATE
                        ),
                        amount=_clean_decimal(
                            item.get("amount"), default=None, minimum=Decimal("0"), maximum=MAX_AMOUNT
                        ),
                        trade_time=_clean_time(item.get("time")),
                    )
                )
                seen_ids.add(row_id)

                if len(batch) >= batch_size:
                    if not dry_run:
                        NepseFloorsheet.objects.bulk_create(batch, ignore_conflicts=True)
                    processed += len(batch)
                    batch = []
            if max_pages and pages >= max_pages:
                break
            if url and pages >= HARD_MAX_PAGES_PER_DAY:
                raise CommandError(
                    f"Floorsheet API exceeded {HARD_MAX_PAGES_PER_DAY} pages for {day}."
                )

        if batch:
            if not dry_run:
                NepseFloorsheet.objects.bulk_create(batch, ignore_conflicts=True)
            processed += len(batch)

        self.stdout.write(
            self.style.SUCCESS(
                f"  {day}: pages={pages}, processed={processed}, skipped_invalid={skipped_invalid}"
            )
        )
        return processed, skipped_invalid


def _latest_trading_date(session, api_base_url):
    """Most recent calculation_date that has floorsheet rows, or None."""
    url = _build_url(
        api_base_url,
        FLOORSHEET_PATH,
        {"format": "json", "ordering": "-calculation_date", "page_size": 1},
    )
    payload, _next = _fetch_page(session, url, allowed_origin=api_base_url)
    results = payload.get("results") or []
    if not results:
        return None
    return _clean_date(results[0].get("calculation_date"))


def _build_url(api_base_url, path, params=None):
    base = api_base_url.rstrip("/") + "/"
    url = urljoin(base, path.lstrip("/"))
    return _merge_query_params(url, params or {})


def _merge_query_params(url, params):
    parsed = urlparse(url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query.update({key: str(value) for key, value in params.items() if value is not None})
    return urlunparse(parsed._replace(query=urlencode(query)))


def _same_origin(url, allowed_origin):
    candidate = urlparse(url)
    allowed = urlparse(allowed_origin)
    return (
        candidate.scheme in {"http", "https"}
        and candidate.scheme.lower() == allowed.scheme.lower()
        and candidate.netloc.lower() == allowed.netloc.lower()
    )


def _fetch_page(session, url, allowed_origin=None):
    if allowed_origin and not _same_origin(url, allowed_origin):
        raise CommandError("Refusing a floorsheet request outside the configured API origin.")
    try:
        response = session.get(url, timeout=30, allow_redirects=False)
        if 300 <= response.status_code < 400:
            location = response.headers.get("Location")
            redirect_url = urljoin(response.url, location) if location else ""
            if not redirect_url or (allowed_origin and not _same_origin(redirect_url, allowed_origin)):
                raise CommandError("Refusing an unsafe floorsheet API redirect.")
            response = session.get(redirect_url, timeout=30, allow_redirects=False)
            if 300 <= response.status_code < 400:
                raise CommandError("Floorsheet API returned too many redirects.")
        response.raise_for_status()
        payload = response.json()
    except requests.RequestException as exc:
        detail = ""
        response = getattr(exc, "response", None)
        if response is not None and response.status_code == 403:
            body = (response.text or "").strip().replace("\n", " ")[:300]
            detail = (
                " Upstream returned 403 Forbidden. The endpoint may be blocking this request client, "
                "IP address, date range, or configured API base URL."
            )
            if body:
                detail += f" Response: {body}"
        raise CommandError(f"Floorsheet API request failed for {url}: {exc}.{detail}") from exc
    except ValueError as exc:
        raise CommandError(f"Floorsheet API returned invalid JSON for {url}.") from exc

    if not isinstance(payload, dict) or not isinstance(payload.get("results", []), list):
        raise CommandError("Floorsheet API returned an invalid paginated payload.")

    next_url = payload.get("next")
    if next_url:
        if not isinstance(next_url, str):
            raise CommandError("Floorsheet API returned an invalid pagination URL.")
        next_url = urljoin(response.url, next_url)
        if allowed_origin and not _same_origin(next_url, allowed_origin):
            raise CommandError("Refusing an unsafe cross-origin floorsheet pagination URL.")
    return payload, next_url


def _configure_session(session, api_base_url="", api_token="", api_key="", api_cookie=""):
    parsed_base = urlparse(api_base_url or "")
    origin = f"{parsed_base.scheme}://{parsed_base.netloc}" if parsed_base.scheme and parsed_base.netloc else ""
    session.headers.update({
        "Accept": "application/json",
        "Accept-Language": "en-US,en;q=0.9",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0 Safari/537.36"
        ),
    })
    if origin:
        session.headers["Origin"] = origin
        session.headers["Referer"] = f"{origin}/"
    if api_token:
        auth_header = os.environ.get("NEPSE_API_AUTH_HEADER", "Authorization").strip() or "Authorization"
        auth_prefix = os.environ.get("NEPSE_API_AUTH_PREFIX", "Bearer").strip()
        session.headers[auth_header] = f"{auth_prefix} {api_token}".strip()
    if api_key:
        api_key_header = os.environ.get("NEPSE_API_KEY_HEADER", "X-API-Key").strip() or "X-API-Key"
        session.headers[api_key_header] = api_key
    if api_cookie:
        session.headers["Cookie"] = api_cookie


def _clean_text(value, max_length=None):
    cleaned = str(value).strip() if value is not None else ""
    return cleaned[:max_length] if max_length is not None else cleaned


def _clean_date(value):
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _clean_time(value):
    if not value:
        return None
    return parse_time(str(value).strip())


def _clean_decimal(value, default=Decimal("0.00"), minimum=None, maximum=None):
    if value is None:
        return default
    value_str = str(value).replace(",", "").strip()
    if value_str == "" or value_str.lower() in {"none", "null", "nan", "-"}:
        return default
    try:
        cleaned = Decimal(value_str)
    except (InvalidOperation, ValueError):
        return default
    if not cleaned.is_finite():
        return default
    if minimum is not None and cleaned < minimum:
        return default
    if maximum is not None and cleaned >= maximum:
        return default
    return cleaned


def _clean_int(value, default=None, minimum=None, maximum=None):
    if value is None:
        return default
    value_str = str(value).replace(",", "").strip()
    if value_str == "" or value_str.lower() in {"none", "null", "nan", "-"}:
        return default
    try:
        cleaned = Decimal(value_str)
    except (InvalidOperation, ValueError):
        return default
    if not cleaned.is_finite() or cleaned != cleaned.to_integral_value():
        return default
    if minimum is not None and cleaned < Decimal(minimum):
        return default
    if maximum is not None and cleaned > Decimal(maximum):
        return default
    return int(cleaned)
