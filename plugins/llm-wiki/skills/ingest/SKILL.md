---
name: ingest
description: |
  Ingests one or many sources — a Confluence page URL, a Jira issue URL or
  key, a local file (Markdown, plain text, HTML, PDF, DOCX, XLSX, CSV, PPTX,
  or an image), a Slack channel/thread/search, a public website page, a whole
  Confluence space, a Confluence CQL query, a Jira JQL query, or a website
  sitemap — into an LLM wiki. The skill auto-detects single-item vs bulk from
  the source shape or explicit flags. It fetches content, writes raw sources,
  folds the new knowledge into the existing wiki pages it touches (creating a
  page only for a topic with no home yet), and commits the result to git so
  the next ingest can diff against it. Embedded images are off by default;
  when enabled, only new-or-changed images are described, by the session
  model itself.

  Bulk mode uses a resumable job queue with rate limiting and a circuit
  breaker so a whole-space or whole-site ingest can be paused (Ctrl-C) and
  resumed with `/ingest --resume <job-id>` without re-fetching completed items.

  Bare `/ingest` with no source at all (or `--refresh-all`) re-fetches every
  source the wiki already holds, diffs each against its `raw/` copy, and
  ingests only what actually changed.

  Use this skill whenever the user wants to add, update, refresh, re-ingest,
  import, backfill, or batch-import content into their wiki. Trigger on
  phrases like: "ingest this Confluence page", "add this Jira ticket to the
  wiki", "pull this URL into the wiki", "ingest this PDF", "process this
  document", "refresh this source", "ingest the FOO space", "backfill space
  FOO", "ingest all tickets matching this JQL", "ingest this website", "scrape
  these docs into the wiki", "ingest this sitemap", "crawl this site into the
  wiki", "resume the last bulk ingest", "refresh everything", "refresh the
  whole wiki", "resync the wiki", "update all sources", "bring the wiki up to
  date".

  Requires a per-wiki `.wikirc.json` file (Confluence/Jira endpoints and
  Personal Access Tokens, a Slack token, as needed). Public website ingest
  needs no credentials. If the config is missing or scripts cannot import
  their dependencies, direct the user to references/setup.md before proceeding.
---

# Ingest — LLM Wiki

These rules take precedence over workflow text in the wiki's `CLAUDE.md`.
A wiki bootstrapped before this version may still carry older ingest
instructions there (a summary page per source, hand-edited `index.md` and
`log.md`); follow this file instead, and suggest
`bootstrap.py --upgrade` (see the `create-wiki` skill) to refresh it.

One skill covering both single-item ingest and bulk ingest of whole
Confluence spaces / CQL queries / Jira JQL queries / websites. Auto-detects
the mode.

## Prerequisites

Before running any script, verify:

1. The wiki root has a `.wikirc.json` (not `.wikirc.example.json`). If missing,
   direct the user to `/create-wiki` or to fill in the example config.
2. Python dependencies are installed. If any script exits with a "Missing
   dependencies" message, direct the user to
   [references/setup.md](references/setup.md) and stop.
3. `git` is available and the wiki root is a git repository. If not,
   `git init` first (bootstrap.py does this for new wikis).

## Phase 0 — Decide: single or bulk?

Before running anything, classify the source. `ingest.py` will do this
automatically from arguments; state which path it took as an FYI, then
proceed — no confirmation needed.

### Detection rules (implemented in `ingest.py`)

| Input | Mode |
|-------|------|
| URL with `/pages/<digits>/` or `?pageId=…` | Single Confluence page |
| URL with `/browse/<KEY>` | Single Jira issue |
| Bare Jira key like `PROJ-123` | Single Jira issue |
| URL with `/spaces/<KEY>` (no `/pages/…`) | Bulk Confluence space |
| URL with `/display/<KEY>` (no page name) | Bulk Confluence space |
| URL with `?spaceKey=<KEY>` and no `pageId` | Bulk Confluence space |
| Existing local file path | Single local file |
| URL ending `sitemap*.xml`, `*sitemap*.xml.gz`, or `/robots.txt` | Bulk website |
| Any other `http(s)` URL | **Single web page** |
| `--space FOO` | Bulk Confluence space |
| `--cql "…"` | Bulk Confluence CQL |
| `--jql "…"` | Bulk Jira JQL |
| `--sitemap <url>` | Bulk website from an exact sitemap URL |
| `--site <url>` | Bulk website, auto-discovering the sitemap |
| `--crawl <url> --depth N --max-pages M` | Bulk website by crawling |
| `--resume <job-id>` | Resume a prior bulk job |
| `--slack-channel CHANNEL` or `--channel CHANNEL` with `fetch_slack.py` | Slack channel |
| `--slack-search "QUERY"` or `--search "QUERY"` with `fetch_slack.py` | Slack search results |
| `--slack-thread CHANNEL THREAD_TS` or `--channel`+`--thread-ts` | Slack thread |
| No source at all — bare `/ingest --wiki-root <path>` | Refresh-all |
| `--refresh-all` | Refresh-all (explicit; same effect as bare invocation) |
| `--resume refresh` | Continue an in-flight or confirmed refresh |

Explicit flags win over URL heuristics. A URL that matches both "single
page" and "space" resolves to single (the `/pages/` segment wins).
Ambiguous bare tokens (e.g., "FOO" — could be a space key or a slug) must
be disambiguated by asking the user or by requiring an explicit flag.

A **bare site URL is always one page**, never a whole site — so
`/ingest https://example.com/docs/intro` can't accidentally enumerate a
domain. When the user's wording implies the whole site ("ingest their docs",
"pull in this website"), use `--site <url>` explicitly rather than passing the
URL as `--source`.

### Scope note for bulk

Bulk mode with full per-item wiki synthesis is expensive (Claude tokens
and wall time). State the detected scope as an FYI and proceed — do not
pause for confirmation:

> Ingesting space **FOO**: discovering every page, prefetching raw content
> (rate-limited, resumable), then synthesizing wiki pages and
> committing + pushing one commit per item. For a large space this can take
> hours and touch every wiki category.

If the user wants to scope down, they can re-run with `--cql`
(`label=…` or `updated > …`) — or, for a website, `--include` / `--exclude` /
`--since` / `--limit` — but that's their call, not a blocking prompt.

The one exception is a **sitemap-less website**: `discover.py` refuses to crawl
without bounds and hands the decision back to you (see the bulk website
workflow below). That prompt is required, because guessing a crawl depth on an
unknown domain is not a safe default.

## Resolving the Python interpreter

Before running any script, resolve the correct Python binary. The llm-wiki
deps live in a dedicated venv (`~/.llm-wiki-venv` by default):

```bash
_LLMWIKI_VENV="${LLMWIKI_VENV:-${HOME}/.llm-wiki-venv}"
if [ -x "${_LLMWIKI_VENV}/bin/python3" ]; then
  WIKI_PY="${_LLMWIKI_VENV}/bin/python3"
else
  WIKI_PY="${PYTHON:-python3}"
fi
```

Use `${WIKI_PY}` instead of `python3` in all script invocations below.
`${SKILL_DIR}` refers to the directory containing this file. `${WIKI_ROOT}`
is the wiki directory (contains `.wikirc.json`). `${WIKI_TOOLS}` is
`${SKILL_DIR}/../lint/scripts/wiki_tools.py` (stdlib only, shared with
`/lint`). `${RAW_DIR}` is `${WIKI_ROOT}/raw` unless `.wikirc.json` sets
another `raw_dir`.

## Wiki-update rules

Every workflow below — Single, Slack, Website, Bulk and Refresh — ends the
same way: one raw source gets folded into the wiki. This section holds the
rules for that step once; each workflow's synthesis phase points back here.
Read [references/page-format.md](references/page-format.md) for the page
template.

### Start from the context

Each synthesis starts from a `context` object that tells you where the
source's knowledge already lives:

- **Single item and web page**: the `context` key of `ingest.py`'s JSON
  summary (present whenever the source was not unchanged).
- **Slack**, and any time you need it again: run
  ```bash
  ${WIKI_PY} "${SKILL_DIR}/scripts/ingest.py" \
    --wiki-root "${WIKI_ROOT}" --context --slug "<slug>"
  ```
- **Bulk and refresh**: the same command per item, with the job id, so an
  item that was fetched unchanged is reported as `unchanged` instead of being
  guessed from git history:
  ```bash
  ${WIKI_PY} "${SKILL_DIR}/scripts/ingest.py" \
    --wiki-root "${WIKI_ROOT}" --context --slug "<slug>" --job-id "<job-id>"
  ```

The object is `{status, related_pages, citing_pages, raw_diff,
raw_diff_truncated}`:

- `status` — `new`, `changed` or `unchanged`.
- `related_pages` — existing pages the source is probably about, best first,
  each `{page, summary, score, why}`. `why` says how it was found:
  `mentioned` (the page's name appears in the source), `title` (the source's
  title resembles the page's title) or `both`. These are candidates found by
  string matching, not a verdict: open the top entries and judge. A page
  that is only mentioned in passing is a place to add a link or a sentence,
  not necessarily the source's home.
- `citing_pages` — pages that already cite this raw file (filled for
  `changed` and `unchanged`).
- `raw_diff` — for `changed`, the git diff of `raw/<slug>.md` against its
  previous version. When `raw_diff_truncated` is true the diff was cut short;
  read `raw/<slug>.md` itself for the rest.

To look for a home under other words, run
`${WIKI_PY} "${WIKI_TOOLS}" locate --wiki-root "${WIKI_ROOT}" <term> [<term> ...]`
(a `{page, summary, score}` list ranked by matches in name, Summary and body).

`--context` exits 1 with a message on stderr when `raw/<slug>.md` does not
exist (check the slug), and it cannot be combined with `--commit-only` or
`--push-only`.

### The rules

1. **Update the related pages first.** Read the top pages in
   `related_pages` and put the new knowledge in the ones that are really
   about the same topic. A wiki compounds only when
   everything known about one topic accumulates on one page; a second page
   on the same topic splits the knowledge, and the two copies drift apart.
2. **Create a page only for a topic with no home.** A new page is right when
   no existing page covers the topic *and* the source has enough substance
   to fill a page of its own — including when `related_pages` is not empty
   but none of its entries is really about the source's topic. Use the
   template in `page-format.md`.
3. **No summary page per source, and no stub pages.** A source is not a
   topic — `raw/<slug>.md` already is the per-source record, and a page that
   only restates one document duplicates it. A concept you mention but
   cannot fill in stays plain text, without `[[brackets]]`, until a source
   gives it substance: a link to an empty placeholder looks like knowledge
   but holds none, and `/lint` will flag it.
4. **A changed source is rewritten in place.** When `status` is `changed`,
   read `raw_diff`, open every page in `citing_pages`, and rewrite the claims
   the diff affects where they stand. Never append a second, "updated"
   version of a claim: readers then have to work out which one is current,
   and `/lint` reports the pair as a contradiction.
5. **Conflicts: the newer source wins.** When the source contradicts an
   existing claim that cites a *different* source, compare the `source_date`
   in each source's `raw/<slug>.source.json`. The claim from the source with
   the later date replaces the other one, and the replacement cites the
   newer source. `source_date` is when the source itself last changed
   (Confluence version date, Jira update, newest Slack message, the local
   file's modification date, a web page's `Last-Modified`), so it is the best
   evidence of which statement is current. If the dates are equal, or either
   source has no `source_date` (older raw files and pasted notes often
   don't), keep both claims with an explicit note that they disagree, each
   with its own citation. Guessing would silently drop a claim that may be
   true; the note keeps it visible, and `/lint` brings the pair to the user.
6. **Cite every claim as `(source: raw/<file>)`**, and list the same
   `raw/...` paths in the page's `**Sources**` block. One format everywhere
   is what lets `/lint` and `wiki_tools.py citing` find the pages that depend
   on a source when it changes.
7. **Let the script keep `index.md` and `log.md`.** After the pages are
   written, run:
   ```bash
   ${WIKI_PY} "${WIKI_TOOLS}" index-sync --wiki-root "${WIKI_ROOT}"
   ${WIKI_PY} "${WIKI_TOOLS}" log --wiki-root "${WIKI_ROOT}" --op ingest \
     --title "Ingested raw/<slug>.md (<source title>)" \
     --bullet "New pages: [[a]], [[b]]" --bullet "Updated: [[c]] (what changed)"
   ```
   Do not edit either file by hand: hand edits drift out of step with the
   pages on disk, and the script keeps every existing section, ordering and
   description untouched. `index-sync` lists each new page under
   `## Uncategorized` with its Summary, drops lines for pages that no longer
   exist, and moves lines for archived pages under `## Archive`; it never
   touches journal pages or the `## Diary` section, and leaves an entry that
   has nested lines under it alone (it names it on stderr — fix that one by
   hand). After it runs, you may move a newly added line out of
   `## Uncategorized` into an existing section when one obviously fits —
   only that line. `log` puts the entry at the top of `wiki/log.md` and, when
   today already has an ingest entry, adds the bullets to it. `--bullet` is
   repeatable.

`raw/` holds the immutable sources: never modify a raw file during synthesis.
The only files written under `raw/` by hand are image descriptions, below.

### Describing images (only when `images.enabled` is true)

Images are off by default, and the ingest summary then carries
`"images": {"enabled": false}`: skip this step. When the user turned them on
(`images.describe: true` in `.wikirc.json`, or `--describe-images` for one
run), the summary's `images` object lists `pending_images` — each
`{image, description, status}`, where `image` is the downloaded file,
`description` the path to write, and `status` `new` or `changed`. Describe
them before writing wiki pages, so the descriptions can feed the synthesis.

For each entry, read the image file and write its description to the
`description` path in four parts:

1. **Image type** — screenshot, architecture diagram, flowchart, chart,
   table, photo, …
2. **Visible text, verbatim** — every label, heading and value you can read,
   exactly as written.
3. **Structure and content** — what it shows and how the parts relate, in
   3–8 sentences.
4. **Key facts** — up to 8 bullet points a reader would want to cite.

Then record them, naming the image files (`0.png`, not paths):

```bash
${WIKI_PY} "${SKILL_DIR}/scripts/image_manifest.py" \
  --raw-dir "${RAW_DIR}" --slug "<slug>" mark-described 0.png 3.jpg
```

An image stays pending until it is marked, so an interrupted run lists it
again next time. Bulk and refresh items have no per-item summary; get their
list with

```bash
${WIKI_PY} "${SKILL_DIR}/scripts/image_manifest.py" \
  --raw-dir "${RAW_DIR}" --slug "<slug>" status
```

where every image whose `status` is `new` or `changed` is pending and its
description goes to `raw/images/<slug>/<image stem>.md`.

If the session model cannot read images, do not write or mark anything:
report the images as skipped and carry on with the text.

## Single-item workflow

Follow these phases in order.

### Phase 1 — Detect and fetch the source

Fetch with `--no-commit` so the raw files are staged but not yet committed —
this lets Phase 4 land a **single** commit covering both raw and the wiki
pages you synthesize in Phase 3 (one commit, one push, right after the whole
ingest completes):

```bash
${WIKI_PY} "${SKILL_DIR}/scripts/ingest.py" \
  --wiki-root "${WIKI_ROOT}" \
  --source "<Confluence URL | Jira key | local file path>" \
  --no-commit
```

The orchestrator prints a JSON summary: `fetch` (the fetcher's result,
including `slug` and `status`), `images` (see
[Describing images](#describing-images-only-when-imagesenabled-is-true)) and,
unless the source was unchanged, `context` (see
[Wiki-update rules](#wiki-update-rules)). Add `--describe-images` to turn
images on for this run when the user asks for them.

### Phase 1.5 — Cascade to linked wikis (opt-in, non-blocking)

Single-item workflow only — bulk and refresh-all don't run this phase (judging
per-item relevance across a whole bulk queue would be expensive; a possible
future extension, not attempted here).

Read `cfg.linked_wikis()` (see `scripts/config.py`). For each entry whose
`cascade_ingest` is `true` and whose `path` isn't a placeholder (skip entries
that still look like `wikirc.example.json`'s illustrative paths — e.g.
containing `your-`), judge whether the source you just fetched
(`raw/<slug>.md`) is topically relevant to that entry's `purpose` string. This
is your own judgment call, not a script.

For each relevant match, launch one `Agent` tool call (no model override —
it runs on the session model) — issue every matching cascade **in the same
message** so they run in parallel.
Prompt template:

```
You are executing the llm-wiki `ingest` skill's single-item workflow as a
cascade triggered by another wiki's /ingest run. Read and follow
"<absolute-path-to-this-plugin-root>/skills/ingest/SKILL.md" — specifically
its "Single-item workflow" Phases 1 through 4 — for exactly this invocation:

  wiki_root: <linked-wiki-absolute-path>
  source: <the original --source value from the parent run>

Use this wiki's own .wikirc.json at <linked-wiki-absolute-path>/.wikirc.json
for all credentials — do not reuse the parent wiki's config or credentials.

Before doing anything else, run `git -C <linked-wiki-absolute-path> status
--porcelain`. If it shows any pending changes, STOP before Phase 4 (do not
commit) and report outcome="error" — those changes aren't yours to sweep into
this cascade's commit.

Do not skip Phase 4 (commit + push) once you do reach it. If Phase 1 reports
status="unchanged", stop after Phase 1 and report outcome="unchanged" —
nothing to synthesize or commit.

Reply with ONLY this JSON, no prose:
{"label": "<label>", "wiki_root": "<path>", "outcome":
 "ingested"|"unchanged"|"error", "slug": "<slug-or-null>",
 "pages_touched": ["<slug>", ...], "commit": "<sha-or-null>",
 "pushed": true|false|null, "error": "<message-or-null>"}
```

Don't block this wiki's own Phase 2-4 on cascade completion — collect each
subagent's JSON reply when convenient and fold a `## Cascade results` block
into the final ingest summary you show the user, including
`skipped_irrelevant` entries (label + one-line reason) for transparency. A
wiki with no `linked_wikis` configured (every wiki today, until you opt one
in) sees no change at all from this phase.

### Phase 2 — Report takeaways (non-blocking)

After the orchestrator finishes, read the newly written `raw/<slug>.md`.
If images are enabled, describe the pending ones now (see
[Describing images](#describing-images-only-when-imagesenabled-is-true)) and
read their descriptions. Print a short summary for the user's awareness, then
**proceed directly to Phase 3** — do not wait for confirmation:

> I ingested **[title]** from [source-type]. Key takeaways:
>
> - [3-6 bullet points on what the source is about]
>
> Images: [off | N described, K unchanged, S skipped]
>
> Updating the wiki now:
> - Update [[existing-page]] with [what changes]
> - New page [[new-topic]] — [why no existing page fits]

This summary is informational. Ingest is fully automatic: fetch →
synthesize → commit → push runs end-to-end without pausing. (The user can
still interrupt or correct after the fact — the commit history and diff are
always reviewable, and unwanted changes can be reverted.)

### Phase 3 — Update wiki pages

Apply the [Wiki-update rules](#wiki-update-rules) with the summary's
`context`: update the `related_pages` first, rewrite claims in the
`citing_pages` when the source changed, create a page only for a topic with
no home, then run `index-sync` and `log --op ingest`.

### Phase 4 — Commit and push (raw + wiki together)

After synthesis, run one `--commit-only` to commit the staged raw files
**and** the wiki pages in a single commit, then push:

```bash
${WIKI_PY} "${SKILL_DIR}/scripts/ingest.py" \
  --wiki-root "${WIKI_ROOT}" --commit-only --slug "<slug>"
```

Commit message format:

```
ingest: <slug> (N new, M changed images)
```

If `auto_push: true` in `.wikirc.json`, `ingest.py` pushes to the configured
remote after the commit. Push failures warn but do not fail the ingest — the
local commit is always preserved. Credential resolution is delegated to Git
(SSH key, macOS Keychain, `git-credential-store`).

The commit stages `raw` + `wiki` wholesale (`git add raw wiki`), which honors
`.gitignore`: the downloaded image **byte files** (`raw/images/<slug>/<n>.<ext>`)
are ignored and stay local, while each `<n>.md` description and the per-slug
`.manifest.json` are committed. No special flag or code path is needed — git
skips the ignored bytes automatically.

Skip this phase only when Phase 1 reported `status="unchanged"` (nothing was
staged, so there's nothing to commit or push). One exception: with images
enabled, an unchanged source still lists images an earlier run left
undescribed. If you describe them, there is nothing to synthesize, but run
the `--commit-only` above so the new description files and manifest are
committed.

## Slack workflow

Slack content is fetched entirely by `fetch_slack.py` via the Slack Web API —
zero Claude token usage at fetch time. Claude only does wiki synthesis, same
as any other source.

**Diff strategy**: each run produces a slug encoding the **exact message date
range** (e.g. `slack-general-20260701-20260720`). Re-running the same slug
triggers the SHA-256 content-diff gate — if nothing changed, it returns
`"unchanged"` without writing anything or committing. Without `--after`, the
script auto-increments from the last `fetched_until` timestamp, so a plain
`/ingest --slack-channel general` always fetches only new messages.

**Watermark precedence** (reported as `window_from` in the JSON):
`--oldest-ts` (exact epoch) → `--after` (a date, so day granularity) →
`.wiki-state/last-fetched.json` → the `fetched_until` in the committed
`raw/*.source.json` for that channel. The last one is what makes an
incremental fetch work on a **fresh clone**, where `.wiki-state/` doesn't
exist: without it there would be no floor at all and the whole channel history
would be refetched. Prefer letting the watermark do the work — passing
`--after` for a refresh would re-ingest up to a day of messages the wiki
already has, into a second overlapping raw shard.

### Phase 1 — Run fetch_slack.py

```bash
# Channel (new messages since last fetch by default):
${WIKI_PY} "${SKILL_DIR}/scripts/fetch_slack.py" \
  --wiki-root "${WIKI_ROOT}" \
  --channel "general"

# Explicit date window:
${WIKI_PY} "${SKILL_DIR}/scripts/fetch_slack.py" \
  --wiki-root "${WIKI_ROOT}" \
  --channel "general" --after 2026-07-01 --before 2026-07-20

# Single thread:
${WIKI_PY} "${SKILL_DIR}/scripts/fetch_slack.py" \
  --wiki-root "${WIKI_ROOT}" \
  --channel "general" --thread-ts "1234567890.123456"

# Search:
${WIKI_PY} "${SKILL_DIR}/scripts/fetch_slack.py" \
  --wiki-root "${WIKI_ROOT}" \
  --search "topic:decision after:2026-07-01"
```

Parse the JSON summary: `{"slug", "status", "title", "message_count", "date_range"}`.

If `status == "unchanged"` → no new messages since last fetch. Tell the user and
skip synthesis. Nothing to commit.

**Whole-channel ingest**: by default there is no message cap — the script
paginates the entire window (respecting Slack's Retry-After on 429 via the
shared rate limiter). Pass `--limit N` only as a safety cap. If a positive
`--limit` is hit, the script keeps the **newest** N messages, sets
`"truncated": true` in the JSON, and prints a `WARNING:` to stderr. The
incremental watermark then advances to the newest message fetched, so the
skipped older messages fall **below** the watermark — a plain incremental re-run
will not pick them up. When you see `truncated`, you must tell the user older
messages were skipped and backfill the earlier range explicitly with
`--before <first-fetched-date> --limit 0` before continuing. Prefer leaving
`--limit` unset (no cap) for whole-channel ingest so this never happens.

### Phase 2 — Report takeaways (non-blocking)

Read `raw/<slug>.md`. Print a short summary of the key discussion points,
decisions, or topics for the user's awareness, then **proceed directly to
Phase 3** — do not wait for confirmation.

### Phase 3 — Update wiki pages + commit

`fetch_slack.py` prints no `context`, so get it with
`ingest.py --context --slug "<slug>"`, then apply the
[Wiki-update rules](#wiki-update-rules). Commit message format:

```
ingest: <slug> (<N> messages, <date-range>)
```

Use `--commit-only` to commit after synthesis:

```bash
${WIKI_PY} "${SKILL_DIR}/scripts/ingest.py" \
  --wiki-root "${WIKI_ROOT}" --commit-only --slug "<slug>"
```

If `auto_push: true`, the push happens automatically after the commit.

## Website workflow (single page)

A public web page needs no credentials — nothing in `.wikirc.json` is
required. It follows the **standard single-item workflow** above (Phase 1
fetch with `--no-commit` → Phase 2 takeaways → Phase 3 wiki pages → Phase 4
one commit): `ingest.py` routes any non-Atlassian `http(s)` URL to
`fetch_web.py` automatically.

```bash
${WIKI_PY} "${SKILL_DIR}/scripts/ingest.py" \
  --wiki-root "${WIKI_ROOT}" \
  --source "https://example.com/docs/getting-started" \
  --no-commit
```

Two things differ from Confluence:

- **`extractor` in the JSON summary** is `trafilatura` (normal) or `bs4`
  (fallback). If it says `bs4`, the page may include some navigation or
  footer text — skim `raw/<slug>.md` before synthesizing so you don't cite
  boilerplate as a fact.
- **Slugs are URL-derived**, e.g. `web-docs-python-org-3-library-json`. Cite
  them as-is; they're stable across page retitles.

If the fetch fails with "no readable content extracted", the page is rendered
client-side. Tell the user to save it from their browser (Save As → Web Page,
Complete) and ingest the `.html` file, which goes through `fetch_local.py`.

## Bulk workflow

Bulk runs in three phases: **discovery**, **prefetch** (long-running, no
Claude needed), and **synthesis** (Claude-in-the-loop, one commit + push
per item, no pauses).

### Bulk Phase 1 — Discover + prefetch

Run the orchestrator with a bulk source. It will invoke `discover.py`
(paginating the space/query into a queue) then `prefetch.py` (fetching
each item and, only when images are enabled, downloading its images and
counting the new/changed ones for you to describe at synthesis time). Add
`--describe-images` to enable images for this run.

```bash
${WIKI_PY} "${SKILL_DIR}/scripts/ingest.py" \
  --wiki-root "${WIKI_ROOT}" \
  --space FOO
# or:
#   --cql "space=FOO AND label=onboarding"
#   --jql "project=PROJ AND updated > -30d"
#   --sitemap https://example.com/sitemap.xml
#   --site https://example.com
#   --crawl https://example.com --depth 2 --max-pages 100
#   --resume <job-id>
```

The orchestrator streams one JSON line per item during prefetch so you
can watch progress. When it finishes it prints a summary containing the
`job_id` and counts. Save the `job_id` — you'll need it for synthesis and
resume.

**If the same query already has a queue**, `discover.py` reuses it and
`prefetch.py` continues where it left off (skipping items already `done`
or `unchanged`). **A plain re-run therefore only resumes**: it does not look for
new pages and does not re-check pages that were already fetched, so pages edited
upstream stay stale. The discover summary says `"rechecked": false` and carries a
`note` when this happens; if you see it on a source the user wanted brought up to
date, re-run with `--replace`.

**To bring an earlier bulk ingest up to date, re-run it with `--replace`.** Use it
whenever the user asks to refresh, re-ingest, update, re-sync or backfill a space,
query, site or earlier bulk job. (To refresh *everything* the wiki has ever ingested,
run a bare `/ingest` instead; see **Refresh-all workflow**.)

```bash
${WIKI_PY} "${SKILL_DIR}/scripts/ingest.py" \
  --wiki-root "${WIKI_ROOT}" \
  --space FOO --replace
```

`--replace` rebuilds the queue (so new pages are found) and re-checks every page
against the copy already in `raw/`. Pages whose content did not change keep their
existing wiki status, so Phase 3 below only sees pages that are new, changed, or
still unsynthesized from an interrupted earlier run. Do **not** add `--force` for an
ordinary refresh: `--force` re-fetches every body *and* re-runs synthesis for every
page, even when the content is identical.

**If prefetch aborts** (Ctrl-C, or the circuit breaker trips after 5
consecutive item failures), the queue is safe on disk. Resume with:

```bash
${WIKI_PY} "${SKILL_DIR}/scripts/ingest.py" \
  --wiki-root "${WIKI_ROOT}" \
  --resume "<job-id>"
```

`--resume` implies `--retry-failed`, so previously-failed items get
another attempt.

#### Bulk websites: sitemap first, crawl only with the user's bounds

`--site <url>` looks for a sitemap in `robots.txt` first, then at the standard
`/sitemap.xml` locations. Nested sitemap indexes and `.gz` sitemaps are
followed automatically.

**When no sitemap exists**, discovery stops and returns this instead of a
`job_id` — it will not crawl on its own:

```json
{"status": "needs_bounds", "site": "https://example.com",
 "robots_crawl_delay": null, "suggested": {"depth": 2, "max_pages": 100},
 "note": "No sitemap found …"}
```

When you see `needs_bounds`, **use `AskUserQuestion`** to get a crawl depth and
a page cap — offer the `suggested` values as the recommended option — then
re-run:

```bash
${WIKI_PY} "${SKILL_DIR}/scripts/ingest.py" \
  --wiki-root "${WIKI_ROOT}" \
  --crawl "https://example.com" --depth 2 --max-pages 100
```

Do not invent bounds and do not fall back to `--depth 99`. If the user declines
to pick, ingest the pages they actually care about individually instead.

**Scoping a large sitemap.** These compose, and all of them beat crawling:

| Flag | Use for |
|------|---------|
| `--include REGEX` | Only one section, e.g. `--include '/docs/'` (repeatable) |
| `--exclude REGEX` | Drop noise, e.g. `--exclude '/blog/' --exclude '/tag/'` |
| `--since YYYY-MM-DD` | Only pages whose `<lastmod>` is recent — the cheapest refresh |
| `--limit N` | Hard cap, good for a trial run before committing to the full site |

Re-running the same `--site` / `--sitemap` URL **reuses its queue**, which only
resumes it: pages already fetched are not re-checked and new pages are not found.
For a periodic refresh, re-run the same command with `--replace`. It re-enumerates,
re-checks every page (the conditional-GET gate makes untouched pages nearly free),
and only changed or new pages reach synthesis.

**robots.txt is enforced on all bulk website paths** — disallowed URLs are
dropped from the queue and reported. `--ignore-robots` overrides it; only pass
it if the user confirms they have permission for that site.

### Bulk Phase 2 — Status report (non-blocking)

After prefetch completes, print the queue counts:

```bash
${WIKI_PY} "${SKILL_DIR}/scripts/queue_admin.py" --wiki-root "${WIKI_ROOT}" show <job-id>
```

Report the counts as an FYI, then proceed automatically to synthesis — do
not pause for confirmation:

> Prefetch complete for job **<job-id>**: N pages fetched, M unchanged,
> K failed. Synthesizing wiki pages now, committing + pushing one commit
> per item.

### Bulk Phase 3 — Synthesis loop (automatic, one commit per item)

For each item with `raw_status in {done, unchanged}` and
`wiki_status == pending`, run this end-to-end without pausing:

1. Get the item's context — prefetch ran long before this, so the context is
   computed now:

    ```bash
    ${WIKI_PY} "${SKILL_DIR}/scripts/ingest.py" \
      --wiki-root "${WIKI_ROOT}" --context --slug "<slug>" --job-id "<job-id>"
    ```

2. Read `raw/<slug>.md`. If images are enabled, describe the item's pending
   images (`image_manifest.py … status`, see
   [Describing images](#describing-images-only-when-imagesenabled-is-true)).
3. Apply the [Wiki-update rules](#wiki-update-rules). They matter most here:
   bulk ingest is where a space's structure emerges, and many items should
   feed the same topic pages rather than each producing a page of its own.
4. Mark the item done:

    ```bash
    ${WIKI_PY} "${SKILL_DIR}/scripts/queue_admin.py" --wiki-root "${WIKI_ROOT}" \
      mark <job-id> --ref <ref> --wiki-done
    ```

5. Commit **and push** this item immediately via `ingest.py --commit-only`
   (this is what triggers `git_push` when `auto_push: true` — never use a
   bare `git commit`, which would skip the push):

    ```bash
    ${WIKI_PY} "${SKILL_DIR}/scripts/ingest.py" \
      --wiki-root "${WIKI_ROOT}" --commit-only --slug "<slug>" \
      --message "ingest: <slug> (bulk <job-id>)"
    ```

6. Continue automatically to the next item. Do not ask whether to continue.

If a specific item is clearly a draft or otherwise unwanted, mark it with
`--wiki-skipped` and move on.

A *new* item whose page rendered empty never reaches this loop: prefetch
deletes its raw files and marks it `wiki_status: skipped` with
`last_error: "empty body"`, because an empty page has nothing to synthesize
and would only become an empty raw file for `/lint` to prune. List those
items in the final report so the user knows they were left out. (A source
already in the wiki that becomes empty is kept and synthesized like any other
change.)

### Bulk Phase 4 — Final report

```bash
${WIKI_PY} "${SKILL_DIR}/scripts/queue_admin.py" --wiki-root "${WIKI_ROOT}" show <job-id>
```

Tell the user how many items are done, skipped (naming the `empty body`
ones), and (if any) still failed. Offer to `/ingest --resume <job-id>` for
failures.

## Refresh-all workflow

Bare `/ingest` (no `--source`, no bulk flag, no `--resume`) — or the explicit
`--refresh-all` flag — brings the whole wiki up to date: it **really re-fetches
every source the wiki holds**, diffs each against its `raw/` copy, and leaves
only the genuinely changed or new ones for you to synthesize.

It is not a new pathway. Refresh is **one more bulk queue kind**, so it reuses
the same discovery → prefetch → synthesis machinery as the Bulk workflow
above: one resumable queue, rate limiting, the circuit breaker, per-item
streaming, `queue_admin.py`, and `--resume`. The only new part is how the queue
is built.

```bash
${WIKI_PY} "${SKILL_DIR}/scripts/ingest.py" --wiki-root "${WIKI_ROOT}"
```

That single command runs Phase 1 and Phase 2 back to back. Everything below
describes how to read its output.

### Refresh Phase 1 — Discovery (what will be checked)

`ingest.py` runs `discover.py --refresh`, which builds one queue (job id
`refresh`) from two sources:

- **one item per `raw/*.source.json`** — every Confluence page, Jira issue,
  web page, local file, Slack channel and Slack thread already in the wiki;
- **a fresh re-enumeration of every recorded bulk query** in
  `raw/.bulk-queries.json`, so a page *added* to a tracked space or sitemap
  since the last ingest is picked up too.

The two are merged by ref (page id / issue key / URL), so a page ingested both
individually and via a space query is one queue item, not two.

Discovery is cheap — one paginated search per bulk query, no page fetches — and
prints:

| Field | Meaning |
|-------|---------|
| `status` | `ready`, `resumable`, `needs_confirmation`, or `empty` |
| `counts.total` | Sources that will be fetched |
| `counts.known_sources` / `brand_new_upstream` | Already in `raw/` vs newly discovered |
| `bulk_queries` | Per-query enumeration counts |
| `disappeared_upstream` | Refs in `raw/` the re-enumeration no longer returns |
| `skipped` | Everything deliberately not refreshed (see below) |
| `query_warnings` | Bulk queries that failed to enumerate this run |

**`status` handling:**

- `ready` — fetching starts automatically; report the scope as an FYI and let
  it run.
- `empty` — nothing has ever been ingested. Say so; there is nothing to do.
- `resumable` — an unfinished refresh is already on disk and is being
  continued rather than rebuilt (re-enumerating would reset and re-download
  everything already fetched). Only pass `--replace` if the user explicitly
  wants to start over.
- `needs_confirmation` — more than ~200 sources. `ingest.py` **stops before
  fetching anything**. Use `AskUserQuestion` to confirm, quoting the counts and
  that this means hundreds of API calls. On approval, continue with the queue
  that is already on disk:

    ```bash
    ${WIKI_PY} "${SKILL_DIR}/scripts/ingest.py" \
      --wiki-root "${WIKI_ROOT}" --resume refresh
    ```

  If the user declines, stop — nothing has been fetched. Do not pass `--yes`
  on their behalf; that flag exists for unattended/cron runs.

### Refresh Phase 2 — Fetch and diff (long-running, no Claude needed)

`prefetch.py` then walks the queue and invokes the ordinary single-source
fetcher for each item, so every existing diff gate applies unchanged:
Confluence's version pre-check, Jira's `updated_at` check, the web
conditional-GET (ETag / If-Modified-Since), Slack's incremental watermark, and
the SHA-256 content gate behind all of them. One JSON line is streamed per
item.

Each item lands in one of:

| `raw_status` | Meaning | Needs synthesis? |
|--------------|---------|------------------|
| `unchanged` | Refetched, content identical to `raw/` | **No** — `wiki_status` stays `done` |
| `done` | Content changed (or the source is new) and `raw/` was rewritten | **Yes** |
| `failed` | Fetch errored — including a page deleted upstream (404) | No |

Ctrl-C is safe; the circuit breaker stops after 5 consecutive failures. Either
way re-run `--resume refresh` to continue.

Pass `--force` to bypass every diff gate and re-fetch, re-download images (when
enabled), and re-synthesize everything.

### Refresh Phase 3 — Synthesis (only what changed)

Identical to **Bulk Phase 3** above, with `<job-id>` = `refresh`: for each item
with `raw_status in {done, unchanged}` and `wiki_status == pending`, run
`ingest.py --context --slug <slug> --job-id refresh`, read `raw/<slug>.md`,
apply the [Wiki-update rules](#wiki-update-rules) — a refreshed source is
usually `changed`, so this is mostly rewriting claims in its `citing_pages`
from the `raw_diff` — then `queue_admin.py mark refresh --ref <ref>
--wiki-done`, and commit + push that item with `--commit-only`.

On a wiki that is already current this list is **empty** — that is the expected
outcome, and the right report is "everything is up to date", not silence.

### Refresh Phase 4 — Final report

```bash
${WIKI_PY} "${SKILL_DIR}/scripts/queue_admin.py" --wiki-root "${WIKI_ROOT}" show refresh
```

Report, in this order:

1. **Changed** — how many sources were re-ingested and which wiki pages moved.
2. **Unchanged** — the count only; do not list them.
3. **Failed** — with `last_error`. Offer `/ingest --resume refresh`.
4. **`disappeared_upstream`** — sources whose upstream page is gone. Their
   `raw/` files and wiki pages are still present and are **not** touched by
   refresh; suggest `/lint` to retire them.
5. **`skipped`** — with the relevant advice per bucket:
   - `dropped_duplicates` — two raw files for one upstream page → `/lint`
   - `local_missing_original` — the original file isn't on this machine
   - `slack_searches` — excluded by design (see Concrete rules)
   - `unhandled_type`, `unreadable_source_json`, `unreplayable_bulk_queries`
   - `registry_error` / `query_warnings` — a broken registry or a bulk query
     that failed to enumerate, so **part of the wiki was not checked**. Say so
     explicitly; do not report the refresh as complete.

## Concrete rules

- **Slug**: `slugify(title)` for Confluence, `KEY-123-<slug-of-summary>` for
  Jira, filename stem for local files, `web-<host>-<url-path>` for web pages
  (URL-derived, not title-derived, so a retitled page updates its existing raw
  file instead of creating a second one). Enforced by `ingest.py`.
- **Raw layout**:
  - `raw/<slug>.md` — Markdown-converted source content
  - `raw/<slug>.source.json` — stable metadata:
    `{ "type", "url" or "path", "title", "content_sha256", "source_sha256"
    (local only), "original_filename"/"original_path" (local only),
    "image_hints", "version_number" (Confluence), "updated_at" (Jira),
    "source_date" }`.
    For local files `path` is the **relative** in-wiki copy
    (`raw/<slug><ext>`), never an external absolute path. **No wall-clock
    timestamps.** `source_date` is source-side time, not fetch time — the
    ISO date the source itself last changed (Confluence version date, Jira
    `updated_at`, newest Slack message, the local file's modification time
    when copied, a web page's `Last-Modified`). It is omitted when unknown
    (a web page without `Last-Modified`, a pasted `/log` note, a raw file
    written before this field existed). It is left out of the change
    comparison, so adding it never makes an unchanged source `changed`. The
    wiki-update rules use it to decide which of two conflicting claims wins.
  - `raw/<slug>.<ext>` — for **local** ingests, the original file copied into
    the wiki (same stem as the `.md`). Documents/spreadsheets/presentations are
    **committed** (wiki stays self-contained + diffable); image/video/audio
    originals are **git-ignored** (kept local). `source_sha256` hashes this
    copy, so diffs survive the external original moving or being deleted.
  - `raw/images/<slug>/<n>.<ext>` — downloaded image bytes. **Git-ignored,
    kept local only** (large binaries; re-downloaded fresh each ingest).
  - `raw/images/<slug>/<n>.md` — the description the session model wrote.
    **Committed** — the cached description so unchanged images are never
    re-described.
  - `raw/images/<slug>/.manifest.json` — per-image `{ sha256, source_url,
    description_file, described_at }`; the source of truth for image diffs
    and URL-based dedup. **Committed** — it is the SHA baseline every future
    ingest diffs against, so it must be in git even though the bytes are not.
- **Bulk queue layout**: `.wiki-state/bulk-jobs/<job-id>/queue.json` holds
  the queue for one bulk job. Git-ignored. Never referenced by wiki pages.
  A refresh uses the fixed job id `refresh`, so there is only ever one.
- **Bulk query registry**: `raw/.bulk-queries.json` — **committed** (unlike
  the job queues above), because it's what lets a refresh know which bulk
  queries were ever run even from a fresh clone where `.wiki-state/` never
  existed. One entry per `(kind, query)`: kind, query, canonical options,
  first/last job id, first/last run timestamp.
  - Dot-prefixed on purpose. `raw/` otherwise holds only immutable source
    documents (the wiki's own CLAUDE.md says never to edit anything in there);
    this is plugin-managed metadata, and the leading dot also keeps it out of
    the `raw/*.md` and `raw/*.source.json` globs every other script walks.
  - Written whenever discovery enumerates, **and** on a plain reuse when the
    query isn't registered yet (so a query first run before this file existed
    still becomes refreshable). A reuse of an already-registered query leaves
    the file byte-identical — no git churn on a no-op discovery.
  - Queries known only from a local `.wiki-state/bulk-jobs/` queue are
    **backfilled** into it on the next refresh.
  - If the file is malformed, discovery **fails loudly and leaves it alone**
    rather than overwriting it — silently rewriting it would discard every
    query the wiki had registered.
- **`wiki_status` carry-over**: re-enumeration builds fresh `Item`s, which are
  `wiki_status="pending"` by construction — so a refresh (or a `--replace`)
  would otherwise force full re-synthesis of an already-finished space. To
  avoid that, `discover.py` stamps `prior_wiki_status` on each newly
  enumerated item: from the queue being replaced (matched by `ref`, only for
  refs whose old `wiki_status` was `done`/`skipped`), or, for a refresh, on
  every ref that already has a `raw/<slug>.source.json`. `prefetch.py` then
  restores `wiki_status` from it whenever a refetch comes back
  `raw_status="unchanged"`, so only genuinely new or changed items reach
  `Queue.pending_wiki()`.
  - The hint is **retired the moment a fetch comes back changed**. Every
    failure path after the fetch (such as an image download) marks the
    item `failed` *after* the new raw bytes are already on disk — so without
    this, a retry would refetch, see `unchanged`, and restore
    `wiki_status="done"` for a page whose wiki side was never re-synthesized.
- **Per-item `source_kind`**: a `refresh` queue mixes Confluence, Jira, web,
  local and Slack items, so each carries the fetcher to use. Bulk query queues
  don't set it and dispatch on `queue.kind` as before. `web` vs `web_bulk`
  decides whether the per-page robots.txt check runs: a page enumerated from a
  sitemap/crawl was already robots-filtered at discovery, an individually
  ingested page was not.
- **Volatile / local-only state lives outside git**:
  - `.wiki-state/last-fetched.json` at the wiki root records the timestamp
    and status of the most recent single-item fetch per slug. For web sources
    it also holds the `ETag` / `Last-Modified` validators under a separate
    `web:<slug>` key (a prefixed key, because `write_fetch_history` overwrites
    `data[<slug>]` wholesale).
  - `.wiki-state/bulk-jobs/` holds bulk job queues.
  - Image **byte files** under `raw/images/**/*.{png,jpg,jpeg,gif,webp,bmp}`
    are ignored too — they're re-downloaded each ingest, so only their
    `.md` descriptions and `.manifest.json` need to persist in git.
  - Copied-in **media** originals at the raw root
    (`raw/*.{png,jpg,…,mp4,mov,mp3,…}`) are ignored — local only. Copied-in
    **documents/spreadsheets/presentations** are NOT ignored (committed).
  - All git-ignored via the template `.gitignore`.
- **Content diff gate (Layer 1)**: each fetcher computes a SHA-256 over the
  rendered Markdown. If it matches the previous `content_sha256`, the
  fetcher returns `status="unchanged"` and does not rewrite `raw/<slug>.md`
  or `raw/<slug>.source.json`. `fetch_local.py` also fast-paths on
  `source_sha256` (the SHA of the copied-in `raw/<slug><ext>`, not the external
  original) to avoid re-parsing PDFs/DOCX. `fetch_web.py` fast-paths on a
  **conditional GET** (`If-None-Match` / `If-Modified-Since` from validators
  stored under the *requested* URL's slug, resolving to the final slug after
  redirects) — an HTTP 304 returns `unchanged` without downloading or parsing
  the page at all. The conditional headers are only sent when both raw files
  for the resolved slug still exist on disk, so a deleted raw file can never
  be mistaken for "unchanged". In bulk mode, unchanged items skip the image
  and description steps.
- **Orchestrator gate (Layer 2)**: when the fetcher reports `unchanged`,
  `ingest.py` skips image download and the git commit, and prints no
  `context`. Only `.wiki-state/last-fetched.json` is updated (with images
  enabled, the summary still lists images an earlier run left pending).
  Pass `--force` to bypass this and re-run every step. (Single-item only;
  bulk always uses the queue for its skip logic.)
- **Image dedup gate (Layer 3)**: `extract_images.py` downloads each image
  into memory, hashes it, and looks up the manifest by `source_url` first,
  then by `sha256`. Matches reuse the existing filename; mismatched hashes
  overwrite the same filename in place.
- **Image description gate (Layer 4)**: `image_manifest.py.classify()`
  returns `new` / `changed` / `unchanged`. Only `new` or `changed` images
  are listed as `pending_images` for you to describe; `mark-described`
  records the sha that was described, so an image is pending again only when
  its bytes change.
- **Images are opt-in**: off unless `images.describe` is `true` in
  `.wikirc.json` or the run passes `--describe-images`; `--skip-images` wins
  over both. With images off nothing is downloaded and no manifest is
  touched, but each source's `image_hints` stay in its `source.json`, so a
  later run with images on can still pick them up. Turning images on acts
  only on sources that are new or changed in that run — or on any source
  when combined with `--force` — so it does not by itself reopen unchanged
  sources. A local image file ingested as the source is already on disk, and
  is likewise only described when images are on. No script calls a model:
  the description is always written by the session.
- **Web image filtering**: web pages are mostly chrome, so hints are collected
  from the extracted content subtree only, then filtered by form (`data:`,
  `.svg`, `.ico`), by name/role pattern
  (`logo|icon|avatar|sprite|badge|pixel|tracking|spacer|favicon`), by declared
  dimensions (<100px), and finally by byte size (`web.min_image_bytes`, default
  8192, applied after download in `extract_images.py`). Max 20 hints per page.
  Survivors go through Layers 3 and 4 exactly like Confluence attachments.
- **Rate limiting**: every HTTP call to Atlassian, Slack, and the open web
  goes through `rate_limiter.py`. Config lives in `.wikirc.json` under
  `atlassian.rate_limit_rps` / `.burst` / `.max_retries` /
  `.retry_base_delay_seconds` (and the same keys under `slack` and `web`).
  Defaults: Atlassian 2 rps / burst 5, web 1 rps / burst 2. On HTTP 429 or 503, the limiter respects `Retry-After`
  (seconds or HTTP-date) and otherwise backs off exponentially with jitter.
  After `max_retries` a request fails and (in bulk mode) the item's queue entry
  goes to `failed`.
- **robots.txt**: advisory for a single explicitly-named page (warn, proceed),
  **enforced** for every bulk website path (`--site` / `--sitemap` / `--crawl`).
  `--ignore-robots` or `web.respect_robots: false` overrides it. A crawl also
  honors `Crawl-delay`. **Enforced per-origin**: a sitemap entry (or a nested
  `<sitemapindex>` sitemap) that points at a different host is checked against
  *that host's own* robots.txt, cached per origin — never against the
  entry-point host's rules.
- **Credential scoping**: `web.extra_headers` (Cookie, Authorization, …) is sent
  only to the **entry-point origin** — compared as scheme+host+port, so a secret
  never crosses an http/https boundary either. That covers images (a third-party
  CDN gets only `web.user_agent`) *and* discovery: when a sitemap lists URLs on
  another host, that host's `robots.txt` and any nested sitemap file on it are
  fetched without the credentials configured for the site being ingested.
- **Cookies survive redirects**: `requests` strips a header-supplied `Cookie` on
  every redirect hop, so `rate_limiter.py` follows redirects manually for web
  requests that carry one — preserving it across same-origin hops (an
  `http`→`https` upgrade, a trailing-slash normalization) and dropping it
  cross-origin, mirroring what `requests` already does for `Authorization`.
- **Slug collisions**: `web_slug()` flattens `/` and `-` alike, so `/a/b` and
  `/a-b` produce the same slug. Before writing, `fetch_web.py` compares the
  stored `url` in an existing `raw/<slug>.source.json`; a *different* page gets a
  deterministic `-<hash>` suffix instead of overwriting. Same-page re-ingests
  (including http↔https of one page) keep their slug, so the diff gate holds.
- **No embedded credentials in URLs**: a `user:pass@host` URL is rejected up
  front by both `fetch_web.py` and `discover.py` — it would otherwise land in a
  filename and in the committed `source.json`. Use `web.extra_headers` instead.
- **Circuit breaker (bulk only)**: `prefetch.py` aborts if 5 consecutive
  items fail. The user resumes with `/ingest --resume <job-id>`.
- **PAT auth**: `Authorization: Bearer <PAT>` for both Confluence and Jira
  Server/DC. If the required PAT is empty in `.wikirc.json`, the fetch
  script exits with a clear message — direct the user to fill in the token.
- **Never modify anything in `raw/`** during wiki-update phases. `raw/` is
  the immutable ingested source; wiki pages are your synthesis. The one
  exception is writing image descriptions (`raw/images/<slug>/<n>.md`) and
  recording them with `image_manifest.py mark-described`.

## Individual script reference

You can run scripts individually for debugging or non-standard flows.

| Script | Purpose |
|--------|---------|
| `scripts/ingest.py` | Orchestrator — single or bulk, auto-detects |
| `scripts/ingest.py --wiki-root <p> --context --slug <s> [--job-id <id>]` | Print the synthesis context for one raw source |
| `scripts/config.py --wiki-root <path>` | Print the resolved `.wikirc.json` (redacts PATs) |
| `scripts/fetch_confluence.py --wiki-root <path> --url <url>` | Fetch one Confluence page |
| `scripts/fetch_jira.py --wiki-root <path> --key <KEY-123>` | Fetch one Jira issue |
| `scripts/fetch_local.py --wiki-root <path> --path <file>` | Ingest one local file |
| `scripts/fetch_web.py --wiki-root <path> --url <url>` | Fetch one web page |
| `scripts/web_discover.py` | Library: sitemap/robots discovery + bounded crawler (no CLI) |
| `scripts/web_url.py` | Library: URL normalization + `web_slug()` (no CLI) |
| `scripts/extract_images.py --wiki-root <p> --source-json <f>` | Download image_hints for a slug |
| `scripts/image_manifest.py --raw-dir <raw> --slug <s> status` | Print per-image diff status |
| `scripts/image_manifest.py --raw-dir <raw> --slug <s> mark-described <name>...` | Record that each image's description is written |
| `../lint/scripts/wiki_tools.py locate \| citing \| index-sync \| log --wiki-root <p> …` | Find pages, find citing pages, sync `index.md`, add a `log.md` entry |
| `scripts/discover.py --wiki-root <p> --space/--cql/--jql/--sitemap/--site/--crawl <q>` | Enumerate items, write a job queue |
| `scripts/discover.py --wiki-root <p> --refresh` | Build the one `refresh` queue covering every known source |
| `scripts/prefetch.py --wiki-root <p> --job-id <id>` | Fetch + diff every item in a queue, resumable |
| `scripts/queue_admin.py --wiki-root <p> list \| show \| reset \| mark \| delete` | Inspect and manage job queues |
| `scripts/list_sources.py --wiki-root <p>` | Print the source manifest a refresh is built from (read-only) |

All scripts respond to `--help` with their full argument list.

## Edge cases

- **Confluence page has no body** (e.g. draft): warn the user and stop —
  do not create an empty raw file.
- **Jira ticket not found or 403**: surface the API status code to the
  user and suggest checking the PAT.
- **Local file type unsupported** (e.g. legacy binary `.xls`/`.ppt`/`.doc`):
  the original is still copied into `raw/` and versioned; the `.md` is a
  placeholder telling you to synthesize wiki content directly from the
  source file. Suggest exporting to `.xlsx`/`.pptx`/`.docx`/`.pdf`/`.md` for
  native parsing.
- **Image URL is authenticated** (Confluence attachment): the fetcher
  passes the Confluence PAT when downloading images from the same host.
- **Images enabled but the session model cannot read images**: leave the
  images undescribed and unmarked, report them as skipped, and carry on —
  they stay pending for a later session that can read them. A leftover
  vision-endpoint block from an older `.wikirc.json` is ignored.
- **Wiki not a git repo**: `ingest.py` refuses to run with
  `auto_commit=true` and instructs the user to `git init` or run
  `/create-wiki`.
- **Re-ingest of unchanged source (single)**: the content diff gate
  matches; skill reports `status="unchanged"`, skips image download and
  the commit, and only updates `.wiki-state/last-fetched.json`.
  Pass `--force` to override.
- **User manually deleted a file under `raw/`**: for Confluence/Jira/local,
  the diff gate still sees the source as unchanged (source hash matches) and
  won't restore the file — advise `--force` (single) or `queue_admin.py
  reset` (bulk). **Web sources self-heal**: `fetch_web.py` checks that both
  `raw/<slug>.md` and `raw/<slug>.source.json` exist before trusting a
  server's 304; if either is missing it omits the conditional headers, gets a
  full 200, and rewrites them — no `--force` needed.
- **Bulk: user Ctrl-C's during prefetch**: safe. The queue is
  checkpointed after every item. Resume with `/ingest --resume <job-id>`.
- **Bulk: rate limit hit**: `rate_limiter.py` transparently retries with
  Retry-After. If a single request exhausts `max_retries`, the item is
  marked `failed`; the circuit breaker aborts the whole run after 5
  consecutive failures. User backs off and resumes.
- **Bulk: same query re-run**: `discover.py` detects the matching queue and reuses
  it (skips re-enumeration), and prefetch skips pages already fetched. That is a
  resume, not a refresh (`rechecked: false`). Pass `--replace` to refresh: it
  re-enumerates, re-checks every page, and keeps the wiki status of unchanged ones.
- **Bulk: same query, different filters**: reuse is keyed on (kind, query),
  which says nothing about `--include`/`--exclude`/`--since`/`--limit`/
  `--depth`/`--max-pages`/`--ignore-robots`. Those are recorded on the queue, so
  a re-run with different ones returns `status="options_changed"` (exit 1) naming
  what changed rather than silently handing back the old, differently-scoped
  queue. Re-run with `--replace` to rebuild, or `--resume <job-id>` to continue
  the existing one on its original scope.
- **Web page is JavaScript-rendered**: extraction yields nothing and
  `fetch_web.py` exits with a message saying so. Tell the user to save the
  rendered page from their browser (Save As → Web Page, Complete) and ingest
  the local `.html` file — `fetch_local.py` handles it. Never write an empty
  raw file.
- **Web URL is not HTML** (PDF, DOCX, ZIP, image): rejected with a pointer to
  local-file ingest. Suggest downloading it and running `/ingest <path>`, which
  parses all of those natively.
- **Web page returns 403 but loads in a browser**: the default User-Agent is
  blocked. Tell the user to set `web.user_agent` to a browser string, or to add
  the site's session cookie under `web.extra_headers` in `.wikirc.json`.
- **Web page needs a login**: same fix — `web.extra_headers` accepts a `Cookie`
  or `Authorization` header. `config.py` redacts the values when printing.
  These headers are sent **only to the page's own host** — an image embedded
  from a third-party CDN never receives them.
- **Ingested URL redirects** (e.g. `http://` → `https://`, or a moved page):
  the raw file is written under the *target's* slug. Re-ingesting the
  original URL still hits the 304 cache — validators are recorded under both
  the requested URL's slug and the resolved one.
- **Bulk website: no sitemap found**: `discover.py` returns
  `status="needs_bounds"` and exits 0. Ask the user for `--depth` and
  `--max-pages` via `AskUserQuestion`, then re-run with `--crawl`. Do not guess
  the bounds.
- **Bulk website: robots.txt disallows everything** (including a robots.txt
  that returns 401/403, which the standard reads as a blanket disallow):
  discovery aborts with an explanatory error. Only re-run with
  `--ignore-robots` if the user confirms they have permission.
- **Sitemap lists a URL on a different host**: normal — a root sitemap
  commonly lists a docs subdomain. That URL is checked against **its own**
  host's robots.txt, not the entry-point host's.
- **Sitemap contains a `mailto:`/`ftp:`/non-http `<loc>`**: dropped during
  discovery with a warning; it never reaches the queue.
- **Sitemap is enormous or a gzip bomb**: sitemap fetches are streamed with a
  50 MB transfer cap and a 200 MB decompression cap, so a hostile or
  misconfigured response fails with a clear `ERROR:` instead of exhausting
  memory. (`MAX_SITEMAP_URLS` only caps *parsed entries*, which is too late.)
- **URL contains embedded credentials** (`https://user:pass@host/…`): rejected
  with a pointer to `web.extra_headers`. They would otherwise be written into a
  slug/filename and into the committed `source.json`.
- **Sitemap enumerates thousands of pages**: don't ingest them all by reflex.
  Scope with `--include` / `--exclude` / `--since` / `--limit` and say what you
  scoped to. Discovery hard-stops at 50,000 URLs.
- **`--since`, `--depth`, `--max-pages`, or a site URL flag is malformed**:
  `discover.py` validates all of these before making any HTTP request and
  exits with a plain `ERROR:` message — never a traceback. Fix the value and
  re-run; nothing was fetched.
- **Web page's rendered Markdown changes on every ingest**: something volatile
  is inside the extracted region (a CSRF token, a visitor counter, an ad slot).
  Show the user `git diff raw/<slug>.md` — there's no per-source ignore
  mechanism, so the practical answer is to stop re-ingesting that page.
- **Refresh: "nothing to synthesize"** — that's success, not a failure. Every
  source was refetched and every one matched its `raw/` copy. Report the
  unchanged count and stop; don't re-run with `--force` looking for work.
- **Refresh: local source's original file moved or was deleted**: routed to
  `skipped.local_missing_original`; it never blocks the rest of the run. Normal
  on a fresh clone, since `original_path` is an absolute path on whichever
  machine did the ingest. Tell the user which originals are missing; re-ingest
  manually once the file is available again.
- **Refresh: a source is gone upstream** — a deleted Confluence page shows up
  twice: as `disappeared_upstream` from discovery (the re-enumeration no longer
  lists it) and, if it was in `raw/`, as a `failed` item whose fetch 404s.
  Refresh never deletes anything, so its raw file and wiki pages remain.
  Suggest `/lint` to archive the page and retire the raw file.
- **Refresh: Slack threads vs searches**: threads **are** refreshed — refetching
  by `(channel_id, thread_ts)` is deterministic and picks up new replies. Ad hoc
  searches are not (`skipped.slack_searches`): a search's result set shifts over
  time, so re-running it would rewrite that raw file with a different set of
  messages than the wiki cited. Re-ingest a search explicitly if the user wants
  it refreshed.
- **Refresh: duplicate raw files for the same page** (a retitled Confluence page
  or a Jira issue whose summary changed left an orphaned old `.source.json` —
  `fetch_confluence.py`/`fetch_jira.py` never delete the superseded file):
  deduped by `page_id`/`key`, keeping the higher `version_number` (compared
  numerically) or newer `updated_at`, with the rest under
  `skipped.dropped_duplicates`. Suggest `/lint` to clean up the orphaned raw
  files and any wiki pages still citing them.
- **Refresh: `registry_error` or `query_warnings` in the output**: a malformed
  `raw/.bulk-queries.json`, or a bulk query that failed to enumerate this run
  (auth, network, a sitemap that has since vanished). Either means **part of the
  wiki was not checked** — the individually-ingested sources still refreshed
  normally. Report it explicitly rather than calling the refresh complete. For a
  malformed registry, show the user the parse error; the file is deliberately
  left untouched so nothing is lost by fixing it by hand.
- **Refresh: a source was ingested both individually and via a bulk query**:
  not a problem any more. Both sides key items by the same ref (page id / issue
  key / URL), so they merge into one queue item and the source is fetched once.

## Reference docs

| Doc | Load when |
|-----|-----------|
| [references/setup.md](references/setup.md) | User hits any dependency or config error, or is setting up for the first time |
| [references/atlassian-api.md](references/atlassian-api.md) | Debugging Confluence/Jira fetches or CQL/JQL queries |
| [references/local-files.md](references/local-files.md) | Debugging local file parsing or supporting a new format |
| [references/web-pages.md](references/web-pages.md) | Debugging a web page fetch, sitemap discovery, a crawl, or web image filtering |
| [references/page-format.md](references/page-format.md) | Wiki-update phase — page template and citation rules |
