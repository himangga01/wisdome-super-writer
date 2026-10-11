from types import SimpleNamespace

import pytest
from django.test import Client, RequestFactory, override_settings


@pytest.fixture(autouse=True)
def portal_settings(settings):
    import importlib

    importlib.import_module("wisdome_writer.urls")
    settings.IS_LOCAL_RUNTIME = True
    settings.STORAGES = {
        **settings.STORAGES,
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    }


def test_root_is_a_portal_with_three_independent_workspaces():
    response = Client().get("/")
    assert response.status_code == 200
    assert response.context["portal"]["topic_key"] == "home"
    assert {item["href"] for item in response.context["portal"]["topics"]} == {
        "/housing/",
        "/semiconductor/",
        "/policy-funding/",
    }


def test_topic_navigation_is_selected_by_boundary_not_substring():
    from wisdome_writer.portal.navigation import portal_navigation

    factory = RequestFactory()
    housing = portal_navigation(factory.get("/housing/notices/"))["portal"]
    funding = portal_navigation(factory.get("/policy-funding/guide/"))["portal"]
    semi = portal_navigation(factory.get("/semiconductor/news/"))["portal"]
    assert housing["topic_key"] == "housing"
    assert funding["topic_key"] == "funding"
    assert semi["topic_key"] == "semiconductor"
    assert all(
        item["href"].startswith(("/housing/", "/local-articles/")) for item in housing["menu"]
    )
    assert all(item["href"].startswith("/policy-funding/") for item in funding["menu"])
    assert all(item["href"].startswith("/semiconductor/") for item in semi["menu"])
    assert sum(item["active"] for item in semi["menu"]) == 1
    assert portal_navigation(factory.get("/other/housing/"))["portal"]["topic_key"] == "home"


def test_housing_guard_runs_before_any_file_projection(monkeypatch):
    from django.http import Http404

    from wisdome_writer.portal import views

    def forbidden_read():
        pytest.fail("housing files were read before access checks")

    monkeypatch.setattr(views, "load_housing_snapshot", forbidden_read)
    request = RequestFactory().get("/housing/", REMOTE_ADDR="203.0.113.1")
    with pytest.raises(Http404):
        views.housing(request)
    request.META["REMOTE_ADDR"] = "127.0.0.1"
    request.user = SimpleNamespace(is_authenticated=True, is_staff=True, is_active=True)
    with override_settings(IS_LOCAL_RUNTIME=False), pytest.raises(Http404):
        views.housing(request)


def test_portal_remote_access_and_writes_are_denied_before_data(monkeypatch):
    from wisdome_writer.portal import views

    def forbidden_read(*args, **kwargs):
        pytest.fail("news data was read before access checks")

    monkeypatch.setattr(views, "load_semiconductor_data", forbidden_read)
    assert Client().get("/semiconductor/", REMOTE_ADDR="203.0.113.2").status_code == 403
    assert Client().post("/semiconductor/").status_code == 405
    assert Client().get("/semiconductor/news/?q=" + "a" * 201).status_code == 400


def test_housing_search_is_forwarded_before_projection_limit(monkeypatch):
    from wisdome_writer.portal import views

    seen = []

    def snapshot(query=""):
        seen.append(query)
        return {
            "available": True,
            "message": "",
            "total": 101,
            "matching_total": 1,
            "observed_at": None,
            "runs": [],
            "notices": [{"title": "unique-needle", "publisher": "LH", "status": "접수마감"}],
        }

    monkeypatch.setattr(views, "load_housing_snapshot", snapshot)
    response = Client().get("/housing/notices/?q=unique-needle")
    assert response.status_code == 200 and seen == ["unique-needle"]
    assert "unique-needle" in response.content.decode()
    assert response.context["data"]["total"] == 101


def test_funding_glossary_and_history_keep_readonly_boundary(tmp_path):
    with override_settings(POLICY_FUNDING_ROOT=tmp_path):
        for path in ("/policy-funding/glossary/", "/policy-funding/history/"):
            response = Client().get(path)
            assert response.status_code == 200
            assert response.context["portal"]["topic_key"] == "funding"
            assert Client().post(path).status_code == 405
            assert Client().get(path, REMOTE_ADDR="203.0.113.8").status_code == 403


@pytest.mark.parametrize(
    "path",
    [
        "/housing/",
        "/housing/notices/",
        "/housing/sources/",
        "/semiconductor/",
        "/semiconductor/news/",
        "/semiconductor/articles/",
        "/semiconductor/sources/",
    ],
)
def test_topic_pages_render_saved_data_and_escape_titles(path, monkeypatch):
    from datetime import UTC, datetime

    from selectolax.parser import HTMLParser

    from wisdome_writer.portal import views

    stamp = datetime(2026, 10, 8, tzinfo=UTC)
    title = "<img src=x onerror=alert(1)> " + "긴 제목 " * 30
    monkeypatch.setattr(
        views,
        "load_housing_snapshot",
        lambda query="": {
            "available": True,
            "message": "",
            "total": 1,
            "observed_at": stamp,
            "notices": [
                {
                    "title": title,
                    "publisher": "주택 출처",
                    "url": None,
                    "application_start": None,
                    "application_end": None,
                    "deadline": None,
                    "published_on": None,
                    "region": None,
                    "status": "접수마감",
                }
            ],
            "runs": [],
        },
    )
    monkeypatch.setattr(
        views,
        "load_semiconductor_data",
        lambda query="": {
            "available": True,
            "message": "",
            "total": 1,
            "last_seen": stamp,
            "news": [
                {
                    "title": title,
                    "publisher": "반도체 출처",
                    "url": None,
                    "published_at": None,
                    "first_collected_at": stamp,
                    "status_label": "수집됨",
                    "rights_label": "내부 분석용",
                }
            ],
            "drafts": [{"title": title, "summary": title, "state": "draft"}],
            "sources": [
                {
                    "display_name": title,
                    "publisher": "반도체 기관",
                    "url": None,
                    "enabled": False,
                    "rights_label": "이용 제한",
                }
            ],
            "runs": [],
        },
    )
    response = Client().get(path)
    assert response.status_code == 200
    tree = HTMLParser(response.content.decode())
    assert len(tree.css("h1")) == 1
    assert not tree.css("img[onerror]")
    assert tree.css_first('meta[name="viewport"]')
    assert tree.css_first(".mobile-menu summary")
    assert response["Cache-Control"] == "no-store"
    if path.startswith("/housing/"):
        assert response.context["portal"]["topic_key"] == "housing"
        assert "반도체 출처" not in response.content.decode()
        if path != "/housing/sources/":
            assert "저장 당시 표시" in response.content.decode()
            assert "접수마감" in response.content.decode()
    else:
        assert response.context["portal"]["topic_key"] == "semiconductor"
        assert "주택 출처" not in response.content.decode()
