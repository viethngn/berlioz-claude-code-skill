# Berlioz Claude Code Skills

A collection of Claude Code plugins for Product Management, design, and knowledge-base workflows.

## Plugins

### `ad-suite-skills`

PM-focused PRD writing.

**Skills:**
- `prd-writer` — Creates PRDs covering background, user stories, user interaction & design, and ROI/RICE scoring. Technical architecture is excluded — that belongs to engineering.

### `nano-banana-pro`

Generate detailed, high-quality documentation images using Google's Gemini 3 Pro.

**Skills:**
- `generate` — Detailed diagrams, infographics, technical illustrations from long, descriptive prompts.

### `release-tools`

**Skills:**
- `release-note-writer` — Generates Slack-style release notes from a Confluence release manual page and its linked Jira tickets.

### `figma-prd-designer`

**Skills:**
- `figma-prd-designer` — Reads a Confluence PRD and builds Figma screen designs using the target file's existing design system.

### `llm-wiki`

Maintain a personal LLM knowledge wiki, backed by git.

**Skills:**
- `ingest` — Pull Confluence pages, Jira issues, or local files (MD/HTML/PDF/DOCX/images) into a wiki. Describes embedded images via a nano-banana-pro-compatible vision endpoint only when they change. Commits raw + wiki to git after every run.
- `lint` — Audit the wiki for gaps, contradictions, orphan pages, broken `[[links]]`, format violations, and stale facts.
- `create-wiki` — Bootstrap a fresh LLM wiki: folder layout, `CLAUDE.md`, page template, git repo, and marketplace pinning.

See [plugins/llm-wiki/README.md](plugins/llm-wiki/README.md) for the one-time setup steps.

## Installation

```
/plugin marketplace add <path-to-this-repo>
/plugin install <plugin-name>@berlioz-claude-code-skill
```
