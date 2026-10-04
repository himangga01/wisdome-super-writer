# Round 1 pipeline-integrity review

- Date: 2026-10-03, Asia/Seoul.
- Reviewer: B; collection, extraction, evidence, source lineage, editorial quality.
- Source baseline: `main`, `7eb33310665dfffafa15561d66d6d1a2d4b48e6b`.
- Initial working tree: existing `README.md` modification and untracked `AGENTS.md`,
  `CLAUDE.md`, `docs/code-analysis.md`, `docs/service-analysis.md`. These were not
  changed by this reviewer. Product fixes by the main session are concurrent work;
  findings below describe the inspected original implementation.
- Starting references: `AGENTS.md`, the shared analysis policy, and both canonical
  code/service reports dated 2026-10-03. This report supplements them; the main
  session owns canonical report/instruction updates and product fixes.
- Writes by this reviewer: this report and isolated, ignored diagnostics under
  `.local/reviews/round1-pipeline-integrity/` only.

## Purpose and architecture

The assigned service surface freezes approved source/configuration material into
collection runs, records immutable source versions and per-run observations,
downloads evidence inputs, runs approved deterministic/PDF/OCR extractors, binds
result/content/locator identities, clusters official observations, and produces
evidence-bound article revisions with ten blocking editorial gates. Transactional
outbox deliveries connect stages. Leases and generations protect extraction
completion; editorial publication checks compare frozen policy, claims, evidence,
quality, source lineage and current eligibility again.

The Windows local housing writer is a separate implementation. Findings here
concern distributed extraction/editorial paths and do not invalidate historical
local acceptance. Approved profiles, source policies, live publishers, the OCR
corpus and the intentionally closed legacy-HWP activation boundary still require
their independent release evidence.

## Verified findings

### B1 — P1: HTML locators resolve other text or no node

- Location: `src/adapters/extractors/html.py:86`–`91` (fallback selector), producer
  loop at line 30. `src/apps/evidence/models.py:103`–`105` only checks selector/XPath
  shape; `src/apps/editorial/quality.py:265`–`274` delegates to that validator.
- Trigger: ordinary downloaded HTML with a heading and multiple paragraphs,
  without IDs. The extractor uses the index of the global matching-node list as
  the CSS sibling ordinal.
- Expected: each persisted locator uniquely resolves the exact node supplying its
  extracted text in the input document.
- Actual: `<h1>Official title</h1><p>First fact.</p><p>Second fact.</p>` produces
  `p:nth-of-type(2)` for `First fact.`; resolving it returns `Second fact.`. The
  second and nested paragraph selectors resolve no node. Valid-looking but wrong
  locators satisfy the shape-only evidence/quality checks. Other baseline pipeline
  blockers currently mask end-to-end persistence, but do not make these references
  correct.
- Evidence: real `HtmlExtractor` plus `selectolax.HTMLParser.css()` resolution in
  `extractor_probes.py`; three of four text records fail exact resolution.
- Counterargument considered: an ID can give the correct locator. Most matched
  content need not have IDs; duplicate IDs also need uniqueness checks. The
  fallback is demonstrably incorrect on minimal valid HTML.
- Minimal fix: derive a full ancestor/sibling path from the source tree, or an
  equivalent exact XPath. Use an ID only when unique. Build locators before
  removing source siblings if removal changes ordinals; verify each locator
  resolves one original-input node and matches its record material.
- Meaningful regression: real extraction of mixed tags, repeated tags, nested
  containers, duplicate IDs and script/style siblings; resolve every emitted
  locator against the original downloaded HTML, then persist the real records
  through the generic worker. Merely asserting that a selector is nonempty misses
  this failure.

### B2 — P1: date/time spreadsheet cells cannot cross the result-hash boundary

- Location: `src/adapters/extractors/spreadsheet.py:66`–`67` copies `cell.value`
  into `structured_data`; `src/apps/evidence/tasks.py:1520` canonicalizes the
  output before storage, called at `2534`–`2535`.
- Trigger: an ordinary XLSX containing a date or datetime cell. Openpyxl returns
  `datetime.datetime` values for the reproduced date/time cells.
- Expected: supported cell values have deterministic JSON-compatible typed
  representations, and ordinary dates produce stored derived evidence.
- Actual: `canonical_bytes(output.as_dict())` raises `CanonicalizationError:
  unsupported type: <class 'datetime.datetime'>`. The worker converts unexpected
  exceptions to `unexpected_extraction_failure` at `tasks.py:3357`–`3361`, and the
  routed terminal callback fails the attempt. Date cells fail before DB evidence
  validation or result-object reservation.
- Evidence: a workbook created with real openpyxl and extracted with the real
  `SpreadsheetExtractor`, in `extractor_probes.py`.
- Counterargument considered: `text` is already stringified. The separate
  `structured_data.cells[].value` remains the raw date object and is included in
  the canonical result. Fixing locator shape (B3) cannot repair this earlier error.
- Minimal fix: normalize supported non-JSON cell types into explicitly typed,
  stable representations before producing records. Preserve calendar/timezone
  meaning; do not invent a timezone for Excel's naive dates. Keep normal scalars,
  formula strings and error cells intact; reject unsupported values explicitly.
- Meaningful regression: real XLSX with date, datetime, time and timedelta cells,
  plus formulas and ordinary scalars; canonicalize its complete output and execute
  a real generic worker using fake versioned storage, verifying persisted text and
  structured values. Numeric/text-only mocked outputs do not cover this case.

### B3 — P1: every real spreadsheet locator violates the evidence contract

- Location: `src/adapters/extractors/spreadsheet.py:65` and `106` emit
  `table_name: null`; `src/apps/evidence/models.py:73`, `88`–`89` allows only
  `locator_type`, `sheet_name`, `cell_range` for `spreadsheet_cell`.
- Trigger: any nonempty XLSX/XLSM/CSV/TSV result from the shipped extractor.
- Expected: the producer's locator conforms to the same contract used at evidence
  persistence.
- Actual: real CSV and XLSX locators raise `ValidationError: Locator contains
  unsupported fields`. `_run_generic_extraction` calls `evidence.full_clean()` at
  `tasks.py:2672`, so ordinary spreadsheet records cannot commit as evidence,
  independently of B2. Transaction rollback prevents the partial evidence graph
  from becoming a successful extraction.
- Evidence: the real CSV and XLSX output locators were passed directly to
  `validate_evidence_locator` in `extractor_probes.py`; both fail.
- Counterargument considered: `table_name` might have been intentional earlier
  schema material. It is explicitly absent from the current persisted contract,
  and null does not exempt an unsupported key.
- Minimal fix: make the real producer emit the current exact contract, or version
  and implement a justified table locator consistently across contracts and
  consumers. Do not make the validator silently accept arbitrary keys.
- Meaningful regression: execute the real spreadsheet adapter through the generic
  task with a valid frozen profile and fake versioned storage; assert succeeded
  attempt, exact evidence count/manifest, ready event and no orphaned partial
  graph. Hand-built records without `table_name` bypass the faulty producer.

### B4 — P1: raw official-record locators are rejected by editorial quality

- Location: `src/apps/evidence/tasks.py:1018`–`1023` emits
  `structured_path/record_key/body_text`; `src/apps/evidence/models.py:106`–`108`
  allows only `json_pointer/xpath/jsonpath`; `src/apps/editorial/quality.py:265`–
  `274` validates that locator for high-impact claims.
- Trigger: source-record evidence from `_create_raw_evidence`, used by ordinary
  distributed official-source generation.
- Expected: a fresh rights-approved source record has a semantically resolvable
  locator, and an otherwise valid short official draft can pass its locator gate.
- Actual: RAW `EvidenceAsset.clean()` skips locator validation, so the evidence
  persists as publishable. `_frozen_evidence_snapshot` preserves its locator.
  Generated claim normalization conservatively marks unclassified claims as
  high-impact. A real template/normalization/quality probe passes nine gates and
  fails only `high_risk_verification_satisfied` for the producer's locator.
- Evidence: `editorial_raw_probe.py`, using the real policy, template generator,
  claim normalization and quality evaluator. No DB-to-publication acceptance is
  claimed for this probe.
- Exact stored shape: raw evidence has `extracted_text=SourceItem.body_text`,
  `structured_data=SourceItem.metadata`, and no result object. Housing adapter
  body text is canonical JSON; semiconductor body text is plain prose. The
  locator currently names the immutable SourceItem field, not a member of its
  metadata JSON.
- Counterargument considered: RAW records may intentionally use a logical
  source-record locator. That is reasonable, but the editorial validator lacks
  the corresponding explicit, lineage-bound variant. Relabeling it as an allowed
  JSON enum over unrelated metadata would make the locator false.
- Minimal fix: implement an explicitly bound raw source-record locator variant,
  allowed only for the exact source-record producer and source/content lineage,
  or persist a real canonical source-record envelope and use a valid pointer into
  that envelope. Preserve the high-risk locator gate and generic engine matrix.
- Meaningful regression: real raw fanout for a fresh approved official record,
  cluster/verify and real generation; resolve the raw locator to the exact stored
  body text and require all ten gates to pass. Cover both prose semiconductor
  records and structured housing bodies.

### B5 — P1: a contradictory high-impact factual value can pass every quality gate

- Location: `src/apps/editorial/quality.py:191`–`207` checks statement/block/link
  presence; `350`–`374` checks cited-span/source presence, without checking the
  statement against its cited span. `services.py:333`–`474` accepts these manual
  bindings; publication re-evaluates the same quality material at `2157`–`2164`.
- Trigger: an administrator's manual revision changes a housing price statement
  but keeps a genuine, contradictory cited source span.
- Expected: a claim marked factual, high-impact and price cannot be verified as
  supported when its numeric value disagrees with the cited official source.
- Actual: `분양가는 8억 원입니다.` is accepted with source span
  `분양가는 3억 원입니다.` and a frozen source containing only the latter. The real
  manual-binding validator, claim normalization and quality evaluator return
  `passed`, with no failed gates. Persisted equality checks cannot detect the
  semantic contradiction because they bind this same incorrect claim material.
- Evidence: `editorial_claim_probe.py` / `editorial-claim-probe.json`.
- Counterargument considered: administrators are trusted and paraphrases may be
  supported. This is an editorial quality-promise failure, not a claim that a
  trusted administrator crossed an authorization boundary. A changed factual
  number is a concrete contradiction rather than an uncertain paraphrase.
- Minimal fix: require deterministic statement/span agreement for factual and
  company claims in this MVP, or explicitly support canonical typed high-impact
  value comparison and bounded paraphrase rules. A source span being genuine must
  not alone verify a different claim.
- Meaningful regression: use real manual revision and revalidation against a
  frozen official source, change only the price/date/supply count in the claim,
  retain the valid original span, and require a blocking quality result and failed
  publication eligibility. Also retain passing equivalent formatting cases.

### B6 — P1: attachment-free source observations cannot be persisted

- Location: `src/apps/collection/models.py:535` defines
  `attachments = models.JSONField(default=list)` with `blank=False`;
  `SourceItem.save()` calls `full_clean()` at `579`.
- Trigger: a legitimate `CollectedSourceRecord` with zero attachments, the
  adapter interface's default output shape.
- Expected: empty attachment lists are accepted and ordinary text-only notices
  become source items/run observations.
- Actual: real `_persist_record` in a migrated in-memory SQLite collection fixture
  raises `ValidationError` for the empty attachments field and the transaction
  stores no source observation. The surrounding collector treats this as an
  unexpected infrastructure failure rather than accepting a valid empty list.
- Evidence: `collection-empty-attachments-probe.json` preserves the original
  diagnostic error and zero persisted rows. The later stale-result experiment
  uses an attachment-bearing record to avoid masking other code paths.
- Counterargument considered: some official documents normally carry attachments;
  the source interface and the supported newsroom/HTML paths permit text-only
  records. An empty collection must not be confused with invalid evidence.
- Minimal fix: allow a valid empty attachments array in model validation, with the
  corresponding migration. Preserve validation of array member shapes elsewhere.
- Meaningful regression: real `collect_source_attempt` with a contract-valid,
  fresh text-only record and no attachments; require succeeded attempt, immutable
  SourceItem/RunSourceItem, exact change event, and no infrastructure retry.

### B7 — P1: changed-item events reject the producer's enum value

- Location: `src/apps/collection/services.py:2274` passes
  `link.discovery_kind` to the outbox; `_discovery_kind` returns
  `SourceDiscoveryKind` TextChoices members at `1328`–`1354`.
  `src/wisdome_writer/infrastructure/outbox.py:246` requires an exact plain `str`.
- Trigger: a newly created changed source observation. Django retains its enum
  value in the returned in-memory instance until a DB reload.
- Expected: collection serializes domain values into the exact event contract and
  commits source observations/success/change events atomically.
- Actual: an attachment-bearing real collection fixture raises
  `ForbiddenEventPayload: event payload field has invalid type: change_kind
  (expected str)` at enqueue. The transaction rolls back the source/run-item
  creation and attempt-success mutation. A hand-built event with a plain string
  or a reloaded ORM object misses the faulty producer.
- Evidence: `collection-change-event-probe.json` captures the real exception at
  collector line 2259 and zero persisted observations/change events.
- Counterargument considered: TextChoices is a `str` subclass. The deliberate
  outbox check uses `type(value)`, so subclassing does not satisfy the contract.
- Minimal fix: serialize `str(link.discovery_kind)` at this producer boundary;
  keep the outbox's exact type validation.
- Meaningful regression: real fresh/corrected/retracted/unavailable/restored
  collection records; assert event values are plain strings, exact event identity
  and atomic DB success. Exercise newly created links, not only reloaded fixtures.

### B8 — P2: ragged CSV/TSV cell counting both undercounts and overcounts

- Location: original `src/adapters/extractors/spreadsheet.py:94` computes
  `row_index * max(1, len(row))` rather than cumulative cells scanned.
- Trigger: delimited records with varying widths.
- Expected: a cell budget is checked against the sum of all observed row widths.
- Actual: with `max_cells=10`, widths `[10, 1]` (11 cells) succeed, whereas widths
  `[1, 6]` (7 cells) are incorrectly rejected as `spreadsheet_limit_exceeded`.
- Evidence: `csv_cells_probe.py` and `csv-cells-probe.json`, real adapter output.
- Counterargument considered: row count times width works for rectangular input;
  CSV/TSV are not required to be rectangular, and ordinary ragged inputs expose
  both wrong decisions without exceptional file size or hostile data.
- Minimal fix: accumulate each row's actual cell count before accepting it and
  compare the running total with the same configured bound.
- Meaningful regression: both ragged fixtures above, exact-bound cases, and TSV
  equivalents; verify neither false admission nor false rejection.

### B9 — P1: both derived-evidence constructors duplicate a keyword argument

- Location: `src/apps/evidence/tasks.py:887`–`892` always includes
  `manual_review_required` in `_rights()`; document construction supplies it again
  at `1750` before `**rights` at `1755`, and generic construction at `2664` before
  `**rights` at `2667`.
- Trigger: any real document/generic extractor result requiring new EvidenceAsset
  construction with the real rights helper.
- Expected: one merged rights/review material envelope is passed to construction,
  preserving both confidence/manual-review requirements and source rights.
- Actual: Python raises `TypeError: EvidenceAsset() got multiple values for keyword
  argument 'manual_review_required'` before object construction. Real native-PDF
  and generic records cannot reach persistence on these paths. Tests replacing
  `_rights` with dictionaries omitting this key do not exercise the actual caller.
- Evidence: real `_rights()` with frozen MOTIR rights policy plus the real model
  constructor reproduces the exact exception without storage/network/DB writes.
- Counterargument considered: dictionary unpacking overrides earlier dictionary
  entries, but a named function keyword plus the same unpacked keyword is an
  error, not an override.
- Minimal fix: construct one combined keyword dictionary, preserving the logical
  OR of extraction review requirements and policy review requirements. Verify
  attachment-derived rights inheritance while composing it; the current default
  record-scope lookup must not promote internally-only attachment evidence.
- Meaningful regression: real `_rights` through real generic and native document
  evidence materialization, with both policy-publishable and internally-only
  document attachment policies; verify evidence, manifests, bound object ledger
  and ready events, and retained manual/rights restrictions.

## Rejected hypotheses and unresolved traces

- **Rejected:** URL query strings corrupt the generic worker's local filename
  suffix. `_persist_attachment` applies `redact_url`, and the implementation at
  `http_safety.py:74`–`88` removes queries before storing `source_url`. The initial
  raw synthetic probe was unreachable from that producer; the corrected probe
  uses actual redaction and successfully extracts `.xlsx`. This is not B3.
- **Unverified:** extensionless official MOTIR downloads lose filename/type
  context. Its adapter preserves a filename in attachment title, whereas download
  naming and HWPX routing depend on the URL's path suffix. Need a full valid HWPX
  attachment path test; preserve the separate legacy-HWP hard-false gate.
- **Unverified:** ordinary editorial quality failure sets the origin run to
  `failed/manual_required` without the usual queue-one release hook, and manual
  editing rejects failed origin runs. Need an isolated scheduled-run/blocked-draft
  integration proof before treating this as an actionable finding.
- **Unverified:** corrected/restored observations and historical SourceItem
  `supersedes` existence may disagree with current editorial source eligibility.
  Need complete source-observation lineage fixtures through generation/publication.
- **Conditional, reproduced second-pass trace:** after a diagnostic-only plain
  string conversion of the B7 enum, a newer delivery terminalizes an attempt as
  failed while the older collector is inside its adapter call. When the older
  result resumes, the current success transaction checks only for an existing
  succeeded state, overwrites failed with succeeded, persists one RunSourceItem
  and emits one change event; delivery-2's immutable observation remains failed.
  `collection-stale-probe.json` records this exact migrated SQLite interleaving.
  The diagnostic wrapper, not product source, masks B7. Review receipt-generation
  claim/result/failure fencing before distributed acceptance; PostgreSQL parallel
  execution and the complete outbox reclaim path were not exercised here.
- **Conditional rights trace:** document/generic materialization calls `_rights`
  with its default record scope although downloaded attachment inputs carry
  documentAttachment/mediaAttachment rights. Source registries explicitly grant
  record attribution while restricting document attachments to internal analysis.
  After B9 is repaired, a real derived-evidence regression must prove rights never
  broaden from the input. No published rights-escalation incident is claimed.
- **Intentional calibration boundary:** current Paddle profiles have no calibrated
  material, while the producer emits numeric confidence and model validation
  rejects uncalibrated numeric values. The extraction contract explicitly requires
  approved calibration. Preserve that rejection and establish admitted calibration
  wiring/corpus evidence rather than removing this release boundary.

## Verification and limits

Executed diagnostic commands use the existing Python 3.12 virtual environment,
`WISDOME_ENVIRONMENT=development`, `WISDOME_RUNTIME_MODE=local`, `PYTHONUTF8=1`, and
remove inherited local root overrides with `os.environ.pop`. Commands so far:

| Command | Actual result |
| --- | --- |
| `python extractor_probes.py` | Exit 0; incorrect HTML resolution, date canonicalization failure, real spreadsheet locator rejection; query hypothesis rejected after real URL redaction |
| `python editorial_raw_probe.py` | Exit 0; 9 passing gates, raw-locator high-risk gate fails |
| `python editorial_claim_probe.py` | Exit 0; contradictory price passes all 10 gates |
| `python csv_cells_probe.py` | Exit 0; 11 cells admitted and 7 cells rejected at a budget of 10 |
| `python collection_stale_probe.py`, first fixture | Exit 0; actual persistence error for zero attachments captured; no shared DB changes |
| `python collection_stale_probe.py`, attachment-bearing fixture | Exit 0; exact outbox change_kind enum error captured |
| `python collection_stale_probe.py`, diagnostic-only enum conversion | Exit 0; older result overwrites newer failed state and persists one change observation/event |
| Real rights helper/model constructor probe | Exit 1 with the expected duplicate-keyword TypeError; no DB/storage writes |

These are isolated reproductions, not new production incidents. No source network
request, publishing action, live OCR/HWP run, full test suite, or PostgreSQL race
test was executed. Real `.env` files were enumerated by name/existence only; no
credential content was read. No safety/release gate, task checkbox, source file,
test, canonical analysis or instruction file was changed by this reviewer.

Some independent defects mask later ones: B6/B7 prevent ordinary changed-item
collection, and B9 prevents derived construction before B3's locator validation.
The probes isolate each faulty boundary; they do not claim a complete new baseline
run, approved article or actual incorrect external publication.

The collection diagnostic migrates only its own in-memory SQLite database and
constructs frozen policy/source/membership material directly. It tests collection
business logic; it does not claim approved-source API admission or live collection.
The stale experiment masks B7 locally and uses deterministic interleaving, so its
scope is explicitly narrower than a real broker/lease-expiry acceptance test.

## Priorities and open questions

Fix producer/consumer mismatches with real-adapter integration tests first; B3 and
B4 stop ordinary supported service paths, B1 produces invalid evidence references,
B2 stops common date-bearing workbooks, and B5 admits unsupported factual values.
Keep source/profile approvals, publication revalidation and signed HWP acceptance
boundaries intact.

Product questions: which factual paraphrases must the deterministic MVP accept;
which canonical raw source-record locator envelope should be supported; should a
quality-blocked article remain editable while its run is terminal; and which
attachment formats/sources form the next distributed acceptance corpus?

## Scope and coverage appendix

The baseline inventory contains **125 assigned files / 46,522 lines**. Full baseline product/config text was inspected for **100 files / 37,091 lines**. The **25 migration files / 9,431 lines** received metadata, function/dependency/operation and representative DDL sampling; their entire backfill/DDL bodies were not read. Empty `__init__.py` files are included. No assigned non-migration baseline product/config file remains unvisited.

Tool output truncation was followed by targeted rereads of omitted product chunks; the first raw extractor/generator reads preceded the numbered coverage helper. Inventory and line counts are anchored to HEAD, not concurrently added fixes. Main-session changes are visible in the shared workspace; this reviewer did not claim their correctness from the original scan.

| Scope | Files | Baseline lines | Full text | Sampled migrations |
| --- | ---: | ---: | ---: | ---: |
| `src/apps/topics` | 17 | 6,968 | 13 | 4 |
| `src/apps/collection` | 21 | 7,413 | 10 | 11 |
| `src/apps/evidence` | 23 | 12,859 | 17 | 6 |
| `src/apps/editorial` | 18 | 10,101 | 14 | 4 |
| `src/adapters/sources/housing` | 4 | 2,029 | 4 | 0 |
| `src/adapters/sources/semiconductor` | 3 | 3,128 | 3 | 0 |
| `src/adapters/extractors` | 13 | 3,071 | 13 | 0 |
| `src/adapters/generators` | 3 | 203 | 3 | 0 |
| `config/source-registry` | 2 | 407 | 2 | 0 |
| `config/extraction-profiles` | 19 | 299 | 19 | 0 |
| `config/editorial-policies` | 2 | 44 | 2 | 0 |

### Complete assigned baseline file inventory

Status `F` means full product/config text; `M` means explicitly sampled migration.

```text
F config/editorial-policies/housing_subscription.json (22 lines)
F config/editorial-policies/semiconductor_news.json (22 lines)
F config/extraction-profiles/generic/browser-capture-deterministic-v1.json (8 lines)
F config/extraction-profiles/generic/html-deterministic-v1.1.json (7 lines)
F config/extraction-profiles/generic/html-deterministic-v1.json (8 lines)
F config/extraction-profiles/generic/hwpx-deterministic-v1.1.json (14 lines)
F config/extraction-profiles/generic/hwpx-deterministic-v1.json (8 lines)
F config/extraction-profiles/generic/legacy-hwp-v1.json (49 lines)
F config/extraction-profiles/generic/manual-entry-v1.json (8 lines)
F config/extraction-profiles/generic/media-deterministic-v1.json (8 lines)
F config/extraction-profiles/generic/native-pdf-v1.1.json (14 lines)
F config/extraction-profiles/generic/native-pdf-v1.json (18 lines)
F config/extraction-profiles/generic/spreadsheet-deterministic-v1.1.json (7 lines)
F config/extraction-profiles/generic/spreadsheet-deterministic-v1.json (8 lines)
F config/extraction-profiles/generic/structured-deterministic-v1.1.json (12 lines)
F config/extraction-profiles/generic/structured-deterministic-v1.json (8 lines)
F config/extraction-profiles/manifest.json (14 lines)
F config/extraction-profiles/paddleocr/paddle-en-v1.1.json (25 lines)
F config/extraction-profiles/paddleocr/paddle-en-v1.json (29 lines)
F config/extraction-profiles/paddleocr/paddle-ko-v1.1.json (25 lines)
F config/extraction-profiles/paddleocr/paddle-ko-v1.json (29 lines)
F config/source-registry/housing_subscription.json (223 lines)
F config/source-registry/semiconductor_news.json (184 lines)
F src/adapters/extractors/__init__.py (4 lines)
F src/adapters/extractors/base.py (195 lines)
F src/adapters/extractors/browser_capture.py (55 lines)
F src/adapters/extractors/html.py (81 lines)
F src/adapters/extractors/hwpx.py (753 lines)
F src/adapters/extractors/legacy_hwp.py (684 lines)
F src/adapters/extractors/media.py (102 lines)
F src/adapters/extractors/native_pdf/__init__.py (4 lines)
F src/adapters/extractors/native_pdf/adapter.py (233 lines)
F src/adapters/extractors/paddleocr/__init__.py (4 lines)
F src/adapters/extractors/paddleocr/adapter.py (609 lines)
F src/adapters/extractors/spreadsheet.py (116 lines)
F src/adapters/extractors/structured.py (231 lines)
F src/adapters/generators/__init__.py (3 lines)
F src/adapters/generators/base.py (54 lines)
F src/adapters/generators/template.py (146 lines)
F src/adapters/sources/housing/__init__.py (5 lines)
F src/adapters/sources/housing/applyhome.py (475 lines)
F src/adapters/sources/housing/common.py (857 lines)
F src/adapters/sources/housing/lh.py (692 lines)
F src/adapters/sources/semiconductor/__init__.py (24 lines)
F src/adapters/sources/semiconductor/adapters.py (2274 lines)
F src/adapters/sources/semiconductor/common.py (830 lines)
F src/apps/collection/__init__.py (1 lines)
F src/apps/collection/admin.py (35 lines)
F src/apps/collection/api.py (435 lines)
F src/apps/collection/apps.py (7 lines)
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
F src/apps/collection/migrations/__init__.py (0 lines)
F src/apps/collection/models.py (776 lines)
F src/apps/collection/services.py (2678 lines)
F src/apps/collection/tasks.py (335 lines)
F src/apps/collection/urls.py (10 lines)
F src/apps/collection/views.py (7 lines)
F src/apps/editorial/__init__.py (1 lines)
F src/apps/editorial/admin.py (34 lines)
F src/apps/editorial/api.py (858 lines)
F src/apps/editorial/apps.py (7 lines)
F src/apps/editorial/clustering.py (1013 lines)
F src/apps/editorial/corrections.py (524 lines)
M src/apps/editorial/migrations/0001_initial.py (172 lines)
M src/apps/editorial/migrations/0002_event_cluster_verification.py (427 lines)
M src/apps/editorial/migrations/0003_editorial_policy_runtime.py (1039 lines)
M src/apps/editorial/migrations/0004_correction_decision.py (410 lines)
F src/apps/editorial/migrations/__init__.py (0 lines)
F src/apps/editorial/models.py (874 lines)
F src/apps/editorial/policies.py (343 lines)
F src/apps/editorial/quality.py (586 lines)
F src/apps/editorial/services.py (2823 lines)
F src/apps/editorial/tasks.py (963 lines)
F src/apps/editorial/urls.py (20 lines)
F src/apps/editorial/views.py (7 lines)
F src/apps/evidence/__init__.py (2 lines)
F src/apps/evidence/admin.py (65 lines)
F src/apps/evidence/api.py (498 lines)
F src/apps/evidence/apps.py (9 lines)
F src/apps/evidence/management/__init__.py (1 lines)
F src/apps/evidence/management/commands/__init__.py (1 lines)
F src/apps/evidence/management/commands/import_extraction_profiles.py (47 lines)
F src/apps/evidence/management/commands/verify_extraction_profile_files.py (124 lines)
F src/apps/evidence/management/commands/verify_extraction_profile_snapshots.py (27 lines)
F src/apps/evidence/management/commands/verify_extraction_profiles.py (18 lines)
F src/apps/evidence/management/commands/verify_ocr_manifest.py (28 lines)
M src/apps/evidence/migrations/0001_initial.py (359 lines)
M src/apps/evidence/migrations/0002_documentextraction_input_fingerprint.py (130 lines)
M src/apps/evidence/migrations/0003_evidenceasset_raw_input_fingerprint.py (352 lines)
M src/apps/evidence/migrations/0004_extractionprofiledecision_report_envelope.py (84 lines)
M src/apps/evidence/migrations/0005_extraction_generation_fencing.py (1111 lines)
M src/apps/evidence/migrations/0006_generic_evidence_manifest.py (765 lines)
F src/apps/evidence/migrations/__init__.py (1 lines)
F src/apps/evidence/models.py (1195 lines)
F src/apps/evidence/profiles.py (609 lines)
F src/apps/evidence/services.py (2530 lines)
F src/apps/evidence/tasks.py (4872 lines)
F src/apps/evidence/urls.py (31 lines)
F src/apps/topics/__init__.py (1 lines)
F src/apps/topics/admin.py (41 lines)
F src/apps/topics/api.py (397 lines)
F src/apps/topics/apps.py (7 lines)
F src/apps/topics/management/__init__.py (0 lines)
F src/apps/topics/management/commands/__init__.py (0 lines)
F src/apps/topics/management/commands/seed_source_registry.py (43 lines)
F src/apps/topics/management/commands/verify_source_registry_snapshots.py (211 lines)
M src/apps/topics/migrations/0001_initial.py (118 lines)
M src/apps/topics/migrations/0002_source_registry_contract.py (1165 lines)
M src/apps/topics/migrations/0003_freeze_legacy_source_execution_material.py (145 lines)
M src/apps/topics/migrations/0004_semiconductor_source_choices.py (25 lines)
F src/apps/topics/migrations/__init__.py (0 lines)
F src/apps/topics/models.py (899 lines)
F src/apps/topics/services.py (3697 lines)
F src/apps/topics/tasks.py (180 lines)
F src/apps/topics/urls.py (39 lines)
```

### Tests, fixtures and contract reading limits

- Tests were inspected selectively, including real generic-manifest test material/rights stubs (`test_extraction_generation_fencing.py:2437-2559`, `1560-1614`), locator/calibration model tests (`test_extraction_safety_boundaries.py:908-985`), editorial claim/span fixtures and rejection test (`test_editorial_policy.py:90-146`, `559-583`), shared fixture setup, and function/test inventories. Remaining bodies of those modules and the relevant run-control, editorial API/admin/publication/correction, profile/OCR/HWP, source HTML/API and scheduling test modules were not fully read or executed by this reviewer.
- Baseline `git grep collect_source_attempt HEAD -- tests` found no direct worker tests. The generic-manifest test patches `_generic_extractor` and `_rights`; its rights stub at lines 2507-2511 omits the actual helper's manual-review key. This is specific test-gap evidence, not a claim that every existing test is invalid.
- All seven ApplyHome/LH fixture files were read fully; the 18 files under `tests/fixtures/live-regressions/` were enumerated but not read fully. The local-content fixture manifest/README and local integration workflow are outside this reviewer's operational path and were not inspected as a complete acceptance corpus.
- Generic-extractor contract was substantially read (initial engine/locator/event section plus safety/HWP/verification sections); document-extractor input/profile and safety/calibration clauses were read. Job-events source-change lineage, source collection, verified clustering, editorial policy/revalidation, extraction/redelivery and fencing clauses were read/search-cross-checked. Unrelated publishing/scheduling clauses and the entire OpenAPI were not line-by-line reviewed here. Contract reads are partial, and no route-conformance acceptance claim is made.
- Cross-read source adapter base/interface, outbox enqueue/schema/consumer-lease/handler boundaries and selected scheduling queue-release code. Other publishing/outbox/scheduling implementation belongs to the main session/other reviewers; it was not exhaustively audited here.
- Generated migration backfill logic, reverse migration behavior and PostgreSQL-only guards remain explicit review/execution limits. Private fresh SQLite migration success does not prove them on PostgreSQL or populated legacy data.

Read-range diagnostics, exact baseline path/hash inventory and reproduction JSON remain under `.local/reviews/round1-pipeline-integrity/`. Durable findings and these limits are in this Markdown report. All substantive fixes, new release versions and canonical report updates belong to the main session.

## Update log

- 2026-10-03: baseline/policy/canonical reports read; real extractor/editorial
  reproductions established B1–B5; query-suffix hypothesis rejected by caller trace.
- 2026-10-03: full collection/materialization traces established B6–B9; conditional
  stale-result and rights/calibration follow-up evidence preserved separately.
