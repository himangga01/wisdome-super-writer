from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="adminaccount",
            name="reauth_failure_count",
            field=models.PositiveSmallIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="adminaccount",
            name="reauth_failure_window_started_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="adminaccount",
            name="reauth_locked_until",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
