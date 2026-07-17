from http import HTTPStatus

from django.http import JsonResponse

from wisdome_writer.observability import current_correlation_id


def problem_response(
    *,
    status: int,
    title: str | None = None,
    detail: str = "",
    code: str = "request_failed",
    remediation: str | None = None,
    correlation_id: str | None = None,
) -> JsonResponse:
    payload = {
        "type": f"/problems/{code}",
        "title": title or HTTPStatus(status).phrase,
        "status": status,
        "correlationId": correlation_id or current_correlation_id(),
        "code": code,
        "remediation": remediation,
    }
    if detail:
        payload["detail"] = detail
    return JsonResponse(payload, status=status, content_type="application/problem+json")


def handler400(request, exception=None):
    return problem_response(status=400, code="bad_request", title="잘못된 요청입니다")


def handler403(request, exception=None):
    return problem_response(status=403, code="forbidden", title="권한이 없습니다")


def handler404(request, exception=None):
    return problem_response(status=404, code="not_found", title="대상을 찾을 수 없습니다")


def handler500(request):
    return problem_response(status=500, code="internal_error", title="내부 오류가 발생했습니다")

