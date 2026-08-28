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

Pending the final live rerun, deterministic runner, machine artifact audit, and
all-page Brave pass. The final section will record the new ignored run path, exact
counts, command results, service PIDs/logs, screenshots, shutdown state, and commit.
