import json
import os
import subprocess
import sys
from datetime import datetime
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
    assert settings.PUBLIC_BASE_URL == "http://localhost:7667"
    assert Path(settings.DATABASE_URL.removeprefix("sqlite:///")) == (
        settings.LOCAL_STATE_ROOT / "db.sqlite3"
    )
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


def test_clean_child_manage_command_loads_env_local_with_process_precedence(settings) -> None:
    """A fresh shell must use .env.local, while explicit process values still win."""

    environment = {
        key: value
        for key, value in os.environ.items()
        if key
        not in {
            "WISDOME_ENVIRONMENT",
            "WISDOME_RUNTIME_MODE",
            "DJANGO_SETTINGS_MODULE",
        }
    }
    check = subprocess.run(
        [sys.executable, "src/manage.py", "check"],
        cwd=settings.REPOSITORY_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        check=False,
    )
    assert check.returncode == 0, check.stdout + check.stderr

    environment.update(
        {
            "WISDOME_ENVIRONMENT": "development",
            "WISDOME_RUNTIME_MODE": "distributed",
            "DJANGO_SETTINGS_MODULE": "wisdome_writer.settings",
            "PYTHONPATH": str(settings.SRC_ROOT),
        }
    )
    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import json; from django.conf import settings; "
                "print(json.dumps({'environment': settings.WISDOME_ENVIRONMENT, "
                "'runtime': settings.WISDOME_RUNTIME_MODE}))"
            ),
        ],
        cwd=settings.REPOSITORY_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        check=False,
    )
    assert probe.returncode == 0, probe.stdout + probe.stderr
    assert json.loads(probe.stdout.strip()) == {
        "environment": "development",
        "runtime": "distributed",
    }


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
    monkeypatch.setattr(
        "wisdome_writer.api.health.local_runtime_status",
        lambda **_kwargs: {
            "humanizer": {"status": "ready", "endpoint": "http://127.0.0.1:3210"},
            "workflow": {"status": "idle"},
        },
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
        "humanizer": "ok",
        "workflow_lock": "idle",
    }
    assert payload["local_runtime"]["workflow"] == {"status": "idle"}
    assert "outbox" not in payload


@pytest.mark.django_db
def test_local_ready_reports_file_root_as_unavailable(client, monkeypatch, tmp_path):
    """A probe cleanup error for a file root must not turn readiness into HTTP 500."""
    blocked_root = tmp_path / "state-file"
    blocked_root.write_text("not a directory")
    original_unlink = Path.unlink

    def reject_probe_cleanup(path, missing_ok=False):
        if path.parent == blocked_root:
            raise NotADirectoryError("state-file is not a directory")
        return original_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", reject_probe_cleanup)
    client.raise_request_exception = False
    monkeypatch.setattr(
        "wisdome_writer.api.health.local_runtime_status",
        lambda **_kwargs: {
            "humanizer": {"status": "unavailable", "endpoint": "http://127.0.0.1:3210"},
            "workflow": {"status": "active", "workflowId": "wf", "pid": 1},
        },
    )
    with override_settings(
        IS_LOCAL_RUNTIME=True,
        LOCAL_STATE_ROOT=blocked_root,
        LOCAL_ARTICLE_ROOT=tmp_path / "articles",
        LOCAL_OBJECT_ROOT=tmp_path / "objects",
    ):
        response = client.get("/health/ready")

    assert response.status_code == 503
    payload = response.json()
    assert payload["status"] == "unavailable"
    assert payload["checks"]["local_state"] == "unavailable"
    assert payload["checks"]["humanizer"] == "unavailable"
    assert payload["checks"]["workflow_lock"] == "active"
    assert payload["local_runtime"]["workflow"]["workflowId"] == "wf"


def test_workflow_status_detects_a_physically_held_guard(tmp_path: Path) -> None:
    """Reading stale metadata alone must never claim that a workflow is active."""

    from apps.local_content.status import workflow_lock_status
    from apps.local_content.workflow import _WorkflowGuard

    output_root = tmp_path / "output"
    state_root = tmp_path / "state"
    guard = _WorkflowGuard(
        output_root,
        state_root,
        "workflow-live",
        datetime.fromisoformat("2026-08-28T12:00:00+09:00"),
    )
    guard.acquire()
    try:
        active = workflow_lock_status(output_root=output_root, state_root=state_root)
    finally:
        guard.release()

    idle = workflow_lock_status(output_root=output_root, state_root=state_root)
    assert active == {"status": "active"}
    assert idle == {"status": "idle"}
