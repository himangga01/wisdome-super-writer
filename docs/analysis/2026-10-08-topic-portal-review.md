# Independent three-topic portal review

## Review baseline and scope

- Date: 2026-10-08, Asia/Seoul.
- Branch: `codex/policy-funding`.
- HEAD: `db5edec35aad39ae85e3aca7b6f9e1b9f2e3a0dc` plus the uncommitted portal implementation and its pre-existing funding dependency. Findings refer to the inspected working-tree bytes, not committed HEAD alone.
- Initial working tree contained modified instructions, README, canonical reports, local-content bundle reader/templates, console base, settings and root URLs; funding and portal source/config/templates/static/tests and their reports were untracked. Unrelated changes were preserved.
- Purpose: give the existing operator/development service separate housing, semiconductor and policy-funding workspaces using the supplied black/white/lime reference screenshots and mobile navigation.
- Reviewed new portal loaders, views, routes, navigation, shared templates/CSS; changed root/settings integration; funding base/index/glossary/history/views; local-article template integration.
- Starting authorities: [AGENTS.md](../../AGENTS.md), shared `C:/Users/강지혜/.agents/analysis-policy.md`, [progress checkpoint](2026-10-08-topic-portal-progress.md), [design](../superpowers/specs/2026-10-08-topic-portal-design.md), [plan](../superpowers/plans/2026-10-08-topic-portal.md), [housing reconnaissance](2026-10-08-portal-housing-recon.md), [semiconductor reconnaissance](2026-10-08-portal-semiconductor-recon.md), and the earlier documentation review's continuation steps/remediation Task 8. Older unrelated R1–R16 findings were not repeated.
- The reviewer owns only this report. The parent implementation agent owns registration in AGENTS.md and updates to shared reports, avoiding concurrent edits.

Pre-repair source fingerprints (SHA-256):

| File | Digest |
| --- | --- |
| `src/wisdome_writer/portal/views.py` | `c9f8d3e2a794c744d966722dafe1156fb3d6f2b922f24a19ce302d96ebc4af9f` |
| `src/wisdome_writer/portal/housing.py` | `d6dfba8ff879b848ce281a6506e714ac2dd15ba0b0c677565176b877de911c37` |
| `src/templates/portal/housing.html` | `5e3ce4563396f7f0c2f29076195edb6770fba39fe768869e4b64dfdf4192b943` |

## Implemented structure and verified boundaries

The shared Django shell derives topic menus from request paths. Portal/funding/semiconductor views use the existing loopback-local or active-staff-nonlocal guard. Housing calls its stricter local-and-loopback guard before projection. Housing projects one validated immutable run snapshot; semiconductor selects topic-scoped stored source versions/current drafts and presents database unavailability separately from empty results. Funding keeps its independent storage, live/fixture mode and education contracts.

Source review and scoped tests support these narrower conclusions:

- Housing rejection precedes filesystem projection, including staff requests in nonlocal mode; semiconductor rejection precedes its data loader. No read performs collection, publication or a humanizer probe.
- Housing checks schema-v2 metadata binding, validates immutable run material, retains legacy archive links without trusting unbound metadata, selects by the saved time window and preserves unknown application dates. Invalid snapshots do not become trusted notice cards; no root directory creation is requested by the projection.
- Semiconductor queries and counts are scoped to `semiconductor_news`; tombstones are excluded and revision ownership is checked before displaying current draft text. Missing tables produce unavailable metrics rather than zero. Returned fields omit source secrets, raw configuration and article bodies.
- Templates use escaped text; housing links pass the official HTTPS policy and semiconductor links must match the configured HTTPS host. Existing local-article rendered Markdown continues through its existing safe renderer.
- The responsive source contains viewport metadata, a skip link, native mobile details/summary navigation, labelled search controls, visible focus styles and reduced-motion handling. These are source-level observations only; no rendered mobile layout, target-size, color-contrast or keyboard interaction acceptance was executed.

## Findings requiring correction

Parent disposition (2026-10-08): both findings below were accepted and repaired.
TP-R1 now filters the selected snapshot before limiting visible rows and exposes
matching_total separately from total. New 101st-record title/publisher/region
regressions passed. TP-R2 has failing-then-passing overview/list HTML checks and
now displays escaped saved source status. Original findings/fingerprints remain
as historical review evidence; this parent update is not another reviewer verdict.

### P2 / TP-R1 — Apply housing search before the display limit

**Location:** `src/wisdome_writer/portal/views.py:37–46`, coupled to `src/wisdome_writer/portal/housing.py:132`.

The loader returns only `notices[:100]`, then the view applies the user's title/publisher/region query to that truncated list. A valid saved notice outside the first 100 therefore cannot be found, even by its exact unique title. The page states that the search has no matching notices although the selected snapshot contains the record. The visible maximum is suitable for rendering; it should be applied after the search over the bounded validated snapshot.

**Executed reproduction:** create a schema-v2 run through the existing test fixture helper with 100 records titled `ZZZ notice 0` through `ZZZ notice 99` and one record titled `AAA unique-needle`, all with the same publication timestamp. Request the housing notices view in process with `q=unique-needle`. Observed:

```text
snapshot_total=101
projected_rows=100
known_match_visible=False
no_matches_message=True
```

The request used `RequestFactory`, `override_settings` with a temporary housing root and `StaticFilesStorage`, and ordinary Django template rendering. No HTTP connection or browser was used. The temporary bundle was removed by `TemporaryDirectory` on completion.

**Correction:** filter the complete bounded validated snapshot before slicing to the display limit, preserving selected-snapshot total separately from matching total if both are displayed. Add a regression for a matching notice beyond the first 100. If an intentionally limited search is retained, both the search scope and no-results text must explicitly state that limitation; full-snapshot search is the preferable current design.

### P2 / TP-R2 — Display the saved housing notice status

**Location:** `src/templates/portal/housing.html:12`.

The projection returns `row.status`, but the shared housing record row never displays it. LH ingestion explicitly captures the notice table's status cell (`src/apps/local_content/sources/lh.py:343`), so known source wording such as an ended or changed notice state disappears while its title and dates remain visible. A generic warning that saved records can be stale does not preserve the observed status required by the design.

**Executed reproduction:** the same schema-v2 fixture contained `status="접수마감-검토표식"`. `load_housing_snapshot()` preserved that value, but rendering `/housing/notices/` omitted it entirely:

```text
loader_preserves_observed_status='접수마감-검토표식'
observed_status_visible=False
```

**Correction:** display the escaped saved source status beside each housing notice with explicit saved-observation wording. Keep a neutral fallback for the producer's generic `published` value; do not infer currently open applications from it. Verify both the overview and notice-list branch, including HTML escaping of source text.

No P1 defect was established in this scoped review. This statement does not claim exhaustive security or operational acceptance.

## Rejected hypotheses and remaining limits

| Hypothesis | Disposition and evidence |
| --- | --- |
| A nonlocal staff request can read housing files through the new portal | Rejected for inspected routes: `_require_local_request` is the first action; scoped guard test passed. |
| Portal home/navigation reads each topic's storage before authorization | Rejected: navigation is display-only and home does not call topic loaders. |
| Legacy housing `notices.json` becomes trusted merely because a run exists | Rejected: loader requires schema 2 plus exact bound metadata inventory; legacy/corrupt tests passed. The older shared helper's schema-v1 behavior is not used without this guard here. |
| Same-day random suffix determines the chosen housing snapshot | Rejected: validated `window.end` controls selection; suffix is a tie-breaker. |
| The semiconductor fallback disguises unavailable storage as zero news | Rejected: `available=False`, `total=None`, labelled configuration fallback; early and late database-failure tests passed. |
| Semiconductor drafts disclose a cross-linked housing revision | Rejected for the current projection: `current_revision__article_id=F("id")` is enforced and the adversarial ownership test passed. |
| Source rights are silently treated as new per-record approval | No separate defect established: the implementation uses the current source default, retains restrictive labels and describes directory entries as configuration. It does not establish historical item-specific clearance. |
| Successful template rendering proves Chrome/mobile acceptance | Rejected: no browser was available or used. Responsive geometry, overflow, actual link interaction, focus order and screenshot matching remain unexecuted. |

The semiconductor list is bounded to 60 records and 30 drafts. A future pagination or limit-label decision would improve navigation, but no additional P1/P2 correctness defect was established from that bound in this pass. The service remains an operator/development experience; no change to publishing readiness, live collection coverage or unrestricted public access was accepted.

## Verification performed

Environment: existing Python 3.12 virtual environment, `WISDOME_ENVIRONMENT=development`, `WISDOME_RUNTIME_MODE=local`, `PYTHONUTF8=1`; inherited `LOCAL_STATE_ROOT`, `LOCAL_OBJECT_ROOT`, `LOCAL_ARTICLE_ROOT`, and obsolete `LOCAL_OUTPUT_ROOT` overrides were removed for the scoped test subprocess.

```text
.venv/Scripts/python.exe -m pytest -q tests/unit/test_portal_housing.py tests/unit/test_portal_semiconductor.py tests/unit/test_topic_portal.py
63 passed in 27.00s
```

Additionally executed the two temporary-bundle/in-process rendering probes described above; both reproduced the reported defects. Read-only Git/source/Markdown inspections established current baseline and references. An initial plan lookup used a nonexistent `...topic-portal-implementation.md` filename; it was corrected to `...topic-portal.md`. Source searches naming nonexistent collector directories were corrected to `src/apps/local_content/sources/`.

No product code, shared tests, existing data, migrations, shared reports or Git state was modified by this reviewer. No full suite, port-binding test, external collection, network request, browser navigation, Chrome/CDP/Playwright operation, deployment or reference-site inspection occurred. The saved browser denial was respected; in-process server rendering is not a workaround for browser acceptance and does not satisfy that deliverable.

## Priorities and handoff

1. Correct TP-R1 and TP-R2; add targeted regressions and re-run the affected portal tests.
2. Parent agent should register this report in AGENTS.md and append disposition/reverification evidence here after changes, preserving the original reproduced evidence.
3. Keep Chrome desktop/mobile acceptance explicitly open until a sanctioned browser state permits it. Do not bypass the saved denial.

Open product question: whether later housing search should include historical snapshots. The current approved single-snapshot model is coherent; TP-R1 concerns searching the selected snapshot and does not require a history merge.

Update log: 2026-10-08 — initial independent working-tree review; two P2 defects reproduced, 63 scoped tests passed, browser acceptance remains unavailable. Parent implementer accepted both findings pending their own reproduction, correction and regression verification; this initial report does not claim the repairs have been verified.
