# {{ title }}

A personal knowledge base maintained with the `llm-wiki` plugin.
Based on Andrej Karpathy's LLM Wiki pattern.

## Purpose

This wiki is a structured, interlinked knowledge base. Claude maintains
the wiki. The human curates sources, asks questions, and guides the
analysis.

## Folder structure

```
raw/       -- source documents (immutable -- never modify these)
wiki/      -- markdown pages maintained by Claude
wiki/index.md -- table of contents for the entire wiki
wiki/log.md   -- append-only record of all operations
templates/ -- page template
```

## Ingest workflow

When the user adds a new source to `raw/` and asks you to ingest it, or
provides a Confluence/Jira URL or local file to the `/ingest` skill:

1. Read the full source document (or let `/ingest` fetch it)
2. Summarize key takeaways for the user's awareness (non-blocking — do not
   wait for approval), then proceed automatically
3. Create a summary page in `wiki/` named after the source
4. Create or update concept pages for each major idea or entity
5. Add wiki-links ([[page-name]]) to connect related pages
6. Update `wiki/index.md` with new pages and one-line descriptions
7. Append an entry to `wiki/log.md` with the date, source name, and what
   changed
8. Commit and push raw + wiki together — run
   `ingest.py --commit-only --slug <slug>`, which commits both and pushes when
   `auto_push` is enabled. Do not use a bare `git commit` (it skips the push).

The full flow runs end-to-end without pausing: fetch → synthesize → commit →
push. A single source may touch 10-15 wiki pages. That is normal.

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
- Use the format (source: filename.md) after the claim
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

## Lint

When the user asks you to lint or audit the wiki, use the `/lint` skill. It
keeps the knowledge base clean so future reads always surface the latest
information:

- **Automatic structural cleanup** (no approval): delete empty/stub pages, fix
  broken `[[links]]`, link or archive orphans, fix format violations and
  broken `Sources` paths.
- **Conflicts verified with the user**: contradictions and outdated facts are
  collected into ONE report; the user confirms the resolutions before they're
  applied.
- **Archive, don't delete, retired knowledge**: mark outdated pages
  `Superseded by [[current-page]]` (or `Archived`) with a banner — never touch
  `raw/`. Retag them in `index.md`.
- **Logs**: append a `## <date> (lint)` entry to `wiki/log.md`, and update
  `index.md`.
- **Commit + push**: grouped commits per category, then push (via
  `ingest.py --push-only`).

## Rules

- Never modify anything in the `raw/` folder
- Always update `wiki/index.md` and `wiki/log.md` after changes
- Keep page names lowercase with hyphens (e.g. `machine-learning.md`)
- Write in clear, plain language
- When uncertain about how to categorize something, ask the user
