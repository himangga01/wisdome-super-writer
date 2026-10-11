# Three-topic content portal design

## Purpose and authorization

The user authorized a complete portal UX change with independent housing,
semiconductor and policy-funding workspaces, topic-specific menus, mobile support
and direct Chrome testing. Preserve the current development/operator audience.
The reference URL remains inaccessible because browser authorization rejects
navigation. The user has now supplied two reference images and descriptive text:
a Permitly solar-permitting dashboard and its black/white/lime palette. Those
user-provided images are the visual authority. Direct Chrome acceptance remains open.

Portable copies: [dashboard](../../reference/topic-portal/dashboard.png) and
[palette](../../reference/topic-portal/palette.png). These are the supplied reference
assets, not screenshots of the implemented portal.

## Information architecture

- `/`: portal entrance with three clearly labelled topic cards and direct links.
- `/housing/`: housing overview; `/housing/notices/`: validated saved notices;
  `/housing/sources/`: official source directory. Existing `/local-articles/`
  archive, run and article links remain valid.
- `/semiconductor/`: semiconductor overview; `/semiconductor/news/`: saved source
  records; `/semiconductor/sources/`: configured source directory;
  `/semiconductor/articles/`: topic-scoped draft inventory.
- `/policy-funding/`: existing notice search; existing guide/detail/source routes
  remain; add separate glossary and collection-history pages.
- `/console/`: existing staff-only operations remain available through an
  administration link, outside the topic-specific reading menus.

## Shared layout

Use a consistent restrained light workspace layout, a left desktop navigation
rail, topic identity, breadcrumb and compact content header. Use pale gray surfaces,
white rounded cards, near-black selected controls and restrained lime highlights
inspired by the supplied images. Topic names, icons, URL and separate menu groups
provide identity. Global switching and local topic navigation are visually distinct.
Mobile replaces the rail with a native details
menu and visible topic links. Use external CSS, semantic headings, keyboard focus,
labelled controls, minimum 44px targets and flexible single-column layouts.
Do not add speculative charts, fabricated counts or nonfunctional action buttons.
Adapt the reference's dense statistics/table plus dark guidance-card composition
to real source inventories and educational actions. Do not copy its invented
projects, approval percentages, AI predictions or business-specific controls.

## Data and security

GET/HEAD only. The portal and semiconductor/funding pages use the existing
loopback-local or active-staff-nonlocal rule. Housing remains strictly local and
loopback-only, including its filesystem access. No GET performs collection,
migration, humanizer probes, publication or directory creation.

Housing enriched notices require schema-v2 run-bound metadata; older runs remain
archive links. Preserve unknown dates, saved-observation wording, safe official
URLs, immutable bundle validation and independent output roots. Prefer a single
validated snapshot over ambiguous all-history merging.

Semiconductor queries are topic-scoped on the server, and stored versions are
labelled as such. Retain correction/retraction/rights labels. Missing database
tables produce an unavailable state distinct from zero records. Directory entries
are configuration, not evidence of live collection. Do not expose secret references,
raw external configuration or article bodies in the portal inventory.

Funding retains all existing provenance, observed-status and financial explanations.
The shared layout must not imply personal eligibility, current budget or approval.

## Acceptance

Meaningful tests cover access before I/O, topic isolation, schema/metadata handling,
unknown and unavailable states, safe outgoing URLs, existing route compatibility
and server-rendered navigation. Run Django check/drift and scoped lint.
Direct Chrome desktop/mobile inspection remains a required but blocked acceptance
step. Source rendering tests cannot substitute for it. No deployment claim.
