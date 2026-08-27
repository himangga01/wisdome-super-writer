# Task 6 Report: LH official public HTML collector

## RED

`$env:WISDOME_ENVIRONMENT='development'; .venv\\Scripts\\python.exe -m pytest tests/unit/test_lh_public_html.py -q`
failed during collection with the expected missing-module error:
`ModuleNotFoundError: No module named 'apps.local_content.sources.lh'`.

The prescribed `py -3.12` launcher is unavailable on this machine; the worktree's
Python 3.12.14 virtual environment was used instead.

## GREEN

Implemented `LhPublicCollector` with official form POST pagination, KST date
parameters, canonical LH identity/detail URLs, strict pagination and publication
window checks, label-only detail extraction, and attachment observations without
attachment downloads. The initial focused suite passed: `7 passed`.

## Tests

Fresh verification command:

```powershell
$env:WISDOME_ENVIRONMENT='development'
.venv\\Scripts\\python.exe -m pytest tests/unit/test_lh_public_html.py tests/unit/test_applyhome_public_html.py tests/unit/test_local_content_dates.py tests/unit/test_local_content_http.py tests/unit/test_extraction_safety_boundaries.py -q
```

Result: `108 passed, 4 subtests passed in 0.37s`.

Fresh Ruff command:

```powershell
.venv\\Scripts\\python.exe -m ruff check src/apps/local_content/sources/lh.py tests/unit/test_lh_public_html.py
```

Result: `All checks passed!`

## Files

- `src/apps/local_content/sources/lh.py`
- `tests/unit/test_lh_public_html.py`
- `tests/fixtures/lh/notice-list-page-1.html`
- `tests/fixtures/lh/notice-list-page-2.html`
- `tests/fixtures/lh/notice-detail.html`

Implementation commit: `9598964 feat: collect lh notices from official html`.

## Self-review

- Network access is confined to the injected official HTML fetcher protocol.
- List requests use the official LH POST endpoint and required KST window,
  pagination, search-mode, and menu fields.
- The collector rejects parser drift, page/index/total changes, explicit-empty
  inconsistencies, repeated pages, duplicate/conflicting identities, truncated
  pagination, cap exhaustion, and any published date outside the requested window.
- Detail values are emitted only from labelled values; unavailable schedule, supply,
  price, and eligibility facts remain absent with warnings. Attachment links are
  preserved as `internal_analysis_only` observations and are never fetched.

## Concerns

- The full legacy suite was not run because it is known-red; the Task 3--5 related
  focused suites listed above are green.
- Git emitted normal Windows LF-to-CRLF checkout warnings while staging new files;
  `git diff --cached --check` completed without whitespace errors.

---

# Task 6 Fix Round 1/5

## RED

After adding the review regressions, the focused LH suite failed with six
behavioral failures:

- non-final 49-row and 51-row pages were accepted or only rejected by the
  weaker aggregate-truncation check;
- empty and two-row final pages were not rejected as bad page cardinality;
- an empty final page surfaced as the generic empty-page error;
- a two-period labelled schedule incorrectly emitted the first period.

The reproduction command was:

```powershell
$env:WISDOME_ENVIRONMENT='development'
.venv\\Scripts\\python.exe -m pytest tests/unit/test_lh_public_html.py -q
```

Result before the production fix: `6 failed, 19 passed`.

## GREEN

- Enforced `last_page == ceil(total_count / 50)` for every non-empty result,
  50 records on every non-final page, and the exact final-page remainder.
- Preserved the explicit zero-result state while rejecting malformed empty pages.
- Replaced greedy schedule-date selection with an exactly-one-labelled-range
  parser; multiple or malformed periods now stay empty with an ambiguity warning.
- Added assertions for complete form payload, page metadata/index/cap/cardinality,
  duplicate and conflicting identities, hidden text cleanup, checksum sensitivity,
  and attachment observations with `internal_analysis_only` and no download call.
- Removed unused `_DETAIL_QUERY_KEYS`.

## Tests

Focused LH GREEN:

```powershell
$env:WISDOME_ENVIRONMENT='development'
.venv\\Scripts\\python.exe -m pytest tests/unit/test_lh_public_html.py -q
```

Result: `25 passed in 0.71s`.

Cross-task fresh verification:

```powershell
$env:WISDOME_ENVIRONMENT='development'
.venv\\Scripts\\python.exe -m pytest tests/unit/test_lh_public_html.py tests/unit/test_applyhome_public_html.py tests/unit/test_local_content_dates.py tests/unit/test_local_content_http.py tests/unit/test_extraction_safety_boundaries.py -q
.venv\\Scripts\\python.exe -m ruff check src/apps/local_content/sources/lh.py tests/unit/test_lh_public_html.py
```

Results: `126 passed, 4 subtests passed in 0.84s`; `All checks passed!`.

## Files

- `src/apps/local_content/sources/lh.py`
- `tests/unit/test_lh_public_html.py`
- `.superpowers/sdd/2026-08-28-dockerless-local-housing-writer/task-6-report.md`

## Self-review

- Cardinality validation occurs before records can be admitted, so aggregate
  totals cannot hide a short, overfull, or missing page.
- Publication-only admission, non-residential retention, canonical identity/query,
  and the injected official-fetcher boundary remain unchanged.
- Attachment links stay immutable facts with the required rights marker and are
  never passed to the fetcher.

## Concerns

- Full legacy-suite execution remains intentionally omitted because it is
  known-red; all Task 3--5 related regression suites above are green.

---

# Task 6 Fix Round 2/5

## RED

Added parametrized regressions for `data-empty-results="true"` combined with
actual rows and both a normal declared total (`51`) and contradictory zero total
(`0`).

```powershell
$env:WISDOME_ENVIRONMENT='development'
.venv\\Scripts\\python.exe -m pytest tests/unit/test_lh_public_html.py -q
```

Result before the parser change: `2 failed, 25 passed in 0.84s`. The normal-total
case admitted 51 notices; the zero-total case reported the less-specific
`invalid list pagination material` error.

## GREEN

`_parse_list()` now reads the explicit-empty marker before pagination admission
and fail-closes with `contradictory explicit empty result page` whenever rows are
present, irrespective of the declared total.

## Tests

```powershell
$env:WISDOME_ENVIRONMENT='development'
.venv\\Scripts\\python.exe -m pytest tests/unit/test_lh_public_html.py tests/unit/test_applyhome_public_html.py tests/unit/test_local_content_dates.py tests/unit/test_local_content_http.py tests/unit/test_extraction_safety_boundaries.py -q
.venv\\Scripts\\python.exe -m ruff check src/apps/local_content/sources/lh.py tests/unit/test_lh_public_html.py
```

Results: `128 passed, 4 subtests passed in 0.76s`; `All checks passed!`.

## Files

- `src/apps/local_content/sources/lh.py`
- `tests/unit/test_lh_public_html.py`
- `.superpowers/sdd/2026-08-28-dockerless-local-housing-writer/task-6-report.md`

## Self-review

- The contradiction is rejected before any row parsing, identity creation,
  pagination validation, or detail fetches.
- Proper explicit-empty zero results continue to be admitted.

## Concerns

- Full legacy-suite execution remains intentionally omitted because it is
  known-red; Task 3--5 related focused suites are green.
