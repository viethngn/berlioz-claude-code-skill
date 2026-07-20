# Wiki Page Format Reference

Load during Phase 3 of ingest, or whenever you're writing/updating a wiki
page. This is the canonical template the plugin enforces.

## Page template

Every page in `wiki/` follows this shape:

```markdown
# Page Title

**Summary**: One to two sentences describing this page.

**Sources**: List of raw source files this page draws from.

**Last updated**: YYYY-MM-DD

---

Main content goes here. Use clear headings and short paragraphs.

Link to related concepts using [[wiki-links]] throughout the text.

## Related pages

- [[related-concept-1]]
- [[related-concept-2]]
```

Fields:

- **Title**: `# Page Title` — the H1 must match the filename slug converted
  back to Title Case with hyphens as spaces.
- **Summary**: 1-2 sentences. Not marketing copy, not a mystery — just a
  concrete description of what the page covers.
- **Sources**: Each entry is a relative path to a file in `raw/`, e.g.
  `raw/prd-onboarding-flow.md`. If a page synthesizes multiple sources,
  list all of them. Empty list is a red flag — every page should trace back
  to a source.
- **Last updated**: ISO date. Update every time the page is edited.
- **Body**: Free-form Markdown. Short paragraphs. Cite claims inline with
  `(source: <raw-filename>)`.
- **Related pages**: 2-8 wiki-links, sorted alphabetically.

## Status field (optional — for archiving)

Pages are `Active` by default and carry **no** `Status` line. When a page's
knowledge is retired or replaced, add an optional `**Status**:` line immediately
after the H1, plus a banner blockquote as the first body line so any reader is
redirected to the current information:

```markdown
# Old Concept

> **⚠️ Superseded** — see [[new-concept]] for current information. Kept for historical reference.

**Status**: Superseded by [[new-concept]]

**Summary**: ...
```

- Values: `Active` (or omit the line), `Archived` (retired, no direct
  replacement), or `Superseded by [[current-page]]`.
- The `Status` line is **optional** — its absence is never a format violation.
- `/lint` excludes `Archived` / `Superseded` pages from orphan and stale checks,
  so intentionally-retired pages stop being nagged.
- In `wiki/index.md`, tag such pages, e.g.
  `[[old-concept]] — *(superseded by [[new-concept]])*`.

### Archiving vs. deleting

- **Never edit `raw/`.** It is the immutable source record. Archival happens only
  in `wiki/`.
- Prefer **superseding by banner + status** over deleting a page that has
  historical value — the banner guarantees future reads (human or Claude) land
  on the current page.
- Genuinely empty or duplicate pages with no historical value are deleted by
  `/lint` (git keeps the history).

## Filename conventions

- Lowercase, hyphen-separated: `machine-learning.md`, not `Machine Learning.md`
  or `machine_learning.md`.
- ASCII only. Unicode-normalize titles before slugifying.
- No dates in filenames unless the page is inherently temporal
  (`log.md`, `retrospective-2026-q1.md`).

## Wiki-links

- Use `[[double-brackets]]` for internal links, matching the target
  filename without the `.md` extension.
- Prefer wiki-links to markdown links for anything inside the wiki. Reserve
  markdown links (`[text](url)`) for external URLs.
- Never leave a broken wiki-link. If you mention `[[foo]]` and no `foo.md`
  exists yet, create a stub page:

  ```markdown
  # Foo

  **Summary**: TBD — mentioned in [[bar]] but not yet expanded.

  **Sources**: (none yet)

  **Last updated**: YYYY-MM-DD

  ---

  Placeholder — add content when a source covers this concept.
  ```

  `/lint` flags these stubs but does not delete them.

## Citation rules

- Every factual claim references its source file inline:
  `The API rate limit is 1000 req/min (source: raw/api-guide.pdf).`
- If two sources disagree, note the contradiction explicitly:

  > The rate limit is 1000 req/min (source: raw/api-guide.pdf), though the
  > 2026-05 changelog notes it was raised to 5000 (source:
  > raw/changelog-2026-05.md). The newer source likely reflects the current
  > state.

- If a claim has no source, mark it: `(source: needs verification)` — `/lint`
  reports these for follow-up.

## Updating `wiki/index.md`

Every ingest that creates or renames a wiki page updates `wiki/index.md`:

```markdown
# Wiki Index

## Concepts
- [[machine-learning]] — Foundations of ML and how we use it
- [[transformer-architecture]] — Attention-based sequence models

## Products
- [[product-alpha]] — Our flagship product; ...

## People
- [[jane-doe]] — Product manager for [[product-alpha]]
```

Group by natural category (Concepts, Products, People, Projects, etc.) —
create new categories as needed but keep the count small (5-8 categories max).

## Updating `wiki/log.md`

Append-only. Newest entry at the top:

```markdown
# Wiki Log

## 2026-07-02
- Ingested `raw/prd-onboarding.md`
- New pages: [[onboarding-flow]], [[activation-metrics]]
- Updated: [[product-alpha]] (added onboarding section)

## 2026-07-01
- Ingested `raw/PROJ-123.md`
- ...
```

Each entry has:

- ISO date header (`## YYYY-MM-DD`) — add a suffix for non-ingest operations,
  e.g. `## 2026-07-20 (lint)`
- Bullet list of what was ingested/changed (or, for lint, what was cleaned:
  pages deleted, archived/superseded, conflicts resolved, fixes applied)
- Wiki-links to the affected pages

`log.md` records **every ingest and lint operation**. Both `/ingest` and `/lint`
append an entry. Never edit past entries — treat `log.md` as append-only history.

## Anti-patterns to avoid

- **One giant page**: Split into 5-10 focused pages linked by `[[wiki-links]]`.
- **Duplicated content**: If two pages contain the same paragraph, factor it
  into a third page and link both to it.
- **Unsourced claims**: Every fact should trace back to a `raw/` file. If it
  doesn't, mark `(source: needs verification)` and `/lint` will remind you.
- **Marketing tone**: Write plainly. "The product supports X" not "The
  revolutionary product harnesses cutting-edge X".
- **Stale wiki-links**: When renaming a page, grep for all references
  (`rg '\[\[old-name\]\]' wiki/`) and update them.
