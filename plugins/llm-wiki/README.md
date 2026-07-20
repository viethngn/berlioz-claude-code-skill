# LLM Wiki

Three skills for maintaining a personal LLM knowledge wiki, backed by git.

- **`/ingest`** — Pull content from a Confluence page, Jira issue, local file
  (Markdown, HTML, PDF, DOCX, image), **or** Slack channel / thread / search
  results, **or** bulk-ingest a whole Confluence space / CQL query / JQL query.
  The skill auto-detects single vs bulk from the source shape or explicit flags.
  Extracts embedded images, describes them via a nano-banana-pro-compatible
  vision endpoint **only when they change**, and updates the wiki. Bulk mode
  uses a resumable job queue with rate limiting and a circuit breaker. Commits
  raw + wiki to git and optionally pushes to remote.
- **`/lint`** — Thoroughly clean the wiki. Auto-removes empty/orphaned pages,
  fixes broken `[[wiki-links]]` and format violations, and archives outdated
  knowledge with a status banner (never touching `raw/`) so reads always surface
  the latest info. Contradictions and outdated facts are batched into one report
  you confirm before applying. Updates `index.md`, appends a `log.md` entry, then
  commits (grouped by category) and pushes.
- **`/create-wiki`** — Bootstrap a fresh LLM wiki: folder layout, `CLAUDE.md`
  system prompt, page template, git repo, `.wikirc.json` config, and marketplace
  pinning so `/ingest` and `/lint` are auto-discovered on next session.

Vendor-neutral: no hardcoded URLs or product names. Every endpoint comes from
your `.wikirc.json`.

## One-time setup

### 1. Add the marketplace and install the plugin

```
/plugin marketplace add /absolute/path/to/berlioz-claude-code-skill
/plugin install llm-wiki@berlioz-claude-code-skill
```

### 2. Install Python dependencies

Requires Python 3.10+ and `git` on your PATH.

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

### 3. Verify the install

```bash
bash /absolute/path/to/berlioz-claude-code-skill/plugins/llm-wiki/check-setup.sh
```

Reports Python version, imports each dep, and checks for `git`. Optionally
pass a `.wikirc.json` path as an argument to validate the config file too.

### 4. Bootstrap a wiki (if you don't have one yet)

Ask Claude:

> Create a new LLM wiki in `~/my-wiki`.

The `/create-wiki` skill will lay out the folders, drop in `CLAUDE.md` and a
starter `.wikirc.example.json`, run `git init`, and print the marketplace
install commands with the correct absolute paths.

### 5. Fill in `.wikirc.json`

Copy `.wikirc.example.json` to `.wikirc.json` and fill in your endpoints and
tokens. The file is git-ignored — never committed.

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
  }
}
```

**`auto_push`** — set to `true` to push to the configured `git.remote` after
every commit. Push failures warn but never fail the ingest; the local commit is
always preserved. Credential resolution is delegated to Git — set up an SSH key
or `git credential-osxkeychain` (macOS) / `git-credential-store` once and it
just works. No token goes in `.wikirc.json`.

**`slack.token`** — a Slack User OAuth Token (`xoxp-…`). See
[How to get a Slack token](#how-to-get-a-slack-token) below.

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

### Lint the wiki

> /lint

### Bootstrap a new wiki

> /create-wiki in ~/projects/my-team-wiki

## What each skill does

### `/ingest`

1. Detect source type (Confluence URL / Jira key / local file).
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
| 1. Source-file gate (local only) | `fetch_local.py` | SHA-256 of the raw file bytes; skips parsing PDFs/DOCX when unchanged |
| 2. Content-diff gate | `raw_store.write_raw_if_changed` | SHA-256 of the rendered Markdown; skips rewriting `raw/<slug>.md` + `raw/<slug>.source.json` when unchanged |
| 3. Image dedup gate | `extract_images.py` | Manifest lookup by `source_url`, then by SHA-256; prevents duplicate files |
| 4. Description gate | `image_manifest.py.classify()` | SHA-256 + presence of a description file; only new/changed images invoke nano-banana-pro |

Pass `--force` to `ingest.py` to bypass gates 1 and 2 for a full refresh.

**Bulk mode** (Confluence space / CQL / JQL / `--resume`) reuses the same
per-item flow but routes it through three phases:

1. **Discovery** — [`discover.py`](skills/ingest/scripts/discover.py)
   paginates the space/query with the Atlassian rate limiter and writes
   `.wiki-state/bulk-jobs/<job-id>/queue.json`. If a queue for the same
   `(kind, query)` already exists, it's reused; pass `--replace` to
   overwrite.
2. **Prefetch** — [`prefetch.py`](skills/ingest/scripts/prefetch.py)
   iterates pending items, invoking the single-item fetchers via
   subprocess so the diff gates fire per page. Every item is checkpointed
   to `queue.json`, so Ctrl-C is always safe. A circuit breaker aborts
   the run after 5 consecutive item failures.
3. **Synthesis** — Claude reads items with
   `raw_status in {done, unchanged}` and `wiki_status == pending`, writes
   wiki pages, and commits + pushes one commit per item via
   `ingest.py --commit-only` automatically — no per-batch pause.

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
pages, stale pages, unsourced claims, `Status`-tagged pages, and `Sources`
paths that no longer exist in `raw/`. Then Claude:

1. **Auto-cleans structure** (no approval): deletes empty pages, fixes broken
   links, links or archives orphans, repairs format violations.
2. **Verifies conflicts with you**: contradictions and outdated facts are
   gathered into a single report; you approve the resolutions before they apply.
3. **Archives, never deletes, retired knowledge**: outdated pages get a
   `**Status**: Superseded by [[...]]` (or `Archived`) field plus a top-of-page
   banner, so `raw/` stays immutable and every read is routed to the current
   page. Archived pages are excluded from future orphan/stale checks and tagged
   in `index.md`.
4. **Maintains logs**: appends a `## <date> (lint)` entry to `wiki/log.md` and
   updates `index.md`.
5. **Commits + pushes**: one commit per category, then `ingest.py --push-only`
   pushes them all (gated on `auto_push`).

`raw/` is never modified by lint.

### `/create-wiki`

Runs [bootstrap.py](skills/create-wiki/scripts/bootstrap.py) to lay out the
directory structure, copy templates (`CLAUDE.md`, `index.md`, `log.md`, page
template, `.wikirc.example.json`, `.gitignore`, `.claude/settings.json`), and
initialize git. Then prints marketplace install commands and a numbered
"Next steps" checklist.

## File layout of an LLM wiki

```
my-wiki/
├── .claude/settings.json     # pins the marketplace so /ingest and /lint auto-discover
├── .gitignore                # ignores .wikirc.json, .wiki-state/, tmp files
├── .wiki-state/              # git-ignored; volatile per-machine state
│   ├── last-fetched.json     # last-fetch timestamp + status per slug (single mode)
│   └── bulk-jobs/            # one directory per bulk-ingest job
│       └── <job-id>/queue.json  # discovery output + per-item status
├── .wikirc.json              # your endpoints + PATs (git-ignored)
├── .wikirc.example.json      # example config, committed
├── CLAUDE.md                 # wiki system prompt
├── raw/                      # immutable ingested sources
│   ├── <slug>.md             # rendered Markdown
│   ├── <slug>.source.json    # stable metadata: URL/path, title, content_sha256, image_hints (NO fetched_at)
│   └── images/<slug>/
│       ├── .manifest.json    # sha256 + source_url per image (source of truth for diffs and dedup)
│       ├── <n>.<ext>         # downloaded image bytes
│       └── <n>.md            # nano-banana-pro description
├── wiki/                     # Claude-maintained pages
│   ├── index.md
│   ├── log.md
│   └── <page>.md
└── templates/
    └── page.md
```

## Reference documents

- [skills/ingest/references/setup.md](skills/ingest/references/setup.md) —
  Step-by-step setup guide including offline install patterns.
- [skills/ingest/references/atlassian-api.md](skills/ingest/references/atlassian-api.md) —
  Confluence and Jira REST endpoints, PAT auth, storage-format tips.
- [skills/ingest/references/local-files.md](skills/ingest/references/local-files.md) —
  Per-format parsing notes (PDF, DOCX, HTML, images).
- [skills/ingest/references/page-format.md](skills/ingest/references/page-format.md) —
  Wiki page template and citation rules.

## Requirements

- Python 3.10 or newer
- `git` on PATH
- `bash`
- Access to your Confluence, Jira, and nano-banana-pro endpoints
- Personal Access Tokens for Confluence and Jira (either optional if you don't
  use that source)
