# Task 12 Round 2 Evidence Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Task 12 deterministic, humanizer, source-identity, and KST evidence independently verifiable from closed persisted material.

**Architecture:** The production acceptance runner owns one canonical ordered command plan and pinned Ruff ruling. It records complete attempt facts, while the production finalizer reconstructs the expected plan, reads and hashes confined logs, reparses command outcomes, and selects only an eligible attempt. Artifact auditing separately verifies four unique per-article identities while reporting duplicate candidate prose non-gating, and source/date validators reject ambiguous identity sets and non-literal KST representations.

**Tech Stack:** Python 3.12, pytest, Django 5.2, Ruff, subprocess, SHA-256, pathlib, selectolax.

**Spec:** `.superpowers/sdd/2026-08-28-dockerless-local-housing-writer/task-12-brief.md` plus parent review round 2.

## Global Constraints

- Work only in the existing Dockerless linked worktree.
- Use strict red-green TDD for production behavior changes.
- Do not dispatch subagents or reviewers.
- Keep live output, logs, screenshots, and acceptance JSON ignored.
- Never commit secrets, `.env.local`, `.local`, tool binaries, or job bodies.
- Whole-repository Ruff is waivable only under the exact pinned ruling; changed/local-content Ruff remains binding.

---

### Task 1: Closed deterministic plan and independent verification

**Files:**
- Modify: `src/apps/local_content/acceptance_runner.py`
- Modify: `src/apps/local_content/acceptance.py`
- Modify: `scripts/run_task12_deterministic.py`
- Modify: `scripts/finalize_task12_acceptance.py`
- Modify: `tests/unit/test_task12_deterministic_runner.py`
- Modify: `tests/unit/test_local_content_acceptance.py`

**Interfaces:**
- Produces: canonical `CommandSpec` sequence, `TASK12_LEGACY_RUFF_*` constants, schema-v2 attempt evidence, `verify_deterministic_evidence(...)`.
- Consumes: actual command logs and current repository/base/changed-Python context.

- [ ] Write failing tests for exact argv/order/plan hash, timestamps/duration, log confinement/digests, parsed pytest/Ruff outcomes, exit 127 rejection, fake ruling rejection, and selected attempt history.
- [ ] Run the focused runner/finalizer tests and confirm each new mutation fails for the intended reason.
- [ ] Implement schema-v2 result capture and independent finalizer verification with the exact pinned ruling.
- [ ] Re-run focused tests until green, then Ruff the changed files.

### Task 2: Humanizer identity cardinality without prose uniqueness

**Files:**
- Modify: `src/apps/local_content/acceptance.py`
- Modify: `tests/unit/test_local_content_acceptance.py`
- Modify: `tests/integration/test_local_housing_workflow.py`

**Interfaces:**
- Produces: a production cardinality verdict for job, final bundle, final article, verification, and candidate-output hashes.
- Consumes: production bundle manifests and `verify_humanization_audit` results.

- [ ] Write failing counterexample tests showing duplicate candidate output hashes remain passing but duplicate job/bundle/article/verification identities fail.
- [ ] Run the new tests red.
- [ ] Implement audit collection and the closed cardinality verdict, including transparent `14/19`-style candidate evidence.
- [ ] Run unit and workflow integration tests green.

### Task 3: Reject mixed official detail identities

**Files:**
- Modify: `src/apps/local_content/sources/applyhome.py`
- Modify: `src/apps/local_content/sources/lh.py`
- Modify: `tests/unit/test_applyhome_public_html.py`
- Modify: `tests/unit/test_lh_public_html.py`

**Interfaces:**
- ApplyHome requires the complete observed identity-pair set to equal the expected singleton.
- LH requires every required hidden-field value set to equal its expected singleton.

- [ ] Add matching-plus-wrong mixed-value tests for both sources and run them red.
- [ ] Collect all observed values before deciding authenticity; never return after the first match.
- [ ] Run both public HTML suites green.

### Task 4: Enforce literal KST window representation

**Files:**
- Modify: `src/apps/local_content/acceptance.py`
- Modify: `tests/unit/test_local_content_acceptance.py`
- Modify: `tests/integration/test_local_housing_workflow.py`

**Interfaces:**
- Audit requires exact start text, literal `+09:00` offsets, run-date end, and end no later than the supplied execution time.

- [ ] Add UTC-equivalent start and non-KST/future end counterexamples and run them red.
- [ ] Implement exact string/offset/date/execution validation.
- [ ] Re-run artifact audit tests green.

### Task 5: Regenerate authoritative evidence and reports

**Files:**
- Modify: `docs/acceptance/2026-08-28-live-housing-acceptance.md`
- Append ignored: `.superpowers/sdd/2026-08-28-dockerless-local-housing-writer/task-12-report.md`

**Interfaces:**
- Consumes: schema-v2 runner output and unchanged Brave evidence.
- Produces: selected-attempt machine verdict and durable round-2 report.

- [ ] Run targeted tests, all changed/local-content Ruff, Django checks, migrations, and diff check.
- [ ] Commit the implementation before authoritative execution.
- [ ] Run the complete deterministic command runner with ports free, including the actual full pytest suite.
- [ ] Re-run the artifact audit and finalizer; confirm candidate outputs are reported transparently and all binding identities are unique.
- [ ] Append exact attempts, counts, commands, paths, concerns, and shutdown state to both reports.
- [ ] Commit tracked documentation and verify the final worktree/status.

## Self-Review

- All four review findings map to Tasks 1-4; evidence regeneration and reporting map to Task 5.
- The plan contains no placeholders or deferred implementation.
- Runner/finalizer interfaces use the same canonical plan, attempt, ruling, and verification names.
- No Brave rerun is included because its evidence schema is unchanged, as explicitly permitted.
