from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("collection", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="runstep",
            name="fanout_completed_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
