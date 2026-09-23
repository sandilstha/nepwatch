"""
analytics_views.py — self-hosted visit-analytics dashboard (/stats/).

Rolls up the ``PageVisit`` rows written by ``VisitTrackingMiddleware`` into the
"how many times was the site visited / reached / opened" view the user asked for.
Everything is computed here in Python/ORM and the template renders with inline
CSS + CSS bar charts (no CDN, no JS libraries), so it works on a fully offline /
air-gapped LAN. Staff-only — it exposes visitor IPs.
"""
from datetime import timedelta

from django.contrib.admin.views.decorators import staff_member_required
from django.db.models import Count
from django.db.models.functions import TruncDate
from django.shortcuts import render
from django.utils import timezone


def _classify_agent(ua):
    """Coarse browser/OS bucket from a User-Agent string (best-effort)."""
    ua = (ua or "").lower()
    if not ua:
        return "Unknown"
    if any(b in ua for b in ("bot", "spider", "crawl", "slurp", "curl", "wget", "python")):
        return "Bot / script"
    if "edg" in ua:
        return "Edge"
    if "opr" in ua or "opera" in ua:
        return "Opera"
    if "firefox" in ua:
        return "Firefox"
    if "chrome" in ua or "crios" in ua:
        return "Chrome"
    if "safari" in ua:
        return "Safari"
    return "Other"


def _device(ua):
    ua = (ua or "").lower()
    if any(m in ua for m in ("mobile", "android", "iphone", "ipad", "ipod")):
        return "Mobile / tablet"
    return "Desktop"


@staff_member_required
def site_stats_view(request):
    from core_analysis.models import PageVisit

    now = timezone.now()
    today = timezone.localdate()
    since_7 = now - timedelta(days=7)
    since_30 = now - timedelta(days=30)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    qs = PageVisit.objects.all()

    totals = {
        "all_time": qs.count(),
        "unique_ips": qs.values("ip_address").distinct().count(),
        "unique_sessions": qs.exclude(session_key="").values("session_key").distinct().count(),
        "today": qs.filter(created_at__gte=day_start).count(),
        "last_7": qs.filter(created_at__gte=since_7).count(),
        "last_30": qs.filter(created_at__gte=since_30).count(),
        "logged_in": qs.filter(user__isnull=False).count(),
    }

    # Per-day visits for the last 30 days, gap-filled so the chart axis is
    # continuous even on days with no traffic.
    per_day_raw = {
        row["day"]: row["n"]
        for row in qs.filter(created_at__gte=since_30)
        .annotate(day=TruncDate("created_at"))
        .values("day")
        .annotate(n=Count("id"))
    }
    days = [today - timedelta(days=i) for i in range(29, -1, -1)]
    day_counts = [per_day_raw.get(d, 0) for d in days]
    day_max = max(day_counts) if day_counts else 0
    daily = [
        {
            "date": d.isoformat(),
            "label": d.strftime("%d %b"),
            "count": c,
            "pct": round(100.0 * c / day_max, 1) if day_max else 0.0,
        }
        for d, c in zip(days, day_counts)
    ]

    # Top pages (last 30 days).
    top_pages = list(
        qs.filter(created_at__gte=since_30)
        .values("path")
        .annotate(n=Count("id"))
        .order_by("-n")[:15]
    )
    tp_max = top_pages[0]["n"] if top_pages else 0
    for p in top_pages:
        p["pct"] = round(100.0 * p["n"] / tp_max, 1) if tp_max else 0.0

    # Top visitors by IP (last 30 days).
    top_ips = list(
        qs.filter(created_at__gte=since_30)
        .values("ip_address")
        .annotate(n=Count("id"))
        .order_by("-n")[:10]
    )

    # Browser + device split (last 30 days) — bucketed in Python from the UA.
    browsers, devices = {}, {}
    for ua in qs.filter(created_at__gte=since_30).values_list("user_agent", flat=True):
        browsers[_classify_agent(ua)] = browsers.get(_classify_agent(ua), 0) + 1
        devices[_device(ua)] = devices.get(_device(ua), 0) + 1
    browser_rows = sorted(
        ({"name": k, "n": v} for k, v in browsers.items()), key=lambda r: r["n"], reverse=True
    )
    device_rows = sorted(
        ({"name": k, "n": v} for k, v in devices.items()), key=lambda r: r["n"], reverse=True
    )

    recent = list(qs.select_related("user")[:25])

    context = {
        "totals": totals,
        "daily": daily,
        "top_pages": top_pages,
        "top_ips": top_ips,
        "browser_rows": browser_rows,
        "device_rows": device_rows,
        "recent": recent,
        "generated_at": now,
    }
    return render(request, "core_analysis/site_stats.html", context)
