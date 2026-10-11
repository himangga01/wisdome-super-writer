# Semiconductor portal data reconnaissance

## Baseline and scope

- Analysis date: 2026-10-08, Asia/Seoul.
- Source: branch `codex/policy-funding`, HEAD `db5edec35aad39ae85e3aca7b6f9e1b9f2e3a0dc` plus the existing funding, navigation, settings and documentation working changes. The checkout was dirty before inspection; none of those changes were modified by this reconnaissance.
- Starting references: [AGENTS.md](../../AGENTS.md), the shared `C:/Users/강지혜/.agents/analysis-policy.md`, and [service analysis](../service-analysis.md), particularly its October 8 introduction, runtime architecture and collection sections.
- Purpose: identify existing read-only data and access boundaries for a separate `/semiconductor/` topic portal alongside housing and policy funding. This is implementation guidance, not implemented portal behavior or browser acceptance.
- Ownership: this report is the only file written by the delegated reconnaissance. The main implementation agent owns the canonical-report link in `AGENTS.md` and final UI/runtime verification.

## Existing architecture and usable fields

Semiconductor news uses the Django collection/editorial database. There is no equivalent semiconductor filesystem bundle workflow in the inspected code. Housing uses local immutable bundles; funding has its separate catalog, storage and schemas. Keep their topic inventories separate.

| Need | Exact source | Useful fields and meaning |
| --- | --- | --- |
| Collected original records | `apps.collection.models.SourceItem` (line 517) | `id`, `source_id`, `external_id`, `title`, `publisher`, `canonical_url`, nullable `published_at` and `modified_at`, `first_collected_at`, `status`, `source_version_hash`, `source_version_schema`, nullable `retention_tombstoned_at` |
| Topic filter | `SourceItem.source` → `apps.topics.models.SourceDefinition` | Filter `source__topic_code="semiconductor_news"`; `SourceItem` has no direct topic field |
| Collection provenance | `apps.collection.models.RunSourceItem` (line 586) | `run`, `collection_attempt`, `source_item`, `source_snapshot`, `discovery_kind`, `discovered_at`, `previous_run_source_item`; linked by `SourceItem.run_links` |
| Run summary | `apps.collection.models.CollectionRun` (line 222) | `topic_code`, `display_id`, `state`, `window_start`, `window_end`, `created_at`, `started_at`, `completed_at`, `counters` |
| Article inventory | `apps.editorial.models.DraftArticle` (line 105) | `topic_code`, `article_type`, `state` (default `draft`), `source_run`, nullable `current_revision`, `created_at`, `updated_at` |
| Article display text | `apps.editorial.models.ArticleRevision` (line 162) | `title`, `summary`, `body_markdown`, `revision_no`, `provenance_kind`, `quality_state`, `claim_graph_state`; use only the selected `current_revision` for a current-article list |
| Source directory | `apps.topics.models.SourceDefinition` (line 48) | `display_name`, `publisher`, `base_url`, `authority_tier`, `access_method`, `enabled`, `default_rights_status`, `last_health`, `latest_approved_snapshot_version`; these are source configuration/projection fields, not evidence of a successful collection |
| Approved registry identity | `TopicRegistryHead` (line 625), `SourceRegistrySnapshot` (line 393), `SourceRegistryMembership` (line 692) | `current_approved_registry` points to the head; membership has `source_definition`, `source_snapshot`, `enabled`, `display_order` |

`publisher` is the source publisher, not a separately extracted company issuer. All inspected semiconductor adapters set `publisher=self.source.publisher` (for example `src/adapters/sources/semiconductor/adapters.py:809` for KRX). Label this field “출처/발행기관”; do not imply a KRX filing's company issuer was independently extracted. There is no dedicated `issuer` model field here.

`published_at` is the original publication date and can be absent. Display “발표일 미상” when missing and, separately, “수집일” from `first_collected_at`; do not silently replace one meaning with the other. `status` can be `active`, `corrected`, `retracted`, or `unavailable`. Records are append-only versions, so `SourceItem.count()` is a version count, not a unique-news count, and old versions must not be presented as current without provenance-aware selection.

`record_metadata()` in `src/adapters/sources/semiconductor/common.py:742` records `schemaVersion`, `sourceContext` (including publisher/display name/authority), `originIdentity`, `syndicationKind`, `contentKinds`, and `rights`. Prefer the explicit display fields above over dumping `metadata`, `external_config`, body text or raw evidence. Source content, titles and URLs remain untrusted; escape text and validate outgoing URL schemes/hosts rather than marking them safe HTML.

## Read/query examples

These are source-derived examples, not executed ORM results. A new server-rendered GET/HEAD view can call the ORM directly under the portal access guard; it should not impersonate staff or bypass the existing administrative API middleware.

```python
from django.db.models import F, OuterRef, Subquery
from apps.collection.models import CollectionRun, RunSourceItem, SourceItem
from apps.editorial.models import DraftArticle
from apps.topics.models import SourceDefinition

topic = "semiconductor_news"

# Precisely label this metric "stored source versions", not unique news.
records = SourceItem.objects.filter(source__topic_code=topic)
stored_version_count = records.count()
recent_versions = records.select_related("source").order_by(
    F("published_at").desc(nulls_last=True), "-first_collected_at", "-id"
)[:20]

# For a deduplicated reader list: latest successful observation per source identity.
# This reports last observed status; it does not assert today's live source status.
observations = RunSourceItem.objects.filter(
    run__topic_code=topic,
    collection_attempt__state="succeeded",
)
latest_observation = observations.filter(
    source_item__source_id=OuterRef("source_item__source_id"),
    source_item__external_id=OuterRef("source_item__external_id"),
).order_by("-discovered_at", "-id").values("id")[:1]
latest_records = observations.filter(
    id=Subquery(latest_observation),
    source_item__retention_tombstoned_at__isnull=True,
).select_related("source_item__source", "source_snapshot", "run").order_by(
    F("source_item__published_at").desc(nulls_last=True), "-discovered_at", "-id"
)
# Keep corrected/retracted/unavailable badges visible, or explicitly explain filtering.
observed_identity_count = latest_records.count()
news_rows = list(latest_records[:20])

articles = DraftArticle.objects.filter(topic_code=topic)
article_count = articles.count()  # All article states, not "draft count".
draft_count = articles.filter(state="draft").count()
draft_rows = list(articles.filter(state="draft").select_related(
    "current_revision", "source_run"
).order_by("-updated_at", "-id")[:12])
source_rows = SourceDefinition.objects.filter(topic_code=topic).order_by("display_name")
recent_runs = CollectionRun.objects.filter(topic_code=topic).order_by("-created_at")[:5]
```

Evaluate querysets and counts inside the database-error boundary; Django querysets are lazy. The latest-observation example needs targeted tests before adoption, including unchanged observations, corrections, retractions and tombstones. Do not count articles without a current revision as readable completed drafts.

`apps.topics.services.current_registry(topic, for_update=False)` (line 3561) is a read-only resolver that verifies the head, approved state, manifest and enabled snapshot consistency. It can raise `StateConflict`/`Conflict` for missing or inconsistent registry material. A plain source directory can read `SourceDefinition`; an “approved active sources” claim requires this stronger resolver and enabled registry memberships. `approved_registry_material()` additionally resolves approved adapter release material and is unnecessary for a simple list. Do not call registry import/create/update functions from a GET view.

`CollectionRun.counters["items"]` is set in collection services around line 2584 to the count of successful-attempt `RunSourceItem` rows excluding `discovery_kind="unchanged"`. Other counters include `sources`, `sourceFailures`, `sourceSuccesses`, and `freshnessExcluded`. Thus summing run counters is not an inventory of unique current news.

## Existing read APIs and authentication

| Existing endpoint/function | Reuse limit |
| --- | --- |
| `GET /api/v1/runs?topic=semiconductor_news`, `apps.collection.api.runs` | Supports topic/state/limit; returns run summaries and counters |
| `GET /api/v1/runs/<uuid>`, `apps.collection.api.run_detail` | Returns run source rows with id/title/URL/status/discovery kind, but lacks publication date and publisher in that source list |
| `GET /api/v1/sources?topic=semiconductor_news`, `apps.topics.api.sources` | Directory output contains administration material such as `externalConfig` and `secretRef`; do not copy the complete response into a reader-facing page |
| `GET /api/v1/source-registries`, topics registry views | Administration registry/membership inspection, not a news feed |
| `GET /api/v1/articles`, `apps.editorial.api.articles` | Current implementation takes the first 100 articles across all topics and applies no topic filter; client-side filtering after this limit is insufficient |
| `GET /api/v1/articles/<uuid>`, `apps.editorial.api.article_detail` | Detailed staff inspection; evaluates revision publishability read-only and returns evidence/quality/publication details |

No dedicated semiconductor feed API was found. `/console/` pages use `staff_member_required(login_url="/admin/login/")` and `ensure_csrf_cookie` in `src/wisdome_writer/console.py`. `AdminApiSecurityMiddleware` in `src/wisdome_writer/api/middleware.py:95` requires an authenticated active staff user for `/api/v1/`; only local article status GET/HEAD is exempt. It also validates the CSRF header in the API path, including ordinary reads. Preserve these boundaries.

The existing `apps.policy_funding.views.policy_page` decorator provides the suitable standalone-page pattern: GET/HEAD only; in local runtime, parse `REMOTE_ADDR` with `ipaddress.ip_address(...).is_loopback`; in nonlocal runtime require `is_authenticated`, `is_active`, and `is_staff`. Local requests from non-loopback are denied even when staff. Do not trust `X-Forwarded-For` or the Host header to establish local access. It adds no-store, nosniff, no-referrer and a restrictive CSP. Housing's `_require_local_request` is stricter: local-only and 404 outside loopback. A shared topic-page guard can preserve funding semantics while `/local-articles/` keeps its existing guard.

## Catalog and current database observation

The repository catalog is `config/source-registry/semiconductor_news.json`. Its five directory entries are:

| Source | Catalog enabled | Record rights |
| --- | --- | --- |
| 산업통상부 (MOTIR) | Yes | `attribution_required` |
| KRX KIND | Yes | `internal_analysis_only` |
| Samsung Global Newsroom Semiconductor | Yes | `internal_analysis_only` |
| SK hynix Newsroom | **No** | **`prohibited`** |
| Semiconductor Industry Association | Yes | `internal_analysis_only` |

These are repository settings, not current web research, database import confirmation or active collection coverage. A catalog fallback must say “configured sources”, preserve disabled/rights status, and must not claim five active collectors. Read only an allowlist of display name, URL, enabled and rights fields; do not expose the complete configuration. The existing restricted reader/operator boundary matters for internally restricted records and drafts.

Read-only inspection with Python 3.12.10 and SQLite URI `mode=ro` found that the existing default `.local/state/db.sqlite3` had **no tables at all**. It therefore cannot establish zero semiconductor items, zero drafts, imported source rows, or an approved registry. No `.env.local` values or credentials were read. This is an observation of the default file only: runtime environment overrides may select another database, and another agent can migrate this file later.

Suggested empty states:

1. Database/schema unavailable: show “반도체 수집 데이터를 아직 불러올 수 없습니다” with metrics as unavailable (`—`) and keep topic navigation plus the explicitly configured directory usable. Catch database access/schema errors around the narrow data loader, without exposing raw exception messages. Do not catch all exceptions or mask rendering/programming errors.
2. Available database with zero rows: show a genuine zero and “아직 수집된 반도체 소식이 없습니다”; retain original-source discovery links. No fabricated articles, graphs, trends, timestamps or seeded sample news.
3. Missing/inconsistent approved registry: distinguish “출처 설정 확인 필요” from a network collection failure. GET rendering must not import registries, run migrations, collect news or generate drafts.
4. Missing original publication date/revision: show the explicit missing state and provide only links supported by existing routes. Administrative detail links require staff login.

## Priorities, open decisions and verification limits

- P1: Implement topic-scoped server queries and guard; define exact count labels; provide DB-unavailable versus verified-empty states. Preserve source status, rights and topic boundaries.
- P1: Confirm that the portal remains an operator/development experience. This report does not authorize unrestricted public exposure of draft or internally restricted source content.
- P2: Decide whether the news list shows stored versions or latest successful observations, and whether corrected/retracted rows are shown with badges or separately filtered. The UI must make its choice clear.
- P2: Validate desktop/mobile topic navigation, source links, empty states and no-overflow behavior in Chrome as part of the parent implementation. This reconnaissance did not execute browser tests.
- Read commands: `git status --short`, `git branch --show-current`, `git rev-parse HEAD`; targeted `rg` searches; `Get-Content -Encoding UTF8`; Python 3.12.10 JSON parsing and SQLite read-only schema inspection. Initial broad searches named nonexistent `src/config`/`src/common` paths and were corrected to actual project paths.
- No network collection, web research, Django migration, test suite, API request, source publication or product-code edit was performed. The permission-denied design reference was not accessed by any method. Query examples are inspected-source proposals, not acceptance evidence.
- Update log: 2026-10-08 — initial delegated reconnaissance; existing service-report claims checked against current source; empty default SQLite schema and catalog rights/enablement explicitly recorded.

## Implementation evidence — 2026-10-08

The user subsequently supplied reference screenshots and authorized the redesign.
The delegated implementation added only
`src/wisdome_writer/portal/semiconductor.py` and
`tests/unit/test_portal_semiconductor.py`; this report records the handoff. The
main agent owns the package, route access guards, views, templates, navigation,
global reports and full integration/browser acceptance. The denied reference
origins were not accessed, and no browser, port, network or Git operation was
performed by this implementation task.

Implemented `load_semiconductor_data(query="")` returns `available`, `message`,
`total`, `news`, `drafts`, `sources`, `runs`, and `last_seen`. Source records are
filtered by `source__topic_code="semiconductor_news"` and exclude retention
tombstones. Counts explicitly describe stored versions, including multiple
versions of an identity; this implementation does not adopt the earlier proposed
latest-observation feed. Search applies server-side to original title/publisher
and current article title/summary. `total` counts all matching versions; `news`
is bounded to 60 rows, `drafts` to 30 current-revision article inventory rows
(retaining their actual state), and `runs` to 5. `last_seen` is the maximum eligible
saved collection timestamp for the topic, independent of the search query.

The projection uses selected database fields only and does not return article
bodies, source metadata, external configuration or secret references. News and
directory links must be HTTPS, have the exact configured source hostname, contain
no credentials/backslashes/whitespace/control characters, and use the default
HTTPS port. Unsafe links are `None`. Rights labels reflect the current source's
`default_rights_status`, not a new rights review or historical per-record approval.
Correction, retraction and unavailable status labels remain explicit. Sources use
`display_name`, `publisher`, `url`, `enabled`, and `rights_label` only.

Database errors, including errors during later lazy-query evaluation, discard
partial rows and return `available=False`, `total=None`, `last_seen=None`, empty
news/drafts/runs, and an explicitly labelled repository-catalog source fallback.
No records are imported and no migration/collection is dispatched. A successfully
read empty database returns an actual zero. Non-database programming errors are
not converted into empty results.

An additional adversarial fixture reproduced a cross-topic disclosure when a
semiconductor article's `current_revision` referred to a housing article's
revision. The query now requires `current_revision__article_id=F("id")`; the
mislinked row is excluded. This check protects the portal projection without
altering editorial models or other existing APIs.

Executed evidence (Python 3.12, development/local, `PYTHONUTF8=1`, inherited local
root overrides removed from the test subprocess):

- Initial RED: `.venv/Scripts/python.exe -m pytest -q tests/unit/test_portal_semiconductor.py`
  — 14 expected failures because the new module did not yet exist; fixtures
  completed successfully.
- Initial GREEN: the same command — 14 passed in 16.73 seconds.
- Adversarial RED: the cross-linked-revision and late-database-error cases —
  1 failed (the demonstrated revision disclosure), 1 passed.
- Final GREEN: the complete file — **16 passed in 15.36 seconds**. The tests cover
  mixed topics, duplicate source versions, retention tombstones, original-date
  absence, source restrictions, search, real SQL reads without writes, database
  unavailability, partial-read failure, valid/unsafe URLs and revision ownership.
- `.venv/Scripts/python.exe -m ruff check src/wisdome_writer/portal/semiconductor.py tests/unit/test_portal_semiconductor.py`
  — **All checks passed** after correcting a test-only UTC alias lint finding.

These scoped results do not establish full-suite, route/auth, Chrome desktop/mobile,
live-source, database migration or deployment acceptance. The parent task owns
those checks; the prior empty-default-database observation is historical and was
not asserted as the runtime state after concurrent implementation work.
