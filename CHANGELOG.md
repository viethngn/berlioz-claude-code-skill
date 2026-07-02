# Changelog

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
