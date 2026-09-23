import os
from datetime import date
from decimal import Decimal, InvalidOperation
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

import requests
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from core_analysis.models import NepseDailyStockPrice, NepseMarketIndex
from core_analysis.management.commands.backfill_companies import (
    backfill_missing_company_profiles,
)


# Default upstream NEPSE API host. Overridable per-run with --api-base-url, or
# globally via the NEPSE_API_BASE_URL environment variable.
DEFAULT_API_BASE_URL = os.environ.get("NEPSE_API_BASE_URL", "http://192.168.1.100:8000")


# Everything except the pk and the (business_date, symbol) unique key. api_id is
# itself unique, so it is refreshed too - a revised row can carry a new upstream id.
_STOCK_UPDATE_FIELDS = [
    "api_id", "security_id", "security_name",
    "open_price", "high_price", "low_price", "close_price", "previous_close",
    "average_traded_price", "total_traded_quantity", "total_traded_value",
    "total_trades", "market_capitalization",
    "fifty_two_week_high", "fifty_two_week_low", "last_updated_time",
]


def _upsert_stock_rows(model, batch, update_existing):
    """Insert a batch, optionally overwriting rows that already exist.

    ignore_conflicts is right for the daily pass (today's rows are new, and
    skipping is fast). It is WRONG for a revision re-sync: an existing row can
    never be corrected, so a stale close survives every future sync. When
    update_existing is set, switch to a real upsert.

    MySQL note: its ON DUPLICATE KEY UPDATE has no conflict-target clause, so
    Django rejects unique_fields there. Branch on the backend feature flag
    rather than hard-coding either dialect.
    """
    if not update_existing:
        model.objects.bulk_create(batch, ignore_conflicts=True)
        return
    from django.db import connection

    kwargs = {"update_conflicts": True, "update_fields": _STOCK_UPDATE_FIELDS}
    if connection.features.supports_update_conflicts_with_target:
        kwargs["unique_fields"] = ["business_date", "symbol"]
    model.objects.bulk_create(batch, **kwargs)


class Command(BaseCommand):
    help = "Sync raw NEPSE daily stock prices and market indices into local MySQL tables."

    def add_arguments(self, parser):
        parser.add_argument(
            "--from-date",
            dest="from_date",
            type=date.fromisoformat,
            help="Start date filter (inclusive) in YYYY-MM-DD format.",
        )
        parser.add_argument(
            "--to-date",
            dest="to_date",
            type=date.fromisoformat,
            help="End date filter (inclusive) in YYYY-MM-DD format.",
        )
        parser.add_argument(
            "--source",
            choices=["both", "stocks", "indices"],
            default="both",
            help="Select which dataset to sync.",
        )

        parser.add_argument(
            "--update-existing",
            action="store_true",
            help=(
                "Overwrite rows that already exist instead of skipping them. "
                "NEPSE revises closes after the session, and the default "
                "ignore_conflicts path silently drops those corrections."
            ),
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
            "--batch-size",
            type=int,
            default=2000,
            help="Bulk insert batch size.",
        )
        parser.add_argument(
            "--max-pages",
            type=int,
            default=None,
            help="Stop after this many API pages per selected dataset. Useful for smoke tests.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Download and validate data without writing to the database.",
        )
        parser.add_argument(
            "--no-dividends",
            action="store_true",
            help="Skip the proposed-dividend refresh that normally follows the price sync.",
        )

    def handle(self, *args, **options):
        from_date = options.get("from_date")
        to_date = options.get("to_date")
        source = options.get("source", "both")
        api_base_url = options["api_base_url"].rstrip("/")
        api_token = (options.get("api_token") or "").strip()
        api_key = (options.get("api_key") or "").strip()
        api_cookie = (options.get("api_cookie") or "").strip()
        batch_size = max(int(options.get("batch_size") or 2000), 1)
        max_pages = options.get("max_pages")
        dry_run = bool(options.get("dry_run"))

        if from_date and to_date and from_date > to_date:
            raise CommandError("--from-date cannot be later than --to-date.")

        session = requests.Session()
        _configure_session(
            session,
            api_base_url=api_base_url,
            api_token=api_token,
            api_key=api_key,
            api_cookie=api_cookie,
        )
        self.stdout.write(
            self.style.WARNING(
                f"Sync scope={source}, from_date={from_date or 'None'}, to_date={to_date or 'None'}, api={api_base_url}"
                f"{', dry_run=True' if dry_run else ''}"
            )
        )

        if source in ("both", "stocks"):
            self._sync_stocks(session, api_base_url, from_date, to_date, batch_size,
                              max_pages, dry_run, options.get("update_existing", False))

        if source in ("both", "indices"):
            self._sync_indices(session, api_base_url, from_date, to_date, batch_size, max_pages, dry_run)

        # Safety net: a freshly listed company trades (so it lands in the price
        # table) before the listed-companies API gives it a profile, which would
        # leave it invisible in the dropdown. Backfill provisional profiles for
        # any recently-traded symbol that still has none. Only runs when stock
        # prices were just synced and not in dry-run.
        if source in ("both", "stocks") and not dry_run:
            created = backfill_missing_company_profiles()
            if created:
                self.stdout.write(
                    self.style.SUCCESS(
                        f"Backfilled {len(created)} new company profile(s): {', '.join(created)}"
                    )
                )

        # Proposed dividends ride along with the daily sync. Only the newest two
        # fiscal years are refreshed — closed years never change, and the current
        # one keeps filling in book-closure / distribution dates on dividends
        # already announced. Use `sync_proposed_dividend --all` for a backfill.
        if not dry_run and not options.get("no_dividends"):
            self._sync_proposed_dividends()

    def _sync_proposed_dividends(self):
        """Refresh proposed dividends. Never fails the price sync.

        This one reaches a third-party website rather than the NEPSE relay, so
        it is the most likely step to break (layout change, rate limit, site
        down). Prices are the point of this command — a dividend failure is
        reported and swallowed.
        """
        from core_analysis.services import proposed_dividend

        self.stdout.write(self.style.SUCCESS("Syncing proposed dividends..."))
        try:
            stats = proposed_dividend.sync(years=2)
        except Exception as exc:
            self.stdout.write(self.style.WARNING(f"Proposed dividend sync failed (prices are unaffected): {exc}"))
            return
        self.stdout.write(self.style.SUCCESS(
            f"Proposed dividends: {stats['upserted']} upserted, {stats['total_rows']} total"
            + (f" (skipped {', '.join(stats['failed_years'])})" if stats["failed_years"] else "")
        ))

    def _sync_stocks(self, session, api_base_url, from_date, to_date, batch_size,
                     max_pages, dry_run, update_existing=False):
        stock_url = _build_url(
            api_base_url,
            "/api/nepse-data/api/stock-prices/",
            _date_query_params(from_date, to_date),
        )
        self.stdout.write(self.style.SUCCESS("Opening stock prices pipeline stream..."))

        stock_batch = []
        seen_api_ids = set()
        pages = 0
        processed = 0
        skipped_range = 0
        skipped_invalid = 0
        saw_date_at_or_after_from = from_date is None

        while stock_url:
            pages += 1
            payload, stock_url = _fetch_page(session, stock_url, "stock price")
            page_dates = []
            for item in payload.get("results", []):
                api_id = _clean_int(item.get("id"))
                business_date = _clean_date(item.get("business_date"))
                symbol = _clean_text(item.get("symbol")).upper()
                if business_date:
                    page_dates.append(business_date)

                if api_id is None or business_date is None or not symbol:
                    skipped_invalid += 1
                    continue
                if api_id in seen_api_ids:
                    skipped_invalid += 1
                    continue
                if not _date_in_range(business_date, from_date, to_date):
                    skipped_range += 1
                    continue

                stock_batch.append(
                    NepseDailyStockPrice(
                        api_id=api_id,
                        business_date=business_date,
                        security_id=_clean_text(item.get("security_id")),
                        symbol=symbol,
                        security_name=_clean_text(item.get("security_name")) or symbol,
                        open_price=_clean_decimal(item.get("open_price")),
                        high_price=_clean_decimal(item.get("high_price")),
                        low_price=_clean_decimal(item.get("low_price")),
                        close_price=_clean_decimal(item.get("close_price")),
                        previous_close=_clean_decimal(item.get("previous_close")),
                        average_traded_price=_clean_decimal(item.get("average_traded_price")),
                        total_traded_quantity=_clean_int(item.get("total_traded_quantity"), default=0),
                        total_traded_value=_clean_decimal(item.get("total_traded_value")),
                        total_trades=_clean_int(item.get("total_trades"), default=0),
                        market_capitalization=_clean_decimal(item.get("market_capitalization")),
                        fifty_two_week_high=_clean_decimal(item.get("fifty_two_week_high")),
                        fifty_two_week_low=_clean_decimal(item.get("fifty_two_week_low")),
                        last_updated_time=_clean_datetime(item.get("last_updated_time")),
                    )
                )
                seen_api_ids.add(api_id)

                if len(stock_batch) >= batch_size:
                    if not dry_run:
                        _upsert_stock_rows(NepseDailyStockPrice, stock_batch, update_existing)
                    processed += len(stock_batch)
                    self.stdout.write(self.style.WARNING(f"Processed {processed} stock records..."))
                    stock_batch = []
            if max_pages and pages >= max_pages:
                break
            if _can_stop_after_page(page_dates, from_date, saw_date_at_or_after_from):
                stock_url = None
            if from_date and any(row_date >= from_date for row_date in page_dates):
                saw_date_at_or_after_from = True

        if stock_batch:
            if not dry_run:
                _upsert_stock_rows(NepseDailyStockPrice, stock_batch, update_existing)
            processed += len(stock_batch)

        self.stdout.write(
            self.style.SUCCESS(
                f"Stock price sync complete. pages={pages}, processed={processed}, "
                f"skipped_range={skipped_range}, skipped_invalid={skipped_invalid}"
            )
        )

    def _sync_indices(self, session, api_base_url, from_date, to_date, batch_size, max_pages, dry_run):
        index_url = _build_url(
            api_base_url,
            "/api/nepse-data/api/indices/",
            _date_query_params(from_date, to_date, date_field="date"),
        )
        self.stdout.write(self.style.SUCCESS("Opening aggregate market index pipeline stream..."))

        index_batch = []
        seen_api_ids = set()
        pages = 0
        processed = 0
        skipped_range = 0
        skipped_invalid = 0
        saw_date_at_or_after_from = from_date is None

        while index_url:
            pages += 1
            payload, index_url = _fetch_page(session, index_url, "market index")
            page_dates = []
            for item in payload.get("results", []):
                api_id = _clean_int(item.get("id"))
                business_date = _clean_date(item.get("date"))
                sector_name = _clean_text(item.get("sector")).upper()
                if business_date:
                    page_dates.append(business_date)

                if api_id is None or business_date is None or not sector_name:
                    skipped_invalid += 1
                    continue
                if api_id in seen_api_ids:
                    skipped_invalid += 1
                    continue
                if not _date_in_range(business_date, from_date, to_date):
                    skipped_range += 1
                    continue

                index_batch.append(
                    NepseMarketIndex(
                        api_id=api_id,
                        business_date=business_date,
                        sector_name=sector_name,
                        open_index=_clean_decimal(item.get("open")),
                        high_index=_clean_decimal(item.get("high")),
                        low_index=_clean_decimal(item.get("low")),
                        close_index=_clean_decimal(item.get("close")),
                        absolute_change=_clean_decimal(item.get("absolute_change")),
                        percentage_change=_clean_decimal(item.get("percentage_change")),
                        turnover_values=_clean_decimal(item.get("turnover_values")),
                        turnover_volume=_clean_int(item.get("turnover_volume"), default=0),
                        total_transaction=_clean_int(item.get("total_transaction"), default=0),
                        number_52_weeks_high=_clean_decimal(item.get("number_52_weeks_high")),
                        number_52_weeks_low=_clean_decimal(item.get("number_52_weeks_low")),
                        created_at=_clean_datetime(item.get("created_at")),
                    )
                )
                seen_api_ids.add(api_id)

                if len(index_batch) >= batch_size:
                    if not dry_run:
                        NepseMarketIndex.objects.bulk_create(index_batch, ignore_conflicts=True)
                    processed += len(index_batch)
                    self.stdout.write(self.style.WARNING(f"Processed {processed} index records..."))
                    index_batch = []
            if max_pages and pages >= max_pages:
                break
            if _can_stop_after_page(page_dates, from_date, saw_date_at_or_after_from):
                index_url = None
            if from_date and any(row_date >= from_date for row_date in page_dates):
                saw_date_at_or_after_from = True

        if index_batch:
            if not dry_run:
                NepseMarketIndex.objects.bulk_create(index_batch, ignore_conflicts=True)
            processed += len(index_batch)

        self.stdout.write(
            self.style.SUCCESS(
                f"Market index sync complete. pages={pages}, processed={processed}, "
                f"skipped_range={skipped_range}, skipped_invalid={skipped_invalid}"
            )
        )


def _build_url(api_base_url, path, params=None):
    base = api_base_url.rstrip("/") + "/"
    url = urljoin(base, path.lstrip("/"))
    return _merge_query_params(url, params or {})


def _merge_query_params(url, params):
    parsed = urlparse(url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query.update({key: str(value) for key, value in params.items() if value is not None})
    return urlunparse(parsed._replace(query=urlencode(query)))


def _date_query_params(from_date, to_date, date_field="business_date"):
    params = {"format": "json"}
    if from_date:
        params["from_date"] = from_date.isoformat()
        params[f"{date_field}__gte"] = from_date.isoformat()
    if to_date:
        params["to_date"] = to_date.isoformat()
        params[f"{date_field}__lte"] = to_date.isoformat()
    return params


def _can_stop_after_page(page_dates, from_date, saw_date_at_or_after_from):
    if not from_date or not page_dates or not saw_date_at_or_after_from:
        return False
    return min(page_dates) < from_date and max(page_dates) < from_date


def _fetch_page(session, url, label):
    try:
        response = session.get(url, timeout=30)
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
        raise CommandError(f"{label.title()} API request failed for {url}: {exc}.{detail}") from exc
    except ValueError as exc:
        raise CommandError(f"{label.title()} API returned invalid JSON for {url}.") from exc

    next_url = payload.get("next")
    if next_url:
        next_url = urljoin(response.url, next_url)
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


def _date_in_range(row_date, from_date, to_date):
    if from_date and row_date < from_date:
        return False
    if to_date and row_date > to_date:
        return False
    return True


def _clean_text(value):
    return str(value).strip() if value is not None else ""


def _clean_date(value):
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _clean_datetime(value):
    if not value:
        return timezone.now()
    parsed = parse_datetime(str(value))
    if parsed is None:
        return timezone.now()
    if timezone.is_naive(parsed):
        return timezone.make_aware(parsed, timezone.get_current_timezone())
    return parsed


def _clean_decimal(value, default=Decimal("0.00")):
    if value is None:
        return default
    value_str = str(value).replace(",", "").strip()
    if value_str == "" or value_str.lower() in {"none", "null", "nan", "-"}:
        return default
    try:
        return Decimal(value_str)
    except (InvalidOperation, ValueError):
        return default


def _clean_int(value, default=None):
    if value is None:
        return default
    value_str = str(value).replace(",", "").strip()
    if value_str == "" or value_str.lower() in {"none", "null", "nan", "-"}:
        return default
    try:
        return int(Decimal(value_str))
    except (InvalidOperation, ValueError):
        return default
