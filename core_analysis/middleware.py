"""
middleware.py — site-wide Google Analytics (GA4) tag injection.

Every page in this project renders its own ``<head>`` (base.html, market_insights,
portfolio, floorsheet, login, …), so instead of pasting the tracking snippet into
each template — and silently missing a page whenever a new one is added — this
middleware injects the GA4 ``gtag.js`` snippet into every HTML response from one
place.

Controlled entirely by ``settings.GOOGLE_ANALYTICS_ID`` (env ``GOOGLE_ANALYTICS_ID``,
a GA4 Measurement ID like ``G-XXXXXXXXXX``). When it is blank the middleware is a
pure no-op, so nothing is sent to Google until you opt in by setting the ID —
handy for keeping local/dev traffic out of your analytics.
"""
from django.conf import settings

# GA4 loader + config. Doubled braces survive str.format(); {id} is the only field.
_GA_SNIPPET = (
    '<script async src="https://www.googletagmanager.com/gtag/js?id={id}"></script>'
    "<script>"
    "window.dataLayer = window.dataLayer || [];"
    "function gtag(){{dataLayer.push(arguments);}}"
    "gtag('js', new Date());"
    "gtag('config', '{id}');"
    "</script>"
)


class GoogleAnalyticsMiddleware:
    """Insert the GA4 tag just before ``</head>`` on full HTML responses."""

    def __init__(self, get_response):
        self.get_response = get_response
        self.ga_id = (getattr(settings, "GOOGLE_ANALYTICS_ID", "") or "").strip()
        # Nothing to do without an ID — tell Django to drop this middleware
        # from the chain entirely so it costs zero per request.
        if not self.ga_id:
            from django.core.exceptions import MiddlewareNotUsed

            raise MiddlewareNotUsed()
        self._snippet = _GA_SNIPPET.format(id=self.ga_id).encode("utf-8")

    def __call__(self, request):
        response = self.get_response(request)

        # Don't track the Django admin (staff back-office, not site visitors).
        if request.path.startswith("/admin/"):
            return response
        # Only rewrite complete HTML documents: skip JSON API responses, static
        # files, redirects, and streaming responses (which have no .content).
        if getattr(response, "streaming", False):
            return response
        ctype = response.headers.get("Content-Type", "")
        if "text/html" not in ctype:
            return response
        body = getattr(response, "content", b"")
        marker = b"</head>"
        if marker not in body:
            return response

        response.content = body.replace(marker, self._snippet + marker, 1)
        response["Content-Length"] = str(len(response.content))
        return response


# ── Self-hosted visit tracking (offline / air-gapped alternative to GA) ────────
# URL prefixes that are never counted as "page visits": static assets, the admin
# back-office, and every JSON/AJAX endpoint (the Insights & Floorsheet dashboards
# poll these on a timer, which would otherwise flood the visits table).
_SKIP_PREFIXES = (
    "/static/", "/media/", "/admin/", "/favicon",
    "/insights/api", "/insights/subindices", "/insights/udf",
    "/portfolio/api", "/floorsheet/api",
    "/workbench/calc", "/workbench/ai-analysis",
    "/dashboard/", "/fundamentals/api", "/fundamentals/matrix",
    "/fundamentals/model", "/chart/indicator",
)


def _client_ip(request):
    """Best-effort client IP: honour X-Forwarded-For (first hop) behind a proxy,
    else fall back to REMOTE_ADDR (direct LAN connections)."""
    xff = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR") or None


class VisitTrackingMiddleware:
    """Record one ``PageVisit`` row per real HTML page load.

    Writes only GET / text-html / HTTP-200 navigations, skipping static files,
    admin, JSON APIs and AJAX polls (see ``_SKIP_PREFIXES``). Any failure is
    swallowed — tracking must never break a page. Disable with
    ``VISIT_TRACKING_ENABLED = False`` in settings.
    """

    def __init__(self, get_response):
        self.get_response = get_response
        if not getattr(settings, "VISIT_TRACKING_ENABLED", True):
            from django.core.exceptions import MiddlewareNotUsed

            raise MiddlewareNotUsed()

    def __call__(self, request):
        response = self.get_response(request)
        try:
            if self._should_track(request, response):
                self._record(request, response)
        except Exception:  # never let analytics break the site
            import logging

            logging.getLogger(__name__).exception("visit tracking failed")
        return response

    @staticmethod
    def _should_track(request, response):
        if request.method != "GET" or response.status_code != 200:
            return False
        # AJAX / fetch requests set this header — they're not page navigations.
        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return False
        if getattr(response, "streaming", False):
            return False
        if "text/html" not in response.headers.get("Content-Type", ""):
            return False
        path = request.path
        return not any(path.startswith(p) for p in _SKIP_PREFIXES)

    @staticmethod
    def _record(request, response):
        from core_analysis.models import PageVisit

        user = getattr(request, "user", None)
        PageVisit.objects.create(
            path=request.path[:300],
            method=request.method,
            status_code=response.status_code,
            ip_address=_client_ip(request),
            session_key=(getattr(request.session, "session_key", "") or "")[:40],
            user=user if (user is not None and user.is_authenticated) else None,
            user_agent=request.META.get("HTTP_USER_AGENT", "")[:400],
            referer=request.META.get("HTTP_REFERER", "")[:400],
        )
