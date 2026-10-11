# Monthly data refresh implementation plan

**Goal:** Update October 2026 information and add explicit year/month browsing.

**Architecture:** A common PeriodSelection utility filters existing validated
housing/funding histories and topic-scoped news records. A local semiconductor
metadata archive enables safe month ingestion without changing source approvals
or the distributed publication pipeline.

**Spec:** ../specs/2026-10-08-monthly-update-design.md

## Tasks

- [x] Implement portal/periods.py: PeriodSelection(year, month, basis), query parsing,
  KST bounds, date/range matching, explicit funding-period parsing and UI context.
  Add test_period_selection.py with RED/GREEN evidence.
- [x] Add period-aware housing history selection in portal/housing.py; preserve
  default latest-snapshot behavior and saved identity/capture dates. Extend tests.
- [x] Add bounded local semiconductor metadata collection/archive command and
  read validation; respect configured access, rights, robots and rate limits.
  Tests must not call fixtures live or fabricate source approval.
- [x] Integrate semiconductor database/local-archive period selection and draft
  modification-month filtering; retain unavailable/empty distinction.
- [x] Integrate funding month/history filtering before dedup/display cap and add
  shared date controls to all relevant pages/search forms and housing archives.
- [x] Diagnose the observed funding staging-integrity failure, reproduce safely,
  and repair only a demonstrated defect while retaining substitution defenses.
- [x] Execute live October refreshes, save counts/source failures and validate
  persisted records. Preserve existing data and do not claim exhaustive coverage.
- [x] Run targeted and necessary full checks, independent review, refresh English
  canonical reports/progress and restart preview. Chrome acceptance stays open.

## Execution constraints

Use the current existing feature checkout because its uncommitted funding/portal
work is a dependency. User approval already covers implementation and collection.
No secrets, migration/approval forgery, external publishing or browser-denial
workaround. Do not check off any collection task from fixtures or uncommitted files.

## Actual checkpoint

Housing and funding have validated partial live updates; no exhaustive coverage
is claimed. Final news generation2026-10-09 has two validated live October records:
MOTIR1 and SIA1, with Samsung successfully empty. KRX remains unsupported and SK
hynix disabled. Both parser defects and all adjudicated review findings are repaired.
Two interrupted monthly test attempts are preserved; the final stable full suite
passed1,689 tests with3 skipped and203 passing subtests. Django check/drift, scoped
Ruff, static build and actual saved-data in-process rendering pass. Canonical
documentation is updated and preview22363 is running after tests. Source polling
reservations must still elapse before another collection, even when code changes.
The [progress authority](../../analysis/2026-10-08-monthly-data-update.md) records
commands, counts and actual limits. Browser acceptance stays open independently.
