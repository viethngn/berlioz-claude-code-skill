# Changelog

## 1.5.0 - 2026-07-31

### secretary

- Real Outlook connectivity, replacing the stub from 1.4.0. Corrected
  premise: the stub reused a separate app's (R-Musubi's) `{url, token}`
  settings pair, which turned out to never actually be read for Outlook
  specifically — R-Musubi itself spawns a bundled MCP server subprocess
  and ignores that pair. This release drops all R-Musubi involvement and
  installs the same underlying open-source project directly instead
  — R-Musubi is not part of the design at all.
- Added `connect-outlook`, a one-time setup skill: installs
  `outlook-local-mcp` (`github.com/desek/outlook-local-mcp`, MIT licensed)
  straight from upstream via `go install` (no Azure AD app registration
  needed — the project supports a shared/well-known client id), confirms
  it's connected, and walks through the interactive Microsoft device-code
  sign-in (the agent calling the server's own `account` MCP tool live in
  chat and relaying the code — Claude Code's own MCP OAuth handling only
  covers HTTP/SSE servers, not this kind of stdio server)
- The `secretary` plugin now ships its own `.mcp.json`, registering the
  `outlook` stdio server automatically the moment the plugin is enabled —
  no manual `claude mcp add` step. `scripts/outlook_mcp_server.py` is the
  launch wrapper: resolves the installed binary, hardcodes read-only
  security defaults (`OUTLOOK_MCP_READ_ONLY=true`,
  `OUTLOOK_MCP_MAIL_MANAGE_ENABLED=false` — not user-configurable, since
  this feature only ever needs to *read* mail/calendar to derive todos),
  and `exec`s straight into it
- The signed-in token cache lives at `~/.secretary/outlook/accounts.json`
  — a fixed, user-home-scoped path outside any git working tree entirely,
  since a Microsoft identity belongs to the person, not whichever project
  they're in. `sync.json`'s Outlook config simplifies to a single optional
  `enabled` flag (was `{url, token}`, which never did anything)
- `connectors/outlook.py`'s `not_configured`/`delegate` decision stays a
  cheap local file check (never a live `claude mcp` probe) so the
  automatic per-session-start sync path stays fast
- No new Python dependency — the OAuth/Graph work all happens inside the
  separate Go binary over MCP stdio; `secretary` stays 100% stdlib

## 1.4.0 - 2026-07-30

### secretary

- Fixed three bugs in the task-store engine:
  - A newline in any frontmatter field (e.g. a title copied from a chat
    message) corrupted the task file on write and made it unparseable on
    the next read — `parse_task_file` would silently skip it, dropping the
    task from every list/digest with only a stderr warning. `serialize_task`
    now collapses embedded newlines to spaces before writing.
  - `update_task` could never clear a field once set (`--due-date ""` was a
    no-op) — an empty string now means "clear," matching how every CLI flag
    already defaults to `None` for "leave alone."
  - `list --status done|archived --format text` always rendered empty
    because the renderer re-filtered rows to open statuses regardless of
    what the caller asked for; it now renders exactly the rows it's given.
- Added a reconciliation engine so synced tasks are never blindly
  duplicated: two new fields (`sourceRef`, `sourceHash`) and
  `upsert_from_source`/`tasks_cli.py upsert`, which match an incoming item
  to its existing task by `sourceRef` and update it in place (no-op if
  unchanged, and never resurrects an archived match) instead of creating a
  second task. Sync commits are tagged `sync-add`/`sync-update` in git, so
  `git log secretary/` is a readable change history.
- Added a connector framework (`scripts/connectors/{base,slack,outlook}.py`)
  and `scripts/sync.py`, folded into the existing `tasks` skill rather than
  a new one — both Slack and Outlook are checked automatically, every time,
  with no explicit "check Slack" request needed:
  - **Slack** (working): primary path is the Slack MCP tools already
    connected in-session (no token, no `llm-wiki` dependency); falls back
    to reusing `llm-wiki`'s `fetch_slack.py` when a `.wikirc.json` with a
    real token is present. Default window when unspecified: last 3 days,
    all messages (read and unread).
  - **Outlook** (stub, R-Musubi-aware): reads
    `~/Library/Application Support/R-Musubi/settings.json` and reuses its
    Outlook `url`/`token` the moment they're filled in there (R-Musubi
    stores that connection as a plain url+token pair, not a private
    keychain session); reports `not_configured` until then. Full Graph/
    OAuth remains out of scope — the future `connect-outlook` skill.
  - `secretary/sync.json` (optional, opt-in) configures channels/search,
    the sync window, and `autoSyncOnStart`; the `SessionStart` hook now
    also instructs an auto-sync at the start of every session when that
    flag is set, even if nothing is currently overdue.
  - Extraction (deciding what's an actionable item) and same-work-item
    matching stay model judgment in the `tasks` skill, propose-then-confirm
    before creating; only an exact `sourceRef` refresh of an already-known
    item applies silently.

## 1.3.0 - 2026-07-29

### secretary

- Added `secretary` plugin with two skills:
  - `create-secretary` — Bootstrap an *existing* project into a secretary agent: scaffolds `secretary/tasks/` (active), `secretary/archived/` (done/removed), and `secretary/index/index.md` (a maintained listing of every task, mirroring `llm-wiki`'s `raw/`/`wiki/` split); creates a `CLAUDE.md` if absent, or merges a managed, idempotent block into an existing one; merges a `SessionStart` hook and this marketplace's pin into `.claude/settings.json` without touching unrelated keys; always ensures the project is git-initialized and makes one scoped scaffold commit
  - `tasks` — CRUD + render for todos: add, update, mark done (moves to `archived/`), remove (archive, never hard-delete, with optional `--cascade` for subtasks), add subtasks via `--parent-id`, and render flat/tree views grouped Overdue → Due soon → Later. Every mutating action commits to git immediately, no batching, no confirmation prompt
- Ships a `SessionStart` hook (`scripts/due_soon.py`, matcher `startup|resume`) that surfaces overdue and due-soon tasks as injected context at the start of every session — silent when nothing is due or the project was never bootstrapped
- Stdlib-only Python throughout (no `requirements.txt`/`install.sh`); tasks use a flat, hand-parseable `key: value` frontmatter block (no YAML dependency), consistent with the repo's low-dependency norm outside `llm-wiki`
- Task `refs` reuse `llm-wiki`'s `[[wiki-link]]` syntax; the `tasks` skill soft-verifies ref targets against a sibling `wiki/` directory when present (no dependency on the `llm-wiki` plugin's code)
- Outlook/calendar integration is explicitly out of scope for this release — a future `connect-outlook` skill

## 1.2.0 - 2026-07-03

### llm-wiki

- `ingest` now handles both single-item and bulk ingest in one skill/one slash command; auto-detects from the source shape or explicit flags
  - New sources: `--space <KEY>` (Confluence space), `--cql "..."` (Confluence CQL), `--jql "..."` (Jira JQL), `--resume <job-id>` (continue a prior bulk job)
  - URLs like `.../spaces/<KEY>` (no `/pages/…`) and `.../display/<KEY>` (no page name) auto-route to bulk Confluence space mode
  - `.../pages/<N>`, `.../browse/<KEY>`, bare Jira keys, and existing file paths continue to auto-detect as single-item
  - The single-item workflow is unchanged; existing invocations behave the same
- Resumable job queues under `.wiki-state/bulk-jobs/<job-id>/queue.json` (git-ignored)
  - `discover.py` paginates the space/query and writes the queue; reuses an existing queue for the same (kind, query) unless `--replace` is passed
  - `prefetch.py` iterates pending items via subprocess into the existing single-item fetchers so all four diff gates fire per page; checkpoints after every item; circuit-breaks after 5 consecutive failures
  - `queue_admin.py list|show|reset|mark|delete` for inspecting jobs and re-queueing failures (named `queue_admin.py` rather than `queue.py` so it doesn't shadow the stdlib `queue` module for other scripts in the same directory)
  - Ctrl-C during prefetch is safe; `/ingest --resume <job-id>` continues where it left off and retries any items marked `failed`
- Rate limiting: shared `rate_limiter.py` token bucket + `Retry-After` + exponential backoff wraps every Atlassian and nano-banana HTTP call, keyed by API section
  - Config: `.wikirc.json` gains `atlassian.rate_limit_rps` / `.burst` / `.max_retries` / `.retry_base_delay_seconds` and the same keys under `nano_banana`
  - Defaults: Atlassian 2 rps / burst 5, nano-banana 1 rps / burst 2 — conservative enough for a first-time whole-space run
  - Single-item `/ingest` also honors these limits so multiple concurrent chats can't collectively blow the budget

## 1.1.0 - 2026-07-02

### llm-wiki

- Added `llm-wiki` plugin with three skills:
  - `ingest` — Pull Confluence/Jira/local content into a git-backed wiki; describe embedded images via a nano-banana-pro-compatible endpoint only when their SHA-256 hash changes; commit raw + wiki after every run
  - `lint` — Deterministic report (orphans, broken links, missing pages, format violations, stale pages) plus semantic checks (contradictions, outdated facts) applied with user approval
  - `create-wiki` — Bootstrap a new LLM wiki (folder layout, CLAUDE.md system prompt, git init, `.claude/settings.json` marketplace pinning)
- Four-layer diff gate makes `/ingest` idempotent when nothing changed:
  1. Source-file SHA-256 (local only) — skips PDF/DOCX parsing on match
  2. Rendered-Markdown SHA-256 — skips rewriting `raw/<slug>.md` and `raw/<slug>.source.json`; short-circuits the orchestrator when unchanged
  3. Image-manifest URL/hash reconciliation — no duplicate `raw/images/<slug>/N.png` files on re-ingest
  4. Image description gate — nano-banana-pro is called only for images whose bytes changed
- Volatile `fetched_at` timestamps moved from the git-tracked `source.json` into `.wiki-state/last-fetched.json`, which is git-ignored via the template `.gitignore`
- `ingest.py --force` bypasses gates 1 and 2 for a full refresh
- Ships `install.sh` and `check-setup.sh` utility scripts for one-time Python dependency setup
- Vendor-neutral: every endpoint is configured via `.wikirc.json`, no hardcoded URLs or product names

## 1.0.0 - 2026-05-20

### ad-suite-skills

- Added `prd-writer` skill: PM-focused PRD writing covering background, user stories, user interaction & design, and ROI/RICE scoring
