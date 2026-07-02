---
name: create-wiki
description: |
  Bootstraps a fresh LLM wiki: creates the folder layout (raw/, wiki/,
  templates/, .claude/), copies in a CLAUDE.md system prompt, a starter
  index.md and log.md, a page template, a .wikirc.example.json,
  a .gitignore, and a .claude/settings.json that pins this plugin's
  marketplace. Runs `git init` and offers to install Python dependencies.

  Use this skill whenever the user wants to create, bootstrap, initialize,
  scaffold, set up, or start a new LLM wiki. Trigger on phrases like:
  "create a new wiki", "bootstrap an LLM wiki", "set up a knowledge base",
  "initialize a wiki in ~/foo", "make a new wiki repo", or "scaffold a wiki".
---

# Create Wiki — LLM Wiki

Lay out a new LLM wiki with everything needed to start using `/ingest`
and `/lint`.

## Required inputs

Ask upfront if not provided:

| Input | Format |
|-------|--------|
| Target directory | Absolute or `~`-relative path (default: cwd) |
| Wiki title (optional) | Free-form; used as the top-line H1 in `index.md` and `CLAUDE.md` |

The target directory must be empty (or non-existent) unless the user
explicitly asks to force-init on top of existing files.

## Workflow

### Phase 1 — Confirm inputs with the user

Repeat back the plan:

> I'll bootstrap an LLM wiki at `<target>` with:
>
> - `raw/` — for ingested sources
> - `wiki/` — Claude-maintained pages
> - `templates/page.md` — canonical page template
> - `CLAUDE.md` — wiki system prompt
> - `.wikirc.example.json` — config template
> - `.gitignore` — ignores `.wikirc.json` and other transient files
> - `.claude/settings.json` — pins this plugin's marketplace
>
> I'll also `git init` and print the commands to install the plugin +
> Python dependencies. Continue?

### Phase 2 — Run bootstrap.py

```bash
python3 "${SKILL_DIR}/scripts/bootstrap.py" \
  --target "${TARGET}" \
  --title "${TITLE:-My LLM Wiki}"
```

Optional flags:

- `--force` — allow bootstrap into a non-empty directory
- `--no-git` — skip `git init` (rare — user must confirm)
- `--marketplace <path>` — pin a specific marketplace source path in
  `.claude/settings.json`; defaults to auto-detected

The script prints a JSON summary on stdout:

```json
{
  "target": "/Users/.../my-wiki",
  "created": ["raw", "wiki", "templates", ".claude", "CLAUDE.md", ...],
  "marketplace": "/Users/.../berlioz-claude-code-skill",
  "next_steps": ["...", "..."]
}
```

### Phase 3 — Offer to install Python deps

If dependencies are not already installed, ask the user:

> Would you like me to also install the Python dependencies now?
> (`bash <marketplace>/plugins/llm-wiki/install.sh`)

If yes, run `install.sh`. If no, include the command in the next-steps
checklist shown to the user.

### Phase 4 — Print the next-steps checklist

Show the user:

```
Wiki bootstrapped at <target>.

Next steps:
  1. cd <target>
  2. cp .wikirc.example.json .wikirc.json
     Edit .wikirc.json with your Confluence, Jira, and nano-banana-pro
     endpoints and Personal Access Tokens.
  3. If you have not run it yet:
       bash <marketplace>/plugins/llm-wiki/install.sh
  4. Verify:
       bash <marketplace>/plugins/llm-wiki/check-setup.sh .wikirc.json
  5. Try:
       /ingest <URL-or-file>
  6. Add the marketplace and install the plugin globally (if not done):
       /plugin marketplace add <marketplace>
       /plugin install llm-wiki@berlioz-claude-code-skill
```

### Phase 5 — First-run guidance

Ask the user if they want to do a first ingest right away. If yes,
invoke the `/ingest` skill with their chosen source. If no, stop
here — everything is ready when they are.

## Edge cases

- **Target already exists and is non-empty**: `bootstrap.py` refuses to run
  without `--force`. Show the user the offending files and ask before
  passing `--force`.
- **Target is already a git repo**: `bootstrap.py` skips `git init` but
  still copies templates. Warn the user that untracked template files will
  appear in their working tree.
- **`.claude/settings.json` already exists**: `bootstrap.py` merges rather
  than overwrites — appends the marketplace source if missing, leaves
  other keys alone.
- **Marketplace path cannot be auto-detected**: `bootstrap.py` writes a
  placeholder in `.claude/settings.json` and warns the user to edit it
  manually.
