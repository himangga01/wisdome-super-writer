# Policy funding feature acceptance — 2026-10-05

## Release identity and scope

- Date/time zone: 2026-10-05, Asia/Seoul.
- Source baseline: db5edec35aad39ae85e3aca7b6f9e1b9f2e3a0dc plus the current
  policy-funding working changes on codex/policy-funding. No feature commit/push.
- Existing October 5 review/report changes were preserved.
- Request: research official Korean central/local funding sources, collect notices,
  provide elementary-level Korean terminology/procedures and use the supplied
  October 6 example for verification; persist all engineering work in English MD.
- Implemented scope: dedicated Django policy_funding module, three live collector
  configurations, 48-entry official directory, 32 terms, three learning journeys,
  notice-purpose explanations, immutable runs/change history, CLI and read-only UI.
- Authorities: [design](../superpowers/specs/2026-10-05-policy-funding-design.md),
  [plan](../superpowers/plans/2026-10-05-policy-funding-implementation.md),
  [restart ledger](../analysis/2026-10-05-policy-funding-progress.md),
  [independent review](../analysis/2026-10-05-policy-funding-review.md).

## Implemented user behavior

The service shows official funding notices with source registration dates, collection
times, original links, application-period wording and observed list status. Unknown
geography remains unknown; explicit province scope takes precedence over same-named
municipalities. Fee costs/exclusions do not become fee support. Repayment relief and
amendments do not receive a new-loan application journey.

The guide distinguishes direct loans, agency loans and grants, conditional guarantee
review, confirmation versus approval, ceilings versus awards, principal/interest,
interest support, guarantee fees, grace/maturity and repayment methods. Examples are
labeled hypothetical. Personal eligibility, approved amount and actual rate are not
invented. Preparatory checklists and central/regional source comparisons are included.

Live and fixture data have separate provenance. The shared schema-v2 bundle writer
stores immutable Markdown and closed policy metadata. Repeated observations compare
semantic values; a missing item in a bounded scan is not inferred closed. Explicit
historical-run links read that run's notice snapshot, not a later revision.

## Actual live collection

Executed twice with the final collector logic:

```powershell
.venv/Scripts/python.exe src/manage.py collect_policy_funding --pages 1 --max-notices 4 --seconds 90 --as-of 2026-10-05
```

Environment: development/local, Python 3.12.10, PYTHONUTF8=1. Requests use existing
DNS-pinned HTTPS, source host/path checks, robots checks before redirects, strict
decoding, bounded bytes/time/count and final notice-ID/board identity validation.
OLS public search is a read-only POST returning JSON/HTTP 201, not a loan application.

| Configuration | Captured notices | Requests per final run | Outcome |
| --- | --- | --- | --- |
| OLS agency loans | 4 | 6 | Completed within selected bounds; no errors |
| OLS direct loans | 4 | 6 | Completed within selected bounds; no errors |
| Bizinfo finance | 4 | 7 | Two open-list and two ended-list notices; no errors |
| Total | 12 | 19 | Every outcome explicitly marks coverage_limited |

Final run names:
- 2026-10-05--run-a0555b8f14f0
- 2026-10-05--run-6b4ba832282a

The later run found all 12 notices unchanged. The first refreshed run had 11 unchanged
and one changed record after classification corrections; that is not asserted to be
an official policy amendment. Both retained eight OLS records with unknown list
status, two Bizinfo open-list records and two ended-list records.

Data is under output/policy-funding, ignored by Git. Each run includes index.md,
notices.json, raw-observations.json, beginner-guide/article.md and per-notice Markdown/
verification metadata with the shared integrity manifests. Logs and compact summary
are under .local/reviews/policy-funding/live-final-*.log and live-final-summary.json.
Those machine artifacts supplement this portable acceptance record.

## Supplied example verification

The user text is preserved in
[the untrusted reference fixture](../../tests/fixtures/policy_funding/user_reference_2026_10_06.txt).
It is not a source authority and the repeated “tomorrow” refers to the supplied
2026-10-05 context. The UI labels this as a dated case study, not current approval.

| Claim | Evidence and result |
| --- | --- |
| National Q4 agency opening, October 6 at 10:00 | Supported by original OLS 404–407 and matching collected program/date/scope. [Original 404](https://ols.semas.or.kr/ols/man/SMAN052M/page.do?bltwtrSeq=404&bbsTypeCd=01). No inference of remaining budget or every other fund's opening. |
| Certificate/maximum amount is not approval | Preserved in the primary notice, guide, explanations and tests. Guarantee/lender review and disbursement remain separate. |
| Yangsan general Q4 opening | Official attachment says October 6 at 09:00. [Official Bizinfo record](https://www.bizinfo.go.kr/sii/siia/selectSIIA200Detail.do?pblancId=PBLN_000000000126815). |
| Yangsan youth opening | The attachment's separate youth section says February 4 at 09:00; do not add an October 6 opening to that program. |
| Yangsan amounts/subsidy durations | Dated manual review supports the stated ceilings with business-age, repayment and fee-support conditions. Up to four years is not four years for every repayment option. |
| Pohang expansion to KRW150bn | Unverified against a retrieved primary amendment; the retrieved product 5 table still showed KRW120bn. The older table does not disprove a later expansion. |
| Yeongcheon expansion to approximately KRW12.8bn | Unverified against a retrieved primary amendment; retrieved product 12 showed KRW11.76bn. Arithmetic resemblance does not verify an expansion. |

Portable evidence records include exact issuer/product URLs, dates or explicit
unknown issue dates, scoped excerpts/locators and the Yangsan HWPX SHA-256
5e7b9902d109f71ab5aa90bb18388c734bf0b921df0ed0ac757fa6667141958c.
The attachment filename includes (안); the qualifier and municipal notice number
are retained. This was manual table review, not general attachment-parser acceptance.

## Browser and verification evidence

- Actual installed Brave through Playwright, headless fresh context: 1280×960 desktop
  and 390×844 mobile. Index, search, detail, 32-term expandable guide, 48-source
  directory, ended-status badge and repayment-relief wording passed. No page errors
  or mobile horizontal overflow. Source links were inspected, not used to apply.
- Screenshots: .local/reviews/policy-funding/browser/{desktop-index,desktop-detail,
  mobile-index,mobile-cards}.png. Parent visually inspected desktop/mobile screenshots.
- Django client covers loopback access, remote denial, staff access, write rejection,
  search, fixture separation, region CLI selection and historical snapshot detail.
- Static asset collection passed: 139 initially copied; updated stylesheet rebuilt.
- Focused integration before final residual cases: 246 passed in 39.21 seconds.
- Independent review found eight Important issues and three small plan gaps. Main
  reproduced and repaired them. Scoped re-review confirmed original cases addressed,
  then found two residual negation/board variants. Five new failures reproduced those
  variants; final main checks for review/collector scope: 31 passed in 0.37 seconds.
- Final full unit/integration suite: exit 0; **1,425 passed, 3 skipped,
  203 subtests passed in 515.01 seconds**, with QA listeners stopped.
- A prior full run had 1,424 passed, 1 timeout, 3 skipped and 203 subtests in
  524.65 s because main's QA server and the lifecycle test both used port 7667.
  Main stopped its server; the isolated failed test passed in 3.49 s. The clean
  final run keeps ports 3210/7667 free. No product timeout or guard was weakened.
- Current AST: 361 Python files parse (269 source / 85 tests / 7 scripts).
- New policy modules/tests Ruff passes. Whole Ruff still reports the same 1,561
  pre-existing findings; this is not a repository-wide lint pass.
- Django system check and migration drift: passed before final documentation; no
  database model or migration was introduced by this feature.

Initial failures and corrections are retained in the restart ledger: missing-feature
RED tests, test static-storage setup, PowerShell UTF-8 catalog labels, wrong loan-route
context, test URL-cache pollution and the review reproductions. Interrupted suite
output is not counted as a pass. No new broad review round followed the scoped review;
the final residual variants were closed by main-session regressions.

## Coverage limits and remaining product choices

- 48 catalog entries and 17 requested regional buckets do not mean 48 active crawlers
  or exhaustive municipality coverage. Three source configurations are active; Bizinfo
  aggregates finance notices nationally. Other source entries are discovery links.
- Open/ended list observations are dated, not real-time budget/acceptance guarantees.
  Projection is bounded to recent runs, and incomplete sources may leave earlier data.
- Regional government branding/routes can change. The requested region buckets are
  not a legal assertion of the current administrative-unit count or eligibility.
- Authenticated APIs, unsupported sites/attachments, general HWP/OCR extraction,
  every municipal amendment, bank/guarantee approval and future funding availability
  were not accepted. Existing HWP activation stays closed.
- No loan applications, external publisher writes, production deployment, merge or
  push. The old distributed R1–R16 findings remain separate and unresolved.
- No large-history/concurrency benchmark or complete licensing adjudication. Public
  access/robots allowance is not a blanket redistribution license. The feature uses
  source-linked factual material and independently written educational explanations.

Next source-expansion choices: additional direct regional adapters, allowed API keys,
attachment extraction with exact table scope, larger backfill/refresh policies and
distributed publication integration after that pipeline's existing release blockers.
