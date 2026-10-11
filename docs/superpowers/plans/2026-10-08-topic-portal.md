# Three-topic portal implementation plan

> For agentic workers: use Superpowers execution practices, preserve the English
> progress ledger, and execute independently of blocked browser navigation.

**Goal:** Give housing, semiconductor and policy funding independent pages and
topic-specific navigation within one responsive operator portal.

**Architecture:** Server-rendered Django pages reuse existing validated storage
and topic-scoped data models. A shared navigation context and layout provide
desktop/mobile topic switching without changing collection or publication.

**Tech stack:** Existing Python 3.12, Django, templates and external CSS.

**Spec:** ../specs/2026-10-08-topic-portal-design.md

## Global constraints

- Preserve existing working changes, old URLs, access guards and immutable data.
- No new dependencies, model migrations, external publishing or fake content.
- Do not bypass browser permission denial. Keep Chrome/reference acceptance open.
- Use current feature branch; existing uncommitted funding code is a dependency.

## Review focus

- Remote/nonlocal requests must not read housing files.
- Missing database/schema must not masquerade as zero collected news.
- Legacy/corrupt housing metadata must not become trusted notice cards.
- Queries, counts and menus must remain confined to their topic.
- Escaped text, safe URLs, long titles and empty states must remain usable.

## Tasks

### 1. Read-only topic data projections

- [x] Add `wisdome_writer/portal/housing.py` and `semiconductor.py` loaders.
- [x] Add meaningful regression tests for the review-focus cases before code.
- [x] Reuse validated housing readers and topic-scoped ORM; no GET side effects.
- [x] Run projection tests and record actual results.

### 2. Routing and shared responsive shell

- [x] Add `portal/navigation.py`, `views.py`, `urls.py`, and package init.
- [x] Add navigation context processor and replace root redirect with portal view.
- [x] Create `templates/portal/` shell, home, topic and inventory templates plus
  `static/portal/app.css`; preserve real links and explicit unavailable states.
- [x] Test routing/access/topic menus and old route compatibility.

### 3. Integrate existing topic screens

- [x] Adapt funding and local-article templates to the shared shell.
- [x] Add funding glossary/history pages using existing data contracts.
- [x] Keep staff console independent and add portal return navigation.
- [x] Run existing housing/funding/console regression checks.

### 4. Verification and handoff

- [x] Run Django check/drift, appropriate test suite, scoped lint and static build.
- [x] Obtain independent scoped review and revalidate material findings.
- [x] Update canonical English reports, README and AGENTS with actual results.
- [ ] Inspect requested reference and run direct Chrome desktop/mobile acceptance
  only after an authorized browser-state change. This item remains blocked.

No commit/push/deployment is required merely to share the documentation.

Final server-side evidence: 1,502 passed, 3 skipped, 203 subtests (529.16 seconds),
Django check/drift and scoped lint pass, static build complete. Task-owned preview
session 53227 (subsequently stopped for monthly checks). Current-byte evidence and
preview status are in [monthly progress](../../analysis/2026-10-08-monthly-data-update.md).
Browser acceptance remains unchecked; see the progress ledger for
the saved-permission blocker and the existing local schema initialization limit.
