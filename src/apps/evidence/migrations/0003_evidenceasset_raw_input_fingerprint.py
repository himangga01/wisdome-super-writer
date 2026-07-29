import hashlib
import json

from django.core.validators import RegexValidator
from django.db import migrations, models
from django.utils import timezone


def _is_sha256(value):
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _stable_fingerprint(evidence) -> str:
    checksum = evidence.checksum or ""
    if evidence.kind == "attachment":
        input_kind = "attachment"
        input_hash = checksum
    elif evidence.kind == "text":
        input_kind = "source_record"
        input_hash = evidence.evidence_content_hash
    else:
        raise ValueError("unsupported legacy raw evidence kind")
    material = {
        "schema": "raw-input-v1",
        "run_source_item_id": str(evidence.origin_run_source_item_id),
        "source_item_id": str(evidence.source_item_id),
        "input_kind": input_kind,
        "input_hash": input_hash,
    }
    encoded = json.dumps(
        material,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _quarantine(EvidenceAsset, db_alias, evidence_id):
    EvidenceAsset.objects.using(db_alias).filter(pk=evidence_id).update(
        publishable=False,
        manual_review_required=True,
        review_state="manual_required",
    )


def _canonical_raw_priority(
    evidence,
    *,
    canonical_document_input_ids,
    completed_generic_input_ids,
):
    if evidence.id in canonical_document_input_ids:
        lineage_priority = 0
    elif evidence.id in completed_generic_input_ids:
        lineage_priority = 1
    else:
        lineage_priority = 2
    return (
        lineage_priority,
        0 if evidence.publishable else 1,
        evidence.created_at,
        str(evidence.id),
    )


def backfill_raw_input_fingerprints(apps, schema_editor):
    EvidenceAsset = apps.get_model("evidence", "EvidenceAsset")
    DocumentExtraction = apps.get_model("evidence", "DocumentExtraction")
    GenericExtractionAttempt = apps.get_model(
        "evidence",
        "GenericExtractionAttempt",
    )
    db_alias = schema_editor.connection.alias
    canonical_document_input_ids = set(
        DocumentExtraction.objects.using(db_alias)
        .filter(
            input_fingerprint__isnull=False,
            input_asset_id__isnull=False,
        )
        .values_list("input_asset_id", flat=True)
    )
    completed_generic_input_ids = set(
        GenericExtractionAttempt.objects.using(db_alias)
        .filter(
            state__in=("succeeded", "low_confidence"),
            input_asset_id__isnull=False,
        )
        .values_list("input_asset_id", flat=True)
    )
    by_fingerprint = {}
    raw_evidence = (
        EvidenceAsset.objects.using(db_alias)
        .filter(derivation_type="raw")
        .order_by("created_at", "id")
    )
    for evidence in raw_evidence.iterator():
        if (
            evidence.origin_run_source_item_id is None
            or evidence.source_item_id is None
            or not _is_sha256(evidence.evidence_content_hash)
            or (
                evidence.kind == "attachment"
                and not _is_sha256(evidence.checksum)
            )
            or evidence.kind not in {"attachment", "text"}
        ):
            _quarantine(EvidenceAsset, db_alias, evidence.id)
            continue
        fingerprint = _stable_fingerprint(evidence)
        by_fingerprint.setdefault(fingerprint, []).append(evidence)
    for fingerprint, candidates in by_fingerprint.items():
        canonical = min(
            candidates,
            key=lambda evidence: _canonical_raw_priority(
                evidence,
                canonical_document_input_ids=canonical_document_input_ids,
                completed_generic_input_ids=completed_generic_input_ids,
            ),
        )
        EvidenceAsset.objects.using(db_alias).filter(
            pk=canonical.id
        ).update(raw_input_fingerprint=fingerprint)
        for evidence in candidates:
            if evidence.id == canonical.id:
                continue
            _quarantine(EvidenceAsset, db_alias, evidence.id)


def clear_raw_input_fingerprints(apps, schema_editor):
    EvidenceAsset = apps.get_model("evidence", "EvidenceAsset")
    EvidenceAsset.objects.using(
        schema_editor.connection.alias
    ).update(raw_input_fingerprint=None)


def quarantine_duplicate_raw_derivations(apps, schema_editor):
    EvidenceAsset = apps.get_model("evidence", "EvidenceAsset")
    GenericExtractionAttempt = apps.get_model(
        "evidence",
        "GenericExtractionAttempt",
    )
    DocumentExtraction = apps.get_model("evidence", "DocumentExtraction")
    ExtractionRun = apps.get_model("evidence", "ExtractionRun")
    RunStep = apps.get_model("collection", "RunStep")
    db_alias = schema_editor.connection.alias
    now = timezone.now()
    duplicate_raw_ids = list(
        EvidenceAsset.objects.using(db_alias)
        .filter(
            derivation_type="raw",
            raw_input_fingerprint__isnull=True,
        )
        .values_list("id", flat=True)
    )
    if not duplicate_raw_ids:
        return
    affected_run_ids = set(
        EvidenceAsset.objects.using(db_alias)
        .filter(
            id__in=duplicate_raw_ids,
            origin_run_source_item__isnull=False,
        )
        .values_list("origin_run_source_item__run_id", flat=True)
    )
    attempts = GenericExtractionAttempt.objects.using(db_alias).filter(
        input_asset_id__in=duplicate_raw_ids,
    )
    affected_run_ids.update(
        attempts.values_list("run_source_item__run_id", flat=True)
    )
    attempt_ids = list(attempts.values_list("id", flat=True))
    linked_evidence_ids = list(
        attempts.exclude(evidence_asset_id__isnull=True).values_list(
            "evidence_asset_id",
            flat=True,
        )
    )
    attempts.filter(state__in=("queued", "running")).update(
        state="failed",
        error_code="legacy_duplicate_raw_quarantined",
        error_detail_redacted=(
            "noncanonical raw input preserved for audit only"
        ),
        finished_at=now,
    )
    derived = EvidenceAsset.objects.using(db_alias).filter(
        models.Q(generic_extraction_attempt_id__in=attempt_ids)
        | models.Q(id__in=linked_evidence_ids)
        | models.Q(parent_asset_id__in=duplicate_raw_ids)
    )
    derived_ids = list(derived.values_list("id", flat=True))
    derived.update(
        publishable=False,
        manual_review_required=True,
        review_state="manual_required",
    )
    documents = DocumentExtraction.objects.using(db_alias).filter(
        models.Q(input_asset_id__in=duplicate_raw_ids)
        | models.Q(input_asset_id__in=derived_ids),
    )
    document_ids = list(documents.values_list("id", flat=True))
    affected_run_ids.update(
        documents.values_list("run_source_item__run_id", flat=True)
    )
    documents.update(input_fingerprint=None)
    documents.filter(state__in=("queued", "running")).update(
        state="failed",
        document_complete=False,
        coverage_manifest_hash=None,
        selected_evidence_manifest_hash=None,
        error_code="legacy_duplicate_raw_quarantined",
        error_detail_redacted=(
            "document derived from a noncanonical raw input"
        ),
        finished_at=now,
    )
    ExtractionRun.objects.using(db_alias).filter(
        document_extraction_id__in=document_ids,
    ).exclude(
        state__in=("succeeded", "low_confidence", "failed"),
    ).update(
        state="failed",
        error_code="legacy_duplicate_raw_quarantined",
        finished_at=now,
    )
    EvidenceAsset.objects.using(db_alias).filter(
        document_extraction_id__in=document_ids,
    ).update(
        publishable=False,
        manual_review_required=True,
        review_state="manual_required",
    )
    RunStep.objects.using(db_alias).filter(
        run_id__in=affected_run_ids,
        run__state="extracting",
        run__stop_requested_at__isnull=True,
        name="extract",
        attempt_no=1,
        state="running",
        fanout_completed_at__isnull=False,
    ).update(fanout_completed_at=None)


def quarantine_legacy_document_duplicates(apps, schema_editor):
    DocumentExtraction = apps.get_model("evidence", "DocumentExtraction")
    ExtractionRun = apps.get_model("evidence", "ExtractionRun")
    EvidenceAsset = apps.get_model("evidence", "EvidenceAsset")
    db_alias = schema_editor.connection.alias
    now = timezone.now()
    duplicates = (
        DocumentExtraction.objects.using(db_alias)
        .filter(input_fingerprint__isnull=True)
        .order_by("created_at", "id")
    )
    for document in duplicates.iterator():
        canonical = list(
            DocumentExtraction.objects.using(db_alias).filter(
                run_source_item_id=document.run_source_item_id,
                input_kind=document.input_kind,
                input_checksum=document.input_checksum,
                input_fingerprint__isnull=False,
            )[:2]
        )
        if len(canonical) != 1:
            raise RuntimeError(
                "legacy document quarantine requires exactly one canonical "
                f"row for {document.id}"
            )
        DocumentExtraction.objects.using(db_alias).filter(
            pk=document.id,
            state__in=("queued", "running"),
        ).update(
            state="failed",
            document_complete=False,
            coverage_manifest_hash=None,
            selected_evidence_manifest_hash=None,
            error_code="legacy_duplicate_document_quarantined",
            error_detail_redacted=(
                "noncanonical legacy document preserved for audit only"
            ),
            finished_at=document.finished_at or now,
        )
        ExtractionRun.objects.using(db_alias).filter(
            document_extraction_id=document.id
        ).exclude(
            state__in=("succeeded", "low_confidence", "failed")
        ).update(
            state="failed",
            error_code="legacy_duplicate_document_quarantined",
            finished_at=now,
        )
        EvidenceAsset.objects.using(db_alias).filter(
            document_extraction_id=document.id
        ).update(
            publishable=False,
            manual_review_required=True,
            review_state="manual_required",
        )


class Migration(migrations.Migration):
    dependencies = [
        ("evidence", "0002_documentextraction_input_fingerprint"),
        ("collection", "0003_recover_inflight_evidence_fanout"),
    ]

    operations = [
        migrations.AddField(
            model_name="evidenceasset",
            name="raw_input_fingerprint",
            field=models.CharField(
                blank=True,
                max_length=64,
                null=True,
                unique=True,
            ),
        ),
        migrations.RunPython(
            backfill_raw_input_fingerprints,
            clear_raw_input_fingerprints,
        ),
        migrations.RunPython(
            quarantine_legacy_document_duplicates,
            migrations.RunPython.noop,
        ),
        migrations.RunPython(
            quarantine_duplicate_raw_derivations,
            migrations.RunPython.noop,
        ),
        migrations.AlterField(
            model_name="evidenceasset",
            name="raw_input_fingerprint",
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
