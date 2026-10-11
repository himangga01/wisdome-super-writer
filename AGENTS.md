# Wisdome Super Writer agent instructions

## Canonical analysis reports

- Browser permission root cause:
  `docs/analysis/2026-10-09-browser-access-diagnosis.md`.
  Read before further browser recovery. The original chat
  `01a100b3-6786-7330-82bf-3a4bb6f44ef4` had persisted origin denies that took
  precedence over global approval. User-authorized fresh chat
  `01a11c68-dc66-7b60-b39d-0c14a6716e8a` successfully opened the exact reference
  and local preview in sanctioned Chrome on October 9; desktop and 390 x 844
  responsive menu/period/overflow checks passed. Captures and DOM evidence:
  `docs/acceptance/browser-recovery-2026-10-09/`. This does not reset the original
  chat's denies. Follow the current conversation's actual tool decision; never
  edit permission files or use alternate access to bypass a denial. Refresh this
  canonical diagnosis after further recovery analysis.

- Active October data refresh and year/month UX:
  `docs/analysis/2026-10-08-monthly-data-update.md`.
  Read its date semantics, collection outcomes and linked design/plan before resuming.
  Research: `docs/analysis/2026-10-08-monthly-housing-recon.md` and
  `docs/analysis/2026-10-08-monthly-semiconductor-recon.md`.
  Preserve original-date vs capture-date meaning, historical runs, unknown dates,
  source rights and local-only archive provenance. Failed live writes are not updates.
  Housing month selection uses actual special-supply/first-priority reception dates
  with regional labels; funding uses official posting/registration dates. Do not
  substitute generic application windows or collection dates for either.
  Storage investigation: `docs/analysis/2026-10-08-monthly-storage-diagnosis.md`.
  Source polling ledgers are authoritative; do not erase or bypass reserved intervals.
  Monthly critical review: `docs/analysis/2026-10-08-monthly-update-review.md`.

- Active three-topic portal redesign:
  `docs/analysis/2026-10-08-topic-portal-progress.md`.
  Read its implementation and browser-permission checkpoints before resuming.
  User-supplied reference screenshots are saved under `docs/reference/topic-portal/`.
  October 9 fresh-chat Chrome acceptance now covers root, topic menus, period
  controls, internal destinations and responsive overflow; the reference URL and
  initial video frame were inspected. Full-video/physical-mobile verification
  remains outside that evidence. Read the browser diagnosis for capture limits
  and current authorization; do not bypass any saved browser denial.
  Design: `docs/superpowers/specs/2026-10-08-topic-portal-design.md`;
  plan: `docs/superpowers/plans/2026-10-08-topic-portal.md`;
  independent review: `docs/analysis/2026-10-08-topic-portal-review.md`.

- Policy funding feature (implemented and verified):
  `docs/analysis/2026-10-05-policy-funding-progress.md`.
  Read its task table and linked design/implementation plan before changes. User authorized
  research through implementation and verification. Preserve the supplied sample as
  untrusted test data; current financial terms require dated official evidence.
- Policy funding acceptance: `docs/acceptance/2026-10-05-policy-funding-acceptance.md`.
  Conversational education and consultant-claim follow-up:
  `docs/analysis/2026-10-06-policy-funding-consultant-claims.md`.
  Read it before changing the explanations. Keep opinions/forecasts separate from
  official rules; preserve date/year uncertainty, program-specific windows and exceptions.
  Source research: `docs/analysis/2026-10-05-policy-funding-national-research.md` and
  `docs/analysis/2026-10-05-policy-funding-regional-research.md`. Architecture mapping:
  `docs/analysis/2026-10-05-policy-funding-integration-recon.md`. Independent review:
  `docs/analysis/2026-10-05-policy-funding-review.md`.
- The funding module has separate schemas/output from housing. Keep fixture/live
  provenance, source/board identity, observed open/ended status, unknown regions,
  evidence context and confirmation-versus-approval distinctions intact. Source
  directory entries do not establish active or exhaustive collection coverage.
- Service analysis: `docs/service-analysis.md` (English).
  For service introductions, reuse its "Reusable service introduction and positioning"
  section, recheck current implementation, and preserve the operator/development
  scope and separate operational acceptance of publishing integrations.
- Code analysis: `docs/code-analysis.md` (English).
- Latest service/code revalidation: 2026-10-10 sections in those same canonical
  reports. Read them before resuming: 468 focused tests passed, 16 bad PostgreSQL
  lock shapes and selected R1/R7/R13/R5 defects reproduced; active SQLite has 71
  pending migrations although fresh isolated migration succeeds. Saved October
  counts remain housing 23 / funding 17 / news 2. These checks did not rerun the
  full suite, collect new data, migrate active state or establish new browser/
  distributed acceptance. Preserve the uncommitted feature work and refresh these
  reports after later substantive analysis.
- Critical review/adjudication: `docs/analysis/2026-10-03-two-round-review.md`.
- Code modification/continuation plan (English):
  `docs/superpowers/plans/2026-10-03-critical-review-remediation.md`.
- Documentation analysis and follow-up code review of `db5edec`:
  `docs/analysis/2026-10-05-documentation-and-code-review.md` (document-to-code
  discrepancies D1–D21 and findings R1–R16; reboot continuation verification and
  synthesis completed on 2026-10-05). Read its "Continuation steps" and the plan's
  Task 8 for implementation priorities, especially remaining PostgreSQL locks,
  credential/canary recovery and schedule isolation. Findings are not code fixes.
- For later review or implementation, read the relevant canonical report and the
  review/plan above. Revalidate current bytes, release identity and working-tree
  changes; preserve six dated reviewer reports and distinguish completed repairs
  from unexecuted PostgreSQL/channel/corpus acceptance and open feature tasks.
- Before a requested code or service analysis, read the relevant report as the
  starting reference. Check its analysis date and source baseline against current
  `HEAD`, branch, working-tree changes, and newer evidence; verify affected
  conclusions in the current source.
- After every requested substantive analysis, update the relevant Markdown report
  before the final reply. For another analysis topic, reuse its existing report or
  create `docs/analysis/<topic>.md` and add its canonical path here.
- Record date, scope, source baseline, implemented behavior, evidence, commands
  and actual results, risks, priorities, open questions, and verification limits.
  Distinguish plans, code, tests executed now, and historical acceptance. Preserve
  a short dated update log and historical review evidence. Never record secrets.
- These project files are the common handoff for all AI agents. Keep one canonical
  report per topic; a `CLAUDE.md` bridge imports this file for Claude Code.

## Implementation and historical evidence

- `specs/001-automated-content-publishing/tasks.md` is the task and dependency
  authority. Do not check a task off solely because its code exists or tests pass;
  honor its explicit acceptance conditions and outstanding dependencies.
- `REMAINING_WORK.md` and
  `specs/001-automated-content-publishing/T031_WORK_IN_PROGRESS.md` preserve the
  2026-08-21 operations handoff. Check newer code and evidence before treating
  their status statements as current.
- `docs/acceptance/2026-08-28-live-housing-acceptance.md` is historical local
  housing acceptance. It does not prove a new live run or distributed deployment.
- Preserve unrelated working-tree changes and dated review/acceptance records.

## Runtime and verification

- Python is restricted to 3.12; dependencies are pinned in `pyproject.toml` and
  `uv.lock`. Use the existing environment or `python -m uv sync --frozen --extra dev`.
- Set `WISDOME_ENVIRONMENT=development` and `WISDOME_RUNTIME_MODE=local` for local
  checks. Run `.venv/Scripts/python.exe src/manage.py check`, then
  `makemigrations --check --dry-run`; use a disposable local state directory for
  fresh migration verification.
- Tests: `.venv/Scripts/python.exe -m pytest -q tests/unit tests/integration`.
  Initialize `.env.local` from its example if absent; preserve an existing file.
  Use default local roots for pytest because local runtime/script tests assert
  defaults and create independent fixture repositories. Remove inherited root
  overrides from the test subprocess environment rather than setting them empty.
  The actual settings are `LOCAL_STATE_ROOT`, `LOCAL_OBJECT_ROOT` and
  `LOCAL_ARTICLE_ROOT`; historical `LOCAL_OUTPUT_ROOT` was a documentation mistake.
  Clear that obsolete name too when inheriting an old test shell.
  Use `PYTHONUTF8=1` for Python subprocess output. Record Windows PowerShell
  fixture/encoding/timing failures truthfully; ASCII-path checks do not establish
  Unicode support. See the code report's diagnosis before repeating these checks.
  Local-content lint: `.venv/Scripts/python.exe -m ruff check src/apps/local_content`.
  Record any whole-repository Ruff findings truthfully; an existing lint-debt
  ruling does not make `ruff check .` pass.
- Local preview binds to `127.0.0.1:7667`; the sibling humanizer uses loopback
  port 3210. Local mode is development-only and disables external publishing.
- Stop only task-owned preview servers before the full Windows suite: lifecycle
  tests temporarily bind ports 3210 and 7667. An active QA server on 7667 can make
  the existing-readiness test time out. Never terminate an unrelated listener.
- Funding UI: `/policy-funding/`; collection command: `collect_policy_funding`.
  Its root is `POLICY_FUNDING_ROOT` (default `output/policy-funding`). Keep the
  funding root independent of `LOCAL_ARTICLE_ROOT` and housing inventories.
- Local news metadata: `collect_semiconductor_news --year YYYY --month MM` writes
  `SEMICONDUCTOR_ARCHIVE_ROOT` (default `output/semiconductor`). Its local archive
  is independent of database source approval, drafts and publishing. Respect
  persisted source polling; unavailable database drafts remain unavailable even
  when local news metadata is available.
- PostgreSQL/Celery/Redis/S3, real publishers, PaddleOCR, and the legacy HWP
  sandbox require separate verification. Keep the HWP activation boundary closed
  until its signed acceptance requirements are implemented and satisfied.

## Personal machine preferences

- Prefer direct filesystem and purpose-built tools for routine work.
- Do not reinstall Orca or its Claude/OpenCode status hooks unless explicitly
  requested by the user.
