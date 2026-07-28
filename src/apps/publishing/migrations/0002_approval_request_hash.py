from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("publishing", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="approval",
            name="request_hash",
            field=models.CharField(blank=True, max_length=64, null=True),
        ),
    ]
