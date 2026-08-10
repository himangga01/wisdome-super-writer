from __future__ import annotations

import time
import uuid
from datetime import UTC, datetime, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

from croniter import croniter
from django.contrib.auth import get_user_model
from django.db import IntegrityError, OperationalError, transaction
from django.utils import timezone

from apps.accounts.services import consume_reauthentication_proof
from apps.audit.models import AuditEvent
from apps.audit.services import (
    AuditContext,
    AuditIdentityConflict,
    audit_event_id,
    record_audit_event,
    require_audit_replay,
)
from apps.collection.models import CollectionRun, RunState
from apps.collection.services import create_run
from wisdome_writer.domain.hashing import (
    CANONICAL_HASH_SCHEMA_V1,
    canonical_hash,
    sha256_hex,
)
from wisdome_writer.infrastructure.outbox import enqueue_event

from .controls import is_external_write_blocked
from .models import (
    SCHEDULE_DISPATCH_MATERIAL_VERSION,
    SCHEDULE_EXECUTION_MATERIAL_VERSION,
    KillSwitchDecision,
    OperationalControl,
    Schedule,
    ScheduleDispatch,
)


TERMINAL_RUN_STATES = {RunState.COMPLETED, RunState.STOPPED, RunState.FAILED}
_SCHEDULE_ID_NAMESPACE = uuid.UUID("af919249-fd4b-4d1d-8e7e-d6943171cfcb")
_SCHEDULE_DISPATCH_ID_NAMESPACE = uuid.UUID(
    "4975ad31-f714-4d8d-a67e-406675c46146"
)


def _request_hash(material) -> str:
    return canonical_hash(
        material,
        schema_version=CANONICAL_HASH_SCHEMA_V1,
    )


def _schedule_config_hash(schedule: Schedule) -> str:
    return _request_hash(
        {
            "schema_version": "schedule-config-v1",
            "name": schedule.name,
            "topic_code": schedule.topic_code,
            "cron_expression": schedule.cron_expression,
            "timezone": schedule.timezone,
            "window_minutes": schedule.window_minutes,
            "target_ids": schedule.target_ids,
            "approval_mode": schedule.approval_mode,
            "auto_publish_validation_refs": schedule.auto_publish_validation_refs,
            "auto_publish_activation_refs": schedule.auto_publish_activation_refs,
            "overlap_policy": schedule.overlap_policy,
        }
    )


def _schedule_material(schedule: Schedule) -> dict:
    return {
        "schema_version": "schedule-audit-v1",
        "schedule_id": str(schedule.id),
        "version": schedule.version,
        "config_hash": _schedule_config_hash(schedule),
        "enabled": schedule.enabled,
        "next_run_at": (
            schedule.next_run_at.isoformat() if schedule.next_run_at else None
        ),
        "last_dispatched_at": (
            schedule.last_dispatched_at.isoformat()
            if schedule.last_dispatched_at
            else None
        ),
        "updated_by_id": (
            str(schedule.updated_by_id) if schedule.updated_by_id else None
        ),
    }


def _dispatch_material(dispatch: ScheduleDispatch) -> dict:
    return {
        "schema_version": "schedule-dispatch-audit-v1",
        "dispatch_id": str(dispatch.id),
        "schedule_id": str(dispatch.schedule_id),
        "schedule_version": dispatch.schedule_version,
        "material_version": dispatch.material_version,
        "schedule_material_hash": dispatch.schedule_material_hash,
        "registry_snapshot_id": (
            str(dispatch.source_registry_id) if dispatch.source_registry_id else None
        ),
        "registry_manifest_hash": dispatch.registry_manifest_hash,
        "topic_policy_id": (
            str(dispatch.topic_policy_id) if dispatch.topic_policy_id else None
        ),
        "topic_policy_version": dispatch.topic_policy_version,
        "topic_policy_hash": dispatch.topic_policy_hash,
        "target_snapshot_refs": dispatch.target_snapshot_refs,
        "approval_mode_snapshot": dispatch.approval_mode_snapshot,
        "validation_refs": dispatch.validation_refs,
        "activation_refs": dispatch.activation_refs,
        "requested_by_id": (
            str(dispatch.requested_by_id) if dispatch.requested_by_id else None
        ),
        "scheduled_for": dispatch.scheduled_for.isoformat(),
        "tick_key": dispatch.tick_key,
        "state": dispatch.state,
        "reason_code": dispatch.reason_code,
        "coalesced_into_id": (
            str(dispatch.coalesced_into_id)
            if dispatch.coalesced_into_id
            else None
        ),
        "collection_run_id": (
            str(dispatch.collection_run_id)
            if dispatch.collection_run_id
            else None
        ),
        "tick_set_version": dispatch.tick_set_version,
        "coalesced_tick_manifest_hash": dispatch.coalesced_tick_manifest_hash,
        "execution_material_hash": dispatch.execution_material_hash,
        "run_request_fingerprint": dispatch.run_request_fingerprint,
    }


def _parse_frozen_datetime(value: str, *, field_name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be an ISO 8601 datetime") from exc
    if timezone.is_naive(parsed):
        raise ValueError(f"{field_name} must include a timezone")
    return parsed.astimezone(UTC)


def _coalesced_tick_union(
    existing_refs: list[dict],
    new_ref: dict,
    *,
    max_ticks: int = 100,
) -> tuple[list[dict], datetime, datetime, str]:
    """Return the bounded, canonical queue-one tick set and union window."""

    by_tick: dict[str, dict] = {}
    for raw in [*existing_refs, new_ref]:
        if not isinstance(raw, dict):
            raise ValueError("coalesced tick references must be objects")
        normalized = {
            "dispatchId": str(raw["dispatchId"]),
            "tickKey": str(raw["tickKey"]),
            "scheduledFor": _parse_frozen_datetime(
                str(raw["scheduledFor"]),
                field_name="scheduledFor",
            ).isoformat(),
            "windowStart": _parse_frozen_datetime(
                str(raw["windowStart"]),
                field_name="windowStart",
            ).isoformat(),
            "windowEnd": _parse_frozen_datetime(
                str(raw["windowEnd"]),
                field_name="windowEnd",
            ).isoformat(),
        }
        if normalized["windowStart"] >= normalized["windowEnd"]:
            raise ValueError("coalesced tick window must be increasing")
        prior = by_tick.get(normalized["tickKey"])
        if prior is not None and prior != normalized:
            raise ValueError("a tick key cannot identify different material")
        by_tick[normalized["tickKey"]] = normalized
    refs = sorted(
        by_tick.values(),
        key=lambda row: (row["scheduledFor"], row["tickKey"]),
    )
    if not refs or len(refs) > max_ticks:
        raise ValueError("queue-one tick set exceeds its bounded contract")
    starts = [
        _parse_frozen_datetime(row["windowStart"], field_name="windowStart")
        for row in refs
    ]
    ends = [
        _parse_frozen_datetime(row["windowEnd"], field_name="windowEnd")
        for row in refs
    ]
    return refs, min(starts), max(ends), _request_hash(refs)


def _canonical_schedule_target_ids(values) -> list[str]:
    if not isinstance(values, list) or not 1 <= len(values) <= 20:
        raise ValueError("schedule target_ids must contain between 1 and 20 UUIDs")
    try:
        normalized = [str(UUID(str(value))) for value in values]
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError("schedule target_ids must contain valid UUIDs") from exc
    if len(normalized) != len(set(normalized)):
        raise ValueError("schedule target_ids cannot contain duplicates")
    return sorted(normalized)


def _canonical_activation_refs(values) -> list[dict]:
    if not isinstance(values, list):
        raise ValueError("auto-publish activation refs must be an array")
    try:
        normalized = [
            {
                "targetId": str(UUID(str(row["targetId"]))),
                "targetSnapshotId": str(UUID(str(row["targetSnapshotId"]))),
                "activationId": str(UUID(str(row["activationId"]))),
                "version": int(row["version"]),
                "activationHash": str(row["activationHash"]),
            }
            for row in values
        ]
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise ValueError("auto-publish activation refs are invalid") from exc
    if any(row["version"] < 1 for row in normalized):
        raise ValueError("auto-publish activation versions must be positive")
    if any(
        len(row["activationHash"]) != 64
        or any(char not in "0123456789abcdef" for char in row["activationHash"])
        for row in normalized
    ):
        raise ValueError("auto-publish activation hashes must be SHA-256 digests")
    result = sorted(normalized, key=lambda row: (row["targetId"], row["activationId"]))
    if len({row["targetId"] for row in result}) != len(result):
        raise ValueError("each schedule target requires exactly one activation ref")
    return result


def build_schedule_dispatch_material(
    schedule: Schedule,
    *,
    dispatch_id,
    scheduled_for: datetime,
    using: str = "default",
) -> dict:
    """Resolve one tick exclusively from locked, server-owned material."""

    if not transaction.get_connection(using).in_atomic_block:
        raise RuntimeError("schedule material resolution requires a transaction")
    from apps.publishing.models import ApprovalMode, PublicationTarget
    from apps.publishing.services import (
        _lock_target_intent_fences,
        _normalized_validation_refs,
        _validated_auto_target_material_eligible,
    )
    from apps.topics.services import (
        approved_registry_material,
        approved_topic_policy_material,
    )

    scheduled_for = scheduled_for.astimezone(UTC)
    target_ids = _canonical_schedule_target_ids(schedule.target_ids)
    _lock_target_intent_fences(target_ids)
    target_rows = list(
        PublicationTarget.objects.using(using)
        .select_for_update()
        .select_related("canary_target")
        .filter(id__in=target_ids)
        .order_by("id")
    )
    if len(target_rows) != len(target_ids):
        raise ValueError("schedule references a publication target that does not exist")
    target_refs = []
    for target in target_rows:
        if target.current_snapshot_id is None or not target.current_config_hash:
            raise ValueError("schedule target has no current operational snapshot")
        target_refs.append(
            {
                "targetId": str(target.id),
                "targetSnapshotId": str(target.current_snapshot_id),
                "targetConfigHash": target.current_config_hash,
                "channel": target.channel,
                "role": target.role,
                "environment": target.environment,
                "credentialVersion": target.credential_version,
            }
        )
    registry = approved_registry_material(
        schedule.topic_code,
        using=using,
        for_update=True,
    )
    topic_policy = approved_topic_policy_material(
        schedule.topic_code,
        using=using,
        for_update=True,
    )
    if schedule.updated_by_id is None:
        raise ValueError("an enabled schedule requires an owning administrator")
    if schedule.approval_mode == ApprovalMode.MANUAL:
        if schedule.auto_publish_validation_refs or schedule.auto_publish_activation_refs:
            raise ValueError("manual schedules cannot freeze auto-publish refs")
        validation_refs: list[dict] = []
        activation_refs: list[dict] = []
    elif schedule.approval_mode == ApprovalMode.VALIDATED_AUTO:
        try:
            validation_refs = _normalized_validation_refs(
                schedule.auto_publish_validation_refs
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("schedule validation refs are invalid") from exc
        activation_refs = _canonical_activation_refs(
            schedule.auto_publish_activation_refs
        )
        if {row["targetId"] for row in validation_refs} != set(target_ids):
            raise ValueError("schedule validation refs do not match its targets")
        if {row["targetId"] for row in activation_refs} != set(target_ids):
            raise ValueError("schedule activation refs do not match its targets")
        validation_manifest_hash = sha256_hex(validation_refs)
        activations_by_target = {
            row["targetId"]: row for row in activation_refs
        }
        for target in target_rows:
            if not _validated_auto_target_material_eligible(
                target=target,
                supplied_validation_refs=validation_refs,
                supplied_validation_manifest_hash=validation_manifest_hash,
                activation_ref=activations_by_target[str(target.id)],
                using=using,
            ):
                raise ValueError(
                    "schedule auto-publish material is not currently eligible"
                )
    else:
        raise ValueError("schedule approval_mode is invalid")
    window_end = scheduled_for
    window_start = window_end - timedelta(minutes=schedule.window_minutes)
    material = {
        "schemaVersion": SCHEDULE_DISPATCH_MATERIAL_VERSION,
        "scheduleDispatchId": str(dispatch_id),
        "scheduleId": str(schedule.id),
        "scheduleVersion": schedule.version,
        "scheduleConfigHash": _schedule_config_hash(schedule),
        "scheduledFor": scheduled_for.isoformat(),
        "topic": schedule.topic_code,
        "approvalMode": schedule.approval_mode,
        "overlapPolicy": schedule.overlap_policy,
        "requestedById": str(schedule.updated_by_id),
        "registry": registry,
        "topicPolicy": topic_policy,
        "targetSnapshots": target_refs,
        "validationRefs": validation_refs,
        "activationRefs": activation_refs,
        "windowStart": window_start.isoformat(),
        "windowEnd": window_end.isoformat(),
    }
    return material


def _tick_ref_from_material(material: dict) -> dict:
    return {
        "dispatchId": str(material["scheduleDispatchId"]),
        "tickKey": str(material["tickKey"]),
        "scheduledFor": str(material["scheduledFor"]),
        "windowStart": str(material["windowStart"]),
        "windowEnd": str(material["windowEnd"]),
    }


def _schedule_execution_material(dispatch: ScheduleDispatch) -> dict:
    material = dispatch.schedule_material
    if (
        dispatch.material_version != SCHEDULE_DISPATCH_MATERIAL_VERSION
        or not isinstance(material, dict)
        or material.get("schemaVersion") != SCHEDULE_DISPATCH_MATERIAL_VERSION
        or dispatch.schedule_material_hash != _request_hash(material)
    ):
        raise ValueError("schedule dispatch material is not verifiable")
    refs, window_start, window_end, manifest_hash = _coalesced_tick_union(
        [],
        dispatch.coalesced_tick_refs[0],
    )
    for ref in dispatch.coalesced_tick_refs[1:]:
        refs, window_start, window_end, manifest_hash = _coalesced_tick_union(
            refs,
            ref,
        )
    if (
        manifest_hash != dispatch.coalesced_tick_manifest_hash
        or window_start != dispatch.window_start
        or window_end != dispatch.window_end
    ):
        raise ValueError("schedule queue-one tick union is inconsistent")
    return {
        "schemaVersion": SCHEDULE_EXECUTION_MATERIAL_VERSION,
        "scheduleDispatchId": str(dispatch.id),
        "scheduleId": str(dispatch.schedule_id),
        "scheduleVersion": dispatch.schedule_version,
        "scheduleConfigHash": material["scheduleConfigHash"],
        "scheduledFor": material["scheduledFor"],
        "topic": material["topic"],
        "approvalMode": dispatch.approval_mode_snapshot,
        "requestedById": str(dispatch.requested_by_id),
        "registry": material["registry"],
        "topicPolicy": material["topicPolicy"],
        "targetSnapshots": dispatch.target_snapshot_refs,
        "validationRefs": dispatch.validation_refs,
        "activationRefs": dispatch.activation_refs,
        "tickRefs": refs,
        "tickManifestHash": manifest_hash,
        "tickSetVersion": dispatch.tick_set_version,
        "windowStart": window_start.isoformat(),
        "windowEnd": window_end.isoformat(),
    }


def _create_run_for_schedule_dispatch(
    dispatch: ScheduleDispatch,
    *,
    audit_context: AuditContext,
) -> CollectionRun:
    execution = _schedule_execution_material(dispatch)
    execution_hash = _request_hash(execution)
    registry = execution["registry"]
    policy = execution["topicPolicy"]
    run, _created = create_run(
        topic_code=execution["topic"],
        window_start=_parse_frozen_datetime(
            execution["windowStart"],
            field_name="windowStart",
        ),
        window_end=_parse_frozen_datetime(
            execution["windowEnd"],
            field_name="windowEnd",
        ),
        user=dispatch.requested_by,
        trigger="schedule",
        correlation_id=audit_context.correlation_id,
        requested_target_ids=[
            row["targetId"] for row in execution["targetSnapshots"]
        ],
        approval_mode=execution["approvalMode"],
        execution_material_hash=execution_hash,
        source_registry_id=registry["snapshotId"],
        expected_registry_manifest_hash=registry["manifestHash"],
        topic_policy_id=policy["id"],
        expected_policy_version=int(policy["version"]),
        expected_policy_hash=policy["policyHash"],
    )
    dispatch.execution_material = execution
    dispatch.execution_material_hash = execution_hash
    dispatch.run_request_fingerprint = run.request_fingerprint
    dispatch.collection_run = run
    dispatch.state = ScheduleDispatch.State.DISPATCHED
    dispatch.dispatched_at = timezone.now()
    return run


def _lock_frozen_schedule_sources(
    dispatch: ScheduleDispatch,
    *,
    using: str,
) -> dict:
    """Lock frozen registry/policy before any CollectionRun row lock."""

    from apps.topics.models import SourceRegistrySnapshot, TopicPolicy
    from apps.topics.services import registry_manifest_hash

    execution = _schedule_execution_material(dispatch)
    registry_ref = execution["registry"]
    policy_ref = execution["topicPolicy"]
    registry = (
        SourceRegistrySnapshot.objects.using(using)
        .select_for_update()
        .filter(
            pk=registry_ref["snapshotId"],
            topic_code=execution["topic"],
        )
        .first()
    )
    policy = (
        TopicPolicy.objects.using(using)
        .select_for_update()
        .filter(
            pk=policy_ref["id"],
            code=execution["topic"],
        )
        .first()
    )
    if (
        registry is None
        or registry.manifest_hash != registry_ref["manifestHash"]
        or registry_manifest_hash(registry, using=using)
        != registry_ref["manifestHash"]
        or policy is None
        or policy.version != int(policy_ref["version"])
        or policy.policy_hash != policy_ref["policyHash"]
        or _request_hash(policy.policy) != policy.policy_hash
    ):
        raise ValueError("frozen schedule registry or topic policy is inconsistent")
    return execution


def _collection_run_material(run: CollectionRun | None) -> dict | None:
    if run is None:
        return None
    return {
        "run_id": str(run.id),
        "topic_code": run.topic_code,
        "window_start": run.window_start.isoformat(),
        "window_end": run.window_end.isoformat(),
        "trigger": run.trigger,
        "state": run.state,
        "requested_target_ids": run.requested_target_ids,
        "approval_mode": run.approval_mode,
    }


def _control_material(control: OperationalControl) -> dict:
    return {
        "schema_version": "kill-switch-audit-v1",
        "key": control.key,
        "enabled": control.enabled,
        "version": control.version,
        "reason_hash": _request_hash(
            {
                "schema_version": "kill-switch-reason-v1",
                "reason": control.reason,
            }
        ),
        "updated_by_id": (
            str(control.updated_by_id) if control.updated_by_id else None
        ),
    }


def _audit_for_request(
    *,
    alias: str,
    action: str,
    entity,
    request_key: str,
) -> AuditEvent | None:
    return AuditEvent.objects.using(alias).filter(
        id=audit_event_id(
            action=action,
            entity=entity,
            identity_key=request_key,
        )
    ).first()


def _require_admin_context(
    *,
    audit_context: AuditContext,
    user,
) -> None:
    if (
        audit_context.actor_type != AuditEvent.ActorType.ADMIN
        or str(audit_context.actor_id) != str(user.pk)
    ):
        raise ValueError("audit actor does not match the schedule administrator")
    if not audit_context.request_key:
        raise ValueError("request_key is required")
    if audit_context.reason_code is None:
        raise ValueError("reason is required")


def _schedule_create_id(audit_context: AuditContext) -> uuid.UUID:
    return uuid.uuid5(
        _SCHEDULE_ID_NAMESPACE,
        _request_hash(
            {
                "schema_version": "schedule-create-identity-v1",
                "request_key": audit_context.request_key,
            }
        ),
    )


def create_schedule(
    *,
    values: dict,
    user,
    audit_context: AuditContext,
) -> tuple[Schedule, bool]:
    _require_admin_context(audit_context=audit_context, user=user)
    alias = audit_context.database_alias
    schedule_id = _schedule_create_id(audit_context)
    payload_hash = _request_hash(
        {
            "schema_version": "schedule-create-request-v1",
            "values": values,
            "request_key": audit_context.request_key,
            "reason": audit_context.reason_code,
            "actor_id": str(user.pk),
        }
    )
    with transaction.atomic(using=alias):
        (
            get_user_model()
            .objects.using(alias)
            .select_for_update()
            .get(pk=user.pk)
        )
        existing = (
            Schedule.objects.using(alias)
            .select_for_update()
            .filter(pk=schedule_id)
            .first()
        )
        if existing is not None:
            replay = _audit_for_request(
                alias=alias,
                action="schedule.created",
                entity=existing,
                request_key=audit_context.request_key,
            )
            if replay is None:
                raise ValueError(
                    "request_key was already used with a different schedule create request"
                )
            require_audit_replay(
                context=audit_context,
                action="schedule.created",
                entity=existing,
                identity_key=audit_context.request_key,
                request_hash=payload_hash,
            )
            return existing, False

        schedule = Schedule(
            id=schedule_id,
            name=values["name"],
            topic_code=values["topic_code"],
            cron_expression=values["cron_expression"],
            timezone=values.get("timezone", "Asia/Seoul"),
            window_minutes=values.get("window_minutes", 1440),
            target_ids=values.get("target_ids", []),
            approval_mode=values.get("approval_mode", "manual"),
            auto_publish_validation_refs=values.get(
                "auto_publish_validation_refs", []
            ),
            auto_publish_activation_refs=values.get(
                "auto_publish_activation_refs", []
            ),
            overlap_policy=values.get("overlap_policy", "skip"),
            enabled=values.get("enabled", False),
            updated_by=user,
        )
        schedule.next_run_at = calculate_next_run(schedule, timezone.now())
        schedule.save(using=alias)
        record_audit_event(
            context=audit_context,
            action="schedule.created",
            entity=schedule,
            identity_key=audit_context.request_key,
            material_schema_version="schedule-audit-v1",
            before_material={"state": "not_created"},
            after_material=_schedule_material(schedule),
            metadata={
                "request_hash": payload_hash,
                "result": "created",
                "schedule_id": str(schedule.id),
                "schedule_version": schedule.version,
                "enabled": schedule.enabled,
            },
        )
        return schedule, True


def update_schedule(
    *,
    schedule_id,
    changes: dict,
    user,
    audit_context: AuditContext,
) -> tuple[Schedule, bool]:
    _require_admin_context(audit_context=audit_context, user=user)
    alias = audit_context.database_alias
    payload_hash = _request_hash(
        {
            "schema_version": "schedule-update-request-v1",
            "schedule_id": str(schedule_id),
            "changes": changes,
            "request_key": audit_context.request_key,
            "reason": audit_context.reason_code,
            "actor_id": str(user.pk),
        }
    )
    with transaction.atomic(using=alias):
        schedule = (
            Schedule.objects.using(alias)
            .select_for_update()
            .get(pk=schedule_id)
        )
        replay = _audit_for_request(
            alias=alias,
            action="schedule.updated",
            entity=schedule,
            request_key=audit_context.request_key,
        )
        if replay is not None:
            require_audit_replay(
                context=audit_context,
                action="schedule.updated",
                entity=schedule,
                identity_key=audit_context.request_key,
                request_hash=payload_hash,
            )
            return schedule, False

        before_material = _schedule_material(schedule)
        for field, value in changes.items():
            setattr(schedule, field, value)
        schedule.version += 1
        schedule.updated_by = user
        schedule.next_run_at = calculate_next_run(schedule)
        schedule.save(using=alias)
        record_audit_event(
            context=audit_context,
            action="schedule.updated",
            entity=schedule,
            identity_key=audit_context.request_key,
            material_schema_version="schedule-audit-v1",
            before_material=before_material,
            after_material=_schedule_material(schedule),
            metadata={
                "request_hash": payload_hash,
                "result": "updated",
                "schedule_id": str(schedule.id),
                "schedule_version": schedule.version,
                "enabled": schedule.enabled,
            },
        )
        return schedule, True


def disable_schedule(
    *,
    schedule_id,
    user,
    audit_context: AuditContext,
) -> tuple[Schedule, bool]:
    _require_admin_context(audit_context=audit_context, user=user)
    alias = audit_context.database_alias
    payload_hash = _request_hash(
        {
            "schema_version": "schedule-disable-request-v1",
            "schedule_id": str(schedule_id),
            "request_key": audit_context.request_key,
            "reason": audit_context.reason_code,
            "actor_id": str(user.pk),
        }
    )
    with transaction.atomic(using=alias):
        schedule = (
            Schedule.objects.using(alias)
            .select_for_update()
            .get(pk=schedule_id)
        )
        replay = _audit_for_request(
            alias=alias,
            action="schedule.disabled",
            entity=schedule,
            request_key=audit_context.request_key,
        )
        if replay is not None:
            require_audit_replay(
                context=audit_context,
                action="schedule.disabled",
                entity=schedule,
                identity_key=audit_context.request_key,
                request_hash=payload_hash,
            )
            return schedule, False

        before_material = _schedule_material(schedule)
        schedule.enabled = False
        schedule.version += 1
        schedule.updated_by = user
        schedule.save(
            update_fields=("enabled", "version", "updated_by", "updated_at"),
            using=alias,
        )
        record_audit_event(
            context=audit_context,
            action="schedule.disabled",
            entity=schedule,
            identity_key=audit_context.request_key,
            material_schema_version="schedule-audit-v1",
            before_material=before_material,
            after_material=_schedule_material(schedule),
            metadata={
                "request_hash": payload_hash,
                "result": "disabled",
                "schedule_id": str(schedule.id),
                "schedule_version": schedule.version,
                "enabled": False,
            },
        )
        return schedule, True


def calculate_next_run(schedule: Schedule, after: datetime | None = None) -> datetime:
    after = after or timezone.now()
    local = after.astimezone(ZoneInfo(schedule.timezone))
    value = croniter(schedule.cron_expression, local).get_next(datetime)
    return value.astimezone(UTC)


def _sqlite_busy(exc: OperationalError) -> bool:
    message = str(exc).lower()
    return any(
        token in message
        for token in (
            "database is locked",
            "database table is locked",
            "database schema is locked",
        )
    )


def dispatch_schedule(
    schedule_id,
    scheduled_for: datetime | None = None,
    *,
    audit_context: AuditContext,
):
    if audit_context.actor_type != AuditEvent.ActorType.SYSTEM:
        raise ValueError("scheduled dispatch requires explicit system provenance")
    last_race: IntegrityError | OperationalError | None = None
    for operation_attempt in range(4):
        try:
            return _dispatch_schedule_atomic(
                schedule_id,
                scheduled_for=scheduled_for,
                audit_context=audit_context,
            )
        except IntegrityError as exc:
            last_race = exc
        except OperationalError as exc:
            if not _sqlite_busy(exc):
                raise
            last_race = exc
        if operation_attempt < 3:
            time.sleep(0.01 * (2**operation_attempt))
    raise AuditIdentityConflict(
        "schedule tick could not settle a concurrent dispatcher"
    ) from last_race


def _dispatch_schedule_atomic(
    schedule_id,
    scheduled_for: datetime | None = None,
    *,
    audit_context: AuditContext,
):
    alias = audit_context.database_alias
    with transaction.atomic(using=alias):
        schedule = (
            Schedule.objects.using(alias)
            .select_for_update()
            .get(id=schedule_id)
        )
        scheduled_for = scheduled_for or schedule.next_run_at or timezone.now()
        if timezone.is_naive(scheduled_for):
            raise ValueError("scheduled_for must include a timezone")
        scheduled_for = scheduled_for.astimezone(UTC)
        tick_key = f"{schedule.id}:{scheduled_for.isoformat()}"
        existing = (
            ScheduleDispatch.objects.using(alias)
            .select_for_update()
            .filter(schedule=schedule, scheduled_for=scheduled_for)
            .first()
        )
        if existing is not None:
            for replay_action in (
                "schedule_dispatch.skipped",
                "schedule_dispatch.queued",
                "schedule_dispatch.coalesced",
                "schedule_dispatch.dispatched",
            ):
                expected_audit_id = audit_event_id(
                    action=replay_action,
                    entity=existing,
                    identity_key=existing.tick_key,
                )
                if not AuditEvent.objects.using(alias).filter(
                    id=expected_audit_id
                ).exists():
                    continue
                require_audit_replay(
                    context=audit_context,
                    action=replay_action,
                    entity=existing,
                    identity_key=existing.tick_key,
                )
                return existing
            raise AuditIdentityConflict(
                "existing schedule tick has no immutable initial audit event"
            )
        if not schedule.enabled:
            return None
        external_writes_blocked = is_external_write_blocked(using=alias)
        schedule_before_material = _schedule_material(schedule)
        window_end = scheduled_for
        window_start = window_end - timedelta(minutes=schedule.window_minutes)
        dispatch_id = uuid.uuid5(
            _SCHEDULE_DISPATCH_ID_NAMESPACE,
            tick_key,
        )
        frozen = build_schedule_dispatch_material(
            schedule,
            dispatch_id=dispatch_id,
            scheduled_for=scheduled_for,
            using=alias,
        )
        frozen["tickKey"] = tick_key
        tick_ref = _tick_ref_from_material(frozen)
        tick_refs, union_start, union_end, tick_manifest_hash = (
            _coalesced_tick_union([], tick_ref)
        )
        registry = frozen["registry"]
        topic_policy = frozen["topicPolicy"]
        dispatch = ScheduleDispatch(
            id=dispatch_id,
            schedule=schedule,
            schedule_version=schedule.version,
            scheduled_for=scheduled_for,
            tick_key=tick_key,
            material_version=SCHEDULE_DISPATCH_MATERIAL_VERSION,
            schedule_material=frozen,
            schedule_material_hash=_request_hash(frozen),
            source_registry_id=registry["snapshotId"],
            registry_manifest_hash=registry["manifestHash"],
            topic_policy_id=topic_policy["id"],
            topic_policy_version=int(topic_policy["version"]),
            topic_policy_hash=topic_policy["policyHash"],
            target_snapshot_refs=frozen["targetSnapshots"],
            approval_mode_snapshot=frozen["approvalMode"],
            validation_refs=frozen["validationRefs"],
            activation_refs=frozen["activationRefs"],
            requested_by=schedule.updated_by,
            state=ScheduleDispatch.State.SKIPPED,
            coalesced_tick_refs=tick_refs,
            coalesced_tick_manifest_hash=tick_manifest_hash,
            tick_set_version=1,
            window_start=union_start,
            window_end=union_end,
        )
        created_run = None
        if external_writes_blocked:
            dispatch.reason_code = "global_kill_switch"
        else:
            active = (
                CollectionRun.objects.using(alias)
                .select_for_update()
                .filter(topic_code=frozen["topic"])
                .exclude(state__in=TERMINAL_RUN_STATES)
            )
            if active.exists():
                if frozen["overlapPolicy"] == Schedule.OverlapPolicy.SKIP:
                    dispatch.reason_code = "active_run"
                else:
                    pending = (
                        ScheduleDispatch.objects.using(alias)
                        .select_for_update()
                        .filter(
                            schedule=schedule,
                            state=ScheduleDispatch.State.QUEUED
                        )
                        .order_by("scheduled_for")
                        .first()
                    )
                    if pending:
                        (
                            pending.coalesced_tick_refs,
                            pending.window_start,
                            pending.window_end,
                            pending.coalesced_tick_manifest_hash,
                        ) = _coalesced_tick_union(
                            pending.coalesced_tick_refs,
                            tick_ref,
                        )
                        pending.tick_set_version += 1
                        pending.save(
                            update_fields=(
                                "coalesced_tick_refs",
                                "coalesced_tick_manifest_hash",
                                "tick_set_version",
                                "window_start",
                                "window_end",
                            ),
                            using=alias,
                        )
                        dispatch.state = ScheduleDispatch.State.COALESCED
                        dispatch.coalesced_into = pending
                        dispatch.reason_code = "coalesced"
                    else:
                        dispatch.state = ScheduleDispatch.State.QUEUED
                        dispatch.reason_code = "waiting_for_active_run"
            else:
                run = _create_run_for_schedule_dispatch(
                    dispatch,
                    audit_context=audit_context,
                )
                if run._state.db != alias:
                    raise ValueError(
                        "schedule run and audit database aliases differ"
                    )
                created_run = run
                enqueue_event(
                    event_type="run.requested",
                    aggregate_type="collection_run",
                    aggregate_id=run.id,
                    job_id=run.id,
                    dedupe_key=f"run.requested:{run.id}",
                    correlation_id=audit_context.correlation_id,
                    payload={"run_id": str(run.id)},
                )
        dispatch.save(using=alias)
        schedule.last_dispatched_at = scheduled_for
        schedule.next_run_at = calculate_next_run(schedule, scheduled_for)
        schedule.save(
            update_fields=["last_dispatched_at", "next_run_at"],
            using=alias,
        )
        action = {
            ScheduleDispatch.State.SKIPPED: "schedule_dispatch.skipped",
            ScheduleDispatch.State.QUEUED: "schedule_dispatch.queued",
            ScheduleDispatch.State.COALESCED: "schedule_dispatch.coalesced",
            ScheduleDispatch.State.DISPATCHED: "schedule_dispatch.dispatched",
        }[dispatch.state]
        record_audit_event(
            context=audit_context,
            action=action,
            entity=dispatch,
            identity_key=tick_key,
            material_schema_version="schedule-dispatch-audit-v1",
            before_material={
                "dispatch": {
                    "state": "not_dispatched",
                    "tick_key": tick_key,
                },
                "schedule": schedule_before_material,
                "collectionRun": None,
            },
            after_material={
                "dispatch": _dispatch_material(dispatch),
                "schedule": _schedule_material(schedule),
                "collectionRun": _collection_run_material(created_run),
            },
            metadata={
                "result": dispatch.state,
                "state": dispatch.state,
                "reason_code": dispatch.reason_code,
                "dispatch_id": str(dispatch.id),
                "schedule_id": str(schedule.id),
                "schedule_version": dispatch.schedule_version,
                "collection_run_id": (
                    str(dispatch.collection_run_id)
                    if dispatch.collection_run_id
                    else None
                ),
                "coalesced_into_id": (
                    str(dispatch.coalesced_into_id)
                    if dispatch.coalesced_into_id
                    else None
                ),
            },
        )
        return dispatch


def dispatch_due_schedules(
    now: datetime | None = None,
    *,
    audit_context: AuditContext,
):
    now = now or timezone.now()
    alias = audit_context.database_alias
    due_ticks = list(
        Schedule.objects.using(alias)
        .filter(enabled=True, next_run_at__lte=now)
        .values_list("id", "next_run_at")
    )
    return [
        dispatch_schedule(
            schedule_id,
            scheduled_for=scheduled_for,
            audit_context=audit_context,
        )
        for schedule_id, scheduled_for in due_ticks
    ]


def release_queued_dispatch(
    schedule_id,
    *,
    audit_context: AuditContext,
):
    """Start the single coalesced tick after the active topic run reaches a terminal state."""
    if audit_context.actor_type != AuditEvent.ActorType.SYSTEM:
        raise ValueError(
            "queued schedule release requires explicit system provenance"
        )
    last_race: IntegrityError | OperationalError | None = None
    for operation_attempt in range(4):
        try:
            return _release_queued_dispatch_atomic(
                schedule_id,
                audit_context=audit_context,
            )
        except IntegrityError as exc:
            last_race = exc
        except OperationalError as exc:
            if not _sqlite_busy(exc):
                raise
            last_race = exc
        if operation_attempt < 3:
            time.sleep(0.01 * (2**operation_attempt))
    raise AuditIdentityConflict(
        "queued schedule release could not settle a concurrent dispatcher"
    ) from last_race


def _release_queued_dispatch_atomic(
    schedule_id,
    *,
    audit_context: AuditContext,
):
    alias = audit_context.database_alias
    with transaction.atomic(using=alias):
        schedule = (
            Schedule.objects.using(alias)
            .select_for_update()
            .get(id=schedule_id)
        )
        dispatch = (
            schedule.dispatches.select_for_update()
            .filter(state=ScheduleDispatch.State.QUEUED)
            .order_by("scheduled_for")
            .first()
        )
        if dispatch is None:
            released_rows = list(
                schedule.dispatches.select_for_update()
                .filter(
                    state=ScheduleDispatch.State.DISPATCHED,
                    reason_code="released_after_terminal_run",
                )
                .order_by("-dispatched_at", "-id")
            )
            for released in released_rows:
                replay_identity = _request_hash(
                    {
                        "schema_version": "schedule-release-identity-v1",
                        "dispatch_id": str(released.id),
                        "event_key": audit_context.event_key,
                        "operation_key": audit_context.operation_key,
                    }
                )
                expected_audit_id = audit_event_id(
                    action="schedule_dispatch.released",
                    entity=released,
                    identity_key=replay_identity,
                )
                if not AuditEvent.objects.using(alias).filter(
                    id=expected_audit_id
                ).exists():
                    continue
                require_audit_replay(
                    context=audit_context,
                    action="schedule_dispatch.released",
                    entity=released,
                    identity_key=replay_identity,
                )
                return released
            return None
        if not schedule.enabled:
            return dispatch
        if (
            dispatch.material_version != SCHEDULE_DISPATCH_MATERIAL_VERSION
            or not isinstance(dispatch.schedule_material, dict)
            or dispatch.schedule_material.get("schemaVersion")
            != SCHEDULE_DISPATCH_MATERIAL_VERSION
        ):
            if dispatch.reason_code != "legacy_unverifiable_material":
                dispatch.reason_code = "legacy_unverifiable_material"
                dispatch.save(update_fields=["reason_code"], using=alias)
            return dispatch
        external_writes_blocked = is_external_write_blocked(
            using=alias
        )
        before_material = _dispatch_material(dispatch)
        identity_key = _request_hash(
            {
                "schema_version": "schedule-release-identity-v1",
                "dispatch_id": str(dispatch.id),
                "event_key": audit_context.event_key,
                "operation_key": audit_context.operation_key,
            }
        )
        if external_writes_blocked:
            if dispatch.reason_code == "global_kill_switch":
                return dispatch
            dispatch.reason_code = "global_kill_switch"
            dispatch.save(update_fields=["reason_code"], using=alias)
            record_audit_event(
                context=audit_context,
                action="schedule_dispatch.skipped",
                entity=dispatch,
                identity_key=identity_key,
                material_schema_version="schedule-dispatch-audit-v1",
                before_material={
                    "dispatch": before_material,
                    "collectionRun": None,
                },
                after_material={
                    "dispatch": _dispatch_material(dispatch),
                    "collectionRun": None,
                },
                metadata={
                    "result": "release_blocked",
                    "state": dispatch.state,
                    "reason_code": dispatch.reason_code,
                    "dispatch_id": str(dispatch.id),
                    "schedule_id": str(schedule.id),
                    "schedule_version": dispatch.schedule_version,
                },
            )
            return dispatch
        execution_material = _lock_frozen_schedule_sources(
            dispatch,
            using=alias,
        )
        active = (
            CollectionRun.objects.using(alias)
            .select_for_update()
            .filter(topic_code=execution_material["topic"])
            .exclude(state__in=TERMINAL_RUN_STATES)
        )
        if active.exists():
            return dispatch
        run = _create_run_for_schedule_dispatch(
            dispatch,
            audit_context=audit_context,
        )
        if run._state.db != alias:
            raise ValueError("schedule run and audit database aliases differ")
        dispatch.reason_code = "released_after_terminal_run"
        dispatch.save(
            update_fields=[
                "collection_run",
                "state",
                "reason_code",
                "execution_material",
                "execution_material_hash",
                "run_request_fingerprint",
                "dispatched_at",
            ],
            using=alias,
        )
        enqueue_event(
            event_type="run.requested",
            aggregate_type="collection_run",
            aggregate_id=run.id,
            job_id=run.id,
            dedupe_key=f"run.requested:{run.id}",
            correlation_id=audit_context.correlation_id,
            payload={"run_id": str(run.id)},
        )
        record_audit_event(
            context=audit_context,
            action="schedule_dispatch.released",
            entity=dispatch,
            identity_key=identity_key,
            material_schema_version="schedule-dispatch-audit-v1",
            before_material={
                "dispatch": before_material,
                "collectionRun": None,
            },
            after_material={
                "dispatch": _dispatch_material(dispatch),
                "collectionRun": _collection_run_material(run),
            },
            metadata={
                "result": "released",
                "state": dispatch.state,
                "reason_code": dispatch.reason_code,
                "dispatch_id": str(dispatch.id),
                "schedule_id": str(schedule.id),
                "schedule_version": dispatch.schedule_version,
                "collection_run_id": str(run.id),
            },
        )
        return dispatch


def release_waiting_for_topic(
    topic_code: str,
    *,
    audit_context: AuditContext,
) -> None:
    alias = audit_context.database_alias
    schedule_ids = list(
        Schedule.objects.using(alias).filter(
            dispatches__state=ScheduleDispatch.State.QUEUED,
            dispatches__source_registry__topic_code=topic_code,
        )
        .distinct()
        .values_list("id", flat=True)
    )
    for schedule_id in schedule_ids:
        release_queued_dispatch(
            schedule_id,
            audit_context=audit_context,
        )


def set_kill_switch(
    *,
    enabled: bool,
    expected_version: int,
    request_key: str,
    reason: str,
    user,
    request=None,
    reauth_proof_id=None,
    audit_context: AuditContext,
):
    _require_admin_context(audit_context=audit_context, user=user)
    if audit_context.request_key != request_key:
        raise ValueError("audit request_key does not match kill-switch request")
    if audit_context.reason_code != reason:
        raise ValueError("audit reason does not match kill-switch request")

    alias = audit_context.database_alias
    request_hash = _request_hash(
        {
            "schema_version": "kill-switch-request-v1",
            "enabled": enabled,
            "expected_version": expected_version,
            "request_key": request_key,
            "reason": reason,
            "actor_id": str(user.pk),
        }
    )
    with transaction.atomic(using=alias):
        existing = (
            KillSwitchDecision.objects.using(alias)
            .select_for_update()
            .filter(request_key=request_key)
            .first()
        )
        if existing:
            if (
                existing.enabled != enabled
                or existing.expected_version != expected_version
                or existing.reason != reason
                or existing.decided_by_id != user.pk
            ):
                raise ValueError("request_key_conflict")
            replay = _audit_for_request(
                alias=alias,
                action="kill_switch.decided",
                entity=existing,
                request_key=request_key,
            )
            if replay is None:
                raise ValueError(
                    "existing kill-switch decision has no matching audit event"
                )
            require_audit_replay(
                context=audit_context,
                action="kill_switch.decided",
                entity=existing,
                identity_key=request_key,
                request_hash=request_hash,
            )
            return existing
        control, control_created = (
            OperationalControl.objects.using(alias)
            .select_for_update()
            .get_or_create(key="global_kill_switch")
        )
        if control.version != expected_version:
            raise ValueError("stale_control_version")
        before_material = (
            {
                "schema_version": "kill-switch-audit-v1",
                "state": "not_created",
            }
            if control_created
            else _control_material(control)
        )
        if not enabled:
            if request is None or not reauth_proof_id:
                raise ValueError("reauthentication_required")
            consume_reauthentication_proof(
                request=request,
                proof_id=reauth_proof_id,
                action_scope="kill_switch_disable",
                entity_type="operational_control",
                entity_id=UUID(
                    "00000000-0000-4000-8000-000000000001"
                ),
            )
        decision = KillSwitchDecision.objects.using(alias).create(
            expected_version=expected_version,
            enabled=enabled,
            request_key=request_key,
            reason=reason,
            decided_by=user,
        )
        control.enabled = enabled
        control.version += 1
        control.reason = reason
        control.updated_by = user
        control.save(using=alias)
        record_audit_event(
            context=audit_context,
            action="kill_switch.decided",
            entity=decision,
            identity_key=request_key,
            material_schema_version="kill-switch-audit-v1",
            before_material=before_material,
            after_material=_control_material(control),
            metadata={
                "request_hash": request_hash,
                "decision": "enabled" if enabled else "disabled",
                "enabled": enabled,
                "version": control.version,
                "decision_id": str(decision.id),
                "reauth_proof_id": (
                    str(reauth_proof_id) if reauth_proof_id else None
                ),
            },
        )
        return decision
