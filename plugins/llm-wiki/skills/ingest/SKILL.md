---
name: ingest
description: |
  Ingests one or many sources — a Confluence page URL, a Jira issue URL or
  key, a local file (Markdown, plain text, HTML, PDF, DOCX, XLSX, CSV, PPTX,
  or an image), a Slack channel/thread/search, a public website page, a whole
  Confluence space, a Confluence CQL query, a Jira JQL query, or a website
  sitemap — into an LLM wiki. The skill auto-detects single-item vs bulk from
  the source shape or explicit flags. It fetches content, extracts embedded
  images, describes only new-or-changed images via a nano-banana-pro-
  compatible vision endpoint, writes raw sources + wiki pages, and commits
  the result to git so the next ingest can diff against it.

  Bulk mode uses a resumable job queue with rate limiting and a circuit
  breaker so a whole-space or whole-site ingest can be paused (Ctrl-C) and
  resumed with `/ingest --resume <job-id>` without re-fetching completed items.

  Use this skill whenever the user wants to add, update, refresh, re-ingest,
  import, backfill, or batch-import content into their wiki. Trigger on
  phrases like: "ingest this Confluence page", "add this Jira ticket to the
  wiki", "pull this URL into the wiki", "ingest this PDF", "process this
  document", "refresh this source", "ingest the FOO space", "backfill space
  FOO", "ingest all tickets matching this JQL", "ingest this website", "scrape
  these docs into the wiki", "ingest this sitemap", "crawl this site into the
  wiki", "resume the last bulk ingest".

  Requires a per-wiki `.wikirc.json` file with Confluence, Jira, and
  nano-banana endpoints and Personal Access Tokens. Public website ingest
  needs no credentials. If the config is missing or scripts cannot import
  their dependencies, direct the user to references/setup.md before proceeding.
---

# Ingest — LLM Wiki

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
> and images (rate-limited, resumable), then synthesizing wiki pages and
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

## Single-item workflow

Follow these phases in order. `${SKILL_DIR}` refers to the directory
containing this file. `${WIKI_ROOT}` is the wiki directory (contains
`.wikirc.json`).

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

The orchestrator prints a JSON summary. Parse it to know which files were
created and how many images were described.

### Phase 2 — Report takeaways (non-blocking)

After the orchestrator finishes, read the newly written `raw/<slug>.md`
and any image description files under `raw/images/<slug>/`. Print a short
summary for the user's awareness, then **proceed directly to Phase 3** —
do not wait for confirmation:

> I ingested **[title]** from [source-type]. Key takeaways:
>
> - [3-6 bullet points on what the source is about]
>
> Images: [N new, M changed, K unchanged, described via nano-banana-pro]
>
> Updating the wiki now:
> - [[proposed-page-1]] — [one-line reason]
> - [[proposed-page-2]] — [one-line reason]
> - Update [[existing-page]] with [what changes]

This summary is informational. Ingest is fully automatic: fetch →
synthesize → commit → push runs end-to-end without pausing. (The user can
still interrupt or correct after the fact — the commit history and diff are
always reviewable, and unwanted changes can be reverted.)

### Phase 3 — Update wiki pages

Follow the rules in [references/page-format.md](references/page-format.md).
For each page you touch:

- Use the page template (Summary / Sources / Last updated / body).
- Add or update `[[wiki-links]]` to connect related concepts.
- Cite every factual claim: `(source: <raw-filename>)`.
- Update `wiki/index.md` with new pages and one-line descriptions.
- Append an entry to `wiki/log.md` with the date, source name, and what
  changed.

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
staged, so there's nothing to commit or push).

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

Follow the standard wiki-update rules (page template, `[[wiki-links]]`,
citations, `wiki/index.md`, `wiki/log.md`). Commit message format:

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
each item, downloading images, describing new/changed images).

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
or `unchanged`). Pass `--replace` to discard the old queue and start fresh.

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

Re-running the same `--site` / `--sitemap` URL **reuses its queue**, so a
periodic refresh is just the same command again; `prefetch.py` skips items
already `done`/`unchanged` and the conditional-GET gate makes untouched pages
nearly free. `--replace` starts fresh.

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

1. Read `raw/<slug>.md` and every `raw/images/<slug>/*.md`.
2. Follow the single-item **Phase 3** (update wiki pages, index.md,
   log.md). Reuse existing pages when possible — bulk ingest is where a
   space's structure emerges, so many items will feed the same concept
   pages rather than each producing a new page.
3. Mark the item done:

    ```bash
    ${WIKI_PY} "${SKILL_DIR}/scripts/queue_admin.py" --wiki-root "${WIKI_ROOT}" \
      mark <job-id> --ref <ref> --wiki-done
    ```

4. Commit **and push** this item immediately via `ingest.py --commit-only`
   (this is what triggers `git_push` when `auto_push: true` — never use a
   bare `git commit`, which would skip the push):

    ```bash
    ${WIKI_PY} "${SKILL_DIR}/scripts/ingest.py" \
      --wiki-root "${WIKI_ROOT}" --commit-only --slug "<slug>" \
      --message "ingest: <slug> (bulk <job-id>)"
    ```

5. Continue automatically to the next item. Do not ask whether to continue.

If a specific item is clearly a draft or otherwise unwanted, mark it with
`--wiki-skipped` and move on.

### Bulk Phase 4 — Final report

```bash
${WIKI_PY} "${SKILL_DIR}/scripts/queue_admin.py" --wiki-root "${WIKI_ROOT}" show <job-id>
```

Tell the user how many items are done, skipped, and (if any) still
failed. Offer to `/ingest --resume <job-id>` for failures.

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
    "image_hints", "version_number" (Confluence), "updated_at" (Jira) }`.
    For local files `path` is the **relative** in-wiki copy
    (`raw/<slug><ext>`), never an external absolute path. **No wall-clock
    timestamps.**
  - `raw/<slug>.<ext>` — for **local** ingests, the original file copied into
    the wiki (same stem as the `.md`). Documents/spreadsheets/presentations are
    **committed** (wiki stays self-contained + diffable); image/video/audio
    originals are **git-ignored** (kept local). `source_sha256` hashes this
    copy, so diffs survive the external original moving or being deleted.
  - `raw/images/<slug>/<n>.<ext>` — downloaded image bytes. **Git-ignored,
    kept local only** (large binaries; re-downloaded fresh each ingest).
  - `raw/images/<slug>/<n>.md` — nano-banana-pro description. **Committed** —
    the cached description so unchanged images are never re-described.
  - `raw/images/<slug>/.manifest.json` — per-image `{ sha256, source_url,
    description_file, described_at }`; the source of truth for image diffs
    and URL-based dedup. **Committed** — it is the SHA baseline every future
    ingest diffs against, so it must be in git even though the bytes are not.
- **Bulk queue layout**: `.wiki-state/bulk-jobs/<job-id>/queue.json` holds
  the queue for one bulk job. Git-ignored. Never referenced by wiki pages.
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
  `ingest.py` skips image download, image description, and the git commit.
  Only `.wiki-state/last-fetched.json` is updated. Pass `--force` to
  bypass this and re-run every step. (Single-item only; bulk always uses
  the queue for its skip logic.)
- **Image dedup gate (Layer 3)**: `extract_images.py` downloads each image
  into memory, hashes it, and looks up the manifest by `source_url` first,
  then by `sha256`. Matches reuse the existing filename; mismatched hashes
  overwrite the same filename in place.
- **Image description gate (Layer 4)**: `image_manifest.py.classify()`
  returns `new` / `changed` / `unchanged`. Only `new` or `changed` images
  invoke `describe_image.py`.
- **Web image filtering**: web pages are mostly chrome, so hints are collected
  from the extracted content subtree only, then filtered by form (`data:`,
  `.svg`, `.ico`), by name/role pattern
  (`logo|icon|avatar|sprite|badge|pixel|tracking|spacer|favicon`), by declared
  dimensions (<100px), and finally by byte size (`web.min_image_bytes`, default
  8192, applied after download in `extract_images.py`). Max 20 hints per page.
  Survivors go through Layers 3 and 4 exactly like Confluence attachments.
- **Rate limiting**: every HTTP call to Atlassian, nano-banana-pro, Slack, and
  the open web goes through `rate_limiter.py`. Config lives in `.wikirc.json`
  under `atlassian.rate_limit_rps` / `.burst` / `.max_retries` /
  `.retry_base_delay_seconds` (and the same keys under `nano_banana`, `slack`,
  and `web`). Defaults: Atlassian 2 rps / burst 5, nano-banana 1 rps / burst 2,
  web 1 rps / burst 2. On HTTP 429 or 503, the limiter respects `Retry-After`
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
- **Credential scoping**: `web.extra_headers` (Cookie, Authorization, …) is
  sent to the page itself, and to images only when the image's host matches
  the page's own host. An image embedded from a third-party CDN never
  receives them — only `web.user_agent` is sent cross-origin.
- **Circuit breaker (bulk only)**: `prefetch.py` aborts if 5 consecutive
  items fail. The user resumes with `/ingest --resume <job-id>`.
- **PAT auth**: `Authorization: Bearer <PAT>` for both Confluence and Jira
  Server/DC. If the required PAT is empty in `.wikirc.json`, the fetch
  script exits with a clear message — direct the user to fill in the token.
- **Never modify anything in `raw/`** during wiki-update phases. `raw/` is
  the immutable ingested source; wiki pages are your synthesis.

## Individual script reference

You can run scripts individually for debugging or non-standard flows.

| Script | Purpose |
|--------|---------|
| `scripts/ingest.py` | Orchestrator — single or bulk, auto-detects |
| `scripts/config.py --wiki-root <path>` | Print the resolved `.wikirc.json` (redacts PATs) |
| `scripts/fetch_confluence.py --wiki-root <path> --url <url>` | Fetch one Confluence page |
| `scripts/fetch_jira.py --wiki-root <path> --key <KEY-123>` | Fetch one Jira issue |
| `scripts/fetch_local.py --wiki-root <path> --path <file>` | Ingest one local file |
| `scripts/fetch_web.py --wiki-root <path> --url <url>` | Fetch one web page |
| `scripts/web_discover.py` | Library: sitemap/robots discovery + bounded crawler (no CLI) |
| `scripts/web_url.py` | Library: URL normalization + `web_slug()` (no CLI) |
| `scripts/extract_images.py --wiki-root <p> --source-json <f>` | Download image_hints for a slug |
| `scripts/image_manifest.py --wiki-root <p> --slug <s> status` | Print per-image diff status |
| `scripts/describe_image.py --wiki-root <p> --image <p> --output <p>` | Describe one image |
| `scripts/discover.py --wiki-root <p> --space/--cql/--jql/--sitemap/--site/--crawl <q>` | Enumerate items, write a job queue |
| `scripts/prefetch.py --wiki-root <p> --job-id <id>` | Bulk-fetch items in a queue, resumable |
| `scripts/queue_admin.py --wiki-root <p> list \| show \| reset \| mark \| delete` | Inspect and manage job queues |

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
- **`nano_banana.api_key` empty or placeholder**: image description is
  skipped; the manifest records images without descriptions and the user
  is warned. Everything else still runs.
- **Wiki not a git repo**: `ingest.py` refuses to run with
  `auto_commit=true` and instructs the user to `git init` or run
  `/create-wiki`.
- **Re-ingest of unchanged source (single)**: the content diff gate
  matches; skill reports `status="unchanged"`, skips image download /
  description / commit, and only updates `.wiki-state/last-fetched.json`.
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
- **Bulk: same query re-run**: `discover.py` detects the matching queue
  and reuses it (skips re-enumeration). Pass `--replace` to overwrite.
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

## Reference docs

| Doc | Load when |
|-----|-----------|
| [references/setup.md](references/setup.md) | User hits any dependency or config error, or is setting up for the first time |
| [references/atlassian-api.md](references/atlassian-api.md) | Debugging Confluence/Jira fetches or CQL/JQL queries |
| [references/local-files.md](references/local-files.md) | Debugging local file parsing or supporting a new format |
| [references/web-pages.md](references/web-pages.md) | Debugging a web page fetch, sitemap discovery, a crawl, or web image filtering |
| [references/page-format.md](references/page-format.md) | Wiki-update phase — page template and citation rules |
