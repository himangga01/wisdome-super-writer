"""Single runtime policy boundary for every external publishing surface."""

from __future__ import annotations

import re
from collections.abc import Callable
from functools import wraps

from django.conf import settings

EXTERNAL_PUBLISHING_EVENT_TYPES = frozenset(
    {
        "publication.scheduled_run_requested",
        "publication.preflight_requested",
        "publication.requested",
        "publication.reconcile_requested",
        "publishing.target_canary.requested",
        "publishing.target_disconnect.requested",
        "delivery.delete_requested",
        "delivery.prepare_requested",
        "delivery.reconcile_requested",
        "media.upload_requested",
        "media.reconcile_requested",
        "media.delete_requested",
    }
)
_PUBLISHING_API_PATH = re.compile(
    r"^/api/v1/(?:"
    r"targets(?:/|$)|"
    r"publishing(?:/|$)|"
    r"publication-attempts(?:/|$)|"
    r"publications(?:/|$)|"
    r"articles/[^/]+/(?:publication-intents|preview|approvals|publish|publications)(?:/|$)|"
    r"corrections/[^/]+/prepare-publication(?:/|$)"
    r")"
)


class ExternalPublishingDisabled(RuntimeError):
    """Local runtime attempted to enter an external publishing boundary."""


def external_publishing_enabled() -> bool:
    return settings.EXTERNAL_PUBLISHING_ENABLED is True


def require_external_publishing_enabled() -> None:
    if not external_publishing_enabled():
        raise ExternalPublishingDisabled("external_publishing_disabled")


def external_publishing_event(event_type: str) -> bool:
    return event_type in EXTERNAL_PUBLISHING_EVENT_TYPES


def external_publishing_http_path(path: str) -> bool:
    return path.startswith("/console/publishing") or _PUBLISHING_API_PATH.match(path) is not None


def external_publishing_boundary[**P, R](function: Callable[P, R]) -> Callable[P, R]:
    @wraps(function)
    def guarded(*args: P.args, **kwargs: P.kwargs) -> R:
        require_external_publishing_enabled()
        return function(*args, **kwargs)

    return guarded
