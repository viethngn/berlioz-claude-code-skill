# llm-wiki lean rework — design

Date: 2026-11-20
Scope: `plugins/llm-wiki/` (lint skill, ingest skill, create-wiki templates, docs, tests)

## Intent

The wiki does not stay clean. Lint does not detect overlapping, outdated or
contradicting content, and ingest creates that content in the first place.
The owner wants a wiki that stays lean and correct while using the model as
little as possible.

Guiding rule: **anything that can be done mechanically is done by a script.
The model is used only for reasoning and for writing wiki prose.** Quality must
not drop as a result.

## Goals

1. Lint detects overlap, outdated pages and contradicting facts, and gets them
   resolved with user approval.
2. Ingest updates existing pages instead of creating near-duplicates, and
   replaces old claims when a source changes.
3. Detection, bookkeeping (`index.md`, `log.md`) and mechanical fixes are done
   by scripts.
4. No model configuration for the user. The plugin never names a model and
   calls no external model endpoint. Everything the model does is done by the
   session model.
5. Image description is off by default and, when on, is done by the session
   model reading the image.

## Non-goals

- Topic-grouped bulk synthesis (grouping items by Confluence parent / Jira
  epic before writing). Deferred.
- Embedding-based similarity. It needs a model endpoint.
- Structured per-page fact blocks. Rejected, see Approaches.
- Changing the raw layout, the fetchers' diff gates, the bulk queue, rate
  limiting for Atlassian/Slack, or the archive-by-move mechanism.
- Migrating existing wikis. They must keep working unchanged.

## Anti-goals

- A lint that reports "clean" because the model sampled a few pages.
- More model work per ingest than today.
- Destructive semantic changes (merging, deleting non-empty pages) applied
  without the user's approval.
- New third-party Python dependencies. `lint.py` and the new bookkeeping
  script stay stdlib-only.

## Constraints

- Python stdlib only for lint and bookkeeping scripts. Git is available and
  the wiki root is a git repository (already a prerequisite).
- Existing wikis: flat `wiki/*.md` pages plus `wiki/archive/`, page header
  fields `**Summary**`, `**Sources**`, `**Last updated**`, optional
  `**Status**`. No new required page fields.
- A leftover `nano_banana` block in an existing `.wikirc.json` must be ignored
  without error.
- Scripts report and fix; they never edit `raw/` file contents.

## Approaches

**A. Script shortlist, model verdict — chosen, combined with C.**
Pages stay free-form prose. `lint.py` finds suspects mechanically; the session
model judges only that shortlist. Works on existing wikis with no migration.
Weakness: a contradiction phrased very differently on two pages can be missed.

**B. Structured facts on every page.** A facts block per page lets scripts
detect conflicts with no model. Rejected: ingest must extract and maintain the
facts on every write (more model work, not less), existing wikis need a
migration, and non key/value knowledge is still missed.

**C. Prevent at ingest only.** Ingest finds existing pages first and rewrites
on changed sources; lint gets bug fixes only. Rejected on its own because it
never cleans up what is already in a wiki — but all of it is included in the
chosen design.

The owner picked A with C's ingest prevention included.

## Design

### 1. `lint.py` (skills/lint/scripts/lint.py)

Existing checks stay. Changes:

**Orphan fix.** Links from `index` and `log` do not count as inbound links.
`index`, `log`, `README` are also excluded from `format_violations` and
`unsourced_claims`.

**Source drift replaces the mtime stale check.** For each active page, take
the `raw/` paths in its `Sources` block. The page is drifted when any cited
raw file's last git commit is newer than the page's last git commit. Report
entry: page, the drifted raw paths, and for each the commit range so the skill
can show `git diff <page-commit>..HEAD -- raw/<file>`. Uncommitted/untracked
files: treat a modified or untracked cited raw as drifted; an untracked page
is never drifted. If the wiki root is not a git repo, `source_drift` is an
empty list and the report carries a `warnings` entry. `--stale-days` and
`stale_pages` are removed. A commit that touches both the page and the raw
(the normal ingest commit) is not drift.

**Overlap candidates (`overlap_candidates`).** For every pair of active pages,
compute a score from:
- Jaccard overlap of cited raw paths,
- Jaccard overlap of outbound wiki-links,
- slug similarity (`difflib.SequenceMatcher` on slugs),
- body text similarity (`difflib.SequenceMatcher` on normalized body, only
  computed for pairs that already share a source, a link, or a slug token, to
  keep the run fast).
Emit the top N pairs (default 20, `--max-candidates`) above a threshold, each
with the component scores and the reason. Weights and thresholds are left to
the builder; they must be constants at the top of the file and the planted
fixture cases must be caught.

**Fact collisions (`fact_collisions`).** Among pairs that share a source, a
link, or a slug token: split bodies into sentences, and report sentence pairs
with high word overlap (numbers and dates removed before comparing) whose
numeric/date tokens differ. Each entry: both pages, both sentences verbatim,
the differing tokens. This is what catches "1000 req/min" vs "5000 req/min"
without a model.

**`--fix`.** Applies only fixes that need no judgment, then prints the report
of what remains plus a `fixed` section:
- delete empty/stub pages (existing `empty_pages` rule), and strip the
  brackets of links that pointed at them,
- strip brackets from broken links (leave the text),
- repair a `Sources` path when exactly one raw file matches the missing
  path's slug.
Without `--fix`, lint.py never writes (unchanged behaviour).

**`--since-last-lint`.** Limits `overlap_candidates`, `fact_collisions` and
`source_drift` to pairs/pages where at least one side changed since the most
recent commit whose message starts with `lint:`. If there is no such commit,
behaves as `--full`. Structural checks always cover the whole wiki. `--full`
is the default when neither flag is given.

**Compact output.** `edges` and `inbound_counts` are emitted only with
`--graph`.

**Language-agnostic matching (author's call).** Wiki content may not be
English. Similarity is computed on characters (`difflib` over normalized
strings), not whitespace word tokens. Sentences split on `. ! ? 。 ！ ？` and
newlines. `locate` uses case-insensitive substring matching. The fixture
includes one non-English (CJK) collision case.

**Tolerant source parsing (author's call).** Existing wikis may cite sources
as bare filenames (`api-guide.md`) instead of `raw/api-guide.md`. Drift,
shared-source pairing and `citing` resolve a bare filename to `raw/<name>`
when that file exists.

**Review memory (author's call): `wiki/.lint-reviewed.json`, committed.**
Without it, pairs judged "fine" come back on every run and crowd real
duplicates out of the top N. Entries:
- pair verdicts: sorted page pair + content hash of both pages (for a fact
  collision: hash of the two sentences) + verdict (`fine` or `rejected`),
- drift acknowledgements: page + raw path + hash of the raw file content.
`lint.py` drops any candidate, collision or drift entry that matches a stored
entry whose hashes still match; a change to either page (or to the raw file)
invalidates the entry automatically. `lint.py --mark-reviewed` takes the
entries to record as JSON on stdin and writes the file; the skill calls it
after step 4 for everything judged fine, rejected by the user, or drift with
nothing to rewrite. Stale entries for deleted pages are pruned on write.

**Drift and mechanical edits (author's call).** When computing a page's last
commit, commits whose message starts with `lint:` are ignored, so lint's own
link fixes do not hide unreviewed drift. Known limitation, accepted: an ingest
of a different source that edits the same page does reset that page's drift
for other sources.

### 2. Bookkeeping script (new: skills/lint/scripts/wiki_tools.py)

Stdlib only. Shared by both skills; reuses `lint.py`'s parsing helpers by
import. Subcommands, all taking `--wiki-root`:

- `locate TERM [TERM...]` — rank active pages by matches in slug/H1 (highest),
  Summary, then body. Output: JSON list of `{page, summary, score}`, top 10.
- `citing RAW_SLUG` — pages whose text references `raw/<slug>.*` or
  `raw/images/<slug>/`. Reuses `cited_raw_slugs` logic.
- `index-sync` — make `wiki/index.md` consistent with the pages on disk:
  add each missing active page as `- [[slug]] — <Summary>` under an
  `## Uncategorized` section (created if needed), remove entries for pages
  that no longer exist, move entries for pages now in `wiki/archive/` under
  `## Archive`. Existing sections, ordering and hand-written descriptions are
  left untouched.
- `log --op ingest|lint --title TEXT [--bullet TEXT ...]` — insert a
  `## YYYY-MM-DD` (or `## YYYY-MM-DD (lint)`) entry at the top of
  `wiki/log.md`, below the H1.

### 3. Ingest scripts

**Images off by default.** New config key `images.describe` (default
`false`) and CLI flag `--describe-images` on `ingest.py` (applies to single
and bulk). When off: no image download, no manifest work; `image_hints` stay
in `source.json` so a later run with the flag can pick them up. Local image
files ingested directly as a source are unaffected by the download skip (they
are already on disk) but are likewise only described when the flag/config is
on.

**No external endpoint.** Delete `describe_image.py`. Remove `nano_banana`
from `config.py` defaults/accessors/redaction, `wikirc.example.json`,
`rate_limiter.py` references, `_deps.py` (`google.genai`, `PIL`),
`requirements.txt` (`google-genai`, `pillow`), `bootstrap.py`, and the smoke
test. When description is on, `do_image_diff_loop` no longer describes: it
classifies via the manifest and returns `pending_images` — a list of
`{image_path, description_path}` for `new`/`changed` images. A new
`image_manifest.py mark-described --slug S IMAGE...` subcommand records the
sha and `described_at` after the model has written the description. Until
marked, an image stays pending, so an interrupted run resumes correctly.

**Locate and change context in the ingest summary.** `ingest.py`'s JSON
summary (single) and each bulk item's synthesis input gain:
- `related_pages` — `wiki_tools.locate` run on the source title and its
  Markdown headings,
- for `status == "changed"`: `citing_pages` (`wiki_tools.citing`) and
  `raw_diff` (the staged diff of `raw/<slug>.md`, truncated to a sane size
  with a note when truncated).
For bulk, where prefetch runs long before synthesis, these are produced by a
small `ingest.py --context --slug S` call made at synthesis time. In that
mode "changed" and `raw_diff` are derived from the git history of
`raw/<slug>.md` (author's call): the file is "changed" when it has an earlier
committed version, and the diff is between that version and the current
content. The bulk queue format is not changed.

**Image backfill (author's call).** `--describe-images` acts only on sources
that are new or changed in that run, or on any source when combined with
`--force`. It does not by itself reopen unchanged sources; the unchanged
early-return in `ingest.py` and `prefetch.py` stays as it is.

**Uniform source date.** Every `source.json` carries `source_date` (ISO
date): Confluence `version.when`, Jira `fields.updated`, Slack end of the
message range, local file: the file's mtime at copy time. This is
source-side time, not fetch time, so it does not break the "no wall-clock
timestamps" rule for stable diffs.

**Bulk.** If the rendered Markdown body of an item is empty/stub (same rule
as `lint.py`'s empty check), prefetch marks it skipped and writes no raw
file. Synthesis commits per item as today but pushes once at the end of the
job.

### 4. Skill and template text

**Ingest `SKILL.md`, template `CLAUDE.md`, `page-format.md`:**
- Phase 3 starts from `related_pages`: read them, update them by default.
  Create a new page only when the topic has no existing home and the source
  has enough substance for a page of its own.
- No summary page per source. Remove "10-15 pages is normal" and "split into
  5-10 focused pages".
- No stub pages. Mention a concept in plain text until a real page exists.
- Changed source: read `raw_diff`, open `citing_pages`, rewrite the affected
  claims in place. Do not append a second version of a claim.
- Conflict at ingest: when the new source contradicts an existing claim from
  a different source, the claim from the source with the later `source_date`
  wins and replaces the old one, keeping the citation. If dates are equal or
  missing, keep both with an explicit note; lint will surface it.
- One citation format everywhere: `(source: raw/<file>)`.
- `index.md` and `log.md` are updated by `wiki_tools.py index-sync` / `log`,
  not by hand. A new page may then be moved from `Uncategorized` to a fitting
  section if one obviously exists.
- Image step (only when on): for each `pending_images` entry, read the image,
  write the description file using the prompt structure from the old
  `DEFAULT_PROMPT` (type, visible text, structure, key facts), then call
  `mark-described`. If the session model cannot read images, report them as
  skipped and continue.

**Lint `SKILL.md`:**
1. `lint.py --fix --since-last-lint` (or `--full` when the user asks for a
   full audit, and on the first run).
2. Judge the shortlist: for each `fact_collisions` entry decide from the
   quoted sentences (open the pages only if unclear); for each
   `overlap_candidates` pair read both pages and label it duplicate /
   partial overlap / fine; for each `source_drift` page read the raw diff and
   list the claims it invalidates.
3. Remaining orphans: propose integrate / archive / delete.
4. One approval report covering contradictions, merges, drift rewrites,
   orphan actions, and raw prunes. Nothing in it is applied before approval.
5. Apply approved items. Merge = fold unique content into the surviving page,
   repoint links, archive the other by `git mv` to `wiki/archive/`.
6. Re-run `lint.py` (no `--fix`); structural sections must be empty.
7. `wiki_tools.py index-sync`, `wiki_tools.py log --op lint`, then the
   grouped commits and push as today. Every lint run that changes anything
   produces at least one commit whose message starts with `lint:`.

**Existing wikis' own `CLAUDE.md` — upgrade command (owner's decision).**
Each existing wiki has a bootstrap-time copy of the template `CLAUDE.md` that
still carries the old rules (summary page per source, 10-15 pages, hand-edited
index/log, `(source: filename.md)`), and it is loaded every session.

- The template `CLAUDE.md` puts all plugin-owned workflow text inside a
  managed block (`<!-- llm-wiki:begin -->` … `<!-- llm-wiki:end -->`), and
  that text is slimmed to point at the skills rather than restate them. The
  title and Purpose stay outside the block.
- `bootstrap.py --upgrade --target <wiki-root>` rewrites an existing wiki's
  `CLAUDE.md`:
  - markers present: replace the block's contents with the current template
    block;
  - markers absent (pre-rework wiki): remove the template-owned sections by
    heading (`## Folder structure`, `## Ingest workflow`, `## Page format`,
    `## Citation rules`, `## Question answering`, `## Lint`, `## Rules`) and
    insert the managed block where the first of them was. Any other section,
    and the title/Purpose, are kept untouched.
  It is idempotent, touches no other file, prints the diff, and does not
  commit (author's call) — the user reviews it with git. `--upgrade` refuses
  to run when `CLAUDE.md` is missing.
- The `create-wiki` skill documents the command ("upgrade my wiki's
  CLAUDE.md").
- `lint.py` adds a `warnings` entry when the wiki's `CLAUDE.md` has no managed
  block, naming the upgrade command. Both `SKILL.md` files also state that
  their rules take precedence over workflow text in the wiki's `CLAUDE.md`,
  so a wiki that has not been upgraded yet still behaves correctly.
- Test: `tests/smoke_lint.py` (or a sibling) covers upgrade on an old-style
  file with one custom section (kept), and a second run producing no change.

**Docs:** `README.md`, `references/setup.md`, `references/local-files.md`,
`create-wiki/SKILL.md`, `wikirc.example.json` updated to match (no
nano-banana, images off by default, new lint behaviour).

### 5. Tests

- New `tests/smoke_lint.py` (stdlib, same style as the existing smoke test):
  builds a fixture wiki in a temp git repo and asserts: index-only page is an
  orphan; two pages with "1000"/"5000" produce a fact collision; two
  near-duplicate pages are an overlap candidate; a page whose cited raw was
  committed later shows in `source_drift`, and a page committed together with
  its raw does not; `--fix` deletes a stub and unlinks references to it;
  `index-sync` adds/removes/archives entries without touching existing
  sections; `locate` and `citing` return the expected pages.
- `tests/smoke_bulk_ingest.py` updated for the removed `nano_banana` config.

## Amendment 1 (rebase onto origin/main)

The design was written against a base 8 commits behind `origin/main`, which
adds web ingest, refresh-all and the `log` skill. The resulting decisions
(journal pages exempt from lint judgment, cross-wiki link handling, no model
name in cascade ingest, `source_date` excluded from change detection, empty
bulk items, push cadence kept per item, release bookkeeping) are recorded in
`docs/superpowers/plans/2026-11-20-llm-wiki-lean-rework-amendment-1.md` and
are part of this design. The "push once at the end" line under Bulk is
withdrawn. Sentence splitting: `. ! ?` end a sentence only before whitespace
or end of text; `。 ！ ？` and newline always do.

## Left to the builder

- Similarity weights, thresholds and sentence-splitting details, within the
  fixture requirements above.
- Internal structure of `wiki_tools.py` and how it imports from `lint.py`.
- Exact wording of the skill and template text.
- Truncation size for `raw_diff`.
- Order of implementation, as long as lint scripts and their test come first.
