# National policy-funding source and education research

## Baseline and scope

- Analysis date: 2026-10-05, Asia/Seoul.
- Source baseline: `main` at `db5edec35aad39ae85e3aca7b6f9e1b9f2e3a0dc`.
- Initial working tree: existing changes in `AGENTS.md`, the service/code reports,
  the October 3 adjudication and remediation plan, plus the untracked October 5
  documentation/code review. These were preserved.
- Read first: project `AGENTS.md`, the shared
  `C:/Users/강지혜/.agents/analysis-policy.md`, the service/code baselines, the
  October 5 review and relevant collection/acceptance references in the task ledger.
- Assignment: official national funding sources, feasible public collection
  routes, the claimed October 6 application opening, and beginner education facts.
  Regional source completeness is a separate research assignment.
- This report records live research and proposed collection behavior. It does not
  establish implemented product behavior, a deployed collector, approved source
  rights, application acceptance, financing eligibility or a funding award.
- The parent implementation session owns product changes, canonical-report
  integration and adding this report's path to `AGENTS.md`. This subtask changes
  only this report and ignored research scratch files.

The existing service collects housing notices and semiconductor material through
an evidence-preserving Django pipeline and a separate local writing path. A funding
feature should reuse source identity, immutable evidence, factual review and local
preview boundaries. Its beginner explanations need their own reviewed references;
an aggregated announcement is not a personalized financing decision.

## Main findings

1. The **2026-10-06 10:00** opening is verified in four original SEMAS agency-loan
   notices, not merely a search snippet or municipal repost. It applies to the
   identified Q4 funds and does not establish every policy fund's opening date.
2. The strongest immediately accessible collection candidates are MSS's public
   RSS plus HTML, Bizinfo's public HTML, and SEMAS OLS's public notice-search
   request plus HTML details. The OLS search uses POST for reading; it is not an
   application submission.
3. Bizinfo's open and ended lists are separate. Fetching only its default list
   cannot observe all closures. Its national coverage aggregates multiple
   publishers and needs original-source links and duplicate relationships.
4. KODIT's robots file excludes notice list/detail routes. KOREG's robots file
   excludes its guarantee-product finder. Keep these routes out of automatic
   collection unless an allowed alternative or explicit source permission exists.
5. Official APIs exist, but their documented key requirements were not bypassed.
   API documentation verification is not authenticated API execution.

## Verified opening-date evidence

The read-only OLS search returned these original issuer notices, all registered
2026-09-11. Each detail page returned HTTP 200 through an ordinary unauthenticated
HTTP client, and each body's `#cntnDiv` gives the same October 6 opening:

| OLS ID | Fund | Original issuer URL |
| --- | --- | --- |
| 404 | General management stability, agency loan | [SEMAS original 404](https://ols.semas.or.kr/ols/man/SMAN052M/page.do?bltwtrSeq=404&bbsTypeCd=01) |
| 405 | Emergency management stability, temporary difficulty, agency loan | [SEMAS original 405](https://ols.semas.or.kr/ols/man/SMAN052M/page.do?bltwtrSeq=405&bbsTypeCd=01) |
| 406 | Disabled business support, agency loan | [SEMAS original 406](https://ols.semas.or.kr/ols/man/SMAN052M/page.do?bltwtrSeq=406&bbsTypeCd=01) |
| 407 | Youth employment linked funding, agency loan | [SEMAS original 407](https://ols.semas.or.kr/ols/man/SMAN052M/page.do?bltwtrSeq=407&bbsTypeCd=01) |

The issuer states October 6, 2026, Tuesday, 10:00, until the budget is exhausted,
with possible early closure. As of this report date this is a **scheduled start**,
not evidence of successful application or available balance on October 6.
The HTML does not explicitly print a timezone; interpreting Korean domestic
application times as Asia/Seoul is a documented product assumption.

Notice 404 also says that the eligibility confirmation does not guarantee a loan;
guarantee/lender review and agreement follow; funding is within the annual fund
budget and follows loan-execution order. Its confirmation validity date is
2026-12-23. **That validity date must not become the application closing date.**
Neither a known opening time nor a confirmation reserves funding.

The [Geumcheon government repost](https://www.geumcheon.go.kr/portal/selectBbsNttView.do?bbsNo=150682&key=4099&nttNo=266639)
corroborates the date and time, but is an official repost, not the issuer.
It should link to the SEMAS originals instead of becoming a second independent
confirmation of borrower eligibility.

## National source inventory

| Publisher / authority | Verified collection or reference route | Coverage and limitations |
| --- | --- | --- |
| MSS, responsible ministry | [Business-announcement RSS](https://mss.go.kr/rss/smba/board/310.do), [HTML list](https://www.mss.go.kr/site/smba/ex/bbs/List.do?cbIdx=310), [RSS documentation](https://www.mss.go.kr/site/smba/contents/view.do?menuCd=20905000000002024122902&siteCd=smba) | Original ministerial plans, amendments and programs. Public, no key. The sampled RSS contains 20 recent records, not a full history or a universal national register. |
| SEMAS / OLS, small-business policy-fund operator | [Agency notices](https://ols.semas.or.kr/ols/man/SMAN056M/page.do), [direct notices](https://ols.semas.or.kr/ols/man/SMAN055M/page.do), [fund overview](https://ols.semas.or.kr/ols/man/SMAN018M/page.do) | Operational direct/agency schedules and guidance. Public reading succeeded without login. Corporate [SEMAS](https://www.semas.or.kr/) did not provide a usable body through browsing and a later direct connection failed. OLS success does not verify every SEMAS grant program. |
| KOSMES / KOSME, SME policy-fund operator | [Notices](https://www.kosmes.or.kr/nsh/SH/NTS/SHNTS001M0.do), [fund overview](https://www.kosmes.or.kr/nsh/SH/SBI/SHSBI001M0.do), [regional application schedules](https://www.kosmes.or.kr/nsh/SH/SBI/SHSBI099M0.do) | Different institution/program population from SEMAS. Notices use an HTML shell and JSON requests. The page exposes `/sh/nts/notice_list.json`; a read-only POST timed out. Regional schedule content was identified in navigation but not collected or accepted. |
| KODIT, credit-guarantee provider | [Notice list](https://www.kodit.or.kr/kodit/na/ntt/selectNttList.do?bbsId=148&mi=2638), [guarantee overview](https://www.kodit.or.kr/kodit/cm/cntnts/cntntsView.do?cntntsId=11064&mi=2521) | Official guarantee programs and explanatory references. `.co.kr` redirects to `.or.kr`. Readable notice HTML exists, but robots excludes its list/detail routes; automatic notice collection should remain disabled. No suitable public announcement API was verified. |
| KIBO, technology-guarantee provider | [Notice list](https://www.kibo.or.kr/main/board/boardType01.do), [sample original notice](https://www.kibo.or.kr/main/board/boardType01.do?articleNo=66253&mode=view) | Public paginated HTML with `articleNo` identity; sampled list includes guarantees, amended programs, recruitment and other non-funding notices. No public funding API or RSS was verified. Its robots rules specifically name Googlebot; check the actual crawler identity and source rights before activation. |
| KOREG, federation of regional credit-guarantee foundations | [Product news](https://www.koreg.or.kr/haedream/na/ntt/selectNttList.do?bbsId=1069&mi=1139), [product finder](https://www.koreg.or.kr/haedream/gu/gurt/selectGurtList.do?mi=1124), [foundation directory](https://www.koreg.or.kr/intro/main.do) | Official overview of nationwide/common and region-specific guarantees. Its finder states coverage of 17 foundations, but this is not proof of every municipality's current funds or remaining budgets. Finder automation is excluded by robots; product-news and educational routes are separate paths. |
| Bizinfo, MSS-operated aggregator | [Current public list](https://www.bizinfo.go.kr/sii/siia/selectSIIA200View.do), [official API documentation](https://www.bizinfo.go.kr/apiDetail.do?id=bizinfoApi) | Central ministries, local governments and related institutions. Aggregation is useful discovery, not original authorship. Public HTML requires no key; direct API requires a Bizinfo-issued key. A finance-only filter misses grants categorized as technology, startup, employment or management. |
| MSS through data.go.kr | [SME support-announcement API dataset](https://www.data.go.kr/data/15157820/openapi.do) | REST JSON/XML; documented nationwide, current-year scope, announcement ID and modification-date filters. Requires data.go.kr key. Documentation lists KOGL Type 3 (attribution, no changes), free access, development auto-approval and production review. Rights for generated adaptations require separate evaluation. |
| MOIS / Government24 through data.go.kr | [Public-benefit API dataset](https://www.data.go.kr/data/15113968/openapi.do), [published Swagger](https://infuser.odcloud.kr/api/stages/44436/api-docs?1684891964110) | Broad government-benefit catalogue, not a dedicated business-loan timetable or live budget service. Documentation says real-time updates and no usage restriction, with a key and usage application. List, details and support-condition endpoints exist; no credentialed calls were made. |

## Collection handoff

### MSS RSS and amendment identity

Live `GET https://mss.go.kr/rss/smba/board/310.do` returned 200 and parsed as RSS 2.0.
`item/title`, `item/link`, and `item/pubDate` are usable; dates appear as
`YYYYMMDDHHMMSS`, not RFC-822. Every sampled `item/id` is **310**, the board ID.
Use the link's `bcIdx` with `cbIdx`, not `item/id`, for notice identity.

The HTML list embeds `doBbsFView(cbIdx, bcIdx, ..., parentSeq)` in row handlers;
the verified detail pattern is `/site/smba/ex/bbs/View.do?cbIdx=310&bcIdx=...`.
The [October 1 fifth SEMAS-plan amendment](https://www.mss.go.kr/site/smba/ex/bbs/View.do?cbIdx=310&bcIdx=1071565)
has notice number `2026-576` and both PDF/HWPX attachments. Preserve this as an
amendment relationship, not a new application round inferred from its publication
date. Its HTML heading and registration date were checked; attachment content was
not parsed for this subtask.

### Bizinfo HTML: verified queries and selectors

Verified finance list request:

```text
GET https://www.bizinfo.go.kr/sii/siia/selectSIIA200View.do
    ?hashCode=01&rows=15&cpage=1&orderGb=1&sort=desc&schEndAt=N
```

- `hashCode=01`: finance; `cpage`: page; `rows=15`: sampled page size.
- `schEndAt=N`, including empty/default, returned open records; `Y` returned
  ended records. Poll both deliberately. Do not infer closure solely from absence.
- `.table_Type_1 table tbody tr`: eight cells, in order: display number,
  category, title/link, application-period text, ministry/region, executor,
  registration date, views.
- `td.txt_l a[href]` contains `pblancId`; canonicalize to
  `/sii/siia/selectSIIA200Detail.do?pblancId=...`.
- Detail `.view_cont > ul > li`: `span.s_title` label and `div.txt` value.
  `a#barogagi[href]` is the original-source link, not the application URL.

The inspected [Bizinfo amendment repost](https://www.bizinfo.go.kr/sii/siia/selectSIIA200Detail.do?pblancId=PBLN_000000000126965)
links to MSS `bcIdx=1071565`. Its Bizinfo registration is October 2 while the
ministry original is October 1. Store publisher dates independently. Its period
text gives budget exhaustion without a fixed deadline; preserve that text.

### SEMAS OLS: verified read-only request

```text
POST https://ols.semas.or.kr/ols/man/SMAN051M/search.do
Content-Type: application/x-www-form-urlencoded

bltwtrClcd=&bltwtrTitNm=&searchStd=1&pageNo=1&type=2
```

`type=2` selects agency loans; `type=1` is used by the direct-loan page. The live
agency call returned HTTP **201** with valid JSON, 10 rows, 23 total records and
three pages. Accept documented successful 2xx responses rather than requiring 200.
For the targeted research query, `bltwtrTitNm=4분기` returned the four Q4 notices.

Use `result[].bltwtrSeq` and `bbsTypeCd` as identity; preserve `bltwtrTitNm`,
`loanSeCdNm`, and `frstRegDt`. Pagination includes `totalCount` and `vTotalPage`.
Verified GET detail pattern:

```text
https://ols.semas.or.kr/ols/man/SMAN052M/page.do?bltwtrSeq=404&bbsTypeCd=01
```

Title: `.board_view .view_title`; substantive body: `#cntnDiv`. The date span
`#fstRegDttm` is initially empty, but list JSON provides the date and detail JS
contains the corresponding `fstRegDttm` literal. Do not read the empty span as
an undated notice. Attachment buttons call `fnDownFile`, backed by
`POST /ols/man/SMAN052M/downloadFile.do` with `bltwtrSeq`, `bbsTypeCd`, and
`bltwtrFlSeq`; the route was identified, not downloaded or validated.

The list request is a public website implementation detail, not a documented
stable open-API contract. Explicitly handle layout/schema changes and fetch errors.
No login, certificate, queue bypass, application request or business identity was
used. The browsing tool could not open one detail URL, while direct HTTP did;
retain that verification-method distinction.

### Optional official APIs

| API | Exact documented endpoint | Key / relevant fields | Execution status |
| --- | --- | --- | --- |
| Bizinfo direct | `https://www.bizinfo.go.kr/uss/rss/bizinfoApi.do` | Required `crtfcKey`; `dataType=rss` or `json`; `searchLclasId=01` finance; `pageIndex`, `pageUnit`, `searchCnt`, `hashtags` | Documentation inspected; no key supplied. Its direct API field codes should not be assumed identical to every HTML category code. |
| MSS Bizinfo through data.go.kr | `https://apis.data.go.kr/1421000/bizinfo/pblancBsnsService` | Required `serviceKey`; optional `dataType`, `pageNo`, `numOfRows`, `searchLclasId`, `hashtags`, `pblancId`, `registDe`, `updtPnttm` | Exact host/path and parameters extracted from the official page's embedded Swagger. Date-filter formatting/semantics and live responses remain untested. |
| Government24 v3 | `https://api.odcloud.kr/api/gov24/v3/serviceList`, `/serviceDetail`, `/supportConditions` | Swagger permits API key in `Authorization` or `serviceKey`; key/application required | Published Swagger fetched successfully; service responses unexecuted. |

Do not claim that the newer data.go.kr route replaces the direct Bizinfo API:
both were documented, but no retirement statement was verified. Keep each
credential namespace and response schema explicit.

## Source access and reuse boundaries

Live robots checks were read-only and are observations for 2026-10-05, not durable
permissions. Recheck before recurring collection. An absent robots file or an
allowed path does not itself establish copyright/reuse rights.

| Host | Actual result | Consequence for planned automatic collection |
| --- | --- | --- |
| `www.mss.go.kr`, `mss.go.kr` | 200; wildcard user-agent allows `/` | RSS and inspected public notice paths are not excluded by these rules. |
| `ols.semas.or.kr` | 404 HTML response | Record unavailable robots policy; evaluate published terms/rights separately. |
| `www.semas.or.kr` | Connection error | Corporate-site policy and collector remain unverified. |
| `www.bizinfo.go.kr` | 200; wildcard exclusions include `/upload`, `/download`, `/common`, `/images`, `/super` and internal paths | Inspected `/sii/siia/` pages are outside listed exclusions. Validate every attachment URL independently; avoid treating all download paths as allowed. |
| `www.kosmes.or.kr` | Robots read timed out | Keep activation contingent on a successful source-policy check; HTML availability does not resolve it. |
| `www.kibo.or.kr` | 200; `Googlebot` group excludes `/main/board/` and two service paths | Preserve user-agent-specific semantics and separately verify intended crawler policy. |
| `www.kodit.or.kr` | 200; wildcard exclusions cover notice list/detail routes and document-extension patterns | Reference links/manual research only for those excluded routes. |
| `www.koreg.or.kr` | 200; wildcard exclusion `/*/gu/gurt/`, plus private/service paths | Product finder is not an enabled crawler candidate; education/news paths need their own checks. |

The Bizinfo data.go.kr catalogue explicitly lists attribution/no-change terms.
Do not silently equate machine-readable access with permission to redistribute
altered source prose. Factual educational explanations should be independently
written and separately sourced. Per-document licences, attachments, third-party
images, and complete website terms were not exhaustively adjudicated here.

## Beginner education: facts and plain-language wording

The following English wording is a design reference for later Korean educational
copy. It is not a direct quotation, financial recommendation, or statement that a
specific applicant qualifies.

| Topic | Beginner explanation to preserve | Primary reference |
| --- | --- | --- |
| Policy loan / financing (`융자`) | Money lent for a public-policy purpose. The business repays the borrowed amount according to its agreement and usually pays interest. Rules and screening still apply. | [KOSMES funding methods](https://www.kosmes.or.kr/nsh/SH/SBI/SHSBI108M0.do), [OLS repayment calculator](https://ols.semas.or.kr/ols/man/SMAN080M/page.do) (calculator linked from the live main page; calculations not executed) |
| Grant / subsidy (`보조금`) | Support for a stated project or expense. It is generally not an ordinary repayable loan, but spending conditions, evidence and settlement matter; misuse or ineligible receipt can require return. Avoid saying “free money with no conditions.” | [Official legal interpretation on subsidy recovery](https://law.go.kr/LSW/expcInfoP.do?expcSeq=330887), an historical interpretation used for the principle, not a substitute for a current program's rules |
| Guarantee (`보증`) | A guarantee institution supports the business's borrowing by promising the lender payment under the guarantee terms if the borrower does not pay. It is not cash granted to the business or cancellation of its debt. | [KODIT guarantee overview](https://www.kodit.or.kr/kodit/cm/cntnts/cntntsView.do?cntntsId=11064&mi=2521), [KOREG terminology](https://www.koreg.or.kr/haedream/cm/cntnts/cntntsView.do?cntntsId=1082&mi=1131) |
| Interest subsidy (`이차보전`) | Support pays part of the cost of borrowing. The bank loan and its repayment remain. Do not describe an interest-support amount as loan principal or the full interest rate. | [KOSMES funding methods](https://www.kosmes.or.kr/nsh/SH/SBI/SHSBI108M0.do), [MOEL explanation](https://moel.go.kr/news/enews/report/enewsView.do?news_seq=19106) |
| Direct vs agency loan | Direct: the policy institution lends directly. Agency: a participating bank makes the loan through the program. These are lending routes, not different names for grants; guarantee involvement varies. | [KOSMES funding methods](https://www.kosmes.or.kr/nsh/SH/SBI/SHSBI108M0.do) |
| Confirmation vs final approval | “You may apply under these policy conditions” is different from “the lender has approved and paid your loan.” Review, agreement and payment are separate steps. | [SEMAS original Q4 notice 404](https://ols.semas.or.kr/ols/man/SMAN052M/page.do?bltwtrSeq=404&bbsTypeCd=01) |
| Ceiling vs actual amount | “Up to” is the largest permitted amount under stated conditions. The actual approved amount can be lower or zero following review and existing exposures. Never convert a ceiling into “you receive.” | [KOREG guarantee procedure and limit assessment](https://www.koreg.or.kr/haedream/cm/cntnts/cntntsView.do?cntntsId=1085&mi=1133) |
| Screening and fees | A guarantee institution examines the business and decides whether/how much to guarantee. Guarantee fees and bank interest are different costs; waivers and support depend on the program. | [KOREG procedure](https://www.koreg.or.kr/haedream/cm/cntnts/cntntsView.do?cntntsId=1085&mi=1133), [KOREG terminology](https://www.koreg.or.kr/haedream/cm/cntnts/cntntsView.do?cntntsId=1082&mi=1131) |

Do not copy today's numeric rates, fund amounts, age limits, fee ranges, guarantee
percentages or bank lists into permanent beginner lessons. Bind those facts to the
dated notice and specific fund. Similarly, `운전자금` and `시설자금` describe purposes;
the loan/grant/guarantee/interest-support classification is a separate dimension.

## Planned normalization and correction handling

These are research recommendations, not implemented or accepted behavior:

- Keep source notice ID, issuer, republisher, program/round, registration date,
  effective/application dates, fetched-at time, body hash, attachment identities,
  original-source URL and application URL separately.
- Store original period text and explicit date precision. Missing end date,
  ongoing reception and budget exhaustion are distinct. A passed opening date
  alone cannot prove current availability.
- Version changed bodies and attachments under the same notice identity. Preserve
  withdrawal, amendment, extension and additional-round notices as related events.
- Use exact issuer URLs/notice numbers and attachment identity to cluster reposts.
  Matching titles alone are insufficient. Multiple official reposts are not
  independent issuers and should not artificially increase verification strength.
- Exclude volatile view counters and layout/navigation from factual change hashes.
  Do not mark an item deleted or closed because a source timed out or one page cap
  excluded it.
- Report pagination truncation, source failures and unsupported attachments in
  coverage metadata. Record institution and region coverage, not “all Korean
  policy funding.” A finance-category filter is narrower than all support grants.
- Separate standing curriculum references from frequently changing announcements.
  Unknown eligibility/rate/amount remains unknown until a suitable source supplies
  it; the content generator must not invent defaults.

## Priorities and open product questions

**P0:** Implement issuer/repost provenance, typed periods, source failures,
duplicate/correction identity, protected numeric facts and the confirmation/award
distinction before claiming useful financial coverage.

**P1:** Start with the verified MSS/Bizinfo/OLS routes within approved source
policies. Handle Bizinfo open/ended partitions and OLS read-only POST responses.
Keep KODIT blocked notice routes, KOREG blocked product routes and unverified
KOSMES JSON collection outside an “active and tested” claim.

**P2:** Add credentialed official APIs only with appropriate keys and acceptance
evidence; add other grant categories, agency boards and regional originals under
explicit coverage accounting.

Open decisions for the parent design: whether the first release includes all
grant categories or only financial programs; how far to backfill; when to refresh
long-lived open notices; who reviews amended rates and eligibility; how to show
an official aggregator's delayed repost; and how the product distinguishes a
beginner learning lesson from a current program explanation. These questions do
not block the verified-source handoff.

## Verification record and limits

Executed in this subtask:

- `git status --short`, `git rev-parse HEAD`, `git branch --show-current`; inspected
  existing reports and relevant task/source-policy references.
- Browser search/open/click of the cited official sources; direct `httpx` GET of
  public HTML, RSS, robots and official Swagger documents. No credentials used.
- Public OLS search POST: valid JSON, HTTP 201; Q4 search returned four items;
  unfiltered first agency page returned ten items and 23 total records.
- GET of OLS IDs 404-407: HTTP 200 and matching exact opening-time text.
- Bizinfo finance lists with `schEndAt=N`, `Y`, and empty: HTTP 200, fifteen rows
  each; details and issuer link inspected. This is sampled parsing evidence, not
  a complete crawl or a freshness SLA.
- MSS RSS: HTTP 200, XML parse succeeded, twenty records, nonunique board IDs.
- Government24 public Swagger: HTTP 200; host, authentication names and three v3
  paths inspected. MSS Bizinfo Swagger was embedded in the official dataset page.
- KOSMES JSON read probe: connection timeout. Its robots probe also timed out.
  SEMAS corporate robots probe failed to connect.
- One scratch attempt imported absent `bs4` and failed before requests; inspection
  continued with standard-library HTML/text handling. An initial PowerShell-piped
  Korean query was misencoded; it was repeated with Unicode escapes/UTF-8 and the
  valid issuer responses above. No dependency was installed.

Ignored captures are under `.local/reviews/policy-funding/national/`; they are
optional reproduction aids, not the durable evidence authority. This Markdown
report holds the handoff. No attachments were converted, no rates calculated, no
loan or guarantee application submitted, no publisher modified, no product tests
run, no commit made, and no claim of complete national coverage is established.

## Update log

- 2026-10-05: First national source/education research; original Q4 date verified;
  keyless routes, API contracts, source-policy limits and collector selectors
  supplied to the parent implementation session.
