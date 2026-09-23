import json
import re
import unittest
from contextlib import ExitStack
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import Mock, patch

import numpy as np
import pandas as pd
from django.contrib.auth import get_user_model
from django.core.exceptions import ImproperlyConfigured
from django.core.files.uploadedfile import SimpleUploadedFile
from django.template.loader import render_to_string
from django.test import Client, RequestFactory, SimpleTestCase, TestCase
from django.urls import reverse

from core_analysis.models import (
    AccountApproval,
    BrokerLedgerImport,
    BrokerLedgerTransaction,
    BrokerTrade,
    Holding,
    HoldingCost,
    Portfolio,
)

from core_analysis.services import IMM, msv_strategy, portfolio_analytics
from core_analysis.services.advanced_market_structure import (
    generate_dummy_ohlcv,
    run_advanced_market_structure_analysis,
)
from core_analysis.services.support_resistance import (
    build_institutional_analysis_rows,
    run_support_resistance_analysis,
)

try:
    from core_analysis.services import broker_analytics, market_insights, nepse_contributors
except ImproperlyConfigured:  # Allows `python core_analysis/tests.py` outside Django.
    broker_analytics = None
    market_insights = None
    nepse_contributors = None

try:
    from core_analysis import udf_views
except ImproperlyConfigured:  # Allows `python core_analysis/tests.py` outside Django.
    udf_views = None

try:
    from core_analysis import fundamental_views
except ImproperlyConfigured:  # Allows `python core_analysis/tests.py` outside Django.
    fundamental_views = None


@unittest.skipIf(fundamental_views is None, "Django settings unavailable")
class FundamentalGrowthValueModelTests(unittest.TestCase):
    @patch("core_analysis.fundamental_views.CompanyProfile.objects")
    @patch("core_analysis.fundamental_views.FinancialStatement.objects")
    def test_fundamental_tickers_are_grouped_by_sector_with_unclassified_last(
        self, statements, profiles
    ):
        statements.order_by.return_value.values_list.return_value.distinct.return_value = {
            "AAA", "BBB", "CCC"
        }
        profiles.filter.return_value.values_list.return_value = [
            ("CCC", "Company C", None),
            ("BBB", "Company B", "Hydropower"),
            ("AAA", "Company A", "Commercial Banks"),
        ]

        companies = fundamental_views._fundamental_tickers()

        self.assertEqual(
            [(company["symbol"], company["sector"]) for company in companies],
            [
                ("AAA", "Commercial Banks"),
                ("BBB", "Hydropower"),
                ("CCC", "Unclassified"),
            ],
        )

    def test_industry_sector_picker_renders_sectors(self):
        # The Fundamentals page is now the Industry Analysis / Smart Stock Score
        # desk: one sector <select> (banks pre-selected) instead of a per-company list.
        html = render_to_string(
            "core_analysis/fundamental_analysis.html",
            {
                "sectors": [
                    {"sector": "Commercial Banks", "companies": 20},
                    {"sector": "Hydropower", "companies": 90},
                ],
                "asset_version": "test",
            },
        )

        self.assertIn('id="ia-sector"', html)
        self.assertIn('<option value="Commercial Banks" selected>', html)
        self.assertIn('<option value="Hydropower">', html)
        self.assertNotIn('id="fa-symlist"', html)

    def test_cap_segments_use_cumulative_market_cap_share(self):
        segments = fundamental_views._gv_cap_segments(
            {"AAA": 70.0, "BBB": 20.0, "CCC": 10.0, "DDD": None}
        )

        self.assertEqual(segments["AAA"], "Large")
        self.assertEqual(segments["BBB"], "Mid")
        self.assertEqual(segments["CCC"], "Small")
        self.assertEqual(segments["DDD"], "Unclassified")

    def test_cap_segments_handle_dominant_large_cap(self):
        segments = fundamental_views._gv_cap_segments({"AAA": 95.0, "BBB": 3.0, "CCC": 2.0})

        self.assertEqual(segments["AAA"], "Large")
        self.assertEqual(segments["BBB"], "Small")
        self.assertEqual(segments["CCC"], "Small")


class AdminApprovalTests(TestCase):
    def _register(self, username, email):
        return self.client.post(
            reverse("register"),
            {
                "username": username,
                "email": email,
                "password1": "StrongPass123!",
                "password2": "StrongPass123!",
            },
        )

    def test_registration_creates_inactive_user_and_pending_admin_request(self):
        response = self._register("pendinguser", "pendinguser@example.com")
        user = get_user_model().objects.get(username="pendinguser")
        approval = user.account_approval

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], reverse("approval_pending"))
        self.assertFalse(user.is_active)
        self.assertEqual(user.email, "pendinguser@example.com")
        self.assertEqual(approval.contact_email, "pendinguser@example.com")
        self.assertEqual(approval.status, AccountApproval.PENDING)
        self.assertIsNone(approval.reviewed_at)

    def test_admin_approval_activates_user(self):
        self._register("approveuser", "approveuser@example.com")
        UserModel = get_user_model()
        reviewer = UserModel.objects.create_user(
            username="reviewer", email="reviewer@example.com", password="StrongPass123!"
        )
        user = UserModel.objects.get(username="approveuser")
        approval = user.account_approval

        approval.approve(reviewer=reviewer, note="Approved for access")

        user.refresh_from_db()
        approval.refresh_from_db()
        self.assertTrue(user.is_active)
        self.assertEqual(approval.status, AccountApproval.APPROVED)
        self.assertEqual(approval.reviewed_by, reviewer)
        self.assertEqual(approval.review_note, "Approved for access")
        self.assertIsNotNone(approval.reviewed_at)

    def test_admin_rejection_keeps_user_inactive(self):
        self._register("rejectuser", "rejectuser@example.com")
        UserModel = get_user_model()
        reviewer = UserModel.objects.create_user(
            username="reviewer", email="reviewer@example.com", password="StrongPass123!"
        )
        user = UserModel.objects.get(username="rejectuser")
        approval = user.account_approval

        approval.reject(reviewer=reviewer, note="Rejected")

        user.refresh_from_db()
        approval.refresh_from_db()
        self.assertFalse(user.is_active)
        self.assertEqual(approval.status, AccountApproval.REJECTED)
        self.assertEqual(approval.reviewed_by, reviewer)
        self.assertEqual(approval.review_note, "Rejected")
        self.assertIsNotNone(approval.reviewed_at)


class MultiPortfolioManagementTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="investor", email="investor@example.com", password="StrongPass123!"
        )
        self.client.force_login(self.user)

    @staticmethod
    def _holdings_upload(owner="SHARADA DEVI SHRESTHA"):
        content = (
            f"Holder Name :,{owner}\n"
            "Scrip,Current Balance,Last Closing Price,Last Transaction Price (LTP)\n"
            "NABIL,10,500,502\n"
        )
        return SimpleUploadedFile(
            "my-shares.csv", content.encode("utf-8"), content_type="text/csv"
        )

    @staticmethod
    def _wacc_upload():
        content = (
            "Scrip Name,WACC Calculated Quantity,WACC Rate,"
            "Total Cost of Capital,Last Modification Date\n"
            "NABIL,10,400,4000,2026-07-09\n"
        )
        return SimpleUploadedFile(
            "my-wacc.csv", content.encode("utf-8"), content_type="text/csv"
        )

    def test_holdings_report_extracts_owner_name(self):
        from core_analysis.portfolio_views import parse_holdings_report_details

        rows, skipped, owner = parse_holdings_report_details(self._holdings_upload())

        self.assertEqual(owner, "Sharada Devi Shrestha")
        self.assertEqual([row["symbol"] for row in rows], ["NABIL"])
        self.assertEqual(skipped, 1)

    def test_first_import_automatically_names_default_portfolio(self):
        self.client.get(reverse("portfolio"))
        portfolio = Portfolio.objects.get(user=self.user)

        response = self.client.post(
            reverse("portfolio_import"),
            {"portfolio_id": portfolio.id, "file": self._holdings_upload()},
        )

        portfolio.refresh_from_db()
        self.assertRedirects(
            response,
            f"{reverse('portfolio')}?portfolio={portfolio.id}",
            fetch_redirect_response=False,
        )
        self.assertEqual(portfolio.name, "Sharada Devi Shrestha")
        self.assertEqual(portfolio.holdings.count(), 1)

    def test_create_with_file_reuses_empty_default_and_imports_in_one_request(self):
        response = self.client.post(
            reverse("portfolio_manage"),
            {
                "action": "create",
                "portfolio_type": Portfolio.PERSONAL,
                "file": self._holdings_upload(),
            },
        )

        portfolio = Portfolio.objects.get(user=self.user)
        self.assertRedirects(
            response,
            f"{reverse('portfolio')}?portfolio={portfolio.id}",
            fetch_redirect_response=False,
        )
        self.assertEqual(portfolio.name, "Sharada Devi Shrestha")
        self.assertEqual(portfolio.holdings.count(), 1)

    def test_create_imports_holdings_and_wacc_together_and_honors_typed_name(self):
        response = self.client.post(
            reverse("portfolio_manage"),
            {
                "action": "create",
                "name": "Family Portfolio",
                "portfolio_type": Portfolio.PERSONAL,
                "file": self._holdings_upload(),
                "wacc_file": self._wacc_upload(),
            },
        )

        portfolio = Portfolio.objects.get(user=self.user)
        self.assertRedirects(
            response,
            f"{reverse('portfolio')}?portfolio={portfolio.id}",
            fetch_redirect_response=False,
        )
        self.assertEqual(portfolio.name, "Family Portfolio")
        self.assertEqual(portfolio.holdings.count(), 1)
        self.assertEqual(portfolio.costs.count(), 1)
        self.assertEqual(portfolio.costs.get().symbol, "NABIL")

    def test_create_controls_render_in_requested_order(self):
        response = self.client.get(reverse("portfolio"))
        html = response.content.decode()

        positions = [
            html.index('name="name"'),
            html.index('id="pf-new-portfolio-file"'),
            html.index('id="pf-new-wacc-file"'),
            html.index(">Create Portfolio</button>"),
        ]
        self.assertEqual(positions, sorted(positions))
        self.assertContains(response, 'class="pf-manager-form pf-create-series"')
        self.assertContains(response, 'class="pf-portfolio-manager" open')
        self.assertNotContains(response, 'class="pf-import-card"')
        self.assertContains(
            response,
            'class="pf-file-picker pf-file-picker-combined"',
            count=2,
        )
        self.assertContains(response, "My Shares Values")
        self.assertContains(response, "My WACC Report")
        self.assertNotContains(response, "Choose Holdings")
        self.assertNotContains(response, "Choose WACC")
        self.assertNotContains(response, "Upload WACC <small>optional</small>")

    def test_active_delete_is_kept_inside_manager_without_duplicate_toolbar(self):
        primary = Portfolio.objects.create(user=self.user, name="Saraswoti Joshi Shrestha")
        Portfolio.objects.create(user=self.user, name="Other", is_default=True)
        Holding.objects.create(
            portfolio=primary, symbol="NABIL", quantity=10, last_close=500, ltp=502
        )

        response = self.client.get(reverse("portfolio"), {"portfolio": primary.id})
        html = response.content.decode()

        manager_start = html.index("<summary>Manage portfolios</summary>")
        manager_end = html.index("</section>", manager_start)
        active_actions = html.index("<summary>Active portfolio actions</summary>")
        delete_action = html.index("Delete Portfolio", active_actions)

        self.assertNotContains(response, 'class="pf-toolbar"')
        self.assertLess(manager_start, active_actions)
        self.assertLess(active_actions, delete_action)
        self.assertLess(delete_action, manager_end)
        self.assertContains(response, "Delete Portfolio")
        self.assertNotContains(response, "Clear Holdings")
        self.assertNotContains(response, "Update Holdings")
        self.assertNotContains(response, "Import WACC (cost)")
        self.assertNotContains(response, 'id="pf-holdings-file"')
        self.assertNotContains(response, 'id="pf-wacc-file"')
        self.assertNotContains(response, "Rename active")
        self.assertNotContains(response, "Duplicate")
        self.assertNotContains(response, "Set Default")
        self.assertNotContains(response, "Archive")
        self.assertContains(response, f'data-name="{primary.name}"')
        self.assertContains(
            response,
            'class="pf-file-picker pf-file-picker-combined"',
            count=2,
        )
        element_ids = re.findall(r'\sid="([^"]+)"', html)
        self.assertEqual(len(element_ids), len(set(element_ids)))

    def test_portfolio_page_creates_default_portfolio(self):
        response = self.client.get(reverse("portfolio"))

        portfolio = Portfolio.objects.get(user=self.user)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(portfolio.name, "My Portfolio")
        self.assertEqual(portfolio.portfolio_type, Portfolio.PERSONAL)
        self.assertTrue(portfolio.is_default)
        self.assertFalse(portfolio.is_archived)

    def test_manage_create_rename_set_default_archive_and_delete(self):
        self.client.get(reverse("portfolio"))  # seeds the default legacy portfolio

        response = self.client.post(
            reverse("portfolio_manage"),
            {"action": "create", "name": "Spouse", "portfolio_type": Portfolio.SPOUSE},
        )
        spouse = Portfolio.objects.get(user=self.user, name="Spouse")
        self.assertRedirects(
            response,
            f"{reverse('portfolio')}?portfolio={spouse.id}",
            fetch_redirect_response=False,
        )
        self.assertFalse(spouse.is_default)
        self.assertEqual(spouse.portfolio_type, Portfolio.SPOUSE)

        self.client.post(
            reverse("portfolio_manage"),
            {"action": "rename", "portfolio_id": spouse.id, "name": "Family"},
        )
        spouse.refresh_from_db()
        self.assertEqual(spouse.name, "Family")

        self.client.post(
            reverse("portfolio_manage"),
            {"action": "set_default", "portfolio_id": spouse.id},
        )
        spouse.refresh_from_db()
        self.assertTrue(spouse.is_default)
        self.assertEqual(
            Portfolio.objects.filter(user=self.user, is_default=True).count(),
            1,
        )

        self.client.post(
            reverse("portfolio_manage"),
            {"action": "archive", "portfolio_id": spouse.id},
        )
        spouse.refresh_from_db()
        self.assertTrue(spouse.is_archived)
        self.assertFalse(spouse.is_default)
        self.assertTrue(Portfolio.objects.get(user=self.user, name="My Portfolio").is_default)

        self.client.post(
            reverse("portfolio_manage"),
            {"action": "delete", "portfolio_id": spouse.id},
        )
        self.assertFalse(Portfolio.objects.filter(pk=spouse.pk).exists())

    def test_duplicate_copies_holdings_and_wacc_rows_independently(self):
        source = Portfolio.objects.create(
            user=self.user, name="Personal", portfolio_type=Portfolio.PERSONAL, is_default=True
        )
        Holding.objects.create(portfolio=source, symbol="NABIL", quantity=10, last_close=500, ltp=502)
        HoldingCost.objects.create(
            portfolio=source, symbol="NABIL", wacc_rate=400, quantity=10, total_cost=4000
        )

        self.client.post(
            reverse("portfolio_manage"),
            {"action": "duplicate", "portfolio_id": source.id},
        )

        duplicate = Portfolio.objects.get(user=self.user, name="Personal Copy")
        self.assertFalse(duplicate.is_default)
        self.assertEqual(duplicate.holdings.count(), 1)
        self.assertEqual(duplicate.costs.count(), 1)
        self.assertNotEqual(duplicate.holdings.first().portfolio_id, source.id)

    def test_delete_active_duplicate_cascades_data_and_keeps_an_active_portfolio(self):
        primary = Portfolio.objects.create(user=self.user, name="Primary", is_default=True)
        duplicate = Portfolio.objects.create(user=self.user, name="Primary Copy")
        Holding.objects.create(
            portfolio=duplicate, symbol="NABIL", quantity=10, last_close=500, ltp=502
        )
        HoldingCost.objects.create(
            portfolio=duplicate, symbol="NABIL", wacc_rate=400, quantity=10, total_cost=4000
        )

        response = self.client.post(
            reverse("portfolio_manage"),
            {"action": "delete", "portfolio_id": duplicate.id},
        )

        self.assertRedirects(
            response,
            f"{reverse('portfolio')}?portfolio={primary.id}",
            fetch_redirect_response=False,
        )
        self.assertFalse(Portfolio.objects.filter(pk=duplicate.pk).exists())
        self.assertTrue(Portfolio.objects.filter(pk=primary.pk, is_default=True).exists())

    def test_last_active_portfolio_cannot_be_deleted(self):
        portfolio = Portfolio.objects.create(user=self.user, name="Only", is_default=True)

        response = self.client.post(
            reverse("portfolio_manage"),
            {"action": "delete", "portfolio_id": portfolio.id},
        )

        self.assertRedirects(
            response,
            f"{reverse('portfolio')}?portfolio={portfolio.id}",
            fetch_redirect_response=False,
        )
        self.assertTrue(Portfolio.objects.filter(pk=portfolio.pk).exists())

    def test_portfolio_names_are_case_insensitively_unique(self):
        Portfolio.objects.create(user=self.user, name="Personal", is_default=True)

        self.client.post(
            reverse("portfolio_manage"),
            {"action": "create", "name": "personal", "portfolio_type": Portfolio.PERSONAL},
        )

        self.assertEqual(Portfolio.objects.filter(user=self.user).count(), 1)

    def test_compact_selector_replaces_redundant_portfolio_tabs(self):
        Portfolio.objects.create(user=self.user, name="Primary", is_default=True)
        Portfolio.objects.create(user=self.user, name="Primary Copy")

        response = self.client.get(reverse("portfolio"))

        self.assertContains(response, 'id="pf-portfolio-select"')
        self.assertContains(response, "Primary (default)")
        self.assertContains(response, "Primary Copy")
        self.assertNotContains(response, 'class="pf-portfolio-tabs"')
        self.assertNotContains(response, 'class="pf-tab-delete"')

    def test_portfolio_data_api_uses_requested_portfolio(self):
        p1 = Portfolio.objects.create(user=self.user, name="Personal", is_default=True)
        p2 = Portfolio.objects.create(user=self.user, name="Client", portfolio_type=Portfolio.CLIENT)

        with patch(
            "core_analysis.services.portfolio_analytics.build_portfolio_payload",
            side_effect=lambda portfolio, **kwargs: {"ok": True, "portfolio": portfolio.name},
        ):
            response = self.client.get(reverse("portfolio_data_api"), {"portfolio": p2.id})

        payload = json.loads(response.content)
        self.assertEqual(payload["portfolio_id"], p2.id)
        self.assertEqual(payload["portfolio_name"], "Client")
        self.assertEqual(payload["portfolio_type"], Portfolio.CLIENT)
        p1.refresh_from_db()
        self.assertTrue(p1.is_default)

    def test_portfolio_data_api_passes_liquidity_stress_assumptions(self):
        portfolio = Portfolio.objects.create(user=self.user, name="Personal", is_default=True)

        with patch(
            "core_analysis.services.portfolio_analytics.build_portfolio_payload",
            return_value={"ok": True, "portfolio": portfolio.name},
        ) as build:
            response = self.client.get(
                reverse("portfolio_data_api"),
                {"participation": "25", "liquidation_pct": "62.5"},
            )

        self.assertEqual(response.status_code, 200)
        build.assert_called_once_with(
            portfolio,
            participation_rate="25",
            liquidation_target="62.5",
        )

    def test_portfolio_data_api_rejects_another_users_portfolio(self):
        other = get_user_model().objects.create_user(username="other-investor")
        foreign = Portfolio.objects.create(user=other, name="Private")

        response = self.client.get(
            reverse("portfolio_data_api"), {"portfolio": foreign.id}
        )

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json(), {"ok": False, "error": "Portfolio not found."})

    def test_import_and_clear_never_fall_back_from_a_foreign_portfolio_id(self):
        own = Portfolio.objects.create(user=self.user, name="Own", is_default=True)
        Holding.objects.create(
            portfolio=own, symbol="NABIL", quantity=25, last_close=500, ltp=502
        )
        HoldingCost.objects.create(
            portfolio=own, symbol="NABIL", wacc_rate=400, quantity=25, total_cost=10_000
        )
        other = get_user_model().objects.create_user(username="foreign-owner")
        foreign = Portfolio.objects.create(user=other, name="Foreign", is_default=True)

        self.client.post(
            reverse("portfolio_import"),
            {"portfolio_id": foreign.id, "file": self._holdings_upload()},
        )
        self.client.post(
            reverse("portfolio_wacc_import"),
            {"portfolio_id": foreign.id, "file": self._wacc_upload()},
        )
        self.client.post(reverse("portfolio_clear"), {"portfolio_id": foreign.id})

        self.assertEqual(own.holdings.get().quantity, 25)
        self.assertEqual(own.costs.get().wacc_rate, 400)
        self.assertFalse(foreign.holdings.exists())
        self.assertFalse(foreign.costs.exists())

    def test_import_rejects_unsupported_extension_without_replacing_holdings(self):
        portfolio = Portfolio.objects.create(user=self.user, name="Own", is_default=True)
        Holding.objects.create(portfolio=portfolio, symbol="NABIL", quantity=25)
        disguised_csv = SimpleUploadedFile(
            "my-shares.exe",
            b"Scrip,Current Balance\nCHCL,99\n",
            content_type="application/octet-stream",
        )

        response = self.client.post(
            reverse("portfolio_import"),
            {"portfolio_id": portfolio.id, "file": disguised_csv},
            follow=True,
        )

        self.assertContains(response, "Unsupported holdings file")
        self.assertEqual(list(portfolio.holdings.values_list("symbol", flat=True)), ["NABIL"])

    def test_archiving_with_limited_update_fields_also_clears_default(self):
        portfolio = Portfolio.objects.create(user=self.user, name="Own", is_default=True)

        portfolio.is_archived = True
        portfolio.save(update_fields=["is_archived"])
        portfolio.refresh_from_db()

        self.assertTrue(portfolio.is_archived)
        self.assertFalse(portfolio.is_default)


class PortfolioBrokerLedgerTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="ledger-investor", password="StrongPass123!"
        )
        self.client.force_login(self.user)
        self.portfolio = Portfolio.objects.create(
            user=self.user, name="Ledger Portfolio", is_default=True
        )
        Holding.objects.create(
            portfolio=self.portfolio, symbol="NABIL", quantity=5, last_close=130, ltp=130
        )
        HoldingCost.objects.create(
            portfolio=self.portfolio,
            symbol="NABIL",
            wacc_rate=101,
            quantity=5,
            total_cost=505,
            modified="2026-07-13 10:00:00",
        )

    @staticmethod
    def _ledger_upload(name="account-statement.csv", extra_line=""):
        content = (
            "Account Name: 7: Test Investor\n"
            "From Date: 07/13/2026\n"
            "To Date: 07/13/2026\n"
            "Fiscal Year: 2082/83\n"
            "SN,Date AD,Date BS,Voucher No.,Particulars,Reference No.,Branch,Debit,Credit,Balance\n"
            "1,,,,Opening Balance,,,,,0.00\n"
            "2,07/13/2026,2083-03-29,JV/000001/R/082-083,Received from Test Investor,R/000001/082-083,KTM,,1000.00,1000.00 CR\n"
            "3,07/13/2026,2083-03-29,JV/000002/B/082-083,Receivable from client for purchase bill no. B/000002/082-083 (NABIL 10 kitta @ 100.00),B/000002/082-083,KTM,1010.00,,10.00 DR\n"
            "4,07/13/2026,2083-03-29,JV/000003/S/082-083,Payable to client for sell bill no. S/000003/082-083 (NABIL 5 kitta @ 120.00),S/000003/082-083,KTM,,590.00,580.00 CR\n"
            f"{extra_line}"
        )
        return SimpleUploadedFile(name, content.encode("utf-8"), content_type="text/csv")

    def _import(self, upload=None, portfolio=None):
        return self.client.post(
            reverse("portfolio_ledger_import"),
            {
                "portfolio_id": (portfolio or self.portfolio).id,
                "file": upload or self._ledger_upload(),
            },
        )

    def test_fiscal_year_uses_exact_bs_month_boundary(self):
        from core_analysis.services.portfolio_ledger import fiscal_year_from_dates

        self.assertEqual(fiscal_year_from_dates("2082-03-30"), "2081/82")
        self.assertEqual(fiscal_year_from_dates("2082-04-01"), "2082/83")

    def test_import_persists_cash_transactions_trade_legs_and_metadata(self):
        response = self._import()

        self.assertRedirects(
            response,
            f"{reverse('portfolio')}?portfolio={self.portfolio.id}",
            fetch_redirect_response=False,
        )
        batch = BrokerLedgerImport.objects.get(portfolio=self.portfolio)
        self.assertEqual(batch.account_code, "7")
        self.assertEqual(batch.account_name, "Test Investor")
        self.assertEqual(batch.source_fiscal_year, "2082/83")
        self.assertEqual(batch.imported_rows, 4)
        self.assertEqual(batch.warning_count, 0)
        self.assertEqual(BrokerLedgerTransaction.objects.filter(portfolio=self.portfolio).count(), 4)
        self.assertEqual(BrokerTrade.objects.filter(transaction__portfolio=self.portfolio).count(), 2)
        sale = BrokerLedgerTransaction.objects.get(
            portfolio=self.portfolio, transaction_type=BrokerLedgerTransaction.SELL
        )
        self.assertEqual(sale.derived_deductions, Decimal("10.00"))
        self.assertEqual(sale.balance, Decimal("580.00"))
        self.assertEqual(sale.fiscal_year, "2082/83")

    def test_weighted_average_realized_pl_cash_and_reconciliation(self):
        from core_analysis.services.portfolio_ledger import build_ledger_dashboard

        self._import()
        dashboard = build_ledger_dashboard(self.portfolio, fiscal_year="2082/83")

        self.assertEqual(dashboard["summary"]["purchases"], Decimal("1010.00"))
        self.assertEqual(dashboard["summary"]["sales"], Decimal("590.00"))
        self.assertEqual(dashboard["summary"]["charges_and_taxes"], Decimal("20.00"))
        self.assertEqual(dashboard["summary"]["realized_pl"], Decimal("85.00"))
        self.assertEqual(dashboard["summary"]["realized_pl_confirmed"], Decimal("85.00"))
        self.assertEqual(dashboard["summary"]["realized_coverage_pct"], Decimal("100.00"))
        self.assertEqual(dashboard["summary"]["closing_balance"], Decimal("580.00"))
        self.assertEqual(dashboard["mismatched_symbols"], 0)

    def test_missing_opening_lot_uses_labelled_wacc_estimate(self):
        from core_analysis.services.portfolio_ledger import build_ledger_dashboard

        content = (
            "SN,Date AD,Date BS,Voucher No.,Particulars,Reference No.,Branch,Debit,Credit,Balance\n"
            "1,,,,Opening Balance,,,,,0.00\n"
            "2,07/13/2026,2083-03-29,JV/000010/S/082-083,Payable to client for sell bill no. S/000010/082-083 (NABIL 5 kitta @ 120.00),S/000010/082-083,KTM,,590.00,590.00 CR\n"
        )
        self._import(
            SimpleUploadedFile("opening-gap.csv", content.encode(), content_type="text/csv")
        )

        summary = build_ledger_dashboard(self.portfolio, "2082/83")["summary"]
        self.assertEqual(summary["realized_pl_confirmed"], Decimal("0.00"))
        self.assertEqual(summary["realized_pl_estimated"], Decimal("85.00"))
        self.assertEqual(summary["estimated_sale_qty"], Decimal("5.00"))
        self.assertEqual(summary["realized_coverage_pct"], Decimal("0.00"))

    def test_sequence_and_running_balance_gaps_are_flagged(self):
        from core_analysis.services.portfolio_ledger import build_ledger_dashboard

        content = (
            "SN,Date AD,Date BS,Voucher No.,Particulars,Reference No.,Branch,Debit,Credit,Balance\n"
            "1,,,,Opening Balance,,,,,0.00\n"
            "3,07/13/2026,2083-03-29,JV/000020/R/082-083,Received from Test,R/000020/082-083,KTM,,100.00,90.00 CR\n"
        )
        self._import(
            SimpleUploadedFile("gapped.csv", content.encode(), content_type="text/csv")
        )

        gap = BrokerLedgerTransaction.objects.get(source_row_no=3)
        self.assertTrue(gap.sequence_gap)
        self.assertTrue(gap.balance_mismatch)
        self.assertEqual(build_ledger_dashboard(self.portfolio)["integrity_issue_count"], 1)

    def test_exact_reimport_is_idempotent(self):
        self._import()
        self._import()

        self.assertEqual(BrokerLedgerImport.objects.filter(portfolio=self.portfolio).count(), 1)
        self.assertEqual(BrokerLedgerTransaction.objects.filter(portfolio=self.portfolio).count(), 4)
        self.assertEqual(BrokerTrade.objects.filter(transaction__portfolio=self.portfolio).count(), 2)

    def test_overlapping_different_file_skips_duplicate_transaction_fingerprints(self):
        self._import()
        self._import(self._ledger_upload(name="overlap.csv", extra_line="\n"))

        self.assertEqual(BrokerLedgerImport.objects.filter(portfolio=self.portfolio).count(), 2)
        latest = BrokerLedgerImport.objects.filter(portfolio=self.portfolio).order_by("-id").first()
        self.assertEqual(latest.imported_rows, 0)
        self.assertEqual(latest.duplicate_rows, 4)
        self.assertEqual(BrokerLedgerTransaction.objects.filter(portfolio=self.portfolio).count(), 4)

    def test_identical_rows_in_one_file_are_all_imported(self):
        """Two genuinely distinct rows that look identical (same day, blank
        voucher/reference, same amount — e.g. repeated DP charges) must both
        import. They share every fingerprinted field except the running
        balance, so the parser has to disambiguate them per occurrence rather
        than collapse them into one / crash on the unique constraint."""
        content = (
            "SN,Date AD,Date BS,Voucher No.,Particulars,Reference No.,Branch,Debit,Credit,Balance\n"
            "1,,,,Opening Balance,,,,,1000.00\n"
            "2,07/13/2026,2083-03-29,,DP Charge,,KTM,25.00,,975.00 DR\n"
            "3,07/13/2026,2083-03-29,,DP Charge,,KTM,25.00,,950.00 DR\n"
        )
        response = self._import(
            SimpleUploadedFile("dp-charges.csv", content.encode(), content_type="text/csv")
        )

        self.assertRedirects(
            response,
            f"{reverse('portfolio')}?portfolio={self.portfolio.id}",
            fetch_redirect_response=False,
        )
        batch = BrokerLedgerImport.objects.get(portfolio=self.portfolio)
        self.assertEqual(batch.imported_rows, 3)
        self.assertEqual(batch.duplicate_rows, 0)
        self.assertEqual(
            BrokerLedgerTransaction.objects.filter(portfolio=self.portfolio).count(), 3
        )
        # Re-importing the same file stays idempotent (file-hash short-circuit).
        self._import(
            SimpleUploadedFile("dp-charges.csv", content.encode(), content_type="text/csv")
        )
        self.assertEqual(
            BrokerLedgerTransaction.objects.filter(portfolio=self.portfolio).count(), 3
        )

    def test_foreign_portfolio_cannot_be_mutated(self):
        other = get_user_model().objects.create_user(username="ledger-other")
        foreign = Portfolio.objects.create(user=other, name="Private")

        self._import(portfolio=foreign)

        self.assertFalse(foreign.ledger_transactions.exists())
        self.assertFalse(self.portfolio.ledger_transactions.exists())

    def test_page_filters_fy_and_exposes_accounting_summary(self):
        self._import()
        Holding.objects.filter(portfolio=self.portfolio, symbol="NABIL").update(quantity=6)

        response = self.client.get(
            reverse("portfolio"), {"portfolio": self.portfolio.id, "fy": "2082/83"}
        )

        self.assertContains(response, "Broker Ledger &amp; Fiscal Year")
        self.assertContains(response, "FY 2082/83 Transactions")
        self.assertContains(response, "Realized P/L")
        self.assertContains(response, "85.00")
        self.assertContains(response, "JV/000003/S/082-083")
        self.assertContains(response, 'class="pf-create-panel pf-ledger-manager-panel"')
        self.assertContains(response, 'id="pf-ledger-file"', count=1)
        self.assertContains(response, 'class="pf-reconciliation pf-ledger-collapse" open', count=1)
        self.assertContains(response, "Holdings/WACC reconciliation", count=1)
        self.assertContains(response, 'class="pf-ledger-transactions pf-ledger-collapse" open')
        self.assertContains(response, 'class="pf-ledger-toggle-hide">Hide</b>')
        self.assertContains(response, 'class="pf-ledger-toggle-show">Show</b>')
        self.assertContains(response, "Risk Decomposition &amp; Sector Exposure", count=1)
        self.assertContains(response, 'id="pf-factors"', count=1)
        self.assertContains(response, 'id="pf-sector-donut"', count=1)

        html = response.content.decode()
        manager_start = html.index("<summary>Manage portfolios</summary>")
        manager_end = html.index("</section>", manager_start)
        ledger_controls = html.index("Broker Account Statement")
        self.assertLess(manager_start, ledger_controls)
        self.assertLess(ledger_controls, manager_end)
        risk_sector_card = html.index('id="pf-risk-sector-decomposition"')
        factor_view = html.index('id="pf-factors"')
        sector_view = html.index('id="pf-sector-donut"')
        combined_card = html.index('id="pf-concentration-reconciliation"')
        concentration = html.index("Concentration &amp; Attribution", combined_card)
        reconciliation = html.index("Holdings/WACC reconciliation")
        ledger_section = html.index('id="broker-ledger"')
        transactions = html.index("FY 2082/83 Transactions")
        self.assertLess(risk_sector_card, factor_view)
        self.assertLess(factor_view, sector_view)
        self.assertLess(sector_view, combined_card)
        self.assertLess(combined_card, concentration)
        self.assertLess(concentration, reconciliation)
        self.assertLess(reconciliation, ledger_section)
        self.assertLess(ledger_section, transactions)

    def test_empty_portfolio_keeps_reconciliation_in_ledger_fallback(self):
        self._import()
        self.portfolio.holdings.all().delete()

        response = self.client.get(reverse("portfolio"), {"portfolio": self.portfolio.id})

        html = response.content.decode()
        self.assertNotContains(response, 'id="pf-concentration-reconciliation"')
        self.assertContains(response, "Holdings/WACC reconciliation", count=1)
        self.assertLess(html.index('id="broker-ledger"'), html.index("Holdings/WACC reconciliation"))

    def test_fy_transactions_paginate_five_rows_at_a_time(self):
        extra = (
            "5,07/13/2026,2083-03-29,JV/000004/R/082-083,Received from Test,R/000004/082-083,KTM,,10.00,590.00 CR\n"
            "6,07/13/2026,2083-03-29,JV/000005/R/082-083,Received from Test,R/000005/082-083,KTM,,10.00,600.00 CR\n"
            "7,07/13/2026,2083-03-29,JV/000006/R/082-083,Received from Test,R/000006/082-083,KTM,,10.00,610.00 CR\n"
            "8,07/13/2026,2083-03-29,JV/000007/R/082-083,Received from Test,R/000007/082-083,KTM,,10.00,620.00 CR\n"
        )
        self._import(self._ledger_upload(name="paged-ledger.csv", extra_line=extra))

        first = self.client.get(
            reverse("portfolio"),
            {"portfolio": self.portfolio.id, "fy": "2082/83", "ledger_page": 1},
        )
        self.assertEqual(len(first.context["ledger"]["transactions"]), 5)
        self.assertContains(first, "Page 1 of 2")
        self.assertContains(first, "JV/000007/R/082-083")
        self.assertNotContains(first, "JV/000001/R/082-083")

        second = self.client.get(
            reverse("portfolio"),
            {"portfolio": self.portfolio.id, "fy": "2082/83", "ledger_page": 2},
        )
        self.assertEqual(len(second.context["ledger"]["transactions"]), 2)
        self.assertContains(second, "Page 2 of 2")
        self.assertContains(second, "JV/000001/R/082-083")

    def test_duplicate_portfolio_copies_ledger_independently(self):
        self._import()

        self.client.post(
            reverse("portfolio_manage"),
            {"action": "duplicate", "portfolio_id": self.portfolio.id},
        )

        duplicate = Portfolio.objects.get(user=self.user, name="Ledger Portfolio Copy")
        self.assertEqual(duplicate.ledger_imports.count(), 1)
        self.assertEqual(duplicate.ledger_transactions.count(), 4)
        self.assertEqual(BrokerTrade.objects.filter(transaction__portfolio=duplicate).count(), 2)
        self.assertNotEqual(
            duplicate.ledger_transactions.first().portfolio_id,
            self.portfolio.id,
        )


class PortfolioStressAnalyticsTests(SimpleTestCase):
    def test_latest_prices_include_previous_close_for_daily_gain_loss(self):
        latest = date(2026, 7, 10)
        with (
            patch.object(portfolio_analytics, "_latest_session", return_value=latest),
            patch("core_analysis.models.NepseDailyStockPrice.objects") as objects,
        ):
            objects.filter.return_value.order_by.return_value.values_list.return_value = [
                ("NABIL", latest, Decimal("510.00"), Decimal("1000000"), Decimal("500.00")),
            ]

            prices = portfolio_analytics._latest_prices(["NABIL"])

        self.assertEqual(prices["NABIL"], (510.0, latest, 1000000.0, 500.0))

    def test_float_coercion_rejects_non_finite_values(self):
        self.assertEqual(portfolio_analytics._f(float("nan"), 7.0), 7.0)
        self.assertEqual(portfolio_analytics._f(float("inf"), 7.0), 7.0)

    def test_liquidation_milestones_use_parallel_position_capacity(self):
        rows = [
            {"value": 1000.0, "adv_qty": 100.0, "price": 10.0},
            {"value": 1000.0, "adv_qty": 50.0, "price": 10.0},
        ]

        self.assertAlmostEqual(
            portfolio_analytics._days_to_liquidate_value(rows, 2000.0, 25, 0.10),
            10 / 3,
            places=4,
        )
        self.assertAlmostEqual(
            portfolio_analytics._days_to_liquidate_value(rows, 2000.0, 50, 0.10),
            20 / 3,
            places=4,
        )
        self.assertAlmostEqual(
            portfolio_analytics._days_to_liquidate_value(rows, 2000.0, 75, 0.10),
            10.0,
            places=4,
        )
        self.assertAlmostEqual(
            portfolio_analytics._days_to_liquidate_value(rows, 2000.0, 100, 0.10),
            20.0,
            places=4,
        )

    def test_liquidation_target_reports_unreachable_untradeable_value(self):
        rows = [
            {"value": 500.0, "adv_qty": 100.0, "price": 10.0},
            {"value": 500.0, "adv_qty": 0.0, "price": 10.0},
        ]

        self.assertIsNone(
            portfolio_analytics._days_to_liquidate_value(rows, 1000.0, 75, 0.20)
        )

    def test_liquidity_risk_labels_match_required_bands(self):
        self.assertEqual(portfolio_analytics._liquidity_risk_label("liquid"), "Low")
        self.assertEqual(portfolio_analytics._liquidity_risk_label("moderate"), "Moderate")
        self.assertEqual(portfolio_analytics._liquidity_risk_label("illiquid"), "High")
        self.assertEqual(
            portfolio_analytics._liquidity_risk_label("untradeable"),
            "Very High",
        )

    def test_var_matrix_contains_all_horizons_confidences_and_methods(self):
        start = date(2026, 1, 1)
        returns = [
            -0.08, -0.05, -0.03, -0.02, -0.01,
            0.0, 0.01, 0.02, 0.03, 0.04,
        ] * 4
        stock_returns = {
            "AAA": {
                start + timedelta(days=offset): value
                for offset, value in enumerate(returns)
            }
        }

        risk = portfolio_analytics._risk_block(
            {"AAA": 1.0},
            100000.0,
            1.2,
            stock_returns,
            {"value": 2800.0, "highest": 3200.0},
        )
        var = risk["var"]

        for method in ("hist", "param"):
            for confidence in ("95", "99"):
                for horizon in ("1d", "10d"):
                    self.assertIn(f"{method}_{confidence}_{horizon}_pct", var)
                    self.assertIn(f"{method}_{confidence}_{horizon}_rs", var)
        for confidence in ("95", "99"):
            for horizon in ("1d", "10d"):
                self.assertIn(f"cvar_{confidence}_{horizon}_pct", var)
        self.assertGreaterEqual(var["cvar_95_1d_pct"], var["hist_95_1d_pct"])
        self.assertGreater(var["param_99_10d_pct"], var["param_95_10d_pct"])

    def test_beta_stress_scenarios_include_required_targets_and_nav(self):
        scenarios = portfolio_analytics._beta_stress_scenarios(
            1.1,
            100000.0,
            {"value": 2800.0, "highest": 3250.0},
        )
        labels = {row["label"] for row in scenarios}

        self.assertTrue({
            "Current NEPSE",
            "NEPSE -20%", "NEPSE -10%", "NEPSE -5%",
            "NEPSE +5%", "NEPSE +10%", "NEPSE +20%",
            "NEPSE at 3,200", "NEPSE at all-time high",
        }.issubset(labels))
        current = next(row for row in scenarios if row["kind"] == "current")
        self.assertEqual(current["shock"], 0.0)
        self.assertEqual(current["impact_rs"], 0.0)
        self.assertEqual(current["portfolio_value"], 100000.0)
        self.assertEqual(current["target_index"], 2800.0)
        for row in scenarios:
            self.assertAlmostEqual(
                row["portfolio_value"],
                100000.0 + row["impact_rs"],
                places=2,
            )
            self.assertEqual(row["nav"], row["portfolio_value"])

    def test_beta_stress_merges_3200_with_nearby_all_time_high(self):
        scenarios = portfolio_analytics._beta_stress_scenarios(
            0.8,
            100000.0,
            {
                "value": 2622.0,
                "highest": 3198.0,
                "highest_date": "2021-08-18",
            },
        )
        target_rows = [row for row in scenarios if row["kind"] not in ("shock", "current")]

        self.assertEqual(len(target_rows), 1)
        self.assertEqual(target_rows[0]["label"], "NEPSE 3,200 / ATH")
        self.assertEqual(target_rows[0]["target_index"], 3200.0)
        self.assertIn("3,198.00", target_rows[0]["reference"])
        self.assertIn("2021-08-18", target_rows[0]["reference"])


@unittest.skipIf(udf_views is None, "Django settings unavailable")
class UdfChartBarsTests(SimpleTestCase):
    def test_index_search_returns_all_configured_subindices(self):
        request = RequestFactory().get(
            "/insights/udf/search",
            {"type": "index", "limit": "50"},
        )

        response = udf_views.udf_search(request)
        payload = json.loads(response.content)

        self.assertEqual(
            {row["symbol"] for row in payload},
            set(udf_views.INDEX_TICKERS),
        )

    def test_chart_bars_appends_live_index_bar(self):
        stored = [(date(2026, 6, 15), 1.0, 2.0, 0.5, 1.5, 1000)]
        live = (date(2026, 6, 16), 2.0, 3.0, 1.5, 2.5, 2000)

        with (
            patch.object(udf_views, "_bars", return_value=stored) as bars,
            patch.object(udf_views, "_live_index_bar", return_value=live),
        ):
            result = udf_views._chart_bars("index", "NEPSE INDEX", None, date(2026, 6, 16), 5000)

        bars.assert_called_once_with("index", "NEPSE INDEX", None, date(2026, 6, 16), 5000)
        self.assertEqual(result, stored + [live])

    def test_chart_bars_keeps_synced_bar_over_live_same_day(self):
        # Once the post-close sync has written today's row, that EOD bar is
        # authoritative — the live snapshot (which can lag a session behind the
        # official close) must NOT overwrite it, or the candle's close can fall
        # below its own low. See _append_live_index_bar.
        stored = [(date(2026, 6, 16), 2632.96, 2638.17, 2603.44, 2608.33, 6592209)]
        live = (date(2026, 6, 16), 2631.55, 2638.17, 2584.79, 2584.79, 6592209)

        with (
            patch.object(udf_views, "_bars", return_value=stored),
            patch.object(udf_views, "_live_index_bar", return_value=live),
        ):
            result = udf_views._chart_bars("index", "NEPSE INDEX", None, date(2026, 6, 16), 5000)

        self.assertEqual(result, stored)

    def _nepse_live(self, sub_row, headline):
        return (
            patch.object(udf_views, "fetch_subindices", return_value={"NepseIndex": sub_row}),
            patch.object(udf_views, "fetch_contributors", return_value={"index": headline}),
        )

    def test_live_bar_rejects_headline_rolled_past_the_close(self):
        # 2026-09-14, after the close but before the EOD sync. The contributors
        # feed had rolled a session: prev_close 2585.04 was the day's true close,
        # and value 2610.83 re-applied the day's +1% on top of it. The candle must
        # close at the sub-index close, not at a high the market never printed.
        stored = [(date(2026, 9, 11), 2531.55, 2559.49, 2524.55, 2559.49, 10765179)]
        sub = {"businessDate": "2026-09-14", "openIndex": 2559.49, "highIndex": 2587.47,
               "lowIndex": 2556.90, "closingIndex": 2585.04, "turnoverVolume": 10666631}
        subs, contrib = self._nepse_live(sub, {"value": 2610.83, "prev_close": 2585.04})

        with patch.object(udf_views, "_bars", return_value=stored), subs, contrib:
            result = udf_views._chart_bars("index", "NEPSE INDEX", None, date(2026, 9, 14), 5000)

        bar = result[-1]
        self.assertEqual(bar[0], date(2026, 9, 14))
        self.assertAlmostEqual(bar[4], 2585.04)
        self.assertAlmostEqual(bar[2], 2587.47)      # high not stretched to the bogus value
        self.assertLessEqual(bar[4], bar[2])

    def test_live_bar_uses_headline_intraday_when_session_matches(self):
        # Intraday the sub-index close is not published yet; the headline value is
        # the live index and references the stored previous close, so it is used.
        stored = [(date(2026, 9, 11), 2531.55, 2559.49, 2524.55, 2559.49, 10765179)]
        sub = {"businessDate": "2026-09-14", "openIndex": 2559.49, "highIndex": 2570.10,
               "lowIndex": 2556.90, "closingIndex": 0, "turnoverVolume": 5000000}
        subs, contrib = self._nepse_live(sub, {"value": 2566.20, "prev_close": 2559.49})

        with patch.object(udf_views, "_bars", return_value=stored), subs, contrib:
            result = udf_views._chart_bars("index", "NEPSE INDEX", None, date(2026, 9, 14), 5000)

        self.assertAlmostEqual(result[-1][4], 2566.20)


class WaccImportTests(SimpleTestCase):
    def test_holdings_normalizer_rejects_non_positive_and_non_finite_balances(self):
        from core_analysis.portfolio_views import _normalize_holdings_table

        table = [
            ["Scrip", "Current Balance", "Last Closing Price", "LTP"],
            ["NEG", "-1", "100", "100"],
            ["NAN", "NaN", "100", "100"],
            ["INF", "Infinity", "100", "100"],
            ["OK", "2", "-100", "101"],
        ]

        rows, skipped = _normalize_holdings_table(table)

        self.assertEqual([row["symbol"] for row in rows], ["OK"])
        self.assertIsNone(rows[0]["last_close"])
        self.assertEqual(rows[0]["ltp"], 101)
        self.assertEqual(skipped, 3)

    def test_normalizers_reject_unreasonable_position_counts(self):
        from core_analysis.portfolio_views import MAX_IMPORT_ROWS, _normalize_holdings_table

        table = [["Scrip", "Current Balance"]]
        table.extend([f"S{i}", "1"] for i in range(MAX_IMPORT_ROWS + 1))

        with self.assertRaisesRegex(ValueError, "too many holdings"):
            _normalize_holdings_table(table)

    def test_wacc_normalizer_rejects_invalid_or_out_of_range_values(self):
        from core_analysis.portfolio_views import _normalize_wacc_table

        table = [
            ["Scrip Name", "WACC Calculated Quantity", "WACC Rate", "Total Cost"],
            ["NEG", "10", "-1", "100"],
            ["INF", "10", "Infinity", "100"],
            ["HUGE", "10", "1e50", "100"],
            ["SYMBOL-THAT-IS-FAR-TOO-LONG", "10", "100", "1000"],
            ["OK", "10", "100", "1000"],
        ]

        rows, skipped = _normalize_wacc_table(table)

        self.assertEqual([row["symbol"] for row in rows], ["OK"])
        self.assertEqual(skipped, 4)

    def test_normalize_maps_newline_headers_and_skips_noise(self):
        from core_analysis.portfolio_views import _normalize_wacc_table
        # Mirrors pdfplumber's extraction: header cells carry embedded newlines.
        table = [
            ["S.N.", "Scrip Name", "WACC\nCalculated\nQuantity", "WACC Rate",
             "Total Cost of\nCapital", "Last\nModification\nDate"],
            ["1.", "GCIL", "2", "252.2728", "504.5455", "2026-03-12 14:28:46"],
            ["2.", "HBL", "10", "157.0", "1570.0", "2025-09-26 13:19:15"],
            ["", "", "", "", "", ""],                       # blank row
            ["", "Total :", "", "", "", ""],                # summary row
        ]
        rows, skipped = _normalize_wacc_table(table)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0], {
            "symbol": "GCIL", "wacc_rate": 252.2728, "quantity": 2.0,
            "total_cost": 504.5455, "modified": "2026-03-12 14:28:46",
        })
        self.assertEqual(rows[1]["symbol"], "HBL")
        self.assertEqual(rows[1]["wacc_rate"], 157.0)
        self.assertGreaterEqual(skipped, 1)  # the "Total :" line is skipped


class RrgAnalyticsTests(unittest.TestCase):
    @staticmethod
    def _price_frame(start, closes):
        return pd.DataFrame({
            "business_date": pd.date_range(start, periods=len(closes), freq="D"),
            "close_price_adj": closes,
        })

    def test_rrg_formula_reports_overlap_quality(self):
        from core_analysis.services.RGG_Chart import run_rrg_simulation

        stock = self._price_frame("2026-01-01", [100 + i * 1.8 for i in range(30)])
        benchmark = self._price_frame("2026-01-01", [1000 + i * 4.0 for i in range(30)])

        metrics, rows = run_rrg_simulation(stock, benchmark, lookback=5)

        self.assertNotIn("error", metrics)
        self.assertEqual(metrics["source_bars"], 30)
        self.assertEqual(metrics["benchmark_bars"], 30)
        self.assertEqual(metrics["matched_bars"], 30)
        self.assertEqual(metrics["data_points"], len(rows))
        self.assertEqual(len(rows), 22)
        self.assertAlmostEqual(rows.iloc[-1]["RS"], (stock.iloc[-1]["close_price_adj"] / benchmark.iloc[-1]["close_price_adj"]) * 100.0)
        self.assertTrue(np.isfinite(rows.iloc[-1]["RS_Ratio"]))
        self.assertTrue(np.isfinite(rows.iloc[-1]["RS_Momentum"]))

    def test_rrg_formula_rejects_sparse_overlap(self):
        from core_analysis.services.RGG_Chart import run_rrg_simulation

        stock = self._price_frame("2026-01-01", [100 + i for i in range(30)])
        benchmark = self._price_frame("2026-01-01", [1000 + i for i in range(8)])

        metrics, rows = run_rrg_simulation(stock, benchmark, lookback=5)

        self.assertIn("Insufficient overlapping data", metrics["error"])
        self.assertEqual(metrics["source_bars"], 30)
        self.assertEqual(metrics["benchmark_bars"], 8)
        self.assertEqual(metrics["matched_bars"], 8)
        self.assertTrue(rows.empty)

    def test_indices_rrg_preserves_skip_reason_and_shared_bar_count(self):
        from core_analysis.services.RGG_indices import run_rrg_indices_simulation

        benchmark = self._price_frame("2026-01-01", [1000 + i * 3.0 for i in range(30)])
        index_frames = {
            "BANKING SUBINDEX": self._price_frame("2026-01-01", [500 + i * 2.0 for i in range(30)]),
            "HYDROPOWER INDEX": self._price_frame("2026-01-01", [300 + i for i in range(8)]),
        }

        metrics, points, trails, skipped = run_rrg_indices_simulation(
            index_frames,
            benchmark,
            lookback=5,
            selected_symbols=["BANKING SUBINDEX", "HYDROPOWER INDEX"],
        )

        self.assertEqual(metrics["indices_plotted"], 1)
        self.assertEqual(points[0]["symbol"], "BANKING SUBINDEX")
        self.assertEqual(points[0]["matched_bars"], 30)
        self.assertTrue(trails)
        self.assertEqual(skipped[0]["symbol"], "HYDROPOWER INDEX")
        self.assertIn("Insufficient overlapping data", skipped[0]["reason"])


class WorkbenchSecurityTests(SimpleTestCase):
    def test_workbench_routes_redirect_anonymous_users_to_admin_login(self):
        client = Client()
        checks = [
            ("get", "/workbench/"),
            # /dashboard/symbols/ is intentionally public (search boxes on the
            # public analysis desks) — see symbol_autocomplete_view.
            ("post", "/dashboard/process/"),
            ("post", "/dashboard/delete/1/"),
            ("post", "/dashboard/sync/"),
            ("post", "/dashboard/sync-calculate/"),
        ]

        for method, path in checks:
            with self.subTest(path=path):
                response = getattr(client, method)(path)

                self.assertEqual(response.status_code, 302)
                self.assertIn("/admin/login/", response["Location"])


class FakeTechnicalAnalysis:
    @staticmethod
    def sma(series, length):
        values = pd.to_numeric(series, errors="coerce")
        if series.name == "volume":
            return pd.Series(values * 0.5, index=series.index)

        offset = {20: 1.0, 50: 2.0, 200: 3.0}.get(length, 1.0)
        return pd.Series(values - offset, index=series.index)

    @staticmethod
    def rsi(series, length):
        return pd.Series(60.0, index=series.index)

    @staticmethod
    def macd(series, fast, slow, signal):
        line = pd.Series(-1.0, index=series.index)
        if len(line) > 200:
            line.iloc[200:] = 1.0
        signal_line = pd.Series(0.0, index=series.index)
        return pd.DataFrame(
            {
                "MACD": line,
                "MACDh": line - signal_line,
                "MACDs": signal_line,
            },
            index=series.index,
        )

    @staticmethod
    def supertrend(high, low, close, length, multiplier):
        direction = pd.Series(1, index=close.index)
        if len(direction) > 205:
            direction.iloc[205] = -1
        return pd.DataFrame(
            {
                "SUPERT_10_3.0": pd.to_numeric(close, errors="coerce") - 5.0,
                "SUPERTd_10_3.0": direction,
            },
            index=close.index,
        )

    @staticmethod
    def vwap(high, low, close, volume):
        return pd.Series(pd.to_numeric(close, errors="coerce") - 1.0, index=close.index)

    @staticmethod
    def atr(high, low, close, length):
        return pd.Series(2.0, index=close.index)


@unittest.skipIf(market_insights is None, "Django settings unavailable")
class MarketInsightsHeadlineTests(unittest.TestCase):
    def test_payload_prefers_contributor_headline_over_stale_subindex(self):
        stale_subindex = {
            "NepseIndex": {
                "closingIndex": 2731.53,
                "absChange": 3.50,
                "percentageChange": 0.13,
                "highIndex": 2731.53,
                "lowIndex": 2721.40,
                "turnoverValue": 0,
                "businessDate": "2026-06-11",
            }
        }
        live_contributors = {
            "index": {
                "value": 2721.72,
                "change": -6.31,
                "prev_close": 2728.03,
                "pct": -0.23,
            },
            "positive": [],
            "negative": [],
        }
        summary = [{
            "businessDate": "2026-06-12",
            "totalTurnover": 1164469269.88,
            "totalTradedShares": 2524762,
            "totalTransactions": 0,
            "tradedScrips": 0,
        }]

        patches = [
            patch.object(market_insights.cache, "get", return_value=None),
            patch.object(market_insights.cache, "set"),
            patch.object(market_insights, "fetch_live_rows", return_value=None),
            patch.object(market_insights, "fetch_subindices", return_value=stale_subindex),
            patch.object(market_insights, "fetch_market_summary", return_value=summary),
            patch.object(market_insights, "_cached_contributors", return_value=live_contributors),
            patch.object(market_insights, "fetch_top_gainers", return_value=None),
            patch.object(market_insights, "fetch_top_losers", return_value=None),
            patch.object(market_insights, "fetch_top_active", return_value=None),
            patch.object(market_insights, "_sector_map", return_value={}),
            patch.object(market_insights, "_latest_stock_rows", return_value=(date(2026, 6, 11), [])),
            patch.object(market_insights, "_nepse_history", return_value=[]),
            # Pin the clock to the live (pre-3 PM NPT) session so this exercises
            # the feed-driven path regardless of when the suite runs.
            patch.object(market_insights, "_after_market_close", return_value=False),
        ]

        with ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            payload = market_insights.build_payload(force=True)

        self.assertEqual(payload["as_of"], "2026-06-12")
        self.assertEqual(payload["overview"]["nepse_index"], 2721.72)
        self.assertEqual(payload["overview"]["nepse_change"], -6.31)
        self.assertEqual(payload["overview"]["nepse_pct"], -0.23)
        self.assertEqual(payload["overview"]["turnover"], 1164469269.88)
        self.assertEqual(payload["overview"]["volume"], 2524762)

    def test_subindex_intraday_zero_close_recovers_prev_from_abschange(self):
        # Mid-session the NepseSubIndices feed leaves closingIndex at 0 and
        # computes its own absChange/percentageChange against that 0 — so it
        # reports absChange = -prevClose and percentageChange = -100%. Reading
        # that as the intraday value's move produced a ~5,251 "previous close"
        # (today + yesterday) and a bogus -49.98% headline. The metrics must
        # recover the previous close as -absChange and derive a small real move.
        row = {
            "closingIndex": 0.0,
            "openIndex": 2626.83,
            "highIndex": 2626.83,
            "lowIndex": 2626.83,
            "absChange": -2624.36,       # == -(yesterday's real close)
            "percentageChange": -100.0,
            "turnoverValue": 5927148186.8,
            "businessDate": "2026-09-18",
        }
        m = market_insights._subindex_metrics(row)
        self.assertEqual(round(m["value"], 2), 2626.83)
        self.assertEqual(round(m["value"] - m["change"], 2), 2624.36)  # prev close
        self.assertEqual(round(m["change"], 2), 2.47)
        self.assertAlmostEqual(m["pct"], 0.09, places=2)

    def test_subindex_published_close_is_unchanged(self):
        # A normal settled row (closingIndex > 0) must keep the feed's own values.
        row = {
            "closingIndex": 2624.36,
            "openIndex": 2612.0,
            "highIndex": 2637.58,
            "lowIndex": 2615.69,
            "absChange": 11.93,
            "percentageChange": 0.45,
            "businessDate": "2026-09-17",
        }
        m = market_insights._subindex_metrics(row)
        self.assertEqual(m["value"], 2624.36)
        self.assertEqual(m["change"], 11.93)
        self.assertEqual(m["pct"], 0.45)

    def test_stale_live_feed_falls_back_to_eod_for_stock_widgets(self):
        # The per-scrip live feed serves prior-session quotes (Jun 8) while the
        # official index headline reports the real trading day (Jun 16). The
        # heatmap/breadth must reflect the fresher end-of-day DB rows, not the
        # stale live quotes. Regression for the heatmap showing week-old prices.
        stale_live_rows = [{
            "symbol": "CFCL",
            "securityName": "Central Finance",
            "closePrice": 641.8,
            "previousDayClosePrice": 630.0,
            "openPrice": 630.0,
            "highPrice": 645.0,
            "lowPrice": 628.0,
            "totalTradedQuantity": 1000,
            "totalTradedValue": 641800.0,
            "totalTrades": 50,
            "marketCapitalization": 0.0,
            "businessDate": "2026-06-08",
            "lastUpdatedTime": "2026-06-08 15:00:00",
        }]
        eod_rows = [{
            "symbol": "CFCL",
            "security_name": "Central Finance",
            "open_price": 689.0,
            "high_price": 689.0,
            "low_price": 616.0,
            "close_price": 620.0,
            "previous_close": 659.0,
            "total_traded_quantity": 2000,
            "total_traded_value": 1240000.0,
            "total_trades": 80,
            "market_capitalization": 0.0,
        }]
        live_contributors = {
            "index": {"value": 2699.66, "change": -5.79, "prev_close": 2705.45, "pct": -0.21},
            "positive": [],
            "negative": [],
        }
        summary = [{
            "businessDate": "2026-06-16",
            "totalTurnover": 4047858084.12,
            "totalTradedShares": 100,
            "totalTransactions": 0,
            "tradedScrips": 0,
        }]

        patches = [
            patch.object(market_insights.cache, "get", return_value=None),
            patch.object(market_insights.cache, "set"),
            patch.object(market_insights, "fetch_live_rows", return_value=stale_live_rows),
            patch.object(market_insights, "fetch_subindices", return_value=None),
            patch.object(market_insights, "fetch_market_summary", return_value=summary),
            patch.object(market_insights, "_cached_contributors", return_value=live_contributors),
            patch.object(market_insights, "fetch_top_gainers", return_value=None),
            patch.object(market_insights, "fetch_top_losers", return_value=None),
            patch.object(market_insights, "fetch_top_active", return_value=None),
            patch.object(market_insights, "_sector_map", return_value={"CFCL": "Finance"}),
            patch.object(market_insights, "_latest_stock_rows", return_value=(date(2026, 6, 15), eod_rows)),
            patch.object(market_insights, "_nepse_history", return_value=[]),
            patch.object(market_insights, "_after_market_close", return_value=False),
        ]

        with ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            payload = market_insights.build_payload(force=True)

        # Stale live feed must not be badged LIVE, and the stock widgets must use EOD.
        self.assertFalse(payload["live"])
        self.assertEqual(payload["source"], "eod")
        cfcl = next(t for t in payload["heatmap"] if t["symbol"] == "CFCL")
        self.assertEqual(cfcl["ltp"], 620.0)      # EOD close, not the stale 641.8
        self.assertEqual(cfcl["pct"], -5.92)      # (620-659)/659, not the stale +1.87

    def test_after_close_serves_eod_from_sql_and_skips_live_feeds(self):
        # After 3 PM NPT the dashboard must ignore the intraday live feeds and
        # build the payload from the settled end-of-day DB rows, while kicking
        # off the one-shot background sync exactly once.
        eod_rows = [{
            "symbol": "NABIL",
            "security_name": "Nabil Bank",
            "open_price": 500.0,
            "high_price": 510.0,
            "low_price": 495.0,
            "close_price": 505.0,
            "previous_close": 500.0,
            "total_traded_quantity": 1000,
            "total_traded_value": 505000.0,
            "total_trades": 60,
            "market_capitalization": 0.0,
        }]

        def boom(*a, **k):
            raise AssertionError("a per-scrip live feed was called after market close")

        def contributor_boom(*a, **k):
            raise AssertionError("contributors were fetched synchronously after market close")

        # Contributors have no SQL equivalent, so use the cached feed after the
        # close and refresh it separately; the numeric-widget feeds are not used.
        contrib = {
            "positive": [{"symbol": "NABIL", "points": 1.2}],
            "negative": [],
            # Sector Movers rides on the same contributors feed and must survive.
            "sectors": {"positive": [{"sector": "Banking", "points": 0.8}], "negative": []},
        }

        patches = [
            patch.object(market_insights.cache, "get", return_value=None),
            patch.object(market_insights.cache, "set"),
            patch.object(market_insights, "_after_market_close", return_value=True),
            patch.object(market_insights, "_sector_map", return_value={"NABIL": "Banking"}),
            patch.object(market_insights, "_latest_stock_rows", return_value=(date(2026, 6, 25), eod_rows)),
            patch.object(market_insights, "_nepse_history", return_value=[]),
            patch.object(market_insights, "_greed_history", return_value=[]),
            patch.object(market_insights, "_overview", return_value={}),
            patch.object(market_insights, "_sectors", return_value=[]),
            patch.object(market_insights, "_cached_contributors", return_value=contrib),
            patch.object(market_insights, "fetch_contributors", contributor_boom),
            # The per-scrip / index feeds must NOT be consulted after the close.
            patch.object(market_insights, "fetch_live_rows", boom),
            patch.object(market_insights, "fetch_subindices", boom),
        ]

        with ExitStack() as stack:
            trigger = stack.enter_context(
                patch.object(market_insights, "_maybe_trigger_eod_sync")
            )
            refresh = stack.enter_context(
                patch.object(market_insights, "_refresh_contributors_async")
            )
            for p in patches:
                stack.enter_context(p)
            payload = market_insights.build_payload(force=True)

        self.assertFalse(payload["live"])
        self.assertEqual(payload["source"], "eod")
        self.assertEqual(payload["index_source"], "eod")
        nabil = next(t for t in payload["heatmap"] if t["symbol"] == "NABIL")
        self.assertEqual(nabil["ltp"], 505.0)
        self.assertEqual(nabil["pct"], 1.0)       # (505-500)/500
        # Index Contributors AND Sector Movers stay populated after close.
        self.assertEqual(payload["contributors"]["positive"], contrib["positive"])
        self.assertEqual(payload["contributors"]["sectors"], contrib["sectors"])
        trigger.assert_called_once()
        refresh.assert_called_once()

    def test_after_close_empty_contributor_cache_does_not_block_payload(self):
        eod_rows = [{
            "symbol": "NABIL",
            "security_name": "Nabil Bank",
            "open_price": 500.0,
            "high_price": 510.0,
            "low_price": 495.0,
            "close_price": 505.0,
            "previous_close": 500.0,
            "total_traded_quantity": 1000,
            "total_traded_value": 505000.0,
            "total_trades": 60,
            "market_capitalization": 0.0,
        }]

        def boom(*a, **k):
            raise AssertionError("a live feed was called while building post-close EOD payload")

        patches = [
            patch.object(market_insights.cache, "get", return_value=None),
            patch.object(market_insights.cache, "set"),
            patch.object(market_insights, "_after_market_close", return_value=True),
            patch.object(market_insights, "_sector_map", return_value={"NABIL": "Banking"}),
            patch.object(market_insights, "_latest_stock_rows", return_value=(date(2026, 6, 25), eod_rows)),
            patch.object(market_insights, "_nepse_history", return_value=[]),
            patch.object(market_insights, "_greed_history", return_value=[]),
            patch.object(market_insights, "_overview", return_value={}),
            patch.object(market_insights, "_sectors", return_value=[]),
            patch.object(market_insights, "_cached_contributors", return_value=None),
            patch.object(market_insights, "fetch_contributors", boom),
            patch.object(market_insights, "fetch_live_rows", boom),
            patch.object(market_insights, "fetch_subindices", boom),
        ]

        with ExitStack() as stack:
            trigger = stack.enter_context(
                patch.object(market_insights, "_maybe_trigger_eod_sync")
            )
            refresh = stack.enter_context(
                patch.object(market_insights, "_refresh_contributors_async")
            )
            for p in patches:
                stack.enter_context(p)
            payload = market_insights.build_payload(force=False)

        self.assertFalse(payload["live"])
        self.assertEqual(payload["source"], "eod")
        self.assertEqual(payload["contributors"]["positive"], [])
        self.assertEqual(payload["contributors"]["negative"], [])
        self.assertEqual(payload["contributors"]["sectors"], {"positive": [], "negative": []})
        trigger.assert_called_once()
        refresh.assert_called_once()


@unittest.skipIf(market_insights is None, "Django settings unavailable")
class SubindexComparisonTests(TestCase):
    # Eight consecutive NEPSE sessions (Jun 1–8) anchor the window.
    NEPSE_DATES = [date(2026, 6, d) for d in range(1, 9)]

    @classmethod
    def setUpTestData(cls):
        from core_analysis.models import NepseMarketIndex
        created = datetime(2026, 6, 16, tzinfo=timezone.utc)
        api_id = 0

        def add(sector, bdate, close):
            nonlocal api_id
            api_id += 1
            NepseMarketIndex.objects.create(
                api_id=api_id, business_date=bdate, sector_name=sector,
                open_index=close, high_index=close, low_index=close, close_index=close,
                absolute_change=0, percentage_change=0,
                turnover_values=0, turnover_volume=0, total_transaction=0,
                number_52_weeks_high=close, number_52_weeks_low=close, created_at=created,
            )

        for i, d in enumerate(cls.NEPSE_DATES):
            add("NEPSE INDEX", d, 1000.0 + i * 10)
        # The bucketing is case-insensitive, so store the sub-indices in the DB's
        # mixed case to prove the upper()-keyed match works regardless of casing.
        for i, d in enumerate(cls.NEPSE_DATES):
            add("Banking SubIndex", d, 500.0 + i * 5)
        # A sparser sub-index that only traded on two of the eight sessions.
        add("HydroPower Index", cls.NEPSE_DATES[0], 200.0)
        add("HydroPower Index", cls.NEPSE_DATES[-1], 190.0)

    def test_comparison_aligns_indices_over_window(self):
        from django.core.cache import cache
        cache.clear()  # the function caches per-window; isolate this test
        result = market_insights.subindex_comparison(days=50)

        self.assertEqual(result["start"], "2026-06-01")
        self.assertEqual(result["sessions"], 8)  # all eight NEPSE dates
        labels = {s["label"]: s for s in result["series"]}
        self.assertEqual(set(labels), {"NEPSE", "Banking", "Hydropower"})

        nepse = labels["NEPSE"]["points"]
        self.assertEqual(nepse[0], ["2026-06-01", 1000.0])  # raw closes, oldest first
        self.assertEqual(nepse[-1], ["2026-06-08", 1070.0])
        # Sparser sub-index keeps only the sessions it actually traded.
        self.assertEqual(len(labels["Hydropower"]["points"]), 2)

    def test_comparison_window_limits_to_recent_sessions(self):
        from django.core.cache import cache
        cache.clear()
        result = market_insights.subindex_comparison(days=5)
        # Window = the most recent 5 NEPSE sessions (Jun 4–8).
        self.assertEqual(result["start"], "2026-06-04")
        self.assertEqual(result["sessions"], 5)
        nepse = next(s for s in result["series"] if s["label"] == "NEPSE")["points"]
        self.assertEqual([p[0] for p in nepse], ["2026-06-04", "2026-06-05", "2026-06-06", "2026-06-07", "2026-06-08"])


@unittest.skipIf(nepse_contributors is None, "Django settings unavailable")
class NepseContributorsParserTests(unittest.TestCase):
    def test_sector_view_parser_extracts_top_gainers_and_losers(self):
        html = """
        <div class="pill-stream">
          <div class="stream-title up">▲ Top Gainers</div>
          <div class="pill up"><span>Investment</span><span class="pill-pts">+0.50</span></div>
          <div class="pill up"><span>Finance</span><span class="pill-pts">+0.18</span></div>
        </div>
        <div class="pill-stream">
          <div class="stream-title down">▼ Top Losers</div>
          <div class="pill down"><span>Microfinance</span><span class="pill-pts">-0.94</span></div>
          <div class="pill down"><span>Commercial Banks</span><span class="pill-pts">-0.74</span></div>
        </div>
        """

        movers = nepse_contributors._parse_sector_movers(html)

        self.assertEqual(movers["positive"][0], {"sector": "Investment", "points": 0.5})
        self.assertEqual(movers["positive"][1], {"sector": "Finance", "points": 0.18})
        self.assertEqual(movers["negative"][0], {"sector": "Microfinance", "points": -0.94})
        self.assertEqual(movers["negative"][1], {"sector": "Commercial Banks", "points": -0.74})


@unittest.skipIf(broker_analytics is None, "Django settings unavailable")
class BrokerFlowRadarTests(SimpleTestCase):
    def test_persistence_hhi_uses_populated_side_when_buy_counterparty_is_incomplete(self):
        agg = {
            "dates": ["2026-07-09"],
            "buy": {"NABIL": {1: [10.0, 1_000.0]}},
            "sell": {
                "NABIL": {
                    2: [40.0, 4_000.0],
                    3: [60.0, 6_000.0],
                }
            },
            "sector": {"NABIL": "Commercial Banks"},
        }

        with (
            patch.object(broker_analytics, "_window_aggregate", return_value=agg),
            patch.object(broker_analytics, "get_day_aggregate", return_value=agg),
        ):
            result = broker_analytics.broker_persistence([2])

        row = result["rows"][0]
        self.assertEqual(row["side"], "sell")
        self.assertEqual(row["hhi"], 5200)
        self.assertEqual(row["dominant"], {"broker": 3, "pct": 60.0})

    def test_favorites_summary_covers_full_desk_not_only_top_ten_rows(self):
        buy = {
            f"S{i}": {1: [float(i), float(i * 100)]}
            for i in range(1, 13)
        }
        agg = {"dates": ["2026-07-09"], "buy": buy, "sell": {}, "sector": {}}

        with patch.object(broker_analytics, "_window_aggregate", return_value=agg):
            result = broker_analytics.broker_favorites([1])

        self.assertEqual(len(result["buy"]), 10)
        self.assertEqual(result["summary"]["buy_stocks"], 12)
        self.assertEqual(result["summary"]["stocks_touched"], 12)
        self.assertEqual(result["summary"]["buy_amount"], 7_800.0)

    def test_side_percentages_use_each_sides_own_total_when_counterparty_is_missing(self):
        agg = {
            "dates": ["2026-07-09"],
            "buy": {"NABIL": {1: [100.0, 10_000.0]}},
            "sell": {"NABIL": {2: [40.0, 4_000.0]}},
            "sector": {"NABIL": "Commercial Banks"},
        }

        with patch.object(broker_analytics, "_window_aggregate", return_value=agg):
            stock = broker_analytics.stock_wise("NABIL")
            concentration = broker_analytics.broker_concentration()
            hot = broker_analytics.hotstocks()

        self.assertEqual(stock["buy"][0]["pct"], 100.0)
        self.assertEqual(stock["sell"][0]["pct"], 100.0)
        self.assertEqual(concentration["rows"][0]["buy_sum"], 100.0)
        self.assertEqual(concentration["rows"][0]["sell_sum"], 100.0)
        self.assertEqual(hot["rows"][0]["top_buy"]["pct"], 100.0)
        self.assertEqual(hot["rows"][0]["top_sell"]["pct"], 100.0)

    def test_broker_flow_radar_ranks_and_labels_flow(self):
        agg = {
            "dates": ["2026-06-19"],
            "buy": {
                "AAA": {1: [100, 1000.0], 2: [20, 300.0]},
                "BBB": {1: [10, 200.0], 3: [50, 500.0]},
            },
            "sell": {
                "AAA": {1: [30, 450.0], 2: [50, 800.0]},
                "CCC": {2: [20, 700.0], 3: [10, 150.0]},
            },
            "sector": {},
        }

        with patch.object(broker_analytics, "_window_aggregate", return_value=agg):
            result = broker_analytics.broker_flow_radar()

        rows = result["rows"]
        self.assertTrue(result["ok"])
        self.assertEqual([row["broker"] for row in rows], [2, 1, 3])
        self.assertEqual(rows[0]["total_amount"], 1800.0)
        self.assertEqual(rows[0]["difference"], -1200.0)
        self.assertEqual(rows[0]["matching_amount"], 300.0)
        self.assertEqual(rows[0]["stance"], "Distributing")
        self.assertEqual(rows[1]["stance"], "Accumulating")


class FloorsheetEndpointValidationTests(SimpleTestCase):
    def test_frontend_escapes_upstream_labels_before_html_rendering(self):
        from pathlib import Path

        source = (
            Path(__file__).parent
            / "static"
            / "core_analysis"
            / "js"
            / "floorsheet-brokers.js"
        ).read_text(encoding="utf-8")

        for fragment in (
            "esc(r.broker_name",
            "esc(r.symbol)",
            "esc(r.sector)",
            "esc(cell.getAttribute(\"data-sym\"))",
        ):
            self.assertIn(fragment, source)

    def test_valid_dashboard_query_is_normalized_before_service_call(self):
        with patch(
            "core_analysis.broker_views.ba.broker_favorites",
            return_value={"ok": True, "buy": [], "sell": []},
        ) as builder:
            response = self.client.get(
                reverse("broker_favorites_api"),
                {
                    "brokers": "2, 2, 5",
                    "view": "turnover",
                    "range": "custom",
                    "start_date": "2026-07-01",
                    "end_date": "2026-07-09",
                },
            )

        self.assertEqual(response.status_code, 200)
        builder.assert_called_once_with(
            [2, 5],
            view="turnover",
            range_key="custom",
            start="2026-07-01",
            end="2026-07-09",
        )

    def test_dashboard_apis_reject_malformed_or_runaway_queries(self):
        too_many = ",".join(str(i) for i in range(1, 202))
        cases = [
            ("broker_favorites_api", {"brokers": "1,not-a-broker"}),
            ("broker_favorites_api", {"brokers": too_many}),
            ("stock_wise_api", {"symbol": "<script>"}),
            ("hotstocks_api", {"view": "money"}),
            ("net_holding_api", {"brokers": "1", "exclude_mf": "perhaps"}),
            ("broker_flow_radar_api", {"range": "yesterday"}),
            ("broker_flow_radar_api", {"range": "custom", "start_date": "2026-07-01"}),
            (
                "broker_flow_radar_api",
                {"range": "custom", "start_date": "2026-07-10", "end_date": "2026-07-01"},
            ),
            (
                "broker_flow_radar_api",
                {"range": "custom", "start_date": "2025-01-01", "end_date": "2026-07-01"},
            ),
            ("broker_persistence_api", {"brokers": "1", "lookback": "5y"}),
        ]

        for route_name, params in cases:
            with self.subTest(route=route_name, params=params):
                response = self.client.get(reverse(route_name), params)
                self.assertEqual(response.status_code, 400)
                self.assertFalse(response.json()["ok"])

    def test_raw_floorsheet_api_requires_a_bounded_indexed_slice(self):
        cases = [
            {},
            {"date_from": "2026-07-01"},
            {"date_from": "2026-07-10", "date_to": "2026-07-01"},
            {"date_from": "2026-05-01", "date_to": "2026-07-01"},
            {"symbol": "<script>"},
        ]

        for params in cases:
            with self.subTest(params=params):
                response = self.client.get("/api/v1/floorsheet/", params)
                self.assertEqual(response.status_code, 400)


class FloorsheetSyncSecurityTests(SimpleTestCase):
    def test_valid_sync_row_is_normalized_without_database_write_in_dry_run(self):
        from io import StringIO

        from core_analysis.management.commands.sync_floorsheet import Command

        payload = {
            "results": [
                {
                    "id": "1",
                    "calculation_date": "2026-07-09",
                    "stock_symbol": " nabil ",
                    "contract_no": "C-1",
                    "sector": "Commercial Banks",
                    "buyer": "1",
                    "seller": "2",
                    "quantity": "1,000",
                    "rate": "500.25",
                    "amount": "500,250",
                }
            ]
        }
        with patch(
            "core_analysis.management.commands.sync_floorsheet._fetch_page",
            return_value=(payload, None),
        ), patch(
            "core_analysis.management.commands.sync_floorsheet.NepseFloorsheet"
        ) as floorsheet_model:
            command = Command()
            command.stdout = StringIO()
            processed, skipped = command._sync_one_day(
                Mock(),
                "https://trusted.example",
                date(2026, 7, 9),
                page_size=100,
                batch_size=100,
                max_pages=None,
                dry_run=True,
            )

        self.assertEqual((processed, skipped), (1, 0))
        floorsheet_model.assert_called_once()
        kwargs = floorsheet_model.call_args.kwargs
        self.assertEqual(kwargs["stock_symbol"], "NABIL")
        self.assertEqual(kwargs["quantity"], 1000)

    def test_pagination_refuses_cross_origin_next_url(self):
        from django.core.management.base import CommandError

        from core_analysis.management.commands.sync_floorsheet import _fetch_page

        response = Mock()
        response.status_code = 200
        response.url = "https://trusted.example/api/floorsheet/?page=1"
        response.headers = {}
        response.json.return_value = {
            "results": [],
            "next": "https://attacker.example/steal-credentials",
        }
        session = Mock()
        session.get.return_value = response

        with self.assertRaisesRegex(CommandError, "cross-origin"):
            _fetch_page(
                session,
                response.url,
                allowed_origin="https://trusted.example",
            )

        session.get.assert_called_once_with(
            response.url, timeout=30, allow_redirects=False
        )

    def test_numeric_cleaners_reject_fractional_nonfinite_and_out_of_range_values(self):
        from decimal import Decimal

        from core_analysis.management.commands.sync_floorsheet import (
            _clean_decimal,
            _clean_int,
        )

        self.assertIsNone(_clean_int("1.5"))
        self.assertIsNone(_clean_int("Infinity"))
        self.assertIsNone(_clean_int("0", minimum=1))
        self.assertEqual(_clean_int("1,234", minimum=1), 1234)
        self.assertIsNone(
            _clean_decimal("NaN", default=None, minimum=Decimal("0"))
        )
        self.assertIsNone(
            _clean_decimal("-1", default=None, minimum=Decimal("0"))
        )

    def test_sync_rejects_wrong_day_rows_and_pagination_loops(self):
        from django.core.management.base import CommandError

        from core_analysis.management.commands.sync_floorsheet import Command

        day = date(2026, 7, 9)
        payload = {
            "results": [
                {
                    "id": 1,
                    "calculation_date": "2026-07-08",
                    "stock_symbol": "NABIL",
                    "buyer": 1,
                    "seller": 2,
                    "quantity": 10,
                }
            ]
        }
        same_url = "https://trusted.example/api/floorsheet/?page=1"
        with patch(
            "core_analysis.management.commands.sync_floorsheet._fetch_page",
            return_value=(payload, same_url),
        ), patch(
            "core_analysis.management.commands.sync_floorsheet.NepseFloorsheet"
        ) as floorsheet_model:
            with self.assertRaisesRegex(CommandError, "pagination loop"):
                Command()._sync_one_day(
                    Mock(),
                    "https://trusted.example",
                    day,
                    page_size=100,
                    batch_size=100,
                    max_pages=None,
                    dry_run=True,
                )
        floorsheet_model.assert_not_called()


class FloorsheetSyncViewTests(TestCase):
    def setUp(self):
        self.staff = get_user_model().objects.create_user(
            username="floorsheet-admin", password="StrongPass123!", is_staff=True
        )
        self.client.force_login(self.staff)

    def test_web_sync_rejects_reversed_and_oversized_ranges_before_running_command(self):
        cases = [
            {"from_date": "2026-07-10", "to_date": "2026-07-01"},
            {"from_date": "2025-01-01", "to_date": "2026-07-01"},
        ]

        with patch("core_analysis.views.call_command") as call_command:
            for payload in cases:
                with self.subTest(payload=payload):
                    response = self.client.post(reverse("trigger_floorsheet_sync"), payload)
                    self.assertRedirects(
                        response,
                        reverse("crud_dashboard"),
                        fetch_redirect_response=False,
                    )

        call_command.assert_not_called()


def make_price_frame(close_values):
    dates = pd.date_range("2025-01-01", periods=len(close_values), freq="D")
    close = pd.Series(close_values, dtype=float)
    return pd.DataFrame(
        {
            "business_date": dates,
            "open_price_adj": close,
            "high_price_adj": close + 1.0,
            "low_price_adj": close - 1.0,
            "close_price_adj": close,
            "volume": 1000.0,
        }
    )


class IMMScoringTests(unittest.TestCase):
    def test_generate_sell_signal_tolerates_missing_atr_stop(self):
        df = pd.DataFrame(
            {
                "close_price_adj": [100.0, 99.0],
                "SMA_20": [95.0, 100.0],
                "RSI_14": [60.0, 60.0],
                "MACD_line": [1.0, 1.0],
                "MACD_signal": [0.0, 0.0],
                "supertrend_bullish": [True, False],
            }
        )

        sell_signal = IMM.generate_sell_signal(df)

        self.assertFalse(bool(sell_signal.iloc[0]))
        self.assertTrue(bool(sell_signal.iloc[1]))

    def test_position_builder_sells_on_atr_stop_and_resets_after_exit(self):
        df = pd.DataFrame(
            {
                "close_price_adj": [100.0, 105.0, 103.0, 99.0, 101.0, 102.0],
                "ATR": [2.0] * 6,
                "SMA_20": [90.0, 90.0, 90.0, 100.0, 90.0, 90.0],
                "RSI_14": [60.0] * 6,
                "MACD_line": [1.0] * 6,
                "MACD_signal": [0.0] * 6,
                "supertrend_bullish": [True, True, True, False, True, True],
            }
        )
        raw_buy = pd.Series([True, True, False, False, True, False])

        buy_signal, sell_signal, stop = IMM._build_position_signals_with_atr_stop(
            df,
            raw_buy,
            multiplier=2.0,
        )

        self.assertEqual(buy_signal[buy_signal].index.tolist(), [0, 4])
        self.assertEqual(sell_signal[sell_signal].index.tolist(), [3])
        self.assertEqual(stop.iloc[1], 101.0)
        self.assertEqual(stop.iloc[3], 101.0)
        self.assertEqual(stop.iloc[4], 97.0)

    def test_run_imm_scoring_system_returns_stop_aware_sell_event(self):
        stock_close = np.arange(100.0, 310.0)
        stock_close[205:] = stock_close[205:] - 80.0
        nepse_close = np.arange(1000.0, 1210.0)

        stock_df = make_price_frame(stock_close)
        nepse_df = make_price_frame(nepse_close)

        with patch.object(IMM, "ta", FakeTechnicalAnalysis):
            metrics, output = IMM.run_imm_scoring_system(stock_df, nepse_df)

        self.assertNotIn("error", metrics)
        self.assertEqual(metrics["buy_count"], 1)
        self.assertEqual(metrics["sell_count"], 1)
        self.assertTrue(bool(output.loc[200, "buy_signal"]))
        self.assertTrue(bool(output.loc[205, "sell_signal"]))
        self.assertTrue(pd.isna(output.loc[206, "atr_trailing_stop"]))


class MSVStrategyTests(unittest.TestCase):
    @unittest.skipIf(msv_strategy.ta is None, "pandas_ta unavailable")
    def test_msv_vwap_works_with_standard_business_date_column(self):
        close_values = np.arange(100.0, 180.0)
        stock_df = make_price_frame(close_values)

        metrics, trades, output = msv_strategy.run_msv_long_only_simulation(stock_df)

        self.assertNotIn("error", metrics)
        self.assertEqual(len(output), len(stock_df))
        self.assertFalse(output["VWAP"].isna().all())


class SupportResistanceTests(unittest.TestCase):
    def _sample_support_resistance_frame(self):
        return pd.DataFrame(
            {
                "business_date": pd.to_datetime(["2025-06-01", "2025-06-02"]),
                "high_price_adj": [29.20, 28.64],
                "low_price_adj": [28.70, 28.01],
                "close_price_adj": [28.99, 28.17],
                "price_source": ["Adjusted", "Adjusted"],
            }
        )

    def test_support_resistance_pivots_match_standard_formula(self):
        df = self._sample_support_resistance_frame()

        metrics, rows = run_support_resistance_analysis(df, symbol="KGC")
        levels_by_label = {
            label: row["price"]
            for row in rows
            for label in row["level_names"]
        }

        self.assertNotIn("error", metrics)
        self.assertEqual(metrics["pivot"], 28.27)
        self.assertEqual(levels_by_label["Pivot Point 1st Resistance Point"], 28.54)
        self.assertEqual(levels_by_label["Pivot Point 2nd Level Resistance"], 28.90)
        self.assertEqual(levels_by_label["Pivot Point 3rd Level Resistance"], 29.17)
        self.assertEqual(levels_by_label["Pivot Point 1st Support Point"], 27.91)
        self.assertEqual(levels_by_label["Pivot Point 2nd Support Point"], 27.64)
        self.assertEqual(levels_by_label["Pivot Point 3rd Support Point"], 27.28)
        self.assertEqual(len(metrics["simple_level_rows"]), 8)
        self.assertEqual(metrics["simple_level_rows"][-1]["basis"], "Pivot S1/S2/R1/R2")

    def test_support_resistance_honors_selected_level_families(self):
        df = self._sample_support_resistance_frame()

        metrics, rows = run_support_resistance_analysis(
            df,
            symbol="KGC",
            enabled_families=["hlc"],
        )
        labels = {
            label
            for row in rows
            for label in row["level_names"]
        }

        self.assertEqual(metrics["enabled_families"], ["hlc"])
        self.assertIn("High", labels)
        self.assertIn("Low", labels)
        self.assertNotIn("Pivot Point", labels)

    def test_nearest_headline_levels_use_confluence(self):
        df = pd.DataFrame(
            {
                "business_date": pd.to_datetime(["2025-06-01", "2025-06-02", "2025-06-03"]),
                "open_price_adj": [90.0, 118.0, 101.0],
                "high_price_adj": [120.0, 116.0, 105.0],
                "low_price_adj": [80.0, 96.0, 95.0],
                "close_price_adj": [118.0, 102.0, 100.0],
                "volume": [1000, 1200, 900],
            }
        )

        metrics, _ = run_support_resistance_analysis(df, symbol="BB")

        # Headline cards now use the confluence engine (real reaction levels with
        # >= 2 agreeing methods), not the raw Bollinger band.
        self.assertEqual(metrics["nearest_level_basis"], "Confluence")
        self.assertEqual(metrics["nearest_resistance"]["basis"], "Confluence")
        self.assertEqual(metrics["nearest_support"]["basis"], "Confluence")
        self.assertGreaterEqual(metrics["nearest_resistance"]["method_count"], 2)
        self.assertGreaterEqual(metrics["nearest_support"]["method_count"], 2)
        self.assertGreaterEqual(metrics["nearest_resistance"]["price"], metrics["latest_price"])
        self.assertLessEqual(metrics["nearest_support"]["price"], metrics["latest_price"])
        # Bollinger bands are still computed and exposed as a reference.
        self.assertIn("middle_band", metrics["bollinger_bands"])


class AdvancedMarketStructureTests(unittest.TestCase):
    def test_advanced_market_structure_runs_on_dummy_ohlcv(self):
        df = generate_dummy_ohlcv(rows=180, seed=7)

        metrics = run_advanced_market_structure_analysis(df, symbol="DUMMY", fractal_window=5)

        self.assertNotIn("error", metrics)
        self.assertEqual(metrics["symbol"], "DUMMY")
        self.assertGreater(metrics["pivot_count"], 0)
        self.assertIn("density_zones", metrics)
        self.assertIn("profile", metrics)
        self.assertIn("chart", metrics)
        self.assertGreater(len(metrics["chart"]["candles"]), 0)

    def test_advanced_market_structure_rejects_short_data(self):
        df = generate_dummy_ohlcv(rows=8, seed=7)

        metrics = run_advanced_market_structure_analysis(df, symbol="SHORT", fractal_window=5)

        self.assertIn("error", metrics)


class InstitutionalAnalysisTests(unittest.TestCase):
    def test_institutional_analysis_returns_exact_framework_table_contract(self):
        df = generate_dummy_ohlcv(rows=160, seed=11)
        support_metrics, _ = run_support_resistance_analysis(df)
        advanced_metrics = run_advanced_market_structure_analysis(df, symbol="DUMMY", fractal_window=5)

        rows = build_institutional_analysis_rows(support_metrics, advanced_metrics)

        # 9 framework systems plus the appended "Institutional Consensus" row.
        self.assertEqual(len(rows), 10)
        self.assertEqual(rows[-1]["system"], "Institutional Consensus")
        # Keys are lowercase snake_case to match the template ({{ row.system }}).
        for row in rows:
            self.assertIn("system", row)
            self.assertIn("institutional_logic", row)
            self.assertIn("price_sentiment", row)
            self.assertIn("status", row)
            self.assertTrue(row["system"])
            self.assertTrue(row["institutional_logic"])
            self.assertTrue(row["price_sentiment"])
            self.assertTrue(row["status"])


if __name__ == "__main__":
    unittest.main()
