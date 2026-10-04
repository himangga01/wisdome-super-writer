# Two-round critical code and service review

## Outcome and baseline

- Date: 2026-10-03, Asia/Seoul.
- Source: main, 7eb33310665dfffafa15561d66d6d1a2d4b48e6b, plus the uncommitted repairs below.
- Existing README links, AGENTS/CLAUDE bridges and initial analysis reports preserved.
- User method completed: three parallel critical reviewers, main revalidation and repair,
  then three fresh reviewers and a second main repair. Supporting clarifications reused
  second-round reviewers; no third broad review round. Main owned all product edits.
- Companion [code analysis](../code-analysis.md), [service analysis](../service-analysis.md),
  [instructions](../../AGENTS.md), and completed
  [English modification plan](../superpowers/plans/2026-10-03-critical-review-remediation.md).
- No commit, push, task-checkbox change or real publisher write.

The service is a Django modular monolith for evidence-bound Korean housing and
semiconductor articles, with a separate Windows local housing writer. Reviews cover
collection, extraction, editorial, publishing, schedules, security, storage, local
tooling, deployment and public contracts. Reproduced defects were repaired; unfinished
features and external acceptance remain distinct.

## Reviewer evidence and coverage

| Round | Report | Perspective and actual coverage |
| --- | --- | --- |
| 1 | [A: security/local](review-round1-security-local.md) | 135 product/config files inspected, 30 tests sampled; runtime, accounts/audit/outbox, local tooling, storage and publishers. Generated DDL sampling is identified. |
| 1 | [B: pipeline integrity](review-round1-pipeline-integrity.md) | All 100 assigned baseline non-migration product/config files fully read; 25 migrations initially sampled. Collection, evidence, rights and grounding. |
| 1 | [C: publishing/contracts](review-round1-publishing-contracts.md) | 67 product/contract files inspected, full large publishing service; 35 tests selectively read; four large migrations initially sampled. |
| 2 | [D: security/state](review-round2-security-state.md) | Fresh security/state critique; four previously sampled large publishing migration bodies fully read; targeted service rereads with explicit gaps. |
| 2 | [E: pipeline reliability](review-round2-pipeline-reliability.md) | All 25 baseline pipeline migrations and new 0012 fully read. Additional 23 full reads, 20 partial reads and 55 inventory-only entries identified. PostgreSQL compiler/DDL inspection. |
| 2 | [F: service/contracts](review-round2-service-contracts.md) | 146 inventory paths, targeted semantic reads and sampling distinguished; local reconciliation, console, reports/API and acceptance interactions. |

Coverage is broad across components. Inventory/AST/testing do not mean every line
was semantically reviewed twice. Per-file appendices retain exact read and sampling
limits. Other DDL and test bodies still have gaps. Successful reviewer scratch tests
often assert a reproduced failure; main regression results below concern corrected
behavior. Finding IDs count observations, including duplicates, not independent bugs.

## Main-session adjudication

Main read producers, consumers and invariants before accepting criticism. Demonstrated
defects received failing regressions and minimal repairs, preserving immutable approval
material, replay ordering, ownership, generations, audits and locks. Reviewer reports
retain their dated snapshots; this ledger states dispositions after repairs.

### Round-one findings

| ID | Criticism | Main disposition and evidence |
| --- | --- | --- |
| A1 | S3 rejection deletes unowned destinations or trusts substitution | Repaired. Created device/inode identity controls cleanup. Real collision/header/version/checksum/substitution regressions; storage/safety group: 44 passed, 7 subtests. |
| A2/A5/A11 | Invalid Windows venv fixture, UTF-8 assumptions and lock-start timing | Repaired. Valid fixture venv, BOM-equipped PS probes, explicit UTF-8 state reads and actual write-denial synchronization with owned-process cleanup. Default-root/Unicode runtime-script group: 86 passed, 2 skipped. |
| A3 | Compose development selects local/eager mode | Repaired. Shared container environment explicitly selects distributed; configuration regression. |
| A4 | Exact WordPress self-revocation remains unknown on Core 401 | Repaired. Match successful DELETE UUID and accept only subsequent Core incorrect_password 401; unrelated/wrong-identity cases remain unknown. Four regressions. |
| A6 | Filtered ODCloud counts block reconciliation | Repaired across both passes. Use matchCount, validate both counts, reject malformed/contradictory/overfull envelopes. Supported total=200/match=1 completes. |
| A7/A8 | Filenames become Ruff options or lose Unicode | Repaired. File arguments follow --; NUL-delimited Git bytes preserve Unicode and whitespace. Actual isolated Git/Ruff regressions. |
| A9 | HWP rejection deletes pre-existing output/report | Repaired in pass two. Early collision rejection, exclusive report creation and identity-bound owned cleanup. Wrapper/client group: 33 passed, 1 skipped, 16 subtests. Linux converter unexecuted. |
| A10 | Acceptance combines another run's artifact audit | Repaired. Artifact run name must match report run path; mismatch fails overall acceptance. |
| B1 | HTML selectors resolve wrong nodes | Repaired. Structural ancestor paths and same-tag sibling ordinals. Actual DOM controls include nesting, mixed siblings and duplicate/numeric IDs. |
| B2 | Native XLSX date/time/duration fails canonical hashing | Repaired. Deterministic ISO/string values retain type metadata; real workbook regression. |
| B3 | Spreadsheet locator violates persistence contract | Repaired. Omit unsupported table_name. CSV/TSV/XLSX producer-to-validator tests; B1–B3/safety group: 47 passed, 4 subtests. |
| B4 | Publishable raw record fails editorial locator gate | Repaired as E2. Canonical source_record envelope and resolvable JSON pointer bind real source text to frozen editorial input. |
| B5 | Contradictory factual quantity passes quality | Repaired as E3 for numeric contradictions. Unit-aware quantities must be witnessed in cited spans; equivalent units pass, unsupported quantities fail. Not complete semantic entailment. |
| B6/B7 | No-attachment collection fails; change event contains enum | Repaired. Optional-attachments migration 0012 and plain-string event. Actual worker/SQLite/outbox regressions. |
| B8 | Ragged CSV/TSV budget undercounts and overcounts | Repaired with cumulative cells, valid and overflow controls; E9 closes blank-record allocation escape. |
| B9 | Derived constructors duplicate a keyword and crash | Repaired as E4, with E5 inherited-rights protection. Both document and generic materialization tested. |
| C1/C2 | Cron regex rejects normal values; PATCH includes immutable topic | Repaired. Compiled schemas and actual JS requests; create retains topic, edit disables/omits it. |
| C3 | Ineligible first tick aborts healthy schedules | Repaired as D5. Isolate material-ineligibility, audit skipped hold, advance captured tick and continue. Other failures are not broadly swallowed. |
| C4 | Operations reauthentication omits MFA | Repaired. Real JS forwards optional MFA; C1/C2/C4 group: 13 passed, 30 subtests. |
| C5 | Validation decision uses wrong proof scope | Repaired. validation_decision plus immutable audit request hash supports exact replay; real scoped proof/decision/replay test. |
| C6 | Preview creation and approval hashes disagree | Repaired through D1. Shared hashes preserve historic no-history form; narrowly accept paired legacy explicit-empty-history hashes. Mixed/stale/history-downgrade cases denied. |
| C7 | Media wait loses accepted future publication time | Repaired as D3. Save scheduled_for before media planning; resumed queueing uses it. |
| C8 | Reused available mapping leaves new binding prepared | Repaired as D4. Activate only from exact timestamped reconciliation proof; otherwise queue reconcile. Both channels tested. |
| C9 | Delayed disconnect revokes replacement credentials | Repaired as D2. Pending mutation guard, original accepted subject binding and per-write authorization fence. |
| C10 | Target response violates casing/nullability | Repaired as D7/F3. Seven capability names follow contract; absent activation version is null. Compiled schema regression. |
| C11 | Report conflates approval state and executed results | Repaired as F4/F5. Dedicated truthful schema, independent stage outcomes, bounded exact-version/digest-verified bytes, no invented samples/timestamps. |
| C12 | Console cannot approve renderless withdrawal | Confirmed T026 integration gap; concrete continuation task. Needs server-derived remote-state subject and scoped withdrawal proof, not a fabricated render. |
| C13 | Fresh preflight reuses a completed old job | Repaired as D6. Immutable request identity distinguishes new checks; exact replay preserved. |
| C14 | Uploaded approved visuals never enter final body | Source-traced unfinished T022 feature; no complete publication fixture/remote visual proof here. Plan binds approved placeholders, URLs, body/hash and featured media before acceptance. |

### Fresh second-round findings

| ID | Criticism | Main disposition |
| --- | --- | --- |
| D1 | C6 misses stored explicit-empty-history previews | Reproduced and repaired with compatibility and negative controls. |
| D2 | C9 replacement credential binding confirmed | Actual disconnect/update/begin and simulated legacy replacement tested; repaired. Combined group: 24 passed, 4 subtests. |
| D3 | C7 future-date loss confirmed | Failing queue/future-date regression passes; accepted time persists across media/dependency waits. |
| D4 | C8 mapping reuse stalls both channels | Actual WordPress/Blogger reuse regressions; proof-required activation repaired. |
| D5 | C3 due-scan starvation confirmed | Bad-builder/healthy-tick continuation and audited hold regression; repaired. |
| D6/D7 | C13/C10 confirmed | Fresh-request and compiled target-contract regressions; repaired. |
| D8 | Uncertain media deletion rolls back recovery outcome | Reproduced and repaired. Persist unknown outcome/audit without invalid publication-attempt reconcile event; mapping stays uncertain until actual absence proof. |
| D9 | Blogger revoke skips supplied write guard | Reproduced and repaired. Denial before POST prevents write. Credential group: 17 passed, 4 subtests. |
| E1 | Old collection overwrites newer failed/running delivery | Both successor states reproduced and repaired. Start/settlement bind current running delivery counter; run-control group: 15 passed. |
| E2/E3/E4 | B4/B5/B9 confirmed | Real raw fanout/locator snapshot, quantity controls and both materializers verify the repairs above. |
| E5 | Child attachment rights widen to record rights | Reproduced and repaired. Child rights/review derive from input; confidence may add restrictions, never remove them. |
| E6 | Old PostgreSQL trigger defeats retention exception | Source/DDL confirmed. Forward migration 0013 makes lineage guard INSERT-only; 0011 retains one-way UPDATE/DELETE guard. Fresh SQLite migration passes; PostgreSQL execution remains a release check. |
| E7 | Nullable PostgreSQL joins receive unqualified FOR UPDATE | Reproduced via actual PostgreSQL compilation of 13 production expressions, connection forbidden. Qualified owned-row locks; correction retains mutable article-head lock. Run-first and sorted evidence/child ordering unchanged. Compiler regressions pass; execution/races/deadlocks unverified. |
| E8 | Approved old material cannot authorize repaired bytes | Accepted rollout consequence. New HTML/spreadsheet 1.2.0 drafts and editorial policy 1.1.0; old profile files/approvals unchanged. HWP 1.2.0 candidate remains unapproved, requires rebuilt manifest. Fresh source snapshots/verification/ordinary approval remain release work. |
| E9 | Zero-cell blank rows bypass allocation budget | Reproduced and repaired. Skip blank records while retaining physical row indices. |
| F1/F2/F3 | A6/A9/C10 residuals | Reproduced or contract checked; repaired. F1 restores strict count validation after first-pass regression. |
| F4/F5 | C11 result/schema defect and wrong-digest report passes | Actual view/schema tests fail before repair, then pass: 12 tests, 18 subtests. Malformed/mismatched/unknown material fails closed. |
| F6 | C12 renderless initialization aborts | Confirmed; same scoped T026 task retained, no completion claim. |
| F7 | Evidence card uses locator object as URL | Actual production JS fails, then passes using sourceUrl, rejecting unsafe protocols. Console/API group: 11 passed, 18 subtests. |
| F8 | Repeated deterministic acceptance emits invalid history | Main probe/regression reproduced. Reverify same-plan history against current targets; include all eligible passing IDs, choose latest eligible success. |

## Rejected and conditional traces

- B-query rejected for the claimed normal path: redact_url removes queries before
  persistence; raw synthetic suffix input did not establish a reachable supported bug.
- Extensionless MOTIR HWPX routing, queue-one release after editorial quality failure,
  and corrected/restored historical source eligibility remain conditional B traces.
  Complete fixtures are required before accepting a defect.
- Early Blogger media preparation may precede WordPress dependency eligibility.
  Source-traced ordering hypothesis needs a staged media/dependency regression;
  no unexecuted race is recorded as a production incident.
- Uncalibrated OCR numeric confidence is an intentional admission block. Preserve it.

## Verification and limits

Python 3.12.10, Django 5.2.16, uv 0.12.19 and Ruff 0.16.0 used; CI/setup pins uv
0.12.7. Dependencies/lockfile unchanged. Pytest uses explicit development/local mode,
PYTHONUTF8=1 and default local roots with inherited overrides removed.

| Check | Actual result |
| --- | --- |
| First integrated repair suite | 1,319 passed, 3 skipped, 7 warnings, 203 subtests; 596.37 s |
| Second integrated check before final fixture/SQL/link repairs | 1 failed, 1,348 passed, 3 skipped, 7 warnings, 203 subtests; 613.51 s. Fixture lacked new publication save method. |
| Corrected dispatch fixture group | 26 passed, 20 subtests; persisted schedule precedes media and ledger/queue order stays protected |
| Final integrated suite | Exit 0; 1,363 passed, 3 skipped, 7 warnings, 203 subtests; 620.21 s |
| AST/current inventory | 335 parsed; source 251 files/108,908 lines; tests 77/40,644; scripts 7/1,105 |
| Django check / migration drift / applied migration check | Exit 0; no issues, no changes, no pending migrations in disposable state |
| Fresh SQLite migration | All migrations including collection 0012/0013 applied; PostgreSQL branch not executed |
| Draft import | HTML/spreadsheet created=2 unchanged=0; full candidate root blocked by unresolved HWP converter manifest without actual release material |
| Local-content Ruff / new review tests and migrations | Passed |
| Whole Ruff | Exit 1; 1,561 findings, versus initial analysis 1,561. Newly introduced style findings removed; pre-existing debt remains. Not a passing repository lint claim. |
| git diff --check | Passed |

A verification attempt was interrupted early to include extra nullable publication
eligibility joins, then restarted. Partial output is not a passing suite.
After the full suite, formatting-only cleanup removed 25 introduced Ruff findings.
All four affected product-file ASTs and the local-script fixture AST are unchanged;
39 relevant media/schedule/evidence/acceptance/compiler tests pass after cleanup.
A draft-import repeat returns created=0 unchanged=2.
Warnings concern absent generated staticfiles. Initial contaminated setup/empty-root
runs and two Windows fixture/timing failures are historical, superseded by repairs
and clean checks; canonical reports preserve their diagnoses.

Primary references: [WordPress credential authentication](https://developer.wordpress.org/reference/functions/wp_authenticate_application_password/),
[REST error conversion](https://developer.wordpress.org/reference/functions/rest_application_password_check_errors/),
[Django nullable locking semantics](https://docs.djangoproject.com/en/5.2/ref/models/querysets/#select-for-update).
These support contract interpretation, not live execution.

Not executed: PostgreSQL transactions/triggers, Redis/Celery delivery, MinIO/S3
integration, real publisher canary/pilot/withdrawal, official source/humanizer/browser
acceptance, OCR corpus, Linux HWP converter/signed acceptance, hosted CI, dependency
vulnerability audit or benchmarks. Numeric grounding is narrower than semantic
fact checking. HWP cleanup tests assume a private converter output directory,
not arbitrary external filesystem race certification.

## Continuation and update log

Follow the English plan: PostgreSQL acceptance; fresh immutable material approval
and rollout; action-specific withdrawal console; approved visual rendering;
Blogger media/dependency proof; distributed channel/corpus/operations acceptance.
T022/T026/T031/T032 and other unchecked dependency/acceptance gates stay authoritative.
Product questions remain in the [service report](../service-analysis.md#open-product-questions).

| Date | Update |
| --- | --- |
| 2026-10-03 | Two rounds of three reviewers completed; main reproduced, adjudicated and repaired defects; conditional features, rejected hypothesis and external release gates preserved |
