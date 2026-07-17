import logging
import uuid
from contextvars import ContextVar

from django.http import HttpRequest, HttpResponse

_correlation_id: ContextVar[str] = ContextVar("correlation_id", default="-")


def current_correlation_id() -> str:
    return _correlation_id.get()


class CorrelationIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.correlation_id = current_correlation_id()
        return True


class CorrelationIdMiddleware:
    header_name = "HTTP_X_CORRELATION_ID"

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        supplied = request.META.get(self.header_name, "")
        try:
            correlation_id = str(uuid.UUID(supplied)) if supplied else str(uuid.uuid4())
        except (ValueError, TypeError, AttributeError):
            correlation_id = str(uuid.uuid4())
        request.correlation_id = correlation_id
        token = _correlation_id.set(correlation_id)
        try:
            response = self.get_response(request)
            response["X-Correlation-ID"] = correlation_id
            return response
        finally:
            _correlation_id.reset(token)

