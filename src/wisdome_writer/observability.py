from __future__ import annotations

import json
import logging
import os
import re
import time
import traceback
import uuid
from collections.abc import Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from django.http import HttpRequest, HttpResponse

_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,159}$")
_TERMINAL_IMPACT_FIELDS = (
    "scope",
    "stage",
    "step",
    "final_state",
    "state",
    "count",
    "affected_count",
    "error_code",
)
_MAX_OBSERVATION_COUNT = 9_007_199_254_740_991
_MAX_LOG_INPUT_LENGTH = 4_096
_MAX_LOG_MESSAGE_LENGTH = 512
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)[\"']?\b("
    r"authorization|cookie|set-cookie|password|passwd|credential|credentials|"
    r"access[-_]?token|refresh[-_]?token|id[-_]?token|session[-_]?token|"
    r"private[-_]?key|api[-_]?key|client[-_]?secret|secret|token"
    r")\b[\"']?\s*[:=]\s*(?:\"[^\"]*\"|'[^']*'|[^\s,;}\]]+)"
)
_BEARER_VALUE = re.compile(r"(?i)\bbearer\s+[^\s,;]+")
_URI_VALUE = re.compile(r"(?i)\b[a-z][a-z0-9+.-]*://[^\s]+")

_correlation_id: ContextVar[str] = ContextVar("correlation_id", default="-")


@dataclass(frozen=True, slots=True)
class TaskObservationContext:
    task_id: str | None
    task_name: str | None
    retry_count: int
    job_id: str | None
    entity_type: str | None
    entity_id: str | None
    operation: str | None
    attempt: int | None


@dataclass(frozen=True, slots=True)
class TaskContextBinding:
    correlation_token: object
    task_token: object
    started_monotonic: float


_task_context: ContextVar[TaskObservationContext | None] = ContextVar(
    "task_observation_context",
    default=None,
)


def _uuid_or_none(value: Any) -> uuid.UUID | None:
    if value in (None, "", "-"):
        return None
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        return None


def _resolved_correlation_uuid(
    value: Any,
    *,
    fallback: Any = None,
) -> uuid.UUID:
    if value not in (None, "", "-"):
        return _uuid_or_none(value) or uuid.uuid4()
    return _uuid_or_none(fallback) or uuid.uuid4()


def _safe_identifier(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(
        value,
        (str, int, uuid.UUID),
    ):
        return None
    candidate = str(value)
    if _SAFE_IDENTIFIER.fullmatch(candidate) is None:
        return None
    return candidate


def _safe_integer(
    value: Any,
    *,
    minimum: int = 0,
    maximum: int = _MAX_OBSERVATION_COUNT,
) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        candidate = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if candidate < minimum:
        return minimum
    return min(candidate, maximum)


def current_correlation_id() -> str:
    return _correlation_id.get()


def current_correlation_uuid(*, fallback: Any = None) -> uuid.UUID:
    return (
        _uuid_or_none(current_correlation_id())
        or _uuid_or_none(fallback)
        or uuid.uuid4()
    )


def current_task_id() -> str | None:
    context = _task_context.get()
    return context.task_id if context is not None else None


def current_task_retry_count() -> int:
    context = _task_context.get()
    return context.retry_count if context is not None else 0


def elapsed_milliseconds(
    started_at: datetime | None,
    finished_at: datetime | None,
) -> int | None:
    if started_at is None or finished_at is None:
        return None
    try:
        elapsed = int((finished_at - started_at).total_seconds() * 1000)
    except (AttributeError, TypeError, ValueError, OverflowError):
        return None
    return max(elapsed, 0)


def monotonic_elapsed_milliseconds(started_monotonic: float) -> int:
    return max(int((time.monotonic() - started_monotonic) * 1000), 0)


@contextmanager
def correlation_context(correlation_id: str | uuid.UUID):
    resolved = _resolved_correlation_uuid(correlation_id)
    token = _correlation_id.set(str(resolved))
    try:
        yield
    finally:
        _correlation_id.reset(token)


def bind_task_context(
    *,
    correlation_id: Any = None,
    task_id: Any = None,
    task_name: Any = None,
    retry_count: Any = 0,
    job_id: Any = None,
    entity_type: Any = None,
    entity_id: Any = None,
    operation: Any = None,
    attempt: Any = None,
) -> TaskContextBinding:
    resolved_correlation = _resolved_correlation_uuid(
        correlation_id,
        fallback=task_id,
    )
    context = TaskObservationContext(
        task_id=_safe_identifier(task_id),
        task_name=_safe_identifier(task_name),
        retry_count=_safe_integer(retry_count) or 0,
        job_id=_safe_identifier(job_id),
        entity_type=_safe_identifier(entity_type),
        entity_id=_safe_identifier(entity_id),
        operation=_safe_identifier(operation),
        attempt=_safe_integer(attempt, minimum=1),
    )
    correlation_token = _correlation_id.set(str(resolved_correlation))
    task_token = _task_context.set(context)
    return TaskContextBinding(
        correlation_token=correlation_token,
        task_token=task_token,
        started_monotonic=time.monotonic(),
    )


def reset_task_context(binding: TaskContextBinding) -> None:
    _task_context.reset(binding.task_token)
    _correlation_id.reset(binding.correlation_token)


def build_observation_headers(
    *,
    correlation_id: Any = None,
    job_id: Any = None,
    entity_type: Any = None,
    entity_id: Any = None,
    operation: Any = None,
    attempt: Any = None,
) -> dict[str, str | int]:
    context = _task_context.get()
    if correlation_id is None:
        resolved_correlation = current_correlation_uuid()
    else:
        resolved_correlation = _resolved_correlation_uuid(correlation_id)

    candidate_values = {
        "job_id": job_id if job_id is not None else getattr(context, "job_id", None),
        "entity_type": (
            entity_type
            if entity_type is not None
            else getattr(context, "entity_type", None)
        ),
        "entity_id": (
            entity_id
            if entity_id is not None
            else getattr(context, "entity_id", None)
        ),
        "operation": (
            operation
            if operation is not None
            else getattr(context, "operation", None)
        ),
    }
    headers: dict[str, str | int] = {
        "correlation_id": str(resolved_correlation),
    }
    for name, value in candidate_values.items():
        safe_value = _safe_identifier(value)
        if safe_value is not None:
            headers[name] = safe_value
    resolved_attempt = _safe_integer(
        attempt if attempt is not None else getattr(context, "attempt", None),
        minimum=1,
    )
    if resolved_attempt is not None:
        headers["attempt"] = resolved_attempt
    return headers


def _safe_terminal_impact(value: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    impact: dict[str, Any] = {}
    for key in _TERMINAL_IMPACT_FIELDS:
        try:
            item = value.get(key)
        except Exception:
            continue
        if key in {"count", "affected_count"}:
            safe_count = _safe_integer(item)
            if safe_count is not None:
                impact[key] = safe_count
            continue
        safe_value = _safe_identifier(item)
        if safe_value is not None:
            impact[key] = safe_value
    return impact or None


def log_lifecycle_event(
    logger: logging.Logger,
    *,
    event: str,
    level: int = logging.INFO,
    correlation_id: Any = None,
    task_id: Any = None,
    task_name: Any = None,
    job_id: Any = None,
    entity_type: Any = None,
    entity_id: Any = None,
    operation: Any = None,
    attempt: Any = None,
    retry_count: Any = None,
    state: Any = None,
    duration_ms: Any = None,
    error_code: Any = None,
    terminal_impact: Mapping[str, Any] | None = None,
    recovery_state: Any = None,
    http_status: Any = None,
    input_count: Any = None,
    output_count: Any = None,
) -> None:
    safe_event = _safe_identifier(event)
    if safe_event is None:
        raise ValueError("lifecycle event must be a safe identifier")

    context = _task_context.get()
    extra: dict[str, Any] = {
        "event": safe_event,
        "correlation_id": str(
            _resolved_correlation_uuid(
                correlation_id,
                fallback=current_correlation_id(),
            )
        ),
    }
    identifier_values = {
        "task_id": task_id if task_id is not None else getattr(context, "task_id", None),
        "task_name": (
            task_name
            if task_name is not None
            else getattr(context, "task_name", None)
        ),
        "job_id": job_id if job_id is not None else getattr(context, "job_id", None),
        "entity_type": (
            entity_type
            if entity_type is not None
            else getattr(context, "entity_type", None)
        ),
        "entity_id": (
            entity_id
            if entity_id is not None
            else getattr(context, "entity_id", None)
        ),
        "operation": (
            operation
            if operation is not None
            else getattr(context, "operation", None)
        ),
        "state": state,
        "error_code": error_code,
        "recovery_state": recovery_state,
    }
    for name, value in identifier_values.items():
        safe_value = _safe_identifier(value)
        if safe_value is not None:
            extra[name] = safe_value

    integer_values = {
        "attempt": (
            attempt
            if attempt is not None
            else getattr(context, "attempt", None)
        ),
        "retry_count": (
            retry_count
            if retry_count is not None
            else getattr(context, "retry_count", 0)
        ),
        "duration_ms": duration_ms,
        "http_status": http_status,
        "input_count": input_count,
        "output_count": output_count,
    }
    for name, value in integer_values.items():
        minimum = 1 if name == "attempt" else 0
        safe_value = _safe_integer(value, minimum=minimum)
        if safe_value is not None:
            extra[name] = safe_value

    safe_impact = _safe_terminal_impact(terminal_impact)
    if safe_impact is not None:
        extra["terminal_impact"] = safe_impact

    logger.log(level, safe_event, extra=extra)


class CorrelationIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        explicit = _uuid_or_none(getattr(record, "correlation_id", None))
        inherited = _uuid_or_none(current_correlation_id())
        record.correlation_id = str(explicit or inherited) if explicit or inherited else "-"
        return True


class SafeJsonFormatter(logging.Formatter):
    _observation_fields = (
        "event",
        "task_id",
        "task_name",
        "job_id",
        "entity_type",
        "entity_id",
        "operation",
        "attempt",
        "retry_count",
        "state",
        "duration_ms",
        "error_code",
        "terminal_impact",
        "recovery_state",
        "http_status",
        "input_count",
        "output_count",
    )
    _identifier_fields = frozenset(
        {
            "event",
            "task_id",
            "task_name",
            "job_id",
            "entity_type",
            "entity_id",
            "operation",
            "state",
            "error_code",
            "recovery_state",
        }
    )
    _integer_fields = frozenset(
        {
            "attempt",
            "retry_count",
            "duration_ms",
            "http_status",
            "input_count",
            "output_count",
        }
    )

    @staticmethod
    def _safe_message(record: logging.LogRecord) -> str:
        # Deliberately omit ``record.args``: arbitrary library log arguments can
        # contain response bodies, URLs, credentials, or exception text.
        if isinstance(record.msg, str):
            message = record.msg[:_MAX_LOG_INPUT_LENGTH]
        else:
            message = record.msg.__class__.__name__
        message = _URI_VALUE.sub("[uri]", message)
        message = _BEARER_VALUE.sub("Bearer [redacted]", message)
        message = _SECRET_ASSIGNMENT.sub(
            lambda match: f"{match.group(1)}=[redacted]",
            message,
        )
        message = " ".join(message.split())
        return message[:_MAX_LOG_MESSAGE_LENGTH]

    @staticmethod
    def _safe_exception(record: logging.LogRecord) -> dict[str, Any] | None:
        if not record.exc_info:
            return None
        exception_type, _, exception_traceback = record.exc_info
        frames = []
        if exception_traceback is not None:
            for frame in traceback.extract_tb(exception_traceback, limit=8):
                frames.append(
                    {
                        "file": (
                            _safe_identifier(os.path.basename(frame.filename))
                            or "unknown"
                        ),
                        "line": _safe_integer(frame.lineno, minimum=1),
                        "function": _safe_identifier(frame.name) or "unknown",
                    }
                )
        return {
            "type": _safe_identifier(
                getattr(exception_type, "__name__", None)
            )
            or "Exception",
            "module": _safe_identifier(
                getattr(exception_type, "__module__", None)
            )
            or "unknown",
            "frames": frames,
        }

    def format(self, record: logging.LogRecord) -> str:
        correlation_id = _uuid_or_none(
            getattr(record, "correlation_id", None)
        )
        payload: dict[str, Any] = {
            "time": datetime.fromtimestamp(
                record.created,
                tz=timezone.utc,
            ).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": _safe_identifier(record.name) or "unknown",
            "correlation_id": (
                str(correlation_id) if correlation_id is not None else "-"
            ),
            "message": self._safe_message(record),
        }
        for field in self._observation_fields:
            value = getattr(record, field, None)
            if field in self._identifier_fields:
                safe_value = _safe_identifier(value)
            elif field in self._integer_fields:
                safe_value = _safe_integer(
                    value,
                    minimum=1 if field == "attempt" else 0,
                )
            else:
                safe_value = _safe_terminal_impact(value)
            if safe_value is not None:
                payload[field] = safe_value
        safe_exception = self._safe_exception(record)
        if safe_exception is not None:
            payload["exception"] = safe_exception
        if record.stack_info:
            payload["stack_present"] = True
        return json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )


class CorrelationIdMiddleware:
    header_name = "HTTP_X_CORRELATION_ID"

    def __init__(self, get_response):
        self.get_response = get_response
        self.logger = logging.getLogger("wisdome_writer.http")

    def __call__(self, request: HttpRequest) -> HttpResponse:
        correlation_id = str(
            _resolved_correlation_uuid(
                request.META.get(self.header_name),
            )
        )
        request.correlation_id = correlation_id
        token = _correlation_id.set(correlation_id)
        started_monotonic = time.monotonic()
        try:
            response = self.get_response(request)
            response["X-Correlation-ID"] = correlation_id
            if response.status_code >= 500:
                state = "failed"
            elif response.status_code >= 400:
                state = "rejected"
            else:
                state = "succeeded"
            log_lifecycle_event(
                self.logger,
                event="http.request.completed",
                correlation_id=correlation_id,
                operation=f"http.{request.method.lower()}",
                state=state,
                duration_ms=monotonic_elapsed_milliseconds(started_monotonic),
                http_status=response.status_code,
            )
            return response
        except Exception as exc:
            log_lifecycle_event(
                self.logger,
                event="http.request.failed",
                level=logging.ERROR,
                correlation_id=correlation_id,
                operation=f"http.{request.method.lower()}",
                state="failed",
                duration_ms=monotonic_elapsed_milliseconds(started_monotonic),
                error_code=exc.__class__.__name__,
            )
            raise
        finally:
            _correlation_id.reset(token)
