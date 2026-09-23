"""Template context shared by every page.

The primary nav is included from many different views; anything it needs has to
be available globally rather than added to each view by hand — otherwise the
menu silently renders empty on whichever page was missed.
"""

from core_analysis.services.nepse_reports import REPORT_ORDER, REPORTS


def global_assets(request):
    """Cache-bust token for the stylesheets base.html loads on EVERY page.

    base.html links dashboard.css and workbench-layout.css with
    ``?v={{ dashboard_asset_version|default:1 }}``, but only the workbench view
    ever supplied that variable — so every other page shipped ``?v=1`` and
    browsers held a permanently stale copy. Any edit to those two files was
    invisible outside the workbench until a manual cache clear. Supplying it
    globally makes the fallback unreachable.
    """
    from django.conf import settings as _s

    from core_analysis.views import _dashboard_asset_version

    return {
        "dashboard_asset_version": _dashboard_asset_version(),
        # Every "Charts" / "Full chart" link reads this, so repointing the
        # charting terminal is a one-line settings change, not a template hunt.
        "external_chart_url": getattr(_s, "EXTERNAL_CHART_URL", ""),
    }


def nepse_data_menu(request):
    """The NEPSE Data dropdown items, in the exchange's own menu order.

    Built from the registry constants only — no queries — so it costs nothing to
    include on every render.
    """
    return {
        "nepse_data_menu": [
            (slug, REPORTS[slug]["title"]) for slug in REPORT_ORDER if slug in REPORTS
        ]
    }
