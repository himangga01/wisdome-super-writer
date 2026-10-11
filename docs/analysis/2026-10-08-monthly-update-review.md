# Independent monthly refresh and year/month review

Latest revalidation: **2026-10-09, Asia/Seoul**. M1, M2 and Q1 are repaired in the
independently verified scope. Historical detail/return state, explicit/local
inventory selection and route-specific unknown-date presentation passed. The
stable source-parser scope passed independently and actual captured bytes were
replayed offline. No additional material review finding remains open. Full-suite,
new live source and browser/mobile acceptance remain separate parent-owned
requirements. Initial findings and their evidence are preserved below.

## Review checkpoint and scope

- Date: 2026-10-08, Asia/Seoul. Independent review during active implementation;
  the parent owns final revalidation after adjudicated repairs.
- Source baseline: `codex/policy-funding` at
  `db5edec35aad39ae85e3aca7b6f9e1b9f2e3a0dc`, plus the existing uncommitted
  funding, topic portal, monthly navigation and bundle storage changes.
- Starting authorities: project `AGENTS.md`, the shared
  `C:/Users/강지혜/.agents/analysis-policy.md`,
  [monthly design](../superpowers/specs/2026-10-08-monthly-update-design.md),
  [implementation plan](../superpowers/plans/2026-10-08-monthly-update.md),
  [progress](2026-10-08-monthly-data-update.md), and
  [storage diagnosis](2026-10-08-monthly-storage-diagnosis.md).
- Initial working tree contained existing modified instructions, README,
  canonical reports, bundle writer, housing collector/workflow, templates,
  settings and URLs, plus untracked funding/portal modules, tests and reports.
  This review changes only this report. Canonical registration in `AGENTS.md`
  is delegated to the parent to avoid concurrent edits.
- Reviewed date selection, housing stage parsing/history, housing `--since`,
  funding period/history/detail projections, semiconductor database/local archive
  projections and local CLI, shared calendar forms/CSS, local housing archive
  index, and native-validated Windows identity reconciliation.
- No live collection, network request, browser/reference access, server/port
  operation, migration, dependency change, commit or full-suite execution was
  performed. In-process Django Client calls are test routing, not HTTP.

## Purpose and implemented structure

The local operator portal separates housing, funding and semiconductor
workspaces. The requested change makes saved information navigable by KST year
and month while retaining its actual date meaning: explicitly labelled special
supply and first priority reception dates for housing, official notice posting
dates for funding, and original publication dates for news. Collection dates
remain separate. A missing date must not acquire the capture month.

`portal/periods.py` supplies strict query parsing, calendar boundaries and shared
UI state. Housing validates immutable run metadata before selecting stage dates
and resolving identities. Funding selects saved notice observations before
identity deduplication and display limits. The new semiconductor archive writes
local metadata with live/fixture provenance and per-source outcomes, rather than
creating distributed source approval or publication records. The portal's news
projection preserves positive observations across bounded or failed refreshes.

The storage repair changes identity-only snapshots after a demonstrated Windows
attribute discrepancy. Full content/read fingerprints, no-link/reparse checks,
inode/device/type comparisons and native verification remain distinct.

## Material finding

### M1 — P2: historical funding cards open a different saved notice version

**Historical reproduction; repaired and independently revalidated below.**

Before repair, the monthly list correctly returned the September observation of an identity
whose newer observation was posted in October. Both card links in
`src/templates/policy_funding/index.html:35` and `:45` supply only `mode`,
however. `src/apps/policy_funding/views.py:185` therefore resolves the detail via
`latest_notices` instead of the run selected by `period_notices`. A user selecting
September sees one version in the card and another version in the detail. A
selected historical record beyond the detail's default recent-run range can
also become unavailable even though the period list found it; that latter
variant is a source inference, not a separately executed reproduction.

Executed reproduction used two real, isolated fixture bundles produced by
`publish_collection`, with one stable identity:

1. September 20 posting/capture, title `SEPTEMBER ORIGINAL`.
2. October 6 posting/October 8 capture, title `OCTOBER CORRECTION`.
3. In-process request `/policy-funding/?year=2026&month=9&mode=fixture`.
4. Follow the actual rendered `.notice-card h3 a` link.

Actual result:

```text
list_status=200
list_has_september=True
detail_link=/policy-funding/notices/<stable-id>/?mode=fixture
detail_status=200
detail_has_september=False
detail_has_october=True
```

Priority: preserve the originating validated run in the period projection and
bind both card detail links to it. `detail` already supports a validated `run`
parameter. Preserve the search/region/kind/mode/period state on the return link.
The parent accepted this finding and implemented a maintained regression.
Independent repair verification is recorded below; the original failure evidence
is retained above.

## Bounded quality gap

### Q1 — selected news publication months do not explain excluded unknown dates

At the initial checkpoint, `load_news_archive` preserved a row with no original publication date under the
all-period or collection-date basis, and correctly excludes it from a selected
publication month. Unlike the housing and funding projections, it does not
return an excluded unknown-date count or add an explanatory note. The
semiconductor template likewise has no such note. This misses the design's
explicit unknown-date reporting requirement; it does not fabricate a date or
establish a failed source collection.

Executed isolated fixture evidence using the existing `FixtureFetcher`:

```text
all_total=4
unknown_original_dates=1
published_october_total=3
monthly_unknown_summary_keys=[]
monthly_message=bounded-local-metadata explanation + fixture label only
```

Suggested repair: count unknown original dates across validated provider
identities before the publication-period exclusion and display a concise
excluded-unknown note for an active publication selection. Keep all-period and
collection-date behavior intact, preserve provenance, and do not infer a
publication date. Parent adjudication/repair is not yet verified here.

## Additional material finding

### M2 — P2: existing database rows hide the newly collected local archive

**Historical reproduction with mocked SELECT projections; repaired and
independently revalidated below.**

Before repair, `load_semiconductor_data` read the local archive only when database reads failed
or the matching database count is zero (`semiconductor.py:236`). One matching
database row therefore takes precedence over every newly saved local metadata
row for the same selected period. The user-requested local monthly refresh can
therefore persist successfully while its new captures remain invisible through
the news portal merely because an independent database version exists.

Executed reproduction created a real validated October fixture archive with
three dated rows, then mocked only the database SELECT projections with one
distinct matching row. No database or migration was used:

```text
validated_archive_total=3
portal_kind=database_versions
portal_total=1
portal_titles=[DATABASE CHIP]
local_titles_hidden=[MOTIR fixture, SIA fixture, Samsung fixture]
```

The parent accepted this as P2 and supplied the following implementation ruling:
in local mode, automatic selection should prefer a validated local archive with
positive observations or a completed empty outcome, retain database drafts/runs
as separate operational data, and offer explicit `inventory=local|database`
selection. An all-failed archive with no previous positive observations should
permit automatic database fallback with an honest warning. Nonlocal mode
remains database-only. Preserve selection in UI links/forms. Local metadata must
not inherit database approval/status or be counted as the same distributed
source version. This is the accepted repair plan, not a verified implementation
at this review checkpoint.

The parent also accepted Q1 and assigned its maintained regression and fix to
the news owner. Independent repair verification and the remaining UI follow-up
are recorded below.

## Independent repair revalidation

Date: 2026-10-08, Asia/Seoul, after the parent interrupted its full-suite run to
allow the remaining production repairs. This reviewer still changed only this
report. The baseline commit is unchanged; the actively modified working-tree
files are the source under review. `AGENTS.md` now registers this report.

```powershell
.venv/Scripts/python.exe -m pytest -q tests/unit/test_funding_periods.py tests/unit/test_monthly_portal.py tests/unit/test_housing_collection_start.py tests/unit/test_portal_housing.py tests/unit/test_applyhome_subscription_schedule.py tests/unit/test_period_selection.py tests/unit/test_news_archive.py::test_publication_month_discloses_unknown_dates_without_assigning_capture_month
```

Actual result: **124 passed in 7.89 seconds**, exit 0. Same development/local,
UTF-8 and root-override removal settings as the original targeted commands.
This is a focused revalidation command, not an additional full suite.

### M1 verdict: repaired in the verified scope

`period_notices` now returns the originating `notice_runs` identity; the list
cards retain their selected run. Both card links bind that run. The detail's
return URL is built from a fixed `/policy-funding/` path and URL-encoded allowed
list parameters, omitting the detail run from the returned list selection.

The executed maintained regression uses real September and October fixture
bundles and follows the rendered link. It confirms the original September title
in detail, absence of the October correction, the original run in the URL, and
an exact return-parameter round trip for `mode`, `year`, `month`, `basis`, `q`,
`region` and `kind`. Following the return link produces one September result.
The current implementation addresses the reproduced card/detail mismatch.

### M2 verdict: repaired in the verified local/database scope

An independent temporary-root reproduction used actual validated good and
all-failed fixture archives, with mocked database SELECT projections containing
one database news row, one draft and one run. All assertions passed:

- Automatic local selection returned three local dated rows while the database
  had one matching row, preserving local counts/provenance.
- Explicit local and explicit database selection returned their respective
  inventories; their news counts were not merged.
- A healthy archive with no selected-period matches remained an available,
  explicitly empty local inventory.
- Database drafts and collection runs remained separate in the local projection.
- An all-failed local archive with no previous records allowed automatic
  database fallback with a failure warning; explicit local selection remained
  unavailable with `total=None`.
- Nonlocal automatic selection used database records. Explicit local selection
  stayed unavailable, and a patched guard confirmed no private local read.

The executed route regression also confirms explicit choices, invalid inventory
rejection, and inventory preservation in month navigation. The reproduction
used mocked SELECT rows, so it does not establish a real PostgreSQL deployment.

### Q1 verdict: summary, separate note and generic-message routing repaired

The exact archive regression now reports `unknown_date_count=1` and publication
selection excludes that undated record without assigning its capture month.
The collection-date selection retains all four rows, including the unknown
publication date. This correct data behavior is verified.

The template's new note currently checks only `period_selection.active` and a
nonzero unknown count. An independent real-fixture/in-process route reproduction
at `/semiconductor/news/?year=2026&month=10&basis=collected&inventory=local`
returned HTTP 200, four rows including the undated publication, and nevertheless
displayed the note saying the unknown item was excluded from the selected
publication month. The same note should not describe draft-modification months.

Reported this bounded follow-up to the parent: render this exclusion note only
for an active `published` basis on the news inventory rather than drafts. The
unknown count can remain available under collection selection.

The parent then restricted the separate template note to active publication
selection on news/overview. Independent execution of the maintained four-case
regression passed **4 tests in 1.85 seconds**, exit 0:

```powershell
.venv/Scripts/python.exe -m pytest -q tests/unit/test_monthly_portal.py::test_unknown_publication_note_only_describes_excluded_news
```

It verifies the note on publication news, its absence on collection-date news,
and its absence on drafts and source directories. This closes the reproduced
separate-note condition. The original failing observation is retained above.

A subsequent independent real-fixture routing check used the actual production
loader with mocked empty database SELECT projections. The loader's generic
`data.message` also contains the publication-date exclusion wording. The
template renders that generic message on all three of these sections, producing:

| Route, active publication basis | Separate template note | Generic exclusion message |
| --- | --- | --- |
| `/semiconductor/news/` | visible, correct | visible, correct |
| `/semiconductor/articles/` | absent | visible alongside `초안 수정일`, misleading |
| `/semiconductor/sources/` | absent | visible although no dated news inventory is shown |

All three responses were HTTP 200. The archive was a real validated temporary
fixture containing one unknown original publication date; no live source or
database was needed. This bounded route-presentation follow-up was reported to
the parent. At that checkpoint Q1 remained open for the generic message
condition. The parent stopped the affected full-suite attempt before making the
route-specific repair. Independent final closure follows; the interrupted suite
log does not establish acceptance. This reviewer made no product edits.

### Final Q1 route closure — 2026-10-09, Asia/Seoul

The final projection carries draft availability independently of the local news
inventory. A database error with a validated local archive sets
`drafts_available=False`; completed database reads set it true. The articles view
uses that draft availability and a draft-specific message. The source-directory
template hides generic news messages. Articles retain period controls while
their news inventory metrics are omitted.

Fresh command after the last metric-template edit:

```powershell
.venv/Scripts/python.exe -m pytest -q tests/unit/test_monthly_portal.py
```

Actual result: **16 passed in 3.20 seconds**, exit 0. These include the real
validated-archive/missing-database route regression and prior M1/M2/Q1 routing
controls. The earlier interrupted parent suite was not reused as evidence.

An additional independent temporary-archive/in-process reproduction used the
actual loader and template, first with a mocked `OperationalError`, then with
mocked successful empty SELECT projections. All assertions passed:

| Case | Result verified now |
| --- | --- |
| Missing database, articles | HTTP 200; draft availability false; honest unavailable-draft message; no publication-exclusion wording; no news metrics; period controls retained |
| Missing database, sources | HTTP 200; no generic news status/publication-exclusion message |
| Missing database, news | HTTP 200; local news available with three dated records while draft availability remains false; correct original-publication exclusion note and news metrics retained |
| Successful empty database, articles | HTTP 200; draft availability true; honest empty-draft message; no news/publication-exclusion message or metrics; period controls retained |

This reproduces both previously misleading routes and checks the newly edited
metric guard directly. Q1 is closed in this verified backend/template scope;
news availability does not establish database draft availability. No live HTTP,
browser, port, full-suite, migration or ledger operation was performed.

Source SHA-256 at this final route checkpoint:

| Source | SHA-256 |
| --- | --- |
| `portal/views.py` | `73cd9ede3ce9cf6c494749cff1907423592df37ff1966d7bb2555ab04bb178fd` |
| `portal/semiconductor.py` | `7f6ff6d32070e737cecfc4e8148e842aa871e7e1b9a4c59e7431fe8f5285e428` |
| `templates/portal/semiconductor.html` | `21ee7c7165732d8ddf69255ee52aaf5a31aa923d7b8bbd444f5929194c88ec72` |

### Housing stage verdict and deferred scope

The 124-test command revalidated explicit special/first stages and regional
association, stage-specific visible facts, rejection of generic/second-priority
and eligibility facts, malformed/unknown dates, September-announced October
receptions, period selection before identity deduplication and display limits,
and the bounded `--since` workflow/CLI. No new material stage-semantics defect
was reproduced. This is fixture/backend acceptance; no new live housing run or
browser/mobile rendering was performed.

At the initial revalidation checkpoint, Samsung metadata-only media independence
and the SIA exact known type/date-prefix parser changes were deferred at the
parent's request. The parent subsequently declared the production bytes stable;
the independent result below supersedes that temporary deferral.

### Stable local source-parser verdict: scoped and offline replay passed

The final Samsung local helper first parses with defused XML, retains only
title/link/original publication/updated/identity metadata, and strips body,
media and enclosure fields before calling the existing strict shared parser.
It does not guess an enclosure MIME type, fetch media, alter the distributed
feed parser or substitute an update timestamp for original publication.

SIA normalization matches only a flat `Press Release|Blog: MM/DD/YY` metadata
node, then reuses the existing strict list parser. Unknown labels, extra or
multiple dates, invalid calendar dates, child markup and unchanged original
years are not converted into approved dates. Existing host/path/rights and
collection-window guards continue after normalization.

```powershell
.venv/Scripts/python.exe -m pytest -q tests/unit/test_news_archive.py
```

Actual result: **64 passed in 7.90 seconds**, exit 0. Controls include missing
original links, foreign origins, XML entities, irrelevant MIME-free media,
strict shared-parser attachment rejection, unsupported/ambiguous SIA labels and
dates, preserved original year, MOTIR identity wrappers, redirects, robots,
polling/rate limits, bounds, archive integrity and failure outcomes. No new
material parser/safety concern was reproduced in this scope.

Independent offline replay read the actual diagnostic payloads under
`.local/state/semiconductor-diagnostics/a4eeb7cef82b496cbb33a2f121b9f111/`, verified
each body's SHA-256 against its diagnostic index, and ran the stable pure
metadata helpers plus `_metadata` using the saved capture window. Exit 0:

| Source | Parsed metadata items | Captured October window matches |
| --- | ---: | ---: |
| Samsung | 50 | 0 |
| SIA | 12 | 1 |

The SIA match retains original `2026-10-05T00:00:00+00:00` and
`internal_analysis_only` rights. This establishes compatibility with those
captured bytes; it is not a new live update or exhaustive monthly coverage.

Read-only validation of current real archive `2026-10-08--run-5c8262014531`
returned mode `live`, one saved record, MOTIR `completed`, Samsung and SIA
`failed`, KRX `unsupported`, and SK hynix `disabled`. Its original failed source
outcomes remain intact. The offline SIA match is not a persisted live row, and
the offline Samsung zero does not rewrite the historical failed outcome.
No HTTP, browser, port, polling-ledger, source approval or archive mutation was
performed by this review.

Reviewed source hashes at the preceding stable-parser checkpoint (the later
route/projection hashes above supersede those affected files):

| Source | SHA-256 |
| --- | --- |
| `portal/news_archive.py` | `2c6e4bab72b64152c3dd172057beb360ff5f69139a0b94e7bdd95218dd2b449d` |
| `portal/semiconductor.py` | `4fd4da90a05e89f16f6732577ef92ab1029632395ade029432123b01fce5a4a8` |
| `policy_funding/views.py` | `eac7a5f50cb5b417edd82756b3d5989ac57458209610ad7b689b79d643b5c203` |
| `policy_funding/storage.py` | `6e588ee5eed434dd5d0a08b546a47469195f2f89d4a327bf52c91eed153d47d1` |
| `local_content/bundles.py` | `1d1979fb4b0a9202ee1aface4434e61b7c086859f7cd5b1362e5ee6932dd5d4a` |

## Executed verification and boundaries

Environment: existing Python 3.12 virtual environment; development/local mode;
`PYTHONUTF8=1`; inherited local-root overrides removed for pytest.

```powershell
.venv/Scripts/python.exe -m pytest -q tests/unit/test_period_selection.py tests/unit/test_funding_periods.py tests/unit/test_news_archive.py tests/unit/test_monthly_portal.py tests/unit/test_applyhome_subscription_schedule.py tests/unit/test_bundle_windows_metadata.py
```

Actual result: **100 passed in 7.08 seconds**, exit 0. These checks cover KST
boundaries, invalid/repeated date inputs, stage facts and regional association,
period-before-identity selection, unknown dates, bounded archive integrity,
robots/poll/rate controls, failure-versus-empty outcomes and the native Windows
identity conditions. They did not cover M1 before this review's reproduction.
The historical full suite and other agents' scoped runs are not new acceptance
evidence from this reviewer.

An additional independent housing scope exercised the `--since` command and
workflow fixture window, housing validated reads and historical date selection:

```powershell
.venv/Scripts/python.exe -m pytest -q tests/unit/test_housing_collection_start.py tests/unit/test_portal_housing.py
```

Actual result: **85 passed in 5.77 seconds**, exit 0. Total targeted review
verification is 185 passing tests across the two distinct commands; this is not
the full suite or live housing acceptance.

Three isolated Python reproductions additionally verified M1, Q1 and the
database/local precedence question. All used temporary roots. The first M1
attempt failed because Django rejected the test client's default `testserver`
host; rerunning with `HTTP_HOST=127.0.0.1` reached the intended in-process routes
and produced the result above. No network request or listener was involved.

Static review also checked `--since` propagation to the bounded KST workflow
window, read-only archive access boundaries, preserved form parameters, native
form controls and external CSS under the current CSP. Those source reads do not
establish browser/mobile visual acceptance. No new P1/P2 substitution-defense
defect was reproduced in the Windows repair; its maintained targeted controls
passed in the command above. This is bounded evidence, not a claim of exhaustive
filesystem-race safety.

At the reviewed checkpoint, source SHA-256 values were:

| Source | SHA-256 |
| --- | --- |
| `portal/periods.py` | `67136a6bec9b8f8b1a181db836be307a0d32d195a75b239c7fcfadd377fc5728` |
| `portal/housing.py` | `29ed65678ceb0308a352bf9e5639fa019042a4780c21aef42bfb1852736ab439` |
| `portal/semiconductor.py` | `7d73024ce62ad769493a96ed4dc0335bc49f12407aefc35ae1c3124ab1675c94` |
| `portal/news_archive.py` | `ab0258b0cf54897d9fbb2dbb52674348931eaa750238ad46d4ec6c382d6c7800` |
| `local_content/bundles.py` | `1d1979fb4b0a9202ee1aface4434e61b7c086859f7cd5b1362e5ee6932dd5d4a` |

The parent handoff reports validated housing history of 175 identities with 23
October-stage matches and 93 validated funding notices with 17 October postings.
Those counts were not reread by this reviewer. The 218 legacy-agency detail
failures and all supported semiconductor source schema failures remain source
coverage limitations. Review fixtures must not be used to label the live news
update successful. Persisted polling intervals remain authoritative; this review
neither reset ledgers nor performed a new poll. Current live counts and final
release identity must be revalidated by the parent.

## Continuation and update log

- Priorities: the reviewed M1/M2/Q1 repairs are verified; perform the parent's
  final necessary checks and documentation
  refresh. Do not duplicate unrelated older R1–R16 findings or mark acceptance
  complete from this scoped result.
- Read this review and monthly progress before a later review, compare current
  bytes and dated acceptance evidence, and update the affected findings after
  repair verification. Preserve the original reproduction and status history.
- 2026-10-08: created independent review, recorded two reproduced P2 findings
  accepted by the parent, an accepted bounded unknown-date reporting gap, 185
  passing targeted tests, accepted inventory-selection ruling, and explicit
  verification limits. Parent/assigned implementers own repair verification.
- 2026-10-08 repair revalidation: 124 focused tests passed; independently
  verified M1's historical detail and complete return state, M2's selection and
  failure matrix, and housing stage semantics. Q1's data summary passed but its
  collection-basis display note remains an independently reproduced follow-up.
- 2026-10-08 final stable-parser checkpoint: Q1's separate-note regression passed
  4/4 after repair; its generic message still mislabels drafts/source pages and
  was reported. Final archive scope passed 64 tests; actual hash-checked Samsung
  and SIA captures replayed offline with 0 and 1 October matches respectively.
  Real live archive validation preserved the original one-record partial result.
- 2026-10-09 final route checkpoint: independently verified 16 passing route
  tests after the last metric-template edit plus actual loader/template fixture
  reproductions for both unavailable and available database draft stores.
  Q1's generic-message follow-up is closed, M1/M2 closure is preserved, and no
  further material finding remains open in this bounded review. Full-suite/live
  source/browser acceptance remains parent-owned.

### Subsequent parent verification — 2026-10-09 KST

Parent final code checks returned1,689 passed,3 skipped and203 passing subtests
(540.75 seconds), plus clean Django check/drift and scoped Ruff. After eligible
polling, its normal live news run validated two records with all three supported
sources completed (Samsung successfully empty). Parent actual-data in-process
pages verified housing23/funding17/news2, bound funding detail/return state and
explicitly unavailable database drafts. These results are parent evidence;
this reviewer performed no new HTTP or browser operation. See the
[final progress authority](2026-10-08-monthly-data-update.md) for counts/limits and
runtime ownership. At that parent checkpoint direct Chrome/mobile acceptance and
exhaustive coverage remained unestablished; historical review reproductions and
interrupted suites are retained.

Subsequent parent browser-only acceptance on October 9 succeeded in the
user-authorized fresh chat using sanctioned Chrome. Desktop and 390 x 844
responsive menus/period/overflow, saved October counts and funding detail/return
state are recorded in the [canonical browser diagnosis](2026-10-09-browser-access-diagnosis.md).
This is separate parent evidence, not a new browser execution by this reviewer.
No code/data/collection or full-suite rerun occurred; exhaustive coverage and
distributed/publishing gates remain unestablished.
