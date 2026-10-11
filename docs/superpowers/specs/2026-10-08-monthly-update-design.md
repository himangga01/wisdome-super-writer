# Monthly data refresh and date navigation

## Goal and authorization

The user requested October data collection/update and year/month browsing across
the three independent workspaces. Use October 2026, with live collection only up
to its actual capture time (work began October8 and continues October9 KST).
Preserve older immutable
runs, independent topic roots, source rules and browser-denial boundaries.

## Date semantics

Use query parameters year, month and basis. Empty year/month means all saved
periods, retaining current default compatibility. A selected year with no month
means that full year; a month requires a valid year. Invalid/repeated inputs return
400 before data loading. Supported basis values are published, collected and,
for housing, schedule. All datetimes use KST calendar boundaries.

- User clarification: housing defaults to actual special-supply and first-priority
  reception dates, including their regional distinctions. Generic application
  start/end and posting dates must not be substituted for these stages. Unknown
  stage dates remain unknown; stage choice is all/special/first.
- Funding defaults to official notice posting/registration date, labelled with
  the source's actual date meaning. Application dates do not determine its month.
- Semiconductor news uses original publication or collection date. Drafts use
  their modification month, labelled separately from original news dates.
- Collection history uses collection time/run window rather than notice date.
- Unknown dates remain unknown and are reported outside a selected dated result.

Search/date filtering precedes display limits. Monthly history is filtered before
identity deduplication, so later corrections or absence do not erase old-month
observations. Show bounded coverage honestly. Forms preserve query, region, kind,
mode and date selection when the user changes another filter.

## Collection and storage

Housing supports an explicit earlier --since date so a September-announced
October reception is not lost by an October-only publication window. Preserve
existing days behavior; do not claim exhaustive coverage before the lookback date.

Refresh funding through its existing bounded CLI and housing through the current
notice-only workflow. Preserve any failed or partial outcomes; never call them a
successful full update. Initial funding write hit an owned-staging integrity guard
and needs diagnosis rather than guard removal.

The distributed semiconductor pipeline requires source-check approval, Redis and
12-hour freshness policy; it cannot represent monthly local archive ingestion by
merely inserting approved-looking rows. Add a separate local-only metadata archive
for enabled permitted official sources. Reuse strict source parsers and safe pinned
HTTP, check robots and configured rate/access/rights limits, and write hash-bound
immutable run metadata. Store titles, URLs, original dates and capture/provenance;
do not invent registry approval, generate article bodies or publish externally.
Disabled/prohibited sources remain disabled. The portal may read this local archive
under its existing local access boundary while keeping database state distinct.

## UX

Add compact year/month/date-basis controls with all-period and current-month
shortcuts. Keep the shared black/white/lime design, labelled native controls,
responsive wrapping and existing topic-specific menus. Explain the selected date
basis and show explicit empty/unknown/unavailable states.

## Verification

Tests must cover KST boundary dates, year-only/all-period inputs, invalid/repeated
parameters, unknown dates, application overlap, old-month observations after newer
updates, filtering before caps, real metadata integrity, no fabricated approval,
disabled-source/robots/rate handling and preserved form state. Run meaningful
integration checks and actual CLI collection. Chrome remains blocked; backend tests
and raw source collection do not establish browser/mobile visual acceptance.
