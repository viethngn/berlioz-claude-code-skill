# Wiki Page Format Reference

Load during the wiki-update phase of ingest, or whenever you're
writing/updating a wiki page. This is the canonical template the plugin
enforces. *When* to create a page versus update one is decided by the ingest
skill's "Wiki-update rules"; this file covers what a page looks like.

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
  `(source: raw/<file>)`.
- **Related pages**: 2-8 wiki-links, sorted alphabetically.

## Status field (optional — legacy in-place archiving)

> The primary way `/lint` archives a retired page is now to **move it into
> `wiki/archive/`** (see "Archiving vs. deleting" below). The `**Status**`
> banner described here is still parsed and supported for backward-compat and
> for cases where you want to keep a page in place while flagging it.

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
- `/lint` excludes `Archived` / `Superseded` pages from orphan and drift checks,
  so intentionally-retired pages stop being nagged.
- In `wiki/index.md`, tag such pages, e.g.
  `[[old-concept]] — *(superseded by [[new-concept]])*`.

### Archiving vs. deleting

- **Never edit `raw/` file contents.** A raw file is the immutable record of
  what a source said. `/lint` may **delete** an empty, uncited raw container
  page (title-only, no body, cited by no wiki page) — but only after user
  confirmation, and it never edits or deletes a raw that still carries content
  or is cited.
- **Archive a retired wiki page by moving it to `wiki/archive/`** (`/lint` uses
  `git mv`). Archived pages leave active lint scope (no more orphan/drift
  nagging) but stay in git and remain valid `[[link]]` targets — a reference to
  an archived page is not a broken link. Tag them under an `## Archive` section
  of `index.md`.
- The in-place `**Status**` banner is still supported (backward-compat), but the
  folder move is the primary mechanism and is preferred over deleting a page
  that has historical value.
- Genuinely empty or duplicate wiki pages with no historical value are deleted
  by `/lint` (git keeps the history).

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
- Never leave a broken wiki-link, and never create an empty placeholder page
  to make one resolve. Until a source gives a concept enough substance for a
  page of its own, mention it in plain text without brackets; add the
  `[[link]]` when the page exists. A placeholder page looks like knowledge
  but holds none, and `/lint --fix` deletes placeholder stub pages.
- `[[label/slug]]` (a `/` whose first part is not `archive`) is a cross-wiki
  link into a linked wiki written by `/log`; it is never resolved locally and
  `/lint` never reports it as broken. `[[page#Heading]]` links to `page`.

## Citation rules

- Every factual claim references its source file inline:
  `The API rate limit is 1000 req/min (source: raw/api-guide.pdf).`
- If two sources disagree, the claim from the source with the later
  `source_date` (in its `raw/<slug>.source.json`) replaces the older one and
  cites the newer source. When the dates are equal or one is missing, keep
  both and say so explicitly:

  > The rate limit is 1000 req/min (source: raw/api-guide.md), though the
  > changelog says 5000 req/min (source: raw/changelog-2026-05.md); the
  > sources do not say which is current.

- If a claim has no source, mark it: `(source: needs verification)` — `/lint`
  reports these for follow-up.

## Updating `wiki/index.md`

`wiki/index.md` is kept in step with the pages on disk by a script, not by
hand:

```bash
python3 <plugin>/skills/lint/scripts/wiki_tools.py index-sync --wiki-root <wiki>
```

It adds each page missing from the index as `- [[slug]] — <Summary>` under
`## Uncategorized`, removes lines for pages that no longer exist, and moves
lines for pages now in `wiki/archive/` under `## Archive`. Existing sections,
their order and hand-written descriptions are left alone, as are journal
pages and the `## Diary` section (owned by `/log`). After it runs, a newly
added line may be moved into an existing section when one obviously fits.
The resulting index looks like:

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

Append-only, newest entry at the top, written by a script:

```bash
python3 <plugin>/skills/lint/scripts/wiki_tools.py log --wiki-root <wiki> \
  --op ingest --title "Ingested raw/prd-onboarding.md" \
  --bullet "New pages: [[onboarding-flow]]" --bullet "Updated: [[product-alpha]]"
```

`--op` is `ingest`, `lint` or `log`; a second run on the same day with the
same op adds its bullets to that day's entry. The result:

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

- ISO date header (`## YYYY-MM-DD`); other operations get a suffix,
  `## 2026-07-20 (lint)` or `## 2026-07-20 (log)`
- Bullet list of what was ingested/changed (or, for lint, what was cleaned:
  pages deleted, archived/superseded, conflicts resolved, fixes applied)
- Wiki-links to the affected pages

`log.md` records **every ingest, lint and log operation**. Never edit past
entries — treat `log.md` as append-only history.

## Anti-patterns to avoid

- **One page per source**: A source is not a topic. Fold its knowledge into
  the pages for the topics it covers; `raw/<slug>.md` already is the
  per-source record.
- **One giant page**: Split only when a page covers clearly separate topics,
  each with enough substance for a page of its own.
- **Duplicated content**: If two pages contain the same paragraph, keep it on
  the page whose topic it belongs to and link there from the other.
- **Unsourced claims**: Every fact should trace back to a `raw/` file. If it
  doesn't, mark `(source: needs verification)` and `/lint` will remind you.
- **Marketing tone**: Write plainly. "The product supports X" not "The
  revolutionary product harnesses cutting-edge X".
- **Stale wiki-links**: When renaming a page, grep for all references
  (`rg '\[\[old-name\]\]' wiki/`) and update them.
