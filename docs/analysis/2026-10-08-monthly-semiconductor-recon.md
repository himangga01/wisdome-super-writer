# October semiconductor collection reconnaissance

Current checkpoint: the new explicit local metadata collector, immutable archive
reader, CLI and month-aware semiconductor projection are implemented. Latest scoped
verification after all captured-source and inventory repairs: **87 passed in
24.45 seconds**, and scoped Ruff passes. The parent-owned third normal live run
persisted one validated MOTIR record. Fresh bytes now prove Samsung's media-MIME
and SIA's combined label/date incompatibilities; both local parser repairs are now
implemented and pass production-parser replay of the captured bytes. A fresh parent
full suite and normally eligible live collection remain separate acceptance. The parent owns
the authorized live command and overall portal/browser acceptance; this worker did
not execute a live crawl or change the runtime database.

## Baseline and purpose

- Date: 2026-10-08, Asia/Seoul.
- Repository: `C:/2026_project/wisdome-super-writer`; branch `codex/policy-funding`; HEAD `db5edec35aad39ae85e3aca7b6f9e1b9f2e3a0dc`.
- Initial checkout was dirty: prior funding implementation, portal files/tests, settings/templates and analysis/instruction edits were present. The initial delegated reconnaissance changed only this report; the later authorized implementation is recorded below. The parent owns the canonical instruction link and overall integration.
- Read-before references: [AGENTS.md](../../AGENTS.md), shared `C:/Users/강지혜/.agents/analysis-policy.md`, [portal progress](2026-10-08-topic-portal-progress.md), and [semiconductor portal reconnaissance](2026-10-08-portal-semiconductor-recon.md). Current source bytes were checked; previous acceptance is historical evidence.
- User goal: source-backed October 2026 records and year/month portal navigation for the three-topic operator/development service. This report assesses the semiconductor route only. The initial read-only checkpoint executed no collection, approval, seed, migration, product change or external publication; later implementation and test evidence is separately dated below.

## Main finding

At the initial read-only checkpoint there was **no existing semiconductor collection management command and no complete Dockerless monthly semiconductor workflow**. The prior database collector requires approval, Redis, outbox and freshness prerequisites beyond migration/seeding. The subsequently authorized local metadata route is documented below; it does not manufacture approved registry rows, successful source checks, administrator identities, reauthentication proofs or run/attempt provenance.

The existing portal displays non-tombstoned `SourceItem` versions from the Django database, plus current article revisions, source definitions and runs. It does not read a semiconductor filesystem catalog or a monthly local snapshot. It distinguishes unavailable storage from a successful empty query; preserve that distinction.

## Existing database path: exact entry points and prerequisites

| Step | Existing command/function | Actual behavior and constraint |
| --- | --- | --- |
| Runtime bootstrap | `.venv/Scripts/python.exe src/manage.py check`; `makemigrations --check --dry-run`; then normal `migrate` if the parent elects to bootstrap | Use `WISDOME_ENVIRONMENT=development`, `WISDOME_RUNTIME_MODE=local`, `PYTHONUTF8=1`; preserve `.env.local`. No migration was run by this task. Local mode disables external publishing. |
| Catalog import | `.venv/Scripts/python.exe src/manage.py seed_source_registry` | Calls `apps.topics.services.import_registry_manifest`. Imports **draft** immutable source/registry material and active topic policy; does not approve the registry. Default `--root` contains both topic manifests, with no topic flag. For semiconductor only, pass a parent-owned directory containing only its manifest, or call the function for that file using an explicit system `AuditContext`. |
| Source checks | `POST /api/v1/sources/<uuid>/check`; `request_source_check`; `apps.topics.tasks.check_source_snapshot` | Staff/admin audit context; enqueues a check of the selected frozen snapshot. Worker invokes its strict adapter in `source_check` mode over `sourceCheckDays=30`. Empty contract-valid results fail with `source_check_no_records`; prohibited rights fail before adapter access. Checks do not save news inventory. |
| Registry approval | `POST /api/v1/source-registries/<uuid>/decisions`; `decide_source_registry` | Requires consistent head, row version, manifest and decision identity, enabled valid source snapshots, a real policy-bound recent successful check for every enabled member, admin audit provenance and a real `reauthProofId` scoped to `registry_decision`. Repository `accessPolicy.decision=approved` is not database registry approval. |
| Run creation | `POST /api/v1/runs`; `apps.collection.services.create_run` | Requires timezone-aware increasing dates, `window_end <= now + 5 minutes`, consistent approved registry, active hashed topic policy and explicit authority tiers. The October request must end at the actual run time, not future November 1. API enqueues `run.requested`; `create_run` alone does not collect. |
| Collect/persist | `execute_collection_run` / `dispatch_collection_run`; `collect_source_attempt`; `_persist_record` | Creates per-source attempts from enabled registry memberships; invokes `build_source_adapter(snapshot).collect(since=run.window_start, until=run.window_end)`; persists append-only versions and `RunSourceItem` provenance after validation and freshness filtering. `requested_target_ids` are publication targets, not a source selection flag. |
| Completion | `finalize_collection_run_state` | Summarizes source success/failure/exclusion counts, transitions successful collection to `extracting`, and enqueues `run.evidence_requested`. Success is not a collection-only finished article. |

Important infrastructure limits:

- `HttpSourceAdapter` constructs `_RedisSourceLimiter` unconditionally, including local mode. Redis poll reservations, source-wide rate/concurrency leases and durable request counts fail closed when Redis is unavailable (`source_limiter_infrastructure_unavailable`). Local Celery eager settings do not replace this limiter.
- Outbox dispatch is separate from run creation. `dispatch_outbox` scans due schedules and all eligible outbox messages, then uses `current_app.send_task`; it is not a topic-scoped local run loop. Do not invoke a blanket dispatcher to process one monthly collection without accounting for existing schedules/messages and broker availability.
- A run normally queues evidence extraction and source-change handling. No S3/OCR/extraction/publication acceptance is established here. Local mode blocks external publishing, but that does not make every pipeline prerequisite available.
- Catalog `freshnessMinutes=720` freezes `freshness_cutoff=window_end-12 hours`. `_fresh_records` excludes most older October records even if the requested window starts October 1. A separate archival/monthly policy or explicit local archival path is necessary; do not silently change the daily-news policy or mark excluded records eligible.

## Sources, rights and access boundaries

These are current **repository profile facts**, not freshly researched terms, granted permissions or proven live collection coverage. The embedded access review dates are July 29, 2026.

| Key | Catalog state / record rights | Scope and limits |
| --- | --- | --- |
| `motie` | Enabled / `attribution_required` | Two exact MOTIR press/explanation boards; fixed 50-row pages; ministry releases are not filtered by semiconductor keywords in the adapter. Explicit topic relevance selection is needed for the portal. |
| `krx_kind` | Enabled / `internal_analysis_only` | Exact KIND public form/viewer profile; issuer codes restricted to `A005930`, `A000660`; issuer name is not a separate saved field. |
| `samsung_global_newsroom` | Enabled / `internal_analysis_only` | Exact semiconductor category RSS/Atom feeds and detail identity checks. Narrowest configured topic feed for an initial bounded attempt; live success is unknown. |
| `skhynix_newsroom` | **Disabled / `prohibited`** | Exclude from live attempts; profile explicitly binds disabled status. Do not switch rights or substitute a generic RSS adapter. |
| `semiconductor_industry_association` | Enabled / `internal_analysis_only` | Exact latest-news pagination and post identity/date contracts; industry-association content is not independently verified corporate claims. |

`_validate_semiconductor_profile` binds publisher/owner/editorial identity, base URL, authority tier, independence group, rights, terms and enabled status; it also binds entrypoints, record hosts, namespace, page size, KRX issuer codes and purpose MIME unions. `build_source_adapter` verifies the frozen hash and deployed adapter implementation manifest before use. Adapter URL profiles constrain paths and queries beyond host allowlisting.

The existing HTTP transport enforces approved origin/purpose/method, runtime scope, HTTPS and safe outbound URLs, purpose MIME, redirect, response-size, runtime `robots.txt`, retry, request and elapsed bounds. Catalog bounds are 10 pages, 1,000 requests and 900 seconds per source; default rates are 5/minute (MOTIR/KRX/SIA) or 10/minute (Samsung), burst 1, concurrency 1. Bounds exhausted, changed schemas, login/WAF responses, unstable pagination and robots denial are explicit failures. A smaller bounded attempt can legitimately be incomplete; do not call truncation complete coverage.

Document/media rights are separate from record rights. Preserve `mediaDownloadPolicy=metadata_only`; do not download/extract/publish attachments merely to fill the news portal. Internally restricted records remain behind the existing local-loopback/nonlocal-active-staff portal guard. GET rendering must remain read-only. The denied design-reference and localhost browser origins were not accessed by any method.

## Month filtering and smallest implementation option

Implemented at the initial read-only checkpoint:

- Adapters accept `since`/`until`; `in_window` is inclusive at both ends. Original timestamps normalize to UTC; naive/date-only values are interpreted in Asia/Seoul.
- Portal `load_semiconductor_data(query="")` accepts text only; no year/month filter, monthly metrics, period options or backfill command exists. It counts saved versions, limits news to 60, articles to 30 and runs to 5, and computes latest saved collection time independently of search.

For monthly display, use aware Asia/Seoul boundaries `[first day, first day of next month)` and filter original `published_at` **before** counts/slicing; label missing publication dates explicitly rather than replacing them with collection dates. Filter run history by its documented window/date semantics, and article history by a separately named creation/update date. Current-month collection ends at the actual run time; it cannot contain future October records. Display selected month and effective collection cutoff together.

If the approved database/broker/Redis workflow cannot be satisfied in the local session, the smallest credible implementation is a **new explicit local archival ingestion path**, not an approval shortcut. Reuse profile normalization, frozen hashes, strict semiconductor adapters/parsers and outbound/robots/rights checks. Add a real persisted local rate/concurrency/poll/request limiter under an explicit local-only runtime boundary if Redis is absent; never monkeypatch a no-op limiter or weaken deployed distributed controls. Persist per-source attempts/outcomes and record provenance to a separate validated local snapshot, then teach the portal to read it, or implement a separately labelled local database ingestion service. This is a proposed feature, not existing behavior.

`SourceItem` can technically hold append-only versions without a `RunSourceItem`, but raw ORM insertion is not a supported collection workflow and does not prove approved collection. A new local DB ingestion service must use an explicit schema/provenance label and actual source identity, publication/collection timestamps, rights, original URL, version hash, outcome/bounds and live/fixture origin. Do not call `_persist_record` with invented runs: it stamps the frozen topic policy and `freshnessDecision="eligible"`. Keep collection-only archival records and completion counts separate from editorial/extraction approval.

Priorities: preserve the approval boundary first; establish an honest local ingestion/limiter and per-source outcome contract next; implement month projection before limits; then execute bounded attempts and source-aware acceptance. Open product choice: archive direct public source records through the new local route, or require full existing operator registry approval and infrastructure. The parent owns that choice under the user's implementation authorization.

## Verification performed and limits

Read-only targeted file/`rg` inspection established the functions above. Baseline commands `git status --short`, `git branch --show-current`, `git rev-parse HEAD` succeeded. A Python 3.12 subprocess loaded development/local settings and ran pure `normalize_registry_import` against the actual catalog: exit 0, five profiles validated, four enabled, one disabled/prohibited, freshness 720 minutes. Sanitized runtime output confirmed local=true, external publishing=false, Celery eager=true, SQLite backend.

The selected runtime SQLite file was opened with URI `mode=ro`; it contained none of `django_migrations`, `topics_sourcedefinition`, `topics_sourceregistrysnapshot`, `topics_topicpolicy`, `collection_sourceitem`, or `collection_collectionrun` at inspection time. This confirms unavailable storage for these projections, not zero saved news. Parent-reported 71 unapplied migrations were not re-run by this task. Concurrent parent bootstrap may change this state after inspection.

No `.env` values, credentials, Redis connection, port probe, browser, source network request, full/scoped test suite, migration, seed, approval or collection was executed. Live source reachability, current terms/robots, October coverage, broker/Redis availability and source schema acceptance remain unverified. Initial searches naming nonexistent source/settings directories were corrected to the actual paths; those failed searches are not product defects.

Update log: 2026-10-08 — read-only monthly collection and portal reconnaissance completed; approval, Redis, freshness, topic relevance and unavailable storage limits recorded for the parent implementation.

## Authorized implementation checkpoint — 2026-10-08

The parent subsequently delegated implementation of the local-only archival path.
Files owned by this worker:

- `src/wisdome_writer/portal/news_archive.py`: local request budget, real persistent
  source limiter, metadata collector, closed-schema reader and read-only projection.
- `src/apps/local_content/management/commands/collect_semiconductor_news.py`: explicit
  local-only CLI.
- `src/wisdome_writer/portal/semiconductor.py`: date filtering and validated local
  archive fallback; its existing security/topic/revision safeguards remain intact.
- `tests/unit/test_news_archive.py`, `tests/unit/test_portal_semiconductor.py` and
  this report. The parent owns shared periods/settings/views/templates/global reports.

Exact interfaces:

```python
collect_news_archive(root, year, month, max_items=60, max_seconds=180,
                     *, now=None, fetcher_factory=None) -> Path
read_news_archive(root, name) -> dict
load_news_archive(root, query="", period=None) -> dict | None
load_semiconductor_data(query="", period=None) -> dict
```

The live command, for the parent to execute once with the actual settings:

```powershell
$env:WISDOME_ENVIRONMENT = 'development'
$env:WISDOME_RUNTIME_MODE = 'local'
$env:PYTHONUTF8 = '1'
.venv/Scripts/python.exe src/manage.py collect_semiconductor_news --year 2026 --month 10 --max-items 60 --seconds 180
```

The root is `SEMICONDUCTOR_ARCHIVE_ROOT`, default `output/semiconductor`. All writes
use the existing `ArticleBundleWriter` with an owned generation lease, no article
bundles, schema-v2 manifest and hash-bound `notices.json`/`raw-observations.json`.
Their inner schemas are closed: only title, original URL, publisher, nullable
publication time, collection time, source key, rights and provenance are stored
for a record. No article body, source attachment, credential, approval or fake
database run is persisted. Source catalog identity, captured/requested KST window,
limits and actual source outcomes are bound separately. History remains immutable.
The reader checks hashes, catalog/profile identity, duplicate identities, per-source
counts, fixture/live identity, disabled sources, date bounds and global request bounds.
It does not create missing roots on GET. Symlink/reparse roots fail closed.

Collection scope is deliberately bounded metadata discovery: first page of both
MOTIR boards, both Samsung semiconductor feeds and SIA latest. MOTIR titles require
an explicit semiconductor/memory/foundry/chip relevance match; shared provider
sequences across boards are deduplicated. KRX is honestly `unsupported` in this
local metadata route. SK hynix remains `disabled` with zero attempts. Every run has
`coverage_limited=true`; a completed first-page source outcome does not establish a
whole month, all pages, current article status or editorial/publication eligibility.
The source page parser can fail safely if the live site has changed.

Transport reuses DNS-pinned `safe_get`, strict source URL/query profiles and pure
parsers; it does not instantiate or monkeypatch the Redis-backed distributed
adapters. Configured access/rights profiles are normalized by the existing service.
Runtime robots are fetched before content, then enforced on every redirect target
before the next request. Requests remain HTTPS, approved-origin/purpose/method and
purpose-MIME bound. The global operation is capped at **30 requests**, counting
robots/redirects, and the requested elapsed budget; content responses are capped at
2 MiB and robots at 128 KiB. HTTP failures are recorded without source error text.

An exclusive collection lock prevents competing writers. Per-source exclusive
leases and atomic timestamp/poll ledgers under `LOCAL_STATE_ROOT/semiconductor-http`
enforce concurrency 1, minimum **12-second** request spacing at 5/minute (MOTIR/SIA)
or 6 seconds at 10/minute (Samsung), and the existing 30/60-minute polling interval.
These limits persist across fetcher/process recreation. A failed operation still
reserves its configured polling interval; do not repeatedly rerun or delete its
ledger to avoid this restriction. A crash leaves a fail-closed lock; any manual
recovery requires checking ownership rather than removing arbitrary listeners or
locks. Distributed collection and its approval/freshness policy are unchanged.

A supplied test fetcher factory always forces `mode=fixture`; the live CLI exposes
no fixture injection or fake approval switch. The current-month publication window
ends at the captured real start time and excludes the next month's midnight.
Final capture time is recorded separately after collection completes. Original
publication dates remain unknown when absent; collected-date display never turns
them into inferred original dates.

The portal filters database publication/collection dates before counts and the
60-row limit. Articles are filtered by their actual `updated_at` month, and run
history by overlap of saved collection windows. When local storage is valid and
database projection is unavailable or has zero matching versions, the portal can
return local metadata with `inventory_kind="local_metadata"`,
`inventory_label="로컬 원문 메타데이터"`, `mode`, `source_outcomes`, `snapshot_name`,
`archive_snapshot_count`, `coverage_limited` and `window`/`windows`. Otherwise the
existing inventory is labelled `database_versions`. Empty available database
results still mean zero unless a validated archive is present. Nonlocal mode never
uses this filesystem fallback. The parent templates must preserve the distinction.

Month selection chooses the latest validated snapshot for that archival month.
Year/all-period selection keeps the latest snapshot of each selected month and
deduplicates provider identities; this prevents a later October run from discarding
September inventory. Filtering/search applies before the 60-row display limit.
Fixture-only projections say they are test data; a mixed fixture/live projection
keeps row provenance and explicitly labels the mixture.

Verification evidence:

- Initial archive RED: **14 failed** because the module/CLI were absent.
- First archive GREEN: **14 passed in 1.28 seconds**, after correcting the initial
  reader run-name assumption and a fixture encoding/marker issue.
- Portal extension RED: **3 failed, 16 passed** because the loader lacked `period`;
  the combined archive/projection GREEN then reached 33 passed.
- Adversarial RED reproduced missing future-capture validation; later RED cases
  reproduced malformed mode/source values, excessive summed request counts and
  next-month midnight inclusion. Each was repaired and rechecked.
- Year-only selection/local transport RED: 2 failed, 27 passed; both were repaired.
- Final command: `.venv/Scripts/python.exe -m pytest -q tests/unit/test_news_archive.py tests/unit/test_portal_semiconductor.py`
  — **48 passed in 22.96 seconds**, exit 0, development/local Python 3.12 with UTF-8.
- Scoped Ruff for the two implementation modules, CLI and two test files —
  **All checks passed** after formatting/import/line-length cleanup.

The tests exercise actual immutable writer/read contracts, recomputed-but-invalid
hash-bound documents, real HTTP safety with isolated DNS/mock transport, robots
denial before requests and redirects, persistent poll/rate/concurrency limits,
publication/collection month boundaries, year history, DB/topic isolation and
nonlocal denial. Fixture work uses disposable roots; no live source was fetched.
The initial empty runtime database observation remains historical; this worker
did not reclassify it after possible concurrent parent work. No browser, port,
full-suite, runtime migration, database seed/approval, Git mutation, OCR, external
publisher or global machine policy change was performed. Full integration/live
source and Chrome acceptance remain parent-owned and unclaimed.

Update log: 2026-10-08 — implemented the authorized local metadata route and period
projection; scoped tests/lint pass. Parent notified of the exact bounded live
command, polling reservations and UI provenance fields before running it.

## Live failure diagnosis and projection repairs — 2026-10-08

Read the replaced AGENTS instructions, shared analysis policy, monthly progress and
saved source/log evidence before this continuation. Current source bytes and
the preserved analysis baseline were checked; no Git operation was executed by
this continuation. The parent-owned actual live attempt is recorded in
`.local/reviews/monthly-update/semiconductor-collection.log` and the immutable
`output/semiconductor/2026-10-08` archive. It ran from 13:23:19 to 13:23:52 UTC:
all three supported sources recorded `source_schema_invalid`, two HTTP attempts
each and zero metadata records. KRX stayed unsupported; SK hynix stayed disabled.
The CLI exited with a failure after preserving the archive. This is **not a data
update or a successful empty source result**.

The original run retained only the exception code. `SourceSchemaError.detail`
contains the specific assertion, but the collector had not recorded it. That
code covers different MOTIR/Samsung/SIA parsers and URL/profile assertions; two
requests do not establish that parsing, rather than a redirect/profile check,
failed. The saved evidence has no response body, exact detail, MIME or encoding,
so it cannot prove a shared implementation bug or current source-parser drift.
Offline profile/date/parser fixtures pass. No speculative parser rewrite,
access/rights relaxation or fabricated empty result was made.

The existing persisted polling reservations were read without changing them:

| Source | Next eligible UTC time |
| --- | --- |
| MOTIR | 2026-10-08 13:53:19.695652 |
| Samsung semiconductor feeds | 2026-10-08 13:53:32.269581 |
| SIA | 2026-10-08 14:23:38.852464 |

The parent owns the next normal collection command after eligibility. This worker
did not refetch, delete/reset ledgers, change clocks or use another tool to avoid
the interval. It did not access denied browser origins, migrate/approve database
material or run a full suite.

Added bounded diagnostics for the next eligible default live operation:

- `CollectionDiagnostics` writes under the ignored
  `LOCAL_STATE_ROOT/semiconductor-diagnostics/<operation UUID>/` directory.
- `diagnostics.json` retains source key, purpose, redacted request/final URLs,
  configured entrypoints, HTTP status, normalized MIME, encoding, byte size, SHA-256
  and an optional public-response body filename. No request/response header dump,
  cookies or URL query values are retained in that index.
- Public response bytes are private scratch evidence capped at 8 MiB per
  operation; the existing per-response HTTP limits and 30-request cap still apply.
- Failure entries preserve bounded/redacted `SourceAccessError.detail`, exception
  class and the actual stage (`transport_init`, `fetch_feed/list` or
  `parse_feed/list`). Generic exception text is not copied. A completed diagnostic
  index links to the saved archive generation by `archive_run_name`.
- `collect_news_archive` additionally accepts optional `diagnostics_root` for
  isolated tests. A custom fetcher still forces fixture provenance. The CLI needs
  no new flag, and portal/artifact schemas, source profiles and limiters are unchanged.

The parent also confirmed four P2 projection issues. RED cases reproduced
all-failed archives appearing available/zero, a failed newer attempt erasing prior
records, a September-publication archive captured in October disappearing from
collection-date selection, and a healthy dataset yielding a generic missing-schema
state for an empty selected month. These now behave as follows:

- All supported sources failed and no eligible earlier records exist:
  `available=false`, `total=None`, failure wording and actual source outcomes.
- Validated **positive observations merge across earlier runs**. Missing from a
  later first page, an empty result, item cap or failed refresh is not deletion.
  Preserve older identities and choose the latest eligible saved observation.
- Filter each record's selected original/capture date **before identity
  deduplication**, without pre-filtering by the archive's requested publication
  month. This preserves an eligible earlier capture of the same identity.
- A healthy stored dataset with no selected-period records returns an explicit
  available empty selection; it does not fall through to a missing database schema.
- Any live archive takes precedence over fixture storage, including a failed real
  attempt; fixture news cannot make that real failure look successful. Fixture-only
  storage remains explicitly labelled and each row keeps its original provenance.
- `last_seen` is the latest actual collection timestamp among retained selected
  records; `last_attempt_at` is the latest refresh attempt's capture timestamp.
  `source_outcomes` remain the latest actual attempt. `outcome_summary` is a display
  string: completed/partial/failed/unsupported/disabled source counts in Korean.
  Failure/partial messages explicitly say earlier validated records were preserved.

Additional checks cover item-limited refreshes, historical collection dates before
deduplication, failed live attempts beside healthy fixture storage, and a real
database-schema failure beside an available empty archive selection. Original
publication dates and all dataset/source boundaries remain intact. The prior
“latest snapshot per month” behavior described above is historical and superseded
by this positive-observation merge; no collection permission or bound was expanded.

Actual new verification:

- Diagnostic RED: 2 failed because capture/class/argument were absent; diagnostic
  GREEN: 2 passed. A further detail-redaction check excludes URL userinfo/query values.
- Projection RED: **4 failed, 1 passed**, reproducing the four states above.
- After repair: 37 archive tests passed in 4.28 seconds; follow-up cases added.
- Final `.venv/Scripts/python.exe -m pytest -q tests/unit/test_news_archive.py tests/unit/test_portal_semiconductor.py`
  — **60 passed in 19.19 seconds**, exit 0.
- Scoped Ruff for archive, projection, CLI and both test files — **All checks passed**.

These are controlled source/HTTP fixtures and actual writer/reader/ORM projection
checks; they do not establish repaired live parsing, full-suite or browser
acceptance. The next eligible live diagnostic capture must identify the actual
source failure before any parser/profile repair is proposed. Keep the failed
immutable archive and its historical outcome visible.

## Captured payload repair and explicit inventory — 2026-10-08

The parent-owned second eligible run at 13:57 UTC saved failure generation
`2026-10-08--run-d259c352e677`. Its diagnostic index is
`.local/state/semiconductor-diagnostics/06bab5eaba5849adb3e0ad975339e6bf/diagnostics.json`.
The earlier evidence gap is now partially resolved, without another worker fetch:

- MOTIR: HTTP 200, `text/html`, UTF-8, 137,847 bytes, SHA-256
  `c65a4e5be782eaeb16275fcca79a9f9ea0480dcd3bf9d0f3ed066244bc418636`.
  Exact failure: `MOTIR list row must expose exactly one article.view identity.`
  The actual list contains `article.view('172275'); return false;` inside nested
  title markup, with an independently matching same-board href. The old pure parser
  only accepted the bare call/optional semicolon; this is proven source-format
  incompatibility, not a CAPTCHA, fabricated empty result or common date bug.
- Samsung: exact failure `Samsung feed page query is invalid.` at `fetch_feed`.
  The initial `paged=1` request passed validation; after two counted requests
  (robots plus content), a redirect target failed before a third request. Only the
  robots response was returned to capture. The precise live Location/query was
  not retained, so a specific live target is an inference, not established fact.
- SIA: `source_poll_interval_reserved`, zero requests on this second run. This is
  correct enforcement of the initial hour-long reservation. Its original parser
  failure remains undiagnosed until an eligible parent-owned capture.

The parent accepted a narrowly local MOTIR repair. Only `news_archive.py` now
normalizes the exact known return-false wrapper after verifying the href has the
same approved host, board and ID. Its cosmetic `mno` must be empty and `pageIndex`
must be 1; duplicate/extra query fields, different IDs, different boards and other
JavaScript are rejected. The normalized saved URL contains no list query. Nested
title markup is supported. No JavaScript executes and no detail/attachment request
is added. Distributed adapter files and implementation manifests remain unchanged.

Offline replay of the **actual captured MOTIR bytes** now parses the 19-row board
successfully and yields **zero qualifying semiconductor-title matches** within
the requested window. This proves parser compatibility for that captured page;
it does not prove current live collection, other boards/pages or a whole-month news
inventory. No source content was invented to fill the result.

The parent also accepted first-page canonical redirect equivalence for the local
Samsung fetcher: only an initial `paged=1` request may redirect to a query-free URL
on the same already-approved feed host/path, allowing a trailing slash change.
Robots, origin/path, HTTPS, MIME, request/redirect/time and rate limits still apply
before the redirected fetch. Page 2, extra query fields, a different feed path or
origin retain normal strict validation. A controlled MockTransport reproduced the
observed fetch-stage query exception after two requests; this narrow case now
passes, while three negative controls still deny before the third request. This
is the accepted canonical first-page semantics of a configured bare entrypoint;
**the exact live Samsung redirect is still unproven** until the parent's next run.
Private diagnostics now record checked redirect target paths, empty-query state
and allowed/denied decision without retaining query values or bypassing validation.

Independent review also proved that a single matching database version hid a
healthy three-record local archive. The accepted interface is now:

```python
load_semiconductor_data(query="", period=None, inventory="auto")
# inventory is one of "auto", "local", "database"
```

- Local `auto` prefers a validated available local archive, including a completed
  empty selected-period result, while retaining separately projected DB drafts and
  runs. News row/count units remain local metadata; DB versions are not added.
- Explicit `database` reads only the DB inventory. Explicit `local` reports the
  local inventory or its actual unavailable state.
- An unavailable local refresh may fall back to a healthy matching database
  inventory in `auto`, with failure wording, source outcomes and latest attempt
  metadata. Explicit `local` keeps the failure visible. Nonlocal explicit `local`
  returns unavailable without reading the private archive.
- `unknown_date_count` counts latest validated local identities whose publication
  date remains unknown, regardless of which month cannot be assigned. Active
  publication-date selection explicitly says those records were not assigned to
  the selected month. All/collected-date modes preserve the actual undated rows.
  DB inventory separately counts its undated stored versions and labels those
  units; counts are not mixed between inventories.

Actual verification in this continuation:

- Five initial RED failures reproduced MOTIR's click suffix, missing unknown-date
  disclosure, local/DB precedence, absent failure warning and missing explicit
  inventory argument.
- Samsung controlled RED: one failed, three negative controls passed; subsequent
  canonical first-page case and controls pass.
- Full captured MOTIR replay initially exposed nested title markup omitted by the
  first local normalization. A nested-markup RED fixture reproduced that issue;
  corrected traversal passes and the actual captured page replay exits 0.
- Additional adversarial MOTIR cases reject a mismatched ID, nonempty `mno`,
  pageIndex 2, an unknown query field and an arbitrary JS suffix.
- Final `.venv/Scripts/python.exe -m pytest -q tests/unit/test_news_archive.py tests/unit/test_portal_semiconductor.py`
  — **75 passed in 21.51 seconds**, exit 0. Scoped Ruff passes for archive,
  projection, CLI and both test files.

The parent was notified that production code was stable before its planned 14:28
normal collection. This worker changed no ledger, performed no HTTP/browser/port
operation, altered no distributed approval/profile manifest, and ran no full suite
or Git operation. Captured public bytes were read offline. Fresh live source
acceptance and the exact remaining source failures remain parent-owned.

## Third live run and remaining parser contracts — 2026-10-08

The parent-owned normal live command completed with exit 0 and saved generation
`2026-10-08--run-5c8262014531`, as recorded in
`.local/reviews/monthly-update/semiconductor-collection-reviewed.log`. The window
ended at `2026-10-08T14:30:24.732338+00:00`; source requests completed by 14:31:15 UTC.
Read-only `read_news_archive` verified the actual hash-bound artifact: mode `live`,
one MOTIR record, original publication `2026-10-01T15:00:00+00:00` (October 2 KST),
and `coverage_limited=true`. MOTIR completed with three requests; Samsung failed
with three requests; SIA failed with two. KRX remains unsupported and SK hynix
disabled. This is a genuine **bounded partial metadata update**, not exhaustive
source coverage or proof that the two remaining live parsers are repaired.

Diagnostic authority:
`.local/state/semiconductor-diagnostics/a4eeb7cef82b496cbb33a2f121b9f111/diagnostics.json`.
The captured source payloads establish:

| Source | Actual response evidence | Exact failure |
| --- | --- | --- |
| Samsung | HTTP 200, UTF-8 `text/xml`, 345,113 bytes; SHA-256 `fea6dcca9df6f20cfba897be33377c3a7c336f0852bbc28020774234d4e0252c` | `Feed enclosure lacks URL or MIME.` at `parse_feed` |
| SIA | HTTP 200, UTF-8 `text/html`, 72,187 bytes; SHA-256 `7536e59045efda1688eb55ce33a4882872d0c818989db9764ae73865a7f829df` | `SIA resource title or date is invalid.` at `parse_list` |

Samsung's previously inferred first-page canonical redirect is now **verified in
the real diagnostic index**: same configured feed path, empty target query, allowed
before the next request; the actual RSS response returned successfully. Its
remaining error is a different metadata/attachment contract mismatch. The genuine
RSS contains 50 items and MRSS `<media:content>` elements with a public image `url`
and `medium="image"`, but no `type` MIME attribute. The reused `feed_entries`
function validates that irrelevant media enclosure and aborts before returning
title/link/date rows. The local archive's contract requires no media or body, so
this is proven parser incompatibility, not an access denial, CAPTCHA or unsupported
publisher. Do not guess a MIME type or download an image to satisfy that parser.

An isolated **in-memory offline probe**, without changing a production file, used
only title/link/pubDate/published/updated/id/guid children and omitted content,
media/enclosures and body. The existing pure feed parser then returned 50 metadata
items; their latest original publication was `2026-09-29T08:00:00+00:00`, and **zero
were in the captured October window**. This establishes a prospective narrow
metadata-only parsing path; it does not convert the actual failed run into a
completed Samsung update or claim new October records.

SIA's genuine page has 12 exact resource items. Its metadata text combines a type
label and date, for example `Press Release: 10/05/26` or `Blog: 09/23/26`. The pure
parser sends that entire string to its bare-date parser. An in-memory probe
accepted only the exact known `Press Release|Blog` prefix plus one `MM/DD/YY` date,
removed that prefix in the temporary DOM, and reused the unchanged pure parser.
It returned 12 metadata items, **one in October**, with original normalized date
`2026-10-05T00:00:00+00:00`. This proves the remaining label/date contract mismatch;
it is not a need for alternate access, broader URL profiles or a guessed date.
The full existing local metadata projection was also replayed on that temporary
DOM: it returned the same one October record and passed the existing strict SIA
host/path, title and date guards. The original captured file was not modified.

The parent started the full suite and explicitly froze production/test edits.
Accordingly, **no production repair or test mutation was made in this continuation**.
The two normalization probes and validated artifact read exited 0. The previously
reported 75 scoped tests/Ruff results remain historical code acceptance for the
unchanged production state; the parent's running full suite is not claimed as
passing here. The captured public files were read offline; no source refetch,
ledger reset, clock change, HTTP/browser/port operation, Git operation or registry
approval occurred.

After the parent's verification checkpoint, a further authorized implementation
can add these two tightly scoped **local metadata** normalizations with failing
fixtures copied from the demonstrated shapes, while preserving distributed parser
manifests, source profiles, rights and limits. Fresh normal eligible collection is
still necessary to claim repaired Samsung/SIA live operation. Keep the current
one-record partial archive and all earlier failure generations intact.

## Implemented remaining local parser repairs — 2026-10-08

The parent deliberately stopped full-suite session 34311 and explicitly authorized
both previously diagnosed repairs. Production editing resumed only after that
instruction. This worker changed `news_archive.py`, its owned archive tests and
this report; no shared/distributed source parser or manifest, catalog, setting,
other agent's product files, ledger or actual saved live generation changed.

`_metadata_feed_entries` now parses XML through `defusedxml` and retains only
title/link/pubDate/published/updated/id/guid children for record metadata. Body,
description, MRSS/media nodes and RSS/Atom enclosure links are not supplied to the
unchanged shared `feed_entries` function. This neither guesses a missing media
MIME nor weakens the shared attachment contract. A regression check proves the
shared parser still rejects the same MIME-free MRSS payload. Missing original
links, foreign original hosts and XML entities remain rejected; downstream source
URL/profile/rights and original-date/window checks remain unchanged. No image or
article-body fetch or archive field is added.

`_metadata_sia_rows` only recognizes a flat, exact `Press Release|Blog: MM/DD/YY`
metadata string demonstrated by the captured page. It strips that known label in
the temporary DOM, then reuses the unchanged strict SIA title/link/date parser.
Unknown labels, multiple dates, malformed dates and extra trailing text remain
source failures. The existing date parser normalizes the source's two-digit year;
it is not replaced with the requested archive year. A `10/05/25` regression is
correctly excluded from requested October 2026 and yields a contract-valid empty
selection, rather than an invented 2026 date. The original captured files are intact.

Actual verification:

- Initial RED for the two repairs: **4 failed, 4 negative controls passed**.
  Failures reproduced MIME-free RSS media, Atom enclosure links and the two known
  SIA labels. The shared parsers' original strict failure was also asserted.
- Targeted GREEN after the local helpers: **8 passed, 52 deselected in 1.05 seconds**.
- Additional controls verify missing canonical links, foreign hosts, forbidden XML
  entities and source year preservation.
- Combined `.venv/Scripts/python.exe -m pytest -q tests/unit/test_news_archive.py tests/unit/test_portal_semiconductor.py`
  — **87 passed in 24.45 seconds**, exit 0. Scoped Ruff passes for local archive,
  semiconductor projection, CLI and both test files after formatting cleanup.
- **Unmodified actual captured bytes** were replayed through the production
  `_metadata` function: Samsung returns zero October records; SIA returns one
  October 5 record with `internal_analysis_only` rights. Both probes exit 0.

The parent was notified that production is stable and its final full suite may
restart. No HTTP/browser/port operation, source poll reset, clock manipulation,
registry approval, database migration, Git operation or full suite was executed
by this worker. The existing third live archive still reports its actual Samsung
and SIA failures; successful offline replay does not retroactively change those
observations. Only a new normal eligible live run can establish repaired source
collection, and only the parent owns that acceptance and updated UI inventory.

## Parent-owned final live acceptance — 2026-10-09 KST

The parent waited until the persisted polling reservations elapsed and executed
one normal eligible CLI run. It returned exit0 and validated generation
`2026-10-09`, mode `live`, two October records: MOTIR1/completed/3 requests,
SIA1/completed/2 requests, Samsung0/completed/5 requests. KRX remains unsupported
and SK hynix disabled. Requests ran00:31:10–00:32:13 KST. These new actual results
supersede the failure outcomes for the new generation without rewriting history.

The parent then validated the archive projection and actual in-process news page:
two live records match October by original date, with correct source outcomes.
Final stable full suite passed1,689 tests,3 skipped and203 subtests (540.75 seconds).
These are parent-executed results, not additional worker HTTP/tests or exhaustive
source coverage. Direct Chrome/mobile acceptance remains blocked. Actual evidence
and runtime handoff: [monthly progress](2026-10-08-monthly-data-update.md).
