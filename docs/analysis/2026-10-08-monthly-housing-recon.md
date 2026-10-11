# Monthly housing collection and archive reconnaissance

Current authority: the **Schedule-date implementation follow-up** below supersedes the initial read-only recommendations where they differ. The user chose actual special-supply and first-priority subscription dates for housing, not publication month or generic application dates. The approved implementation now adds `--since`, stage facts and validated monthly history. The original source baseline and reconnaissance remain historical evidence. The parent owns the coordinated broader live collection and shared UI/acceptance report.

## Baseline, scope and authority

- Analysis date: 2026-10-08, Asia/Seoul.
- Repository: `C:/2026_project/wisdome-super-writer`.
- Branch: `codex/policy-funding`; HEAD: `db5edec35aad39ae85e3aca7b6f9e1b9f2e3a0dc`.
- Scope: source-only reconnaissance for the authorized October 2026 collection and year/month portal selection. This pass writes only this English report. It does not implement the monthly feature or execute collection.
- Read-before authorities: [AGENTS.md](../../AGENTS.md), shared `C:/Users/강지혜/.agents/analysis-policy.md`, [service analysis](../service-analysis.md), [code analysis](../code-analysis.md), [portal housing reconnaissance](2026-10-08-portal-housing-recon.md), and the [portal progress and browser checkpoint](2026-10-08-topic-portal-progress.md).
- Initial working tree already contains tracked changes in AGENTS, README, service/code/review/plan documents, local bundle helpers, local-article/console templates, settings and URLs; funding and portal code/config/static/templates/tests and dated reports are untracked. They were preserved. The current portal projection is working-tree implementation, not part of HEAD.
- Reference and local browser origins remain denied. No browser, alternate browser, CDP, local web request or reference access was attempted.
- Parent agent owns AGENTS registration and shared-report updates, avoiding concurrent edits to common authorities. Re-read this report and current bytes before reuse; refresh affected sections after monthly behavior changes.

The service purpose is to collect official housing observations into immutable local runs, expose saved notices and article archives, and retain evidence without presenting saved observations as current application availability. The local housing collector is separate from distributed database collection and policy-funding storage.

## Current collection entry points

The exact command is `collect_recent_housing`. Its argument declarations are at `src/apps/local_content/management/commands/collect_recent_housing.py:54`.

| Argument | Existing declaration and effect |
| --- | --- |
| `--days` | Integer, default `7`, choices `1..31`; includes the current KST calendar day |
| `--humanize` / `--no-humanize` | `argparse.BooleanOptionalAction`, default `True` |
| `--write-articles` / `--no-write-articles` | `argparse.BooleanOptionalAction`, default `True` |
| `--selected-id` | Repeatable, default empty list; selects detailed articles, not which notices to collect |
| `--fixture-root` | Optional path; only the approved checked-in fixture root is accepted |
| `--dry-run` | Boolean flag; collects but does not commit a portal snapshot |

There is no CLI `--start`, `--end`, `--year`, `--month`, source selector, list-page cap, detail-count cap, total-request cap or overall-deadline argument. `handle()` requires local runtime, calls `_build_workflow(fixture_root: Path | None, dry_run: bool)`, and passes `now=datetime.now(SEOUL)`.

The source-level workflow signatures are:

```python
LocalHousingWorkflow(
    *, collectors: Iterable[Collector], humanizer: Humanizer | None,
    output_root: Path, state_root: Path | None = None,
    mode: str = "live", dry_run: bool = False,
)
LocalHousingWorkflow.run(
    now: datetime, days: int = 7, humanize: bool = True,
    write_articles: bool = True, selected_ids: Collection[str] = (),
) -> WorkflowReport
```

`run()` converts aware `now` to KST and sets `window.start` to midnight on `now.date() - (days - 1)` and `window.end` to the captured `now`. Publication matching is inclusive at both ends. Parsed public notice publication dates are midnight KST. On October 8 KST, `--days 8` therefore selects publication dates from October 1 through the command's start time on October 8. If the date changes to October 9 before invocation, `--days 8` starts October 2; the operator must recompute the day count or use an explicitly anchored programmatic call. This is month-to-date, not an October 31 future window.

CLI backfill is limited to 31 calendar days ending now. The workflow's programmatic `now` can anchor an older month, but that does not establish that current public boards still expose all historical notices or that historical fetches have been accepted.

## Saving notices without generating articles

**The existing workflow can persist a validated notice-only snapshot.** Use both `--no-humanize` and `--no-write-articles`, without `--dry-run`.

`workflow.py:548..709` skips rendering, image generation, humanizer transformation and humanized verification when `write_articles=False`. The later write branch (`workflow.py:710..840`) still writes:

- `index.md`, with selected detailed links labeled `NOT WRITTEN: WRITE_DISABLED`;
- `notices.json`, containing all admitted residential notices rather than only detailed selections;
- `raw-observations.json`, containing source counts, collection codes and identity/checksum observations;
- schema-v2 `manifest.json`, binding index and both metadata files by hashes, with an empty article-bundle inventory;
- a mutable `acceptance-report.json` for operational phase/status reporting.

The live builder still constructs `HumanizerClient(settings.HUMANIZER_BASE_URL)`. Its constructor validates the configured loopback URL but does not perform a health request or transform. A running humanizer is therefore unnecessary for the notice-only path; an invalid configured URL can still prevent construction. Do not call local runtime status merely to display saved data, because it probes humanizer health.

`--no-humanize` alone, while keeping article writing enabled, marks eligible article work `HUMANIZATION_DISABLED`, stores blocked diagnostics, and cannot create final verified articles. `--dry-run` saves no committed run and cannot update the portal. A complete notice-only live collection deliberately reports `live_success=False`: that field also requires `write_articles=True` and represents the article-writing acceptance path, not merely successful source reads.

Both failed sources block final index/manifest publication; one failed source can leave an immutable incomplete snapshot from the surviving source and return a nonzero command result. Source detail failures likewise preserve list observations with unknown details, produce an incomplete run, and affect command exit status. Inspect committed metadata and sanitized result codes rather than treating an exit code or a directory alone as proof of complete coverage. `HousingCollectionResult.complete` checks source errors; quarantined identity conflicts are stored separately, so a true `complete` flag does not prove that no conflict was excluded.

The CLI `article_count` is the number of selected article-work records, not files written. Notice-only collection can report a positive `article_count` with `written_count=0` and no manifest-referenced articles. Use the latter count and validated bundle inventory for written-content totals.

The workflow retains a filesystem guard, unique immutable run generations, atomic writes and existing bundle validation. It also scans prior detailed-article runs even when article generation is disabled, with a 512-candidate prior-run limit (`workflow.py:916`). Older runs are not overwritten by a newer collection.

## Actual source scopes and bounds

| Source | Implemented list and date behavior | Bounds and coverage limits |
| --- | --- | --- |
| ApplyHome | GET `https://www.applyhome.co.kr/ai/aia/selectAPTLttotPblancListView.do` and `selectAPTRemndrLttotPblancListView.do`; `_list_page_url()` adds only `pageIndex`. It traverses both full visible lists before applying `window.contains_publication()` locally, then fetches detail for every in-window notice. | Maximum 100 list pages per board. Current public-table parser expects at most 10 rows/page, and full nonfinal pages. There is no date search parameter or early stop at an older publication date. The fixture-style parser accepts a different marked-up structure; do not generalize the public 10-row limit to every accepted fixture shape. |
| LH | POST `https://apply.lh.or.kr/lhapply/apply/wt/wrtanc/selectWrtancList.do` using `_list_form(window, page)`, with `schTy=0`, KST `startDt`/`endDt`, `currPage`, `listCo=50`, `viewType=srch`, `mi=1026` and blank title/region/type filters. It validates every returned publication against the requested window and fetches every listed detail. | Maximum 100 pages; exact 50-row page cardinality and final-page count are checked. Regions and notice types are not restricted at list level. The merge later retains only recognized residential categories; land/commercial/unknown categories do not become notice cards. |

ApplyHome scope is its APT and remaining-supply boards. LH scope is that public notice list followed by the residential selection policy. This does not establish collection of every Korean housing provider, every ApplyHome board, attachment-only facts, or an exhaustive national inventory. Detail HTML is fetched for each eligible list observation even when article writing is disabled; the flags skip generation, not detail collection.

`OfficialHtmlFetcher(*, allowed_hosts: set[str], path_prefixes=("/",), transport=None)` permits only the configured HTTPS official host/path. Each logical GET/POST operation has:

- a 30-second total budget, shared by retries and redirects;
- at most three physical request attempts;
- a 5 MiB response limit and HTML/XHTML content-type checks;
- bounded redirects through the existing outbound safety layer, with physical requests counted by the callback.

There is **no workflow-wide request count or wall-clock budget**. In the current public-table path, list caps imply at most 200 ApplyHome list requests plus up to 2,000 in-window ApplyHome details, and 100 LH list requests plus up to 5,000 details. These are loose code ceilings, not expected October workload or an accepted practical duration. Source drift, malformed pagination, repeated pages, changing totals, duplicate identities or a page-cap breach fail the affected source instead of claiming truncated data is complete.

The earliest available publication is unknown from source inspection. ApplyHome has no server-side earliest-date filter in this implementation; LH transmits the requested start date but public retention and supported historical depth are unverified. No source response was fetched in this pass.

## Optional API reconciliation and public fallback

Django settings load `.env`, then nonproduction `.env.local` without overriding existing environment variables. The builder checks only whether stripped `DATA_GO_KR_SERVICE_KEY` is nonempty. This pass did not inspect those files, the inherited value or any credential contents, and does not establish whether the current runtime has a key.

- No key: both public HTML collectors remain active, with `official_api_reconciliation="inactive_no_key"`.
- Key present: each HTML collector is wrapped by `ReconciledOfficialCollector`, with API reconciliation marked `active`.
- HTML source errors return immediately without API observation.
- API unavailability/schema/window/duplicate failures return a failed source with `OFFICIAL_API_RECONCILIATION_FAILED` and no surviving HTML notices. There is no automatic fallback after API failure.
- Different identity sets, publication dates or normalized titles produce `OFFICIAL_API_RECONCILIATION_CONFLICT`; agreeing rows remain but the source is incomplete. Reconciliation requires equality of HTML/API identities, not merely API availability.

API observers use at most 100 pages per endpoint, requested page size 1,000. ApplyHome has two approved odcloud endpoints (`getAPTLttotPblancDetail`, `getRemndrLttotPblancDetail`) with `cond[RCRIT_PBLANC_DE::GTE]`/`LTE`. LH uses the approved `lhLeaseNoticeInfo1` endpoint with `PAN_NT_ST_DT` and `CLSG_DT`, then independently requires each returned publication to lie in the window. The actual semantics and coverage of LH's `CLSG_DT` parameter versus the broad public list have not been verified here; its lease-oriented endpoint may differ in scope, which can manifest as a fail-closed reconciliation conflict.

`OfficialApiFetcher.get_json()` enforces approved exact HTTPS targets, JSON type, 5 MiB, timeout 20 seconds, max elapsed 30 seconds and zero redirects per request. It adds the opaque key only in memory. API result bodies/credential query strings must never be copied to reports. Do not silently disable a configured key to manufacture successful collection; preserve whether reconciliation was active and any incomplete result.

## Recommended October collection path

For an actual run begun on **October 8 KST**, the smallest existing production-code path that saves portal data without article generation is:

```powershell
$env:WISDOME_ENVIRONMENT = 'development'
$env:WISDOME_RUNTIME_MODE = 'local'
$env:PYTHONUTF8 = '1'
.\.venv\Scripts\python.exe src\manage.py collect_recent_housing --days 8 --no-humanize --no-write-articles
```

This command is a recommendation, **not executed in this reconnaissance**. Preserve configured roots rather than resetting or deleting them. Default roots are `output/housing` and `.local/state`. Check runtime/root policy and the actual KST date before execution. Record the sanitized command result, captured window, active/inactive reconciliation mode and committed run path. Validate the run through `ArticleBundleWriter.validate_run()` and hash-bound metadata reads before presenting it as saved October evidence. An incomplete source run is still useful observed data when clearly labeled; it is not a reason to fabricate notices or declare national coverage.

Existing fetcher/page ceilings make this path finite but do not provide a short overall deadline. If the parent needs a tighter operational envelope before collecting, add an explicit request/deadline budget around the existing approved fetchers or a documented CLI bound, and keep exhaustion as an incomplete source result. Preserve pagination integrity: lowering a page cap must not label the first pages complete. Do not forcefully terminate unrelated listeners or bypass the guard; collection does not require the preview server, a browser, humanizer startup or runtime database migration.

For a fixed historical month, an explicitly anchored `LocalHousingWorkflow.run(now=<aware month-end KST>, days=<days in month>, humanize=False, write_articles=False)` is a source-level option, not an existing CLI UX or proof of historical completeness. Do not use future month-end for the current month.

## Monthly portal selection without losing saved history

Current `load_housing_snapshot(query: str = "")` deliberately projects **one latest valid saved snapshot**. It scans at most 512 root entries and 128 run candidates, requires hash-bound schema-v2 metadata, checks strict shape/date/window/URL rules, and selects greatest KST `window.end`, with run name only as a stable tie-breaker. It accepts at most 1,000 notice rows per snapshot, searches all selected rows before showing 100, and shows 12 archive links. Schema-v1 runs remain archive links, never enriched notice cards. `acceptance-report.json` is mutable and must not be treated as immutable notice provenance or mode evidence.

Filtering that one latest snapshot by year/month would hide older saved months, including October 1 notices after later short-window runs. The new requirement needs a validated history projection; existing immutable runs and article routes should remain untouched.

Recommended history algorithm, pending the user's date-basis choice:

1. Enforce local-loopback access before all housing I/O. Keep `_article_root`, confined nonlinked roots, `_ReadOnlyBundleWriter`, full run validation, strict metadata parsing and exact schema-v2 hash binding.
2. Scan all permitted candidates under an explicit bound. Do not silently take the lexicographically newest or first 12/128 candidates and call it the whole archive. Over-limit/unavailable histories need a visible limit state or deliberate integrity-preserving indexing design.
3. Retain `source_key`, `external_id`, `source_checksum`, parser version, run name and aware observation `window.end` before display projection. The current `_project_notice()` drops identity fields and is insufficient for cross-run deduplication as returned.
4. Select qualifying **observations by the requested month before deduplicating identities**. Deduplicating globally to the latest observation and only then filtering could remove a historical month when a correction changes dates. Deduplicate identical `(source_key, external_id, source_checksum)` observations; for a one-card-per-identity month view, prefer greatest `window.end` among observations qualifying for that month, retain version/run provenance, and do not infer deletion from absence in newer runs.
5. Keep different source identities separate, even if titles match. Preserve corrections, quarantined conflicts, incomplete-source/detail warnings and unsafe-URL exclusion. Never turn a missing date into a guessed month or silently substitute a deadline for application end.
6. Build available year/month controls and full matching counts from the same validated history and chosen date basis. Apply month and text filters before the visible-card limit. Clearly distinguish empty saved month, excluded invalid history and scan/display limits.

| Date basis | Safe matching semantics | Product limitation |
| --- | --- | --- |
| Publication month (recommended default) | KST `published_at.year/month` equals selected month. October month-to-date collection and this UX use the same basis. | A September-published notice with October applications is not an October publication. Label the control as publication month. |
| Application month | Known application interval overlaps `[month start, next month start)`; retain one-sided/unknown dates as explicitly uncertain, not invented overlap. | October-only publication collection does not discover every older notice whose application extends into October. Saved older observations or a wider collection/backfill are required. Deadline remains a separate fact. |
| Collection month | KST saved `window.end.year/month` equals selected month; label as collection month. | This groups collection observations, including older publication dates, and is not a publication/application calendar. `window.end` is workflow start reference time, not completion time. |

The pending product question is which of these dates the year/month control should mean. The parent owns the final choice. The reported service currently implements publication-window collection and one-snapshot display; the history algorithm above is a recommendation, not completed code or acceptance.

## Evidence, priorities and actual verification limits

Read and searched: command, workflow, contracts/dates, selection policy, official HTML fetcher, ApplyHome/LH collectors, optional API reconciliation, humanizer constructor, run writer, current housing projection, workflow tests and canonical reports. Source evidence includes `workflow.py:281` (window), `:548` (generation controls), `:710` (notice-only write), `:849` (merge), `:916` (prior scan), `:1291` (notice JSON), `sources/applyhome.py:83/:106/:550`, `sources/lh.py:86/:104/:566`, `http.py:27/:103`, `api_reconciliation.py:138/:175/:267`, and `portal/housing.py:52/:163/:177/:202`.

Commands actually executed: `git status --short`, `git rev-parse HEAD`, `git branch --show-current`, targeted `rg`/`rg --files`, PowerShell `Get-Content`/`Select-Object`, SHA-256 source hashes and a nonrecursive default `output/housing` directory listing. The listing returned no directory entries; it does not establish inventory of an alternate configured root or stored-run integrity. Source-inspected test authorities include notice-only guard/report tests around `tests/integration/test_local_housing_workflow.py:703/:1048`, disabled humanization `:1461`, dry-run `:1777`, and exact CLI flags `:1800`. **No test was executed in this pass.** Their assertions and earlier portal full-suite evidence are not new executed acceptance.

Captured SHA-256 source baseline (uppercase hex; working-tree bytes):

| File | SHA-256 |
| --- | --- |
| `src/apps/local_content/management/commands/collect_recent_housing.py` | `A151E2D27E6AD30F839FC678296AFD48BBC7BCD86F5E071D029C4EDDC94052EF` |
| `src/apps/local_content/workflow.py` | `BD7EEC6736F0061352E3249CA35C8AAAA987245C79005897FB4BD7712EE718D8` |
| `src/apps/local_content/sources/applyhome.py` | `1C7E1E02331F4EA397AE43D8FBAB0F5D3AB421EF95C8462C2BB12E514C9FA7ED` |
| `src/apps/local_content/sources/lh.py` | `0EA8480947E68C738382487BE49BF0EE9CD0DB7E7762E88C26D98D343B8B5889` |
| `src/apps/local_content/http.py` | `4C51A4B1C140586B525CE922BBFE46048804C1B1CDEA5722FEB033A92971538E` |
| `src/apps/local_content/api_reconciliation.py` | `D1B342B8EAD327E9420BA5CF6186D3EFD75B30C3140E32DF08640F5E9DA98A13` |
| `src/wisdome_writer/portal/housing.py` | `1ACA74A55521AB31C98307190FD3847341620471551DC3C625C9F58C38E0F1FA` |

Priorities: (P1) retain publication/observation provenance and incomplete/conflict distinctions; (P1) validate all monthly history and identities without weakening filesystem/hash boundaries; (P1) collect with honest request/page/time limits and preserve source failures; (P2) filter month/search before truncation, keep archives and unknown dates, and test cross-month correction/dedup behavior. Open questions remain date basis, acceptable collection deadline/request budget, historical retention/index scale and whether application-month backfill is expected.

Report QA: a Python strict UTF-8 read succeeded and all report-relative Markdown links resolve; the final PowerShell fence check found four balanced fence lines. `git diff --check -- docs/analysis/2026-10-08-monthly-housing-recon.md` exited 0, but the report is untracked, so that Git command does not inspect its complete added contents. The explicit text checks cover the saved artifact.

Not executed: source HTTP collection, real API authentication/reconciliation, humanizer/preview startup, migration, browser navigation, monthly UX implementation, live stored-bundle validation, full suite, external publishing or attachment extraction. Historical August housing acceptance remains historical and does not establish an October run.

## Update log

- 2026-10-08: read-only collection/monthly-history reconnaissance saved for the parent implementation agent. No product or shared instruction file modified by this pass.
- 2026-10-08: user selected special-supply/first-priority subscription dates; parent authorized the scoped implementation and explicit publication-start extension. The implementation and verification below supersede the initial proposal, preserving historical evidence.

## Schedule-date implementation follow-up

### Approved brief and owned work

The user clarified that housing year/month selection must follow **actual special-supply and first-priority subscription dates**. Funding uses posting date and belongs to the parent/other agent. The parent approved implementation of the housing projection, ApplyHome collector, CLI, minimal workflow start extension, focused tests and this report. Existing immutable runs and other agents' changes were preserved. No new dataclass field, database schema or dependency was introduced.

The scoped implementation plan was: (1) RED/GREEN tests for explicit stage/region facts and changed checksums; (2) RED/GREEN validated-history month/stage filtering before identity deduplication and display limits; (3) RED/GREEN optional aware `collection_start` plus strict `--since`; (4) current official stage probe, relevant workflow/preview regressions, scoped lint and report update. The user supplied the decisive date basis; no additional permission was requested and no browser-origin denial was bypassed.

Owned product files:

- `src/apps/local_content/sources/applyhome.py`: parser version `applyhome-public-html-v2`, exact stage facts and region labels.
- `src/wisdome_writer/portal/housing.py`: confirmed stage events, source identities, observation provenance, bounded month-history projection and unknown counts.
- `src/apps/local_content/workflow.py`: optional aware explicit collection start, otherwise unchanged calendar-day window behavior.
- `src/apps/local_content/management/commands/collect_recent_housing.py`: strict `--since YYYY-MM-DD` and stable invalid-window/configuration errors.

Owned tests: `tests/unit/test_portal_housing.py`, `tests/unit/test_applyhome_subscription_schedule.py`, and `tests/unit/test_housing_collection_start.py`. Shared `periods.py`, routes, views, templates, funding behavior, canonical shared reports and AGENTS registration remain parent-owned.

### Implemented collector and stage evidence

The ApplyHome collector still validates the fetched detail's identity using the existing logic before extracting schedule facts. It retains stage information through the existing `HousingNotice.facts` tuple, so `workflow._notices_document()` writes it unchanged inside hash-bound schema-v2 metadata.

Examples of actual persisted shape:

```python
facts = (
    ("특별공급 · 해당지역", "2026-10-19"),
    ("특별공급 · 기타지역", "2026-10-19"),
    ("1순위 · 해당지역", "2026-10-20"),
    ("1순위 · 기타지역", "2026-10-20"),
)
```

The collector reads explicitly labelled special/first-priority table rows, resolves rowspan/colspan columns to retain applicable-region headings, and reads explicitly labelled fixture detail definition lists. The already-known `rnk1CrsRceptPd` source marker establishes first priority even when the visible row label is generic. Unsupported/ambiguous labels are not classified. Stage labels are constrained to explicit stages and recognized region wording; eligibility/birthdate labels such as `특별공급 자격`, `1순위 대상` and `1순위 신청자 출생일` are rejected as schedule labels. Generic application dates, publication dates and second-priority dates never become special/first events. Changed stage facts affect the notice source checksum. Generic `application_start`/`application_end` fields retain existing behavior for backward compatibility and are never used as the monthly-stage authority.

Extraction limits include 64 fact pairs, 256-character labels, 2,048-character raw texts, bounded table spans/columns and the inherited 5 MiB HTTP response limit. These facts are source observations, not evidence that future applications actually occurred. Source labels and raw date text remain visible for original-notice confirmation.

### Implemented projection contract

```python
load_housing_snapshot(
    query: str = "", period: PeriodSelection | None = None, stage: str = "all"
) -> dict[str, object]
```

`period` uses the parent-owned `PeriodSelection` with `basis="schedule"`; stage choices are `all`, `special`, `first`. Invalid query/stage values are rejected before storage access. The caller must still enforce local-loopback access first.

The default no-period call retains latest-snapshot selection, prior publication ordering/search semantics and archive links. A stage-only request filters that latest snapshot by confirmed chosen-stage dates. An active schedule period validates all bounded history, filters every observation against the chosen stage's confirmed dates, then deduplicates by `(source_key, external_id)` to greatest `(captured_at, run_name)` **among observations qualifying for the selected month/year**. It sorts matching cards by earliest qualifying stage date, applies text search to all results, counts before truncation, and displays at most 100. Absence in a newer snapshot does not delete older monthly observations. A newer correction that moves dates out of a historical month does not overwrite that month's older saved observation; per-card provenance makes that historical status explicit.

Each card now includes `source_key`, `external_id`, `source_checksum`, aware KST `captured_at`, `run_name` and:

```python
subscription_events = [
    {"kind": "special", "label": "특별공급 · 해당지역",
     "text": "2026-10-19", "dates": ["2026-10-19"]},
    {"kind": "first", "label": "1순위 · 기타지역",
     "text": "2026-10-20", "dates": ["2026-10-20"]},
]
```

Both stages and all preserved regional labels remain in a matching card; the chosen stage controls inclusion rather than discarding the card's other source facts. Invalid dates or explicit uncertainty/noncalendar descriptions yield empty `dates` for that event. Birthdate/eligibility text cannot create a calendar match; weekday and reception-time text is retained and accepted. No generic date or deadline substitution is performed.

Additional result fields:

| Field | Implemented meaning |
| --- | --- |
| `total` | Distinct month/stage-qualified notices before search when the period is active; latest-snapshot selected-stage notices otherwise |
| `matching_total` | Full qualified text-match count before the 100-card limit |
| `history_total` | Distinct valid `(source_key, external_id)` identities across bounded schema-v2 history |
| `available_years` | Descending years found in confirmed selected-stage dates across that validated history |
| `unknown_schedule_count` | Active period: distinct identities in their latest saved observation with no confirmed selected-stage date, across history. No period: the same count within the latest snapshot. This count cannot be assigned to a month and includes programs that do not use special/first-priority stages. |

`snapshot_name`/`observed_at` still identify the newest valid saved collection reference. Active month cards can come from different runs; each card's `captured_at`/`run_name` is its provenance. Unknown counts are separate from confirmed monthly totals. Corrupt generations, duplicate identities within one run, quarantined conflicts, incomplete sources/detail failures, legacy metadata and scan/display limits remain visible caveats. Unknown dates remain unknown. Schema-v1 metadata is never promoted to trustworthy monthly facts.

Existing limits remain 512 root entries, 128 candidate runs, 1,000 notices/run, 12 archive links and 100 visible notices. Reads retain exact manifest/hash binding, strict shape checks, full bundle validation, confined nonlinked paths and no directory creation. No long-lived unchecked cache or weakened validation was added. This finite history can be costly when old runs contain many image bundles; an integrity-preserving archive index remains a scale question.

### Explicit announcement lookback for stage-month collection

The implemented workflow signature is now:

```python
LocalHousingWorkflow.run(
    now: datetime, days: int = 7, humanize: bool = True,
    write_articles: bool = True, selected_ids: Collection[str] = (),
    *, collection_start: datetime | None = None,
) -> WorkflowReport
```

When provided, `collection_start` must be aware, convertible to KST, not future, and no more than 365 elapsed days before `now`. Validation occurs before collection/storage. Existing `days=1..31` validation and default calendar-day window behavior remain unchanged when it is absent. `--since` accepts only valid `YYYY-MM-DD`, converts it to midnight KST and forwards it to this argument; malformed/unsupported dates do not silently revert to the recent-days window.

The parent-coordinated collection command for September announcements that may have October special/first schedules is:

```powershell
$env:WISDOME_ENVIRONMENT = 'development'
$env:WISDOME_RUNTIME_MODE = 'local'
$env:PYTHONUTF8 = '1'
.\.venv\Scripts\python.exe src\manage.py collect_recent_housing --since 2026-09-01 --no-humanize --no-write-articles
```

`--since` remains a **publication lookup boundary**, not a subscription-date API query. Earlier announcements, programs/providers outside the two current official source scopes, source pagination/API failures and attachment-only schedules can still be missing. September 1 is explicit coverage, not proof of every October subscription. Page/request/time ceilings and optional API fail-closed behavior described in the initial analysis still apply; no total collection deadline or request budget was added. The parent owns the actual broader run and its final result. This subtask did not launch a second full collection process or stop an existing process.

### Current official and saved-data evidence

The parent reported that its initial old-parser `--days 8` live process finished with exit 1, `DETAIL_INCOMPLETE`, reconciliation `inactive_no_key`, run `2026-10-08`, selected article-work count 32 and written count 0. That is operational evidence from the parent, not an independently rerun command.

This subtask independently loaded that run through the current validated projection: `available=True`, `total=54`, `unknown_schedule_count=54`. This is expected because the old parser stored no explicit stage facts. The existing immutable run was not rewritten to infer stages from its generic application fields.

One bounded current official detail request was executed using **the existing `OfficialHtmlFetcher` and collector identity validation**, from a candidate URL obtained through that validated snapshot. Notice `applyhome:apt:2026000464:2026000464` was published October 8, and the new parser returned special-supply October 19 and first-priority October 20 for both `해당지역` and `기타지역`. Request completed successfully through the existing approved ApplyHome HTTPS/path boundary. No key, raw response body, browser origin, attachment bytes or credential value was read into this report. This confirms one current HTML shape and its exact stage dates; it does not establish source-wide exhaustive acceptance or the broader run's outcome.

### Actual verification and handoff limits

Tests use Python 3.12, `WISDOME_ENVIRONMENT=development`, `WISDOME_RUNTIME_MODE=local`, `PYTHONUTF8=1`, removed inherited local-root overrides, and disposable fixture storage. No full suite, browser, preview/humanizer server, port binding or migration was executed by this subtask.

| Verification | Actual result |
| --- | --- |
| Initial collector schedule RED | 4 failed / 5 passed; empty facts and unchanged checksum exposed the missing stage persistence |
| Initial projection RED | 15 failed / 46 passed; missing period/stage interface and missing identity/provenance fields |
| Explicit-start/CLI RED | 6 failed / 1 passed; missing start argument/flag and unforwarded start |
| Additional adversarial stage/history tests | Reproduced eligibility/birthdate misclassification, duplicate identity and conflict-caveat gaps; corrected and retained as regressions |
| Initial expanded test run | One test assumed argparse `SystemExit`; Django's inspected `CommandParser.error()` correctly uses `CommandError` for API callers. Test expectation corrected to the actual framework contract. |
| First focused regression after formatting | `195 passed in 25.12s`, exit 0; superseded by post-review checks below |
| Final scoped Ruff | `All checks passed!`, exit 0; initial 16 E501 findings were fixed by formatting the owned files |

Final executed pytest command:

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/unit/test_housing_collection_start.py tests/unit/test_portal_housing.py tests/unit/test_applyhome_subscription_schedule.py tests/unit/test_applyhome_public_html.py tests/unit/test_local_content_preview.py tests/integration/test_local_housing_workflow.py --tb=short
```

Final Ruff covers the four owned product files and three owned test files. No whole-repository lint or suite success is claimed. Broader shared view/template tests and UI acceptance are parent-owned. Current direct Chrome acceptance remains blocked and was not attempted here.

Final owned-product SHA-256 snapshot:

| File | SHA-256 |
| --- | --- |
| `src/wisdome_writer/portal/housing.py` | `29ED65678CEB0308A352BF9E5639FA019042A4780C21AEF42BFB1852736AB439` |
| `src/apps/local_content/sources/applyhome.py` | `22BAC44065A81203569DD689EFBAF639EA6EBE10D954B22F758C1AACC3A4614F` |
| `src/apps/local_content/workflow.py` | `3F54D49F2DEA4F9F7FACD991536B77172322DE644E807D0F072F48FBE367FD08` |
| `src/apps/local_content/management/commands/collect_recent_housing.py` | `A24455311895230D3B666BCA3D1B77485920FC237B144DB9A4B3CD4D2C26E7D7` |

Remaining product/acceptance questions: whether an earlier publication boundary is needed for October schedules, attachment/source scope expansion, archival scale beyond 128 runs and clearer nonapplicable-versus-unknown stage distinctions. The requested confirmed-date projection and bounded explicit lookback exist and have focused executed verification; live collection completeness, distributed operation, actual applications and browser UX are separate evidence.

### Independent review and repairs

A narrow independent reviewer inspected current owned bytes and ran offline parser/projection reproducers. It did not use Git, network, browser, collection, services, migrations, a full suite or source writes. It found three P2 boundaries: a colspanned stage label duplicated the stage and dropped its date; row-oriented region labels separated from their dates; and malformed numeric date tokens such as `2026-10-062` were accepted as the valid prefix October 6. The review found no other material issue in month filtering, identity deduplication, observation provenance, old-day windows or `--since` validation.

All three findings were reproduced with failing regression tests and repaired. Table stages are now deduplicated by their source cell, with their last occupied column controlling associated values. Regional row labels bind to following date cells, including generic row labels with the explicit first-priority marker. Date matching requires numeric boundaries. Reviewer recheck confirmed the table repairs and standalone malformed-token repair, then found the related mixed-token case (`2026-10-062 ~ 2026-11-01`). Three additional RED regressions confirmed it; residual malformed date-shaped tokens now clear the whole event's dates, consistent with existing invalid-start/valid-end behavior. Valid weekday/reception-time text still passes. The latest residual-token repair has fresh regression verification; no third independent review was requested, because the parent requested immediate handoff and needs the concurrency slot.

- Review-repair RED: 4 failures for colspan, row-region and standalone malformed date cases; a further generic-marker row-region case also failed as expected before its repair.
- First repaired expanded check: **200 passed in 24.76s**, exit 0.
- Mixed malformed/valid event RED: **3 failed, 5 passed, 70 deselected**.
- Final post-review expanded command (the six test files above): **203 passed in 25.08s**, exit 0.
- Final scoped Ruff: **All checks passed!**, exit 0. Current source hashes above include all review repairs.

The parent clarified at handoff that the broader `--since 2026-09-01` collection **has not started**; only the baseline old-parser process 72055 completed. Earlier coordination messages assumed that a broader process might be running, but no such process is accepted as evidence here. The parent can now execute the single recommended broader notice-only command with the repaired parser. This subtask did not launch or stop any full collection. Browser and full service acceptance remain separate.

## Subsequent parent acceptance — 2026-10-09 KST

The parent later executed that broader notice-only command and validated saved
run `2026-10-08--run-4a05683f2944`. Inspected history has175 distinct identities;
23 notices have confirmed October special/first dates. Actual page rendering
verified special18 and first21 with overlapping identities, filtered by their
own stage dates. Unknown/nonapplicable stages remain excluded. The workflow is
still DETAIL_INCOMPLETE, selected article work120 and written0; this is neither
exhaustive older-publication coverage nor generated-writing acceptance.
Final stable parent full suite passed1,689 tests with3 skipped and203 subtests.
See [monthly progress](2026-10-08-monthly-data-update.md) for commands, private logs,
dated source limits and the still-open direct Chrome/mobile acceptance.
