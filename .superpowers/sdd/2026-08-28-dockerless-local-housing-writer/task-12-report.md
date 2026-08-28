# Task 12 Report — Deterministic Verification and Live Housing Acceptance

## Outcome

Task 12 completed successfully in the requested linked worktree at base
`711c03869674fbb12cb0554f31552da60927e664`, without Docker, subagents, reviewers,
firewall changes, or security-policy changes.

The acceptance-support implementation is committed as:

```text
3a4e5f5 test: verify dockerless housing writer end to end
```

The authoritative ignored live run is:

```text
output/housing/2026-08-28--run-9c1c6b19a244
```

Its machine report is:

```text
output/housing/2026-08-28--run-9c1c6b19a244/acceptance-report.json
```

The report has `live_success=true`, `complete=true`, `blocked=false`, no error codes,
all artifact and browser requirements passing, and `task12_acceptance.overall_passed=true`.

## Dockerless setup and deterministic baseline

The real setup command completed without a Docker executable:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\setup-local.ps1
```

Result:

```text
uv 0.12.7
Python 3.12 already installed
86 packages checked
No migrations to apply
2 source-registry topics verified
System check identified no issues
exit 0
```

The hardened settings loader correctly rejected later Django/pytest invocations that omitted
`WISDOME_ENVIRONMENT`. Re-running with the explicit local context was the intended boundary:

```powershell
$env:WISDOME_ENVIRONMENT='development'
$env:WISDOME_RUNTIME_MODE='local'
.\.venv\Scripts\python.exe src\manage.py check
.\.venv\Scripts\python.exe src\manage.py makemigrations --check --dry-run
```

Final result:

```text
System check identified no issues (0 silenced).
No changes detected
```

## Full-suite remediation

The original focused Task 12 suite passed before fixes:

```text
459 passed, 2 skipped, 6 warnings in 37.71s
```

The original unrestricted suite reproduced the known SQLite contamination. Investigation found
and fixed each root cause instead of waiving the broad red suite:

1. An editorial test inserted a publication intent with a placeholder revision hash. The test
   failed alone under the real lineage trigger. It now uses the created revision's content hash.
2. Django `TransactionTestCase` flush attempted raw deletes while production append-only SQLite
   triggers were active. Test-only teardown now restores migration leaves, snapshots and suspends
   SQLite triggers only around flush, and restores their exact SQL in `finally`.
3. A migration test restored only the evidence leaf, leaving collection at migration 0009. The
   test harness now restores all graph leaf nodes before destructive test cleanup.
4. The publication 0010 irreversibility test used current ORM models against a historical schema.
   It now returns to the leaf schema before current-model cleanup.
5. Stale unit tests were aligned with current immutable approval/dispatch contracts and queue-one
   release behavior without weakening production validation.
6. A real reused-document bug omitted verified legacy-HWP page identity. The production path now
   distinguishes authoritative converted-page identity from provisional PDF inspection defaults.
7. The offline OCR test now recognizes all four committed versioned PaddleOCR profiles.

Final focused command:

```powershell
.\.venv\Scripts\python.exe -m pytest `
  tests\unit\test_queue_configuration.py `
  tests\unit\test_local_runtime.py `
  tests\unit\test_local_content_dates.py `
  tests\unit\test_applyhome_public_html.py `
  tests\unit\test_lh_public_html.py `
  tests\unit\test_local_content_selection.py `
  tests\unit\test_local_content_rendering.py `
  tests\unit\test_local_content_images.py `
  tests\unit\test_local_content_humanizer.py `
  tests\unit\test_local_content_bundles.py `
  tests\unit\test_local_content_preview.py `
  tests\integration\test_local_housing_workflow.py -q
```

Final focused result:

```text
469 passed, 2 skipped, 6 warnings in 46.25s
```

Final unrestricted result:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

```text
1187 passed, 5 skipped, 6 warnings, 198 subtests passed in 467.41s
```

The six warnings are the known Django notice that generated `staticfiles/` is absent inside the
preview tests. They are not treated as test failures.

## Live parser drift and TDD fixes

The first live run failed closed with both sources unavailable:

```text
2026-08-28--run-c2783adac14f
SOURCE_INCOMPLETE, ALL_SOURCES_FAILED
```

Persistence-safe probes isolated two live defects:

- ApplyHome returned malformed negotiated compression to the default HTTPX request. A failing
  mock-transport test reproduced `DecodingError`; the official HTML fetcher now requests
  `Accept-Encoding: identity`.
- ApplyHome and LH had moved from synthetic data-attribute fixture DOMs to current official table
  layouts. Minimal sanitized 2026-08-28 regression fixtures were added before parser changes.

The live parser now verifies:

- ApplyHome `data-hmno`/`data-pbno`, 10-row pagination, both current detail endpoints, visible
  application schedules, and supply counts;
- LH classless result tables, exact `data-id1..4` mapping, ordinal/cardinality pagination,
  multiple supply tables, visible application date tables, and sparse-but-authentic detail pages;
- excluded land/store rows remain available only to the selection exclusion layer;
- no official attachment document is downloaded or copied.

Fresh source regression results:

```text
ApplyHome public HTML: 24 passed
LH public HTML: 33 passed
HTTP/source combined matrix: clean
all changed/local_content Python Ruff: All checks passed
```

## Humanizer runtime

The approved sibling service was built and run from
`C:\project\2026\ai-text-makes-likes-human`:

```powershell
$env:HOST='127.0.0.1'
$env:PORT='3210'
$env:CODEX_CHUNK_CONCURRENCY='1'
npm.cmd run build
node dist/server.js
```

Verified runtime diagnostics:

```text
owned PID: 38888
listener PID: 38888
health: ready
engine: codex-cli
engine version: 0.150.1
humanize version: 2.3.2
active/queued before run: 0/0
final verified articles: 19
```

Logs are retained at:

```text
output/housing/2026-08-28/acceptance/humanizer.stdout.log
output/housing/2026-08-28/acceptance/humanizer.stderr.log
```

The user-authorized live humanization quota was used. Every final article has both bundle-level and
humanizer-level `verified` status, empty humanizer error codes, and completed/verified event records.
No article bodies are stored in the acceptance report.

## Authoritative live collection

Final command:

```powershell
$env:WISDOME_ENVIRONMENT='development'
$env:WISDOME_RUNTIME_MODE='local'
$env:HUMANIZER_BASE_URL='http://127.0.0.1:3210'
.\.venv\Scripts\python.exe src\manage.py collect_recent_housing `
  --days 7 --humanize --write-articles
```

Final window:

```text
start: 2026-08-22T00:00:00+09:00
end:   2026-08-28T12:06:00.924142+09:00
```

Counts:

```text
ApplyHome official notices: 16, complete, 0 errors
LH official notices:        56, complete, 0 errors
raw official notices:       72
excluded land/store:        29
residential weekly index:   43
selected detailed articles: 19
written/verified articles:  19
conflicts:                  0
images:                     57 (3 per article)
official attachment files:  0
```

Article bundle paths:

```text
2026-08-28--run-9c1c6b19a244/applyhome-applyhome-apt-2026000399-2026000399-d894918390
2026-08-28--run-9c1c6b19a244/applyhome-applyhome-apt-2026000401-2026000401-484701151e
2026-08-28--run-9c1c6b19a244/applyhome-applyhome-remaining-2026910225-2026910225-5070fafb01
2026-08-28--run-9c1c6b19a244/applyhome-applyhome-remaining-2026930029-2026930029-cdbc86d1e8
2026-08-28--run-9c1c6b19a244/applyhome-applyhome-remaining-2026930030-2026930030-15f302ebaf
2026-08-28--run-9c1c6b19a244/applyhome-applyhome-remaining-2026940184-2026940184-5ce9a25d89
2026-08-28--run-9c1c6b19a244/applyhome-applyhome-apt-2026000416-2026000416-be7e0939d1
2026-08-28--run-9c1c6b19a244/applyhome-applyhome-remaining-2026910221-2026910221-4cbad9c969
2026-08-28--run-9c1c6b19a244/applyhome-applyhome-remaining-2026910223-2026910223-70fdc30502
2026-08-28--run-9c1c6b19a244/applyhome-applyhome-remaining-2026910224-2026910224-b1abd26a42
2026-08-28--run-9c1c6b19a244/applyhome-applyhome-remaining-2026910226-2026910226-e61de982de
2026-08-28--run-9c1c6b19a244/applyhome-applyhome-remaining-2026940182-2026940182-6704423009
2026-08-28--run-9c1c6b19a244/lh-lh-02-0000061158-05-05-509035e619
2026-08-28--run-9c1c6b19a244/applyhome-applyhome-apt-2026000409-2026000409-e4ce10b84b
2026-08-28--run-9c1c6b19a244/applyhome-applyhome-remaining-2026910229-2026910229-8ab8fdafe3
2026-08-28--run-9c1c6b19a244/applyhome-applyhome-remaining-2026940179-2026940179-ce9a4c5e20
2026-08-28--run-9c1c6b19a244/lh-lh-02-0000061159-05-05-2f94c6a28d
2026-08-28--run-9c1c6b19a244/applyhome-applyhome-remaining-2026910222-2026910222-bdd5ed83c2
2026-08-28--run-9c1c6b19a244/lh-lh-03-2015122300020425-05-05-25365b3216
```

## Artifact audit

The retained audit command:

```powershell
.\.venv\Scripts\python.exe `
  output\housing\2026-08-28\acceptance\audit_live_run.py
```

passed all machine requirements:

1. exact KST window and all publication dates inside it;
2. 43 residential-only index notices, with no land/store category;
3. both official sources complete and no false detail failure;
4. 43 HTTPS allowlisted official links and 19 resolving in-run article links;
5. 19 verified bundles, all file digests, 19 humanizer verifications, and 57 real images;
6. zero `.hwp`, `.hwpx`, `.pdf`, spreadsheet, or archive attachment files; and
7. root manifest/article/index checksum consistency.

Every article has exactly these three assets:

```text
assets/hero.png
assets/summary-card.webp
assets/timeline.webp
```

All image records have verified bytes, SHA-256, dimensions, MIME, rights status/basis, alt text,
caption, attribution, and null external source URL.

## Django and Brave acceptance

The final hidden Django process used:

```text
launcher PID: 40200
listener PID: 31036
address: 127.0.0.1:8000
```

Logs:

```text
output/housing/2026-08-28/acceptance/django-final.stdout.log
output/housing/2026-08-28/acceptance/django-final.stderr.log
```

Playwright launched the exact installed executable:

```text
C:\Users\c\AppData\Local\BraveSoftware\Brave-Browser\Application\brave.exe
```

Owned Brave PIDs were `15700, 18700, 23284, 34776, 35324`; all closed after the run.

Verified pages:

```text
requested date index: http://127.0.0.1:8000/local-articles/2026-08-28/ (200)
live run index:       http://127.0.0.1:8000/local-articles/2026-08-28--run-9c1c6b19a244/ (200)
detail:               http://127.0.0.1:8000/local-articles/2026-08-28--run-9c1c6b19a244/applyhome-applyhome-apt-2026000399-2026000399-d894918390/ (200)
```

Brave assertions all passed:

- Korean headings/layout on requested date index, live index, desktop detail, and 390 px detail;
- 19 live detail links;
- hero, summary, and timeline images loaded with nonzero dimensions and alt text;
- official links are HTTPS and host-allowlisted;
- no desktop/mobile horizontal overflow and mobile body text is at least 16 px;
- zero console errors, page errors, failed requests, or bad local responses;
- both traversal probes returned 404;
- loopback status returned 200, humanizer `ready`, and `Cache-Control: no-store`;
- CSP/referrer/content-type/frame protections were present; and
- favicon loaded from the repository-owned housing image with no 404.

Evidence and screenshots:

```text
output/housing/2026-08-28/acceptance/brave-evidence.json
output/housing/2026-08-28/acceptance/brave-index-desktop.png
output/housing/2026-08-28/acceptance/brave-live-index-desktop.png
output/housing/2026-08-28/acceptance/brave-detail-desktop.png
output/housing/2026-08-28/acceptance/brave-detail-mobile.png
```

Visual inspection confirmed a readable index, all three desktop images, and clean mobile wrapping.

## Static and CI-material verification

Final changed-file/local-content Ruff:

```text
All checks passed!
```

Final local CI-material command after the code commit:

```powershell
.\.venv\Scripts\python.exe scripts\ci_changed_python.py `
  --event explicit `
  --base 711c03869674fbb12cb0554f31552da60927e664
```

```text
All checks passed!
```

`git diff --check` exited 0. GitHub-hosted CI was not executed locally and is not claimed.

## Runtime shutdown and remaining concerns

Only owned processes were stopped. Final state:

```text
humanizer PID 38888: stopped
Django PIDs 40200/31036: stopped
port 3210: free
port 8000: free
Brave owned PIDs: closed
```

Remaining concerns are limited to existing ledgered repository debt:

- whole-repository Ruff: 1,566 findings, 208 auto-fixable; the largest groups are E501 1,067,
  DJ001 139, I001 125, DJ008 62, and E402 56;
- six deterministic-test warnings about absent generated `staticfiles/`;
- some official LH pages expose schedule/price/eligibility only through attachments. Those bytes
  were deliberately not fetched; unavailable facts remain explicit warnings/unknown values; and
- prior failed/intermediate live runs and logs remain ignored under `output/housing/` as requested.

No `.env.local`, `.local`, tool binary, secret, Codex job, humanizer job body, or live output bundle
was committed.
