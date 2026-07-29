from __future__ import annotations

import json
import logging
import re

from django.conf import settings
from django.core.exceptions import (
    BadRequest as DjangoBadRequest,
    NON_FIELD_ERRORS,
    ObjectDoesNotExist,
    PermissionDenied,
    RequestDataTooBig,
    SuspiciousOperation,
    ValidationError as DjangoValidationError,
)
from django.http import Http404, HttpRequest, HttpResponse
from django.middleware.csrf import (
    InvalidTokenFormat,
    _check_token_format,
    _does_token_match,
)

from wisdome_writer.domain.errors import (
    DomainError,
    MethodNotAllowed,
    RateLimited,
    ValidationIssue,
)

from .problems import is_api_request, problem_response

logger = logging.getLogger(__name__)

_PROBLEM_CODE_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_PRESERVED_ERROR_HEADERS = ("Allow", "Retry-After", "WWW-Authenticate")
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})
_JSON_MEDIA_TYPES = frozenset(
    {
        "application/json",
        "application/merge-patch+json",
    }
)
_STATUS_PROBLEMS: dict[int, tuple[str, str, str | None]] = {
    400: ("bad_request", "잘못된 요청입니다", None),
    401: ("authentication_required", "인증이 필요합니다", "관리자 로그인 후 다시 요청하세요."),
    403: ("forbidden", "권한이 없습니다", None),
    404: ("not_found", "대상을 찾을 수 없습니다", None),
    405: (
        "method_not_allowed",
        "허용되지 않은 HTTP 메서드입니다",
        "Allow 헤더에 표시된 HTTP 메서드를 사용하세요.",
    ),
    406: ("not_acceptable", "지원하지 않는 응답 형식입니다", None),
    408: ("request_timeout", "요청 시간이 초과되었습니다", None),
    409: ("state_conflict", "현재 상태에서는 요청을 처리할 수 없습니다", "최신 상태를 확인한 뒤 요청하세요."),
    410: ("gone", "더 이상 제공되지 않는 대상입니다", None),
    413: ("payload_too_large", "요청 본문이 너무 큽니다", None),
    414: ("uri_too_long", "요청 URI가 너무 깁니다", None),
    415: (
        "unsupported_media_type",
        "지원하지 않는 미디어 타입입니다",
        "Content-Type을 application/json으로 설정하세요.",
    ),
    422: (
        "request_validation_failed",
        "요청 입력값 검증에 실패했습니다",
        "요청 형식과 필수 입력값을 확인하세요.",
    ),
    429: ("rate_limited", "요청이 너무 많습니다", "잠시 후 다시 요청하세요."),
    500: ("internal_error", "내부 오류가 발생했습니다", None),
    501: ("not_implemented", "지원하지 않는 기능입니다", None),
    502: ("upstream_failure", "외부 서비스 응답에 실패했습니다", None),
    503: ("service_unavailable", "서비스를 일시적으로 사용할 수 없습니다", "잠시 후 다시 요청하세요."),
    504: ("upstream_timeout", "외부 서비스 응답 시간이 초과되었습니다", "잠시 후 다시 요청하세요."),
}


class AdminApiSecurityMiddleware:
    api_prefix = "/api/v1/"
    csrf_exempt_paths = frozenset({"/api/v1/publishing/oauth/google/callback"})

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.path.startswith(self.api_prefix):
            if not request.user.is_authenticated:
                return problem_response(
                    status=401,
                    code="authentication_required",
                    title="관리자 로그인이 필요합니다",
                )
            if not request.user.is_active or not request.user.is_staff:
                return problem_response(
                    status=403,
                    code="staff_required",
                    title="관리자 권한이 필요합니다",
                )
            if self._unsupported_unsafe_media_type(request):
                return problem_response(
                    status=415,
                    code="unsupported_media_type",
                    title="지원하지 않는 미디어 타입입니다",
                    remediation="Content-Type을 application/json으로 설정하세요.",
                )
            if request.path not in self.csrf_exempt_paths and not self._valid_csrf_header(request):
                return problem_response(
                    status=403,
                    code="csrf_required",
                    title="유효한 CSRF 헤더가 필요합니다",
                )
        return self.get_response(request)

    @staticmethod
    def _unsupported_unsafe_media_type(request) -> bool:
        if str(request.method).upper() in _SAFE_METHODS:
            return False
        raw_content_type = request.META.get("CONTENT_TYPE", "")
        if not isinstance(raw_content_type, str) or not raw_content_type.strip():
            return False
        media_type = raw_content_type.partition(";")[0].strip().lower()
        return media_type not in _JSON_MEDIA_TYPES

    @staticmethod
    def _valid_csrf_header(request) -> bool:
        token = request.META.get("HTTP_X_CSRFTOKEN", "")
        secret = request.META.get("CSRF_COOKIE") or request.COOKIES.get(settings.CSRF_COOKIE_NAME)
        if not token or not secret:
            return False
        try:
            _check_token_format(token)
            _check_token_format(secret)
            return _does_token_match(token, secret)
        except (AssertionError, InvalidTokenFormat, ValueError, TypeError):
            return False


def _json_pointer_segment(value: str) -> str:
    return value.replace("~", "~0").replace("/", "~1")


def _django_validation_issues(exception: DjangoValidationError) -> tuple[ValidationIssue, ...]:
    issues: list[ValidationIssue] = []
    if hasattr(exception, "error_dict"):
        for field, field_errors in exception.error_dict.items():
            path = "/body"
            if isinstance(field, str) and field != NON_FIELD_ERRORS:
                path = f"/body/{_json_pointer_segment(field)}"
            for field_error in field_errors:
                issue = ValidationIssue(path=path, code=getattr(field_error, "code", None))
                if issue not in issues:
                    issues.append(issue)
    else:
        for field_error in exception.error_list:
            issue = ValidationIssue(path="/body", code=getattr(field_error, "code", None))
            if issue not in issues:
                issues.append(issue)
    return tuple(issues)


def _problem_headers(response: HttpResponse) -> dict[str, str]:
    return {
        name: response[name]
        for name in _PRESERVED_ERROR_HEADERS
        if response.has_header(name)
    }


def _legacy_problem_code(response: HttpResponse) -> str | None:
    """Recover only a machine code from legacy JSON, never its detail or values."""

    if getattr(response, "streaming", False):
        return None
    content_type = response.get("Content-Type", "").partition(";")[0].strip().lower()
    if content_type != "application/json" or len(response.content) > 16_384:
        return None
    try:
        payload = json.loads(response.content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None

    code = payload.get("code")
    if isinstance(code, str) and _PROBLEM_CODE_PATTERN.fullmatch(code):
        return code

    problem_type = payload.get("type")
    if not isinstance(problem_type, str):
        return None
    for prefix in ("/problems/", "urn:wisdome-writer:problem:"):
        if problem_type.startswith(prefix):
            candidate = problem_type.removeprefix(prefix)
            if _PROBLEM_CODE_PATTERN.fullmatch(candidate):
                return candidate
    return None


def _status_problem(status: int) -> tuple[str, str, str | None]:
    if status in _STATUS_PROBLEMS:
        return _STATUS_PROBLEMS[status]
    if 400 <= status < 500:
        return ("request_failed", "요청을 처리할 수 없습니다", None)
    return ("internal_error", "내부 오류가 발생했습니다", None)


class ProblemDetailsMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        response = self.get_response(request)
        if not is_api_request(request) or response.status_code < 400:
            return response

        content_type = response.get("Content-Type", "").partition(";")[0].strip().lower()
        if content_type == "application/problem+json":
            return response

        code, title, remediation = _status_problem(response.status_code)
        code = _legacy_problem_code(response) or code
        return problem_response(
            status=response.status_code,
            code=code,
            title=title,
            remediation=remediation,
            correlation_id=getattr(request, "correlation_id", None),
            headers=_problem_headers(response),
        )

    def process_exception(
        self,
        request: HttpRequest,
        exception: BaseException,
    ) -> HttpResponse | None:
        if not is_api_request(request):
            return None

        if isinstance(exception, DomainError):
            headers: dict[str, str] = {}
            if isinstance(exception, MethodNotAllowed) and exception.allowed_methods:
                headers["Allow"] = ", ".join(exception.allowed_methods)
            if isinstance(exception, RateLimited):
                headers["Retry-After"] = str(exception.retry_after_seconds)
            return problem_response(
                status=exception.status,
                title=exception.title,
                detail=exception.public_detail,
                code=exception.code,
                errors=exception.errors,
                remediation=exception.remediation,
                correlation_id=getattr(request, "correlation_id", None),
                headers=headers,
            )

        if isinstance(exception, (Http404, ObjectDoesNotExist)):
            return problem_response(
                status=404,
                code="not_found",
                title="대상을 찾을 수 없습니다",
                correlation_id=getattr(request, "correlation_id", None),
            )

        if isinstance(exception, RequestDataTooBig):
            return problem_response(
                status=413,
                code="payload_too_large",
                title="요청 본문이 너무 큽니다",
                correlation_id=getattr(request, "correlation_id", None),
            )

        if isinstance(exception, (DjangoBadRequest, SuspiciousOperation)):
            return problem_response(
                status=400,
                code="bad_request",
                title="잘못된 요청입니다",
                correlation_id=getattr(request, "correlation_id", None),
            )

        if isinstance(exception, PermissionDenied):
            return problem_response(
                status=403,
                code="forbidden",
                title="권한이 없습니다",
                correlation_id=getattr(request, "correlation_id", None),
            )

        if isinstance(exception, DjangoValidationError):
            return problem_response(
                status=422,
                code="request_validation_failed",
                title="요청 입력값 검증에 실패했습니다",
                errors=_django_validation_issues(exception),
                remediation="errors의 path와 code를 확인해 요청을 수정하세요.",
                correlation_id=getattr(request, "correlation_id", None),
            )

        correlation_id = getattr(request, "correlation_id", None)
        logger.error(
            "Unhandled API exception [correlation_id=%s]",
            correlation_id,
            exc_info=(type(exception), exception, exception.__traceback__),
        )
        return problem_response(
            status=500,
            code="internal_error",
            title="내부 오류가 발생했습니다",
            correlation_id=correlation_id,
        )
