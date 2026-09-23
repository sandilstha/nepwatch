import os
from datetime import date
from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode, urljoin, urlparse, urlunparse, parse_qsl

import requests
from django.core.management.base import BaseCommand, CommandError
from django.db import connection

from core_analysis.models import CompanyProfile, StockPriceAdjustment


# Default upstream NEPSE API host. Overridable per-run with --api-base-url, or
# globally via the NEPSE_API_BASE_URL environment variable.
DEFAULT_API_BASE_URL = os.environ.get("NEPSE_API_BASE_URL", "http://192.168.1.100:8000")


class Command(BaseCommand):
    help = "Sync listed companies and adjusted stock prices into local MySQL tables."

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
            choices=["both", "companies", "adjustments"],
            default="both",
            help="Select which dataset to sync.",
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

        if source in ("both", "companies"):
            self._sync_companies(session, api_base_url, max_pages, dry_run)

        if source in ("both", "adjustments"):
            self._sync_adjusted_prices(session, api_base_url, from_date, to_date, batch_size, max_pages, dry_run)

    def _sync_companies(self, session, api_base_url, max_pages, dry_run):
        company_url = _build_url(api_base_url, "/api/listed-companies/companies/", {"format": "json"})
        self.stdout.write(self.style.SUCCESS("Downloading company profiles..."))

        company_by_symbol = {}
        processed = 0
        skipped = 0
        pages = 0

        while company_url:
            pages += 1
            payload, company_url = _fetch_page(session, company_url, "company profile")
            for item in payload.get("results", []):
                symbol = _clean_text(item.get("script_ticker")).upper()
                if not symbol:
                    skipped += 1
                    continue

                if not dry_run:
                    company_by_symbol[symbol] = CompanyProfile(
                        symbol=symbol,
                        security_name=_clean_text(item.get("company_name")) or symbol,
                        sector_name=_clean_text(item.get("sector")) or None,
                        status=_clean_text(item.get("status")) or "Active",
                    )
                processed += 1
            if max_pages and pages >= max_pages:
                break

        if company_by_symbol and not dry_run:
            # Upsert on the `symbol` unique key. MySQL (ON DUPLICATE KEY UPDATE)
            # supports update_conflicts but rejects an explicit conflict target,
            # whereas PostgreSQL/SQLite (ON CONFLICT) require `unique_fields`.
            # Branch on the backend so the same command runs on either.
            upsert_kwargs = {
                "update_conflicts": True,
                "update_fields": ["security_name", "sector_name", "status"],
            }
            if connection.features.supports_update_conflicts_with_target:
                upsert_kwargs["unique_fields"] = ["symbol"]
            CompanyProfile.objects.bulk_create(
                list(company_by_symbol.values()),
                **upsert_kwargs,
            )

        self.stdout.write(
            self.style.SUCCESS(
                f"Company profile sync complete. pages={pages}, processed={processed}, skipped={skipped}"
            )
        )

    def _sync_adjusted_prices(self, session, api_base_url, from_date, to_date, batch_size, max_pages, dry_run):
        price_url = _build_url(
            api_base_url,
            "/api/stock-adjustments/stock-price-adj/",
            _date_query_params(from_date, to_date),
        )
        self.stdout.write(self.style.SUCCESS("Downloading adjusted stock prices..."))

        company_symbols = set(CompanyProfile.objects.values_list("symbol", flat=True))
        if not company_symbols:
            raise CommandError("No company profiles found. Run company sync first or use --source both.")

        price_batch = []
        seen_external_ids = set()
        pages = 0
        processed = 0
        skipped_range = 0
        skipped_company = 0
        skipped_invalid = 0
        pending_payload = None
        pending_next_url = None

        if from_date:
            pending_payload, pending_next_url = _fetch_page(session, price_url, "adjusted price")
            pages += 1
            pending_payload, pending_next_url, seek_pages = _seek_first_page_for_date(
                session,
                price_url,
                pending_payload,
                pending_next_url,
                from_date,
                "adjusted price",
            )
            pages += seek_pages
            price_url = pending_next_url

        while pending_payload is not None or price_url:
            if pending_payload is not None:
                payload = pending_payload
                price_url = pending_next_url
                pending_payload = None
                pending_next_url = None
            else:
                pages += 1
                payload, price_url = _fetch_page(session, price_url, "adjusted price")
            page_dates = []
            for item in payload.get("results", []):
                external_id = _clean_int(item.get("id"))
                business_date = _clean_date(item.get("business_date"))
                symbol = _clean_text(item.get("symbol")).upper()
                if business_date:
                    page_dates.append(business_date)

                if external_id is None or business_date is None or not symbol:
                    skipped_invalid += 1
                    continue
                if external_id in seen_external_ids:
                    skipped_invalid += 1
                    continue
                if not _date_in_range(business_date, from_date, to_date):
                    skipped_range += 1
                    continue
                if symbol not in company_symbols:
                    skipped_company += 1
                    continue

                price_batch.append(
                    StockPriceAdjustment(
                        external_id=external_id,
                        business_date=business_date,
                        company_id=symbol,
                        security_id=_clean_int(item.get("security_id"), default=0),
                        open_price=_clean_decimal(item.get("open_price")),
                        high_price=_clean_decimal(item.get("high_price")),
                        low_price=_clean_decimal(item.get("low_price")),
                        close_price=_clean_decimal(item.get("close_price")),
                        open_price_adj=_clean_decimal(item.get("open_price_adj")),
                        high_price_adj=_clean_decimal(item.get("high_price_adj")),
                        low_price_adj=_clean_decimal(item.get("low_price_adj")),
                        close_price_adj=_clean_decimal(item.get("close_price_adj")),
                        adjustment_factor=_clean_decimal(item.get("adjustment_factor")),
                        average_traded_price_adj=_clean_decimal(
                            item.get("average_traded_price_adj"),
                            default=None,
                        ),
                    )
                )
                seen_external_ids.add(external_id)

                if len(price_batch) >= batch_size:
                    if not dry_run:
                        StockPriceAdjustment.objects.bulk_create(price_batch, ignore_conflicts=True)
                    processed += len(price_batch)
                    self.stdout.write(self.style.WARNING(f"Processed {processed} adjusted price rows..."))
                    price_batch = []
            if max_pages and pages >= max_pages:
                break
            if _can_stop_after_ascending_page(page_dates, to_date):
                price_url = None

        if price_batch:
            if not dry_run:
                StockPriceAdjustment.objects.bulk_create(price_batch, ignore_conflicts=True)
            processed += len(price_batch)

        self.stdout.write(
            self.style.SUCCESS(
                "Adjusted price sync complete. "
                f"pages={pages}, processed={processed}, skipped_range={skipped_range}, "
                f"skipped_missing_company={skipped_company}, skipped_invalid={skipped_invalid}"
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


def _date_query_params(from_date, to_date):
    params = {"format": "json"}
    if from_date:
        params["from_date"] = from_date.isoformat()
        params["business_date__gte"] = from_date.isoformat()
    if to_date:
        params["to_date"] = to_date.isoformat()
        params["business_date__lte"] = to_date.isoformat()
    return params


def _seek_first_page_for_date(session, first_url, first_payload, first_next_url, target_date, label):
    first_dates = _payload_dates(first_payload)
    if not first_dates or max(first_dates) >= target_date:
        return first_payload, first_next_url, 0

    page_size = len(first_payload.get("results", []))
    total_count = _clean_int(first_payload.get("count"), default=0)
    if page_size <= 0 or total_count <= page_size:
        return {"results": []}, None, 0

    total_pages = (total_count + page_size - 1) // page_size
    low = 2
    high = total_pages
    candidate_payload = None
    candidate_next_url = None
    fetches = 0

    while low <= high:
        mid = (low + high) // 2
        mid_payload, mid_next_url = _fetch_page(
            session,
            _page_url(first_url, mid),
            label,
        )
        fetches += 1
        mid_dates = _payload_dates(mid_payload)
        if not mid_dates:
            low = mid + 1
            continue
        if max(mid_dates) >= target_date:
            candidate_payload = mid_payload
            candidate_next_url = mid_next_url
            high = mid - 1
        else:
            low = mid + 1

    if candidate_payload is None:
        return {"results": []}, None, fetches
    return candidate_payload, candidate_next_url, fetches


def _payload_dates(payload):
    return [
        row_date
        for row_date in (_clean_date(item.get("business_date")) for item in payload.get("results", []))
        if row_date is not None
    ]


def _page_url(url, page_number):
    return _merge_query_params(url, {"page": page_number})


def _can_stop_after_ascending_page(page_dates, to_date):
    if not to_date or not page_dates:
        return False
    return min(page_dates) > to_date


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
