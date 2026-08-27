# Dockerless Local Housing Writer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run Wisdome Super Writer without Docker, collect the latest seven KST calendar days of official housing notices, and produce rights-safe, humanized Markdown article bundles that a local Django server can preview.

**Architecture:** Add a development-only local runtime backed by SQLite and the filesystem, plus a synchronous `apps.local_content` workflow isolated from distributed publication. Official ApplyHome and LH HTML collectors normalize notices, deterministic renderers create factual Markdown and images, and a localhost HTTP adapter humanizes only protected prose blocks before strict verification and atomic bundle publication.

**Tech Stack:** Python 3.12, Django 5.2, SQLite WAL, httpx, selectolax, Pillow, pytest/pytest-django, Ruff, PowerShell, GitHub Actions, Node-based `ai-text-makes-likes-human`, Playwright/Brave for live acceptance.

**Spec:** `docs/superpowers/specs/2026-08-27-dockerless-local-housing-writer-design.md`

## Global Constraints

- Local execution must not invoke Docker, Redis, PostgreSQL, MinIO, Celery workers, OCR, HWP, WordPress, or Blogger.
- Local mode is valid only with `WISDOME_ENVIRONMENT=development`, `WISDOME_RUNTIME_MODE=local`, and loopback HTTP binds.
- Python must satisfy `>=3.12,<3.13`; dependency versions continue to come from `uv.lock`.
- The live collection window is seven inclusive KST calendar dates: run date minus six days through the actual execution time.
- Publication date admits a notice; modification date never admits an older notice.
- Official source text is untrusted input. Existing host, path, HTTPS, timeout, MIME, size, robots, retry, and redaction boundaries remain in force.
- No rights-uncleared official attachment image may be copied to an article bundle.
- Humanization must never alter numbers, dates, proper names, URLs, citation markers, eligibility phrases, or Markdown structure.
- A failed source, detail, image, or humanization check is visible and fail-closed; it is never represented as a complete successful article.
- Every production-code change follows RED → GREEN → REFACTOR, with the failing output observed before implementation.
- The approved scope is one weekly index containing every matching residential notice plus detailed articles for sale, remaining, optional, manually selected, and relevant correction notices.

---

## File Map

```text
src/wisdome_writer/settings/__init__.py        runtime-mode settings and local readiness inputs
src/wisdome_writer/infrastructure/queues.py    canonical Celery queue registry
src/wisdome_writer/infrastructure/event_routes.py queue references
src/wisdome_writer/api/health.py               runtime-aware readiness
src/wisdome_writer/urls.py                     development-only local preview routes

src/apps/local_content/apps.py                 Django app registration
src/apps/local_content/contracts.py            immutable normalized notice/run/artifact types
src/apps/local_content/dates.py                KST seven-day window and publication admission
src/apps/local_content/http.py                 official-page bounded fetch protocol/implementation
src/apps/local_content/sources/applyhome.py     ApplyHome public HTML collector
src/apps/local_content/sources/lh.py            LH public HTML collector
src/apps/local_content/selection.py             residential and detailed-article policies
src/apps/local_content/workflow.py              synchronous collection/orchestration
src/apps/local_content/rendering.py             deterministic index/detail Markdown
src/apps/local_content/images.py                generic hero and fact/timeline cards
src/apps/local_content/humanizer.py             NDJSON client and protected prose verifier
src/apps/local_content/bundles.py               atomic immutable filesystem bundles
src/apps/local_content/views.py                 loopback preview/status views
src/apps/local_content/urls.py                  preview routes
src/apps/local_content/management/commands/collect_recent_housing.py

src/templates/local_articles/                   preview templates
src/static/local_articles/generic-housing-hero.png rights-safe generic image asset
scripts/toolchain-lock.json                      pinned uv download metadata
scripts/setup-local.ps1                          Dockerless bootstrap
scripts/start-local.ps1                          humanizer and Django supervisor
.env.local.example                               safe local defaults
.github/workflows/quality.yml                    Dockerless CI

tests/unit/test_queue_configuration.py
tests/unit/test_local_runtime.py
tests/unit/test_local_content_dates.py
tests/unit/test_applyhome_public_html.py
tests/unit/test_lh_public_html.py
tests/unit/test_local_content_selection.py
tests/unit/test_local_content_rendering.py
tests/unit/test_local_content_images.py
tests/unit/test_local_content_humanizer.py
tests/unit/test_local_content_bundles.py
tests/unit/test_local_content_preview.py
tests/integration/test_local_housing_workflow.py
```

---

## Execution Bootstrap Before Task 1

The repository does not currently have a compatible Python 3.12 interpreter or `uv`. Provision the temporary development toolchain before the first RED test; Task 11 replaces this manual bootstrap with the checked-in deterministic script.

```powershell
python -m pip install --user uv
python -m uv python install 3.12
python -m uv sync --frozen --extra dev --python 3.12
.\.venv\Scripts\python.exe --version
```

Expected: the final command reports Python 3.12.x. Do not change `pyproject.toml` to admit Python 3.14 and do not run tests under the installed Python 3.14 interpreter.

---

### Task 1: Canonical Queue Registry and Broken Route Repair

**Files:**
- Create: `src/wisdome_writer/infrastructure/queues.py`
- Modify: `src/wisdome_writer/settings/__init__.py:358-401`
- Modify: `src/wisdome_writer/infrastructure/event_routes.py:141-265`
- Test: `tests/unit/test_queue_configuration.py`

**Interfaces:**
- Produces: `CELERY_QUEUE_NAMES: tuple[str, ...]`, `STATIC_EVENT_QUEUE_NAMES: frozenset[str]`
- Consumes: existing `EVENT_ROUTES` and `queue_for()`

- [ ] **Step 1: Write the failing queue-invariant tests**

```python
from django.conf import settings

from wisdome_writer.infrastructure.event_routes import EVENT_ROUTES
from wisdome_writer.infrastructure.queues import CELERY_QUEUE_NAMES


def test_every_static_event_queue_is_declared():
    routed = {route.queue for route in EVENT_ROUTES.values()}
    assert routed <= set(CELERY_QUEUE_NAMES)


def test_queue_registry_matches_django_settings():
    assert tuple(queue.name for queue in settings.CELERY_TASK_QUEUES) == CELERY_QUEUE_NAMES


def test_invalidation_events_use_consumed_maintenance_queue():
    assert EVENT_ROUTES[("evidence.profile_decided", 1)].queue == "maintenance"
    assert EVENT_ROUTES[("topics.registry_decided", 1)].queue == "maintenance"
```

- [ ] **Step 2: Run RED**

Run:

```powershell
$env:WISDOME_ENVIRONMENT='development'
$env:WISDOME_RUNTIME_MODE='local'
py -3.12 -m pytest tests/unit/test_queue_configuration.py -q
```

Expected: import failure for `infrastructure.queues` or assertions showing `source.change` and `publishing` are undeclared.

- [ ] **Step 3: Add the canonical registry and route fixes**

```python
# wisdome_writer/infrastructure/queues.py
CELERY_QUEUE_NAMES = (
    "outbox.dispatch",
    "source.check",
    "source.change",
    "collect.housing",
    "collect.semiconductor",
    "extract.fanout",
    "extract.document",
    "extract.generic",
    "extract.ocr.paddle",
    "editorial",
    "publish.media.wordpress",
    "publish.wordpress",
    "publish.blogger",
    "reconcile",
    "maintenance",
)
```

Import this tuple into settings. Change only the two database-only invalidation routes from `publishing` to `maintenance`; retain `source.item_changed -> source.change` and declare it.

- [ ] **Step 4: Run GREEN and focused existing route tests**

```powershell
py -3.12 -m pytest tests/unit/test_queue_configuration.py tests/unit/test_auto_publish_server_material.py -q
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

```powershell
git add src/wisdome_writer/infrastructure/queues.py src/wisdome_writer/settings/__init__.py src/wisdome_writer/infrastructure/event_routes.py tests/unit/test_queue_configuration.py
git commit -m "fix: make event queue registry consistent"
```

---

### Task 2: Dockerless Local Runtime Settings and Readiness

**Files:**
- Modify: `src/wisdome_writer/settings/__init__.py`
- Modify: `src/wisdome_writer/api/health.py`
- Modify: `.gitignore`
- Create: `.env.local.example`
- Test: `tests/unit/test_local_runtime.py`

**Interfaces:**
- Produces: `WISDOME_RUNTIME_MODE`, `IS_LOCAL_RUNTIME`, `LOCAL_STATE_ROOT`, `LOCAL_ARTICLE_ROOT`, `LOCAL_OBJECT_ROOT`, `HUMANIZER_BASE_URL`
- Produces: local readiness payload without Redis

- [ ] **Step 1: Write failing settings and health tests**

```python
import os
from pathlib import Path

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.test import override_settings


def test_local_runtime_uses_repository_local_paths(settings):
    assert settings.WISDOME_RUNTIME_MODE == "local"
    assert settings.IS_LOCAL_RUNTIME is True
    assert Path(settings.LOCAL_ARTICLE_ROOT).is_absolute()
    assert settings.CELERY_TASK_ALWAYS_EAGER is True


def test_production_rejects_local_runtime(settings):
    settings.WISDOME_ENVIRONMENT = "production"
    settings.WISDOME_RUNTIME_MODE = "local"
    with pytest.raises(ImproperlyConfigured):
        settings.validate_runtime_mode()


@override_settings(IS_LOCAL_RUNTIME=True)
def test_local_ready_does_not_require_redis(client, monkeypatch):
    monkeypatch.setattr(
        "wisdome_writer.api.health.Redis.from_url",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("Redis must not be used")),
    )
    response = client.get("/health/ready")
    assert response.status_code in {200, 503}
```

- [ ] **Step 2: Run RED**

```powershell
py -3.12 -m pytest tests/unit/test_local_runtime.py -q
```

Expected: missing settings and runtime-aware health behavior.

- [ ] **Step 3: Implement local runtime settings**

```python
WISDOME_RUNTIME_MODE = env_value("WISDOME_RUNTIME_MODE", "local" if not IS_PRODUCTION else "distributed")
if WISDOME_RUNTIME_MODE not in {"local", "distributed"}:
    raise ImproperlyConfigured("WISDOME_RUNTIME_MODE must be 'local' or 'distributed'.")
IS_LOCAL_RUNTIME = WISDOME_RUNTIME_MODE == "local"

def validate_runtime_mode() -> None:
    if IS_PRODUCTION and IS_LOCAL_RUNTIME:
        raise ImproperlyConfigured("The local runtime is development-only.")

LOCAL_STATE_ROOT = Path(env_value("LOCAL_STATE_ROOT", str(REPOSITORY_ROOT / ".local" / "state"))).resolve()
LOCAL_ARTICLE_ROOT = Path(env_value("LOCAL_ARTICLE_ROOT", str(REPOSITORY_ROOT / "output" / "housing"))).resolve()
LOCAL_OBJECT_ROOT = Path(env_value("LOCAL_OBJECT_ROOT", str(REPOSITORY_ROOT / ".local" / "objects"))).resolve()
HUMANIZER_BASE_URL = env_value("HUMANIZER_BASE_URL", "http://127.0.0.1:3210")
CELERY_TASK_ALWAYS_EAGER = IS_LOCAL_RUNTIME
CELERY_TASK_EAGER_PROPAGATES = IS_LOCAL_RUNTIME
```

Keep development SQLite as the default. Add `.local/` and generated `output/` to `.gitignore`. In health, call Redis/outbox-staleness checks only for distributed mode; local readiness checks database access and local roots with create/write/fsync/delete probes.

- [ ] **Step 4: Run GREEN**

```powershell
py -3.12 -m pytest tests/unit/test_local_runtime.py -q
py -3.12 src/manage.py check
```

Expected: pass with no Docker services running.

- [ ] **Step 5: Commit**

```powershell
git add .gitignore .env.local.example src/wisdome_writer/settings/__init__.py src/wisdome_writer/api/health.py tests/unit/test_local_runtime.py
git commit -m "feat: add dockerless local runtime settings"
```

---

### Task 3: Local Content Contracts, KST Window, and Existing ApplyHome Marker Fix

**Files:**
- Create: `src/apps/local_content/__init__.py`
- Create: `src/apps/local_content/apps.py`
- Create: `src/apps/local_content/contracts.py`
- Create: `src/apps/local_content/dates.py`
- Modify: `src/wisdome_writer/settings/__init__.py` app registration
- Modify: `src/adapters/sources/housing/applyhome.py:103-109`
- Test: `tests/unit/test_local_content_dates.py`

**Interfaces:**
- Produces: `CollectionWindow`, `HousingNotice`, `SourceRunReport`, `HousingCollectionResult`
- Produces: `seven_day_window(now: datetime) -> CollectionWindow`
- Produces: `published_in_window(published_at: datetime, window: CollectionWindow) -> bool`

- [ ] **Step 1: Write failing contract/date/marker tests**

```python
from datetime import datetime
from zoneinfo import ZoneInfo

from adapters.sources.housing.applyhome import ApplyHomeAdapter
from apps.local_content.dates import published_in_window, seven_day_window


SEOUL = ZoneInfo("Asia/Seoul")


def test_seven_day_window_uses_inclusive_kst_calendar_dates():
    window = seven_day_window(datetime(2026, 8, 28, 15, 30, tzinfo=SEOUL))
    assert window.start.isoformat() == "2026-08-22T00:00:00+09:00"
    assert window.end.isoformat() == "2026-08-28T15:30:00+09:00"


def test_modification_date_cannot_admit_old_publication():
    window = seven_day_window(datetime(2026, 8, 28, 15, 30, tzinfo=SEOUL))
    old_publication = datetime(2026, 8, 21, 23, 59, tzinfo=SEOUL)
    assert published_in_window(old_publication, window) is False


def test_applyhome_urban_officetel_endpoint_maps_without_row_fallback():
    category = ApplyHomeAdapter._category(
        "https://api.odcloud.kr/api/ApplyhomeInfoDetailSvc/v1/getUrbtyOfctlLttotPblancDetail",
        {},
    )
    assert category == "urban_officetel"
```

- [ ] **Step 2: Run RED**

```powershell
py -3.12 -m pytest tests/unit/test_local_content_dates.py -q
```

Expected: missing local-content modules and the marker test raises `SourceSchemaError`.

- [ ] **Step 3: Implement immutable contracts and date logic**

```python
@dataclass(frozen=True)
class CollectionWindow:
    start: datetime
    end: datetime

    def contains_publication(self, value: datetime) -> bool:
        observed = value.astimezone(SEOUL)
        return self.start <= observed <= self.end


def published_in_window(published_at: datetime, window: CollectionWindow) -> bool:
    return window.contains_publication(published_at)


@dataclass(frozen=True)
class HousingNotice:
    source_key: str
    external_id: str
    canonical_url: str
    title: str
    publisher: str
    category: str
    region: str | None
    status: str
    published_at: datetime
    application_start: date | None = None
    application_end: date | None = None
    deadline: date | None = None
    announcement_date: date | None = None
    supply_count: int | None = None
    price_summary: str | None = None
    eligibility_summary: tuple[str, ...] = ()
    restriction_summary: tuple[str, ...] = ()
    facts: tuple[tuple[str, str], ...] = ()
    source_checksum: str = ""
    parser_version: str = ""
    warnings: tuple[str, ...] = ()
```

Use `value.astimezone(SEOUL).date()` for exact day admission. Correct the category marker to the lowercase substring actually present in `getUrbtyOfctlLttotPblancDetail`.

- [ ] **Step 4: Run GREEN**

```powershell
py -3.12 -m pytest tests/unit/test_local_content_dates.py -q
```

- [ ] **Step 5: Commit**

```powershell
git add src/apps/local_content src/wisdome_writer/settings/__init__.py src/adapters/sources/housing/applyhome.py tests/unit/test_local_content_dates.py
git commit -m "feat: define local housing notice contracts"
```

---

### Task 4: Bounded Official HTML Fetcher

**Files:**
- Create: `src/apps/local_content/http.py`
- Test: `tests/unit/test_local_content_http.py`

**Interfaces:**
- Produces: `HtmlResponse(url, status_code, content_type, body, fetched_at)`
- Produces: `OfficialHtmlFetcher.get(url, *, params=None)`, `.post(url, *, data)`
- Consumes: existing `wisdome_writer.infrastructure.http_safety.safe_get` principles

- [ ] **Step 1: Write failing boundary tests**

```python
import pytest

from apps.local_content.http import OfficialHtmlFetcher, OfficialSourceError


def test_fetcher_rejects_non_https_and_private_hosts():
    fetcher = OfficialHtmlFetcher(allowed_hosts={"www.applyhome.co.kr"})
    with pytest.raises(OfficialSourceError, match="https"):
        fetcher.get("http://www.applyhome.co.kr/list")
    with pytest.raises(OfficialSourceError, match="host"):
        fetcher.get("https://127.0.0.1/list")


def test_fetcher_rejects_oversized_or_non_html_response(fake_transport):
    fetcher = OfficialHtmlFetcher(allowed_hosts={"example.go.kr"}, transport=fake_transport)
    fake_transport.respond(content_type="application/octet-stream", body=b"x")
    with pytest.raises(OfficialSourceError, match="content type"):
        fetcher.get("https://example.go.kr/list")
```

- [ ] **Step 2: Run RED**

```powershell
py -3.12 -m pytest tests/unit/test_local_content_http.py -q
```

- [ ] **Step 3: Implement the bounded client**

Use a dependency-injected `httpx.Client`/transport. Validate IDNA hostname against a literal allowlist, HTTPS scheme, no credentials, allowed path prefixes, redirect targets, status 200, HTML MIME, maximum 5 MiB, UTF-8/declared encoding, three attempts, `Retry-After`, and a 30-second total deadline. Redact query values in all exceptions.

```python
class OfficialHtmlFetcher:
    def __init__(self, *, allowed_hosts: set[str], path_prefixes: tuple[str, ...] = ("/",), transport=None): ...
    def get(self, url: str, *, params: Mapping[str, str] | None = None) -> HtmlResponse: ...
    def post(self, url: str, *, data: Mapping[str, str]) -> HtmlResponse: ...
```

- [ ] **Step 4: Run GREEN and SSRF regression tests**

```powershell
py -3.12 -m pytest tests/unit/test_local_content_http.py tests/unit/test_extraction_safety_boundaries.py -q
```

- [ ] **Step 5: Commit**

```powershell
git add src/apps/local_content/http.py tests/unit/test_local_content_http.py
git commit -m "feat: add bounded official html fetcher"
```

---

### Task 5: ApplyHome Public HTML Collector

**Files:**
- Create: `src/apps/local_content/sources/__init__.py`
- Create: `src/apps/local_content/sources/applyhome.py`
- Create: `tests/fixtures/applyhome/apt-list.html`
- Create: `tests/fixtures/applyhome/apt-detail.html`
- Create: `tests/fixtures/applyhome/remaining-list.html`
- Test: `tests/unit/test_applyhome_public_html.py`

**Interfaces:**
- Produces: `ApplyHomePublicCollector.collect(window: CollectionWindow) -> SourceRunReport`
- Consumes: `OfficialHtmlFetcher`, `HousingNotice`

- [ ] **Step 1: Save minimal sanitized fixtures and write parser tests**

Fixtures contain one in-window APT, one in-window remaining-supply notice, one old notice, and the actual official identifiers/labels with all unrelated navigation removed.

```python
def test_applyhome_collector_keeps_only_publication_dates_in_window(fixture_fetcher, window):
    report = ApplyHomePublicCollector(fixture_fetcher).collect(window)
    assert [row.external_id for row in report.notices] == [
        "applyhome:apt:2026000001:2026000001",
        "applyhome:remaining:2026940001:2026940001",
    ]
    assert all(window.contains_publication(row.published_at) for row in report.notices)


def test_applyhome_detail_extracts_only_explicit_schedule_and_supply(fixture_fetcher, window):
    notice = ApplyHomePublicCollector(fixture_fetcher).collect(window).notices[0]
    assert notice.application_start.isoformat() == "2026-09-07"
    assert notice.application_end.isoformat() == "2026-09-09"
    assert notice.supply_count == 1147
    assert notice.price_summary is None
```

- [ ] **Step 2: Run RED**

```powershell
py -3.12 -m pytest tests/unit/test_applyhome_public_html.py -q
```

- [ ] **Step 3: Implement list/detail parsers**

Use `selectolax` and label-based extraction rather than element positions. Supported official routes:

```python
APT_LIST = "https://www.applyhome.co.kr/ai/aia/selectAPTLttotPblancListView.do"
REMAINING_LIST = "https://www.applyhome.co.kr/ai/aia/selectAPTRemndrLttotPblancListView.do"
```

Follow only HTTPS detail URLs on `www.applyhome.co.kr` with `houseManageNo`, `pblancNo`, and known `houseSecd` keys. Normalize whitespace, strip hidden accessibility duplicates, hash the canonical extracted record, and retain parser warnings.

- [ ] **Step 4: Run GREEN and malformed-page cases**

```powershell
py -3.12 -m pytest tests/unit/test_applyhome_public_html.py -q
```

Add and pass tests for repeated identities, malformed dates, missing detail links, unexpected hosts, and empty result pages.

- [ ] **Step 5: Commit**

```powershell
git add src/apps/local_content/sources tests/fixtures/applyhome tests/unit/test_applyhome_public_html.py
git commit -m "feat: collect applyhome notices from official html"
```

---

### Task 6: LH Public HTML Collector

**Files:**
- Create: `src/apps/local_content/sources/lh.py`
- Create: `tests/fixtures/lh/notice-list-page-1.html`
- Create: `tests/fixtures/lh/notice-list-page-2.html`
- Create: `tests/fixtures/lh/notice-detail.html`
- Test: `tests/unit/test_lh_public_html.py`

**Interfaces:**
- Produces: `LhPublicCollector.collect(window: CollectionWindow) -> SourceRunReport`
- Consumes: `OfficialHtmlFetcher`, `HousingNotice`

- [ ] **Step 1: Write pagination, filtering, and identity tests**

```python
def test_lh_collector_posts_exact_publication_window_and_paginates(fetcher, window):
    report = LhPublicCollector(fetcher).collect(window)
    assert fetcher.posts[0].data["schTy"] == "0"
    assert fetcher.posts[0].data["startDt"] == "2026-08-22"
    assert fetcher.posts[0].data["endDt"] == "2026-08-28"
    assert [call.data["currPage"] for call in fetcher.posts] == ["1", "2"]


def test_lh_identity_and_detail_url_are_derived_from_official_row(fetcher, window):
    notice = LhPublicCollector(fetcher).collect(window).notices[0]
    assert notice.external_id == "lh:02:0000061158:05:05"
    assert notice.canonical_url.endswith(
        "selectWrtancInfo.do?aisTpCd=05&ccrCnntSysDsCd=02&mi=1026&panId=0000061158&uppAisTpCd=05"
    )
```

- [ ] **Step 2: Run RED**

```powershell
py -3.12 -m pytest tests/unit/test_lh_public_html.py -q
```

- [ ] **Step 3: Implement POST pagination and detail parsing**

Post `schTy=0`, KST dates, `currPage`, `listCo=50`, `viewType=srch`, and `mi=1026`. Parse `data-id1..4`, category, title, region, publication/deadline, status, and declared page count. Refuse repeated page signatures, missing pages, conflicting duplicate IDs, and dates outside the requested window.

Detail URLs are built with sorted query parameters. Detail parsing extracts labelled schedule, supply, eligibility, and rent/deposit facts only when present; attachment links remain observations with `internal_analysis_only` rights and are not downloaded.

- [ ] **Step 4: Run GREEN and failure cases**

```powershell
py -3.12 -m pytest tests/unit/test_lh_public_html.py -q
```

Add passing tests for 0 results, repeated pages, an old corrected notice, non-residential rows retained at collector level, and missing optional detail values.

- [ ] **Step 5: Commit**

```powershell
git add src/apps/local_content/sources/lh.py tests/fixtures/lh tests/unit/test_lh_public_html.py
git commit -m "feat: collect lh notices from official html"
```

---

### Task 7: Selection, Deduplication, and Coverage Reporting

**Files:**
- Create: `src/apps/local_content/selection.py`
- Modify: `src/apps/local_content/workflow.py`
- Test: `tests/unit/test_local_content_selection.py`

**Interfaces:**
- Produces: `is_residential(notice)`, `needs_detailed_article(notice, selected_ids=())`
- Produces: `merge_source_reports(reports, window) -> HousingCollectionResult`

- [ ] **Step 1: Write failing policy and conflict tests**

```python
@pytest.mark.parametrize("category", ["분양주택", "공공임대", "국민임대", "영구임대", "행복주택", "매입임대", "remaining"])
def test_residential_categories_are_included(category, notice_factory):
    assert is_residential(notice_factory(category=category))


@pytest.mark.parametrize("category", ["토지", "임대상가(추첨)", "산업시설용지", "주유소용지"])
def test_non_residential_categories_are_excluded(category, notice_factory):
    assert not is_residential(notice_factory(category=category))


def test_conflicting_same_source_identity_is_quarantined(notice_factory):
    first = notice_factory(external_id="n1", source_checksum="a" * 64)
    second = notice_factory(external_id="n1", source_checksum="b" * 64)
    result = merge_source_reports((report(first, second),), window)
    assert result.notices == ()
    assert result.conflicts[0].external_id == "n1"
```

- [ ] **Step 2: Run RED**

```powershell
py -3.12 -m pytest tests/unit/test_local_content_selection.py -q
```

- [ ] **Step 3: Implement explicit policies and deterministic ordering**

Normalize categories through an explicit mapping, never substring-match arbitrary titles except for the approved detailed keywords `무순위`, `잔여세대`, `임의공급`, `취소분`, and `불법행위재공급`. Sort final notices by `published_at`, publisher, and stable ID descending/ascending deterministically. Record source status, item warnings, conflicts, excluded counts, and completeness.

- [ ] **Step 4: Run GREEN**

```powershell
py -3.12 -m pytest tests/unit/test_local_content_selection.py -q
```

- [ ] **Step 5: Commit**

```powershell
git add src/apps/local_content/selection.py src/apps/local_content/workflow.py tests/unit/test_local_content_selection.py
git commit -m "feat: select and reconcile local housing notices"
```

---

### Task 8: Deterministic Markdown, Images, and Atomic Bundles

**Files:**
- Create: `src/apps/local_content/rendering.py`
- Create: `src/apps/local_content/images.py`
- Create: `src/apps/local_content/bundles.py`
- Create: `src/static/local_articles/generic-housing-hero.png`
- Test: `tests/unit/test_local_content_rendering.py`
- Test: `tests/unit/test_local_content_images.py`
- Test: `tests/unit/test_local_content_bundles.py`

**Interfaces:**
- Produces: `RenderedArticle(title, slug, frontmatter, prose_blocks, factual_markdown, sources)`
- Produces: `render_weekly_index(result)`, `render_detailed_article(notice)`
- Produces: `render_summary_card()`, `render_timeline()`
- Produces: `ArticleBundleWriter.write_run()`

- [ ] **Step 1: Write failing renderer tests**

```python
def test_detail_article_uses_approved_information_order(notice_factory):
    rendered = render_detailed_article(notice_factory())
    headings = [line for line in rendered.factual_markdown.splitlines() if line.startswith("## ")]
    assert headings == [
        "## 한눈에 보기",
        "## 공식 공고",
        "## 위치와 공급 규모",
        "## 청약 일정",
        "## 비용과 자금 확인",
        "## 신청 자격과 제한사항",
        "## 신청 전 체크리스트",
        "## 반드시 다시 확인할 내용",
        "## 출처와 이미지 정보",
    ]
    assert "수익 보장" not in rendered.factual_markdown


def test_unknown_values_are_not_invented(notice_factory):
    markdown = render_detailed_article(notice_factory(price_summary=None)).factual_markdown
    assert "공고문에서 직접 확인 필요" in markdown
```

- [ ] **Step 2: Write failing image and bundle tests**

```python
def test_image_manifest_has_required_rights_and_accessibility_fields(image_set):
    for image in image_set.images:
        assert image.sha256
        assert image.alt
        assert image.caption
        assert image.rights_status in {"owned", "generated"}


def test_bundle_write_is_atomic_and_repeatable(tmp_path, article_bundle):
    writer = ArticleBundleWriter(tmp_path)
    first = writer.write(article_bundle)
    second = writer.write(article_bundle)
    assert first == second
    assert (first / "article.draft.md").exists()
    assert not list(tmp_path.rglob("*.tmp-*"))
```

- [ ] **Step 3: Run RED**

```powershell
py -3.12 -m pytest tests/unit/test_local_content_rendering.py tests/unit/test_local_content_images.py tests/unit/test_local_content_bundles.py -q
```

- [ ] **Step 4: Implement renderers and bundle writer**

Use explicit Markdown templates and YAML-safe scalar serialization. Prose blocks have stable IDs such as `intro`, `context`, and `strategy`; factual tables/bullets are separate and never humanized. Pillow renders 1200×630 WebP summary/timeline cards. The generic source image is marked `generated`, includes the disclaimer in its caption, and contains no project branding.

```python
class ArticleBundleWriter:
    def write(self, bundle: ArticleBundle) -> Path:
        target = self.root / bundle.run_date.isoformat() / bundle.slug
        staging = target.with_name(f".{target.name}.tmp-{bundle.bundle_hash[:12]}")
        # write, flush, fsync files, write manifest last, atomic os.replace
        return target
```

Reject paths outside the configured root, symlinks, duplicate manifest paths, checksum mismatch, missing image metadata, and non-UTF-8 text.

- [ ] **Step 5: Generate and inspect the generic hero with the imagegen skill**

Generate a neutral modern Korean apartment-city hero with no logos, signs, text, distinctive real project, or identifiable people. Save it at the exact static path, inspect it visually, and record SHA-256/creator/rights metadata in the image renderer.

- [ ] **Step 6: Run GREEN**

```powershell
py -3.12 -m pytest tests/unit/test_local_content_rendering.py tests/unit/test_local_content_images.py tests/unit/test_local_content_bundles.py -q
```

- [ ] **Step 7: Commit**

```powershell
git add src/apps/local_content/rendering.py src/apps/local_content/images.py src/apps/local_content/bundles.py src/static/local_articles/generic-housing-hero.png tests/unit/test_local_content_rendering.py tests/unit/test_local_content_images.py tests/unit/test_local_content_bundles.py
git commit -m "feat: render immutable local housing article bundles"
```

---

### Task 9: Safe Local Humanizer Adapter

**Files:**
- Create: `src/apps/local_content/humanizer.py`
- Test: `tests/unit/test_local_content_humanizer.py`

**Interfaces:**
- Produces: `HumanizerClient.health()`, `HumanizerClient.transform(document)`
- Produces: `protect_article_prose(blocks, anchors)`, `verify_humanized_candidate()`
- Consumes: `http://127.0.0.1:3210/api/transform` NDJSON

- [ ] **Step 1: Write failing NDJSON protocol tests**

```python
def test_client_accepts_one_complete_done_stream(ndjson_server):
    ndjson_server.events = [
        {"type": "accepted", "jobId": "j1", "position": 0},
        {"type": "result-start"},
        {"type": "result-delta", "text": "자연스러운 "},
        {"type": "result-delta", "text": "문장"},
        {"type": "done", "sourceChars": 5, "outputChars": 8, "chunks": 1, "elapsedMs": 10},
    ]
    assert HumanizerClient(ndjson_server.url).transform("원문") == "자연스러운 문장"


@pytest.mark.parametrize("events", [TRUNCATED, DOUBLE_DONE, DELTA_BEFORE_START, ERROR_EVENT])
def test_client_rejects_invalid_terminal_streams(ndjson_server, events):
    ndjson_server.events = events
    with pytest.raises(HumanizationError):
        HumanizerClient(ndjson_server.url).transform("원문")
```

- [ ] **Step 2: Write failing protected-token tests**

```python
def test_protected_values_must_survive_exactly():
    protected = protect_article_prose(
        {"intro": "양주회천 A-26BL은 2026년 8월 28일 공고됐습니다."},
        anchors=("양주회천 A-26BL", "2026년 8월 28일"),
    )
    tampered = protected.document.replace("[[P0002]]", "")
    with pytest.raises(HumanizationVerificationError, match="protected token"):
        verify_humanized_candidate(protected, tampered)
```

- [ ] **Step 3: Run RED**

```powershell
py -3.12 -m pytest tests/unit/test_local_content_humanizer.py -q
```

- [ ] **Step 4: Implement client and verifier**

The client permits only loopback HTTP/HTTPS URLs, uses streamed `httpx`, 10-minute timeout, maximum 5 MiB result, and never logs bodies. Protection finds URLs, numbers, dates, times, amounts, units, phone numbers, explicit anchors, and Markdown markers; it sorts longest-first before replacement. Block boundaries use `<!-- WSW:block:<id> -->` and must return exactly once in original order.

After restoration, require:

- identical protected-token multiset;
- identical block-ID sequence;
- no new raw number/date/currency token;
- identical Markdown link targets;
- parseable UTF-8 Markdown;
- exact factual Markdown outside prose blocks; and
- humanizer output different from input only within prose blocks.

- [ ] **Step 5: Run GREEN and sibling-engine contract fixture**

```powershell
py -3.12 -m pytest tests/unit/test_local_content_humanizer.py -q
```

Use a captured protocol fixture matching `C:\project\2026\ai-text-makes-likes-human\src\contracts.ts`; do not call Codex in normal tests.

- [ ] **Step 6: Commit**

```powershell
git add src/apps/local_content/humanizer.py tests/unit/test_local_content_humanizer.py
git commit -m "feat: verify local korean humanization"
```

---

### Task 10: End-to-End Workflow and Management Command

**Files:**
- Modify: `src/apps/local_content/workflow.py`
- Create: `src/apps/local_content/management/__init__.py`
- Create: `src/apps/local_content/management/commands/__init__.py`
- Create: `src/apps/local_content/management/commands/collect_recent_housing.py`
- Test: `tests/integration/test_local_housing_workflow.py`

**Interfaces:**
- Produces: `LocalHousingWorkflow.run(now, days=7, humanize=True, write_articles=True, selected_ids=()) -> WorkflowReport`
- Produces management command contract from the approved spec

- [ ] **Step 1: Write failing integration test with official sanitized fixtures and stub humanizer**

```python
def test_workflow_collects_indexes_humanizes_and_writes_articles(tmp_path, fixture_sources, stub_humanizer):
    workflow = LocalHousingWorkflow(
        collectors=fixture_sources,
        humanizer=stub_humanizer,
        output_root=tmp_path,
    )
    report = workflow.run(
        now=datetime(2026, 8, 28, 12, 0, tzinfo=SEOUL),
        days=7,
        humanize=True,
        write_articles=True,
    )
    assert report.window.start.date().isoformat() == "2026-08-22"
    assert report.complete is True
    assert (tmp_path / "2026-08-28" / "index.md").exists()
    assert list((tmp_path / "2026-08-28").glob("*/article.md"))
    assert all(row.humanization_status == "verified" for row in report.articles)
```

- [ ] **Step 2: Run RED**

```powershell
py -3.12 -m pytest tests/integration/test_local_housing_workflow.py -q
```

- [ ] **Step 3: Implement phase orchestration and report**

Phases are `collect`, `merge`, `select`, `render`, `images`, `humanize`, `verify`, `write`. Persist an acceptance JSON after each phase using atomic replacement. Acquire the single-run lock before network work. A source failure marks incomplete; two source failures block final index. Detail or humanization failure keeps draft diagnostics and continues other notices, then returns a non-zero command exit.

```python
class Command(BaseCommand):
    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=7, choices=range(1, 32))
        parser.add_argument("--humanize", action=BooleanOptionalAction, default=True)
        parser.add_argument("--write-articles", action=BooleanOptionalAction, default=True)
        parser.add_argument("--selected-id", action="append", default=[])
        parser.add_argument("--fixture-root")
        parser.add_argument("--dry-run", action="store_true")
```

Live success output prints IDs/counts/paths only; no source or article body.

- [ ] **Step 4: Run GREEN and command smoke**

```powershell
py -3.12 -m pytest tests/integration/test_local_housing_workflow.py -q
py -3.12 src/manage.py collect_recent_housing --days 7 --fixture-root tests/fixtures/local-live --humanize --write-articles
```

- [ ] **Step 5: Commit**

```powershell
git add src/apps/local_content/workflow.py src/apps/local_content/management tests/integration/test_local_housing_workflow.py
git commit -m "feat: orchestrate local housing article workflow"
```

---

### Task 11: Local Preview UI, Toolchain Scripts, and Dockerless CI

**Files:**
- Create: `src/apps/local_content/views.py`
- Create: `src/apps/local_content/urls.py`
- Create: `src/templates/local_articles/index.html`
- Create: `src/templates/local_articles/run.html`
- Create: `src/templates/local_articles/article.html`
- Modify: `src/wisdome_writer/urls.py`
- Create: `scripts/toolchain-lock.json`
- Create: `scripts/setup-local.ps1`
- Create: `scripts/start-local.ps1`
- Create: `.github/workflows/quality.yml`
- Modify: `README.md`
- Test: `tests/unit/test_local_content_preview.py`
- Test: `tests/unit/test_local_scripts.py`

**Interfaces:**
- Produces: loopback-only preview and status routes
- Produces: one-command setup/start contracts
- Produces: Windows and Ubuntu Dockerless CI gates

- [ ] **Step 1: Write failing preview security tests**

```python
@override_settings(IS_LOCAL_RUNTIME=True, LOCAL_ARTICLE_ROOT=fixture_article_root)
def test_local_preview_renders_article_and_security_headers(client):
    response = client.get("/local-articles/2026-08-28/sample/")
    assert response.status_code == 200
    assert "한눈에 보기" in response.content.decode("utf-8")
    assert response["X-Content-Type-Options"] == "nosniff"
    assert "default-src 'self'" in response["Content-Security-Policy"]


def test_preview_rejects_traversal_and_is_absent_outside_local_mode(client, settings):
    settings.IS_LOCAL_RUNTIME = False
    assert client.get("/local-articles/").status_code == 404
```

- [ ] **Step 2: Write failing script/CI material tests**

```python
def test_local_scripts_never_invoke_docker(repository_root):
    material = "\n".join((repository_root / path).read_text("utf-8") for path in (
        "scripts/setup-local.ps1", "scripts/start-local.ps1", ".github/workflows/quality.yml"
    ))
    assert "docker " not in material.lower()
    assert "127.0.0.1:3210" in material
    assert "127.0.0.1:8000" in material
```

- [ ] **Step 3: Run RED**

```powershell
py -3.12 -m pytest tests/unit/test_local_content_preview.py tests/unit/test_local_scripts.py -q
```

- [ ] **Step 4: Implement preview views and safe Markdown rendering**

Resolve paths under `LOCAL_ARTICLE_ROOT` with `Path.resolve()` and `relative_to()`. Parse the limited generated Markdown subset; escape raw HTML; permit only local checksum-addressed image URLs and official HTTPS links. Set loopback/local-mode gates before file access.

- [ ] **Step 5: Implement setup/start scripts**

`toolchain-lock.json` records uv version, Windows x64 URL, and SHA-256. `setup-local.ps1` verifies target paths, downloads to `.tools/`, validates hash, installs Python 3.12, runs `uv sync --frozen --extra dev`, creates `.env.local` only if absent, migrates, seeds registries, and checks the sibling humanizer. `start-local.ps1` starts only missing processes, waits for health, writes PID ownership under `.local/state`, starts Django, and stops only owned children.

- [ ] **Step 6: Implement Dockerless CI**

Windows and Ubuntu jobs use Python 3.12 and locked dependencies, then run:

```text
ruff check .
python src/manage.py check
python src/manage.py makemigrations --check --dry-run
pytest tests/unit tests/integration
```

The humanizer endpoint is stubbed; live source/Codex workflows are manual dispatch jobs.

- [ ] **Step 7: Run GREEN and local server smoke**

```powershell
py -3.12 -m pytest tests/unit/test_local_content_preview.py tests/unit/test_local_scripts.py -q
py -3.12 src/manage.py runserver 127.0.0.1:8000 --noreload
```

From a second process, expect HTTP 200 from `/health/live`, a runtime-appropriate result from `/health/ready`, and HTTP 200 from `/local-articles/` when fixture output exists.

- [ ] **Step 8: Commit**

```powershell
git add src/apps/local_content/views.py src/apps/local_content/urls.py src/templates/local_articles src/wisdome_writer/urls.py scripts/toolchain-lock.json scripts/setup-local.ps1 scripts/start-local.ps1 .github/workflows/quality.yml README.md tests/unit/test_local_content_preview.py tests/unit/test_local_scripts.py
git commit -m "feat: serve and verify dockerless local articles"
```

---

### Task 12: Full Verification and Live 2026-08-22–2026-08-28 Acceptance

**Files:**
- Create during execution: `output/housing/2026-08-28/acceptance-report.json`
- Modify as defects demand: only files already scoped by Tasks 1–11

**Interfaces:**
- Consumes all prior interfaces
- Produces authoritative live article bundles and acceptance evidence

- [ ] **Step 1: Provision the Dockerless toolchain**

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\setup-local.ps1
```

Expected: Python 3.12 environment, migrations, source registry checks, and humanizer preflight succeed without a Docker executable.

- [ ] **Step 2: Run static and Django checks**

```powershell
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe src\manage.py check
.\.venv\Scripts\python.exe src\manage.py makemigrations --check --dry-run
```

Expected: all exit 0 with no warnings treated as success.

- [ ] **Step 3: Run focused, then complete test suites**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\unit\test_queue_configuration.py tests\unit\test_local_runtime.py tests\unit\test_local_content_dates.py tests\unit\test_applyhome_public_html.py tests\unit\test_lh_public_html.py tests\unit\test_local_content_selection.py tests\unit\test_local_content_rendering.py tests\unit\test_local_content_images.py tests\unit\test_local_content_humanizer.py tests\unit\test_local_content_bundles.py tests\unit\test_local_content_preview.py tests\integration\test_local_housing_workflow.py -q
.\.venv\Scripts\python.exe -m pytest -q
```

Expected: all collected tests pass. Any pre-existing failure is investigated, reproduced, and fixed or explicitly separated with evidence; it is not waived.

- [ ] **Step 4: Start the approved humanizer locally**

From `C:\project\2026\ai-text-makes-likes-human`:

```powershell
$env:HOST='127.0.0.1'
$env:PORT='3210'
$env:CODEX_CHUNK_CONCURRENCY='1'
npm.cmd run build
npm.cmd start
```

Expected: `GET http://127.0.0.1:3210/api/health` returns `status: ready`, with Codex login available.

- [ ] **Step 5: Run the live seven-day collection and article workflow**

```powershell
$env:WISDOME_ENVIRONMENT='development'
$env:WISDOME_RUNTIME_MODE='local'
$env:HUMANIZER_BASE_URL='http://127.0.0.1:3210'
.\.venv\Scripts\python.exe src\manage.py collect_recent_housing --days 7 --humanize --write-articles
```

Expected: the report records KST start `2026-08-22T00:00:00+09:00`, end at the actual run time on `2026-08-28`, official ApplyHome and LH source reports, all residential notices in `index.md`, and final detailed `article.md` only for verified articles.

- [ ] **Step 6: Audit generated artifacts programmatically**

Run the acceptance verifier and assert:

- all manifest publication dates lie in the exact window;
- no excluded category is in the weekly index;
- no source reports false completeness;
- every article link resolves inside the run root;
- every `article.md` passed humanization verification;
- every final article has hero, summary, and timeline image records;
- every image checksum/dimension/rights/alt/caption field is valid;
- all official links are HTTPS and allowed; and
- no official attachment bytes were copied.

- [ ] **Step 7: Start Django locally and test with Brave**

```powershell
.\.venv\Scripts\python.exe src\manage.py runserver 127.0.0.1:8000 --noreload
```

Use Brave to open `http://127.0.0.1:8000/local-articles/2026-08-28/`, then at least one detailed article. Confirm layout, Korean text, all three image types, official links, console errors, broken images, traversal rejection, and responsive readability. Save HTTP/status findings, not browser history or unrelated personal data, into the acceptance report.

- [ ] **Step 8: Run final verification and inspect the worktree**

```powershell
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m pytest -q
git diff --check
git status --short
```

Expected: checks pass; only intentional output artifacts, if ignored, remain outside Git status.

- [ ] **Step 9: Commit acceptance-support fixes and documentation**

```powershell
git add -A
git commit -m "test: verify dockerless housing writer end to end"
```

Do not commit `.env.local`, API keys, humanizer job files, `.local/`, or generated live article bundles unless the user explicitly changes the artifact policy.

---

## Plan Self-Review Result

- Every design section maps to at least one task: runtime (2, 11), queues (1), date/source defects (3), official collection (4–6), selection (7), Markdown/images/storage (8), humanization (9), workflow (10), preview/setup/CI (11), and live acceptance (12).
- Interfaces use the same names across tasks: `CollectionWindow`, `HousingNotice`, `SourceRunReport`, `HousingCollectionResult`, `LocalHousingWorkflow`, `HumanizerClient`, and `ArticleBundleWriter`.
- No task claims distributed concurrency, external publishing, OCR, HWP, or S3 equivalence in local mode.
- Live acceptance uses the actual current date and does not substitute fixtures, search snippets, or narrow tests for the requested end-to-end result.
