from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("topics", "0003_freeze_legacy_source_execution_material"),
    ]

    operations = [
        migrations.AlterField(
            model_name="sourcedefinition",
            name="authority_tier",
            field=models.CharField(
                choices=[
                    ("primary_official", "공식 1차 출처"),
                    ("primary_regulatory", "규제기관 1차 출처"),
                    ("primary_corporate", "기업 1차 출처"),
                    ("trusted_industry", "신뢰 산업단체 출처"),
                    ("trusted_secondary", "신뢰 보조 출처"),
                    ("discovery_only", "발견 전용"),
                ],
                max_length=32,
            ),
        ),
    ]
