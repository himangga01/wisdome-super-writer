from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("publishing", "0002_approval_request_hash"),
    ]

    operations = [
        migrations.AddField(
            model_name="publicationattempt",
            name="reconcile_attempt_no",
            field=models.PositiveIntegerField(default=0),
        ),
    ]
