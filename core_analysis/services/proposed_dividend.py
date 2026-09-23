"""proposed_dividend.py — scraper for ShareSansar's proposed-dividend table.

The page at /proposed-dividend renders an empty <table> and fills it from a
DataTables server-side feed: the SAME url with ?type=YEARWISE&year=<id>&sector=0
returns JSON. We hit that feed directly — no HTML table parsing, no browser.

Two things about the payload:
  * ``symbol`` / ``companyname`` arrive wrapped in <a> tags, so the text has to
    be pulled out of the anchor.
  * ``bookclose_date`` arrives as "2025-08-11 [Closed]" — date plus a state
    suffix that is split off so the date column stays a real date.

The fiscal-year ids are page-defined (2081/2082 is id 31, not 2081), so they are
scraped from the year <select> rather than hard-coded — a new fiscal year then
picks itself up without a code change.
"""
from __future__ import annotations

import logging
import re
import time

import requests

logger = logging.getLogger(__name__)

BASE_URL = "https://www.sharesansar.com/proposed-dividend"
TIMEOUT = 30
# The feed pages server-side and validates ``length`` against the page's own
# lengthMenu — anything over 50 answers HTTP 202 with an empty payload rather
# than an error, so this must stay at the maximum the site actually allows.
PAGE_SIZE = 50
THROTTLE_SECONDS = 0.6   # be a polite scraper — 25 fiscal years back to back

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "X-Requested-With": "XMLHttpRequest",
    "Referer": BASE_URL,
    "Accept": "application/json, text/javascript, */*; q=0.01",
}

_TAG_RE = re.compile(r"<[^>]+>")
_YEAR_SELECT_RE = re.compile(r'name="year".*?</select>', re.S)
_YEAR_OPTION_RE = re.compile(r'value="(\d+)"[^>]*>\s*([\d/]+)', re.S)
_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")
_STATE_RE = re.compile(r"\[([^\]]+)\]")


def _text(value):
    """Strip the anchor markup upstream wraps symbols / company names in."""
    if not value:
        return ""
    return _TAG_RE.sub("", str(value)).strip()


def _num(value):
    """'19.00' / '' / None / '-' -> float or None."""
    raw = _text(value).replace(",", "")
    if raw in ("", "-", "N/A", "null"):
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _date(value):
    """Pull the ISO date out of a cell, ignoring any '[Closed]' suffix."""
    match = _DATE_RE.search(_text(value))
    return match.group(1) if match else None


def _state(value):
    match = _STATE_RE.search(_text(value))
    return match.group(1).strip()[:40] if match else ""


def _session():
    session = requests.Session()
    session.headers.update(_HEADERS)
    return session


def fiscal_years(session=None):
    """[(year_id, '2081/2082'), …] newest first, scraped from the page's dropdown."""
    session = session or _session()
    # The session sends X-Requested-With for the data feed; with that header the
    # same URL answers with JSON, so it has to be dropped to get the HTML shell
    # the dropdown lives in.
    response = session.get(
        BASE_URL, timeout=TIMEOUT,
        headers={"X-Requested-With": None, "Accept": "text/html"},
    )
    response.raise_for_status()
    block = _YEAR_SELECT_RE.search(response.text)
    if not block:
        raise RuntimeError("Year dropdown not found — the page layout changed.")
    return _YEAR_OPTION_RE.findall(block.group(0))


def fetch_year(year_id, session=None):
    """Every proposed-dividend row for one fiscal-year id, as raw feed dicts."""
    session = session or _session()
    rows, start, draw = [], 0, 1
    while True:
        params = {
            "type": "YEARWISE", "year": year_id, "sector": "0",
            "draw": draw, "start": start, "length": PAGE_SIZE,
        }
        response = session.get(BASE_URL, params=params, timeout=TIMEOUT)
        response.raise_for_status()
        payload = response.json()
        batch = payload.get("data") or []
        rows.extend(batch)
        total = payload.get("recordsFiltered") or payload.get("recordsTotal") or 0
        # An empty page before the end means the feed rejected the request
        # (it answers 202 + empty rather than erroring). Fail loudly instead of
        # silently returning a truncated year.
        if not batch and start < total:
            raise RuntimeError(
                f"Feed returned no rows at offset {start} of {total} (HTTP {response.status_code})."
            )
        start += PAGE_SIZE
        draw += 1
        if start >= total or not batch:
            break
        time.sleep(THROTTLE_SECONDS)
    return rows


def parse_row(row):
    """Feed dict -> model kwargs. Returns None for rows with no usable identity."""
    symbol = _text(row.get("symbol")).upper()
    source_id = row.get("id")
    if not symbol or source_id in (None, ""):
        return None
    return {
        "source_id": int(source_id),
        "symbol": symbol[:20],
        "company_name": _text(row.get("companyname"))[:255],
        "fiscal_year": _text(row.get("year"))[:20],
        "bonus_percent": _num(row.get("bonus_share")),
        "cash_percent": _num(row.get("cash_dividend")),
        "total_percent": _num(row.get("total_dividend")),
        "announcement_date": _date(row.get("announcement_date")),
        "bookclose_date": _date(row.get("bookclose_date")),
        "bookclose_status": _state(row.get("bookclose_date")),
        "distribution_date": _date(row.get("distribution_date")),
        "bonus_listing_date": _date(row.get("bonus_listing_date")),
        "ltp": _num(row.get("close")),
        "price_as_of": _date(row.get("published_date")),
    }


# ── sync ───────────────────────────────────────────────────────────────────

_UPDATE_FIELDS = [
    "symbol", "company_name", "fiscal_year",
    "bonus_percent", "cash_percent", "total_percent",
    "announcement_date", "bookclose_date", "bookclose_status",
    "distribution_date", "bonus_listing_date",
    "ltp", "price_as_of",
]


def sync(years=2, all_years=False, year="", on_progress=None):
    """Fetch and upsert proposed dividends. Returns a stats dict.

    Lives here rather than in the management command so the nightly price sync
    can call the same implementation — one code path, one set of behaviours.

    Rows are keyed on the upstream ``source_id``, so re-running updates the
    existing row as the book-closure / distribution / bonus-listing dates fill
    in on an already-announced dividend.
    """
    # Imported lazily: this module is also imported by scripts that only want
    # the scraping helpers, and importing models at module scope would require
    # Django to be configured first.
    from django.db import connection

    from core_analysis.models import ProposedDividend

    session = _session()
    available = fiscal_years(session)

    if year:
        wanted = year.strip()
        available = [y for y in available if y[1] == wanted]
        if not available:
            raise ValueError(f"Unknown fiscal year {wanted!r}.")
    elif not all_years:
        available = available[: max(1, int(years))]

    objs, seen, failed = [], set(), []
    for index, (year_id, label) in enumerate(available):
        try:
            rows = fetch_year(year_id, session)
        except Exception as exc:
            # One bad year must not lose the years already fetched.
            logger.warning("Proposed dividend sync skipped %s: %s", label, exc)
            failed.append(label)
            if on_progress:
                on_progress(label, None, exc)
            continue
        kept = 0
        for row in rows:
            parsed = parse_row(row)
            # source_id is the unique key; a repeat inside one batch would make
            # bulk_create reject the whole thing.
            if not parsed or parsed["source_id"] in seen:
                continue
            seen.add(parsed["source_id"])
            objs.append(ProposedDividend(**parsed))
            kept += 1
        if on_progress:
            on_progress(label, kept, None)
        if index < len(available) - 1:
            time.sleep(THROTTLE_SECONDS)

    if objs:
        # MySQL's ON DUPLICATE KEY UPDATE has no conflict target, and Django
        # rejects unique_fields on backends without native support.
        kw = {"update_conflicts": True, "update_fields": _UPDATE_FIELDS}
        if connection.features.supports_update_conflicts_with_target:
            kw["unique_fields"] = ["source_id"]
        ProposedDividend.objects.bulk_create(objs, batch_size=500, **kw)

    return {
        "upserted": len(objs),
        "years": [label for _, label in available],
        "failed_years": failed,
        "total_rows": ProposedDividend.objects.count(),
    }
