---
name: lint
description: |
  Thoroughly cleans up an LLM wiki. A deterministic script applies the
  judgment-free fixes (deletes placeholder stub pages and unlinks links to
  them, repairs a `Sources` path whose file was renamed) and shortlists what
  needs judgment: likely contradictions between pages (fact collisions),
  near-duplicate pages, pages whose cited `raw/` sources changed after the
  page was last written (source drift), orphans, broken links, short pages,
  format violations, and empty uncited `raw/` files. Every semantic change —
  merges, deletions of real pages, rewrites, archiving, raw-file pruning — is
  batched into one report the user approves before anything is applied.
  Retired pages are archived by moving them to `wiki/archive/`. Remembers
  reviewed verdicts in `wiki/.lint-reviewed.json`, updates `wiki/index.md` and
  `wiki/log.md` by script, then commits (grouped, every message starting with
  `lint:`) and pushes.

  Use this skill whenever the user wants to lint, audit, review, clean up,
  or check the health of their wiki. Trigger on phrases like: "lint the
  wiki", "audit the wiki", "check for contradictions", "find duplicate
  pages", "clean up broken links", "review my wiki pages", "check what's
  outdated", "archive outdated pages", or "run lint".
---

# Lint — LLM Wiki

These rules take precedence over workflow text in the wiki's `CLAUDE.md`.
A wiki bootstrapped before this version may still describe an older lint
there; follow this file.

Goal: keep the knowledge base clean so future reads always surface the
**latest** information. The split of work is deliberate. `lint.py` does what
needs no judgment, so it is cheap and repeatable; it then hands you a short
list of candidates instead of the whole wiki, so you read only the pages
that matter. You judge those candidates, and the user approves every change
that alters meaning in **one** report — so lint never deletes knowledge,
merges pages or rewrites a claim behind the user's back. `raw/` file
*contents* are never edited; empty, uncited raw files may be deleted, but
only after approval.

## Prerequisites

- The wiki has a `.wikirc.json` at its root.
- The wiki root is a git repository. Lint reads git history (to find drift
  and what changed since the last lint) and its fixes are reviewed and
  reverted through git.
- Python 3. `lint.py` and `wiki_tools.py` are stdlib only.

## Resolving the Python interpreter

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
`${LINT}` is `${SKILL_DIR}/scripts/lint.py`, `${WIKI_TOOLS}` is
`${SKILL_DIR}/scripts/wiki_tools.py`, and `${INGEST_PY}` is
`${SKILL_DIR}/../ingest/scripts/ingest.py` (used only to push).

## Workflow

### Step 1 — Run the linter with its automatic fixes

Start from a clean tree: `git -C "${WIKI_ROOT}" status --porcelain` should
show nothing under `wiki/`. On a clean tree every change lint makes is its
own reviewable diff, and the `lint:` commits at the end hold only lint's
work, never the user's unfinished edits.

```bash
${WIKI_PY} "${LINT}" --wiki-root "${WIKI_ROOT}" --fix --since-last-lint
```

`--since-last-lint` limits the expensive comparisons (overlap, collisions,
drift) to pages and raw files changed since the newest commit whose message
starts with `lint:`, so a routine lint stays fast and does not re-raise what
an earlier lint already settled. Structural checks always cover the whole
wiki. On the first lint there is no such commit; the script then checks
everything and says so in `warnings`. Use `--full` instead of
`--since-last-lint` when the user asks for a full audit.

**When `--fix` refuses** (exit code 2, nothing written, the reason on
stderr):

- *Uncommitted changes under `wiki/`* — stderr lists them. Stop and show the
  user the list: they are the user's work, and lint must not mix its edits
  into them or commit them under a `lint:` message. Ask them to commit (or
  discard) those changes, then run Step 1 again. Pass `--allow-dirty` only
  if the user explicitly says to go ahead anyway; then stage only lint's own
  paths in Step 7.
- *Not a git work tree* — lint cannot make reviewable fixes there. Run
  `lint.py` without `--fix` for a report only, show it, and suggest
  `git init` (or `/create-wiki`) before a real lint.

### What `--fix` already did

`--fix` applies only changes that need no judgment, and lists them under
`fixed`:

- `deleted_pages` — stub pages: an explicit placeholder marker
  (`Placeholder — add content`, `TBD — mentioned in`) outside code and
  blockquotes, and almost nothing else once the marker, header, headings and
  `Related pages` section are set aside. A page that merely mentions such a
  phrase is not a stub. Never a short page with real content, a retired
  page, `index`/`log`/`README`, or a journal page.
- `unlinked` — `{page, target}`: brackets removed from links to the pages
  deleted in this same run (the text stays). Links inside code or embeds are
  left alone.
- `removed_index_lines` — `index.md` list lines whose only link was to a
  deleted page (a link inside inline code does not count; such a line stays).
- `repaired_sources` — `{page, from, to}`: a missing `Sources` path
  repointed to the one `raw/*.md` with exactly the same filename stem.

Everything else in the report is **not** fixed; it is yours to judge.

### The report

JSON on stdout:

| Key | What it holds | Your job |
|-----|---------------|----------|
| `fact_collisions` | `{pages, sentences, differing}`: two sentences, one per page, that say the same thing with different numbers or dates (`differing` = the tokens each side has that the other lacks) | Step 2 |
| `overlap_candidates` | `{pages, score, shared_sources, shared_links, slug_sim, text_sim}`: page pairs that look like duplicates, best first | Step 2 |
| `source_drift` | `{page, drifted: [{raw, page_commit, raw_commit, uncommitted}]}`: a cited raw file changed after the page was last written | Step 2 |
| `orphans` | pages no content page links to (links from `index`/`log` do not count) | Step 3 |
| `empty_pages` | `{page, reason, char_count}`; after `--fix` these are pages with a short body but some real text | Step 3 |
| `broken_links` / `missing_pages` | `{from, to}` links to pages that do not exist, and the same grouped by target | Step 3 |
| `missing_sources` | `{page, missing}`: `Sources` paths not on disk that `--fix` could not repair | Step 3 |
| `format_violations` | `{page, missing}`: no H1, or no `Summary` / `Sources` / `Last updated` | Step 3 |
| `archive_links_without_banner` | `{from, to, archived_slug}`: links into `wiki/archive/` whose target has no `Archived`/`Superseded` banner — usually a link someone forgot to repoint after archiving | Step 3 |
| `unsourced_claims` | pages with no `Sources` or with `(source: needs verification)` markers | Step 3, if the user wants it |
| `status_pages` | pages with a `**Status**` field | context |
| `empty_raw` | `{slug, reason, char_count, companions, has_images_dir}`: raw files with an empty body that no page cites — prune candidates | Step 4 |
| `fixed` | what `--fix` did (above) | Step 4 |
| `suppressed` | `{pairs, collisions, drift}`: how many items were left out because an earlier review settled them | context |
| `warnings` | anything the run could not do fully | always read |

Read `warnings` every time. They say when results were cut short —
`fact_collisions: showing 50 of …`, `overlap: skipped hub key … shared by …
pages`, `source_drift skipped: …`, an unreadable `.lint-reviewed.json` — and
when the wiki's `CLAUDE.md` has no managed block (then suggest the
`bootstrap.py --upgrade` command it names). A truncated list means more
exists than you were shown: say so in the report, and never call the wiki
clean while such a warning is present. Collisions are capped at 50 (exact
matches shared by the fewest pages first) and overlap pairs at
`--max-candidates` (default 20); a later lint shows the rest once these are
settled.

### Step 2 — Judge the shortlist

Do not apply anything yet; collect proposals for Step 4.

- **Each `fact_collisions` entry**: decide from the two quoted sentences —
  open the pages only when the sentences alone are unclear. Is it a real
  contradiction (the same fact with two values), or two different facts
  that happen to read alike (two products, two dates in a timeline)? For a
  real one, check each page's cited sources and their `source_date` in
  `raw/<slug>.source.json`: propose the newer source's value and say why.
  Without dates, say which source looks current and why, or that you cannot
  tell.
- **Each `overlap_candidates` pair**: read both pages and label it
  *duplicate* (one topic, two pages — propose a merge), *partial overlap*
  (propose moving the shared part to the page it belongs on and linking), or
  *fine* (related but distinct).
- **Each `source_drift` page**: read what changed in each drifted raw file —
  `git -C "${WIKI_ROOT}" diff <page_commit> -- <raw>` shows every change
  since the page was last written, committed or not (for a raw with no
  `raw_commit`, read the whole file) — and list the page's claims it
  invalidates, with the rewrite you propose. If nothing on the page is
  affected, it needs no rewrite; record it in Step 5 so it stops coming back.

### Step 3 — Propose what to do with the rest

- **Orphans**: read the page and its neighbours, then propose one action:
  *integrate* (preferred when it holds value: fold its content into the most
  relevant page, or link to it from a page where it naturally belongs),
  *archive* (outdated or superseded but worth keeping as history), or
  *delete* (a duplicate, or unsourced with no lasting value).
- **Short pages** (`empty_pages`, reason `body too short`): they have some
  real text, which is why `--fix` left them. Propose delete, merge into the
  page where the text belongs, or keep.
- **Broken links**: propose, per link, creating the page (only when a source
  has enough on the topic — no stubs), removing the brackets (the text
  stays), or fixing the target (a typo or a case difference).
- **Missing sources**: propose the right path when you can find the renamed
  file, otherwise marking the claim `(source: needs verification)`. Never
  recreate a raw file.
- **Format violations**: propose the missing H1 or fields (`Last updated` =
  today).
- **Archive links without a banner**: propose repointing the link to the
  page that replaced the archived one, or adding the `**Status**` banner to
  the archived page.

### Step 4 — One approval report

Present everything in a **single** report and ask the user to approve or
edit it — this is the only place lint waits. Nothing semantic is applied
before this report is approved: no merge, no deletion of a page with real
content, no rewrite of a claim, no archiving, no raw prune, no structural
edit from Step 3.

```
Lint results for <wiki-root> (<page_count> pages, <archived_count> archived):

ALREADY FIXED (--fix):
  - Deleted 2 placeholder pages: stub-a, stub-b (unlinked 3 links, 1 index line)
  - Repaired Sources path in [[setup]]: raw/old-name.md -> raw/new-name.md

NOTE: fact_collisions showed 50 of 73 found — the rest come up on the next lint.

NEEDS YOUR APPROVAL:
  1. CONTRADICTION — [[api-limits]] "1000 req/min" vs [[changelog]] "5000 req/min".
     raw/changelog-2026-05.md is newer (2026-05-12 vs 2025-11-03).
     Proposed: change [[api-limits]] to 5000, cite raw/changelog-2026-05.md.
  2. MERGE — [[auth-flow]] and [[login-flow]] describe the same flow.
     Proposed: fold login-flow's unique section into auth-flow, repoint links,
     archive login-flow.
  3. DRIFT — [[onboarding]]: raw/PROJ-800.md now says activation is day 3, not 7.
     Proposed: rewrite the activation paragraph.
  4. ORPHAN — [[old-notes]]: superseded by [[auth-v2]]. Proposed: archive.
  5. SHORT PAGE — [[glossary-x]] (22 chars). Proposed: merge into [[glossary]].
  6. BROKEN LINK — [[Api-Limits]] in [[faq]]. Proposed: fix case to [[api-limits]].
  7. RAW PRUNE — 12 empty, uncited raw files (e.g. raw/2025.md, raw/wip.md).
     Proposed: delete each one's .md + .source.json (+ images dir).

JUDGED FINE (recorded so they do not come back):
  - [[pricing]] / [[pricing-faq]]: related, not duplicates
```

For a large `empty_raw` set, give the count with a sample, and make clear the
whole set is deleted as a group on approval. Use `AskUserQuestion` (or a
numbered prompt) to collect all decisions in one pass.

### Step 5 — Apply what was approved, then record the verdicts

- **Contradictions and drift**: rewrite the claim in place, cite
  `(source: raw/<file>)`, bump `Last updated`. Do not leave the old value
  next to the new one.
- **Merge**: fold the unique content of one page into the surviving page
  (with its citations and `Sources`), repoint every `[[link]]` to the merged
  page at the survivor, then archive the merged page with
  `git mv wiki/<slug>.md wiki/archive/<slug>.md`.
- **Archive**: `git mv wiki/<slug>.md wiki/archive/<slug>.md`, and repoint
  inbound links to the replacement (or to `[[archive/<slug>]]` when there is
  none). Archived pages leave active lint scope but stay linkable. The
  `**Status**` banner (see
  [../ingest/references/page-format.md](../ingest/references/page-format.md))
  is still supported; the folder move is the primary mechanism.
- **Delete** an approved page with `git rm`.
- **Step 3 items**: apply the approved choices.
- **Raw prune** (`empty_raw`): for each approved slug,
  `git -C "${WIKI_ROOT}" rm -r raw/<slug>.md raw/<slug>.source.json raw/images/<slug>`
  (omit paths that do not exist; the images dir only when `has_images_dir`).
  This is the only deletion under `raw/`; lint never edits raw contents and
  never deletes a non-empty or cited raw. A Slack raw's message window will
  not be re-fetched on the next ingest (its watermark stays), so prune a
  Slack raw only when it is genuinely empty.

Then record what was settled without a change, so the next lint does not
raise it again:

```bash
${WIKI_PY} "${LINT}" --wiki-root "${WIKI_ROOT}" --mark-reviewed <<'EOF'
{
  "pairs":      [{"pages": ["pricing", "pricing-faq"], "verdict": "fine"}],
  "collisions": [{"pages": ["plan-a", "plan-b"],
                  "sentences": ["<first sentence, exactly as reported>",
                                "<second sentence, exactly as reported>"],
                  "verdict": "fine"}],
  "drift":      [{"page": "onboarding", "raw": "raw/PROJ-800.md"}]
}
EOF
```

- `verdict` is `fine` when you judged the item needs no action, `rejected`
  when the user turned down your proposal.
- Record every drift entry you handled — rewritten or found unaffected.
  Lint's own `lint:` commits never count as the page catching up with its
  source (so a lint link-fix cannot hide real drift), which means a drift
  rewrite made by lint only clears once it is recorded here.
- Record collisions only while both sentences still stand as reported; a
  collision you resolved by editing a sentence is gone already. Pairs and
  pages that were merged, archived or deleted need no entry.
- Run it **after** the Step 5 edits: an entry stores a hash of the current
  page (or raw file) content and stops matching as soon as that content
  changes — so an edit made later would cancel the verdict, which is the
  point: a changed page deserves a fresh look.
- It prints `{"recorded": N, "pruned": M}` and writes
  `wiki/.lint-reviewed.json`. On a bad entry — a page or raw file that does
  not exist, a verdict other than `fine`/`rejected`, sentences not found in
  those pages — it exits 1, writes nothing, and names the entry on stderr;
  fix or drop that entry and run it again.

### Step 6 — Re-run the linter to check the result

```bash
${WIKI_PY} "${LINT}" --wiki-root "${WIKI_ROOT}" --since-last-lint
```

No `--fix` this time. `broken_links`, `empty_pages`, `format_violations`
and `missing_sources` must all be empty, apart from items the user chose to
keep (name them in the final report). Anything else means an applied change
left a loose end — a link to a merged page, a moved page still cited by its
old name — so fix it before committing.

### Step 7 — Index, log, grouped commits, push

```bash
${WIKI_PY} "${WIKI_TOOLS}" index-sync --wiki-root "${WIKI_ROOT}"
${WIKI_PY} "${WIKI_TOOLS}" log --wiki-root "${WIKI_ROOT}" --op lint \
  --title "Lint: <one-line summary>" \
  --bullet "Deleted: [[a]], [[b]]" --bullet "Archived: [[c]]" \
  --bullet "Resolved: [[api-limits]] rate limit (5000, raw/changelog-2026-05.md)"
```

`index-sync` drops lines for deleted pages, moves archived pages under
`## Archive` and lists any new page under `## Uncategorized`, leaving every
other line alone (an entry with nested lines under it is left for you and
named on stderr). `log` writes one `## YYYY-MM-DD (lint)` entry at the top of
`wiki/log.md` (repeatable `--bullet`). Do not edit either file by hand.

Then commit by category, each message starting with `lint:` — that prefix
is how the next `--since-last-lint` finds where this lint ended. Stage only
the paths each commit touches (`git add -A <paths>` stages deletions and
moves too) and skip empty categories:

```bash
git -C "${WIKI_ROOT}" add -A <paths from fixed> \
  && git -C "${WIKI_ROOT}" commit -m "lint: automatic fixes (N stub pages, M links, K sources)"
git -C "${WIKI_ROOT}" add -A wiki/archive <merged/archived/deleted page paths> <link-repoint paths> \
  && git -C "${WIKI_ROOT}" commit -m "lint: merge/archive/delete N pages"
git -C "${WIKI_ROOT}" add -A <rewritten page paths> \
  && git -C "${WIKI_ROOT}" commit -m "lint: resolve K contradictions / drift"
git -C "${WIKI_ROOT}" add -A <structural fix paths> \
  && git -C "${WIKI_ROOT}" commit -m "lint: fix links, sources and format"
git -C "${WIKI_ROOT}" add -A raw \
  && git -C "${WIKI_ROOT}" commit -m "lint: prune P empty raw files"
git -C "${WIKI_ROOT}" add wiki/index.md wiki/log.md wiki/.lint-reviewed.json \
  && git -C "${WIKI_ROOT}" commit -m "lint: update index, log and review memory"

${WIKI_PY} "${INGEST_PY}" --wiki-root "${WIKI_ROOT}" --push-only
```

`wiki/.lint-reviewed.json` is committed so verdicts survive a fresh clone
and are shared by everyone using the wiki. Every lint run that changes
anything ends with at least one `lint:` commit. Committing follows the
user's `auto_commit` setting (if it is false, leave the changes staged and
say so) and `--push-only` pushes only when `auto_push` is true; a failed
push warns but never undoes the local commits.

Finish with a short summary: what was fixed automatically, what was applied,
what was judged fine, what the user kept, and any truncation warning.

## `lint.py` options

```
${WIKI_PY} lint.py --wiki-root PATH [--full | --since-last-lint] [--fix [--allow-dirty]]
                   [--max-candidates N] [--graph] [--sources] [--wiki-dir DIR] [--raw-dir DIR]
${WIKI_PY} lint.py --wiki-root PATH --mark-reviewed < verdicts.json
```

- `--full` (default) / `--since-last-lint` — scope of the overlap, collision
  and drift checks.
- `--fix` — the judgment-free fixes above; refuses (exit 2) outside a git
  work tree or with uncommitted changes under the wiki dir unless
  `--allow-dirty`.
- `--mark-reviewed` — record verdicts from stdin; runs no checks.
- `--max-candidates N` — cap on `overlap_candidates` (default 20).
- `--graph` — add the wiki-link graph (`edges`, `inbound_counts`).
- `--sources` — add a scan of `raw/` files (`raw_sources`).
- `--wiki-dir` / `--raw-dir` — override the subdirectory names (default from
  `.wikirc.json`, else `wiki` / `raw`).

Without `--fix` or `--mark-reviewed`, `lint.py` writes nothing.

## Design notes

- **Exempt pages**: `index`, `log` and `README` get no per-page checks, and
  links from them do not count as inbound links — a page reachable only from
  the index is still an orphan.
- **Journal pages** — `wiki/YYYY-MM-DD.md` day-pages and
  `wiki/YYYY-MM-index.md` monthly rollups, written by `/log` — are an
  append-only record, so lint never judges or edits them: they are left out
  of orphans, overlap, collisions, drift, empty pages and format checks, and
  `--fix` never writes to them. Their links still count as inbound links,
  and their own broken links are reported (report only). `index-sync` never
  adds, removes or moves them and never touches the `## Diary` section.
- **Links**: a `#anchor` suffix is ignored when resolving
  (`[[page#Heading]]` → `page`). `[[label/slug]]` — a `/` whose first part is
  not `archive` — is a cross-wiki link into a linked wiki; it is never
  reported as broken and never unlinked. Links to archived pages
  (`[[old]]` or `[[archive/old]]`) resolve.
- **Source drift** compares git history: a page drifts when a raw file it
  cites was committed after the page's last commit, ignoring `lint:`
  commits. A commit that touches both (the normal ingest commit) is not
  drift. A cited raw with uncommitted changes always counts. Not a git repo →
  no drift check, with a warning.
- **Matching is character-based**, so collisions and overlap work in any
  language. Sources may be cited as `raw/<file>` or as a bare filename that
  exists in `raw/`.
- **Retired pages** (`**Status**: Archived` / `Superseded by [[...]]`) and
  everything in `wiki/archive/` are out of active scope: not orphans, not
  compared, not drifted. Archived slugs remain valid link targets.
- `raw/` file *contents* are never edited. The only change lint makes under
  `raw/` is deleting approved `empty_raw` files. `empty_raw` is conservative:
  empty body **and** cited by no page (Sources, inline image or prose).
