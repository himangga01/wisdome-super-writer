import hashlib
import unicodedata
from collections.abc import Mapping

import rfc8785
from django.db import migrations, models

from apps.topics import models as topics_models


_LEGACY_SCHEMA = "source-definition-snapshot-legacy-v1"
_V2_SCHEMA = "source-definition-snapshot-v2"


def _normalize_json(value):
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if isinstance(value, Mapping):
        normalized = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise RuntimeError(
                    "Source snapshot config keys must be strings."
                )
            normalized_key = unicodedata.normalize("NFC", key)
            if normalized_key in normalized:
                raise RuntimeError(
                    "Source snapshot config keys collide after NFC normalization."
                )
            normalized[normalized_key] = _normalize_json(item)
        return normalized
    if isinstance(value, (list, tuple)):
        return [_normalize_json(item) for item in value]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    raise RuntimeError(
        f"Unsupported source snapshot config value: {type(value).__name__}"
    )


def _canonical_hash(value):
    encoded = rfc8785.dumps(_normalize_json(value))
    return hashlib.sha256(encoded).hexdigest()


def freeze_legacy_execution_material(apps, schema_editor):
    SourceDefinitionSnapshot = apps.get_model(
        "topics",
        "SourceDefinitionSnapshot",
    )
    for snapshot in SourceDefinitionSnapshot.objects.all().iterator():
        config = snapshot.config
        if not isinstance(config, dict):
            raise RuntimeError(
                f"Source snapshot {snapshot.pk} has non-object config."
            )
        schema_version = config.get("schemaVersion")
        if schema_version == _LEGACY_SCHEMA:
            legacy_config = config.get("legacyConfig")
            if not isinstance(legacy_config, dict):
                raise RuntimeError(
                    f"Legacy source snapshot {snapshot.pk} has no original config."
                )
            if _canonical_hash(legacy_config) != snapshot.config_hash:
                raise RuntimeError(
                    f"Legacy source snapshot {snapshot.pk} does not match its historical hash."
                )
            SourceDefinitionSnapshot.objects.filter(
                pk=snapshot.pk
            ).update(
                config=legacy_config,
                frozen_config=config,
                frozen_config_hash=_canonical_hash(config),
            )
            continue
        if (
            schema_version == _V2_SCHEMA
            and _canonical_hash(config) == snapshot.config_hash
        ):
            SourceDefinitionSnapshot.objects.filter(
                pk=snapshot.pk
            ).update(
                frozen_config=config,
                frozen_config_hash=snapshot.config_hash,
            )
            continue
        raise RuntimeError(
            f"Source snapshot {snapshot.pk} has unverifiable execution material."
        )


def restore_wrapped_legacy_config(apps, schema_editor):
    SourceDefinitionSnapshot = apps.get_model(
        "topics",
        "SourceDefinitionSnapshot",
    )
    for snapshot in SourceDefinitionSnapshot.objects.all().iterator():
        frozen_config = snapshot.frozen_config
        if (
            isinstance(frozen_config, dict)
            and frozen_config.get("schemaVersion") == _LEGACY_SCHEMA
        ):
            SourceDefinitionSnapshot.objects.filter(
                pk=snapshot.pk
            ).update(config=frozen_config)


class Migration(migrations.Migration):
    dependencies = [
        ("topics", "0002_source_registry_contract"),
    ]

    operations = [
        migrations.AddField(
            model_name="sourcedefinitionsnapshot",
            name="frozen_config",
            field=models.JSONField(null=True),
        ),
        migrations.AddField(
            model_name="sourcedefinitionsnapshot",
            name="frozen_config_hash",
            field=models.CharField(
                max_length=64,
                null=True,
                validators=[topics_models.sha256_validator],
            ),
        ),
        migrations.RunPython(
            freeze_legacy_execution_material,
            restore_wrapped_legacy_config,
        ),
        migrations.AlterField(
            model_name="sourcedefinitionsnapshot",
            name="frozen_config",
            field=models.JSONField(),
        ),
        migrations.AlterField(
            model_name="sourcedefinitionsnapshot",
            name="frozen_config_hash",
            field=models.CharField(
                max_length=64,
                validators=[topics_models.sha256_validator],
            ),
        ),
    ]
