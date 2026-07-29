from __future__ import annotations

import logging
import os
from contextvars import ContextVar
from typing import Any

from celery import Celery, signals

from wisdome_writer.observability import (
    TaskContextBinding,
    bind_task_context,
    build_observation_headers,
    current_task_retry_count,
    log_lifecycle_event,
    monotonic_elapsed_milliseconds,
    reset_task_context,
)

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "wisdome_writer.settings")

logger = logging.getLogger(__name__)
_task_bindings: ContextVar[tuple[TaskContextBinding, ...]] = ContextVar(
    "celery_task_context_bindings",
    default=(),
)


def _request_headers(request: Any) -> dict[str, Any]:
    headers = getattr(request, "headers", None)
    return headers if isinstance(headers, dict) else {}


def _stable_error_code(value: Any) -> str:
    underlying = getattr(value, "exc", None) or value
    explicit = getattr(underlying, "code", None)
    if isinstance(explicit, str) and explicit:
        return explicit
    return underlying.__class__.__name__


def _current_binding() -> TaskContextBinding | None:
    bindings = _task_bindings.get()
    return bindings[-1] if bindings else None


@signals.before_task_publish.connect(weak=False)
def attach_observation_headers(
    sender=None,
    headers=None,
    **kwargs,
) -> None:
    if not isinstance(headers, dict):
        return
    propagated = build_observation_headers(
        correlation_id=headers.get("correlation_id"),
        job_id=headers.get("job_id"),
        entity_type=headers.get("entity_type"),
        entity_id=headers.get("entity_id"),
        operation=headers.get("operation"),
        attempt=headers.get("attempt"),
    )
    headers.update(propagated)


@signals.after_task_publish.connect(weak=False)
def record_task_published(
    sender=None,
    headers=None,
    **kwargs,
) -> None:
    if not isinstance(headers, dict):
        return
    propagated = build_observation_headers(
        correlation_id=headers.get("correlation_id"),
        job_id=headers.get("job_id"),
        entity_type=headers.get("entity_type"),
        entity_id=headers.get("entity_id"),
        operation=headers.get("operation"),
        attempt=headers.get("attempt"),
    )
    log_lifecycle_event(
        logger,
        event="celery.task.published",
        correlation_id=propagated["correlation_id"],
        task_id=headers.get("id"),
        task_name=sender,
        job_id=propagated.get("job_id"),
        entity_type=propagated.get("entity_type"),
        entity_id=propagated.get("entity_id"),
        operation=propagated.get("operation"),
        attempt=propagated.get("attempt"),
        state="published",
    )


@signals.task_prerun.connect(weak=False)
def bind_worker_observation_context(
    sender=None,
    task_id=None,
    task=None,
    **kwargs,
) -> None:
    request = getattr(task, "request", None)
    headers = _request_headers(request)
    binding = bind_task_context(
        correlation_id=headers.get("correlation_id"),
        task_id=task_id,
        task_name=getattr(sender, "name", None),
        retry_count=getattr(request, "retries", 0),
        job_id=headers.get("job_id"),
        entity_type=headers.get("entity_type"),
        entity_id=headers.get("entity_id"),
        operation=headers.get("operation"),
        attempt=headers.get("attempt"),
    )
    _task_bindings.set((*_task_bindings.get(), binding))
    log_lifecycle_event(
        logger,
        event="celery.task.started",
        state="running",
    )


@signals.task_retry.connect(weak=False)
def record_task_retry(
    sender=None,
    request=None,
    reason=None,
    **kwargs,
) -> None:
    request_retry_count = getattr(request, "retries", None)
    if isinstance(request_retry_count, bool):
        request_retry_count = None
    try:
        scheduled_retry_count = (
            int(request_retry_count) + 1
            if request_retry_count is not None
            else current_task_retry_count() + 1
        )
    except (TypeError, ValueError, OverflowError):
        scheduled_retry_count = current_task_retry_count() + 1
    log_lifecycle_event(
        logger,
        event="celery.task.retry_scheduled",
        level=logging.WARNING,
        task_name=getattr(sender, "name", None),
        retry_count=scheduled_retry_count,
        state="retry",
        duration_ms=(
            monotonic_elapsed_milliseconds(binding.started_monotonic)
            if (binding := _current_binding()) is not None
            else None
        ),
        error_code=_stable_error_code(reason),
        recovery_state="broker_managed",
    )


@signals.task_failure.connect(weak=False)
def record_task_failure(
    sender=None,
    task_id=None,
    exception=None,
    **kwargs,
) -> None:
    binding = _current_binding()
    log_lifecycle_event(
        logger,
        event="celery.task.failed",
        level=logging.ERROR,
        task_id=task_id,
        task_name=getattr(sender, "name", None),
        state="failed",
        duration_ms=(
            monotonic_elapsed_milliseconds(binding.started_monotonic)
            if binding is not None
            else None
        ),
        error_code=_stable_error_code(exception),
        terminal_impact={
            "scope": "task",
            "final_state": "failed",
            "count": 1,
            "error_code": _stable_error_code(exception),
        },
        recovery_state="unknown_delivery",
    )


@signals.task_postrun.connect(weak=False)
def release_worker_observation_context(
    sender=None,
    task_id=None,
    state=None,
    **kwargs,
) -> None:
    bindings = _task_bindings.get()
    if not bindings:
        return
    binding = bindings[-1]
    try:
        log_lifecycle_event(
            logger,
            event="celery.task.completed",
            task_id=task_id,
            task_name=getattr(sender, "name", None),
            state=str(state or "unknown").lower(),
            duration_ms=monotonic_elapsed_milliseconds(
                binding.started_monotonic
            ),
        )
    finally:
        reset_task_context(binding)
        _task_bindings.set(bindings[:-1])


app = Celery("wisdome_writer")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()
app.autodiscover_tasks(["wisdome_writer.infrastructure"])
app.set_default()
