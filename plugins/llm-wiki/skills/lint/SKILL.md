---
name: lint
description: |
  Thoroughly cleans up an LLM wiki: removes empty and orphaned pages,
  fixes broken `[[wiki-links]]` and format violations, archives outdated
  knowledge with status banners (never touching `raw/`), and verifies
  conflicting information with the user before resolving it. Structural
  cleanup is automatic; conflicts are batched into one report for approval.
  Updates `wiki/index.md` and appends a `wiki/log.md` entry, then commits
  (grouped by category) and pushes.

  Use this skill whenever the user wants to lint, audit, review, clean up,
  or check the health of their wiki. Trigger on phrases like: "lint the
  wiki", "audit the wiki", "check for contradictions", "find knowledge
  gaps", "clean up broken links", "review my wiki pages", "check what's
  outdated", "archive outdated pages", or "run lint".
---

# Lint — LLM Wiki

Goal: keep the knowledge base clean so future reads always surface the
**latest** information. Lint runs mostly automatically — structural cleanup
(empty/orphan/broken/format) applies without asking; only genuine
information **conflicts** are batched into one report you approve before
they're applied. `raw/` is never modified. When done, lint commits (grouped
by category) and pushes.

## Prerequisites

- The wiki has a `.wikirc.json` at its root.
- The wiki root is a git repository (so fixes can be committed and pushed).
- Python is available. `lint.py` is stdlib only — no other dependencies.

## Resolving the Python interpreter

Before running any script, resolve the correct Python binary (the llm-wiki
deps live in a dedicated venv):

```bash
_LLMWIKI_VENV="${LLMWIKI_VENV:-${HOME}/.llm-wiki-venv}"
if [ -x "${_LLMWIKI_VENV}/bin/python3" ]; then
  WIKI_PY="${_LLMWIKI_VENV}/bin/python3"
else
  WIKI_PY="${PYTHON:-python3}"
fi
```

Use `${WIKI_PY}` instead of `python3` below. `${SKILL_DIR}` is this file's
directory; `${WIKI_ROOT}` is the wiki directory (contains `.wikirc.json`).
`${INGEST_DIR}` is `${SKILL_DIR}/../ingest` (for `ingest.py`).

## Workflow

### Phase 1 — Run the deterministic linter

```bash
${WIKI_PY} "${SKILL_DIR}/scripts/lint.py" --wiki-root "${WIKI_ROOT}"
```

JSON on stdout. Top-level keys:

- `page_count` — total wiki pages seen
- `edges` / `inbound_counts` — the wiki-link graph (for the semantic pass)
- `orphans` — pages no other page links to (excludes `index`/`log`/`README`
  **and** `Archived`/`Superseded` pages)
- `broken_links` — `[[link]]` targets that don't exist in `wiki/`
- `missing_pages` — de-duplicated broken-link targets, grouped by referrer
- `format_violations` — pages missing an H1 or `Summary`/`Sources`/`Last updated`
- `empty_pages` — pages with an empty/stub body (too short, or a placeholder
  marker like `Placeholder — add content` / `TBD — mentioned in`)
- `stale_pages` — `Last updated` older than `--stale-days` (default 90) AND a
  newer matching file exists in `raw/` (excludes retired pages)
- `unsourced_claims` — pages with no `Sources` or `needs verification` markers
- `status_pages` — pages carrying a `**Status**` field, with their
  `superseded_by` target
- `missing_sources` — `Sources` entries pointing at `raw/` paths that no longer
  exist on disk

Read the whole report before acting.

### Phase 2 — Automatic structural cleanup (no approval needed)

Apply these directly — they are safe and git-recoverable:

- **Empty pages** (`empty_pages`): delete the file. (If a page is empty only
  because it's a deliberate stub for an upcoming concept the user cares about,
  keep it — but default to deleting placeholder/TBD pages.)
- **Broken links** (`broken_links` / `missing_pages`): if the target is a real
  concept worth a page, create a proper page; otherwise remove the `[[ ]]`
  brackets (leave the plain text). Never leave a dangling wiki-link.
- **Orphans** (`orphans`): add an inbound link from a naturally-related page.
  If the page has no clear home and no lasting value, archive it (Phase 4
  status banner) rather than delete — unless it's also empty (already handled).
- **Format violations** (`format_violations`): add the missing H1 or
  `Summary`/`Sources`/`Last updated` block; use today's date for `Last updated`.
- **Missing sources** (`missing_sources`): the `raw/` file was renamed or
  removed. Fix the `Sources` path if you can identify the new name; otherwise
  mark that claim `(source: needs verification)`. Never recreate `raw/`.

`Archived`/`Superseded` pages are already excluded from orphan/stale results,
so they won't be re-flagged here.

### Phase 3 — Semantic pass (find conflicts and outdated knowledge)

Read the flagged pages plus their neighbors (via `edges`) and identify:

- **Contradictions**: two pages asserting different values for the same fact
  (a number, date, name, behavior). For each pair that co-occurs in the graph,
  read both fully and compare.
- **Outdated facts**: for each `stale_pages` entry, read the raw sources in its
  `Sources` block plus the newer `raw/` files listed in `newer_raw_files`; find
  claims the newer source contradicts. Determine which version is current.
- **Supersession candidates**: pages whose entire topic has been replaced by a
  newer page — these become `Superseded by [[...]]` in Phase 4.

Do NOT apply semantic changes yet — collect them for the Phase 4 report.

### Phase 4 — One report, then apply (the conflict gate)

Present a **single** consolidated report: what Phase 2 already cleaned
(informational) plus every proposed **conflict / outdated / supersession**
resolution as a numbered list. Then ask the user to approve or edit the set —
this is the one place lint waits for you.

```
Lint results for <wiki-root> (<page_count> pages):

ALREADY CLEANED (automatic):
  - Deleted 2 empty pages: [[stub-a]], [[stub-b]]
  - Fixed 3 broken links; linked 1 orphan ([[foo]] ← [[bar]])
  - Added missing "Last updated" to [[quux]]

NEEDS YOUR CONFIRMATION:
  1. CONFLICT — [[api-limits]] says "1000 req/min" but [[changelog-2026-05]]
     says "5000". Newer source (raw/changelog-2026-05.md) likely current.
     Proposed: update [[api-limits]] to 5000, cite the newer source.
  2. OUTDATED — [[onboarding]] (2025-11) contradicted by raw/PROJ-800.md
     (2026-06). Proposed: refresh the activation-metrics section.
  3. SUPERSEDE — [[old-auth-design]] fully replaced by [[auth-v2]].
     Proposed: mark [[old-auth-design]] "Superseded by [[auth-v2]]" with a
     banner; tag it in index.md.
```

Use `AskUserQuestion` (or a plain numbered prompt) to collect decisions in one
pass. After approval, apply:

- **Conflicts / outdated**: rewrite the wiki page to reflect the current
  source, cite it inline `(source: raw/<file>)`, bump `Last updated`. When two
  sources genuinely disagree and the user hasn't picked a winner, note the
  contradiction explicitly per the citation rules.
- **Supersession / archival**: add the `Status` field + banner blockquote at
  the top of the retired page (see
  [../ingest/references/page-format.md](../ingest/references/page-format.md)
  "Status field"). **Never edit `raw/`.** The banner is the first body line so
  future reads route to the current page.

### Phase 5 — Update index.md and log.md

- **`wiki/index.md`**: drop deleted pages; retag archived/superseded pages,
  e.g. `[[old-auth-design]] — *(superseded by [[auth-v2]])*`.
- **`wiki/log.md`**: append ONE entry (newest at top), header
  `## <YYYY-MM-DD> (lint)`, bullets covering deletions, archives/supersessions,
  conflict resolutions, and structural fixes, with wiki-links to affected
  pages. `log.md` is append-only — never edit past entries. See the log format
  in [../ingest/references/page-format.md](../ingest/references/page-format.md).

### Phase 6 — Grouped commit + push

Commit per category so history is readable, then push once. Stage only the
paths each commit touches:

```bash
# one commit per non-empty category (skip categories with no changes):
git -C "${WIKI_ROOT}" add <deleted+empty paths> \
  && git -C "${WIKI_ROOT}" commit -m "lint: remove N empty/orphan pages"
git -C "${WIKI_ROOT}" add <archived page paths> \
  && git -C "${WIKI_ROOT}" commit -m "lint: archive M superseded pages"
git -C "${WIKI_ROOT}" add <conflict-fix paths> \
  && git -C "${WIKI_ROOT}" commit -m "lint: resolve K conflicts / outdated facts"
git -C "${WIKI_ROOT}" add wiki/index.md wiki/log.md \
  && git -C "${WIKI_ROOT}" commit -m "lint: update index + log"

# then push all commits in one shot (gated on auto_push):
${WIKI_PY} "${SKILL_DIR}/../ingest/scripts/ingest.py" \
  --wiki-root "${WIKI_ROOT}" --push-only
```

Committing is gated on `auto_commit` and pushing on `auto_push` in
`.wikirc.json` (both default to the user's config). `--push-only` reuses
ingest's `git_push` — push failures warn but never fail the lint; local commits
are always preserved. Credential resolution is delegated to Git (SSH key,
macOS Keychain, `git-credential-store`).

If `auto_commit` is false, leave the changes staged and tell the user.

## `lint.py` options

```
${WIKI_PY} lint.py --wiki-root PATH [--stale-days DAYS] [--sources]
```

- `--stale-days DAYS` — Threshold in days for stale detection (default 90).
- `--sources` — Also include raw/ scan metadata for the semantic pass.

## Design notes

- The linter never modifies files. All writes happen in Phases 2/4/5 through
  your editing tools. `raw/` is never touched by any phase.
- `index.md`, `log.md`, `README` are exempt from the orphan check; so are
  `Archived`/`Superseded` pages (intentionally retired).
- Stale detection is a heuristic (raw/ mtime vs the `Last updated:` line) — use
  it to decide which pages to read in Phase 3, then confirm real conflicts with
  the user before rewriting.
- Contradictions and outdated facts are semantic — `lint.py` only surfaces the
  pages to look at; you make the call and the user confirms.
- Archival is by **status banner**, not deletion — historical pages stay in git
  and in `index.md` (tagged), but readers are always redirected to the current
  page.
