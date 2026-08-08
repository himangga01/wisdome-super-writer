import hashlib
import json
import uuid
from datetime import timedelta

import django.core.validators
import django.db.models.deletion
from django.db import migrations, models


LEGACY_SENTINEL_VERSION = 2147483647


def _policy_authority_tiers(policy_material):
    if not isinstance(policy_material, dict):
        return []
    tiers = policy_material.get("allowedAuthorityTiers")
    if isinstance(tiers, list) and all(
        isinstance(tier, str) and tier for tier in tiers
    ):
        return list(dict.fromkeys(tiers))
    required_tier = policy_material.get("requiredAuthority")
    return [required_tier] if isinstance(required_tier, str) and required_tier else []


def _legacy_sentinel(TopicPolicy, db_alias, topic_code):
    """Create an explicit non-runtime marker for unprovable legacy policy lineage."""
    policy = TopicPolicy.objects.using(db_alias).filter(
        code=topic_code,
        version=LEGACY_SENTINEL_VERSION,
    ).first()
    if policy is not None:
        if (
            isinstance(policy.policy, dict)
            and policy.policy.get("schemaVersion")
            == "collection-run-policy-legacy-sentinel-v1"
        ):
            return policy
        raise RuntimeError(
            "Legacy sentinel TopicPolicy version is already occupied."
        )
    material = {
        "schemaVersion": "collection-run-policy-legacy-sentinel-v1",
        "runtimeEligible": False,
        "reason": (
            "The run predates explicit TopicPolicy pinning; its runtime policy "
            "cannot be proven."
        ),
    }
    policy_hash = hashlib.sha256(
        json.dumps(
            material,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    return TopicPolicy.objects.using(db_alias).create(
        code=topic_code,
        version=LEGACY_SENTINEL_VERSION,
        title="Legacy run policy unknown (not runtime eligible)",
        freshness_minutes=1,
        policy=material,
        policy_hash=policy_hash,
        active=False,
    )


def backfill_run_policy_snapshots(apps, schema_editor):
    CollectionRun = apps.get_model("collection", "CollectionRun")
    TopicPolicy = apps.get_model("topics", "TopicPolicy")
    db_alias = schema_editor.connection.alias

    runs = CollectionRun.objects.using(db_alias).order_by("id")
    for run in runs.iterator():
        policy = _legacy_sentinel(TopicPolicy, db_alias, run.topic_code)
        CollectionRun.objects.using(db_alias).filter(pk=run.pk).update(
            topic_policy_id=policy.id,
            policy_version=policy.version,
            policy_hash=policy.policy_hash,
            freshness_minutes=policy.freshness_minutes,
            allowed_authority_tiers=_policy_authority_tiers(policy.policy),
            freshness_cutoff=(
                run.window_end - timedelta(minutes=policy.freshness_minutes)
            ),
        )


def reverse_run_policy_snapshots(apps, schema_editor):
    CollectionRun = apps.get_model("collection", "CollectionRun")
    TopicPolicy = apps.get_model("topics", "TopicPolicy")
    db_alias = schema_editor.connection.alias
    sentinel_ids = list(
        TopicPolicy.objects.using(db_alias)
        .filter(version=LEGACY_SENTINEL_VERSION)
        .values_list("id", flat=True)
    )
    if not sentinel_ids:
        return
    CollectionRun.objects.using(db_alias).filter(
        topic_policy_id__in=sentinel_ids,
    ).update(topic_policy_id=None)
    TopicPolicy.objects.using(db_alias).filter(
        id__in=sentinel_ids,
        policy__schemaVersion="collection-run-policy-legacy-sentinel-v1",
    ).delete()


def create_source_collection_observation_guard(apps, schema_editor):
    del apps
    if schema_editor.connection.vendor != "postgresql":
        return
    schema_editor.execute(
        """
        CREATE OR REPLACE FUNCTION
            collection_guard_source_collection_observation()
        RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION
                'SourceCollectionObservation is append-only'
                USING ERRCODE = '23514';
        END;
        $$ LANGUAGE plpgsql;
        CREATE TRIGGER collection_source_observation_append_only
        BEFORE UPDATE OR DELETE
        ON collection_sourcecollectionobservation
        FOR EACH ROW EXECUTE FUNCTION
            collection_guard_source_collection_observation();
        """
    )


def drop_source_collection_observation_guard(apps, schema_editor):
    del apps
    if schema_editor.connection.vendor != "postgresql":
        return
    schema_editor.execute(
        """
        DROP TRIGGER IF EXISTS collection_source_observation_append_only
            ON collection_sourcecollectionobservation;
        DROP FUNCTION IF EXISTS
            collection_guard_source_collection_observation();
        """
    )


class Migration(migrations.Migration):

    dependencies = [
        ("collection", "0007_source_item_status_lineage"),
        ("topics", "0004_semiconductor_source_choices"),
    ]

    operations = [
        migrations.AddField(
            model_name="collectionrun",
            name="topic_policy",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="collection_runs",
                to="topics.topicpolicy",
            ),
        ),
        migrations.AddField(
            model_name="collectionrun",
            name="policy_version",
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="collectionrun",
            name="policy_hash",
            field=models.CharField(blank=True, max_length=64, null=True),
        ),
        migrations.AddField(
            model_name="collectionrun",
            name="freshness_minutes",
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="collectionrun",
            name="allowed_authority_tiers",
            field=models.JSONField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="collectionrun",
            name="freshness_cutoff",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.RunPython(
            backfill_run_policy_snapshots,
            reverse_run_policy_snapshots,
        ),
        migrations.AlterField(
            model_name="collectionrun",
            name="topic_policy",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="collection_runs",
                to="topics.topicpolicy",
            ),
        ),
        migrations.AlterField(
            model_name="collectionrun",
            name="policy_version",
            field=models.PositiveIntegerField(),
        ),
        migrations.AlterField(
            model_name="collectionrun",
            name="policy_hash",
            field=models.CharField(
                max_length=64,
                validators=[
                    django.core.validators.RegexValidator(
                        "^[a-f0-9]{64}$",
                        "Expected a lowercase SHA-256 digest",
                    )
                ],
            ),
        ),
        migrations.AlterField(
            model_name="collectionrun",
            name="freshness_minutes",
            field=models.PositiveIntegerField(),
        ),
        migrations.AlterField(
            model_name="collectionrun",
            name="allowed_authority_tiers",
            field=models.JSONField(default=list),
        ),
        migrations.AlterField(
            model_name="collectionrun",
            name="freshness_cutoff",
            field=models.DateTimeField(),
        ),
        migrations.AddConstraint(
            model_name="collectionrun",
            constraint=models.CheckConstraint(
                condition=models.Q(("policy_version__gt", 0)),
                name="ck_collection_run_policy_version_positive",
            ),
        ),
        migrations.AddConstraint(
            model_name="collectionrun",
            constraint=models.CheckConstraint(
                condition=models.Q(("freshness_minutes__gt", 0)),
                name="ck_collection_run_freshness_minutes_positive",
            ),
        ),
        migrations.AlterField(
            model_name="sourcecollectionattempt",
            name="state",
            field=models.CharField(
                choices=[
                    ("queued", "Queued"),
                    ("running", "Running"),
                    ("retry_scheduled", "Retry scheduled"),
                    ("succeeded", "Succeeded"),
                    ("failed", "Failed"),
                    ("skipped", "Skipped"),
                ],
                default="queued",
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name="sourcecollectionattempt",
            name="failure_category",
            field=models.CharField(blank=True, choices=[("policy", "Policy"), ("schema", "Schema"), ("authentication", "Authentication"), ("transient", "Transient"), ("security", "Security"), ("infrastructure", "Infrastructure"), ("freshness", "Freshness"), ("authority", "Authority"), ("rights", "Rights")], default="", max_length=32),
        ),
        migrations.AddField(model_name="sourcecollectionattempt", name="http_status", field=models.PositiveSmallIntegerField(blank=True, null=True)),
        migrations.AddField(model_name="sourcecollectionattempt", name="retry_count", field=models.PositiveIntegerField(default=0)),
        migrations.AddField(model_name="sourcecollectionattempt", name="retry_at", field=models.DateTimeField(blank=True, null=True)),
        migrations.AddField(model_name="sourcecollectionattempt", name="retry_after_seconds", field=models.PositiveIntegerField(blank=True, null=True)),
        migrations.AddField(model_name="sourcecollectionattempt", name="request_count", field=models.PositiveIntegerField(default=0)),
        migrations.AddField(model_name="sourcecollectionattempt", name="access_policy_hash", field=models.CharField(blank=True, default="", max_length=64, validators=[django.core.validators.RegexValidator("^[a-f0-9]{64}$", "Expected a lowercase SHA-256 digest")])),
        migrations.AddField(model_name="sourcecollectionattempt", name="rights_policy_hash", field=models.CharField(blank=True, default="", max_length=64, validators=[django.core.validators.RegexValidator("^[a-f0-9]{64}$", "Expected a lowercase SHA-256 digest")])),
        migrations.AddField(model_name="sourcecollectionattempt", name="authority_tier", field=models.CharField(blank=True, default="", max_length=32)),
        migrations.AddField(model_name="sourcecollectionattempt", name="freshness_cutoff", field=models.DateTimeField(blank=True, null=True)),
        migrations.AddField(model_name="sourcecollectionattempt", name="freshness_excluded_count", field=models.PositiveIntegerField(default=0)),
        migrations.AddField(model_name="sourcecollectionattempt", name="duration_ms", field=models.PositiveBigIntegerField(blank=True, null=True)),
        migrations.AddConstraint(model_name="sourcecollectionattempt", constraint=models.CheckConstraint(condition=models.Q(("retry_count__gte", 0)), name="ck_collection_attempt_retry_count_nonnegative")),
        migrations.AddConstraint(model_name="sourcecollectionattempt", constraint=models.CheckConstraint(condition=models.Q(("http_status__isnull", True), models.Q(("http_status__gte", 100), ("http_status__lte", 599)), _connector="OR"), name="ck_collection_attempt_http_status_valid")),
        migrations.AddConstraint(model_name="sourcecollectionattempt", constraint=models.CheckConstraint(condition=models.Q(("retry_after_seconds__isnull", True), ("retry_after_seconds__gte", 0), _connector="OR"), name="ck_collection_attempt_retry_after_nonnegative")),
        migrations.AddConstraint(model_name="sourcecollectionattempt", constraint=models.CheckConstraint(condition=models.Q(("request_count__gte", 0)), name="ck_collection_attempt_request_count_nonnegative")),
        migrations.AddConstraint(model_name="sourcecollectionattempt", constraint=models.CheckConstraint(condition=models.Q(("duration_ms__isnull", True), ("duration_ms__gte", 0), _connector="OR"), name="ck_collection_attempt_duration_nonnegative")),
        migrations.AddIndex(model_name="sourcecollectionattempt", index=models.Index(fields=["run", "state", "retry_at"], name="collection__run_id_4ed4f3_idx")),
        migrations.CreateModel(
            name="SourceCollectionObservation",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("delivery_attempt_no", models.PositiveIntegerField()),
                ("outcome", models.CharField(choices=[("queued", "Queued"), ("running", "Running"), ("retry_scheduled", "Retry scheduled"), ("succeeded", "Succeeded"), ("failed", "Failed"), ("skipped", "Skipped")], max_length=20)),
                ("failure_category", models.CharField(blank=True, choices=[("policy", "Policy"), ("schema", "Schema"), ("authentication", "Authentication"), ("transient", "Transient"), ("security", "Security"), ("infrastructure", "Infrastructure"), ("freshness", "Freshness"), ("authority", "Authority"), ("rights", "Rights")], default="", max_length=32)),
                ("error_code", models.CharField(blank=True, max_length=100, null=True)),
                ("error_detail_redacted", models.CharField(blank=True, max_length=500, null=True)),
                ("http_status", models.PositiveSmallIntegerField(blank=True, null=True)),
                ("retry_count", models.PositiveIntegerField(default=0)),
                ("retry_at", models.DateTimeField(blank=True, null=True)),
                ("retry_after_seconds", models.PositiveIntegerField(blank=True, null=True)),
                ("request_count", models.PositiveIntegerField(default=0)),
                ("freshness_excluded_count", models.PositiveIntegerField(default=0)),
                ("duration_ms", models.PositiveBigIntegerField(blank=True, null=True)),
                ("access_policy_hash", models.CharField(blank=True, default="", max_length=64, validators=[django.core.validators.RegexValidator("^[a-f0-9]{64}$", "Expected a lowercase SHA-256 digest")])),
                ("authority_tier", models.CharField(blank=True, default="", max_length=32)),
                ("freshness_cutoff", models.DateTimeField(blank=True, null=True)),
                ("recorded_at", models.DateTimeField(auto_now_add=True)),
                ("attempt", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="observations", to="collection.sourcecollectionattempt")),
            ],
            options={"base_manager_name": "objects"},
        ),
        migrations.AddConstraint(model_name="sourcecollectionobservation", constraint=models.UniqueConstraint(fields=("attempt", "delivery_attempt_no"), name="uq_collection_observation_delivery_attempt")),
        migrations.AddConstraint(model_name="sourcecollectionobservation", constraint=models.CheckConstraint(condition=models.Q(("delivery_attempt_no__gt", 0)), name="ck_collection_observation_delivery_attempt_positive")),
        migrations.AddConstraint(model_name="sourcecollectionobservation", constraint=models.CheckConstraint(condition=models.Q(("retry_count__gte", 0)), name="ck_collection_observation_retry_count_nonnegative")),
        migrations.AddConstraint(model_name="sourcecollectionobservation", constraint=models.CheckConstraint(condition=models.Q(("http_status__isnull", True), models.Q(("http_status__gte", 100), ("http_status__lte", 599)), _connector="OR"), name="ck_collection_observation_http_status_valid")),
        migrations.AddConstraint(model_name="sourcecollectionobservation", constraint=models.CheckConstraint(condition=models.Q(("retry_after_seconds__isnull", True), ("retry_after_seconds__gte", 0), _connector="OR"), name="ck_collection_observation_retry_after_nonnegative")),
        migrations.AddConstraint(model_name="sourcecollectionobservation", constraint=models.CheckConstraint(condition=models.Q(("request_count__gte", 0)), name="ck_collection_observation_request_count_nonnegative")),
        migrations.AddConstraint(model_name="sourcecollectionobservation", constraint=models.CheckConstraint(condition=models.Q(("duration_ms__isnull", True), ("duration_ms__gte", 0), _connector="OR"), name="ck_collection_observation_duration_nonnegative")),
        migrations.AddIndex(model_name="sourcecollectionobservation", index=models.Index(fields=["attempt", "recorded_at"], name="collection__attempt_2c7c03_idx")),
        migrations.RunPython(
            create_source_collection_observation_guard,
            drop_source_collection_observation_guard,
        ),
    ]
