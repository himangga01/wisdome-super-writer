# Round 2 pipeline reliability and provenance review

## Baseline and scope

- Date: 2026-10-03, Asia/Seoul.
- Reviewer: E, fresh second-round reviewer. Main session owns implementation,
  integration checks, canonical report updates and the AGENTS.md registration.
- Source: `main`, `7eb33310665dfffafa15561d66d6d1a2d4b48e6b`.
- Initial working tree was already modified by the first repair pass: HTML and
  spreadsheet adapters, SourceItem optional attachments and migration 0012,
  collection event serialization, publishing/storage/runtime/console/acceptance
  and Windows fixture repairs. Existing analysis/instruction files were untracked.
  This reviewer changed no product code, tests, canonical reports or instructions.
- Starting references: [project instructions](../../AGENTS.md), shared
  `C:/Users/강지혜/.agents/analysis-policy.md`, [code analysis](../code-analysis.md),
  [service analysis](../service-analysis.md), [two-round ledger](2026-10-03-two-round-review.md),
  and [round-one pipeline review](review-round1-pipeline-integrity.md).
- Scope: topic/source release identity, collection result settlement, evidence
  rights/locators/materialization, editorial grounding, generation/terminal guards,
  and complete text review of all 25 baseline topic/collection/evidence/editorial
  migrations. The new collection migration 0012 was also read.
- Permitted writes: this report and ignored diagnostics under
  `.local/reviews/round2-pipeline-reliability/`. No credentials, shared database,
  network writes, service startup or port-binding tests were used.

The service collects official housing and semiconductor records, freezes source
and extraction releases, stores evidence and observations, then creates Korean
drafts with claim/quality bindings. Collection, extraction and editorial work are
linked through a transactional outbox. Extraction uses generation/lease fences
and object-write reservations; editorial work freezes policy, source text and claim
material. This review evaluates those implemented boundaries. It is not new live
source acceptance, OCR/HWP acceptance, publication or distributed deployment.

## Findings

| ID | Priority | Baseline or first-pass change | Result |
| --- | --- | --- | --- |
| E1 | P1 | Baseline conditional round-one trace | Old collection response overwrites newer terminal failure; reproduced now without masking B7 |
| E2 | P1 | Baseline B4 | Real raw fanout becomes publishable evidence but fails the editorial locator gate |
| E3 | P1 | Baseline B5 | Contradictory price statement with genuine source span passes all ten gates |
| E4 | P1 | Baseline B9 | Both derived constructors still duplicate `manual_review_required` |
| E5 | P1 | Baseline conditional rights trace | Record-scope child rights broaden attachment rights once E4 is removed |
| E6 | P1 | Baseline, newly identified | PostgreSQL old append-only trigger defeats the retention tombstone exception |
| E7 | P1 | Baseline, newly identified | Nullable joined rows receive unqualified `FOR UPDATE` in PostgreSQL queries |
| E8 | P1 before release | Introduced by accepted first-pass byte changes | Same profile versions now resolve different material; old approvals cannot authorize it |
| E9 | P2 | Introduced by cumulative CSV-budget repair | Zero-cell blank rows allocate unlimited records under the cell budget |

### E1: collection success does not fence the returning delivery

- Location: `src/apps/collection/services.py:1991`, success transaction at
  `2180`–`2288`, especially the succeeded-only check at `2197`–`2209` and state
  assignment at `2220`. Failure settlement is at `2448`–`2516`; immutable
  observation creation is at `1649`–`1721`. The task passes only consumer attempt
  number at `src/apps/collection/tasks.py:62`–`67`.
- Trigger: delivery 1 starts adapter work; delivery 2 terminalizes the attempt as
  failed while delivery 1 is outside a transaction; delivery 1 returns a valid
  fresh record while the run is still collecting.
- Expected: a returning stale response cannot replace the terminal outcome or
  create new observations/change events after another delivery settled it.
- Actual: the real collector overwrites `failed/newer_terminal_failure` with
  `succeeded`, persists one RunSourceItem, and emits one `source.item_changed`.
  Immutable observations remain delivery 1 `retry_scheduled` and delivery 2
  `failed`; neither records that supposedly successful attempt projection.
- Evidence: `pipeline_repro.py` and `pipeline-repro.json`; fresh, migrated private
  in-memory SQLite, real collector/persistence/failure services, fake adapter only.
  No enum/persistence wrapper or product patch was needed after the B6/B7 repair.
- Counterevidence: returning success checks run state/stop and detects conflicting
  responses when the attempt is already succeeded. Those do not reject a failed,
  skipped or newly owned attempt. Outbox settlement at
  `src/wisdome_writer/infrastructure/outbox.py:1392` checks receipt ownership after
  `handler(*args)` at `1313`; it cannot roll back business work already committed
  by the handler. Full consumer lease reclaim was not executed here.
- Minimal fix: claim and compare a persisted delivery/generation identity at every
  success/retry/failure settlement. At minimum, the success path must preserve
  existing terminal failure/skipped outcomes. Maintain one lock order and couple
  business records, observations and change events in the owned transaction.
- Regression: deterministically pause the old adapter, settle a newer terminal
  failure, resume the old response, and require unchanged terminal projection,
  zero old-result side effects and consistent immutable observations. Also test
  a newer in-flight claim and stale failure; a terminal-only check does not prove
  all lease races are closed. PostgreSQL parallel delivery remains required.

### E2: raw source-record locator cannot pass the consumer contract

- Location: `src/apps/evidence/tasks.py:991`–`1053`, especially `1021`;
  `src/apps/evidence/models.py:106`–`108`;
  `src/apps/editorial/quality.py:265`–`274`;
  `_frozen_evidence_snapshot` at `src/apps/editorial/services.py:661`–`801`.
- Trigger: ordinary fresh official source record, real raw fanout, and generated
  high-impact claim. The producer stores `structured_path/record_key/body_text`.
- Expected: the exact raw body-text field has a valid, lineage-bound locator which
  editorial quality can verify without pretending that SourceItem metadata is
  the source-record envelope.
- Actual: real raw persistence and frozen editorial snapshot succeed, with
  `publishable=true`. Real template generation/normalization then passes nine
  quality gates and fails only `high_risk_verification_satisfied`. The locator
  validator rejects `record_key`. Raw EvidenceAsset.clean does not invoke the
  generic/document locator validator.
- Evidence: the private SQLite probe uses real `_create_raw_evidence` and
  `_frozen_evidence_snapshot`, improving on round one's synthetic manifest probe.
- Counterevidence: a logical SourceItem field locator is a reasonable producer
  design. The current validator lacks its explicit binding. Relabeling it as
  `json_pointer` over unrelated metadata does not establish a true reference.
- Minimal fix/regression: implement a dedicated raw field variant with source
  item, observation, extraction method and content identity checks, or freeze a
  true source-record envelope and point into it. Preserve the generic matrix and
  high-impact gate. Exercise real housing structured bodies and semiconductor
  prose through fanout, frozen snapshot and all ten gates, with wrong field,
  source item and content mismatch rejection.

### E3: presence of a genuine source span does not ground a different number

- Location: `src/apps/editorial/quality.py:191`–`207` and `350`–`374`;
  `src/apps/editorial/services.py:333`–`474` (manual binding normalization).
- Trigger: change a manual factual price statement and the matching body text
  from `분양가는 3억 원입니다.` to `분양가는 8억 원입니다.`, retaining the
  genuine `3억` source span and frozen official text.
- Expected: a contradictory high-impact fact fails grounding before it can receive
  passed quality or publication eligibility.
- Actual: real manual-binding validation and quality evaluation report ten passed
  gates for both the unchanged `3억` control and changed `8억` claim.
- Evidence: `pipeline-repro.json`. The numeric experiment uses a shape-valid
  pointer variant solely to isolate grounding from E2; it does not claim to repair
  the raw locator or prove a full publication journey.
- Counterevidence: title equality, complete block claims, correct citation markers,
  exact source entries, real source-span membership and current policy hashes are
  checked. All can hold while the factual number contradicts that genuine span.
  This is a quality-promise failure by an authorized editor, not an authorization
  bypass. Arbitrary paraphrases cannot be declared false by simple string inequality.
- Minimal fix: use deterministic statement/span agreement for supported MVP facts,
  or explicit typed value/unit/date comparison with bounded equivalent formatting.
  Do not treat mere span membership as support for an unrelated statement.
- Regression: price, date and supply-count contradiction with the valid original
  span must block quality and publication revalidation; equivalent numeric/unit
  formatting should retain a passing control.

### E4: both real derived constructors still duplicate the review keyword

- Location: `_rights` returns the key at `src/apps/evidence/tasks.py:891`;
  document constructor at `1750` and `1755`; generic constructor at `2664` and
  `2667`. Current task bodies were checked independently.
- Trigger: a new generic or document EvidenceAsset, using the real rights helper.
- Expected: one composed keyword envelope carries both extraction and rights
  review restrictions.
- Actual: Python raises duplicate-keyword TypeError before model construction.
  Direct real helper/model call and real `_make_document_evidence` both reproduce
  this. The latter patches only its existing-evidence lookup to return no record;
  it does not stub rights or constructor behavior.
- Counterevidence: existing evidence can be reused without this constructor.
  Mock rights dictionaries which omit the key do not exercise the new-record path.
  Mapping unpacking does not overwrite a separately named function keyword.
- Minimal fix/regression: compose one dictionary, preserving logical OR of parser
  manual review and rights-policy review requirements. Run real generic and
  document materialization, manifest creation and ready events with the real
  helper. Address E5 in the same composition so deduplication cannot expose broader
  rights. No successful full generic/document worker was established here.

### E5: record-scope rights do not inherit the attachment input restrictions

- Location: default `_rights` scope at `src/apps/evidence/tasks.py:823`–`892`;
  attachment scope producer at `1257`–`1261`; document/generic callers at `1699`
  and `2612`; publishability at `src/apps/evidence/services.py:1135`–`1178`;
  EvidenceAsset.clean at `src/apps/evidence/models.py:1061`–`1146`.
- Trigger: MOTIR source's record rights are `attribution_required` and publishable,
  while its downloaded document attachment rights are `internal_analysis_only`
  and manual-required. Both child callers instead use default record scope.
- Expected: child evidence preserves the most restrictive applicable input rights
  and review requirement. Extraction must not turn an internal attachment into
  publishable source evidence.
- Actual, conditional: the real helper returns attribution/manual=false for the
  child and internal/manual=true for the attachment. After diagnostic-only keyword
  deduplication, an unsaved real document EvidenceAsset over that restricted parent
  passes `.clean()` and `calculate_publishable()` returns true. Parent publishability
  is false. E4 currently prevents the actual new-record constructor from completing.
- Evidence: `conditional_parent_rights_projection` in `pipeline-repro.json`;
  no parent/child saved by this projection, no profile approval and no publication.
- Counterevidence: `_rights(..., attachment=...)` can narrow the original attachment
  rights and `_uses_audit_only_raw_input` blocks quarantined fingerprintless input.
  Those do not compare the legitimate parent's rights/manual restrictions during
  child composition or publishability.
- Minimal fix/regression: use the frozen attachment scope and exact parent rights
  envelope, take the most restrictive status, preserve required attribution/basis
  and union review restrictions with extraction restrictions. Test allowed record
  versus internal/unknown/prohibited attachment and independently restricted
  attachment metadata through real document/generic workers and publication
  eligibility. Do not merely AND the child's boolean with an arbitrary historical
  `publishable` projection without checking its provenance and reason.

### E6: PostgreSQL retention still has the old unconditional update rejection

- Location: `src/apps/collection/migrations/0007_source_item_status_lineage.py:17`
  and `37`–`40`; `0011_source_item_retention_tombstone.py:58`–`106`, especially
  `100`; intended caller `src/apps/collection/models.py:115`–`128`.
- Trigger: valid one-way `SourceItem.objects.retention_tombstone(...)` on a
  PostgreSQL database migrated through 0011.
- Expected: payload is cleared once, identity/hash/status/lineage stay unchanged;
  all other UPDATE/DELETE remains rejected and supersedes INSERT guards remain.
- Source-traced actual: 0007 installs `collection_source_item_append_only` BEFORE
  INSERT/UPDATE/DELETE and rejects every UPDATE. 0011 creates a distinct
  `collection_sourceitem_retention_guard`; its DROP removes only its own name.
  The old update-rejecting trigger therefore remains. No later assigned migration
  replaces it. A valid tombstone is still rejected by the old function.
- Evidence: real forward functions called with a capture-only PostgreSQL schema
  editor; `retention-forward-ddl.sql` and `release-and-ddl-probe.json` record both
  created names and the unmatched old trigger. SQL was not executed on PostgreSQL.
- Counterevidence: SQLite 0007 installs no source-item trigger, so 0011's exception
  can work there. The current SQLite retention test cannot establish PostgreSQL
  behavior. 0011's populated-reverse refusal correctly preserves erased-history
  limitations; it does not repair forward trigger coexistence.
- Minimal fix: add a new forward migration which changes the old trigger to
  INSERT-only lineage checking or merges lineage checking and the one-way retention
  exception under one function/trigger. Preserve immutable update/delete and
  lineage checks; do not globally disable either guard. Handle already-migrated
  databases instead of editing only historical migration text.
- Regression: populated PostgreSQL migration followed by one valid tombstone,
  repeat/identity/hash/status mutation and direct DELETE rejection, plus cross-source
  supersedes INSERT rejection. Record real SQL execution when available.

### E7: joined nullable relations are locked by unqualified PostgreSQL FOR UPDATE

- Location/evidence: the real Django PostgreSQL compiler produced the following
  query shapes, with representative exact primary-key/run filters and current
  nullable fields. `ensure_connection()` was replaced with a function which raises;
  no database connection or query execution occurred.

| Caller | Location | Remaining LEFT OUTER JOINs | Lock clause |
| --- | --- | --- | --- |
| aggregate_document_extraction | `src/apps/evidence/services.py:1410` | 6 | unqualified `FOR UPDATE` |
| consume_other_ready | `src/apps/evidence/tasks.py:3465` | 2 | same |
| finalize_run_evidence documents | `src/apps/evidence/tasks.py:3711` | 8 | same |
| finalize_run_evidence attempts | `src/apps/evidence/tasks.py:3721` | 2 | same |
| finalize_run_evidence assets | `src/apps/evidence/tasks.py:3736` | 6 | same |
| revalidate_manual_revision and failure | `src/apps/editorial/tasks.py:537`, `614` | 1 each | same |
| generation replay | `src/apps/editorial/services.py:1148` | 1 | same |
| correction decision | `src/apps/editorial/corrections.py:228` | 2 | same |

- Trigger: execute these normal paths on PostgreSQL, including legitimate non-null
  rows; schema-nullable and reverse-optional joins still compile as outer joins.
- Expected: the owned row and any explicitly required related rows are locked
  without asking PostgreSQL to lock the nullable side of an outer join.
- Actual classification: compiled SQL plus backend contract, not an executed
  production failure. Django's primary documentation explains that default
  `select_for_update` locks selected related rows and nullable joins raise
  NotSupportedError. [Django 5.2 select_for_update documentation](https://docs.djangoproject.com/en/5.2/ref/models/querysets/#select-for-update).
- Counterevidence: SQLite omits this lock clause; successful private SQLite runs
  therefore cannot refute the PostgreSQL incompatibility. Filtering a different
  joined relation to non-null does not remove the remaining nullable input,
  decision or verification joins. Related rows may still need explicit locking.
- Minimal fix: qualify the lock set, for example `of=("self",)`, and lock other
  required related rows separately in the existing canonical order. Alternatively,
  remove optional joined reads from locking queries. Do not blanket-replace every
  lock without inspecting each invariant. The compiler comparison confirms that
  `of=("self",)` emits `FOR UPDATE OF` the base table for these shapes.
- Regression: real PostgreSQL transaction tests for evidence aggregation/ready,
  manual revalidation, replay, corrections and immutable terminal behavior with
  nullable and populated relations. Compiler checks are useful early regressions
  but cannot certify races, deadlocks or execution.

### E8: changed release bytes need new artifacts and ordinary approval

- Location: `config/extraction-profiles/generic/html-deterministic-v1.1.json`,
  `spreadsheet-deterministic-v1.1.json`, `legacy-hwp-v1.json`;
  `src/apps/evidence/profiles.py:396`–`457`, `tasks.py:911`–`969`;
  importer `src/apps/evidence/management/commands/import_extraction_profiles.py:31`–`37`;
  source manifests at `src/adapters/sources/manifests.py:65`–`71`, execution check
  `src/adapters/sources/base.py:239`–`263`; editorial snapshot resolution at
  `src/apps/editorial/policies.py:298`–`343`.
- Trigger: deploy current first-pass repairs while an existing database holds old
  HTML/spreadsheet profile snapshots at key/version 1.1.0 and source snapshots v3.
- Expected: historical rows/approvals remain unchanged; new executable material
  has an independently identifiable release and verification/approval chain.
- Verified actual: against HEAD as materialized with `git cat-file --filters`
  on this Windows checkout, current HTML/spreadsheet config and version are equal
  but implementation/material hashes differ. `_verify_profile` rejects the old
  snapshots with `profile_release_mismatch`. Existing-key/version import rejects
  changed material rather than creating a new draft. Every source adapter's shared
  implementation manifest includes collection/services.py, so current fixes also
  make old source snapshots reject unavailable deployment material.
- Further source trace: the active HWP 1.1.0 profile includes compose.yaml; the
  accepted distributed-mode repair changes that material too. Editorial material
  was unchanged at the checked second-pass baseline, but repairs to E2/E3/evidence
  models change files hashed into both 1.0.0 editorial policies. Same-key/version
  resolution will then refuse reused versions.
- Counterevidence: these fail-closed guards work as designed. Recalculating old
  stored hashes would destroy their approval meaning. An initial raw Git-blob
  comparison falsely marked untouched CRLF files changed; the corrected probe
  uses checkout filters and identifies only collection/services.py for source
  manifests. Golden/corpus state was never approved by this reviewer.
- Concrete release work: create `generic/html-deterministic-v1.2.json` and
  `generic/spreadsheet-deterministic-v1.2.json` with profile_version 1.2.0, update
  manifest references and retain previous release documents/DB rows. Keep or bump
  extractor_version deliberately; any bump must match actual adapter output.
  Create `generic/legacy-hwp-v1.2.json` for changed deployment material while keeping
  `golden_corpus_approved=false` and null acceptance. Give both editorial policy
  documents a new policyVersion, e.g. 1.1.0, and meaningful changed check versions.
  Create fresh source-definition and registry snapshot versions through current
  check/approval APIs. Preserve old decision chains and live approvals; do not
  auto-approve new material or reuse old acceptance. Drain or explicitly fail old
  in-flight work, then create new runs from approved material. Verify source/profile/
  policy/auto-publish invalidation before enabling schedules again.
- Regression: old imported snapshots remain byte/hash/decision identical; old
  execution rejects; new draft imports coexist; approval requires new verification;
  actual new engine outputs match release identity; HWP stays closed.

### E9: blank delimited rows escape the early allocation budget

- Location: `src/adapters/extractors/spreadsheet.py:91`–`106`;
  later output bound `src/apps/evidence/tasks.py:2369`–`2374`.
- Trigger: delimited file containing empty physical lines. csv.reader produces
  `[]`; the repaired cumulative budget increments by zero while appending a record.
- Expected: empty rows are skipped or record/row allocation is bounded during
  parsing, independent of a cumulative nonempty-cell budget.
- Actual: four blank lines with max_cells=1 return four empty GenericEvidenceRecords
  and row_count=4. Baseline code charged at least one per row and would reject this
  input. A byte-bounded file can therefore allocate many more records than max_cells.
- Evidence: pure real adapter probe, `blank-rows.csv`; no large-memory stress test.
  The first-pass ragged input regressions still pass.
- Counterevidence: `_validate_generic_output` later rejects more than 50,000 records.
  It runs after the extractor has allocated the full result list, so it protects
  storage/persistence rather than the early parse allocation. Large-memory impact
  is a scaling inference, not a measured crash.
- Minimal fix/regression: skip zero-cell rows while preserving original physical
  row numbers in nonblank locators, or impose an early explicit max_rows/max_records
  bound. Cover all-blank CSV/TSV, blank rows before/between real data and ragged rows
  at exact cell limits. Keep cumulative cell counting for real rows.

## Independent verification and positive evidence

| Check | Actual result |
| --- | --- |
| Existing instructions/reports, baseline/status | Read; HEAD unchanged; main repairs preserved |
| pipeline_repro.py | Exit 0; fresh private SQLite migration, repaired B6/B7 normal collection, E1–E4 reproductions and conditional E5 projection |
| release_and_ddl_probe.py | Exit 0; old/current release mismatch and capture-only PostgreSQL retention DDL |
| postgresql_query_shape.py | Exit 0; eight query shapes compiled with PostgreSQL backend, connection forbidden, no SQL executed |
| Blank CSV real adapter | Exit 0; max_cells=1 admitted four empty records |
| `pytest -q tests/unit/test_review_extractor_regressions.py --basetemp=.local/reviews/round2-pipeline-reliability/pytest-extractors` | 10 passed in 0.34 s |

The narrow pytest child removed inherited LOCAL_STATE_ROOT/LOCAL_OBJECT_ROOT/
LOCAL_OUTPUT_ROOT, selected development/local mode and PYTHONUTF8=1. It runs real
HTML selector resolution, date/time/duration canonicalization, the exact CSV/TSV/
XLSX locator contract, and ragged-cell budget assertions. It starts no service.
The collector repro used empty attachments and an unwrapped real event producer;
normal collection now stores one succeeded observation and plain-string change kind.

Extraction's current completion helpers do check run state/stop, generation,
lease owner and token under run-first locks. Object-write reservation creation
also checks ownership and retains orphaned objects rather than deleting a shared
content-addressed key. Those are counterevidence to an unrestricted stale-extraction
result claim; E1 is specifically the collection settlement boundary. No new generic
stale-extraction defect was demonstrated here.

The 25 baseline migration bodies were read, including forward/backfill/reverse
logic, PostgreSQL/SQLite differences and operation ordering. Notable safeguards:

- Topics 0002 checks ambiguous legacy membership/approved registry multiplicity;
  0003 binds original config separately from frozen legacy execution material.
- Collection 0004/0005 restrict legacy recovery to exact envelopes/counters and
  recognize active delivery/stop boundaries; 0008 gives old runs a non-runtime
  policy sentinel. 0009 requires inactive legacy deliveries and binds generation.
- Evidence 0002/0003 retain canonical input fingerprints and quarantine duplicate
  raw/document derivations. 0004 requires complete report envelopes. 0005 requires
  legacy evidence/ready identity proof, drains active delivery and installs parent
  generation/terminal guards. 0006 proves complete generic manifests, fails
  unprovable rows closed and strengthens the terminal manifest guard.
- Editorial 0002 protects verification/membership identity; 0003 explicitly
  quarantines legacy material, blocks active runs and freezes completed claim/
  quality children; 0004 serializes correction head extension with PostgreSQL
  FOR UPDATE and rejects populated reversal.
- Collection 0011 blocks reversing erased payload; 0012 changes optional-attachment
  validation metadata and does not add a data backfill or broaden runtime rights.

This is source inspection, not a populated upgrade/reverse acceptance run. Fresh
SQLite migration executes empty backfills. Existing topic snapshot/mutation/decision
immutability also has ORM save/delete guards; the four topic migrations do not
install the equivalent append-only database triggers. That is an explicit guard
coverage limit, not a demonstrated normal-flow mutation or a generic DB threat claim.

## Coverage and limits

The appendix records this review's per-file coverage. `F` means current full text
read; `M` full migration body/DDL/backfill/reverse read; `P` targeted bodies or
callers/shape inspected; `N` inventory only, not reread this round. Earlier round-one
full-file reading remains historical and is not relabeled as this reviewer's work.
APIs/admin/URLs, long housing/semiconductor parser bodies and unrelated extractor
implementations were not exhaustively reread. Main/other reviewers own complementary
coverage and the integrated full suite.

No PostgreSQL server, concurrent transaction stress, Redis/Celery reclaim, real S3,
live source adapter, publisher, actual OCR corpus, HWP sandbox/container or fresh
live acceptance was executed. SQL compilation does not prove runtime locking or
migration execution. HWP admission still hard-returns false at
`src/adapters/extractors/legacy_hwp.py:209`–`217`, and shipped config remains
golden-unapproved. No task checkbox or production readiness claim follows from
these probes.

Open decisions: supported deterministic factual paraphrase rules; intended raw
source-record locator representation; ownership/approval of new release material;
PostgreSQL acceptance environment and migration quiescence procedure; rights
restrictions that remain sticky through multiple derivation levels; handling of
old queued work and schedules after a release change.

## Update log

- 2026-10-03: canonical references and repaired working baseline read; independently
  reproduced stale collection/raw locator/contradictory facts/duplicate constructors;
  fully reviewed 25 baseline migrations plus new 0012; identified retention trigger
  coexistence, nullable PostgreSQL locking queries and CSV blank-row regression;
  verified release-version implications and preserved explicit execution limits.

## Per-file coverage appendix

<!-- Generated appendix below; reviewed status definitions above remain authoritative. -->

Recorded file counts: F=23, M=26 (25 baseline + new 0012), P=20, N=55.

```text
P config/editorial-policies/housing_subscription.json (22 lines)
P config/editorial-policies/semiconductor_news.json (22 lines)
F config/extraction-profiles/generic/html-deterministic-v1.1.json (7 lines)
F config/extraction-profiles/generic/legacy-hwp-v1.json (49 lines)
F config/extraction-profiles/generic/native-pdf-v1.1.json (14 lines)
F config/extraction-profiles/generic/spreadsheet-deterministic-v1.1.json (7 lines)
F config/extraction-profiles/manifest.json (14 lines)
F config/extraction-profiles/paddleocr/paddle-ko-v1.1.json (25 lines)
P config/source-registry/housing_subscription.json (223 lines)
P config/source-registry/semiconductor_news.json (184 lines)
N src/adapters/extractors/__init__.py (4 lines)
F src/adapters/extractors/base.py (195 lines)
N src/adapters/extractors/browser_capture.py (55 lines)
F src/adapters/extractors/html.py (89 lines)
N src/adapters/extractors/hwpx.py (753 lines)
P src/adapters/extractors/legacy_hwp.py (684 lines)
N src/adapters/extractors/media.py (102 lines)
N src/adapters/extractors/native_pdf/__init__.py (4 lines)
N src/adapters/extractors/native_pdf/adapter.py (233 lines)
N src/adapters/extractors/paddleocr/__init__.py (4 lines)
N src/adapters/extractors/paddleocr/adapter.py (609 lines)
F src/adapters/extractors/spreadsheet.py (121 lines)
N src/adapters/extractors/structured.py (231 lines)
N src/adapters/generators/__init__.py (3 lines)
F src/adapters/generators/base.py (54 lines)
F src/adapters/generators/template.py (146 lines)
N src/adapters/sources/__init__.py (25 lines)
F src/adapters/sources/base.py (368 lines)
F src/adapters/sources/errors.py (68 lines)
N src/adapters/sources/housing/__init__.py (5 lines)
N src/adapters/sources/housing/applyhome.py (475 lines)
N src/adapters/sources/housing/common.py (857 lines)
N src/adapters/sources/housing/lh.py (692 lines)
N src/adapters/sources/http.py (2104 lines)
F src/adapters/sources/manifests.py (185 lines)
N src/adapters/sources/semiconductor/__init__.py (24 lines)
N src/adapters/sources/semiconductor/adapters.py (2274 lines)
N src/adapters/sources/semiconductor/common.py (830 lines)
N src/apps/collection/__init__.py (1 lines)
N src/apps/collection/admin.py (35 lines)
N src/apps/collection/api.py (435 lines)
N src/apps/collection/apps.py (7 lines)
M src/apps/collection/migrations/0001_initial.py (130 lines)
M src/apps/collection/migrations/0002_runstep_fanout_completed_at.py (15 lines)
M src/apps/collection/migrations/0003_recover_inflight_evidence_fanout.py (208 lines)
M src/apps/collection/migrations/0004_rearm_original_evidence_parent.py (721 lines)
M src/apps/collection/migrations/0005_converge_legacy_terminal_recovery.py (589 lines)
M src/apps/collection/migrations/0006_collection_observability.py (277 lines)
M src/apps/collection/migrations/0007_source_item_status_lineage.py (287 lines)
M src/apps/collection/migrations/0008_source_access_policy_runtime.py (325 lines)
M src/apps/collection/migrations/0009_run_step_generation_fencing.py (288 lines)
M src/apps/collection/migrations/0010_run_control_decision.py (146 lines)
M src/apps/collection/migrations/0011_source_item_retention_tombstone.py (143 lines)
M src/apps/collection/migrations/0012_sourceitem_optional_attachments.py (13 lines)
N src/apps/collection/migrations/__init__.py (0 lines)
P src/apps/collection/models.py (776 lines)
P src/apps/collection/services.py (2678 lines)
F src/apps/collection/tasks.py (335 lines)
N src/apps/collection/urls.py (10 lines)
N src/apps/collection/views.py (7 lines)
N src/apps/editorial/__init__.py (1 lines)
N src/apps/editorial/admin.py (34 lines)
N src/apps/editorial/api.py (858 lines)
N src/apps/editorial/apps.py (7 lines)
P src/apps/editorial/clustering.py (1013 lines)
P src/apps/editorial/corrections.py (524 lines)
M src/apps/editorial/migrations/0001_initial.py (172 lines)
M src/apps/editorial/migrations/0002_event_cluster_verification.py (427 lines)
M src/apps/editorial/migrations/0003_editorial_policy_runtime.py (1039 lines)
M src/apps/editorial/migrations/0004_correction_decision.py (410 lines)
N src/apps/editorial/migrations/__init__.py (0 lines)
P src/apps/editorial/models.py (874 lines)
F src/apps/editorial/policies.py (343 lines)
F src/apps/editorial/quality.py (586 lines)
P src/apps/editorial/services.py (2823 lines)
P src/apps/editorial/tasks.py (963 lines)
N src/apps/editorial/urls.py (20 lines)
N src/apps/editorial/views.py (7 lines)
N src/apps/evidence/__init__.py (2 lines)
N src/apps/evidence/admin.py (65 lines)
N src/apps/evidence/api.py (498 lines)
N src/apps/evidence/apps.py (9 lines)
N src/apps/evidence/management/__init__.py (1 lines)
N src/apps/evidence/management/commands/__init__.py (1 lines)
F src/apps/evidence/management/commands/import_extraction_profiles.py (47 lines)
N src/apps/evidence/management/commands/verify_extraction_profile_files.py (124 lines)
N src/apps/evidence/management/commands/verify_extraction_profile_snapshots.py (27 lines)
N src/apps/evidence/management/commands/verify_extraction_profiles.py (18 lines)
N src/apps/evidence/management/commands/verify_ocr_manifest.py (28 lines)
M src/apps/evidence/migrations/0001_initial.py (359 lines)
M src/apps/evidence/migrations/0002_documentextraction_input_fingerprint.py (130 lines)
M src/apps/evidence/migrations/0003_evidenceasset_raw_input_fingerprint.py (352 lines)
M src/apps/evidence/migrations/0004_extractionprofiledecision_report_envelope.py (84 lines)
M src/apps/evidence/migrations/0005_extraction_generation_fencing.py (1111 lines)
M src/apps/evidence/migrations/0006_generic_evidence_manifest.py (765 lines)
N src/apps/evidence/migrations/__init__.py (1 lines)
P src/apps/evidence/models.py (1195 lines)
F src/apps/evidence/profiles.py (609 lines)
P src/apps/evidence/services.py (2530 lines)
P src/apps/evidence/tasks.py (4872 lines)
N src/apps/evidence/urls.py (31 lines)
N src/apps/topics/__init__.py (1 lines)
N src/apps/topics/admin.py (41 lines)
N src/apps/topics/api.py (397 lines)
N src/apps/topics/apps.py (7 lines)
N src/apps/topics/management/__init__.py (0 lines)
N src/apps/topics/management/commands/__init__.py (0 lines)
N src/apps/topics/management/commands/seed_source_registry.py (43 lines)
N src/apps/topics/management/commands/verify_source_registry_snapshots.py (211 lines)
M src/apps/topics/migrations/0001_initial.py (118 lines)
M src/apps/topics/migrations/0002_source_registry_contract.py (1165 lines)
M src/apps/topics/migrations/0003_freeze_legacy_source_execution_material.py (145 lines)
M src/apps/topics/migrations/0004_semiconductor_source_choices.py (25 lines)
N src/apps/topics/migrations/__init__.py (0 lines)
P src/apps/topics/models.py (899 lines)
P src/apps/topics/services.py (3697 lines)
F src/apps/topics/tasks.py (180 lines)
N src/apps/topics/urls.py (39 lines)
F tests/conftest.py (95 lines)
P tests/unit/test_editorial_policy.py (2168 lines)
P tests/unit/test_extraction_generation_fencing.py (2952 lines)
P tests/unit/test_retention_dependency_graph.py (697 lines)
F tests/unit/test_review_collection_regressions.py (97 lines)
F tests/unit/test_review_extractor_regressions.py (76 lines)
```
