# Critical Review Remediation and Code Modification Plan

**Date:** 2026-10-03, Asia/Seoul.
**Source:** main at 7eb33310665dfffafa15561d66d6d1a2d4b48e6b plus this session's
uncommitted repairs. Dependencies and task checkboxes unchanged.

**Goal:** Preserve the verified repairs from two review rounds, complete the remaining
publication features, and obtain actual backend/channel/corpus acceptance before
distributed release. The two requested review-and-fix rounds are complete. Unchecked
tasks below are continuation work, not claims that their acceptance already exists.

**Architecture:** Retain the Django modular monolith and separate local/distributed
runtimes. Extend existing immutable snapshots, approval heads, replay, generation,
media and outbox boundaries. Local mode keeps external publishing disabled.
Python 3.12, locked Django/Celery/HTTPX, PostgreSQL/SQLite, Redis, S3,
WordPress/Blogger, PowerShell and current JavaScript tooling remain the stack.

**Authorities:** [review/adjudication](../../analysis/2026-10-03-two-round-review.md),
[code analysis](../../code-analysis.md), [service analysis](../../service-analysis.md),
[task/dependency ledger](../../../specs/001-automated-content-publishing/tasks.md),
[closed admin API](../../../specs/001-automated-content-publishing/contracts/admin-api.openapi.yaml),
[publisher contract](../../../specs/001-automated-content-publishing/contracts/publisher-adapter.md).

## Execution constraints

- Read canonical reports and current diff first; reproduce an accepted defect before
  modifying its owner. Use meaningful negative/positive regressions and record actual
  commands. Reuse existing tests before creating overlapping suites.
- Preserve historical snapshot/decision/report hashes and unrelated changes. Changed
  executable material needs a new immutable release and ordinary approval.
- Preserve run-first/sorted locks, current heads, request replay before mutable gates,
  exact target/dependency identity, audit/outbox atomicity and filesystem ownership.
- No automatic production publication, credentials in reports, commit/push or task
  acceptance claims. Existing HWP admission remains hard-false and golden-unapproved
  until the signed-release implementation and evidence exist.
- SQLite, compiler, fake-DOM and mocked HTTP evidence remain distinct from PostgreSQL
  races, a live browser, actual remote state and supported corpus acceptance.
- Do not repeat the two formal review rounds as an unrequested third round.
  Supporting reviewer clarification is already reflected in the main adjudication.

## Completed code modifications

All numbered observations are mapped in the review ledger. This table groups owning
boundaries; duplicate reviewer IDs do not represent additional independent defects.

| Completed boundary | Findings | Owning files and retained regression |
| --- | --- | --- |
| File and CLI ownership | A1, A7/A8, A9/F2 | S3 storage; CI/acceptance filename discovery; HWP wrapper. Collision/substitution, Unicode and option-boundary tests, owned cleanup. |
| Windows and runtime | A2/A5/A11, A3 | Local-script fixture venv/UTF-8/lock synchronization; Compose distributed mode. Default-root Unicode fixture checks. |
| Collection input and settlement | A6/F1, B6/B7, E1 | Local API count validation; SourceItem optional attachments/migration 0012; plain-string change event; current-delivery start/result/failure fence. |
| Actual extractor output | B1/B2/B3/B8, E9 | HTML structural locators; deterministic XLSX types; closed locators; cumulative CSV cells and skipped blank records. Real adapter output tests. |
| Frozen evidence and quality | B4/B5/B9, E2–E5 | Raw source_record pointer; unit-aware numeric citation checks; one derived rights envelope preserving input restrictions. Real fanout/snapshot and materializer tests. |
| Console and public contracts | C1/C2/C4/C10, D7/F3/F7 | Cron escaping, edit/create topic boundary, optional MFA, target casing/nullability, evidence sourceUrl links. Production JS harness and compiled response schema. |
| Approval and validation | C5/C6/C11, D1/F4/F5 | Correct proof scope/audit replay; shared and narrowly compatible preview hashes; truthful stage/result reports with bounded version/digest verification. |
| Dispatch and recovery | C3/C7/C8/C13, D3–D6/D8 | Isolated audited held schedule ticks, future-time persistence, proof-required mapping reuse, request-bound preflight, persisted uncertain delete result. |
| Credential revocation | A4/C9, D2/D9 | Exact WordPress deletion identity/Core response; pending mutation and original-subject fence; guards immediately before WordPress/Blogger writes. |
| Acceptance composition | A10/F8 | Current-run artifact binding and all eligible same-plan history documents; latest eligible passing report selection. |
| PostgreSQL compatibility | E6/E7 | Forward migration 0013 retains INSERT lineage and one-way retention UPDATE/DELETE guards. Qualified locks in evidence/editorial/corrections; mutable article head retained. Thirteen real compiler checks, no PostgreSQL execution. |
| Release candidates | E8 | Separate HTML/spreadsheet/HWP 1.2.0 draft candidates; editorial policies 1.1.0. No old profile JSON/approval mutation or new approval. |

New regression modules are tests/unit/test_review_*.py, console behavior modules
and tests/js/*.cjs. Existing S3, local script, operation contract, media binding,
schedule material and dispatch idempotency tests were extended. Exact evidence and
counts remain in the [review report](../../analysis/2026-10-03-two-round-review.md).

## Task 1 — P0 for distributed release: execute PostgreSQL invariants

**Code already repaired:** evidence/services.py and tasks.py, editorial/services.py,
tasks.py and corrections.py, and collection migration 0013.
**Create:** tests/integration/test_postgresql_lock_contracts.py.
**Extend:** existing extraction-generation, editorial-publication-eligibility,
correction and retention regressions; use their existing frozen fixture builders.

- [x] Compile thirteen production query expressions with the real PostgreSQL backend,
  forbidding connections. Optional joins remain, locks qualify owned tables.
- [x] Preserve explicit run → step → aggregate → child/evidence ordering; correction
  locks both case and article because current_revision_id is mutable.
- [ ] Execute aggregation, generic ready/finalization, manual validation and failure,
  generation replay, publishability/visual checks and correction decisions in actual
  PostgreSQL transactions with optional relations both absent and present.
- [ ] Use two connections for stale deliveries, changed article heads, concurrent
  evidence review and terminal replay. Assert current projections, audit/outbox
  identity and lock ordering; do not drop guards to avoid an execution failure.
- [ ] Populate collection schema at 0012, migrate to 0013, prove exactly one legitimate
  tombstone succeeds and repeat/identity/hash/status mutation, DELETE and invalid
  supersedes INSERT fail. Exercise deployment/runtime roles and rollback behavior.

**Command:** In a verified disposable distributed test environment with PostgreSQL
DATABASE_URL, run .venv/Scripts/python.exe -m pytest -q
tests/integration/test_postgresql_lock_contracts.py. The suite must assert
connection.vendor == "postgresql"; a skipped SQLite run cannot close this task.
Avoid the local fixture's trigger suspension during the operations being tested.

**Acceptance:** Actual populated forward migration, role/trigger/transaction and
two-connection results saved in docs/acceptance/. Compiler output alone is insufficient.

## Task 2 — P1: finish renderless withdrawal review (C12/F6)

**Modify:** src/static/admin_console/publishing_article.js, its publishing templates,
src/apps/publishing/api.py/services.py and the closed admin API schema.
**Extend:** tests/js/publishing_console_probe.cjs,
tests/unit/test_publishing_console_behavior.py, test_publishing_admin_contract.py,
test_publication_approval_contract.py and correction tests.

**Interface decision:** Add a typed server-derived targetReviewSubjects projection
to CurrentPublicationIntentResult, separate from immutable intent hash material.
Reuse UnpublishApprovalSubject: kind/action, target/snapshot/config, remotePostId,
observedRemoteState, reason, affectedTargetIds and correctionEvidenceManifestHash.
Construct it from the accepted correction and current verified remote projection;
do not trust client-invented hashes or scrape a missing preview.

- [ ] Branch review/loading by resolvedAction: content actions require valid previews;
  unpublish displays its exact remote-state subject. A withdrawal-only or mixed cohort
  must not fail initialization because it intentionally has no content render.
- [ ] Approving withdrawal obtains an unpublish proof; revocation retains
  approval_revoke. Keep null articleChannelRenderId, both head CAS fields, current
  permission and exact replay rules. Expired/wrong-scope proof or stale remote/
  correction material remains rejected.
- [ ] Add verified-correction preparation through the existing prepare-publication API,
  using prepare_verified_correction rather than a parallel withdrawal implementation.
- [x] F7 source links now use safeHttpUrl(evidence.sourceUrl); actual JS verifies safe
  links and rejects unsafe schemes with protected link attributes.
- [ ] Test actual JS withdrawal-only/mixed/content cases and failed/cancelled proofs;
  then authenticated browser review → approval → dispatch with mocked publisher I/O.

**Command:** .venv/Scripts/python.exe -m pytest -q
tests/unit/test_publishing_console_behavior.py tests/unit/test_publishing_admin_contract.py
tests/unit/test_publication_approval_contract.py.

**Acceptance:** The console completes a legitimate renderless withdrawal without
invented content or weakened approval, and normal content previews still fail closed.
T026 dependencies/browser acceptance remain authoritative.

## Task 3 — P1: bind approved visual placements into final content (C14)

**Modify:** src/apps/publishing/services.py/contracts.py and WordPress/Blogger clients.
**Create:** src/apps/publishing/rendering.py for pure placement resolution and
tests/unit/test_publication_visual_rendering.py.
**Extend:** test_published_asset_snapshots.py, test_publication_media_bindings.py,
test_wordpress_media_delivery.py and test_publication_approval_service.py.

- [ ] _create_preview_render freezes stable block/order/asset/presentation slots from
  approved placements; escape alt, caption and attribution and retain correction/
  source material. Reject missing, duplicate, foreign or unapproved slots.
- [ ] _final_render resolves only approved slots from _ready_publication_media_manifest,
  with exact delivery identity/checksum/presentation. Include resolved HTML in final
  content hash and transport marker; retain canonical WordPress URL rules.
- [ ] Define WordPress featured placement and send its exact approved featured_media.
  Both channel payloads must contain the authorized body visuals, not unused DTO data.
- [ ] Existing approved visual previews without slots need a superseding intent and
  reapproval. Never silently modify the meaning of an existing template hash.
- [ ] Regress real preview → approval → ready mapping → final render → exact adapter
  payload; cover multiple placements, escaped presentation, replay and tampering.

**Command:** Run the new visual-rendering module plus existing media-bindings,
WordPress-media-delivery and publication-approval-service modules.

**Acceptance:** Every approved visual appears once at its approved position, with
bound rights/presentation/delivery/hash. Actual remote visual proof closes T022
acceptance; source traces alone do not.

## Task 4 — P1 investigation: prove Blogger media/dependency ordering

**Modify only after reproduction:** publishing/services.py/tasks.py.
**Extend:** test_publication_dependency.py, test_media_delivery_operations.py and
test_review_publishing_regressions.py.
**Create:** tests/integration/test_publishing_cohort.py.

- [ ] First stage pending/future WordPress and dependent Blogger with media. Reproduce
  whether premature media jobs exhaust retries or release marks the cohort stale.
  If not reproduced, record counterevidence and retain existing safe ordering.
- [ ] If confirmed, keep prepared binding identities at dispatch but release media
  only after the exact frozen canonical attempt succeeds. A proposed
  _release_dependent_media_or_publication_locked verifies dependency, plans media,
  then queues publication after readiness. Preserve all pre-I/O/current guards.
- [ ] Exercise canonical/media completion in both orders, callbacks repeated,
  accepted future date, canonical failure/reconcile and removed mappings.
  Never consume retry budgets simply for an ineligible future dependency.

**Acceptance:** Exact canonical success plus ready media are required; Blogger
never publishes early or follows a different WordPress attempt. No hypothesis is
promoted to a defect without the first staged regression.

## Task 5 — P0 for rollout: approve new immutable release material (E8)

**Files:** config/review-release-2026-10-03/extraction-profiles/,
canonical extraction release manifests, both editorial policies, source registry
configuration/snapshots and the release runbook.

- [x] HTML/spreadsheet candidates declare 1.2.0; both editorial policy versions are
  1.1.0. Old profiles and decisions remain unchanged.
- [x] Disposable SQLite imports the two non-HWP candidates as drafts (created=2).
  Full-root import correctly fails on the unresolved HWP converter manifest.
- [ ] Freeze final deployment bytes, run actual supported adapter corpora, publish
  immutable material manifests and promote the new release directory into normal
  runtime discovery. Keep extractor_version aligned with actual adapter outputs.
- [ ] Create fresh source-definition/registry snapshots for changed implementation
  hashes and resolve new editorial snapshots. Test old key/version reuse rejection,
  old execution invalidation and coexistence without rewriting accepted hashes.
- [ ] Drain or explicitly settle old in-flight work; obtain ordinary verification and
  scoped approval of new material. Create new server-built validations/activations
  before re-enabling schedules. File creation is not approval.
- [ ] Prepare an environment with exact-version storage and artifact paths before
  import_extraction_profiles/verify_extraction_profile_files --root execution.
  Do not use the full candidate-root command on this PC as if it already has HWP
  artifacts. Stage HTML/spreadsheet separately; retain precise report hashes.

**Acceptance:** New releases are discoverable, independently verified and approved;
new runs/schedules use them, old history remains intact. Additional future byte
changes need unused immutable versions once a candidate is imported/approved.

## Task 6 — P0 for release: real stack, channels, corpus and HWP admission

**Files:** integration/browser tests, docs/acceptance/ and T031/T032 runbooks.

- [ ] Exercise PostgreSQL + Redis/Celery + exact-version S3 with worker reclaim,
  delayed collection settlement, audit/outbox terminal callbacks, stopped runs,
  held schedule recovery, retention and restored media references.
- [ ] On isolated authorized targets, run WordPress/Blogger canary and approved pilot,
  exact cleanup IDs, anonymous reads and body/asset hashes. Cover credential rotation,
  future media dispatch, uncertain DELETE and actual exact-object absence proof.
- [ ] Run supported housing/semiconductor source and HTML/spreadsheet/PDF/HWPX/OCR
  corpora, calibrated confidence and restrictive attachment rights. Local attachment-
  only facts and semantic assurance require the product decisions below.
- [ ] Implement bounded acceptance-byte fetch/hash/schema/subject/OCI/converter/
  all-results checks and independent signed-release trust validation before HWP can
  activate. Require hermetic build/SBOM/corpus/failure/tamper evidence.
  The 1.2.0 HWP candidate stays golden-false; rebuilding/signing needs new valid
  pinned artifact references, not flipping an old draft or fabricating acceptance.

**Acceptance:** Save actual SC-001–SC-012, channel/corpus/operations outcomes and
limits. Unchecked T031/T032 and prerequisite task acceptance govern completion.

## Task 7 — P2: resolve conditional traces and maintenance debt

- [ ] Reproduce extensionless MOTIR HWPX download/routing with actual filename/type
  metadata; keep legacy HWP admission independent.
- [ ] Stage quality-failed scheduled drafts through manual edit and queue-one release;
  stage corrected/restored source lineage through current editorial eligibility.
  Reject unsupported hypotheses with recorded counterevidence.
- [ ] Triage current Ruff debt by rule/owner; prioritize introduced diagnostics without
  broad schema churn. Decompose publishing/evidence services only after backend
  invariants are executable. Every release-byte change participates in versioning.
- [ ] Retain per-file sampling limits. Fully read affected unread DDL/test bodies during
  implementation; inventory/AST is not a fresh exhaustive semantic audit.

## Verification and handoff contract

- Local commands use development/local, PYTHONUTF8=1 and removed inherited root
  overrides; empty LOCAL_* values are not equivalent to absence.
- Run Django check and makemigrations --check --dry-run; migrate only disposable
  state. Run pytest -q tests/unit tests/integration with default local roots.
- Check local-content and new-test/migration Ruff, whole Ruff with actual findings,
  Python parsing, Markdown links and git diff --check. Do not call whole lint green.
- Current checks: 335 Python files parse; Django/drift/applied-migration checks pass;
  new review tests/migrations and local-content lint pass. Whole Ruff reports 1,561
  findings. Integrated pytest: 1,363 passed, 3 skipped, 7 warnings, 203 subtests in 620.21 s.
  Formatting-only cleanup keeps product ASTs identical; 39 relevant tests pass
  afterward. No further functional code changes; final ledger retains both steps.
- Initial review suite: 1,319 passed; second: 1,348 passed with one missing-save
  fixture failure, corrected in a 26-test/20-subtest recheck. These are dated steps,
  not a guessed final pass. No actual distributed or remote acceptance in this session.
- Update the same canonical reports and AGENTS paths after subsequent analysis.
  Preserve historical August evidence, all six reviewer reports and task authority.

## Product decisions with one canonical home

See [service questions](../../service-analysis.md#open-product-questions) for supported
runtime, local attachment facts, required factual/semantic assurance, withdrawal UX,
acceptance-signing ownership, scale and retention. Tasks above define engineering
continuation without claiming those product decisions were made.
