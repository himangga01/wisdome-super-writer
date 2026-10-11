# Policy-funding integration reconnaissance

## Baseline, scope, and handoff

- Analysis date: 2026-10-05, Asia/Seoul.
- Source baseline: `main` at `db5edec35aad39ae85e3aca7b6f9e1b9f2e3a0dc`.
- Initial working tree: modified `AGENTS.md`, `docs/service-analysis.md`,
  `docs/code-analysis.md`, `docs/analysis/2026-10-03-two-round-review.md`, and
  `docs/superpowers/plans/2026-10-03-critical-review-remediation.md`; untracked
  `docs/analysis/2026-10-05-documentation-and-code-review.md`. These were preserved.
- Assignment: inspect integration boundaries for the user-approved new official
  policy-funding collection and very easy Korean education feature; write this
  report only. No product edits, commits, delegated work, or external writes.
- Required references read: shared `C:/Users/강지혜/.agents/analysis-policy.md`,
  project [AGENTS.md](../../AGENTS.md), [code report](../code-analysis.md),
  [service report](../service-analysis.md),
  [R1–R16 follow-up](2026-10-05-documentation-and-code-review.md),
  [adjudication](2026-10-03-two-round-review.md), and Task 8 of the
  [remediation plan](../superpowers/plans/2026-10-03-critical-review-remediation.md).
- Source inspection focused on local housing collection/rendering/storage/preview,
  distributed source/registry/editorial boundaries, Django routes/security, console
  navigation, and relevant test fixtures. This is not another exhaustive code review.
- This report is an implementation recommendation, not implementation or acceptance
  evidence. Government endpoints and financial explanations require the parent
  session's official-source verification. No government-site fetch was executed here.

The parent session should register this report in `AGENTS.md` and link it from the
feature design/plan. Its source boundary must be rechecked after implementation.
The delegated write scope intentionally excludes editing those shared documents.

## Recommendation and service purpose

Build a dedicated `apps.policy_funding` Django module that actually fetches approved
official notice lists and details, preserves observations and revisions, and serves
searchable Korean explanations in the existing local service. Use shared safe HTTP,
canonical hashing, and immutable bundle inventory primitives. Keep its normalization,
education, source directory, and display schemas independent of housing.

This is a choice of integration boundary, not a reduction in the user's requested
source scope. The work still needs central sources, all 17 first-level regions,
municipal sources, and guarantee institutions, with an explicit source-by-source
coverage ledger. Directory presence, a working parser, and a successful live
collection are separate facts. Do not close the feature by replacing collection
with an institution link directory or by calling a handful of fixtures nationwide
live coverage.

The service should let a beginner understand what a notice offers, which organization
to contact, what remains unverified, which application steps apply, and what changed
since an earlier observation. It should distinguish notice discovery and educational
explanation from individual eligibility, credit/guarantee decisions, loan execution,
and external article publishing.

The Oct 6, 2026 user sample belongs in an explicitly unverified, user-supplied fixture.
It must not become official evidence, a verified publication timestamp, a current
interest rate, or a live collection success. This agent received the sample's role
and date but not its full content; the parent owns faithful capture and claim checks.

## Current implementation and exact seams

| Existing seam | Source evidence | Integration implication |
| --- | --- | --- |
| Local CLI | `src/apps/local_content/management/commands/collect_recent_housing.py:51` | Demonstrates a Django command, local runtime guard, fixture/live distinction, JSON result summary, and nonzero incomplete outcome. Add a separate policy command. |
| Local collector protocol | `src/apps/local_content/workflow.py:74`; `contracts.py:8` | `collect(CollectionWindow) -> SourceRunReport` is a useful shape, but report and notice types are housing-specific. Define policy types. |
| Housing data | `src/apps/local_content/contracts.py:31` | `HousingNotice` requires housing category/status and has supply/price/announcement fields. Funding must not be smuggled into these fields. |
| Official HTML transport | `src/apps/local_content/http.py:59` | `OfficialHtmlFetcher(allowed_hosts, path_prefixes)` supports GET parameters and POST forms, HTTPS, bounded attempts/bytes/time, redirect checks, and declared encoding. It is usable by new official HTML collectors. |
| Shared outbound transport | `src/wisdome_writer/infrastructure/http_safety.py:123` | `safe_get`, `safe_post_form`, and `validated_logical_url` supply the network safety boundary for JSON/RSS/document variants. Do not use arbitrary requests or trust directory URLs without source-specific constraints. |
| Identity and hash | `src/wisdome_writer/domain/hashing.py:56` | `canonical_hash(..., schema_version=...)` is reusable. Policy-specific semantic hashes should exclude retrieval-time noise. |
| Selection | `src/apps/local_content/selection.py:111`; `workflow.py:849` | Housing merger rejects nonresidential categories and conflicting checksums. Reuse the conflict-quarantine principle, not the housing merger. |
| Article renderer | `src/apps/local_content/rendering.py:132` and `:606` | Housing labels, facts, article type, and fixed official source map prevent direct funding reuse. New deterministic rendering is required. |
| Immutable storage | `src/apps/local_content/bundles.py:172`, `:264`, `:449`, `:733` | `ArticleBundle`, `BundleFile.text`, `ArticleBundleWriter`, `write_run`, and `validate_run` can store policy Markdown/JSON without housing images. See the storage caveats below. |
| Local preview | `src/apps/local_content/views.py:58`, `:393`, `:595` | Read-only, local-runtime plus loopback guard, confined reads, escaping, CSP, and no-store are useful patterns. Housing preview link allowlists cannot display funding links. |
| Django installation and roots | `src/wisdome_writer/settings/__init__.py:50`, `:262` | Add the app and independent policy output setting; do not overwrite `LOCAL_ARTICLE_ROOT` housing output. |
| Root routing | `src/wisdome_writer/urls.py:15`, `:32` | Add policy URLs inside the local-runtime branch. Root currently redirects to the authenticated console. |
| Console access | `src/wisdome_writer/console.py:9` | Existing console pages use `staff_member_required` and `ensure_csrf_cookie`. Collection controls should retain this protection. |
| Navigation | `src/templates/admin_console/base.html:14`; `src/templates/local_articles/index.html` | Add a visible policy-funding entry. Provide a direct local policy URL so use does not depend on discovering a hidden route. |
| API middleware | `src/wisdome_writer/api/middleware.py:95` | `/api/v1/` requires active staff and an `X-CSRFToken` even on ordinary reads; only the exact local-article status route is a public-read exception. Do not assume a new JSON endpoint is anonymous. |

### Transport and parsing limits

The local HTML fetcher is not a complete source access/rights policy engine.
Per-source reviewed access rules, allowed host/path pairs, rate budgets, encoding,
and attribution belong in the new source configuration. `OfficialHtmlFetcher`
permits HTML only; changing its MIME list for funding JSON or attachments would
broaden the housing boundary. Add a narrow policy wrapper around `safe_get` instead.

`HtmlResponse` contains decoded text, fetched time, content type, status, and a
separately retained validated final URL. It does not expose the original response
bytes as a raw evidence blob. A checksum of its UTF-8 re-encoding must be labeled a
decoded-body checksum. If exact downloaded-byte provenance is needed, capture bytes
through the shared lower-level transport and preserve the encoding information.

The ApplyHome and LH collectors demonstrate stable source identities, requested KST
windows, detail-identity verification, bounded pagination, repeated-page detection,
count consistency, explicit-empty handling, and source-level errors. Their selectors
and request forms are source-specific. A generic funding link scraper would lose
these guarantees. HTML from each actual source needs fixture-backed list/detail
parsers; a source rendering its records only through a separate JSON endpoint needs
an adapter for that observed endpoint.

### Storage reuse without changing housing formats

The existing bundle writer accepts arbitrary UTF-8 Markdown, strict JSON, and NDJSON
inside an `ArticleBundle`; its image path/renderer checks are housing-specific.
Policy can initially use text-only bundles under a distinct root such as
`output/policy-funding`. This does not require a database migration or distributed
publishing tables.

Prefer the writer's schema-v2 run path:

```python
writer.write_run(
    run_date,
    index_markdown,
    policy_bundles,
    run_directory=lease,
    run_files={
        "notices.json": serialized_policy_records,
        "raw-observations.json": serialized_source_observations,
    },
)
```

Those two filenames are already allowed and describe funding records accurately.
The writer then enforces the closed run inventory and file hashes. The schema-v1
path omits the closed run-root inventory check and is a weaker default here.
`_validated_run_metadata` checks size and UTF-8 but does not parse the run-level JSON;
the policy module must validate its own closed schema, date types, value bounds,
duplicate keys, and record identity before writing and after reading.

Persist policy record data, deterministic explanations, collection outcomes, coverage
observations, and digest metadata within the declared inventory. The existence of a
valid bundle proves integrity of committed bytes, not financial correctness, live
mode, full coverage, or a successful source fetch. Those require separate fields and
checks. A failed source must not erase its last good observation or make an incomplete
run appear fully current.

Use the existing writer-issued run lease rather than hand-writing a mutable
`latest.json`. A policy reader should only read declared files from validated runs,
bind reads to observed file identity, recheck the run afterward, and fail closed on
tampering. Narrowly expose a public validated JSON reader if necessary; do not copy
thousands of storage lines or casually depend on many private housing functions.
Housing's workflow-wide lock is private and includes housing assumptions; define a
small policy-specific guard or carefully extract only the generic guard interface
with existing housing regressions intact.

For initial bounded collections, reconstruct a search projection from validated
policy snapshots. Bound history scans and expose stale last-good results explicitly.
If volume requires a Django index later, treat it as rebuildable projection of the
immutable source records. Never make an unchecked mutable cache the evidence source.
This recommendation does not claim current scale or benchmark evidence.

## Why a whole distributed third topic is a separate integration project

Adding a third `TopicCode` value alone cannot work end to end. Current source contains
explicit two-topic routing, clustering, UI, schema, and article-type assumptions.

| Distributed change | Exact current boundary |
| --- | --- |
| Models and forward migrations | `src/apps/topics/models.py:24` and every `TopicCode.choices` field across apps. Inventory all referencing fields before generating migrations. |
| Source registry release | `config/source-registry/`; `apps/topics/services.py:1170`, `:2020`, `:3133`; seed command. Source snapshots freeze implementation versions/hashes and require ordinary approval. |
| Adapter factory and release bytes | `src/adapters/sources/base.py:315`; `src/adapters/sources/manifests.py:20`, `:64` |
| Event validation and collection queue | `src/wisdome_writer/infrastructure/event_routes.py:758`, `:1017`; `infrastructure/queues.py`; worker deployment/queue declarations. |
| Editorial clustering and verification | `src/apps/editorial/clustering.py:297` explicitly raises for an unsupported topic; further housing/semiconductor branches govern verification. |
| Article type | `src/apps/editorial/services.py:912` sends every nonhousing topic to `semiconductor_daily_digest`; adding an enum would silently mislabel funding. |
| Editorial release | `config/editorial-policies/`; `apps/editorial/policies.py`; generator contracts and source-grounded funding claims. |
| Admin API contract | `specs/001-automated-content-publishing/contracts/admin-api.openapi.yaml:1717` enumerates only housing and semiconductor. |
| Console choices | `src/templates/admin_console/runs.html:8`, `publishing/target_detail.html:43`, `operations/index.html:40`, and `src/static/admin_console/sources.js:6`. |

The registry adapter manifest also hashes shared collection/evidence/event-route/
HTTP-safety implementation files. Broad changes to those files can invalidate
existing frozen releases, so compatibility cannot be established merely by passing
new policy tests. New immutable material and approval remain required.

R9's 16 remaining PostgreSQL lock expressions are a distributed release blocker.
R7 schedule isolation, R1/R13 credential/canary recovery, R3 Blogger/media sequencing,
unfinished withdrawal/visual integration, and actual remote acceptance remain open.
This recon did not reproduce those diagnostics; their executed evidence is in the
canonical follow-up. A new local module need not execute these paths. A future
distributed funding adapter can consume its normalized contract when those owners
are ready, without falsely advertising external publication now.

## Proposed policy module and record contract

Proposed new locations are recommendations, not files created by this reconnaissance:

| Location | Responsibility |
| --- | --- |
| `src/apps/policy_funding/apps.py`, `contracts.py` | App registration; source, notice, fact, observation, run result types. |
| `src/apps/policy_funding/catalog.py` and versioned `config/policy-funding/` material | Reviewed official source directory, region hierarchy/aliases, source access and parser configuration, education references. |
| `src/apps/policy_funding/sources/` | Actual per-source list/detail collectors over shared safe transport; injectable fetchers. |
| `src/apps/policy_funding/workflow.py` | Local synchronous collection, per-source isolation, normalization, deduplication, change comparison, truthful result flags. |
| `src/apps/policy_funding/storage.py` | Independent root, validated schema-v2 immutable inventory, safe snapshot reads, bounded search projection. |
| `src/apps/policy_funding/education.py`, `rendering.py` | Korean learning content and deterministic notice explanations with evidence links. |
| `src/apps/policy_funding/urls.py`, `views.py` | Read-only local index/detail/guide/source coverage/change views and explicit access boundary. |
| `src/apps/policy_funding/management/commands/collect_policy_funding.py` | Usable fixture/live CLI, source/region selection, bounded collection, JSON summary and meaningful exit status. |
| `src/templates/policy_funding/`, `src/static/policy_funding/` | Mobile-readable Korean pages and plain filters. |
| `tests/fixtures/policy-funding/` | Verified list/detail captures, malformed/error fixtures, user-supplied sample with separate trust state. |
| `tests/unit/test_policy_funding_*.py`, `tests/integration/test_policy_funding_workflow.py` | Parser, semantics, integrity, UI, CLI and source-isolation tests. |

A normalized notice should preserve at least:

- Stable `source_key` and `external_id`; canonical detail URL; title; publisher;
  issuing institution and operating institution when separately stated.
- Administrative level and explicit region/municipality identifiers. Geographic
  program eligibility is distinct from the publisher's office location.
- Program category, financing mechanism, audience, application period, official
  status text, conditional/budget-exhaustion deadlines, and eligibility text.
- Amount/rate/term/repayment/security/guarantee-fee fields as optional evidenced
  facts, retaining original wording, units, qualifying clauses, and unknowns.
- Publication/modification/effective dates separately from retrieval timestamp and
  verified-as-of time. Never turn retrieval time into publication time.
- Field-level supporting excerpt/locator and exact official source reference;
  attachments listed separately with extraction/verification status.
- Parser/config version, canonical content hash, raw or decoded observation hash,
  provenance/trust class, collection mode, and warnings/conflicts.

Do not parse a range, maximum, subsidy, or guarantee percentage into an unconditional
borrowable amount. Keep rate components and changeable reference dates distinct.
The financial semantics must be supported by the verified official material in the
parent research; this section specifies a representation, not lending advice.

Coverage observations should identify expected source, parser availability, last
attempt, last successful listing/detail fetch, pages and records observed, window,
errors, and whether pagination completed. Useful states include `directory_only`,
`implemented_not_live_verified`, `live_ok`, `live_partial`, `temporarily_failed`,
and `manual_confirmation_required`. Render understandable Korean labels.

For change digests, compare the same source identity across successful observations.
Separate newly discovered notices, evidenced changed fields, unchanged records,
and sources that could not be checked. Missing records in one failed/truncated scan
must not become withdrawals. Distinguish source changes from parser/config changes.
Show both the previous and current observation dates with the changed evidence.

## Education and usable UI

Provide a short guided reading order: what kind of help this is, who the notice
addresses, where/how to apply, what evidence is needed, and what still requires an
official check. Offer deeper explanations adjacent to difficult terms rather than
requiring the beginner to read a long glossary first.

The source-grounded education work needs to explain funding versus grant, loan,
guarantee, and interest support; direct versus intermediary lending; working versus
facility funds; collateral/credit/guarantee; interest and guarantee fee; grace period
and repayment; maximum versus approved amount; application, screening, confirmation,
guarantee assessment, lender assessment, and disbursement. Applicable steps vary by
program, so a single universal sequence must not overwrite a notice's procedure.
In particular, clearly explain the user's requested distinction that a confirmation
or recommendation is not itself final financing approval.

Recommended local pages:

- `/policy-funding/`: search text, region, municipality, support/loan type, institution,
  application status, and verification state; count and last update; clear empty and
  failed-source states.
- `/policy-funding/notices/<stable-id>/`: beginner summary, original conditions,
  application route, dates, source proof, missing information, and changes.
- `/policy-funding/guide/`: progressive Korean learning content and glossary linked
  from notice terms.
- `/policy-funding/sources/`: central/17-region/municipal/guarantee coverage matrix
  with actual collector and live-verification status, not just a map of links.
- `/policy-funding/changes/`: bounded, dated digest with per-source check failures.

Use Django templates' escaping for normalized text; validate official links against
the source catalog. A new policy Markdown preview, if used, needs catalog-specific
URL validation. The housing renderer is unsuitable because its host set is fixed.
Generated explanations must not contain raw source HTML or scripts.

Read-only loopback pages can match the current local preview boundary. For a visible
collection button, require local runtime, loopback, active staff, POST and CSRF;
accept only known catalog source keys and bounded options. Do not add network writes
to GET requests or bypass the global API middleware. Keep CLI collection fully usable
even before interactive run progress is implemented. A broad nationwide run should
not be hidden behind a synchronous unbounded web request.

The user sample should be selectable as demonstration/test material with a prominent
unverified status. A fixture run must remain excluded from live freshness and
coverage counts. The fixture date is later than this session date; tests must model
that deliberately rather than treating it as a genuine future official release.

## Verification and acceptance plan

The existing tests supply reusable patterns, not funding coverage:

- `tests/unit/test_local_content_http.py`: prohibited host/scheme/path, redirect
  revalidation, private-IP rejection, bytes/MIME/encoding budgets, attempt/deadline
  limits, and error redaction.
- `tests/unit/test_local_content_bundles.py`: atomic commit, leases, immutable
  generations, hash/identity tampering, undeclared files, traversal, Windows reparse
  and device-name handling, UTF-8 and strict JSON.
- `tests/unit/test_local_content_preview.py`: loopback/local guard, CSP/cache,
  escaping, validated final artifacts, and traversal rejection.
- `tests/integration/test_local_housing_workflow.py`: source isolation, incomplete
  runs, crashes, same-date reruns, fixture provenance, humanizer boundaries, locks.
  Housing humanizer completion requirements should not be copied into deterministic
  policy education.

Before claiming working policy collection, execute meaningful new tests for:

1. Realistic official list/detail captures for each implemented parser, stable IDs,
   pagination completion and repeated pages, explicit empty results, date parsing,
   foreign detail identity, and supported encodings.
2. Cross-source/region isolation, duplicate or conflicting notices, official detail
   URLs, attachments not extracted, deadlines with unknown/conditional dates, and
   financial values whose qualifier cannot be dropped.
3. Fixture/live separation, future sample date, unverified sample claims, immutable
   reruns, changed-field digests, no false withdrawal after failures, and stale
   last-good results retaining their real verification timestamp.
4. UI search/filter combinations, region/municipality hierarchy, unknown fields,
   sources panel, escaping, source-link validation, access controls and no GET
   side effects. Include responsive visual checks and actual keyboard navigation.
5. Real management command execution with fixture data and subsequent rendered
   pages; collecting normalized notices without a readable detail/guide UI is an
   incomplete end-to-end result.
6. Bounded live official-source reads with saved timestamps, identities, counts,
   parser versions and source failures; evaluate every expected coverage row. A
   passing fixture suite cannot substitute for these results.

Use the repository's Python 3.12 environment, development/local mode and
`PYTHONUTF8=1`. Run Django check, migration-drift check, targeted new and impacted
tests, then the required unit/integration suite after implementation. Remove
inherited local root overrides for the suite, preserving the existing `.env.local`.
Use disposable state for migration checks if models are added. Report actual lint
results; historical whole-repository debt does not become a passing check.

This reconnaissance executed only filesystem/source inspections and Git baseline
queries. `git diff --name-only -- src config tests scripts deploy compose.yaml`
returned no product changes at the inspection point. No pytest, Django check,
collector, browser journey, PostgreSQL server, broker, storage service, financial
eligibility decision, or publisher was executed by this agent. Historical tests in
the canonical reports remain historical.

Report checks: all six local Markdown links resolved, code fences paired, and
`git diff --check` returned exit 0 (existing LF/CRLF notices only). A direct whitespace
check of this untracked report found one trailing space, which was removed before
handoff. These document checks do not verify the proposed feature.

## Risks, priorities, and open decisions

| Priority | Risk | Required action |
| --- | --- | --- |
| P0 for truthfulness | Directory entries or a sample are presented as collected, verified nationwide notices. | Preserve independent directory/parser/live-status counts and fixture trust state throughout storage/UI/reports. |
| P0 for financial meaning | Partial headline fields become unconditional eligibility, approval, rates, or borrowing promises. | Preserve qualifiers, field-level evidence, unknowns, source dates, and source-backed procedural education. |
| P1 | Extending a two-topic distributed enum invokes incompatible routing or existing release blockers. | Use the dedicated module; record distributed integration as a separate explicit work item. |
| P1 | A parser silently returns zero after a government site changes. | Detect known page shape and explicit emptiness; surface unsupported markup as source failure. |
| P1 | Funding attachments contain essential eligibility/rate details absent from HTML. | Track unextracted attachments visibly; add verified extraction through approved boundaries, keeping HWP admission closed. |
| P1 | Mutable search cache or failed partial run replaces good evidence and appears current. | Preserve immutable snapshots, last-good status and exact timestamps; compare only qualified observations. |
| P1 | A source registry or preview change weakens safety or invalidates frozen releases. | Reuse established primitives with narrow interfaces, distinct policy schema/root, and impacted regression checks. |
| P2 | Reading and hashing all historical runs makes regional search slow. | Bound/rebuild projection and measure actual data volume before optimization. |

Resolve during implementation without shrinking the request silently: exact municipal
coverage denominator and the initial source inventory; full-text attachment support
required for each source; which official documents support each educational rule;
whether the user needs a UI collection action in addition to CLI; the durable output
retention policy; and how completeness is judged when an official endpoint is
temporarily unavailable. These do not prevent building the independent collector,
evidence contract, fixture pipeline, and usable reader now.

## Resume checklist

1. Read this report and the parent's feature design/research; recheck HEAD and the
   current working tree before editing.
2. Register the canonical report path in `AGENTS.md` and preserve existing analysis
   evidence and task acceptance conditions.
3. Agree the concrete source/coverage ledger and normalized/education contracts in
   the feature plan, retaining user-sample trust boundaries.
4. Implement the dedicated module and existing Django navigation/settings seams;
   establish fixtures and live source adapters, then storage/digest/search/UI/CLI.
5. Execute and record actual verification at each source tier and the complete
   user-visible path. Keep unavailable sources and unexecuted acceptance explicit.
6. Update this report's implemented/verified sections from current code and results;
   do not leave this recommendation phrased as the final implementation state.

| Date | Update |
| --- | --- |
| 2026-10-05 | Initial read-only reconnaissance; exact integration seams, storage reuse conditions, distributed comparison, scope-preserving acceptance, and resume handoff recorded. |
