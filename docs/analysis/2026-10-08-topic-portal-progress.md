# Three-topic portal redesign: restart checkpoint

Portal-only checkpoint: implementation, independent-review repairs and server-side
verification completed in the working tree before the monthly changes. Its suite: 1,502 passed, 3 skipped,
203 subtests in 529.16 seconds; process exit 0. Log:
.local/reviews/topic-portal/final-full-suite.log. User-supplied screenshots provided
the visual reference. Later October 9 user-authorized fresh-chat Chrome checks
accepted the root, all topic menus, period controls and responsive overflow.
See the browser-only follow-up below for actual coverage and limits.

Current monthly integration/actual collection authority:
[monthly progress](2026-10-08-monthly-data-update.md), continued October9 KST.
Its checks supersede the portal-only test result for current bytes. Preview53227
was stopped before monthly full-suite verification. Read the latest task-owned
server/session state there before resuming; do not assume the old preview runs.

October9 browser diagnosis now supersedes the earlier unresolved mismatch below:
[confirmed conversation permission override](2026-10-09-browser-access-diagnosis.md).
The original chat's saved denials took precedence over its allowed global default.
Recovery subsequently succeeded in authorized fresh chat
`01a11c68-dc66-7b60-b39d-0c14a6716e8a`, using normal Chrome APIs for both exact
origins. The historical denial sections below are retained as dated evidence;
they are not the current acceptance state. Read the diagnosis before further
settings/restart instructions or browser work.

## Baseline and authorization

- Date: 2026-10-08, Asia/Seoul.
- Repository: C:/2026_project/wisdome-super-writer.
- Branch: codex/policy-funding.
- HEAD: db5edec35aad39ae85e3aca7b6f9e1b9f2e3a0dc.
- Existing uncommitted funding implementation, README, AGENTS and canonical
  report changes are present and preserved. No product code changed for this task yet.
- User authorized the complete dashboard UX redesign, separate housing,
  semiconductor and policy-funding pages, topic-specific menus, mobile support
  and direct Chrome acceptance.
- Requested visual reference: https://ref.theseeker.io/r/7526.
- The latest user specifically requested a Chrome approval prompt and direct opening.
  Reference access and Chrome testing remain required deliverables.

## Starting references and confirmed implementation

Read project AGENTS, shared analysis-policy.md, canonical service/code reports,
funding follow-up, current routes/templates and local preview safeguards.
Superpowers brainstorming, writing-plans, dispatching-parallel-agents and
worktree guidance were read. Worktree metadata is currently read-only under the
execution policy; no Git metadata or unrelated working changes were altered.

- The root currently redirects to the staff-only administration console.
- Funding has an independent green header and pages for notice search, education,
  sources, detail and run history.
- Local housing has a separate minimal preview layout and run/article pages.
- Semiconductor adapters and distributed data models exist, but a dedicated
  content portal does not yet exist.
- The tentative direction is a common portal entrance and three independently
  routed workspaces, each with its own topic color, label and task-specific menu.
  This is a proposal awaiting reference inspection, not an implemented design.
- Existing local-only housing access, immutable bundle validation, staff controls
  and financial provenance distinctions must survive the redesign.

## Browser permission blocker

1. web.open could not fetch the reference (cache miss).
2. The sanctioned Chrome createBrowserTab call for the reference was rejected by
   browser security policy, initially citing declined user permission.
3. A separate sanctioned Chrome call for http://127.0.0.1:7667/ was also rejected
   with declined user permission.
4. After the user explicitly asked to request approval and open Chrome, the same
   reference call was retried. It was rejected because a saved user permission
   setting blocks access. No approval prompt was made available by the tool.
5. No permission-management tool is exposed in the available tool metadata.

The user was informed that the saved browser permission for ref.theseeker.io and
127.0.0.1:7667 needs to be allowed through the application's permission controls.
Do not bypass the denial using another browser surface, Playwright, raw CDP,
native automation, shell/network fetching or screenshots from another service.
Only retry after a user-reported permission change or a new authorized tool state.

No Chrome navigation, reference screenshot, desktop/mobile browser verification
or reference-matching claim has succeeded in this task. The exact reference layout
is unknown. Do not claim to have used it.

### Verified user-facing permission recovery guidance

On 2026-10-08, official OpenAI documentation was searched and opened after the
user asked where to remove the saved block:

- [Browser extension: manage allowed and blocked websites](https://learn.chatgpt.com/docs/chrome-extension):
  in the desktop app, Settings > Computer Use > Manage beside Chrome opens the
  shared website allowlist/blocklist. Removing a host from the blocklist allows
  a new approval request. The relevant hosts are ref.theseeker.io and 127.0.0.1.
- [Desktop app settings](https://learn.chatgpt.com/docs/app/settings):
  Settings > Browser > Agent permissions also documents default Browse permissions
  and per-website exceptions with Requires approval, Always allow and Block.
  Removing an exception restores defaults.

These are official documented UI paths, not proof of which app version/layout the
user currently sees. Earlier generic advice to change browser permissions lacked
the exact path and was corrected. No permission setting was changed by the agent.

### Retry after the user reported changing permissions

The user said the change was complete and requested another attempt. The sanctioned
Chrome createBrowserTab call was retried for the exact reference and then for
http://127.0.0.1:7667/. Both returned the saved-user-permission block again.
This is evidence that access remains blocked in the tool's current state; it does
not establish that the user failed to change the displayed setting. No alternate
browser/network route was attempted. Reference inspection and Chrome acceptance
remain incomplete.

### Screenshot and retry in the refreshed session

The user supplied the Chrome permissions screen. Its visible default row shows
Always allow for the column labelled web browsing/search and downloads; no
individual blocked-site row is visible. Chrome is shown as installed. A sanctioned
browser inventory also confirmed a connected Chrome extension. This does not prove
all effective policy layers, but it contradicts treating a visible user block as
established fact. Do not keep telling the user to loosen permissions.

After an interrupted turn, the execution environment changed to unrestricted
filesystem/network access and the user explicitly asked to retry here. The exact
sanctioned Chrome requests for the reference and local preview were repeated.
Both still returned the saved-user-permission block. Shell sandbox access and the
browser's authorization state are separate; broader shell permissions did not
establish browser access.

Current diagnosis: a mismatch between the visible settings and the tool's effective
permission decision remains unresolved. A specific client defect or stale cache
has not been proven. The documented troubleshooting step is to restart the desktop
app and Chrome, then retry; if persistent, collect the chat ID and report it through
the app's feedback flow. No browser reinstall, permission-file edit, alternate
browser path or remote-browser workaround was attempted.

## Parallel read-only reconnaissance

Two bounded read-only workers were asked to document existing integration points;
neither is authorized to bypass the browser restriction.

- [Semiconductor data reconnaissance](2026-10-08-portal-semiconductor-recon.md):
  current source/news/draft models, topic scoping and unavailable database states.
- [Housing reconnaissance](2026-10-08-portal-housing-recon.md):
  validated run metadata, notice dates and local-only access constraints.

Initial findings to retain:

- Housing schema-v2 notices carry application dates, categories, regions and
  source links. The file must be bound to the run manifest; legacy schema-v1
  metadata must not be treated as validated schema-v2 notice data.
- Same-day random run suffix ordering is not actual execution chronology.
- The local database may lack the distributed news/source/draft tables. Display
  unavailable storage separately from a successful query with zero records.
- Source registration/configuration does not prove active collection coverage.

## Continuation steps

1. Confirm the browser permission setting has changed, then open the exact reference
   in Chrome using the sanctioned browser tool and inspect desktop/mobile layout.
2. Read both reconnaissance reports and current bytes before making integration decisions.
3. Finish the English portal design and implementation plan. The user's broad
   authorization covers routine design and implementation decisions; do not ask
   for repeated design approvals.
4. Implement a common portal plus independent topic pages and menus, preserving
   current data and security boundaries. Use real records or explicit empty/
   unavailable states, never invented notice counts or news.
5. Run meaningful routing/isolation/access tests, Django checks, scoped lint and
   direct Chrome desktop/mobile acceptance. Keep browser acceptance distinct
   from backend rendering or historical tests.
6. Refresh this ledger and canonical service/code analyses with actual evidence.

No implementation tests were run in this reconnaissance stage. No commit, push,
deployment or process restart was performed. The requested redesign remains open.

## Blank Chrome window clarification

The user supplied a screenshot showing the task's Chrome window at about:blank.
Chrome/window creation therefore occurred; navigation to the requested reference
was the blocked step. Earlier wording implying Chrome itself never opened was
imprecise. The screenshot is not evidence that website authorization changed.
Do not retry blocked navigation or request manual navigation as a way around the
denial. Continue independent source-code implementation and server-side tests;
reference matching and direct Chrome acceptance remain outstanding.

## User-provided visual reference and implementation resumption

The user supplied the reference dashboard screenshot and palette directly. The
images show a fixed left rail, pale gray canvas, white rounded cards, black active
navigation, lime accents, compact metrics/table and dark guidance panel. This
material can be used directly without accessing the blocked website. The design
has been updated to use this visual direction. The live reference/video has still
not been inspected; Chrome desktop/mobile acceptance remains blocked.

Proceed with the approved three-topic implementation using the supplied images,
existing source code and server-side tests. Design/plan:
../superpowers/specs/2026-10-08-topic-portal-design.md and
../superpowers/plans/2026-10-08-topic-portal.md.

## Implementation checkpoint

- Implemented common root portal, distinct housing/semiconductor/funding routes,
  shared navigation context, a black/white/lime CSS shell and mobile details menu.
- Housing data projection uses schema-v2 hash-bound snapshots and safe archive
  links. Semiconductor data is topic-scoped, excludes tombstones and mislinked
  revisions, and distinguishes database-unavailable from zero saved versions.
- Housing remains local+loopback-only before any file projection. Other portal
  pages retain local-loopback/nonlocal-active-staff access. Existing console and
  historical article/asset safeguards are preserved.
- Adapted funding and housing archive templates to the common layout; added
  funding glossary and collection history. Existing funding knowledge and
  original supplied-case meaning remain unchanged.
- Initial new route/access tests: five expected failures (root redirected, missing
  portal module and new funding routes). After implementation, 77 focused tests
  passed. Added seven actual template-rendering cases for long hostile titles and
  topic isolation; updated route/preview/funding group: 33 passed.
- Projection workers independently recorded RED/GREEN evidence: housing 35 new
  cases plus 15 existing preview tests pass; semiconductor 16 cases pass.
- Django check and migration drift pass. Five initial scoped lint formatting
  findings were corrected; scoped Ruff now passes. Static collection copied two
  changed/new files and postprocessed 390. git diff --check passes.
- Full unit/integration suite is running; log .local/reviews/topic-portal/full-suite.log.
  Ports 3210/7667 had no listening connections when started; no preview was started.
  Do not run a duplicate full suite or infer completion from partial log output.
- Independent scoped review is running. Browser/reference URL access remains
  blocked; no browser rendering or mobile interaction acceptance is claimed.

## Independent review adjudication

- Review completed with two reproduced P2 findings and no additional established
  P1/P2. The historical pre-repair findings remain in topic-portal-review.md.
- TP-R1 accepted: the housing view previously searched only the displayed first
  100 records. The loader now filters the full bounded selected snapshot before
  limiting display. total remains snapshot size; matching_total records matches.
  New title/publisher/region cases prove that a 101st matching record is found.
- TP-R2 accepted: the loader preserved source status but the template omitted it.
  Main reproduced two failing overview/list rendering checks, then added escaped
  saved-status wording. The seven rendering cases pass after correction.
- Ruling: keep limits explicit and preserve saved facts rather than infer live
  application status. Cost if misunderstood: users could mistake stale source
  wording for current availability, so the saved-observation label is retained.
- The first full suite was deliberately interrupted before these repairs; its
  partial log is not a passing result. A fresh full suite is required.
- The repaired full suite is session 41115, final-full-suite.log. Read-only process
  inspection confirmed a single pytest process chain, not a duplicate prior run;
  the test runner reached Windows setup-script fixtures. Buffered progress output
  alone must not be interpreted as a hang or completion.
- AST parsing passed for all 371 Python files: src 276 / 112,280 lines; tests 88 /
  42,381 lines; scripts 7 / 1,105 lines. This is syntax/inventory evidence, not
  semantic coverage or browser acceptance.
- The reference images supplied by the user are now preserved in
  docs/reference/topic-portal/. They are reference assets, not application captures.

## Final server-side verification and runtime handoff

- Repaired full suite: `.venv/Scripts/python.exe -m pytest -q tests/unit tests/integration`
  returned **1,502 passed, 3 skipped, 203 subtests passed in 529.16 seconds**, exit 0.
  This supersedes the interrupted initial run, not historical acceptance records.
- Final Django check: no issues. `makemigrations --check --dry-run`: no changes.
- Scoped Ruff passes for portal, funding, local-content and all three new test files.
  Whole-repository historical lint debt was not reclassified as passing.
- Final collectstatic: 2 copied, 138 unmodified, 390 postprocessed. Eight Markdown
  reports and 102 local links passed UTF-8, fence and link checks before this final
  ledger update; git diff --check passed. No model/dependency changes.
- Preview started only after the full suite, task-owned session **53227** at
  http://127.0.0.1:7667/. Startup system checks passed. Stop only this owned session
  before future lifecycle tests need port 7667. No browser was opened or queried.
- Startup reports **71 existing unapplied migrations** in the actual local runtime
  database. The new semiconductor projection correctly handles unavailable schema;
  its tests used isolated test storage. Actual news ingestion/admin operation is
  not established by this UI change. No migration was applied to the user's runtime
  database and no live source collection/publishing was executed for this task.
- No commit or push was performed. The existing feature branch and earlier work
  remain intact. No global machine policy, browser permission or extension was changed.

## Remaining acceptance

The only requested UI acceptance not executed is direct Chrome desktop/mobile
testing. Browser navigation is still denied by the tool even though the supplied
settings screenshot shows an allow default. The blank Chrome window proves window
creation, not successful navigation. Do not mark this acceptance complete or use
alternate browser/CDP/network paths to bypass the denial.

When authorized browser access actually works, inspect the root and all topic
menus at desktop and mobile widths, search and empty states, source/detail/archive
links, mobile menu keyboard/touch behavior, horizontal overflow and focus order.
Compare rendered spacing/contrast with the saved user reference images and repair
any verified defects. Use the existing implementation and fresh evidence; do not
redo completed reconnaissance or the whole test suite without a new reason.

## Dedicated Chrome session retry

After implementation, the user explicitly requested a completely new dedicated
Chrome window/session. The sanctioned createBrowserTab request used a fresh
session name for http://127.0.0.1:7667/. The tool again rejected navigation with
the saved-user-permission block. New-session creation did not establish website
access; no direct Chrome acceptance succeeded. No alternate access method or
permission change was attempted. Product code and passing test evidence are unchanged.

## Browser-only acceptance closure — 2026-10-09 KST

The user-authorized fresh chat reused the current checkout, uncommitted product
work and preview PID 30240. Sanctioned Chrome opened both requested origins.
The root title, three topic cards and independent menus were visible. The actual
reference page and initial video frame confirmed the sidebar/card/black/lime
visual direction; complete video playback and pixel-identical matching are not
claimed.

Default desktop and official 390 x 844 viewport checks exercised topic changes,
mobile menu pointer activation, funding menu Return toggling, native year/month
submission, housing stage selection, year-only/all-period controls, honest empty
searches and query preservation. All listed internal menu destinations rendered;
database drafts stayed explicitly unavailable. October counts were housing 23
(special 18 / first 21, overlap), funding 17 and local news 2. Funding detail bound
the immutable saved run and returned to the selected October list. Inspected
pages had `scrollWidth == clientWidth`; the viewport was reset and the root tab
marked as a deliverable.

Actual captures, DOM states, geometry and intermittent screenshot limitations are
in the canonical [browser diagnosis](2026-10-09-browser-access-diagnosis.md).
Readable full-page mobile captures closed the initial screenshot scaling gap.
No material new product defect was reproduced; no service-code change, collection,
poll-ledger edit, migration or full-suite rerun was required. This closes the
requested Chrome desktop/responsive acceptance; physical touch/assistive-tech,
all filter combinations, external links and the complete reference video remain
unverified. Existing source/publishing operational gates remain separate.

Update log: October 9 fresh-chat browser recovery and responsive acceptance passed;
the prior original-chat denial history and completed backend evidence are preserved.
