# Wisdome Super Writer code analysis

## Baseline and conclusion

- Updated: 2026-10-03, Asia/Seoul, after the requested two-round critical review.
- Source: main at 7eb33310665dfffafa15561d66d6d1a2d4b48e6b
  (2026-08-30, fix: bind local web service to port 7667), plus uncommitted repairs.
- Initial analysis started with a clean tree. This review began with existing README
  links, AGENTS/CLAUDE bridges and these two reports; those changes were preserved.
- Scope: all major product domains, migrations, adapters, configuration, local tooling,
  deployment/CI, console/contracts and relevant tests. Exact semantic-read/sampling
  boundaries are in the [six reviewer reports](analysis/2026-10-03-two-round-review.md#reviewer-evidence-and-coverage).
- Companion [service analysis](service-analysis.md), [adjudication ledger](analysis/2026-10-03-two-round-review.md),
  and [English modification plan](superpowers/plans/2026-10-03-critical-review-remediation.md).

The code is a substantial Django modular monolith with asynchronous distributed work
and a separate Windows local housing writer. Two groups of three independent critical
reviewers found real producer/consumer, ownership, generation, approval, media,
scheduling and reporting defects. Main independently reproduced and repaired them.
The principal remaining release risks are actual PostgreSQL/distributed acceptance,
unfinished withdrawal/visual integration, fresh release approval and closed HWP
admission. A passing local suite cannot resolve these release gates.

## Measured inventory

Counts exclude virtual environments/generated output. Lines include blank lines,
comments and migrations; counts measure review surface rather than complexity.

| Item | Current working-tree value |
| --- | --- |
| Python source | 251 files; 108,908 lines under src/ |
| Python tests | 77 files; 40,644 lines including conftest.py |
| Python scripts | 7 files; 1,105 lines |
| Parsed Python | 335 files under src/, scripts/ and tests/ |
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
| apps/accounts, topics, audit | Scoped proofs, immutable source/policy registry heads, append-only audit and retention |
| apps/collection | Runs/steps, source versions/status lineage, collection delivery settlement, stop/retry |
| apps/evidence | Profile release verification, extraction generations, object reservations and immutable evidence |
| apps/editorial | Clustering/verification, claims/citations, revisions, quality and correction decisions |
| apps/publishing | Credentials, target snapshots, canaries, validation/activation, previews, approval, dispatch/media/reconcile |
| apps/scheduling | CAS/frozen tick material, due scans, queue-one/coalescing and kill switch |
| apps/local_content | Official HTML/API reconciliation, selection/rendering, humanizer, graphics, immutable bundles/preview |
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

## Current findings and priorities

| ID | Priority | Current finding/status | Required next work |
| --- | --- | --- | --- |
| C01 | P0 for supported HWP release | Legacy activation deliberately returns false; old and candidate profiles remain golden-unapproved | Implement independently signed acceptance-byte/trust validation and actual converter/corpus evidence before admission |
| C02 | P0 for distributed release | Local/compiler/mocked checks do not execute PostgreSQL, broker/storage or real publishers | Execute plan Tasks 1 and 6; honor T031/T032 and prerequisite gates |
| C03 | P1 | Large publishing/evidence orchestration modules mix many transaction/invariant boundaries | Decompose only after real backend regressions protect lock/replay behavior |
| C04 | P1 | E6/E7 PostgreSQL compatibility repairs present but unexecuted against PostgreSQL | Populated forward migration, role/trigger and two-connection transaction tests |
| C05 | P1 | Whole Ruff still fails: 1,561 findings, initial 1,561 | Introduced diagnostics removed; triage pre-existing debt without claiming repository lint passes |
| C06 | Resolved locally | Invalid Windows fixture launcher, UTF-8 assumptions and fixed startup delay | Valid venv/BOM/explicit encoding/observable lock now tested; preserve platform skips |
| C07 | P1 | Historical root handoff/task evidence predates current runtime and repairs | Use current canonical reports; preserve dated task/acceptance authority |
| C08 | P1 | Distributed readiness checks DB/broker/outbox, not every worker/storage/publisher | Define/exercise dependency and alerting acceptance |
| C09 | P2 | Preview repeatedly validates/re-hashes growing run/article histories | Benchmark before adding integrity-preserving caching |
| C10 | P2 | Test fixture replaces TransactionTestCase teardown and manages SQLite triggers | Review cleanup/version coupling; real PostgreSQL tests must not suspend tested guards |
| C11 | P1 | C12/F6 console has no action-specific renderless withdrawal review | Server-derived subject, scoped proof, CAS and authenticated console journey |
| C12 | P1 | C14 approved media lacks final body placement | Bind approved slots/delivery URLs/final hash and channel payload; T022 acceptance |
| C13 | P0 for rollout | Repaired bytes invalidate old immutable source/profile/policy approval | Fresh releases/snapshots/verification and ordinary approval; do not rewrite old hashes |

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
- HTML pointers resolve original nodes; native spreadsheet values serialize;
  locator schema/cumulative cells/blank rows are coherent.
- Raw records freeze a resolvable source_record envelope. Child rights/review
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
  per-I/O guards run on both adapters. WordPress self-revocation accepts only an
  exact identity-bound Core result.
- Ineligible due material produces an audited hold and does not starve another tick.
  Console PATCH/MFA/source links and target/validation report contracts are repaired;
  report bytes are bounded, exact-version and digest verified without fabricated data.
- E7 qualifies owned-row locks, preserving explicit run-first order and correction's
  mutable article-head lock. E6 has a forward INSERT-only lineage guard while the
  retention one-way UPDATE/DELETE guard remains. PostgreSQL execution unproved.
- Two non-HWP 1.2.0 candidates import as drafts; editorial policies are 1.1.0.
  The HWP candidate requires actual artifact/manifest inputs and remains unapproved.
  New material on disk does not authorize deployment.

Conditional traces needing fixtures: extensionless MOTIR HWPX routing, scheduled
quality-failure/manual edit/queue-one release, corrected/restored source eligibility,
and early Blogger media/dependency ordering. B-query was rejected: the normal
producer strips URL queries before persistence.

## Actual verification

Use development/local and PYTHONUTF8=1. Pytest removes LOCAL_STATE_ROOT,
LOCAL_OBJECT_ROOT and LOCAL_OUTPUT_ROOT from the environment instead of assigning
empty values; default roots and Unicode temporary paths are exercised.

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
