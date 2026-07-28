import hashlib
import json

from django.core.validators import RegexValidator
from django.db import migrations, models


def _stable_fingerprint(document) -> str:
    material = {
        "schema": "document-input-v1",
        "run_source_item_id": str(document.run_source_item_id),
        "input_kind": document.input_kind,
        "input_checksum": document.input_checksum,
    }
    encoded = json.dumps(
        material,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _canonical_priority(document):
    if document.document_complete and document.state == "succeeded":
        state_priority = 0
    elif document.state == "succeeded":
        state_priority = 1
    elif document.state == "low_confidence":
        state_priority = 2
    elif document.state == "failed" or document.finished_at is not None:
        state_priority = 3
    else:
        state_priority = 4
    return (
        state_priority,
        document.created_at,
        str(document.id),
    )


def _assign_canonical_fingerprint(DocumentExtraction, db_alias, documents):
    canonical = min(documents, key=_canonical_priority)
    DocumentExtraction.objects.using(db_alias).filter(
        pk=canonical.pk
    ).update(
        input_fingerprint=_stable_fingerprint(canonical)
    )


def backfill_input_fingerprints(apps, schema_editor):
    DocumentExtraction = apps.get_model("evidence", "DocumentExtraction")
    db_alias = schema_editor.connection.alias
    documents = DocumentExtraction.objects.using(db_alias).order_by(
        "run_source_item_id",
        "input_kind",
        "input_checksum",
        "created_at",
        "id",
    )
    current_identity = None
    identity_documents = []
    for document in documents.iterator():
        identity = (
            str(document.run_source_item_id),
            document.input_kind,
            document.input_checksum,
        )
        if current_identity is not None and identity != current_identity:
            _assign_canonical_fingerprint(
                DocumentExtraction,
                db_alias,
                identity_documents,
            )
            identity_documents = []
        current_identity = identity
        identity_documents.append(document)
    if identity_documents:
        _assign_canonical_fingerprint(
            DocumentExtraction,
            db_alias,
            identity_documents,
        )


def clear_input_fingerprints(apps, schema_editor):
    DocumentExtraction = apps.get_model("evidence", "DocumentExtraction")
    DocumentExtraction.objects.using(
        schema_editor.connection.alias
    ).update(input_fingerprint=None)


class Migration(migrations.Migration):
    dependencies = [
        ("evidence", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="documentextraction",
            name="input_fingerprint",
            field=models.CharField(
                blank=True,
                max_length=64,
                null=True,
                unique=True,
            ),
        ),
        migrations.RunPython(
            backfill_input_fingerprints,
            clear_input_fingerprints,
        ),
        migrations.AlterField(
            model_name="documentextraction",
            name="input_fingerprint",
            field=models.CharField(
                blank=True,
                max_length=64,
                null=True,
                unique=True,
                validators=[
                    RegexValidator(
                        "^[a-f0-9]{64}$",
                        "Expected a lowercase SHA-256 digest",
                    )
                ],
            ),
        ),
    ]
