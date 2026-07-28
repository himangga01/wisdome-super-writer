import django.db.models.deletion
import django.utils.timezone
import hashlib
import unicodedata
import uuid
import rfc8785
from django.db import migrations, models


def _nfc(value):
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if isinstance(value, dict):
        return {_nfc(key): _nfc(child) for key, child in value.items()}
    if isinstance(value, list):
        return [_nfc(child) for child in value]
    return value


def backfill_envelope(apps, schema_editor):
    OutboxMessage = apps.get_model("infrastructure", "OutboxMessage")
    for message in OutboxMessage.objects.all().iterator():
        if message.topic == "publishing.target_canary.requested":
            message.payload = {
                "canary_run_id": message.payload.get("canary_run_id")
                or message.payload.get("canaryRunId"),
                "target_id": message.payload.get("target_id")
                or message.payload.get("targetId"),
            }
        elif message.topic == "publishing.target_disconnect.requested":
            message.payload = {
                "decision_id": message.payload.get("decision_id")
                or message.payload.get("decisionId"),
                "target_id": message.payload.get("target_id")
                or message.payload.get("targetId"),
            }
        elif message.topic == "run.requested":
            message.payload = {"run_id": message.payload.get("run_id")}
        elif message.topic == "article.draft_requested":
            message.topic = "run.draft_requested"
            message.payload = {"run_id": message.payload.get("run_id")}
        elif message.topic in {
            "publication.requested",
            "publication.reconcile_requested",
        }:
            message.payload = {
                "publication_attempt_id": message.payload.get(
                    "publication_attempt_id"
                )
            }
        message.job_id = message.aggregate_id
        message.occurred_at = message.created_at
        message.not_before = message.available_at
        message.status = "published" if message.published_at else "pending"
        material = {
            "event_id": str(message.id),
            "event_type": message.topic,
            "event_version": message.event_version,
            "occurred_at": message.occurred_at.isoformat(),
            "correlation_id": str(message.correlation_id),
            "causation_id": None,
            "job_id": str(message.aggregate_id),
            "entity_type": message.aggregate_type,
            "entity_id": str(message.aggregate_id),
            "operation": message.operation,
            "dedupe_key": message.message_key,
            "policy_versions": message.policy_versions,
            "not_before": message.not_before.isoformat(),
            "payload": message.payload,
        }
        message.immutable_material_hash = hashlib.sha256(
            rfc8785.dumps(_nfc(material))
        ).hexdigest()
        message.save(
            update_fields=(
                "job_id",
                "occurred_at",
                "not_before",
                "status",
                "topic",
                "payload",
                "immutable_material_hash",
            )
        )


class Migration(migrations.Migration):

    dependencies = [
        ('infrastructure', '0001_initial'),
    ]

    operations = [
        migrations.CreateModel(
            name='OutboxConsumerReceipt',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('consumer_name', models.CharField(max_length=160)),
                ('state', models.CharField(choices=[('processing', 'Processing'), ('retry', 'Retry'), ('succeeded', 'Succeeded'), ('dead_letter', 'Dead letter')], db_index=True, default='processing', max_length=20)),
                ('attempts', models.PositiveIntegerField(default=0)),
                ('last_error_code', models.CharField(blank=True, default='', max_length=120)),
                ('next_retry_at', models.DateTimeField(blank=True, null=True)),
                ('started_at', models.DateTimeField(blank=True, null=True)),
                ('completed_at', models.DateTimeField(blank=True, null=True)),
                ('dead_lettered_at', models.DateTimeField(blank=True, null=True)),
                ('claimed_at', models.DateTimeField(blank=True, null=True)),
                ('claimed_until', models.DateTimeField(blank=True, db_index=True, null=True)),
                ('lease_token', models.UUIDField(blank=True, null=True)),
                ('lease_generation', models.PositiveBigIntegerField(default=0)),
            ],
        ),
        migrations.RemoveIndex(
            model_name='outboxmessage',
            name='infrastruct_publish_cf9a4f_idx',
        ),
        migrations.AddField(
            model_name='outboxmessage',
            name='causation_id',
            field=models.UUIDField(blank=True, db_index=True, null=True),
        ),
        migrations.AddField(
            model_name='outboxmessage',
            name='dead_lettered_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='outboxmessage',
            name='event_version',
            field=models.PositiveSmallIntegerField(default=1),
        ),
        migrations.AddField(
            model_name='outboxmessage',
            name='immutable_material_hash',
            field=models.CharField(default='', max_length=64),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name='outboxmessage',
            name='job_id',
            field=models.UUIDField(db_index=True, default=uuid.uuid4),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name='outboxmessage',
            name='last_error_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='outboxmessage',
            name='lease_generation',
            field=models.PositiveBigIntegerField(default=0),
        ),
        migrations.AddField(
            model_name='outboxmessage',
            name='lease_owner',
            field=models.CharField(blank=True, default='', max_length=160),
        ),
        migrations.AddField(
            model_name='outboxmessage',
            name='lease_token',
            field=models.UUIDField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='outboxmessage',
            name='max_attempts',
            field=models.PositiveSmallIntegerField(default=5),
        ),
        migrations.AddField(
            model_name='outboxmessage',
            name='not_before',
            field=models.DateTimeField(default=django.utils.timezone.now),
        ),
        migrations.AddField(
            model_name='outboxmessage',
            name='occurred_at',
            field=models.DateTimeField(default=django.utils.timezone.now),
        ),
        migrations.AddField(
            model_name='outboxmessage',
            name='operation',
            field=models.CharField(default='process', max_length=80),
        ),
        migrations.AddField(
            model_name='outboxmessage',
            name='policy_versions',
            field=models.JSONField(default=dict),
        ),
        migrations.AddField(
            model_name='outboxmessage',
            name='status',
            field=models.CharField(choices=[('pending', 'Pending'), ('dispatching', 'Dispatching'), ('published', 'Published'), ('dead_letter', 'Dead letter')], db_index=True, default='pending', max_length=20),
        ),
        migrations.RunPython(backfill_envelope, migrations.RunPython.noop),
        migrations.AddIndex(
            model_name='outboxmessage',
            index=models.Index(fields=['status', 'available_at', 'claimed_until'], name='infrastruct_status_27a19c_idx'),
        ),
        migrations.AddField(
            model_name='outboxconsumerreceipt',
            name='event',
            field=models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='consumer_receipts', to='infrastructure.outboxmessage'),
        ),
        migrations.AddConstraint(
            model_name='outboxconsumerreceipt',
            constraint=models.UniqueConstraint(fields=('event', 'consumer_name'), name='unique_outbox_event_consumer'),
        ),
    ]
