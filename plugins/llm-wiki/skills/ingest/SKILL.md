---
name: ingest
description: |
  Ingests one or many sources — a Confluence page URL, a Jira issue URL or
  key, a local file (Markdown, plain text, HTML, PDF, DOCX, or an image),
  a whole Confluence space, a Confluence CQL query, or a Jira JQL query —
  into an LLM wiki. The skill auto-detects single-item vs bulk from the
  source shape or explicit flags. It fetches content, extracts embedded
  images, describes only new-or-changed images via a nano-banana-pro-
  compatible vision endpoint, writes raw sources + wiki pages, and commits
  the result to git so the next ingest can diff against it.

  Bulk mode uses a resumable job queue with rate limiting and a circuit
  breaker so a whole-space ingest can be paused (Ctrl-C) and resumed with
  `/ingest --resume <job-id>` without re-fetching completed items.

  Use this skill whenever the user wants to add, update, refresh, re-ingest,
  import, backfill, or batch-import content into their wiki. Trigger on
  phrases like: "ingest this Confluence page", "add this Jira ticket to the
  wiki", "pull this URL into the wiki", "ingest this PDF", "process this
  document", "refresh this source", "ingest the FOO space", "backfill space
  FOO", "ingest all tickets matching this JQL", "resume the last bulk
  ingest".

  Requires a per-wiki `.wikirc.json` file with Confluence, Jira, and
  nano-banana endpoints and Personal Access Tokens. If the config is
  missing or scripts cannot import their dependencies, direct the user to
  references/setup.md before proceeding.
---

# Ingest — LLM Wiki

One skill covering both single-item ingest and bulk ingest of whole
Confluence spaces / CQL queries / Jira JQL queries. Auto-detects the mode.

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
automatically from arguments, but you should still tell the user which
path it will take so scope is confirmed.

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
| `--space FOO` | Bulk Confluence space |
| `--cql "…"` | Bulk Confluence CQL |
| `--jql "…"` | Bulk Jira JQL |
| `--resume <job-id>` | Resume a prior bulk job |

Explicit flags win over URL heuristics. A URL that matches both "single
page" and "space" resolves to single (the `/pages/` segment wins).
Ambiguous bare tokens (e.g., "FOO" — could be a space key or a slug) must
be disambiguated by asking the user or by requiring an explicit flag.

### Scope confirmation for bulk

Bulk mode with full per-item wiki synthesis is expensive (Claude tokens
and wall time). Before running a bulk job, tell the user:

> This will discover every page in space **FOO**, prefetch the raw content
> and images (rate-limited, resumable), and then I will synthesize wiki
> pages for each item in batches of N. For a large space this can take
> hours and touch every wiki category. Continue?

If they say yes, proceed. If they'd rather scope down, offer `--cql`
with a `label=…` or `updated > …` filter.

## Single-item workflow

Follow these phases in order. `${SKILL_DIR}` refers to the directory
containing this file. `${WIKI_ROOT}` is the wiki directory (contains
`.wikirc.json`).

### Phase 1 — Detect and fetch the source

```bash
python3 "${SKILL_DIR}/scripts/ingest.py" \
  --wiki-root "${WIKI_ROOT}" \
  --source "<Confluence URL | Jira key | local file path>"
```

The orchestrator prints a JSON summary. Parse it to know which files were
created and how many images were described.

### Phase 2 — Review takeaways with the user

After the orchestrator finishes, read the newly written `raw/<slug>.md`
and any image description files under `raw/images/<slug>/`. Then tell the
user:

> I ingested **[title]** from [source-type]. Key takeaways:
>
> - [3-6 bullet points on what the source is about]
>
> Images: [N new, M changed, K unchanged, described via nano-banana-pro]
>
> Ready to update the wiki? I'll create/update these pages:
> - [[proposed-page-1]] — [one-line reason]
> - [[proposed-page-2]] — [one-line reason]
> - Update [[existing-page]] with [what changes]

Wait for the user's confirmation or corrections before writing wiki pages.

### Phase 3 — Update wiki pages

Follow the rules in [references/page-format.md](references/page-format.md).
For each page you touch:

- Use the page template (Summary / Sources / Last updated / body).
- Add or update `[[wiki-links]]` to connect related concepts.
- Cite every factual claim: `(source: <raw-filename>)`.
- Update `wiki/index.md` with new pages and one-line descriptions.
- Append an entry to `wiki/log.md` with the date, source name, and what
  changed.

### Phase 4 — Commit to git

`ingest.py` handles this when `auto_commit=true` (the default). Or call
`ingest.py --commit-only --slug ...`. Commit message format:

```
ingest: <slug> (N new, M changed images)
```

## Bulk workflow

Bulk runs in three phases: **discovery**, **prefetch** (long-running, no
Claude needed), and **synthesis** (Claude-in-the-loop, batched).

### Bulk Phase 1 — Discover + prefetch

Run the orchestrator with a bulk source. It will invoke `discover.py`
(paginating the space/query into a queue) then `prefetch.py` (fetching
each item, downloading images, describing new/changed images).

```bash
python3 "${SKILL_DIR}/scripts/ingest.py" \
  --wiki-root "${WIKI_ROOT}" \
  --space FOO
# or:
#   --cql "space=FOO AND label=onboarding"
#   --jql "project=PROJ AND updated > -30d"
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
python3 "${SKILL_DIR}/scripts/ingest.py" \
  --wiki-root "${WIKI_ROOT}" \
  --resume "<job-id>"
```

`--resume` implies `--retry-failed`, so previously-failed items get
another attempt.

### Bulk Phase 2 — Confirm scope with the user

After prefetch completes, print the queue counts:

```bash
python3 "${SKILL_DIR}/scripts/queue_admin.py" --wiki-root "${WIKI_ROOT}" show <job-id>
```

Tell the user:

> Prefetch complete for job **<job-id>**: N pages fetched, M unchanged,
> K failed. Now I'll synthesize wiki pages in batches of B. After each
> batch I'll commit and pause for your OK. Continue?

### Bulk Phase 3 — Synthesis loop (Claude in the loop)

For each item with `raw_status in {done, unchanged}` and
`wiki_status == pending`:

1. Read `raw/<slug>.md` and every `raw/images/<slug>/*.md`.
2. Follow the single-item **Phase 3** (update wiki pages, index.md,
   log.md). Reuse existing pages when possible — bulk ingest is where a
   space's structure emerges, so many items will feed the same concept
   pages rather than each producing a new page.
3. Mark the item done:

    ```bash
    python3 "${SKILL_DIR}/scripts/queue_admin.py" --wiki-root "${WIKI_ROOT}" \
      mark <job-id> --ref <ref> --wiki-done
    ```

4. Every `--batch` items (default 5): `git commit -m "ingest: <job-id>
   [items X-Y]"` and ask the user whether to continue with the next batch.

If the user asks to skip an item (e.g., a draft page), mark it with
`--wiki-skipped`.

### Bulk Phase 4 — Final report

```bash
python3 "${SKILL_DIR}/scripts/queue_admin.py" --wiki-root "${WIKI_ROOT}" show <job-id>
```

Tell the user how many items are done, skipped, and (if any) still
failed. Offer to `/ingest --resume <job-id>` for failures.

## Concrete rules

- **Slug**: `slugify(title)` for Confluence, `KEY-123-<slug-of-summary>` for
  Jira, filename stem for local files. Enforced by `ingest.py`.
- **Raw layout**:
  - `raw/<slug>.md` — Markdown-converted source content
  - `raw/<slug>.source.json` — stable metadata:
    `{ "type", "url" or "path", "title", "content_sha256", "source_sha256"
    (local only), "image_hints", "version_number" (Confluence), "updated_at"
    (Jira) }`. **No wall-clock timestamps.**
  - `raw/images/<slug>/<n>.<ext>` — downloaded image bytes
  - `raw/images/<slug>/<n>.md` — nano-banana-pro description
  - `raw/images/<slug>/.manifest.json` — per-image `{ sha256, source_url,
    description_file, described_at }`; the source of truth for image diffs
    and URL-based dedup
- **Bulk queue layout**: `.wiki-state/bulk-jobs/<job-id>/queue.json` holds
  the queue for one bulk job. Git-ignored. Never referenced by wiki pages.
- **Volatile state lives outside git**:
  - `.wiki-state/last-fetched.json` at the wiki root records the timestamp
    and status of the most recent single-item fetch per slug.
  - `.wiki-state/bulk-jobs/` holds bulk job queues.
  - Both git-ignored via the template `.gitignore`.
- **Content diff gate (Layer 1)**: each fetcher computes a SHA-256 over the
  rendered Markdown. If it matches the previous `content_sha256`, the
  fetcher returns `status="unchanged"` and does not rewrite `raw/<slug>.md`
  or `raw/<slug>.source.json`. `fetch_local.py` also fast-paths on
  `source_sha256` (the raw file bytes) to avoid re-parsing PDFs/DOCX. In
  bulk mode, unchanged items skip the image and description steps.
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
- **Rate limiting**: every HTTP call to Atlassian and nano-banana-pro goes
  through `rate_limiter.py`. Config lives in `.wikirc.json` under
  `atlassian.rate_limit_rps` / `.burst` / `.max_retries` /
  `.retry_base_delay_seconds` (and the same keys under `nano_banana`).
  Defaults: Atlassian 2 rps / burst 5, nano-banana 1 rps / burst 2. On
  HTTP 429 or 503, the limiter respects `Retry-After` (seconds or
  HTTP-date) and otherwise backs off exponentially with jitter. After
  `max_retries` a request fails and (in bulk mode) the item's queue entry
  goes to `failed`.
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
| `scripts/extract_images.py --wiki-root <p> --source-json <f>` | Download image_hints for a slug |
| `scripts/image_manifest.py --wiki-root <p> --slug <s> status` | Print per-image diff status |
| `scripts/describe_image.py --wiki-root <p> --image <p> --output <p>` | Describe one image |
| `scripts/discover.py --wiki-root <p> --space/--cql/--jql <q>` | Enumerate items, write a job queue |
| `scripts/prefetch.py --wiki-root <p> --job-id <id>` | Bulk-fetch items in a queue, resumable |
| `scripts/queue_admin.py --wiki-root <p> list \| show \| reset \| mark \| delete` | Inspect and manage job queues |

All scripts respond to `--help` with their full argument list.

## Edge cases

- **Confluence page has no body** (e.g. draft): warn the user and stop —
  do not create an empty raw file.
- **Jira ticket not found or 403**: surface the API status code to the
  user and suggest checking the PAT.
- **Local file type unsupported** (e.g. `.xlsx`): skill exits with a list
  of supported types. Suggest exporting to PDF or Markdown.
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
- **User manually deleted a file under `raw/`**: the diff gate still sees
  the source as unchanged (source hash matches) and won't restore the
  file. Advise the user to run with `--force` (single) or
  `queue_admin.py reset` (bulk).
- **Bulk: user Ctrl-C's during prefetch**: safe. The queue is
  checkpointed after every item. Resume with `/ingest --resume <job-id>`.
- **Bulk: rate limit hit**: `rate_limiter.py` transparently retries with
  Retry-After. If a single request exhausts `max_retries`, the item is
  marked `failed`; the circuit breaker aborts the whole run after 5
  consecutive failures. User backs off and resumes.
- **Bulk: same query re-run**: `discover.py` detects the matching queue
  and reuses it (skips re-enumeration). Pass `--replace` to overwrite.

## Reference docs

| Doc | Load when |
|-----|-----------|
| [references/setup.md](references/setup.md) | User hits any dependency or config error, or is setting up for the first time |
| [references/atlassian-api.md](references/atlassian-api.md) | Debugging Confluence/Jira fetches or CQL/JQL queries |
| [references/local-files.md](references/local-files.md) | Debugging local file parsing or supporting a new format |
| [references/page-format.md](references/page-format.md) | Wiki-update phase — page template and citation rules |
