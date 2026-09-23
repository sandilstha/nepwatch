# Repoint NepseFloorsheet from the old `nepse_floorsheet` table to the
# `floorsheet_raw` table, which was created directly in the database.
#
# Because `floorsheet_raw` already exists physically (and the model now matches
# it exactly), this migration only rewrites Django's migration *state* — it runs
# no DDL. The old `nepse_floorsheet` table is left untouched; drop it by hand if
# it is no longer needed.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core_analysis', '0003_nepsefloorsheet'),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[],
            state_operations=[
                migrations.DeleteModel(name='NepseFloorsheet'),
                migrations.CreateModel(
                    name='NepseFloorsheet',
                    fields=[
                        ('id', models.BigIntegerField(help_text="Maps to JSON 'id'", primary_key=True, serialize=False)),
                        ('contract_no', models.CharField(blank=True, db_index=True, help_text="Maps to JSON 'contract_no'", max_length=255, null=True)),
                        ('stock_symbol', models.CharField(db_index=True, max_length=50)),
                        ('buyer', models.IntegerField(blank=True, db_index=True, null=True)),
                        ('seller', models.IntegerField(blank=True, db_index=True, null=True)),
                        ('quantity', models.IntegerField(blank=True, null=True)),
                        ('rate', models.DecimalField(blank=True, decimal_places=2, max_digits=10, null=True)),
                        ('amount', models.DecimalField(blank=True, decimal_places=2, max_digits=15, null=True)),
                        ('sector', models.CharField(blank=True, db_index=True, max_length=100, null=True)),
                        ('business_date', models.DateField(db_column='calculation_date', db_index=True, help_text="Maps to JSON 'calculation_date'")),
                        ('trade_time', models.TimeField(blank=True, db_column='time', help_text="Maps to JSON 'time'", null=True)),
                    ],
                    options={
                        'db_table': 'floorsheet_raw',
                        'ordering': ['-business_date', 'stock_symbol'],
                    },
                ),
                migrations.AddIndex(
                    model_name='nepsefloorsheet',
                    index=models.Index(fields=['stock_symbol', 'business_date'], name='floorsheet__stock_s_90eacb_idx'),
                ),
                migrations.AddIndex(
                    model_name='nepsefloorsheet',
                    index=models.Index(fields=['business_date', 'buyer'], name='floorsheet__calcula_a5f435_idx'),
                ),
                migrations.AddIndex(
                    model_name='nepsefloorsheet',
                    index=models.Index(fields=['business_date', 'seller'], name='floorsheet__calcula_175a99_idx'),
                ),
                migrations.AddIndex(
                    model_name='nepsefloorsheet',
                    index=models.Index(fields=['business_date', 'sector'], name='floorsheet__calcula_021ea6_idx'),
                ),
            ],
        ),
    ]
