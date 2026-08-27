# Dockerless Local Housing Writer Design

**Date:** 2026-08-27  
**Status:** Approved design, pending implementation  
**Scope:** Dockerless local runtime, queue/configuration repair, current housing-notice collection, Markdown article bundles, safe Korean humanization, local-server verification, and CI.

## 1. Goal

Wisdome Super Writer must run on a Windows development machine without Docker, PostgreSQL, Redis, or MinIO and must complete this user-visible flow:

1. determine the seven calendar days ending today in `Asia/Seoul`;
2. collect official housing notices whose **publication date** falls in that window;
3. retain every matching residential notice in a weekly index;
4. create detailed articles for sale, remaining-supply, optional-supply, and other application-oriented priority notices;
5. attach rights-safe explanatory images;
6. humanize Korean prose through `C:\project\2026\ai-text-makes-likes-human` without changing facts;
7. store immutable, inspectable Markdown bundles on the local filesystem; and
8. serve and verify those bundles from a local Django server.

The local runtime is the primary developer and demonstration path. Existing container deployment material may remain for production parity, but no local setup, test, collection, writing, or preview command may require Docker.

## 2. Chosen Approach

### 2.1 Decision

Add a first-class Django `local` runtime profile backed by SQLite and local files, plus a focused synchronous housing-content workflow. Reuse existing source safety, hashing, editorial policy, and adapter contracts where they fit. Do not attempt to emulate distributed Celery concurrency inside SQLite.

### 2.2 Rejected alternatives

- Installing PostgreSQL, Redis, and MinIO as Windows services preserves more production behavior but replaces Docker with an equally heavy prerequisite stack.
- Building a separate scraper application is initially simpler but duplicates source, rights, hashing, and validation rules outside the existing project.
- Relying only on Celery eager mode is insufficient because outbox dispatch uses `current_app.send_task()` and because SQLite cannot reproduce the existing lease and `SKIP LOCKED` concurrency contract.

## 3. Runtime Architecture

### 3.1 Runtime modes

`WISDOME_RUNTIME_MODE` accepts:

- `local`: SQLite, local article/object roots, synchronous workflow, loopback preview server, no Redis readiness dependency;
- `distributed`: the existing PostgreSQL, Redis, Celery, and S3/MinIO behavior.

`WISDOME_ENVIRONMENT` retains its existing `development`/`production` meaning. `local` is permitted only with `WISDOME_ENVIRONMENT=development` and `DEBUG=true`. Production must fail closed if `WISDOME_RUNTIME_MODE=local`.

### 3.2 Local process topology

```text
scripts/start-local.ps1
  +-- Python 3.12 virtual environment
  +-- Django migration and seed checks
  +-- humanizer health check / local humanizer process on 127.0.0.1:3210
  +-- Django server on 127.0.0.1:8000

Django local workflow
  +-- official HTML/API collectors
  +-- normalized housing notices
  +-- deterministic factual article renderer
  +-- local image renderer
  +-- HTTP humanizer client
  +-- strict post-humanization verifier
  +-- atomic article-bundle writer
```

Only one local collection/write workflow may run at a time. A process lock records PID, start time, and workflow ID. Stale locks are recoverable only after verifying the owning process no longer exists.

### 3.3 Local settings

Local defaults:

- database: repository-local SQLite file under `.local/state/`;
- SQLite: WAL mode, bounded busy timeout, one workflow writer;
- article root: `output/housing/` unless `LOCAL_ARTICLE_ROOT` overrides it;
- object root: `.local/objects/` unless `LOCAL_OBJECT_ROOT` overrides it;
- humanizer URL: `http://127.0.0.1:3210`;
- preview bind: `127.0.0.1:8000`;
- external publishing: disabled;
- OCR and legacy-HWP workers: unavailable in the core local profile.

The local health endpoint checks SQLite, writable/atomic local storage, the workflow lock, and humanizer health. It does not ping Redis. Liveness remains dependency-free.

## 4. Queue and Configuration Repair

Queue names must have one canonical registry used by Django settings, event routing tests, and local execution.

Required repairs:

1. declare `source.change`, which is currently routed and consumed but omitted from `CELERY_TASK_QUEUES`;
2. route profile/registry auto-publish invalidation away from the unconsumed `publishing` queue to a declared database-only maintenance queue;
3. assert every static route queue is declared;
4. assert dynamic housing and publishing queue outcomes are declared;
5. assert the local runner can resolve every event route it is permitted to execute; and
6. keep external publishing routes disabled in local mode.

The repair must include a regression test that fails against the current repository state.

## 5. Notice Collection

### 5.1 Date contract

For a run on local date `D`, the inclusive publication window is:

```text
start = D - 6 days at 00:00:00 Asia/Seoul
end   = current time on D Asia/Seoul
```

A notice qualifies only when its official **publication date** intersects this window. A modification date never makes an older notice newly eligible. Modified or corrected notices remain linked to their original publication date and are marked as corrections in metadata.

Add separate functions for publication-window admission and correction reconciliation. Do not change the existing correction semantics by silently changing the shared `record_is_in_window()` behavior.

### 5.2 Official source priority

1. **ApplyHome official public HTML**
   - APT list and detail pages;
   - APT remaining/unsold-supply list and detail pages;
   - other public application-oriented lists when a stable official contract is available.
2. **LH Apply official public HTML**
   - list pages filtered by official publication date;
   - deterministic detail URLs derived from the official row identifiers.
3. **Existing public-data APIs**, when `DATA_GO_KR_SERVICE_KEY` is present
   - use the current ApplyHome and LH adapters;
   - compare API identity/date/title with the HTML observation;
   - fail the affected item closed on material conflict.

HTML is a first-class official-source fallback, not a test fixture or search-engine result. Requests retain the existing HTTPS, host, path, timeout, response-size, rate-limit, retry, robots, and content-type boundaries.

### 5.3 Source defects to repair

- Fix the ApplyHome urban/officetel endpoint marker so it matches `getUrbtyOfctlLttotPblancDetail` after lowercasing.
- Add contract fixtures for every configured ApplyHome category and the LH list/detail identity fields.
- Keep the stable identities already used by API adapters and define equivalent identities for HTML observations.

### 5.4 Residential inclusion policy

The weekly index includes:

- sale/public sale housing;
- remaining or optional supply;
- public, national, permanent, integrated-public, and happy housing;
- purchase/lease housing applications; and
- residential corrections to those categories.

It excludes land, factories, religious sites, parking lots, retail units, childcare operators, and non-residential auction notices.

Detailed articles are generated for:

- sale/public-sale housing;
- remaining, unranked, optional, or cancelled-unit supply;
- notices explicitly selected by the local operator; and
- correction notices for an already generated detailed article.

Every matching notice remains visible in `index.md`, even when no detailed article is generated.

### 5.5 Normalized notice contract

Each normalized notice contains:

- source and stable external identity;
- canonical official URL;
- title, publisher, category, region, and status;
- publication date, application period, announcement date, and deadline when available;
- housing/project identifiers;
- supply counts and housing types when available;
- price, deposit, rent, or financing facts only when explicitly present;
- eligibility and restriction facts only when explicitly present;
- attachment observations and their rights status;
- source checksum, collection timestamp, and parser version; and
- warnings for missing optional fields.

Unknown information remains `null` and is rendered as “공고문에서 직접 확인 필요”; it is never inferred.

## 6. Article Generation

### 6.1 Output layout

```text
output/housing/YYYY-MM-DD/
  index.md
  manifest.json
  notices.json
  <stable-slug>/
    article.md
    article.draft.md
    sources.json
    verification.json
    manifest.sha256
    humanize/
      input.md
      output.md
      events.ndjson
      verification.json
    assets/
      hero.webp
      summary-card.webp
      timeline.webp
```

Writes use a temporary sibling directory followed by an atomic rename. Existing completed bundles are not overwritten unless the source checksum changed; revisions receive a deterministic revision suffix and retain the prior bundle.

### 6.2 Weekly index

`index.md` contains:

- exact KST date window and collection time;
- source health and completeness status;
- counts by residential category and publication date;
- a table of every qualifying notice;
- links to generated detailed articles;
- a short “이번 주 먼저 볼 공고” explanation; and
- official-source and liability notices.

### 6.3 Detailed article flow

The article structure adopts the useful information order observed in the approved reference article without copying its prose:

1. title and one-sentence reader-oriented summary;
2. generic, clearly labelled hero image;
3. “한눈에 보기” fact card;
4. official announcement and source links;
5. location and supply scale;
6. application schedule;
7. price, deposit, rent, and funding facts when present;
8. eligibility and restrictions;
9. location/context explanation supported by official evidence;
10. applicant-type checklist and practical strategy;
11. missing/uncertain facts and items requiring official-document confirmation; and
12. sources, image credits, generated-at time, and correction history.

The renderer produces factual blocks deterministically from normalized fields. It never calculates safety margin, expected profit, loan approval, competition rate, or eligibility unless the required official facts and an explicit deterministic formula are present and disclosed.

## 7. Korean Humanization

### 7.1 Integration

Use the existing local service at `C:\project\2026\ai-text-makes-likes-human` through:

```http
POST http://127.0.0.1:3210/api/transform
Content-Type: text/plain; charset=utf-8
Accept: application/x-ndjson
```

The client accepts `accepted`, `queued`, `progress`, `warning`, `result-start`, `result-delta`, `done`, and `error` events. It concatenates result deltas only after `result-start` and accepts the output only after one terminal `done`. Truncated, duplicated-terminal, malformed, timeout, 429, 503, or error streams fail the humanization attempt.

### 7.2 Protected content

The humanizer never receives frontmatter, source lists, image syntax, tables of exact facts, or checksums. Only prose blocks are humanized.

Before transmission, the integration replaces these values with deterministic protected tokens:

- all numbers, dates, times, currencies, percentages, measurements, and phone numbers;
- project, institution, region, and policy names;
- URLs and source/citation markers;
- Markdown links and block boundary markers; and
- legally meaningful eligibility/restriction phrases.

After the response, the verifier requires exact block IDs/order and exact protected-token multisets, restores the values, rejects any new raw numeric token, and reruns Markdown and editorial validation.

### 7.3 Failure behavior

Humanization output is a candidate, never an in-place revision. On any mismatch:

- preserve `article.draft.md` and diagnostics;
- do not create or replace `article.md`;
- show a blocked status in the local preview; and
- allow an explicit retry after the humanizer is healthy.

This design intentionally strengthens the current humanizer, whose existing gates do not make numeric omission or proper-name preservation a sufficient hard guarantee for publication facts.

## 8. Images and Rights

ApplyHome/LH record metadata may be publishable with attribution, but current source policy marks downloaded document/media attachments as internal-analysis-only. Those attachment images must not be copied into article bundles by default.

Every detailed article instead includes:

1. a project-generic apartment hero generated for this repository and visibly labelled “이해를 돕기 위한 이미지 · 실제 단지 모습과 다를 수 있음”;
2. a deterministic summary card rendered from verified facts; and
3. a deterministic application timeline rendered from verified dates.

The generic hero may be AI-generated once and reused as a design asset. It must not contain a real project name, logo, map, or claim to depict the property. Fact cards and timelines are rendered locally with Pillow.

Every image manifest entry records local path, SHA-256, MIME type, dimensions, alt text, caption, creator/source, source URL when applicable, rights status, rights basis, and attribution. A rights or metadata failure blocks final article creation.

## 9. Local Preview Server

Add a development-only preview application:

- `/local-articles/`: date-run index;
- `/local-articles/<run-date>/`: weekly index;
- `/local-articles/<run-date>/<slug>/`: rendered detailed article;
- `/local-articles/assets/...`: immutable local assets; and
- `/api/v1/local-articles/status`: workflow and humanizer status.

The routes exist only in local mode, bind to loopback, reject path traversal, set `nosniff`, restrictive CSP, `no-store` for status, and immutable caching for checksum-addressed assets. They perform no external writes.

The management command is the authoritative mutation interface:

```powershell
python src/manage.py collect_recent_housing --days 7 --humanize --write-articles
```

Dry-run and fixture modes are explicit flags and may not be mistaken for a live successful run.

## 10. Setup and Operation

`scripts/setup-local.ps1`:

1. uses `py -3.12` when available; otherwise downloads an official pinned `uv` Windows release, verifies the SHA-256 recorded in `scripts/toolchain-lock.json`, and uses it to install the latest Python 3.12 patch release;
2. creates `.venv` and installs the locked project plus development dependencies;
3. creates `.env.local` from safe development defaults without embedding source API secrets;
4. runs migrations, source-registry seed/import, and configuration checks; and
5. verifies Node, Codex login, and the sibling humanizer project.

`scripts/start-local.ps1`:

1. refuses production mode;
2. starts or verifies the humanizer bound to `127.0.0.1:3210`;
3. starts Django at `127.0.0.1:8000`;
4. waits for both health endpoints;
5. prints the collection command and preview URL; and
6. terminates only processes it started when stopped.

No setup or start path invokes Docker.

## 11. Error Handling and Recovery

- One source failure produces an explicitly incomplete run; it never silently claims full coverage.
- Both official HTML sources failing blocks final index generation.
- A single detail failure keeps the notice in `notices.json` and `index.md` with a warning but blocks its detailed article.
- Duplicate identities with conflicting material are quarantined.
- Humanizer failure blocks only affected final articles and is retryable.
- Atomic bundle writes ensure an interrupted run leaves either the prior complete revision or an identifiable temporary directory.
- Local workflow logs include IDs, counts, durations, status, and error codes, never article bodies or secrets.

## 12. Verification Strategy

Implementation follows test-driven development.

### 12.1 Unit and contract tests

- canonical queue registry and route/declaration coverage;
- ApplyHome endpoint-category mapping, including urban/officetel;
- KST seven-day inclusive publication filtering and separate correction handling;
- ApplyHome and LH public-HTML list/detail parsing from minimal sanitized fixtures;
- residential inclusion/exclusion policy;
- identity, deduplication, conflict, and correction behavior;
- deterministic Markdown structure and unknown-field wording;
- atomic bundle and checksum manifests;
- image metadata and rights gates;
- humanizer NDJSON parsing, protected-token verification, timeout, and error cases;
- preview path traversal and security headers; and
- local readiness without Redis.

### 12.2 Repository verification

- Ruff;
- Django checks;
- migration drift check;
- the existing unit suite;
- fresh SQLite migration and local workflow integration tests;
- local server smoke test; and
- Git status review.

### 12.3 Live acceptance

On 2026-08-27, or the actual later execution date if implementation crosses midnight:

1. calculate and record the exact KST seven-day window;
2. collect both official public HTML sources;
3. prove every manifest item publication date is in the window;
4. prove excluded non-residential categories are absent;
5. write the weekly index and all selected detailed articles;
6. successfully humanize and strictly verify final prose;
7. verify every final article has the required images and rights manifest;
8. start the local server and load the index and at least one detailed article;
9. verify visible Korean text, images, links, console, and HTTP responses; and
10. retain output bundles and a machine-readable acceptance report.

Search-engine snippets are discovery evidence only and cannot satisfy live acceptance. The saved official source observations and local runtime output are authoritative.

## 13. CI

Add GitHub Actions that do not require Docker:

- Windows + Python 3.12: setup, Ruff, Django checks, SQLite migrations, unit/contract/integration tests, and local preview smoke;
- Ubuntu + Python 3.12: the same deterministic suite for portability;
- Node humanizer contract tests use a stub NDJSON server and do not spend Codex quota;
- live official-source and live humanizer tests remain explicit manual workflows because they depend on external availability and Codex account quota.

CI must fail on queue drift, migration drift, fixture parser drift, humanization protection failure, or uncommitted generated schema material.

## 14. Compatibility and Non-Goals

- Existing distributed models and production settings remain compatible.
- Local mode does not claim PostgreSQL row-lock, Celery lease, broker visibility-timeout, concurrent worker, S3 versioning, OCR, HWP sandbox, or external WordPress/Blogger production equivalence.
- Local mode disables external publication rather than weakening its safety gates.
- This feature does not estimate investment returns, guarantee loans, determine personal eligibility, or republish rights-uncleared official attachments.
- Refactoring unrelated large publishing modules is outside this implementation.

## 15. Completion Criteria

The feature is complete only when all of the following are proven:

- a clean Windows machine can set up and run the requested flow without Docker;
- queue and endpoint/date defects have failing-before/passing-after regression tests;
- CI exists and the deterministic jobs pass;
- a live seven-day official collection completes with a coverage report;
- the weekly index and selected detailed Markdown articles exist on disk;
- final articles contain rights-safe images and validated natural Korean prose;
- the local server serves those exact artifacts successfully; and
- no required step is represented only by documentation or a mock.
