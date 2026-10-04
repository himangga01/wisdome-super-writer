from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("collection", "0011_source_item_retention_tombstone")]

    operations = [
        migrations.AlterField(
            model_name="sourceitem",
            name="attachments",
            field=models.JSONField(blank=True, default=list),
        ),
    ]
