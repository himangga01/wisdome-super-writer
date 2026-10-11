"""Read projection regressions for the semiconductor operator portal."""

import importlib
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from django.db import OperationalError, connection
from django.test.utils import CaptureQueriesContext

from apps.collection.models import CollectionRun, SourceItem
from apps.editorial.models import ArticleRevision, DraftArticle, EditorialPolicySnapshot
from apps.topics.models import SourceDefinition, SourceRegistrySnapshot, TopicPolicy

TOPIC = "semiconductor_news"
NOW = datetime(2026, 10, 8, 1, 0, tzinfo=UTC)
SHA = "a" * 64


@pytest.fixture(autouse=True)
def isolated_local_archive(tmp_path, settings):
    settings.SEMICONDUCTOR_ARCHIVE_ROOT = tmp_path / "semiconductor"


def _load(query=""):
    return importlib.import_module("wisdome_writer.portal.semiconductor").load_semiconductor_data(
        query
    )


def _source(topic=TOPIC, **changes):
    values = {
        "topic_code": topic,
        "key": uuid.uuid4().hex,
        "display_name": "Chip newsroom" if topic == TOPIC else "Housing authority",
        "publisher": "Chip publisher",
        "owner_name": "Owner",
        "editorial_control_name": "Editor",
        "base_url": "https://news.example.test/",
        "authority_tier": "primary_corporate",
        "access_method": "rss_atom",
        "independence_group": "chips",
        "adapter_key": "test",
        "default_rights_status": "internal_analysis_only",
        "external_config": {"private": "must-not-escape"},
        "secret_ref": "private-reference-must-not-escape",
    }
    return SourceDefinition.objects.create(**(values | changes))


def _item(source, **changes):
    values = {
        "source": source,
        "external_id": uuid.uuid4().hex,
        "canonical_url": "https://news.example.test/chips",
        "title": "Chip production update",
        "publisher": "Chip publisher",
        "published_at": NOW - timedelta(days=1),
        "first_collected_at": NOW,
        "content_hash": SHA,
        "source_version_hash": uuid.uuid4().hex * 2,
        "metadata": {"private": "must-not-escape"},
        "body_text": "body-must-not-escape",
    }
    # Stored legacy URLs can predate today's validators; the projection must
    # validate them even though ingestion normally enforces stronger constraints.
    item = SourceItem(**(values | changes))
    SourceItem.objects.bulk_create([item])
    return item


def _run(topic=TOPIC):
    policy = TopicPolicy.objects.create(code=topic, version=1, title=topic, policy_hash=SHA)
    registry = SourceRegistrySnapshot.objects.create(topic_code=topic, version=1, manifest_hash=SHA)
    return CollectionRun.objects.create(
        display_id="RUN-" + uuid.uuid4().hex[:8],
        topic_code=topic,
        source_registry=registry,
        registry_manifest_hash=SHA,
        topic_policy=policy,
        policy_version=1,
        policy_hash=SHA,
        freshness_minutes=720,
        freshness_cutoff=NOW - timedelta(hours=12),
        window_start=NOW - timedelta(days=1),
        window_end=NOW,
        request_fingerprint=uuid.uuid4().hex * 2,
    )


def _draft(run, title, *, revision=True):
    article = DraftArticle.objects.create(
        topic_code=run.topic_code,
        article_identity_key=uuid.uuid4().hex,
        article_type="semiconductor_daily_digest",
        source_run=run,
    )
    if revision:
        policy = EditorialPolicySnapshot(
            topic_code=run.topic_code,
            policy_key=uuid.uuid4().hex,
            policy_version="1",
            document={},
            release_document_hash=SHA,
            config_hash=SHA,
            implementation_manifest={},
            implementation_manifest_hash=SHA,
            material_hash=SHA,
        )
        EditorialPolicySnapshot.objects.bulk_create([policy])
        current = ArticleRevision(
            article=article,
            origin_run=run,
            revision_no=1,
            editorial_policy_snapshot=policy,
            title=title,
            summary="A saved summary",
            body_markdown="private-draft-body",
            input_manifest_hash=SHA,
            claim_manifest_hash=SHA,
            quality_manifest_hash=SHA,
        )
        ArticleRevision.objects.bulk_create([current])
        article.current_revision = current
        article.save(update_fields=["current_revision"])
    return article


@pytest.mark.django_db
def test_stored_versions_exclude_other_topics_and_retention_tombstones():
    source = _source()
    first = _item(source, external_id="same-story", published_at=None, status="corrected")
    _item(source, external_id="same-story", first_collected_at=NOW + timedelta(hours=1))
    _item(source, title="Removed", retention_tombstoned_at=NOW)
    _item(_source("housing_subscription"), title="Housing must not escape")

    with CaptureQueriesContext(connection) as queries:
        data = _load()

    assert data["available"] is True
    assert data["total"] == 2
    assert len(data["news"]) == 2
    assert data["last_seen"] == NOW + timedelta(hours=1)
    assert {row["display_name"] for row in data["sources"]} == {"Chip newsroom"}
    corrected = next(row for row in data["news"] if row["status"] == "corrected")
    assert corrected["published_at"] is None
    assert corrected["first_collected_at"] == first.first_collected_at
    assert corrected["status_label"] == "정정"
    assert corrected["rights_label"] == "내부 분석 전용"
    assert "must-not-escape" not in str(data)
    assert "body-must-not-escape" not in str(data)
    assert all(q["sql"].lstrip().upper().startswith("SELECT") for q in queries)


@pytest.mark.django_db
def test_query_is_server_scoped_and_missing_current_revision_is_not_a_readable_draft():
    source = _source()
    _item(source, title="Memory outlook")
    _item(source, title="Other chip update")
    _item(_source("housing_subscription"), title="Memory apartment")
    chip_run = _run()
    _draft(chip_run, "Memory draft")
    _draft(chip_run, "No current revision", revision=False)
    housing_run = _run("housing_subscription")
    _draft(housing_run, "Memory housing draft")

    data = _load("memory")

    assert data["total"] == 1
    assert [row["title"] for row in data["news"]] == ["Memory outlook"]
    assert [row["title"] for row in data["drafts"]] == ["Memory draft"]
    assert [row["id"] for row in data["runs"]] == [str(chip_run.pk)]
    assert "private-draft-body" not in str(data)


@pytest.mark.django_db
def test_available_empty_database_has_zero_records_and_no_fabricated_news():
    data = _load()
    assert data["available"] is True
    assert data["total"] == 0
    assert data["news"] == data["drafts"] == data["runs"] == []
    assert data["last_seen"] is None


@pytest.mark.django_db
def test_database_failure_is_unavailable_and_only_returns_labelled_catalog(monkeypatch):
    def fail(*args, **kwargs):
        raise OperationalError("private database path must-not-escape")

    monkeypatch.setattr(SourceItem.objects, "filter", fail)
    data = _load()
    assert data["available"] is False
    assert data["total"] is None
    assert data["last_seen"] is None
    assert data["news"] == data["drafts"] == data["runs"] == []
    assert "설정" in data["message"]
    assert "must-not-escape" not in str(data)
    assert len(data["sources"]) == 5
    skhynix = next(row for row in data["sources"] if row["display_name"] == "SK hynix Newsroom")
    assert skhynix["enabled"] is False
    assert skhynix["rights_label"] == "금지"
    assert set(skhynix) == {"display_name", "publisher", "url", "enabled", "rights_label"}


@pytest.mark.django_db
@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "//attacker.test/news",
        "http://news.example.test/news",
        "https://news.example.test.attacker.test/news",
        "https://user:pass@news.example.test/news",
        "https://news.example.test:444/news",
        "https://news.example.test\\@attacker.test/news",
        "https://news.example.test/\nnews",
    ],
)
def test_unsafe_or_foreign_original_url_is_not_clickable(url):
    _item(_source(), canonical_url=url)
    assert _load()["news"][0]["url"] is None


@pytest.mark.django_db
def test_safe_original_url_and_retraction_remain_visible():
    url = "https://news.example.test/chips?id=42#details"
    _item(_source(), canonical_url=url, status="retracted")
    row = _load()["news"][0]
    assert row["url"] == url
    assert row["status_label"] == "철회"


@pytest.mark.django_db
def test_non_database_programming_failure_is_not_disguised_as_empty(monkeypatch):
    def fail(*args, **kwargs):
        raise ValueError("projection programming failure")

    monkeypatch.setattr(SourceItem.objects, "filter", fail)
    with pytest.raises(ValueError, match="programming failure"):
        _load()


@pytest.mark.django_db
def test_cross_linked_revision_cannot_disclose_another_topics_draft():
    chip_article = _draft(_run(), "Chip draft", revision=False)
    other_article = _draft(_run("housing_subscription"), "Housing private draft")
    chip_article.current_revision = other_article.current_revision
    chip_article.save(update_fields=["current_revision"])

    assert _load()["drafts"] == []


@pytest.mark.django_db
def test_late_database_failure_does_not_return_partially_loaded_rows():
    _item(_source())

    def unavailable_editorial(execute, sql, params, many, context):
        if "editorial_draftarticle" in sql:
            raise OperationalError("schema missing")
        return execute(sql, params, many, context)

    with connection.execute_wrapper(unavailable_editorial):
        data = _load()

    assert data["available"] is False
    assert data["total"] is None
    assert data["news"] == data["drafts"] == data["runs"] == []
    assert data["last_seen"] is None


@pytest.mark.django_db
def test_original_publication_month_filters_before_limit_and_counts():
    from wisdome_writer.portal.periods import PeriodSelection

    source = _source()
    for index in range(61):
        _item(source, title=f"September {index}", published_at=NOW - timedelta(days=20))
    _item(source, title="October edge", published_at=datetime(2026, 9, 30, 15, tzinfo=UTC))
    _item(source, title="Before October", published_at=datetime(2026, 9, 30, 14, 59, tzinfo=UTC))
    _item(source, title="November edge", published_at=datetime(2026, 10, 31, 15, tzinfo=UTC))
    _item(source, title="Unknown", published_at=None)
    loader = importlib.import_module("wisdome_writer.portal.semiconductor").load_semiconductor_data
    data = loader(period=PeriodSelection(2026, 10))
    assert data["total"] == 1
    assert [row["title"] for row in data["news"]] == ["October edge"]
    assert data["unknown_date_count"] == 1
    assert "발표일 미상" in data["message"]
    collected = loader(period=PeriodSelection(2026, 10, basis="collected"))
    assert collected["total"] == 65


@pytest.mark.django_db
def test_local_metadata_fallback_is_real_fixture_provenance_not_database_versions(
    tmp_path, settings
):
    from tests.unit.test_news_archive import NOW as fixture_now
    from tests.unit.test_news_archive import FixtureFetcher
    from wisdome_writer.portal.news_archive import collect_news_archive
    from wisdome_writer.portal.periods import PeriodSelection

    settings.IS_LOCAL_RUNTIME = True
    settings.SEMICONDUCTOR_ARCHIVE_ROOT = tmp_path
    collect_news_archive(tmp_path, 2026, 10, now=fixture_now, fetcher_factory=FixtureFetcher)
    loader = importlib.import_module("wisdome_writer.portal.semiconductor").load_semiconductor_data
    data = loader(period=PeriodSelection(2026, 10))
    assert data["available"] is True
    assert data["total"] == 3
    assert data["inventory_kind"] == "local_metadata"
    assert data["mode"] == "fixture"
    assert data["drafts"] == data["runs"] == []
    assert SourceItem.objects.count() == CollectionRun.objects.count() == 0
    settings.IS_LOCAL_RUNTIME = False
    assert loader(period=PeriodSelection(2026, 10))["total"] == 0


@pytest.mark.django_db
def test_current_drafts_filter_by_updated_month_without_using_news_dates():
    from wisdome_writer.portal.periods import PeriodSelection

    old = _draft(_run(), "Older draft")
    current = _draft(CollectionRun.objects.first(), "October draft")
    DraftArticle.objects.filter(pk=old.pk).update(updated_at=NOW - timedelta(days=20))
    DraftArticle.objects.filter(pk=current.pk).update(updated_at=NOW)
    loader = importlib.import_module("wisdome_writer.portal.semiconductor").load_semiconductor_data
    assert [row["title"] for row in loader(period=PeriodSelection(2026, 10))["drafts"]] == [
        "October draft"
    ]


@pytest.mark.django_db
def test_selected_period_empty_archive_is_available_when_database_schema_is_missing(
    tmp_path, settings, monkeypatch
):
    from tests.unit.test_news_archive import NOW as fixture_now
    from tests.unit.test_news_archive import FixtureFetcher
    from wisdome_writer.portal.news_archive import collect_news_archive
    from wisdome_writer.portal.periods import PeriodSelection

    settings.IS_LOCAL_RUNTIME = True
    settings.SEMICONDUCTOR_ARCHIVE_ROOT = tmp_path
    collect_news_archive(tmp_path, 2026, 10, now=fixture_now, fetcher_factory=FixtureFetcher)

    def fail(*args, **kwargs):
        raise OperationalError("schema unavailable")

    monkeypatch.setattr(SourceItem.objects, "filter", fail)
    loader = importlib.import_module("wisdome_writer.portal.semiconductor").load_semiconductor_data
    data = loader(period=PeriodSelection(2026, 8))
    assert data["available"] is True
    assert data["total"] == 0
    assert data["inventory_kind"] == "local_metadata"
    assert "선택" in data["message"]


@pytest.mark.django_db
def test_auto_local_inventory_precedes_database_versions_without_mixing_counts(tmp_path, settings):
    from tests.unit.test_news_archive import NOW as fixture_now
    from tests.unit.test_news_archive import FixtureFetcher
    from wisdome_writer.portal.news_archive import collect_news_archive
    from wisdome_writer.portal.periods import PeriodSelection

    settings.IS_LOCAL_RUNTIME = True
    settings.SEMICONDUCTOR_ARCHIVE_ROOT = tmp_path
    _item(_source(), title="Database version")
    draft = _draft(_run(), "Stored draft")
    DraftArticle.objects.filter(pk=draft.pk).update(updated_at=NOW)
    collect_news_archive(tmp_path, 2026, 10, now=fixture_now, fetcher_factory=FixtureFetcher)
    loader = importlib.import_module("wisdome_writer.portal.semiconductor").load_semiconductor_data
    data = loader(period=PeriodSelection(2026, 10))
    assert data["inventory_kind"] == "local_metadata"
    assert data["total"] == 3
    assert "Database version" not in {row["title"] for row in data["news"]}
    assert [row["title"] for row in data["drafts"]] == ["Stored draft"]
    database = loader(period=PeriodSelection(2026, 10), inventory="database")
    assert database["inventory_kind"] == "database_versions"
    assert database["total"] == 1
    assert database["news"][0]["title"] == "Database version"


@pytest.mark.django_db
def test_auto_failed_local_inventory_can_fall_back_to_healthy_database(tmp_path, settings):
    from tests.unit.test_news_archive import NOW as fixture_now
    from tests.unit.test_news_archive import FailedFetcher
    from wisdome_writer.portal.news_archive import collect_news_archive
    from wisdome_writer.portal.periods import PeriodSelection

    settings.IS_LOCAL_RUNTIME = True
    settings.SEMICONDUCTOR_ARCHIVE_ROOT = tmp_path
    _item(_source(), title="Database version")
    collect_news_archive(tmp_path, 2026, 10, now=fixture_now, fetcher_factory=FailedFetcher)
    loader = importlib.import_module("wisdome_writer.portal.semiconductor").load_semiconductor_data
    data = loader(period=PeriodSelection(2026, 10))
    assert data["inventory_kind"] == "database_versions"
    assert data["total"] == 1
    assert "실패" in data["message"]
    explicit = loader(period=PeriodSelection(2026, 10), inventory="local")
    assert explicit["available"] is False
    assert explicit["total"] is None
    assert explicit["inventory_kind"] == "local_metadata"


@pytest.mark.django_db
def test_nonlocal_explicit_inventory_never_reads_private_local_archive(monkeypatch, settings):
    settings.IS_LOCAL_RUNTIME = False
    module = importlib.import_module("wisdome_writer.portal.semiconductor")

    def forbidden(*args, **kwargs):
        raise AssertionError("private local archive read")

    monkeypatch.setattr(module, "_local_archive", forbidden)
    data = module.load_semiconductor_data(inventory="local")
    assert data["available"] is False
    assert data["total"] is None
