from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

_ERROR_CODE_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_HTTP_METHOD_PATTERN = re.compile(r"^[A-Z]+$")
_ISSUE_ROOTS = ("/body", "/query", "/path")


def _public_text(value: object) -> str:
    """Keep explicitly public text without coercing arbitrary values."""

    if not isinstance(value, str):
        return ""
    if any(ord(character) < 32 and character not in "\t" for character in value):
        return ""
    return value.strip()


def _issue_path(value: object) -> str:
    if not isinstance(value, str) or len(value) > 512:
        return "/body"
    if not any(value == root or value.startswith(f"{root}/") for root in _ISSUE_ROOTS):
        return "/body"
    if any(ord(character) < 32 for character in value):
        return "/body"
    return value


def _issue_code(value: object) -> str:
    if isinstance(value, str) and _ERROR_CODE_PATTERN.fullmatch(value):
        return value
    return "invalid"


@dataclass(frozen=True, slots=True)
class ValidationIssue:
    """A value-free validation failure safe to include in an API response."""

    path: str
    code: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", _issue_path(self.path))
        object.__setattr__(self, "code", _issue_code(self.code))

    def as_dict(self) -> dict[str, str]:
        return {"path": self.path, "code": self.code}


def _validation_issues(
    values: Iterable[ValidationIssue | Mapping[str, Any]] | None,
) -> tuple[ValidationIssue, ...]:
    if values is None:
        return ()

    issues: list[ValidationIssue] = []
    for value in values:
        if isinstance(value, ValidationIssue):
            issue = value
        elif isinstance(value, Mapping):
            issue = ValidationIssue(
                path=value.get("path"),
                code=value.get("code"),
            )
        else:
            continue
        if issue not in issues:
            issues.append(issue)
    return tuple(issues)


class DomainError(Exception):
    status = 400
    code = "domain_error"
    title = "요청을 처리할 수 없습니다"
    remediation: str | None = None

    def __init__(
        self,
        detail: str = "",
        *,
        errors: Iterable[ValidationIssue | Mapping[str, Any]] | None = None,
        remediation: str | None = None,
    ):
        self.public_detail = _public_text(detail)
        # Compatibility for existing API boundaries. New code should use
        # ``public_detail`` to make the disclosure contract explicit.
        self.detail = self.public_detail
        self.errors = _validation_issues(errors)
        inherited_remediation = _public_text(type(self).remediation)
        supplied_remediation = _public_text(remediation)
        self.remediation = supplied_remediation or inherited_remediation or None
        super().__init__(self.public_detail or self.code)


class BadRequest(DomainError):
    status = 400
    code = "bad_request"
    title = "잘못된 요청입니다"


class MalformedJson(DomainError):
    status = 400
    code = "malformed_json"
    title = "JSON 요청 본문이 올바르지 않습니다"
    remediation = "UTF-8 JSON object 형식으로 요청을 다시 작성하세요."


MalformedJSON = MalformedJson


class InvalidCursor(DomainError):
    status = 400
    code = "invalid_cursor"
    title = "페이지 커서가 올바르지 않습니다"
    remediation = "현재 조건으로 목록을 처음부터 다시 조회하세요."


class MethodNotAllowed(DomainError):
    status = 405
    code = "method_not_allowed"
    title = "허용되지 않은 HTTP 메서드입니다"
    remediation = "Allow 헤더에 표시된 HTTP 메서드를 사용하세요."

    def __init__(
        self,
        detail: str = "",
        *,
        allowed_methods: Iterable[str] = (),
        remediation: str | None = None,
    ):
        methods: list[str] = []
        for value in allowed_methods:
            if not isinstance(value, str):
                continue
            method = value.upper()
            if _HTTP_METHOD_PATTERN.fullmatch(method) and method not in methods:
                methods.append(method)
        self.allowed_methods = tuple(methods)
        super().__init__(detail, remediation=remediation)


class UnsupportedMediaType(DomainError):
    status = 415
    code = "unsupported_media_type"
    title = "지원하지 않는 미디어 타입입니다"
    remediation = "Content-Type을 application/json으로 설정하세요."


class InvalidInput(DomainError):
    status = 422
    code = "invalid_input"
    title = "입력값이 올바르지 않습니다"


class RequestValidationError(DomainError):
    status = 422
    code = "request_validation_failed"
    title = "요청 입력값 검증에 실패했습니다"
    remediation = "errors의 path와 code를 확인해 요청을 수정하세요."


class AuthenticationFailed(DomainError):
    status = 401
    code = "authentication_failed"
    title = "재인증에 실패했습니다"
    remediation = "현재 인증 정보를 확인한 뒤 다시 시도하세요."


class RateLimited(DomainError):
    status = 429
    code = "rate_limited"
    title = "요청이 너무 많습니다"
    remediation = "잠시 후 다시 시도하세요."

    def __init__(self, *, retry_after_seconds: int):
        try:
            retry_after = int(retry_after_seconds)
        except (TypeError, ValueError, OverflowError):
            retry_after = 1
        self.retry_after_seconds = min(max(retry_after, 1), 86_400)
        super().__init__()


class Forbidden(DomainError):
    status = 403
    code = "forbidden"
    title = "권한이 없습니다"


class NotFound(DomainError):
    status = 404
    code = "not_found"
    title = "대상을 찾을 수 없습니다"


class Conflict(DomainError):
    status = 409
    code = "conflict"
    title = "현재 상태와 요청이 충돌합니다"


class RequestKeyConflict(Conflict):
    code = "request_key_conflict"
    title = "요청 키가 다른 요청 본문과 충돌합니다"
    remediation = "동일한 요청에는 같은 본문을 사용하거나 새 요청 키를 사용하세요."


class StaleVersion(Conflict):
    code = "stale_version"
    title = "대상의 버전이 변경되었습니다"
    remediation = "최신 상태를 다시 조회한 뒤 요청하세요."


class StateConflict(Conflict):
    code = "state_conflict"
    title = "현재 상태에서는 요청을 처리할 수 없습니다"
    remediation = "최신 상태를 확인한 뒤 요청하세요."
