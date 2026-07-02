# LLM Wiki

Three skills for maintaining a personal LLM knowledge wiki, backed by git.

- **`/ingest`** — Pull content from a Confluence page, Jira issue, or local file
  (Markdown, HTML, PDF, DOCX, image). Extract embedded images, describe them via
  a nano-banana-pro-compatible endpoint **only when they change**, and update the
  wiki. Commits raw + wiki to git at the end of every run so the next ingest
  can diff against the last committed state.
- **`/lint`** — Audit the wiki for knowledge gaps, contradictions, orphaned
  pages, broken `[[wiki-links]]`, format violations, and stale facts. Emits a
  numbered report with suggested fixes and applies them with your approval.
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

1. `uv pip install -r requirements.txt --system` (if `uv` is available)
2. `python3 -m pip install --user -r requirements.txt`
3. `python3 -m pip install -r requirements.txt`

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
Personal Access Tokens.

```json
{
  "wiki_root": ".",
  "raw_dir": "raw",
  "wiki_dir": "wiki",
  "auto_commit": true,
  "atlassian": {
    "confluence_base_url": "https://your-confluence.example.com",
    "jira_base_url": "https://your-jira.example.com",
    "confluence_pat": "YOUR_CONFLUENCE_TOKEN_OR_EMPTY",
    "jira_pat": "YOUR_JIRA_TOKEN_OR_EMPTY",
    "verify_ssl": true
  },
  "nano_banana": {
    "base_url": "https://your-nano-banana-endpoint.example.com/v1/",
    "api_key": "YOUR_NANO_BANANA_API_KEY",
    "vision_model": "gemini-3-pro",
    "verify_ssl": true
  }
}
```

`.wikirc.json` is git-ignored by default. Only `.wikirc.example.json` is
committed.

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
6. Discuss key takeaways with you.
7. Create or update pages in `wiki/` following your `CLAUDE.md` rules
   (page format, `[[wiki-links]]`, citations, `wiki/index.md`, `wiki/log.md`).
8. `git add raw/ wiki/ && git commit -m "ingest: <slug> (N new, M changed images)"`
   when `auto_commit=true` — the commit is skipped automatically if nothing
   staged actually differs.

**Diff-gate summary:**

| Layer | Where | What it compares |
|-------|-------|------------------|
| 1. Source-file gate (local only) | `fetch_local.py` | SHA-256 of the raw file bytes; skips parsing PDFs/DOCX when unchanged |
| 2. Content-diff gate | `raw_store.write_raw_if_changed` | SHA-256 of the rendered Markdown; skips rewriting `raw/<slug>.md` + `raw/<slug>.source.json` when unchanged |
| 3. Image dedup gate | `extract_images.py` | Manifest lookup by `source_url`, then by SHA-256; prevents duplicate files |
| 4. Description gate | `image_manifest.py.classify()` | SHA-256 + presence of a description file; only new/changed images invoke nano-banana-pro |

Pass `--force` to `ingest.py` to bypass gates 1 and 2 for a full refresh.

### `/lint`

Runs [lint.py](skills/lint/scripts/lint.py) to build a JSON report of orphaned
pages, broken links, missing concept pages, format violations, and stale
pages. Then Claude reads the flagged pages, cross-checks for contradictions,
compares against `raw/` sources for outdated facts, presents a numbered list of
findings with suggested fixes, and applies them with your approval.

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
│   └── last-fetched.json     # last-fetch timestamp + status per slug
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
