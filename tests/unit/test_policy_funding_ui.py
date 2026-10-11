from datetime import date

import pytest
from django.test import Client, override_settings

from tests.unit.test_policy_funding_workflow import notice


@pytest.fixture(autouse=True)
def unbuilt_test_static_storage(settings):
    import importlib

    # Root routes are selected once at startup; warm them before temporary mode overrides.
    importlib.import_module("wisdome_writer.urls")
    # UI tests do not run collectstatic; real static collection is checked separately.
    settings.STORAGES = {
        **settings.STORAGES,
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    }


@override_settings(IS_LOCAL_RUNTIME=True)
def test_local_policy_pages_expose_learning_sources_and_filtering(tmp_path):
    from apps.policy_funding.workflow import publish_collection

    path = publish_collection(tmp_path, (notice(),), (), run_date=date(2026, 10, 5), mode="live")
    client = Client()
    with override_settings(POLICY_FUNDING_ROOT=tmp_path):
        index = client.get("/policy-funding/")
        assert index.status_code == 200
        assert "2026년 4분기" in index.content.decode()
        assert client.get("/policy-funding/?q=없는단어").context["notices"] == []
        assert client.get("/policy-funding/?region=seoul").status_code == 200
        detail = client.get(f"/policy-funding/notices/{notice().stable_id}/")
        assert detail.status_code == 200 and "확인서" in detail.content.decode()
        assert "승인" in detail.content.decode()
        guide = client.get("/policy-funding/guide/")
        assert guide.status_code == 200 and "대리대출" in guide.content.decode()
        directory = client.get("/policy-funding/sources/")
        assert directory.status_code == 200 and "경북" in directory.content.decode()
        assert client.get(f"/policy-funding/runs/{path.name}/").status_code == 200
        assert "Content-Security-Policy" in index


@override_settings(IS_LOCAL_RUNTIME=True)
def test_policy_ui_rejects_remote_local_access_and_writes(tmp_path):
    with override_settings(POLICY_FUNDING_ROOT=tmp_path):
        assert Client().get("/policy-funding/", REMOTE_ADDR="203.0.113.12").status_code == 403
        assert Client().post("/policy-funding/").status_code == 405


@pytest.mark.django_db
@override_settings(IS_LOCAL_RUNTIME=False)
def test_nonlocal_policy_ui_requires_active_staff(tmp_path, django_user_model):
    with override_settings(POLICY_FUNDING_ROOT=tmp_path):
        assert Client().get("/policy-funding/").status_code == 403
        user = django_user_model.objects.create_user(
            email="policy-staff@example.com", is_staff=True
        )
        client = Client()
        client.force_login(user)
        assert client.get("/policy-funding/").status_code == 200


def test_cli_fixture_run_is_visible_as_fixture_and_never_live(tmp_path):
    from django.core.management import call_command

    from apps.policy_funding.storage import list_runs

    call_command("collect_policy_funding", fixture=True, output=str(tmp_path), as_of="2026-10-05")
    rows = list_runs(tmp_path)
    assert len(rows) == 1
    assert rows[0]["mode"] == "fixture" and rows[0]["notices"]
    assert all(row.provenance == "fixture" for row in rows[0]["notices"])


def test_cli_region_selection_is_recorded_without_losing_national_notices(tmp_path):
    from django.core.management import call_command

    from apps.policy_funding.storage import list_runs

    call_command(
        "collect_policy_funding",
        fixture=True,
        output=str(tmp_path),
        as_of="2026-10-05",
        region="gyeongnam",
    )
    result = list_runs(tmp_path)[0]
    assert result["selection"] == {"region": "gyeongnam", "include_national": True}
    assert result["notices"]
    assert all("gyeongnam" in row.regions or "national" in row.regions for row in result["notices"])


@override_settings(IS_LOCAL_RUNTIME=True)
def test_run_notice_link_reads_the_selected_historical_snapshot(tmp_path):
    from dataclasses import replace

    from apps.policy_funding.workflow import publish_collection

    first = publish_collection(tmp_path, (notice(),), (), run_date=date(2026, 10, 5), mode="live")
    publish_collection(
        tmp_path,
        (replace(notice(), application_period="마감으로 변경"),),
        (),
        run_date=date(2026, 10, 5),
        mode="live",
    )
    with override_settings(POLICY_FUNDING_ROOT=tmp_path):
        result = Client().get(f"/policy-funding/notices/{notice().stable_id}/?run={first.name}")
    assert result.context["notice"].application_period == notice().application_period
