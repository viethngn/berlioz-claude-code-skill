# Amendment 1 — rebase onto origin/main (fc0314f)

The design and plan were written against f3e9029. `origin/main` is 8 commits
ahead and adds web ingest, refresh-all, the `log` skill, fetcher hardening and
new smoke tests. The branch is rebased onto `origin/main`. This file corrects
the plan and records the decisions the design did not make. Where this file
and the plan disagree, this file wins. Line numbers refer to `origin/main`.

## Decisions (author's rulings unless marked)

**R1 — Journal pages.** The `log` skill writes day-pages `wiki/YYYY-MM-DD.md`
and monthly rollups `wiki/YYYY-MM-index.md`. They are a *journal* page class,
recognised by slug: `^\d{4}-\d{2}-\d{2}$` and `^\d{4}-\d{2}-index$`
(`is_journal_slug(slug) -> bool` in `lint.py`). Journal pages are an
append-only record, so lint never edits or judges them:
- excluded from `orphans`, `overlap_candidates`, `fact_collisions`,
  `source_drift`, `empty_pages`, `format_violations`, `unsourced_claims`;
- `--fix` never writes to a journal page (it may still delete nothing there);
- `index-sync` never adds, removes or moves a journal page (the log skill
  owns the `## Diary` section and the rollups);
- they still count as link *sources* for inbound counts, and their own links
  are still checked for `broken_links` (report only).

**R2 — Link resolution.** Before resolving a wiki-link target: strip a
`#anchor` suffix; a target containing `/` whose first segment is not
`archive` is a cross-wiki link (`[[label/slug]]`) — never reported as broken
and never unlinked. Applies to `find_broken_links`, `--fix`, `wiki_tools`.

**R3 — No model names (spec Goal 4, binding).** The cascade ingest in the
ingest `SKILL.md` (~:173) launches an `Agent` with `model: "haiku"`. Remove
the model parameter so it inherits the session model. Flag to the owner.

**R4 — `source_date` must not trigger re-ingest.** `source_date` is written to
`source.json` but excluded from the metadata comparison in `raw_store` (treat
it like the existing volatile fields), so adding it never turns an unchanged
source into `changed`, and a `touch` of a local file changes nothing. When a
source is unchanged and its `source.json` lacks `source_date`, leave the file
alone. Values: Confluence `version_when`; Jira `updated_at`; Slack newest
message date; local = original file mtime at copy time; web = HTTP
`Last-Modified` when the fetcher has it, else omitted; log-skill pasted notes
(`original_path: null`) = omitted. A missing `source_date` means "keep both
claims with a note" at ingest.

**R5 — Empty bulk items.** Only an item with no prior `raw/<slug>.md` in git
(a new source) that renders empty is dropped: its just-written raw files are
removed and the item is marked `wiki_status = "skipped"` (existing status;
do NOT add a raw `skipped` status) with `last_error = "empty body"`, raw
status `done`. A known source that becomes empty keeps its files and is
handled like any other changed source.

**R6 — Push cadence.** Dropped from the design: bulk and refresh keep
upstream's "commit and push per item". No `--no-push` flag is added.

**R7 — `--skip-images` stays** as an accepted flag (13 test call sites). With
images off by default it is a no-op; with `--describe-images` or
`images.describe: true`, `--skip-images` wins and disables images for the run.

**R8 — Release bookkeeping.** Add a `CHANGELOG.md` entry in the repo's format
(`## X.Y.Z - YYYY-MM-DD`, `### llm-wiki — <headline>`, prose, bold-lead
bullets) and bump `.claude-plugin/marketplace.json` `metadata.version` from
1.9.0 to **2.0.0** (the `nano_banana` config and `--stale-days` are removed);
update the llm-wiki `description` there and the root `README.md:36` to drop
nano-banana.

**R9 — Refresh report.** Refresh-all's final report points
`disappeared_upstream` / `dropped_duplicates` at `/lint`, which has no check
for them. Out of scope; left as is.

## Rebase notes (lint.py, commits already on the branch)

- Keep upstream's `find_unbannered_archive_links` and report key
  `archive_links_without_banner`; exempt `index`/`log` as link sources there
  (so `index-sync`'s `[[archive/slug]]` lines do not trigger it).
- Move upstream's `..` traversal guard into `resolve_source_paths`: a path
  with a `..` segment is never resolved against the filesystem; it is reported
  in `missing_sources` and ignored by drift and `citing`.

## Per-task corrections

**Task 4.** Filtering must leave `archive_links_without_banner` untouched.
Journal pages (R1) never appear in pairs, collisions or drift.

**Task 5 (`--fix`).** Apply R1 and R2. Add tests: a day-page containing
`[[other/slug]]` and `[[2026-01-02#Heading]]` is byte-identical after
`--fix`; a normal page's `[[other/slug]]` link is kept; `[[ghost#part]]` to a
missing page is unlinked.

**Task 6 (`wiki_tools.py`).** Apply R1 to `index-sync` (test: day-pages and a
rollup absent from `index.md` are not added; a `## Diary` section is
untouched). `log --op` accepts `ingest | lint | log`; `log` gives the header
`## YYYY-MM-DD (log)`. `locate` skips journal pages.

**Task 7 (images).**
- `do_image_diff_loop` is at `ingest.py:311`; `_looks_like_placeholder` at
  :301 (the copy in `log/scripts/resolve_link.py` is separate — leave it).
- The unchanged early return (`ingest.py:701`) hard-codes the old images
  summary; make it `{"enabled": false}` / the new shape too.
- Prefetch: `--skip-images` (`prefetch.py:283`, `do_images` :331,
  `_describe_changed_images` :149), forwarded from `ingest.py:854` via
  `_stream_prefetch(skip_images=…)`. `extract_images.py` currently runs in
  prefetch regardless (:213) — gate it on images being enabled (R7).
- Web sources go through the same image path as Confluence; same gating.
- Corrected locations: bootstrap nano text `bootstrap.py:383`;
  `smoke_bulk_ingest.py` `nano_banana` blocks :198 and :475; `config.py`
  defaults :50–59, accessors :136 and :154–164, redaction ~:220,
  `apply_ssl_env` comment :297.
- Added to the file list: `tests/smoke_refresh_all.py:88`,
  `tests/smoke_web_ingest.py:343`, `check-setup.sh:62-63`,
  `prefetch.py:14-18,285`. All three existing smoke tests must pass.

**Task 8 (context, source_date, bulk).**
- With no `--source`, `main()` now falls through to refresh-all. Dispatch
  `--context --slug S` before that fallback and add it to the
  `--refresh-all` exclusion checks. Test: `--context` never creates a queue.
- Signatures now: `git_commit(wiki_root, message, raw_dir, wiki_dir)`,
  `detect_source_type(source, wiki_root, cfg)`.
- `fetch_status` may be `"forced"`: `build_context` treats it as `changed`
  when the raw file has a prior committed version, else `new`.
- `context` is added only on the non-unchanged path of the single summary.
- Bulk `--context` for an item whose `raw_status` is `unchanged`: status
  `unchanged`, empty `raw_diff`, `citing_pages` still filled.
- Apply R4, R5, R6 (so: no `--no-push`; no raw `skipped` status). The empty
  mock page goes into the `pages` dict used by `test_discover_and_prefetch`
  (`smoke_bulk_ingest.py:257`).

**Task 9 (`--upgrade`).**
- The `## Page format` section has a fenced example containing
  `## Related pages`; the section scan must ignore headings inside fenced
  code blocks. Test with exactly that shape.
- The template's `## Rules` now has a `raw/.bulk-queries.json` rule; keep it
  in the managed block.
- `SKIP_DEPS_FLAGS = ("--skip-deps",)`.
- 9b (the `lint.py` warning + `test_claude_md_warning`) is done with the lint
  chain, after Task 5.

**Task 10 (docs).**
- Ingest `SKILL.md` is ~1015 lines. The Phase 3 rules apply to Single (:235),
  Slack (:347), Website (:365), Bulk Phase 3 (:523) and Refresh Phase 3
  (:656) — state them once and reference them from each workflow.
- Keep "commit and push per item" (R6). Remove `--no-push` from
  `smoke_docs.py`'s required strings.
- Apply R3. `smoke_docs.py` additionally bans the string `model: "haiku"`.
- Lint `SKILL.md` keeps `archive_links_without_banner` and documents journal
  pages (R1) and cross-wiki links (R2).
- Added to the file list: `skills/log/SKILL.md` (index/log maintenance stays
  the log skill's own; only remove conflicts with the new rules),
  `skills/ingest/references/web-pages.md` (:145, :185 nano-banana),
  root `README.md`, `.claude-plugin/marketplace.json`, `CHANGELOG.md` (R8).
  `smoke_docs.py` also checks the root `README.md` and `marketplace.json` for
  `nano-banana` (CHANGELOG history is exempt).
