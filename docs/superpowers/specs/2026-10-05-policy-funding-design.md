# Korean policy funding collection and beginner education design

Date: 2026-10-05, Asia/Seoul. Baseline db5edec; branch codex/policy-funding.
User authorized end-to-end planning, implementation and verification. Engineering
records are English; the product, explanations and preserved user sample are Korean.

## Purpose and success

Add a usable service for small-business owners who do not understand policy funding.
Collect central/local official notices, show where/when/how to apply and explain
loans, guarantees, support certificates, costs and repayment in elementary-level
language. A support certificate and its ceiling never mean approval or an awarded sum.
The supplied October 6 nationwide/Pohang/Yangsan/Yeongcheon text is a regression input,
not trusted authority. Verify fields individually against actual dated official evidence.

## Alternatives and chosen architecture

1. Extend housing contracts: rejected because they require housing-specific dates,
   selection, image metadata and official hosts; that would damage existing behavior.
2. Extend the whole distributed topic pipeline now: requires unrelated locked-release,
   editorial, registry and R9 publishing repairs before a working funding experience.
3. Chosen: a dedicated apps.policy_funding module inside this Django service, with
   real public-source collectors, Korean education, immutable collection bundles,
   CLI commands and integrated read-only pages. Reuse safe HTTP and bundle primitives.

This feature collects and explains funding; it does not submit financial applications,
calculate personal approval, or enable external publishing. Existing housing/news
flows remain intact. The new module has its own schema, directory and source catalog.

## Functional contract

- Source catalog: central agencies, KOREG and the requested 17 regional buckets,
  their foundations/government notice portals, municipal examples. Entries carry
  authority, exact URLs, coverage, verified-on date, collection adapter or discovery
  status. A directory entry is not a claim of live automated coverage.
- Working no-key collectors: OLS public notice search/detail (read-only form search)
  and Bizinfo finance HTML lists/detail, including separate open/ended lists.
  Further official routes may be registered only with tested parsers and policy.
- Each notice: stable source identity, official URL, issuer, title, published date
  when explicit, application-period text, region buckets, funding kinds, source text,
  references/attachments and fetched timestamp/content hash. Preserve unknown values.
- No guessed rates, ceilings, application dates, eligibility or approval. Distinguish
  source publication time, registration time, collection time and event/application time.
- Closed-schema records; canonical URLs retain required notice IDs and allow only
  researched official hosts/routes. Bounded HTTPS transport checks redirects/DNS,
  bytes, time and request count. Classified per-source failures remain visible.
- Bounded pagination and detail counts. Report covered sources, skipped/discovery
  sources, errors and truncation. Empty/blocked HTML is not successful collection.
- Stable semantic hashes ignore view counts/retrieval time. Compare prior validated
  runs for added/changed/unchanged notices; absent in a bounded scan does not mean closed.
- Immutable v2 bundles under output/policy-funding (configurable POLICY_FUNDING_ROOT):
  Korean index/article Markdown, schema-checked notices.json/raw-observations.json,
  article verification metadata and shared manifest checks. No unvalidated file reader.
- CLI supports bounded live collection and explicit repository fixture mode, a fixed
  KST as-of date for tests, output root and source/region filters. Fixture output is
  labeled and never accepted as live verification.
- UI: policy home/notices, notice details with source/date badges, region/type/query
  filters, source directory, glossary, step-by-step guide, comparison/preparation
  checklist and run/change status. No external calls from page GETs.
- Local requests require loopback; nonlocal deployment requires an active staff
  session. Read-only integration keeps existing API middleware and CSRF unchanged.

## Education contract

Explain policy loan, grant, guarantee, direct/agency loans, working/facility/startup/
emergency/refinancing funding, principal, interest, fixed/variable rates, rate subsidy,
guarantee fees, grace period, maturity, repayment methods, credit, arrears, debt and
guarantee balances. Every term uses a short explanation and a clearly hypothetical
example; numeric examples never become current program terms.

Agency process: choose program → confirm eligibility → obtain support confirmation →
guarantee review if required → bank review → contract/disbursement → repayment.
Direct loans and grants have separate paths. Explain conditional stages, refusals,
remaining guarantee capacity and why a maximum ceiling differs from the actual sum.
Provide business-registration/sales/debt/guarantee/credit/staff/use-of-funds checklist,
official application links and scam/contact cautions grounded in primary sources.
Central funds and location-specific guarantees/subsidies must remain distinct.

## Reference-case verification

Store the user's wording and numbered assertions with provenance=user_provided and
as_of=2026-10-05. Verification output distinguishes supported, conflicting, partially
supported and unverified fields with evidence URL, issuer/date and exact relevant
excerpt/context. Matching a number somewhere on a page is insufficient; municipality,
program, fiscal year and stage must match. Conflicting sources are shown for review,
not silently resolved by whichever page was fetched last.

## Acceptance and limits

Meaningful tests cover official parsers, redirect/URL rejection, malformed pages,
KST/date boundaries, duplicate/revised records, missing values, source failure,
immutable/tampered bundles, fixture/live separation, all 17 catalog buckets,
education distinctions, reference-case mismatches and UI authorization/escaping.
Run actual bounded official collection and retain a dated English acceptance report.
If a portal is blocked or untested, display that exact limit. Actual bank/guarantee
approval and future budget availability cannot be inferred by this service.

## Documentation and restart

Progress: ../../analysis/2026-10-05-policy-funding-progress.md.
Source research reports and the implementation plan are linked from that ledger.
Preserve all earlier October 5 review work and historical acceptance records.
