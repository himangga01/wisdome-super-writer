from types import SimpleNamespace

import pytest
from django.test import override_settings


def test_local_runtime_centrally_disables_external_publishing(settings) -> None:
    assert settings.IS_LOCAL_RUNTIME is True
    assert settings.EXTERNAL_PUBLISHING_ENABLED is False


@override_settings(EXTERNAL_PUBLISHING_ENABLED=False)
def test_local_publishing_api_fails_before_service_dispatch(
    client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "apps.publishing.api.create_target",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("publishing service must not be reached")
        ),
    )

    response = client.post("/api/v1/targets", data="{}", content_type="application/json")

    assert response.status_code == 404


@override_settings(EXTERNAL_PUBLISHING_ENABLED=False)
def test_local_publisher_resolution_fails_before_adapter_construction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from apps.publishing import services

    monkeypatch.setattr(
        services,
        "WordPressPublisher",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("WordPress adapter must not be constructed")
        ),
    )

    with pytest.raises(RuntimeError, match="external_publishing_disabled"):
        services.publisher_for_target(SimpleNamespace(channel="wordpress"))


@override_settings(EXTERNAL_PUBLISHING_ENABLED=False)
def test_local_publication_task_fails_before_domain_or_adapter_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from apps.publishing import tasks

    monkeypatch.setattr(
        tasks,
        "_worker_audit_context",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("task body must not be reached")
        ),
    )

    with pytest.raises(RuntimeError, match="external_publishing_disabled"):
        tasks.execute_publication_attempt.run("00000000-0000-4000-8000-000000000000", 1)


@override_settings(EXTERNAL_PUBLISHING_ENABLED=False)
def test_local_event_routing_rejects_external_publication() -> None:
    from wisdome_writer.infrastructure.event_routes import (
        EVENT_ROUTES,
        EventRoutingError,
        queue_for,
        route_for,
    )

    external = EVENT_ROUTES[("publication.requested", 2)]
    envelope = {
        "event_type": "publication.requested",
        "payload": {"publication_attempt_id": "00000000-0000-4000-8000-000000000000"},
    }

    assert route_for("publication.requested", 2) is None
    with pytest.raises(EventRoutingError, match="external_publishing_disabled"):
        queue_for(external, envelope)
    assert route_for("run.requested", 1) is not None


@override_settings(EXTERNAL_PUBLISHING_ENABLED=True)
def test_distributed_policy_keeps_publication_route_available() -> None:
    from wisdome_writer.infrastructure.event_routes import route_for

    assert route_for("publication.requested", 2) is not None
