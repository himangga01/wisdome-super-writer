import importlib

import pytest
from django.test import Client


@pytest.fixture(autouse=True)
def monthly_settings(settings, tmp_path):
    importlib.import_module("wisdome_writer.urls")
    settings.IS_LOCAL_RUNTIME = True
    settings.LOCAL_ARTICLE_ROOT = tmp_path / "housing"
    settings.POLICY_FUNDING_ROOT = tmp_path / "funding"
    settings.SEMICONDUCTOR_ARCHIVE_ROOT = tmp_path / "semiconductor"
    settings.STORAGES = {
        **settings.STORAGES,
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    }


def test_housing_month_uses_special_and_first_dates_with_stage_choice(monkeypatch):
    from wisdome_writer.portal import views

    seen = []

    def data(query="", period=None, stage="all"):
        seen.append((period.year, period.month, period.basis, stage))
        return {
            "available": True,
            "message": "",
            "total": 1,
            "matching_total": 1,
            "notices": [
                {
                    "title": "달력 공고",
                    "publisher": "청약홈",
                    "url": None,
                    "subscription_events": [
                        {
                            "kind": "special",
                            "label": "특별공급",
                            "text": "2026-10-12",
                            "dates": ["2026-10-12"],
                        }
                    ],
                }
            ],
            "runs": [],
            "observed_at": None,
            "unknown_schedule_count": 0,
        }

    monkeypatch.setattr(views, "load_housing_snapshot", data)
    response = Client().get("/housing/notices/?year=2026&month=10&stage=special")
    assert response.status_code == 200
    assert seen == [(2026, 10, "schedule", "special")]
    body = response.content.decode()
    assert "특별공급" in body and "2026-10-12" in body
    assert 'name="year"' in body and 'name="month"' in body
    assert response.context["period_ui"]["label"] == "2026년 10월"


@pytest.mark.parametrize("path", ["/housing/notices/", "/semiconductor/news/", "/policy-funding/"])
def test_invalid_month_is_bad_request_before_source_reads(path):
    assert Client().get(path + "?year=2026&month=13").status_code == 400


def test_funding_date_controls_explicitly_use_posting_date():
    response = Client().get("/policy-funding/?year=2026&month=10")
    assert response.status_code == 200
    assert response.context["period_selection"].basis == "published"
    assert "공고 게시·등록일" in response.content.decode()
    assert Client().get("/policy-funding/?year=2026&month=10&basis=schedule").status_code == 400


def test_housing_stage_and_month_are_preserved_on_search_and_month_links():
    response = Client().get("/housing/notices/?year=2026&month=10&stage=first&q=서울")
    assert response.status_code == 200
    ui = response.context["period_ui"]
    assert "stage=first" in ui["previous_url"]
    assert {"name": "q", "value": "서울"} in ui["preserved"]
    assert "month=9" in ui["previous_url"]


def test_invalid_stage_is_rejected():
    assert Client().get("/housing/notices/?stage=second").status_code == 400
    assert Client().get("/housing/notices/?year=2026&month=10&basis=collected").status_code == 400


def test_stage_choice_limits_visible_dates_to_that_stage(monkeypatch):
    from selectolax.parser import HTMLParser

    from wisdome_writer.portal import views

    def data(query="", period=None, stage="all"):
        return {
            "available": True,
            "message": "",
            "total": 1,
            "matching_total": 1,
            "runs": [],
            "observed_at": None,
            "notices": [
                {
                    "title": "확인된 단계",
                    "publisher": "청약홈",
                    "subscription_events": [
                        {"kind": "special", "label": "특별공급", "text": "2026-10-19"},
                        {"kind": "first", "label": "1순위 해당지역", "text": "2026-10-20"},
                    ],
                }
            ],
        }

    monkeypatch.setattr(views, "load_housing_snapshot", data)
    response = Client().get("/housing/notices/?year=2026&month=10&stage=first")
    tree = HTMLParser(response.content.decode())
    assert len(tree.css(".subscription-event.event-first")) == 1
    assert not tree.css(".subscription-event.event-special")


def test_historical_funding_card_opens_the_selected_saved_version(tmp_path, settings):
    from dataclasses import replace
    from datetime import date
    from urllib.parse import parse_qs, urlsplit

    from selectolax.parser import HTMLParser

    from apps.policy_funding.workflow import publish_collection
    from tests.unit.test_policy_funding_workflow import notice

    settings.POLICY_FUNDING_ROOT = tmp_path / "funding"
    older = replace(notice(), title="SEPTEMBER ORIGINAL", published_on=date(2026, 9, 20))
    newer = replace(older, title="OCTOBER CORRECTION", published_on=date(2026, 10, 6))
    first = publish_collection(
        settings.POLICY_FUNDING_ROOT, (older,), (), run_date=date(2026, 9, 20), mode="fixture"
    )
    publish_collection(
        settings.POLICY_FUNDING_ROOT, (newer,), (), run_date=date(2026, 10, 8), mode="fixture"
    )
    selection = {
        "year": "2026", "month": "9", "mode": "fixture", "basis": "published",
        "q": "SEPTEMBER ORIGINAL", "region": "seoul", "kind": "agency_loan",
    }
    response = Client().get("/policy-funding/", selection)
    link = HTMLParser(response.content.decode()).css_first(".notice-card h3 a").attributes["href"]
    detail = Client().get(link)
    assert detail.status_code == 200
    assert "SEPTEMBER ORIGINAL" in detail.content.decode()
    assert "OCTOBER CORRECTION" not in detail.content.decode()
    assert "run=" + first.name in link
    back = HTMLParser(detail.content.decode()).css_first("a.text-link").attributes["href"]
    assert urlsplit(back).path == "/policy-funding/"
    assert parse_qs(urlsplit(back).query) == {key: [value] for key, value in selection.items()}
    returned = Client().get(back)
    assert returned.status_code == 200
    assert returned.context["total"] == 1
    assert returned.context["period_selection"].month == 9


def test_news_inventory_switch_preserves_month_and_has_explicit_choices(monkeypatch):
    from wisdome_writer.portal import views

    seen = []

    def data(query="", period=None, inventory="auto"):
        seen.append(inventory)
        return {
            "available": True,
            "message": "",
            "total": 0,
            "news": [],
            "drafts": [],
            "sources": [],
            "runs": [],
            "last_seen": None,
        }

    monkeypatch.setattr(views, "load_semiconductor_data", data)
    response = Client().get("/semiconductor/news/?year=2026&month=10&inventory=local")
    assert response.status_code == 200 and seen == ["local"]
    assert 'name="inventory"' in response.content.decode()
    assert "inventory=local" in response.context["period_ui"]["previous_url"]
    assert Client().get("/semiconductor/news/?inventory=invalid").status_code == 400


@pytest.mark.parametrize(
    "path,basis,excluded_note",
    [
        ("/semiconductor/news/", "published", True),
        ("/semiconductor/news/", "collected", False),
        ("/semiconductor/articles/", "published", False),
        ("/semiconductor/sources/", "published", False),
    ],
)
def test_unknown_publication_note_only_describes_excluded_news(
    monkeypatch, path, basis, excluded_note
):
    from wisdome_writer.portal import views

    def data(query="", period=None, inventory="auto"):
        return {
            "available": True,
            "total": 1,
            "news": [{"title": "Undated captured item"}],
            "drafts": [],
            "sources": [],
            "runs": [],
            "unknown_date_count": 1,
        }

    monkeypatch.setattr(views, "load_semiconductor_data", data)
    response = Client().get(path, {"year": "2026", "month": "10", "basis": basis})
    assert response.status_code == 200
    body = response.content.decode()
    assert ("선택한 발표월 결과에서 제외했습니다." in body) is excluded_note
    if basis == "collected":
        assert "Undated captured item" in body


@pytest.mark.parametrize("path", ["/semiconductor/articles/", "/semiconductor/sources/"])
def test_news_archive_messages_do_not_describe_other_sections(
    settings, monkeypatch, path
):
    from django.db import OperationalError

    from apps.collection.models import SourceItem
    from tests.unit.test_news_archive import NOW, FixtureFetcher
    from wisdome_writer.portal.news_archive import collect_news_archive

    collect_news_archive(
        settings.SEMICONDUCTOR_ARCHIVE_ROOT, 2026, 10,
        now=NOW, fetcher_factory=FixtureFetcher,
    )

    def unavailable(*args, **kwargs):
        raise OperationalError("unavailable private database")

    monkeypatch.setattr(SourceItem.objects, "filter", unavailable)
    response = Client().get(path, {"year": "2026", "month": "10", "inventory": "local"})
    assert response.status_code == 200
    body = response.content.decode()
    assert "발표일 미상" not in body
    if path.endswith("articles/"):
        assert "초안 데이터를 불러올 수 없어요" in body
        assert response.context["data"]["available"] is False
    news = Client().get("/semiconductor/news/?year=2026&month=10&inventory=local")
    assert news.status_code == 200
    assert news.context["data"]["available"] is True
    assert news.context["data"]["total"] == 3
