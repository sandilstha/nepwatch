# Generated manually for Django 6.0.5 on 2026-07-03

from django.db import migrations, models


def seed_default_portfolios(apps, schema_editor):
    Portfolio = apps.get_model("core_analysis", "Portfolio")
    user_ids = (
        Portfolio.objects.values_list("user_id", flat=True)
        .distinct()
        .order_by("user_id")
    )
    for user_id in user_ids:
        default = (
            Portfolio.objects.filter(user_id=user_id, is_archived=False)
            .order_by("-updated_at", "id")
            .first()
        )
        if default:
            Portfolio.objects.filter(user_id=user_id).update(is_default=False)
            Portfolio.objects.filter(pk=default.pk).update(is_default=True)


def clear_default_portfolios(apps, schema_editor):
    Portfolio = apps.get_model("core_analysis", "Portfolio")
    Portfolio.objects.update(is_default=False)


class Migration(migrations.Migration):

    dependencies = [
        ("core_analysis", "0010_pagevisit"),
    ]

    operations = [
        migrations.AddField(
            model_name="portfolio",
            name="is_archived",
            field=models.BooleanField(db_index=True, default=False),
        ),
        migrations.AddField(
            model_name="portfolio",
            name="is_default",
            field=models.BooleanField(db_index=True, default=False),
        ),
        migrations.AddField(
            model_name="portfolio",
            name="portfolio_type",
            field=models.CharField(
                choices=[
                    ("personal", "Personal"),
                    ("spouse", "Spouse"),
                    ("parents", "Parents"),
                    ("children", "Children"),
                    ("joint", "Joint"),
                    ("client", "Client"),
                    ("custom", "Custom"),
                ],
                db_index=True,
                default="personal",
                max_length=20,
            ),
        ),
        migrations.AddIndex(
            model_name="portfolio",
            index=models.Index(
                fields=["user", "is_archived", "is_default"],
                name="portfolio_p_user_id_4f7677_idx",
            ),
        ),
        migrations.RunPython(seed_default_portfolios, clear_default_portfolios),
        migrations.AlterModelOptions(
            name="portfolio",
            options={"ordering": ["is_archived", "-is_default", "-updated_at"]},
        ),
    ]
