from __future__ import annotations

import re
import uuid
from collections.abc import Iterable, Mapping
from http import HTTPStatus
from typing import Any

from django.http import HttpRequest, HttpResponse, JsonResponse
from django.views.defaults import (
    bad_request as django_bad_request,
    page_not_found as django_page_not_found,
    permission_denied as django_permission_denied,
    server_error as django_server_error,
)
from django.views.csrf import csrf_failure as django_csrf_failure

from wisdome_writer.domain.errors import ValidationIssue
from wisdome_writer.observability import current_correlation_id

API_PREFIX = "/api/v1/"
_PROBLEM_CODE_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_MAX_PROBLEM_ISSUES = 50


def is_api_request(request: HttpRequest) -> bool:
    return request.path.startswith(API_PREFIX)


def correlation_id_for(value: object = None) -> str:
    candidate = value if value not in (None, "") else current_correlation_id()
    try:
        return str(uuid.UUID(str(candidate)))
    except (ValueError, TypeError, AttributeError):
        return str(uuid.uuid4())


def _safe_code(value: object) -> str:
    if isinstance(value, str) and _PROBLEM_CODE_PATTERN.fullmatch(value):
        return value
    return "request_failed"


def _safe_public_text(value: object) -> str:
    if not isinstance(value, str):
        return ""
    if any(ord(character) < 32 and character not in "\t" for character in value):
        return ""
    return value.strip()


def _safe_issues(
    errors: Iterable[ValidationIssue | Mapping[str, Any]] | None,
) -> list[dict[str, str]]:
    if errors is None:
        return []

    issues: list[dict[str, str]] = []
    for value in errors:
        if isinstance(value, ValidationIssue):
            issue = value
        elif isinstance(value, Mapping):
            issue = ValidationIssue(
                path=value.get("path"),
                code=value.get("code"),
            )
        else:
            continue
        payload = issue.as_dict()
        if payload not in issues:
            issues.append(payload)
            if len(issues) >= _MAX_PROBLEM_ISSUES:
                break
    return issues


def problem_response(
    *,
    status: int,
    title: str | None = None,
    detail: str = "",
    code: str = "request_failed",
    errors: Iterable[ValidationIssue | Mapping[str, Any]] | None = None,
    remediation: str | None = None,
    correlation_id: str | None = None,
    headers: Mapping[str, str] | None = None,
) -> JsonResponse:
    safe_code = _safe_code(code)
    safe_title = _safe_public_text(title)
    if not safe_title:
        try:
            safe_title = HTTPStatus(status).phrase
        except ValueError:
            safe_title = "Request failed"

    response_correlation_id = correlation_id_for(correlation_id)
    payload: dict[str, Any] = {
        "type": f"/problems/{safe_code}",
        "title": safe_title,
        "status": status,
        "code": safe_code,
        "correlationId": response_correlation_id,
        "remediation": _safe_public_text(remediation) or None,
    }
    safe_detail = _safe_public_text(detail)
    if safe_detail:
        payload["detail"] = safe_detail
    safe_errors = _safe_issues(errors)
    if safe_errors:
        payload["errors"] = safe_errors

    response = JsonResponse(
        payload,
        status=status,
        content_type="application/problem+json",
    )
    if headers:
        for name, value in headers.items():
            if (
                isinstance(name, str)
                and name.lower() not in {"content-type", "content-length", "x-correlation-id"}
                and isinstance(value, str)
            ):
                response[name] = value
    response["X-Correlation-ID"] = response_correlation_id
    return response


def handler400(request: HttpRequest, exception=None) -> HttpResponse:
    if not is_api_request(request):
        return django_bad_request(request, exception)
    return problem_response(
        status=400,
        code="bad_request",
        title="잘못된 요청입니다",
        correlation_id=getattr(request, "correlation_id", None),
    )


def handler403(request: HttpRequest, exception=None) -> HttpResponse:
    if not is_api_request(request):
        return django_permission_denied(request, exception)
    return problem_response(
        status=403,
        code="forbidden",
        title="권한이 없습니다",
        correlation_id=getattr(request, "correlation_id", None),
    )


def handler404(request: HttpRequest, exception=None) -> HttpResponse:
    if not is_api_request(request):
        return django_page_not_found(request, exception)
    return problem_response(
        status=404,
        code="not_found",
        title="대상을 찾을 수 없습니다",
        correlation_id=getattr(request, "correlation_id", None),
    )


def handler500(request: HttpRequest) -> HttpResponse:
    if not is_api_request(request):
        return django_server_error(request)
    return problem_response(
        status=500,
        code="internal_error",
        title="내부 오류가 발생했습니다",
        correlation_id=getattr(request, "correlation_id", None),
    )


def csrf_failure(request: HttpRequest, reason: str = "") -> HttpResponse:
    if not is_api_request(request):
        return django_csrf_failure(request, reason=reason)
    return problem_response(
        status=403,
        code="csrf_required",
        title="유효한 CSRF 검증이 필요합니다",
        correlation_id=getattr(request, "correlation_id", None),
    )
