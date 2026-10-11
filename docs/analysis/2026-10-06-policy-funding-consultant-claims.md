# Conversational policy-funding education and consultant claims

## Baseline and intent

- Analysis date: 2026-10-06, Asia/Seoul.
- Source: branch codex/policy-funding, HEAD
  db5edec35aad39ae85e3aca7b6f9e1b9f2e3a0dc plus existing uncommitted funding
  implementation and documentation changes. Earlier review evidence is preserved.
- Starting references: [service analysis](../service-analysis.md),
  [code analysis](../code-analysis.md), [funding progress](2026-10-05-policy-funding-progress.md),
  [design](../superpowers/specs/2026-10-05-policy-funding-design.md) and
  [acceptance](../acceptance/2026-10-05-policy-funding-acceptance.md).
- Service purpose: collect official Korean funding notices and help beginners
  understand loans, guarantees, subsidies, application stages and preparation.
- The user provided additional consultant prose and asked whether the service can
  explain funding in this accessible conversational style. This extends the
  already authorized education feature; it does not create a consultation service.
- One long input passage appears twice verbatim. It is one evidence item, not two
  independent sources. Greetings and consultation advertising are not product facts.
- The text contains no explicit year. Compare its calendar claims with 2026 official
  notices without asserting the original author's year. “Today/tomorrow” refers to
  the original message date, not whichever day the page is later viewed.

## Explanation design and implemented behavior

Use a respectful Korean address, a short question, a plain answer, a clearly
hypothetical example, and a related official link. Keep the essential distinction
first, then explain the next action. Avoid personal approval predictions and a
single rule that purports to cover every program.

The existing typed curriculum now includes six conversational answers covering
confirmation versus approval, target checks, conditional guarantees, revenue
versus money remaining, application-timing opinions and year-end forecasts.
The HTML guide and newly generated Markdown guide both render this content.
The curriculum version is 2026-10-06-v2. Historical immutable bundles remain intact.

A separate dated manual case in consultant_case.py exposes the twelve grouped
claims below through the read-only guide. It does not automatically verify arbitrary
messages, update approval amounts, replace the original October 5 case, or make a
live budget claim. The original strict bundle/case contracts are unchanged.

The intended style is available in /policy-funding/guide/. It is a fixed reviewed
curriculum, not an implemented free-form AI chat or individualized loan recommendation.

## Official-source research and adjudication

On 2026-10-06 the existing bounded, DNS-pinned, robots-aware funding transport
retrieved the first public OLS list page for each loan route and twenty detail
pages. Relevant bodies 404, 409, 410, 411 and 412 were inspected, together with
agency openings 405-407. Raw HTML, list JSON and parsed observations persist in
.local/reviews/policy-funding/2026-10-06/ for local handoff.

The web browsing tool could not open these OLS detail URLs. Direct safe public
fetches succeeded; the findings below depend on those captured issuer bodies,
not search snippets or consultant posts.

| Grouped input claim | Ruling | Evidence and practical meaning |
| --- | --- | --- |
| A confirmation certificate and its amount are not a loan approval | Supported | [OLS 404](https://ols.semas.or.kr/ols/man/SMAN052M/page.do?bltwtrSeq=404&bbsTypeCd=01), caution section: target confirmation does not guarantee a loan; later institution reviews and agreement remain. |
| Every small-business owner obtains confirmation without separate screening | Requires correction | Target eligibility and a bank credit review are different stages. OLS 404 requires checking the program guide; the [official system user guide](https://ols.semas.or.kr/ols/file/%EC%86%8C%EC%83%81%EA%B3%B5%EC%9D%B8%EC%A0%95%EC%B1%85%EC%9E%90%EA%B8%88%EC%82%AC%EC%9D%B4%ED%8A%B8%20%EC%9D%B4%EC%9A%A9%EC%95%88%EB%82%B4%28%EC%86%8C%EC%83%81%EA%B3%B5%EC%9D%B8%EC%9A%A9%29_v1.4.pdf) describes the self-check, required information and submission steps. Do not describe issuance as unconditional or characterize target checks as a bank credit approval. |
| Only a regional-foundation guarantee enables agency disbursement | Conditional | Guarantee review applies to a guarantee-backed route. Existing [official guarantee procedure reference](https://www.koreg.or.kr/haedream/cm/cntnts/cntntsView.do?cntntsId=1085&mi=1133) and the previously reviewed curriculum preserve different credit/collateral routes. No new review of a particular bank contract was performed. |
| October 6 agency application | Supported for identified 2026 programs | [404](https://ols.semas.or.kr/ols/man/SMAN052M/page.do?bltwtrSeq=404&bbsTypeCd=01), [405](https://ols.semas.or.kr/ols/man/SMAN052M/page.do?bltwtrSeq=405&bbsTypeCd=01), [406](https://ols.semas.or.kr/ols/man/SMAN052M/page.do?bltwtrSeq=406&bbsTypeCd=01), [407](https://ols.semas.or.kr/ols/man/SMAN052M/page.do?bltwtrSeq=407&bbsTypeCd=01): October 6 at 10:00, until budget exhaustion. These are four specific Q4 programs. “Last application of the second half” remains unverified. |
| October 12 innovation/credit-vulnerable application | Supported with window and program exceptions | [OLS 409](https://ols.semas.or.kr/ols/man/SMAN052M/page.do?bltwtrSeq=409&bbsTypeCd=01) and [410](https://ols.semas.or.kr/ols/man/SMAN052M/page.do?bltwtrSeq=410&bbsTypeCd=01), application-period bullets: October 12-13, 10:00-18:00 on each day. Policy-priority selection does not guarantee lending. OLS 410 separately describes continuous intake for social-solidarity and graduation-candidate types until budget exhaustion; their working-capital route has no priority evaluation. Facility routes also have evaluation exceptions. |
| October 12 KOSMES direct application | Unverified | No exact 2026 October notice confirming this across KOSMES programs was located. The [official KOSMES process](https://www.kosmes.or.kr/nsh/SH/SBI/SHSBI003M0.do) directs users to application notices. The SEMAS dates do not verify KOSMES dates. |
| October 26 re-challenge/temporary hardship | Supported for identified 2026 direct programs | [OLS 411](https://ols.semas.or.kr/ols/man/SMAN052M/page.do?bltwtrSeq=411&bbsTypeCd=01) and [412](https://ols.semas.or.kr/ols/man/SMAN052M/page.do?bltwtrSeq=412&bbsTypeCd=01), application bullets: October 26 at 10:00 until budget exhaustion. Eligibility remains program-specific. |
| Revenue is not money remaining after business costs | Basic concept | Independently written explanation and hypothetical monthly example. It does not calculate an applicant's profit, tax, cash flow or borrowing capacity. |
| Debt must never exceed annual revenue, e.g. KRW 30 million | Unsupported as a universal rule | Reviewed 409/410 describe evaluations and repayment ability, not a universal 1:1 debt/revenue approval test. Treat the input as a debt-burden caution, not an eligibility threshold. Specific programs or institutions may apply their own ratios. |
| Higher revenue means waiting until late January/early February is preferable | Consultant opinion | No universal application-timing rule was verified. Accepted revenue documents and their availability, funding need, calendar and budget differ. No tax filing deadline or individual recommendation is asserted. |
| Unpopular funds will remain available until December/year-end | Forecast | No remaining-budget or future-supply evidence was obtained. Continuous intake can end at budget exhaustion, as 410 explicitly states. |
| Three employees plus 30 percent of a sales threshold permits graduation-candidate consideration | Detailed current criteria unverified | OLS 410 confirms a graduation-candidate type exists. The precise current attachment criteria, industry-specific small-enterprise revenue ceiling and worker-count calculation were not verified. Historical 2025 examples cannot establish 2026 eligibility. |

The input's named programs must also be reconciled with exact official titles:
social-solidarity appears as a type within the reviewed innovation program.
The input's informal “graduation candidate” name is not by itself a distinct
verified program. This pass does not establish current dates for every other named fund.

## Parser repair

The official 409/410 bodies use an explicit application-period heading; the
collector previously accepted only an explicit receipt-period heading and returned
None. A regression reproduced this on the full two-day, daily-hour window.
The minimal change recognizes both labels while retaining heading anchoring and
stopping before footnotes or the next bullet. It does not infer dates from unrelated
text, compress a daily window into continuous overnight availability, or infer a year.

## Verification and continuation

- RED: the new direct-window regression failed with None instead of the source window.
- GREEN after the heading repair: all ten collector tests passed.
- Focused funding suite: **63 passed in 20.71 seconds**, process exit 0. Command:
  .venv/Scripts/python.exe -m pytest -q followed by all eight
  tests/unit/test_policy_funding_*.py files. This includes parser, transport,
  contracts, storage/workflow, education, review regressions, supplied case and UI.
- Django check: no issues. makemigrations --check --dry-run: no changes.
- The first follow-up Ruff run found three long content lines and one import order
  issue. Formatting was repaired; module and changed-test Ruff now pass.
  Whole-repository lint debt was not rechecked or claimed resolved.
- Live CLI: collect_policy_funding --pages 1 --max-notices 20 --seconds 90
  --as-of 2026-10-06, exit 0. Forty records, 47 requests, no source errors:
  ten agency, ten direct, twenty Bizinfo. Per-source and per-partition budgets
  remain bounded; this is not exhaustive coverage. New immutable run: 2026-10-06.
- read_run validated the persisted run. OLS 409/410 now retain both days and
  10:00-18:00 daily hours; 411/412 retain October 26 at 10:00. The new
  beginner-guide/article.md includes the conversational explanations.
- Actual Brave/Playwright: desktop 1280x960 and mobile 390x844; HTTP 200, six
  answers, 32 glossary entries, twelve dated claims, working expanded answers and
  program exceptions, no horizontal overflow and no page errors. Main visually
  inspected the mobile answer and desktop claim screenshots.
- Local browser/research/live summaries and screenshots:
  .local/reviews/policy-funding/2026-10-06/. This directory is ignored by Git;
  portable findings, official source identities and verification limits are here.
- git diff --check passed. No feature commit or push was performed.
- Historical October 5 acceptance remains 1,425 passed, 3 skipped, 203 subtests.
  That run predates this follow-up and is not evidence of a newly executed full suite.
- Exact KOSMES October notice, graduation criteria attachments, real-time budget,
  individual approval, bank terms and tax-document availability remain verification
  limits, not facts inferred from the supplied prose.
- Implementation uses the existing curriculum/rendering/views, one dated case
  module and a narrow parser repair. No migrations or publisher/runtime interfaces change.
- This bounded follow-up is complete. For a new consultant message, read this
  report first, compare current source and official evidence, then append a dated
  adjudication. Exact program/date/eligibility limits above remain open research
  questions, not silently accepted requirements. Preserve previous immutable bundles.
- Task-owned preview was restarted with current code, session 23948 on loopback
  port 7667. Stop only this owned session before local lifecycle tests bind that port.
  No automatic collection task or separate service was started.

## Risks, priorities and open questions

Priority is understandable and truthful explanation: preserve conditional guarantees,
target checks, date/year context and hypothesis labels. Keep archived case results
visibly dated so they cannot become evergreen application advice. New user prose
requires a new dated review; do not silently mark it current based on keyword matches.

Whether to add an AI question-answer interface or a private consultation workflow
remains an open product decision. Neither was necessary to fulfill this request.
Future source expansion and specific institution eligibility require separate evidence.
