# Changelog

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
