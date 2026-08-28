# 2026-08-28 Live Housing Acceptance

This tracked document is the durable acceptance record for the Dockerless local
housing writer. Generated live bundles, machine evidence, screenshots, and logs
remain ignored under `output/housing/`; the coordinator SDD report remains in its
ignored `.superpowers` location.

## Baseline acceptance

The first acceptance pass at base
`711c03869674fbb12cb0554f31552da60927e664` established:

- Dockerless Python 3.12 setup and Django migration checks;
- a clean focused suite and full suite after SQLite teardown remediation;
- official ApplyHome and LH public HTML collection for the KST window beginning
  `2026-08-22T00:00:00+09:00`;
- 72 official observations, 29 non-residential exclusions, 43 indexed residential
  notices, 19 detailed articles, and 57 generated/owned images;
- 19 locally humanized and verified article bundles without official attachment
  bytes; and
- local Django/Brave preview acceptance with owned processes stopped afterward.

The original acceptance-support commits were:

- `3a4e5f5 test: verify dockerless housing writer end to end`
- `8433a53 docs: record live housing acceptance`

## Review remediation

Review round 1 replaces manually asserted evidence with independently reproducible
evidence:

- an actual deterministic subprocess runner records exact argv, UTC timestamps,
  exit codes, bounded log paths, output byte counts, and SHA-256 digests;
- whole-repository Ruff remains truthfully nonzero and `passed: false` when legacy
  debt is observed, under an explicit ID/text/hash ruling; changed-file Ruff,
  every `local_content` Python file, and CI-material checks remain binding gates;
- the workflow persists a closed, metadata-only `raw-observations.json` and binds
  it and `notices.json` in the schema-v2 root manifest;
- production bundle validation now enforces an exact schema-v2 run inventory;
- each final bundle stores a closed `humanize/audit.json`; the independent auditor
  rebuilds protection from the draft, reruns the production humanization verifier,
  and checks draft/final factual structure, front matter, sources, and all images;
- public detail fallback parsing binds official identity and title material before
  accepting stable ApplyHome/LH structures; generic, error, unbound sparse, and
  mismatched responses fail closed;
- sanitized two-page official-table regressions cover both sources;
- SQLite test cleanup restores every successfully dropped trigger after partial
  drop/alias failures and preserves the primary drop/flush exception if restoration
  also fails; and
- Brave acceptance checks exact response headers and every detailed article on
  desktop, plus a representative mobile detail.

## Authoritative round-1 evidence

The review support implementation is committed as `62e879a`.

The authoritative ignored run is:

```text
output/housing/2026-08-28--run-9cf4661bcd83
```

Its machine report is:

```text
output/housing/2026-08-28--run-9cf4661bcd83/acceptance-report.json
```

The final machine-derived verdict is `overall_passed=true`; its four derived gates
(`workflow_live_success`, `artifact_audit`, `django_brave_all_pages`, and
`deterministic_commands_and_ruff_ruling`) all pass.

### Official collection and artifacts

The authoritative window is `2026-08-22T00:00:00+09:00` through
`2026-08-28T13:23:31.621371+09:00`.

- ApplyHome: 16 observations, collection `OK`, zero detail failures.
- LH: 56 observations, collection `OK`, zero detail failures.
- Raw official observations: 72 unique stable IDs.
- Excluded by production `is_residential`: 29.
- Residential index rows and official links: 43.
- Detailed, written, and humanizer-verified articles: 19.
- Verified images: 57, exactly hero/summary/timeline per article.
- Official attachment bytes copied: zero.
- Humanizer audit proofs: 19 unique job hashes and 19 verification hashes.

The production auditor passed schema-v2 root validation, exact root/bundle
inventories, raw-source completeness, production selection recomputation, exact
index ID/link reconciliation, all article/source/image checks, and a fresh
`verify_humanization_audit` call for every final article.

### Deterministic commands

The evidence runner executed and recorded exact argv, timestamps, exit codes, log
paths, byte counts, and SHA-256 output digests. The authoritative attempt passed:

- setup-local: exit 0;
- Django check: exit 0;
- migration check: exit 0;
- focused pytest: 499 passed, 2 skipped, 6 warnings;
- unrestricted pytest: 1224 passed, 5 skipped, 8 warnings, 198 subtests;
- changed/branch Python plus every `local_content` Python Ruff target: exit 0;
- local CI-material check: exit 0; and
- diff check against the requested base: exit 0.

Whole-repository Ruff exited 1 with 1,566 legacy findings. Its command record remains
`passed=false` and `disposition=legacy_debt_not_gate`; the explicit ruling ID, text,
and SHA-256 are stored in the report. It does not weaken changed/local-content Ruff.

The first deterministic attempt is retained separately. It failed only because the
then-running required humanizer owned port 3210 while a local-script test needed to
bind that port (`1 failed, 1223 passed, 5 skipped, 198 subtests`). Only the owned
humanizer was stopped, both ports were verified free, and the complete matrix was
rerun rather than selectively retrying the failing test.

GitHub-hosted CI was not executed locally and is not claimed.

### Django and Brave

Playwright launched the exact installed Brave executable and opened both indexes,
all 19 detail URLs on desktop, and one representative 390 px mobile detail.

- Every page returned 200 and had Korean layout without horizontal overflow.
- Every detail loaded exactly three nonzero images with alt text.
- Every detail's official links were HTTPS and host-allowlisted.
- CSP, referrer, nosniff, frame, and cache headers matched exact required values.
- The representative immutable asset headers matched exactly.
- Console errors, page errors, failed requests, and bad local responses were zero.
- Both traversal probes returned 404.
- Mobile body font was 16 px.
- Brave owned PIDs `7980, 9176, 36508, 37700, 38744` all exited.

Evidence is retained under:

```text
output/housing/2026-08-28/acceptance/brave-round1/
output/housing/2026-08-28/acceptance/deterministic-round1-attempt2/
output/housing/2026-08-28/acceptance/deterministic-round1-attempt2-evidence.json
```

### Process ownership and remaining concerns

The live humanizer used owned PID/listener `35204`; the preview humanizer used
`43636`; Django used launcher/listener `30508/38632`. Only those owned service
processes were stopped. Ports 3210 and 8000 are free, and no Brave process launched
by the acceptance run survives.

Remaining concerns are limited to the ledgered whole-repository Ruff debt, eight
non-failing pytest warnings (six absent-generated-staticfiles notices and two
Windows subprocess reader encoding warnings), and official facts available only in
attachments. Attachment bytes remain deliberately unfetched and uncopied.

## Review fix round 2

The round-2 evidence-contract implementation is committed as `8cda141`.

Deterministic evidence now uses a production-defined ordered command plan with exact
argv, gate classification, parser, and allowed exits. The selected schema-v2 attempt
is `round2-attempt1`, with plan SHA-256
`a5e15afd772067fc0f5d0b21f7f4438e661a1e954c126a89b834d949762fc7b0`.
Each command record contains aware start/end timestamps, duration, exit code, output
and log byte counts/SHA-256, confined log path, and a closed parsed result.

The finalizer independently rebuilt the plan and verified every command in order,
re-read each confined log, recomputed its byte count and digest, reparsed results,
and checked the exact pinned Ruff ruling. Counterexamples for a fake ruling, wrong
argv, missing digest, launch exit 127, reordered command, naive timestamp, escaped
log path, modified log, forged parsed count, and forged plan hash all fail.

Retained attempt history is digest-bound and ordered:

- `round1-attempt1`: schema 1, failed `full_pytest` because port 3210 was occupied;
- `round1-attempt2`: schema 1, passed under the older evidence contract; and
- `round2-attempt1`: schema 2, independently eligible and selected.

The exact selection rule is `latest_complete_exact_plan_pass`; only
`round2-attempt1` is eligible under the closed schema. Its authoritative results are:

- setup-local: exit 0;
- whole-repository Ruff: exit 1, 1,566 parsed findings, `passed=false`, exact pinned
  `legacy_debt_not_gate` ruling;
- Django and migrations: exit 0;
- focused pytest: 533 passed, 2 skipped, 6 warnings;
- full pytest: 1251 passed, 5 skipped, 8 warnings, 198 subtests;
- changed/branch plus every `local_content` Ruff target: exit 0;
- CI-material and diff checks: exit 0.

Humanizer evidence no longer treats prose reuse as a uniqueness gate. The report
transparently records 19 candidate output hashes with 14 unique values and
`candidate_output_uniqueness_gate=false`. Binding identities remain strict: 19/19
unique job hashes, 19/19 final bundle hashes, 19/19 final article hashes, and 19/19
verification-record hashes. Every candidate was still reverified against protected
input and factual/frontmatter/source/image identity.

ApplyHome now evaluates the complete set of identity-bearing observed response and
official attachment/detail URLs; it must equal the expected singleton pair. LH now
requires each hidden identity field's complete value set to equal its expected
singleton. Matching-plus-wrong mixed identities fail for both sources.

Artifact audit also requires the exact literal start
`2026-08-22T00:00:00+09:00`, literal `+09:00` end representation, same KST run date,
and end not after audit execution. UTC-equivalent strings fail even when they denote
the same instant. The existing live run re-audits with all artifact requirements and
the four final derived acceptance requirements passing.

Brave was not rerun because neither browser evidence nor preview behavior changed;
the previously retained all-19-page schema-v2 Brave evidence remains a passing gate.
No service was started for round 2, and ports 3210/8000 remain free.

New deterministic evidence is retained under:

```text
output/housing/2026-08-28/acceptance/deterministic-round2-attempt1/
output/housing/2026-08-28/acceptance/deterministic-round2-attempt1-evidence.json
```
