"""Fundamentals pulled from the funda.aurasrp.com.np financials API.

The site exposes per-company financial statements (KeyStats, Balance Sheet,
Income Statement) at ``/api/financials/<SYMBOL>``. For Stock 360 we only need
the KeyStats block, which already carries every ratio the overview cards show —
EPS, P/E, book value, ROE/ROA, net income, revenue, margins, DPS, shares.

This module fetches that payload, picks the LATEST reported quarter, and maps it
onto the same canonical ``ks`` keys the local ``/fundamentals/api/`` produces, so
the client renders it with no change. It also builds a small multi-year trend
(annual = Q4 rows) for the revenue / net-income growth figures. Network and parse
failures are swallowed and return ``None`` — the page then falls back to whatever
local fundamentals exist.
"""

import logging

import requests
from django.core.cache import cache

logger = logging.getLogger(__name__)

_BASE = "https://funda.aurasrp.com.np"
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
_TIMEOUT = 8          # seconds — one desk call, keep it snappy
_CACHE_TTL = 6 * 60 * 60   # 6h; statements change at most quarterly

# funda KeyStats field  ->  canonical metric key used by the Stock 360 cards.
# Rate metrics (roe/roa/gross_margin) stay FRACTIONS to match the local feed;
# money lines (net_income/revenue) stay in Rs '000 as the source reports them.
_KS_MAP = {
    "EpsAnnualized": "eps",
    "ReportedPeAnnualized": "pe",
    "BookValuePerShare": "bvps",
    "Mps": "price",
    "DividendPerShare": "dps",
    "ReturnOnEquity": "roe",
    "ReturnOnAsset": "roa",
    "NetIncome": "net_income",
    "TotalRevenue": "revenue",
    "GrossProfitMargin": "gross_margin",
    "NetIncomeMargin": "net_margin",
    "OutstandingShares": "shares",
}


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f


def _period_key(entry):
    """Sortable (year, quarter) so max() picks the newest reported period."""
    year = str(entry.get("Year") or "")
    try:
        q = int(entry.get("Quarter") or 0)
    except (TypeError, ValueError):
        q = 0
    return (year, q)


def _fetch(symbol):
    url = f"{_BASE}/api/financials/{symbol}"
    resp = requests.get(url, headers={"User-Agent": _UA}, timeout=_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def _build_trend(keystats):
    """Annual (Q4) revenue / net-income series, oldest→newest, for growth calcs.

    Shaped exactly like the local feed's ``trend`` so the client's cagr helper
    reads it unchanged: ``[{"label": ..., "points": [{"fy", "value"}]}]``.
    """
    annual = {}
    for e in keystats:
        try:
            if int(e.get("Quarter") or 0) != 4:
                continue
        except (TypeError, ValueError):
            continue
        fy = str(e.get("Year") or "")
        if fy:
            annual[fy] = e  # later rows overwrite → one entry per year
    years = sorted(annual)
    out = []
    for label, field in (("Total Revenue", "TotalRevenue"), ("Net Income", "NetIncome")):
        points = [
            {"fy": fy, "value": _num(annual[fy].get(field))}
            for fy in years
            if _num(annual[fy].get(field)) is not None
        ]
        if points:
            out.append({"label": label, "points": points})
    return out


def latest_keystats(symbol):
    """Latest-quarter KeyStats for ``symbol`` as ``{ok, ks, trend, period, source}``.

    Returns ``None`` on any network/parse failure or when the symbol has no
    KeyStats. Cached per symbol for a few hours.
    """
    sym = (symbol or "").strip().upper()
    if not sym:
        return None

    cache_key = f"funda:ks:v1:{sym}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached or None  # empty dict sentinel means "known-missing"

    try:
        data = _fetch(sym)
    except Exception:  # pragma: no cover - external service is best-effort
        logger.warning("funda financials fetch failed for %s", sym, exc_info=True)
        return None

    keystats = ((data or {}).get("statements") or {}).get("keyStats") or []
    if not keystats:
        cache.set(cache_key, {}, _CACHE_TTL)  # remember the miss, don't refetch
        return None

    latest = max(keystats, key=_period_key)
    ks = {}
    for src, dst in _KS_MAP.items():
        val = _num(latest.get(src))
        if val is not None:
            ks[dst] = val

    result = {
        "ok": True,
        "ks": ks,
        "trend": _build_trend(keystats),
        "period": f"{latest.get('Year')} Q{latest.get('Quarter')}",
        "source": "funda.aurasrp.com.np",
    }
    cache.set(cache_key, result, _CACHE_TTL)
    return result


# Which statements we push into FinancialStatement, and how each maps between the
# funda payload and the local schema: (spec key, statements[] key, local fs_type).
_STMT_MAP = (
    ("KeyStats", "keyStats", "KS"),
    ("BalanceSheet", "balanceSheet", "BS"),
    ("IncomeStatement", "incomeStatement", "IS"),
)


def _fybs_for(fy):
    """The fiscal_year_bs FK id an existing row already uses for ``fy`` (or None)."""
    from core_analysis.models import FinancialStatement as FS

    return (
        FS.objects.filter(fiscal_year_ad=fy)
        .values_list("fiscal_year_bs", flat=True)
        .first()
    )


def _upsert_one_statement(sym, sector, fs_type, columns, entry):
    """Write one statement's latest-quarter line items into FinancialStatement.

    The funda ``spec`` column ``label`` matches the local ``item_name`` exactly and
    both are in sorting order, so every line item is mapped by label (columns and
    template rows consumed in order, which also disambiguates repeated labels like
    "Margin (MRQ) %"). FK-safe: item_id and fiscal_year_bs are reused from real
    rows. Returns ``(written, note)``.

    Every mapped column is written, including the ones the source reports as
    ``null`` (stored as 0). Skipping them used to leave whatever row already sat
    on that period — including rows an earlier, mis-aligned mapping had written —
    so a section's sub-items no longer added up to its total. Writing the full
    column set makes a period's rows a pure function of the current payload.

    Section totals the source omits (e.g. "Operating Expenses", "Income Tax") are
    derived from their sub-items, which the sorting_code carries: "420.1".."420.3"
    are the children of "420". Without that they rendered as 0 above non-zero
    children — the other half of the parts-vs-total mismatch.
    """
    from collections import defaultdict, deque

    from django.utils import timezone

    from core_analysis.models import FinancialStatement as FS

    fy = str(entry.get("Year") or "")
    try:
        quarter = int(entry.get("Quarter") or 0)
    except (TypeError, ValueError):
        quarter = 0

    fybs = _fybs_for(fy)
    if fybs is None:
        return 0, f"{fs_type}: fiscal year {fy} not in FinancialStatement yet"

    # Sector template: one representative row per item_code (item_id / name /
    # sorting / unit are stable across companies and years), grouped by item_name
    # in sorting order so repeated labels are consumed in the spec's column order.
    by_code = {}
    for r in (
        FS.objects.filter(fs_type=fs_type, sector=sector)
        .values("item_code", "item", "item_name", "sorting_code", "unit")
    ):
        by_code.setdefault(r["item_code"], r)
    if not by_code:
        return 0, f"{fs_type}: no {sector} template"

    def _sort_val(r):
        try:
            return float(r["sorting_code"])   # some codes are decimals, e.g. "485.1"
        except (TypeError, ValueError):
            return 0.0

    queues = defaultdict(deque)
    for r in sorted(by_code.values(), key=_sort_val):
        queues[(r["item_name"] or "").strip()].append(r)   # spec labels carry stray spaces

    # Pass 1 — resolve every column to a template row and its reported value.
    picked = []                    # [(row, value_or_None)] in spec order
    for col in columns:
        name, label = col.get("name"), col.get("label")
        if not name or not label:
            continue
        q = queues.get((label or "").strip())
        if not q:
            continue
        row = q.popleft()          # consume in order (handles duplicate labels)
        picked.append((row, _num(entry.get(name))))

    # Pass 2 — fill omitted section totals from their sub-items ("420" <- "420.x").
    child_sums = defaultdict(float)
    have_children = set()
    for row, value in picked:
        parent, _, frac = (row["sorting_code"] or "").partition(".")
        if frac and value is not None:
            child_sums[parent] += value
            have_children.add(parent)
    derived = set()
    for idx, (row, value) in enumerate(picked):
        code = row["sorting_code"] or ""
        if value is None and "." not in code and code in have_children:
            picked[idx] = (row, child_sums[code])
            derived.add(row["item_code"])

    now = timezone.now()
    written = 0
    for row, value in picked:
        FS.objects.update_or_create(
            sector=sector,
            fiscal_year_ad=fy,
            quarter=quarter,
            data_source="PUBLISHED",
            ticker=sym,
            fs_type=fs_type,
            item_code=row["item_code"],
            defaults={
                "item_name": row["item_name"],
                "sorting_code": row["sorting_code"],
                "unit": row["unit"],
                "amount": 0.0 if value is None else value,   # null = not reported
                "fiscal_year_bs": fybs,
                "item": row["item"],
                "remarks": "derived from sub-items" if row["item_code"] in derived else "",
                "created_at": now,
            },
        )
        written += 1
    return written, f"{fs_type} {fy} Q{quarter}: {written}"


def _upsert_financial_statements(sym, sector, statements, spec):
    """Write KeyStats + Balance Sheet + Income Statement (each at its own latest
    reported quarter) into the shared FinancialStatement table, so the fundamentals
    desk, Morningstar and the Workbench all reflect the new period.

    The FK targets (nepali_datetime_fiscalyear / fundamentals_accountdictionary)
    don't exist in this database — the statements table is a mysqldump'd copy, so
    its constraints are vestigial and any insert that validates them fails. The
    original bulk load ran with checks off; we do the same, scoped and restored.
    Returns ``(total_written, note)``.
    """
    from django.db import connection

    total, notes = 0, []
    try:
        with connection.cursor() as cur:
            cur.execute("SET FOREIGN_KEY_CHECKS=0")
        for spec_key, entry_key, fs_type in _STMT_MAP:
            columns = (spec.get(spec_key) or {}).get("columns") or []
            entries = statements.get(entry_key) or []
            if not columns or not entries:
                continue
            latest = max(entries, key=_period_key)
            written, note = _upsert_one_statement(sym, sector, fs_type, columns, latest)
            total += written
            notes.append(note)
    except Exception:  # pragma: no cover - shared table may be read-only
        logger.warning("FS multi-statement upsert failed for %s", sym, exc_info=True)
        return total, "FinancialStatement write partially failed"
    finally:
        try:
            with connection.cursor() as cur:
                cur.execute("SET FOREIGN_KEY_CHECKS=1")
        except Exception:  # pragma: no cover
            pass

    return total, "; ".join(notes) if notes else "no statements to write"


def sector_symbols(sector):
    """Symbols the source covers for one sector (``None`` on fetch failure).

    Drives the Workbench sector-sync loop: the client syncs these one at a time,
    so only symbols funda actually serves are attempted (no 404 noise). Cached —
    sector membership changes rarely.
    """
    sec = (sector or "").strip()
    if not sec:
        return None
    cache_key = f"funda:sector:v1:{sec}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached
    try:
        resp = requests.get(
            f"{_BASE}/api/companies",
            params={"sector": sec},
            headers={"User-Agent": _UA},
            timeout=15,
        )
        resp.raise_for_status()
        symbols = sorted(
            c["symbol"] for c in resp.json().get("results", []) if c.get("symbol")
        )
    except Exception:  # pragma: no cover - external service is best-effort
        logger.warning("funda sector list failed for %s", sec, exc_info=True)
        return None
    cache.set(cache_key, symbols, _CACHE_TTL)
    return symbols


def sync_symbol(symbol):
    """Pull ``symbol``'s latest published report and persist it in our DB.

    Fetches the full financials payload, maps the newest quarter, and upserts one
    ``FundaFundamentalSnapshot`` row (overwriting any prior snapshot for the
    symbol). Returns ``{ok, symbol, period, security_name, sector, synced_at}`` on
    success, or ``{ok: False, error}`` — never raises.
    """
    from core_analysis.models import FundaFundamentalSnapshot

    sym = (symbol or "").strip().upper()
    if not sym:
        return {"ok": False, "error": "Invalid symbol."}

    try:
        data = _fetch(sym)
    except requests.HTTPError as exc:
        status = getattr(exc.response, "status_code", None)
        if status == 404:
            return {"ok": False, "error": f"{sym} is not on the source yet (not covered / no report published)."}
        return {"ok": False, "error": f"Source returned HTTP {status}."}
    except Exception:  # pragma: no cover - external service is best-effort
        logger.warning("funda sync fetch failed for %s", sym, exc_info=True)
        return {"ok": False, "error": "Could not reach the source. Try again."}

    statements = (data or {}).get("statements") or {}
    keystats = statements.get("keyStats") or []
    if not keystats:
        return {"ok": False, "error": f"No published statements found for {sym}."}

    latest = max(keystats, key=_period_key)
    ks = {}
    for src, dst in _KS_MAP.items():
        val = _num(latest.get(src))
        if val is not None:
            ks[dst] = val

    try:
        quarter = int(latest.get("Quarter") or 0)
    except (TypeError, ValueError):
        quarter = 0
    fy = str(latest.get("Year") or "")

    # The source sometimes ships a blank sector (SAPIL, 2026-09) — and the
    # sector is what selects the statement templates, so a blank one silently
    # writes NO statements. Fall back to our own company profile.
    sector = (data.get("sector") or "").strip()
    if not sector:
        from core_analysis.models import CompanyProfile
        sector = (
            CompanyProfile.objects.filter(symbol=sym)
            .values_list("sector_name", flat=True).first() or ""
        )

    snap, _created = FundaFundamentalSnapshot.objects.update_or_create(
        symbol=sym,
        defaults={
            "security_name": (data.get("security_name") or "")[:255],
            "sector": sector[:100],
            "period": f"{fy} Q{quarter}",
            "fiscal_year_ad": fy[:10],
            "quarter": quarter,
            "ks": ks,
            "trend": _build_trend(keystats),
            "raw": statements,
            "source": "funda.aurasrp.com.np",
        },
    )
    # Push KeyStats + Balance Sheet + Income Statement into the shared
    # FinancialStatement table so the fundamentals desk / Morningstar / Workbench
    # reflect the new quarter too.
    fs_written, fs_note = _upsert_financial_statements(
        sym, snap.sector, statements, (data or {}).get("spec") or {}
    )
    if snap.fs_written != fs_written:
        snap.fs_written = fs_written
        snap.save(update_fields=["fs_written"])

    # Refresh the read cache so the page reflects the sync immediately.
    cache.delete(f"funda:ks:v1:{sym}")
    return {
        "ok": True,
        "symbol": sym,
        "period": snap.period,
        "security_name": snap.security_name,
        "sector": snap.sector,
        "synced_at": snap.synced_at.isoformat(),
        "fs_written": fs_written,
        "fs_note": fs_note,
    }


def stored_keystats(symbol):
    """Read the persisted snapshot for ``symbol`` (``None`` if never synced)."""
    from core_analysis.models import FundaFundamentalSnapshot

    sym = (symbol or "").strip().upper()
    if not sym:
        return None
    snap = FundaFundamentalSnapshot.objects.filter(symbol=sym).first()
    if not snap:
        return None
    return {
        "ok": True,
        "ks": snap.ks or {},
        "trend": snap.trend or [],
        "period": snap.period,
        "source": snap.source,
        "synced_at": snap.synced_at.isoformat(),
        "stored": True,
    }
