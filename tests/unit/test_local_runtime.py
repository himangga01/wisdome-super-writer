from pathlib import Path

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.test import override_settings


def test_local_runtime_uses_repository_local_paths(settings):
    """Changing the local defaults must keep Docker-free state inside the repository."""
    assert settings.WISDOME_RUNTIME_MODE == "local"
    assert settings.IS_LOCAL_RUNTIME is True
    assert Path(settings.LOCAL_STATE_ROOT).is_absolute()
    assert settings.LOCAL_STATE_ROOT == settings.REPOSITORY_ROOT / ".local" / "state"
    assert settings.LOCAL_ARTICLE_ROOT == settings.REPOSITORY_ROOT / "output" / "housing"
    assert settings.LOCAL_OBJECT_ROOT == settings.REPOSITORY_ROOT / ".local" / "objects"
    assert settings.HUMANIZER_BASE_URL == "http://127.0.0.1:3210"
    assert settings.CELERY_TASK_ALWAYS_EAGER is True
    assert settings.CELERY_TASK_EAGER_PROPAGATES is True


def test_production_rejects_local_runtime():
    """Removing the development-only guard must fail this production safety check."""
    from wisdome_writer.runtime_mode import validate_runtime_mode

    with pytest.raises(ImproperlyConfigured, match="development-only"):
        validate_runtime_mode("production", "local")


def test_runtime_mode_rejects_unknown_value():
    """Accepting an unsupported mode would make readiness behavior ambiguous."""
    from wisdome_writer.runtime_mode import validate_runtime_mode

    with pytest.raises(ImproperlyConfigured, match="must be 'local' or 'distributed'"):
        validate_runtime_mode("development", "container")


@pytest.mark.django_db
def test_local_ready_bypasses_redis_and_checks_local_roots(client, monkeypatch, tmp_path):
    """Calling Redis or outbox readiness in local mode would reintroduce the Docker dependency."""
    redis_called = False

    def fail_if_redis_is_used(*args, **kwargs):
        nonlocal redis_called
        redis_called = True
        raise AssertionError("Redis must not be used in local readiness")

    monkeypatch.setattr(
        "wisdome_writer.api.health.Redis.from_url",
        fail_if_redis_is_used,
    )
    with override_settings(
        IS_LOCAL_RUNTIME=True,
        LOCAL_STATE_ROOT=tmp_path / "state",
        LOCAL_ARTICLE_ROOT=tmp_path / "articles",
        LOCAL_OBJECT_ROOT=tmp_path / "objects",
    ):
        response = client.get("/health/ready")

    assert redis_called is False
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["checks"] == {
        "database": "ok",
        "local_state": "ok",
        "local_articles": "ok",
        "local_objects": "ok",
    }
    assert "outbox" not in payload
