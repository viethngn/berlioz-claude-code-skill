---
name: lint
description: |
  Audits an LLM wiki for knowledge gaps, contradictions between pages,
  outdated facts, orphan pages, broken `[[wiki-links]]`, missing concept
  pages, format violations, and stale pages. Emits a numbered report of
  findings with suggested fixes, applies them with user approval, and
  commits the changes.

  Use this skill whenever the user wants to lint, audit, review, clean up,
  or check the health of their wiki. Trigger on phrases like: "lint the
  wiki", "audit the wiki", "check for contradictions", "find knowledge
  gaps", "clean up broken links", "review my wiki pages", "check what's
  outdated", or "run lint".
---

# Lint — LLM Wiki

Two-pass audit of an LLM wiki:

1. **Deterministic pass** (via `lint.py`) — finds orphan pages, broken
   `[[links]]`, missing concept pages, format violations, and stale pages.
   Emits a JSON report.
2. **Semantic pass** (this skill / you) — reads the report and the flagged
   pages, cross-checks pairs of related pages for contradictions, compares
   wiki claims against `raw/` sources to spot outdated facts, and drafts
   fixes.

Fixes are always applied with user approval, then committed to git.

## Prerequisites

- The wiki has a `.wikirc.json` at its root.
- The wiki root is a git repository (so fixes can be committed).
- `python3` is available. `lint.py` is stdlib only — no other dependencies.

## Workflow

### Phase 1 — Run the deterministic linter

```bash
python3 "${SKILL_DIR}/scripts/lint.py" --wiki-root "${WIKI_ROOT}"
```

Output is JSON on stdout with these top-level keys:

- `page_count` — total wiki pages seen
- `orphans` — pages that no other wiki page links to (excluding `index.md` and `log.md`)
- `broken_links` — `[[link]]` targets that don't exist in `wiki/`
- `missing_pages` — de-duplicated broken-link targets, grouped by referring pages
- `format_violations` — pages missing `Summary` / `Sources` / `Last updated` blocks or an H1
- `stale_pages` — pages whose `Last updated` is older than `--stale-days` (default 90)
  and where a newer file exists in `raw/`
- `unsourced_claims` — pages that mention "needs verification" or that have no `Sources` entries

Read the whole report before showing anything to the user.

### Phase 2 — Semantic checks (you do this by reading pages)

For **contradictions**:

- Build the wiki-link graph from the report's `edges` field.
- For every pair of pages that co-occur (page A links to X and page B also
  links to X, or A ↔ B), read both pages fully.
- Look for claims about the same fact (a number, a date, a name, a
  behavior) that disagree. Report them.

For **outdated facts**:

- For each page in `stale_pages`, read the raw sources listed in its
  `Sources` block plus any newer files in `raw/` that reference the same
  topic (by title similarity or shared wiki-link targets).
- Flag any claim in the wiki page that the newer raw source contradicts.

For **knowledge gaps**:

- For every entry in `missing_pages`, decide if the concept warrants its
  own page. If yes, propose the new page; if no (e.g., the wiki-link is
  overkill for a passing mention), propose removing the brackets.

### Phase 3 — Present findings to the user

Show a numbered list. Group by severity:

```
Lint results for <wiki-root> (<page_count> pages):

BROKEN LINKS (N):
  1. [[foo]] referenced in [[bar]], [[baz]] — no file exists
  2. ...

FORMAT VIOLATIONS (N):
  3. wiki/quux.md is missing the "Last updated" block
  ...

CONTRADICTIONS (N):
  4. [[api-limits]] says "1000 req/min" but [[changelog-2026-05]] says
     "5000 req/min". Newer source likely correct.

OUTDATED FACTS (N):
  5. [[jane-doe]] lists title "PM" but raw/PROJ-500.md dated 2026-06-15
     names her "Senior PM".

KNOWLEDGE GAPS (N):
  6. [[event-loop]] is referenced 3x but has no page. Worth creating?

ORPHAN PAGES (N):
  7. wiki/deprecated-service.md — no inbound links. Delete or link?

STALE PAGES (N):
  8. wiki/onboarding.md last updated 2025-11-01 but raw/PROJ-800.md (2026-06)
     covers the same topic. Refresh?

UNSOURCED CLAIMS (N):
  9. wiki/pricing.md has 3 unsourced statements.
```

### Phase 4 — Apply fixes

For each finding, propose a concrete fix and ask the user:

- **Broken links / knowledge gaps**: create the page (with `TBD` stub) or
  remove the brackets.
- **Format violations**: add the missing block, using today's date for
  `Last updated`.
- **Contradictions**: rewrite the older/incorrect claim to defer to the
  newer source, or note the contradiction explicitly.
- **Outdated facts**: update the wiki page and bump `Last updated`.
- **Orphan pages**: add inbound links from a naturally-related page, or
  delete if the page is truly obsolete (user must confirm delete).
- **Unsourced claims**: mark with `(source: needs verification)` and add
  to a follow-up task list.

Ask before applying each category — don't batch. The user may want to
skip whole categories.

### Phase 5 — Commit

```bash
git -C "${WIKI_ROOT}" add wiki
git -C "${WIKI_ROOT}" commit -m "lint: <one-line summary>"
```

Where the one-line summary names the categories fixed, e.g.
`lint: fix 3 broken links, refresh 2 stale pages`.

## `lint.py` options

```
python3 lint.py --wiki-root PATH [--stale-days DAYS] [--sources]
```

- `--stale-days DAYS` — Threshold in days for stale detection (default 90).
- `--sources` — Also include raw/ scan metadata for the semantic pass
  (list of raw file mtimes and titles).

## Design notes

- The linter never modifies files. All writes happen from the semantic pass
  through your `Write`/`StrReplace` tools with user approval.
- `index.md` and `log.md` are exempt from the orphan check — they're
  intentionally unreferenced.
- The stale detection uses filesystem mtime of `raw/` files vs the
  `Last updated:` line in each wiki page. It's a heuristic — always confirm
  with the user before rewriting.
- Contradictions and outdated facts are semantic — the linter cannot detect
  them. It only surfaces the pages you should look at.
