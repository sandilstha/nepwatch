# Generated manually for Django 6.0.5 on 2026-07-13

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core_analysis", "0011_portfolio_multi_management"),
    ]

    operations = [
        migrations.CreateModel(
            name="BrokerLedgerImport",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("source_name", models.CharField(max_length=255)),
                ("file_sha256", models.CharField(db_index=True, max_length=64)),
                ("account_name", models.CharField(blank=True, default="", max_length=160)),
                ("account_code", models.CharField(blank=True, default="", max_length=80)),
                ("report_from_ad", models.DateField(blank=True, null=True)),
                ("report_to_ad", models.DateField(blank=True, null=True)),
                ("source_fiscal_year", models.CharField(blank=True, default="", max_length=20)),
                ("imported_rows", models.PositiveIntegerField(default=0)),
                ("duplicate_rows", models.PositiveIntegerField(default=0)),
                ("warning_count", models.PositiveIntegerField(default=0)),
                ("imported_at", models.DateTimeField(auto_now_add=True)),
                ("portfolio", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="ledger_imports", to="core_analysis.portfolio")),
            ],
            options={
                "db_table": "portfolio_ledger_import",
                "ordering": ["-imported_at", "-id"],
                "constraints": [models.UniqueConstraint(fields=("portfolio", "file_sha256"), name="uniq_pf_ledger_file")],
            },
        ),
        migrations.CreateModel(
            name="BrokerLedgerTransaction",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("source_row_no", models.PositiveIntegerField(blank=True, null=True)),
                ("date_ad", models.DateField(blank=True, db_index=True, null=True)),
                ("date_bs", models.CharField(blank=True, default="", max_length=10)),
                ("fiscal_year", models.CharField(blank=True, db_index=True, default="", max_length=10)),
                ("voucher_no", models.CharField(blank=True, default="", max_length=80)),
                ("transaction_type", models.CharField(choices=[("opening", "Opening balance"), ("deposit", "Deposit / receipt"), ("buy", "Purchase bill"), ("sell", "Sale bill"), ("payment", "Payment / withdrawal"), ("dividend", "Dividend"), ("charge", "Charge"), ("tax", "Tax"), ("adjustment", "Adjustment"), ("other", "Other")], db_index=True, default="other", max_length=16)),
                ("particulars", models.TextField(blank=True, default="")),
                ("reference_no", models.CharField(blank=True, default="", max_length=100)),
                ("branch", models.CharField(blank=True, default="", max_length=40)),
                ("debit", models.DecimalField(decimal_places=2, default=0, max_digits=18)),
                ("credit", models.DecimalField(decimal_places=2, default=0, max_digits=18)),
                ("balance", models.DecimalField(decimal_places=2, default=0, max_digits=18)),
                ("balance_side", models.CharField(blank=True, choices=[("CR", "Credit"), ("DR", "Debit")], default="", max_length=2)),
                ("derived_deductions", models.DecimalField(decimal_places=2, default=0, max_digits=18)),
                ("fingerprint", models.CharField(max_length=64)),
                ("sequence_gap", models.BooleanField(default=False)),
                ("balance_mismatch", models.BooleanField(default=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("import_batch", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="transactions", to="core_analysis.brokerledgerimport")),
                ("portfolio", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="ledger_transactions", to="core_analysis.portfolio")),
            ],
            options={
                "db_table": "portfolio_ledger_transaction",
                "ordering": ["date_ad", "source_row_no", "id"],
                "indexes": [models.Index(fields=["portfolio", "date_ad"], name="pf_ledger_date_idx"), models.Index(fields=["portfolio", "fiscal_year", "transaction_type"], name="pf_ledger_fy_type_idx")],
                "constraints": [
                    models.UniqueConstraint(fields=("portfolio", "fingerprint"), name="uniq_pf_ledger_tx"),
                    models.CheckConstraint(condition=models.Q(("debit__gte", 0)), name="ledger_debit_nonneg"),
                    models.CheckConstraint(condition=models.Q(("credit__gte", 0)), name="ledger_credit_nonneg"),
                    models.CheckConstraint(condition=models.Q(("derived_deductions__gte", 0)), name="ledger_deduct_nonneg"),
                ],
            },
        ),
        migrations.CreateModel(
            name="BrokerTrade",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("symbol", models.CharField(db_index=True, max_length=20)),
                ("side", models.CharField(choices=[("buy", "Buy"), ("sell", "Sell")], max_length=4)),
                ("quantity", models.DecimalField(decimal_places=2, max_digits=18)),
                ("price", models.DecimalField(decimal_places=4, max_digits=14)),
                ("gross_amount", models.DecimalField(decimal_places=2, max_digits=18)),
                ("allocated_deductions", models.DecimalField(decimal_places=2, default=0, max_digits=18)),
                ("net_amount", models.DecimalField(decimal_places=2, max_digits=18)),
                ("transaction", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="trades", to="core_analysis.brokerledgertransaction")),
            ],
            options={
                "db_table": "portfolio_broker_trade",
                "ordering": ["transaction_id", "id"],
                "constraints": [
                    models.CheckConstraint(condition=models.Q(("quantity__gt", 0)), name="broker_trade_qty_pos"),
                    models.CheckConstraint(condition=models.Q(("price__gte", 0)), name="broker_trade_price_nonneg"),
                    models.CheckConstraint(condition=models.Q(("gross_amount__gte", 0)), name="broker_trade_gross_nonneg"),
                    models.CheckConstraint(condition=models.Q(("allocated_deductions__gte", 0)), name="broker_trade_deduct_nonneg"),
                ],
            },
        ),
    ]
