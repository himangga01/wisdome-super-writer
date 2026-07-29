import django.db.models.deletion
import uuid
from django.conf import settings
from django.db import migrations, models

from apps.topics import models as topics_models


_RIGHTS_STATUSES = {
    "allowed",
    "attribution_required",
    "internal_analysis_only",
    "unknown",
    "prohibited",
}


def _positive_int(value, fallback):
    if isinstance(value, bool):
        return fallback
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return fallback
    return parsed if parsed > 0 else fallback


def _legacy_adapter_key(config, access_method):
    adapter = config.get("adapter")
    if isinstance(adapter, str) and adapter.strip():
        return adapter.strip()
    return {
        "rss_atom": "rss",
        "public_html": "public_html",
        "public_file": "public_html",
        "public_api": "public_html",
        "open_data_api": "open_data_json",
    }.get(access_method, "public_html")


def _legacy_external_config(config):
    entrypoints = config.get("entrypoints")
    if not isinstance(entrypoints, list):
        return {}
    return {
        "entrypoints": [
            entrypoint
            for entrypoint in entrypoints
            if isinstance(entrypoint, str) and entrypoint
        ]
    }


def _legacy_allowed_mime_types(config):
    values = config.get("allowedContentTypes")
    if not isinstance(values, list):
        return []
    return [
        value
        for value in values
        if isinstance(value, str) and value
    ]


def _optional_text(value):
    return value if isinstance(value, str) and value else None


def _legacy_snapshot_material(source, config):
    rights_status = config.get("rightsStatus")
    if rights_status not in _RIGHTS_STATUSES:
        rights_status = "unknown"
    poll_minutes = _positive_int(config.get("pollMinutes"), 60)
    if poll_minutes > 153722867280912930:
        poll_minutes = 60
    requests_per_minute = _positive_int(
        config.get("rateLimitPerMinute"),
        10,
    )
    adapter_key = _legacy_adapter_key(config, source.access_method)
    external_config = _legacy_external_config(config)
    allowed_mime_types = _legacy_allowed_mime_types(config)
    rate_limit_policy = {
        "maxConcurrency": 1,
        "requestsPerMinute": requests_per_minute,
        "burst": 1,
    }
    return {
        "schemaVersion": "source-definition-snapshot-legacy-v1",
        "topic": source.topic_code,
        "name": source.display_name,
        "publisher": source.owner_name,
        "ownerName": source.owner_name,
        "editorialControlName": source.owner_name,
        "baseUrl": source.base_url,
        "authorityTier": source.authority_tier,
        "accessMethod": source.access_method,
        "independenceGroupId": source.independence_group,
        "adapterKey": adapter_key,
        "externalConfig": external_config,
        "secretRef": None,
        "allowedMimeTypes": allowed_mime_types,
        "defaultRightsStatus": rights_status,
        "termsUrl": _optional_text(config.get("termsUrl")),
        "robotsUrl": _optional_text(config.get("robotsUrl")),
        "licenseUrl": _optional_text(config.get("licenseUrl")),
        "pollIntervalSeconds": poll_minutes * 60,
        "rateLimitPolicy": rate_limit_policy,
        "enabled": source.enabled,
        "legacyConfig": config,
    }


def backfill_source_registry_contract(apps, schema_editor):
    SourceDefinition = apps.get_model("topics", "SourceDefinition")
    SourceDefinitionSnapshot = apps.get_model(
        "topics",
        "SourceDefinitionSnapshot",
    )
    SourceRegistrySnapshot = apps.get_model(
        "topics",
        "SourceRegistrySnapshot",
    )
    SourceRegistryMembership = apps.get_model(
        "topics",
        "SourceRegistryMembership",
    )

    for source in SourceDefinition.objects.all().iterator():
        snapshots = list(
            SourceDefinitionSnapshot.objects.filter(
                source_id=source.pk,
            ).order_by("version", "created_at", "pk")
        )
        selected_snapshot = next(
            (
                snapshot
                for snapshot in snapshots
                if snapshot.version == source.current_snapshot_version
            ),
            snapshots[-1] if snapshots else None,
        )
        config = (
            selected_snapshot.config
            if selected_snapshot and isinstance(selected_snapshot.config, dict)
            else {}
        )
        projection_material = _legacy_snapshot_material(
            source,
            config,
        )
        approved_versions = [
            snapshot.version
            for snapshot in snapshots
            if snapshot.state == "approved"
        ]
        draft_snapshots = [
            snapshot
            for snapshot in snapshots
            if snapshot.state == "draft"
        ]
        latest_draft = (
            max(draft_snapshots, key=lambda snapshot: snapshot.version)
            if draft_snapshots
            else None
        )

        SourceDefinition.objects.filter(pk=source.pk).update(
            publisher=source.owner_name,
            editorial_control_name=source.owner_name,
            adapter_key=projection_material["adapterKey"],
            external_config=projection_material["externalConfig"],
            allowed_mime_types=projection_material["allowedMimeTypes"],
            default_rights_status=projection_material[
                "defaultRightsStatus"
            ],
            terms_url=projection_material["termsUrl"],
            robots_url=projection_material["robotsUrl"],
            license_url=projection_material["licenseUrl"],
            poll_interval_seconds=projection_material[
                "pollIntervalSeconds"
            ],
            rate_limit_policy=projection_material["rateLimitPolicy"],
            latest_approved_snapshot_version=(
                max(approved_versions) if approved_versions else None
            ),
            latest_draft_snapshot_id=(
                latest_draft.pk if latest_draft else None
            ),
            latest_draft_snapshot_version=(
                latest_draft.version if latest_draft else None
            ),
            latest_draft_config_hash=(
                latest_draft.config_hash if latest_draft else None
            ),
        )

        for snapshot in snapshots:
            snapshot_config = (
                snapshot.config
                if isinstance(snapshot.config, dict)
                else {}
            )
            SourceDefinitionSnapshot.objects.filter(
                pk=snapshot.pk,
            ).update(
                topic_code=source.topic_code,
                independence_group=source.independence_group,
                owner_name=source.owner_name,
                editorial_control_name=source.owner_name,
                config=_legacy_snapshot_material(
                    source,
                    snapshot_config,
                ),
            )

    seen_memberships = set()
    for membership in (
        SourceRegistryMembership.objects.select_related(
            "source_snapshot",
        )
        .all()
        .iterator()
    ):
        source_definition_id = membership.source_snapshot.source_id
        identity = (membership.registry_id, source_definition_id)
        if identity in seen_memberships:
            raise RuntimeError(
                "Cannot enforce one membership per source definition: "
                f"registry {membership.registry_id} contains source "
                f"{source_definition_id} more than once."
            )
        seen_memberships.add(identity)
        SourceRegistryMembership.objects.filter(pk=membership.pk).update(
            source_definition_id=source_definition_id,
        )

    approved_topics = set()
    for registry_id, topic_code in (
        SourceRegistrySnapshot.objects.filter(state="approved")
        .values_list("pk", "topic_code")
        .iterator()
    ):
        if topic_code in approved_topics:
            raise RuntimeError(
                "Cannot enforce one approved registry per topic: "
                f"topic {topic_code} has multiple approved rows, "
                f"including {registry_id}."
            )
        approved_topics.add(topic_code)


def restore_legacy_snapshot_config(apps, schema_editor):
    SourceDefinitionSnapshot = apps.get_model(
        "topics",
        "SourceDefinitionSnapshot",
    )
    for snapshot in SourceDefinitionSnapshot.objects.all().iterator():
        config = snapshot.config
        if (
            isinstance(config, dict)
            and config.get("schemaVersion")
            == "source-definition-snapshot-legacy-v1"
            and isinstance(config.get("legacyConfig"), dict)
        ):
            SourceDefinitionSnapshot.objects.filter(pk=snapshot.pk).update(
                config=config["legacyConfig"],
            )


class Migration(migrations.Migration):

    dependencies = [
        ("topics", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="sourcedefinition",
            name="publisher",
            field=models.CharField(max_length=200, null=True),
        ),
        migrations.AddField(
            model_name="sourcedefinition",
            name="editorial_control_name",
            field=models.CharField(max_length=200, null=True),
        ),
        migrations.AddField(
            model_name="sourcedefinition",
            name="adapter_key",
            field=models.CharField(max_length=160, null=True),
        ),
        migrations.AddField(
            model_name="sourcedefinition",
            name="external_config",
            field=models.JSONField(null=True),
        ),
        migrations.AddField(
            model_name="sourcedefinition",
            name="secret_ref",
            field=models.CharField(
                blank=True,
                max_length=300,
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="sourcedefinition",
            name="allowed_mime_types",
            field=models.JSONField(null=True),
        ),
        migrations.AddField(
            model_name="sourcedefinition",
            name="default_rights_status",
            field=models.CharField(
                choices=[
                    ("allowed", "허용"),
                    ("attribution_required", "출처 표시 필요"),
                    ("internal_analysis_only", "내부 분석 전용"),
                    ("unknown", "불명"),
                    ("prohibited", "금지"),
                ],
                max_length=32,
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="sourcedefinition",
            name="terms_url",
            field=models.URLField(
                blank=True,
                max_length=500,
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="sourcedefinition",
            name="robots_url",
            field=models.URLField(
                blank=True,
                max_length=500,
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="sourcedefinition",
            name="license_url",
            field=models.URLField(
                blank=True,
                max_length=500,
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="sourcedefinition",
            name="poll_interval_seconds",
            field=models.PositiveBigIntegerField(null=True),
        ),
        migrations.AddField(
            model_name="sourcedefinition",
            name="rate_limit_policy",
            field=models.JSONField(null=True),
        ),
        migrations.AddField(
            model_name="sourcedefinition",
            name="latest_approved_snapshot_version",
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="sourcedefinition",
            name="latest_draft_snapshot",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="topics.sourcedefinitionsnapshot",
            ),
        ),
        migrations.AddField(
            model_name="sourcedefinition",
            name="latest_draft_snapshot_version",
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="sourcedefinition",
            name="latest_draft_config_hash",
            field=models.CharField(
                blank=True,
                max_length=64,
                null=True,
                validators=[topics_models.sha256_validator],
            ),
        ),
        migrations.AddField(
            model_name="sourcedefinition",
            name="last_health",
            field=models.JSONField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="sourcedefinition",
            name="creation_request_key",
            field=models.CharField(
                blank=True,
                max_length=200,
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="sourcedefinition",
            name="creation_request_hash",
            field=models.CharField(
                blank=True,
                max_length=64,
                null=True,
                validators=[topics_models.sha256_validator],
            ),
        ),
        migrations.AddField(
            model_name="sourcedefinitionsnapshot",
            name="topic_code",
            field=models.CharField(
                choices=[
                    (
                        "housing_subscription",
                        "대한민국 부동산 청약 정보",
                    ),
                    (
                        "semiconductor_news",
                        "한국 및 글로벌 반도체 뉴스",
                    ),
                ],
                max_length=40,
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="sourcedefinitionsnapshot",
            name="independence_group",
            field=models.CharField(max_length=200, null=True),
        ),
        migrations.AddField(
            model_name="sourcedefinitionsnapshot",
            name="owner_name",
            field=models.CharField(max_length=200, null=True),
        ),
        migrations.AddField(
            model_name="sourcedefinitionsnapshot",
            name="editorial_control_name",
            field=models.CharField(max_length=200, null=True),
        ),
        migrations.AddField(
            model_name="sourcedefinitionsnapshot",
            name="request_key",
            field=models.CharField(
                blank=True,
                max_length=200,
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="sourcedefinitionsnapshot",
            name="request_hash",
            field=models.CharField(
                blank=True,
                max_length=64,
                null=True,
                validators=[topics_models.sha256_validator],
            ),
        ),
        migrations.AddField(
            model_name="sourcedefinitionsnapshot",
            name="retired_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="sourceregistrysnapshot",
            name="base_approved_registry",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="derived_drafts",
                to="topics.sourceregistrysnapshot",
            ),
        ),
        migrations.AddField(
            model_name="sourceregistrysnapshot",
            name="base_approved_version",
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="sourceregistrysnapshot",
            name="base_approved_manifest_hash",
            field=models.CharField(
                blank=True,
                max_length=64,
                null=True,
                validators=[topics_models.sha256_validator],
            ),
        ),
        migrations.AddField(
            model_name="sourceregistrysnapshot",
            name="draft_request_key",
            field=models.CharField(
                blank=True,
                max_length=200,
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="sourceregistrysnapshot",
            name="draft_request_hash",
            field=models.CharField(
                blank=True,
                max_length=64,
                null=True,
                validators=[topics_models.sha256_validator],
            ),
        ),
        migrations.AddField(
            model_name="sourceregistrysnapshot",
            name="retired_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="sourceregistrymembership",
            name="source_definition",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="registry_memberships",
                to="topics.sourcedefinition",
            ),
        ),
        migrations.RunPython(
            backfill_source_registry_contract,
            restore_legacy_snapshot_config,
        ),
        migrations.AlterField(
            model_name="sourcedefinition",
            name="creation_request_key",
            field=models.CharField(
                blank=True,
                max_length=200,
                null=True,
                unique=True,
            ),
        ),
        migrations.AlterField(
            model_name="sourcedefinition",
            name="publisher",
            field=models.CharField(max_length=200),
        ),
        migrations.AlterField(
            model_name="sourcedefinition",
            name="topic_code",
            field=models.CharField(
                choices=[
                    (
                        "housing_subscription",
                        "대한민국 부동산 청약 정보",
                    ),
                    (
                        "semiconductor_news",
                        "한국 및 글로벌 반도체 뉴스",
                    ),
                ],
                db_index=True,
                max_length=40,
            ),
        ),
        migrations.AlterField(
            model_name="sourcedefinition",
            name="editorial_control_name",
            field=models.CharField(max_length=200),
        ),
        migrations.AlterField(
            model_name="sourcedefinition",
            name="adapter_key",
            field=models.CharField(max_length=160),
        ),
        migrations.AlterField(
            model_name="sourcedefinition",
            name="external_config",
            field=models.JSONField(default=dict),
        ),
        migrations.AlterField(
            model_name="sourcedefinition",
            name="allowed_mime_types",
            field=models.JSONField(default=list),
        ),
        migrations.AlterField(
            model_name="sourcedefinition",
            name="default_rights_status",
            field=models.CharField(
                choices=[
                    ("allowed", "허용"),
                    ("attribution_required", "출처 표시 필요"),
                    ("internal_analysis_only", "내부 분석 전용"),
                    ("unknown", "불명"),
                    ("prohibited", "금지"),
                ],
                default="unknown",
                max_length=32,
            ),
        ),
        migrations.AlterField(
            model_name="sourcedefinition",
            name="poll_interval_seconds",
            field=models.PositiveBigIntegerField(default=3600),
        ),
        migrations.AlterField(
            model_name="sourcedefinition",
            name="rate_limit_policy",
            field=models.JSONField(
                default=topics_models.default_rate_limit_policy,
            ),
        ),
        migrations.AlterField(
            model_name="sourcedefinition",
            name="independence_group",
            field=models.CharField(max_length=200),
        ),
        migrations.AlterField(
            model_name="sourcedefinitionsnapshot",
            name="topic_code",
            field=models.CharField(
                choices=[
                    (
                        "housing_subscription",
                        "대한민국 부동산 청약 정보",
                    ),
                    (
                        "semiconductor_news",
                        "한국 및 글로벌 반도체 뉴스",
                    ),
                ],
                db_index=True,
                max_length=40,
            ),
        ),
        migrations.AlterField(
            model_name="sourcedefinitionsnapshot",
            name="independence_group",
            field=models.CharField(max_length=200),
        ),
        migrations.AlterField(
            model_name="sourcedefinitionsnapshot",
            name="owner_name",
            field=models.CharField(max_length=200),
        ),
        migrations.AlterField(
            model_name="sourcedefinitionsnapshot",
            name="editorial_control_name",
            field=models.CharField(max_length=200),
        ),
        migrations.AlterField(
            model_name="sourcedefinitionsnapshot",
            name="config_hash",
            field=models.CharField(
                max_length=64,
                validators=[topics_models.sha256_validator],
            ),
        ),
        migrations.AlterField(
            model_name="sourcedefinitionsnapshot",
            name="approved_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AlterField(
            model_name="sourceregistrysnapshot",
            name="topic_code",
            field=models.CharField(
                choices=[
                    (
                        "housing_subscription",
                        "대한민국 부동산 청약 정보",
                    ),
                    (
                        "semiconductor_news",
                        "한국 및 글로벌 반도체 뉴스",
                    ),
                ],
                db_index=True,
                max_length=40,
            ),
        ),
        migrations.AlterField(
            model_name="sourceregistrysnapshot",
            name="manifest_hash",
            field=models.CharField(
                max_length=64,
                validators=[topics_models.sha256_validator],
            ),
        ),
        migrations.AlterField(
            model_name="sourceregistrysnapshot",
            name="row_version",
            field=models.PositiveBigIntegerField(default=1),
        ),
        migrations.AlterField(
            model_name="sourceregistrysnapshot",
            name="approved_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AlterField(
            model_name="sourceregistrymembership",
            name="source_definition",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="registry_memberships",
                to="topics.sourcedefinition",
            ),
        ),
        migrations.AlterField(
            model_name="sourceregistrymembership",
            name="display_order",
            field=models.PositiveBigIntegerField(default=0),
        ),
        migrations.AlterField(
            model_name="topicpolicy",
            name="code",
            field=models.CharField(
                choices=[
                    (
                        "housing_subscription",
                        "대한민국 부동산 청약 정보",
                    ),
                    (
                        "semiconductor_news",
                        "한국 및 글로벌 반도체 뉴스",
                    ),
                ],
                max_length=40,
            ),
        ),
        migrations.AlterField(
            model_name="topicpolicy",
            name="policy_hash",
            field=models.CharField(
                max_length=64,
                validators=[topics_models.sha256_validator],
            ),
        ),
        migrations.AddConstraint(
            model_name="sourcedefinition",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(
                        latest_draft_snapshot__isnull=True,
                        latest_draft_snapshot_version__isnull=True,
                        latest_draft_config_hash__isnull=True,
                    )
                    | models.Q(
                        latest_draft_snapshot__isnull=False,
                        latest_draft_snapshot_version__isnull=False,
                        latest_draft_config_hash__isnull=False,
                    )
                ),
                name="ck_source_latest_draft_complete",
            ),
        ),
        migrations.AddConstraint(
            model_name="sourcedefinition",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(
                        creation_request_key__isnull=True,
                        creation_request_hash__isnull=True,
                    )
                    | models.Q(
                        creation_request_key__isnull=False,
                        creation_request_hash__isnull=False,
                    )
                ),
                name="ck_source_creation_request_complete",
            ),
        ),
        migrations.RemoveConstraint(
            model_name="sourcedefinitionsnapshot",
            name="uq_source_snapshot_material",
        ),
        migrations.AddConstraint(
            model_name="sourcedefinitionsnapshot",
            constraint=models.UniqueConstraint(
                condition=models.Q(("request_key__isnull", False)),
                fields=("source", "request_key"),
                name="uq_source_snapshot_request",
            ),
        ),
        migrations.AddConstraint(
            model_name="sourcedefinitionsnapshot",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(
                        request_key__isnull=True,
                        request_hash__isnull=True,
                    )
                    | models.Q(
                        request_key__isnull=False,
                        request_hash__isnull=False,
                    )
                ),
                name="ck_source_snapshot_request_complete",
            ),
        ),
        migrations.RemoveConstraint(
            model_name="sourceregistrymembership",
            name="uq_registry_source_snapshot",
        ),
        migrations.AddConstraint(
            model_name="sourceregistrymembership",
            constraint=models.UniqueConstraint(
                fields=("registry", "source_definition"),
                name="uq_registry_source_definition",
            ),
        ),
        migrations.AddConstraint(
            model_name="sourceregistrysnapshot",
            constraint=models.UniqueConstraint(
                condition=models.Q(("draft_request_key__isnull", False)),
                fields=("topic_code", "draft_request_key"),
                name="uq_registry_draft_request",
            ),
        ),
        migrations.AddConstraint(
            model_name="sourceregistrysnapshot",
            constraint=models.UniqueConstraint(
                condition=models.Q(("state", "approved")),
                fields=("topic_code",),
                name="uq_registry_approved_topic",
            ),
        ),
        migrations.AddConstraint(
            model_name="sourceregistrysnapshot",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(
                        base_approved_registry__isnull=True,
                        base_approved_version__isnull=True,
                        base_approved_manifest_hash__isnull=True,
                    )
                    | models.Q(
                        base_approved_registry__isnull=False,
                        base_approved_version__isnull=False,
                        base_approved_manifest_hash__isnull=False,
                    )
                ),
                name="ck_registry_base_complete",
            ),
        ),
        migrations.AddConstraint(
            model_name="sourceregistrysnapshot",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(
                        draft_request_key__isnull=True,
                        draft_request_hash__isnull=True,
                    )
                    | models.Q(
                        draft_request_key__isnull=False,
                        draft_request_hash__isnull=False,
                    )
                ),
                name="ck_registry_draft_request_complete",
            ),
        ),
        migrations.AlterModelOptions(
            name="sourcedefinitionsnapshot",
            options={"ordering": ["source_id", "version"]},
        ),
        migrations.AlterModelOptions(
            name="sourceregistrymembership",
            options={"ordering": ["display_order", "source_definition_id"]},
        ),
        migrations.CreateModel(
            name="TopicRegistryHead",
            fields=[
                (
                    "topic_code",
                    models.CharField(
                        choices=[
                            (
                                "housing_subscription",
                                "대한민국 부동산 청약 정보",
                            ),
                            (
                                "semiconductor_news",
                                "한국 및 글로벌 반도체 뉴스",
                            ),
                        ],
                        max_length=40,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                (
                    "current_approved_version",
                    models.PositiveIntegerField(blank=True, null=True),
                ),
                (
                    "current_approved_manifest_hash",
                    models.CharField(
                        blank=True,
                        max_length=64,
                        null=True,
                        validators=[topics_models.sha256_validator],
                    ),
                ),
                (
                    "row_version",
                    models.PositiveBigIntegerField(default=1),
                ),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "current_approved_registry",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="topics.sourceregistrysnapshot",
                    ),
                ),
            ],
            options={
                "constraints": [
                    models.CheckConstraint(
                        condition=(
                            models.Q(
                                current_approved_registry__isnull=True,
                                current_approved_version__isnull=True,
                                current_approved_manifest_hash__isnull=True,
                            )
                            | models.Q(
                                current_approved_registry__isnull=False,
                                current_approved_version__isnull=False,
                                current_approved_manifest_hash__isnull=False,
                            )
                        ),
                        name="ck_topic_registry_head_complete",
                    )
                ],
            },
        ),
        migrations.CreateModel(
            name="SourceRegistryMutation",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                ("request_key", models.CharField(max_length=200)),
                (
                    "request_hash",
                    models.CharField(
                        max_length=64,
                        validators=[topics_models.sha256_validator],
                    ),
                ),
                (
                    "before_manifest_hash",
                    models.CharField(
                        max_length=64,
                        validators=[topics_models.sha256_validator],
                    ),
                ),
                (
                    "after_manifest_hash",
                    models.CharField(
                        max_length=64,
                        validators=[topics_models.sha256_validator],
                    ),
                ),
                (
                    "resulting_row_version",
                    models.PositiveBigIntegerField(),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "registry",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="mutations",
                        to="topics.sourceregistrysnapshot",
                    ),
                ),
                (
                    "source_definition",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="topics.sourcedefinition",
                    ),
                ),
            ],
            options={
                "ordering": ["registry_id", "created_at", "id"],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("registry", "request_key"),
                        name="uq_registry_mutation_request",
                    )
                ],
            },
        ),
        migrations.CreateModel(
            name="SourceRegistryDecision",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                ("version", models.PositiveIntegerField()),
                (
                    "decision",
                    models.CharField(
                        choices=[
                            ("approved", "승인"),
                            ("retired", "폐기"),
                        ],
                        max_length=16,
                    ),
                ),
                (
                    "expected_row_version",
                    models.PositiveBigIntegerField(),
                ),
                (
                    "expected_manifest_hash",
                    models.CharField(
                        max_length=64,
                        validators=[topics_models.sha256_validator],
                    ),
                ),
                (
                    "expected_current_head_version",
                    models.PositiveIntegerField(blank=True, null=True),
                ),
                (
                    "expected_current_head_manifest_hash",
                    models.CharField(
                        blank=True,
                        max_length=64,
                        null=True,
                        validators=[topics_models.sha256_validator],
                    ),
                ),
                ("request_key", models.CharField(max_length=200)),
                (
                    "request_hash",
                    models.CharField(
                        max_length=64,
                        validators=[topics_models.sha256_validator],
                    ),
                ),
                (
                    "decision_hash",
                    models.CharField(
                        max_length=64,
                        validators=[topics_models.sha256_validator],
                    ),
                ),
                ("decided_at", models.DateTimeField()),
                ("reason", models.CharField(max_length=500)),
                (
                    "decided_by",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "expected_current_head_registry",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="topics.sourceregistrysnapshot",
                    ),
                ),
                (
                    "registry",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="decisions",
                        to="topics.sourceregistrysnapshot",
                    ),
                ),
                (
                    "supersedes_decision",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="superseded_by",
                        to="topics.sourceregistrydecision",
                    ),
                ),
            ],
            options={
                "ordering": ["registry_id", "version"],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("registry", "version"),
                        name="uq_registry_decision_version",
                    ),
                    models.UniqueConstraint(
                        fields=("registry", "request_key"),
                        name="uq_registry_decision_request",
                    ),
                    models.CheckConstraint(
                        condition=(
                            models.Q(
                                expected_current_head_registry__isnull=True,
                                expected_current_head_version__isnull=True,
                                expected_current_head_manifest_hash__isnull=True,
                            )
                            | models.Q(
                                expected_current_head_registry__isnull=False,
                                expected_current_head_version__isnull=False,
                                expected_current_head_manifest_hash__isnull=False,
                            )
                        ),
                        name="ck_registry_decision_head_complete",
                    ),
                ],
            },
        ),
        migrations.AddField(
            model_name="sourceregistrysnapshot",
            name="latest_decision",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="topics.sourceregistrydecision",
            ),
        ),
    ]
