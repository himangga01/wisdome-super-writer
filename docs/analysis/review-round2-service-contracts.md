# Round 2 service, console, local tooling and contract review

## Baseline and scope

- Review date: 2026-10-03, Asia/Seoul; reviewer F, fresh second-round reviewer.
- Source baseline: `main`, `7eb33310665dfffafa15561d66d6d1a2d4b48e6b`.
- This review examines the concurrent working tree after the first main-session
  repair pass. Initial changes included the first-pass Compose, command-boundary,
  local acceptance, storage, collection, extractor, publisher and operations fixes
  and their regressions, plus canonical reports and agent instructions. These
  changes were preserved; HEAD was not changed by this reviewer.
- Starting authority: `AGENTS.md`, the shared policy at
  `C:/Users/강지혜/.agents/analysis-policy.md`, [code analysis](../code-analysis.md),
  [service analysis](../service-analysis.md), [two-round ledger](2026-10-03-two-round-review.md),
  and all three round-one reports. Their results are historical inputs, not newly
  executed verification.
- Assigned perspective: user-visible local service behavior, administrator console
  and preview, API/console integration, acceptance/CI/Windows tooling, deployment,
  runtime/readiness and account/audit service interfaces. Cross-reads inspect the
  publishing serializers and action boundaries relevant to the console.
- Allowed writes made here: this report and isolated diagnostics under
  `.local/reviews/round2-service-contracts/` only. Application source, tests,
  dependencies, task checkboxes, historical acceptance, canonical reports and
  instructions were not edited by this reviewer. The main session owns fixes and
  canonical documentation integration.

Locations below identify the working-tree source when each probe was executed.
Concurrent main-session fixes subsequently advanced some lines. A later independent
recheck (`post-main-results.json`) confirms F3's response shape is repaired and
F1's valid filtered-count scenario succeeds; F1 still has the negative count-boundary
regression described below. Original probe outputs are retained separately rather
than overwritten. Other findings remain observations for main-session adjudication.

The service has two distinct implemented paths: a protected local housing writer
with immutable bundles and loopback preview, and a distributed evidence/editorial/
publishing service operated through staff/session/CSRF APIs. Local humanization,
distributed publisher validation, administrator approval, command evidence and
actual external acceptance remain separate facts. The strongest newly executed
observations here concern optional API reconciliation, destructive HWP CLI rejection,
truthfulness of report responses and acceptance-history composition.

This is broad file inventory plus targeted semantic and executable boundary review.
It is **not an exhaustive line-by-line audit of 52,078 assigned text lines**. The
appendix names every inventoried file, inspected ranges and explicit unread bodies.

## Findings and cross-round mapping

| ID | Priority | Cross-round identity | Observation |
| --- | --- | --- | --- |
| F1 | P1 | A6 | Supported filtered ODCloud envelope blocks local reconciliation |
| F2 | P2 | A9 | HWP exact CLI deletes pre-existing output/report on rejection |
| F3 | P2 | C10 | Target serializer violates its public response schema |
| F4 | P2 | C11 | Validation report confuses approval projection with test result and violates schema |
| F5 | P2 | New extension of C11 | A retrieved report with the wrong digest can still be reported as passed |
| F6 | P2 | C12 | Existing renderless withdrawal intent aborts console initialization |
| F7 | P2 | Round-one C additional observation | Publishing evidence source links use the wrong property |
| F8 | P2 | New | Acceptance CLI creates an invalid eligible-history declaration after two passes |

Priorities refer to user effects within the implemented boundary. F2 is a closed
HWP pre-release/helper defect; it does not justify enabling unsigned HWP execution.
F3-F7 are contract/reporting/console defects, not demonstrated privilege bypasses
or real remote publishing incidents.

### F1 — filtered ODCloud counts still block credential-enabled local writing

- Current location: `src/apps/local_content/api_reconciliation.py:217-226`,
  `:251-255`; live command wiring at
  `src/apps/local_content/management/commands/collect_recent_housing.py:131-142`.
- Trigger: an otherwise valid date-filtered response has `totalCount=200`,
  `matchCount=1`, one matching notice on page 1 and an empty page 2. The current
  loop uses the entire-dataset total rather than the filtered match count.
- Expected: accept the supported one-record filtered envelope without requesting
  an unnecessary second page. Observed independently: pages `[1, 2]`, followed by
  `OFFICIAL_API_PAGINATION_FAILED`. The distributed approved-envelope parser at
  `src/adapters/sources/http.py:381-406` returns an expected count of `1` for the
  same material. Reconciliation catches the error and returns a source failure;
  configured local workflow completeness is affected.
- Reproduction: actual local observer and actual distributed count parser with a
  synthetic fetcher; no source request, credential or shared database was used.
- Counterargument: many unfiltered fixtures have equal total/match counts, and
  runs without an API key do not enter this branch. Those cases cannot establish
  filtered-envelope correctness. No new credentialed ODCloud acceptance is claimed.
- Minimal fix: validate/prefer the supported `matchCount` envelope consistently
  with the existing approved parser, retain total/match validation and stable
  pagination identity, and reject malformed/drifting/duplicate material.
- Regression: filtered count smaller than total, zero matches, absent match count,
  malformed count types, match count greater than total, page drift and truncation;
  real `ReconciledOfficialCollector` composition should keep a matching HTML/API
  run complete and conflicting material incomplete.

Second-main-pass recheck: preferring `matchCount` at line 217 fixes the original
valid-envelope scenario (one notice, pages `[1]`). The initial one-line repair
also bypasses `totalCount` validation whenever `matchCount` exists. Actual local
observer now accepts all three negative envelopes `totalCount=0/matchCount=1`,
`totalCount=-1/matchCount=1`, and `totalCount="invalid"/matchCount=1`; the existing
distributed approved-envelope parser rejects each with `SourceSchemaError`.
This is a P2 fail-closed schema regression in the same F1 boundary, not an
unexecuted hypothesis. Keep validation of both supplied count fields and the
`matchCount <= totalCount` relation while selecting the filtered pagination count.
The original P1 happy-path block is repaired; this narrower negative case remains
for the main session. Evidence: `count-negative-results.json`.

### F2 — invalid exact HWP CLI input still removes caller-owned files

- Current location: `deploy/containers/hwp-worker/wisdome-hwp-sandbox:939-940`,
  `:995-1005`.
- Trigger: call `run_exact_cli` with existing output/report paths. The validation
  correctly refuses those paths as not-new, then its exception handlers unlink
  both paths despite no invocation-owned output being created.
- Expected: return invalid-input status and leave both caller files unchanged.
  Observed independently: exit `20`; both sentinel files became absent, before
  converter invocation or Linux isolation. Only disposable files under this
  reviewer's diagnostic directory were affected.
- Counterargument: the normal sidecar uses a fresh private job workspace, and
  `validate_legacy_hwp_activation_config` remains hard-false. This limits current
  product exposure; it does not make direct CLI rejection safe. No Linux converter
  or activated HWP pipeline was executed.
- Minimal fix: remove only outputs created by this invocation and still matching
  its opened/accepted file identity; preserve collisions and substituted paths.
  Validate unsafe/relative output arguments without deleting them. Preserve the
  existing disabled activation boundary.
- Regression: output-only collision, report-only collision, both collisions,
  early invalid input, genuine newly created partial-output cleanup and pathname
  substitution during cleanup. Test the actual CLI entry point, not only the
  private-workspace success path.

### F3 — ordinary target responses remain incompatible with their schema

- Current location: `src/apps/publishing/api.py:185`, `:192`; contract
  `specs/001-automated-content-publishing/contracts/admin-api.openapi.yaml:4899-4913`.
- Trigger: serialize a normal snapshot-bound WordPress target with internal
  `mark_withdrawn`/`media_upload` capabilities and no activation history.
- Expected: public `markWithdrawn`/`mediaUpload` names and a declared no-activation
  value. Observed: actual `target_json` plus the real compiled `PublicationTarget`
  schema returns exactly three errors: both camel-case required properties are
  missing and activation version `0` is below minimum `1`.
- Reproduction: serializer and schema executed on a synthetic unsaved target
  material object with valid channel role/states; no DB or network writes.
- Counterargument: the current vanilla JS mostly ignores capability keys and
  version `0` and therefore may still display the target. Schema-valid generated
  clients and contract assertions are nevertheless incompatible.
- Minimal fix: explicitly serialize public capability names, keep internal command
  names intact, and represent absent activation consistently with the agreed
  public schema. Do not relax every unrelated target field.
- Regression: actual create/detail/list and activation responses for both channels,
  with and without activation history, validated against the real response schema.

Second-main-pass recheck: the new explicit camel-case capability mapping and
`row.auto_publish_activation_version or None` produce **zero** errors for the
same actual serializer/compiled-schema probe. This narrower F3 repair is independently
confirmed. Full endpoint/real-stack acceptance remains outside this probe.

### F4 — report approval status is presented as the test outcome

- Current location: `src/apps/publishing/api.py:615-647`; `VerificationReport` at
  OpenAPI lines `2466-2496`; consumer
  `src/static/admin_console/publishing_target.js:97-104`.
- Trigger: inspect a draft server-owned canary candidate whose recorded stages
  passed. The view derives `overallResult` from the mutable approval projection
  (`row.status`) instead of the immutable test evidence.
- Expected: display the actual canary/pilot result and approval state separately.
  Observed independently: every emitted synthetic stage is `passed`, but
  `overallResult` is `failed` because the candidate is still `draft`. The same
  response has six real schema errors: missing `reportObjectKey`,
  `sampleManifestHash`, `generatedAt`; empty required `samples`; and `{}` instead
  of arrays for `metrics` and `thresholds`.
- Reproduction: actual unwrapped view with a synthetic ORM return and actual
  response-schema compiler. This verifies composition/serialization, not a real
  canary or approval journey.
- Counterargument: draft approval must remain ineligible for automatic publication.
  That is correct and must be preserved. It does not mean the underlying test
  failed; the console explicitly labels this value as the report result.
- Minimal fix: align/version the public report type with the real server canary/
  pilot evidence, expose truthful provenance/result and a distinct approval state.
  Do not invent corpus samples, metrics or thresholds to satisfy the old generic
  report schema.
- Regression: actual responses for draft, approved, revoked and stale material;
  passed and failed canary/pilot tests; missing and malformed evidence; complete
  schema validation and console result labels.

### F5 — immutable-report digest mismatch falls through to a passed result

- Current location: `src/apps/publishing/api.py:625`, `:649-670`, particularly
  the conditional at `:654`.
- Trigger: an approved validation uses a versioned S3 report reference; retrieving
  bytes succeeds but their SHA-256 differs from `row.test_report_hash`.
- Expected: identify the evidence as unavailable/corrupt and avoid asserting a
  verified passed report. Observed independently: actual view returns
  `overallResult="passed"`, `stageResults=[]` with `actual_hash_matches=false`.
  A mismatch is not raised or handled by the existing unavailable-report branch.
- Reproduction: actual view, synthetic passed row and mocked byte retrieval; no
  external storage was accessed. The bytes deliberately represented a failed
  report, but the relevant observation is the digest mismatch/fallthrough.
- Counterargument: the stored approval projection may be legitimate historical
  state. Returning that historical state separately is reasonable. Returning it
  as the retrieved report's verified result is misleading. This probe does not
  demonstrate that the activation service admits corrupt evidence.
- Minimal fix: treat hash mismatch, invalid JSON, incomplete/incorrect subject
  binding and incompatible report material as explicit evidence-verification
  failures. Keep historical approval state separate and retain redaction.
- Regression: correct digest, wrong digest, malformed bytes, unavailable version
  and subject mismatch for both approved and draft candidates; never fabricate a
  passed report from an approval projection after evidence verification fails.

### F6 — renderless withdrawal cannot be reviewed through the existing console

- Current location: `src/static/admin_console/publishing_article.js:152-165`,
  `:173-185`, `:301-314`, `:461-472`; server action guard at
  `src/apps/publishing/services.py:1522-1557`, `:8070-8074`.
- Trigger: open the publishing page with an existing unpublish intent prepared by
  the supported API. Unpublish has a frozen command subject and intentionally no
  content render. JS unconditionally fetches a content preview for every target.
- Expected: review the frozen remote identity/state/correction subject, issue the
  `unpublish` scoped proof, approve and dispatch. Observed in actual JS execution:
  legitimate no-preview response aborts initialization; no approvals or
  publications request occurs, zero approval controls are rendered, and the page
  shows `No publication preview`. A mixed target list also shares the rejecting
  `Promise.all` path. The subject builder always emits `content_preview` and
  approval only obtains a proof for revocation, which independently prevents a
  correct unpublish approval.
- Reproduction: real untouched JS in Node with a small DOM/fetch harness and
  synthetic valid intent material. No browser rendering, server, remote post or
  privileged operation was used.
- Counterargument: T026 UI completion and distributed acceptance are still open.
  The absence of a correction prepare-publication button is a **planned UI gap**,
  not a claim that the backend action is absent or historically accepted. The
  narrower proved defect is that the existing page cannot consume a supported
  already-existing renderless intent.
- Minimal fix: branch material/review/approval by action, expose a server-derived
  unpublish subject, preserve renderlessness and purpose-bound proof requirements;
  then connect the verified-correction preparation UI in its planned task scope.
- Regression: actual JS behavior for unpublish-only and mixed cohorts, refused/
  expired proof, stale remote identity, normal preview approvals, and corrected
  publication preparation. Real browser journey remains required separately.

### F7 — evidence source links disappear on the publishing review page

- Current location: `src/static/admin_console/publishing_article.js:102-108`.
  Correct existing consumer: `src/static/admin_console/articles.js:89-91`.
- Trigger: normal evidence snapshot has `sourceUrl` as a valid HTTP(S) string and
  `locator` as a structured locator object. Publishing calls `safeHttpUrl` on that
  object instead of the URL field.
- Expected: administrator can open the original evidence source while approving.
  Observed in the actual console JS probe: zero source anchors for a snapshot with
  a valid synthetic official URL. `url_safety.js:5` correctly rejects non-strings;
  changing that guard would be the wrong repair.
- Counterargument: preview sourceLinks or the article-detail screen can provide
  alternate links. This narrows the user impact but does not fix the advertised
  evidence review card.
- Minimal fix: pass `evidence.sourceUrl` through the current URL safety helper and
  retain structured locator display separately if useful.
- Regression: real JS evidence card with a structured locator and valid source URL,
  plus unsafe, absent and malformed source URLs. No browser acceptance is claimed.

### F8 — repeated valid command histories cannot pass final selection verification

- Current location: `scripts/run_task12_deterministic.py:129-142`; independent
  verifier `src/apps/local_content/acceptance_runner.py:785-804`.
- Trigger: use `--history-evidence` for an earlier complete passing attempt with
  the same exact current command plan, then finish another passing attempt.
  `_merge_report` declares only the current attempt eligible, even though it
  retains the earlier eligible document in history.
- Expected: retain both eligible IDs in order and select the latest. Observed:
  actual `_merge_report` produces `[new-pass]`; actual independent verifier derives
  `[old-pass,new-pass]` and fails `ATTEMPT_SELECTION_NOT_LATEST_ELIGIBLE`. Both
  individual documents independently pass `verify_deterministic_evidence`.
- Reproduction: actual combiner and verifier, using the existing test's synthetic
  closed-log fixture and a second same-plan copy in one private fixture project.
  No command suite was executed; these are fixture evidence documents, not a new
  acceptance or fabricated claim that application tests passed.
- Counterargument: failure-to-pass history (the current test at
  `tests/unit/test_task12_deterministic_runner.py:324-363`) has only one eligible
  entry and masks this bug. The documented rule is `latest_complete_exact_plan_pass`,
  not `current_attempt_only`.
- Minimal fix: derive full eligibility with the same verifier/current plan used
  at finalization, select the last eligible entry, and bind its actual evidence.
  Preserve failed history. Clarify CLI failure exit behavior independently from
  the chosen historical acceptance subject.
- Regression: failed-to-pass, pass-to-pass, pass-to-failure, changed-plan history,
  invalid digest, duplicate IDs and exact replay of selected evidence; use the
  actual CLI merge helper followed by the actual independent selection verifier.

## Repair rechecks executed independently

| First-round repair | New evidence from this reviewer | Limits |
| --- | --- | --- |
| A1 storage cleanup ownership | Existing destination and invalid-header cases retain caller bytes; new checksum failure removes only the new file; each mock body closes exactly once | Mock streaming/storage; external S3 and concurrent hardlink/process races not exercised |
| A2 valid fixture interpreter | Actual repaired `_full_setup_fixture` creates a venv under a Korean path; its launcher exits 0 and reports Python 3.12; `pyvenv.cfg` exists | No full setup script or manage migration invoked here |
| A5 UTF-8 harness | Generated controlled PS file has UTF-8 BOM; simple BOM script with Korean JSON value roundtrips correctly through the current UTF-8 decoder | This is a narrow text/fixture check, not full Unicode lifecycle acceptance |
| A11 lock synchronization | Source now probes non-truncating `r+b` denial with bounded polling, deliberately delayed startup and `finally` child cleanup | Timing/lock test not rerun while main suite owns lifecycle tests |
| A3 Compose mode | Shared YAML environment explicitly selects distributed; all Django/worker merge references inspected | No Compose build/start/deployment |
| A7/A8 file boundaries | Actual isolated Git repo discovers Korean, leading-space and leading-hyphen files; actual Ruff reaches all three via `--` and returns expected F821 | Newline preserved by pure NUL decoder only; Windows forbids an actual newline filename |
| A10 acceptance run binding | Actual combiner accepts matching synthetic subjects and rejects mismatched run with `ARTIFACT_RUN_IDENTITY_MISMATCH` | Composition probe mocks upstream verifier results, not two complete live artifact trees |
| C1 schedule cron | Real inherited configuration and patch field schemas accept five fields and reject four/six | Semantic cron/timezone execution not reaccepted here |
| C2/C4 console requests | Actual operations JS executed: PATCH omits topic, edit disables it, create includes it, optional MFA reaches the correct proof request | DOM/fetch harness, not real browser/authentication |
| Local lock/status/resource boundary | Actual `_WorkflowGuard` under Korean paths reports idle -> active -> idle; descriptor released and persistent guard retained | Single-process Windows file locking; optional metadata may be absent while byte lock is held; no server started |

The WordPress A4 repair was source-cross-checked through the existing first-pass
diff and tests; this reviewer did not run another real or mocked credential
revocation. It remains a first-pass main-session result rather than a fresh remote
verification here.

## Verification commands and actual limits

All Python diagnostics used the existing `.venv/Scripts/python.exe`, development/
local settings and `PYTHONUTF8=1`; inherited local-root override variables were
removed in the Django/probe child environment. No shared DB was changed.

| Command/check | Actual result |
| --- | --- |
| `python .local/reviews/round2-service-contracts/probe_initial.py` | Exit 0; reproduced F1-F5 and verified A10 composition repair |
| `python .local/reviews/round2-service-contracts/probe_initial.py post-main-results.json` | Exit 0; F1 happy-path repair and F3 shape repair confirmed; F2/F4/F5 still reproduced at this recheck |
| `node .local/reviews/round2-service-contracts/console_probe.cjs src/static/admin_console/publishing_article.js` | Exit 0; actual JS reproduces F6 and F7 |
| `python .local/reviews/round2-service-contracts/probe_edges.py` | Final exit 0; actual filename/Ruff/storage/lock/venv/UTF-8 boundary results above. Its synthetic Ruff child intentionally exits 1 for F821 |
| `node tests/js/operations_console_probe.cjs src/static/admin_console/operations.js` | Exit 0; PATCH/create/MFA behavior confirms C2/C4 changes |
| `python .local/reviews/round2-service-contracts/probe_history.py` | Exit 0; F8 reproduced, both individual synthetic evidence documents valid and combined selection invalid |
| `python .local/reviews/round2-service-contracts/inventory.py` | Exit 0; 120 product/config/assets, 26 related test files, 100 Python parses, one PNG header/dimensions/digest |
| All OpenAPI component schemas through actual `_compile_schema` | 156 schemas compile, zero schema construction errors; 54 paths/67 operation declarations inventoried |

Private probe scaffolding initially needed a corrected quote and an inherited
schema lookup; those were diagnostic preparation errors. Failed scaffolding runs
are not application failures or application-test results. Broad tool reads that
truncated output are treated as samples below, not complete semantic reads.

No pytest suite/test invocation, Django check/migration, Windows port-3210 lifecycle
test, application server, browser, Compose, source collection, real sibling
transform, PostgreSQL/Redis/Celery delivery, S3 integration, publisher write,
PaddleOCR model/corpus or Linux HWP execution was performed by this reviewer.
Main-session tests in progress are not this reviewer's verification results.
Historical August acceptance remains historical and task completion/dependencies
remain governed by `specs/001-automated-content-publishing/tasks.md`.

## Risks, priorities and open questions

1. Close the reproducible local filtered-envelope and owned-file cleanup defects;
   preserve their negative cases and keep HWP activation disabled.
2. Give administrators truthful report evidence and schema-valid responses before
   treating their manual decisions as a supported end-to-end console journey.
3. Repair action-specific withdrawal review and the history selection producer;
   perform genuine browser/command acceptance after the integrated code fixes.
4. Do not promote current static/fixture checks into distributed readiness,
   publisher canary/pilot, OCR/HWP release or historical Task 12 reacceptance.

Open product decisions: how to version a canary/pilot report separately from a
corpus verification report; which approval state should remain visible when
immutable evidence is unavailable; whether correcting/withdrawing articles is a
required next supported console journey; and when command acceptance must bind
the source contents/HEAD rather than only the exact command/filename plan.
The latter is a verification-design question here, not a newly proved privilege
or acceptance bypass finding.

Additional source observations, not counted as reproduced defects: several list
screens consume only their first cursor page (articles/runs/corrections/publications);
preparation/revision/configuration actions remain partly API-only while T026 is
open; distributed readiness observes DB/broker/outbox but not all workers/storage/
publishers; and renderer/font changes can intentionally invalidate historical
bundle replay. Resolve product/scale requirements before speculative UI/cache/
readiness redesign.

## Coverage appendix

Every assigned path in the snapshot is listed below. `Ranges` are source-text
read requests, supplemented by direct complete reads called out separately.
Some large batched outputs truncated, so ranged files are conservatively marked
**sampled** unless the complete text was independently received. AST parsing and
file/digest inventory establish coverage accounting, not semantic correctness.
An `inventory only` entry has an unread body; for a sampled entry, all ranges not
listed are unread, and truncated ranges have the noted extra limit. Generated
migrations/DDL are not executed or semantically certified by parsing.

The whole OpenAPI file was parsed and all operation declarations and component
schema construction were inventoried. Raw description/constraint reading was
targeted; it is not full route/response conformance acceptance. The PNG is valid
as a header/dimension/digest observation (1730 x 909, SHA-256 matches the image
module); its visual design/rights provenance was not independently audited.

| File | Snapshot lines | Depth / inspected ranges | Unread limits |
| --- | ---: | --- | --- |
| `.env.example` | 67 | File inventory only | Entire body semantically unread |
| `.env.local.example` | 12 | File inventory only | Entire body semantically unread |
| `.github/workflows/quality.yml` | 70 | Complete received source text | No executed acceptance implied |
| `compose.yaml` | 489 | Sampled text: 1-489 | Outside ranges: none; truncation limits also apply |
| `deploy/compose-deploy.ps1` | 366 | Sampled text: 1-366 | Outside ranges: none; truncation limits also apply |
| `deploy/containers/app/Dockerfile` | 26 | Sampled text: 1-26 | Outside ranges: none; truncation limits also apply |
| `deploy/containers/app/entrypoint.sh` | 11 | Sampled text: 1-11 | Outside ranges: none; truncation limits also apply |
| `deploy/containers/hwp-worker/build_manifest.py` | 158 | AST declarations + file inventory | Entire body semantically unread |
| `deploy/containers/hwp-worker/Dockerfile` | 92 | Sampled text: 1-92 | Outside ranges: none; truncation limits also apply |
| `deploy/containers/hwp-worker/fonts.conf` | 7 | Sampled text: 1-7 | Outside ranges: none; truncation limits also apply |
| `deploy/containers/hwp-worker/wisdome-hwp-sandbox` | 1615 | Sampled text: 1-145, 199-464, 906-1006 | Outside ranges: 146-198, 465-905, 1007-1615; truncation limits also apply |
| `deploy/containers/paddleocr-model-bootstrap/bootstrap.py` | 159 | AST declarations + file inventory | Entire body semantically unread |
| `deploy/containers/paddleocr-model-bootstrap/Dockerfile` | 23 | Sampled text: 1-23 | Outside ranges: none; truncation limits also apply |
| `deploy/containers/paddleocr-worker/Dockerfile` | 25 | Sampled text: 1-25 | Outside ranges: none; truncation limits also apply |
| `deploy/containers/profile-admin/Dockerfile` | 25 | Sampled text: 1-25 | Outside ranges: none; truncation limits also apply |
| `pyproject.toml` | 69 | File inventory only | Entire body semantically unread |
| `README.md` | 632 | File inventory only | Entire body semantically unread |
| `scripts/audit_task12_live_run.py` | 101 | Complete received source text | No executed acceptance implied |
| `scripts/ci_changed_python.py` | 184 | Complete received source text | No executed acceptance implied |
| `scripts/finalize_task12_acceptance.py` | 49 | Complete received source text | No executed acceptance implied |
| `scripts/local-process-guard.psm1` | 896 | Sampled text: 622-710, 782-896 | Outside ranges: 1-621, 711-781; truncation limits also apply |
| `scripts/local-toolchain.psm1` | 262 | Sampled text: 1-120, 190-262 | Outside ranges: 121-189; truncation limits also apply |
| `scripts/prepare_ci_sqlite.py` | 40 | Complete received source text | No executed acceptance implied |
| `scripts/run_task12_deterministic.py` | 155 | Complete received source text | No executed acceptance implied |
| `scripts/run_task12_live_workflow.py` | 144 | Complete received source text | No executed acceptance implied |
| `scripts/setup-local.ps1` | 766 | File inventory only | Entire body semantically unread |
| `scripts/start-local.ps1` | 281 | Complete received source text | No executed acceptance implied |
| `scripts/toolchain-lock.json` | 14 | Complete received source text | No executed acceptance implied |
| `scripts/verify_task12_brave.py` | 420 | AST declarations + file inventory | Entire body semantically unread |
| `specs/001-automated-content-publishing/contracts/admin-api.openapi.yaml` | 5216 | Full parse; 67 operations, 156 schema compilation; raw ranges 2460-2505, 4550-4638, 4860-4925 | Other raw descriptions/constraints not semantically read; no full response conformance |
| `src/apps/accounts/__init__.py` | 2 | AST declarations + file inventory | Entire body semantically unread |
| `src/apps/accounts/admin.py` | 56 | AST declarations + file inventory | Entire body semantically unread |
| `src/apps/accounts/api.py` | 29 | Complete received source text | No executed acceptance implied |
| `src/apps/accounts/apps.py` | 9 | AST declarations + file inventory | Entire body semantically unread |
| `src/apps/accounts/forms.py` | 16 | AST declarations + file inventory | Entire body semantically unread |
| `src/apps/accounts/managers.py` | 28 | AST declarations + file inventory | Entire body semantically unread |
| `src/apps/accounts/migrations/0001_initial.py` | 61 | AST declarations + file inventory | Entire body semantically unread |
| `src/apps/accounts/migrations/0002_adminaccount_reauthentication_throttle.py` | 25 | AST declarations + file inventory | Entire body semantically unread |
| `src/apps/accounts/migrations/__init__.py` | 0 | AST declarations + file inventory | Empty module |
| `src/apps/accounts/models.py` | 57 | Complete received source text | No executed acceptance implied |
| `src/apps/accounts/services.py` | 233 | Complete received source text | No executed acceptance implied |
| `src/apps/accounts/urls.py` | 8 | AST declarations + file inventory | Entire body semantically unread |
| `src/apps/audit/__init__.py` | 2 | AST declarations + file inventory | Entire body semantically unread |
| `src/apps/audit/admin.py` | 72 | AST declarations + file inventory | Entire body semantically unread |
| `src/apps/audit/api.py` | 395 | Sampled text: 1-395 | Outside ranges: none; truncation limits also apply |
| `src/apps/audit/apps.py` | 9 | AST declarations + file inventory | Entire body semantically unread |
| `src/apps/audit/cursor.py` | 93 | Complete received source text | No executed acceptance implied |
| `src/apps/audit/management/__init__.py` | 1 | AST declarations + file inventory | Entire body semantically unread |
| `src/apps/audit/management/commands/__init__.py` | 1 | AST declarations + file inventory | Entire body semantically unread |
| `src/apps/audit/management/commands/configure_runtime_database_role.py` | 357 | AST declarations + file inventory | Entire body semantically unread |
| `src/apps/audit/migrations/0001_initial.py` | 98 | AST declarations + file inventory | Entire body semantically unread |
| `src/apps/audit/migrations/0002_auditevent_append_only.py` | 206 | AST declarations + file inventory | Entire body semantically unread |
| `src/apps/audit/migrations/0003_retention_object_tombstone.py` | 220 | AST declarations + file inventory | Entire body semantically unread |
| `src/apps/audit/migrations/__init__.py` | 0 | AST declarations + file inventory | Empty module |
| `src/apps/audit/models.py` | 358 | AST declarations + file inventory | Entire body semantically unread |
| `src/apps/audit/redaction.py` | 1001 | AST declarations + file inventory | Entire body semantically unread |
| `src/apps/audit/retention.py` | 1337 | Sampled text: 1-190 | Outside ranges: 191-1337; truncation limits also apply |
| `src/apps/audit/services.py` | 661 | Sampled text: 1-130 | Outside ranges: 131-661; truncation limits also apply |
| `src/apps/audit/urls.py` | 31 | AST declarations + file inventory | Entire body semantically unread |
| `src/apps/local_content/__init__.py` | 1 | AST declarations + file inventory | Entire body semantically unread |
| `src/apps/local_content/acceptance.py` | 1620 | Sampled text: 186-265, 590-680 | Outside ranges: 1-185, 266-589, 681-1620; truncation limits also apply |
| `src/apps/local_content/acceptance_runner.py` | 1019 | Sampled text: 1-180, 208-684, 750-884 | Outside ranges: 181-207, 685-749, 885-1019; truncation limits also apply |
| `src/apps/local_content/api_reconciliation.py` | 396 | Complete received source text | No executed acceptance implied |
| `src/apps/local_content/apps.py` | 7 | AST declarations + file inventory | Entire body semantically unread |
| `src/apps/local_content/bundles.py` | 2203 | AST declarations + file inventory | Entire body semantically unread |
| `src/apps/local_content/contracts.py` | 92 | Complete received source text | No executed acceptance implied |
| `src/apps/local_content/dates.py` | 21 | Complete received source text | No executed acceptance implied |
| `src/apps/local_content/http.py` | 296 | Sampled text: 1-296 | Outside ranges: none; truncation limits also apply |
| `src/apps/local_content/humanizer.py` | 1141 | Sampled text: 108-219 | Outside ranges: 1-107, 220-1141; truncation limits also apply |
| `src/apps/local_content/images.py` | 669 | Sampled text: 1-156, 343-435 | Outside ranges: 157-342, 436-669; truncation limits also apply |
| `src/apps/local_content/management/__init__.py` | 1 | AST declarations + file inventory | Entire body semantically unread |
| `src/apps/local_content/management/commands/__init__.py` | 1 | AST declarations + file inventory | Entire body semantically unread |
| `src/apps/local_content/management/commands/collect_recent_housing.py` | 477 | Sampled text: 1-220 | Outside ranges: 221-477; truncation limits also apply |
| `src/apps/local_content/rendering.py` | 687 | Sampled text: 97-247 | Outside ranges: 1-96, 248-687; truncation limits also apply |
| `src/apps/local_content/selection.py` | 139 | Complete received source text | No executed acceptance implied |
| `src/apps/local_content/sources/__init__.py` | 1 | AST declarations + file inventory | Entire body semantically unread |
| `src/apps/local_content/sources/applyhome.py` | 691 | Sampled text: 75-235 | Outside ranges: 1-74, 236-691; truncation limits also apply |
| `src/apps/local_content/sources/lh.py` | 708 | Sampled text: 1-250 | Outside ranges: 251-708; truncation limits also apply |
| `src/apps/local_content/status.py` | 161 | Complete received source text | No executed acceptance implied |
| `src/apps/local_content/urls.py` | 27 | AST declarations + file inventory | Entire body semantically unread |
| `src/apps/local_content/views.py` | 622 | Sampled text: 1-411, 528-622 | Outside ranges: 412-527; truncation limits also apply |
| `src/apps/local_content/workflow.py` | 1769 | Sampled text: 278-914, 957-1160, 1310-1697 | Outside ranges: 1-277, 915-956, 1161-1309, 1698-1769; truncation limits also apply |
| `src/static/.gitkeep` | 1 | File inventory only | Entire body semantically unread |
| `src/static/admin_console/app.css` | 8 | Sampled text: 1-8 | Outside ranges: none; truncation limits also apply |
| `src/static/admin_console/articles.js` | 283 | Complete received source text | No executed acceptance implied |
| `src/static/admin_console/operations.js` | 509 | Complete received source text | No executed acceptance implied |
| `src/static/admin_console/publishing_article.js` | 474 | Sampled text: 1-474 | Outside ranges: none; truncation limits also apply |
| `src/static/admin_console/publishing_target.js` | 284 | Complete received source text | No executed acceptance implied |
| `src/static/admin_console/run_detail.js` | 7 | Complete received source text | No executed acceptance implied |
| `src/static/admin_console/runs.js` | 4 | Complete received source text | No executed acceptance implied |
| `src/static/admin_console/sources.js` | 979 | Sampled text: 1-979 | Outside ranges: none; truncation limits also apply |
| `src/static/admin_console/url_safety.js` | 17 | Complete received source text | No executed acceptance implied |
| `src/static/local_articles/generic-housing-hero.png` | binary | PNG header, dimensions and SHA-256 | No visual/provenance audit |
| `src/static/local_articles/preview.css` | 116 | Sampled text: 1-116 | Outside ranges: none; truncation limits also apply |
| `src/templates/admin_console/articles.html` | 18 | Sampled text: 1-18 | Outside ranges: none; truncation limits also apply |
| `src/templates/admin_console/base.html` | 29 | Sampled text: 1-29 | Outside ranges: none; truncation limits also apply |
| `src/templates/admin_console/index.html` | 14 | Sampled text: 1-14 | Outside ranges: none; truncation limits also apply |
| `src/templates/admin_console/operations/index.html` | 123 | Sampled text: 1-123 | Outside ranges: none; truncation limits also apply |
| `src/templates/admin_console/publishing/article_publish.html` | 61 | Complete received source text | No executed acceptance implied |
| `src/templates/admin_console/publishing/index.html` | 106 | Sampled text: 1-106 | Outside ranges: none; truncation limits also apply |
| `src/templates/admin_console/publishing/target_detail.html` | 73 | Sampled text: 1-73 | Outside ranges: none; truncation limits also apply |
| `src/templates/admin_console/run_detail.html` | 19 | Complete received source text | No executed acceptance implied |
| `src/templates/admin_console/runs.html` | 16 | Sampled text: 1-16 | Outside ranges: none; truncation limits also apply |
| `src/templates/admin_console/sources/index.html` | 194 | File inventory only | Entire body semantically unread |
| `src/templates/local_articles/article.html` | 20 | Sampled text: 1-20 | Outside ranges: none; truncation limits also apply |
| `src/templates/local_articles/index.html` | 31 | Complete received source text | No executed acceptance implied |
| `src/templates/local_articles/run.html` | 16 | Sampled text: 1-16 | Outside ranges: none; truncation limits also apply |
| `src/wisdome_writer/api/__init__.py` | 9 | AST declarations + file inventory | Entire body semantically unread |
| `src/wisdome_writer/api/health.py` | 325 | Complete received source text | No executed acceptance implied |
| `src/wisdome_writer/api/middleware.py` | 338 | Sampled text: 1-183 | Outside ranges: 184-338; truncation limits also apply |
| `src/wisdome_writer/api/openapi.py` | 1125 | Sampled text: 1-85, 360-384, 420-485 | Outside ranges: 86-359, 385-419, 486-1125; truncation limits also apply |
| `src/wisdome_writer/api/pagination.py` | 279 | AST declarations + file inventory | Entire body semantically unread |
| `src/wisdome_writer/api/problems.py` | 181 | AST declarations + file inventory | Entire body semantically unread |
| `src/wisdome_writer/api/urls.py` | 6 | AST declarations + file inventory | Entire body semantically unread |
| `src/wisdome_writer/celery.py` | 222 | AST declarations + file inventory | Entire body semantically unread |
| `src/wisdome_writer/console.py` | 38 | Complete received source text | No executed acceptance implied |
| `src/wisdome_writer/external_publishing.py` | 66 | Sampled text: 1-66 | Outside ranges: none; truncation limits also apply |
| `src/wisdome_writer/runtime_mode.py` | 11 | Sampled text: 1-11 | Outside ranges: none; truncation limits also apply |
| `src/wisdome_writer/settings/__init__.py` | 465 | Sampled text: 1-250, 330-465 | Outside ranges: 251-329; truncation limits also apply |
| `src/wisdome_writer/urls.py` | 48 | AST declarations + file inventory | Entire body semantically unread |
| `tests/conftest.py` | 95 | AST declarations + file inventory | Entire body semantically unread |
| `tests/integration/test_local_housing_workflow.py` | 2056 | AST declarations + file inventory | Entire body semantically unread |
| `tests/js/operations_console_probe.cjs` | 85 | Complete received source text | No executed acceptance implied |
| `tests/unit/test_legacy_hwp_sandbox.py` | 1129 | AST declarations + file inventory | Entire body semantically unread |
| `tests/unit/test_local_api_reconciliation.py` | 229 | AST declarations + file inventory | Entire body semantically unread |
| `tests/unit/test_local_content_acceptance.py` | 570 | AST declarations + file inventory | Entire body semantically unread |
| `tests/unit/test_local_content_bundles.py` | 2202 | AST declarations + file inventory | Entire body semantically unread |
| `tests/unit/test_local_content_dates.py` | 197 | AST declarations + file inventory | Entire body semantically unread |
| `tests/unit/test_local_content_http.py` | 482 | AST declarations + file inventory | Entire body semantically unread |
| `tests/unit/test_local_content_humanizer.py` | 1350 | AST declarations + file inventory | Entire body semantically unread |
| `tests/unit/test_local_content_images.py` | 306 | AST declarations + file inventory | Entire body semantically unread |
| `tests/unit/test_local_content_preview.py` | 304 | AST declarations + file inventory | Entire body semantically unread |
| `tests/unit/test_local_content_rendering.py` | 346 | AST declarations + file inventory | Entire body semantically unread |
| `tests/unit/test_local_content_selection.py` | 294 | AST declarations + file inventory | Entire body semantically unread |
| `tests/unit/test_local_publishing_policy.py` | 90 | AST declarations + file inventory | Entire body semantically unread |
| `tests/unit/test_local_runtime.py` | 209 | AST declarations + file inventory | Entire body semantically unread |
| `tests/unit/test_local_scripts.py` | 2055 | Sampled text: 1-180, 759-833 | Outside ranges: 181-758, 834-2055; truncation limits also apply |
| `tests/unit/test_operations_admin_contract.py` | 451 | AST declarations + file inventory | Entire body semantically unread |
| `tests/unit/test_operations_console_behavior.py` | 45 | AST declarations + file inventory | Entire body semantically unread |
| `tests/unit/test_profile_verification_report.py` | 263 | AST declarations + file inventory | Entire body semantically unread |
| `tests/unit/test_publisher_credentials.py` | 609 | AST declarations + file inventory | Entire body semantically unread |
| `tests/unit/test_publishing_admin_contract.py` | 301 | AST declarations + file inventory | Entire body semantically unread |
| `tests/unit/test_retention_dependency_graph.py` | 697 | AST declarations + file inventory | Entire body semantically unread |
| `tests/unit/test_review_acceptance_regressions.py` | 145 | AST declarations + file inventory | Entire body semantically unread |
| `tests/unit/test_s3_streaming_upload.py` | 165 | Sampled text: 90-165 | Outside ranges: 1-89; truncation limits also apply |
| `tests/unit/test_task12_deterministic_runner.py` | 399 | Sampled text: 1-190 | Outside ranges: 191-399; truncation limits also apply |

Cross-reads outside the primary inventory:
- `src/adapters/extractors/legacy_hwp.py`: sampled 169-215; remaining body not claimed read.
- `src/adapters/sources/http.py`: sampled 370-410; remaining body not claimed read.
- `src/adapters/storage/s3.py`: sampled 1-82, 218-300; remaining body not claimed read.
- `src/apps/publishing/api.py`: sampled 145-205, 580-675; remaining body not claimed read.
- `tests/unit/test_review_wordpress_revocation.py`: sampled 1-42; remaining body not claimed read.

## Update log

- 2026-10-03: starting policy/canonical context and three round-one reports read;
  independent F1-F7 and repair-boundary probes shared with the main session.
- 2026-10-03: F8 actual CLI/history composition defect reproduced; full assigned
  path inventory and explicit source/test/DDL/external verification limits saved.
  Main session remains responsible for adjudication, fixes, canonical report and
  instruction-path updates.
- 2026-10-03: concurrent second-main-pass F1/F3 changes independently rechecked;
  F3 schema shape passes, F1 valid filtered envelope passes, and negative total/match
  count validation regression shared for correction. Original probe evidence preserved.
