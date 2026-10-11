# Wisdome Super Writer code analysis

## Baseline and conclusion

- Updated: 2026-10-10, Asia/Seoul; current source, saved-data projections,
  focused tests and selected release defects revalidated. October 9 full-suite,
  live-refresh and Chrome desktop/responsive acceptance remain historical evidence.
- Source: codex/policy-funding at db5edec35aad39ae85e3aca7b6f9e1b9f2e3a0dc plus new
  policy-funding, portal and monthly code. The feature began with documentation-only working changes,
  which were preserved. The base commit contains the
  2026-10-03 repairs below; earlier "7eb3331 plus uncommitted repairs" wording in the
  linked review documents describes the pre-commit state.
- 2026-10-05 evidence: [documentation and follow-up code review](analysis/2026-10-05-documentation-and-code-review.md).
- The original October 3 analysis began clean, its repair review preserved existing
  documentation, and October 4 committed that work. October 5's follow-up began
  clean; this reboot continuation preserved its five modified reports/instructions
  and untracked follow-up report.
- Scope: all major product domains, migrations, adapters, configuration, local tooling,
  deployment/CI, console/contracts and relevant tests. Exact semantic-read/sampling
  boundaries are in the [six reviewer reports](analysis/2026-10-03-two-round-review.md#reviewer-evidence-and-coverage).
  That whole-domain scope describes the historical reviews. October 10 samples
  current portal/collection/storage paths and selected distributed defects below.
- Companion [service analysis](service-analysis.md), [adjudication ledger](analysis/2026-10-03-two-round-review.md),
  and [English modification plan](superpowers/plans/2026-10-03-critical-review-remediation.md).

The code is a substantial Django modular monolith with asynchronous distributed work
and a separate Windows local housing writer. Two groups of three independent critical
reviewers found real producer/consumer, ownership, generation, approval, media,
scheduling and reporting defects. Main independently reproduced and repaired them.
The principal remaining release risks include 16 still-invalid PostgreSQL locking
queries (R9), credential/canary recovery (R1/R13), schedule starvation (R7), and
actual PostgreSQL/distributed acceptance,
unfinished withdrawal/visual integration, fresh release approval and closed HWP
admission. A passing local suite cannot resolve these release gates.

## 2026-10-10 current-state revalidation

The current branch and full HEAD are unchanged from the baseline above. Initial
`git status --porcelain=v1 -z` recorded 18 modified tracked files and 53 untracked
entries. The new funding, portal, news archive and regression modules are among the
untracked entries; `git diff --stat` alone excludes their implementation. Existing
work was preserved. This analysis changes only the canonical reports and AGENTS
handoff, not product code, dependencies, source/output state or task completion.

Current source reads confirm the root/topic routes, strict KST period parsing,
housing stage-date history selection, funding posting-date history selection,
immutable detail references and local news metadata precedence. The architecture
still separates local immutable archives from database-backed editorial/publishing
work. Source inspection and actual readback retain partial coverage, unknown dates,
live/fixture provenance and disabled/unsupported-source distinctions.

Actual saved-data readback returned housing 23 October identities (special 18,
first 21), funding 17 postings and two live news records. Housing history has 175
distinct identities and 124 without the selected stage dates; latest funding run
has 93 records. These counts are saved observations, not a new live collection.
In-process home/housing/news/drafts/funding rendering returned 200; four topic
pages contain year controls. The active SQLite has 71 pending migrations, and
`drafts_available=False` is displayed as unavailable rather than an empty usable
store. Explicit UTF-8 pipeline output was required to correct an initial false
Korean-literal match in the inspection script. It did not require a product fix.

### Revalidated defects and priorities

| Finding | October 10 evidence | Consequence / priority |
| --- | --- | --- |
| R9 | Re-ran all 16 saved compiler probes against the actual production expressions; each retains nullable outer joins and unqualified `FOR UPDATE`; connections forbidden | Distributed release blocker; qualify the intended lock set and prove related-row invariants on real PostgreSQL |
| R1 | Two parameterized local probes reproduce recovery blocking after unknown revocation outcome and worker redelivery | Add audited reconcile/abandon/terminal recovery; no live credential revocation executed |
| R13 | Local canary/disconnect probe again loses pending cleanup references and records stale failure on redelivery | Persist immutable outcome/cleanup evidence before rejecting stale current-state projection |
| R7 | Typed StateConflict probe again aborts the due scan before the later healthy tick | Explicit per-tick domain-ineligibility isolation; actual inactive-policy end-to-end acceptance remains unexecuted |
| R2 | Reread `collection/services.py:829`: records a decision, resolves the target and consumes proof, then returns without dispatch | Run retry still does not execute selected work; T028/T031 remain open |
| R5 | Numeric diagnostic again treats `5천만원` as unit-less 5,000 and fails equivalent compound-unit quantities | Grounding remains a narrow parser; implement compound Korean monetary units before making broader assurance claims |
| Local database | MigrationExecutor reports 71 unapplied migrations; isolated fresh SQLite applies all 71 | Active runtime preparation/recovery gap, not demonstrated migration-code failure |
| Release baseline | Initial 18 modified files / 53 untracked entries | Recent feature acceptance does not describe HEAD alone; establish a reviewed source identity before release |

The source bytes of publishing services/automation/corrections, scheduling services,
evidence tasks, collection services, editorial quality and the CI workflow match
HEAD after CRLF normalization. Other historical R findings retain their prior
source/probe limits; this refresh does not claim to re-execute all R1–R16. The CI
workflow still specifies local mode on Windows/Ubuntu without a PostgreSQL service.
The legacy HWP activation validator still returns false; admission remains closed.

### Commands and results executed today

All commands used `.venv/Scripts/python.exe`, development/local settings and
`PYTHONUTF8=1`; the existing `.env.local` was preserved. Default local roots were
used after removing inherited `LOCAL_STATE_ROOT`, `LOCAL_OBJECT_ROOT`,
`LOCAL_ARTICLE_ROOT` and obsolete `LOCAL_OUTPUT_ROOT` from the subprocess
environment. UTF-8 `$OutputEncoding` was set for the corrected stdin probe.

| Command / scope | Result |
| --- | --- |
| `src/manage.py check`, then `src/manage.py makemigrations --check --dry-run` | No issues; no changes detected; exit 0 |
| `-m pytest -q <19 selected unit modules>` | 468 passed in 51.64 seconds; exit 0 |
| `-m ruff check src/apps/local_content src/apps/policy_funding src/wisdome_writer/portal` | All checks passed; exit 0 |
| `-m ruff check . --output-format json` | Exit 1; 1,561 diagnostics, including E501 1,065; same total as the historical baseline |
| `ast.parse` of every Python file in src/tests/scripts | 381 parsed, no syntax errors; inventory below verified today |
| `-m pytest -q -c pyproject.toml --rootdir . .local/reviews/2026-10-05/probes/test_resumed_pg_queries.py` | 16 passed in 1.58 seconds; assertions confirm bad SQL shapes, no PostgreSQL execution |
| Diagnostic selection below, using `-c pyproject.toml --rootdir . -p tests.conftest` | Five passed in 24.40 seconds; assertions confirm R1/R13/R7/R5 defects remain |
| `src/manage.py migrate --noinput`, with unique scratch `LOCAL_STATE_ROOT` and `DATABASE_URL` | All 71 migrations applied, exit 0; log at `.local/reviews/analysis-2026-10-10/fresh-sqlite-882b3d32c8a344f4addb270994e217f1/migrate.log`; active DB unchanged |
| `src/manage.py migrate --check`, against the same scratch database | Exit 0; no pending migrations in the isolated database |
| UTF-8/fence/local-link validation and `git diff --check` on both reports and AGENTS | No validation errors; 106 resolving local links; diff check exit 0 with existing LF/CRLF warnings |

The focused unit selection is reproducible by enumerating `tests/unit` files
matching `test_applyhome_subscription_schedule.py`, `test_bundle_windows_metadata.py`,
`test_funding_periods.py`, `test_housing_collection_start.py`,
`test_local_content_bundles.py`, `test_monthly_portal.py`, `test_news_archive.py`,
`test_period_selection.py`, `test_policy_funding_*.py`, `test_portal_*.py` and
`test_topic_portal.py`, then passing their absolute paths to pytest.

The five diagnostic cases are the two parameterized cases in
`test_probe_credential_revoke_lock.py`,
`test_resumed_candidates.py::test_canary_loses_cleanup_refs_when_disconnect_blocks_result`,
`test_resumed_candidates.py::test_state_conflict_still_aborts_due_scan` and
`test_probe_misc.py::test_numeric_quantity_parsing`, under the historical
`.local/reviews/2026-10-05/probes/` directory. These ignored scripts are local
reproduction aids. The findings, source locations and outcomes remain portable
in this report and the canonical October 5 review.

No full suite, new browser/physical-mobile check, live collection, active-state
migration, PostgreSQL/Celery/Redis/S3 acceptance, publisher write or OCR/HWP
acceptance ran today. The 1,689-pass October 9 full result retains its historical
date. All task gates and the existing implementation plan remain unchanged.

## Measured inventory

Counts exclude virtual environments/generated output. Lines include blank lines,
comments and migrations; counts measure review surface rather than complexity.

| Item | Current working-tree value |
| --- | --- |
| Python source | 279 files; 114,207 lines under src/ (2026-10-10 AST pass) |
| Python tests | 95 files; 44,806 lines including conftest.py (2026-10-10) |
| Python scripts | 7 files; 1,105 lines |
| Parsed Python | 381 files under src/, scripts/ and tests/ (2026-10-10) |
| OpenAPI | 54 paths, 67 operations, unique operation IDs at initial inventory; report schema updated without a new route |
| Celery queues | 15 |
| Seed registries | Three housing, five semiconductor sources |
| Review process | Three reviewers per round, two rounds; main alone edited product code |

Initial inventory was 249 source files/108,524 lines, 63 test files/38,966 lines and
319 parsed Python files. Two forward migrations and regression modules account for
the new surface. Inventory/AST/full pytest do not mean every line was semantically
reviewed twice. Round two fully read all 25 baseline pipeline migrations and four
previously sampled large publishing migrations; other DDL and test bodies retain
explicit sampling limits. No coverage percentage or security certification is claimed.

## Code map and execution paths

| Location | Responsibility |
| --- | --- |
| wisdome_writer/settings and runtime_mode | Explicit local/distributed environments and production configuration |
| wisdome_writer/api and infrastructure | Staff/session/CSRF, closed requests, safe HTTP, secrets, outbox and consumer leases |
| wisdome_writer/portal | Read-only root/topic views, display-only navigation, bounded validated housing snapshot and topic-scoped semiconductor projections |
| apps/accounts, topics, audit | Scoped proofs, immutable source/policy registry heads, append-only audit and retention |
| apps/collection | Runs/steps, source versions/status lineage, collection delivery settlement, stop/retry |
| apps/evidence | Profile release verification, extraction generations, object reservations and immutable evidence |
| apps/editorial | Clustering/verification, claims/citations, revisions, quality and correction decisions |
| apps/publishing | Credentials, target snapshots, canaries, validation/activation, previews, approval, dispatch/media/reconcile |
| apps/scheduling | CAS/frozen tick material, due scans, queue-one/coalescing and kill switch |
| apps/local_content | Official HTML/API reconciliation, selection/rendering, humanizer, graphics, immutable bundles/preview |
| apps/policy_funding | Official funding source catalog/collectors, closed notice contracts, simple Korean education, dated case evidence, immutable policy runs, CLI and read-only UI |
| adapters, templates/static, scripts/deploy | External interfaces, Korean console, Windows lifecycle, acceptance/CI and deployment |
| specs/docs | Requirements, dependency/task authority, plans and dated acceptance |

Python is restricted to 3.12. This locked environment uses Django 5.2.16,
Celery 5.6.3 and HTTPX 0.28.1; PaddleOCR is optional. Version inventory is not a
current vulnerability or upgrade assessment. Dependency declarations/lockfile unchanged.

Local collection creates a KST window, applies housing selection, renders factual
Markdown/graphics, verifies protected humanized prose and atomically publishes
confined immutable bundles. Loopback preview independently checks inventories,
hashes, URL policy and security headers. Mode, completeness and live success remain
separate. The sibling humanizer implementation is outside this repository's scope.

Distributed requests freeze approved registry/profile/policy material. Transactional
outbox and separate dispatcher/consumer leases handle replay, retry and terminal work.
Extraction binds inputs, releases, generations and objects. Editorial generation is
deterministic and evidence-bound. Intent, approval and dispatch have separate replay
and current-head boundaries; attempts/reconciliation/target fences bind work.
Blogger follows the exact accepted WordPress attempt. Exact replay must continue to
precede mutable current checks; historical approval alone is not current permission.

Evidence: [local workflow](../src/apps/local_content/workflow.py),
[bundles](../src/apps/local_content/bundles.py),
[outbox](../src/wisdome_writer/infrastructure/outbox.py),
[publishing](../src/apps/publishing/services.py),
[evidence](../src/apps/evidence/tasks.py),
[editorial](../src/apps/editorial/services.py),
[scheduling](../src/apps/scheduling/services.py).

## Three-topic portal integration (2026-10-08)

The new portal package separates root, housing and semiconductor views; funding
remains in its existing module with new glossary/history views. A shared template
shell and external CSS replace the disconnected reading-page headers. Existing
console authentication, local article URLs/assets and funding contracts remain.
No dependencies, models or migrations changed.

Housing uses a read-only validation subclass to prevent root creation, requires
schema-v2 metadata binding, selects by saved window time, and preserves legacy
archive links without trusting unbound metadata. Search now precedes the 100-row
display limit. Semiconductor projections exclude other topics/tombstones and
verify current-revision ownership; database errors return unavailable counts and
a labelled source-config fallback. Outgoing URLs and escaped rendering retain
their existing constraints.

An independent scoped reviewer found two P2s: search-after-truncation and omitted
saved housing status. Both were reproduced and repaired. Projection, access,
topic-menu and template-rendering regression evidence is recorded in the
[portal progress record](analysis/2026-10-08-topic-portal-progress.md) and
[review](analysis/2026-10-08-topic-portal-review.md). The original chat's permission
denial blocked direct Chrome checks at that checkpoint. October 9 user-authorized
fresh-chat [Chrome acceptance](analysis/2026-10-09-browser-access-diagnosis.md)
now verifies desktop and responsive menu/period/overflow behavior. Physical-device
and complete reference-video verification remain outside that scope. Source-level
responsive CSS and passing server tests alone are not visual verification. Final repaired full suite:
1,502 passed, 3 skipped, 203 subtests in 529.16 seconds. Django check/drift,
scoped portal/funding/local-content lint and static collection passed. The actual
local runtime schema still has 71 unapplied migrations; no migration was applied
to that database as part of this UI work.

## Monthly projections, collection and storage repair (2026-10-08)

`portal/periods.py` centralizes strict year/month/basis parsing, KST calendar bounds,
unknown-date exclusion and preserved UI parameters. Housing views allow schedule
dates only and explicit special/first selection. Hash-bound parser-v2 stage facts
carry regional labels; generic application bounds cannot establish these stages.
Active period selection reads bounded history before identity deduplication and
the display cap. Funding follows official posting dates, selects history before
deduplication, and binds both detail links to the selected run. Return links
preserve the selected period and filters. Collection history remains date-distinct.

`collect_recent_housing --since` adds a bounded explicit earlier publication
lookback without changing legacy days behavior. Notice-only capture commits
validated metadata while writing no generated articles. The local semiconductor
command and `portal/news_archive.py` reuse configured source access/rights and
pinned HTTPS/robots checks, persist polling/rate reservations, and save immutable
metadata and per-source outcomes. Injected fetchers are always fixture provenance.
Reads preserve prior positive observations across failed or bounded refreshes.
No database approval, runtime migration or article publication is fabricated.

Independent monthly review reproduced historical funding detail drift, database
news hiding the new local archive, and omitted unknown-date explanation. Repairs
bind run identity, add explicit inventory selection with local auto precedence,
and display unknown dates. Main revalidated return-filter behavior RED/GREEN;
the route/funding group18 passed. Separate combined monthly scope230 passed
before the subsequent Samsung/SIA parser repairs. See the
[review and adjudication](analysis/2026-10-08-monthly-update-review.md) and
[progress](analysis/2026-10-08-monthly-data-update.md) for final evidence.

The funding live commit exposed two Windows identity-only false positives.
The immutable bundle writer ignores only the harmless archive-bit identity delta.
A separate observed0x10000000 lstat/native disagreement requires two identical
native attribute reads plus a second unchanged full fingerprint before identity
reconciliation; arbitrary flags, actual native bits, inode/time/content/reparse
changes still fail closed. Full read/content fingerprints are unchanged.
Controlled and naturally reproduced fixture evidence plus242 scoped passing tests
and an actual validated funding commit are in the
[storage diagnosis](analysis/2026-10-08-monthly-storage-diagnosis.md).

These local repairs do not close the older R1–R16 distributed findings. Two monthly
full-suite attempts were intentionally stopped for proven parser/presentation
fixes; their partial output is not acceptance. The final stable full suite returned
**1,689 passed, 3 skipped, 203 subtests passed in 540.75 seconds**, exit 0. Final Django
checks and migration drift pass; scoped Ruff passes. Actual saved-data in-process
rendering verifies the selected schedule/posting semantics, snapshot/return state
and truthful missing-draft-store behavior. A final normal eligible news command
validated generation `2026-10-09`: MOTIR1, SIA1 and Samsung successfully empty,
with KRX unsupported and SK hynix disabled. The final in-process actual-data
rendering confirms both live news records. These are bounded metadata captures,
not article bodies, distributed source approvals or exhaustive coverage.
Subsequent October 9 sanctioned Chrome acceptance confirms October housing 23
(special 18 / first 21), funding 17 and news 2, selected date semantics, honest
empty searches, unavailable database drafts and immutable funding detail/return
state. No material new product defect was reproduced; no code repair, collection
or full-suite rerun was performed. The browser diagnosis records actual captures,
viewport geometry and remaining physical-device/external-source limits.

## Policy funding implementation and review

The feature is a separate module alongside housing and semiconductor workflows.
It reuses safe HTTP and adds one public identity-bound run-metadata reader to
ArticleBundleWriter. It does not change housing bundle formats, dependency locks,
database models/migrations or distributed publication behavior.

New contracts bind official source/notice identity, aware capture time, source
registration date, unknown scope and fixture/live provenance. Per-source parsers
cover OLS public JSON/HTML and Bizinfo finance open/ended lists; source policy,
robots, MIME/bytes/time/count and final redirect identity are checked. Directory
entries are not promoted to active crawlers automatically.

Independent review identified eight material defects in the first implementation.
Main reproduced and repaired province/unknown-region classification, fee-support
negation, pre-redirect robots checks, final ID/board binding, ended-status display,
program/event-context case verification, malformed nested metadata handling and
repayment-relief purpose. Registration labels, portable case evidence, CLI region
selection and historical snapshot links were also completed. Review evidence
retains the original failed cases and main's final residual-fix proof.

Tests exercise real parsers, shared storage, Django routes and safe HTTP with
external-I/O doubles. An actual three-source CLI run collected 12 notices through
19 requests without source errors; repeated final input produced 12 unchanged
notices. Actual Brave desktop/mobile checks passed. Whole Ruff remains 1,561
pre-existing findings; new modules/tests/local-content lint pass.

Canonical evidence: [acceptance](acceptance/2026-10-05-policy-funding-acceptance.md),
[independent review](analysis/2026-10-05-policy-funding-review.md),
[plan](superpowers/plans/2026-10-05-policy-funding-implementation.md),
[restart ledger](analysis/2026-10-05-policy-funding-progress.md).
The [October 6 follow-up](analysis/2026-10-06-policy-funding-consultant-claims.md)
adds shared conversational curriculum content and a separate manual dated case
to the existing read-only guide. It fixes explicit application-period heading
extraction with a failing-then-passing daily-window regression. The closed policy
bundle metadata contract, original supplied-case verifier and historical bytes
remain unchanged. Fresh evidence: 63 funding tests, Django/drift and scoped Ruff
pass; forty live notices through 47 requests and desktop/mobile guide checks pass.
The previous 1,425-pass full suite was not rerun for these narrow changes.
Old distributed R1–R16 below remain separate outstanding issues; this new module
does not execute those publication/row-lock paths.

## Current findings and priorities

| ID | Priority | Current finding/status | Required next work |
| --- | --- | --- | --- |
| C01 | P0 for supported HWP release | Legacy activation deliberately returns false; old and candidate profiles remain golden-unapproved | Implement independently signed acceptance-byte/trust validation and actual converter/corpus evidence before admission |
| C02 | P0 for distributed release | Local/compiler/mocked checks do not execute PostgreSQL, broker/storage or real publishers | Execute plan Tasks 1 and 6; honor T031/T032 and prerequisite gates |
| C03 | P1 | Large publishing/evidence orchestration modules mix many transaction/invariant boundaries | Decompose only after real backend regressions protect lock/replay behavior |
| C04 | P1 / distributed blocker | E7 repaired 13 enumerated expressions, but R9 confirms 16 remaining nullable-join lock expressions; E6 migration still not executed on PostgreSQL | Repair remaining lock invariants and add PostgreSQL CI, populated migration/role/trigger/two-connection tests |
| C05 | P1 | Whole Ruff still fails: 1,561 findings, initial 1,561 | Introduced diagnostics removed; triage pre-existing debt without claiming repository lint passes |
| C06 | Resolved locally | Invalid Windows fixture launcher, UTF-8 assumptions and fixed startup delay | Valid venv/BOM/explicit encoding/observable lock now tested; preserve platform skips |
| C07 | P1 | Historical root handoff/task evidence predates current runtime and repairs | Use current canonical reports; preserve dated task/acceptance authority |
| C08 | P1 | Distributed readiness checks DB/broker/outbox, not every worker/storage/publisher | Define/exercise dependency and alerting acceptance |
| C09 | P2 | Preview repeatedly validates/re-hashes growing run/article histories | Benchmark before adding integrity-preserving caching |
| C10 | P2 | Test fixture replaces TransactionTestCase teardown and manages SQLite triggers | Review cleanup/version coupling; real PostgreSQL tests must not suspend tested guards |
| C11 | P1 | C12/F6 console has no action-specific renderless withdrawal review | Server-derived subject, scoped proof, CAS and authenticated console journey |
| C12 | P1 | C14 approved media lacks final body placement | Bind approved slots/delivery URLs/final hash and channel payload; T022 acceptance |
| C13 | P0 for rollout | Repaired bytes invalidate old immutable source/profile/policy approval | Fresh releases/snapshots/verification and ordinary approval; do not rewrite old hashes |
| C14 | P1 for database-backed local work | October 10 active SQLite has 71 unapplied migrations; drafts unavailable despite local news metadata | Validate/back up active state before a separately scoped migration/recovery task; fresh isolated migration succeeds |
| C15 | P2 for release reproducibility | Funding/portal/monthly work remains uncommitted, including untracked implementation modules | Preserve and review the complete working tree; establish source identity before release |

2026-10-05 follow-up findings on `db5edec` (details, evidence and locations in the
[follow-up report](analysis/2026-10-05-documentation-and-code-review.md#findings)).
`R` IDs avoid the collision between the `C01`–`C15` rows above and reviewer C1–C14.

| ID | Priority | Current finding/status | Required next work |
| --- | --- | --- | --- |
| R1 | P1 | Reconciling revocation has no exit and blocks recovery; pre-call adapter/guard failures can also reach it on redelivery without any remote write. Unknown-outcome/redelivery lock reproduced, pre-call extension source-traced | Audited reconcile/abandon and terminal handling that distinguishes pre-I/O failure from uncertain external outcome |
| R2 | P1 | `POST /runs/{id}/retry` records a `RunControlDecision` and requeues nothing; source/document/run-scoped publication retries do not execute (open T028/T031 item) | Execute the selected unit through existing owners, or narrow API/console wording until implemented |
| R3 | P1 | Source trace: dependent Blogger attempts with media plan media before WordPress, media writes fail the dependency gate, WordPress success releases Blogger without a media check and the gate marks it `stale` | Plan Task 4 staged regression first; then release media/publication only after the exact dependency |
| R4 | P2 | Same-number collection redelivery returns without collecting after an outbox infrastructure rollback | Treat `<=` only as replay of a terminal settlement; regression with consumer double fault |
| R5 | P2 | Numeric grounding parses "5천만원" as 5,000 and fails "3억 5,000만원" vs "350,000,000원" | Compound Korean scale parser with tests |
| R6 | P3 | HTML structural selector cost quadratic in siblings (5,000 items 4.70 s) | Precompute sibling ordinals once per tree |
| R7 | P2 / Medium | Ordinary inactive/unverifiable policy/registry conditions raise StateConflict/Conflict, bypass ValueError-only isolation, keep the earliest tick due and starve later schedules; typed-error replay reproduced | Explicit domain-ineligibility contract and audited per-tick isolation with a real inactive-policy/healthy-tick regression |
| R8 | P2 | Uncertain media DELETE has no automated follow-up | Bounded absence-proof reconcile |
| R9 | P1 / distributed blocker | All 16 remaining production expressions compile LEFT OUTER JOIN plus unqualified FOR UPDATE; compiler only, PostgreSQL execution absent | Inventory every query/related-manager/helper lock, preserve required mutable-related locks, add PostgreSQL transaction CI |
| R10 | P3 | ArrayFormula values from a real XLSX workbook remain non-JSON, despite the temporal-value repair | Define formula-cell material and canonical serialization/rejection; preserve formula provenance |
| R11 | P3 | Asia and an overlong timezone produce OSError instead of validation ValueError on this PC; 500 implication is source-traced | Normalize bounded invalid-zone errors to 422 and test actual API create/update |
| R12 | P3 | Two raw attachment producers still emit rejected record_key locators | Shared raw-locator contract plus actual downloaded/metadata-only attachment → claim verification |
| R13 | P2 / Medium | Canary result with a pending cleanup ID is rejected by the disconnect guard; actual local worker/service probe retains running/empty refs, then redelivery records stale failure/empty refs | Persist immutable outcome/cleanup evidence before declining stale target projection; cover preflight/canary/rotation/replay |
| R14 | Policy / Info | Kill switch makes revocation fail terminally without automatic retry while target is locally revoked | Define whether revocation is a permitted protective action or a paused operation; preserve truthful provider state |
| R15 | P3 | Hash-valid malformed report keeps copied samples after fallback; integer verifiedAt violates response schema | Validate a complete temporary report before atomic projection, including timestamp type/format |
| R16 | P3 / HWP gated | Output metadata denial masks converter resource-limit exit; late-file ownership needs private-directory integrity | Preserve original failure during bounded cleanup; test isolated ownership and filesystem denial without opening HWP admission |

Size/performance risks are structural inferences, not measured incidents. Lint
diagnostics are not automatically functional defects: baseline B023 nested resolver
warnings, for example, were synchronously consumed and corruption was not demonstrated.
The full finding-by-finding ledger includes accepted, rejected and conditional claims.

## Verified repairs and limitations

- Storage/HWP rejection cleans only owned paths. CI/acceptance discovery preserves
  Unicode filenames and separates CLI options. Acceptance binds current run and
  independently verified eligible history.
- Attachment-free collection persists, change events contain plain strings, and
  older collector responses cannot overwrite a newer failed/running delivery.
- HTML pointers resolve original nodes; XLSX date/time/duration values serialize.
  Array formulas remain non-JSON (R10). Spreadsheet locators/cumulative cells/blank
  rows have narrow passing regressions; CP949 decoding remains a pre-existing limit.
- Raw text records freeze a resolvable source_record envelope; raw attachment
  producers still use invalid record_key locators (R12). Child rights/review
  requirements derive from parent inputs. Numeric fact/company-claim quantities
  must be witnessed in cited spans, including equivalent unit normalization.
  This narrower gate does not establish full semantic entailment.
- Validation consumes its own scoped proof and supports audited exact replay.
  Preview hashes share a producer/consumer contract with narrow historical
  empty-history compatibility; mutation/downgrade cases remain rejected.
- Dispatch stores accepted future time before media waits. Reused media activates
  only with exact prior reconciliation proof. Uncertain deletion retains recovery
  evidence without falsely declaring absence.
- Disconnect freezes original credential subject and blocks concurrent replacement;
  its pending guard also locks unresolved targets (R1) and blocks canary result
  evidence (R13). Per-I/O guards run on both adapters. WordPress self-revocation accepts only an
  exact identity-bound Core result.
- ValueError-derived ineligible material produces an audited hold, but ordinary
  StateConflict/Conflict bypasses it and still starves later ticks (R7).
  Console PATCH/MFA/source links and target contracts have passing controls.
  Report bytes are bounded/versioned/digest-checked, but malformed response fields
  can be partially copied or retain invalid timestamp types (R15).
- E7 qualifies 13 enumerated expressions, preserving their explicit run-first order
  and correction's mutable article-head lock. R9 finds 16 other expressions still
  rejected by PostgreSQL's lock semantics, including publication/media/schedule paths.
  E6 retains INSERT lineage plus the separate retention UPDATE/DELETE guard.
  Compiler and SQLite checks do not close PostgreSQL compatibility or execution.
- Two non-HWP 1.2.0 candidates import as drafts; editorial policies are 1.1.0.
  The HWP candidate requires actual artifact/manifest inputs and remains unapproved.
  New material on disk does not authorize deployment.

Conditional traces needing fixtures: extensionless MOTIR HWPX routing, scheduled
quality-failure/manual edit/queue-one release, corrected/restored source eligibility,
and early Blogger media/dependency ordering. B-query was rejected: the normal
producer strips URL queries before persistence.

<a id="actual-verification-and-failure-investigation"></a>

## Actual verification

### 2026-10-05 re-run at committed HEAD `db5edec`

| Check | Actual result |
| --- | --- |
| Environment | Python 3.12.10, Django 5.2.16, Celery 5.6.3, Ruff 0.16.0, pytest 9.1.1 |
| AST / inventory | 335 files parse; src 251/108,908, tests 77/40,644, scripts 7/1,105 |
| Django check / makemigrations --check --dry-run | Exit 0 / no changes |
| Fresh disposable SQLite migrate + migrate --check | Exit 0; collection 0012/0013 applied |
| Draft import HTML/spreadsheet 1.2.0 | created=2, repeat unchanged=2; full candidate root fails on unresolved HWP manifest (intended) |
| Local-content Ruff / whole Ruff | Passed / exit 1, 1,561 findings |
| Full pytest `tests/unit tests/integration` | Exit 0; 1,363 passed, 3 skipped, 7 warnings, 203 subtests; 565.72 s |
| Scratch probes outside the repository | 6 passed (R1, R4, R5, R6 evidence) |

The full suite above preceded reboot. On resume, product source was unchanged, so
only the saved/new diagnostic boundary was rerun:

| Resumed check | Result and interpretation |
| --- | --- |
| Saved probes | 9 passed, 26.67 s; assert existing defects/controls, not fixes |
| All 16 R9 production queries | 16 passed, 1.63 s; bad SQL shapes reproduced with connections forbidden |
| Candidate probes | 11 passed, 14 deselected, 23.43 s; canary IDs, schedule exception, arrays/zones/report projection, HWP faults and withdrawal controls |
| CP949/UTF-8 control | 1 passed, 25 deselected, 1.58 s; existing encoding limitation |
| Existing repaired-query/WordPress/HWP collision regressions | 19 passed, 0.38 s; their narrow repaired behavior remains valid |
| Django check / migration drift | Exit 0; no issues / no changes detected |

See the follow-up report for exact scope, synthetic faults and initial scratch-import
correction. HWP R16 ran converter/filesystem fault stubs, not Linux conversion.
The unpublish planner only queues media for the unproven prepared-binding control;
an active verified binding does not wait. No product files or tracked tests changed.

The 2026-10-03 figures below were reproduced before reboot; they are not a new full
suite after this continuation.

### 2026-10-03 verification (historical)

Use development/local and PYTHONUTF8=1. Remove LOCAL_STATE_ROOT, LOCAL_OBJECT_ROOT
and LOCAL_ARTICLE_ROOT rather than assigning empty values. Correction (2026-10-05):
the earlier text mistakenly named LOCAL_OUTPUT_ROOT; settings reads LOCAL_ARTICLE_ROOT.
The original mistaken name remains in dated reviewer evidence. Both the earlier
October 5 full run and resumed probes removed all four names, so their results stand.

| Check | Actual result |
| --- | --- |
| Environment | Python 3.12.10/uv 0.12.19; CI/setup pins uv 0.12.7, not hosted CI equivalence |
| AST | 335 Python files parse |
| Django check / makemigrations --check --dry-run | Exit 0; no issues/no drift |
| Fresh disposable SQLite migration / migrate --check | All migrations applied, none pending; includes collection 0012/0013 |
| Draft import | HTML/spreadsheet created=2; full candidate root blocked by unresolved HWP converter manifest |
| PostgreSQL query regression | 13 production expressions compile qualified locks with connections forbidden |
| Local-content Ruff / new review tests and migrations | Passed |
| Whole Ruff | Exit 1; 1,561 findings |
| First integrated repair suite | 1,319 passed, 3 skipped, 7 warnings, 203 subtests; 596.37 s |
| Second check before final fixture/SQL/link repairs | 1 failed, 1,348 passed, 3 skipped, 7 warnings, 203 subtests; 613.51 s |
| Dispatch fixture correction | 26 passed, 20 subtests; fake publication now models accepted schedule save |
| Final integrated suite | Exit 0; 1,363 passed, 3 skipped, 7 warnings, 203 subtests; 620.21 s |
| git diff --check | Passed |

After the integrated suite, formatting-only cleanup removed the 25 introduced
Ruff findings. All four affected product-file ASTs and the local-script fixture AST
are identical; 39 relevant tests pass after cleanup, and draft import repeats with
created=0 unchanged=2. No further functional changes followed the full pass.

Current Ruff groups: E501 1,065; DJ001 139; I001 122; DJ008 62; E402 56;
other selected rules 117. Total 1,561. The new review test/migration boundary and
local-content module pass; whole-repository debt is still real.

Logs under ignored .local/reviews/ supplement this durable report and the
adjudication ledger. Missing generated staticfiles accounts for current warnings.
Skipped platform cases are not verified paths. No tests were weakened to grant
publication or erase evidence; fixture preparation was corrected explicitly.

### Initial analysis history preserved

Before authorized code repairs, environment initialization succeeded but the first
analysis suite had 1,261 passed/26 failed/3 skipped, 198 subtests. Its LOCAL_STATE_ROOT
override conflicted with fixture/default-path assumptions, and .env.local was absent.
An intermediate cleanup left empty root variables and still contaminated tests.

A clean ASCII-path check had 84 passed/2 failed/2 skipped: copied Windows venv launcher
without pyvenv.cfg and a 0.4-second lock probe. Independent observation first denied
writes at 0.453 seconds and retained the helper hash. Minimal PowerShell probes
reproduced Korean-path UTF-8 script/JSON decoding failures. This supported harness
corrections, not missing production lock protection. These defects are now repaired;
the old recommendations are historical, not current failing-suite conclusions.

Initial Ruff was 1,561; initial AST 319; initial Django/drift/fresh SQLite passed.
[August acceptance](acceptance/2026-08-28-live-housing-acceptance.md) records
1,276 passed/5 skipped/198 subtests and source/Brave evidence. Its date/scope stay
historical; it was not re-executed.

No actual PostgreSQL locking/triggers, Redis/Celery delivery, S3 integration,
publisher canary/pilot, live source/humanizer/browser acceptance, OCR corpus,
Linux HWP/signed artifacts, dependency vulnerability audit, scale benchmark,
coverage measurement or hosted CI occurred. Source/compiler/SQLite/mock evidence
cannot establish production safety, exactly-once behavior or specification targets.

## Next work and handoff

Execute the [English plan](superpowers/plans/2026-10-03-critical-review-remediation.md)
in dependency order: PostgreSQL invariants, withdrawal console, approved visual
rendering, staged Blogger cohort, fresh immutable release approval and real-stack
acceptance. T022/T026/T031/T032 and other unchecked task conditions remain authoritative.

[AGENTS.md](../AGENTS.md) registers canonical reports and the read-before/update-after
workflow; [CLAUDE.md](../CLAUDE.md) bridges to it. Machine entry points refer to
C:/Users/강지혜/.agents/analysis-policy.md. File creation does not prove an already
running agent reloaded instructions. No remote commit/push or other chat messaging.

Engineering questions: supported material compatibility period, deployment/acceptance
owner, scale and invariant/test maintenance. Product questions live once in the
[service report](service-analysis.md#open-product-questions).

| Date | Update |
| --- | --- |
| 2026-10-03, initial analysis | Architecture/inventory/verification and isolated environment/Windows failure evidence; shared policy established |
| 2026-10-03, two-round update | Six critical reviewers, main revalidation and repairs, forward migrations/new release drafts, regression evidence and concrete continuation plan; historic evidence preserved |
| 2026-10-05, documentation/follow-up review | Baseline moved to committed `db5edec`; full checks reproduced; document-to-code discrepancies recorded; R1–R8 added from review of the repair commit and probes |
| 2026-10-05, resumed synthesis | All 16 remaining lock expressions compiled; R9–R16 and R1/R7/R13 updates incorporated; earlier broad repair claims and environment guidance narrowed; no product changes |
| 2026-10-05, policy funding | Added and tested funding module/shared metadata reader/UI, repaired independent review findings, retained source/coverage limits and updated inventory; acceptance contains final test/live/browser evidence |
| 2026-10-06, funding explanations | Added six conversational answers and twelve manually reviewed claim groups; reproduced/repaired direct-period extraction, ran 63 focused tests and fresh live/browser verification; no metadata/model/runtime interface change |
| 2026-10-08, topic portal | Added separate workspaces, shared responsive templates, secure topic data projections and new funding pages; repaired two scoped review findings; final tests and outstanding Chrome acceptance live in portal ledger |
| 2026-10-08–09, monthly update | Added strict KST period controls, actual housing stages/history, funding snapshot/return state and local news archive; repaired verified Windows/parser/review defects;1,689 tests pass and real bounded captures validate; older release gates and Chrome acceptance remain open |
| 2026-10-09, browser-only recovery | Sanctioned Chrome in the user-authorized fresh chat accepts root/topic desktop and responsive period/menu/overflow checks; unchanged code/data, saved captures/DOM measurements and exact limits in the browser diagnosis |
| 2026-10-10, current-state analysis | Revalidated source/inventory and actual saved counts; 468 focused tests, 16 SQL-shape and five recovery/numeric diagnostics pass; active DB retains 71 pending migrations while fresh isolated SQLite applies all 71; whole Ruff remains 1,561 findings; documentation-only changes and no new full-suite/external/browser acceptance |
