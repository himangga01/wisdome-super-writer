# Documentation analysis and follow-up code review (2026-10-05)

## Baseline and scope

| Item | Value |
| --- | --- |
| Date | 2026-10-05, Asia/Seoul (checks started 2026-10-04 23:57) |
| Source | `main` at `db5edec35aad39ae85e3aca7b6f9e1b9f2e3a0dc` (2026-10-04 14:43 +0900, "fix: address two-round code and service review findings") |
| Initial working tree | Clean at the start of the original analysis; reboot continuation began with five modified documentation/instruction files and this untracked report. Product source remains identical to db5edec. |
| Resumed status | 2026-10-05: remaining verification and synthesis completed; no new helper review was dispatched. R9–R15 propagated, R1/R7 strengthened, conditional candidates adjudicated and R16 added. |
| Request | Analyse the Markdown documents first, review the code, explain the service |
| Method | Main session read the canonical reports, README, spec, plan, task ledger, constitution, handoffs and acceptance record in full; three read-only helper agents cross-checked the large design/contract documents, the operational documents and the six reviewer reports against current source; main read the full product diff `7eb3331..db5edec` and traced affected callers; scratch probes ran outside the repository |
| Companions | [Code analysis](../code-analysis.md), [service analysis](../service-analysis.md), [2026-10-03 adjudication](2026-10-03-two-round-review.md), [continuation plan](../superpowers/plans/2026-10-03-critical-review-remediation.md) |

This is not a third formal review round of the whole code base. It revalidates the
committed baseline, reviews the repair commit that no independent reviewer saw in
its final form, and records what the documents do and do not say.

## Part 1 — Documentation analysis

### Inventory and authority

39 project Markdown files (about 1.25 MB) excluding vendored `.agents/skills`,
`.specify/templates` and `.venv`.

| Group | Files | Last change | Role today |
| --- | --- | --- | --- |
| Agent entry points | `AGENTS.md`, `CLAUDE.md` | 2026-10-04 | Current workflow authority |
| Canonical analysis | `docs/code-analysis.md`, `docs/service-analysis.md`, `docs/analysis/2026-10-03-two-round-review.md`, six `review-round*.md`, 2026-10-03 plan | 2026-10-04 | Current, baseline refreshed by this report |
| Product authority | `.specify/memory/constitution.md` (v1.0.1), `spec.md`, `plan.md`, `research.md` | 2026-07-17 to 2026-08-09 | Requirements and intent; not implementation status |
| Task authority | `specs/.../tasks.md` | 2026-08-11 | 17 of 33 checked; unchanged |
| Design contracts | `data-model.md`, `quickstart.md`, `contracts/*.md`, `admin-api.openapi.yaml` | 2026-08-09 to 2026-08-11 (OpenAPI 2026-10-04) | Partly behind the code, see below |
| Handoffs | root and feature `REMAINING_WORK.md`, `T031_WORK_IN_PROGRESS.md` | 2026-08-21 | Historical; T031 open items still relevant |
| Historical plans/designs | `docs/superpowers/plans/2026-08-*`, `docs/superpowers/specs/*` | 2026-08-07 to 2026-08-28 | Historical; their unchecked boxes are not a status source |
| Acceptance | `docs/acceptance/2026-08-28-live-housing-acceptance.md` | 2026-08-28 | Historical local acceptance only |
| User guide | `README.md` | 2026-10-04 | Mostly current; stale items below |

Reading order that matches authority: `AGENTS.md` → canonical reports → `tasks.md`
→ `spec.md`/constitution → contracts → dated handoffs and plans.

### What the documents say the product is

A single-administrator service that collects Korean housing-subscription notices
and semiconductor news from approved sources, preserves evidence with locators and
rights, drafts Korean articles whose claims cite evidence, and publishes to
WordPress (canonical) then Blogger (secondary) under approval, idempotency, kill
switch, correction and retention controls. Six constitution principles (evidence
first, lawful collection, per-topic adapters, honest editing, administrator control,
testability) rank above every plan. A later Windows-only local path writes housing
articles without containers and never publishes.

### Verified document-to-code discrepancies

All rows were checked against HEAD `db5edec`.

| # | Document statement | Current source |
| --- | --- | --- |
| D1 | Canonical reports and plan say "7eb3331 plus uncommitted repairs", "no commit" | Repairs and reports are committed in `db5edec`; refreshed here |
| D2 | `plan.md`: beat calls `dispatch_due_schedules` every minute | Beat runs `dispatch_outbox` every 5 s, which performs the due-schedule scan (`settings/__init__.py:396-401`, `infrastructure/tasks.py:86-101`) |
| D3 | `job-events.md` lists 11 queues and 57 event keys | 15 queues are registered; 37 keys are routed; 20 documented keys (for example `run.stop_requested`, `article.draft_requested`, `correction.*`, v1 media events) are neither routed nor emitted |
| D4 | `data-model.md` entity fields | At least 25 verified field/constraint mismatches (UUIDv7 vs `uuid4`, TopicPolicy shape, CollectionRun schedule fields, PublishedEvidenceSnapshot shape, RetentionBatch states and others); three documented entities have no model |
| D5 | `publisher-adapter.md` names `run_canary()`, `render(stage)`, `prepare_media()` | The adapter protocol has four methods; these live in services |
| D6 | README: tests are a future Phase 7 task; run plain `pytest`; `pip install -e ".[dev]"` | 77 test modules exist; AGENTS.md and CI use `uv sync --frozen --extra dev` and explicit environment variables |
| D7 | README project tree and `plan.md` list `tests/contract`, `tests/e2e`, `tests/fault_injection`, `config/calibration-manifests` | None exists; tests are `tests/unit` and one integration module |
| D8 | README local quick start shows `C:\project\2026\` | Illustrative only; the sibling humanizer is resolved relative to the repository |
| D9 | `docs/service-analysis.md` links `code-analysis.md#findings-and-priorities` | Heading is "Current findings and priorities"; fixed in this refresh |
| D10 | Source registry allows `application/vnd.ms-excel` attachments | `SpreadsheetExtractor` accepts only XLSX/XLSM/CSV/TSV, so legacy XLS fails closed |
| D11 | OpenAPI `retryRun`: "Retry a safe run step", "Safe retry accepted" | Only a decision row is recorded, see finding R2 |
| D12 | `tasks.md`, T031 handoff: selective retry must requeue the selected unit | Still open; never carried into the 2026-10-03 reports |

Operational documents (helper-agent findings; file-level reading, Compose not run):

| # | Document statement | Current source |
| --- | --- | --- |
| D13 | `quickstart.md:3-5` says the repository has no application code; its 23-key environment table | Stale; 18 of the 23 names are read nowhere (for example `GLOBAL_KILL_SWITCH`, `WORDPRESS_BASE_URL`) |
| D14 | `quickstart.md:849` `verify_success_criteria`; `uv sync --all-groups` | No such command; only extras `ocr` and `dev` exist. The other nine named commands exist |
| D15 | README: native PDF runs on `extract.generic` | PDF/image documents route to `extract.ocr.paddle`, which runs the native extractor itself (`event_routes.py:167-174`) |
| D16 | README: every `/api/v1/*` request needs a session and CSRF | `GET /api/v1/local-articles/status` is exempt and loopback-only in local mode |
| D17 | README WordPress `env://WORDPRESS_*` variables | Compose forwards them to no service (inference from `compose.yaml`; not executed) |
| D18 | README first boot: run `compose-deploy.ps1`, then seed | The script requires an approved source registry before web starts, which a fresh database cannot have (inference from code; not executed) |
| D19 | README manual PaddleOCR model copy, seven models | An `ocr-model-bootstrap` service downloads 14 pinned models; README does not mention it |
| D20 | `research.md` source survey (2026-07-17) | Six of roughly fifty surveyed sources have adapters; eight registry entries exist, two disabled (`data_go_kr_housing`, `skhynix_newsroom`) |
| D21 | Container images pin uv 0.11.26; CI and local toolchain pin 0.12.7 | No document states the split |

Consistent and verified: the 15 queue names across README, code and Compose; ports
7667/3210/8000; pinned Python, Django, Celery, PostgreSQL, PaddleOCR and qpdf
versions; seed counts (3 housing, 5 semiconductor); 8 manifest extraction profiles;
all 54 OpenAPI paths resolve to routes.

### Reviewer-report cross-check

- All 60 numbered findings (A1–F8) and the rejected B-query have a disposition.
- Items with no disposition in the ledger or plan: invalid schedule timezone
  (repaired in code but unrecorded), OpenAPI wording that contradicts the dependency
  invariant, preflight replay after a snapshot change, bounded DELETE recovery,
  first-cursor-page-only console lists, missing topic append-only triggers, the
  kill-switch policy for revocation, and unchanged per-check policy versions after
  the grounding change.
- Regression tests exist for every "repaired" disposition except E6 (PostgreSQL
  only, no test) and the generic-materializer half of B9/E4/E5.
- Coverage limit kept: 8 account/audit/infrastructure migrations were never fully
  read by any reviewer, and the final state of the 33 product files changed in
  `db5edec` had no independent review before this session.
- The plan is called "completed" in two reports although 27 of its boxes are open;
  "completed" refers to writing the plan.

## Part 2 — Code review

### Verification executed in this session

| Check | Result |
| --- | --- |
| Environment | Python 3.12.10, Django 5.2.16, Celery 5.6.3, HTTPX 0.28.1, Ruff 0.16.0, pytest 9.1.1 |
| Inventory/AST | 335 Python files parse; src 251/108,908 lines, tests 77/40,644, scripts 7/1,105; 53 numbered migrations |
| OpenAPI | 3.1.0, 54 paths, 67 operations, unique operation IDs |
| `manage.py check` | No issues, exit 0 |
| `makemigrations --check --dry-run` | No changes, exit 0 |
| Fresh disposable SQLite `migrate`, `migrate --check` | Exit 0; collection 0012 and 0013 applied |
| Draft profile import (HTML, spreadsheet 1.2.0) | created=2, repeat unchanged=2; full candidate root fails on the unresolved HWP manifest as intended |
| Ruff `src/apps/local_content` | Passed |
| Ruff whole repository | Exit 1, 1,561 findings (E501 1,065; DJ001 139; I001 122; DJ008 62; E402 56; other 117) |
| Full pytest `tests/unit tests/integration` | Exit 0; 1,363 passed, 3 skipped, 7 warnings, 203 subtests; 565.72 s |
| Scratch probes (outside the repository) | 6 passed; evidence for R1, R4, R5, R6 |

Raw logs: ignored `.local/reviews/2026-10-04/`. The earlier report figures are
therefore reproduced at the committed HEAD.

### Findings

Severity is the reviewer's judgement for a distributed release; none affects the
local housing writer, which never enters these paths.

| ID | Severity | Status | Finding |
| --- | --- | --- | --- |
| R1 | High | Confirmed by execution | An unresolved credential revocation permanently locks the publication target |
| R2 | High | Confirmed by source | Run-level selective retry records a decision and retries nothing |
| R3 | High | Source-traced, not executed | A Blogger attempt with new media goes stale instead of publishing after WordPress |
| R4 | Low | Mechanism executed, trigger simulated | A same-number collection redelivery is acknowledged without collecting |
| R5 | Low | Confirmed by execution | Numeric grounding mis-parses compound Korean units |
| R6 | Low | Measured | HTML structural selector cost is quadratic in sibling count |
| R7 | Medium | Typed exception reproduced; ordinary triggers source-traced | StateConflict/Conflict from inactive or unverifiable topic/registry material aborts every due scan before later schedules |
| R8 | Info | Source | Uncertain media DELETE has no automated follow-up |

**R1 — revocation lock.** `db5edec` added
`_assert_target_has_no_pending_credential_revoke`
(`src/apps/publishing/services.py:2245-2252`) and calls it from target update,
OAuth start/complete, Blogger token refresh, preflight/canary persistence and
`disconnect_target`. It treats `accepted`, `revoking` and `reconciling` as pending.
`reconciling` is assigned at `services.py:13012` and `:13172` and nothing ever leaves
it; the disconnect route has no terminal handler (`event_routes.py:387-392`), and the
Django admin is read-only. A WordPress timeout during DELETE, or a worker redelivery
after the call boundary, therefore leaves a target that can never be disconnected
again, reconnected or given new credentials. Before the commit a second disconnect
was accepted. The probe drove the real `begin_target_credential_revoke` and
`persist_target_credential_revoke_result` to `reconciling` in both ways and observed
`Conflict("publication target cannot change during credential revocation")` from a
fresh re-authenticated disconnect and from a credential replacement. Suggested
direction: an audited reconcile/abandon transition for `reconciling`, a terminal
handler for dead-lettered deliveries, and regression tests for both.

**R2 — selective retry.** `POST /api/v1/runs/{id}/retry` calls
`request_selective_retry` (`src/apps/collection/services.py:829-869`), which records
a `RunControlDecision`, validates the scope and consumes a `bulk_retry` proof. No
code reads a retry decision or requeues a source attempt, document extraction or
publication attempt; the only executing retry is the separate
`publication-attempts/{id}/retry` route, which rejects `stale`, `permanent_failed`
and `manual_required` states that the run console offers. The API answers 202
"Safe retry accepted" and the console says the decision was recorded. This matches
the open item in `T031_WORK_IN_PROGRESS.md` and unchecked T028/T031, but the
canonical reports listed selective retry as implemented. FR-017 and FR-019 are not
met.

**R3 — Blogger with media.** Trace:

1. Dispatch plans media for every attempt, including the dependent Blogger attempt
   (`services.py:8666-8670`).
2. The media write guard runs `validate_attempt_gate(..., require_media=False)`
   (`:7350-7354`), which for Blogger requires the WordPress attempt to have succeeded
   (`:9203-9205`). `Conflict` is not a `ValueError`, so the consumer retries with
   backoff (probe: 17, 36, 62, 124 s) and dead-letters the operation after about four
   minutes; nothing re-plans it.
3. WordPress success releases every queued dependent immediately, without a media
   check (`:11645-11667`).
4. `_begin_attempt_locked` turns any gate conflict, including "media not ready",
   into terminal `stale` (`:10339-10397`).
5. Media success queues the attempt without checking the dependency (`:7600-7610`),
   using the same dedupe key as the release.

Blogger media can only become ready after WordPress succeeds, while the release
event is created in the same transaction as that success. Unless a previously
verified mapping is reused, the Blogger attempt is expected to end `stale`; with a
future `publishAt` the media operation is also expected to be dead-lettered first.
This is the mechanism behind plan Task 4, now with exact locations. It was not
executed end to end: a valid two-target cohort with real approvals was not built.
The plan's staged regression remains the acceptance test.

**R4 — collection redelivery.** `collect_source_attempt` returns early when
`delivery_attempt_no <= retry_count` (`collection/services.py:2127-2128`). The
outbox consumer rolls its attempt counter back after a database error
(`outbox.py:1152-1163`, `:1244-1249`). If the failure handler has committed
`retry_scheduled` and the receipt settlement then hits a database error, the
redelivery carries the same number, the handler returns normally, the receipt is
marked succeeded and the attempt stays `retry_scheduled`. The probe showed the
silent return; the double fault itself was simulated.

**R5 — numeric gate.** `_numeric_quantities` (`editorial/quality.py:38-53`) reads
"5천만원" as a unit-less 5,000, so it is "supported" by a span containing a bare
5000, and rejects "3억 5,000만원" against "350,000,000원". The gate is fail-closed in
most cases and remains the narrow check the reports describe.

**R6 — selector cost.** `HtmlExtractor._stable_selector` walks previous siblings for
every node and ancestor. Measured: 1,000 sibling items 0.18 s, 5,000 items 4.70 s.
A page with very many siblings before the content multiplies this. Sources are
approved official sites, so this is a robustness note.

**R7 — schedule isolation.** Only `ScheduleMaterialIneligible` is isolated
(`scheduling/services.py:1146-1156`). Any other persistent exception from one
schedule aborts the ordered scan and that schedule stays first. The mapping of every
`ValueError` from the material builder to "ineligible" is also broader than the
ledger wording.

**R8 — media DELETE.** After an unknown DELETE outcome no reconcile is queued
(`services.py:7588-7591`); the mapping stays uncertain until a manual action that
does not exist yet.

### Repairs re-read, with confirmed scope limits

S3 owned-download cleanup, HWP exclusive report creation, HTML structural selectors,
XLSX temporal serialisation (not array formulas: R10), cumulative CSV budget,
raw text `source_record` pointer (not attachment locators: R12),
derived input rights, WordPress exact self-revocation, Blogger write guard,
preview-hash compatibility pair, persisted `scheduled_for`, proof-required media
activation, request-bound preflight dedupe, validation verdict/digest protection
(with malformed response projection limits: R15), the 13 individually qualified
lock expressions (16 other affected expressions remain: R9), and console
cron/topic/MFA/source-link changes. These are bounded repairs, not closure of every
related service path. Schedule isolation does not cover ordinary domain conflicts (R7).

## Limits

No PostgreSQL, Redis/Celery, S3, WordPress, Blogger, OCR, HWP converter, browser,
official-source, humanizer or hosted-CI execution. R2 and R3 rest on source reading.
The whole code base was not re-read; unchanged modules rely on the 2026-10-03
coverage and its stated sampling gaps. Helper-agent statements were spot-checked,
not exhaustively re-verified. Three pytest skips were not individually identified.
An additional automated multi-agent review (`/code-review max`) was started during
this session and terminated by an API rate limit before reporting. Its ten finder
reports were later recovered and partly verified; see the next section.

## Follow-up code-review pass (2026-10-05, resumed and synthesized)

Scope: `git diff 7eb3331..db5edec` (code, not docs), reviewed by ten finder angles
(line scan, removed behaviour, cross-file trace, language pitfalls, guards, reuse,
simplification, efficiency, altitude, conventions). All ten finished; only the
verification/synthesis stage was lost to the rate limit. Raw finder reports and the
scratch probes are saved in the ignored `.local/reviews/2026-10-05/finders/` and
`.local/reviews/2026-10-05/probes/`. Source baseline unchanged: `db5edec`, docs-only
working-tree changes. The reboot continuation completed verification/synthesis from
these saved reports. No finder was rerun and no product fix or new commit was made.

### Verified in this pass

| ID | Severity | Status | Finding |
| --- | --- | --- | --- |
| R9 | High (distributed) | Compiled with the PostgreSQL compiler; PostgreSQL not executed | 16 `select_for_update()` calls without `of=` still `select_related` nullable FKs |
| R10 | Low | Confirmed by execution | XLSX array formulas produce non-JSON `ArrayFormula` cell values |
| R11 | Low | Confirmed by execution | Directory-like or over-long schedule time zones raise `OSError`, not `ValueError` |
| R12 | Low | Source | Raw attachment evidence still uses the rejected `record_key` locator |
| R13 | Medium | Canary worker/service path reproduced with mocked publisher and audit provenance; preflight source-traced | Pending disconnect blocks result persistence; cleanup IDs are lost and redelivery records stale failure without them |
| R14 | Info | Source | With the kill switch on, a disconnect ends `failed` and is not retried |
| R15 | Low | Executed report-view/schema probes | Rejected report retains copied samples; an untyped verifiedAt can violate the response schema |
| R16 | Low (HWP gated) | Injected filesystem-error probe; converter not executed | Exact-CLI output metadata failure replaces a resource-limit classification; late-file ownership remains conditional on private-directory integrity |

**R9 — PostgreSQL lock shape.** Django documents that PostgreSQL rejects
`select_for_update()` over the nullable side of an outer join ("FOR UPDATE cannot be
applied to the nullable side of an outer join"). `db5edec` qualified 13 sites with
explicit `of=` lock sets, but a static scan plus Django's PostgreSQL compiler (connections
forbidden) found these still compiling to `LEFT OUTER JOIN … FOR UPDATE` without
`OF`:
- `publishing/services.py`:
  - core publishing: `_lock_publication_attempt_domain` `:2331` and `_begin_attempt_locked` `:10058` (5 outer joins);
  - media delivery: `begin_media_delivery_operation` `:7144` (2 outer joins), `authorize_media_delivery_external_write` `:7301` (7) and `persist_media_delivery_operation_result` `:7390` (3);
  - other locks: `:3088`, `:5069`, `:5986` and `:7967`.
- `scheduling/services.py:274`: the schedule material builder, which joins nullable `canary_target`.
- `publishing/automation.py`: `:407`, which joins nullable `current_revision`, and `:419`.
- `publishing/corrections.py:272`.
- `evidence/tasks.py`: `:2989`, `:4643` and `:4686`.

Expected effect when these queries execute on PostgreSQL: nullable-side lock rejection.
This includes normal publication/media paths and schedule dispatch with target rows.
The workflow at [.github/workflows/quality.yml](../../.github/workflows/quality.yml)
explicitly uses local mode/SQLite on Windows and Ubuntu; it has no PostgreSQL service.
Django documents both the nullable-join restriction and SQLite's absence of row-lock
semantics. [Primary Django reference](https://docs.djangoproject.com/en/5.2/ref/models/querysets/#select-for-update).

The original scan listed 16 candidates, including three unresolved local/related
manager expressions; its compiler probes covered representative shapes. On resume,
main resolved those managers and compiled **all 16 actual production expressions**,
retaining filters/joins and replacing terminal get/first only to avoid evaluation.
Each retained 1–8 LEFT OUTER JOINs and an unqualified FOR UPDATE. Connections were
forbidden. The old 13 repaired-query regressions still pass. Thus the earlier repair
is valid for its enumerated sites, but incomplete across the service. No PostgreSQL
server execution, two-connection race or safe replacement lock set is claimed.
Probes: test_probe_pg_lock_sites.py and test_resumed_pg_queries.py.

**R7 update.** The most ordinary ineligibility conditions raise `StateConflict` or
`Conflict` (`DomainError`), not `ValueError`, so they escape
`ScheduleMaterialIneligible` (`scheduling/services.py:936-941`). The examples are:
- no verifiable active topic policy;
- an approved registry with no enabled source;
- unverifiable registry material.

These are raised in `topics/services.py:3627-3690`. `dispatch_due_schedules` then aborts
at the same oldest schedule on every 5-second beat, and later schedules starve. The
outbox pass itself continues. R7 is therefore Medium.

**R1 addition.** `revoke_target_credentials` (`publishing/tasks.py:316-360`) catches only
`PublisherError`. A `Conflict` from the revocation write guard, or a failure in
`publisher_for_target` before the `try`, propagates. Redelivery then sets `reconciling`
(`services.py:13008-13040`) although no remote call happened, which feeds the R1 lock.

**R10.** The probe saved a workbook with `ArrayFormula("B1:B2", "=A1:A2*2")` and then:
- ran `SpreadsheetExtractor.extract`;
- produced a cell value of type `ArrayFormula`;
- failed `json.dumps` with a `TypeError`.

This uses openpyxl 3.1.5 with `read_only=True, data_only=False`. It is the same class as
the repaired datetime case (`spreadsheet.py:49-52`).

**R11.** `ZoneInfo("Asia")` raised `PermissionError` on this Windows PC; Linux
execution was not performed. A 300-character key raised `OSError`. Only
`ZoneInfoNotFoundError` is mapped (`scheduling/services.py:825-832`). As a result,
schedule create and update return 500 instead of 422. The time zone is a free string
in OpenAPI.

**R12.** `evidence/tasks.py:1292` and `:1456` still write `path_type: "record_key"`.
`validate_evidence_locator` rejects this, and `editorial/quality.py:283-292` lists such
evidence as a missing high-impact locator. `EvidenceAsset.clean()` does not validate
RAW locators, so persistence succeeds. The effect is conditional on a high-impact
claim citing raw attachment evidence.

**R13.** `persist_target_preflight_result` (`services.py:4123`) and canary persistence
(`:4643`) call `_assert_target_has_no_active_external_write` before their stale
handling. Their request and begin steps are not guarded. In-flight work that meets a
newly accepted disconnect therefore:
- performs remote I/O;
- raises `Conflict` instead of recording `stale_after_call`;
- retries and may dead-letter.

On resume, main executed run_target_canary with actual begin/result services and a
synthetic publisher. Its create callback accepted a real reauthenticated disconnect;
subsequent simulated write/cleanup failed. The worker supplied one pending post ID
to persist_canary_run_result, which raised the real pending-revocation Conflict.
The database retained running state and an empty remote_cleanup_refs list.

On redelivery, disconnect's new target snapshot caused the earlier stale-snapshot
branch to record **failed / target_snapshot_stale**, still with no cleanup IDs.
It did not reach the running-to-cleanup_required branch in this scenario. This
confirms loss of operational recovery material rather than only an extra retry.
The test stubs worker-event/audit provenance and external publisher behavior; it
does not demonstrate a live leaked post or broker delivery. R13 is raised to Medium.

**R14.** Blogger revocation now runs through the composed guard; WordPress already did
(`wordpress/client.py:71-75`, `:525-529`). When the kill switch is on,
`PublisherError(category="retryable")` is persisted as `failed`
(`publishing/tasks.py:337-353`). The target is locally `revoked`, but the provider
credential stays valid until an administrator disconnects again. This is a design
decision to confirm.

**R15.** `publishing/api.py:672-681` copies list keys into the response before later
checks fail, and `verifiedAt` is not type-checked. When a later list field is invalid, the verdict falls back to failed while earlier
samples survive. A separate hash-valid report with an integer verifiedAt retains
that integer and fails the compiled response schema. These are projection/contract
defects; they do not establish bypass of digest checks or false acceptance of a
missing report.

### R16 — HWP cleanup exception and conditional ownership

At deploy/containers/hwp-worker/wisdome-hwp-sandbox:974–979, the finally block catches
only FileNotFoundError from output.lstat(). Main injected a resource-limit exception
from the converter boundary followed by PermissionError on that metadata read.
run_exact_cli returned converter-failure exit 21 instead of resource-limit exit 22.
The converter was stubbed; no Linux sandbox or real resource kill ran.

A second probe inserted a file after the initial collision check while the converter
stub failed. The wrapper claimed its inode afterward and removed it. This demonstrates
the ownership limitation if another writer can enter the directory; it is not a
proven exploit in the intended private directory. Existing pre-existing-file
preservation tests still pass. Preserve exclusive directory ownership, make failure
cleanup bounded, and retain the original error classification. HWP admission stays
hard-false and this does not authorize changing its gate.

### Remaining-candidate dispositions after resume

| Candidate | Result and limit |
| --- | --- |
| R13 cleanup references | Confirmed with actual worker/begin/persist/disconnect functions plus mocked publisher/audit provenance; see R13. |
| Unpublish with unproven media | In a real SQLite binding fixture, AVAILABLE mapping + PREPARED binding + no reconciliation timestamp causes the planner to request RECONCILE even for unpublish. Enqueue was intercepted, so no full withdrawal was executed. The same AVAILABLE mapping with an ACTIVE binding and remote_verified_at queues nothing. Narrow conditional planning defect; do not claim every withdrawal is delayed. Add these controls to Task 4/8 implementation. |
| Removed-binding conflict | No current production writer of REMOVED/REMOVAL_PENDING was found; not promoted as a reachable normal failure. Retain guards for historical/future data. |
| CSV/TSV CP949 | Actual CP949 Korean CSV raises UnicodeDecodeError; equivalent UTF-8 succeeds. UTF-8-only decoding already exists at 7eb3331. Existing format/encoding limitation, not a db5edec regression; supported encodings need an explicit policy before expanding decoding. |
| WordPress Core incorrect_password branch | Existing four identity/error-code controls pass. Core returns this code when no application password matches; accepting it after a matching successful DELETE is consistent with the supported Core path. Plugin/WAF variants remain unknown outcomes and require R1 recovery, not an automatic broad acceptance of arbitrary authentication failures. No live WordPress/plugin test. [Core source](https://developer.wordpress.org/reference/functions/wp_authenticate_application_password/). |
| Repeated parsing/hashing, duplicated predicates/discovery/helpers and stored text/selector size | Refactoring/efficiency suggestions, not separately demonstrated correctness failures; keep behind invariant tests and release versioning. No new architecture rewrite follows from these observations. |
| LOCAL_OUTPUT_ROOT naming | Corrected current guidance to LOCAL_ARTICLE_ROOT, verified at settings/__init__.py:53–55. Historical mistaken name is identified, not silently treated as a setting. Resumed commands remove all four names. |
| Six reviewer-report paths | AGENTS links the canonical adjudication index, which links all six. Discoverability is satisfied; no requirement to duplicate the six paths at every entry point. |
| Old A11 report anchor | Added a compatibility anchor in code-analysis.md; the dated reviewer text is preserved. |

### Resumed verification evidence

All commands ran at repository root with development/local and PYTHONUTF8=1.
LOCAL_STATE_ROOT, LOCAL_OBJECT_ROOT, LOCAL_ARTICLE_ROOT and obsolete LOCAL_OUTPUT_ROOT
were removed from child environments, not assigned empty strings. The product tree
remains db5edec; only documentation/instructions and ignored diagnostic files changed.

| Check | Actual result |
| --- | --- |
| Existing scratch probes, explicit -c pyproject.toml --rootdir . | 9 passed in 26.67 s; reconciling lock, same-number retry, numeric parser, timing and representative compiler evidence reproduced |
| All remaining actual lock expressions | 16 passed in 1.63 s, asserting the bad SQL shape; PostgreSQL connection forbidden |
| New candidate probes | 11 passed, 14 inherited tests deselected, 23.43 s; canary cleanup loss, typed schedule starvation, array formula, timezone, report projection, HWP faults and two withdrawal-planner controls |
| CP949/UTF-8 control | 1 passed, 25 deselected, 1.58 s |
| Existing repaired-query/WordPress/pre-existing-HWP regressions | 19 passed in 0.38 s |
| Django check / migration drift | Exit 0; no issues / no changes detected |
| Documentation verification | 13 Markdown files, 97 local links and 7 anchors checked; no broken links/fences/whitespace; git diff --check passed |
| Product diff against db5edec | Empty for src, scripts, tests, config, deploy, Compose and CI |
| Earlier full suite in this 2026-10-05 report | 1,363 passed / 3 skipped / 203 subtests remains earlier evidence; not rerun after reboot because product code did not change |

The 37 diagnostic assertions describe observed defects and controls; they are not
37 bug fixes. The 19 repository regressions prove their existing narrow contracts.
An initial new-probe collection error referenced a nonexistent PublisherError module;
the scratch import was corrected before the reported 11-case run. No product
defect or result is inferred from that setup error.

Reproduction entry points (ignored, persistent on this PC):
- .local/reviews/2026-10-05/probes/test_probe_credential_revoke_lock.py
- .local/reviews/2026-10-05/probes/test_probe_misc.py
- .local/reviews/2026-10-05/probes/test_probe_pg_lock_sites.py
- .local/reviews/2026-10-05/probes/test_resumed_pg_queries.py
- .local/reviews/2026-10-05/probes/test_resumed_candidates.py

Run individual selected probes with:
.venv/Scripts/python.exe -m pytest -q -s -c pyproject.toml --rootdir . <probe-path>.
For TestUnpublishMediaProbe, select its two new methods so inherited repository
tests are not mislabeled as new diagnostics. New candidate names are explicit in
the durable table above; the corresponding implementation plan specifies regressions
to promote into tracked tests when fixes are made.

### Continuation steps

- [x] Propagate R9–R15, R1/R7 updates and the newly verified R16 into code/service
  analysis and the English plan. Correct overbroad lock/spreadsheet/schedule claims.
- [x] Verify canary cleanup-reference loss, conditional withdrawal media planning,
  HWP cleanup, report projection and timezone/array-formula probes.
- [x] Re-run saved probes with explicit repository configuration and clean root variables.
- [x] Replace the interrupted status in AGENTS.md with completed verification/synthesis.

Next implementation work is in Task 8 of the canonical plan: R9 production lock
coverage with explicit related-row invariants and PostgreSQL CI; R1/R13 recovery
and durable result/cleanup evidence; R7 typed schedule isolation; then bounded
input/locator/report/HWP defects. Actual PostgreSQL, broker/storage/publisher and
signed HWP acceptance remain external verification gates. No product code was
changed and no commit/push was made during this continuation.

| Date | Update |
| --- | --- |
| 2026-10-05, initial pass | Documents D1–D21 and repair-commit findings R1–R8; full checks reproduced |
| 2026-10-05, interrupted pass | Ten finder outputs recovered; R9–R15 and R1/R7 additions saved before reboot |
| 2026-10-05, resumed completion | Main compiled all 16 remaining queries, executed candidate probes, corrected R13 redelivery outcome, added gated R16, and reconciled canonical reports/plan without product edits |
