from __future__ import annotations

import os
from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4

from django.conf import settings
from django.db import connection
from django.db.models import Count, Min, Q
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.http import require_GET
from redis import Redis

from wisdome_writer.infrastructure.models import (
    OutboxConsumerReceipt,
    OutboxMessage,
)


def _no_store(response: JsonResponse) -> JsonResponse:
    response["Cache-Control"] = "no-store"
    return response


def _age_seconds(value: datetime | None, *, now: datetime) -> int | None:
    if value is None:
        return None
    return max(int((now - value).total_seconds()), 0)


def _local_root_check(root: Path) -> str:
    probe = root / f".ready-{uuid4().hex}"
    try:
        root.mkdir(parents=True, exist_ok=True)
        with probe.open("xb") as handle:
            handle.write(b"ready")
            handle.flush()
            os.fsync(handle.fileno())
        probe.unlink()
    except OSError:
        try:
            probe.unlink(missing_ok=True)
        except OSError:
            pass
        return "unavailable"
    return "ok"


def _outbox_observation() -> dict[str, int | str | None]:
    now = timezone.now()
    stale_before = now - timedelta(
        seconds=settings.OUTBOX_OBSERVABILITY_STALE_SECONDS
    )

    dispatch = OutboxMessage.objects.aggregate(
        dispatch_due=Count(
            "id",
            filter=Q(
                status=OutboxMessage.Status.PENDING,
                available_at__lte=now,
            ),
        ),
        dispatch_delayed=Count(
            "id",
            filter=Q(
                status=OutboxMessage.Status.PENDING,
                available_at__gt=now,
            ),
        ),
        dispatch_active=Count(
            "id",
            filter=Q(
                status=OutboxMessage.Status.DISPATCHING,
                claimed_until__gt=now,
            ),
        ),
        dispatch_lease_expired=Count(
            "id",
            filter=(
                Q(status=OutboxMessage.Status.DISPATCHING)
                & (
                    Q(claimed_until__isnull=True)
                    | Q(claimed_until__lte=now)
                )
            ),
        ),
        outbox_dead_letter=Count(
            "id",
            filter=Q(status=OutboxMessage.Status.DEAD_LETTER),
        ),
        oldest_due_at=Min(
            "available_at",
            filter=Q(
                status=OutboxMessage.Status.PENDING,
                available_at__lte=now,
            ),
        ),
    )
    receipts = OutboxConsumerReceipt.objects.aggregate(
        consumer_processing=Count(
            "id",
            filter=Q(
                state=OutboxConsumerReceipt.State.PROCESSING,
                claimed_until__gt=now,
            ),
        ),
        consumer_lease_expired=Count(
            "id",
            filter=(
                Q(state=OutboxConsumerReceipt.State.PROCESSING)
                & (
                    Q(claimed_until__isnull=True)
                    | Q(claimed_until__lte=now)
                )
            ),
        ),
        consumer_retry_scheduled=Count(
            "id",
            filter=Q(
                state=OutboxConsumerReceipt.State.RETRY,
                next_retry_at__gt=now,
            ),
        ),
        consumer_retry_due=Count(
            "id",
            filter=(
                Q(state=OutboxConsumerReceipt.State.RETRY)
                & (
                    Q(next_retry_at__isnull=True)
                    | Q(next_retry_at__lte=now)
                )
            ),
        ),
        consumer_retry_schedule_missing=Count(
            "id",
            filter=Q(
                state=OutboxConsumerReceipt.State.RETRY,
                next_retry_at__isnull=True,
            ),
        ),
        oldest_retry_due_at=Min(
            "next_retry_at",
            filter=Q(
                state=OutboxConsumerReceipt.State.RETRY,
                next_retry_at__isnull=False,
                next_retry_at__lte=now,
            ),
        ),
        consumer_dead_letter=Count(
            "id",
            filter=Q(state=OutboxConsumerReceipt.State.DEAD_LETTER),
        ),
    )
    published_without_receipt = (
        OutboxMessage.objects.filter(
            status=OutboxMessage.Status.PUBLISHED,
            published_at__lte=stale_before,
            consumer_receipts__isnull=True,
        )
        .distinct()
        .count()
    )

    manual_required = (
        dispatch["outbox_dead_letter"]
        + receipts["consumer_dead_letter"]
    )
    oldest_retry_due_age = _age_seconds(
        receipts["oldest_retry_due_at"],
        now=now,
    )
    broker_managed = receipts["consumer_lease_expired"]
    if receipts["consumer_retry_schedule_missing"]:
        broker_managed += receipts["consumer_retry_schedule_missing"]
    if (
        oldest_retry_due_age is not None
        and oldest_retry_due_age
        >= settings.OUTBOX_OBSERVABILITY_DUE_AGE_SECONDS
    ):
        broker_managed += receipts["consumer_retry_due"]
    oldest_due_age = _age_seconds(
        dispatch["oldest_due_at"],
        now=now,
    )
    automatic_now = dispatch["dispatch_lease_expired"]
    if (
        oldest_due_age is not None
        and oldest_due_age
        >= settings.OUTBOX_OBSERVABILITY_DUE_AGE_SECONDS
    ):
        automatic_now += dispatch["dispatch_due"]
    if manual_required:
        recovery_state = "manual_required"
    elif published_without_receipt:
        recovery_state = "unknown_delivery"
    elif broker_managed:
        recovery_state = "broker_managed"
    elif automatic_now:
        recovery_state = "automatic"
    else:
        recovery_state = "clear"

    return {
        "dispatchDue": dispatch["dispatch_due"],
        "dispatchDelayed": dispatch["dispatch_delayed"],
        "dispatchActive": dispatch["dispatch_active"],
        "dispatchLeaseExpired": dispatch["dispatch_lease_expired"],
        "publishedWithoutReceipt": published_without_receipt,
        "consumerProcessing": receipts["consumer_processing"],
        "consumerLeaseExpired": receipts["consumer_lease_expired"],
        "consumerRetryScheduled": receipts["consumer_retry_scheduled"],
        "consumerRetryDue": receipts["consumer_retry_due"],
        "outboxDeadLetter": dispatch["outbox_dead_letter"],
        "consumerDeadLetter": receipts["consumer_dead_letter"],
        "oldestDueAgeSeconds": oldest_due_age,
        "recoveryState": recovery_state,
    }


@require_GET
def live(request):
    return _no_store(
        JsonResponse({"status": "ok", "service": "wisdome-super-writer"})
    )


@require_GET
def ready(request):
    checks: dict[str, str] = {}
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
        checks["database"] = "ok"
    except Exception:
        checks["database"] = "unavailable"

    outbox: dict[str, int | str | None] | None = None
    if settings.IS_LOCAL_RUNTIME:
        for check_name, root in (
            ("local_state", settings.LOCAL_STATE_ROOT),
            ("local_articles", settings.LOCAL_ARTICLE_ROOT),
            ("local_objects", settings.LOCAL_OBJECT_ROOT),
        ):
            checks[check_name] = _local_root_check(Path(root))
    else:
        client: Redis | None = None
        try:
            client = Redis.from_url(
                settings.CELERY_BROKER_URL,
                socket_connect_timeout=1,
                socket_timeout=1,
                decode_responses=True,
            )
            checks["broker"] = "ok" if client.ping() else "unavailable"
        except Exception:
            checks["broker"] = "unavailable"
        finally:
            if client is not None:
                try:
                    client.close()
                except Exception:
                    pass

        if checks["database"] != "ok":
            checks["outbox"] = "unavailable"
        else:
            try:
                outbox = _outbox_observation()
                checks["outbox"] = "ok"
            except Exception:
                checks["outbox"] = "unavailable"

    is_ready = all(value == "ok" for value in checks.values())
    is_degraded = bool(
        outbox is not None and outbox["recoveryState"] != "clear"
    )
    payload: dict[str, object] = {
        "status": (
            "unavailable"
            if not is_ready
            else "degraded"
            if is_degraded
            else "ok"
        ),
        "checks": checks,
    }
    if outbox is not None:
        payload["outbox"] = outbox
    return _no_store(
        JsonResponse(
            payload,
            status=200 if is_ready else 503,
        )
    )


@require_GET
def api_root(request):
    return JsonResponse(
        {
            "service": "wisdome-super-writer",
            "apiVersion": "v1",
            "authenticatedAdmin": str(request.user.pk),
        }
    )

