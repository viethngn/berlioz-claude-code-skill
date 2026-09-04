# LLM Wiki

Four skills for maintaining a personal LLM knowledge wiki, backed by git.

- **`/ingest`** — Pull content from a Confluence page, Jira issue, local file
  (Markdown, HTML, PDF, DOCX, XLSX, CSV, PPTX, image — all parsed natively, no
  model dependency), **or** Slack channel / thread / search results, **or** a
  public website page, **or** bulk-ingest a whole Confluence space / CQL query /
  JQL query / website sitemap.
  The skill auto-detects single vs bulk from the source shape or explicit flags.
  Extracts embedded images, describes them via a nano-banana-pro-compatible
  vision endpoint **only when they change**, and updates the wiki. Bulk mode
  uses a resumable job queue with rate limiting and a circuit breaker. Commits
  raw + wiki to git and optionally pushes to remote.
- **`/lint`** — Thoroughly clean the wiki. Auto-removes empty pages, triages
  orphans (integrate valuable ones back into the graph, delete duplicates,
  archive the rest), fixes broken `[[wiki-links]]` and format violations, and
  archives outdated knowledge by **moving it to `wiki/archive/`** so reads
  always surface the latest info. Contradictions, outdated facts, **and the
  deletion of empty non-contributing `raw/` source files** are batched into one
  report you confirm before applying (`raw/` contents are never edited). Updates
  `index.md`, appends a `log.md` entry, then commits (grouped by category) and
  pushes.
- **`/create-wiki`** — Bootstrap a fresh LLM wiki in one run: folder layout,
  `CLAUDE.md` system prompt, page template, git repo, and marketplace pinning so
  `/ingest` and `/lint` are auto-discovered on next session. Also **installs +
  verifies the Python dependencies automatically** (idempotent) and creates a
  ready-to-edit `.wikirc.json` — the only thing left is filling in your
  credentials.
- **`/log`** — Turn a meeting note, transcript, or pasted text into structured
  Event entries on a per-day page (`wiki/YYYY-MM-DD.md` — a flat page like any
  other, no new folder). Each Event captures Action/What/When/Where/Who/Why,
  links to related past Events, and Next steps. What/Who resolve as read-only
  links into other configured `llm-wiki` instances (via `linked_wikis` in
  `.wikirc.json`) — never written into those wikis. Bare `/log` auto-scans for
  raw sources not yet reflected in a day-page. `/ingest` can also opt in to
  cascading a matching source into a linked wiki via a cheap-model subagent —
  see `linked_wikis[].cascade_ingest` below.

Vendor-neutral: no hardcoded URLs or product names. Every endpoint comes from
your `.wikirc.json`.

## One-time setup

> **Fastest path — just run `/create-wiki`.** After adding the marketplace and
> installing the plugin (step 1 below), asking Claude to create a wiki runs the
> whole first-time setup for you: it scaffolds the repo, **installs and verifies
> the Python dependencies automatically** (only if missing), and creates a
> ready-to-edit `.wikirc.json`. The only thing left is filling in your
> credentials. Steps 2–3 below are the **manual/CI equivalent** — you don't need
> to run them by hand when you use `/create-wiki`.

### 1. Add the marketplace and install the plugin

```
/plugin marketplace add /absolute/path/to/berlioz-claude-code-skill
/plugin install llm-wiki@berlioz-claude-code-skill
```

### 2. Install Python dependencies (automatic via `/create-wiki`)

Requires Python 3.10+ and `git` on your PATH. `/create-wiki` runs this for you;
run it by hand only for CI or when managing deps manually:

```bash
bash /absolute/path/to/berlioz-claude-code-skill/plugins/llm-wiki/install.sh
```

The installer tries, in order:

1. `uv pip install --system` (if `uv` is available — fastest)
2. **virtualenv at `~/.llm-wiki-venv`** (recommended on Homebrew / PEP 668 systems)
3. `pip install --user`
4. `pip install` (system-wide, last resort)

On macOS with Homebrew Python (Python 3.12+), option 2 runs automatically and
creates a dedicated venv. You don't need `uv` or `--break-system-packages`.

To use a custom venv location:
```bash
LLMWIKI_VENV=/your/custom/path bash install.sh
```

After installation, `check-setup.sh` auto-detects `~/.llm-wiki-venv` as the
Python source. Override with `LLMWIKI_VENV` or `PYTHON` env vars.

If your machine has no direct PyPI access, see
[skills/ingest/references/setup.md](skills/ingest/references/setup.md) for
offline-install patterns (private mirror, offline wheels, `pip.conf`).

### 3. Verify the install (automatic via `/create-wiki`)

```bash
bash /absolute/path/to/berlioz-claude-code-skill/plugins/llm-wiki/check-setup.sh
```

Reports Python version, imports each dep, and checks for `git`. Optionally
pass a `.wikirc.json` path as an argument to validate the config file too.
`/create-wiki` runs this automatically; run it by hand only to re-check an
existing setup.

### 4. Bootstrap a wiki (if you don't have one yet)

Ask Claude:

> Create a new LLM wiki in `~/my-wiki`.

The `/create-wiki` skill lays out the folders, drops in `CLAUDE.md` and a
starter `.wikirc.example.json`, runs `git init`, **installs + verifies the
Python dependencies** (skipped if already present), and creates a ready-to-edit
`.wikirc.json` — then prints the marketplace install commands with the correct
absolute paths.

### 5. Fill in `.wikirc.json`

`/create-wiki` already created `.wikirc.json` for you (from the example, with
placeholders) — just open it and fill in your endpoints and tokens. (If you're
setting up by hand instead, copy `.wikirc.example.json` to `.wikirc.json`
yourself.) The file is git-ignored — never committed. Each integration is
optional; leave one empty and that source type is simply skipped.

```json
{
  "wiki_root": ".",
  "raw_dir": "raw",
  "wiki_dir": "wiki",
  "auto_commit": true,
  "auto_push": false,
  "git": {
    "remote": "origin",
    "branch": ""
  },
  "linked_wikis": [
    {
      "label": "people",
      "path": "/absolute/path/to/your-people-wiki",
      "role": "who",
      "purpose": "Colleague profiles: roles, working style, org structure",
      "cascade_ingest": false
    },
    {
      "label": "topics",
      "path": "/absolute/path/to/your-topic-wiki",
      "role": "what",
      "purpose": "Product/topic knowledge for your team",
      "cascade_ingest": false
    }
  ],
  "atlassian": {
    "confluence_base_url": "https://your-confluence.example.com",
    "jira_base_url": "https://your-jira.example.com",
    "confluence_pat": "YOUR_CONFLUENCE_PAT_OR_EMPTY",
    "jira_pat": "YOUR_JIRA_PAT_OR_EMPTY",
    "verify_ssl": true,
    "rate_limit_rps": 2,
    "burst": 5,
    "max_retries": 5,
    "retry_base_delay_seconds": 2
  },
  "nano_banana": {
    "base_url": "https://your-nano-banana-endpoint.example.com/v1/",
    "api_key": "YOUR_NANO_BANANA_API_KEY",
    "vision_model": "gemini-3-pro",
    "verify_ssl": true,
    "rate_limit_rps": 1,
    "burst": 2,
    "max_retries": 3,
    "retry_base_delay_seconds": 2
  },
  "slack": {
    "token": "xoxp-YOUR_USER_OAUTH_TOKEN",
    "verify_ssl": true,
    "rate_limit_rps": 1,
    "burst": 3,
    "max_retries": 5,
    "retry_base_delay_seconds": 2
  },
  "web": {
    "user_agent": "Mozilla/5.0 (compatible; llm-wiki-ingest/1.0)",
    "verify_ssl": true,
    "rate_limit_rps": 1,
    "burst": 2,
    "max_retries": 3,
    "retry_base_delay_seconds": 2,
    "timeout_seconds": 30,
    "respect_robots": true,
    "min_image_bytes": 8192,
    "extra_headers": {}
  }
}
```

**`auto_push`** — set to `true` to push to the configured `git.remote` after
every commit. Push failures warn but never fail the ingest; the local commit is
always preserved. Credential resolution is delegated to Git — set up an SSH key
or `git credential-osxkeychain` (macOS) / `git-credential-store` once and it
just works. No token goes in `.wikirc.json`.

**`linked_wikis`** — other `llm-wiki` instances this wiki can cross-link into
via `[[label/slug]]` (used by `/log` to resolve Who/What mentions read-only —
never writing into the linked wiki) and, if `cascade_ingest: true`, that
`/ingest` can automatically re-ingest a matching source into, via a
`model: haiku` subagent. The two entries above are illustrative placeholders
(`/absolute/path/to/your-...-wiki`) — entirely optional and inert until you
replace them with real paths, or delete them to leave the array empty.

**`slack.token`** — a Slack User OAuth Token (`xoxp-…`). See
[How to get a Slack token](#how-to-get-a-slack-token) below.

**`web.*`** — website ingest. **Entirely optional** — every key has a default
and public pages need no credentials, so you can leave this block out. Reach for
it when a site returns `403` to the default User-Agent (set `user_agent` to a
browser string), when a page is behind a login (put a `Cookie` or
`Authorization` header in `extra_headers` — `config.py` redacts the values when
printing), or when you want to loosen `min_image_bytes` so smaller diagrams get
described. `extra_headers` reaches only the entry-point **origin** (scheme+host+
port): an image embedded from a third-party CDN never receives them, nor does a
foreign host whose `robots.txt` or sitemap gets fetched during discovery, nor
does the same hostname over a different scheme. They do survive same-origin
redirects, which `requests` would otherwise silently strip. `respect_robots` is
`true` by default: robots.txt is **enforced** (per origin, so a sitemap entry on
another host is checked against that host's own rules) on bulk website ingest,
and advisory for a single page you name explicitly.

URLs with embedded credentials (`https://user:pass@host/…`) are rejected — they
would land in a filename and in committed metadata. Use `extra_headers`.

Rate-limit knobs are optional. Defaults are conservative so a first-time bulk
run won't get anyone rate-banned. Every HTTP call flows through `rate_limiter.py`,
which respects `Retry-After` on 429/503 and uses exponential backoff with jitter.

#### How to get a Slack token

1. Go to **https://api.slack.com/apps** → **Create New App** → **From scratch**.
2. Name it (e.g. `llm-wiki`), select your workspace.
3. Under **OAuth & Permissions** → **User Token Scopes**, add:
   `channels:history`, `channels:read`, `groups:history`, `groups:read`,
   `im:history`, `mpim:history`, `search:read`, `users:read`.
4. Click **Install App to Workspace** → authorise.
5. Copy the **User OAuth Token** (starts with `xoxp-`).
6. Paste it into `.wikirc.json` under `slack.token`.

A User token (not a Bot token) is required so `search.messages` can reach
private channels you have access to.

## Usage

### Ingest a Confluence page

> /ingest https://your-confluence.example.com/pages/12345678/Feature-Name

### Ingest a Jira issue

> /ingest PROJ-123
>
> Or: /ingest https://your-jira.example.com/browse/PROJ-123

### Ingest a local file

> /ingest ~/Documents/spec.pdf
>
> /ingest ~/Downloads/notes.docx
>
> /ingest ~/Downloads/budget.xlsx
>
> /ingest ~/Downloads/deck.pptx

`.pdf`, `.docx`, `.xlsx`, `.csv`, and `.pptx` are all parsed **natively** —
text, tables, and embedded images are extracted deterministically, with no AI
model in the loop. (Legacy binary `.xls`/`.ppt`/`.doc` have no native parser
and fall back to model-assisted synthesis.)

The original file is **copied into `raw/<slug>.<ext>`** so the wiki owns its
source: the diff check hashes that copy (not the external path), so re-ingest
keeps working even if the original moves or is deleted. Documents, spreadsheets,
and presentations are committed; image/video/audio originals are copied but
git-ignored (kept local).

### Ingest a web page

> /ingest https://docs.example.com/guides/getting-started

No credentials needed for public pages. The HTML is extracted with
[trafilatura](https://trafilatura.readthedocs.io/) (falling back to
BeautifulSoup + markdownify), which strips navigation, sidebars, cookie
banners, and related-article blocks so `raw/<slug>.md` is the article and
nothing else.

The slug comes from the **URL**, not the page title
(`web-docs-example-com-guides-getting-started`), so a retitled page updates its
existing raw file instead of creating a duplicate. Re-ingesting replays the
stored `ETag` as `If-None-Match` — an unchanged page costs a single `304` and
never gets re-parsed or re-committed.

Images are filtered hard before any vision call: only images inside the
extracted content survive, and logos, icons, spacers, SVGs, tracking pixels,
and anything under `web.min_image_bytes` are dropped. Diagrams and screenshots
go through the normal describe-on-change flow.

A JavaScript-rendered page has nothing to extract from its HTML — save it from
your browser (Save As → Web Page, Complete) and ingest the `.html` file instead.

### Bulk-ingest a website

> /ingest --site https://docs.example.com

Finds the sitemap the way a crawler would: `Sitemap:` directives in
`robots.txt` first, then `/sitemap.xml` and friends. Sitemap indexes and
gzipped sitemaps are followed automatically. Then it's the same resumable
queue as a Confluence space — prefetch every page, then synthesize wiki pages
one commit at a time.

Pass an exact sitemap URL if you have one (also auto-detected from `/ingest`
with no flag):

> /ingest --sitemap https://docs.example.com/sitemap_index.xml

Scope a large site — these compose, and all of them beat crawling:

> /ingest --site https://docs.example.com --include '/docs/' --exclude '/blog/'
>
> /ingest --site https://docs.example.com --since 2026-06-01
>
> /ingest --site https://docs.example.com --limit 10

`--since` filters on the sitemap's `<lastmod>`, which makes a periodic refresh
cheap. Re-running the same `--site` URL reuses its existing queue, so refreshing
a site is just the same command again — but if you change a filter, the reuse is
refused with an `options_changed` report rather than silently handing back the
old, differently-scoped queue. Add `--replace` to rebuild with the new scope.

**Sites with no sitemap** don't get crawled blind. Discovery stops and asks you
for a depth and a page cap first:

> /ingest --crawl https://example.com --depth 2 --max-pages 100

The crawl is breadth-first, same-origin, HTML-only, and honors robots.txt
including `Crawl-delay`. `robots.txt` is enforced on every bulk website path;
`--ignore-robots` overrides it, for sites where you have permission.

### Bulk-ingest a whole Confluence space

> /ingest --space FOO
>
> Or paste the space URL (auto-detected):
> /ingest https://your-confluence.example.com/spaces/FOO

Discovery paginates the space, prefetch downloads every page (rate-limited
and resumable), and then Claude synthesizes wiki pages automatically —
committing and pushing one commit per item, with no pauses.

### Bulk-ingest a filtered slice

> /ingest --cql "space=FOO AND label=onboarding AND lastModified > now(-30d)"
>
> /ingest --jql "project=PROJ AND updated > -30d AND labels = 'runbook'"

Use CQL/JQL to scope down big spaces before running a full bulk ingest.

### Resume a bulk job

If prefetch got interrupted (Ctrl-C, rate-limit circuit breaker,
disconnect), continue where it left off:

> /ingest --resume conf-space-foo-20260703-005211

`queue_admin.py list` prints every known job id and its counts.

### Ingest a Slack channel (new messages since last ingest)

> /ingest --slack-channel general

Fetches all messages since the last `fetched_until` timestamp. The slug encodes
the actual message date range (e.g. `slack-general-20260715-20260720`). Re-running
before new messages arrive returns "unchanged" — nothing is committed.

### Ingest a Slack channel with a date window

> /ingest --slack-channel general --after 2026-07-01 --before 2026-07-20

### Ingest a specific Slack thread

> /ingest --slack-channel general --thread-ts 1234567890.123456

### Ingest Slack search results

> /ingest --slack-search "topic:decision after:2026-07-01"

### Bring the whole wiki up to date

> /ingest

With no source at all, `/ingest` refreshes everything: it re-fetches every
source the wiki already holds — Confluence pages, Jira issues, web pages, local
files, Slack channels and threads — plus a fresh re-enumeration of every bulk
query it has run, so pages *added* to a tracked space or sitemap are picked up
too. Each source is diffed against its `raw/` copy, and only what actually
changed is re-synthesized into wiki pages.

On a wiki that's already current this ends with nothing to do and nothing
committed. That's the expected result.

It runs on the same resumable queue as a bulk ingest (job id `refresh`), so
Ctrl-C is safe:

> /ingest --resume refresh

Above ~200 sources it stops after enumerating and asks first, since a full sweep
can mean hours of API calls. Add `--yes` for unattended runs, or `--force` to
re-fetch and re-synthesize everything regardless of whether it changed.

Two things a refresh deliberately does **not** do: it never deletes anything (a
page removed upstream is reported so you can run `/lint`), and it doesn't re-run
ad hoc Slack searches, whose results shift over time.

### Log a meeting or decision

> /log ~/Downloads/2026-09-04-standup-transcript.txt

Or paste notes directly:

> /log
>
> [paste meeting notes]

Extracts one or more Events (Action/What/When/Where/Who/Why/Next steps) and
files each onto `wiki/YYYY-MM-DD.md` — a flat page, not a new folder. `What`/
`Who` resolve as `[[label/slug]]` links into whichever `linked_wikis` entry
matches by role, read-only.

### Catch up the diary

> /log

Bare `/log` scans this wiki's `raw/` for sources not yet reflected in a
day-page and processes only what's pending.

### Lint the wiki

> /lint

### Bootstrap a new wiki

> /create-wiki in ~/projects/my-team-wiki

## What each skill does

### `/ingest`

1. Detect source type (Confluence URL / Jira key / local file / web URL).
2. Fetch and convert to Markdown, then compare against `raw/<slug>.md` on disk.
3. **Content diff gate.** If the freshly-rendered Markdown matches the previous
   `content_sha256`, mark the source `unchanged` and skip everything else —
   no image download, no vision calls, no git commit. Only
   `.wiki-state/last-fetched.json` is updated. Pass `--force` to override.
4. Otherwise, download each `image_hint` into memory, hash it, and reconcile
   against the manifest at `raw/images/<slug>/.manifest.json` — matching URLs
   overwrite in place, matching hashes reuse the existing filename, new
   content lands at the next available index. No duplicates on re-ingest.
5. For images that end up genuinely new or changed, call the nano-banana-pro
   vision endpoint to produce a text description at `raw/images/<slug>/<n>.md`.
   Unchanged images reuse the existing description.
6. Print key takeaways for your awareness (non-blocking — no pause).
7. Create or update pages in `wiki/` following your `CLAUDE.md` rules
   (page format, `[[wiki-links]]`, citations, `wiki/index.md`, `wiki/log.md`).
8. Commit raw + wiki together in a single commit via
   `ingest.py --commit-only --slug <slug>` (message
   `ingest: <slug> (N new, M changed images)`), then push when
   `auto_push=true`. The commit is skipped automatically if nothing staged
   actually differs. The whole flow — fetch → synthesize → commit → push —
   runs end-to-end without waiting for confirmation.

**Diff-gate summary:**

| Layer | Where | What it compares |
|-------|-------|------------------|
| 0. Conditional GET (web only) | `fetch_web.py` | Stored `ETag` / `Last-Modified` replayed as `If-None-Match` / `If-Modified-Since`; a 304 skips the download and parse entirely |
| 1. Source-file gate (local only) | `fetch_local.py` | SHA-256 of the raw file bytes; skips parsing PDFs/DOCX when unchanged |
| 2. Content-diff gate | `raw_store.write_raw_if_changed` | SHA-256 of the rendered Markdown; skips rewriting `raw/<slug>.md` + `raw/<slug>.source.json` when unchanged |
| 3. Image dedup gate | `extract_images.py` | Manifest lookup by `source_url`, then by SHA-256; prevents duplicate files |
| 4. Description gate | `image_manifest.py.classify()` | SHA-256 + presence of a description file; only new/changed images invoke nano-banana-pro |

Pass `--force` to `ingest.py` to bypass gates 0, 1 and 2 for a full refresh.

**Bulk mode** (Confluence space / CQL / JQL / website / `--resume`) reuses the
same per-item flow but routes it through three phases:

1. **Discovery** — [`discover.py`](skills/ingest/scripts/discover.py)
   paginates the space/query with the Atlassian rate limiter, or (for a
   website) enumerates the sitemap via
   [`web_discover.py`](skills/ingest/scripts/web_discover.py), and writes
   `.wiki-state/bulk-jobs/<job-id>/queue.json`. If a queue for the same
   `(kind, query)` already exists, it's reused; pass `--replace` to
   overwrite. For a website with no sitemap, discovery deliberately stops and
   asks for explicit `--depth` / `--max-pages` bounds instead of crawling.
2. **Prefetch** — [`prefetch.py`](skills/ingest/scripts/prefetch.py)
   iterates pending items, invoking the single-item fetchers via
   subprocess so the diff gates fire per page. Every item is checkpointed
   to `queue.json`, so Ctrl-C is always safe. A circuit breaker aborts
   the run after 5 consecutive item failures.
3. **Synthesis** — Claude reads items with
   `raw_status in {done, unchanged}` and `wiki_status == pending`, writes
   wiki pages, and commits + pushes one commit per item via
   `ingest.py --commit-only` automatically — no per-batch pause.

**Refresh mode** (`/ingest` with no source) is a fourth queue kind rather than a
fourth code path — it reuses discovery → prefetch → synthesis exactly as above,
including resumability, rate limiting and the circuit breaker. Only discovery
differs: [`discover.py --refresh`](skills/ingest/scripts/discover.py) builds one
queue (job id `refresh`) from
[`list_sources.py`](skills/ingest/scripts/list_sources.py)'s inventory of every
`raw/*.source.json`, merged with a fresh re-enumeration of every bulk query in
`raw/.bulk-queries.json`. Both sides key items by the same ref (page id / issue
key / URL), so a page ingested individually *and* via a space query is fetched
once. Items that already have a raw file are pre-marked as synthesized, so an
unchanged refetch queues no work — the diff gates above decide what's real.

Inspect and re-queue with
[`queue_admin.py`](skills/ingest/scripts/queue_admin.py):

```bash
python3 .../queue_admin.py --wiki-root <path> list
python3 .../queue_admin.py --wiki-root <path> show <job-id>
python3 .../queue_admin.py --wiki-root <path> reset <job-id> --status failed
python3 .../queue_admin.py --wiki-root <path> mark <job-id> --ref <ref> --wiki-done
```

The script is named `queue_admin.py` rather than `queue.py` because
scripts under this directory get added to `sys.path[0]` at runtime, so a
top-level `queue.py` would shadow the stdlib `queue` module (which
`urllib3` imports).

### `/lint`

Runs [lint.py](skills/lint/scripts/lint.py) to build a JSON report of orphaned
pages, broken links, missing concept pages, format violations, empty/stub
pages, stale pages, unsourced claims, `Status`-tagged pages, `Sources` paths
that no longer exist in `raw/`, and `empty_raw` (empty, uncited raw source
files). Then Claude:

1. **Auto-cleans structure** (no approval): deletes empty pages, fixes broken
   links, triages orphans (integrate / delete / archive), repairs format
   violations.
2. **Verifies conflicts + raw deletions with you**: contradictions, outdated
   facts, and any empty-raw prune candidates are gathered into a single report;
   you approve before they apply.
3. **Archives retired pages by moving them to `wiki/archive/`**: the page
   leaves active lint scope but stays in git and linkable (references to it
   aren't broken), and is tagged under an `## Archive` section of `index.md`.
   Every read is routed to the current page.
4. **Prunes empty raw sources (gated)**: title-only container pages that no
   wiki page cites are deleted with their `.source.json` (+ any images dir),
   only after you confirm. `raw/` file *contents* are never edited; non-empty
   and cited raws are never deleted.
5. **Maintains logs**: appends a `## <date> (lint)` entry to `wiki/log.md` and
   updates `index.md`.
6. **Commits + pushes**: one commit per category, then `ingest.py --push-only`
   pushes them all (gated on `auto_push`).

### `/log`

1. Get the raw material into `raw/` — a local file goes through `/ingest`'s
   own local-file dispatch unchanged; pasted text is written via a new
   `write_raw_note.py` (same diff-gated `raw_store.write_raw_if_changed`
   every fetcher uses); bare `/log` lists sources not yet logged via
   `log_state.py --pending` (tracked in `.wiki-state/last-logged.json`,
   independent of `/ingest`'s own `last-fetched.json`).
2. Extract discrete Events (Action/What/When/Where/Who/Why/Related
   events/Next steps) from the raw text.
3. Resolve `What`/`Who` mentions read-only against `linked_wikis` (by role)
   via `resolve_link.py`, which shells out to the target wiki's own
   `scripts/wiki_search.sh` if present, else falls back to ripgrep. A
   confident match becomes `[[label/slug]]`; no match is left as plain text
   with an unresolved marker — **never** written into the linked wiki.
4. Write/update the correct `wiki/YYYY-MM-DD.md` day-page (creating it from
   `templates/day-page.md` if needed), inserting the Event in time-of-day
   order. A correction to an already-logged Event appends a dated
   `Update`/`Amends` block rather than editing it in place.
5. Update `wiki/index.md`'s Diary section and append one entry to the
   operational `wiki/log.md` — a separate file from the day-pages themselves.
6. Commit (and push, if `auto_push`) via `ingest.py --commit-only`, reused
   as-is.

### `/create-wiki`

Runs [bootstrap.py](skills/create-wiki/scripts/bootstrap.py) to lay out the
directory structure, copy templates (`CLAUDE.md`, `index.md`, `log.md`, page
template, `.wikirc.example.json`, `.gitignore`, `.claude/settings.json`), and
initialize git. It then performs first-time setup end-to-end: creates a
ready-to-edit `.wikirc.json`, and checks the Python dependencies via
`check-setup.sh`, installing them via `install.sh` **only if missing** and
re-verifying — all resolved relative to the installed plugin, so it works on
any machine. Finally it prints marketplace install commands and the remaining
manual step (fill in `.wikirc.json` credentials). Pass `--skip-deps` to skip the
automatic dependency install/verify for CI or air-gapped setups.

## File layout of an LLM wiki

```
my-wiki/
├── .claude/settings.json     # pins the marketplace so /ingest and /lint auto-discover
├── .gitignore                # ignores .wikirc.json, .wiki-state/, tmp files
├── .wiki-state/              # git-ignored; volatile per-machine state
│   ├── last-fetched.json     # last-fetch timestamp + status per slug (single mode)
│   ├── last-logged.json      # last-/log-ed timestamp + day-pages touched per slug
│   └── bulk-jobs/            # one directory per bulk-ingest job
│       ├── <job-id>/queue.json  # discovery output + per-item status
│       └── refresh/queue.json   # the single whole-wiki refresh queue
├── .wikirc.json              # your endpoints + PATs (git-ignored)
├── .wikirc.example.json      # example config, committed
├── CLAUDE.md                 # wiki system prompt
├── raw/                      # immutable ingested sources
│   ├── .bulk-queries.json    # COMMITTED — bulk queries this wiki tracks, so `/ingest` knows what to re-check
│   ├── <slug>.md             # rendered Markdown
│   ├── <slug>.source.json    # stable metadata: rel path, title, content_sha256, source_sha256, image_hints (NO fetched_at)
│   ├── <slug>.<ext>          # local ingests: original copied in. Docs/sheets/decks COMMITTED; media git-ignored
│   └── images/<slug>/
│       ├── .manifest.json    # COMMITTED — sha256 + source_url per image (SHA baseline for diffs and dedup)
│       ├── <n>.<ext>         # git-ignored, LOCAL ONLY — image bytes, re-downloaded each ingest
│       └── <n>.md            # COMMITTED — nano-banana-pro description (cache; avoids re-describing)
├── wiki/                     # Claude-maintained pages
│   ├── index.md
│   ├── log.md                # operational log (every /ingest, /lint, /log run) — distinct from day-pages below
│   ├── <page>.md
│   ├── YYYY-MM-DD.md         # day-pages written by /log — flat pages, same as any other
│   └── archive/              # retired pages moved here by /lint (out of active scope, still linkable)
│       └── <old-page>.md
└── templates/
    ├── page.md
    └── day-page.md           # seed template for a new day-page (used by /log)
```

## Reference documents

- [skills/ingest/references/setup.md](skills/ingest/references/setup.md) —
  Step-by-step setup guide including offline install patterns.
- [skills/ingest/references/atlassian-api.md](skills/ingest/references/atlassian-api.md) —
  Confluence and Jira REST endpoints, PAT auth, storage-format tips.
- [skills/ingest/references/local-files.md](skills/ingest/references/local-files.md) —
  Per-format parsing notes (PDF, DOCX, XLSX, CSV, PPTX, HTML, images).
- [skills/ingest/references/web-pages.md](skills/ingest/references/web-pages.md) —
  Web extraction pipeline, slug scheme, sitemap/robots discovery, crawl bounds,
  image filtering, and troubleshooting.
- [skills/ingest/references/page-format.md](skills/ingest/references/page-format.md) —
  Wiki page template and citation rules.
- [skills/log/references/event-format.md](skills/log/references/event-format.md) —
  Event fields, day-page template, and the immutability/backfill rule.
- [skills/log/references/cross-wiki-links.md](skills/log/references/cross-wiki-links.md) —
  The `[[label/slug]]` convention and its accepted `/lint`/Obsidian limitations.

## Requirements

- Python 3.10 or newer
- `git` on PATH
- `bash`
- Access to your Confluence, Jira, and nano-banana-pro endpoints
- Personal Access Tokens for Confluence and Jira (either optional if you don't
  use that source)
- Nothing extra for website ingest — public pages need no credentials
