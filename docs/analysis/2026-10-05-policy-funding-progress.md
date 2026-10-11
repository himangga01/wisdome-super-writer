# Policy funding feature progress and restart ledger

## Identity and authorization

- Started: 2026-10-05, Asia/Seoul.
- Baseline: db5edec35aad39ae85e3aca7b6f9e1b9f2e3a0dc.
- Branch: codex/policy-funding, current checkout. Existing October 5 analysis changes
  remain in place; no destructive cleanup, unrelated product repair or remote push.
- User explicitly authorized research, planning, design, implementation and verification
  through completion using Superpowers, with English Markdown restart records.
- Workflow: architectural design + writing-plans; parallel read-only research;
  native implementation with failing/passing tests; independent final review.
- Ruling: use the user's full-task authorization to proceed through documented design
  and implementation checkpoints without repeating approval questions. Cost if wrong:
  an explicit requirement correction may require revising the design/implementation.
- Ruling: reuse this checkout on a new feature branch, preserving existing analysis
  work, following the desktop preference. No new worktree or automatic commit/push.

## Artifacts

- Design: ../superpowers/specs/2026-10-05-policy-funding-design.md
- Plan: ../superpowers/plans/2026-10-05-policy-funding-implementation.md
- National source research: 2026-10-05-policy-funding-national-research.md
- Regional research: 2026-10-05-policy-funding-regional-research.md
- Integration reconnaissance: 2026-10-05-policy-funding-integration-recon.md
- Final analysis/acceptance: this ledger and canonical code/service reports.

## Task status

| Task | State | Evidence / next action |
| --- | --- | --- |
| P1 official national/regional research | Complete | Three English reports saved; endpoints, robots, all 17 directory buckets and user-case differences recorded |
| P2 design/specification | Complete | Dedicated policy_funding module; spec self-reviewed and prior user authorization retained |
| P3 written implementation plan | Complete | Native implementation selected; JSON transport correction recorded below |
| P4 source catalog/contracts/collectors | Complete | 48 entries, three accepted bounded collectors; original and residual review cases fixed/tested |
| P5 beginner guide/rendering/reference verification | Complete | 32 terms, 3 journeys, checklist, purpose-aware notices and dated evidence; supplied fixture preserved |
| P6 immutable runs/change digest/CLI | Complete | Immutable v2 runs, semantic changes, historical detail, source/region CLI and fixture separation tested |
| P7 Django UI/integration | Complete | Integrated read-only pages; access/filter/history tests and real desktop/mobile browser acceptance passed |
| P8 regressions/live acceptance/review | Complete | 1,425 tests +203 subtests passed, 3 skips; bounded live repeats and browser checks passed; reviewed issues repaired |
| P9 final docs/handoff | Complete | English design/plan/research/review/acceptance and canonical reports, README, AGENTS updated; limits retained |

## Research discoveries to carry forward

- Primary OLS notice 404 states 2026-10-06 10:00 until exhausted for the relevant Q4
  agency program and explicitly says the confirmation certificate is not a loan guarantee.
  Siblings 405–407 are returned by the public read-only search endpoint.
- Bizinfo legacy list redirects to /sii/siia/selectSIIA200View.do. Finance filter:
  hashCode=01, rows=15, cpage=N, orderGb=1, sort=desc. schEndAt=N lists open and Y
  ended notices; do not treat either request as complete nationwide coverage.
- Bizinfo detail: /sii/siia/selectSIIA200Detail.do?pblancId=PBLN_.... Table rows
  have eight cells; .view_cont > ul > li has span.s_title and div.txt fields.
- Official Yangsan attachment distinguishes general Q4 start 2026-10-06 09:00 from
  youth-program 2026-02-04 start. Do not flatten different programs into one date.
- Requested 17 regions are service/discovery buckets. Gwangju/Jeonnam government
  pages show a combined-government route while KOREG lists 17 foundations. Keep
  original issuer identity and eligibility text; do not infer legal boundaries.

## Resume procedure

1. Read AGENTS, this ledger, spec, plan and completed research reports.
2. Compare git status/HEAD with baseline; preserve unrelated documentation changes.
3. Resume the first incomplete task; inspect existing files/tests before recreating work.
4. Use development/local, PYTHONUTF8=1, clear LOCAL_STATE_ROOT, LOCAL_OBJECT_ROOT,
   LOCAL_ARTICLE_ROOT and obsolete LOCAL_OUTPUT_ROOT for pytest.
5. Append commands, actual results, decisions, failures and review fixes here after
   each milestone. Do not mark a task complete from a source trace alone.

## Implementation checkpoint 1

- Added apps/policy_funding/{__init__,catalog,contracts}.py and the 48-entry JSON catalog.
- RED: test_policy_funding_contracts.py, 8 failures because module did not exist.
- GREEN: same 8 tests pass; official URL boundaries, 17 buckets, unknown fields,
  aware timestamps, required notice IDs and semantic hashes covered.
- Catalog build initially assumed the government probe JSON had 17 region rows;
  that probe contains only seven partial requests. Corrected the build to consume
  all 17 researched Markdown directory rows. This was setup, not a parser acceptance.
- Ruling: use installed selectolax, not the initially named unavailable BeautifulSoup;
  no dependency changes. Cost if wrong: selectors need adjustment, not a new dependency.
- Ruling: OLS search returns JSON with HTTP 201, so use a bounded policy transport
  over existing safe_get/safe_post_form instead of broadening housing's HTML MIME
  boundary. Cost if wrong: source gets a visible classified failure, not silent data.
- No user-reference fixture, education, collector, storage or UI implementation yet.

## Implementation checkpoint 2

- Added per-source parsers, bounded DNS-pinned HTTPS/robots transport and collectors.
  OLS JSON HTTP 201 and Bizinfo's open/ended HTML partitions are supported.
- RED: 7 collector/parser tests failed because modules did not exist.
- GREEN: 15 combined contract/parser/collector tests passed.
- Live bounded prototype run through production collectors succeeded: semas_agency
  6 requests/4 notices and bizinfo_finance 7 requests/4 notices, no source errors.
  Evidence: ignored .local/reviews/policy-funding/live-collector-first.json.
  This does not establish full nationwide completeness; every outcome marks limits.
- Remaining issue to pin before workflow acceptance: allocate detail budget across
  open and ended Bizinfo partitions rather than consuming it entirely on open notices.
- Parser fixtures are reduced official DOM/JSON excerpts with English provenance.
- Ruling: permit HTTPS .go.kr origin/reference links from official aggregator content
  even when that municipality is not a catalog crawler. No arbitrary-host fetching.
  Cost if wrong: an original reference may require further source review; it is not
  used as automatic loan approval or an additional independent authority.

## Implementation checkpoint 3

- Education/rendering/reference review: five RED tests, then five passing tests.
- Storage/workflow/shared read_validated_run_file: eight RED→GREEN tests. Reused
  manifest version is integer 2, not the draft test's initial string assumption.
- UI/CLI: four initial failures for absent routes/command; implemented all pages.
  Unit template tests require non-manifest test static storage because they do not
  run collectstatic. Actual collectstatic separately passed (139 copied/415 processed).
- Real UI regression caught catalog Korean labels corrupted by PowerShell's native
  stdin encoding. A saved UTF-8 Python script restored all labels. Use apply_patch or
  UTF-8 script files for Korean literals; PYTHONUTF8 alone cannot fix shell encoding.
- Added RED→GREEN loan-route mismatch and fair open/ended partition-budget tests.
- Transport tests: robots denial, HTTP 201 JSON, physical request budget and strict
  byte decoding. Invalid UTF-8 had silently replaced bytes; fixed after failing test.
- Latest transport/UI group: 8 passed, 3 missing-staticfiles warnings before the
  separate successful collectstatic run. Final combined/full suite remains pending.
- Real default three-source CLI with pages=1/max-notices=4/as-of=2026-10-05 exited 0
  and published an immutable run under output/policy-funding; read metadata for final
  counts. Log: .local/reviews/policy-funding/live-command.log.
- Owned QA server: exec session 72233, manage.py runserver 127.0.0.1:7667 --noreload.
  It is this task's server; unrelated IPv6 listener on 7679 must not be touched.
- Installed Brave/Edge/Chrome available for Playwright browser acceptance.

## Independent review and repair pass

- Fresh most-capable reviewer: policy_feature_review (GPT-6 Astra, high), per native
  execution skill. Report: 2026-10-05-policy-funding-review.md. Original verdict:
  eight Important findings (PF-R1–R8), three Minor gaps. No Critical finding.
- Main reproduced the important cases and portable-evidence gap: 15 failing/2
  passing adversarial regressions before fixes. Main accepted all eight findings.
- PF-R1: exact province precedence, researched municipality mapping, unknown region
  instead of national fallback; no cross-match of Gyeonggi Gwangju/metropolitan Gwangju.
- PF-R2: fee-support requires positive support wording and excludes charged/negative cases.
- PF-R3: source + robots validator runs before each redirect/request with merged params.
- PF-R4: final response host/path/notice query ID must match the listed identity.
- PF-R5: cards, details and Markdown expose dated open/ended/unknown list status.
- PF-R6: exact reviewed program title, national scope, agency route and a single
  explicit application-opening date/time are required for live case corroboration.
- PF-R7: scalar/list/object validation precedes membership/date parsing; counts,
  source IDs, changes and nested case evidence are bounded and validated.
- PF-R8: repayment relief and amendments are distinguished from new applications;
  generic new-loan journeys are withheld when that purpose is unsupported.
- PF-M1: changed date labels to the source registration date, not issuer publication.
- PF-M2: added portable per-claim issuer/date/locator/excerpt/attachment hash evidence,
  including exact Pohang product 5 URL and Yangsan draft-filename qualifier.
- PF-M3: added post-collection CLI --region and recorded its selection semantics.
- Ruling: implement the three originally Minor gaps as plan-completion work because
  date meaning, portable evidence and the promised CLI filter were explicit requirements.
  Cost if wrong: modest extra code/tests, with no unrelated feature expansion.
- Main also reproduced historical-run links showing current instead of historical
  terms; added explicit run selection and a regression. Prior bundle bytes remain intact.
- Combined tests initially exposed 3 housing-preview failures caused by a new test
  importing startup URL configuration inside a temporary nonlocal-mode override.
  Fixed the test's startup warm-up; no housing runtime policy was relaxed.
- GREEN after repairs: 39 targeted tests; then 46 UI/history/preview checks; final
  focused integration group 246 passed in 39.21 s. New-module/test Ruff passes.
- Full unit/integration suite currently running in exec session 10417; log
  .local/reviews/policy-funding/full-suite.log. Do not launch a duplicate full suite.
- QA server restarted with corrected code: session 59711 at 127.0.0.1:7667.
  Previous task-owned session 72233 was stopped; unrelated 7679 listener untouched.
- Next: one scoped re-review, refreshed live/browser acceptance, final documentation.

## Scoped review closure

- Scoped re-review confirmed the original eight reproductions and three plan gaps
  addressed, but found two residual variants: fee-support negation (불가/대상이 아님)
  and OLS board identity bbsTypeCd. Main reproduced five failing variants, including
  missing/repeated board parameters, then repaired them. 31 review/collector tests pass.
- Ruling: finish these two bounded residual correctness subcases under the user's
  completion authorization instead of leaving misleading financial labels or evidence
  identity. Cost if wrong: conservative rejection of ambiguous source material.
  No new broad review wave; the parent's regression evidence closes these variants.
- Full-suite session 10417 was interrupted to include these final fixes; its partial
  log is not a successful acceptance run. A fresh final suite is required.
- Reviewer declined judgments: exhaustive coverage, live bank/budget outcomes,
  unsupported attachments/HWP, public deployment, legal reuse, old distributed
  defects and high-volume/concurrent history guarantees. These are not claimed by
  this feature; bounded live reads/browser checks and the local test suite are main
  evidence. Costs if assumptions fail: missing source coverage or deployment-scale
  limitations, explicitly shown rather than invented eligibility/approval.

## Final validation checkpoint

- Final live CLI ran twice with all three sources: 12 notices per run, 19 bounded
  requests, no source errors. Latest run 2026-10-05--run-6b4ba832282a reports 12
  unchanged records. Prior refreshed run a0555b8f14f0 had one classification change.
- Actual Brave/Playwright desktop + mobile checks pass again after fixes, including
  ended-list labels and repayment-relief copy. Screenshots/results persist under
  .local/reviews/policy-funding/browser/.
- Final full suite with preview server accidentally active: 1 failed, 1,424 passed,
  3 skipped, 203 subtests, 524.65 s. Failure was start_requires_preexisting_django...
  timing out because both QA and test HTTP servers used port 7667. This is execution
  interference, not a product regression. Stopped task-owned QA session 44452;
  isolated failed-test recheck passed in 3.49 s.
- A clean full suite is now running with ports 3210/7667 free. Log:
  .local/reviews/policy-funding/final-full-suite-isolated.log. Do not start a preview
  server until it finishes. No duplicate suite and no product code changes meanwhile.
- AST: 361 Python files; source 269/111,424 lines, tests 85/41,494, scripts 7/1,105.
  New-module/tests + local-content lint passes. Whole Ruff remains at the original
  1,561 findings; no new lint debt. Django check/drift passes, no model migrations.

## Completion and safe continuation

- Final clean suite: **1,425 passed, 3 skipped, 203 subtests in 515.01 seconds**;
  process exit 0, log final-full-suite-isolated.log. Previous port-interference
  failure is historical and remains documented above.
- Final acceptance: ../acceptance/2026-10-05-policy-funding-acceptance.md.
- Preview restarted only after the full suite: task-owned exec session 83324,
  http://127.0.0.1:7667/policy-funding/. Stop this owned preview before repeating
  lifecycle tests that bind ports 3210/7667. No automatic scheduled collection.
- Branch codex/policy-funding retains all implementation and documentation changes;
  no feature commit/push or old distributed task-checkbox update was performed.
- No incomplete implementation task remains in this plan. Further source coverage,
  credentialed APIs, automatic attachment extraction, high-volume/history benchmarks
  and distributed publishing are explicitly outside the accepted bounded feature.
- Ruling: keep the curriculum in typed Python content and a dated supplied-case
  verifier with portable evidence instead of a duplicate knowledge JSON or an
  arbitrary financial-claim inference engine. Cost if wrong: new cases require
  explicitly reviewed evidence and matching rules, rather than unconstrained inference.
- Before future work, read the acceptance and review closure, compare current HEAD/
  working tree, and add a new dated ledger entry. Do not redispatch completed research
  or rerun successful checks unless code/evidence changed or a new concern requires it.
- Final documentation check: 12 feature/canonical Markdown files, 93 local links,
  no broken links, unbalanced fences or trailing whitespace; git diff --check passed.
  Fixture provenance text was also normalized to plain English after the earlier
  PowerShell encoding issue. No code changed after the clean full-suite pass.

## 2026-10-06 follow-up: conversational explanations and consultant claims

- User supplied a second consultant message, with one long passage repeated twice,
  then asked whether the service can explain funding in that approachable style.
  This continues the authorized beginner-education feature; no new subsystem is needed.
- Current baseline remains codex/policy-funding at db5edec plus the existing
  uncommitted feature and documentation changes. Preserve all previous evidence.
- Scope: add short Korean question/answer explanations to the existing guide and
  exported guide, adjudicate the supplied claims in a dated report, and preserve
  official facts separately from opinions, forecasts and unverified eligibility.
- Public OLS first pages fetched through the existing safe transport on 2026-10-06:
  10 direct + 10 agency records, local raw evidence under
  .local/reviews/policy-funding/2026-10-06/. Reviewed relevant bodies 404, 409-412.
- Found a parser gap: explicit application-period headings were ignored because
  only receipt-period headings were recognized. New regression failed with None
  instead of the complete two-day/daily-hour window. Minimal repair accepts both.
- Completed: six shared-curriculum conversational answers in HTML/exported Markdown,
  twelve dated manual case groups, and the explicit application-heading repair.
- Evidence: 63 funding tests pass; Django check/drift and module/changed-test Ruff
  pass. Initial four Ruff formatting issues were repaired. Whole Ruff not rerun.
- Fresh live run 2026-10-06: forty notices, 47 requests, no errors; read_run and
  persisted dates 409-412 verified, conversational Markdown export verified.
- Actual desktop/mobile Brave inspection passes: six answers, 32 terms, twelve
  claims, expanded examples/exceptions, no page errors or horizontal overflow.
- Portable adjudication and limits:
  [consultant-claim follow-up](2026-10-06-policy-funding-consultant-claims.md).
  No universal debt ratio, January timing recommendation, year-end supply guarantee
  or unverified KOSMES date/graduate eligibility is introduced as an official rule.
- Follow-up complete; canonical service/code reports refreshed. No fresh full-suite
  claim: October 5's 1,425-pass result predates these narrow changes. No commit/push.
- Owned preview 83324 stopped and replaced by session 23948 on 127.0.0.1:7667.
  Only this session was restarted; unrelated listeners were preserved.
