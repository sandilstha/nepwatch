"""
margin.py — margin-eligibility lookups for the search UI and fundamentals panel.

The eligible list is small and changes rarely, so the whole eligible-symbol map
is cached in one blob (busted on admin save/delete, the two admin bulk actions,
and the ``load_margin_eligible`` command). Callers get O(1) dict lookups with no
per-search DB hit.
"""
from __future__ import annotations

from django.core.cache import cache

from core_analysis.models import MarginEligibleCompany

CACHE_KEY = "margin_eligible_map:v1"
CACHE_TTL = 600  # 10 minutes; explicit busting keeps it fresh sooner


def eligible_margin_map() -> dict:
    """{SYMBOL: {eligible, rate, category, sector, name}} for eligible companies."""
    cached = cache.get(CACHE_KEY)
    if cached is not None:
        return cached
    out = {}
    rows = MarginEligibleCompany.objects.filter(is_eligible=True).values(
        "symbol", "margin_rate", "risk_category", "sector", "company_name"
    )
    for r in rows:
        sym = (r["symbol"] or "").strip().upper()
        if not sym:
            continue
        out[sym] = {
            "eligible": True,
            "rate": float(r["margin_rate"]) if r["margin_rate"] is not None else None,
            "category": r["risk_category"] or "",
            "sector": r["sector"] or "",
            "name": r["company_name"] or "",
        }
    cache.set(CACHE_KEY, out, timeout=CACHE_TTL)
    return out


def margin_status(symbol) -> dict:
    """Margin status for one symbol. Always returns a dict with an ``eligible``
    key so callers can render unconditionally."""
    if not symbol:
        return {"eligible": False}
    info = eligible_margin_map().get(str(symbol).strip().upper())
    return dict(info) if info else {"eligible": False}
