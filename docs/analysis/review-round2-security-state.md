# Round 2 reviewer D: publication and security state

## Baseline, purpose, and authority

- Date: 2026-10-03, Asia/Seoul.
- Baseline: `main`, `7eb33310665dfffafa15561d66d6d1a2d4b48e6b`.
- Starting authority: project [AGENTS.md](../../AGENTS.md), shared personal
  `C:/Users/강지혜/.agents/analysis-policy.md`, [code analysis](../code-analysis.md),
  [service analysis](../service-analysis.md), the [combined review](2026-10-03-two-round-review.md),
  and round-1 [publishing](review-round1-publishing-contracts.md) and
  [security/local](review-round1-security-local.md) reports.
- At start, the main session had repairs in collection, extractors, S3 cleanup,
  Compose, console scheduling/MFA, acceptance tools, WordPress self-revocation,
  scoped validation decisions/audit replay, and preview hashing. These changes,
  canonical reports, instructions and historical acceptance records were preserved.
- This is a supplemental, read-only reviewer report. Reviewer writes are confined
  to this file and `.local/reviews/round2-security-state/`. The main session owns
  product/tests/canonical-report/instruction changes and final adjudication.
- Finding line numbers name the post-round-1, pre-round-2-repair working source
  inspected here. Main-session repairs continued concurrently; the later recheck
  below records which conclusions were refreshed instead of presenting old defects
  as current.
- The main full suite was running independently. This reviewer did not execute a
  full suite, Windows lifecycle/port-3210 tests, services, real publisher operations,
  or read credentials. Current-source observations are separated from historical
  test evidence and from release acceptance.

The distributed service freezes editorial revisions, assets, target snapshots,
approvals and intent/dispatch cohorts before invoking WordPress and Blogger.
Asynchronous work is admitted through immutable outbox material and consumer
receipt capabilities; publication and media use separate generations and write
fences. Scheduling freezes server-owned tick material. The local writer deliberately
has external publishing disabled. The findings below concern distributed contracts,
not evidence that a distributed release is admitted.

## Findings and independently observed results

### D1 — P1, C6 residual: stored explicit-empty-history previews remain unapprovable

**Later status:** the main session repaired this during the review. An independent
latest-source recheck accepted both stored empty-history pairs, rejected mixed pairs
and a nonempty history using history-free hashes, accepted a bound nonempty history,
and rejected changed nonempty history. This confirms the hash-checker boundary;
full stored approval/replay and PostgreSQL acceptance remain separate work.

- Current source: `src/apps/publishing/services.py:1431-1487`, `:7695-7740`,
  `:7767-7770`; pre-repair producer at baseline `:7687-7708`.
- Trigger: an ordinary preview was already persisted by the baseline producer,
  whose template and source hashes both included `correctionHistory: []`. The
  repaired helper omits the key for an empty history, and the consumer recognizes
  only that omitted-key pair.
- Actual diagnostic: the real `_render_approval_material` accepted the original
  omitted-key pair, but rejected the baseline explicit-empty pair with
  `Conflict("approval render material is stale")`. The body, source links, revision,
  evidence hash and empty history were identical. Re-fetching a preview returns the
  stored row, so it does not repair an existing intent.
- Counterevidence: the repair correctly makes newly produced ordinary previews
  consistent with the consumer. Nonempty histories are included in both hashes.
  This is a compatibility residual, not a claim that the shared helper makes all
  new previews fail. Baseline explicit-empty previews normally could not be
  approved before the repair either; the repair still leaves those stored requests
  blocked instead of making unchanged material usable.
- Safe correction: recognize both complete historical hash pairs **only when the
  actual history is exactly an empty array**. Preserve the stored hashes in the
  approval subject/replay. Do not rewrite immutable approved/final renders,
  independently accept mixed pairs, or allow a nonempty history to use a legacy
  history-free pair.
- Regression: real stored preview-to-approval for both empty-history variants,
  nonempty history, mixed pairs, altered title/body/source/history, and exact
  approval replay. A producer-only or newly generated preview test misses this case.

### D2 — P1, C9 confirmed: delayed disconnect rebinds to replacement credentials

- Source: `src/apps/publishing/services.py:12860-12965`, `:12997-13060`,
  `:13153-13166`, `:3782-3913`, `:2207-2229`; worker
  `src/apps/publishing/tasks.py:316-354`.
- Trigger: accept disconnect for v1, replace the connection before its asynchronous
  worker begins, then deliver the original disconnect event. The begin service
  ignores the decision's expected original snapshot/credential identity. It selects
  the current target and constructs its fence from replacement state. The worker
  builds the adapter from that selected target.
- Actual mocked probe: `old_decision_snapshot_matches=false`,
  `selected_replacement_credential=true`, `selected_replacement_fence=true`.
- Stronger SQLite probe: issued and consumed a real `credential_disconnect` proof,
  called actual `disconnect_target`, then actual `update_target` with synthetic v2
  reference, then actual transactional begin service. Only worker event/audit
  provenance was mocked for that final begin. It selected v2 and its new snapshot.
  The result fence consequently permits clearing that replacement connection if
  revocation succeeds.
- Counterarguments: the result fence protects a change made **after** begin; it
  does not protect an old decision from being rebound at begin. The first disconnect
  itself changes the snapshot, so comparing only the pre-disconnect snapshot with
  the current snapshot would incorrectly block a legitimate v1 revocation. The
  existing active-write check covers publication attempts, not disconnect decisions.
- Safe correction: bind revocation to the original accepted credential/username/
  version identity, or serialize reconfiguration against active disconnects. Check
  the accepted revocation identity rather than treating any current target as its
  subject. Preserve unknown-outcome and stale-result handling; a retry must not
  switch to replacement credentials. Versioned secret-provider behavior needs its
  own acceptance; immutable reference strings alone do not prove secret bytes.
- Regression: actual disconnect then rotation/OAuth reconnection before delayed
  delivery; replacement remote revocations remain zero. Also cover legitimate
  pre/post-disconnect snapshots, worker reclaim, changes after begin and stale
  settlement. No real secret resolution or network revocation was performed here.

### D3 — P1, C7 confirmed: media preparation loses accepted future publication time

- Source: `src/apps/publishing/services.py:8597-8601`, `:8631-8639`,
  `:8675-8696`, `:7537-7547`, `:9293`; WordPress payload `:146-179` in
  `src/adapters/publishers/wordpress/client.py`.
- Trigger: accept a future `publishAt` for a WordPress content attempt with pending
  media. That attempt is excluded from the initial queue loop. The queue helper is
  the only place that stores the date on the publication. Media success calls it
  again without a date.
- Actual control-flow diagnostic executed the real dispatch branching and queue
  helper, with ORM and unrelated eligibility gates mocked. The no-media control
  persisted `2099-01-01T00:00:00+00:00` and enqueued with that `available_at`.
  The media-pending path persisted `scheduled_for=null`, created no initial
  publication event, and released with `available_at=null`. The command builder
  then reads the missing publication date. There is no later requested-date check
  in `validate_attempt_gate`.
- Counterargument: media can be prepared early without publishing early; the defect
  is loss of the accepted time at the subsequent publication release. A hash that
  incorporates `publishAt` does not provide recoverable frozen scheduling material.
- Safe correction: freeze the exact accepted date before media/dependency branching
  and use it for every initial/recovery/dependency release. Keep date material
  tied to the exact dispatch/attempt, not an unrelated mutable publication history.
- Regression: media completes before/after the date, no-media control, Blogger
  dependency release, duplicate/retry enqueue, and exact dispatch replay.
  No real clock-controlled broker/publisher execution was performed.

### D4 — P1, C8 confirmed on both channels: reused available mappings leave bindings prepared

- Source: `src/apps/publishing/services.py:6638-6653`, `:7040-7058`,
  `:6745-6753`, `:6792-6817`; binding defaults in
  `src/apps/publishing/models.py:2305`.
- Trigger: prepare a binding to an already available exact mapping. The new binding
  defaults to `prepared` with no `remote_verified_at`; the planner immediately
  skips the available mapping. No operation can advance that binding.
- Actual SQLite diagnostics on WordPress and Blogger: planner returned `()`,
  binding remained `prepared`, verification timestamp remained absent, and the
  real readiness check raised `publication media binding is not active and current`.
  Their passing diagnostic assertions describe the broken state, not a successful
  publication. Existing fixture tests manually activate the binding.
- Safe correction: verify the exact available mapping and admit the new binding
  under its lock, or enqueue an exact read-only verification operation. Preserve
  rights/source/presentation checks and races with pending or in-flight deletion,
  supersession, restored references and bounded generations. Do not simply equate
  an arbitrary `available` flag with a trusted binding.
- Regression: reuse across new revisions/articles on both channels, ready timestamp,
  exact material mismatch and deletion/reference-restoration races. PostgreSQL
  locking or live remote availability was not verified.

### D5 — P1, C3 confirmed: one ineligible due tick aborts unrelated schedules

- Source: `src/apps/scheduling/services.py:329-338`, `:929-934`, `:1038-1044`,
  `:1092-1111`; `src/apps/scheduling/tasks.py:18`.
- Trigger: a due validated-auto schedule has material invalidated by an ordinary
  credential/policy change. Material resolution raises before dispatch/audit
  persistence or advancement of `next_run_at`. The due scan's list comprehension
  stops before later healthy ticks.
- Actual independent probe of the real due-scan function, with two captured ticks
  and an expected first dispatch rejection: `dispatch_calls=1`,
  `healthy_schedule_attempted=false`. The failing tick remains due by inspection of
  the transactional dispatch ordering. No intentionally fail-all policy was found;
  SC-006 expects each active schedule to start or record a hold reason.
- Safe correction: a typed expected ineligible-material outcome per locked tick,
  durable audited hold/quarantine identity, and independent progression for other
  schedules. Do not catch every `ValueError`/database/programming failure as success,
  or label incomplete/unapproved inputs `schedule-dispatch-material-v1`.
- Regression: stale and healthy ticks in one scan, repeated identical tick replay,
  hold reason/next-run policy, remediation/rotation and concurrent dispatchers.

### D6 — P2, C13 confirmed: a fresh preflight request can reuse a completed old check

- Source: `src/apps/publishing/services.py:3932-3964`, `:3965-3990`,
  `:3579-3599`, `:3630-3651`, `:4013-4034`; outbox existing-event return
  `src/wisdome_writer/infrastructure/outbox.py:576-582`.
- Trigger: request another preflight with a new request key on unchanged operational
  material after its prior check completed. The event key contains only target ID
  and current snapshot version. `last_preflight_at` is excluded from target material,
  and matching material reuses the snapshot. The completed consumer/result is reused,
  so the request does not perform a new remote connection check.
- Actual independent request-service diagnostic: two different fresh request keys
  produced the same dedupe key and reused one event (`distinct_new_checks=1`). The
  mocked enqueue reproduced the independently read real outbox existing-event
  behavior. This was not an executed broker result.
- Related replay defect: the request hash and event lookup derive from the mutable
  current snapshot before exact replay. Replaying the same accepted key after a
  target change therefore cannot reliably return the original accepted job. This
  part is source-traced here, not separately reproduced with a persisted audit row.
- Safe correction: distinguish new operational checks by original immutable request
  identity, and resolve accepted replay before current state. Preserve event payload,
  active receipt, snapshot and result fences; do not reset a completed receipt to
  manufacture a new check under an old event identity.
- Regression: complete unchanged-material preflight then fresh-key request really
  runs another check; exact old request after snapshot advancement returns its old
  job; mismatched payload/actor/key conflicts; duplicate deliveries remain harmless.

### D7 — P2, C10 confirmed: target serialization violates the public response contract

**Later status:** the main session repaired this during the review. The same actual
serializer/adapter capability/compiled OpenAPI probe now returns zero schema errors.
This is a fresh serializer-contract check, not a live target/API journey.

- Source: `src/apps/publishing/api.py:164-196`; `PublisherCapabilities.as_dict`
  in `src/apps/publishing/contracts.py:51-64`; `PublicationTarget` response schema
  in `specs/001-automated-content-publishing/contracts/admin-api.openapi.yaml`.
- Trigger: return a normal new snapshot-bound target whose capabilities come from
  the actual WordPress adapter. The serializer exposes internal snake-case capability
  names and activation version `0`.
- Actual real serializer + real compiled OpenAPI validator: required `markWithdrawn`
  and `mediaUpload` were missing, and `autoPublishActivationVersion=0` violated
  minimum 1. The final diagnostic uses the adapter's complete capability dictionary,
  not an artificially incomplete mock.
- Safe correction: map public capability names at serialization and consistently
  represent no activation (for example null under the declared contract). Preserve
  internal adapter command names. Validate actual endpoint responses for new,
  activated and disconnected targets.

### D8 — P1, new: uncertain media deletion cannot persist its recovery outcome

- Source: `src/apps/publishing/services.py:7478-7483`, `:7490-7535`,
  `:6881-6883`; media worker `src/apps/publishing/tasks.py:158-200`; result
  conversion `src/apps/publishing/media_delivery.py:142-147`, `:182-191`;
  migration 0012's non-delete operation lineage guard `:292-313` and `:750-771`.
- Trigger: a legitimate WordPress media DELETE crossed its write fence and its
  response/verification has an unknown outcome. DELETE operations intentionally
  have no publication attempt. The settlement service changes the state to unknown
  then unconditionally enqueues `RECONCILE` with `attempt=None`. The payload builder
  correctly rejects this content-media reconciliation.
- Actual SQLite diagnostic: real DELETE v2 event, real active receipt, actual begin
  and write marker, then actual settlement with an unknown-result DTO. It raised
  `Conflict("media prepare/reconcile requires a publication attempt")`. The entire
  settlement transaction rolled back; DELETE remained `running`, operation count
  stayed one, and its unknown outcome/audit/recovery was not committed. No publisher
  or storage call was made. Only test configuration enabled publishing and mocked
  the fixture kill switch off; event/receipt/write/SQL guards remained real.
- Counterargument/limit: the post-write **worker reclaim** path at `:7139-7149`
  correctly chooses DELETE recovery and can eventually recover after consumer retry.
  That does not make direct unknown-result settlement correct. Repeating a DELETE
  after the object is already absent also needs exact absence proof; the current
  WordPress `delete_media` initial 404 is not automatically a successful deletion.
  Permanent unrecoverability or a live orphan incident was not demonstrated.
- Safe correction: settle and audit unknown deletion under its exact deletion
  identity, then use bounded recovery with read-only exact ID/version absence proof
  or an explicitly admitted DELETE recovery generation. Preserve reference checks,
  receipt capability, grace period, delete restoration gates and generation limits.
  Do not invent a publication attempt or weaken the non-delete operation contract.
- Regression: lost DELETE response; successful DELETE with unavailable verification;
  object already absent; still-present object; receipt reclaim/late result; restored
  reference; bounded exhaustion; WordPress and exact-version public-delivery paths.

### D9 — P1, new: Blogger credential revocation bypasses its supplied write guard

- Source: `src/adapters/publishers/blogger/client.py:367-405`, compared with its
  guarded request helper `:67-69`; caller
  `src/apps/publishing/services.py:3125-3175`, revocation worker
  `src/apps/publishing/tasks.py:316-354`, and global-write policy `services.py:9035`.
- Trigger: construct the normal Blogger publisher with its guarded-write callback,
  then revoke credentials while that callback would deny external writes. The
  method directly invokes `self.client.post` without calling `self.write_guard`.
  The supplied callback's current kill-switch/policy checks therefore never run.
  WordPress revocation uses `_request(..., write=True)` and honors the same hook.
- Actual adapter diagnostic: an always-denying guard plus a mocked client returning
  200 produced `guard_calls=0`, `remote_post_calls=1`, and
  `guard_denial_observed=false`. This exercised the actual method and no network
  request or real token. Local task/service boundaries remain independently enforced;
  this does not establish a local-mode bypass or an unauthenticated revocation.
- Counterargument: security policy could intentionally exempt credential revocation
  from a publication kill switch. No explicit exemption was found in the caller,
  callback or WordPress implementation. Regardless of that product decision, this
  method ignores the supplied pre-I/O guard instead of making an explicit policy
  distinction. Google token exchange/refresh methods use a different interface and
  are not included in this finding.
- Safe correction: invoke `self.write_guard()` immediately before the revocation
  POST. If revocation has an approved exemption, express it in the caller's scoped
  guard policy consistently across channels, rather than silently bypassing a hook.
- Regression: denying guard sends zero POSTs; allowed guard runs once before the
  POST; preserve successful/already-revoked/unknown/permanent response classification.

## Revalidated repairs, broader gaps, and limits

- C5's current decision path consumes `validation_decision` and writes its request
  hash into audit metadata. The real account consumer restricts scope/session/expiry,
  and real audit replay compares that request hash. This source read supports the
  intended repair; the main session's regression result is separate evidence.
- A4's self-revocation repair requires a bound successful deletion UUID plus the
  specific incorrect-password 401. The new code does not accept arbitrary 401s.
  No new live WordPress result or official-source lookup was performed by reviewer D.
- C11 remains source-observable: the report endpoint still omits required provenance
  fields, emits empty samples/object metrics, and derives a draft's overall result
  from approval state even if all stored canary stages passed. A truthful versioned
  canary/pilot report contract is required; this reviewer did not execute its report
  endpoint/schema reproduction. Do not fabricate samples or evidence to fill it.
- C12 remains a console integration gap: the current JS loads all target previews
  with one `Promise.all` and builds only `content_preview` approval subjects, while
  the server intentionally has renderless unpublish intents. Action-specific review,
  scoped unpublish proof and verified-correction preparation are still needed for
  the full administrator journey. This is planned release work with no browser
  acceptance here, not proof that the server's withdrawal guards should be removed.
- C14 remains a source-traced media placement gap: `_create_preview_render` only
  renders Markdown/correction/canonical HTML; `_final_render` resolves canonical
  URL but does not insert placement URLs; adapters send body HTML and do not consume
  media DTOs/featured-media placement. The publisher contract explicitly requires
  approved placement placeholders and their URL bindings. No real final-post fixture
  or remote visual acceptance was executed in this pass. Treat the full solution as
  an unfinished end-to-end release feature, with approved-body/hash binding, not a
  reason to relax media rights/approval checks.
- Blogger media is planned before the exact WordPress dependency completes, but its
  pre-write gate requires that dependency; publication release then requires active
  media. This ordering deserves a staged dependency/media regression. It is an
  inspected hypothesis here, not an additional executed finding.
- Four large migration bodies were completely inspected, including both backends,
  admission/backfill/reverse controls and declarations. No additional confirmed SQL
  defect was identified. This does **not** mean PostgreSQL SQL/locking was executed.
- HWP signed-release admission remains closed. T022/T024/T025/T026/T027/T028/T031/
  T032 acceptance and dependency gates remain authoritative. None was marked complete.

## Commands and actual verification

All diagnostic Python processes used the project Python 3.12 environment. The
saved repro sets explicit development/local mode. SQLite pytest children use
`PYTHONUTF8=1`, explicit local mode and removed inherited root overrides; each
uses its own confined ASCII base directory. Synthetic references/passwords are
fixture data. Test-only publishing enablement did not start a service or send a
network request.

| Check | Actual result |
| --- | --- |
| `git status --short` / `git rev-parse HEAD` | Expected baseline and main-session working changes; preserved |
| `.venv/Scripts/python.exe .local/reviews/round2-security-state/reproduce.py` | Exit 0; C6 compatibility rejection, C3 starvation, C9 replacement selection, C13 reused event, C10 three schema errors, C7 future-date loss with valid control |
| Initial scratch SQLite file | 6 passed, 1 failed in 22.96 s; two reuse diagnostics plus four imported existing fixture tests passed; disconnect diagnostic correctly blocked by local policy |
| Narrow disconnect rerun with test-only publishing enablement | 1 passed in 20.14 s; confirmed replacement selection through real disconnect/update/begin services |
| Initial uncertain-delete diagnostic | 1 failed in 19.12 s at correctly closed fixture kill switch, before tested result settlement |
| Final uncertain-delete diagnostic with isolated kill-switch false fixture | 1 passed in 18.04 s; reproduced settlement Conflict, rollback, running delete and one generation |
| Final isolated state diagnostics in a clean explicit child environment | 4 passed in 20.65 s; both reuse stalls, actual disconnect/update replacement selection and uncertain-delete rollback reproduced |
| Latest-source mocked probes (`reproduce.py --latest`) | Exit 0; D1 compatibility and negative controls pass; D7 schema has zero errors; C3/C7/C9/C13 continue to reproduce; D9 supplied revocation guard is skipped |

Diagnostics: `.local/reviews/round2-security-state/reproduce.py`,
`reproduction-results.json`, `test_state_diagnostics.py`, `state-diagnostics.log`,
`delete-diagnostic.log`, `delete-diagnostic-final.log`, and `read_source.py`/`reads.jsonl`.
`final-state-diagnostics.log` records the four final diagnostics;
`source-hashes.json` identifies the source files captured for the diagnostic handoff.
`pre-second-repair-results.json` preserves the original evidence;
`latest-source-results.json` records the recheck after concurrent main repairs.
The passing scratch assertions establish stated failures, not passing acceptance.
One initial mocked dispatch probe had a missing `publication_id` fixture attribute;
the corrected diagnostic then executed. That preparation error was not a product bug.

No complete pytest/Ruff/migration audit command, PostgreSQL execution, Celery/Redis
delivery, S3 integration, live publisher/OAuth, real credentials, official collection,
OCR/HWP runtime, live sibling transform, browser acceptance, hosted CI, dependency
vulnerability audit or benchmark was executed by this reviewer.

## Coverage and continuation

Complete migration text reads: publishing `0009_approval_decision_integrity.py`
(1,258 lines), `0010_intent_dispatch_identity.py` (1,321),
`0011_publication_attempt_fencing.py` (2,443),
`0012_published_asset_snapshots.py` (1,385), and
`0013_publisher_credentials.py` (87). Truncated tool-output portions were reread
in smaller ranges. Both SQLite/PostgreSQL SQL, mutation/lineage guards, lease/result
settlement rules, backfills, irreversibility and schema operations were read; no
backend SQL equivalence or concurrency acceptance is implied.

Complete small boundary reads: accounts `models.py`/`services.py`, publishing
`apps.py`, scheduling `controls.py`/`tasks.py`, `wisdome_writer/external_publishing.py`,
and domain `hashing.py`/`concurrency.py`. Other reads were targeted and are recorded
in the diagnostic range log, including publishing requests/snapshots/OAuth,
preview/approval hashes, dispatch/media/delete/retry/reconcile/write fences;
publishing worker calls, scheduling material/due dispatch, audit replay/event
capability, outbox dedupe/error settlement, secret resolution, HTTP middleware,
publishers, API serialization, contracts and affected test fixture bodies.

This fresh pass has **not** completed a full reread of the 13,225-line publishing
service or 2,592-line models, every publishing/scheduling module, the whole audit/
HTTP/API infrastructure, all earlier migrations, or all related test bodies. The
round-1 complete reads remain historical context; an AST/file inventory or importing
a fixture is not a complete semantic read. The combined main report must preserve
these fresh-pass limits rather than call reviewer D's pass exhaustive. Follow-up
should finish these untouched bodies and execute staged scheduling/media/dependency
and PostgreSQL acceptance after the minimal regressions/fixes.

Open product questions: the supported distributed release scope; the policy for an
ineligible scheduled tick's future cadence/hold; how operators distinguish a fresh
preflight from request replay; versioned secret-store retention during disconnect;
and the accepted presentation/featured-media contract. These questions do not
invalidate the independently reproduced defects.

The main session should reconcile these IDs into the combined/canonical reports
and register this supplemental path if it remains a canonical handoff. Reviewer D
does not edit project instructions or competing canonical conclusions.

### Per-file requested read ranges

These ranges are a reproducible read index, not a coverage percentage. Auxiliary
multi-file output was sometimes truncated, so an indexed range is not itself a
claim that every line was inspected. The explicitly complete migration/small-file
reads above and the stated remaining-body limits take precedence. Line offsets
can shift with concurrent main-session repairs. `0009:1-342`, domain hashing,
publishing app boundaries and the global external-publishing boundary were read
outside the instrumented reader and are identified above.

| Product file | Requested ranges |
| --- | --- |
| `src/adapters/publishers/blogger/client.py` | 1-479 |
| `src/adapters/publishers/wordpress/client.py` | 1-662 |
| `src/apps/accounts/models.py` | 1-57 |
| `src/apps/accounts/services.py` | 1-233 |
| `src/apps/audit/services.py` | 410-661 |
| `src/apps/publishing/api.py` | 145-218, 590-677 |
| `src/apps/publishing/contracts.py` | 1-193 |
| `src/apps/publishing/media_delivery.py` | 1-241 |
| `src/apps/publishing/migrations/0009_approval_decision_integrity.py` | 343-1258 |
| `src/apps/publishing/migrations/0010_intent_dispatch_identity.py` | 1-1321 |
| `src/apps/publishing/migrations/0011_publication_attempt_fencing.py` | 1-2443 |
| `src/apps/publishing/migrations/0012_published_asset_snapshots.py` | 1-1385 |
| `src/apps/publishing/migrations/0013_publisher_credentials.py` | 1-87 |
| `src/apps/publishing/models.py` | 1-200, 2500-2592 |
| `src/apps/publishing/services.py` | 1419-1495, 1604-1825, 2200-2233, 2990-3700, 3782-4258, 5115-5260, 5948-6130, 6419-7549, 7552-7772, 8396-8705, 9040-9300, 9735-9860, 9977-10320, 11556-11605, 12579-12690, 12700-13225 |
| `src/apps/publishing/tasks.py` | 1-808 |
| `src/apps/scheduling/controls.py` | 1-18 |
| `src/apps/scheduling/migrations/0003_schedule_dispatch_material.py` | 1-130 |
| `src/apps/scheduling/services.py` | 1-1250 |
| `src/apps/scheduling/tasks.py` | 1-35 |
| `src/wisdome_writer/api/middleware.py` | 1-200 |
| `src/wisdome_writer/api/openapi.py` | 178-225, 360-400 |
| `src/wisdome_writer/domain/concurrency.py` | 1-145 |
| `src/wisdome_writer/infrastructure/models.py` | 50-152 |
| `src/wisdome_writer/infrastructure/outbox.py` | 520-590, 1310-1400 |
| `src/wisdome_writer/infrastructure/secrets.py` | 1-206 |

## Update log

| Date | Update |
| --- | --- |
| 2026-10-03 | Fresh round-2 source/migration review; D1–D8, independent SQLite/mocked evidence, prior findings and release gaps separated, explicit coverage limits |
| 2026-10-03 | Independent latest-source check confirms main-session D1 hash compatibility/negative controls and D7 target-schema repairs; original evidence retained |
| 2026-10-03 | D9 independent adapter probe confirms Blogger revocation ignores the supplied write guard |
