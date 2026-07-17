class DomainError(Exception):
    status = 400
    code = "domain_error"
    title = "요청을 처리할 수 없습니다"
    remediation: str | None = None

    def __init__(self, detail: str = "", *, remediation: str | None = None):
        super().__init__(detail)
        self.detail = detail
        if remediation is not None:
            self.remediation = remediation


class InvalidInput(DomainError):
    status = 422
    code = "invalid_input"
    title = "입력값이 올바르지 않습니다"


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

