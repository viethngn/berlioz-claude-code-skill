---
name: create-wiki
description: |
  Bootstraps a fresh LLM wiki, end-to-end, with a single run and no manual
  setup: creates the folder layout (raw/, wiki/, templates/, .claude/), copies
  in a CLAUDE.md system prompt, a starter index.md and log.md, a page template,
  a .wikirc.example.json, a .gitignore, and a .claude/settings.json that pins
  this plugin's marketplace. Runs `git init`, auto-installs and verifies the
  Python dependencies (idempotent — skips install when already present), and
  auto-creates a ready-to-edit .wikirc.json. The only thing left for the user is
  to fill in the integration credentials they want.

  Use this skill whenever the user wants to create, bootstrap, initialize,
  scaffold, set up, or start a new LLM wiki. Trigger on phrases like:
  "create a new wiki", "bootstrap an LLM wiki", "set up a knowledge base",
  "initialize a wiki in ~/foo", "make a new wiki repo", or "scaffold a wiki".
---

# Create Wiki — LLM Wiki

Lay out a new LLM wiki with everything needed to start using `/ingest`
and `/lint` — in one run. `bootstrap.py` scaffolds the repo, installs and
verifies the Python dependencies (only if missing), and creates a
ready-to-edit `.wikirc.json`. The user's only remaining task is filling in
whichever integration credentials they want. Don't ask them to run
`install.sh` or `check-setup.sh` — that now happens automatically.

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
> I'll also `git init`, install + verify the Python dependencies (skipped
> if already present), and create a ready-to-edit `.wikirc.json`. After that
> the only thing left for you is to fill in the credentials for the
> integrations you want. Continue?

### Phase 2 — Run bootstrap.py

```bash
python3 "${SKILL_DIR}/scripts/bootstrap.py" \
  --target "${TARGET}" \
  --title "${TITLE:-My LLM Wiki}"
```

Optional flags:

- `--force` — allow bootstrap into a non-empty directory
- `--no-git` — skip `git init` (rare — user must confirm)
- `--skip-deps` — skip the automatic dependency install/verify (for CI or
  air-gapped setups where the user manages deps themselves)
- `--marketplace <path>` — pin a specific marketplace source path in
  `.claude/settings.json`; defaults to auto-detected

`bootstrap.py` runs the whole first-time setup itself: it scaffolds the repo,
creates `.wikirc.json`, then checks the Python dependencies (via the plugin's
`check-setup.sh`) and installs them (via `install.sh`) **only if something is
missing**, re-verifying afterward. Live install/verify progress streams to
stderr; the JSON summary is the only thing on stdout, so parse stdout:

```json
{
  "target": "/Users/.../my-wiki",
  "created": ["raw", "wiki", "templates", ".claude", "CLAUDE.md", ...],
  "marketplace": "/Users/.../berlioz-claude-code-skill",
  "config": { "created": true, "path": ".../.wikirc.json", "note": "..." },
  "deps":   { "ran": true, "installed": false, "ok": true, "note": "..." },
  "next_steps": ["...", "..."]
}
```

- `config.created` — `true` if a fresh `.wikirc.json` was written; `false` if
  one already existed (left untouched) or the example template was missing.
- `deps.ok` — `true` when dependencies are verified present, `false` when the
  install did not fully succeed (e.g. no PyPI access), `null` when
  `--skip-deps` was passed. `deps.installed` is `true` only when `install.sh`
  actually ran.

### Phase 3 — Report the result

Read `config` and `deps` from the JSON and tell the user plainly what happened.

- **Dependencies**: if `deps.ok` is `true`, confirm they're installed and
  verified. If `deps.ok` is `false`, surface the failure and point the user to
  [../ingest/references/setup.md](../ingest/references/setup.md) (offline
  mirror / wheels / proxy patterns) — the wiki is still scaffolded and usable
  once deps are fixed. If `deps.ran` is `false` because `bash` was unavailable,
  show the manual command from `deps.note`.
- **Config**: if `config.created` is `true`, tell the user `.wikirc.json` was
  created from the example. The one remaining manual step:

  > Edit `.wikirc.json` and fill in the integrations you want:
  > - **Confluence / Jira** — `*_base_url` + `*_pat`
  > - **nano-banana vision** — `nano_banana.base_url` + `api_key`
  > - **Slack** — `slack.token`
  >
  > Each is optional — leave one empty and that source type is simply skipped;
  > everything else still works.

  If `config.created` is `false` because a `.wikirc.json` already existed, say
  so and don't touch it.

If the plugin isn't installed as a marketplace plugin yet, also show the
`/plugin marketplace add <marketplace>` + `/plugin install
llm-wiki@berlioz-claude-code-skill` commands from `next_steps` (the wiki's
`.claude/settings.json` already pins the marketplace for in-directory use).

### Phase 4 — First-run guidance

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
- **Dependency install fails** (offline, private mirror, TLS proxy): the JSON
  reports `deps.ok = false`. The wiki is still fully scaffolded — surface the
  failure and point the user to
  [../ingest/references/setup.md](../ingest/references/setup.md) for offline /
  mirrored-network install patterns. Re-running `install.sh` after fixing
  connectivity completes setup.
- **`bash` not on PATH** (e.g. bare Windows): dependency automation is skipped
  with `deps.ran = false`; `deps.note` carries the manual `install.sh` command.
  Everything else (scaffold, `.wikirc.json`, git) still runs.
- **`.wikirc.json` already exists**: `bootstrap.py` leaves it untouched
  (`config.created = false`) — it never clobbers a file that may hold real
  credentials, even with `--force`.
