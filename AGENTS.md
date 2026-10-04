# Wisdome Super Writer agent instructions

## Canonical analysis reports

- Service analysis: `docs/service-analysis.md` (English).
- Code analysis: `docs/code-analysis.md` (English).
- Critical review/adjudication: `docs/analysis/2026-10-03-two-round-review.md`.
- Code modification/continuation plan (English):
  `docs/superpowers/plans/2026-10-03-critical-review-remediation.md`.
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
  Use `PYTHONUTF8=1` for Python subprocess output. Record Windows PowerShell
  fixture/encoding/timing failures truthfully; ASCII-path checks do not establish
  Unicode support. See the code report's diagnosis before repeating these checks.
  Local-content lint: `.venv/Scripts/python.exe -m ruff check src/apps/local_content`.
  Record any whole-repository Ruff findings truthfully; an existing lint-debt
  ruling does not make `ruff check .` pass.
- Local preview binds to `127.0.0.1:7667`; the sibling humanizer uses loopback
  port 3210. Local mode is development-only and disables external publishing.
- PostgreSQL/Celery/Redis/S3, real publishers, PaddleOCR, and the legacy HWP
  sandbox require separate verification. Keep the HWP activation boundary closed
  until its signed acceptance requirements are implemented and satisfied.

## Personal machine preferences

- Prefer direct filesystem and purpose-built tools for routine work.
- Do not reinstall Orca or its Claude/OpenCode status hooks unless explicitly
  requested by the user.
