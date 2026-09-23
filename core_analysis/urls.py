from django.contrib.auth import views as auth_views
from django.urls import path
from .portfolio_views import (
    approval_pending_view,
    register_view,
    portfolio_view,
    portfolio_sop_view,
    portfolio_data_api,
    portfolio_manage,
    portfolio_approvals_api,
    portfolio_approval_action,
    portfolio_import,
    portfolio_wacc_import,
    portfolio_ledger_import,
    portfolio_clear,
)
from .views import (
    crud_dashboard_view,
    dashboard_tab_calc,
    gemini_sr_analysis,
    crud_operations_handler,
    life_indicator_save, life_indicator_delete,
    crud_delete_handler,
    trigger_daily_api_sync_view,
    trigger_floorsheet_sync_view,
    trigger_proposed_dividend_sync_view,
    trigger_mutual_fund_nav_sync_view,
    symbol_autocomplete_view,
    trigger_sync_and_calculate,
    stage_sop_view,
    seasonal_sop_view,
    market_cycle_sop_view,
)
from .insights_views import (
    market_insights_view,
    market_insights_api,
    subindex_comparison_api,
    sector_turnover_api,
)
from .broker_views import (
    floorsheet_view,
    floorsheet_sop_view,
    broker_meta_api,
    broker_favorites_api,
    broker_persistence_api,
    broker_signals_api,
    stock_wise_api,
    net_holding_api,
    depth_dates_api, depth_overview_api, depth_frames_api, market_depth_sop_view,
    broker_concentration_api,
    hotstocks_api,
    broker_flow_radar_api,
    broker_flow_map_api,
    accumulation_api,
    accumulation_ask_api,
    accumulation_sop_view,
)
from .udf_views import (
    udf_config,
    udf_time,
    udf_symbols,
    udf_search,
    udf_history,
)
from .canslim_views import (
    canslim_view,
    canslim_scan_api,
    canslim_stock_api,
    canslim_sop_view,
)
from .analytics_views import site_stats_view
from .nepse_data_views import (
    nepse_data_view,
    nepse_data_api,
    nepse_data_symbols_api,
)
from .fundamental_views import (
    fundamental_analysis_view,
    industry_matrix_api,
    industry_periods_api,
    fundamental_sop_view,
    fundamental_data_api,
    fundamental_matrix_api,
    fundamental_model_api,
    morningstar_scan_api,
    morningstar_sop_view,
)
from .mutual_fund_views import (
    mf_home, mf_list_page, mf_assets_page, mf_sector_page, mf_holdings_page,
    mf_financials_page, mf_fund_list_api, mf_assets_api, mf_sector_api,
    mf_company_holdings_api, mf_financials_api, mf_sync_api,
    mf_dashboard, mf_nav_api, mf_import_api,
)
from .stock360_views import (
    stock360_view, stock360_ai_api, stock360_funda_api, stock360_funda_sync,
    stock360_funda_recent, stock360_funda_sector, stock360_keyfin_api,
    stock360_valuation_api, stock360_dividends_api, stock360_flow_series_api,
    stock360_sop_api, stock360_sop_view, stock_valuation_view, bond_desk_view,
)

urlpatterns = [

    # Market Insights is the landing page (served at root); /insights/ kept as an alias.
    path('', market_insights_view, name='market_insights'),
    path('insights/', market_insights_view),
    path('insights/api/', market_insights_api, name='market_insights_api'),
    path('insights/subindices/', subindex_comparison_api, name='subindex_comparison_api'),
    path('insights/sector-turnover/', sector_turnover_api, name='sector_turnover_api'),

    # Stock 360 — all-in-one single-stock dashboard (aggregates every desk).
    path('stock/', stock360_view, name='stock360'),
    path('stock/api/ai/', stock360_ai_api, name='stock360_ai_api'),
    path('stock/api/funda/', stock360_funda_api, name='stock360_funda_api'),
    path('stock/api/funda/sync/', stock360_funda_sync, name='stock360_funda_sync'),
    path('stock/api/funda/recent/', stock360_funda_recent, name='stock360_funda_recent'),
    path('stock/api/funda/sector/', stock360_funda_sector, name='stock360_funda_sector'),
    path('stock/api/keyfin/', stock360_keyfin_api, name='stock360_keyfin_api'),
    path('stock/api/valuation/', stock360_valuation_api, name='stock360_valuation_api'),
    path('stock/api/sop/', stock360_sop_api, name='stock360_sop_api'),
    path('stock/api/dividends/', stock360_dividends_api, name='stock360_dividends_api'),
    path('stock/api/flow-series/', stock360_flow_series_api, name='stock360_flow_series_api'),
    # More specific than the catch-all below, so both must come first.
    path('stock/sop/', stock360_sop_view, name='stock360_sop'),
    path('bonds/', bond_desk_view, name='bond_desk'),
    path('stock/<str:symbol>/valuation/', stock_valuation_view, name='stock_valuation'),
    path('stock/<str:symbol>/', stock360_view, name='stock360_symbol'),

    # NOTE: the built-in charting TERMINAL (/chart/ + its pandas_ta indicator
    # endpoints) was removed; charting moved to settings.EXTERNAL_CHART_URL.
    #
    # The UDF datafeed below STAYS. It is not part of that terminal — the
    # Market Insights landing page's "Index Price & Volume" card (ohlc-chart.js,
    # tv-chart.js) reads /insights/udf/history for its candles. Removing it
    # blanked that chart, which is how this comment came to exist.

    # Fundamental Analysis Desk — company financial statements + ratios.
    path('fundamentals/', fundamental_analysis_view, name='fundamental_analysis'),
    path('fundamentals/api/', fundamental_data_api, name='fundamental_data_api'),
    path('fundamentals/matrix/', fundamental_matrix_api, name='fundamental_matrix_api'),
    path('fundamentals/model/', fundamental_model_api, name='fundamental_model_api'),
    path('fundamentals/morningstar/', morningstar_scan_api, name='morningstar_scan_api'),
    path('fundamentals/morningstar/sop/', morningstar_sop_view, name='morningstar_sop'),
    path('fundamentals/sop/', fundamental_sop_view, name='fundamental_sop'),
    # Industry Analysis — sector-wide statement comparison (cross-sectional,
    # which is why it lives here and not on the per-symbol Stock 360 page).
    path('fundamentals/industry/', industry_matrix_api, name='industry_matrix_api'),
    path('fundamentals/industry/periods/', industry_periods_api,
         name='industry_periods_api'),
    path('fundamentals/<str:symbol>/', fundamental_analysis_view, name='fundamental_analysis_symbol'),

    # Auth (user-facing; the workbench keeps its separate admin/staff login).
    path('accounts/login/', auth_views.LoginView.as_view(next_page='portfolio'), name='login'),
    path('accounts/logout/', auth_views.LogoutView.as_view(next_page='login'), name='logout'),
    path('accounts/register/', register_view, name='register'),
    path('accounts/approval-pending/', approval_pending_view, name='approval_pending'),

    # Risk & Portfolio Desk — private, per-user holdings + risk analytics.
    path('portfolio/', portfolio_view, name='portfolio'),
    path('portfolio/sop/', portfolio_sop_view, name='portfolio_sop'),
    path('portfolio/api/data/', portfolio_data_api, name='portfolio_data_api'),
    path('portfolio/manage/', portfolio_manage, name='portfolio_manage'),
    path('portfolio/api/approvals/', portfolio_approvals_api, name='portfolio_approvals_api'),
    path('portfolio/api/approvals/action/', portfolio_approval_action, name='portfolio_approval_action'),
    path('portfolio/import/', portfolio_import, name='portfolio_import'),
    path('portfolio/wacc/', portfolio_wacc_import, name='portfolio_wacc_import'),
    path('portfolio/ledger/', portfolio_ledger_import, name='portfolio_ledger_import'),
    path('portfolio/clear/', portfolio_clear, name='portfolio_clear'),

    # Floor sheet — Dalal Street X broker analytics (built on the floorsheet feed).
    path('floorsheet/', floorsheet_view, name='floorsheet'),
    path('floorsheet/sop/', floorsheet_sop_view, name='floorsheet_sop'),
    path('floorsheet/api/meta/', broker_meta_api, name='broker_meta_api'),
    path('floorsheet/api/favorites/', broker_favorites_api, name='broker_favorites_api'),
    path('floorsheet/api/persistence/', broker_persistence_api, name='broker_persistence_api'),
    path('floorsheet/api/signals/', broker_signals_api, name='broker_signals_api'),
    path('floorsheet/api/stockwise/', stock_wise_api, name='stock_wise_api'),
    path('floorsheet/api/netholding/', net_holding_api, name='net_holding_api'),
    path('floorsheet/api/concentration/', broker_concentration_api, name='broker_concentration_api'),
    path('floorsheet/api/hotstocks/', hotstocks_api, name='hotstocks_api'),
    # Market Depth tab — TMS top-5 replay (market_depth_snapshots).
    path('floorsheet/api/depth/dates/', depth_dates_api, name='depth_dates_api'),
    path('floorsheet/api/depth/overview/', depth_overview_api, name='depth_overview_api'),
    path('floorsheet/api/depth/frames/', depth_frames_api, name='depth_frames_api'),
    path('floorsheet/depth/sop/', market_depth_sop_view, name='market_depth_sop'),
    path('floorsheet/api/flow-radar/', broker_flow_radar_api, name='broker_flow_radar_api'),
    path('floorsheet/api/flow-map/', broker_flow_map_api, name='broker_flow_map_api'),
    path('floorsheet/api/accumulation/', accumulation_api, name='accumulation_api'),
    # Grounded natural-language Q&A over the current A/D scan. POST + login:
    # every call spends API credit, so it must not be prefetchable or anonymous.
    path('floorsheet/api/accumulation/ask/', accumulation_ask_api,
         name='accumulation_ask_api'),
    # Dedicated SOP for the A/D Radar model (backtest, rejected features, limits).
    path('floorsheet/accumulation/sop/', accumulation_sop_view, name='accumulation_sop'),

    # CAN SLIM screen — ranked cross-section of every ordinary equity.
    # Public: derived entirely from public filings and public price data.
    path('canslim/', canslim_view, name='canslim'),
    path('canslim/api/scan/', canslim_scan_api, name='canslim_scan_api'),
    path('canslim/api/stock/', canslim_stock_api, name='canslim_stock_api'),
    # Plain-language methodology — every threshold and every honesty rule.
    path('canslim/sop/', canslim_sop_view, name='canslim_sop'),

    # Analytics workbench (moved off root to /workbench/)
    path('workbench/', crud_dashboard_view, name='crud_dashboard'),
    # Methodology SOP for the Stage Analysis desk (Technical Analysis tab).
    path('stage/sop/', stage_sop_view, name='stage_sop'),
    # Methodology SOP for the Seasonal Return desk's Accumulation / Distribution strategy.
    path('seasonal/sop/', seasonal_sop_view, name='seasonal_sop'),
    # SOP for NEPSE macro market-cycle stage classification (Weinstein 4-stage, whole index).
    path('market-cycle/sop/', market_cycle_sop_view, name='market_cycle_sop'),
    # AJAX: run one tab's calculation and return only its results partial.
    path('workbench/calc/', dashboard_tab_calc, name='dashboard_tab_calc'),
    # AJAX: Gemini narrative for the Support & Resistance tab (JSON).
    path('workbench/ai-analysis/', gemini_sr_analysis, name='gemini_sr_analysis'),
    path('dashboard/process/', crud_operations_handler, name='crud_operations'),
    path('dashboard/delete/<int:pk>/', crud_delete_handler, name='crud_delete'),
    path('dashboard/life-indicators/', life_indicator_save, name='life_indicator_save'),
    path('dashboard/life-indicators/delete/<int:pk>/', life_indicator_delete, name='life_indicator_delete'),
    path('dashboard/sync/', trigger_daily_api_sync_view, name='trigger_daily_sync'),
    path('dashboard/sync-floorsheet/', trigger_floorsheet_sync_view, name='trigger_floorsheet_sync'),
    path('dashboard/sync-proposed-dividend/', trigger_proposed_dividend_sync_view, name='trigger_proposed_dividend_sync'),
    path('dashboard/sync-mutual-fund-nav/', trigger_mutual_fund_nav_sync_view, name='trigger_mutual_fund_nav_sync'),

    # Mutual fund desk — allocation views computed from imported monthly
    # portfolios. Read endpoints take ?month= in any Nepali spelling and default
    # to the newest imported month.
    path('mutual-fund/', mf_dashboard, name='mutual_fund'),
    path('mutual-fund/api/nav/', mf_nav_api, name='mf_nav_api'),
    path('mutual-fund/api/import/', mf_import_api, name='mf_import_api'),

    # The six desk screens. Page routes first, then their JSON. `mutual-fund/`
    # above stays the NAV & discount dashboard; `mutual-fund/desk/` is the
    # report suite that mirrors the published site.
    path('mutual-fund/desk/', mf_home, name='mf_home'),
    path('mutual-fund/desk/list/', mf_list_page, name='mf_list_page'),
    path('mutual-fund/desk/assets/', mf_assets_page, name='mf_assets_page'),
    path('mutual-fund/desk/sector/', mf_sector_page, name='mf_sector_page'),
    path('mutual-fund/desk/holdings/', mf_holdings_page, name='mf_holdings_page'),
    path('mutual-fund/desk/financials/', mf_financials_page, name='mf_financials_page'),
    path('mutual-fund/api/fund-list/', mf_fund_list_api, name='mf_fund_list_api'),
    path('mutual-fund/api/assets/', mf_assets_api, name='mf_assets_api'),
    path('mutual-fund/api/sector/', mf_sector_api, name='mf_sector_api'),
    path('mutual-fund/api/company-holdings/', mf_company_holdings_api, name='mf_company_holdings_api'),
    path('mutual-fund/api/financials/', mf_financials_api, name='mf_financials_api'),
    path('mutual-fund/api/sync/', mf_sync_api, name='mf_sync_api'),
    path('dashboard/sync-calculate/', trigger_sync_and_calculate, name='trigger_sync_and_calculate'),
    # NEW — lightweight autocomplete endpoint for symbol search boxes
    path('dashboard/symbols/', symbol_autocomplete_view, name='symbol_autocomplete'),

    # Self-hosted visit analytics — "how many times the site was opened" (staff only).
    path('stats/', site_stats_view, name='site_stats'),

    # ── NEPSE Data — raw exchange reports (one page per report) ──────────
    path('nepse-data/', nepse_data_view, name='nepse_data'),
    path('nepse-data/symbols/', nepse_data_symbols_api, name='nepse_data_symbols'),
    path('nepse-data/api/<slug:slug>/', nepse_data_api, name='nepse_data_api'),
    # Keep last: a bare <slug> would otherwise swallow the two routes above.
    path('nepse-data/<slug:slug>/', nepse_data_view, name='nepse_data_report'),

    # TradingView UDF datafeed (no trailing slashes — UDF spec). Feeds the
    # Market Insights index chart; NOT tied to the removed /chart/ terminal.
    path('insights/udf/config', udf_config, name='udf_config'),
    path('insights/udf/time', udf_time, name='udf_time'),
    path('insights/udf/symbols', udf_symbols, name='udf_symbols'),
    path('insights/udf/search', udf_search, name='udf_search'),
    path('insights/udf/history', udf_history, name='udf_history'),
]
