# {{ title }}

A personal knowledge base maintained with the `llm-wiki` plugin.
Based on Andrej Karpathy's LLM Wiki pattern.

## Purpose

This wiki is a structured, interlinked knowledge base. Claude maintains
the wiki. The human curates sources, asks questions, and guides the
analysis.

<!-- llm-wiki:begin -->
<!-- Managed by the llm-wiki plugin. `bootstrap.py --upgrade` rewrites everything
between these markers; put your own instructions outside them. -->

## Folder structure

```
raw/          -- source documents (immutable -- never modify these)
wiki/         -- markdown pages maintained by Claude
wiki/archive/ -- retired pages (moved here by /lint, still linkable)
wiki/index.md -- table of contents for the entire wiki
wiki/log.md   -- append-only record of all operations
templates/    -- page template
```

## Skills

To ingest a source (Confluence, Jira, Slack, a web page or site, a local
file — or a bare `/ingest` to refresh every tracked source), use `/ingest`.
To lint or audit the wiki, use `/lint`. To record notes or decisions in the
diary, use `/log`. Their instructions take precedence over anything here.

Ingest updates the existing pages a source touches first and creates a new
page only for a concept no page covers yet. Images are described only when
image description is enabled, by the session model.

## Page format

Every wiki page should follow this structure:

```markdown
# Page Title

**Summary**: One to two sentences describing this page.

**Sources**: List of raw source files this page draws from.

**Last updated**: Date of most recent update.

---

Main content goes here. Use clear headings and short paragraphs.

Link to related concepts using [[wiki-links]] throughout the text.

## Related pages

- [[related-concept-1]]
- [[related-concept-2]]
```

**Optional `Status` field (for archiving):** pages are `Active` by default and
omit this line. When a page is retired, add a `**Status**:` line right after the
H1 — `Archived` or `Superseded by [[current-page]]` — plus a banner blockquote
as the first body line so readers are redirected to the latest page:

```markdown
# Old Concept

> **⚠️ Superseded** — see [[new-concept]] for current information.

**Status**: Superseded by [[new-concept]]

**Summary**: ...
```

## Citation rules

- Every factual claim should reference its source file
- Use the format `(source: raw/<file>)` after the claim
- If two sources disagree, note the contradiction explicitly
- If a claim has no source, mark it as needing verification

## Question answering

When the user asks a question:

1. Read `wiki/index.md` first to find relevant pages
2. Read those pages and synthesize an answer
3. If a page is `Archived` or `Superseded`, follow its banner to the current
   page and answer from that — always surface the latest knowledge
4. Cite specific wiki pages in your response
5. If the answer is not in the wiki, say so clearly
6. If the answer is valuable, offer to save it as a new wiki page

Good answers should be filed back into the wiki so they compound over time.

## Rules

- Never edit the *contents* of anything in the `raw/` folder. Two exceptions,
  both plugin-managed:
  - `/lint` may **delete** an empty, uncited raw file after the user confirms —
    it never modifies a raw file's content or removes a non-empty/cited one.
  - `raw/.bulk-queries.json` is metadata, not a source document: it records
    which bulk queries (space / CQL / JQL / sitemap / crawl) this wiki tracks,
    so a bare `/ingest` knows what to re-check. `/ingest` maintains it; don't
    hand-edit it, and don't treat it as a source when answering questions.
- Don't hand-edit `wiki/index.md` or `wiki/log.md`: they are maintained by
  `wiki_tools.py` (`index-sync`, `log`), which the skills run. The `## Diary`
  section and the day pages belong to `/log`.
- Keep page names lowercase with hyphens (e.g. `machine-learning.md`)
- Write in clear, plain language
- When uncertain about how to categorize something, ask the user
<!-- llm-wiki:end -->
