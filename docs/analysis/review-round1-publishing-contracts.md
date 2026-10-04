# Round 1: publishing, scheduling, administrator UI and contracts

## Baseline and scope

- Review date: 2026-10-03, Asia/Seoul.
- Reviewer: C, delegated read-only review for the main session.
- Source baseline: `main`, `7eb33310665dfffafa15561d66d6d1a2d4b48e6b`.
- Starting references: project `AGENTS.md`, shared analysis policy,
  `docs/code-analysis.md`, and `docs/service-analysis.md`.
- Initial working tree: modified `README.md`; untracked `AGENTS.md`, `CLAUDE.md`,
  and the two canonical analysis reports. Those were preserved.
- Product-source findings below name the original baseline. Main-session edits
  began during this review; notably C1/C2/C4 were already being repaired when the
  final OpenAPI read occurred. This report is evidence for revalidation, not a
  claim that the current working tree still contains every finding.
- Allowed writes by this reviewer: this supplemental report and diagnostics in
  `.local/reviews/round1-publishing-contracts/`. No application, test, instruction,
  dependency, canonical report, or remote publisher changes were made here.

The service collects evidence, prepares immutable article revisions and approvals,
and dispatches WordPress canonical publications followed by Blogger distribution.
Targets, current heads, immutable dispatch cohorts, domain execution generations,
consumer receipt leases, media operations and reconciliation observations form
separate authorization and replay boundaries. Schedules freeze server-owned tick
material and queue-one execution material. The console calls the same validated
administrator APIs.

Complete source-text reads covered the current publishing/scheduling modules,
all assigned console assets/templates, and both assigned contracts. Migration and
test depth varies and is recorded in the coverage appendix. This review does not
establish distributed release acceptance or real remote exactly-once behavior.

## Findings

### C6 — P1: real content previews cannot pass the approval hash check

- Evidence: `src/apps/publishing/services.py:1440`, `:1448`, `:7690`, `:7698`.
- Scenario: create a normal publication intent, obtain its actual generated preview,
  and approve it. `_create_preview_render` includes `correctionHistory` in both
  template and source hashes, including the empty list for ordinary articles.
  `_render_approval_material` recomputes both hashes without that key.
- Expected: the unchanged generated preview passes immutable material validation.
  Actual: `Conflict("approval render material is stale")`; new manual and automatic
  content approval is blocked before external publication.
- Reproduction: invoked the actual undecorated preview builder with only the
  persistence/cohort/source dependencies mocked, then the actual approval material
  checker. It rejected `correctionHistory=[]` with the above conflict.
- Correction: use a consistent, preferably shared/versioned hash construction.
  Bind nonempty correction history and preserve verifiable historical empty-history
  material without rewriting immutable approvals/renders. Do not remove the check.
- Regression: real preview-to-approval flow for ordinary and correction articles,
  plus historical empty-history replay and changed-history rejection.

### C9 — P1: an old disconnect event can revoke replacement credentials

- Evidence: `src/apps/publishing/services.py:12843`, `:12940`,
  `src/apps/publishing/tasks.py:326`, `src/apps/publishing/services.py:13031`.
- Scenario: disconnect credential v1, then reconnect or update the target to v2
  before the asynchronous old revocation event runs. `begin_target_credential_revoke`
  selects the current target, ignores the decision's original credential/snapshot
  identity, and creates a fence from the replacement state. The worker constructs
  the adapter from that current target and revokes v2. Result persistence accepts
  that newly captured fence and clears the new credential references.
- Expected: the v1 decision cannot authorize revoking a later connection.
  Actual: the old operation is rebound to the replacement connection.
- Reproduction: actual undecorated begin function, mocked ORM/provenance/audit only:
  `old_snapshot_matched=false`, `selected_new_credential=true`,
  `fence_follows_new_snapshot=true`. No credential values or network calls were used.
- Correction: bind revocation to immutable original credential material and/or
  prevent reconfiguration while revocation is active. The initial disconnect itself
  changes the snapshot, so comparison with the pre-disconnect snapshot alone is
  insufficient; compare the appropriate accepted/original credential identity.
- Regression: delayed disconnect versus reconnect/rotation, worker reclaim, and
  stale result settlement; replacement credential revocation must remain zero.

### C7 — P1: media preparation drops a requested future publication time

- Evidence: `src/apps/publishing/services.py:8580`, `:8615`, `:8660`, `:7524`.
- Scenario: dispatch an approved WordPress article with media and future `publishAt`.
  The media-pending attempt is excluded from the initial queue call. That queue call
  is the only place the date is stored on `Publication.scheduled_for`. Media success
  later calls `_queue_attempt_on_commit(attempt)` without the date.
- Expected: the accepted date survives media preparation and publication cannot
  start before it. Actual: the date is absent and the publication event is immediately
  available after media completion. WordPress's adapter uses `status=publish`.
- Evidence level: complete call-path inspection; no live or clock-controlled
  end-to-end scheduled publication was run by this reviewer.
- Correction: freeze the requested time before branching into media/dependency
  work, and reuse that exact time for every subsequent release/retry enqueue.
- Regression: delayed media completion before and after the future date; assert
  persisted schedule and event `not_before`, including Blogger dependency release.

### C8 — P1: available reused media leaves new bindings permanently unready

- Evidence: `src/apps/publishing/services.py:6615`, `:7020`, `:7034`, `:6725`;
  `src/apps/publishing/models.py:2305`.
- Scenario: a new publication/revision reuses an already available mapping.
  Binding creation defaults to `prepared` with no verification timestamp. The
  delivery planner skips available mappings without activating/verifying that
  binding or enqueuing a reconcile operation. The write gate requires `active`.
- Expected: exact verified reuse produces a ready binding or a recovery operation.
  Actual: no media operation exists to advance the prepared binding; publication
  readiness raises `publication media binding is not active and current`.
- Reproduction: a narrow real-SQLite diagnostic using existing asset/binding
  fixtures and actual prepare/planner/readiness services confirmed zero operations,
  prepared binding and absent `remote_verified_at`. Its passing assertions verify
  the broken state, not successful product publication.
- Correction: perform exact mapping verification and binding activation under the
  mapping lock, or enqueue read-only reconciliation. Preserve pending-delete,
  in-flight delete, generation and restored-reference protections.
- Regression: reuse on new revisions/articles for WordPress and public delivery,
  and races with orphan deletion. Existing tests manually activate bindings and
  therefore do not cover this transition.

### C3 — P1: one ineligible schedule aborts the entire due scan

- Evidence: `src/apps/scheduling/services.py:336`, `:929`, `:1104`;
  `src/apps/scheduling/tasks.py:18`.
- Scenario: the first due validated-auto schedule has revoked/stale material.
  The material builder raises before writing a dispatch/audit or advancing its due
  time. The scan's list comprehension propagates that exception and never attempts
  later healthy schedules. The same schedule remains due for the next scan.
- Expected: the affected schedule fails closed with a recorded hold while unrelated
  schedules can run. Actual: repeated scan failure can starve unrelated schedules.
- Reproduction: actual due-scan function with two mocked captured due ticks and an
  expected first-dispatch `ValueError`; observed dispatch calls contained only the
  invalid schedule. This is a control-flow reproduction, not a PostgreSQL race test.
- Counterargument checked: no intentional fail-all policy was found. `spec.md`
  SC-006 requires each active schedule to start or record a hold reason; source/
  publisher policy changes are ordinary operational failures at line 302.
- Correction: isolate typed expected material-ineligible outcomes per locked tick,
  preserving exact replay and audited hold/quarantine identity. Never label
  unapproved/incomplete inputs `schedule-dispatch-material-v1`, and do not turn
  unexpected invariant/database/programming failures into success.
- Regression: invalid and healthy schedules in one scan, repeated tick replay,
  remediation after rotation, and audit/next-run behavior under concurrency.

### C5 — P1: validation decisions consume the wrong reauthentication scope

- Evidence: `src/static/admin_console/publishing_target.js:83`,
  `src/apps/publishing/services.py:5155`, `src/apps/accounts/services.py:215`.
- Scenario: use the console's pass/revoke validation action. It issues a proof for
  `validation_decision`; the service consumes `auto_publish_change` instead.
- Expected: the action-specific proof is consumed. Actual: the account service
  raises `Forbidden("Reauthentication proof does not include this action scope")`.
- Reproduction: actual proof-consumption function with a valid synthetic bound,
  active, unexpired proof containing `validation_decision` rejected the requested
  `auto_publish_change` scope. No authentication secrets were read.
- Correction: use `validation_decision` for validation decisions; retain
  `auto_publish_change` for activation changes. Do not broaden proof scopes.
- Regression: real issued proof through real decision service, wrong-scope denial
  and exact replay. Existing server-material tests mostly mock the boundary.

### C1 — P1: overescaped cron schema rejects every normal schedule expression

- Original-baseline evidence:
  `specs/001-automated-content-publishing/contracts/admin-api.openapi.yaml:4579`,
  `:4629`.
- Scenario: submit the console default `0 */2 * * *` to create/update a schedule.
  The original single-quoted YAML contained doubled regex backslashes.
- Expected: five valid cron fields reach semantic cron validation.
  Actual: the real compiled schema rejected the default expression on `pattern`.
- Reproduction: actual `load_openapi_contract` and `_compile_schema` returned a
  pattern error for `ScheduleInput` and `SchedulePatch`.
- Correction/regression: correct YAML escaping and exercise actual default and
  representative valid/invalid five-field expressions. Main-session repair began
  during this review; no post-repair success claim is made here.

### C2 — P1: every existing schedule save sends a forbidden topic property

- Original-baseline evidence: `src/static/admin_console/operations.js:176`, `:200`;
  OpenAPI `SchedulePatch` at line 4622 and scheduling API mapping at line 117.
- Scenario: select any existing schedule and save any change. `scheduleBody`
  includes `topic` and the update path reuses that complete body.
- Expected: immutable topic is displayed and omitted from PATCH. Actual: closed
  `SchedulePatch` rejects `topic`, independently of C1.
- Reproduction: real compiled PATCH schema returned `additionalProperties` for the
  exact console-shaped body. Source updates deliberately omit topic.
- Correction/regression: separate create/update payloads and disable immutable
  topic editing on existing schedules; execute the actual JS submit body in a test.

### C4 — P2: operations reauthentication cannot supply required MFA

- Original-baseline evidence: `src/static/admin_console/operations.js:61`;
  `src/apps/accounts/services.py:65`, `:97`.
- Scenario: enable configured MFA-required reauthentication and use kill-switch
  disable, stop/retry, correction verification or retention execution.
- Expected: the operator can submit password and MFA. Actual: the operations
  helper never collects/sends `mfaCode`; proof issuance fails and repeated clicks
  consume the account's failure/lockout budget. Other console modules collect MFA.
- Evidence level: source-traced configuration-dependent defect; no real MFA flow.
- Correction/regression: collect/send the optional MFA code consistently and test
  required-MFA success, cancellation and authentication failure handling.

### C10 — P2: publication-target responses violate their declared schema

- Evidence: `src/apps/publishing/api.py:185`, `:192`; OpenAPI lines 4900 and 4913.
- Actual `target_json` emits stored snake-case `mark_withdrawn`/`media_upload`, but
  the response schema requires `markWithdrawn`/`mediaUpload`. A fresh target emits
  activation version `0`, whereas the schema allows positive integers or null.
- Reproduction: actual serializer and compiled `PublicationTarget` schema returned
  those three errors for an ordinary snapshot-bound new target.
- Correction/regression: explicitly map public capability names and the no-activation
  value, preserving internal command names; validate actual endpoint responses.

### C11 — P2: validation reports are incomplete and misstate draft test results

- Evidence: `src/apps/publishing/api.py:615`; OpenAPI `VerificationReport:2466`.
- Actual view omits `reportObjectKey`, `sampleManifestHash`, `generatedAt`, emits
  `{}` for required array metrics/thresholds, and empty samples despite `minItems=1`.
  It derives `overallResult` from the approval projection, so a draft candidate with
  every canary stage passed is displayed as failed before the administrator decides.
- Reproduction: actual unwrapped report view, synthetic server-owned canary material,
  and actual compiled response schema returned six violations; all emitted stages
  passed while `overallResult` was failed.
- Correction: version/align the report contract with real server v2 canary/pilot
  evidence, expose its truthful provenance and actual test result, and distinguish
  approval state separately. Do not fabricate corpus samples, metrics or thresholds.
- Regression: actual report response for draft/passed/revoked/stale candidates and
  missing/hash-mismatched immutable evidence.

### C12 — P2: the console cannot approve a renderless unpublish intent

- Evidence: `src/apps/publishing/services.py:5786`, `:7750`, `:7993`, `:8052`;
  `src/static/admin_console/publishing_article.js:156`, `:173`, `:305`.
- Unpublish intentionally has no content preview. The JS nevertheless requests
  previews for every target with one `Promise.all`, aborting material loading when
  an unpublish target has no render. Its subject builder always uses `content_preview`
  and obtains no `unpublish` proof when approving. Operations also offers no action
  calling the implemented correction prepare-publication endpoint after verification.
- Expected: review frozen remote identity/state/correction material, issue the
  scoped proof, approve and dispatch the supported withdrawal action.
- Evidence level: complete source/contract call tracing; no browser acceptance run.
- Correction/regression: add action-specific unpublish review/approval and the
  verified-correction preparation path, preserving renderless and reauth guards.

### C13 — P2: new preflight requests reuse a completed old job

- Evidence: `src/apps/publishing/services.py:3919`, `:3991`, `:4145`, `:3608`;
  outbox comparison/return behavior at `src/wisdome_writer/infrastructure/outbox.py:549`.
- A fresh request key on unchanged target material uses only target ID + snapshot
  version as its event dedupe key. Successful repeated preflight can reuse the
  same operational snapshot because `last_preflight_at` is excluded from its
  material hash. Later requests return the old event whose completed consumer/result
  audit is replayed; no new remote connection check occurs. Replaying an earlier
  same request key after a snapshot change also hashes mutable current snapshot
  material and can conflict instead of returning its original accepted job.
- Reproduction: actual undecorated request function with only ORM/audit/enqueue
  dependencies mocked emitted identical event keys for two distinct request keys;
  actual outbox source confirms existing completed events are returned.
- Correction/regression: distinguish new operational checks by immutable request
  identity and resolve exact replay from original acceptance before mutable state.
  Test a completed unchanged-state check followed by a fresh key, and old replay
  after snapshot advancement. Retain worker snapshot/lease/result fences.

## Media interaction requiring second-pass confirmation

### C14 — source-traced media placement gap, pending an executed publication fixture

- Evidence: `src/apps/publishing/services.py:7682`, `:9134`, `:9221`;
  `src/adapters/publishers/wordpress/client.py:146`, `:164`;
  `src/adapters/publishers/blogger/client.py:158`, `:179`.
- Approved visual placements create delivery manifests and upload operations. The
  preview builder emits only Markdown HTML; final rendering only replaces the
  canonical WordPress URL. Neither inserts approved media placement placeholders
  or resolves their URLs into body HTML. Both adapters send only `body_html` and
  ignore `RenderedArticle.media`; WordPress also omits `featured_media`.
- Expected: approved images/figures and presentation material appear in the exact
  final post and its content hash. Actual: assets can be uploaded/protected and
  carried as DTO metadata while the article body contains no corresponding visuals.
- Evidence level: complete service rendering path and targeted adapter execution
  reads; no actual final-render/publication fixture or real remote post was executed
  for this interaction. It is separated from the reproduced actionable blockers
  above for main-session revalidation before remediation scope is chosen.
- Correction/regression: freeze placement placeholders in preview and bind exact
  approved delivery URLs/presentation in final HTML before computing content hash
  and transport marker. Verify exact WordPress/Blogger payloads, captions/alt text,
  no duplicate insertion, and that unapproved body changes still require approval.

## Additional observations and limits

- Blogger media operations enqueue before the canonical attempt succeeds, but their
  pre-write gate still requires that success. After media settlement the publication
  attempt is enqueued unconditionally; a not-yet-ready dependency is classified stale
  by `_begin_attempt_locked`. Main should revalidate this ordering alongside C7/C8
  with a real staged publication test. It is source-traced here, not an independently
  executed distributed incident.
- Invalid schedule timezone reaches `ZoneInfo` before schedule save and raises
  `ZoneInfoNotFoundError`, which is a `KeyError`, outside the API's `ValueError`
  translation. A narrow actual `calculate_next_run` diagnostic confirmed this.
  Mutation rollback remains intact; validate calendar settings at the API/service
  boundary and return an input problem rather than an internal error.
- OpenAPI `TargetCommand` line 2164 still says Blogger unpublish dependency is null,
  and PublishRequest lines 4415-4417 describe an old standalone-Blogger exception.
  Current T024 code/adapter contract instead requires the exact same-cohort WordPress
  dependency for every Blogger action. Align the wording with the stronger current
  invariant; these are not reasons to remove dependency guards.
- The publishing article evidence card passes the structured `locator` object to
  `safeHttpUrl` instead of the contract's `sourceUrl` string; its evidence links
  cannot render. The article detail module already uses `sourceUrl`.

## Actual verification

| Check | Result |
| --- | --- |
| Baseline/status | HEAD stayed `7eb3331`; initial unrelated docs preserved |
| Real compiled schedule schemas | Reproduced C1 pattern and C2 extra-property failures |
| Actual due-scan control flow with isolated mocks | Reproduced first-schedule abort and absent healthy dispatch |
| Actual timezone calculation | `ZoneInfoNotFoundError`, not `ValueError` |
| Actual proof consumption with synthetic valid proof | Reproduced wrong-scope `Forbidden` |
| Actual preview builder followed by approval material checker | Reproduced hash conflict with empty correction history |
| Actual credential-revoke begin path with isolated mocks | Selected replacement credential/fence despite old decision identity |
| Actual target serializer + compiled response schema | Three schema violations |
| Actual report view + compiled response schema | Six schema violations and draft/result misclassification |
| Narrow SQLite media reuse diagnostic | `5 passed in 20.69s`; observed broken state asserted, plus four imported fixture tests |

The narrow pytest command was
`python -m pytest -q .local/reviews/round1-publishing-contracts/test_reuse_diagnostic.py --basetemp=.local/reviews/round1-publishing-contracts/pytest-reuse`.
It ran through `.venv/Scripts/python.exe` in an explicit development/local child
environment with `PYTHONUTF8=1`; inherited local-root override variables were
removed with `environment.pop`, not replaced with empty strings.

No full suite, service start, publisher/OAuth network operation, real secret read,
package install, PostgreSQL execution, broker delivery, S3 integration, or visual
browser acceptance was performed by this reviewer. SQLite fixture behavior cannot
prove PostgreSQL lock ordering. Historical August acceptance is not a new live run.
The HWP signed-release admission boundary remains intentionally closed and is
outside these corrective findings.

## Coverage appendix

The per-file appendix below records complete text, structural review and sampled
ranges separately. Automated AST inventory is not line-by-line semantic review.
Diagnostic read logs are local aids; this report is the durable handoff. All
unread test/migration bodies remain explicit review limits for subsequent rounds.

### Assigned product and contract files

| File | Baseline lines | Review depth |
| --- | ---: | --- |
| `specs/001-automated-content-publishing/contracts/admin-api.openapi.yaml` | 5216 | Complete source text |
| `specs/001-automated-content-publishing/contracts/publisher-adapter.md` | 380 | Complete source text |
| `src/adapters/__init__.py` | 2 | Complete source text |
| `src/apps/__init__.py` | 2 | Complete source text |
| `src/apps/publishing/__init__.py` | 2 | Complete source text |
| `src/apps/publishing/admin.py` | 100 | Complete source text |
| `src/apps/publishing/api.py` | 982 | Complete source text |
| `src/apps/publishing/apps.py` | 62 | Complete source text |
| `src/apps/publishing/automation.py` | 667 | Complete source text |
| `src/apps/publishing/console_urls.py` | 11 | Complete source text |
| `src/apps/publishing/contracts.py` | 193 | Complete source text |
| `src/apps/publishing/corrections.py` | 447 | Complete source text |
| `src/apps/publishing/media_delivery.py` | 241 | Complete source text |
| `src/apps/publishing/migrations/0001_initial.py` | 511 | Complete source text |
| `src/apps/publishing/migrations/0002_approval_request_hash.py` | 15 | Complete source text |
| `src/apps/publishing/migrations/0003_publicationattempt_reconcile_attempt_no.py` | 15 | Complete source text |
| `src/apps/publishing/migrations/0004_publicationreconcilegeneration.py` | 291 | Complete source text |
| `src/apps/publishing/migrations/0005_repair_ambiguous_reconcile_generations.py` | 607 | Complete source text |
| `src/apps/publishing/migrations/0006_restore_legacy_terminal_projection.py` | 177 | Complete source text |
| `src/apps/publishing/migrations/0007_publication_execution_observations.py` | 629 | Complete source text |
| `src/apps/publishing/migrations/0008_approval_head_and_target_intent_fence.py` | 398 | Complete source text |
| `src/apps/publishing/migrations/0009_approval_decision_integrity.py` | 1258 | Structural AST/operation/SQL inventory; complete-text samples: 210-274; other bodies/SQL unread |
| `src/apps/publishing/migrations/0010_intent_dispatch_identity.py` | 1321 | Structural AST/operation/SQL inventory; complete-text samples: none; other bodies/SQL unread |
| `src/apps/publishing/migrations/0011_publication_attempt_fencing.py` | 2443 | Structural AST/operation/SQL inventory; complete-text samples: none; other bodies/SQL unread |
| `src/apps/publishing/migrations/0012_published_asset_snapshots.py` | 1385 | Structural AST/operation/SQL inventory; complete-text samples: 172-409; other bodies/SQL unread |
| `src/apps/publishing/migrations/0013_publisher_credentials.py` | 87 | Complete source text |
| `src/apps/publishing/migrations/0014_publication_dependency.py` | 389 | Complete source text |
| `src/apps/publishing/migrations/0015_auto_publish_server_material.py` | 86 | Complete source text |
| `src/apps/publishing/migrations/__init__.py` | 0 | Empty module verified |
| `src/apps/publishing/models.py` | 2592 | Complete source text |
| `src/apps/publishing/services.py` | 13208 | Complete source text |
| `src/apps/publishing/tasks.py` | 808 | Complete source text |
| `src/apps/publishing/urls.py` | 64 | Complete source text |
| `src/apps/scheduling/__init__.py` | 1 | Complete source text |
| `src/apps/scheduling/admin.py` | 23 | Complete source text |
| `src/apps/scheduling/api.py` | 195 | Complete source text |
| `src/apps/scheduling/apps.py` | 7 | Complete source text |
| `src/apps/scheduling/controls.py` | 18 | Complete source text |
| `src/apps/scheduling/migrations/0001_initial.py` | 83 | Complete source text |
| `src/apps/scheduling/migrations/0002_schedule_auto_publish_activation_refs_and_more.py` | 23 | Complete source text |
| `src/apps/scheduling/migrations/0003_schedule_dispatch_material.py` | 266 | Complete source text |
| `src/apps/scheduling/migrations/__init__.py` | 0 | Empty module verified |
| `src/apps/scheduling/models.py` | 375 | Complete source text |
| `src/apps/scheduling/services.py` | 1457 | Complete source text |
| `src/apps/scheduling/tasks.py` | 35 | Complete source text |
| `src/apps/scheduling/urls.py` | 9 | Complete source text |
| `src/apps/scheduling/views.py` | 7 | Complete source text |
| `src/manage.py` | 15 | Complete source text |
| `src/static/admin_console/app.css` | 8 | Complete source text |
| `src/static/admin_console/articles.js` | 283 | Complete source text |
| `src/static/admin_console/operations.js` | 505 | Complete source text |
| `src/static/admin_console/publishing_article.js` | 474 | Complete source text |
| `src/static/admin_console/publishing_target.js` | 284 | Complete source text |
| `src/static/admin_console/run_detail.js` | 7 | Complete source text |
| `src/static/admin_console/runs.js` | 4 | Complete source text |
| `src/static/admin_console/sources.js` | 979 | Complete source text |
| `src/static/admin_console/url_safety.js` | 17 | Complete source text |
| `src/templates/admin_console/articles.html` | 18 | Complete source text |
| `src/templates/admin_console/base.html` | 29 | Complete source text |
| `src/templates/admin_console/index.html` | 14 | Complete source text |
| `src/templates/admin_console/operations/index.html` | 123 | Complete source text |
| `src/templates/admin_console/publishing/article_publish.html` | 61 | Complete source text |
| `src/templates/admin_console/publishing/index.html` | 106 | Complete source text |
| `src/templates/admin_console/publishing/target_detail.html` | 73 | Complete source text |
| `src/templates/admin_console/run_detail.html` | 19 | Complete source text |
| `src/templates/admin_console/runs.html` | 16 | Complete source text |
| `src/templates/admin_console/sources/index.html` | 194 | Complete source text |

### Relevant test files

Test-name/function-range inventories were inspected for every file below. Body
inspection was focused; the rest of each body is explicitly unread. New tests
added by the main remediation session are outside this original-baseline review.

| File | Baseline lines | Complete-text body ranges inspected |
| --- | ---: | --- |
| `tests/unit/test_auto_publish_server_material.py` | 454 | 1-260; remaining bodies unread |
| `tests/unit/test_editorial_admin_runtime.py` | 1041 | none; remaining bodies unread |
| `tests/unit/test_editorial_publication_eligibility.py` | 420 | none; remaining bodies unread |
| `tests/unit/test_local_publishing_policy.py` | 90 | none; remaining bodies unread |
| `tests/unit/test_media_delivery_operations.py` | 443 | 1-185; remaining bodies unread |
| `tests/unit/test_media_delivery_operations_db.py` | 74 | none; remaining bodies unread |
| `tests/unit/test_operations_admin_contract.py` | 426 | 1-180; remaining bodies unread |
| `tests/unit/test_outbox_terminal_reservation.py` | 108 | none; remaining bodies unread |
| `tests/unit/test_public_delivery_assets.py` | 199 | none; remaining bodies unread |
| `tests/unit/test_publication_approval_contract.py` | 420 | none; remaining bodies unread |
| `tests/unit/test_publication_approval_db.py` | 1346 | 256-375; remaining bodies unread |
| `tests/unit/test_publication_approval_service.py` | 1948 | none; remaining bodies unread |
| `tests/unit/test_publication_attempt_begin_db.py` | 304 | none; remaining bodies unread |
| `tests/unit/test_publication_attempt_event_contract.py` | 192 | none; remaining bodies unread |
| `tests/unit/test_publication_attempt_fencing.py` | 697 | none; remaining bodies unread |
| `tests/unit/test_publication_attempt_fencing_db.py` | 1112 | none; remaining bodies unread |
| `tests/unit/test_publication_attempt_worker_fencing.py` | 870 | none; remaining bodies unread |
| `tests/unit/test_publication_dependency.py` | 331 | none; remaining bodies unread |
| `tests/unit/test_publication_dependency_db.py` | 101 | none; remaining bodies unread |
| `tests/unit/test_publication_intent_contract.py` | 612 | none; remaining bodies unread |
| `tests/unit/test_publication_intent_idempotency.py` | 1183 | none; remaining bodies unread |
| `tests/unit/test_publication_intent_idempotency_db.py` | 1032 | 1-190; remaining bodies unread |
| `tests/unit/test_publication_intent_service_concurrency.py` | 258 | none; remaining bodies unread |
| `tests/unit/test_publication_media_bindings.py` | 279 | 1-279; remaining bodies unread |
| `tests/unit/test_publication_media_cleanup.py` | 164 | none; remaining bodies unread |
| `tests/unit/test_published_asset_snapshots.py` | 567 | none; remaining bodies unread |
| `tests/unit/test_published_asset_snapshots_db.py` | 118 | none; remaining bodies unread |
| `tests/unit/test_publisher_credentials.py` | 609 | 351-400; remaining bodies unread |
| `tests/unit/test_publishing_admin_contract.py` | 301 | none; remaining bodies unread |
| `tests/unit/test_publishing_automation_round1.py` | 408 | none; remaining bodies unread |
| `tests/unit/test_publishing_corrections_round2.py` | 189 | none; remaining bodies unread |
| `tests/unit/test_publishing_worker_audit_round2.py` | 50 | none; remaining bodies unread |
| `tests/unit/test_retention_dependency_graph.py` | 697 | none; remaining bodies unread |
| `tests/unit/test_schedule_dispatch_material.py` | 585 | none; remaining bodies unread |
| `tests/unit/test_wordpress_media_delivery.py` | 193 | none; remaining bodies unread |

### Cross-reads and diagnostic artifacts

- `src/apps/accounts/services.py`: complete proof issuance/consumption boundaries; only lines 154-177 were not fully inspected.
- `src/wisdome_writer/infrastructure/outbox.py`: 445-596, exact enqueue/dedupe return behavior.
- `src/wisdome_writer/infrastructure/event_routes.py`: 355-455 and targeted route searches; other routes outside this reviewer's complete-text scope.
- `src/wisdome_writer/api/openapi.py`: request validation/decorator portions around 757-1117 plus import/schema compilation use; substantial early body text was truncated and is not claimed fully reviewed.
- `src/adapters/publishers/wordpress/client.py`: 118-280; `src/adapters/publishers/blogger/client.py`: 123-247; targeted media/body searches. Complete adapters belong to the main/other review scope.
- `tests/unit/test_published_asset_snapshots.py` and `test_publication_approval_service.py`: fixture helper imports executed by the narrow diagnostic; complete body reading is not claimed.
- Diagnostic helper: `.local/reviews/round1-publishing-contracts/read_review.py`.
- Narrow database reproduction: `.local/reviews/round1-publishing-contracts/test_reuse_diagnostic.py`.
- Saved actual pytest output: `.local/reviews/round1-publishing-contracts/reuse-result.txt`.
- Local structured inventories: `migration-inventory.json`, `test-inventory.json`, `reads.json` in the same diagnostic directory. They are read aids, not substitute semantic review/acceptance.
- Other narrow probes were executed from bounded Python stdin scripts; exact inputs/results are summarized in the findings and verification table. No raw values from real secrets were printed.

## Update log

| Date | Update |
| --- | --- |
| 2026-10-03 | Original-baseline round-1 review, focused reproductions and read-only handoff to main remediation session |
