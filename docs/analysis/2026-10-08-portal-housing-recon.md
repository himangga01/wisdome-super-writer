# Portal housing reconnaissance

## Baseline and scope

- Date: 2026-10-08, Asia/Seoul.
- Source: `codex/policy-funding`, `db5edec35aad39ae85e3aca7b6f9e1b9f2e3a0dc`, plus the existing uncommitted funding implementation and documentation updates.
- Original assignment: read-only source reconnaissance for a three-topic portal and `/housing/` dashboard. That pass authored only this report. The authorized implementation follow-up below supersedes its loader recommendations; original observations remain historical evidence.
- Starting authorities: [AGENTS.md](../../AGENTS.md), shared `C:/Users/강지혜/.agents/analysis-policy.md`, [service report](../service-analysis.md), [code report](../code-analysis.md), follow-up review continuation steps and remediation-plan Task 8.
- Initial dirty tracked files: AGENTS.md, README.md, service/code/two-round reports, remediation plan, `local_content/bundles.py`, the local-article index and console base templates, settings and root URLs. Funding config/source/static/templates/tests and dated funding documents were already untracked. All were preserved.
- The supplied reference URL was permission-denied and was not accessed through any tool or alternative route.

## Current implementation and reusable contracts

The local housing service collects official ApplyHome/LH material into immutable filesystem runs. It selects residential notices, writes some detailed articles, and exposes validated Markdown/image previews. It is separate from the database-backed operator console and from policy-funding storage. A portal should show saved observations, their time window, their sources and available articles; it cannot establish real-time availability or new live acceptance.

| File / symbol | Existing behavior | Recommended use |
| --- | --- | --- |
| `src/apps/local_content/views.py:_require_local_request` | Requires `IS_LOCAL_RUNTIME` and a loopback `REMOTE_ADDR`; fails with 404 before reading files | First operation on every new housing page, including any partial/JSON response |
| `views.py:_article_root(required=False)` | Returns an existing confined non-linked root, returns `None` when absent, rejects symlink/reparse ancestors | Empty-state path; call before constructing readers |
| `views.py:_list_validated_runs` | Validates each candidate run and final article inventory; returns `name`, `date`, `generation`, `article_count`, `url` | Reuse for archival links/counts, with chronology caveat below |
| `views.py:_validated_run` | Parses the run name, validates its manifest and returns `(run_root, writer, manifest)` | Confined selection of a specific run |
| `src/apps/local_content/bundles.py:ArticleBundleWriter.validate_run` | Verifies closed manifest schema, index hash, referenced bundles and their semantic checks; schema v2 also verifies exact metadata inventory/hashes | Mandatory integrity authority; do not replace with a path existence check |
| `bundles.py:ArticleBundleWriter.read_validated_run_file` | Existing working-tree helper; allowlisted bounded reads, validation before/after, bound handle and manifest/file identity checks | Read `notices.json` and `raw-observations.json` only with the schema-v2 qualification below |
| `views.py:_final_articles` | Returns only manifest-referenced article directories that contain validated `article.md` | Authority for detail-link availability, never raw directory enumeration |
| `views.py:_article_manifest_files`, `_read_validated_file`, `_revalidate_run` | Reads and checks article metadata/files with validation around use | Reuse if a projection needs per-article metadata; preserve final validation |
| `bundles.py:ArticleBundleWriter.validated_run_article_metadata` | Returns strict `verification.json` records from committed final bundles, validates hashes and identities | Useful for verified identity mapping; limitations below |
| `views.py:_official_https_url` | Allows HTTPS official hosts only, no credentials or non-443 port | Validate every card's external URL; template autoescape is not URL policy |
| `views.py:_page_response`, `_secure_response` | CSP, no-store for pages, nosniff and no-referrer | Preserve on housing routes; keep immutable checksum-based asset route |

### Data actually stored

`src/apps/local_content/workflow.py:_notices_document` writes schema-1 JSON inside a schema-2 run manifest. Its `notices` includes **all selected/indexed notices**, not just those with detailed articles:

- Identity/source: `source_key`, `external_id`, `canonical_url`, `publisher`, `source_checksum`, `parser_version`.
- Display: `title`, `category`, nullable `region`, observed `status`, timezone-aware `published_at`.
- Dates: nullable `application_start`, `application_end`, `deadline`, `announcement_date`, as ISO dates.
- Facts: nullable `supply_count`, `price_summary`, eligibility/restriction lists and key/value `facts`.
- Limits: `warning_count`, sanitized `error_codes`. Raw warning explanations are not stored here.
- Envelope: collection `window.start`/`window.end`, `complete`, `detail_failures`, quarantined `conflicts`.

The producer dataclass is `src/apps/local_content/contracts.py:HousingNotice`. Missing dates must remain unknown. `published_at` is a notice publication time, not a collection/completion time. `status` is observed source wording; ApplyHome can default it to `published`, so it does not independently mean applications are open. A date-derived state may be labeled explicitly as based on the saved dates and KST today, and must preserve unknown/partial dates, corrections and original-notice confirmation. Do not replace a missing `application_end` with `deadline` silently.

`workflow.py:_raw_observations_document` stores immutable source-level `collection_code`, `observation_count`, `detail_failure_count`, plus source/notice/checksum/status observations and the same time window. This is the best source summary for a read-only dashboard. Source presence/count is not exhaustive collection coverage.

`WorkflowReport.as_dict()` contains `workflow_id`, `started_at`, `mode`, `complete`, `blocked`, `live_success`, source summaries, phases and detailed article paths. However it is persisted to **`acceptance-report.json`, explicitly mutable and not hashed in the run manifest**. Do not present this as immutable provenance or read it through the allowlisted metadata helper. The immutable notice/raw metadata has no `mode` field; a generic valid bundle cannot be labeled live merely because it validates. Show neutral saved-result wording if mode cannot be established safely.

### Integrity and projection traps

1. **Schema-v1 metadata gap:** `read_validated_run_file()` currently allows `notices.json` and `raw-observations.json` for a schema-v1 run, although v1 does not bind those files. Before using these files, require `manifest.schema_version == 2` and the exact manifest `run_files` declaration (or harden the helper itself). Older schema-v1 runs may remain usable as archive/index/detail links without enriched notice cards. Never rewrite old manifests to add fields.
2. **Same-day chronology:** `_list_validated_runs()` sorts names lexicographically. A `--run-<12 hex>` suffix derives from a workflow-ID hash, not elapsed time. For a latest snapshot, sort validated v2 metadata by parsed timezone-aware `window.end`, using the name only as a stable tie-breaker. Display it as collection-window reference time; it is initialized at workflow start. Do not use an article publication date or arbitrary suffix as collection recency.
3. **Article identity mapping:** `validated_run_article_metadata()` returns records in sorted article-name order, but does not include the directory name. It also fails an entire run if a referenced bundle lacks `verification.json` or `article.md`; existing schema-v1 preview fixtures intentionally include blocked drafts. Do not blindly zip its output to all manifest keys or replace `_final_articles()` with it. For current workflow-produced final articles, derive links only from verified final directory names and matching `(source_key, external_id, source_checksum)`. A safe per-article metadata reader is preferable if a richer projection is added.
4. **Creating directories on GET:** `ArticleBundleWriter._safe_root()` creates absent directories. Existing views avoid this with `_article_root()` before writer access. Preserve that order so an empty dashboard stays read-only.
5. **No broad JSON trust:** The writer validates bounded strict JSON, checksums and inventory, but not the complete business shape of a dashboard projection. Validate expected dictionary/list/string/date types and lengths before filtering/sorting. Skip or fail invalid material deterministically, and do not expose OS paths, tracebacks or raw document markup.
6. **Performance:** Existing run listing revalidates all referenced article bytes, including images. Reading two metadata files adds repeated run validation. Bound the displayed results and inspect cost before adding all-history aggregation; do not bypass integrity or use unchecked long-lived caches to optimize it.

## Routes, templates and access boundaries

`src/wisdome_writer/urls.py` currently maps `/` to a temporary redirect named `console-home`. `/console/` and its subpages call `src/wisdome_writer/console.py:_console_view` or `run_detail`, both protected by active-staff login and CSRF setup. Local article URLs are included only when `settings.IS_LOCAL_RUNTIME` is true; their views also independently enforce the local/loopback boundary.

Recommended route approach: add a separate portal root view, keep existing `/console/` names and staff decorators, and add `/housing/` to the local-only URL surface. A portal that reads housing data must gate that branch before touching `LOCAL_ARTICLE_ROOT`; authenticated staff in distributed mode does not grant housing filesystem access. `apps.policy_funding.views.policy_page` allows staff in nonlocal mode, so reusing it unchanged would weaken housing's current boundary. Existing policy-funding URLs and its independent root should remain separate.

Keep these routes and names working:

| URL | Name |
| --- | --- |
| `/local-articles/` | `local-article-index` |
| `/local-articles/<run_name>/` | `local-article-run` |
| `/local-articles/<run_name>/<article_name>/` | `local-article-detail` |
| `/local-articles/assets/<run_name>/<article_name>/<checksum>/<asset_path>` | `local-article-asset` |
| `/api/v1/local-articles/status` | `local-article-status` |

The three standalone templates are `src/templates/local_articles/index.html`, `run.html`, `article.html`; styling is `src/static/local_articles/preview.css`. The index currently lists runs, and run/detail templates render the escaped subset from `render_markdown_preview()`. Safe changes can add portal navigation while preserving archive context and existing route links. The console base already has a funding navigation link; its brand targets `/console/`.

The existing preview CSP has `form-action 'none'` and no inline scripts. Adding a GET search form to a new housing dashboard requires an intentional page-specific `form-action 'self'` decision; do not globally loosen historical article/asset headers. Local preview tests check the exact accepted CSP. Ordinary server-generated links and external static CSS fit the current boundary. Consider enforcing GET/HEAD explicitly for new read-only routes.

`local_article_status()` calls `local_runtime_status()`, which probes the humanizer at `127.0.0.1:3210/api/health`. Do not call it just to render a read-only housing dashboard that should have no network side effects. Saved source/run metadata is sufficient. This reconnaissance did not invoke it.

## Compatibility and verification limits

Source-inspected regression authorities:

- `tests/unit/test_local_content_preview.py`: unchanged local URLs, same-day generations, final-only links, exact favicon URL, external URL filtering, HTML escaping, checksum assets, immutable asset cache, nonlocal/remote rejection before filesystem access, no-store status and exact accepted headers.
- `tests/unit/test_local_content_bundles.py`: schema-v2 metadata hashes/closed inventory, schema-v1 compatibility, link/path/identity/race checks, strict JSON and image semantics.
- `tests/integration/test_local_housing_workflow.py`: the real workflow producer and metadata/run writing.
- `tests/unit/test_local_content_acceptance.py`, `src/apps/local_content/acceptance.py`: historical browser evidence uses the existing preview URLs and header contract.
- `tests/unit/test_operations_console_behavior.py`, `test_publishing_console_behavior.py`: console assets/behavior should remain independent from the new portal.

Executed: Git branch/HEAD/status/diff reads, targeted `rg` searches, UTF-8 source/template/test/document reads, and a nonrecursive default `output/housing` directory listing. The directory listing returned no entries; it does not establish the inventory of an alternate configured root or a runtime process. Some searches included nonexistent/globbed paths; relevant files were subsequently located through explicit directories and `rg` filters. No tests, browser session, port binding, collection, external services or actual stored-bundle validation were executed. Historical acceptance in the canonical reports remains historical.

Priorities for the implementer: (P1) preserve loopback/manifest/URL boundaries; (P1) select a true saved snapshot and show unknown dates and provenance honestly; (P2) retain archive routes and exact preview headers; (P2) add bounded projection tests covering absent roots, corrupt/v1 metadata, same-day ordering, blocked article bundles, unsafe URLs and remote/nonlocal requests. Product question: whether the dashboard should show one selected snapshot or merged latest-known notices across bounded runs; the former has clearer provenance and fewer stale/deletion assumptions.

## Handoff log

- 2026-10-08: source-only reconnaissance completed and recorded here. Parent implementation agent owns registration of this report in AGENTS.md and any common-report updates to avoid concurrent edits. Re-read the current source and this baseline before reuse; update this report when the projection/routing contract changes.

## Housing projection implementation follow-up (2026-10-08)

Authorization changed after the user supplied reference screenshots and approved the redesign. The source URL remains uninspected; neither it nor a blocked local browser origin was accessed. The parent agent owns routes, templates, access decorators, package initialization and shared reports. This subtask implemented only `src/wisdome_writer/portal/housing.py`, `tests/unit/test_portal_housing.py`, and this report update under the approved [design](../superpowers/specs/2026-10-08-topic-portal-design.md) and [plan](../superpowers/plans/2026-10-08-topic-portal.md).

### Implemented contract

`load_housing_snapshot(query: str = "")` returns `available`, `message`, `snapshot_name`, `observed_at`, `notices`, `runs`, `total`, and `matching_total`. The optional query and matching count were added in the review repair recorded below:

- Missing/empty storage: `available=True`, `total=0`, `matching_total=0`, no snapshot/time/rows, explicit no-saved-results message. The reader creates no directories.
- Unsafe, corrupt, excessive or legacy-only storage: `available=False`, `total=None`, `matching_total=None`, no notice cards. Valid schema-v1 runs remain archive links; their unbound metadata is never projected.
- Valid schema-v2 snapshot: `available=True`, `total` is the selected snapshot's notice count, `observed_at` is an aware KST datetime from validated `window.end`. Every card has `title`, `publisher`, `url`, `published_on`, `application_start`, `application_end`, `deadline`, `region`, `category`, `status`. Dates are ISO strings or `None`; observed status is preserved without inferring current applications or approval. URLs use the existing official HTTPS allowlist plus whitespace/control/backslash rejection; disallowed URLs become `None`.
- Snapshot choice uses the greatest validated time window, with the run name only as a deterministic tie-breaker. Notice ordering uses the stored KST publication date and title. Missing end dates stay `None` even if a separate deadline exists. No read uses `acceptance-report.json` or infers live/fixture provenance from validity.
- Run cards expose only `name`, existing `local-article-run` URL, validated final `article_count`, and date. Invalid generations are excluded with a visible caveat if another valid snapshot remains. Incomplete collection metadata receives an explicit caveat.
- Scans stop beyond 512 root entries or 128 candidate runs and return unavailable, rather than guessing which unseen run is latest. Projection accepts up to 1,000 notice rows in addition to inherited strict-JSON resource limits, filters the full selected snapshot before displaying up to 100 matching notices and 12 run links, and retains the full selected-snapshot count. `matching_total` counts every matching notice before display truncation. Search uses trimmed, case-insensitive substring matching on title, publisher or region; an empty query matches the full snapshot.

The loader uses `_article_root`, `_confined_existing`, `_run_manifest`, `_final_articles`, `_official_https_url`, and the existing bundle validation/read machinery. A narrow `_ReadOnlyBundleWriter` subclass replaces only `_safe_root` to remove its `mkdir` side effect. Validation, handle identity checks, image semantics and immutable inventories remain inherited. The returned metadata bytes must additionally hash to the exact schema-v2 manifest captured for projection, closing the old schema-v1 reader qualification gap without modifying historical bundles or the shared writer. Shape checks reject invalid dates, naive/mismatched windows, wrong display-field types and KST conversion overflow.

**Access responsibility:** the caller must enforce the local-and-loopback check before invoking the loader. Route access is implemented and tested by the parent agent, outside this subtask. This loader does not perform collection, networking, humanizer probes, publication, migration or database queries.

### Actual verification

All commands used Python 3.12.10, `WISDOME_ENVIRONMENT=development`, `WISDOME_RUNTIME_MODE=local`, `PYTHONUTF8=1`, and removed inherited `LOCAL_STATE_ROOT`, `LOCAL_OBJECT_ROOT`, `LOCAL_ARTICLE_ROOT`, and obsolete `LOCAL_OUTPUT_ROOT` overrides. Fixtures use disposable pytest directories; existing stored runs were not read or rewritten.

| Check | Actual result |
| --- | --- |
| RED, new projection tests before implementation | 32 failures because the loader/package did not exist; first two full traces inspected, full run confirmed all cases failed |
| Initial GREEN | 32 passed in 4.14 s |
| Additional robustness regression | KST overflow test reproduced an uncaught `OverflowError`; directory-creation denial and invalid-generation fallback checks passed |
| Corrected projection plus existing preview tests | 50 passed in 5.76 s |
| Initial scoped lint | One test-only E501 line-length failure; reformatted the two owned files |
| Final `python -m pytest -q tests/unit/test_portal_housing.py tests/unit/test_local_content_preview.py --tb=short` | 50 passed in 5.81 s (35 projection cases and 15 existing preview cases) |
| Final `python -m ruff check src/wisdome_writer/portal/housing.py tests/unit/test_portal_housing.py` | All checks passed |

No whole suite, port-bound lifecycle test, browser, external network call, collection, migration, live-bundle validation or Git command was executed during implementation. Full integration and direct Chrome acceptance remain owned by the parent verification stage; the passing scoped tests do not establish either. The source baseline above was not re-queried because this delegated subtask explicitly prohibited Git operations.

Remaining limits: the bounded scan intentionally returns unavailable for oversized histories; a later retention/pagination decision may expand that behavior with appropriate integrity tests. Legacy archive order has no trustworthy per-run timestamp beyond its date/name. The loader exposes saved observations, not exhaustive/current national housing availability or personal eligibility. No article-detail identity mapping was added: the UI can use preserved run archive links and official notice links.

- 2026-10-08 implementation update: completed the scoped loader and regression suite, preserved existing product files and immutable bundle formats, and handed the contract/evidence to the parent integration agent.

### Independent review repair: search before display truncation (2026-10-08)

The independent reviewer identified a valid P2 integration issue: the original loader returned at most 100 notices, while the main view applied its search query afterward. A matching notice at position 101 could therefore disappear from search despite being present in the validated snapshot. Source inspection confirmed this order; new fixtures placed a unique match after 100 newer notices for each searched field.

The loader now accepts `query`, chooses and validates the same latest saved snapshot, applies search to all its projected rows, and then enforces the existing 100-row display limit. `total` remains the selected snapshot's overall count; `matching_total` is the filtered count before truncation. No-match search keeps the valid snapshot and overall count, with zero matching rows. The loader rejects non-string or trimmed queries longer than 200 characters with `ValueError` before filesystem access, matching the view's query-length boundary. The parent agent owns forwarding the query from the view and displaying the new count; those files were not edited by this subtask.

| Review-repair check | Actual result |
| --- | --- |
| New search-contract tests against original loader | 12 failed because the loader did not yet accept the query parameter |
| Behavioral RED after introducing only the signature | Three failures (`100 != 1`), one for each field; each fixture separately established that its match was absent from the original 100-row slice |
| GREEN `python -m pytest -q tests/unit/test_portal_housing.py --tb=short` | 47 passed in 4.64 s |
| Scoped Ruff on housing loader and its tests | All checks passed |

The 12 new cases cover the hidden 101st match in title/publisher/region, trimmed case-insensitive search, more than 100 matches, zero matches, empty search, empty/unavailable count semantics, and invalid query rejection before storage access. All prior 35 loader regressions remained green. Execution used the same Python/local/UTF-8 environment and cleared root overrides as above. No ports, full suite, browser, network, collection, migration, Git operation or shared report edit occurred during this repair. The parent verification stage owns the integrated view/template and full-suite rerun.

- 2026-10-08 review repair: moved search into the complete bounded snapshot projection and added explicit matching counts without changing integrity, source provenance or access responsibilities.
