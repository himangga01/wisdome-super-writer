# Policy funding independent feature review

> Main-session status after the scoped review: the two residual variants below were
> reproduced as five failing tests, repaired, and verified in a 31-test review/
> collector run. This preserves the reviewer's stage-specific verdict rather than
> claiming another independent review. Final integrated acceptance is recorded in
> [the acceptance report](../acceptance/2026-10-05-policy-funding-acceptance.md).
> Final main verification: 1,425 passed, 3 skipped, 203 subtests; actual bounded
> live collection and desktop/mobile browser acceptance passed. This is the
> parent's closure evidence, not a third reviewer verdict.

Latest status: see the **2026-10-05 scoped repair re-review** appended below. The
original evidence is preserved; six Important findings and all three Minor items
are addressed, while PF-R2 and PF-R4 retain narrowly reproduced residual defects.

## Verdict and source identity

**Ready to merge: No.** Eight Important findings need adjudication and focused
regressions. No Critical finding was established. The existing focused tests pass,
but additional adversarial probes reproduce misleading financial/region labels,
misbound source evidence, a robots boundary failure, lost closure presentation,
overstated case verification, and malformed-inventory availability failure.

- Review date: 2026-10-05, Asia/Seoul; probes executed at approximately 20:10–20:14 KST.
- Branch: `codex/policy-funding`.
- HEAD/base: `db5edec35aad39ae85e3aca7b6f9e1b9f2e3a0dc`.
- Reviewed release: **uncommitted working tree**, including untracked feature files,
  not HEAD alone. All 35 entries in the supplied
  `.local/reviews/policy-funding/review-source-hashes.json` matched at probe time.
- Initial tree included unrelated modified analysis reports/instructions and the
  historical remediation plan; these were preserved. Feature changes include the
  new module/catalog/fixtures/tests/templates, shared bundle reader, settings/URLs,
  and navigation. No commit, index operation, product edit, publisher operation,
  or further agent delegation was performed by this reviewer.
- Read-before authority: [AGENTS.md](../../AGENTS.md), the shared
  `C:/Users/강지혜/.agents/analysis-policy.md`, the supplied review package,
  [design](../superpowers/specs/2026-10-05-policy-funding-design.md),
  [implementation plan](../superpowers/plans/2026-10-05-policy-funding-implementation.md),
  [progress/rulings](2026-10-05-policy-funding-progress.md), and the national,
  regional, and integration research reports linked there.
- Workflow: Superpowers `requesting-code-review`; this is its independent reviewer
  pass. Parent owns fixes, final acceptance, and adding this canonical review path
  to AGENTS because reviewer writes were restricted to this report and scratch.

## Purpose, architecture, and positive findings

The feature helps Korean business owners find official funding notices and learn
the difference between loans, guarantees, interest support, fees, and grants. Its
dedicated Django module avoids invoking the unrelated distributed publication
pipeline. A 48-entry directory distinguishes discovery sources from three enabled
source configurations: OLS agency/direct lending and Bizinfo finance. The bounded
collectors normalize official HTML/JSON, write immutable schema-v2 bundles, and
expose read-only loopback/staff pages and an explicit collection CLI.

Implemented strengths observed in code and focused checks:

- Confirmation certificates and maximum ceilings are consistently separated from
  personal loan approval; no invented personal rate or approved amount is emitted.
- Direct/agency/grant journeys and conditional guarantee review are separated.
- Source failures remain visible, missing bounded-scan records do not create false
  withdrawals, and prior snapshots remain immutable.
- Live and fixture provenance are checked in the workflow and reader; injected
  collectors require fixture mode through the public workflow.
- HTTP transport reuses DNS pinning, bounded requests/bytes/time, HTTPS and source
  host/path validation. Templates escape source text and set restrictive CSP.
- Directory coverage and actual collection coverage are explicitly distinguished.
- The dated municipal review retains unresolved expansion claims and the Yangsan
  general/youth date distinction. These are historical research conclusions, not
  independent live fact-checks executed in this reviewer pass.

## Important findings

### PF-R1 — Unknown municipal scope becomes nationwide; substring aliases cross regions

**Location:** `src/apps/policy_funding/parsing.py:38`, especially lines 52–54;
`src/apps/policy_funding/views.py:92` (regional filtering).

**Trigger and observed result:**

```python
classify_regions('[양산] 소상공인 육성자금', '양산시')
# ('national',)
classify_regions('[경기] 광주시 특례보증', '경기도 광주시')
# ('gwangju', 'gyeonggi')
```

The first is a municipality with unrecognized parent metadata; the second matches
the substring for Gwangju metropolitan city inside Gyeonggi's Gwangju city. Because
the UI includes `national` in every regional search, the first result advertises a
local notice as nationwide. The second contaminates the Gwangju search.

**Minimal fix:** preserve unknown geographic scope rather than infer national;
require affirmative national evidence; recognize explicit province/municipality
hierarchy and prioritize bracketed province scope over issuer substring matches.
Add regressions for both inputs and mixed national/local notices. Unknown scope
should remain visible with a qualification, not disappear or match every region.

### PF-R2 — A charged or excluded guarantee fee is labeled fee support

**Location:** `src/apps/policy_funding/parsing.py:67`;
`src/apps/policy_funding/contracts.py:18`.

**Trigger and observed result:** both
`classify_kinds('특례보증', '보증료 연 1%, 신청인 부담')` and
`classify_kinds('특례보증', '보증료 지원 제외')` return
`('guarantee', 'guarantee_fee')`. The latter kind is displayed as `보증료 지원`.
Merely discussing a cost therefore produces a positive subsidy claim and matches
the support filter, including when the official text explicitly excludes support.

**Minimal fix:** separate the concept of a fee from affirmative fee-support evidence.
Classify support only from a positive, scoped support statement and preserve
negation/exclusion. General substring classification should not establish a grant
of benefits. Test ordinary fees, exclusions, and genuine conditional support.

### PF-R3 — Redirected robots-excluded paths are fetched before rejection

**Location:** `src/apps/policy_funding/transport.py:61`, `:87`, `:106`.

**Reproduction:** mocked official robots returns
`Disallow: /ols/man/forbidden`; an allowed OLS page redirects to that path.
The actual request log is:

```text
/robots.txt
/ols/man/SMAN051M/page.do
/ols/man/forbidden
```

Only after fetching the third response does the fetcher raise
`Official robots policy excludes this route`. The shared redirect loop invokes
`url_validator` before each hop, but that callback currently checks source URL
rules only. Checking the final response cannot undo the prohibited request.
Likewise the initial robots check precedes merging GET query parameters.

**Minimal fix:** pass a validator that checks both source policy and loaded robots
rules before every physical request/redirect, using the fully parameterized URL.
Keep robots bootstrap explicit. Assert that the excluded destination never appears
in the mock request log. Cross-host redirects remain blocked for current active
single-host sources; this finding does not claim a private-network bypass.

### PF-R4 — Detail redirects can bind another notice's content to the requested ID

**Location:** `src/apps/policy_funding/collectors.py:102` and subsequent parser calls.

`RetrievedPage.url` retains the validated final logical URL, but the collector
discards it. It passes the original list link to the parser, which checks title
equality but cannot compare the fetched notice ID. The diagnostic fetcher returns
an official OLS final URL with `bltwtrSeq=999` and an otherwise matching detail.
The outcome is `completed`, recorded as ID 407 with the original 407 URL.

Government redirects, corrections, or duplicate titles can therefore attach wrong
source bytes to an asserted official identity. Host/path allowlisting alone does
not establish notice identity; this also undermines ID-based case verification.

**Minimal fix:** compare canonical final host/path and required notice query identity
with the requested link before parsing. Permit reviewed route moves only while
preserving identity; classify mismatches as detail failures. Add both OLS and
Bizinfo wrong-ID redirect regressions and a benign canonical redirect case.

### PF-R5 — Ended-list evidence is discarded from every notice presentation

**Location:** `src/apps/policy_funding/rendering.py:7` and `:59`;
`src/templates/policy_funding/detail.html:7`;
`src/templates/policy_funding/index.html:12`.

The collector correctly records `open_list` and `ended_list`. Neither the detail,
index, nor Markdown renderer displays that field. Rendering a notice twice with
only `listing_state` changed from `open_list` to `ended_list` produces byte-identical
detail HTML. Thus an ended notice whose application text says “until budget
exhaustion” appears with the same application guidance as its open-list version.
The general availability disclaimer does not disclose the actual ended evidence.

**Minimal fix:** render a dated source-list-status badge/text in cards, details and
Markdown. Say it was on the official ended list when collected; do not infer the
reason was budget exhaustion or claim real-time closure. Preserve `unknown` and
keep bounded absence distinct from observed ended status. Test all three states.

### PF-R6 — Case verifier accepts the wrong program, region, and event stage

**Location:** `src/apps/policy_funding/verification.py:14`–23.

Starting from the valid ID-404 notice, the probe changes:

```text
title: 2026년 4분기 부산 지역 다른 자금
regions: ('busan',)
application_period: 2026. 10. 6. 설명회 10:00, 대출접수 2026. 11. 2. 09:00
```

The verifier still returns `supported_by_collected_notice` for the national October
6 loan-opening claim. The predicate checks known ID/year, a generic Q4 title
substring, and separate date/time occurrences; it does not bind the expected fund,
national scope, and application-opening stage. This is a synthetic source-context
regression, not a claim that current official notice 404 contains these facts.

**Minimal fix:** require the reviewed ID-to-program mapping, expected scope/route,
and a single explicit application-opening excerpt binding date and time. If
structure or qualifying language is ambiguous, return unverified/conflicting and
retain the actual excerpt. Test wrong known-ID program/region and briefing versus
application stages, not only unknown IDs and previous-year titles.

### PF-R7 — Array-shaped metadata escapes validation and breaks the run reader

**Location:** `src/apps/policy_funding/storage.py:66`, `:93`, `:109`, `:119`, `:142`.

**Reproduction:** create a schema-v2 bundle using the normal shared writer with
the expected policy keys and internally matching hashes, but set `mode=[]` in
both metadata documents. `list_runs()` raises
`TypeError: unhashable type: 'list'` instead of rejecting/skipping the invalid run.
`mode` reaches set membership before its type is checked. Other scalar fields
similarly assume their types. UI and subsequent collection paths use this reader,
so one malformed otherwise inventory-valid run can prevent access to good runs.

This does **not** show that changing a file without updating the manifest bypasses
integrity checks; that existing tamper check passes. It shows that byte integrity
is not sufficient semantic validation at the declared untrusted-data boundary.

**Minimal fix:** validate all scalar/array/object field types before membership,
date parsing or indexing; normalize expected decoding failures to ValueError.
Validate counts, source IDs, change fields, and nested case-report shape rather
than accepting arbitrary nested content. Add malformed-array/null/object cases
with correctly hashed manifests and assert good-run availability is preserved.

### PF-R8 — Repayment-relief notices are explained as new-loan applications

**Location:** `src/apps/policy_funding/parsing.py:242`;
`src/apps/policy_funding/rendering.py:8`–16, `:25`–38.

The parent browser QA reported a live OLS notice headed `홈플러스 피해 ...
만기연장·상환유예 안내` receiving a direct-loan application explanation. This reviewer
independently reproduced the rendering rule with a direct-loan notice titled
`홈플러스 피해 소상공인 만기연장·상환유예 안내`. Its summary is
`정책기관이 직접 심사하고 빌려주는 직접대출 공고입니다.` and its journey includes
`기관에 신청`, `평가와 심사`, and `약정·지급`. The parser assigns a lending route
from the source, and the renderer treats that route as evidence of a new borrowing
opportunity. A maturity extension or payment deferral is a different purpose.

**Minimal fix:** preserve lending route separately from notice purpose. Use
purpose-aware, conservative summaries for repayment relief, corrections,
announcements and applications. Only show a new-loan journey when the notice
supports that purpose; otherwise explain the actual notice or leave the procedure
to its original text. Add the real relief title plus ordinary application controls
as focused regressions. The live discovery is parent evidence; this reviewer's
independent reproduction was offline.

## Minor findings and follow-up

### PF-M1 — Bizinfo registration date is presented as official publication date

**Location:** `src/apps/policy_funding/parsing.py:166`;
`src/templates/policy_funding/detail.html:4`; `rendering.py:67`.

The supplied real Bizinfo fixture's list date is October 2, while national research
records the linked ministry original as October 1. The UI says `공식 게시일` without
identifying the aggregator registration. Preserve the distinct date meaning or
label it `기업마당 등록일`; do not silently call it the issuing ministry's date.
This is supported by the existing research and fixture, not a new live fetch.

### PF-M2 — Dated case claims lack the designed portable evidence fields

**Location:** `src/apps/policy_funding/verification.py:29`–132.

Most reviewed claims contain only sources and paraphrased explanations; there is
no per-claim issuer, source publication date, exact relevant excerpt/locator, or
attachment hash. Pohang support points at a generic product list although the
research identifies product 5. The dated note prevents a direct freshness claim,
but future reviewers cannot reproduce the designed field-level evidence from the
bundle alone. Add reviewed evidence records with the exact product URLs and scoped
excerpts; retain the Yangsan attachment filename qualifier and hash already in the
research. Do not automatically promote old research to current conditions.

### PF-M3 — CLI region selection was planned but is absent

**Location:** `src/apps/policy_funding/management/commands/collect_policy_funding.py:26`.

The accepted design says source/region CLI filters; only source selection exists.
The UI does provide region filtering. Either add documented post-collection region
selection with honest coverage semantics or explicitly adjudicate this small
design deviation in the plan/handoff. Do not represent it as implemented already.

## Verification and reproducibility

Executed with the existing Python 3.12 environment, development/local mode,
`PYTHONUTF8=1`, and all four inherited local-root override names removed:

```powershell
.venv/Scripts/python.exe -m pytest -q tests/unit/test_policy_funding_contracts.py tests/unit/test_policy_funding_collectors.py tests/unit/test_policy_funding_transport.py tests/unit/test_policy_funding_education.py tests/unit/test_policy_funding_verification.py tests/unit/test_policy_funding_workflow.py
# 34 passed in 1.35s
.venv/Scripts/python.exe .local/reviews/policy-funding/reviewer/probe.py
# exit 0; all eight finding observations above reproduced; source-hash-drift: []
```

The diagnostic script uses local functions, Django template rendering, an HTTPX
mock with mocked public DNS, an injected detail fetcher, and temporary directories
under reviewer scratch. It makes no actual network request. Its expected outputs
are embedded above so the portable review does not depend solely on ignored files.
The temporary malformed bundle is generated through `ArticleBundleWriter`, not by
disabling validation. Probe source is deliberately diagnostic rather than a new
passing regression suite asserting the wrong behavior.

Read the tracked diff and untracked source files, checked Git identity/status,
and verified all review-package hashes. One initial README read used the wrong
fixture filename; the existing `PROVENANCE.md` was then read. One PowerShell/rg
quote expansion failed; numbered direct reads supplied the cited lines. These
inspection errors did not change source or affect the executed probes.

## Coverage limits and declined judgments

**Declined to judge:**

- Fresh live source availability, actual bank/guarantee decisions, future budget
  availability, or unresolved Pohang/Yeongcheon expansion truth. Parent owns the
  concurrent bounded live acceptance; this pass relies on dated supplied research.
- Exhaustive nationwide/municipal collection, every discovery source, attachment
  extraction correctness, legacy HWP admission, or OCR acceptance.
- Browser/mobile visual quality, real staff-session deployment, full repository
  suite, migration drift, whole-repository Ruff, and current external deployment.
  Parent was running those checks; this reviewer avoided duplicate full-suite work.
- Old distributed R1–R16 findings unless invoked by this module; no such invocation
  requiring renewed adjudication was established in this pass.
- Legal permission/licensing adjudication or an SSRF exploit through references.
  The progress ledger explicitly permits HTTPS `.go.kr` reference links; that
  accepted ruling is not treated as a new crawler-host expansion finding.
- High-volume or concurrent-run performance/retention guarantees. The implementation
  bounds projection to recent runs, but this pass did not benchmark 100-run history,
  simultaneous publication, or all same-date run-order edge cases.

Open product choices remain the freshness policy for historical open/ended list
observations, how unknown municipal scope should appear in filters, and whether
CLI region selection remains a release requirement. None justifies silently
presenting unknown scope or charged fees as positive eligibility/support facts.

## Required next step and update log

Parent should adjudicate PF-R1–R8, implement accepted minimal fixes with focused
regressions, refresh the source baseline/hashes, and record verification in the
canonical progress report before changing the verdict. No fix or passing acceptance
is implied by this report. Preserve this original finding evidence during updates.

- 2026-10-05: Independent review completed; 34 focused tests passed; eight additional
  fault probes reproduced; no Critical findings, eight Important and three Minor
  findings. Merge verdict: not ready pending material fixes and parent acceptance.

## 2026-10-05 scoped repair re-review

**Scoped verdict: not ready yet; two residual Important defects.** All eight
original diagnostic examples now produce the corrected behavior. Two direct
variants of the original PF-R2 and PF-R4 boundaries still fail, as detailed below.
This is the requested single repair check, not a new general review wave.

Source identity remains branch `codex/policy-funding`, HEAD `db5edec35aad39ae85e3aca7b6f9e1b9f2e3a0dc`,
with uncommitted feature changes. At approximately 20:40 KST all **37** paths in
`.local/reviews/policy-funding/fix-source-hashes.json` matched exactly. Original
review-package hashes remain historical; the old probe reports their expected
differences, followed by `fix-hash-drift: []` for this repair baseline.

### Disposition of existing findings

| Finding | Scoped disposition | Evidence |
| --- | --- | --- |
| PF-R1 | Addressed for reviewed cases | Yangsan maps to Gyeongnam; explicit Gyeonggi excludes metropolitan Gwangju; unknown scope remains unspecified; new tests pass. |
| PF-R2 | Original examples fixed; residual remains | Charged, excluded, and tested negative fee phrases no longer imply support; two ordinary explicit denials still do. |
| PF-R3 | Addressed | Before-hop validator checks robots and merged parameters; original mock logs only robots and allowed route, never the excluded destination. |
| PF-R4 | Original wrong-number example fixed; residual remains | Changed `bltwtrSeq` fails; changed OLS board parameter is still accepted. |
| PF-R5 | Addressed | Source-list status is displayed in cards, details, and Markdown; open/ended detail HTML now differs with clear dated wording. |
| PF-R6 | Addressed for reviewed cases | Exact known program title, national region, agency kind, and anchored opening text reject the wrong-program and briefing-stage probes; valid fixture/live controls pass. |
| PF-R7 | Addressed for reviewed cases | Scalar types, nested case data, count bounds and sources are validated; valid-hash array-mode run is skipped while good runs remain available. |
| PF-R8 | Addressed for reviewed cases | Repayment-relief summary concerns existing loans and has no new-loan journey; application controls still pass. |
| PF-M1 | Addressed | Source-specific registration labels replace unqualified official publication labels in templates and Markdown. |
| PF-M2 | Addressed | Portable per-claim evidence includes issuer, date/unknown date, locator, excerpt, scope, product-5 URL, Yangsan attachment hash and filename qualifier. These remain dated manual research. |
| PF-M3 | Addressed | CLI `--region` performs documented post-collection selection, records selection metadata, includes national-institution notices for regional choices; CLI regression passes. |
| Parent-discovered historical link issue | Addressed | Explicit run selection reads the selected immutable snapshot rather than current terms; historical-detail regression passes. |

### Residual PF-R2 — Additional explicit denials still become fee support

**Location at repair baseline:** `src/apps/policy_funding/parsing.py:105`–110.

```python
classify_kinds('특례보증', '보증료 지원 불가')
# ('guarantee', 'guarantee_fee')
classify_kinds('특례보증', '보증료 지원 대상이 아닙니다')
# ('guarantee', 'guarantee_fee')
```

The newly introduced positive regex matches these phrases, while the exclusion
regex covers neither `불가` nor `아닙니다`. Thus the same substantive defect remains:
an explicit denial displays as `보증료 지원`. This is not a new funding classifier
feature request; it is the original false-positive support finding.

**Required minimal completion:** conservatively reject explicit negative support
predicates including the two reproduced forms; retain a positive support control.
If sentence scope is ambiguous, omit the affirmative support tag rather than infer
a benefit. No automated phrase classifier is being required to infer eligibility.

### Residual PF-R4 — OLS board identity is not checked after redirect

**Location at repair baseline:** `src/apps/policy_funding/collectors.py:106`–110;
notice URL contract in `src/apps/policy_funding/contracts.py:58`–70.

An injected detail fetcher returns:

```text
requested: .../SMAN052M/page.do?bltwtrSeq=407&bbsTypeCd=01
received:  .../SMAN052M/page.do?bltwtrSeq=407&bbsTypeCd=99
```

with otherwise matching detail text. `collect_source()` returns `completed` and
stores the original board-01 URL. The repair compares only `bltwtrSeq` for OLS;
the researched URL identity also includes `bbsTypeCd`, and the list parser itself
requires board `01`. Board changes can therefore still bind fetched content to a
different official identity despite the repair.

**Required minimal completion:** require the received OLS query to have exactly
one `bbsTypeCd=01` as well as the matching notice number; reject missing, changed,
or repeated board values. Enforce the same invariant when decoding stored notice
URLs. Preserve benign irrelevant query parameters only if they do not alter these
required identity fields. Add the changed-board regression beside wrong-number
and ordinary-success controls. This probe does not assert that board 99 exists on
the live service; it establishes the missing boundary when official redirects or
board routing change.

### Executed scoped verification and limits

```powershell
.venv/Scripts/python.exe -m pytest -q tests/unit/test_policy_funding_review_regressions.py tests/unit/test_policy_funding_transport.py tests/unit/test_policy_funding_verification.py tests/unit/test_policy_funding_ui.py tests/unit/test_policy_funding_workflow.py -k 'not nonlocal_policy_ui_requires_active_staff'
# 36 passed, 1 deselected in 5.95s
.venv/Scripts/python.exe .local/reviews/policy-funding/reviewer/scoped_probe.py
# exit 0; original failures corrected; two residual observations above reproduced
# fix-hash-count: 37; fix-hash-drift: []
```

Used development/local, Python UTF-8, and removed inherited local-root overrides.
The staff database test was deliberately deselected to avoid concurrent database
work while the parent full suite ran. Mock/fixture probes made no live requests.
Re-read repaired source, templates, CLI/history tests, portable evidence, and the
progress report's repair record. No product/index/branch modifications were made.

Parent-reported 246 combined passing checks and browser/live acceptance are not
recast as this reviewer's executed evidence. All original declined judgments still
apply. Once the two residual cases are repaired and their focused regressions
pass, the original material review findings have concrete closure criteria; parent
still owns full-suite/live/browser results and final feature readiness.
