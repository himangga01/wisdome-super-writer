from django.conf import settings
from django.middleware.csrf import _does_token_match

from wisdome_writer.domain.errors import DomainError

from .problems import problem_response


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
            if request.path not in self.csrf_exempt_paths and not self._valid_csrf_header(request):
                return problem_response(
                    status=403,
                    code="csrf_required",
                    title="유효한 CSRF 헤더가 필요합니다",
                )
        return self.get_response(request)

    @staticmethod
    def _valid_csrf_header(request) -> bool:
        token = request.META.get("HTTP_X_CSRFTOKEN", "")
        secret = request.META.get("CSRF_COOKIE") or request.COOKIES.get(settings.CSRF_COOKIE_NAME)
        if not token or not secret:
            return False
        try:
            return _does_token_match(token, secret)
        except (ValueError, TypeError):
            return False


class ProblemDetailsMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        try:
            return self.get_response(request)
        except DomainError as exc:
            return problem_response(
                status=exc.status,
                title=exc.title,
                detail=exc.detail,
                code=exc.code,
                remediation=exc.remediation,
                correlation_id=getattr(request, "correlation_id", None),
            )

