# Policy Funding Implementation Plan

> For agentic workers: use superpowers:executing-plans with task-level TDD and one
> independent final review. User explicitly authorized the complete workflow;
> preserve that authorization and the resume ledger instead of repeating approvals.

**Goal:** Working official policy-funding collection and easy Korean education in
the existing service, with source-grounded verification of the supplied sample.
**Architecture:** Dedicated apps.policy_funding module, existing safe HTTP and v2
bundle writer, independent policy schemas, CLI and read-only Django UI.
**Tech stack:** Locked Python 3.12/Django/HTTPX/selectolax, no new dependencies.
**Spec:** ../specs/2026-10-05-policy-funding-design.md.
**Ledger:** ../../analysis/2026-10-05-policy-funding-progress.md.

## Global constraints

- KST dates; bounded official HTTPS reads; preserve source identities and unknowns.
- Korean product education, English process documents; no invented loan approval.
- Source directory coverage is distinct from live collector coverage.
- Preserve existing WIP; no unrelated fixes, automatic commit/push or publisher writes.
- Fixture runs are explicit and cannot satisfy live acceptance.

## Review focus

- Notice year/program/region mismatch despite matching dates or amounts.
- Budget exhaustion/withdrawals absent from a bounded list versus proven closure.
- Government route changes, cross-host redirects and official repost/origin distinctions.
- Untrusted reference text, HTML links, tampered inventories and stale source excerpts.
- Array/missing fields, mixed national/local scope and subsidies mistaken for loan rates.

### Task 1: Catalog, schema and evidence contracts

Files: config/policy-funding/sources.json; apps/policy_funding/catalog.py,
contracts.py; tests/unit/test_policy_funding_contracts.py; reference fixtures.
Interfaces: load_catalog() -> tuple[FundingSource, ...]; FundingNotice normalized
records with stable_id, source_id, title, issuer, official_url, published_on,
application_period, regions, kinds, evidence_text, attachments and fetched_at.

- [x] Write failing tests for 17 buckets, official URL identity, invalid records and
  fixture/user provenance; preserve the supplied text and typed claims.
- [x] Implement frozen contracts, bounded/closed serialization and researched catalog.
- [x] Run focused tests; record source references and classification decisions.

### Task 2: Official collectors

Files: collectors.py and parsing.py; tests/unit/test_policy_funding_collectors.py;
tests/fixtures/policy_funding/ (reduced OLS/Bizinfo files).
Interfaces: collect_source(source_id, limits, fetcher=...) -> CollectionOutcome with notices,
errors, request/coverage counts and truncation; parsers return typed notices.

- [x] Red tests: real saved OLS/Bizinfo HTML, both open/ended lists, canonical query
  IDs, wrong-host redirects, blank pages, partial failures and date boundaries.
- [x] Implement bounded no-key collectors using a policy wrapper over shared safe_get/
  safe_post_form (OLS search is JSON, HTTP 201); validate
  every detail/attachment link, retaining only researched routes and explicit facts.
- [x] Verify parser/fetch controls and one real bounded public-source read per adapter.

### Task 3: Education, notice explanations and reference verification

Files: education.py, rendering.py, verification.py, reference_evidence.py;
tests/unit/test_policy_funding_education.py and test_policy_funding_verification.py.
Interfaces: learning_content()/render_guide(); render_notice(notice);
verify_provided_case(notices) -> dated field verdicts with source/date/context.
The supplied case is the supported verification contract, not an arbitrary-claim engine.

- [x] Red tests: certificate and ceiling are not approval, optional guarantee stage,
  direct versus agency loan, subsidy versus interest, hypothetical examples, wrong
  year/region/program and unresolved claims.
- [x] Implement cited plain Korean content and context-bound verdicts. Never turn
  the supplied sample into automatically verified current financial terms.
- [x] Validate the exact supplied national/Pohang/Yangsan/Yeongcheon cases against
  researched official evidence and preserve uncertainty/corrections.

### Task 4: Immutable workflow and CLI

Files: workflow.py, storage.py, apps.py, management/commands/collect_policy_funding.py;
tests/unit/test_policy_funding_workflow.py.
Interfaces: run_collection(...)->Path; list_runs(root)/read_run(root, name) return
validated metadata with typed FundingNotice records;
source comparison and added/changed/unchanged digest; fixture mode explicit.

- [x] Red tests: repeat/changed notice, failed source, bounded absence, stale/tampered
  metadata, interrupted run, path escaping and fixture/live separation.
- [x] Implement v2 shared writer with closed policy metadata and independently
  validated reader; add bounded CLI and visible source outcomes.
- [x] Run two deterministic collections proving replay/change behavior, then live
  collection with recorded coverage and failures.

### Task 5: Integrated Django experience

Files: views.py, urls.py; templates/policy_funding/; static/policy_funding/;
settings/__init__.py, wisdome_writer/urls.py, admin_console/base.html; UI tests.
Interfaces: /policy-funding/ index, guide, sources and notice/run detail routes;
query/region/kind filters; read-only pages with local-loopback/staff access.

- [x] Red tests for navigation, filters, novice explanations, source/date badges,
  no arbitrary network calls, nonlocal denial, staff access and escaping.
- [x] Implement accessible responsive Korean screens and integration links.
- [x] Verify with Django client and actual browser when available; preserve screenshots
  and exact limits if only DOM/client checks are available.

### Task 6: Integration verification and restart-safe handoff

- [x] Run new tests, Django check/migration drift, changed-module Ruff, full repository
  unit/integration suite and a bounded live official-source run.
- [x] Obtain fresh independent review of the feature; verify and fix material findings
  with focused regressions, then recheck affected boundaries.
- [x] Save English source research, design, decisions, test/live results and case
  verification; update canonical code/service analysis, README and AGENTS paths.
- [x] Record exact completed/limited coverage, remaining external gates and reproducible
  start/collect/view commands. Do not equate fixture passes with live acceptance.

## Completion evidence and implementation clarifications

- All six tasks are implemented and verified; completion means the specified
  collection/education feature, not exhaustive government coverage or external
  financial approval/publication. See the dated acceptance record and progress ledger.
- Full suite: 1,425 passed, 3 skipped, 203 subtests in 515.01 seconds with QA
  listeners stopped. New modules/tests and local-content lint pass; whole Ruff's
  original 1,561 findings remain. Django check/drift and Markdown links pass.
- Final live two-run check: 12 notices/run, 19 requests/run, no source errors;
  repeated final run unchanged=12. Desktop/mobile Brave checks passed.
- Education content lives in a typed Python content module, with portable dated
  evidence in reference_evidence.py; a second mutable knowledge JSON copy is not
  maintained. The reference sample is preserved in UTF-8 test data.
- One independent review plus one scoped re-review was completed. Parent closed
  the two remaining negation/board subcases with RED-to-GREEN tests, without
  claiming an additional independent verdict. No known reviewed issue remains open.
- Three initially minor items were completed because they were explicit plan
  requirements: registration-date meaning, portable evidence and CLI region filter.
- No task-authority checkbox for the old distributed project was changed. No
  automatic commit, merge, push or external publisher action was performed.

## Completed bounded follow-up — 2026-10-06

The user's additional consultant message and request for approachable explanations
extend the existing beginner guide, not the module architecture.

- [x] Keep duplicate input as one source; separate official claims, conditional
  routes, personal opinions, forecasts and unknown original-message year.
- [x] Add six conversational answers with hypothetical examples to the shared
  curriculum, HTML guide and newly generated Markdown guide.
- [x] Show twelve dated manual claim groups without changing closed bundle contracts
  or claiming a free-form financial question-answer engine.
- [x] Reproduce and repair the explicit direct-loan application-period heading gap;
  preserve both days and daily hours, stop before footnotes.
- [x] Verify: 63 funding tests, Django check/drift and scoped Ruff pass; forty live
  notices/47 requests/no errors; actual desktop/mobile guide inspection passes.
- [x] Save English adjudication, limits and restart instructions in
  [the follow-up report](../../analysis/2026-10-06-policy-funding-consultant-claims.md),
  and refresh canonical code/service reports.

Future individualized advice, arbitrary-message verification, current graduation
attachment criteria and a specific KOSMES October notice remain separate product
or research questions. Historical full-suite evidence is preserved as October 5
evidence; no new full-suite execution is claimed.
