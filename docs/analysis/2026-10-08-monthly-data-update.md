# Monthly data update and year/month UX progress

## Current result — 2026-10-09 KST

| Area | Actual result |
| --- | --- |
| Date UX | Implemented year/month/year-only/all-period navigation and preserved topic filters |
| Housing month | Actual special/first reception dates:23 October notices; special18/first21 with overlap |
| Funding month | Official posting/registration date:17 October notices;93 saved overall, one source partial |
| News month | Original publication date:2 validated live records; MOTIR1/SIA1, Samsung completed with0 October records |
| Stable code checks | 1,689 passed,3 skipped,203 subtests passed; Django check/drift and scoped Ruff pass |
| Preview | Task-owned22363 at127.0.0.1:7667, loopback listener confirmed |
| Browser acceptance | October 9 authorized fresh-chat Chrome desktop and 390 x 844 responsive menus/period/overflow passed; see browser diagnosis |

The counts are bounded validated saved observations, not exhaustive national
coverage or current individual loan approval. Code is uncommitted on the existing
feature branch. The chronology below preserves failed attempts, repairs and actual
verification limits for restart/cross-agent handoff.

## Baseline

- Date: 2026-10-08, Asia/Seoul; requested period October 2026.
- Branch codex/policy-funding at db5edec35aad39ae85e3aca7b6f9e1b9f2e3a0dc plus
  existing uncommitted funding/portal work and English documentation.
- Starting authorities: AGENTS, shared analysis policy, canonical service/code
  reports and topic-portal progress. Previous 1,502-test acceptance is historical
  until the new date/collection changes are verified.
- User authorized live update and year/month UX; optional default-date-basis
  preference was asked. Both publication/registration and application/collection
  date meanings will remain explicit rather than be silently conflated.

User answered: housing follows special-supply and first-priority actual reception
schedules; funding follows notice date. The implementation now uses those exact
defaults; semiconductor remains original-publication month. A collection date is
still separately labelled for history, never substituted for a missing source date.
- Browser access remains denied. Official-source crawling uses independent
  authorized collection paths, not a workaround for reference/local browser access.

Pre-recovery collection checkpoint: October9 after midnight KST. Implementation and review repairs
are in the working tree; final code checks and bounded live news refresh pass.
Direct Chrome desktop/mobile acceptance remains outstanding.
Requested collection month remains October2026. Captures keep their real date/time.

## Research and decisions

- [Housing research](2026-10-08-monthly-housing-recon.md): existing --days 8
  --no-humanize --no-write-articles commits validated October metadata. No article
  generation/humanizer is required; this does not count as full writing acceptance.
- [Semiconductor research](2026-10-08-monthly-semiconductor-recon.md): existing
  distributed ingestion requires real approval/Redis and a 720-minute freshness
  policy. Migration/seed alone cannot satisfy monthly archival ingestion.
- Ruling: add an explicitly local official metadata archive rather than forge
  approval/run attempts. Why: satisfy authorized local month collection while
  preserving administrative source/publishing authority. Cost: another narrow
  metadata storage path; its provenance and operator access must remain explicit.
- Design/plan: ../superpowers/specs/2026-10-08-monthly-update-design.md and
  ../superpowers/plans/2026-10-08-monthly-update.md.

## Actual collection checkpoint

- Funding CLI attempted --pages 3 --max-notices 40 --seconds 180 --as-of 2026-10-08.
  Live reads occurred, but commit failed at the owned staging identity guard.
  Exit 1; log .local/reviews/monthly-update/funding-collection.log. This is not a
  completed update. Preserve failed material and diagnose without relaxing guards.
- Housing notice-only initial command finished exit 1 with DETAIL_INCOMPLETE.
  A validated run was saved: 54 notices, selected article work 32, written 0.
  Its old parser did not preserve stage facts, so it does not establish special/
  first-priority October coverage. Log housing-collection.log.
- Funding/housing collection roots remain independent. No migrations, source
  approval, external publication or browser navigation was executed in this phase.

## Continuation

Execute the linked plan, record all actual source outcomes, validate persistence,
test date/filter boundaries and update this same report before the final response.
Chrome/mobile visual acceptance remains a separate blocked requirement.

## Implementation and live-refresh follow-up

- Common PeriodSelection and UI context: 12 RED/GREEN cases passed. Funding
  filtering-before-dedup: 2 cases passed. Month route/selection/form tests: 7 cases
  passed; combined date/portal/funding/news scope then passed 88 cases.
- Housing parser v2 preserves actual special/first stage facts and regional labels.
  Independent review found colspan/row-region/malformed-token cases; all were
  reproduced and repaired. Worker final scope: 203 passed. A bounded real official
  detail probe verified special October 19 and first October 20 for one record.
- --since 2026-09-01 support keeps existing --days behavior, adds bounded explicit
  earlier publication lookback, and enables October stages announced in September.
- Broader housing command started in session 34020 and has returned a saved partial
  run 2026-10-08--run-4a05683f2944, DETAIL_INCOMPLETE, selected article work 120,
  written 0. Exact metadata counts/month matches still require validated readback.
- A controlled NTFS archive-bit false positive was separately reproduced/repaired;
  the second funding retry still failed, now exposing a different 0x10000000 lstat
  attribute discrepancy with unchanged type/device/inode. Actual funding fixture
  reproduction showed native GetFileAttributesW=16 while lstat reported268435472.
  A narrowly twice-native-validated identity reconciliation was tested, preserving
  full read/content fingerprints and all other identity/security checks.
  [Storage diagnosis](2026-10-08-monthly-storage-diagnosis.md) has exact evidence.
  Final storage scope: 242 passed. Third funding retry is session43213; do not
  infer success until the new run validates and command exits successfully.
- First semiconductor live command saved a failure archive, count0, all three
  supported sources source_schema_invalid after robots/content reads. This is not
  a successful zero-news update. KRX unsupported; SK hynix disabled/prohibited.
  Parser-response diagnostics and projection corrections are being implemented.
- Respect persisted source poll eligibility: MOTIR/Samsung next permitted around
  22:53 KST and SIA around23:23 KST on October8. Do not clear ledgers or refetch
  through another tool to defeat these rate/poll limits. Unknown parser cause is
  not grounds to relax source profiles or fabricate records.

## Validated data and second review

- Third funding attempt committed a validated immutable run
  2026-10-08--run-fd18f61df3fc: 93 records; 17 match October posting/registration
  dates. Source results: agency23(partial: one older detail218 rejected), direct30
  complete, Bizinfo40 complete;107 claimed requests. The CLI exits1 because its
  partial source remains explicit; successful persistence is not full coverage.
- September1 housing run validated175 distinct saved identities across inspected
  history;23 notices have confirmed October special/first dates. Unconfirmed or
  nonapplicable stage identities remain excluded from month results. Overall
  workflow DETAIL_INCOMPLETE is retained, not reported as complete writing.
- Initial news failure now shows unavailable rather than successful zero. Later
  failures retain earlier positive metadata; publication/capture filtering occurs
  before deduplication; fixture/live provenance remains explicit.
- Independent [monthly review](2026-10-08-monthly-update-review.md) reproduced
  historical funding cards opening the latest version, DB news hiding refreshed
  local metadata, and omitted unknown-date summary. Accepted fixes bind card
  links to their selected run, add explicit news inventory choice/auto precedence
  without approval mixing, and report unknown publication dates.
- Historical funding fix reproduced RED then GREEN; month/form/snapshot scope
 11 passed. News projection/API and parser fixes have separate worker evidence.
- Captured live diagnostics proved MOTIR's real inline identity syntax changed;
  local normalization preserves exact matching board/id and cosmetic-query checks.
  Samsung's first-page feed redirect needed narrow query-context support; page2,
  other endpoints/origins and unknown query fields remain rejected.
- A third normal news attempt is now session48972 after eligible poll times.
  Do not treat this running attempt as completed or launch a duplicate.

## Current acceptance checkpoint

- Third news attempt completed exit0 and validated run
  2026-10-08--run-5c8262014531 with one October MOTIR source record.
  MOTIRcompleted1/3requests; Samsungfailed3requests; SIAfailed2requests;
  KRXunsupported and SKhynixdisabled. Partial source failure remains visible.
  Original-publication month matches one genuinely fetched source record.
- Source responses, redirects and parser errors now have bounded private
  diagnostics; remaining Samsung/SIA causes are being inspected offline.
  Do not bypass polling reservations to repeat fetches.
- Final combined targeted scope passed230 tests in28.96 seconds. Month route
  historical version/source selector regressions add separate RED/GREEN evidence.
- Task-owned preview53227 was stopped before the full Windows suite. That suite
  (34311, full-suite.log) was deliberately interrupted with exit1 to include two
  now-proven news parser repairs. Its partial output is not acceptance. A fresh
  full run is required after those repairs; no task-owned preview is running.
  Old 1,502-pass evidence predates these monthly changes.
- Exact month semantics now follow the user's clarification: housing special/
  first reception dates; funding posting/registration date; news original date.
  Archive history uses execution/capture dates labelled separately.

## Confirmed parser repairs and final-validation preparation

- The captured Samsung response is a valid 50-entry RSS document. Optional MRSS
  image records lack MIME types, which the shared full-media validator requires.
  This local route only saves title/link/date metadata; it should not validate
  unneeded media. Offline metadata extraction found zero October entries and a
  newest original date of September29. This is a prospective honest empty source
  outcome, not a fabricated October record or completed live refresh.
- The captured SIA resource cards have exact known type prefixes such as
  `Press Release: 10/05/26` and `Blog: 09/23/26`. The unmodified strict date parser
  rejects them. Controlled exact-prefix normalization yielded one October5
  record and retained strict source identity/title/path/date checks. Both fixes
  are assigned to the news owner; shared/distributed media validation stays strict.
  Response hashes and diagnostic limits are in the semiconductor research report.
- Last real attempt reserved MOTIR/Samsung until about October9 00:00 KST and
  SIA until about00:31 KST. Read the persisted exact timestamps before another
  normal collection. No alternate client or ledger reset is authorized.
- Historical funding detail navigation now additionally preserves month, query,
  region, kind and mode when returning to the list. Its regression first failed
  on the missing return state, then passed; route/funding scope18 passed20.26s.
  The initial new-test setup used an invalid national region selector (400) and
  was corrected to Seoul before establishing the intended RED/GREEN evidence.
- Django check: no issues; migration drift: no changes. Current AST inventory
  before the final news repair:279 source files/114,198 lines,95 test files/44,679
  lines and7 scripts/1,105 lines;381 parsed files. Syntax/inventory is not semantic
  coverage. `git diff --check` passed with existing Windows newline warnings.
- An independent repair revalidation is active. Final full tests, validated live
  news refresh, canonical report updates and preview restart remain pending.

## After-midnight review closure — 2026-10-09 KST

- Samsung/SIA local metadata repairs are implemented. Shared/distributed media
  validation and source profiles stay unchanged. Exact known SIA prefixes are
  normalized before the unchanged strict source date parser; a2025 date remains
  2025 and cannot acquire requested2026. Worker scope87 passed24.45s; Ruff passed.
  Independent review also passed64 archive tests and hash-verified offline replay.
  Fresh live acceptance still requires the normal eligible collection.
- Review revalidated funding snapshot/back-filter binding, local news inventory
  selection and housing stage semantics. It found the generic publication-unknown
  message also appeared on drafts/source pages. Main reproduced this with actual
  validated fixture storage and a simulated unavailable database (2 RED failures).
- Sources now show their configuration explanation; drafts have their own database
  availability/message rather than borrowing local-news availability. News counts
  are not displayed as draft counts. Publication-unknown notes appear only on
  active publication-date news views. Combined route/projection39 passed18.90s;
  final month-route16 passed3.46s and all changed-module/test scoped Ruff passed.
- Second monthly suite63034 was deliberately stopped for this confirmed final
  presentation repair. Its partial final-full-suite.log is not a passing result.
  The next full suite must use a new log and stable product bytes.
- Task-owned wait/collection session70069 waits for the saved maximum poll_until
  plus5 seconds:2026-10-08T15:31:07Z (October9 00:31:07 KST), then runs the normal
  local news command. It never modifies reservations and the collector rechecks
  current state. Do not launch a duplicate or call a waiting attempt completed.
- Final review independently closed the generic-message/draft-state follow-up:
  16 route tests passed3.20s; both unavailable and successfully empty draft
  stores were checked through the real projection/routes. No material monthly
  review finding remains open in that scope. Browser/source/full-suite acceptance
  are separate. Final stable full suite is session67271 with
  `.local/reviews/monthly-update/verified-full-suite.log`; read its actual result.
- Static build:1 copied,139 unmodified,390 postprocessed. The actual saved-data
  in-process page probe verified October union23 housing notices (special18,
  first21, overlapping identities),17 funding notices, bound detail/return state,
  and1 live news record. Actual database drafts remained explicitly unavailable.
  An initial probe incorrectly required a script-src directive absent from the
  preserved housing CSP; the helper was corrected to its actual policy contract.
  This was a probe-assumption failure, not a product fix. No HTTP/browser operation.
- Final stable AST inventory:279 source files/114,207 lines,95 test files/44,806
  lines and7 scripts/1,105 lines;381 Python files parsed. No syntax failure.
- Final stable full suite67271 completed exit0: **1,689 passed,3 skipped,203
  subtests passed in540.75 seconds**. Final Django check:no issues; migration
  drift:no changes. This supersedes both interrupted monthly attempts and the
  older portal-only result for current bytes. No further source edits followed.
- Preview restarted after tests:task-owned session22363, loopback127.0.0.1:7667,
  listenerPID30240. Stop only this owned server before lifecycle tests require
  port7667. Startup stdout is buffered; listener ownership and independent final
  Django check are the recorded evidence. No browser navigation was attempted.

## Final bounded collection and handoff — 2026-10-09 KST

- Normal eligible command completed exit0 without changing clocks or poll ledgers:
  `collect_semiconductor_news --year 2026 --month 10 --max-items 60 --seconds 180`.
  Log `.local/reviews/monthly-update/semiconductor-collection-final.log`.
  `read_news_archive` validated immutable generation `2026-10-09`, mode `live`,
  two records and explicit bounded coverage. Requests ran00:31:10–00:32:13 KST.
- Source results: MOTIR1/completed/3 requests; Samsung0/completed/5 requests;
  SIA1/completed/2 requests; KRX unsupported/0 requests; SK hynix disabled/0.
  Samsung's zero is now an actual successful dated-window observation, not a
  parser failure. These results do not mean every site or older page was crawled.
- Final validated history/projection readback and actual in-process page rendering
  passed: housing union23 (special18, first21, overlap), funding17, live news2.
  Funding origin-run detail and October return selection remained consistent;
  database drafts remained explicitly unavailable. Evidence:
  `collection-summary.json`, `final-collection-summary.log`, `verified-pages.json`
  and `final-pages.log` under `.local/reviews/monthly-update/`.
- Housing remains DETAIL_INCOMPLETE with earlier publication lookback bounded at
  September1. Funding's93-record generation includes one partial agency source
  (detail218 rejected);17 October posted notices are validated. These omissions
  remain visible. Unknown/nonapplicable housing stages are not guessed into months.
- Current implementation, independent repair review, code checks, real bounded
  collections and runtime preview handoff are complete for this monthly task.
  No post-suite source change, runtime migration, publication, commit or push.
  Full source coverage and direct Chrome/mobile acceptance were not established
  at this collection checkpoint. The browser-only follow-up below closes the
  requested responsive Chrome checks without changing the collected data.
- Source collection session70069 and readback10797 completed; no collection wait
  remains. Preview22363/PID30240 remains task-owned at127.0.0.1:7667. Stop it before
  a later full lifecycle suite; preserve the validated runs and all failure evidence.
- Continuation: read this final section and the linked reports/plan, verify HEAD,
  current bytes and saved data before further changes. A future authorized browser
  state change was required at that checkpoint for direct desktop/mobile UX
  acceptance; never bypass an actual denial with alternate browser/network/CDP paths.

## Browser-only acceptance follow-up — 2026-10-09 KST

The user-authorized fresh chat successfully opened the root and exact reference
URL through the normal Chrome extension. Existing loopback preview PID 30240
was reused. Actual native period controls and mobile topic/menu navigation
verified October housing 23 (special 18 / first 21, overlap), funding 17 and news 2.
Housing uses regional special/first reception dates, funding posting/registration
dates, and news original publication dates with separate capture labels. Year-only,
all-period, query preservation, empty search and funding snapshot/return checks
were exercised. At 390 x 844, inspected root/topic/internal destination pages had
no document horizontal overflow; the default desktop viewport was restored.

Canonical [browser diagnosis and acceptance](2026-10-09-browser-access-diagnosis.md)
contains exact URLs, usable captures, DOM/geometry evidence and screenshot API
limitations. This is responsive Chrome acceptance, not physical-device/touch
certification or complete external-source/video coverage. The portal tab remains
open as a deliverable. No product code, archive, poll reservation, collection,
migration, full test suite or publisher was changed/run in this browser-only task;
the 1,689-pass result above remains its dated historical verification.

Update log: October 9 browser blocker closed in the authorized fresh chat;
bounded source omissions and distributed/publishing acceptance remain separate.
